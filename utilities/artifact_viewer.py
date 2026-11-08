#!/usr/bin/env python3
"""Optional, foreground, loopback-only, read-only artifact viewer."""
from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import mimetypes
import os
import re
import socket
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote_to_bytes, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_nofollow as nofollow
from artifact_markdown import render_markdown

PORT = 8765
PROTOCOL = 'hearting-artifact-viewer/v1'
PROBE = '/__hearting_viewer__'
HIDDEN = frozenset(('_internal', '.runtime', '_scratch'))
CHUNK = 65536
MAX_MARKDOWN = 2 * 1024 * 1024
PAGE_SIZE = 200


def root_key(root):
    return hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()


def open_root(root):
    return nofollow.open_root(root)


def open_relative(root_fd, parts):
    """Return an already-authorized fd. Never reopen a validated pathname."""
    return nofollow.open_relative(root_fd, parts, HIDDEN)


def parse_target(raw):
    if not raw.startswith('/') or raw.startswith('//') or '\\' in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError
    path, sep, query = raw.partition('?')
    if '#' in path or re.search(r'%(?![0-9a-fA-F]{2})', path):
        raise ValueError
    trailing = path.endswith('/')
    segments = path.split('/')[1:]
    if trailing: segments = segments[:-1]
    decoded = []
    for part in segments:
        if not part: raise ValueError
        value = unquote_to_bytes(part).decode('utf-8', 'strict')
        if value in ('.', '..') or value in HIDDEN or '/' in value or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError
        decoded.append(value)
    return decoded, trailing, query if sep else ''


def _encoded(parts):
    return '/' + '/'.join(quote(part, safe='') for part in parts)


def _range(header, size):
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not size: raise ValueError
    a, b = match.groups()
    if not a:
        if not b or int(b) == 0: raise ValueError
        return max(0, size-int(b)), size-1
    start = int(a)
    end = min(int(b), size-1) if b else size-1
    if start >= size or end < start: raise ValueError
    return start, end


def _probe(root, port=PORT):
    connection = None
    try:
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.25)
        connection.request('GET', PROBE)
        response = connection.getresponse()
        data = response.read(4097)
        if response.status != 200 or len(data) > 4096: return False
        obj = json.loads(data.decode('utf-8'))
        return isinstance(obj, dict) and obj.get('protocol') == PROTOCOL and obj.get('root_key') == root_key(root)
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def viewer_url(root, target, port=PORT):
    """Return a URL only for an existing safe target on the same live viewer."""
    try:
        root = os.path.realpath(root)
        rel = os.path.relpath(os.path.abspath(target), root)
        parts = [] if rel == '.' else rel.split(os.sep)
        root_fd = open_root(root)
        try:
            fd = open_relative(root_fd, parts)
            try:
                directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)
        if not _probe(root, port): return None
        return f'http://127.0.0.1:{port}' + _encoded(parts) + ('/' if directory and parts else '')
    except (OSError, ValueError, RuntimeError):
        return None


