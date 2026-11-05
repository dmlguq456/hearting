"""Threshold state plus the actual Claude and OpenCode prompt entry points."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import context_alert as alert

ROOT = Path(__file__).resolve().parents[1]


class ContextAlertTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.state = self.temp / "state"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.temp),
                    "XDG_STATE_HOME": str(self.state), "AGENT_HOME": str(ROOT)}

    def notice(self, count, sid="sid", harness="claude", env=None):
        return alert.notice(harness, sid, count, state_dir=self.state, env=env or {})

    def transcript(self, count, sid="sid"):
        path = self.temp / "transcript.jsonl"
        path.write_text(json.dumps({"type": "assistant", "sessionId": sid, "message": {
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                      "cache_read_input_tokens": count - 30, "output_tokens": 900000}}}) + "\n")
        return path

    def test_strict_boundaries_and_once_even_after_same_session_compaction(self):
        for count in (499999, 500000):
            self.assertEqual(self.notice(count), "")
        self.assertIn("700K 전에", self.notice(500001))
        self.assertEqual(self.notice(600000), "")
        self.assertEqual(self.notice(699999), "")
        self.assertEqual(self.notice(700000), "")
        self.assertIn("지금", self.notice(700001))
        for count in (0, 500001, 800000):
            self.assertEqual(self.notice(count), "")

    def test_jump_emits_only_urgent_line_and_native_new_session_resets(self):
        self.assertEqual(self.notice(703000), "컨텍스트 약 703K — 지금 /session-tidy로 정리하세요")
        self.assertEqual(self.notice(512000), "")
        self.assertEqual(self.notice(512000, sid="after-clear"),
                         "컨텍스트 약 512K — 700K 전에 /session-tidy로 정리 권장")
        self.assertTrue(self.notice(512000, harness="opencode"))

    def test_workers_owners_and_auxiliary_calls_do_not_consume_state(self):
        for env in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_SESSION_ROLE": "owner"},
                    {"AGENT_DISPATCH_DEPTH": "1"}, {"AGENT_DISPATCH_DEPTH": "2"},
                    {"AGENT_DISPATCH_CHILD": "1"}, {"FLEET_TITLE_REFRESH": "1"},
                    {"MEM_DISTILL": "1"}, {"OPENCODE_DISPATCH_SLUG": "child"}):
            self.assertEqual(self.notice(800000, env=env), "")
        self.assertFalse(self.state.exists())
        self.assertTrue(self.notice(800000))

    def test_input_only_missing_and_invalid_usage(self):
        self.assertEqual(alert.input_tokens({"input": 1, "read": 2, "write": 3, "output": 999999},
                                            ("input", "read", "write")), 6)
        for value in (None, {}, {"input": -1}, {"input": True}, {"input": float("nan")},
                      {"input": "800000"}, {"input": 2, "read": None}):
            self.assertIsNone(alert.input_tokens(value, ("input", "read", "write")))
        for count in (None, False, -1):
            self.assertEqual(self.notice(count), "")

    def test_latest_assistant_not_cumulative_output_sidechain_or_other_session(self):
        path = self.transcript(800000)
        with path.open("a") as handle:
            for row in ({"type": "assistant", "sessionId": "sid", "message": {"usage": {"input_tokens": 4}}},
                        {"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 800000}}},
                        {"type": "assistant", "sessionId": "other", "message": {"usage": {"input_tokens": 800000}}},
                        {"type": "user", "usage": {"input_tokens": 800000}}):
                handle.write(json.dumps(row) + "\n")
            handle.write("partial json\n")
        self.assertEqual(alert.claude_usage(str(path), "sid"), 4)
        self.assertIsNone(alert.claude_usage(str(self.temp / "missing"), "sid"))
        self.assertIsNone(alert.claude_usage(None, "sid"))

    def test_simultaneous_claims_emit_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.notice(703000), range(16)))
        self.assertEqual(sum(bool(value) for value in values), 1)

    def test_latest_assistant_without_usage_never_reuses_an_older_high_count(self):
        path = self.transcript(800000)
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "assistant", "sessionId": "sid",
                                    "message": {"content": "usage unavailable"}}) + "\n")
        self.assertIsNone(alert.claude_usage(str(path), "sid"))
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(alert.claude_notice({"hook_event_name": "UserPromptSubmit",
                "session_id": "sid", "transcript_path": str(path)}), "")
        self.assertFalse((self.state / "hearting/context-alert").exists())

    def test_state_failure_is_silent(self):
        self.state.write_text("cannot create directory")
        self.assertEqual(self.notice(703000), "")

    def codex_rollout(self, sid="codex-session-id", count=703000, *, source=None, latest=True):
        path = self.temp / "rollout.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": sid, "source": source or {"kind": "cli"}}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": count, "cached_input_tokens": 900000,
                                          "output_tokens": 800000},
                    "total_token_usage": {"input_tokens": 9999999}, "model_context_window": 1000000}}}]
        if not latest:
            rows.append({"type": "event_msg", "payload": {"type": "token_count", "info": {}}})
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_codex_latest_input_usage_bounded_head_tail_and_identity(self):
        path = self.codex_rollout(count=703000)
        data = path.read_bytes()
        # The identity is only in the bounded head; the relevant event is in the tail.
        path.write_bytes(data[:data.index(b"\n") + 1] + b"x" * 70000 + b"\n" + data[data.index(b"\n") + 1:])
        self.assertEqual(alert.codex_usage(path, "codex-session-id"), 703000)
        self.assertIsNone(alert.codex_usage(path, "other-session-id"))
        self.assertIsNone(alert.codex_usage(self.codex_rollout(source={"subagent": "reviewer"}),
                                            "codex-session-id"))
        self.assertIsNone(alert.codex_usage(self.codex_rollout(latest=False), "codex-session-id"))
        self.assertIsNone(alert.codex_usage(None, "codex-session-id"))
        link = self.temp / "rollout-link.jsonl"
        link.symlink_to(path)
        self.assertIsNone(alert.codex_usage(link, "codex-session-id"))

    def test_codex_incomplete_uses_context_baseline_not_cumulative_input(self):
        path = self.temp / "incomplete-rollout.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "codex-session-id", "source": {"kind": "cli"}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 12000, "total_tokens": 12250},
                "total_token_usage": {"input_tokens": 13200, "total_tokens": 13490},
                "model_context_window": 258400,
            }}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertEqual(alert.codex_usage(path, "codex-session-id"), 12000)

    def test_codex_first_incomplete_does_not_substitute_cumulative_input(self):
        path = self.temp / "first-incomplete-rollout.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "codex-session-id", "source": {"kind": "cli"}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 0, "total_tokens": 0},
                "total_token_usage": {"input_tokens": 1200, "total_tokens": 1240},
                "model_context_window": 258400,
            }}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertEqual(alert.codex_usage(path, "codex-session-id"), 0)

    def test_codex_notice_uses_shared_once_state_and_exact_rollout_only(self):
        path = self.codex_rollout(count=703000)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "codex-session-id",
                   "transcript_path": str(path)}
        with patch.dict(os.environ, {**self.env, "HOME": str(self.temp)}, clear=True):
            self.assertIn("지금", alert.codex_notice(payload))
            self.assertEqual(alert.codex_notice(payload), "")
            payload["session_id"] = "new-codex-session"
            self.assertEqual(alert.codex_notice(payload), "")
            payload["transcript_path"] = str(self.codex_rollout("new-codex-session", 500001))
            self.assertIn("700K 전에", alert.codex_notice(payload))
            self.assertEqual(alert.codex_notice({**payload, "hook_event_name": "SessionStart"}), "")
            self.assertEqual(alert.codex_notice({**payload, "session_id": ""}), "")
        with patch.dict(os.environ, {**self.env, "HOME": str(self.temp),
                                    "CODEX_HOME": str(self.temp / "missing-codex")}, clear=True):
            self.assertEqual(alert.codex_notice({"hook_event_name": "UserPromptSubmit",
                "session_id": "valid-session-id"}), "")

    def test_codex_lifecycle_places_only_alert_in_system_message_and_worker_is_quiet(self):
        import importlib.util
        hook_path = ROOT / "adapters/codex/hooks/userprompt-lifecycle.py"
        spec = importlib.util.spec_from_file_location("context_alert_codex_hook", hook_path)
        hook = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = hook
        spec.loader.exec_module(hook)
        transcript = self.codex_rollout("codex-main-session", 703000)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "codex-main-session",
                   "transcript_path": str(transcript), "prompt": "hello", "cwd": str(self.temp)}
        patches = [
            patch.object(hook, "load_payload", return_value=payload),
            patch.object(hook, "is_worker_session", return_value=False),
            patch.object(hook, "cwd", return_value=str(self.temp)),
            patch.object(hook, "interaction_session_id", return_value="codex-main-session"),
            patch.object(hook, "session_id", return_value="codex-main-session"),
            patch.object(hook, "launch_trigger"), patch.object(hook, "sd111_first_prompt_sweep"),
            patch.object(hook, "card_context", return_value=("existing card", None)),
            patch.object(hook, "native_queue_prompt_receipts", return_value=([], [])),
            patch.object(hook, "candidate_context", return_value=("existing candidate", None)),
            patch.object(hook, "run_preflight", return_value="existing briefing"),
            patch.object(hook, "token_budget_context", return_value=""),
            patch.object(hook, "peer_notice"), patch.object(hook, "retry_pending_peer"),
        ]
        with patch.dict(os.environ, {**self.env, "HOME": str(self.temp)}, clear=True):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
                    patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13]:
                from contextlib import redirect_stdout
                import io
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(hook.main(), 0)
                emitted = json.loads(stdout.getvalue())
                self.assertIn("지금", emitted["systemMessage"])
                self.assertIn(emitted["systemMessage"], emitted["hookSpecificOutput"]["additionalContext"])
                self.assertIn("existing card", emitted["hookSpecificOutput"]["additionalContext"])
                self.assertNotIn("existing briefing", emitted["systemMessage"])
                # Shared once-state survives a same-session compaction/next prompt.
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(hook.main(), 0)
                self.assertNotIn("systemMessage", json.loads(stdout.getvalue()))
            with patch.object(hook, "is_worker_session", return_value=True), \
                    patch.object(hook, "load_payload", return_value=payload):
                stdout = __import__("io").StringIO()
                with __import__("contextlib").redirect_stdout(stdout):
                    self.assertEqual(hook.main(), 0)
                self.assertEqual(stdout.getvalue(), "")

    def test_claude_prompt_hook_visible_and_model_context_are_identical(self):
        path = self.transcript(512000)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "sid",
                   "transcript_path": str(path), "cwd": str(self.temp)}
        def run(extra=None):
            return subprocess.run(["bash", str(ROOT / "hooks/session-card-inject.sh")],
                                  input=json.dumps(payload), text=True, capture_output=True,
                                  env={**self.env, **(extra or {})}, timeout=15)
        for extra in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_DEPTH": "1"}):
            self.assertEqual(run(extra).stdout, "")
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["systemMessage"], output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("decision", output)
        self.assertNotIn("continue", output)
        self.assertEqual(run().stdout, "")
        payload["agent_id"] = "native-child"
        payload["session_id"] = "native-child"
        self.assertEqual(run().stdout, "")
        payload.pop("agent_id")
        payload["session_id"] = "after-clear"
        payload["transcript_path"] = str(self.transcript(703000, "after-clear"))
        self.assertIn("지금", json.loads(run().stdout)["systemMessage"])
        payload["session_id"] = "without-transcript"
        payload.pop("transcript_path")
        self.assertEqual(run().stdout, "")

    def test_claude_session_start_does_not_claim_threshold(self):
        with patch.dict(os.environ, self.env, clear=True):
            payload = {"hook_event_name": "SessionStart", "session_id": "sid",
                       "transcript_path": str(self.transcript(703000))}
            self.assertEqual(alert.claude_notice(payload), "")
            payload["hook_event_name"] = "UserPromptSubmit"
            self.assertTrue(alert.claude_notice(payload))

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_opencode_actual_plugin_message_to_toast_and_system_transform(self):
        # Minimal fixture harness disables unrelated lifecycle processes while
        # retaining the real plugin, threshold helper and worker classifier.
        fixture = self.temp / "harness"
        (fixture / "core").mkdir(parents=True)
        (fixture / "core/CORE.md").touch()
        (fixture / "adapters/opencode/bin").mkdir(parents=True)
        preflight = fixture / "adapters/opencode/bin/preflight.sh"
        preflight.write_text("#!/bin/sh\nexit 0\n")
        preflight.chmod(0o755)
        (fixture / "utilities").mkdir()
        for name in ("context_alert.py", "session_tidy.py", "session_card_format.py"):
            shutil.copy(ROOT / "utilities" / name, fixture / "utilities" / name)
        (fixture / "capabilities").mkdir()
        shutil.copy(ROOT / "capabilities/session-tidy.md", fixture / "capabilities/session-tidy.md")
        plugin_path = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = r'''
import assert from "node:assert/strict"
const { AgentHarnessGuards } = await import(process.env.PLUGIN_PATH)
let count = 499999, child = false, bad = false, synthetic = false, calls = 0
const toasts = []
const client = {
  session: {
    get: async ({path}) => ({data: {id: path.id, ...(child ? {parentID: "parent"} : {})}}),
    messages: async ({path, query}) => {
      calls++; assert.equal(query.limit, 16)
      if (bad) throw new Error("DB unavailable")
      return {data: [
        {info: {id: "old", sessionID: path.id, role: "assistant", time: {created: 1}, tokens: {input: 800000}}},
        {info: {id: "last", sessionID: path.id, role: "assistant", time: {created: 2},
          tokens: {input: 10, cache: {read: count - 30, write: 20}, output: 999999}}},
        {info: {id: "wrong", sessionID: "other", role: "assistant", time: {created: 10}, tokens: {input: 800000}}},
        {info: {id: "summary", sessionID: path.id, role: "assistant", summary: true, time: {created: 20}, tokens: {input: 800000}}},
      ]}
    },
  },
  tui: {showToast: async ({body}) => { toasts.push(body); return {data: true} }},
}
const make = () => AgentHarnessGuards({directory: process.env.HOME, client})
let plugin = await make(), turn = 0
async function prompt(sid="ses_main") {
  await plugin["chat.message"]({sessionID:sid, messageID: "m"+(++turn)},
    {message: {sessionID:sid, agent: "build"}, parts:[{type:"text", text:"ok", synthetic}]})
  const output = {system:[]}
  await plugin["experimental.chat.system.transform"]({sessionID:sid, model:{}}, output)
  return output.system.filter(text => text.startsWith("컨텍스트 약"))
}
for (count of [499999,500000]) assert.deepEqual(await prompt(), [])
count=512000
let lines=await prompt()
assert.deepEqual(lines, ["컨텍스트 약 512K — 700K 전에 /session-tidy로 정리 권장"])
assert.equal(toasts.length, 1); assert.equal(toasts[0].message, lines[0]); assert.equal(toasts[0].variant,"warning")
let continued={system:[]}
await plugin["experimental.chat.system.transform"]({sessionID:"ses_main",model:{}},continued)
assert(continued.system.includes(lines[0])); assert.equal(toasts.length,1)
plugin.dispose(); plugin=await make() // state survives plugin/server restart
for (count of [512000,699999,700000]) assert.deepEqual(await prompt(), [])
count=703000; lines=await prompt()
assert.equal(lines.length,1); assert.match(lines[0],/지금/); assert.equal(toasts[1].message,lines[0])
assert.deepEqual(await prompt(),[])
count=512000; assert.equal((await prompt("ses_after_clear")).length,1)
child=true; const before=calls; assert.deepEqual(await prompt("ses_child"),[]); assert.equal(calls,before)
child=false; synthetic=true; assert.deepEqual(await prompt("ses_aux"),[]); synthetic=false
for (const [key,value] of [["AGENT_DISPATCH_DEPTH","1"],["AGENT_DISPATCH_DEPTH","2"],["AGENT_SESSION_ROLE","worker"],
  ["AGENT_SESSION_ROLE","owner"],["FLEET_TITLE_REFRESH","1"],["MEM_DISTILL","1"]]) {
  process.env[key]=value; const before=calls; assert.deepEqual(await prompt("ses_worker"),[])
  assert.equal(calls,before); delete process.env[key]
}
bad=true; assert.deepEqual(await prompt("ses_no_transcript"),[]); bad=false
client.tui.showToast=()=>{throw new Error("UI unavailable")}
count=703000; assert.equal((await prompt("ses_ui_failure")).length,1)
client.session.get=()=>new Promise(()=>{})
const started=Date.now(); assert.deepEqual(await prompt("ses_sdk_stall"),[])
assert(Date.now()-started<1500)
plugin.dispose()
console.log("OpenCode actual chat.message -> shared usage/state -> toast/system paths passed")
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script], text=True,
                                capture_output=True, timeout=30, env={**self.env,
                                "AGENT_HOME": str(fixture), "PLUGIN_PATH": str(plugin_path)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
