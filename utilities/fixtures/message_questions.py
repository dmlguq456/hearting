"""Private source/notice/native-receiver fixture; no user pane or model is used."""
from __future__ import annotations

import contextlib
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

    def seat(self, harness="claude", sid="native-question-session"):
        return tidy.session_seat(tidy.Seat("pane", "question-seat-" + harness,
                                          pane="fixture:p1", harness=harness), harness, sid)

    def ask(self, harness="claude", sid="native-question-session", text="PRIVATE-QUESTION-SENTINEL", **kwargs):
        seat = self.seat(harness, sid)
        signal = waits.register_message(harness, sid, seat, text,
            sequence=tidy.read_prompt_seq(seat), cwd=str(self.root), **kwargs)
        return signal, waits.read_selected_request({"id": signal["id"], "harness": harness, "session_id": sid})

    def submit(self, envelope, text="PRIVATE-ANSWER-SENTINEL", action="fixture-action"):
        return waits.submit_selected_response(envelope,
            {"answers": {"message-response": [text]}}, action_id=action)

    def record(self, signal):
        delivery = "delivery-" + __import__("hashlib").sha256(
            ("notice\0question-answer:" + signal["id"]).encode()).hexdigest()
        return pending.read(self.root, signal["session_id"], delivery)

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
                raise RuntimeError("Node is unavailable for the OpenCode callback fixture")
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
            return guidance.stdout.strip()
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

    _PLUGIN_TURN = r"""
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH));
const hooks = await AgentHarnessGuards({directory: process.env.FIX_ROOT, worktree: process.env.FIX_ROOT,
  client: {}}, {initialize: false});
const sid = process.env.FIX_SID, systems = [];
for (const text of JSON.parse(process.env.FIX_PROMPTS)) {
  await hooks["chat.message"]({sessionID: sid, messageID: "m-1"}, {parts: text === null ? [] : [{type: "text", text}]});
  const out = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID: sid, model: {}}, out);
  systems.push(out.system.join("\\n\\n"));
}
process.stdout.write(JSON.stringify(systems));
hooks.dispose?.({silent: true});
setTimeout(() => process.exit(0), 50);
"""

    def opencode_plugin_turns(self, sid, prompts):
        """The OpenCode plugin itself under node: each prompt (``None``: a message with no text, as
        the idle delivery's resume sends) through `chat.message`, then the system transform of the
        model call after it. Returns those system texts."""
        if not self.node:
            raise RuntimeError("Node is unavailable for the OpenCode plugin fixture")
        root = Path(tidy.__file__).resolve().parents[1]
        env = dict(os.environ, PATH=os.path.dirname(self.node) + os.pathsep + os.path.dirname(sys.executable),
                   AGENT_HOME=str(root), PLUGIN_PATH=str(root / "adapters/opencode/plugins/hearting-guards.js"),
                   FIX_ROOT=str(self.root), FIX_SID=sid, FIX_PROMPTS=json.dumps(prompts))
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "HERDR_PANE_ID"):
            env.pop(key, None)
        child = subprocess.run([self.node, "--input-type=module", "-e", self._PLUGIN_TURN], cwd=str(self.root),
                               env=env, text=True, capture_output=True, timeout=120, start_new_session=True)
        if child.returncode:
            raise AssertionError(child.stderr[-800:])
        return json.loads(child.stdout)

    def opencode_tool_boundary(self, sid):
        """The OpenCode plugin's post-tool call (`tool.execute.after`), as its source spells it;
        returns the context it would append to the finished tool result."""
        root = Path(tidy.__file__).resolve().parents[1]
        child = subprocess.run([sys.executable, "-B", str(root / "utilities/peer-message.py"), "context",
                                "--to-harness", "opencode", "--to-session-id", sid,
                                "--event", "PostToolUse", "--native-delivery"],
                               cwd=root, env=dict(os.environ, AGENT_HOME=str(root), HEARTING_PEER_DISPLAY_GATE="2"),
                               capture_output=True, text=True, timeout=30)
        if child.returncode:
            raise AssertionError(child.stderr)
        contexts = []
        for line in child.stdout.splitlines():
            with contextlib.suppress(ValueError, AttributeError):
                printed = json.loads(line)
                contexts.append((printed.get("hookSpecificOutput") or {}).get("additionalContext")
                                or printed.get("context") or "")
        return "\n\n".join(contexts)
