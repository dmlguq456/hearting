#!/usr/bin/env python3
"""Bounded native Herdr integration evidence runner for an isolated test home.

This internal driver keeps durable evidence under ``--output-dir`` and uses a
short private temporary runtime to fit Unix socket limits. It never copies
credentials or persists prompt text, screen contents, or raw command output
in its evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child

ROOT = Path(__file__).resolve().parents[1]
MAX_TIMEOUT = 900
CASES = ("callbacks", "codex-async-local-skip", "codex-busy",
         "opencode-busy", "booked-seat", "events-lost")
RUNTIMES = ("claude", "codex", "opencode")
NATIVE_START_PROMPT = (
    "This is an isolated native-session startup probe. Reply with READY, then stop."
)
_SECRET_ENV = re.compile(r"(?:API[_-]?KEY|ACCESS[_-]?TOKEN|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.I)

_LIVE_PATH = Path(__file__).with_name("herdr_opencode_live.py")
_LIVE_SPEC = importlib.util.spec_from_file_location("herdr_opencode_live_helpers", _LIVE_PATH)
_LIVE = importlib.util.module_from_spec(_LIVE_SPEC)
sys.modules[_LIVE_SPEC.name] = _LIVE
_LIVE_SPEC.loader.exec_module(_LIVE)


class LiveRunError(RuntimeError):
    pass


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _result(value):
    return value.get("result") if isinstance(value, dict) else None


def _error_envelope(value):
    """Return a bounded native error code, including success-exit error envelopes."""
    if not isinstance(value, dict):
        return None
    for node in (value, value.get("result")):
        if not isinstance(node, dict):
            continue
        error = node.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            return str(code)[:80] if code else "error-envelope"
        if error:
            return "error-envelope"
    return None


def _native_status(value):
    """Extract only a typed native agent state; arbitrary JSON is not proof."""
    if not isinstance(value, dict) or _error_envelope(value):
        return None
    result = value.get("result") if isinstance(value.get("result"), dict) else value
    candidates = [result]
    for key in ("agent", "pane", "session"):
        child = result.get(key) if isinstance(result, dict) else None
        if isinstance(child, dict):
            candidates.append(child)
    for node in candidates:
        for key in ("agent_status", "state", "status"):
            status = node.get(key) if isinstance(node, dict) else None
            if isinstance(status, str) and status.lower() in {
                    "idle", "working", "blocked", "done", "unknown"}:
                return status.lower()
    return None


def _pane_ids(value):
    found = set()
    def visit(node):
        if isinstance(node, dict):
            pane = node.get("pane_id")
            if isinstance(pane, str) and pane:
                found.add(pane)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return found


def _session_id(value):
    result = _result(value)
    agent = result.get("agent") if isinstance(result, dict) else None
    session = agent.get("agent_session") if isinstance(agent, dict) else None
    sid = session.get("value") if isinstance(session, dict) else None
    return _usable_session_id(sid)


def _usable_session_id(sid):
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    if not sid or sid.casefold() in {
            "-", "unknown", "none", "null", "undefined", "n/a", "<sid>"}:
        return None
    return sid


_NATIVE_IDENTITY_MISSING = object()


def _native_agent_identity_reason(agent, *, name, runtime, pane, require_complete):
    """Check Herdr's reported agent tuple without guessing the SID's spelling."""
    if agent is _NATIVE_IDENTITY_MISSING:
        return "native-agent-tuple-missing" if require_complete else None
    if not isinstance(agent, dict):
        return "native-agent-tuple-malformed"

    for field, expected, label in (
            ("name", name, "name"),
            ("agent", runtime, "runtime"),
            ("pane_id", pane, "pane")):
        if field not in agent:
            if require_complete:
                return f"native-agent-{label}-missing"
        elif agent[field] != expected:
            return f"native-agent-{label}-mismatch"

    if "agent_session" not in agent:
        return "native-agent-session-missing" if require_complete else None
    session = agent["agent_session"]
    if not isinstance(session, dict):
        return "native-agent-session-malformed"

    if "agent" not in session:
        return "native-agent-session-runtime-missing"
    if session["agent"] != runtime:
        return "native-agent-session-runtime-mismatch"

    if "kind" not in session:
        return "native-agent-session-kind-missing"
    if session["kind"] != "id":
        return "native-agent-session-kind-mismatch"

    if "value" not in session:
        return "native-agent-session-id-missing"
    if _usable_session_id(session["value"]) is None:
        return "native-agent-session-id-unusable"
    return None


def _redact_argv(argv):
    """Keep exact safe argv while blanking URLs and free-form payloads."""
    raw = [str(item) for item in argv]
    sensitive_positions = set()
    for index in range(max(0, len(raw) - 1)):
        if raw[index:index + 2] == ["agent", "prompt"] and index + 3 < len(raw):
            sensitive_positions.add(index + 3)
        if raw[index:index + 2] != ["agent", "start"]:
            continue
        try:
            delimiter = raw.index("--", index + 2)
            kind = raw.index("--kind", index + 2, delimiter)
        except ValueError:
            continue
        if (kind + 1 < delimiter and raw[kind + 1] == "codex"
                and delimiter + 1 < len(raw)):
            sensitive_positions.add(delimiter + 1)
    out, redact_next = [], False
    payload_flags = {"--prompt", "--message", "--text", "--body", "--input"}
    for index, value in enumerate(raw):
        if index in sensitive_positions or redact_next:
            out.append("[redacted]")
            redact_next = False
        elif value in payload_flags:
            out.append(value)
            redact_next = True
        elif value in {"--attach-url", "--backend-url"}:
            out.append(value)
            redact_next = True
        elif "://" in value:
            out.extend(_LIVE._safe_argv([value]))
        else:
            out.append(value)
    return out


def _failure_reason(stderr, returncode, timed_out=False):
    if timed_out:
        return "command-timeout"
    text = (stderr or "").lower()
    if "sun_path" in text or "socket name length exceeds" in text:
        return "local-socket-path-too-long"
    if any(term in text for term in ("not logged in", "authentication", "unauthorized", "login required")):
        return "authentication-unavailable"
    if any(term in text for term in ("unknown command", "unsupported", "not supported")):
        return "native-surface-unsupported"
    if any(term in text for term in ("not found", "no such file", "executable unavailable")):
        return "executable-unavailable"
    return "exit-0" if returncode == 0 else f"exit-{returncode if returncode is not None else 'unknown'}"


class HerdrIntegrationLive:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output_dir).expanduser().resolve()
        self.project = Path(args.project).expanduser().resolve(strict=True)
        if not self.project.is_dir() or self.project.is_symlink():
            raise LiveRunError("scratch project must be an existing real directory")
        if self.project == ROOT or ROOT in self.project.parents:
            raise LiveRunError("scratch project must not be inside the Hearting source tree")
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session = "hearting-live-" + uuid.uuid4().hex[:12]
        self.case_dir = self.output / self.session
        self.case_dir.mkdir(mode=0o700)
        self.commands_path = self.case_dir / "commands.jsonl"
        self.events_path = self.case_dir / "events.jsonl"
        self.result_path = self.case_dir / "result.json"
        self.project_root = self.project
        # Evidence paths can exceed sun_path even when they are valid file paths.
        self.runtime_dir = tempfile.TemporaryDirectory(
            prefix="hhl-", dir="/tmp" if Path("/tmp").is_dir() else None)
        self.runtime_root = Path(self.runtime_dir.name)
        self.env = _LIVE.isolated_environment(self.runtime_root, self.project)
        self.env.update({
            "AGENT_HOME": str(ROOT),
            "CODEX_HOME": str(Path(self.env["HOME"]) / ".codex"),
            "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude"),
            # Herdr's upstream installer/status read this official home path.
            "OPENCODE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".config" / "opencode"),
            "AGENT_PEER_LEDGER_ROOT": str(Path(self.env["XDG_STATE_HOME"]) / "peer-ledger"),
            "HERDR_ENV": "1",
            "HERDR_SESSION": self.session,
            "AGENT_HERDR_SESSION": self.session,
            "HEARTING_HERDR_LIVE_ROOT": str(self.case_dir),
            "HEARTING_HERDR_LIVE_OBSERVATIONS": str(self.events_path),
        })
        for name in (self.env["CODEX_HOME"], self.env["CLAUDE_CONFIG_DIR"],
                     self.env["OPENCODE_CONFIG_DIR"]):
            Path(name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.herdr = shutil.which(args.herdr) if os.path.sep not in args.herdr else args.herdr
        self.native = {name: (shutil.which(getattr(args, name))
                              if os.path.sep not in getattr(args, name)
                              else getattr(args, name)) for name in RUNTIMES}
        self.started = time.monotonic()
        self.deadline = self.started + args.timeout
        self.salt = os.urandom(32)
        self.server = None
        self.panes = []
        self.transport = None
        self.decode_original = None
        self.event_capture = []
        self.cleanup = []
        self.attempts = []
        self.tidy_queue_ids = []
        self.tidy_session_ids = []
        self.tidy_processes = []
        self.result = {
            "schema": "hearting-herdr-integration-live-v1",
            "case": args.case,
            "outcome": "unavailable",
            "started_at": _utc_now(),
            "named_session": self.session,
            "scratch_project": str(self.project),
            "case_root": str(self.case_dir),
            "isolation": {"home": self.env["HOME"], "xdg_config": self.env["XDG_CONFIG_HOME"],
                          "xdg_state": self.env["XDG_STATE_HOME"], "codex_home": self.env["CODEX_HOME"],
                          "claude_config": self.env["CLAUDE_CONFIG_DIR"],
                          "opencode_config": self.env["OPENCODE_CONFIG_DIR"],
                          "runtime_root": str(self.runtime_root), "runtime_storage": "ephemeral",
                          "credentials_copied": False,
                          "pinned_helper_root": str(ROOT)},
            "source_protection": {"before": self._source_hashes()},
            "expected": {}, "observed": {}, "evidence_paths": [], "cleanup": self.cleanup,
        }

    @staticmethod
    def _source_hashes():
        protected = ("utilities/herdr_integration_live.py", "utilities/herdr_opencode_live.py",
                     "tools/install/runtime_activation.py", "tools/install/herdr_integration.py")
        hashes = {}
        for relative in protected:
            path = ROOT / relative
            try:
                hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                hashes[relative] = None
        return hashes

    def _append(self, path, row):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()

    def _remaining(self, limit=None):
        left = self.deadline - time.monotonic()
        if left <= 0:
            return 0.05
        return min(left, limit) if limit is not None else left

    def command(self, name, argv, *, cwd=None, timeout=None, env=None, input_text=None,
                native_output=False):
        actual = [str(item) for item in argv]
        started_at = _utc_now()
        started = time.monotonic()
        stdout = stderr = ""
        returncode = None
        timed_out = False
        try:
            done = subprocess.run(
                actual, cwd=cwd or self.project, env=env or self.env,
                input=input_text, stdin=subprocess.DEVNULL if input_text is None else None,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self._remaining(timeout or self.args.command_timeout), check=False,
            )
            stdout, stderr, returncode = done.stdout or "", done.stderr or "", done.returncode
        except FileNotFoundError as exc:
            stderr = type(exc).__name__
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 124
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        except OSError as exc:
            stderr = type(exc).__name__
        duration = time.monotonic() - started
        stdout_bytes = stdout.encode("utf-8", "replace")
        stderr_bytes = stderr.encode("utf-8", "replace")
        row = {
            "step": name, "argv": _redact_argv(actual), "started_at": started_at,
            "finished_at": _utc_now(), "returncode": returncode,
            "elapsed_ms": int(duration * 1000), "timed_out": timed_out,
            "stdout_bytes": len(stdout_bytes),
            "stdout_hmac": hmac.new(self.salt, stdout_bytes, hashlib.sha256).hexdigest(),
            "stderr_bytes": len(stderr_bytes),
            "stderr_hmac": hmac.new(self.salt, stderr_bytes, hashlib.sha256).hexdigest(),
            "stderr_reason": _failure_reason(stderr, returncode, timed_out),
            "native_output_not_recorded": bool(native_output),
        }
        self._append(self.commands_path, row)
        self.attempts.append(row)
        return returncode, stdout, stderr

    def herdr_command(self, name, args, *, timeout=None, with_session=True,
                      native_output=False):
        if not self.herdr:
            self.command(name, ["herdr", *args], timeout=timeout,
                         native_output=native_output)
            return None, "", ""
        argv = [self.herdr]
        if with_session:
            argv.extend(("--session", self.session))
        argv.extend(args)
        return self.command(name, argv, timeout=timeout, native_output=native_output)

    def _capture_event(self, envelope):
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        event = envelope.get("event") or data.get("type")
        pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
        session = (data.get("agent_session_id") or data.get("session_id")
                   or (pane.get("agent_session") or {}).get("value"))
        row = {"at": _utc_now(), "event": event or "unknown",
               "pane_id": data.get("pane_id") or pane.get("pane_id"),
               "session_id": session if isinstance(session, str) else None,
               "source": data.get("source") or pane.get("source"),
               "session_start_source": (data.get("session_start_source")
                                        or pane.get("session_start_source")),
               "seq": data.get("seq") or pane.get("seq"),
               "receipt_ref": data.get("receipt_ref") or data.get("request_id")}
        status = data.get("agent_status") or pane.get("agent_status")
        if isinstance(status, str) and status.lower() in {
                "idle", "working", "blocked", "done", "unknown"}:
            row["native_status"] = status.lower()
        event_name = str(event or "").lower()
        if any(term in event_name for term in ("stream", "delta", "output", "token")):
            row["stream_event"] = str(event)[:100]
            for key in ("delta", "chunk", "output", "text", "content"):
                chunk = data.get(key)
                if isinstance(chunk, str):
                    raw = chunk.encode("utf-8", "replace")
                    row["stream_bytes"] = len(raw)
                    row["stream_hmac"] = hmac.new(self.salt, raw, hashlib.sha256).hexdigest()
                    break
        if isinstance(envelope.get("error"), dict):
            row["server_error"] = envelope["error"].get("code")
        self.event_capture.append(row)
        self._append(self.events_path, row)

    def _start_event_tap(self):
        import herdr_observation
        socket_path = herdr_observation.socket_path_for_endpoint(self.session, self.env)
        if not socket_path:
            return False
        self.decode_original = herdr_observation._decode_line
        def decode(line):
            envelope = self.decode_original(line)
            if (isinstance(envelope, dict)
                    and ("error" in envelope or "event" in envelope
                         or isinstance(envelope.get("data"), dict))):
                self._capture_event(envelope)
            return envelope
        herdr_observation._decode_line = decode
        self.transport = herdr_observation.ObservationTransport(
            self.session, socket_path, timeout=0.5)
        view = self.transport.observe()
        return bool(view and view.complete)

    def _stop_event_tap(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.decode_original is not None:
            import herdr_observation
            herdr_observation._decode_line = self.decode_original
            self.decode_original = None

    def probe_versions(self):
        if self.herdr:
            self.command("herdr-version", [self.herdr, "--version"], timeout=5)
            self.command("herdr-integration-install-help",
                         [self.herdr, "integration", "install", "--help"], timeout=5)
        else:
            self.command("herdr-version", ["herdr", "--version"], timeout=5)
        for runtime in RUNTIMES:
            binary = self.native[runtime]
            self.command(f"{runtime}-version", [binary or runtime, "--version"], timeout=5)

    def start_server(self):
        if not self.herdr:
            return False
        argv = [self.herdr, "--session", self.session, "server"]
        error_path = Path(self.env["TMPDIR"]) / "herdr-server.stderr"
        try:
            with error_path.open("wb") as errors:
                self.server = subprocess.Popen(
                    argv, cwd=self.project, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errors,
                    start_new_session=True, close_fds=True,
                )
                end_with_test(self.server)
        except OSError as exc:
            self._append(self.commands_path, {"step": "herdr-server-start", "argv": _redact_argv(argv),
                                              "returncode": None, "stderr_reason": type(exc).__name__,
                                              "started_at": _utc_now(), "finished_at": _utc_now()})
            return False
        self._append(self.commands_path, {"step": "herdr-server-start", "argv": _redact_argv(argv),
                                          "pid": self.server.pid, "pid_start": _LIVE._proc_start(self.server.pid),
                                          "started_at": _utc_now(), "finished_at": None})
        ready_until = min(self.deadline, time.monotonic() + 30)
        while time.monotonic() < ready_until and self.server.poll() is None:
            rc, stdout, _ = self.herdr_command("private-pane-list-ready", ["pane", "list"], timeout=2)
            payload = _json(stdout)
            if rc == 0 and payload is not None and not _error_envelope(payload):
                # This is the server just created in our private config root,
                # not an inherited endpoint or a guessed operating session.
                api_socket = (Path(self.env["HERDR_CONFIG_PATH"]).parent
                              / "sessions" / self.session / "herdr.sock")
                if not api_socket.is_socket():
                    return False
                self.env["HERDR_SOCKET_PATH"] = str(api_socket)
                return True
            time.sleep(min(0.2, max(0.0, ready_until - time.monotonic())))
        with error_path.open("rb") as errors:
            diagnostic = errors.read(8192)
        self._append(self.commands_path, {
            "step": "herdr-server-startup-result", "argv": _redact_argv(argv),
            "returncode": self.server.poll(), "finished_at": _utc_now(),
            "stderr_bytes": error_path.stat().st_size,
            "stderr_hmac": hmac.new(self.salt, diagnostic, hashlib.sha256).hexdigest(),
            "stderr_reason": _failure_reason(diagnostic.decode("utf-8", "replace"), self.server.poll()),
        })
        return False

    def create_pane(self, label):
        rc, stdout, _ = self.herdr_command("private-workspace-create",
            ["workspace", "create", "--cwd", str(self.project), "--label", label, "--no-focus"], timeout=10)
        payload = _json(stdout)
        panes = _pane_ids(payload)
        if rc == 0 and not _error_envelope(payload) and len(panes) == 1:
            pane = next(iter(panes))
            self.panes.append(pane)
            return pane
        result = _result(payload)
        workspace = None
        if isinstance(result, dict):
            workspace_value = result.get("workspace")
            workspace = (workspace_value.get("workspace_id") or workspace_value.get("id")
                         if isinstance(workspace_value, dict) else workspace_value)
        if not isinstance(workspace, str) or not workspace or _error_envelope(payload):
            return None
        rc, stdout, _ = self.herdr_command("private-tab-create",
            ["tab", "create", "--workspace", workspace, "--cwd", str(self.project), "--no-focus"], timeout=10)
        payload = _json(stdout)
        panes = _pane_ids(payload)
        if rc == 0 and not _error_envelope(payload) and len(panes) == 1:
            pane = next(iter(panes))
            self.panes.append(pane)
            return pane
        return None

    def _runtime_activation(self, runtime, step):
        command = [str(ROOT / "tools/install/harness.sh"), "runtime", "activate",
                   "--runtime", runtime, "--mode", "packaged", "--source", str(ROOT), "--json"]
        rc, stdout, stderr = self.command(step, command, cwd=self.project, timeout=60)
        payload = _json(stdout)
        return rc, payload, stderr

    def _herdr_status(self):
        return self.herdr_command("herdr-integration-status", ["integration", "status"],
                                  timeout=10, with_session=False)

    def _status_current(self, runtime, stdout):
        payload = _json(stdout)
        if payload and not _error_envelope(payload):
            def scan(node):
                if isinstance(node, dict):
                    if node.get("runtime") == runtime and str(node.get("status", "")).lower() == "current":
                        return True
                    return any(scan(value) for value in node.values())
                if isinstance(node, list):
                    return any(scan(value) for value in node)
                return False
            return scan(payload)
        lines = [line.lower() for line in stdout.splitlines()]
        return any(runtime in line and "current" in line for line in lines)

    def _install_and_reinstall(self, runtimes):
        result = {}
        for runtime in runtimes:
            initial_rc, initial, _ = self._runtime_activation(runtime, f"hearting-{runtime}-install")
            upstream_rc, upstream_out, upstream_err = self.herdr_command(
                f"herdr-{runtime}-integration-install", ["integration", "install", runtime],
                timeout=30, with_session=False)
            update_rc, update, _ = self._runtime_activation(runtime, f"hearting-{runtime}-update")
            result[runtime] = {"hearting_install_exit": initial_rc,
                               "upstream_install_exit": upstream_rc,
                               "hearting_update_exit": update_rc,
                               "upstream_reason": _failure_reason(upstream_err, upstream_rc),
                               "install_ok": initial_rc == 0,
                               "update_ok": update_rc == 0}
        status_rc, status_out, status_err = self._herdr_status()
        result["status_exit"] = status_rc
        result["status_reason"] = _failure_reason(status_err, status_rc)
        for runtime in runtimes:
            result[runtime]["status_current"] = bool(status_rc == 0 and self._status_current(runtime, status_out))
        return result

    def _seed_user_callback(self, runtime):
        """Install one user-owned SessionStart/event sentinel in the isolated home."""
        log = Path(self.env["HOME"]) / f"callback-{runtime}.log"
        log.unlink(missing_ok=True)
        if runtime == "claude":
            destination = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            command = f"printf claude >> {shlex.quote(str(log))}"
            value = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
                {"type": "command", "command": command},
            ]}]}}
        elif runtime == "codex":
            destination = Path(self.env["CODEX_HOME"]) / "hooks.json"
            command = f"printf codex >> {shlex.quote(str(log))}"
            value = {"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": command},
            ]}]}}
        else:
            config = Path(self.env["OPENCODE_CONFIG_DIR"])
            plugins = config / "plugins"
            plugins.mkdir(parents=True, exist_ok=True)
            sentinel = plugins / "hearting-user-sentinel.js"
            sentinel.write_text(
                "import { appendFileSync } from 'node:fs';\n"
                "export default async () => ({ event: async ({ event }) => { "
                "if (event?.type === 'session.created') "
                f"appendFileSync({json.dumps(str(log))}, 'opencode\\n'); "
                "} });\n", encoding="utf-8")
            destination = config / "opencode.json"
            value = {"plugin": ["./plugins/hearting-user-sentinel.js"]}
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(value), encoding="utf-8")
        self._append(self.commands_path, {
            "step": f"seed-user-callback-{runtime}", "started_at": _utc_now(),
            "finished_at": _utc_now(), "returncode": 0,
            "destination": str(destination), "private_fixture": True,
        })
        return log

    def _agent_start(self, runtime, pane, *, name=None):
        name = name or ("live-" + runtime + "-" + uuid.uuid4().hex[:6])
        timeout_ms = max(1000, min(60_000, int(self._remaining(60) * 1000)))
        start_args = ["agent", "start", name, "--kind", runtime, "--pane", pane,
                      "--timeout", str(timeout_ms)]
        if runtime == "codex":
            start_args.extend(("--", NATIVE_START_PROMPT))
        elif runtime == "opencode":
            start_args.extend(("--", "--prompt", NATIVE_START_PROMPT))
        rc, stdout, stderr = self.herdr_command(
            f"native-{runtime}-session-start",
            start_args, timeout=min(65, self._remaining(65)),
        )
        start_payload = _json(stdout) or _json(stderr)
        start_error = _error_envelope(start_payload) or _error_envelope(_json(stderr))
        start_ok = rc == 0 and not start_error
        start_result = _result(start_payload)
        start_agent = (start_result["agent"]
                       if isinstance(start_result, dict) and "agent" in start_result
                       else _NATIVE_IDENTITY_MISSING)
        start_identity_reason = (_native_agent_identity_reason(
            start_agent, name=name, runtime=runtime, pane=pane, require_complete=False)
            if start_ok else None)
        rc_get, get_stdout, get_stderr = self.herdr_command(
            f"native-{runtime}-session-get", ["agent", "get", name], timeout=10)
        get_payload = _json(get_stdout) or _json(get_stderr)
        get_result = _result(get_payload)
        agent = (get_result["agent"]
                 if isinstance(get_result, dict) and "agent" in get_result
                 else _NATIVE_IDENTITY_MISSING)
        get_error = _error_envelope(get_payload) or _error_envelope(_json(get_stderr))
        get_ok = rc_get == 0 and not get_error
        reported_sid = _session_id(get_payload) if get_ok else None
        get_identity_reason = (_native_agent_identity_reason(
            agent, name=name, runtime=runtime, pane=pane, require_complete=True)
            if get_ok else None)
        if not start_ok:
            identity_reason = "native-agent-start-unavailable"
        elif not get_ok:
            identity_reason = "native-agent-get-unavailable"
        else:
            identity_reason = start_identity_reason or get_identity_reason
        confirmed = bool(start_ok and get_ok and identity_reason is None and reported_sid)
        sid = reported_sid if confirmed else None
        return {"name": name, "pane_id": pane, "start_exit": rc, "start_ok": start_ok,
                "get_exit": rc_get, "get_ok": get_ok,
                "session_id": sid, "start_reason": _failure_reason(stderr, rc),
                "get_reason": _failure_reason(get_stderr, rc_get),
                "start_error_code": start_error, "get_error_code": get_error,
                "get_session_id_present": bool(reported_sid),
                "agent_status": (agent or {}).get("agent_status") if isinstance(agent, dict) else None,
                "session_identity_reason": identity_reason,
                "session_confirmed_by_get": confirmed}

    def _wait_for_native_state(self, runtime, name, state, timeout_ms=30_000):
        rc, stdout, stderr = self.herdr_command(f"native-{runtime}-wait-{state}",
            ["agent", "wait", name, "--until", state, "--timeout", str(timeout_ms)],
            timeout=timeout_ms / 1000 + 3)
        payload = _json(stdout) or _json(stderr)
        observed = _native_status(payload)
        return {"exit": rc, "reason": _failure_reason(stderr, rc),
                "error_code": _error_envelope(payload), "typed_state": observed,
                "matched_requested_state": rc == 0 and observed == state}

    def _matching_native_events(self, pane, session_id, since=0):
        return [event for event in self.event_capture[since:]
                if event.get("pane_id") == pane
                and (event.get("session_id") in (None, session_id))]

    @staticmethod
    def _stream_evidence(events):
        return [{key: event.get(key) for key in
                 ("at", "event", "session_id", "pane_id", "seq", "stream_event",
                  "stream_bytes", "stream_hmac") if event.get(key) is not None}
                for event in events if event.get("stream_event")]

    @staticmethod
    def _status_evidence(events):
        return [{key: event.get(key) for key in
                 ("at", "event", "session_id", "pane_id", "seq", "native_status")
                 if event.get(key) is not None}
                for event in events if event.get("native_status")]

    @staticmethod
    def _epoch_time(value):
        try:
            return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def _private_pending_path(self, transfer_ref):
        if not isinstance(transfer_ref, str) or not re.fullmatch(r"[0-9a-f]{32}", transfer_ref):
            return None
        return (Path(self.env["AGENT_PEER_LEDGER_ROOT"])
                / "peer-messages" / "pending" / f"{transfer_ref}.json")

    def _read_private_pending(self, transfer_ref, *, runtime, session_id=None, pane=None):
        path = self._private_pending_path(transfer_ref)
        if path is None:
            return None
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        target = row.get("to") if isinstance(row, dict) else None
        if not isinstance(target, dict) or target.get("harness") != runtime:
            return None
        if session_id and target.get("session_id") != session_id:
            return None
        if not session_id and pane and target.get("pane_id") != pane:
            return None
        if row.get("ref") != transfer_ref:
            return None
        return row

    def _find_private_pending(self, body, *, runtime, session_id=None, pane=None):
        directory = Path(self.env["AGENT_PEER_LEDGER_ROOT"]) / "peer-messages" / "pending"
        digest = hashlib.sha256(body.rstrip("\n").encode("utf-8")).hexdigest()
        try:
            paths = list(directory.glob("[0-9a-f]" * 32 + ".json"))
        except OSError:
            return None
        for path in paths:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            target = row.get("to") if isinstance(row, dict) else None
            if (not isinstance(target, dict) or row.get("source_sha256") != digest
                    or target.get("harness") != runtime):
                continue
            if session_id and target.get("session_id") != session_id:
                continue
            if not session_id and pane and target.get("pane_id") != pane:
                continue
            return row
        return None

    def _peer_receipt_count(self, transfer_ref, runtime, session_id):
        root = Path(self.env["AGENT_PEER_LEDGER_ROOT"]) / "peer-messages"
        count = 0
        try:
            paths = root.glob("*/*.jsonl")
            for path in paths:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        target = row.get("to") if isinstance(row, dict) else None
                        delivery = row.get("delivery") if isinstance(row, dict) else None
                        if (isinstance(target, dict) and isinstance(delivery, dict)
                                and row.get("transfer_ref") == transfer_ref
                                and target.get("harness") == runtime
                                and target.get("session_id") == session_id
                                and delivery.get("status") == "received"):
                            count += 1
        except OSError:
            return count
        return count

    def _queue_private_advisory(self, runtime, pane, session_id, *, purpose):
        nonce = uuid.uuid4().hex[:24]
        body = f"[steer] Hearting isolated native advisory nonce {nonce}"
        started_at = _utc_now()
        argv = [sys.executable, str(ROOT / "utilities/peer-steward.py"),
                "--herdr-session", self.session, "prompt", pane, "--body-stdin"]
        rc, stdout, stderr = self.command(
            f"native-{runtime}-{purpose}-advisory-enqueue", argv,
            timeout=12, input_text=body, native_output=True)
        outcome_match = re.search(r"\bprompted=(true|queued|unverified|failed|false)\b", stdout)
        verify_match = re.search(r"\bverify=([^\s]+)", stdout)
        state_match = re.search(r"\bstate_before=([^\s]+)", stdout)
        row = self._find_private_pending(body, runtime=runtime,
                                         session_id=session_id, pane=pane)
        transfer_ref = row.get("ref") if isinstance(row, dict) else None
        accepted = bool(rc in (0, 3) and outcome_match
                        and outcome_match.group(1) in {"true", "queued"}
                        and transfer_ref)
        command_row = self.attempts[-1] if self.attempts else {}
        return {"attempted": True, "started_at": started_at,
                "accepted_at": command_row.get("finished_at") if accepted else None,
                "exit": rc, "exit_reason": _failure_reason(stderr, rc),
                "outcome": outcome_match.group(1) if outcome_match else None,
                "verify": verify_match.group(1) if verify_match else None,
                "state_before": state_match.group(1) if state_match else None,
                "accepted": accepted, "transfer_ref": transfer_ref,
                "queue_state": row.get("state") if isinstance(row, dict) else None,
                "queue_receipt": row.get("receipt") if isinstance(row, dict) else None,
                "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
                "reason": (None if accepted else
                           ("exact-private-pending-row-not-found" if outcome_match else
                            "peer-steward-returned-no-typed-verdict"))}

    def _wait_private_peer_receipt(self, enqueue, runtime, session_id, pane, timeout=3.0):
        transfer_ref = enqueue.get("transfer_ref") if isinstance(enqueue, dict) else None
        if not enqueue.get("accepted") or not transfer_ref:
            return {"attempted": False, "count": 0, "transfer_ref": None,
                    "consumed_at": None, "reason": "no-accepted-private-advisory-receipt"}
        deadline = min(self.deadline, time.monotonic() + timeout)
        row = None
        while time.monotonic() < deadline:
            row = self._read_private_pending(transfer_ref, runtime=runtime,
                                             session_id=session_id, pane=pane)
            if row and row.get("state") == "received":
                break
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        received = bool(row and row.get("state") == "received")
        ledger_count = self._peer_receipt_count(transfer_ref, runtime, session_id) \
            if received and session_id else 0
        if received and ledger_count == 0:
            # A Codex App Server queue receipt records native history directly in
            # the pending item; it need not produce a second peer ledger row.
            ledger_count = 1 if row.get("receipt") == "exact-native-history" else 0
        return {"attempted": True, "count": ledger_count,
                "transfer_ref": transfer_ref,
                "queue_state": row.get("state") if isinstance(row, dict) else None,
                "receipt": row.get("receipt") if isinstance(row, dict) else None,
                "consumed_at": (self._epoch_time(row.get("received_at"))
                                or (_utc_now() if received else None)),
                "exactly_once": received and ledger_count == 1,
                "reason": None if received and ledger_count == 1 else
                          "exact-native-receipt-not-observed-within-bounded-wait"}

    def _busy_probe(self, runtime, pane, launched=None):
        launched = launched or self._agent_start(runtime, pane)
        if not launched.get("session_id"):
            return {"native_start": launched, "outcome": "unavailable",
                    "reason": launched.get("get_error_code") or launched["get_reason"],
                    "reached": {"session_start": True, "native_prompt": False,
                                "working_wait": False, "advisory_enqueue": False,
                                "idle_wait": False, "consumption": False},
                    "unreached_reasons": {"native_prompt": "no-confirmed-native-session-id",
                                          "working_wait": "native-prompt-not-submitted",
                                          "advisory_enqueue": "native-prompt-not-submitted",
                                          "idle_wait": "native-prompt-not-submitted",
                                          "consumption": "no-advisory-receipt"}}
        event_start = len(self.event_capture)
        prompt_started_at = _utc_now()
        # The prompt is passed only to the native child. command() redacts its
        # free-form argument from durable evidence.
        prompt = "Run one bounded shell command that sleeps for 20 seconds, then stop."
        rc, stdout, stderr = self.herdr_command(
            f"native-{runtime}-busy-probe",
            ["agent", "prompt", launched["name"], prompt, "--wait", "--until", "working",
             "--timeout", "30000"], timeout=35, native_output=True)
        prompt_payload = _json(stdout) or _json(stderr)
        prompt_error = (_error_envelope(prompt_payload)
                        or _error_envelope(_json(stdout))
                        or _error_envelope(_json(stderr)))
        prompt_accepted = rc == 0 and prompt_error is None
        prompt_state = _native_status(prompt_payload)
        working_wait = None
        idle_wait = None
        if prompt_accepted:
            working_wait = self._wait_for_native_state(runtime, launched["name"], "working")
        if self.transport is not None:
            self.transport.observe()
        observed_events = self._matching_native_events(pane, launched["session_id"], event_start)
        status_events = self._status_evidence(observed_events)
        stream_events = self._stream_evidence(observed_events)
        working_seen = (prompt_state == "working"
                        or bool(working_wait and working_wait["matched_requested_state"])
                        or any(row.get("native_status") == "working" for row in status_events))
        advisory = (self._queue_private_advisory(runtime, pane, launched["session_id"], purpose="busy")
                    if prompt_accepted and working_seen else {
                        "attempted": False, "accepted": False, "transfer_ref": None,
                        "accepted_at": None,
                        "reason": ("native-working-state-not-observed" if prompt_accepted else
                                   "native-prompt-not-accepted")})
        if prompt_accepted:
            idle_wait = self._wait_for_native_state(runtime, launched["name"], "idle")
            if self.transport is not None:
                self.transport.observe()
            observed_events = self._matching_native_events(pane, launched["session_id"], event_start)
            status_events = self._status_evidence(observed_events)
            stream_events = self._stream_evidence(observed_events)
        consumption = self._wait_private_peer_receipt(
            advisory, runtime, launched["session_id"], pane, timeout=2.0)
        complete = (prompt_accepted and working_seen and bool(stream_events)
                    and idle_wait is not None and idle_wait["matched_requested_state"]
                    and advisory.get("accepted") and consumption["exactly_once"])
        duplicate = consumption["count"] > 1
        return {"native_start": launched, "session_id": launched["session_id"],
                "prompt": {"attempted": True, "started_at": prompt_started_at,
                           "exit": rc, "reason": _failure_reason(stderr, rc),
                           "accepted": prompt_accepted, "error_code": prompt_error,
                           "typed_state": prompt_state},
                "working_wait": working_wait,
                "idle_wait": idle_wait,
                "native_metadata": {"working_observed": working_seen,
                                    "status_events": status_events,
                                    "stream_events": stream_events,
                                    "stream_metadata_observed": bool(stream_events),
                                    "stream_unavailable_reason": (None if stream_events else
                                        "private-observation-stream-exposed-no-native-output-delta")},
                "advisory_enqueue": {**advisory,
                    "receipt_ref": advisory.get("transfer_ref")},
                "consumption": {**consumption,
                    "receipt_ref": consumption.get("transfer_ref")},
                "reached": {"session_start": True, "native_prompt": True,
                            "working_wait": working_wait is not None,
                            "advisory_enqueue": advisory.get("attempted", False),
                            "idle_wait": idle_wait is not None,
                            "consumption": consumption.get("attempted", False)},
                "unreached_reasons": {
                    **({} if prompt_accepted else {"working_wait": "native-prompt-rejected-or-error-envelope"}),
                    **({} if advisory.get("accepted") else
                       {"advisory_enqueue": advisory.get("reason")}),
                    **({} if consumption.get("exactly_once") else
                       {"consumption": consumption.get("reason")})},
                "outcome": "fail" if duplicate else "measured" if complete else "unavailable",
                "reason": ("duplicate-advisory-consumption" if duplicate else None if complete else
                           "native-busy-stream-and-exact-advisory-consumption-not-proven")}

    def _events_lost(self):
        if not self._start_event_tap():
            return {"outcome": "unavailable", "reason": "private-event-subscription-unavailable"}
        pane = self.create_pane("events-lost-scratch")
        if not pane:
            return {"outcome": "unavailable", "reason": "scratch-pane-create-failed"}
        before = self.transport.observe() if self.transport else None
        initial_count = len(self.event_capture)
        split_rows = []
        pane_chain = pane
        for index in range(32):
            rc, stdout, stderr = self.herdr_command(
                f"events-lost-burst-{index + 1}",
                ["pane", "split", "--pane", pane_chain, "--direction", "right",
                 "--ratio", "0.5", "--no-focus"], timeout=5)
            payload = _json(stdout)
            created = _pane_ids(payload)
            new_pane = next(iter(created), None)
            split_rows.append({"index": index + 1, "exit": rc, "pane_created": bool(new_pane)})
            if rc != 0 or not new_pane:
                break
            self.panes.append(new_pane)
            pane_chain = new_pane
        events_lost = [event for event in self.event_capture[initial_count:]
                       if event.get("server_error") == "events_lost"]
        recovered = None
        if self.transport:
            recovered = self.transport.observe()
        if events_lost and recovered and recovered.complete:
            outcome = "measured"
        else:
            outcome = "unavailable"
        return {"outcome": outcome, "before_complete": bool(before and before.complete),
                "burst_attempts": split_rows, "burst_limit": 32,
                "events_lost_count": len(events_lost),
                "fresh_snapshot_complete": bool(recovered and recovered.complete),
                "reason": None if outcome == "measured" else "events-lost-not-observed-within-bounded-burst"}

    def _install_callbacks(self):
        sentinels = {runtime: self._seed_user_callback(runtime) for runtime in RUNTIMES}
        status = self._install_and_reinstall(RUNTIMES)
        runtime_results = {}
        for runtime in RUNTIMES:
            pane = self.create_pane(f"callbacks-{runtime}")
            if not pane:
                runtime_results[runtime] = {"outcome": "unavailable", "reason": "scratch-pane-create-failed"}
                continue
            tap_ok = self._start_event_tap()
            native = self._agent_start(runtime, pane)
            view = self.transport.observe() if self.transport else None
            confirmed_sid = (native.get("session_id")
                             if native.get("session_confirmed_by_get") else None)
            exact_events = ([event for event in self._matching_native_events(pane, confirmed_sid)
                             if event.get("session_id") == confirmed_sid
                             and event.get("session_start_source")
                             and isinstance(event.get("source"), str)
                             and event["source"].startswith("herdr:")]
                            if confirmed_sid else [])
            sentinel_path = sentinels[runtime]
            sentinel_count = None
            if sentinel_path.exists():
                sentinel_count = len(sentinel_path.read_text(encoding="utf-8").splitlines())
            if (sentinel_count is not None and sentinel_count > 1) or len(exact_events) > 1:
                outcome, reason = "fail", "duplicate-callback-observed"
            elif (status.get(runtime, {}).get("status_current") and native.get("session_id")
                  and native.get("session_confirmed_by_get")
                  and tap_ok and len(exact_events) == 1 and sentinel_count == 1):
                outcome, reason = "measured", None
            else:
                outcome, reason = "unavailable", "native-loader-and-callback-report-count-not-complete"
            runtime_results[runtime] = {
                "outcome": outcome, "reason": reason,
                "status_current": status.get(runtime, {}).get("status_current", False),
                "native_session_id": native.get("session_id"),
                "native_start_exit": native.get("start_exit"),
                "native_start_ok": native.get("start_ok", False),
                "native_get_exit": native.get("get_exit"),
                "native_get_ok": native.get("get_ok", False),
                "native_get_reason": native.get("get_reason"),
                "native_session_identity_reason": native.get("session_identity_reason"),
                "native_session_confirmed_by_get": native.get("session_confirmed_by_get", False),
                "native_callback_report_count": len(exact_events),
                "native_callback_reports": [{key: event.get(key) for key in
                    ("at", "event", "pane_id", "session_id", "source",
                     "session_start_source", "seq", "receipt_ref")
                    if event.get(key) is not None} for event in exact_events],
                "user_sentinel_count": sentinel_count,
                "snapshot_complete": bool(view and view.complete),
                "loader_or_report_unavailable_reason": (None if len(exact_events) == 1 else
                    "no-exact-native-session-start-report"),
            }
            self._stop_event_tap()
        outcomes = {row["outcome"] for row in runtime_results.values()}
        result = "fail" if "fail" in outcomes else "measured" if outcomes == {"measured"} else "unavailable"
        return {"outcome": result, "runtimes": runtime_results, "install_update": status}

    def _case(self):
        if not self.herdr:
            self.probe_versions()
            return {"outcome": "unavailable", "reason": "herdr-executable-unavailable"}
        self.probe_versions()
        if not self.start_server():
            return {"outcome": "unavailable", "reason": "private-herdr-server-unavailable"}
        if self.args.case == "callbacks":
            return self._install_callbacks()
        if self.args.case == "events-lost":
            return self._events_lost()
        runtime = "opencode" if self.args.case == "opencode-busy" else "codex"
        status = self._install_and_reinstall((runtime,))
        pane = self.create_pane(f"{self.args.case}-scratch")
        if not pane:
            return {"outcome": "unavailable", "reason": "scratch-pane-create-failed",
                    "install_update": status}
        tap = self._start_event_tap()
        native = self._agent_start(runtime, pane)
        result = {"outcome": "unavailable", "native_start": native,
                  "install_update": status, "event_subscription": tap}
        if self.args.case in {"codex-busy", "opencode-busy"}:
            result.update(self._busy_probe(runtime, pane, native))
        elif self.args.case == "codex-async-local-skip":
            if not native.get("session_id"):
                result.update({"outcome": "unavailable",
                               "advisory_enqueue": {"attempted": False, "accepted": False,
                                   "receipt_ref": None, "reason": "no-confirmed-native-session-id"},
                               "async_carrier": {"attempted": False, "receipt_ref": None,
                                   "reason": "no-confirmed-native-session-id"},
                               "local_skip": {"attempted": False, "consumed": False,
                                   "reason": "no-advisory-receipt-to-sweep"},
                               "reached": {"private_session_start": False,
                                           "advisory_enqueue": False,
                                           "async_carrier": False,
                                           "local_skip_consumption": False},
                               "unreached_reasons": {
                                   "advisory_enqueue": "native-session-start-did-not-return-a-confirmed-session-id",
                                   "async_carrier": "no-advisory-receipt-to-deliver",
                                   "local_skip_consumption": "no-advisory-receipt-to-sweep"},
                               "reason": "native-session-start-did-not-return-a-confirmed-session-id"})
            else:
                idle = self._wait_for_native_state(runtime, native["name"], "idle")
                advisory = (self._queue_private_advisory(
                    runtime, pane, native["session_id"], purpose="async-local-skip")
                    if idle["matched_requested_state"] else {
                        "attempted": False, "accepted": False, "transfer_ref": None,
                        "reason": "private-native-session-idle-state-not-confirmed"})
                initial_receipt = self._wait_private_peer_receipt(
                    advisory, runtime, native["session_id"], pane, timeout=1.0)
                user_prompt = None
                final_receipt = initial_receipt
                if advisory.get("accepted") and not initial_receipt.get("exactly_once"):
                    user_nonce = uuid.uuid4().hex[:24]
                    separate_user_body = ("Run one bounded shell command that executes true once, "
                                          f"then stop. User prompt nonce {user_nonce}.")
                    user_started_at = _utc_now()
                    prompt_rc, prompt_out, prompt_err = self.herdr_command(
                        "codex-local-skip-user-prompt",
                        ["agent", "prompt", native["name"], separate_user_body,
                         "--wait", "--until", "idle", "--timeout", "30000"],
                        timeout=35, native_output=True)
                    prompt_payload = _json(prompt_out) or _json(prompt_err)
                    prompt_error = (_error_envelope(prompt_payload)
                                    or _error_envelope(_json(prompt_out))
                                    or _error_envelope(_json(prompt_err)))
                    prompt_accepted = prompt_rc == 0 and prompt_error is None
                    user_prompt = {"attempted": True, "started_at": user_started_at,
                                   "exit": prompt_rc, "accepted": prompt_accepted,
                                   "typed_state": _native_status(prompt_payload),
                                   "error_code": prompt_error,
                                   "nonce_sha256": hashlib.sha256(
                                       user_nonce.encode("ascii")).hexdigest()}
                    if prompt_accepted:
                        idle_after_prompt = self._wait_for_native_state(
                            runtime, native["name"], "idle")
                        user_prompt["idle_wait"] = idle_after_prompt
                    final_receipt = self._wait_private_peer_receipt(
                        advisory, runtime, native["session_id"], pane, timeout=3.0)
                duplicate_receipts = (final_receipt.get("count", 0) > 1)
                result.update({"outcome": "fail" if duplicate_receipts else "unavailable",
                               "native_idle_wait": idle,
                               "advisory_enqueue": {**advisory,
                                   "receipt_ref": advisory.get("transfer_ref")},
                               "async_carrier": {
                                   "attempted": advisory.get("attempted", False),
                                   "verify": advisory.get("verify"),
                                   "queue_state": advisory.get("queue_state"),
                                   "receipt_ref": advisory.get("transfer_ref"),
                                   "exact_receipt_observed": initial_receipt.get("exactly_once", False),
                                   "event_attribution": "peer-queue-receipt-only"},
                               "user_prompt_trigger": user_prompt,
                               "local_skip": {
                                   "attempted": bool(user_prompt and user_prompt.get("accepted")),
                                   "consumed": final_receipt.get("exactly_once", False),
                                   "receipt_ref": final_receipt.get("transfer_ref"),
                                   "consumption": final_receipt,
                                   "user_nonce_sha256": (user_prompt or {}).get("nonce_sha256"),
                                   "advisory_nonce_sha256": advisory.get("nonce_sha256"),
                                   "prompt_and_advisory_bodies_separate_at_submission": True,
                                   "transcript_separation_observed": False,
                                   "reason": ("duplicate-native-peer-receipts" if duplicate_receipts else
                                       "native-hook-event-and-transcript-nonce-separation-not-observed")},
                               "reached": {"private_session_start": True,
                                           "advisory_enqueue": advisory.get("attempted", False),
                                           "async_carrier": bool(advisory.get("verify")),
                                           "local_skip_consumption": bool(user_prompt and
                                               user_prompt.get("accepted") and
                                               final_receipt.get("exactly_once"))},
                               "unreached_reasons": {
                                   **({} if advisory.get("accepted") else
                                      {"advisory_enqueue": advisory.get("reason")}),
                                   **({} if initial_receipt.get("exactly_once") else
                                      {"async_carrier": initial_receipt.get("reason")}),
                                   "local_skip_consumption": ("duplicate-native-peer-receipts"
                                       if duplicate_receipts else
                                       "native-hook-event-and-transcript-nonce-separation-not-observed")},
                               "reason": ("duplicate-native-peer-receipts" if duplicate_receipts else
                                          "native-hook-event-and-transcript-nonce-separation-not-observed")})
        else:
            if not native.get("session_id"):
                result.update({"outcome": "unavailable", "enqueue": {"attempted": False,
                    "accepted": False, "receipt_ref": None,
                    "reason": "no-confirmed-native-session-id"},
                    "clear": {"attempted": False, "reason": "enqueue-not-reached"},
                    "successor": {"observed": False, "session_id": None,
                                  "reason": "enqueue-not-reached"},
                    "reason": "native-session-start-did-not-return-a-confirmed-session-id"})
            else:
                enqueue_started = _utc_now()
                rc, stdout, stderr = self.command(
                    "booked-seat-enqueue",
                    [sys.executable, str(ROOT / "utilities/session_tidy.py"), "enqueue",
                     "--harness", "codex", "--session-id", native["session_id"],
                     "--cwd", str(self.project), "--no-continue"], timeout=15)
                match = re.search(r"\benqueue=([^\s]+)", stdout)
                queue_id = match.group(1) if match else None
                queued = bool(rc == 0 and queue_id and queue_id != "none"
                              and "status=queued" in stdout)
                if queued:
                    self.tidy_queue_ids.append(queue_id)
                    self.tidy_session_ids.append(native["session_id"])
                enqueue_attempt = self.attempts[-1] if self.attempts else {}
                clear_match = re.search(r"\bclear=([^\s]+)", stdout)
                clear_state = clear_match.group(1) if clear_match else None
                successor = self._observe_successor(native["name"], native["session_id"], pane) \
                    if queued else {"observed": False, "session_id": None,
                                    "reason": "enqueue-not-accepted"}
                reservation = self._private_clear_reservation(native["session_id"], queue_id) \
                    if queued else None
                self._collect_tidy_processes(native["session_id"], queue_id)
                if (reservation and reservation.get("status") == "cleared"
                        and reservation.get("new_session")):
                    successor = {"observed": True,
                                 "session_id": reservation["new_session"],
                                 "observed_at": reservation.get("finished"),
                                 "source": "session-tidy-clear-receipt"}
                clear_claim = (reservation or {}).get("clear_claim")
                clear_sent = (isinstance(clear_claim, dict)
                              and clear_claim.get("state") in {"sent", "sending"})
                clear_claim_evidence = ({key: clear_claim.get(key) for key in
                    ("state", "at", "finished", "outcome") if clear_claim.get(key) is not None}
                    if isinstance(clear_claim, dict) else None)
                result.update({"outcome": "unavailable",
                    "enqueue": {"attempted": True, "started_at": enqueue_started,
                                "finished_at": enqueue_attempt.get("finished_at"),
                                "exit": rc, "reason": _failure_reason(stderr, rc),
                                "accepted": queued, "receipt_ref": queue_id if queued else None,
                                "receipt_status": "queued" if queued else None},
                    "clear": {"booking_status": (reservation or {}).get("status") or clear_state,
                              "attempted": bool(clear_sent or (reservation or {}).get("status") in
                                                {"cleared", "unverified"}),
                              "clear_claim": clear_claim_evidence,
                              "reason": (None if clear_sent else
                                  "clear-helper-did-not-record-a-native-send-before-bounded-observation-ended")},
                    "successor": successor,
                    "reached": {"native_session": True, "queue_enqueue": True,
                                "queue_acceptance": queued, "native_clear": bool(clear_sent),
                                "new_session_id": bool(successor.get("observed"))},
                    "unreached_reasons": ({"queue_acceptance": "enqueue-did-not-return-a-queued-receipt"}
                        if not queued else {"native_clear": (None if clear_sent else
                            "clear-helper-did-not-record-a-native-send-before-bounded-observation-ended"),
                            "new_session_id": successor.get("reason")}),
                    "reason": ("native-clear-successor-not-observed" if not successor.get("observed")
                               else "new-session-observed-but-seat-lineage-not-proven")})
        return result

    def _observe_successor(self, name, old_sid, pane):
        """Observe only the actual private agent record and endpoint snapshot."""
        deadline = min(self.deadline, time.monotonic() + 8)
        last_reason = "new-native-session-id-not-observed-within-bounded-wait"
        while time.monotonic() < deadline:
            rc, stdout, stderr = self.herdr_command(
                "booked-seat-successor-get", ["agent", "get", name], timeout=3)
            payload = _json(stdout) or _json(stderr)
            sid = _session_id(payload) if rc == 0 and not _error_envelope(payload) else None
            if sid and sid != old_sid:
                return {"observed": True, "session_id": sid,
                        "observed_at": _utc_now(), "source": "herdr-agent-get"}
            if rc != 0 or _error_envelope(payload):
                last_reason = _error_envelope(payload) or _failure_reason(stderr, rc)
            if self.transport is not None:
                view = self.transport.observe()
                for row in getattr(view, "panes", []) or []:
                    if row.get("pane_id") != pane:
                        continue
                    session = row.get("agent_session")
                    current_sid = session.get("value") if isinstance(session, dict) else None
                    if isinstance(current_sid, str) and current_sid != old_sid:
                        return {"observed": True, "session_id": current_sid,
                                "observed_at": _utc_now(),
                                "source": "private-endpoint-pane-snapshot"}
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return {"observed": False, "session_id": None, "reason": last_reason}

    def _private_clear_reservation(self, sid, queue_id=None):
        state = Path(self.env["XDG_STATE_HOME"]) / "hearting" / "session-tidy" / "clear"
        try:
            paths = list(state.glob("*.json"))
        except OSError:
            return None
        for path in paths:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(row, dict) and row.get("harness") == "codex"
                    and row.get("sid") == sid
                    and (not queue_id or row.get("queue_id") == queue_id)):
                return row
        return None

    def _collect_tidy_processes(self, sid, queue_id=None):
        state = Path(self.env["XDG_STATE_HOME"]) / "hearting" / "session-tidy"
        if queue_id:
            item_path = state / "queue" / f"{queue_id}.json"
            try:
                item = json.loads(item_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                item = {}
            if isinstance(item, dict) and (item.get("harness"), item.get("sid")) == ("codex", sid):
                for kind, pid_key, start_key in (
                        ("tidy-runner", "runner_pid", "runner_pid_start"),
                        ("tidy-child", "child_pid", "child_pid_start")):
                    pid, start = item.get(pid_key), item.get(start_key)
                    if isinstance(pid, int) and pid > 0 and start:
                        self.tidy_processes.append({"kind": kind, "pid": pid,
                                                    "pid_start": str(start)})
                    elif isinstance(pid, int) and pid > 0:
                        self.cleanup.append({"action": "stop-private-tidy-process",
                                             "kind": kind, "pid": pid,
                                             "result": "identity-missing-left-untouched"})
        clear_dir = state / "clear"
        try:
            reservations = list(clear_dir.glob("*.json"))
        except OSError:
            reservations = []
        for path in reservations:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (not isinstance(row, dict) or row.get("harness") != "codex"
                    or row.get("sid") != sid or (queue_id and row.get("queue_id") != queue_id)):
                continue
            helper = row.get("helper") if isinstance(row.get("helper"), dict) else {}
            pid, start = helper.get("pid"), helper.get("pid_start")
            if isinstance(pid, int) and pid > 0 and start:
                self.tidy_processes.append({"kind": "tidy-clear-helper", "pid": pid,
                                            "pid_start": str(start)})
            elif isinstance(pid, int) and pid > 0:
                self.cleanup.append({"action": "stop-private-tidy-process",
                                     "kind": "tidy-clear-helper", "pid": pid,
                                     "result": "identity-missing-left-untouched"})

    def _stop_private_tidy_processes(self):
        for sid, queue_id in zip(self.tidy_session_ids, self.tidy_queue_ids):
            self._collect_tidy_processes(sid, queue_id)
        seen = set()
        for record in self.tidy_processes:
            key = (record["pid"], record["pid_start"])
            if key in seen:
                continue
            seen.add(key)
            pid, expected = key
            if _LIVE._proc_start(pid) != expected:
                self.cleanup.append({"action": "stop-private-tidy-process",
                                     "kind": record["kind"], "pid": pid,
                                     "result": "already-exited-or-identity-changed"})
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                self.cleanup.append({"action": "stop-private-tidy-process",
                                     "kind": record["kind"], "pid": pid,
                                     "result": type(exc).__name__})
                continue
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and _LIVE._proc_start(pid) == expected:
                time.sleep(0.05)
            if _LIVE._proc_start(pid) == expected:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    self.cleanup.append({"action": "stop-private-tidy-process",
                                         "kind": record["kind"], "pid": pid,
                                         "result": type(exc).__name__})
                    continue
            stopped = _LIVE._proc_start(pid) != expected
            self.cleanup.append({"action": "stop-private-tidy-process",
                                 "kind": record["kind"], "pid": pid,
                                 "pid_start": expected,
                                 "result": "stopped" if stopped else "still-live-after-signal"})

    def cleanup_owned(self):
        self._stop_event_tap()
        self._stop_private_tidy_processes()
        for pane in reversed(self.panes):
            rc, _out, err = self.herdr_command("cleanup-private-pane-close", ["pane", "close", pane],
                                               timeout=5, with_session=True)
            self.cleanup.append({"action": "close-private-pane", "pane_id": pane,
                                 "exit": rc, "reason": _failure_reason(err, rc)})
        if self.server is not None:
            rc, _out, err = self.herdr_command("cleanup-private-server-stop", ["server", "stop"],
                                               timeout=8, with_session=True)
            self.cleanup.append({"action": "stop-private-server", "session": self.session,
                                 "exit": rc, "reason": _failure_reason(err, rc)})
            stopped = _LIVE.stop_owned_process(self.server, timeout=2)
            self.cleanup.append({"action": "reap-private-server-process", "result": stopped,
                                 "pid": self.server.pid, "pid_start": _LIVE._proc_start(self.server.pid)})

    def execute(self):
        try:
            if not self.herdr:
                result = {"outcome": "unavailable", "reason": "herdr-executable-unavailable"}
            else:
                result = self._case()
            self.result.update(result)
        except (LiveRunError, OSError, subprocess.SubprocessError, ValueError) as exc:
            self.result.update(outcome="unavailable",
                               reason=f"{type(exc).__name__}:{str(exc)[:160]}")
        finally:
            try:
                self.cleanup_owned()
            except Exception as exc:
                self.cleanup.append({"action": "cleanup-error", "reason": type(exc).__name__})
            try:
                self.runtime_dir.cleanup()
                self.cleanup.append({"action": "remove-private-runtime", "result": "removed"})
            except OSError as exc:
                self.cleanup.append({"action": "cleanup-runtime-error", "reason": type(exc).__name__})
            self.result["finished_at"] = _utc_now()
            self.result.setdefault("source_protection", {})["after"] = self._source_hashes()
            before = self.result["source_protection"].get("before")
            after = self.result["source_protection"].get("after")
            self.result["source_protection"]["unchanged"] = before == after
            if before != after and self.result.get("outcome") == "measured":
                self.result.update(outcome="unavailable",
                                   reason="protected-source-hash-changed-during-private-run")
            self.result["evidence_paths"] = [str(self.commands_path), str(self.events_path), str(self.result_path)]
            self._append(self.commands_path, {"step": "driver-finish", "started_at": self.result["finished_at"],
                                              "finished_at": self.result["finished_at"],
                                              "returncode": 0 if self.result.get("outcome") == "measured" else 2,
                                              "stderr_reason": self.result.get("reason")})
            self.result_path.write_text(json.dumps(self.result, ensure_ascii=False, indent=2,
                                                   sort_keys=True) + "\n", encoding="utf-8")
            self.result_path.chmod(0o600)
            print(json.dumps({"case": self.args.case, "outcome": self.result["outcome"],
                              "result": str(self.result_path)}, ensure_ascii=False))
        return 0 if self.result.get("outcome") == "measured" else 1 if self.result.get("outcome") == "fail" else 2


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--command-timeout", type=int, default=15)
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--opencode", default="opencode")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.timeout > MAX_TIMEOUT or args.command_timeout < 1:
        parser.error(f"--timeout must be 1..{MAX_TIMEOUT}; --command-timeout must be positive")
    try:
        return HerdrIntegrationLive(args).execute()
    except (LiveRunError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}:{str(exc)[:160]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
