#!/usr/bin/env python3
"""Run one behaviour scenario on the real Claude, Codex and OpenCode TUIs.

Each harness runs in its own throwaway HOME with Hearting installed from this
source tree (``tools/install/harness.sh install all`` under
``tools/install/fixture_env.py``).  The model is a loopback fixture that speaks
the Anthropic Messages, OpenAI Responses and OpenAI Chat Completions streaming
protocols, records every request, and answers from a small per-scenario script.
The TUI runs inside a private tmux socket owned by the fixture, so no user
session, window, tmux server or shared daemon is touched.

The oracle is what reached the model: a wake carrier is real only when the
model receives a new request that carries the receipt text without a typed
prompt; a stop carrier is real only when the model is asked again after it
tried to end the turn.  This is the behaviour the user sees, measured on the
installed hooks and plugins rather than on documents or synthetic payloads.

Usage::

    python3 utilities/harness_behavior_scenarios.py --list
    python3 utilities/harness_behavior_scenarios.py --scenario all \
        --harness claude,codex,opencode --output-dir DIR [--keep]

The JSON result (one row per harness x scenario) is printed and written to
``DIR/results.json``; ``DIR/results.md`` is the comparison table.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
# The Hearting tree installed into each fixture HOME.  ``--source`` points it at
# another checkout (read-only) to measure that tree's uncommitted carriers.
SOURCE = ROOT
FIXTURE_MODEL = "hearting-fixture"
# Further model ids the fixture lists (a scenario adds the ids its agents name).
EXTRA_MODELS: list[str] = []
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
HARNESSES = ("claude", "codex", "opencode")
NATIVE_ENV = {
    "claude": "HEARTING_SCENARIO_CLAUDE_BIN",
    "codex": "HEARTING_SCENARIO_CODEX_BIN",
    "opencode": "HEARTING_SCENARIO_OPENCODE_BIN",
}
SESSION_SHELL_HEADER = re.compile(r'#\s*hearting-session-shell\s+(\{.*\})')


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# Native binary resolution
# --------------------------------------------------------------------------

def native_binary(harness: str) -> str | None:
    """Return the native harness binary, unwrapping a Hearting session shell."""
    override = os.environ.get(NATIVE_ENV[harness])
    if override:
        return override
    found = shutil.which(harness)
    if not found:
        return None
    try:
        head = Path(found).read_text(encoding="utf-8", errors="replace")[:600]
    except OSError:
        return found
    match = SESSION_SHELL_HEADER.search(head)
    if match:
        try:
            native = json.loads(match.group(1)).get("native")
        except ValueError:
            native = None
        if native and Path(native).exists():
            return native
    return found


# --------------------------------------------------------------------------
# Loopback model fixture
# --------------------------------------------------------------------------

GATE_LIMIT = 170.0   # the longest a gated reply waits, so a forgotten gate never hangs a run


class Reply:
    """One scripted model answer: plain text, a single shell tool call, or one
    named tool call (``tool=(name, arguments)``) such as a skill load.
    ``input_tokens`` is the prompt size the answer reports (default 1).
    ``gate`` (a ``threading.Event``) holds the answer: the request is recorded at once, the
    reply is sent only once the event is set (or after ``GATE_LIMIT`` seconds), so a turn
    stays running on the model's side."""

    def __init__(self, text: str = "", shell: str | None = None, tool=None,
                 input_tokens: int = 1, gate=None):
        self.gate = gate
        self.text = text
        self.shell = shell
        self.tool = tool
        self.input_tokens = input_tokens

    def call(self, protocol: str, names):
        """The (name, arguments) of this reply's tool call, or None."""
        if self.shell:
            return _shell_call(protocol, names, self.shell)
        return self.tool

    def as_dict(self):
        value = {"text": self.text, "shell": self.shell}
        if self.tool:
            value["tool"] = list(self.tool)
        return value


def _flatten_text(value) -> str:
    out: list[str] = []

    def walk(node):
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in ("tools", "functions"):
                    continue
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return "\n".join(out)


def _last_turn(payload: dict, protocol: str):
    """Return (kind, text) for the newest conversation item.

    kind is ``user`` for a prompt (or injected context), ``tool`` for a tool
    result, ``assistant`` otherwise.
    """
    if protocol == "anthropic":
        # Claude Code may append a trailing ``system`` turn after the prompt.
        messages = [m for m in payload.get("messages") or []
                    if isinstance(m, dict) and m.get("role") != "system"]
        if not messages:
            return "none", ""
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
            return "tool", _flatten_text(content)
        return last.get("role", "user"), _flatten_text(content)
    if protocol == "responses":
        items = payload.get("input") or []
        if isinstance(items, str):
            return "user", items
        if not items:
            return "none", ""
        last = items[-1]
        if last.get("type") in ("function_call_output", "custom_tool_call_output",
                                "local_shell_call_output"):
            return "tool", _flatten_text(last.get("output"))
        return last.get("role", "user"), _flatten_text(last.get("content"))
    messages = payload.get("messages") or []
    if not messages:
        return "none", ""
    last = messages[-1]
    if last.get("role") == "tool":
        return "tool", _flatten_text(last.get("content"))
    return last.get("role", "user"), _flatten_text(last.get("content"))


def _user_inputs(payload: dict, protocol: str) -> list[dict]:
    """Every person-role input in the request, oldest first: ``{"id", "text"}``.

    One item per user text part (Anthropic, chat) or user message (Responses, which
    carries the message id).  Tool results are not inputs.  A scenario reads what
    actually reached the model from this, not from the newest item alone.
    """
    out: list[dict] = []

    def parts(content):
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [p.get("text") for p in content if isinstance(p, dict)
                    and p.get("type") in ("text", "input_text") and isinstance(p.get("text"), str)]
        return []
    if protocol == "responses":
        items = payload.get("input") or []
        if isinstance(items, str):
            return [{"id": None, "text": items}]
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" \
                    and item.get("type", "message") == "message":
                out.append({"id": item.get("id"), "text": "\n".join(parts(item.get("content")))})
        return out
    for message in payload.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            out.extend({"id": None, "text": text} for text in parts(message.get("content")))
    return out


def _tool_names(payload: dict) -> list[str]:
    names = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name") or tool.get("type")
        if name:
            names.append(str(name))
    return names


def _shell_call(protocol: str, names: list[str], command: str):
    """Pick the harness shell tool and its argument shape."""
    if protocol == "chat":
        name = "shell" if "shell" in names else "bash"
        return name, {"command": command, "description": "scenario step"}
    if protocol == "anthropic":
        return "Bash", {"command": command, "description": "scenario step"}
    if protocol == "responses":
        for name in ("exec_command", "shell_command", "shell", "local_shell"):
            if name in names:
                if name == "exec_command":
                    return name, {"cmd": command}
                if name == "shell_command":
                    return name, {"command": command}
                return name, {"command": ["bash", "-lc", command]}
        return "shell", {"command": ["bash", "-lc", command]}
    return "bash", {"command": command, "description": "scenario step"}


