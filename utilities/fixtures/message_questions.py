"""Private source/notice/native-receiver fixture; no user pane or model is used."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import os
import json
import re
import shutil
import subprocess
import sys
import io
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import human_wait_state as waits
import session_tidy as tidy
import session_notice as notices
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
import peer_obligations as obligations


class MessageQuestionFixture:
    def __enter__(self):
        self.node = shutil.which("node")
        self.stack = contextlib.ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="message-question-")))
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.env = {"HOME": os.environ.get("HOME", ""), "PATH": str(self.root / "no-bin"),
                    "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_CACHE_HOME": str(self.root / "cache"),
                    "TMPDIR": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "AGENT_PEER_LEDGER_ROOT": str(self.root),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(mock.patch.object(waits, "_request_producer"))
        self.stack.enter_context(mock.patch.object(obligations, "ensure_normal_runner", return_value=False))
        self.stack.enter_context(mock.patch.object(obligations, "request_dispatch_reconciliation", return_value=False))
        spec = importlib.util.spec_from_file_location("message_question_peer", Path(tidy.__file__).with_name("peer-message.py"))
        self.peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.peer)
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def hold_projection_lock(self):
        """Keep CLI children from starting an unrelated projection producer."""
        projection = waits.state_root()
        projection.mkdir(mode=0o700, parents=True)
        lock = self.stack.enter_context((projection / ".projection.lock").open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def seat(self, harness="claude", sid="native-question-session"):
        return tidy.session_seat(tidy.Seat("pane", "question-seat-" + harness,
                                          pane="fixture:p1", harness=harness), harness, sid)

    def ask(self, harness="claude", sid="native-question-session", text="PRIVATE-QUESTION-SENTINEL", **kwargs):
        seat = self.seat(harness, sid)
        signal = waits.register_message(harness, sid, seat, text,
            sequence=tidy.read_prompt_seq(seat), cwd=str(self.root), **kwargs)
        # The shared store the running producer keeps; a question alone adds no answer record to it.
        with waits._cache_write_lock():
            if not waits._cache_path().exists():
                waits._write_cache_value(waits._projection_or_new())
        return signal, waits.read_selected_request({"id": signal["id"], "harness": harness, "session_id": sid})

    def submit(self, envelope, text="PRIVATE-ANSWER-SENTINEL", action="fixture-action"):
        return waits.submit_selected_response(envelope,
            {"answers": {"message-response": [text]}}, action_id=action)

    def record(self, signal):
        """The delivery of the question's current answer: the question's own key, or, after an
        earlier answer's delivery ended without arriving, the later answer's own (``notify``)."""
        sha = lambda text: __import__("hashlib").sha256(text.encode()).hexdigest()
        key = "notice\0question-answer:" + signal["id"]
        found = pending.read(self.root, signal["session_id"], "delivery-" + sha(key))
        entry = waits._selected_entry({"harness": signal["harness"], "session_id": signal["session_id"],
                                       "request_id": signal["id"], "question_id": "message-response",
                                       "turn_id": None}) or {}
        action = entry.get("action_id")
        if action and (found is None or found["receipt"]["answer_binding"]["action_id"] != action):
            return pending.read(self.root, signal["session_id"], "delivery-" + sha(key + "\0" + action)) or found
        return found

    display = False   # True: the receiver has shown it reads the short forms, the producer's line is sealed
    shell = True      # False: the session has no shell host, so its rows wait for its own callbacks

    def prepare(self, signal):
        """The existing courier's real claim, body rendering, sealing and pending path."""
        record = self.record(signal)
        store = obligations.ObligationStore(self.root)
        duty = next(item for item in store.list(kinds={"run-notice"})
                    if item["intent"]["delivery_id"] == record["delivery_id"])
        storage = signal["session_id"]
        claim = pending.claim(self.root, storage, record["delivery_id"],
            claim_owner="retained-rewake:" + duty["id"], lease_seconds=30,
            require_generation_proof=False, jobs=self.jobs)
        from dispatch_recipient_receipt import IDENTITY_KEY
        view = {**claim, sweep.STORAGE_KEY: storage,
                IDENTITY_KEY: pending.received_ack_identity(claim, storage, claim["delivery_id"])}
        body = sweep.delivery_context([(self.root, [view])])
        recipient = {"harness": signal["harness"], "session_id": storage}
        producer = {"schema": self.peer._SESSION_NOTICE_PRODUCER_SCHEMA,
                    "duty_id": duty["id"], "jobs": str(self.jobs), "recipient": recipient,
                    "records": [{"duty_id": duty["id"], "storage_sid": storage,
                                 "delivery_id": record["delivery_id"],
                                 "receipt_digest": record["receipt_digest"]}]}
        display_line = None
        if self.display:
            self.peer.note_display_ready(recipient)
            display_line = sweep.delivery_display_text([(self.root, [view])])
        text, ref = self.peer.prepare_peer_message(body,
            {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}, recipient,
            defer=True, refs=[duty["id"], record["delivery_id"]], producer_binding=producer,
            display_line=display_line)
        store.update(duty["id"], observation={"transfer_ref": ref, "transfer_bindings": {ref: producer}})
        return text, ref, recipient

    def courier(self, signal):
        """Run the real retained courier with only native census/RPC/I/O replaced."""
        from split_module_fixture import patch_split
        import codex_queue_delivery as queue
        import codex_queue_dispatch
        path = Path(tidy.__file__).with_name("peer-steward.py")
        spec = importlib.util.spec_from_file_location("message_question_courier", path)
        runner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = runner
        spec.loader.exec_module(runner)
        self.stack.callback(sys.modules.pop, spec.name, None)
        # The body the courier renders for each send, as the prompt command reads it.
        self.bodies = []
        real_prompt = runner.cmd_prompt
        def prompt(args, **kwargs):
            self.bodies.append(Path(args.body_file).read_text(encoding="utf-8").rstrip("\n"))
            return real_prompt(args, **kwargs)
        runner.cmd_prompt = prompt
        harness, sid = signal["harness"], signal["session_id"]
        pane = "fixture:p1"
        resolved = {"pane": pane, "harness": harness, "session_id": sid,
                    "name": "fixture", "state": "idle"}
        inventory = {"result": {"agents": [{"agent": harness, "agent_session": {"value": sid},
                                              "pane_id": pane}]}}
        store = obligations.ObligationStore(self.root)
        if self.display:
            self.peer.note_display_ready({"harness": harness, "session_id": sid})
        calls = []
        def native_prompt(target, text, **kwargs):
            calls.append(text)
            return 0, {}
        def rpc(endpoint, method, params, **kwargs):
            if method == "thread/queue/list":
                return {"data": []}
            if method == "thread/turns/list":
                return {"data": [{"id": "turn-fixture", "status": "completed"}]}
            if method == "thread/queue/add":
                calls.extend(item["text"] for item in params["input"] if item.get("type") == "text")
                return {"queuedSubmission": {"id": "queued-fixture"}}
            raise AssertionError("unexpected native RPC " + method)
        import peer_address
        import session_shell
        # Claude and OpenCode take the answer through their session-shell host: it
        # reports an idle, empty input, then begins the exact pending write it injects.
        binding = {"harness": harness, "sid": sid, "native_pid": os.getpid(),
                   "host_pid": os.getpid(), "incarnation": "fixture-" + sid}

        def exact_binding(want_harness, want_sid):
            if harness != "codex" and self.shell and (want_harness, want_sid) == (harness, sid):
                return binding, ""
            return None, "attachment-unavailable"

        def control(current, operation, **fields):
            if operation == "status":
                return {"ok": True, "state": "idle", "empty": True, "focused": False, "epoch": 1}
            if operation != "inject":
                return {"ok": False, "reason": "unsupported-operation", "not_started": True}
            row, claim = self.peer._read_pending(fields.get("ref")), fields.get("claim") or {}
            begun = row and self.peer.begin_pending_write(
                row, recipient={"harness": harness, "session_id": sid},
                receipt="session-shell-inflight",
                claimant={"pid": claim.get("pid"), "start": claim.get("start")})
            if not begun:
                return {"ok": False, "reason": "peer-claim-changed", "not_started": True}
            native_prompt(None, self.peer.pasted_text(row))   # what the host's payload carries
            return {"ok": True}

        with contextlib.ExitStack() as patches:
            for patch in (
                mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(inventory))),
                mock.patch.object(session_shell, "exact_binding", side_effect=exact_binding),
                mock.patch.object(session_shell, "control", side_effect=control),
                mock.patch.object(peer_address, "presence", return_value="present"),
                mock.patch.object(queue, "_rpc", side_effect=rpc),
                mock.patch.object(queue, "_known_consumed", return_value=None),
                mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value=self.root / "fixture.sock"),
            ):
                patches.enter_context(patch)
            with contextlib.redirect_stdout(io.StringIO()):
                runner._resume_session_notice_obligations(store.list(kinds={"run-notice"}), store)
        duty = next(item for item in store.list(kinds={"run-notice"})
                    if item["intent"]["delivery_id"] == self.record(signal)["delivery_id"])
        ref = (duty.get("observation") or {}).get("transfer_ref")
        return calls, ref, self.peer._read_pending(ref) if ref else None

    def direct(self, signal, text):
        seat = self.seat(signal["harness"], signal["session_id"])
        # The real prompt hook proves this seat and consumes the nonempty prompt.
        with mock.patch.object(tidy, "resolve_seat", return_value=seat):
            return tidy.run_hook(signal["harness"], "prompt", signal["session_id"],
                                 cwd=str(self.root), prompt_text=text, env=os.environ,
                                 render_context=False)

    def consume(self, signal, text, *, sid=None):
        """Exercise the original harness consumer; only native API/I/O is a fixture.

        Returns what that consumer gives the agent beside the prompt: Claude's UserPromptSubmit
        ``additionalContext``, Codex's guidance part of its own (its hook's peer check, guidance
        call and context emitter), OpenCode's plugin guidance (the plugin's own Python call)."""
        sid = sid or signal["session_id"]
        root = Path(tidy.__file__).resolve().parents[1]
        if signal["harness"] == "claude":
            path = root / "hooks/peer-message-record.py"
            name = "message_question_claude_hook"
        elif signal["harness"] == "codex":
            path = root / "adapters/codex/hooks/userprompt-lifecycle.py"
            name = "message_question_codex_hook"
        else:
            if not self.node:
                # A host without node cannot run the plugin's own call: the OpenCode case is skipped, not failed.
                raise unittest.SkipTest("Node is unavailable for the OpenCode callback fixture")
            # `chat.message` first asks the guidance its system context carries, with the very
            # Python call written in the plugin, then receives the text.
            plugin = (root / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
            call = json.loads(re.search(r'const guidance = spawnSync\(\s*"python3",\s*\["-c", ("(?:[^"\\]|\\.)*")\]',
                                        plugin).group(1))
            guidance = subprocess.run([sys.executable, "-B", "-c", call], cwd=root, text=True,
                env=dict(os.environ, AGENT_HOME=str(root)), capture_output=True, timeout=15,
                input=json.dumps({"prompt": text, "recipient": {"harness": "opencode", "session_id": sid},
                                  "cwd": str(self.root)}))
            if guidance.returncode:
                raise AssertionError(guidance.stderr)
            # Run the unchanged plugin callback, substituting only its detached
            # process launch with a synchronous child bound to this fixture.
            script = r'''
const fs = require("fs"), vm = require("vm"), path = require("path");
const data = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(path.join(data.root, "adapters/opencode/plugins/hearting-guards.js"), "utf8");
const callback = source.slice(source.indexOf("function spawnPeerNotice("), source.indexOf("// Pending transport uses", source.indexOf("function spawnPeerNotice(")));
let calls = 0;
const spawn = (bin, args, options) => ({on() {}, unref() {}, stdin: {end(input) {
  if (bin !== "python3") throw Error("unexpected native helper");
  const child = require("child_process").spawnSync(data.python, ["-B", ...args], {...options, detached: false, input, encoding: "utf8"});
  if (child.status !== 0) throw Error(child.stderr || "consumer failed");
  calls++;
}}});
vm.runInNewContext(callback + "\nspawnPeerNotice(sid, prompt, cwd)", {path, root: data.root, process, spawn, sid: data.sid, prompt: data.prompt, cwd: data.root});
if (calls !== 1) throw Error("native callback did not run exactly one helper");
'''
            child = subprocess.run([self.node, "-e", script], input=json.dumps({
                "root": str(root), "sid": sid, "prompt": text, "python": sys.executable}),
                text=True, capture_output=True, timeout=15)
            if child.returncode:
                raise AssertionError(child.stderr)
            # The plugin keeps the ordinary guidance and a secret answer's text apart; its
            # system context carries both. (An older plugin, measured against, prints one text.)
            try:
                parts = json.loads(guidance.stdout)
            except ValueError:
                return guidance.stdout.strip()
            return "\n\n".join(part.strip() for part in (parts["guidance"], parts["secret"]) if part.strip())
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        out = io.StringIO()
        try:
            spec.loader.exec_module(module)
            with contextlib.redirect_stdout(out):
                if signal["harness"] == "claude":
                    # The hook as Claude runs it: its `prompt` mode reads the payload from stdin.
                    hook_payload = {"session_id": sid, "cwd": str(self.root), "prompt": text,
                                    "hook_event_name": "UserPromptSubmit"}
                    with mock.patch.object(sys, "argv", [str(path), "prompt"]), \
                            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook_payload))):
                        module.main()
                else:
                    module.peer_notice({"session_id": sid}, text, str(self.root))
                    if module.looks_like_peer_prompt(text):
                        peer_spec = importlib.util.spec_from_file_location(
                            "peer_receiver_guidance", root / "utilities/peer-message.py")
                        peer = importlib.util.module_from_spec(peer_spec)
                        peer_spec.loader.exec_module(peer)
                        module.emit_context("UserPromptSubmit", [peer.receiver_guidance_once(
                            text, {"harness": "codex", "session_id": sid}, cwd=str(self.root))])
        finally:
            sys.modules.pop(name, None)
        contexts = []
        for line in out.getvalue().splitlines():
            with contextlib.suppress(ValueError, AttributeError):
                contexts.append(json.loads(line)["hookSpecificOutput"]["additionalContext"])
        return "\n\n".join(contexts)

    def claude_boundary(self, sid, *, mode="post-tool", prompt="an unrelated prompt of the person"):
        """Run Claude's own peer hook at a tool boundary (``post-tool``) or for a prompt that is not
        the delivery (``prompt``); returns the ``additionalContext`` it flushed."""
        root = Path(tidy.__file__).resolve().parents[1]
        path = root / "hooks/peer-message-record.py"
        spec = importlib.util.spec_from_file_location("message_question_claude_boundary", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        payload = ({"session_id": sid, "cwd": str(self.root), "hook_event_name": "PostToolUse",
                    "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_response": {}}
                   if mode == "post-tool" else
                   {"session_id": sid, "cwd": str(self.root), "prompt": prompt,
                    "hook_event_name": "UserPromptSubmit"})
        out = io.StringIO()
        try:
            spec.loader.exec_module(module)
            with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", [str(path), mode]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                module.main()
        finally:
            sys.modules.pop(spec.name, None)
        contexts = []
        for line in out.getvalue().splitlines():
            with contextlib.suppress(ValueError, AttributeError):
                contexts.append(json.loads(line)["hookSpecificOutput"]["additionalContext"])
        return "\n\n".join(contexts)

    # Keep the real context subcommand and its flush/receipt path. Its own
    # spawn-to-flush deadline needs headroom in both fixture entry points.
    _CONTEXT_DRIVER = """import importlib.util, sys
sys.argv.pop(0)
spec = importlib.util.spec_from_file_location("fixture_peer", sys.argv[0])
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)
peer._CONTEXT_CLI_BUDGET_S = 15
raise SystemExit(peer.main())
"""

    _IDLE_DRIVER = r"""
import { pathToFileURL } from "node:url";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { spawnSync as realSpawnSync, spawn as realSpawn } from "node:child_process";
const realSetTimeout = globalThis.setTimeout.bind(globalThis);
// Exercise delivery and ordering here. Keep a fixture watchdog without racing
// production latency budgets against a busy test runner.
const watchdogMs = 20_000;
globalThis.setTimeout = (cb, ms, ...a) => ms === 0 ? {cancelled: true} : realSetTimeout(cb, Math.max(ms, watchdogMs), ...a);
const intervals = [];   // the plugin's own timers (the carrier's 5 s pass) run only on a "tick" step
globalThis.setInterval = (cb) => { intervals.push(cb); return {unref() {}} };
const cfg = JSON.parse(process.env.FIX_CFG);
let guidanceReads = 0;   // every python CLI call and the guidance call are real; `failGuidance` fails listed reads
globalThis.__fixtureSpawnSync = (exe, args, options) => {
  if (args?.[0] === "-c" && String(args[1]).includes("receiver_guidance")
      && (cfg.failGuidance || []).includes(++guidanceReads)) return {status: 1, stdout: "", stderr: "fixture"};
  const boundedCLI = args[0]?.endsWith("peer-message.py") || String(args[1]).includes("receiver_guidance");
  return realSpawnSync(exe, args, boundedCLI ? {...options, timeout: watchdogMs} : options);
};
const children = new Set();
globalThis.__fixtureSpawn = (exe, args, options) => {
  if (args[0]?.endsWith("peer-message.py")) {
    const command = args[1] === "context" ? ["-c", cfg.contextDriver, ...args] : args;
    const child = realSpawn(exe, command, {...options, detached: false});
    child.unref = () => child;
    const closed = new Promise(resolve => child.once("close", resolve));
    children.add(closed);
    closed.then(() => children.delete(closed));
    return child;
  }
  const child = new EventEmitter(); child.pid = 4242; child.stdout = new PassThrough();
  child.stdin = new PassThrough(); child.unref = () => {}; child.kill = () => {};
  queueMicrotask(() => { child.stdout.end("null"); child.emit("close", 0); child.emit("exit", 0) });
  return child;
};
const {AgentHarnessGuards} = await import(pathToFileURL(process.env.FIX_GUARDS).href);
const sid = cfg.sid, dir = process.cwd(), messages = cfg.history || [], systems = [], tools = [], log = [];
const calls = {prompt: 0, promptAsync: 0};
let hooks, n = 0, turns = 0, k = 0;
const deferredTurns = [];
const sdk = (data, status = 200) => ({data, error: undefined, response: {ok: true, status}});
const root = {id: sid, projectID: "project_fixture", directory: dir, slug: "fixture-root", title: "fixture",
  version: "1.19.0", time: {created: 1, updated: 1}};
if (cfg.v2) { delete root.directory; delete root.slug; delete root.version; root.location = {directory: dir} }
// A model call reads the system context of that moment; one answering a user message completes it.
const modelCall = async (mid, answering = true) => {
  const o = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID: sid, model: {}}, o);
  systems.push({for: mid, system: o.system.join("\n\n")});
  if (answering) messages.push({info: {id: "assistant-" + mid, sessionID: sid, role: "assistant", parentID: mid,
    time: {completed: Date.now()}}, parts: []});
};
const person = async (id, parts, call = true) => {
  await hooks["chat.message"]({sessionID: sid, messageID: id}, {parts});
  messages.push({info: {id, role: "user", sessionID: sid, time: {created: Date.now()}}, parts});
  if (call) await modelCall(id);
};
// One started turn: the person's message (`personBetween`) may land before its model call.
const turn = async (mid, parts) => {
  turns++;
  try {
    await hooks["chat.message"]({sessionID: sid, messageID: mid}, {message: {id: mid, role: "user", sessionID: sid}, parts});
    if (cfg.personBetween) await person("person-between-" + mid, [{type: "text", text: cfg.personBetween}], false);
    await modelCall(mid);
  } finally { turns--; }
};
const session = {
  status: async () => sdk(turns > 0 ? {[sid]: {type: "busy"}} : {}),   // busy while a started turn runs
  wait: async () => sdk({}),
  get: async () => sdk(root),
  messages: async () => sdk(messages),
  prompt: async (args) => {   // the noReply save: OpenCode runs the plugin's chat.message for it
    calls.prompt++;
    const id = "native-msg-" + (++n);
    const row = {info: {id, role: "user", sessionID: sid, time: {created: Date.now()}}, parts: args.body.parts};
    await hooks["chat.message"]({sessionID: sid, messageID: id}, {message: row.info, parts: args.body.parts});
    messages.push(row);
    return sdk(row);
  },
  promptAsync: async (args) => {
    calls.promptAsync++;
    let mid = args.body.messageID;
    if (!mid) {   // OpenCode 2: the text prompt itself is saved and starts the turn
      mid = "native-msg-" + (++n);
      messages.push({info: {id: mid, role: "user", sessionID: sid, time: {created: Date.now()}}, parts: args.body.parts});
    }
    if (cfg.deadResume) return sdk(undefined, 204);   // accepted, and no turn ever starts
    if (cfg.deferTurn) deferredTurns.push(() => turn(mid, args.body.parts));
    else await turn(mid, args.body.parts);
    return args.body.messageID ? sdk(undefined, 204) : sdk({});
  },
};
hooks = await AgentHarnessGuards({directory: dir, worktree: dir, client: {session, app: {log: async () => {}}},
  ...(cfg.v2 ? {nativeHost: "v2"} : {})});
// Carrier ticks start work without returning its promise. Let their callbacks
// run and join the real CLI children before observing the next event.
const settle = async () => {
  do {
    await Promise.all([...children]);
    await new Promise(setImmediate);
  } while (children.size);
};
const runTurns = async () => {
  for (const resume of deferredTurns.splice(0)) await resume();
  await settle();
};
const idle = async (holdTurn = false) => {
  await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
  if (holdTurn) await settle();
  else await runTurns();
};
const tool = async () => {   // one finished tool of a working turn
  const o = {output: "tool-ok", title: "", metadata: {}};
  await hooks["tool.execute.after"]({sessionID: sid, tool: "bash", callID: "c" + tools.length}, o);
  tools.push(o.output);
};
if (cfg.personFirst) await person("person-1", [{type: "text", text: cfg.personFirst}]);
for (let i = 0; i < (cfg.idles || 0); i++) await idle();
for (let i = 0; i < (cfg.tools || 0); i++) {   // a working turn: each finished tool, then the loop's next call
  await tool();
  await modelCall("tool-" + i, false);
}
if (cfg.imageOnly) await person("person-image", [{type: "file", mime: "image/png", url: "data:,"}]);
// `steps`: an explicit order of what reaches the plugin (each a string, `kind:name`).
for (const step of cfg.steps || []) {
  const [kind, name] = step.split(":");
  if (kind === "arm") await hooks["shell.env"]({sessionID: sid}, {env: {}});
  else if (kind === "idle") await idle(name === "hold");
  else if (kind === "turn") await runTurns();
  else if (kind === "status") { await hooks.event({event: {type: "session.status", properties: {sessionID: sid, status: {type: name}}}}); await settle() }
  else if (kind === "tick") { for (const cb of intervals) cb(); await settle() }
  else if (kind === "tool") await tool();
  else if (kind === "call") await modelCall(name || "call-" + (++k), false);   // a tool loop's next call
  else if (kind === "text") await person(name, [{type: "text", text: "the person's message " + name}], false);
  else if (kind === "image") await person(name, [{type: "file", mime: "image/png", url: "data:,"}], false);
  else if (kind === "answer") await modelCall(name);   // the call answering message `name`
  else throw new Error("unknown step " + step);
  log.push({step, prompt: calls.prompt, promptAsync: calls.promptAsync});
}
if (deferredTurns.length) throw new Error("fixture still has deferred turns");
hooks.dispose?.({silent: true});
await settle();
process.stdout.write(JSON.stringify({systems, tools, calls, log, history: messages.map(m => ({id: m.info.id, role: m.info.role,
  text: (m.parts || []).map(p => p.text || "").join("")}))}), () => process.exit(0));
"""

    def opencode_plugin_session(self, sid, cfg, *, origin=True):
        """The OpenCode plugin itself under node, with a stand-in OpenCode server client: its own
        `session.idle` delivery (`pendingPeerDelivery`), tool boundaries (`tool.execute.after`) and the
        model calls after them, a person's prompts. The fixture supplies window-origin proof
        (``origin``: this session's own, or none as for ``opencode -c``) and owns the child lifetime.
        Delivery CLI and guidance calls are real, with a fixture time budget. ``cfg`` may give ``steps``, an explicit order
        (``idle``, ``idle:hold``, ``turn``, ``status:<type>``, ``tick`` for the plugin's own carrier timer,
        ``tool``, ``call:<name>``, ``text:<id>``, ``image:<id>``, ``answer:<id>``,
        ``arm``), ``v2`` (the OpenCode 2 host), ``deferTurn`` (accept the resume before starting
        the turn), ``personBetween``, ``deadResume`` and ``failGuidance``. A deferred turn starts after
        idle processing, or at the ``turn`` step after ``idle:hold``. Returns the system
        context of every model call, the tool results, the session history and the count after
        each step."""
        if not self.node:
            raise unittest.SkipTest("Node is unavailable for the OpenCode plugin fixture")
        root = Path(tidy.__file__).resolve().parents[1]
        source = (root / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        start = source.index("function ownNativePaneOrigin() {")
        end = source.index("\n}\n\nfunction sameNativePaneContext", start) + 2
        proof = ('function ownNativePaneOrigin() {\n  const stat = readFileSync("/proc/self/stat", "utf8")\n'
                 '  const start = stat.slice(stat.lastIndexOf(") ") + 2).trim().split(/\\s+/)[19]\n'
                 '  return {pid: process.pid, start, sid: %s, bare: false, directory: realpathSync(process.cwd()), '
                 'state: "pending"}\n}' % json.dumps(sid)) if origin else "function ownNativePaneOrigin() {\n  return null\n}"
        source = source[:start] + proof + source[end:]
        for old, new in (('import { spawnSync, spawn } from "node:child_process"',
                          "const spawnSync = globalThis.__fixtureSpawnSync; const spawn = globalThis.__fixtureSpawn"),
                         ("markPluginLoaded(dispatchSlug())", "// fixture: marker off")):
            source = source.replace(old, new)
        source = source.replace("reconnectPeerObligations()\n", "void 0\n")
        with tempfile.TemporaryDirectory(dir=str(self.root)) as tmp:
            guards = Path(tmp) / "hearting-guards-fixture.mjs"
            guards.write_text(source, encoding="utf-8")
            env = dict(os.environ, PATH=os.pathsep.join([os.path.dirname(self.node), os.path.dirname(sys.executable),
                                                         "/usr/bin", "/bin"]),
                       AGENT_HOME=str(root), FIX_GUARDS=str(guards),
                       FIX_CFG=json.dumps(dict(cfg, sid=sid, contextDriver=self._CONTEXT_DRIVER)),
                       PYTHONDONTWRITEBYTECODE="1")
            for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                        "OPENCODE_DISPATCH_SLUG", "HERDR_PANE_ID", "FLEET_TITLE_REFRESH"):
                env.pop(key, None)
            child = subprocess.run([self.node, "--input-type=module", "-e", self._IDLE_DRIVER], cwd=str(self.root),
                                   env=env, text=True, capture_output=True, timeout=180, start_new_session=True)
        if child.returncode:
            raise AssertionError(child.stderr[-1500:])
        return json.loads(child.stdout)

    def opencode_tool_boundary(self, sid):
        """The OpenCode plugin's post-tool call (`tool.execute.after`), as its source spells it;
        returns the context it appends to the finished tool result and the part it puts in the
        plugin's system context."""
        root = Path(tidy.__file__).resolve().parents[1]
        child = subprocess.run([sys.executable, "-B", "-c", self._CONTEXT_DRIVER,
                                str(root / "utilities/peer-message.py"), "context",
                                "--to-harness", "opencode", "--to-session-id", sid,
                                "--event", "PostToolUse", "--native-delivery"],
                               cwd=root, env=dict(os.environ, AGENT_HOME=str(root), HEARTING_PEER_DISPLAY_GATE="2"),
                               capture_output=True, text=True, timeout=30)
        if child.returncode:
            raise AssertionError(child.stderr)
        contexts, system = [], []
        for line in child.stdout.splitlines():
            with contextlib.suppress(ValueError, AttributeError):
                printed = json.loads(line)
                contexts.append((printed.get("hookSpecificOutput") or {}).get("additionalContext")
                                or printed.get("context") or "")
                system.append(printed.get("system") or "")
        return "\n\n".join(contexts), "\n\n".join(part for part in system if part)
