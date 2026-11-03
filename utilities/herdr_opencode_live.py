#!/usr/bin/env python3
"""Run one bounded OpenCode window against an isolated named Herdr session.

The runner owns the named Herdr server, optional attach backend, pane, and all
CLI children it starts. It records identifiers and state only; visible prompt,
question, and draft text is never written to the output directory.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child

ROOT = Path(__file__).resolve().parents[1]
MAX_TIMEOUT = 900
_SECRET_ENV = re.compile(r"(?:API[_-]?KEY|ACCESS[_-]?TOKEN|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.I)
_START_SOURCE = {"startup", "resume", "select", "clear", "compact", "fork"}


class LiveRunError(RuntimeError):
    pass


def _json(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _result(value):
    return value.get("result") if isinstance(value, dict) else None


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


def _workspace_id(value):
    result = _result(value)
    if not isinstance(result, dict):
        return None
    workspace = result.get("workspace")
    if isinstance(workspace, dict):
        workspace = workspace.get("workspace_id") or workspace.get("id")
    return (result.get("workspace_id") or workspace
            if isinstance(result.get("workspace_id") or workspace, str) else None)


def _proc_start(pid):
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_bytes()
        fields = stat[stat.rindex(b") ") + 2:].split()
        return fields[19].decode("ascii")
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return None


def _safe_argv(argv):
    safe = []
    redact_next = False
    for arg in argv:
        if redact_next:
            try:
                from urllib.parse import urlsplit
                parsed = urlsplit(arg)
                safe.append(f"{parsed.scheme}://{parsed.hostname or ''}/[redacted]")
            except Exception:
                safe.append("[redacted]")
            redact_next = False
            continue
        if arg in {"--attach-url", "--backend-url"}:
            safe.append(arg)
            redact_next = True
        elif "://" in arg:
            try:
                from urllib.parse import urlsplit
                parsed = urlsplit(arg)
                safe.append(f"{parsed.scheme}://{parsed.hostname or ''}/[redacted]")
            except Exception:
                safe.append("[redacted-url]")
        else:
            safe.append(arg)
    return safe


def isolated_environment(output_dir: Path, project: Path) -> dict:
    """Keep all runtime homes/state inside the evidence directory and drop inherited credentials."""
    root = output_dir / "isolated"
    home = root / "home"
    config = root / "config"
    data = root / "data"
    state = root / "state"
    cache = root / "cache"
    runtime = root / "runtime"
    temp = root / "tmp"
    for directory in (home, config, data, state, cache, runtime, temp,
                      config / "herdr", config / "opencode", state / "peer-ledger"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib_suppress_oserror():
            directory.chmod(0o700)
    env = {key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key)}
    for key in ("HERDR_SOCKET_PATH", "HERDR_PANE_ID", "AGENT_HERDR_SESSION", "HERDR_SESSION",
                "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_SESSION_ID", "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG"):
        env.pop(key, None)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "XDG_CACHE_HOME": str(cache),
        "XDG_RUNTIME_DIR": str(runtime),
        "TMPDIR": str(temp),
        "HERDR_CONFIG_PATH": str(config / "herdr" / "config.toml"),
        "OPENCODE_CONFIG_DIR": str(config / "opencode"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "AGENT_HOME": str(ROOT),
        "AGENT_PEER_LEDGER_ROOT": str(state / "peer-ledger"),
        "AGENT_DISPATCH_JOBS": str(state / "jobs.log"),
        "HEARTING_HERDR_LIVE_ROOT": str(output_dir),
        "HEARTING_HERDR_LIVE_OBSERVATIONS": str(output_dir / "native-events.jsonl"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


class contextlib_suppress_oserror:
    def __enter__(self):
        return self
    def __exit__(self, typ, value, tb):
        return typ is not None and issubclass(typ, OSError)


def stop_owned_process(proc: subprocess.Popen | None, *, timeout: float = 3.0) -> str:
    """Stop only a subprocess group created by this runner; always reap the direct child."""
    if proc is None:
        return "not-started"
    if proc.poll() is not None:
        return "already-exited"
    try:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass
    except OSError:
        with suppress_process_errors():
            proc.terminate()
    try:
        proc.wait(timeout=max(0.1, timeout))
        return "terminated"
    except subprocess.TimeoutExpired:
        try:
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except OSError:
            with suppress_process_errors():
                proc.kill()
        with suppress_process_errors():
            proc.wait(timeout=2)
        return "killed-after-timeout"


class suppress_process_errors:
    def __enter__(self):
        return self
    def __exit__(self, typ, value, tb):
        return typ is not None and issubclass(typ, (OSError, subprocess.SubprocessError))


class LiveRunner:
    def __init__(self, args):
        self.args = args
        self.output_root = Path(args.output_dir).expanduser().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.project = Path(args.project).expanduser().resolve(strict=True)
        if not self.project.is_dir():
            raise LiveRunError("sandbox project is not a directory")
        self.herdr = shutil.which(args.herdr) if os.path.sep not in args.herdr else args.herdr
        self.opencode = shutil.which(args.opencode) if os.path.sep not in args.opencode else args.opencode
        if not self.herdr or not Path(self.herdr).is_file():
            raise LiveRunError("herdr executable unavailable")
        if not self.opencode or not Path(self.opencode).is_file():
            raise LiveRunError("opencode executable unavailable")
        self.session = args.session_name or ("hearting-live-" + uuid.uuid4().hex[:12])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.session):
            raise LiveRunError("named session must use 1-32 letters, digits, underscore or hyphen")
        self.output_dir = self.output_root / self.session
        try:
            self.output_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise LiveRunError("output directory already contains this named session") from exc
        self.env = isolated_environment(self.output_dir, self.project)
        self.deadline = time.monotonic() + args.timeout
        self.salt = os.urandom(32)
        self.steps = []
        self.server = None
        self.backend = None
        self.pane_id = None
        self.workspace_id = None
        self.server_started = False
        self.initial_panes = set()
        self.observations = []
        self.cleanup = []
        self.report = {
            "schema": "hearting-herdr-opencode-live-v1",
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "output_dir": str(self.output_dir),
            "sandbox_project": str(self.project),
            "named_session": self.session,
            "isolation": {"home": self.env["HOME"], "xdg_config": self.env["XDG_CONFIG_HOME"],
                          "xdg_data": self.env["XDG_DATA_HOME"], "xdg_state": self.env["XDG_STATE_HOME"],
                          "xdg_cache": self.env["XDG_CACHE_HOME"],
                          "herdr_config": self.env["HERDR_CONFIG_PATH"],
                          "opencode_config": self.env["OPENCODE_CONFIG_DIR"],
                          "credentials_copied": False},
            "versions": {}, "steps": self.steps, "observations": self.observations,
            "cleanup": self.cleanup,
            "interaction_requested": bool(args.interactive),
            "verification": "incomplete",
        }

    def remaining(self, cap=None):
        left = max(0.05, self.deadline - time.monotonic())
        return min(left, cap) if cap else left

    def command(self, name, args, *, timeout=None, input_text=None, screen=False,
                deadline_bound=True):
        argv = [self.herdr, "--session", self.session, *args]
        started = time.monotonic()
        try:
            done = subprocess.run(argv, cwd=self.project, env=self.env, text=True,
                                  input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=(self.remaining(timeout or self.args.command_timeout)
                                           if deadline_bound else max(0.1, timeout or 5)), check=False)
            stdout, stderr, returncode = done.stdout or "", done.stderr or "", done.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            returncode = None
        elapsed = time.monotonic() - started
        raw = stdout.encode("utf-8", "replace")
        digest = hmac.new(self.salt, raw, hashlib.sha256).hexdigest()
        self.steps.append({"name": name, "argv": _safe_argv(argv),
                           "argv_hmac": hmac.new(self.salt, json.dumps(argv).encode(), hashlib.sha256).hexdigest(),
                           "returncode": returncode, "elapsed_ms": int(elapsed * 1000),
                           "stdout_bytes": len(raw), "stdout_hmac": digest,
                           "stderr_bytes": len(stderr.encode("utf-8", "replace")),
                           "stderr_hmac": hmac.new(self.salt, stderr.encode("utf-8", "replace"), hashlib.sha256).hexdigest(),
                           "output_redacted": True})
        return returncode, stdout, stderr

    def runtime_command(self, name, args, *, timeout=15):
        argv = [self.opencode, *args]
        started = time.monotonic()
        try:
            done = subprocess.run(argv, cwd=self.project, env=self.env, text=True,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=self.remaining(timeout), check=False)
            stdout, stderr, returncode = done.stdout or "", done.stderr or "", done.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            returncode = None
        raw = stdout.encode("utf-8", "replace")
        self.steps.append({"name": name, "argv": _safe_argv(argv),
                           "argv_hmac": hmac.new(self.salt, json.dumps(argv).encode(), hashlib.sha256).hexdigest(),
                           "returncode": returncode,
                           "elapsed_ms": int((time.monotonic() - started) * 1000),
                           "stdout_bytes": len(raw),
                           "stdout_hmac": hmac.new(self.salt, raw, hashlib.sha256).hexdigest(),
                           "stderr_hmac": hmac.new(self.salt, stderr.encode("utf-8", "replace"), hashlib.sha256).hexdigest()})
        return returncode, stdout, stderr

    def start_server(self):
        argv = [self.herdr, "--session", self.session, "server"]
        self.server = subprocess.Popen(argv, cwd=self.project, env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       close_fds=True)
        end_with_test(self.server)
        self.server_started = True
        self.steps.append({"name": "server-start", "argv": _safe_argv(argv),
                           "argv_hmac": hmac.new(self.salt, json.dumps(argv).encode(), hashlib.sha256).hexdigest(),
                           "pid": self.server.pid, "pid_start": _proc_start(self.server.pid)})
        deadline = min(self.deadline, time.monotonic() + self.args.ready_timeout)
        last_rc = None
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise LiveRunError(f"named Herdr server exited early ({self.server.returncode})")
            last_rc, stdout, stderr = self.command("initial-pane-list", ["pane", "list"], timeout=2)
            payload = _json(stdout)
            if last_rc == 0 and payload and not payload.get("error"):
                self.initial_panes = _pane_ids(payload)
                if self.initial_panes:
                    raise LiveRunError("fresh named session unexpectedly contains panes")
                return
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise LiveRunError(f"named Herdr server did not become ready (last rc={last_rc})")

    def create_pane(self):
        label = "hearting-live-" + uuid.uuid4().hex[:8]
        rc, stdout, _stderr = self.command("workspace-create",
            ["workspace", "create", "--cwd", str(self.project), "--label", label, "--no-focus"], timeout=10)
        payload = _json(stdout)
        if rc != 0 or not payload or payload.get("error"):
            raise LiveRunError("isolated workspace creation failed")
        self.workspace_id = _workspace_id(payload)
        panes = _pane_ids(payload)
        if len(panes) == 1:
            self.pane_id = next(iter(panes))
            return
        rc, stdout, _stderr = self.command("pane-list-after-workspace", ["pane", "list"], timeout=5)
        payload = _json(stdout)
        new_panes = (_pane_ids(payload) - self.initial_panes) if payload else set()
        if len(new_panes) == 1:
            self.pane_id = next(iter(new_panes))
            return
        if not self.workspace_id:
            raise LiveRunError("workspace response did not expose its workspace id")
        rc, stdout, _stderr = self.command("tab-create",
            ["tab", "create", "--workspace", self.workspace_id, "--cwd", str(self.project), "--no-focus"],
            timeout=10)
        payload = _json(stdout)
        if rc != 0 or not payload or payload.get("error"):
            raise LiveRunError("isolated tab creation failed")
        panes = _pane_ids(payload)
        if len(panes) == 1:
            self.pane_id = next(iter(panes))
        else:
            rc, stdout, _stderr = self.command("pane-list-after-tab", ["pane", "list"], timeout=5)
            payload = _json(stdout)
            new_panes = (_pane_ids(payload) - self.initial_panes) if payload else set()
            if len(new_panes) != 1:
                raise LiveRunError("could not identify the one pane created by this runner")
            self.pane_id = next(iter(new_panes))

    def start_backend(self):
        if not self.args.backend_argv_file:
            return
        try:
            argv = json.loads(Path(self.args.backend_argv_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LiveRunError(f"backend argv file is unreadable ({type(exc).__name__})") from exc
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
            raise LiveRunError("backend argv file must contain a non-empty JSON string array")
        if not Path(argv[0]).is_absolute() and not shutil.which(argv[0]):
            raise LiveRunError("backend executable unavailable")
        self.backend = subprocess.Popen(argv, cwd=self.project, env=self.env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        end_with_test(self.backend)
        self.steps.append({"name": "backend-start", "argv_hmac": hmac.new(
            self.salt, json.dumps(argv).encode(), hashlib.sha256).hexdigest(), "pid": self.backend.pid,
            "pid_start": _proc_start(self.backend.pid), "argv_redacted": True})

    def start_agent(self):
        args = ["agent", "start", "oc-live", "--kind", "opencode", "--pane", self.pane_id,
                "--timeout", str(min(300000, max(1000, int(self.remaining() * 1000))))]
        if self.args.mode == "full":
            args += ["--", str(self.project)]
        elif self.args.mode == "mini":
            args += ["--", str(self.project), "--mini"]
        else:
            if not self.args.attach_url:
                raise LiveRunError("attach mode needs --attach-url")
            args += ["--", "attach", self.args.attach_url, "--dir", str(self.project)]
        rc, _stdout, _stderr = self.command("agent-start", args, timeout=min(300, self.remaining()))
        if rc != 0:
            raise LiveRunError(f"Herdr agent start failed (rc={rc})")

    def _agent(self):
        rc, stdout, _stderr = self.command("agent-get", ["agent", "get", "oc-live"], timeout=5)
        payload = _json(stdout)
        result = _result(payload)
        agent = result.get("agent") if isinstance(result, dict) else None
        if rc != 0 or not isinstance(agent, dict):
            return None
        session = agent.get("agent_session")
        sid = session.get("value") if isinstance(session, dict) else None
        return {"name": agent.get("name"), "harness": agent.get("agent"),
                "pane_id": agent.get("pane_id"), "cwd": agent.get("cwd"),
                "status": agent.get("status"), "agent_session_id": sid}

    def _process_info(self):
        rc, stdout, _stderr = self.command("pane-process-info",
            ["pane", "process-info", "--pane", self.pane_id], timeout=5)
        payload = _json(stdout)
        result = _result(payload)
        info = result.get("process_info") if isinstance(result, dict) else None
        processes = info.get("foreground_processes") if isinstance(info, dict) else None
        return processes if rc == 0 and isinstance(processes, list) else []

    def _screen(self):
        rc, stdout, _stderr = self.command("pane-read-visible",
            ["pane", "read", self.pane_id, "--source", "visible", "--format", "text"],
            timeout=5, screen=True)
        draft = "unknown"
        if rc == 0:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("live_peer_steward", ROOT / "utilities" / "peer-steward.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                draft = module._draft_state("opencode", stdout.splitlines())
            except Exception:
                draft = "unknown"
        raw = stdout.encode("utf-8", "replace")
        return {"read": rc == 0, "draft_state": draft, "visible_bytes": len(raw),
                "visible_hmac": hmac.new(self.salt, raw, hashlib.sha256).hexdigest()}

    def _snapshot(self):
        rc, stdout, _stderr = self.command("api-snapshot", ["api", "snapshot"], timeout=5)
        payload = _json(stdout)
        return {"command_ok": rc == 0, "pane_ids": sorted(_pane_ids(payload)) if payload else [],
                "stdout_bytes": len(stdout.encode("utf-8", "replace"))}

    def _export_ids(self, sid):
        if not sid:
            return []
        # Sanitized native export; only message IDs and roles leave memory.
        rc, stdout, _stderr = self.runtime_command("opencode-export-sanitized",
            ["export", sid, "--sanitize"], timeout=15)
        if rc != 0:
            return []
        payload = _json(stdout)
        rows = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = payload.get("data") if isinstance(payload, dict) else None
        found = []
        for row in rows if isinstance(rows, list) else []:
            info = row.get("info") if isinstance(row, dict) and isinstance(row.get("info"), dict) else row
            if not isinstance(info, dict):
                continue
            message_id = info.get("id") or info.get("messageID")
            role = info.get("role")
            if isinstance(message_id, str) and role in {"user", "assistant"}:
                found.append({"message_id": message_id, "role": role})
        return found[-100:]

    def _native_events(self):
        path = self.output_dir / "native-events.jsonl"
        rows = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append({key: row[key] for key in (
                        "event", "session_id", "kind", "request_id", "status", "message_id", "role", "at")
                        if key in row})
        except OSError:
            pass
        return rows[-200:]

    def _peer_state(self, sid):
        root = Path(self.env["AGENT_PEER_LEDGER_ROOT"]) / "peer-messages"
        result = []
        for path in (root / "pending").glob("*.json") if (root / "pending").is_dir() else []:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            recipient = row.get("to") or row.get("recipient") or row.get("bound_to") or {}
            if isinstance(recipient, dict) and recipient.get("session_id") not in (None, sid):
                continue
            ref = row.get("ref") or row.get("message_id") or path.stem
            result.append({"ref": ref, "state": row.get("state"),
                           "actual_message_id": row.get("actual_message_id"),
                           "receipt": row.get("receipt")})
        if root.is_dir():
            for path in sorted(root.glob("????-??/*.jsonl")):
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    recipient = row.get("to") or {}
                    if isinstance(recipient, dict) and recipient.get("session_id") not in (None, sid):
                        continue
                    delivery = row.get("delivery") if isinstance(row.get("delivery"), dict) else {}
                    ref = row.get("transfer_ref") or row.get("message_id")
                    if ref and (row.get("kind") == "peer" or delivery):
                        result.append({"ref": ref,
                                       "state": delivery.get("status") or row.get("status"),
                                       "actual_message_id": row.get("actual_message_id"),
                                       "receipt": delivery.get("receipt") or row.get("receipt")})
        return result[-200:]

    def observe(self, label):
        agent = self._agent()
        processes = self._process_info()
        sid = agent.get("agent_session_id") if agent else None
        native_sid = None
        pid_record = None
        process_rows = []
        tools = str(ROOT / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        try:
            from fleet.process_identity import PROVEN, process_identity
        except Exception:
            process_identity, PROVEN = None, "proven"
        for process in processes:
            if not isinstance(process, dict):
                continue
            pid = process.get("pid")
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            start = _proc_start(pid)
            identity = process_identity(pid, "opencode") if process_identity else None
            proven = bool(identity and identity.confidence == PROVEN)
            if proven and sid and identity.session_id == sid:
                native_sid = identity.session_id
                pid_record = {"pid": pid, "pid_start": start, "session_id": identity.session_id,
                              "identity_source": identity.source, "confidence": identity.confidence}
            argv = process.get("argv") if isinstance(process.get("argv"), list) else []
            process_rows.append({"pid": pid, "pid_start": start,
                                 "executable": Path(str(argv[0])).name if argv else None,
                                 "identity_source": identity.source if identity else None,
                                 "identity_confidence": identity.confidence if identity else None,
                                 "session_id": identity.session_id if proven else None,
                                 "cwd": process.get("cwd")})
        snapshot = self._snapshot()
        screen = self._screen()
        messages = self._export_ids(native_sid or sid)
        observation = {"label": label, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "pane_id": self.pane_id, "herdr_agent": agent,
                       "native_session_id": native_sid, "herdr_session_id": sid,
                       "root_session_id": native_sid if native_sid == sid else None,
                       "foreground_processes": process_rows, "selected_pid": pid_record,
                       "snapshot": snapshot, "draft": screen,
                       "native_messages": messages, "native_events": self._native_events(),
                       "peer_delivery": self._peer_state(native_sid or sid)}
        self.observations.append(observation)
        return observation

    def versions(self):
        for name, executable in (("herdr", self.herdr), ("opencode", self.opencode)):
            try:
                done = subprocess.run([executable, "--version"], cwd=self.project, env=self.env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True,
                                      timeout=min(5, self.remaining()))
                self.report["versions"][name] = {"returncode": done.returncode,
                    "output": (done.stdout or done.stderr or "").strip()[:160]}
            except (OSError, subprocess.SubprocessError) as exc:
                self.report["versions"][name] = {"error": type(exc).__name__}

    def interactive_attach(self):
        argv = [self.herdr, "--session", self.session, "agent", "attach", "oc-live"]
        started = time.monotonic()
        try:
            done = subprocess.run(argv, cwd=self.project, env=self.env, stdin=None,
                                  stdout=None, stderr=None,
                                  timeout=self.remaining())
            self.steps.append({"name": "interactive-agent-attach", "argv": _safe_argv(argv),
                               "returncode": done.returncode,
                               "elapsed_ms": int((time.monotonic() - started) * 1000),
                               "terminal_output_not_recorded": True})
        except subprocess.TimeoutExpired:
            self.steps.append({"name": "interactive-agent-attach", "argv": _safe_argv(argv),
                               "returncode": None, "timed_out": True,
                               "terminal_output_not_recorded": True})

    def shutdown(self):
        if self.pane_id and self.server_started:
            try:
                rc, _out, _err = self.command("cleanup-close-pane", ["pane", "close", self.pane_id],
                                              timeout=5, deadline_bound=False)
                self.cleanup.append({"action": "close-created-pane", "pane_id": self.pane_id,
                                     "returncode": rc})
            except Exception as exc:
                self.cleanup.append({"action": "close-created-pane", "pane_id": self.pane_id,
                                     "error": type(exc).__name__})
        if self.server_started:
            try:
                rc, _out, _err = self.command("cleanup-stop-named-server", ["server", "stop"],
                                              timeout=8, deadline_bound=False)
                self.cleanup.append({"action": "stop-named-server", "session": self.session,
                                     "returncode": rc})
            except Exception as exc:
                self.cleanup.append({"action": "stop-named-server", "session": self.session,
                                     "error": type(exc).__name__})
        if self.backend is not None:
            try:
                result = stop_owned_process(self.backend)
                self.cleanup.append({"action": "stop-owned-backend", "result": result})
            except Exception as exc:
                self.cleanup.append({"action": "stop-owned-backend", "error": type(exc).__name__})
        if self.server is not None:
            try:
                result = stop_owned_process(self.server, timeout=2)
                self.cleanup.append({"action": "reap-owned-server-process", "result": result})
            except Exception as exc:
                self.cleanup.append({"action": "reap-owned-server-process", "error": type(exc).__name__})

    def execute(self):
        try:
            self.versions()
            self.start_server()
            self.start_backend()
            self.create_pane()
            self.start_agent()
            before = self.observe("before-interaction")
            after = None
            if self.args.interactive:
                self.interactive_attach()
                after = self.observe("after-interaction")
            self.report["runner_status"] = "observed"
            self.report["verification"] = "incomplete"
            observed = [item for item in (before, after) if item]
            events = [event for item in observed for event in item.get("native_events", [])]
            opened = {(event.get("kind"), event.get("request_id")) for event in events
                      if event.get("status") == "open" and event.get("request_id")}
            resolved = {(event.get("kind"), event.get("request_id")) for event in events
                        if event.get("status") in {"resolved", "rejected"} and event.get("request_id")}
            missing = []
            if not opened or not (opened & resolved):
                missing.append("native question/answer id pair")
            deliveries = [delivery for item in observed for delivery in item.get("peer_delivery", [])]
            if not any(row.get("state") == "received" and row.get("ref") for row in deliveries):
                missing.append("exact queued-to-consumed peer ref receipt")
            drafts = [item.get("draft") or {} for item in observed]
            if not (len(drafts) == 2 and all(row.get("draft_state") == "nonempty" for row in drafts)
                    and drafts[0].get("visible_hmac") == drafts[1].get("visible_hmac")):
                missing.append("paired unchanged nonempty draft observation")
            missing.append("separately supervised resume and clear action matrix")
            self.report["missing_live_checks"] = missing
            identity = after or before
            if not identity.get("native_session_id") or not identity.get("selected_pid"):
                self.report["runner_status"] = "ready-for-interaction" if not self.args.interactive else "identity-unproven"
                return 2
            return 0
        except (LiveRunError, OSError, subprocess.SubprocessError) as exc:
            self.report["runner_status"] = "blocked"
            self.report["blocker"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            return 2
        finally:
            try:
                self.shutdown()
            except Exception as exc:
                self.cleanup.append({"action": "cleanup-unhandled-error", "error": type(exc).__name__})
            self.report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                self.write_report()
            except Exception as exc:
                print(f"live runner report write failed: {type(exc).__name__}", file=sys.stderr)

    def write_report(self):
        target = self.output_dir / "live-observations.json"
        temporary = target.with_suffix(".json.tmp")
        data = json.dumps(self.report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary.write_text(data, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        print(json.dumps({"runner_status": self.report.get("runner_status", "blocked"),
                          "verification": self.report.get("verification", "incomplete"),
                          "artifact": str(target)}, ensure_ascii=False))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="isolated evidence and runtime root")
    parser.add_argument("--project", required=True, help="existing sandbox project directory")
    parser.add_argument("--timeout", type=int, default=900,
                        help="whole-run deadline in seconds (maximum 900)")
    parser.add_argument("--ready-timeout", type=int, default=30)
    parser.add_argument("--command-timeout", type=int, default=15)
    parser.add_argument("--session-name", help="unique named Herdr endpoint (generated by default)")
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--mode", choices=("full", "mini", "attach"), default="full")
    parser.add_argument("--attach-url", help="URL for the supported `opencode attach URL --dir PROJECT` form")
    parser.add_argument("--backend-argv-file", help="JSON string array for an owned attach backend")
    parser.add_argument("--interactive", action="store_true",
                        help="attach to oc-live in the foreground for manual isolated-window interaction")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.timeout > MAX_TIMEOUT:
        parser.error(f"--timeout must be between 1 and {MAX_TIMEOUT} seconds")
    if args.ready_timeout < 1 or args.command_timeout < 1:
        parser.error("ready and command timeouts must be positive")
    if args.mode == "attach" and not args.attach_url:
        parser.error("--mode attach requires --attach-url")
    try:
        runner = LiveRunner(args)
    except (LiveRunError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {str(exc)[:240]}", file=sys.stderr)
        return 2
    return runner.execute()


if __name__ == "__main__":
    raise SystemExit(main())