class ModelFixture:
    """Threaded loopback server for all three model wire protocols."""

    def __init__(self, script, side=None):
        # ``side`` answers a request that offers no tools (a title, or a harness's own
        # compaction request); returning None keeps the default title reply.
        self.script, self.side = script, side
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _send(self, code, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if ctype == "text/event-stream":
                    self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(body)
                    self.wfile.flush()

            def do_GET(self):
                path = self.path.split("?")[0].rstrip("/")
                if path.endswith("/models"):
                    body = json.dumps({"object": "list", "data": [{
                        "id": name, "object": "model", "created": 0,
                        "owned_by": "hearting-fixture", "type": "model",
                        "display_name": name} for name in (FIXTURE_MODEL, *EXTRA_MODELS)]}).encode()
                    self._send(200, body)
                    return
                self._send(404, b"{}")

            def do_HEAD(self):
                self._send(200, b"")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(min(length, 16 * 1024 * 1024))
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                path = self.path.split("?")[0].rstrip("/")
                if path.endswith("/messages/count_tokens"):
                    self._send(200, b'{"input_tokens":1}')
                    return
                if path.endswith("/messages"):
                    protocol = "anthropic"
                elif path.endswith("/responses"):
                    protocol = "responses"
                elif path.endswith("/chat/completions"):
                    protocol = "chat"
                else:
                    self._send(404, b"{}")
                    return
                body, ctype = owner._answer(protocol, payload, raw)
                self._send(200, body, ctype)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    # -- recording -------------------------------------------------------
    def _answer(self, protocol: str, payload: dict, raw: bytes):
        names = _tool_names(payload)
        kind, last_text = _last_turn(payload, protocol)
        text = _flatten_text({k: v for k, v in payload.items() if k != "tools"})
        main = bool(names)
        with self.lock:
            index = len([r for r in self.requests if r["main"]])
            record = {
                "at": _now(), "protocol": protocol, "main": main,
                # The model id the harness asked for (a sub-agent's own model, PG-13).
                "model": payload.get("model"),
                "main_index": index if main else None,
                "last_kind": kind, "last_text": last_text[-4000:],
                "text": text, "tools": names[:60],
                "user_inputs": _user_inputs(payload, protocol),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            reply = self.script(record, [r for r in self.requests if r["main"]]) if main \
                else (self.side and self.side(record)) or Reply("fixture title")
            record["reply"] = reply.as_dict()
            self.requests.append(record)
        if reply.gate is not None:
            reply.gate.wait(GATE_LIMIT)
        streaming = bool(payload.get("stream", protocol != "anthropic"))
        if protocol == "anthropic":
            return self._anthropic(reply, streaming, names)
        if protocol == "responses":
            return self._responses(reply, names)
        return self._chat(reply, streaming, names)

    def main_requests(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["main"]]

    # -- protocol encoders ---------------------------------------------------
    @staticmethod
    def _sse(events, named=True) -> bytes:
        chunks = []
        for event in events:
            data = json.dumps(event, separators=(",", ":"))
            if named:
                chunks.append(f"event: {event['type']}\ndata: {data}\n\n")
            else:
                chunks.append(f"data: {data}\n\n")
        return "".join(chunks).encode()

    def _anthropic(self, reply: Reply, streaming: bool, names):
        mid = "msg_fx_" + uuid.uuid4().hex[:20]
        blocks = []
        if reply.text:
            blocks.append({"type": "text", "text": reply.text})
        call = reply.call("anthropic", names)
        if call:
            name, args = call
            blocks.append({"type": "tool_use", "id": "toolu_fx_" + uuid.uuid4().hex[:20],
                           "name": name, "input": args})
        if not blocks:
            blocks.append({"type": "text", "text": "OK"})
        stop = "tool_use" if call else "end_turn"
        usage = {"input_tokens": reply.input_tokens, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not streaming:
            body = {"id": mid, "type": "message", "role": "assistant", "model": FIXTURE_MODEL,
                    "content": blocks, "stop_reason": stop, "stop_sequence": None, "usage": usage}
            return json.dumps(body).encode(), "application/json"
        events = [{"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": FIXTURE_MODEL,
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}]
        for index, block in enumerate(blocks):
            if block["type"] == "text":
                events.append({"type": "content_block_start", "index": index,
                               "content_block": {"type": "text", "text": ""}})
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "text_delta", "text": block["text"]}})
            else:
                events.append({"type": "content_block_start", "index": index,
                               "content_block": {"type": "tool_use", "id": block["id"],
                                                 "name": block["name"], "input": {}}})
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "input_json_delta",
                                         "partial_json": json.dumps(block["input"])}})
            events.append({"type": "content_block_stop", "index": index})
        events.append({"type": "message_delta", "delta": {"stop_reason": stop,
                                                          "stop_sequence": None},
                       "usage": {"output_tokens": 1}})
        events.append({"type": "message_stop"})
        return self._sse(events), "text/event-stream"

    def _responses(self, reply: Reply, names):
        rid = "resp_fx_" + uuid.uuid4().hex
        output = []
        if reply.text:
            output.append({"id": "msg_fx_" + uuid.uuid4().hex, "type": "message",
                           "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": reply.text,
                                        "annotations": []}]})
        call = reply.call("responses", names)
        if call:
            name, args = call
            output.append({"id": "fc_fx_" + uuid.uuid4().hex, "type": "function_call",
                           "status": "completed", "call_id": "call_fx_" + uuid.uuid4().hex[:16],
                           "name": name, "arguments": json.dumps(args)})
        if not output:
            output.append({"id": "msg_fx_" + uuid.uuid4().hex, "type": "message",
                           "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "OK", "annotations": []}]})
        base = {"id": rid, "object": "response", "created_at": int(_now()),
                "model": FIXTURE_MODEL}
        events = [{"type": "response.created", "response": dict(base, status="in_progress",
                                                                 output=[])}]
        for index, item in enumerate(output):
            events.append({"type": "response.output_item.added", "output_index": index,
                           "item": dict(item, status="in_progress")})
            if item["type"] == "message":
                events.append({"type": "response.output_text.delta", "item_id": item["id"],
                               "output_index": index, "content_index": 0,
                               "delta": item["content"][0]["text"]})
            events.append({"type": "response.output_item.done", "output_index": index,
                           "item": item})
        events.append({"type": "response.completed", "response": dict(
            base, status="completed", output=output,
            usage={"input_tokens": reply.input_tokens, "output_tokens": 1,
                   "total_tokens": reply.input_tokens + 1,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 0}})})
        return self._sse(events) + b"data: [DONE]\n\n", "text/event-stream"

    def _chat(self, reply: Reply, streaming: bool, names):
        cid = "chatcmpl-fx" + uuid.uuid4().hex[:16]
        created = int(_now())
        tool_calls = []
        call = reply.call("chat", names)
        if call:
            name, args = call
            tool_calls.append({"index": 0, "id": "call_fx_" + uuid.uuid4().hex[:16],
                               "type": "function",
                               "function": {"name": name, "arguments": json.dumps(args)}})
        text = reply.text or ("" if tool_calls else "OK")
        finish = "tool_calls" if tool_calls else "stop"
        if not streaming:
            message = {"role": "assistant", "content": text or None}
            if tool_calls:
                message["tool_calls"] = [{k: v for k, v in call.items() if k != "index"}
                                         for call in tool_calls]
            body = {"id": cid, "object": "chat.completion", "created": created,
                    "model": FIXTURE_MODEL,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": reply.input_tokens, "completion_tokens": 1,
                              "total_tokens": reply.input_tokens + 1}}
            return json.dumps(body).encode(), "application/json"

        def chunk(delta, finish_reason=None, usage=None):
            value = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": FIXTURE_MODEL,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if usage is not None:
                value["usage"] = usage
            return value
        events = [chunk({"role": "assistant", "content": ""})]
        if text:
            events.append(chunk({"content": text}))
        if tool_calls:
            events.append(chunk({"tool_calls": tool_calls}))
        events.append(chunk({}, finish,
                            {"prompt_tokens": reply.input_tokens, "completion_tokens": 1,
                             "total_tokens": reply.input_tokens + 1}))
        return self._sse(events, named=False) + b"data: [DONE]\n\n", "text/event-stream"


