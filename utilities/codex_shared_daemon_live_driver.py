#!/usr/bin/env python3
"""Isolated native Codex daemon, update-popup, and queue-consumption probe.

This driver uses temporary homes and a loopback Responses fixture. It never reads
the user's Codex home or credentials, and tears down only fixture-owned resources.
"""
from __future__ import annotations

import fcntl
import hashlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tomllib
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
INSTALL_TOOLS = ROOT / "tools" / "install"
sys.path.insert(0, str(UTILITIES))
sys.path.insert(0, str(INSTALL_TOOLS))

import codex_tui_config  # noqa: E402
from detached_lifetime import bind as end_with_test  # noqa: E402

VERSION_RE = re.compile(r"codex-cli\s+(\d+\.\d+\.\d+(?:[-+][^\s]+)?)", re.I)
SID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
UPDATE_MARKERS = ("999.0.0", "update available", "new version of codex", "upgrade to")
OFFICIAL_SOURCES = {
    "config_reference": "https://developers.openai.com/codex/config-reference/",
    "daemon_startup": "https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/daemon_startup.rs",
    "update_check": "https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/updates.rs",
    "update_cache": "https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/updates_cache.rs",
    "daemon_lifecycle": "https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server-daemon/README.md",
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_text(path: Path, limit: int = 131072) -> str:
    try:
        return path.read_bytes()[:limit].decode("utf-8", "replace")
    except OSError:
        return ""


def _strip_terminal(text: str) -> str:
    text = ANSI_RE.sub("", text)
    return "".join(char for char in text if char in "\n\r\t" or char.isprintable())


def _hash_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


def _proc_start(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        return raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _proc_details(pid: int, fixture_home: Path) -> dict | None:
    """Read one PID supplied by this fixture's process tree or pid file."""
    try:
        proc = Path(f"/proc/{int(pid)}")
        raw_env = (proc / "environ").read_bytes().split(b"\0")
        env = dict(part.decode("utf-8", "replace").split("=", 1)
                   for part in raw_env if b"=" in part)
        if env.get("CODEX_HOME") != str(fixture_home):
            return None
        raw_argv = (proc / "cmdline").read_bytes().split(b"\0")
        argv = [part.decode("utf-8", "replace") for part in raw_argv if part]
        return {"pid": int(pid), "start": _proc_start(pid),
                "exe": os.readlink(proc / "exe"), "argv": argv,
                "fixture_codex_home": True}
    except (OSError, ValueError, TypeError):
        return None


def _children(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{int(pid)}/task/{int(pid)}/children").read_text(encoding="ascii")
        return [int(value) for value in raw.split()]
    except (OSError, ValueError):
        return []


def _descendants(roots: list[int], fixture_home: Path, *, limit: int = 128) -> list[dict]:
    todo = list(roots)
    seen = set()
    found = []
    while todo and len(seen) < limit:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        details = _proc_details(pid, fixture_home)
        if details:
            found.append(details)
        todo.extend(child for child in _children(pid) if child not in seen)
    return found


def _pidfile_pid(codex_home: Path) -> tuple[int | None, str | None]:
    raw = _read_text(codex_home / "app-server-daemon" / "app-server.pid", 4096).strip()
    if not raw:
        return None, None
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if isinstance(value, dict):
        for key in ("pid", "process_id", "processId"):
            pid = value.get(key)
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                start = value.get("start", value.get("start_time", value.get("proc_start")))
                return pid, str(start) if start is not None else None
    match = re.search(r"(?<!\d)(\d{1,10})(?!\d)", raw)
    return (int(match.group(1)), None) if match else (None, None)


def _socket_info(codex_home: Path) -> dict:
    endpoint = codex_home / "app-server-control" / "app-server-control.sock"
    result = {"path": str(endpoint), "exists": False, "is_socket": False,
              "symlink": False, "target": None, "dev": None, "inode": None,
              "listener_inode": None}
    try:
        info = endpoint.lstat()
        result["exists"] = True
        result["symlink"] = stat.S_ISLNK(info.st_mode)
        result["target"] = os.readlink(endpoint) if result["symlink"] else str(endpoint)
        target = endpoint.resolve(strict=True) if result["symlink"] else endpoint
        target_info = target.stat()
        result["resolved_path"] = str(target)
        result["is_socket"] = stat.S_ISSOCK(target_info.st_mode)
        result["dev"] = target_info.st_dev
        result["inode"] = target_info.st_ino
        if result["is_socket"]:
            for line in Path("/proc/net/unix").read_text(encoding="utf-8").splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 8 and fields[7] == str(target):
                    result["listener_inode"] = fields[6]
                    break
    except (OSError, RuntimeError):
        pass
    return result


def _pid_has_socket(pid: int, inode: str | None) -> bool:
    if not inode:
        return False
    try:
        for entry in Path(f"/proc/{int(pid)}/fd").iterdir():
            try:
                if os.readlink(entry) == f"socket:[{inode}]":
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _find_json_pids(value) -> list[int]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"pid", "app_server_pid", "appServerPid", "process_id", "processId"}:
                if isinstance(child, int) and not isinstance(child, bool) and child > 0:
                    found.append(child)
            found.extend(_find_json_pids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_json_pids(child))
    return list(dict.fromkeys(found))


def _daemon_evidence(binary: str, fixture_env: dict, fixture_home: Path,
                     launched_roots: list[int]) -> dict:
    endpoint = _socket_info(fixture_home)
    pid, pidfile_start = _pidfile_pid(fixture_home)
    candidates = _descendants(launched_roots, fixture_home)
    if pid:
        details = _proc_details(pid, fixture_home)
        if details and (pidfile_start is None or details.get("start") == pidfile_start):
            candidates.append(details)
    command = [binary, "app-server", "daemon", "version"]
    try:
        version = subprocess.run(command, env=fixture_env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=8, check=False)
        try:
            version_json = json.loads(version.stdout)
        except ValueError:
            version_json = None
        version_result = {"argv": command, "exit": version.returncode,
                          "json": version_json, "stdout": version.stdout[:3000],
                          "stderr": version.stderr[:1000]}
    except (OSError, subprocess.SubprocessError) as exc:
        version_json = None
        version_result = {"argv": command, "error": type(exc).__name__}
    for candidate_pid in _find_json_pids(version_json):
        details = _proc_details(candidate_pid, fixture_home)
        if details:
            candidates.append(details)
    candidates = list({(item["pid"], item.get("start")): item for item in candidates}.values())
    listener_inode = endpoint.get("listener_inode")
    owners = [item for item in candidates if _pid_has_socket(item["pid"], listener_inode)]
    daemon = owners[0] if len(owners) == 1 else None
    if daemon is None and pid:
        daemon = next((item for item in candidates if item["pid"] == pid), None)
    return {"socket": endpoint, "pidfile": str(fixture_home / "app-server-daemon" / "app-server.pid"),
            "pidfile_pid": pid, "pidfile_start": pidfile_start,
            "process_candidates": candidates, "listener_owners": owners,
            "daemon_process": daemon, "daemon_version": version_result}


def _trace_info(paths: list[Path], socket_path: str | None) -> dict:
    successful = []
    tui_pids = set()
    connect_lines = []
    external_connects = []
    for path in paths:
        raw = _read_text(path, 262144)
        for line in raw.splitlines():
            match = re.match(r"\s*(?:\[pid\s+(\d+)\]|(\d+))\s+(.*)", line)
            if not match:
                continue
            pid = int(match.group(1) or match.group(2))
            body = match.group(3)
            if "execve(" in body and "--no-alt-screen" in body and "codex" in body:
                tui_pids.add(pid)
            if "connect(" in body:
                connect_lines.append({"pid": pid, "line": body[:1200]})
            if "connect(" in body and "sun_path=" in body and "= 0" in body:
                successful.append({"pid": pid, "line": body[:1200]})
            if "connect(" in body and ("AF_INET" in body or "AF_INET6" in body):
                address = re.search(r'inet_addr\("([^"]+)"\)', body)
                if address is None:
                    address = re.search(r'inet_pton\(AF_INET6,\s*"([^"]+)"\)', body)
                if address:
                    try:
                        if not ipaddress.ip_address(address.group(1)).is_loopback:
                            external_connects.append({"pid": pid, "address": address.group(1),
                                                      "line": body[:800]})
                    except ValueError:
                        external_connects.append({"pid": pid, "address": "unparsed",
                                                  "line": body[:800]})
    shared = [row for row in successful
              if socket_path and (socket_path in row["line"]
                                  or "app-server-control.sock" in row["line"])]
    return {"trace_files": [str(path) for path in paths],
            "trace_available": any(path.is_file() and path.stat().st_size > 0 for path in paths),
            "tui_pids": sorted(tui_pids), "successful_unix_connects": successful[:100],
            "shared_daemon_connects": shared[:40], "external_connect_attempts": external_connects[:40],
            "connect_excerpt": connect_lines[-120:]}


class LocalResponsesFixture:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_GET(self):
                if self.path.rstrip("/") == "/v1/models":
                    payload = json.dumps({"object": "list", "data": [{
                        "id": "hearting-fixture", "object": "model", "created": 0,
                        "owned_by": "hearting-fixture"}]}, separators=(",", ":")).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(min(length, 4 * 1024 * 1024))
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {}
                with owner.lock:
                    owner.requests.append({"path": self.path, "model": payload.get("model"),
                                           "request_sha256": hashlib.sha256(raw).hexdigest(),
                                           "at": _utc_now()})
                if self.path.rstrip("/") != "/v1/responses":
                    self.send_error(404)
                    return
                response_id = "resp_fixture_" + uuid.uuid4().hex
                item_id = "msg_fixture_" + uuid.uuid4().hex
                answer = "OK"
                output_item = {"id": item_id, "type": "message", "role": "assistant",
                               "status": "completed",
                               "content": [{"type": "output_text", "text": answer, "annotations": []}]}
                events = [
                    {"type": "response.created", "response": {"id": response_id,
                     "object": "response", "created_at": int(time.time()),
                     "status": "in_progress", "model": "hearting-fixture", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {"id": item_id, "type": "message", "role": "assistant",
                              "status": "in_progress", "content": []}},
                    {"type": "response.output_text.delta", "item_id": item_id,
                     "output_index": 0, "content_index": 0, "delta": answer},
                    {"type": "response.output_text.done", "item_id": item_id,
                     "output_index": 0, "content_index": 0, "text": answer},
                    {"type": "response.output_item.done", "output_index": 0, "item": output_item},
                    {"type": "response.completed", "response": {"id": response_id,
                     "object": "response", "created_at": int(time.time()),
                     "status": "completed", "model": "hearting-fixture",
                     "output": [output_item],
                     "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                ]
                body = b"".join((b"event: " + event["type"].encode() + b"\n"
                                 + b"data: " + json.dumps(event, separators=(",", ":")).encode()
                                 + b"\n\n") for event in events) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except OSError:
                    return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LocalRejectProxy:
    """Reject external HTTP(S) traffic while allowing NO_PROXY loopback fixtures."""
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _reject(self):
                with owner.lock:
                    owner.requests.append({"method": self.command, "target": self.path,
                                           "at": _utc_now()})
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def do_CONNECT(self):
                self._reject()

            def do_GET(self):
                self._reject()

            def do_POST(self):
                self._reject()

            def do_PUT(self):
                self._reject()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FixtureHome:
    def __init__(self, root: Path, label: str, provider_url: str, update_value: bool,
                 base_env: dict, *, hooks: bool = False):
        self.root = root / label
        self.root.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.workspace = self.root / "workspace"
        self.state = self.root / "state"
        self.runtime = self.root / "runtime"
        self.config = self.root / "xdg-config"
        self.cache = self.root / "xdg-cache"
        self.tmp = self.root / "tmp"
        self.trace_dir = self.root / "traces"
        self.jobs = self.root / "completion" / "jobs.log"
        for path in (self.home, self.codex_home, self.workspace, self.state, self.runtime,
                     self.config, self.cache, self.tmp, self.trace_dir,
                     self.root / "artifacts", self.root / "peer-ledger", self.jobs.parent):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
        # Codex 0.163 validates this directory before publishing its local
        # app-server control socket. Seed it with the required private mode.
        for path in (self.codex_home / "app-server-control",
                     self.codex_home / "app-server-daemon"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        subprocess.run(["git", "init", "-q", str(self.workspace)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.env = dict(base_env)
        self.env.update({
            "HOME": str(self.home), "CODEX_HOME": str(self.codex_home),
            "CODEX_SQLITE_HOME": str(self.codex_home),
            "XDG_STATE_HOME": str(self.state), "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_CONFIG_HOME": str(self.config), "XDG_CACHE_HOME": str(self.cache),
            "TMPDIR": str(self.tmp), "AGENT_HOME": str(ROOT),
            "AGENT_RUNTIME_ROOT": str(ROOT),
            "AGENT_RUNTIME_IDENTITY": "direct:codex-native-fixture",
            "AGENT_ARTIFACT_ROOT": str(self.root / "artifacts"),
            "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-ledger"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_HERDR_SESSION": "codex-native-fixture",
            "HERDR_SESSION": "codex-native-fixture",
            "HERDR_SOCKET_PATH": str(self.root / "herdr" / "herdr.sock"),
            "TERM": "xterm-256color", "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_NATIVE_TEST_TRACE_DIR": str(self.trace_dir),
        })
        self._write_config(provider_url, update_value)
        self._write_update_cache()
        self.hooks_path = None
        self.observer_path = None
        self.callback_log = self.root / "native-userprompt-callback.jsonl"
        if hooks:
            self._write_observer_hook()

    def _write_config(self, provider_url: str, update_value: bool):
        prefix = "check_for_update_on_startup = true\n" if update_value else ""
        prefix = codex_tui_config.merge_startup_update_default_text(prefix)
        project_key = json.dumps(str(self.workspace), ensure_ascii=False)
        text = (prefix
                + 'model = "hearting-fixture"\n'
                + 'model_provider = "hearting_fixture"\n'
                + 'approval_policy = "never"\n'
                + 'sandbox_mode = "workspace-write"\n'
                + '\n[model_providers.hearting_fixture]\n'
                + 'name = "Hearting native test fixture"\n'
                + f'base_url = {json.dumps(provider_url)}\n'
                + 'wire_api = "responses"\n'
                + 'requires_openai_auth = false\n'
                + 'supports_websockets = false\n'
                + 'request_max_retries = 0\n'
                + 'stream_max_retries = 0\n'
                + f'\n[projects.{project_key}]\ntrust_level = "trusted"\n'
                + '\n[tui]\nstatus_line = ["thread-id"]\n')
        tomllib.loads(text)
        (self.codex_home / "config.toml").write_text(text, encoding="utf-8")

    def _write_update_cache(self):
        value = {"latest_version": "999.0.0", "last_checked_at": _utc_now(),
                 "dismissed_version": None}
        (self.codex_home / "version.json").write_text(
            json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")

    def _write_observer_hook(self):
        self.observer_path = self.root / "observe-userprompt.py"
        self.observer_path.write_text(
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            "import hashlib\n"
            "payload = sys.stdin.buffer.read()\n"
            "payload_hash = hashlib.sha256(payload).hexdigest()\n"
            "log = Path(os.environ['AGENT_CODEX_NATIVE_TEST_CALLBACK_LOG'])\n"
            "log.parent.mkdir(parents=True, exist_ok=True)\n"
            "with log.open('ab') as stream:\n"
            "    stream.write(json.dumps({'phase':'entered','payload_sha256':payload_hash,'payload':payload.decode('utf-8','replace')}, ensure_ascii=False).encode()+b'\\n')\n"
            "command = [str(Path(os.environ['AGENT_HOME'])/'adapters/codex/hooks/run-hook.sh'), 'userprompt-lifecycle.py']\n"
            "done = subprocess.run(command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "sys.stdout.buffer.write(done.stdout)\n"
            "sys.stderr.buffer.write(done.stderr)\n"
            "with log.open('ab') as stream:\n"
            "    stream.write(json.dumps({'phase':'returned','payload_sha256':payload_hash,'exit':done.returncode,'stderr':done.stderr.decode('utf-8','replace')[:1200]}, ensure_ascii=False).encode()+b'\\n')\n"
            "raise SystemExit(done.returncode)\n", encoding="utf-8")
        hooks_source = self.root / "hooks.json"
        hook_command = "python3 " + shlex.quote(str(self.observer_path))
        hooks_source.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command", "command": hook_command, "timeout": 30}]}]}}), encoding="utf-8")
        (self.codex_home / "hooks.json").symlink_to(hooks_source)
        self.hooks_path = hooks_source
        self.env["AGENT_CODEX_NATIVE_TEST_CALLBACK_LOG"] = str(self.callback_log)

    def trust_userprompt_hook(self, binary: str) -> dict:
        if self.hooks_path is None:
            return {"status": "not-configured"}
        helper_code = (
            "import json,sys; from pathlib import Path; "
            f"sys.path.insert(0,{str(INSTALL_TOOLS)!r}); import codex_hook_trust; "
            "result=codex_hook_trust.record_managed_trust(Path(sys.argv[1]),Path(sys.argv[2]),None,"
            "command=[sys.argv[3],'app-server','--listen','stdio://']); "
            "print(json.dumps(result,ensure_ascii=False,default=str))"
        )
        command = [sys.executable, "-B", "-c", helper_code,
                   str(self.codex_home), str(self.hooks_path), binary]
        try:
            done = subprocess.run(command, env=self.env, cwd=self.workspace, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=30, check=False)
            try:
                result = json.loads(done.stdout)
            except ValueError:
                result = {"stdout": done.stdout[:2000]}
            return {"argv": command, "exit": done.returncode, "result": result,
                    "stderr": done.stderr[:1500]}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"argv": command, "error": type(exc).__name__}


class TuiProcess:
    def __init__(self, binary: str, fixture: FixtureHome, trace_path: Path | None = None,
                 extra_args: tuple[str, ...] = ()):
        self.fixture = fixture
        self.trace_path = trace_path or fixture.trace_dir / ("tui-" + uuid.uuid4().hex + ".strace")
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 140, 0, 0))
        except OSError:
            pass
        strace = shutil.which("strace")
        native = [binary, "--no-alt-screen", *extra_args]
        command = ([strace, "-f", "-qq", "-s", "512", "-e", "trace=connect,execve",
                    "-o", str(self.trace_path), "--", *native] if strace else native)
        self.argv = command
        self.proc = subprocess.Popen(command, cwd=fixture.workspace, env=fixture.env,
                                      stdin=slave, stdout=slave, stderr=slave,
                                      start_new_session=True, close_fds=True)
        end_with_test(self.proc)
        os.close(slave)
        self.master = master
        self.raw = bytearray()

    def read_for(self, seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                ready, _, _ = select.select([self.master], [], [], 0.15)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if chunk:
                    self.raw.extend(chunk)
                    if len(self.raw) > 250000:
                        del self.raw[:-250000]
        return _strip_terminal(self.raw.decode("utf-8", "replace"))

    def send(self, text: str):
        try:
            os.write(self.master, text.encode("utf-8"))
        except OSError:
            pass

    def stop(self):
        if self.proc.poll() is None:
            for sig, timeout in ((signal.SIGINT, 2.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.proc.wait(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            os.close(self.master)
        except OSError:
            pass
        return self.proc.returncode


def _recent_rollout(codex_home: Path) -> dict | None:
    root = codex_home / "sessions"
    found = []
    try:
        for path in root.rglob("rollout-*.jsonl"):
            match = SID_RE.search(path.name)
            if match:
                try:
                    info = path.stat()
                except OSError:
                    continue
                found.append((info.st_mtime_ns, path, match.group(1)))
    except OSError:
        return None
    if not found:
        return None
    _stamp, path, sid = max(found, key=lambda item: item[0])
    return {"session_id": sid, "rollout_path": str(path),
            "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}


def _wait_rollout(codex_home: Path, timeout: float = 10.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _recent_rollout(codex_home)
        if found:
            return found
        time.sleep(0.1)
    return None


def _update_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in UPDATE_MARKERS)


def _run_json_helper(env: dict, code: str, args: list[str], *, timeout=30) -> dict:
    command = [sys.executable, "-B", "-c", code, *args]
    try:
        done = subprocess.run(command, env=env, cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False)
        try:
            payload = json.loads(done.stdout.splitlines()[-1])
        except (IndexError, ValueError):
            payload = {"stdout": done.stdout[:3000]}
        return {"argv": command, "exit": done.returncode, "result": payload,
                "stderr": done.stderr[:2000]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"argv": command, "error": type(exc).__name__}


def _send_completion_once(env: dict, jobs: Path, queue_socket: Path, receipt: dict,
                          attempt: str, session_id: str) -> dict:
    code = (
        "import importlib.util,json,sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0,{str(UTILITIES)!r})\n"
        "spec=importlib.util.spec_from_file_location('codex_managed_completion_fixture',"
        f"{str(UTILITIES/'codex-managed-completion.py')!r})\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name]=module\n"
        "spec.loader.exec_module(module)\n"
        "from types import SimpleNamespace\n"
        "jobs=Path(sys.argv[1]); queue=Path(sys.argv[2]); receipt=json.loads(sys.argv[3])\n"
        "attempt=sys.argv[4]; sid=sys.argv[5]\n"
        "args=SimpleNamespace(queue_socket=queue,thread_id=sid,jobs=jobs,timeout=20.0,"
        "interval=0.05,delivery_retry_interval=0.05,"
        "sealed_batch_id=receipt.get('sealed_batch_id'))\n"
        "try:\n"
        "    result=module._native_queue_delivery_once(args,receipt,{attempt})\n"
        "except Exception as exc:\n"
        "    result={'status':'error','reason':type(exc).__name__+':'+str(exc)}\n"
        "print(json.dumps(result,ensure_ascii=False,default=str))\n"
    )
    courier = _run_json_helper(env, code, [str(jobs), str(queue_socket), json.dumps(receipt),
                                           attempt, session_id], timeout=30)
    value = courier.get("result") if isinstance(courier.get("result"), dict) else {}
    return {**courier, "client_user_message_id": value.get("client_user_message_id"),
            "courier_status": value.get("status")}


def _queue_observation(env: dict, queue_socket: Path, session_id: str, client_id: str) -> dict:
    code = (
        "import json,sys; from pathlib import Path; "
        f"sys.path.insert(0,{str(UTILITIES)!r}); import codex_queue_delivery as q; "
        "path=Path(sys.argv[1]); sid=sys.argv[2]; client=sys.argv[3]; "
        "items=q.list_queue(path,sid,timeout=5); "
        "pending=[item for item in items if item.get('clientUserMessageId')==client]; "
        "turn=q.find_turn_by_client_message_id(path,sid,client,timeout=5); "
        "print(json.dumps({'items':pending,'turn':turn},ensure_ascii=False,default=str))"
    )
    return _run_json_helper(env, code, [str(queue_socket), session_id, client_id], timeout=15)


def _pending_record(root: Path, session_id: str, delivery_id: str) -> dict | None:
    if not delivery_id:
        return None
    try:
        import dispatch_pending_delivery
        return dispatch_pending_delivery.read(root, session_id, delivery_id)
    except Exception:
        return None


def _stop_fixture_daemon(binary: str, fixture_env: dict, codex_home: Path) -> dict:
    pid, start = _pidfile_pid(codex_home)
    owned = _proc_details(pid, codex_home) if pid else None
    if not owned or (start is not None and owned.get("start") != start):
        return {"status": "no-verified-daemon-pid", "pidfile_pid": pid}
    command = [binary, "app-server", "daemon", "stop"]
    try:
        result = subprocess.run(command, env=fixture_env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=12, check=False)
        command_result = {"argv": command, "exit": result.returncode,
                          "stdout": result.stdout[:1500], "stderr": result.stderr[:1000]}
    except (OSError, subprocess.SubprocessError) as exc:
        command_result = {"argv": command, "error": type(exc).__name__}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _proc_start(pid) == owned.get("start"):
        time.sleep(0.05)
    if _proc_start(pid) == owned.get("start"):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _proc_start(pid) == owned.get("start"):
            time.sleep(0.05)
    return {"status": "stopped" if _proc_start(pid) != owned.get("start") else "still-live",
            "pid": pid, "start": owned.get("start"), "command": command_result}


def _build_base_env(root: Path, proxy_url: str) -> dict:
    keep = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL")
    env = {key: os.environ[key] for key in keep if os.environ.get(key)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("TERM", "xterm-256color")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(root / "tmp")
    env.update({
        "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url, "https_proxy": proxy_url,
        "ALL_PROXY": proxy_url, "all_proxy": proxy_url,
        "NO_PROXY": "127.0.0.1,localhost,::1,[::1]",
        "no_proxy": "127.0.0.1,localhost,::1,[::1]",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _write_codex_trace_wrapper(bin_dir: Path, binary: str):
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    wrapper = bin_dir / "codex"
    strace = shutil.which("strace")
    if not strace:
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(binary) + " \"$@\"\n", encoding="utf-8")
    else:
        wrapper.write_text(
            "#!/bin/sh\nset -eu\n"
            "trace=$(mktemp \"$CODEX_NATIVE_TEST_TRACE_DIR/managed.XXXXXX\")\n"
            "exec " + shlex.quote(strace) + " -f -qq -s 512 -e trace=connect,execve -o \"$trace\" -- "
            + shlex.quote(binary) + " \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o700)
    return wrapper


def _start_private_herdr(binary: str, fixture):
    herdr = shutil.which("herdr")
    if not herdr:
        return None, {"status": "unsupported", "reason": "herdr-executable-unavailable"}
    try:
        from herdr_integration_live import HerdrIntegrationLive
    except Exception as exc:
        return None, {"status": "unsupported", "reason": "fixture-helper-import-" + type(exc).__name__}
    live = None
    try:
        args = SimpleNamespace(output_dir=str(fixture.root / "herdr-evidence"),
                               project=str(fixture.workspace), timeout=90, command_timeout=20,
                               case="codex-shared-daemon", herdr=herdr, codex=binary,
                               claude="claude", opencode="opencode")
        live = HerdrIntegrationLive(args)
        shell_home = Path(live.env["HOME"]).resolve(strict=False)
        shell_home.relative_to(live.runtime_root.resolve(strict=False))
        shell_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (shell_home / ".zshrc").write_text(
            "# Temporary Hearting native Codex fixture shell.\n", encoding="utf-8")
        for key in ("CODEX_HOME", "CODEX_SQLITE_HOME", "AGENT_HOME",
                    "AGENT_RUNTIME_ROOT", "AGENT_RUNTIME_IDENTITY",
                    "AGENT_ARTIFACT_ROOT", "AGENT_PEER_LEDGER_ROOT", "AGENT_DISPATCH_JOBS",
                    "AGENT_CODEX_NATIVE_TEST_CALLBACK_LOG", "CODEX_NATIVE_TEST_TRACE_DIR",
                    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
                    "GIT_ALLOW_PROTOCOL", "GIT_CONFIG_NOSYSTEM"):
            if key in fixture.env:
                live.env[key] = fixture.env[key]
        wrapper_bin = fixture.root / "bin"
        _write_codex_trace_wrapper(wrapper_bin, binary)
        live.env["PATH"] = str(wrapper_bin) + os.pathsep + live.env.get("PATH", "")
        live.env["HERDR_PANE_ID"] = ""
        if not live.start_server():
            commands = []
            try:
                commands = [json.loads(line) for line in live.commands_path.read_text(
                    encoding="utf-8").splitlines()][-8:]
            except (OSError, ValueError):
                pass
            startup = {"server_pid": live.server.pid if live.server else None,
                       "server_start": _proc_start(live.server.pid) if live.server else None,
                       "server_exit": live.server.poll() if live.server else None,
                       "commands": commands}
            live.cleanup_owned()
            live.runtime_dir.cleanup()
            return None, {"status": "unsupported", "reason": "private-herdr-server-unavailable",
                          "startup_evidence": startup}
        pane = live.create_pane("codex-daemon-fixture")
        if not pane:
            live.cleanup_owned()
            live.runtime_dir.cleanup()
            return None, {"status": "unsupported", "reason": "private-herdr-pane-unavailable"}
        fixture.env = live.env
        return live, {"status": "ready", "session": live.session, "pane": pane,
                      "socket": live.env.get("HERDR_SOCKET_PATH"),
                      "server_pid": live.server.pid if live.server else None,
                      "server_start": _proc_start(live.server.pid) if live.server else None,
                      "config_path": live.env.get("HERDR_CONFIG_PATH")}
    except Exception as exc:
        if live is not None:
            try:
                live.cleanup_owned()
                live.runtime_dir.cleanup()
            except Exception:
                pass
        return None, {"status": "unsupported", "reason": "private-herdr-" + type(exc).__name__}


def _start_managed_codex(live, name: str) -> dict:
    command = [sys.executable, "-B", str(UTILITIES / "peer-steward.py"),
               "start", name, "--kind", "codex", "--pane", live.panes[0],
               "--permission-mode", "inherit", "--", "--no-alt-screen"]
    try:
        result = subprocess.run(command, cwd=live.project, env=live.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=80, check=False)
        return {"argv": command, "exit": result.returncode,
                "stdout": result.stdout[:5000], "stderr": result.stderr[:1500],
                "started": "started=true" in result.stdout}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"argv": command, "error": type(exc).__name__, "started": False}


def _pane_read(live, pane: str) -> dict:
    rc, out, err = live.herdr_command("fixture-pane-read", [
        "pane", "read", pane, "--source", "recent-unwrapped", "--lines", "80", "--format", "text"],
        timeout=4)
    return {"exit": rc, "output": out[-12000:], "error": err[:1000]}


def _agent_read(live, name: str) -> dict:
    rc, out, err = live.herdr_command("fixture-agent-get", ["agent", "get", name], timeout=5)
    try:
        payload = json.loads(out)
    except ValueError:
        payload = None
    return {"exit": rc, "payload": payload, "stdout": out[:3000], "stderr": err[:1000]}


def _agent_thread_id(agent: dict) -> str | None:
    candidates = []
    def walk(value):
        if isinstance(value, dict):
            identity = value.get("agent_session")
            if isinstance(identity, dict) and isinstance(identity.get("value"), str):
                candidates.append(identity["value"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(agent.get("payload"))
    return next((sid for sid in candidates if re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", sid)), None)


def _send_pane_prompt(live, pane: str, prompt: str) -> dict:
    text_rc, text_out, text_err = live.herdr_command(
        "fixture-pane-send-text", ["pane", "send-text", pane, prompt], timeout=5)
    enter_rc, enter_out, enter_err = live.herdr_command(
        "fixture-pane-send-enter", ["pane", "send-keys", pane, "Enter"], timeout=5)
    return {"text_exit": text_rc, "enter_exit": enter_rc,
            "text_stdout": text_out[:1000], "text_stderr": text_err[:1000],
            "enter_stdout": enter_out[:1000], "enter_stderr": enter_err[:1000]}


def _wait_provider(provider: LocalResponsesFixture, before: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with provider.lock:
            count = len(provider.requests)
        if count > before:
            return count
        time.sleep(0.1)
    with provider.lock:
        return len(provider.requests)


def _wait_callback(log_path: Path, marker: str, timeout: float) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            rows = []
        entered = [row for row in rows if row.get("phase") == "entered"
                   and marker in str(row.get("payload", ""))]
        returned_hashes = {row.get("payload_sha256") for row in rows
                           if row.get("phase") == "returned" and row.get("exit") == 0}
        if any(row.get("payload_sha256") in returned_hashes for row in entered):
            return rows
        time.sleep(0.1)
    try:
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        return []


def _setup_completion_fixture(env: dict, jobs: Path, attempt: str, session_id: str) -> dict:
    code = (
        "import json,sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0,{str(UTILITIES)!r})\n"
        "import dispatch_completion_join,dispatch_contract\n"
        "jobs=Path(sys.argv[1]); attempt=sys.argv[2]; sid=sys.argv[3]\n"
        "jobs.parent.mkdir(parents=True,exist_ok=True,mode=0o700)\n"
        "metadata=('attempt_schema_version=2,dispatch_depth=1,transport=headless,'"
        "'execution_surface=registered-headless,registered_worker=1,'"
        "'fallback_hop=same-harness-headless,worker_type=stage,unit=qa/test,'"
        "'launch_claimed=1,launch_outcome=never-launched,'"
        "'parent_completion_delivery=codex-native-queue,'"
        "f'attempt_id={attempt},parent_sid={sid},harness=codex')\n"
        "jobs.write_text(f't\\topen\\t/fixture/repo\\t/fixture/worktree\\tfixture-node\\t{metadata}\\n',encoding='utf-8')\n"
        "closed=dispatch_contract.close_attempt_row(jobs,attempt,'dead-exact-pid')\n"
        "path=dispatch_completion_join.materialize_after_terminal_close(jobs,attempt) if closed else None\n"
        "if path is None or not Path(path).is_file():\n"
        "    result={'status':'unsupported','reason':'fixture-completion-receipt-unavailable',"
        "'closed':closed,'job_row':jobs.read_text(encoding='utf-8')}\n"
        "else:\n"
        "    payload=json.loads(Path(path).read_text(encoding='utf-8'))\n"
        "    result={'status':'ready','attempt_id':attempt,'receipt_path':str(path),"
        "'receipt':payload.get('receipt'),'delivery_id':payload.get('delivery_id'),"
        "'delivery_record':payload}\n"
        "print(json.dumps(result,ensure_ascii=False,default=str))\n"
    )
    result = _run_json_helper(env, code, [str(jobs), attempt, session_id], timeout=20)
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    return {**payload, "setup_command": result}


def _embedded_comparison(binary: str, base_env: dict, base: Path,
                         provider: LocalResponsesFixture) -> dict:
    fixture = FixtureHome(base, "embedded", provider.base_url, False, base_env)
    trace = fixture.trace_dir / "embedded.strace"
    tui = None
    try:
        tui = TuiProcess(binary, fixture, trace, extra_args=("--no-daemon",))
        transcript = tui.read_for(4)
        with provider.lock:
            before = len(provider.requests)
        tui.send("Reply exactly OK and do not use tools.\n")
        count = _wait_provider(provider, before, 10)
        tui.read_for(2)
        socket = _socket_info(fixture.codex_home)
        return {"status": "observed", "argv": tui.argv,
                "startup_transcript": transcript[-8000:],
                "provider_request_observed": count > before,
                "provider_request_count": count, "shared_socket": socket,
                "trace": _trace_info([trace], socket.get("resolved_path")),
                "default_queue_endpoint_available": socket.get("is_socket") is True}
    except Exception as exc:
        return {"status": "unsupported", "reason": type(exc).__name__}
    finally:
        if tui:
            tui.stop()


def _run() -> dict:
    binary = shutil.which("codex")
    if not binary:
        return {"schema": "codex-shared-daemon-live-v1", "status": "unsupported",
                "reason": "codex-cli-not-found", "official_sources": OFFICIAL_SOURCES}
    binary = str(Path(binary).resolve())
    base = Path(tempfile.mkdtemp(prefix="cda-", dir="/tmp" if Path("/tmp").is_dir() else None))
    base.chmod(0o700)
    proxy = LocalRejectProxy()
    provider = LocalResponsesFixture()
    try:
        base_env = _build_base_env(base, proxy.url)
        true_fixture = FixtureHome(base, "update-positive", provider.base_url, True, base_env)
        false_fixture = FixtureHome(base, "shared-daemon", provider.base_url, False, base_env, hooks=True)
    except Exception:
        provider.close()
        proxy.close()
        shutil.rmtree(base, ignore_errors=True)
        raise
    result = {
        "schema": "codex-shared-daemon-live-v1", "started_at": _utc_now(), "status": "unsupported",
        "binary": {"path": binary, "sha256": _hash_file(Path(binary))},
        "official_sources": OFFICIAL_SOURCES,
        "isolation": {"fixture_root": str(base), "credentials_copied": False,
                      "inherited_credentials_stripped": True,
                      "codex_homes": [str(true_fixture.codex_home), str(false_fixture.codex_home)],
                      "provider": provider.base_url, "provider_is_loopback": True,
                      "herdr_endpoint_fallback": False},
        "network_fence": {"proxy": proxy.url, "git_protocols": "file",
                          "external_http_rejected": True},
        "update_control": {}, "shared_daemon": {}, "completion": {},
        "embedded_comparison": {}, "cleanup": [],
    }
    true_tui = false_tui = plain_tui = None
    herdr_live = None
    try:
        version = subprocess.run([binary, "--version"], env=true_fixture.env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=8, check=False)
        version_text = (version.stdout or version.stderr).strip()
        version_match = VERSION_RE.search(version_text)
        version_number = version_match.group(1) if version_match else None
        result["binary"].update({"version_command_exit": version.returncode,
                                 "version_text": version_text, "version": version_number,
                                 "source_tag": "rust-v" + version_number if version_number else None,
                                 "platform": sys.platform})
        true_config = tomllib.loads((true_fixture.codex_home / "config.toml").read_text())
        false_config = tomllib.loads((false_fixture.codex_home / "config.toml").read_text())
        result["update_control"] = {
            "fixture_cache": json.loads((true_fixture.codex_home / "version.json").read_text()),
            "positive_config_value": true_config.get("check_for_update_on_startup"),
            "default_config_value": false_config.get("check_for_update_on_startup"),
            "normal_merge_used": codex_tui_config.merge_startup_update_default_text("")
                                   == "check_for_update_on_startup = false\n",
        }
        true_trace = true_fixture.trace_dir / "update-positive.strace"
        true_tui = TuiProcess(binary, true_fixture, true_trace)
        true_screen = true_tui.read_for(8)
        result["update_control"]["positive_control"] = {
            "transcript": true_screen[-12000:],
            "latest_version_visible": "999.0.0" in true_screen,
            "update_prompt_visible": _update_marker(true_screen),
            "exit": true_tui.proc.poll(),
        }
        true_tui.stop()
        true_tui = None
        result["cleanup"].append(_stop_fixture_daemon(binary, true_fixture.env,
                                                       true_fixture.codex_home))

        result["shared_daemon"]["hook_trust"] = false_fixture.trust_userprompt_hook(binary)
        herdr_live, herdr_info = _start_private_herdr(binary, false_fixture)
        result["shared_daemon"]["private_herdr"] = herdr_info
        if herdr_live is None:
            result["shared_daemon"]["managed_start"] = {
                "status": "unsupported", "reason": herdr_info.get("reason")}
            false_fixture.env["HERDR_SOCKET_PATH"] = str(false_fixture.root / "missing-herdr.sock")
            first_trace = false_fixture.trace_dir / "plain-first.strace"
            false_tui = TuiProcess(binary, false_fixture, first_trace)
            first_screen = false_tui.read_for(6)
            result["shared_daemon"]["plain_tui_transcript"] = first_screen[-12000:]
            first_roots = [false_tui.proc.pid]
            managed_name = None
            pane = None
        else:
            managed_name = "codex-daemon-fixture-" + uuid.uuid4().hex[:6]
            pane = herdr_live.panes[0]
            result["shared_daemon"]["managed_start"] = _start_managed_codex(herdr_live, managed_name)
            result["shared_daemon"]["managed_agent"] = _agent_read(herdr_live, managed_name)
            result["shared_daemon"]["managed_pane_initial"] = _pane_read(herdr_live, pane)
            result["shared_daemon"]["managed_update_prompt_visible"] = _update_marker(
                result["shared_daemon"]["managed_pane_initial"].get("output", ""))
            first_roots = [herdr_live.server.pid] if herdr_live.server else []
            managed_trace_files = sorted(false_fixture.trace_dir.glob("managed.*"))
            result["shared_daemon"]["managed_trace"] = _trace_info(
                managed_trace_files, _socket_info(false_fixture.codex_home).get("resolved_path"))
            first_roots.extend(result["shared_daemon"]["managed_trace"].get("tui_pids", []))

        socket_before = _socket_info(false_fixture.codex_home)
        daemon_before = _daemon_evidence(binary, false_fixture.env,
                                         false_fixture.codex_home, first_roots)
        result["shared_daemon"]["socket_before_second_tui"] = socket_before
        result["shared_daemon"]["daemon_before_second_tui"] = daemon_before

        plain_trace = false_fixture.trace_dir / "plain-second.strace"
        plain_tui = TuiProcess(binary, false_fixture, plain_trace)
        plain_screen = plain_tui.read_for(6)
        socket_after = _socket_info(false_fixture.codex_home)
        daemon_after = _daemon_evidence(binary, false_fixture.env,
                                        false_fixture.codex_home, first_roots + [plain_tui.proc.pid])
        result["shared_daemon"]["plain_second_tui_transcript"] = plain_screen[-12000:]
        result["shared_daemon"]["socket_after_second_tui"] = socket_after
        result["shared_daemon"]["daemon_after_second_tui"] = daemon_after
        traces = sorted(false_fixture.trace_dir.glob("*.strace"))
        result["shared_daemon"]["client_traces"] = _trace_info(
            traces, socket_after.get("resolved_path"))
        before_pid = (daemon_before.get("daemon_process") or {}).get("pid")
        after_pid = (daemon_after.get("daemon_process") or {}).get("pid")
        result["shared_daemon"]["same_daemon_pid_and_start"] = bool(
            before_pid and before_pid == after_pid
            and (daemon_before.get("daemon_process") or {}).get("start")
            == (daemon_after.get("daemon_process") or {}).get("start"))
        result["shared_daemon"]["same_socket_identity"] = bool(
            socket_before.get("is_socket") and socket_after.get("is_socket")
            and (socket_before.get("dev"), socket_before.get("inode"))
            == (socket_after.get("dev"), socket_after.get("inode")))
        negative_screen = (result["shared_daemon"].get("plain_tui_transcript", "")
                           if herdr_live is None else
                           result["shared_daemon"].get("managed_pane_initial", {}).get("output", ""))
        result["shared_daemon"]["positive_control_gates_negative"] = bool(
            result["update_control"]["positive_control"]["latest_version_visible"]
            and false_config.get("check_for_update_on_startup") is False
            and not _update_marker(negative_screen))

        if herdr_live is not None and pane is not None:
            before_requests = len(provider.requests)
            prompt_send = _send_pane_prompt(
                herdr_live, pane, "Reply exactly OK and do not call tools.")
            after_requests = _wait_provider(provider, before_requests, 15)
            agent_snapshot = _agent_read(herdr_live, managed_name)
            session_id = _agent_thread_id(agent_snapshot)
            rollout = _wait_rollout(false_fixture.codex_home, 5)
            session_id = session_id or (rollout or {}).get("session_id")
            result["shared_daemon"]["initial_prompt"] = {
                "send": prompt_send, "provider_request_observed": after_requests > before_requests,
                "provider_request_count": after_requests, "agent": agent_snapshot,
                "rollout": rollout, "pane_after_prompt": _pane_read(herdr_live, pane)}
            if session_id and socket_after.get("is_socket") and false_fixture.hooks_path:
                managed_pids = result["shared_daemon"]["managed_trace"].get("tui_pids", [])
                owned_tuis = [details for pid in managed_pids
                              if (details := _proc_details(pid, false_fixture.codex_home))
                              and "--no-alt-screen" in details.get("argv", [])]
                paused = owned_tuis[0] if len(owned_tuis) == 1 else None
                paused_ok = False
                if paused and paused.get("start"):
                    try:
                        os.kill(paused["pid"], signal.SIGSTOP)
                        paused_ok = True
                    except OSError:
                        paused_ok = False
                jobs = false_fixture.jobs
                attempt = "att-codex-native-fixture-" + uuid.uuid4().hex[:12]
                setup = _setup_completion_fixture(false_fixture.env, jobs, attempt, session_id)
                result["completion"]["fixture"] = setup
                if setup.get("status") == "ready":
                    receipt = setup.get("receipt") or {}
                    courier = _send_completion_once(
                        false_fixture.env, jobs, Path(socket_after["path"]), receipt,
                        attempt, session_id)
                    result["completion"]["courier"] = courier
                    result["completion"]["attempt_id"] = attempt
                    result["completion"]["delivery_id"] = setup.get("delivery_id")
                    result["completion"]["client_user_message_id"] = courier.get("client_user_message_id")
                    if courier.get("client_user_message_id"):
                        result["completion"]["queue_observation_before_resume"] = _queue_observation(
                            false_fixture.env, Path(socket_after["path"]), session_id,
                            courier["client_user_message_id"])
                    result["completion"]["pending_record_before_resume"] = _pending_record(
                        jobs.parent, session_id, setup.get("delivery_id") or "")
                    result["completion"]["tui_paused_during_queue_acceptance"] = paused_ok
                    if paused_ok:
                        try:
                            os.kill(paused["pid"], signal.SIGCONT)
                        except OSError:
                            pass
                    hook_rows = _wait_callback(false_fixture.callback_log, attempt, 25)
                    result["completion"]["callback_hook"] = hook_rows
                    result["completion"]["callback_exact_attempt_seen"] = any(
                        row.get("phase") == "entered"
                        and attempt in str(row.get("payload", "")) for row in hook_rows)
                    client_id = courier.get("client_user_message_id")
                    if client_id:
                        result["completion"]["queue_observation_after_resume"] = _queue_observation(
                            false_fixture.env, Path(socket_after["path"]), session_id, client_id)
                    result["completion"]["pending_record_after_callback"] = _pending_record(
                        jobs.parent, session_id, setup.get("delivery_id") or "")
                    result["completion"]["courier_path"] = (
                        "codex-managed-completion.py::_native_queue_delivery_once"
                        " -> codex_queue_delivery.send_at_least_once")
                    result["completion"]["peer_message_path_used"] = False
                    final_record = result["completion"]["pending_record_after_callback"]
                    result["completion"]["callback_ack_observed"] = bool(
                        result["completion"]["callback_exact_attempt_seen"]
                        and isinstance(final_record, dict)
                        and final_record.get("acked_by") == "codex-native-prompt:" + session_id)
                    before_observation = result["completion"].get(
                        "queue_observation_before_resume", {}).get("result", {})
                    queued_items = before_observation.get("items", []) if isinstance(before_observation, dict) else []
                    queue_acceptance_observed = bool(
                        client_id and (courier.get("courier_status") == "accepted"
                                       or any(item.get("clientUserMessageId") == client_id
                                              for item in queued_items if isinstance(item, dict))))
                    result["completion"]["queue_acceptance_observed"] = queue_acceptance_observed
                    after_observation = result["completion"].get(
                        "queue_observation_after_resume", {}).get("result", {})
                    native_turn = after_observation.get("turn") if isinstance(after_observation, dict) else None
                    result["completion"]["native_turn_observed"] = bool(
                        isinstance(native_turn, dict)
                        and any(isinstance(item, dict) and item.get("clientId") == client_id
                                for item in native_turn.get("items", [])))
                    result["completion"]["effect_claim"] = bool(
                        queue_acceptance_observed
                        and result["completion"]["native_turn_observed"]
                        and result["completion"]["callback_ack_observed"]
                    )
                else:
                    result["completion"]["status"] = "unsupported"
                if paused_ok and _proc_start(paused["pid"]) == paused.get("start"):
                    try:
                        os.kill(paused["pid"], signal.SIGCONT)
                    except OSError:
                        pass
            else:
                result["completion"]["status"] = "unsupported"
                result["completion"]["reason"] = "native-thread-hook-or-shared-socket-unavailable"
                result["completion"]["session_id"] = session_id
            result["shared_daemon"]["managed_agent_after_prompt"] = agent_snapshot
        else:
            result["completion"] = {"status": "unsupported",
                                    "reason": "private-herdr-managed-start-unavailable"}

        result["embedded_comparison"] = _embedded_comparison(binary, base_env, base, provider)
        with provider.lock:
            result["provider"] = {"requests": list(provider.requests), "loopback_only": True}
        with proxy.lock:
            result["network_fence"]["rejected_requests"] = list(proxy.requests)
        result["network_fence"]["external_connect_attempts"] = (
            result["shared_daemon"].get("client_traces", {}).get("external_connect_attempts", []))
        result["status"] = "observed" if (
            result["update_control"]["positive_control"]["latest_version_visible"]
            and result["shared_daemon"].get("positive_control_gates_negative")
            and result["shared_daemon"].get("same_daemon_pid_and_start")
            and result["shared_daemon"].get("same_socket_identity")
            and not result["network_fence"].get("external_connect_attempts")
        ) else "unsupported"
        result["status_detail"] = (
            "Native startup and daemon evidence are retained. Completion effect is claimed only "
            "when queue acceptance, an exact native turn, and the prompt-hook ACK are all present."
        )
    except Exception as exc:
        result["status"] = "unsupported"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        if plain_tui:
            result["cleanup"].append({"plain_tui_exit": plain_tui.stop()})
        if false_tui:
            result["cleanup"].append({"first_tui_exit": false_tui.stop()})
        if true_tui:
            result["cleanup"].append({"positive_tui_exit": true_tui.stop()})
        if herdr_live is not None:
            try:
                herdr_live.cleanup_owned()
                result["cleanup"].append({"private_herdr": "stopped",
                                          "cleanup": herdr_live.cleanup})
            except Exception as exc:
                result["cleanup"].append({"private_herdr": "cleanup-error",
                                          "error": type(exc).__name__})
            try:
                herdr_live.runtime_dir.cleanup()
            except OSError as exc:
                result["cleanup"].append({"private_herdr_runtime": type(exc).__name__})
        result["cleanup"].append(_stop_fixture_daemon(binary, false_fixture.env,
                                                       false_fixture.codex_home))
        result["cleanup"].append(_stop_fixture_daemon(binary, true_fixture.env,
                                                       true_fixture.codex_home))
        provider.close()
        proxy.close()
        result["finished_at"] = _utc_now()
        shutil.rmtree(base, ignore_errors=True)
    return result


def main() -> int:
    try:
        result = _run()
    except Exception as exc:
        result = {"schema": "codex-shared-daemon-live-v1", "status": "unsupported",
                  "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
                  "official_sources": OFFICIAL_SOURCES}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
