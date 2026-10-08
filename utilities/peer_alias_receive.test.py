#!/usr/bin/env python3
"""별칭은 표시 전용: 전송 본문·실제 수신자·로컬 원장 결속 회귀. 운영 전송 없음."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pm = module("alias_peer_message", "utilities/peer-message.py")


def pinned_source(path):
    # HOME is isolated, so use command-local trust for this exact test checkout.
    return subprocess.check_output(["git", "-c", f"safe.directory={ROOT}", "show", f"1c201125:{path}"],
                                   cwd=ROOT, text=True, timeout=5)


class AliasReceive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_DISPATCH_JOBS": str(self.state / "jobs.log"),
            "AGENT_PEER_LEDGER_ROOT": str(self.state),
            "HOME": str(self.state / "home"), "CODEX_HOME": str(self.state / "home/.codex"),
            "AGENT_HOME": str(ROOT), "CLAUDE_CONFIG_DIR": str(self.state / "home/.claude"),
            "XDG_STATE_HOME": str(self.state / "xdg"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sender = {"harness": "codex", "session_id": "01a07b2b-9108-7110-bd41-93b7ade4d9c4", "name": "동명"}
        self.recipient = {"harness": "codex", "session_id": "recipient-a"}

    def prepare(self, body="검증할 실제 본문", sender=None, recipient=None):
        return pm.prepare_peer_message(body, sender or self.sender, recipient or self.recipient)

    def parse(self, text, recipient=None):
        return pm.parse_peer_trailer(text, recipient or self.recipient)["session_id"]

    def rows(self):
        return [json.loads(line) for p in (self.state / "peer-messages").glob("*/*.jsonl")
                for line in p.read_text().splitlines()]

    def test_exact_body_recipient_and_transfer_record(self):
        text, ref = self.prepare()
        self.assertNotIn(self.sender["session_id"], text)
        self.assertEqual(self.parse(text), self.sender["session_id"])
        self.assertIsNone(self.parse(text + "변조"))
        self.assertIsNone(self.parse(text.replace("실제", "다른")))
        for target in ({"harness": "claude", "session_id": "recipient-a"},
                       {"harness": "codex", "session_id": "recipient-b"},
                       {"harness": "codex", "session_id": ""}):
            self.assertIsNone(self.parse(text, target))
        record = json.loads(pm._transfer_path(ref).read_text())
        self.assertEqual(record["message_id"], ref)
        self.assertEqual(record["from"], self.sender)
        self.assertEqual(record["to"], self.recipient)
        self.assertNotIn("검증할", json.dumps(record, ensure_ascii=False))

    def test_whitespace_a_runtime_adds_around_the_prompt_keeps_the_attribution(self):
        # OpenCode delivers the typed prompt with a trailing space; every OpenCode receipt was
        # unattributed (2026-10-06: 0 of 299) until the arrival was compared without it.
        text, ref = self.prepare()
        for arrived in (text + " ", text + "\n", "\n" + text + "  \n"):
            with self.subTest(arrived=arrived[-3:]):
                self.assertEqual(self.parse(arrived), self.sender["session_id"])
                trailer = pm.parse_peer_trailer(arrived, self.recipient, include_ref=True)
                self.assertEqual(trailer["transfer_ref"], ref)
        self.assertIsNone(self.parse(text + " 변조"))
        self.assertIsNone(self.parse(text.replace("실제 본문", "실제  본문")))

    def test_collision_same_name_and_swapped_refs(self):
        # A real 8-bit collision, not a mock alias resolver.
        tag = pm.peer_alias("codex", self.sender["session_id"])
        other = next(f"other-{n}" for n in range(10000) if pm.peer_alias("codex", f"other-{n}") == tag)
        sender2 = dict(self.sender, session_id=other)
        one, r1 = self.prepare("첫 본문")
        two, r2 = self.prepare("둘째 본문", sender=sender2)
        self.assertEqual(self.parse(one), self.sender["session_id"])
        self.assertEqual(self.parse(two), other)
        self.assertNotEqual(r1, r2)
        self.assertIsNone(self.parse(one.replace(r1, r2)))
        self.assertIsNone(self.parse(two.replace(r2, r1)))
        self.assertIsNone(self.parse("복사한 본문\n" + one.splitlines()[-1]))

    def test_missing_corrupt_unknown_and_path_reference(self):
        for damage in ("missing", "json", "shape", "id", "digest"):
            text, ref = self.prepare()
            path = pm._transfer_path(ref)
            if damage == "missing":
                path.rename(path.with_suffix(".missing"))
            elif damage == "json":
                path.write_text("broken")
            elif damage == "shape":
                path.write_text("[]")
            else:
                rec = json.loads(path.read_text())
                rec["message_id" if damage == "id" else "body_sha256"] = "wrong"
                path.write_text(json.dumps(rec))
            self.assertIsNone(self.parse(text), damage)
        text, ref = self.prepare()
        with mock.patch.object(pm, "peer_state_root", side_effect=AssertionError("no path lookup")):
            self.assertIsNone(self.parse(text.replace(ref, "../../secret")))
        unknown, _ = self.prepare(sender={"harness": "unknown", "session_id": "known-id"})
        self.assertIn("[?]", unknown)
        self.assertIsNone(self.parse(unknown))

    def test_delay_prefix_preserves_exact_body_recipient_and_receive_dedup(self):
        for harness in ("claude", "codex", "opencode"):
            recipient = dict(self.recipient, harness=harness)
            text, ref = self.prepare(recipient=recipient)
            prefix = f"[지연 전달 — 원래 보낸 시각 2026-10-07 18:47, 약 2시간 지연] (ref {ref[:8]})\n"
            delayed = prefix + text
            wrapped = '<pasted_content id="late">\n' + delayed + '\n</pasted_content id="late">'
            for value in (delayed, wrapped):
                self.assertEqual(self.parse(value, recipient), self.sender["session_id"])
                self.assertIsNone(self.parse(value.replace("검증할", "변조할"), recipient))
                self.assertIsNone(self.parse(value, dict(recipient, session_id="foreign")))
            self.assertIsNone(self.parse(delayed.replace(ref[:8] + ")", "00000000)"), recipient))
            self.assertEqual(pm.receive_peer_message(delayed, recipient), 0)
            self.assertEqual(pm.receive_peer_message(wrapped, recipient), 0)
            notices = [r for r in pm._iter_records() if r.get("transfer_ref") == ref]
            self.assertEqual(len(notices), 1)

    def test_legacy_last_trailer_and_pinned_old_receiver(self):
        legacy = "기존 본문\n(peer-from: claude full-legacy-id old-name)"
        self.assertEqual(self.parse(legacy), "full-legacy-id")
        text, _ = self.prepare(legacy)
        self.assertEqual(self.parse(text), self.sender["session_id"])
        self.assertIsNone(self.parse("앞에 추가\n" + text))
        self.assertEqual(self.parse(text + "\n(peer-from: claude last-id last-name)"), "last-id")
        # Execute the actual pinned pre-change parser; no mock reinterpretation.
        source = pinned_source("utilities/peer-message.py")
        scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_peer"}
        exec(compile(source, "pinned-peer-message", "exec"), scope)
        old = scope["parse_peer_trailer"](text)
        self.assertIsNone(old["session_id"])
        self.assertIsNone(pm.parse_peer_trailer(text)["session_id"])

    def test_claude_derived_then_rename_remembered_tag(self):
        from fleet import titles
        sessions = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
        sessions.mkdir(parents=True)
        path = sessions / "123.json"
        path.write_text(json.dumps({"sessionId": "claude-id", "name": "project-ab", "nameSource": "derived"}))
        with mock.patch.object(titles, "read_tag", return_value="cd"):
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[ab]")
            path.write_text(json.dumps({"sessionId": "claude-id", "name": "renamed-ef", "nameSource": "user"}))
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[cd]")
        with mock.patch.object(titles, "read_tag", return_value=None):
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[?]")

    def test_pane_move_and_reuse_do_not_supply_identity(self):
        text, _ = self.prepare(recipient=dict(self.recipient, pane="w1:p1", name="동명"))
        self.assertEqual(self.parse(text, dict(self.recipient, pane="w2:p8")), self.sender["session_id"])
        self.assertIsNone(self.parse(text, dict(self.recipient, pane="w1:p1", session_id="new-occupant")))

    def test_transfer_ref_cannot_be_rebound_or_read_through_symlink(self):
        text, ref = self.prepare()
        path = pm._transfer_path(ref)
        original = path.read_bytes()
        with mock.patch.object(pm.secrets, "token_hex", return_value=ref):
            with self.assertRaises(FileExistsError):
                self.prepare("다른 전송")
        self.assertEqual(path.read_bytes(), original)
        renamed = path.with_suffix(".saved")
        path.rename(renamed)
        path.symlink_to(renamed)
        self.assertIsNone(self.parse(text))

    def _receive_actual(self, harness, text, sid="recipient-a"):
        if harness == "claude":
            hook = module("alias_claude_hook", "hooks/peer-message-record.py")
            hook.handle_prompt({"session_id": sid, "cwd": str(ROOT), "prompt": text})
        elif harness == "codex":
            hook = module("alias_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
            hook.peer_notice({"session_id": sid}, text, str(ROOT))
        else:
            proc = subprocess.run([sys.executable, str(ROOT / "utilities/peer-message.py"), "receive",
                                   "--to-harness", harness, "--to-session-id", sid],
                                  input=text, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_distinct_installed_runtime_roots_all_sender_receiver_pairs(self):
        roots = {"claude": self.state / "xdg/hearting/dispatch",
                 "codex": self.state / "home/.codex/.harness/dispatch",
                 "opencode": self.state / "home/.config/opencode/.harness/dispatch"}
        for sender_harness, sender_root in roots.items():
            for receiver_harness, receiver_root in roots.items():
                with self.subTest(sender=sender_harness, receiver=receiver_harness):
                    sender = dict(self.sender, harness=sender_harness,
                                  session_id=f"sender-{sender_harness}-{receiver_harness}")
                    target = dict(self.recipient, harness=receiver_harness)
                    os.environ["AGENT_PEER_LEDGER_ROOT"] = str(sender_root)
                    text, ref = self.prepare(sender=sender, recipient=target)
                    os.environ["AGENT_PEER_LEDGER_ROOT"] = str(receiver_root)
                    self.assertEqual(self.parse(text, target), sender["session_id"])
                    self.assertIsNone(self.parse(text + "改", target))
                    self.assertIsNone(self.parse(text, dict(target, session_id="new-occupant")))
                    self._receive_actual(receiver_harness, text)
                    path = receiver_root / "peer-messages" / time.strftime("%Y-%m", time.gmtime()) / (sender["session_id"] + ".jsonl")
                    row = json.loads(path.read_text().splitlines()[-1])
                    self.assertEqual(row["from"]["session_id"], sender["session_id"])
                    self.assertEqual(row["to"]["session_id"], target["session_id"])
                    transfer = sender_root / "peer-messages/transfers" / (ref + ".json")
                    transfer.rename(transfer.with_suffix(".missing"))
                    self.assertIsNone(self.parse(text, target))

    def test_metadata_types_fail_soft_in_all_receivers(self):
        changes = [(side, field, value) for side in ("from", "to")
                   for field, values in (("session_id", (123, True, [], None, " padded ")),
                                         ("name", ([], {}))) for value in values]
        changes += [(side, None, value) for side in ("from", "to") for value in ([], None)]
        for harness in ("claude", "codex", "opencode"):
            for side, field, value in changes:
                with self.subTest(harness=harness, side=side, field=field, value=value):
                    text, ref = self.prepare(recipient=dict(self.recipient, harness=harness))
                    path = pm._transfer_path(ref)
                    record = json.loads(path.read_text())
                    if field:
                        record[side][field] = value
                    else:
                        record[side] = value
                    path.write_text(json.dumps(record))
                    self._receive_actual(harness, text)
        rows = self.rows()
        self.assertEqual(len(rows), len(changes) * 3)
        self.assertTrue(all(row["from"]["session_id"] == "" for row in rows))

    def test_missing_claude_tag_does_not_remove_proven_identity(self):
        sender = dict(self.sender, harness="claude", session_id="claude-unremembered")
        text, ref = self.prepare(sender=sender)
        self.assertIn("[?]", text)
        self.assertEqual(self.parse(text), sender["session_id"])
        self.assertIsNone(pm.parse_peer_trailer(text)["session_id"])
        pm._transfer_path(ref).write_text("null")
        self.assertIsNone(self.parse(text))

    def test_duplicate_ref_across_trusted_roots_is_ambiguous(self):
        text, ref = self.prepare()
        other_root = self.state / "home/.codex/.harness/dispatch"
        other = other_root / "peer-messages/transfers" / (ref + ".json")
        other.parent.mkdir(parents=True)
        other.write_bytes(pm._transfer_path(ref).read_bytes())
        self.assertIsNone(self.parse(text))

    def test_pinned_opencode_regex_and_writer_leave_new_sender_unattributed(self):
        source = pinned_source("adapters/opencode/plugins/hearting-guards.js")
        old_python = pinned_source("utilities/peer-message.py")
        scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_peer"}
        exec(compile(old_python, "pinned-peer-message", "exec"), scope)
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fn = input.source.slice(input.source.indexOf('const peerTrailerRe ='), input.source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nspawnPeerNotice("recipient-a", prompt, root)', {
  path, root: input.root, prompt: input.prompt, process: {env: {}},
  spawn: (exe, args) => { result = {exe, args}; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        for harness in ("codex", "claude"):
            text, _ = self.prepare(sender=dict(self.sender, harness=harness),
                                   recipient={"harness": "opencode", "session_id": "recipient-a"})
            proc = subprocess.run(["node", "-e", js],
                                  input=json.dumps({"root": str(ROOT), "source": source, "prompt": text}),
                                  text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            call = json.loads(proc.stdout)
            with mock.patch.object(sys, "stdin", io.StringIO(call["body"])):
                self.assertEqual(scope["main"](call["args"][1:]), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["from"]["session_id"] == "" for row in self.rows()))

    def test_python_receive_cli_exact_and_wrong_recipient(self):
        for sid in ("recipient-a", "new-occupant"):
            text, _ = self.prepare(recipient={"harness": "opencode", "session_id": "recipient-a"})
            proc = subprocess.run([sys.executable, str(ROOT / "utilities/peer-message.py"), "receive",
                                   "--to-harness", "opencode", "--to-session-id", sid],
                                  input=text, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["to"]["session_id"]: r["from"]["session_id"] for r in rows},
                         {"recipient-a": self.sender["session_id"], "new-occupant": ""})

    def test_claude_and_codex_actual_receiver_hooks(self):
        claude = module("alias_claude_hook", "hooks/peer-message-record.py")
        codex = module("alias_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
        for harness, hook in (("claude", claude), ("codex", codex)):
            for target in ("recipient-a", "new-occupant"):
                text, _ = self.prepare(recipient={"harness": harness, "session_id": "recipient-a"})
                payload = {"session_id": target, "cwd": str(ROOT), "prompt": text}
                if harness == "claude":
                    hook.handle_prompt(payload)
                else:
                    with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "ambient-wrong"}):
                        hook.peer_notice(payload, text, str(ROOT))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["from"]["session_id"], self.sender["session_id"] if row["to"]["session_id"] == "recipient-a" else "")

    def test_opencode_plugin_forwards_actual_prompt_to_canonical_cli(self):
        # Run the unchanged plugin function in a VM with a recording transport;
        # then execute the captured Python CLI synchronously in the temp root.
        text, _ = self.prepare(recipient={"harness": "opencode", "session_id": "recipient-a"})
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.root + '/adapters/opencode/plugins/hearting-guards.js', 'utf8');
const fn = source.slice(source.indexOf('function spawnPeerNotice('), source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nspawnPeerNotice(sid, prompt, root)', {
  path, root: input.root, sid: input.sid, prompt: input.prompt, process: {env: {}},
  spawn: (exe, args) => { result = {exe, args}; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        for sid in ("recipient-a", "new-occupant"):
            proc = subprocess.run(["node", "-e", js], input=json.dumps({"root": str(ROOT), "sid": sid, "prompt": text}),
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            call = json.loads(proc.stdout)
            self.assertEqual(call["body"], text)
            self.assertIn("receive", call["args"])
            received = subprocess.run([call["exe"], *call["args"]], input=call["body"], text=True, capture_output=True, timeout=5)
            self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(len(self.rows()), 2)
        for row in self.rows():
            self.assertEqual(row["from"]["session_id"], self.sender["session_id"] if row["to"]["session_id"] == "recipient-a" else "")


class PendingCallbacks(unittest.TestCase):
    setUp = AliasReceive.setUp
    rows = AliasReceive.rows

    def test_opencode_normal_registration_and_callback_observation_is_bounded_and_fail_soft(self):
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const logs = [];
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async ({body}) => logs.push(body)}}});
if (typeof hooks.dispose !== "function") throw Error("normal host disposal missing");
await hooks["tool.execute.after"]({sessionID: "actual-callback", tool: "fixture-noop", args: {secret: "private-tool-body"}}, {});
await hooks["tool.execute.after"]({sessionID: "actual-callback", tool: "fixture-noop", args: {}}, {});
const failing = await AgentHarnessGuards({client: {app: {log: () => {throw Error("private-error")}}}});
await failing["tool.execute.after"]({sessionID: "another-callback", tool: "fixture-noop", args: {}}, {});
console.log(JSON.stringify(logs));
'''
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        logs = json.loads(run.stdout)
        self.assertEqual([row["extra"]["reason"] for row in logs], ["plugin-registered", "callback-no-pane"])
        self.assertEqual([row["extra"]["stage"] for row in logs], ["plugin", "callback"])
        self.assertTrue(all(row["level"] == "info" and row["extra"]["module"] == "hearting-peer-identity" for row in logs))
        self.assertEqual(logs[1]["extra"]["sessionID"], "actual-callback")
        self.assertNotIn("private-tool-body", run.stdout)
        self.assertNotIn("private-error", run.stdout)

    def test_opencode_publisher_observation_preserves_attempt_receipt_and_failure_boundaries(self):
        js = r'''
import { readFileSync } from "node:fs";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import vm from "node:vm";
const source = readFileSync(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js", "utf8");
const logs = [], timers = [];
const scope = {process: {env: {}}, isWorkerSession: () => false, Buffer,
  setTimeout: cb => {const t = {cb, unref() {}}; timers.push(t); return t}, clearTimeout: () => {}};
vm.runInNewContext(source.slice(source.indexOf("function sdkResponseData("), source.indexOf("\nfunction collectPreflight")), scope);
const ctx = {client: {app: {log: async ({body}) => logs.push(body)}}};
const observation = {schema: "hearting-pane-observation-v1", reason: "report-attempts-finished",
  session_report: "exit0", metadata_report: "timeout", session_report_rc: 0};
async function emit(text, code = 0, action = "close") {
  const child = new EventEmitter(); child.stdout = new PassThrough(); child.kill = () => {throw Error("no helper signal")};
  scope.observePanePublisher(child, ctx, "exact", 0);
  if (text) child.stdout.write(text);
  await new Promise(resolve => setImmediate(resolve));
  if (action === "timeout") timers.at(-1).cb();
  if (action === "error") child.emit("error", Object.assign(Error("synthetic spawn denied"), {code: "EACCES", errno: -13}));
  if (action === "read-error") child.stdout.emit("error", Error("private-read-error"));
  child.emit("close", code);
  return child;
}
await emit(JSON.stringify(observation));
await emit(JSON.stringify({schema: "hearting-pane-observation-v1", reason: "guard-refused",
  session_report: "not-attempted", metadata_report: "not-attempted"}));
await emit("private raw text");
await emit(JSON.stringify(observation) + "\n" + JSON.stringify(observation));
await emit("가".repeat(400)); // 1200 UTF-8 bytes, below 1024 characters.
await emit("", 7);
await emit("", 0, "error");
await emit("", 0, "read-error");
const late = await emit("", 0, "timeout");
late.emit("close", 0); // Timeout result cannot later become an attempt success.
await emit(JSON.stringify({...observation, sessionID: "foreign", body: "private-body"}));
const stale = new EventEmitter(); stale.stdout = new PassThrough();
scope.observePanePublisher(stale, ctx, "old", -1);
stale.stdout.write(JSON.stringify(observation));
await new Promise(resolve => setImmediate(resolve)); stale.emit("close", 0);
scope.peerIdentityLog(ctx, "callback", "callback-entry", {sessionID: "x".repeat(10000)});
console.log(JSON.stringify(logs));
'''
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        logs = json.loads(run.stdout)
        self.assertEqual([row["extra"]["reason"] for row in logs], [
            "report-attempts-finished", "guard-refused", "publisher-observation-invalid",
            "publisher-observation-invalid", "publisher-observation-overflow", "publisher-exit-error",
            "publisher-spawn-error", "publisher-observation-read-error", "publisher-observation-timeout", "publisher-observation-invalid",
            "publisher-stale-observation", "callback-entry"])
        self.assertEqual(logs[0]["extra"]["sessionReport"], "exit0")
        self.assertEqual(logs[0]["extra"]["metadataReport"], "timeout")
        self.assertEqual({key: logs[6]["extra"][key] for key in ["errorCode", "errorErrno", "errorMessage"]},
                         {"errorCode": "EACCES", "errorErrno": -13, "errorMessage": "synthetic spawn denied"})
        self.assertNotIn("sessionReport", logs[-2]["extra"])
        self.assertNotIn("sessionID", logs[-1]["extra"])
        self.assertTrue(logs[-1]["extra"]["sessionIDInvalid"])
        self.assertNotIn("private", run.stdout)
        self.assertNotIn("received", run.stdout)

    def run_pane_projection_fixture(self, body, timeout=5):
        # Fixed native invocation/SDK/child fixtures; expected results below are literals.
        js = r'''
import { existsSync as fsExistsSync, readFileSync, readFileSync as fsReadFileSync } from "node:fs";
import path from "node:path";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import vm from "node:vm";
const source = readFileSync(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js", "utf8");
function fixture(options = {}) {
  const commands = [], children = [], logs = [], reads = [], timers = [], sync = [];
  let now = 1000;
  const native = options.command || Buffer.from((options.argv || ["/native/opencode", "--session", "ses_A"]).join("\0") + "\0");
  const files = {"/proc/self/cmdline": native,
    "/proc/self/stat": Buffer.from(options.stat || "123 (opencode) S " + "0 ".repeat(18) + "77 0")};
  const scope = {path, Buffer, process: {pid: 123, env: Object.assign(
      {HERDR_PANE_ID: "fixture-pane", OPENCODE_SESSION_ID: "ses_foreign"}, options.env || {})},
    isWorkerSession: () => !!options.worker, existsSync: fsExistsSync, readFileSync: fsReadFileSync,
    root: options.root || "fixture-root", herdrProjection: options.herdrProjection || "fixture-root/tools/fleet/herdr_projection.py",
    Date: {now: () => now}, AbortController,
    openSync: file => {reads.push(file);if (!files[file]) throw Error("foreign proc read");return file},
    readSync: (fd, buffer, offset, size) => files[fd].copy(buffer, offset, 0, size), closeSync: () => {},
    realpathSync: file => file === "/proc/self/cwd" ? "/fixture/project" : file,
    setTimeout: (cb, ms) => {const timer = setTimeout(cb, ms);timers.push({cb, ms, timer});return timer}, clearTimeout,
    spawn: (exe, args) => {
      commands.push(args);
      if (options.spawnThrows) {options.spawnThrows = false;throw Object.assign(Error("synthetic spawn failure"), {code: "EACCES", errno: -13})}
      const child = new EventEmitter();child.pid = options.spawnNoPid ? undefined : children.length + 100;
      child.stdout = new PassThrough();child.unref = () => {};child.kill = () => {throw Error("no signals")};
      children.push(child);return child;
    },
    spawnSync: (exe, args, config) => {
      sync.push({exe, args, config});
      if (options.syncThrows) throw Object.assign(Error("synthetic sync failure"), {code: "EFAULT", errno: -14});
      return options.syncResult || {pid: 0, error: Object.assign(Error("synthetic fallback denied"), {code: "EACCES", errno: -13})};
    }};
  vm.runInNewContext(source.slice(source.indexOf("function sdkResponseData("), source.indexOf("\nfunction collectPreflight")), scope);
  return {scope, commands, children, logs, reads, timers, sync, setNow: value => {now = value},
    ctx: get => ({directory: "/fixture/project", client: {app: {log: async ({body}) => logs.push(body)},
      session: {get: get || (async () => ({data: {id: "ses_A"}}))}}}),
    exit: index => {children[index].emit("exit", 0);children[index].emit("close", 0)}};
}
''' + body
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=timeout)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_opencode_native_selector_keeps_pure_invocation_and_rejects_mixed_continue(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();
const cases = [
 ["opencode", "--session", "ses_A"], ["/native/opencode", "-s", "ses_A", "--model", "provider/model"],
 ["opencode", "--session=ses_A", "--prompt=--continue"],
 ["opencode", "--session", "ses_A", "--continue=false", "--fork=false"],
 ["opencode", "--no-continue", "--session", "ses_A"],
 ["opencode", "--session", "ses_A", "--", "/fixture/project"],
 ["opencode", "-s", "ses_A", "-c"], ["opencode", "--session", "ses_A", "--continue"],
 ["opencode", "--continue=true", "--session", "ses_A"], ["opencode", "--session", "ses_A", "--fork"],
 ["opencode", "--session", "ses_A", "--fork", "--continue=false"],
 ["opencode", "--continue"], ["opencode", "run", "--session", "ses_A"],
 ["opencode", "serve", "--session", "ses_A"], ["opencode", "attach", "--session", "ses_A"],
 ["opencode", "--", "--session", "ses_A"], ["opencode", "--prompt", "--session", "ses_A"],
 ["opencode", "--session", "ses_A", "--session", "ses_A"], ["opencode", "-sA"],
 ["opencode", "--unknown", "--session", "ses_A"], ["node", "--session", "ses_A"],
 ["opencode", "--session", "ses_A\0foreign"], ["opencode", "--session", "ses_A", "--mini"],
 ["opencode"], ["opencode", "--session", "ses_A", "--continue=0"],
];
for (const key of ["--prompt", "--model", "-m", "--agent", "--port", "--hostname", "--mdns-domain", "--cors", "--log-level"]) {
 for (const flag of ["--continue", "-c", "--fork"]) cases.push(["opencode", "--session=ses_A", key, flag]);
}
console.log(JSON.stringify(cases.map(argv => f.scope.nativePaneSelector(argv))));
''')
        self.assertEqual(result, ["ses_A"] * 6 + [None] * 46)

    def test_opencode_native_origin_invalid_or_bounded_proc_data_has_no_sdk_fallback(self):
        result = self.run_pane_projection_fixture(r'''
const results = [];
const cases = [{stat: "999 (opencode) S " + "0 ".repeat(18) + "77 0"}, {stat: "123 (opencode) S 0"},
 {command: Buffer.from("opencode\0--session\0ses_A")},
 {argv: ["opencode", "--session", "ses_A", "--prompt", "x".repeat(33000)]},
 {argv: ["opencode", "--session", "ses_A", "--continue"]},
 {argv: ["opencode", "--session=ses_A", "--prompt", "--continue"]}, {worker: true}];
for (const options of cases) {
 const f = fixture(options);let gets = 0;
 await f.scope.projectPane("ses_A", f.ctx(async () => {gets++;return {id: "ses_A"}}));
 results.push([gets, f.commands.length]);
}
console.log(JSON.stringify(results));
''')
        self.assertEqual(result, [[0, 0]] * 7)

    def test_opencode_foreign_first_directory_and_unknown_origin_consume_nothing(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();let gets = 0;
const ctx = f.ctx(async ({path}) => {gets++;return {data: {id: path.id}}});
await f.scope.projectPane("ses_B", ctx); // Another parentless root in the same directory arrives first.
const foreignDirectory = f.ctx();foreignDirectory.directory = "/another/project";
await f.scope.projectPane("ses_A", foreignDirectory);
const before = [gets, f.commands.length];
await f.scope.projectPane("ses_A", ctx);
const unavailable = fixture({argv: ["opencode", "--session", "ses_A", "--continue"]});
await unavailable.scope.projectPane("ses_A", unavailable.ctx());
console.log(JSON.stringify({before, gets, commands: f.commands, unavailable: unavailable.commands, reads: f.reads}));
''')
        self.assertEqual(result["before"], [0, 0])
        self.assertEqual(result["gets"], 1)
        self.assertEqual(result["commands"], [["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A",
                                              "--seq", "1000001", "--session-start-source", "startup"]])
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["reads"], ["/proc/self/cmdline", "/proc/self/stat"])

    def test_opencode_peer_identity_publication_requires_exact_sdk_top_level_session(self):
        result = self.run_pane_projection_fixture(r'''
const commands = [];
for (const response of [{data: {id: "ses_A"}}, {id: "ses_A", parentID: null},
 {data: {id: "ses_foreign"}}, {data: {id: "ses_A", parentID: "child"}},
 {data: {id: "ses_A", parentID: 0}}, {error: "failed", data: {id: "ses_A"}},
 {response: {ok: false}, data: {id: "ses_A"}}, {}]) {
 const f = fixture();await f.scope.projectPane("ses_A", f.ctx(async options => {
  if (options.path.id !== "ses_A" || options.throwOnError !== true || !options.signal) throw Error("wrong SDK v1 call");
  return response;
 }));commands.push(f.commands[0]);f.exit(0);
}
console.log(JSON.stringify(commands));
''')
        startup = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A",
                   "--seq", "1000001", "--session-start-source", "startup"]
        metadata = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A", "--no-report-session"]
        self.assertEqual(result, [startup, startup] + [metadata] * 6)

    def test_opencode_normal_tool_retry_retains_actual_sdk_identity_and_observes_timeout(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();let gets = 0, resolveFirst;
const ctx = f.ctx(async ({path}) => {
 if (path.id !== "ses_A") throw Error("guessed identity");gets++;
 if (gets === 1) return await new Promise(resolve => {resolveFirst = resolve});
 return {id: "ses_A"};
});
await Promise.all([f.scope.projectPane("ses_A", ctx, true), f.scope.projectPane("ses_A", ctx, true)]);
resolveFirst({id: "late-foreign"});f.exit(0);
await f.scope.projectPane("ses_A", ctx, true);
f.setNow(12000);await f.scope.projectPane("ses_A", ctx, true);
const sdk = f.logs.filter(row => row.extra.stage === "sdk").map(row => [row.extra.reason, row.extra.verified]);
console.log(JSON.stringify({gets, commands: f.commands, sdk}));
''')
        self.assertEqual(result["gets"], 2)
        self.assertEqual(result["commands"], [
            ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A", "--no-report-session"],
            ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A",
             "--seq", "1000001", "--session-start-source", "startup"]])
        self.assertEqual(result["sdk"], [["sdk-timeout", False], ["verified", True]])

    def test_opencode_spent_startup_and_child_slot_survive_reload_and_observer_timeout(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();const first = f.ctx();
f.scope.registerPaneContext(first);await f.scope.projectPane("ses_A", first);
f.scope.retirePaneContext(first);const next = f.ctx();f.scope.registerPaneContext(next);
f.timers.find(t => t.ms === 5000).cb(); // Observation ends, actual child remains alive.
await Promise.all([f.scope.projectPane("ses_A", next), f.scope.projectPane("ses_A", next)]);
const whileAlive = f.commands.length;
f.exit(0);await f.scope.projectPane("ses_A", next);f.exit(1);
f.scope.retirePaneContext(next);const third = f.ctx();f.scope.registerPaneContext(third);
await f.scope.projectPane("ses_A", third);f.exit(2);
f.scope.invalidatePaneOrigin("ses_A");await f.scope.projectPane("ses_A", third);f.exit(3);
console.log(JSON.stringify({whileAlive, commands: f.commands,
 timeout: f.logs.some(row => row.extra.reason === "publisher-stale-observation")}));
''')
        self.assertEqual(result["whileAlive"], 1)
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--seq", "1000001", "--session-start-source", "startup"],
            prefix + ["--seq", "1000002"], prefix + ["--seq", "1000003"], prefix + ["--no-report-session"]])
        self.assertTrue(result["timeout"])

    def test_opencode_disposed_pending_origin_never_rearms_or_spawns_from_late_sdk(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();let resolveOld;
const old = f.ctx(async () => await new Promise(resolve => {resolveOld = resolve}));
f.scope.registerPaneContext(old);const waiting = f.scope.projectPane("ses_A", old);
f.scope.retirePaneContext(old);const next = f.ctx();f.scope.registerPaneContext(next);
await f.scope.projectPane("ses_A", next);f.exit(0);
resolveOld({data: {id: "ses_A"}});await waiting;
f.scope.retirePaneContext(next);const third = f.ctx();f.scope.registerPaneContext(third);
await f.scope.projectPane("ses_A", third);f.exit(1);
await f.scope.projectPane("ses_B", third); // A later fork is not the native launch selection.
console.log(JSON.stringify({commands: f.commands,
 sdk: f.logs.filter(row => row.extra.stage === "sdk").map(row => row.extra.reason)}));
''')
        metadata = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A", "--no-report-session"]
        self.assertEqual(result["commands"], [metadata, metadata])
        self.assertEqual(result["sdk"], ["verified", "sdk-stale-callback", "verified"])

    def test_opencode_spawn_failure_report_zero_can_retry_but_created_attempt_never_replays(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture({spawnThrows: true});const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);await f.scope.projectPane("ses_A", ctx);
f.children[0].stdout.write(JSON.stringify({schema: "hearting-pane-observation-v1", reason: "guard-refused",
 session_report: "not-attempted", metadata_report: "not-attempted"}));
await new Promise(resolve => setImmediate(resolve));f.exit(0);
await f.scope.projectPane("ses_A", ctx);f.exit(1);
const noPid = fixture({spawnNoPid: true});const other = noPid.ctx();
await noPid.scope.projectPane("ses_A", other);noPid.children[0].emit("error", Error("no process"));
await noPid.scope.projectPane("ses_A", other);
console.log(JSON.stringify({commands: f.commands, noPid: noPid.commands}));
''')
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--seq", "1000001", "--session-start-source", "startup"],
            prefix + ["--seq", "1000002", "--session-start-source", "startup"], prefix + ["--seq", "1000003"]])
        self.assertEqual(result["noPid"], [prefix + ["--seq", "1000001", "--session-start-source", "startup"],
            prefix + ["--seq", "1000002", "--session-start-source", "startup"]])

    def test_opencode_spawn_error_fallback_uses_same_invocation_once_and_preserves_errno(self):
        result = self.run_pane_projection_fixture(r'''
const observation = JSON.stringify({schema: "hearting-pane-observation-v1", reason: "report-attempts-finished",
 session_report: "exit0", metadata_report: "exit0", session_report_rc: 0, metadata_report_rc: 0});
const syncResult = {pid: 901, status: 0, stdout: observation};
const f = fixture({spawnNoPid: true, syncResult});const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
const error = Object.assign(Error("synthetic async failure\n" + "가".repeat(200)), {code: "EFAULT", errno: -14});
f.children[0].emit("error", error);f.children[0].emit("error", error);f.exit(0);
await f.scope.projectPane("ses_A", ctx);f.exit(1);
const thrown = fixture({spawnThrows: true, syncResult});const other = thrown.ctx();
await thrown.scope.projectPane("ses_A", other);await thrown.scope.projectPane("ses_A", other);thrown.exit(0);
console.log(JSON.stringify({commands: f.commands, sync: f.sync, logs: f.logs,
 thrown: {commands: thrown.commands, sync: thrown.sync, logs: thrown.logs}}));
''')
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        startup = prefix + ["--seq", "1000001", "--session-start-source", "startup"]
        refresh = prefix + ["--seq", "1000002"]
        for record in [result, result["thrown"]]:
            self.assertEqual(record["commands"], [startup, refresh])
            self.assertEqual(len(record["sync"]), 1)
            attempt = record["sync"][0]
            self.assertEqual(attempt["exe"], "python3")
            self.assertEqual(attempt["args"], startup)
            self.assertEqual(attempt["config"], {"cwd": "fixture-root",
                "env": {"HERDR_PANE_ID": "fixture-pane", "OPENCODE_SESSION_ID": "ses_foreign", "AGENT_HOME": "fixture-root"},
                "detached": True, "stdio": ["ignore", "pipe", "ignore"], "encoding": "utf8",
                "timeout": 10000, "killSignal": "SIGKILL", "maxBuffer": 1024})
            success = [row["extra"] for row in record["logs"] if row["extra"]["reason"] == "publisher-path-success"]
            self.assertEqual(success, [{"module": "hearting-peer-identity", "stage": "publisher",
                "reason": "publisher-path-success", "sessionID": "ses_A", "publisherPath": "sync-fallback", "publisherRc": 0}])
            self.assertTrue(any(row["extra"].get("sessionReport") == "exit0" for row in record["logs"]))
        error = next(row["extra"] for row in result["logs"] if row["extra"]["reason"] == "publisher-spawn-error")
        self.assertEqual((error["errorCode"], error["errorErrno"]), ("EFAULT", -14))
        self.assertLessEqual(len(error["errorMessage"].encode("utf-8")), 256)
        self.assertNotIn("\n", error["errorMessage"])
        error = next(row["extra"] for row in result["thrown"]["logs"] if row["extra"]["reason"] == "publisher-spawn-error")
        self.assertEqual((error["errorCode"], error["errorErrno"], error["errorMessage"]),
                         ("EACCES", -13, "synthetic spawn failure"))

    def test_opencode_spawn_fallback_failure_live_and_stale_boundaries(self):
        result = self.run_pane_projection_fixture(r'''
const resultRows = [];
for (const options of [{syncResult: {pid: 903, status: null, error: Object.assign(Error("synthetic timeout"), {code: "ETIMEDOUT", errno: -110})}},
 {syncResult: {pid: 904, status: 7, stdout: "private-stdout"}},
 {syncResult: {pid: 905, status: 0, stdout: "private-invalid-output"}}, {syncThrows: true},
 {syncResult: {pid: 906, status: null, error: Object.assign(Error("synthetic overflow"), {code: "ENOBUFS", errno: -105})}}]) {
 const f = fixture({...options, spawnThrows: true});await f.scope.projectPane("ses_A", f.ctx());
 resultRows.push({sync: f.sync.length, logs: f.logs});
}
const live = fixture();const liveCtx = live.ctx();await live.scope.projectPane("ses_A", liveCtx);
live.children[0].emit("spawn");live.children[0].emit("error", Object.assign(Error("synthetic launched error"), {errno: -14}));
await live.scope.projectPane("ses_A", liveCtx);const held = live.commands.length;live.exit(0);
await live.scope.projectPane("ses_A", liveCtx);live.exit(1);
const stale = fixture();const old = stale.ctx();await stale.scope.projectPane("ses_A", old);
stale.scope.retirePaneContext(old);stale.children[0].emit("error", Error("synthetic old error"));stale.exit(0);
console.log(JSON.stringify({numericError: live.scope.publisherErrorFields({code: 14, errno: -14, message: "synthetic numeric code"}),
 resultRows, live: {sync: live.sync.length, held, commands: live.commands, logs: live.logs},
 stale: {sync: stale.sync.length, logs: stale.logs}}));
''')
        expected = ["publisher-sync-fallback-error", "publisher-sync-fallback-exit-error",
                    "publisher-observation-invalid", "publisher-sync-fallback-error", "publisher-sync-fallback-error"]
        for row, reason in zip(result["resultRows"], expected):
            self.assertEqual(row["sync"], 1)
            self.assertIn(reason, [log["extra"]["reason"] for log in row["logs"]])
            self.assertNotIn("private", json.dumps(row["logs"]))
        self.assertEqual(result["resultRows"][0]["logs"][-1]["extra"]["errorCode"], "ETIMEDOUT")
        self.assertEqual(result["resultRows"][3]["logs"][-1]["extra"]["errorErrno"], -14)
        self.assertEqual(result["numericError"], {"errorCode": 14, "errorErrno": -14, "errorMessage": "synthetic numeric code"})
        self.assertEqual(result["live"]["sync"], 0)
        self.assertEqual(result["live"]["held"], 1)
        self.assertEqual(len(result["live"]["commands"]), 2)
        self.assertIn("publisher-sync-fallback-skipped-live", [row["extra"]["reason"] for row in result["live"]["logs"]])
        self.assertIn("async", [row["extra"].get("publisherPath") for row in result["live"]["logs"]])
        self.assertEqual(result["stale"]["sync"], 0)
        self.assertIn("publisher-sync-fallback-stale", [row["extra"]["reason"] for row in result["stale"]["logs"]])

    def _live_publisher_root(self, name):
        live = self.state / name
        (live / "core").mkdir(parents=True)
        (live / "core" / "CORE.md").write_text("live")
        projector_dir = live / "adapters" / "opencode" / "bin"
        projector_dir.mkdir(parents=True)
        (projector_dir / "preflight.sh").write_text("live")
        helper_dir = live / "tools" / "fleet"
        helper_dir.mkdir(parents=True)
        (helper_dir / "herdr_projection.py").write_text("live")
        return live

    def test_opencode_stale_release_root_resolves_live_target_with_same_invocation(self):
        # Deleted import-time release (old rune ENOENT/-2 shape): async and sync
        # share one resolved live target, same argv/seq/identity, fallback once.
        live = self._live_publisher_root("live-root")
        helper = str(live / "tools" / "fleet" / "herdr_projection.py")
        observation = {"schema": "hearting-pane-observation-v1", "reason": "report-attempts-finished",
                       "session_report": "exit0", "metadata_report": "exit0"}
        body = r'''
const observation = %%OBSERVATION%%;
const syncResult = {pid: 901, status: 0, stdout: JSON.stringify(observation)};
const f = fixture({root: "/deleted/releases/v3.2.13",
  herdrProjection: "/deleted/releases/v3.2.13/tools/fleet/herdr_projection.py",
  env: {AGENT_HOME: "%%LIVE%%"}, spawnNoPid: true, syncResult});
const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
f.children[0].emit("error", Object.assign(Error("synthetic stale release failure"), {code: "ENOENT", errno: -2}));
await f.scope.projectPane("ses_A", ctx); f.exit(1);
console.log(JSON.stringify({commands: f.commands, sync: f.sync, logs: f.logs}));
'''.replace("%%OBSERVATION%%", json.dumps(observation)).replace("%%LIVE%%", str(live))
        result = self.run_pane_projection_fixture(body)
        startup = [helper, "--harness", "opencode", "--session-id", "ses_A",
                   "--seq", "1000001", "--session-start-source", "startup"]
        refresh = [helper, "--harness", "opencode", "--session-id", "ses_A", "--seq", "1000002"]
        self.assertEqual(result["commands"], [startup, refresh])
        self.assertEqual(len(result["sync"]), 1)
        attempt = result["sync"][0]
        self.assertEqual(attempt["exe"], "python3")
        self.assertEqual(attempt["args"], startup)
        self.assertEqual(attempt["config"]["cwd"], str(live))
        self.assertEqual(attempt["config"]["env"]["AGENT_HOME"], str(live))
        self.assertNotIn("deleted", json.dumps(result))
        error = next(row["extra"] for row in result["logs"] if row["extra"]["reason"] == "publisher-spawn-error")
        self.assertEqual((error["errorCode"], error["errorErrno"]), ("ENOENT", -2))
        success = [row["extra"] for row in result["logs"] if row["extra"]["reason"] == "publisher-path-success"]
        self.assertEqual([(row["publisherPath"], row["publisherRc"]) for row in success], [("sync-fallback", 0)])

    def test_opencode_live_root_kept_when_interpreter_missing_on_both_paths(self):
        # Same errno, other boundary: frozen root/helper live, so no switch —
        # the interpreter failure stays bounded on async and sync, fallback once.
        live = self._live_publisher_root("live-root")
        other = self._live_publisher_root("other-root")
        helper = str(live / "tools" / "fleet" / "herdr_projection.py")
        body = r'''
const f = fixture({root: "%%LIVE%%", herdrProjection: "%%HELPER%%",
  env: {AGENT_HOME: "%%OTHER%%"}, spawnNoPid: true,
  syncResult: {pid: 0, error: Object.assign(Error("synthetic interpreter missing"), {code: "ENOENT", errno: -2})}});
const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
f.children[0].emit("error", Object.assign(Error("synthetic async interpreter missing"), {code: "ENOENT", errno: -2}));
await f.scope.projectPane("ses_A", ctx); f.exit(1);
console.log(JSON.stringify({commands: f.commands, sync: f.sync, logs: f.logs}));
'''.replace("%%LIVE%%", str(live)).replace("%%HELPER%%", helper).replace("%%OTHER%%", str(other))
        result = self.run_pane_projection_fixture(body)
        prefix = [helper, "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--seq", "1000001", "--session-start-source", "startup"],
            prefix + ["--seq", "1000002", "--session-start-source", "startup"]])
        self.assertEqual(len(result["sync"]), 1)
        attempt = result["sync"][0]
        self.assertEqual(attempt["args"], result["commands"][0])
        self.assertEqual(attempt["config"]["cwd"], str(live))
        self.assertEqual(attempt["config"]["env"]["AGENT_HOME"], str(live))
        self.assertNotIn(str(other), json.dumps(result))
        reasons = [row["extra"]["reason"] for row in result["logs"]]
        self.assertIn("publisher-spawn-error", reasons)
        self.assertIn("publisher-sync-fallback-error", reasons)
        self.assertNotIn("publisher-path-success", reasons)
        for row in result["logs"]:
            if row["extra"]["reason"] in ("publisher-spawn-error", "publisher-sync-fallback-error"):
                self.assertEqual((row["extra"]["errorCode"], row["extra"]["errorErrno"]), ("ENOENT", -2))

    def _write_tui_record(self, state_dir, pid, start, sid):
        record_dir = Path(state_dir) / "hearting" / "tui-identity"
        record_dir.mkdir(parents=True, exist_ok=True)
        record = record_dir / f"{pid}-{start}.json"
        record.write_text(json.dumps({"schema": "hearting-tui-selection-v1",
                                      "sessionID": sid, "pid": pid, "start": start}))
        return record

    def test_opencode_bare_origin_with_tui_record_publishes_through_existing_path(self):
        self._write_tui_record(str(self.state), 123, "77", "ses_A")
        result = self.run_pane_projection_fixture(r'''
const observation = JSON.stringify({schema: "hearting-pane-observation-v1", reason: "report-attempts-finished",
 session_report: "exit0", metadata_report: "exit0"});
const f = fixture({argv: ["opencode", "--auto", "--model", "provider/model"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
f.children[0].stdout.write(observation);
await new Promise(resolve => setImmediate(resolve)); f.exit(0);
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state)))
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--seq", "1000001", "--session-start-source", "startup"]])
        finished = [row["extra"] for row in result["logs"] if row["extra"]["reason"] == "report-attempts-finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual((finished[0]["sessionReport"], finished[0]["metadataReport"]), ("exit0", "exit0"))

    def test_opencode_bare_origin_without_matching_tui_record_stays_unavailable(self):
        self._write_tui_record(str(self.state), 123, "77", "ses_other")
        for options, record in (({}, None), ({"record": "mismatch"}, None)):
            with self.subTest(options=options):
                result = self.run_pane_projection_fixture(r'''
const f = fixture({argv: ["opencode", "--auto", "--model", "provider/model"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
await f.scope.projectPane("ses_A", f.ctx());
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state) if options.get("record") else "/nonexistent-state-dir"))
                self.assertEqual(result["commands"], [])
                self.assertIn("native-origin-unavailable", [row["extra"]["reason"] for row in result["logs"]])

    def test_opencode_resume_argv_never_uses_tui_record(self):
        self._write_tui_record(str(self.state), 123, "77", "ses_A")
        result = self.run_pane_projection_fixture(r'''
const f = fixture({argv: ["opencode", "--session", "ses_A", "--continue"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
await f.scope.projectPane("ses_A", f.ctx());
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state)))
        self.assertEqual(result["commands"], [])
        self.assertIn("native-origin-unavailable", [row["extra"]["reason"] for row in result["logs"]])

    def test_opencode_selection_swap_during_sdk_skips_late_publish(self):
        record = self._write_tui_record(str(self.state), 123, "77", "ses_A")
        swapped = {"schema": "hearting-tui-selection-v1", "sessionID": "ses_B", "pid": 123, "start": "77"}
        body = r'''
import * as realFs from "node:fs";
const f = fixture({argv: ["opencode", "--auto", "--model", "provider/model"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
const ctx = f.ctx(async () => {
  realFs.writeFileSync("%%RECORD%%", JSON.stringify(%%SWAPPED%%));
  return {data: {id: "ses_A"}};
});
await f.scope.projectPane("ses_A", ctx);
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state)).replace("%%RECORD%%", str(record)).replace(
            "%%SWAPPED%%", json.dumps(swapped))
        result = self.run_pane_projection_fixture(body)
        self.assertEqual(result["commands"], [])
        reasons = [row["extra"]["reason"] for row in result["logs"]]
        self.assertIn("publisher-stale-selection", reasons)
        self.assertNotIn("publisher-spawned", reasons)

    def test_opencode_tui_entry_records_selection_and_clears_on_home_and_dispose(self):
        body = open(ROOT / "adapters/opencode/tui/hearting-tui-identity.ts").read().replace(
            'import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"', "").replace(
            'import path from "node:path"', "").replace(
            "export default", "var __module =")
        js = r'''
import path from "node:path";
import * as realFs from "node:fs";
import vm from "node:vm";
const input = JSON.parse(process.env.FIXTURE_TUI);
const stat = "456 (opencode) S " + "0 ".repeat(18) + "99 0";
const scope = {path, Date, JSON,
  mkdirSync: realFs.mkdirSync, renameSync: realFs.renameSync, rmSync: realFs.rmSync,
  writeFileSync: realFs.writeFileSync,
  readFileSync: (file, enc) => String(file) === "/proc/self/stat" ? stat : realFs.readFileSync(file, enc),
  process: {pid: 456, env: {XDG_STATE_HOME: input.state}}};
let current = {name: "home"};
const subs = [], unsubs = [], disposed = [], slotRegs = [];
const api = {route: {get current() {return current}},
  event: {on: (type, handler) => {subs.push([type, handler]); return () => unsubs.push(type)}},
  lifecycle: {onDispose: (fn) => disposed.push(fn)},
  slots: {register: (plugin) => {slotRegs.push(plugin); return "mock-slot-id"}}};
vm.runInNewContext(input.source, scope);
await scope.__module.tui(api);
const read = () => {
  try {
    return realFs.readFileSync(input.record, "utf8");
  } catch { return null }
};
const slots = slotRegs[0].slots;
const fire = (type, event) => subs.find(([name]) => name === type)[1](event || {});
const home = read();
fire("session.created", {type: "session.created", properties: {sessionID: "ses_new", info: {id: "ses_new"}}});
const gap = read();
current = {name: "session", params: {sessionID: "ses_new"}};
slots.session_prompt_right({}, {session_id: "ses_new"});
const mounted = read();
slots.session_prompt_right({}, {session_id: "ses_other"});
const disagree = read();
current = {name: "workspace-smoke", params: {tab: 0}};
slots.app_bottom({});
const customCleared = read();
current = {name: "session", params: {sessionID: "ses_back"}};
slots.app_bottom({});
const appWrote = read();
fire("session.deleted", {type: "session.deleted", properties: {sessionID: "ses_back", info: {id: "ses_back"}}});
const clearedByDelete = read();
current = {name: "home"};
slots.home_prompt_right({}, {});
const cleared = read();
for (const fn of disposed) await fn();
console.log(JSON.stringify({id: scope.__module.id, hasServer: "server" in scope.__module,
  slotNames: Object.keys(slots), subscribed: subs.map(([type]) => type),
  unsubscribed: unsubs, home, gap, mounted: mounted && JSON.parse(mounted).sessionID,
  disagree: disagree && JSON.parse(disagree).sessionID, customCleared,
  appWrote: appWrote && JSON.parse(appWrote).sessionID, clearedByDelete, cleared}));
'''
        env = dict(os.environ, FIXTURE_TUI=json.dumps({
            "source": body, "state": str(self.state),
            "record": str(Path(self.state) / "hearting" / "tui-identity" / "456-99.json")}))
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["id"], "hearting.tui-identity")
        self.assertFalse(result["hasServer"])
        self.assertEqual(sorted(result["slotNames"]), ["app_bottom", "home_prompt_right", "session_prompt_right"])
        self.assertEqual(result["subscribed"], ["session.created", "session.deleted", "tui.session.select",
            "session.next.prompted", "message.updated", "session.status", "session.idle"])
        self.assertIsNone(result["home"])
        self.assertIsNone(result["gap"])
        self.assertEqual(result["mounted"], "ses_new")
        self.assertEqual(result["disagree"], "ses_new")
        self.assertIsNone(result["customCleared"])
        self.assertEqual(result["appWrote"], "ses_back")
        self.assertIsNone(result["clearedByDelete"])
        self.assertIsNone(result["cleared"])
        self.assertEqual(sorted(result["unsubscribed"]), sorted(result["subscribed"]))

    def test_opencode_persisted_context_and_completed_turn_ack_once(self):
        js = r'''
import { pathToFileURL } from "node:url";
const root = process.env.AGENT_HOME;
const { AgentHarnessGuards } = await import(pathToFileURL(root + "/adapters/opencode/plugins/hearting-guards.js"));
const sid = "fixture-oc";
let messages = [], calls = 0;
const unwrap = (data) => process.env.FIXTURE_SDK_STYLE === "data" ? data : {data};
const ctx = { directory: root, worktree: root, client: { session: {
  messages: async () => unwrap(messages),
  prompt: async (args) => {
    calls++;
    if (args.body.noReply !== true || Object.keys(args.body).sort().join() !== "noReply,parts") throw Error("unsafe request");
    const row = {info: {id: "fixture-message-" + calls, role: "user", sessionID: sid}, parts: args.body.parts};
    messages.push(row);
    return unwrap(row);
  },
}}};
const hooks = await AgentHarnessGuards(ctx);
const tool = () => hooks["tool.execute.after"]({sessionID: sid, tool: "fixture-noop", args: {}}, {});
await Promise.all([tool(), tool()]);
await tool();
console.log(JSON.stringify({stage: "queued", calls, messages}));
messages.push({info: {id: "assistant", sessionID: sid, role: "assistant", parentID: messages[0]?.info.id,
  time: {completed: 100}}, parts: []});
await tool();
await new Promise(resolve => setTimeout(resolve, 300));
await tool();
console.log(JSON.stringify({stage: "received", calls}));
'''
        recipient = {"harness": "opencode", "session_id": "fixture-oc"}
        text, ref = pm.prepare_peer_message("original pending\u3000peer body", self.sender, recipient, defer=True)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        stages = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(stages[0]["calls"], 1)
        self.assertEqual(stages[0]["messages"][0]["parts"], [{"type": "text", "text": text}])
        self.assertEqual(stages[1]["calls"], 1)
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        self.assertEqual(len([r for r in self.rows() if r.get("transfer_ref") == ref]), 1)

    def test_opencode_data_style_retains_persisted_vs_completed_parent_boundary(self):
        with mock.patch.dict(os.environ, {"FIXTURE_SDK_STYLE": "data"}):
            self.test_opencode_persisted_context_and_completed_turn_ack_once()

    def test_opencode_lost_response_foreign_history_never_blindly_resends(self):
        js = r'''
import { pathToFileURL } from "node:url";
const root = process.env.AGENT_HOME;
const { AgentHarnessGuards } = await import(pathToFileURL(root + "/adapters/opencode/plugins/hearting-guards.js"));
const sid = "fixture-oc";
let messages = [], calls = 0;
const hooks = await AgentHarnessGuards({directory: root, worktree: root, client: {session: {
  messages: async () => ({data: messages}),
  prompt: async (args) => {
    calls++;
    messages.push({info: {id: "persisted", role: "user", sessionID: "foreign-fork"}, parts: args.body.parts});
    throw Error("response-lost-after-persist");
  },
}}});
const tool = () => hooks["tool.execute.after"]({sessionID: sid, tool: "fixture-noop", args: {}}, {});
await tool(); await tool(); await tool();
console.log(JSON.stringify({calls}));
'''
        recipient = {"harness": "opencode", "session_id": "fixture-oc"}
        text, ref = pm.prepare_peer_message("original body", self.sender, recipient, defer=True)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["calls"], 1)
        self.assertEqual(pm._read_pending(ref)["state"], "unverified")
        self.assertEqual(pm._read_pending(ref)["text"], text)
        self.assertEqual(self.rows(), [])

    def test_claude_actual_prompt_observes_ref_once_without_output_ack(self):
        recipient = {"harness": "claude", "session_id": "recipient-a"}
        text, ref = pm.prepare_peer_message("original peer body", self.sender, recipient, defer=True)
        AliasReceive._receive_actual(self, "claude", text)
        original = self.rows()
        AliasReceive._receive_actual(self, "claude", text)
        self.assertEqual(self.rows(), original)
        self.assertEqual(pm._read_pending(ref)["state"], "received")

    def test_codex_actual_prompt_observes_ref_once_without_queue_acceptance_ack(self):
        text, ref = pm.prepare_peer_message("original peer body", self.sender, self.recipient, defer=True)
        AliasReceive._receive_actual(self, "codex", text)
        original = self.rows()
        AliasReceive._receive_actual(self, "codex", text)
        self.assertEqual(self.rows(), original)
        self.assertEqual(pm._read_pending(ref)["state"], "received")


if __name__ == "__main__":
    unittest.main()