# --------------------------------------------------------------------------
# Isolated installed home
# --------------------------------------------------------------------------

def _fixture_env(root: Path) -> dict:
    sys.path.insert(0, str(ROOT / "tools" / "install"))
    import fixture_env  # noqa: E402
    base = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "USER", "LOGNAME",
                                              "SHELL") if key in os.environ}
    # The caller's session-shell wrappers would re-enter the operator's runtime.
    base["PATH"] = os.pathsep.join(
        part for part in base.get("PATH", "").split(os.pathsep)
        if part and "session-shell" not in part)
    env = fixture_env.build_environment(root, SOURCE, base=base)
    fixture_env.prepare_environment(env)
    # The fixture never inherits a dispatch or session identity from its caller.
    jobs = env.get("AGENT_DISPATCH_JOBS")
    for key in list(env):
        if key.startswith(("AGENT_DISPATCH", "AGENT_SESSION", "HERDR_", "TMUX",
                           "CLAUDE_CODE", "CODEX_THREAD", "OPENCODE_")):
            env.pop(key, None)
    # Keep the fixture's own registry; without it carriers fall back to
    # ``<AGENT_HOME>/.dispatch`` inside the source checkout.
    if jobs:
        registry = Path(jobs)
        registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        registry.parent.chmod(0o700)  # the registry refuses a group-writable root
        registry.touch()
        registry.chmod(0o600)
        env["AGENT_DISPATCH_JOBS"] = str(registry)
    _hide_shared_surfaces(root, env)
    return env


