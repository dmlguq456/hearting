"""Private source/notice/native-receiver fixture; no user pane or model is used."""
from __future__ import annotations

import contextlib
import importlib.util
import os
import json
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
        text, ref = self.peer.prepare_peer_message(body,
            {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}, recipient,
            defer=True, refs=[duty["id"], record["delivery_id"]], producer_binding=producer)
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
        harness, sid = signal["harness"], signal["session_id"]
        pane = "fixture:p1"
        resolved = {"pane": pane, "harness": harness, "session_id": sid,
                    "name": "fixture", "state": "idle"}
        inventory = {"result": {"agents": [{"agent": harness, "agent_session": {"value": sid},
                                              "pane_id": pane}]}}
        store = obligations.ObligationStore(self.root)
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
            if harness != "codex" and (want_harness, want_sid) == (harness, sid):
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
            native_prompt(None, row["text"])
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
        """Exercise the original harness consumer; only native API/I/O is a fixture."""
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
            return
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            if signal["harness"] == "claude":
                module.handle_prompt({"session_id": sid, "cwd": str(self.root), "prompt": text})
            else:
                module.peer_notice({"session_id": sid}, text, str(self.root))
        finally:
            sys.modules.pop(name, None)