def _translator():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools' / 'i18n'))
        import ui_text
        return ui_text.translator()
    except Exception:
        messages = {'viewer.start': 'Artifact viewer: {url}', 'viewer.busy': 'Port {port} is already in use by another service or root. Try hearting view --port 0 for a free port.',
                    'viewer.raw': 'View raw', 'viewer.too_large': 'This document is too large to render.',
                    'viewer.empty': 'No visible files.', 'viewer.next': 'Next', 'viewer.previous': 'Previous'}
        return lambda key, **kw: messages.get(key, key).format(**kw)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, root, port=PORT):
        self.root = os.path.realpath(root)
        self.root_fd = open_root(self.root)
        self.t = _translator()
        try:
            super().__init__(('127.0.0.1', port), ViewerHandler)
        except BaseException:
            # super().__init__ already ran server_close() on a bind failure.
            if self.root_fd is not None:
                os.close(self.root_fd); self.root_fd = None
            raise

    def server_close(self):
        super().server_close()
        if self.root_fd is not None:
            os.close(self.root_fd); self.root_fd = None

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = 'HeartingViewer'
    sys_version = ''

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def _error(self, code):
        self.send_response(code)
        if code == 405: self.send_header('Allow', 'GET, HEAD')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _host_ok(self):
        hosts = self.headers.get_all('Host', [])
        port = self.server.server_address[1]
        return len(hosts) == 1 and hosts[0] in (f'127.0.0.1:{port}', f'localhost:{port}')

    def _respond(self, data, content_type='text/html; charset=utf-8', status=200, sandbox=True):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if sandbox: self.send_header('Content-Security-Policy', 'sandbox allow-scripts allow-popups')
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler sends 501 for unknown methods; the viewer has
        # exactly two methods regardless of the spelling used on the wire.
        if code == 501:
            self._method()
            return
        super().send_error(code, message, explain)

    def _directory(self, fd, parts, query):
        try:
            args = parse_qs(query, keep_blank_values=True)
            offset = max(0, int(args.get('offset', ['0'])[0]))
        except (ValueError, OverflowError): offset = 0
        entries = []
        with os.scandir(fd) as iterator:
            for entry in iterator:
                if entry.name in HIDDEN: continue
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)): continue
                    if parts[:1] == ['campaigns'] and len(parts) == 2 and entry.name == 'cycles' and stat.S_ISDIR(mode):
                        try:
                            legacy_fd = open_relative(fd, ['cycles'])
                            try:
                                with os.scandir(legacy_fd) as legacy:
                                    for cycle in legacy:
                                        if cycle.name in HIDDEN: continue
                                        try:
                                            if stat.S_ISDIR(cycle.stat(follow_symlinks=False).st_mode):
                                                entries.append((cycle.name, True, 'cycles/' + quote(cycle.name, safe='')))
                                        except OSError: continue
                            finally: os.close(legacy_fd)
                        except OSError: pass
                    else:
                        entries.append((entry.name, stat.S_ISDIR(mode), quote(entry.name, safe='')))
                except OSError: continue
        entries.sort(key=lambda x: (not x[1], x[0].casefold(), x[0], x[2]))
        lines = ['<!doctype html><meta charset="utf-8"><title>Artifacts</title><h1>Artifacts</h1><ul>']
        if parts: lines.append('<li><a href="../">../</a></li>')
        for name, directory, href in entries[offset:offset+PAGE_SIZE]:
            suffix = '/' if directory else ''
            lines.append('<li><a href="' + href + suffix + '">' + html.escape(name) + suffix + '</a></li>')
        if not entries: lines.append('<li>' + html.escape(self.server.t('viewer.empty')) + '</li>')
        lines.append('</ul>')
        if offset: lines.append(f'<a href="?offset={max(0, offset-PAGE_SIZE)}">{html.escape(self.server.t("viewer.previous"))}</a> ')
        if offset+PAGE_SIZE < len(entries): lines.append(f'<a href="?offset={offset+PAGE_SIZE}">{html.escape(self.server.t("viewer.next"))}</a>')
        self._respond('\n'.join(lines).encode())

    def _file(self, fd, parts, query):
        size = os.fstat(fd).st_size
        name = parts[-1].lower()
        raw = parse_qs(query).get('raw') == ['1']
        markdown = name.endswith(('.md', '.markdown')) and not raw
        if markdown:
            if size > MAX_MARKDOWN:
                content = '<p>' + html.escape(self.server.t('viewer.too_large')) + '</p>'
            else:
                chunks = []
                remaining = MAX_MARKDOWN+1
                while remaining:
                    chunk = os.read(fd, min(CHUNK, remaining))
                    if not chunk: break
                    chunks.append(chunk); remaining -= len(chunk)
                data = b''.join(chunks)
                if len(data) > MAX_MARKDOWN:
                    content = '<p>' + html.escape(self.server.t('viewer.too_large')) + '</p>'
                else:
                    content = render_markdown(data.decode('utf-8', 'replace'), self.server.root)
            page = '<!doctype html><meta charset="utf-8"><a href="?raw=1">' + html.escape(self.server.t('viewer.raw')) + '</a>\n' + content
            self._respond(page.encode('utf-8')); return
        mime = 'text/plain; charset=utf-8' if raw else (mimetypes.guess_type(name)[0] or 'application/octet-stream')
        mime = {'.m4a': 'audio/mp4', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg'}.get(os.path.splitext(name)[1], mime) if not raw else mime
        start, end = 0, size-1
        range_header = self.headers.get('Range') if self.command == 'GET' else None
        if range_header is not None:
            try: start, end = _range(range_header, size)
            except ValueError:
                self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.send_header('Content-Length', '0'); self.end_headers(); return
        self.send_response(206 if range_header is not None else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(max(0, end-start+1)))
        if range_header is not None: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        # Any file may be interpreted as a document, including XML with an
        # XSL stylesheet and MIME types supplied by the host's mime database.
        # A response-wide policy avoids an incomplete active-MIME allowlist.
        self.send_header('Content-Security-Policy', 'sandbox allow-scripts allow-popups')
        self.end_headers()
        if self.command == 'HEAD': return
        os.lseek(fd, start, os.SEEK_SET)
        remaining = max(0, end-start+1)
        while remaining:
            data = os.read(fd, min(CHUNK, remaining))
            if not data: break
            self.wfile.write(data); remaining -= len(data)

    def _get(self):
        if not self._host_ok(): self._error(400); return
        try:
            # self.path collapses initial //; the wire request-target does not.
            raw = self.raw_requestline.decode('iso-8859-1').split()[1]
            parts, trailing, query = parse_target(raw)
        except (ValueError, UnicodeError, IndexError): self._error(404); return
        if parts == ['__hearting_viewer__'] and not trailing:
            data = json.dumps({'protocol': PROTOCOL, 'root_key': root_key(self.server.root)}, separators=(',', ':')).encode()
            self._respond(data, 'application/json', sandbox=False); return
        try:
            fd = open_relative(self.server.root_fd, parts)
        except OSError:
            self._error(404); return
        try:
            mode = os.fstat(fd).st_mode
            if stat.S_ISDIR(mode):
                if not trailing:
                    location = _encoded(parts) + '/' + (('?' + query) if query else '')
                    self.send_response(301); self.send_header('Location', location); self.send_header('Content-Length', '0'); self.end_headers(); return
                if not parts:
                    try: index = open_relative(fd, ['index.html'])
                    except OSError: index = None
                    if index is not None:
                        try:
                            if stat.S_ISREG(os.fstat(index).st_mode): self._file(index, ['index.html'], query); return
                        finally: os.close(index)
                self._directory(fd, parts, query)
            elif trailing:
                self._error(404)
            else:
                self._file(fd, parts, query)
        except (OSError, ValueError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            os.close(fd)

    do_GET = _get
    do_HEAD = _get

    def _method(self):
        if not self._host_ok(): self._error(400)
        else: self._error(405)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _method


def _port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer from 0 to 65535') from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be an integer from 0 to 65535')
    return port


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog='hearting view', description='Read-only local artifact viewer.')
    parser.add_argument('--port', type=_port_number, default=PORT,
                        help='loopback port (default: 8765; 0 chooses a free port)')
    try:
        port = parser.parse_args(args).port
    except SystemExit as exc:
        return exc.code
    script = Path(__file__).resolve().parent / 'artifact-root.sh'
    try:
        result = subprocess.run(['sh', str(script), os.getcwd()], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print('viewer: cannot resolve artifact root: ' + str(exc), file=sys.stderr); return 2
    if result.returncode:
        print(result.stderr, end='', file=sys.stderr); return result.returncode
    if not result.stdout.strip():
        print('viewer: artifact root resolver returned no path', file=sys.stderr); return 2
    root = os.path.realpath(result.stdout.strip())
    try:
        server = ViewerServer(root, port)
    except OSError as exc:
        if exc.errno == getattr(__import__('errno'), 'EADDRINUSE') and port and _probe(root, port):
            print(_translator()('viewer.start', url=f'http://127.0.0.1:{port}/'), flush=True); return 0
        print(_translator()('viewer.busy', port=port) if exc.errno == getattr(__import__('errno'), 'EADDRINUSE') else 'viewer: cannot open artifact root or listener', file=sys.stderr); return 2
    except RuntimeError as exc:
        print('viewer: ' + str(exc), file=sys.stderr); return 2
    print(server.t('viewer.start', url=f'http://127.0.0.1:{server.server_address[1]}/'), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