def _hide_shared_surfaces(root: Path, env: dict):
    """Keep the operator's herdr server and default tmux server out of reach.

    Every PATH entry that carries ``herdr`` is replaced by a symlink copy
    without it, and a bare ``tmux`` resolves its socket inside the fixture.
    """
    shadow_root = root / "path-shadow"
    parts = []
    for index, part in enumerate(env.get("PATH", "").split(os.pathsep)):
        if part and (Path(part) / "herdr").exists():
            shadow = shadow_root / str(index)
            shadow.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                for entry in Path(part).iterdir():
                    link = shadow / entry.name
                    if entry.name != "herdr" and not link.exists():
                        with contextlib.suppress(OSError):
                            link.symlink_to(entry)
            part = str(shadow)
        parts.append(part)
    env["PATH"] = os.pathsep.join(parts)
    tmux_tmp = root / "tmux-tmp"
    tmux_tmp.mkdir(mode=0o700, exist_ok=True)
    env["TMUX_TMPDIR"] = str(tmux_tmp)


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InstalledHome:
    """One throwaway HOME with Hearting installed from this source tree."""

    def __init__(self, root: Path, harness: str, fixture: ModelFixture):
        self.root = root
        self.harness = harness
        self.fixture = fixture
        self.env = _fixture_env(root)
        self.home = Path(self.env["HOME"])
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (root / "report-bundles").mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True,
                       env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.install_log = root / "install.log"

    def install(self) -> dict:
        started = _now()
        with self.install_log.open("w", encoding="utf-8") as log:
            done = subprocess.run(["bash", str(SOURCE / "tools/install/harness.sh"), "install",
                                   self.harness], cwd=str(SOURCE), env=self.env,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=900)
        return {"exit": done.returncode, "seconds": round(_now() - started, 1),
                "log": str(self.install_log)}

    def install_herdr_integration(self) -> dict:
        """Install again with the private ``herdr`` on PATH.

        Hearting projects its pinned Herdr integration itself when it finds
        ``herdr`` at install time (``tools/install/herdr_integration.py``); the
        first install ran with Herdr hidden. Upstream ``herdr integration
        install`` is not used: it follows the projection symlinks back into the
        source tree.
        """
        if not REAL_HERDR:
            return {"status": "herdr-missing"}
        bin_dir = self.root / "herdr-bin"
        bin_dir.mkdir(exist_ok=True)
        link = bin_dir / "herdr"
        if not link.exists():
            link.symlink_to(REAL_HERDR)
        saved = self.env.get("PATH", "")
        self.env["PATH"] = str(bin_dir) + os.pathsep + saved
        try:
            again = self.install()
        finally:
            self.env["PATH"] = saved
        return {"reinstall_exit": again["exit"]}

    # -- per harness model wiring -------------------------------------------
    def configure_model(self):
        getattr(self, f"_configure_{self.harness}")()

    def _configure_claude(self):
        config = Path(self.env["CLAUDE_CONFIG_DIR"])
        key = "sk-ant-fixture-" + "0" * 40
        self.env.update({
            "ANTHROPIC_BASE_URL": self.fixture.base,
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_MODEL": FIXTURE_MODEL,
            "ANTHROPIC_SMALL_FAST_MODEL": FIXTURE_MODEL,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        })
        state_path = config / ".claude.json"
        state = {}
        if state_path.exists():
            with contextlib.suppress(ValueError):
                state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "hasCompletedOnboarding": True, "lastOnboardingVersion": "9.9.9",
            "theme": "dark", "bypassPermissionsModeAccepted": True,
            "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
        })
        projects = state.setdefault("projects", {})
        projects[str(self.workspace)] = {"hasTrustDialogAccepted": True,
                                         "hasCompletedProjectOnboarding": True,
                                         "allowedTools": []}
        state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        settings_path = config / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        permissions = settings.setdefault("permissions", {})
        permissions["defaultMode"] = "bypassPermissions"
        settings["skipDangerousModePermissionPrompt"] = True
        settings_path.write_text(json.dumps(settings, indent=1), encoding="utf-8")

    def _configure_codex(self):
        import tomllib
        config_path = Path(self.env["CODEX_HOME"]) / "config.toml"
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        # Root keys must precede every table; prepend them and drop earlier copies.
        drop = re.compile(r'^(model|model_provider|approval_policy|sandbox_mode|'
                          r'check_for_update_on_startup)\s*=.*$', re.M)
        text = drop.sub("", text)
        head = (f'model = "{FIXTURE_MODEL}"\nmodel_provider = "hearting_fixture"\n'
                'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
                'check_for_update_on_startup = false\n')
        tail = ('\n[model_providers.hearting_fixture]\nname = "Hearting scenario fixture"\n'
                f'base_url = "{self.fixture.base}/v1"\nwire_api = "responses"\n'
                'requires_openai_auth = false\nsupports_websockets = false\n'
                'request_max_retries = 0\nstream_max_retries = 0\n'
                f'\n[projects."{self.workspace}"]\ntrust_level = "trusted"\n')
        merged = head + text + tail
        # Release activation enables the Default-mode question tool
        # (tools/install/runtime_activation.py _codex_feature_insert); a source
        # install skips that step, so apply the same one-line setting here.
        if "default_mode_request_user_input" not in merged:
            if re.search(r"(?m)^\[features\]\s*$", merged):
                merged = re.sub(r"(?m)^\[features\]\s*$",
                                "[features]\ndefault_mode_request_user_input = true",
                                merged, count=1)
            else:
                merged += "\n[features]\ndefault_mode_request_user_input = true\n"
        tomllib.loads(merged)
        config_path.write_text(merged, encoding="utf-8")
        (Path(self.env["CODEX_HOME"]) / "version.json").write_text(json.dumps({
            "latest_version": "0.0.1", "last_checked_at": "2099-01-01T00:00:00Z",
            "dismissed_version": None}), encoding="utf-8")
        self.env["CODEX_HOOK_TRUST_APP_SERVER_COMMAND"] = ""
        self.env.pop("CODEX_HOOK_TRUST_APP_SERVER_COMMAND")

    def trust_codex_hooks(self, binary: str) -> dict:
        helper = (
            "import json,sys; from pathlib import Path; "
            f"sys.path.insert(0,{str(ROOT / 'tools/install')!r}); import codex_hook_trust; "
            "r=codex_hook_trust.record_managed_trust(Path(sys.argv[1]),Path(sys.argv[2]),None,"
            "command=[sys.argv[3],'app-server','--listen','stdio://']); "
            "print(json.dumps(r,default=str))")
        codex_home = Path(self.env["CODEX_HOME"])
        done = subprocess.run([sys.executable, "-B", "-c", helper, str(codex_home),
                               str(codex_home / "hooks.json"), binary], env=self.env,
                              cwd=str(self.workspace), text=True, capture_output=True,
                              timeout=120)
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"exit": done.returncode, "stderr": done.stderr[-800:]}

    def _configure_opencode(self):
        config_dir = Path(self.env["XDG_CONFIG_HOME"]) / "opencode"
        path = config_dir / "opencode.json"
        config = {}
        if path.exists():
            with contextlib.suppress(ValueError):
                config = json.loads(path.read_text(encoding="utf-8"))
        config["model"] = f"fixture/{FIXTURE_MODEL}"
        config["small_model"] = f"fixture/{FIXTURE_MODEL}"
        config["autoupdate"] = False
        config["share"] = "disabled"
        config["permission"] = {"edit": "allow", "bash": "allow", "webfetch": "allow"}
        config.setdefault("provider", {})["fixture"] = {
            "npm": "@ai-sdk/openai-compatible", "name": "Hearting fixture",
            "options": {"baseURL": f"{self.fixture.base}/v1", "apiKey": "fixture"},
            "models": {FIXTURE_MODEL: {"name": FIXTURE_MODEL, "tool_call": True,
                                       "limit": {"context": 200000, "output": 8000}}},
        }
        path.write_text(json.dumps(config, indent=1), encoding="utf-8")
        # Helpers such as the Fleet titler run ``opencode run``, which starts the
        # managed background service.  Pin it to a private port so it can never
        # bind or reach the operator's shared service port.
        (config_dir / "service.json").write_text(
            json.dumps({"port": _free_port()}) + "\n", encoding="utf-8")
        # Release activation (tools/install/runtime_activation.py, opencode branch)
        # places the OpenCode 2 terminal plugins under plugins/<name>/: the session
        # shell's selected-session report and the TUI identity record. A source-tree
        # fixture install skips that step; the identity entry's relative import
        # resolves to the fixture's own tui/ copy.
        for source, name in (("adapters/opencode/tui/hearting-session-shell.js",
                              "hearting-session-shell/tui.js"),
                             ("adapters/opencode/plugins/hearting-tui-identity/tui.ts",
                              "hearting-tui-identity/tui.ts")):
            target = config_dir / "plugins" / name
            if (SOURCE / source).is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / source, target)
        self.env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        self.env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        self.env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "1"


# --------------------------------------------------------------------------
# Private tmux TUI driver
# --------------------------------------------------------------------------

class PrivateTui:
    """A real TUI inside a tmux server that only this fixture owns."""

    def __init__(self, root: Path, env: dict, argv: list[str], cwd: Path):
        # A UNIX socket path is limited to ~107 bytes; keep it short and private.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="hbt-", dir="/tmp"))
        self.socket = self.socket_dir / "s"
        self.env = env
        self.argv = argv
        self.cwd = cwd
        self.started = False

    def _tmux(self, *args, check=False, capture=True):
        return subprocess.run(["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
                              text=True, capture_output=capture, check=check, timeout=30)

    def start(self):
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(self.env.items()))
        # A process in a real tmux pane inherits TMUX and TMUX_PANE; `env -i` would drop them and
        # a pane address (`%0@socket`) could then never match this TUI. Pass the pane's own values.
        command = f"cd {shlex.quote(str(self.cwd))} && env -i TMUX=\"$TMUX\" TMUX_PANE=\"$TMUX_PANE\" {exports} " + \
            " ".join(shlex.quote(a) for a in self.argv) + \
            "; echo HEARTING-SCENARIO-TUI-EXIT=$?; sleep 3600"
        self._tmux("new-session", "-d", "-s", "scenario", "-x", "200", "-y", "50",
                   "sh", "-c", command, check=True)
        self.started = True

    def screen(self) -> str:
        done = self._tmux("capture-pane", "-p", "-t", "scenario", "-S", "-200")
        return ANSI_RE.sub("", done.stdout or "")

    def alive(self) -> bool:
        return self._tmux("has-session", "-t", "scenario").returncode == 0 \
            and "HEARTING-SCENARIO-TUI-EXIT=" not in self.screen()

    def type_text(self, text: str, attempts: int = 4):
        """Type into the composer, confirm it is visible, then submit."""
        probe = text[:24]
        for _ in range(attempts):
            self._tmux("send-keys", "-t", "scenario", "-l", text)
            if wait_for(lambda: probe in self.screen(), 6):
                time.sleep(0.6)
                self._tmux("send-keys", "-t", "scenario", "Enter")
                return True
            # A startup notice may own the keyboard; clear it and retry.
            self._tmux("send-keys", "-t", "scenario", "Escape")
            time.sleep(1.5)
        return False

    def settle(self, quiet: float = 3, timeout: float = 45):
        """Wait until the screen stops changing."""
        deadline = _now() + timeout
        last, since = None, _now()
        while _now() < deadline:
            current = self.screen()
            if current != last:
                last, since = current, _now()
            elif _now() - since >= quiet:
                return True
            time.sleep(0.5)
        return False

    def key(self, *keys: str):
        self._tmux("send-keys", "-t", "scenario", *keys)

    def stop(self):
        if self.started:
            self._tmux("kill-server")
            self.started = False
        shutil.rmtree(self.socket_dir, ignore_errors=True)


# The operator's herdr binary, resolved before any fixture PATH shadowing. The
# ``--surface herdr`` runs start their own server with it; nothing here ever
# connects to the operator's server (HOME, XDG and the socket path are private).
REAL_HERDR = shutil.which("herdr")
SURFACE = "tmux"


def _ids(node, key):
    found = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_ids(value, key))
    elif isinstance(node, list):
        for value in node:
            found.extend(_ids(value, key))
    return found


# The tmux driver class itself, kept apart from the module's ``PrivateTui`` name, which
# companion scenarios replace with a one-shot factory (``harness_behavior_cases_c.install_tui``).
_TMUX_TUI = PrivateTui


def private_herdr_env(root: Path, env: dict) -> str:
    """Point ``env`` at this fixture's private Herdr server; return its session name.

    Idempotent. ``--surface herdr`` calls it before the first install, so every
    long-lived process the fixture starts (the obligation runner that carries
    notices and peer messages is spawned by install and outlives it) sees the
    private ``herdr`` and session, as a person's runner sees their own.
    """
    session = env.get("HERDR_SESSION") or ""
    if not re.fullmatch(r"hb[0-9a-f]{8}", session):
        session = "hb" + uuid.uuid4().hex[:8]
    config = Path(env["XDG_CONFIG_HOME"]) / "herdr"
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    bin_dir = root / "herdr-bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / "herdr"
    if REAL_HERDR and not link.exists():
        link.symlink_to(REAL_HERDR)
    parts = env.get("PATH", "").split(os.pathsep)
    if str(bin_dir) not in parts:
        env["PATH"] = os.pathsep.join([str(bin_dir), *[part for part in parts if part]])
    env.update({"HERDR_SESSION": session, "AGENT_HERDR_SESSION": session,
                "HERDR_CONFIG_PATH": str(config / "config.toml"),
                "HERDR_SOCKET_PATH": str(config / "sessions" / session / "herdr.sock")})
    return session


class PrivateHerdrTui:
    """A real TUI as a Herdr agent inside a Herdr server only this fixture owns.

    Same interface as ``PrivateTui``. The server runs with the fixture's private
    HOME/XDG, its own session name and socket, and a PATH entry holding only the
    ``herdr`` binary, so hooks and ``peer-steward`` reach this server alone.
    """

    def __init__(self, root: Path, env: dict, argv: list[str], cwd: Path, harness: str):
        self.root = root
        self.harness = harness
        self.argv = argv
        self.cwd = cwd
        self.name = "hbsteward"
        self.panes: dict[str, str] = {}
        self.server = None
        self.started = False
        self.calls: list[dict] = []
        self.session = private_herdr_env(root, env)
        config = Path(env["XDG_CONFIG_HOME"]) / "herdr"
        self.socket = config / "sessions" / self.session / "herdr.sock"
        self.env = env

    def herdr(self, *args, timeout=30):
        done = subprocess.run([str(self.root / "herdr-bin" / "herdr"), "--session", self.session,
                               *args], env=self.env, cwd=str(self.cwd), text=True,
                              capture_output=True, timeout=timeout)
        self.calls.append({"args": list(args)[:4], "rc": done.returncode,
                           "stderr": done.stderr[-300:]})
        return done

    @staticmethod
    def _json(text):
        try:
            return json.loads(text)
        except ValueError:
            return None

    def _new_pane(self, label):
        done = self.herdr("workspace", "create", "--cwd", str(self.cwd), "--label", label,
                          "--no-focus")
        panes = _ids(self._json(done.stdout), "pane_id")
        return panes[0] if done.returncode == 0 and panes else None

    def _agent_start(self, name, pane):
        native = self.argv[1:]
        self.panes[name] = pane
        if SESSION_SHELL:
            # ``agent start --kind`` runs the kind's own executable, so the session shell's
            # entry would become its argument: run the whole launch in the pane, then give
            # the agent Herdr detects there (its integration reports it) the run's name.
            launch = " ".join(shlex.quote(part) for part in self.argv)
            ran = self.herdr("pane", "run", pane, launch, timeout=30)
            found = ran.returncode == 0 and wait_for(
                lambda: self.herdr("agent", "get", pane, timeout=10).returncode == 0, 120, 1)
            named = bool(found) and self.herdr("agent", "rename", pane, name, timeout=10).returncode == 0
            return {"ok": named, "rc": ran.returncode, "pane": pane, "shell_launch": True,
                    "stderr": ran.stderr[-300:]}
        done = self.herdr("agent", "start", name, "--kind", self.harness, "--pane", pane,
                          "--timeout", "120000", *(["--", *native] if native else []), timeout=130)
        if done.returncode != 0 and "timeout" in done.stderr:
            # A first start in a fresh HOME can outlast the start wait; the agent
            # usually is up by now, which `agent get` confirms.
            if self.herdr("agent", "get", name, timeout=10).returncode == 0:
                return {"ok": True, "rc": 0, "pane": pane, "late_start": True,
                        "stderr": done.stderr[-300:]}
        return {"ok": done.returncode == 0, "rc": done.returncode, "pane": pane,
                "stderr": done.stderr[-300:]}

    def start(self):
        if not REAL_HERDR:
            raise RuntimeError("herdr binary not found")
        errors = (self.root / "herdr-server.stderr").open("wb")
        self.server = subprocess.Popen(
            [str(self.root / "herdr-bin" / "herdr"), "--session", self.session, "server"],
            cwd=str(self.cwd), env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True, close_fds=True)
        ready = wait_for(lambda: self.server.poll() is None
                         and self.herdr("pane", "list", timeout=5).returncode == 0, 30, 0.3)
        if not ready or not self.socket.is_socket():
            raise RuntimeError("private herdr server did not start")
        self.env["HERDR_SOCKET_PATH"] = str(self.socket)
        self.started = True
        pane = self._new_pane("steward")
        if not pane:
            raise RuntimeError("private herdr pane not created")
        self.start_result = self._agent_start(self.name, pane)

    def start_target(self, name):
        pane = self._new_pane("target")
        if not pane:
            return {"ok": False, "reason": "pane-not-created"}
        result = self._agent_start(name, pane)
        if result["ok"]:
            wait_for(lambda: len(self.read(name).strip()) > 40, 60)
        return result

    def _send(self, name, text):
        """Type into the agent's pane, confirm it is visible, then submit."""
        pane = self.panes.get(name, name)
        probe = text[:24]
        for _ in range(4):
            sent = self.herdr("pane", "send-text", pane, text, timeout=15)
            if sent.returncode == 0 and wait_for(lambda: probe in self.read(name), 6):
                time.sleep(0.6)
                self.herdr("pane", "send-keys", pane, "Enter", timeout=10)
                return True
            self.herdr("pane", "send-keys", pane, "Escape", timeout=10)
            time.sleep(1.5)
        return False

    def prompt_target(self, name, text):
        return {"typed": self._send(name, text)}

    def read(self, name):
        done = self.herdr("agent", "read", name, "--source", "visible", timeout=10)
        payload = self._json(done.stdout)
        if isinstance(payload, dict):
            texts = _ids(payload, "text") or _ids(payload, "content")
            return ANSI_RE.sub("", "\n".join(texts))
        return ANSI_RE.sub("", done.stdout or "")

    def screen(self) -> str:
        return self.read(self.name) if self.started else ""

    def alive(self) -> bool:
        return bool(self.started and self.server and self.server.poll() is None
                    and self.herdr("agent", "get", self.name, timeout=10).returncode == 0)

    def type_text(self, text: str, attempts: int = 2):
        return self._send(self.name, text)

    def settle(self, quiet: float = 3, timeout: float = 45):
        # The class itself: a scenario may swap the module's ``PrivateTui`` for a factory.
        return _TMUX_TUI.settle(self, quiet, timeout)

    def key(self, *keys: str):
        self.herdr("pane", "send-keys", self.panes.get(self.name, self.name), *keys, timeout=10)

    def stop(self):
        if self.started:
            with contextlib.suppress(Exception):
                self.herdr("server", "stop", timeout=15)
            self.started = False
        if self.server is not None and self.server.poll() is None:
            with contextlib.suppress(OSError):
                self.server.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.server.wait(timeout=10)


def fixture_processes(root: Path) -> list[int]:
    """PIDs whose environment or argv belongs to this fixture root only."""
    marker = f"HEARTING_FIXTURE_ROOT={root}".encode()
    prefix = str(root).encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        hit = prefix in argv
        if not hit:
            try:
                hit = marker in (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                hit = False
        if hit:
            found.append(int(entry.name))
    return found


def retire_fixture_processes(root: Path, grace: float = 5) -> list[int]:
    """End only processes this fixture started (its isolated daemons too)."""
    import signal
    pids = fixture_processes(root)
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = _now() + grace
    while _now() < deadline and any(Path(f"/proc/{pid}").exists() for pid in pids):
        time.sleep(0.2)
    killed = fixture_processes(root)
    for pid in killed:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    # The fixture tree is removed only after its processes are gone, so a late
    # write (Codex's state sqlite WAL) cannot recreate files under it.
    deadline = _now() + grace
    while _now() < deadline and fixture_processes(root):
        time.sleep(0.2)
    return pids


def declared_carrier(harness: str, scenario) -> str | None:
    """The carrier the installed tree declares for ``scenario``'s delivery, or None.

    ``gap`` means the harness declares no carrier for it; ``fail`` means a declared
    carrier (or a wired path) did not deliver.  Read from the measured ``SOURCE``.
    """
    section = getattr(scenario, "carrier", None)
    if not section:
        return None
    try:
        import harness_capabilities
        declared = harness_capabilities.capabilities(harness, SOURCE)[section]
    except Exception:  # noqa: BLE001 - an unreadable declaration declares nothing
        return None
    return declared.get("carrier") if isinstance(declared, dict) else None


def wait_for(predicate, timeout: float, step: float = 0.5):
    deadline = _now() + timeout
    while _now() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


# ``--session-shell`` launches the TUI through Hearting's session shell, the way
# an operator with ``harness session shell enable`` starts it.
SESSION_SHELL = False


def enable_session_shell(home: "InstalledHome") -> dict:
    """Turn the session shell on inside the fixture HOME and seed its assets.

    The node assets are content-keyed under ``XDG_CACHE_HOME``; an operator
    build is copied (never linked) so the fixture cannot write into it.
    """
    sys.path.insert(0, str(SOURCE / "utilities" / "session-shell"))
    config = Path(home.env["XDG_CONFIG_HOME"]) / "hearting"
    config.mkdir(parents=True, exist_ok=True)
    policy = config / "session-shell.json"
    policy.write_text('{"enabled":true}\n', encoding="utf-8")
    policy.chmod(0o600)
    node = shutil.which("node", path=home.env.get("PATH"))
    if not node:
        return {"status": "node-missing"}
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hb_shell_entry", SOURCE / "utilities" / "session-shell" / "entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = module.asset_key(node)
    target = Path(home.env["XDG_CACHE_HOME"]) / "hearting" / "session-shell" / key
    if (target / "ready.json").exists():
        return {"status": "ready", "assets": str(target)}
    operator = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") \
        / "hearting" / "session-shell" / key
    if not (operator / "ready.json").exists():
        return {"status": "assets-missing", "key": key}
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(operator, target, symlinks=True)
    return {"status": "copied", "assets": str(target)}


def harness_argv(harness: str, binary: str) -> list[str]:
    native = _native_argv(harness, binary)
    if SESSION_SHELL:
        return [sys.executable, str(SOURCE / "utilities" / "session-shell" / "entry.py"),
                harness, *native]
    return native


def _native_argv(harness: str, binary: str) -> list[str]:
    if harness == "claude":
        return [binary, "--model", FIXTURE_MODEL]
    if harness == "codex":
        return [binary, "--no-alt-screen"]
    # A private server: never reach the operator's shared background service port.
    return [binary, "--standalone"]


READY_MARKERS = {
    "claude": ("? for shortcuts", ">", "bypass permissions"),
    "codex": ("context left", "for shortcuts", "›"),
    "opencode": ("ctrl+p", "tab", "/"),
}


# --------------------------------------------------------------------------
# Scenario plumbing
# --------------------------------------------------------------------------

class Run:
    """One harness x scenario execution."""

    def __init__(self, harness: str, scenario, output: Path, keep: bool):
        self.harness = harness
        self.scenario = scenario
        self.output = output
        self.keep = keep
        self.notes: list[str] = []
        self.evidence: dict = {}

    def execute(self) -> dict:
        binary = native_binary(self.harness)
        row = {"harness": self.harness, "scenario": self.scenario.name,
               "source": str(SOURCE), "session_shell": SESSION_SHELL,
               "binary": binary, "verdict": "not-run", "notes": self.notes,
               "evidence": self.evidence}
        if not binary:
            row["verdict"] = "harness-missing"
            return row
        runtime = Path(os.environ.get("HEARTING_SCENARIO_TMP") or tempfile.gettempdir())
        runtime.mkdir(parents=True, exist_ok=True)
        # Herdr puts its sockets under XDG_CONFIG_HOME inside this root: a short
        # name keeps them within the ~107-byte UNIX socket path limit (also for a
        # scenario that starts its own private Herdr server, e.g. ``tidy-compact-herdr``).
        on_herdr = SURFACE == "herdr" or getattr(self.scenario, "surface", "") == "herdr"
        prefix = (f"hb-{self.harness[:2]}-" if on_herdr
                  else f"hb-{self.harness}-{self.scenario.name}-")
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=str(runtime)))
        row["fixture_root"] = str(root)
        fixture = ModelFixture(self.scenario.script(self), self.scenario.side_script(self))
        tui = None
        try:
            self.home = InstalledHome(root, self.harness, fixture)
            if SURFACE == "herdr":
                self.evidence["herdr_session"] = private_herdr_env(root, self.home.env)
            install = self.home.install()
            self.evidence["install"] = install
            if SURFACE == "herdr":
                self.evidence["herdr_integration"] = self.home.install_herdr_integration()
            self.home.configure_model()
            if self.harness == "codex":
                self.evidence["hook_trust"] = self.home.trust_codex_hooks(binary)
            if SESSION_SHELL:
                self.evidence["session_shell"] = enable_session_shell(self.home)
            self.binary = binary
            self.fixture = fixture
            self.scenario.prepare(self)
            row["surface"] = SURFACE
            if getattr(self.scenario, "needs_tui", True):
                argv = harness_argv(self.harness, binary) + list(
                    getattr(self.scenario, "extra_argv", lambda _harness: [])(self.harness))
                tui = self.scenario.make_tui(self, root, argv)
                self.tui = tui
                tui.start()
                ready = wait_for(lambda: tui.alive() and self._ready(tui.screen()), 90)
                self.evidence["ready"] = bool(ready)
                self._dismiss_startup(tui)
            verdict = self.scenario.drive(self)
            declared = declared_carrier(self.harness, self.scenario)
            if verdict == "gap" and declared:
                verdict = "fail"
                self.note(f"declared carrier {declared} did not deliver")
            row["verdict"] = verdict
        except Exception as exc:  # the row carries the failure; other rows still run
            row["verdict"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if tui is not None:
                with contextlib.suppress(Exception):
                    (root / "screen.txt").write_text(tui.screen(), encoding="utf-8")
                if isinstance(tui, PrivateHerdrTui):
                    self.evidence["herdr_calls"] = tui.calls[-40:]
                    self.evidence["herdr_start"] = getattr(tui, "start_result", None)
                tui.stop()
            with contextlib.suppress(Exception):
                self.scenario.cleanup(self)
            self.evidence["retired_fixture_pids"] = len(retire_fixture_processes(root))
            fixture.close()
            requests = fixture.requests
            self.evidence["model_requests"] = [
                {k: r[k] for k in ("protocol", "main", "main_index", "last_kind", "reply")}
                | {"last_text": r["last_text"][-600:]} for r in requests]
            dest = self.output / f"{self.harness}-{self.scenario.name}"
            dest.mkdir(parents=True, exist_ok=True)
            for name in ("screen.txt", "install.log"):
                src = root / name
                if src.exists():
                    shutil.copy2(src, dest / name)
            (dest / "requests.json").write_text(json.dumps(requests, indent=1, default=str),
                                                encoding="utf-8")
            if not self.keep:
                shutil.rmtree(root, ignore_errors=True)
                if root.exists():   # a write that raced the removal
                    time.sleep(1)
                    shutil.rmtree(root, ignore_errors=True)
        return row

    def _ready(self, screen: str) -> bool:
        lowered = screen.lower()
        return any(marker.lower() in lowered for marker in READY_MARKERS[self.harness]) \
            and len(screen.strip()) > 40

    def _dismiss_startup(self, tui: PrivateTui):
        tui.settle()
        screen = tui.screen().lower()
        # Claude dangerous-mode, Codex trust/update prompts: accept the default.
        if "do you trust" in screen or "yes, i accept" in screen or "update" in screen \
                and "skip" in screen:
            tui.key("Enter")
            time.sleep(2)

    # -- helpers scenarios share -------------------------------------------
    def prompt(self, text: str, expect_request: bool = True):
        before = len(self.main_requests())
        typed = self.tui.type_text(text)
        if not typed:
            self.note(f"prompt not visible in composer: {text[:40]}")
        if expect_request and not self.wait_requests(before + 1, 15):
            self.tui.key("Enter")
        return typed

    def main_requests(self):
        return self.fixture.main_requests()

    def wait_requests(self, count: int, timeout: float = 60) -> bool:
        return bool(wait_for(lambda: len(self.main_requests()) >= count, timeout))

    def wait_idle(self, quiet: float = 6, timeout: float = 90) -> int:
        """Wait until no new model request arrived for ``quiet`` seconds."""
        deadline = _now() + timeout
        last = -1
        since = _now()
        while _now() < deadline:
            count = len(self.main_requests())
            if count != last:
                last, since = count, _now()
            elif _now() - since >= quiet:
                return count
            time.sleep(0.5)
        return len(self.main_requests())

    def note(self, text: str):
        self.notes.append(text)


class Scenario:
    name = "base"
    summary = ""
    # The ``harness-capabilities.json`` section whose ``carrier`` this scenario's
    # delivery rides.  When that harness declares a carrier and nothing arrived, the
    # row is ``fail`` (a declared carrier that does not deliver), never ``gap``.
    carrier = None

    def script(self, run: Run):
        def answer(record, earlier):
            return Reply(f"OK {record['main_index']}")
        return answer

    def side_script(self, run: Run):
        """Answer for a request without tools; None keeps the fixture title reply."""
        return None

    def prepare(self, run: Run):
        return None

    # A headless scenario (``needs_tui = False``) drives the native CLI itself.
    needs_tui = True

    def make_tui(self, run: Run, root: Path, argv: list[str]):
        """The terminal the TUI runs in: the selected ``--surface`` (a private
        tmux server, or a private Herdr server for ``--surface herdr``).

        Read from the module whose ``Run`` executes (``__main__`` when run as a
        script): that is where ``--surface`` is set and where a companion module
        replaces ``PrivateTui`` for its own terminal (a host shell, Herdr)."""
        module = sys.modules.get(type(run).__module__)
        if not hasattr(module, "PrivateTui"):
            module = sys.modules[__name__]
        if module.SURFACE == "herdr":
            # A tmux-only one-shot replacement a scenario installed is not used here; the
            # module's class comes back so the next run in this process starts clean.
            if getattr(module, "_TMUX_TUI", None) is not None:
                module.PrivateTui = module._TMUX_TUI
            tui = module.PrivateHerdrTui(root, run.home.env, argv, run.home.workspace, run.harness)
            run.surface = tui
            return tui
        return module.PrivateTui(root, run.home.env, argv, run.home.workspace)

    def cleanup(self, run: Run):
        """Stop scenario-owned servers before the fixture processes are retired."""
        return None

    def drive(self, run: Run) -> str:
        raise NotImplementedError


# The local-evidence probe's own header: every harness injects it on the first
# prompt through its lifecycle hook (Claude/Codex UserPromptSubmit, OpenCode the
# plugin's system-context hook), and no static instruction file contains it.
HOOK_CONTEXT_MARKER = "# Local evidence present"


class BootScenario(Scenario):
    name = "boot"
    summary = ("Installed TUI starts on the loopback model, answers one prompt, and its "
               "first model request carries Hearting's hook-injected context.")

    def prepare(self, run):
        # One artifact makes the local-evidence probe emit its block, so the check
        # reads hook output rather than the bootstrap instructions every harness loads.
        root = Path(run.home.env.get("AGENT_ARTIFACT_ROOT")
                    or run.home.workspace / ".agent_reports")
        seeded = root / "research" / "2026-01-01_boot-hook-context" / "report.md"
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text("boot scenario hook-context probe\n", encoding="utf-8")

    def drive(self, run):
        run.prompt("SCENARIO-BOOT hello")
        if not run.wait_requests(1, 90):
            run.note("no model request after the prompt")
            return "fail"
        first = run.main_requests()[0]
        carried = HOOK_CONTEXT_MARKER in first["text"]
        run.evidence["hook_context_in_first_request"] = carried
        if not carried:
            # A runtime update that drops the lifecycle hook ends here, not silently.
            run.note("hook context missing from the first model request")
            return "fail"
        return "pass"


class ShellScenario(Scenario):
    name = "shell"
    summary = "The fixture model runs one shell tool call and sees its result."

    def script(self, run):
        def answer(record, earlier):
            if record["last_kind"] != "tool" and "SCENARIO-SHELL" in record["last_text"] \
                    and not any(r["reply"]["shell"] for r in earlier):
                return Reply(shell="echo SCENARIO-SHELL-$((40+2))")
            return Reply("shell observed")
        return answer

    def drive(self, run):
        run.prompt("SCENARIO-SHELL go")
        run.wait_idle(6, 120)
        seen = any("SCENARIO-SHELL-42" in r["text"] for r in run.main_requests()[1:])
        if not seen:
            run.note("shell output never reached the model")
            return "fail"
        return "pass"


SCENARIOS = {cls.name: cls for cls in (BootScenario, ShellScenario)}


def _load_extra():
    """Register the behaviour scenarios defined in the companion modules.

    Name a new companion module by its topic (``harness_behavior_cases_<topic>.py``),
    not the next letter: parallel branches picking the next letter collide.
    """
    import importlib
    for path in sorted(Path(__file__).resolve().parent.glob("harness_behavior_cases*.py")):
        if path.name.endswith(".test.py"):
            continue
        module = importlib.import_module(path.stem)
        for cls in getattr(module, "SCENARIOS", ()):
            SCENARIOS[cls.name] = cls


def render_markdown(rows: list[dict]) -> str:
    scenarios = []
    for row in rows:
        if row["scenario"] not in scenarios:
            scenarios.append(row["scenario"])
    lines = ["| scenario | " + " | ".join(HARNESSES) + " |",
             "|---|" + "---|" * len(HARNESSES)]
    for name in scenarios:
        cells = []
        for harness in HARNESSES:
            match = [r for r in rows if r["scenario"] == name and r["harness"] == harness]
            if not match:
                cells.append("-")
                continue
            row = match[0]
            note = ("; ".join(row["notes"]))[:160]
            cells.append(row["verdict"] + (f" — {note}" if note else ""))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    _load_extra()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--scenario", default="boot")
    parser.add_argument("--harness", default=",".join(HARNESSES))
    parser.add_argument("--output-dir")
    parser.add_argument("--source", help="Hearting checkout to install (default: this tree)")
    parser.add_argument("--session-shell", action="store_true",
                        help="launch each TUI through Hearting's session shell")
    parser.add_argument("--surface", choices=("tmux", "herdr"), default="tmux",
                        help="run each TUI in a private tmux server (default) or as an agent "
                             "of a private Herdr server")
    parser.add_argument("--keep", action="store_true",
                        help="keep each fixture HOME for inspection")
    args = parser.parse_args(argv)
    if args.list:
        for name, cls in SCENARIOS.items():
            print(f"{name}\t{cls.summary}")
        return 0
    if not args.output_dir:
        parser.error("--output-dir is required")
    global SOURCE, SESSION_SHELL, SURFACE
    if args.source:
        SOURCE = Path(args.source).resolve()
    SESSION_SHELL = bool(args.session_shell)
    SURFACE = args.surface
    names = list(SCENARIOS) if args.scenario == "all" else args.scenario.split(",")
    harnesses = [h for h in args.harness.split(",") if h]
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in names:
        for harness in harnesses:
            row = Run(harness, SCENARIOS[name](), output, args.keep).execute()
            rows.append(row)
            print(json.dumps({k: row[k] for k in ("harness", "scenario", "verdict")}
                             | {"notes": row["notes"], "error": row.get("error")},
                             ensure_ascii=False), flush=True)
            (output / "results.json").write_text(json.dumps(rows, indent=1, default=str,
                                                            ensure_ascii=False),
                                                 encoding="utf-8")
    (output / "results.md").write_text(render_markdown(rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
