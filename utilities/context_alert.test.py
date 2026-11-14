"""Threshold state plus the actual Claude and OpenCode prompt entry points."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
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

    URGENT = ("입력 문맥 약 703K — 하던 일 하나를 넘긴 뒤 /session-tidy 기본 정리"
              "('정리만'은 문맥을 줄이지 않음) · 자동 압축 여유 보장 아님")

    def test_strict_boundaries_and_once_while_the_session_stays_above(self):
        for count in (499999, 500000):
            self.assertEqual(self.notice(count), "")
        self.assertIn("작업 상태 저장 권장", self.notice(500001))
        self.assertEqual(self.notice(600000), "")
        self.assertEqual(self.notice(699999), "")
        self.assertEqual(self.notice(700000), "")
        self.assertIn("기본 정리", self.notice(700001))
        # Zero is no observation; a dip that stays above 500K is not a compaction.
        for count in (0, 500001, 800000):
            self.assertEqual(self.notice(count), "")

    def test_a_small_dip_under_500k_is_not_a_compaction(self):
        self.assertIn("작업 상태 저장 권장", self.notice(505000))
        record = next(self.state.glob("*.json"))
        for count in (496000, 503000, 400001, 503000):
            self.assertEqual(self.notice(count), "")
            self.assertEqual(json.loads(record.read_text()), [500000])
        self.assertEqual(self.notice(400000), "")       # this small: compacted
        self.assertFalse(record.exists())
        self.assertIn("작업 상태 저장 권장", self.notice(503000))

    def test_compaction_well_below_500k_lets_each_threshold_speak_once_more(self):
        self.assertEqual(self.notice(703000), self.URGENT)
        self.assertEqual(self.notice(720000), "")
        self.assertEqual(self.notice(400000), "")       # the session's own compaction
        self.assertEqual(self.notice(30000), "")
        self.assertIn("작업 상태 저장 권장", self.notice(512000))
        self.assertEqual(self.notice(530000), "")
        self.assertEqual(self.notice(703000), self.URGENT)
        self.assertEqual(self.notice(710000), "")
        self.assertEqual(self.notice(40000), "")        # compacted again: a jump is one urgent line
        self.assertEqual(self.notice(703000), self.URGENT)
        self.assertEqual(self.notice(512000), "")

    def test_rearm_keeps_corrupt_state_and_excluded_sessions_silent(self):
        self.assertTrue(self.notice(703000))
        record = next(self.state.glob("*.json"))
        self.assertEqual(self.notice(30000, env={"AGENT_SESSION_ROLE": "owner"}), "")
        self.assertEqual(json.loads(record.read_text()), [500000, 700000])
        self.assertEqual(self.notice(703000), "")
        record.write_text("not json")
        for count in (30000, 703000):
            self.assertEqual(self.notice(count), "")
        self.assertEqual(record.read_text(), "not json")
        # Below both with no record touches no state at all.
        self.assertEqual(self.notice(30000, sid="fresh"), "")
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)

    def test_jump_emits_only_urgent_line_and_native_new_session_resets(self):
        self.assertEqual(self.notice(703000), self.URGENT)
        self.assertEqual(self.notice(512000), "")
        self.assertEqual(self.notice(512000, sid="after-clear"),
                         "입력 문맥 약 512K — /session-tidy로 작업 상태 저장 권장 · 자동 압축 여유 보장 아님")
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

    def test_codex_notice_reuses_pressure_only_for_a_fresh_preservation_notice(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        import fleet.token_budget as budget
        path=self.codex_rollout(sid='codex-session-id',count=539596)
        pressure={'state':'observed','token_limit_reached':True,'usage_at':1000.5,
                  'observed_at':1000.501,'fresh_until':1900.5,'scope_tokens':799116,
                  'limit_tokens':784800,'scope':'Total'}
        with patch.dict(os.environ,self.env,clear=True), \
             patch.object(budget,'codex_compaction_pressure',return_value=pressure) as read, \
             patch.object(budget.time,'time',return_value=1001):
            payload={'hook_event_name':'UserPromptSubmit','session_id':'codex-session-id',
                     'transcript_path':str(path)}
            line=alert.codex_notice(payload)
            self.assertIn('자동 압축 여유 보장 아님',line)
            self.assertIn('자동 압축 임박(native 추정)',line)
            self.assertNotIn('700K 전에',line)
            self.assertEqual(alert.codex_notice(payload),'')
            self.assertEqual(read.call_count,1)
            self.assertEqual(read.call_args.args[:2],('codex-session-id',str(path)))

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
            self.assertIn("기본 정리", alert.codex_notice(payload))
            self.assertEqual(alert.codex_notice(payload), "")
            payload["session_id"] = "new-codex-session"
            self.assertEqual(alert.codex_notice(payload), "")
            payload["transcript_path"] = str(self.codex_rollout("new-codex-session", 500001))
            self.assertIn("작업 상태 저장 권장", alert.codex_notice(payload))
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
                self.assertIn("기본 정리", emitted["systemMessage"])
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
        self.assertIn("기본 정리", json.loads(run().stdout)["systemMessage"])
        payload["session_id"] = "without-transcript"
        payload.pop("transcript_path")
        self.assertEqual(run().stdout, "")

    def test_claude_prompt_hook_speaks_again_after_its_compact_boundary(self):
        path = self.transcript(703000)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "sid",
                   "transcript_path": str(path), "cwd": str(self.temp)}

        def append(row):
            with path.open("a") as handle:
                handle.write(json.dumps(row) + "\n")

        def run():
            result = subprocess.run(["bash", str(ROOT / "hooks/session-card-inject.sh")],
                                    input=json.dumps(payload), text=True, capture_output=True,
                                    env=self.env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["systemMessage"] if result.stdout.strip() else ""

        def usage(count):
            return {"type": "assistant", "sessionId": "sid", "message": {"usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": count - 30}}}

        self.assertIn("기본 정리", run())
        self.assertEqual(run(), "")
        append({"type": "system", "subtype": "compact_boundary", "sessionId": "sid"})
        self.assertEqual(run(), "")      # no request in the compacted context yet
        append(usage(30000))
        self.assertEqual(run(), "")
        append(usage(512000))
        self.assertIn("작업 상태 저장 권장", run())
        append(usage(703000))
        self.assertIn("기본 정리", run())
        self.assertEqual(run(), "")

    def test_codex_notice_speaks_again_after_its_compacted_row(self):
        path = self.codex_rollout(count=703000)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "codex-session-id",
                   "transcript_path": str(path)}

        def append(row):
            with path.open("a") as handle:
                handle.write(json.dumps(row) + "\n")

        def count(value):
            return {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": value}}}}

        with patch.dict(os.environ, {**self.env, "HOME": str(self.temp)}, clear=True):
            self.assertIn("기본 정리", alert.codex_notice(payload))
            append({"type": "compacted", "payload": {"message": "summary"}})
            self.assertEqual(alert.codex_notice(payload), "")
            append(count(0))            # Codex's empty count after compaction observes nothing
            self.assertEqual(alert.codex_notice(payload), "")
            append(count(30000))
            self.assertEqual(alert.codex_notice(payload), "")
            append(count(512000))
            self.assertIn("작업 상태 저장 권장", alert.codex_notice(payload))
            append(count(720000))
            self.assertIn("기본 정리", alert.codex_notice(payload))
            self.assertEqual(alert.codex_notice(payload), "")

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
  return output.system.filter(text => text.startsWith("입력 문맥 약"))
}
for (count of [499999,500000]) assert.deepEqual(await prompt(), [])
count=512000
let lines=await prompt()
assert.deepEqual(lines, ["입력 문맥 약 512K — /session-tidy로 작업 상태 저장 권장 · 자동 압축 여유 보장 아님"])
assert.equal(toasts.length, 1); assert.equal(toasts[0].message, lines[0]); assert.equal(toasts[0].variant,"warning")
let continued={system:[]}
await plugin["experimental.chat.system.transform"]({sessionID:"ses_main",model:{}},continued)
assert(continued.system.includes(lines[0])); assert.equal(toasts.length,1)
plugin.dispose(); plugin=await make() // state survives plugin/server restart
for (count of [512000,699999,700000]) assert.deepEqual(await prompt(), [])
count=703000; lines=await prompt()
assert.equal(lines.length,1); assert.match(lines[0],/기본 정리/); assert.equal(toasts[1].message,lines[0])
assert.deepEqual(await prompt(),[])
// The session's own compaction brings its latest request back under 500K: both speak once more.
count=30000; assert.deepEqual(await prompt(),[])
count=512000; lines=await prompt()
assert.deepEqual(lines, ["입력 문맥 약 512K — /session-tidy로 작업 상태 저장 권장 · 자동 압축 여유 보장 아님"])
count=703000; lines=await prompt(); assert.equal(lines.length,1); assert.match(lines[0],/'정리만'은 문맥을 줄이지 않음/)
assert.deepEqual(await prompt(),[]); assert.equal(toasts.length,4)
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

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_opencode_token_budget_reads_v1_and_v2_message_apis(self):
        # PG-17: the plugin passes the latest request's tokens and that model's
        # limit.context to the shared token-budget CLI; the directive rides the
        # same turn's system context. V2 rows come from session.context.
        fixture = self.temp / "harness"
        (fixture / "core").mkdir(parents=True)
        (fixture / "core/CORE.md").touch()
        (fixture / "adapters/opencode/bin").mkdir(parents=True)
        preflight = fixture / "adapters/opencode/bin/preflight.sh"
        preflight.write_text("#!/bin/sh\nexit 0\n")
        preflight.chmod(0o755)
        (fixture / "utilities").mkdir()
        for name in ("context_alert.py", "token-budget.py", "session_tidy.py",
                     "session_card_format.py"):
            shutil.copy(ROOT / "utilities" / name, fixture / "utilities" / name)
        (fixture / "tools/fleet").mkdir(parents=True)
        for name in ("__init__.py", "token_budget.py", "token_accounting.py"):
            shutil.copy(ROOT / "tools/fleet" / name, fixture / "tools/fleet" / name)
        (fixture / "capabilities").mkdir()
        shutil.copy(ROOT / "capabilities/session-tidy.md", fixture / "capabilities/session-tidy.md")
        plugin_path = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = r'''
import assert from "node:assert/strict"
const { AgentHarnessGuards } = await import(process.env.PLUGIN_PATH)
let used = 1000, model = "fixture-model"
const row = () => ({id: "a" + used, sessionID: "ses_v1", role: "assistant", time: {created: used},
  providerID: "fixture", modelID: model, tokens: {input: used, output: 1, cache: {read: 0, write: 0}}})
const client = {session: {get: async ({path}) => ({data: {id: path.id}}),
  messages: async ({path}) => ({data: [{info: {...row(), sessionID: path.id}},
    {info: {id: "u", sessionID: path.id, role: "user", time: {created: 1}}}]})}, tui: {}}
// Each prompt reads the previous request; `turns` is what each request then reports.
async function run(plugin, sid, turns) {
  const seen = []
  for (const [index, value] of turns.entries()) {
    await plugin["chat.message"]({sessionID: sid, messageID: sid + index},
      {message: {sessionID: sid, agent: "build"}, parts: [{type: "text", text: "go"}]})
    const output = {system: []}
    await plugin["experimental.chat.system.transform"]({sessionID: sid}, output)
    seen.push((output.system.find(text => text.startsWith("TOKEN_BUDGET=")) || "").split(":")[0])
    used = value
    await plugin["chat.params"]({sessionID: sid, model: {providerID: "fixture", id: model,
      limit: {context: 200000, output: 8000}}}, {})
  }
  return seen
}
const v1 = await AgentHarnessGuards({directory: process.env.HOME, client})
assert.deepEqual(await run(v1, "ses_v1", [150000, 150000, 175000, 175000, 1]),
  ["", "TOKEN_BUDGET=tight", "", "TOKEN_BUDGET=critical", ""])
// A window seen for another model is never borrowed.
used = 190000; model = "other"
await v1["chat.params"]({sessionID: "ses_mismatch", model: {providerID: "fixture", id: "fixture-model",
  limit: {context: 200000}}}, {})
await v1["chat.message"]({sessionID: "ses_mismatch", messageID: "x"},
  {message: {sessionID: "ses_mismatch", agent: "build"}, parts: [{type: "text", text: "go"}]})
const mismatch = {system: []}
await v1["experimental.chat.system.transform"]({sessionID: "ses_mismatch"}, mismatch)
assert(!mismatch.system.some(text => text.startsWith("TOKEN_BUDGET=")))
v1.dispose()
// V2: the legacy client wrapper returns user rows only; assistant usage is session.context.
used = 1000; model = "fixture-model"
const v2 = await AgentHarnessGuards({directory: process.env.HOME, nativeHost: "v2",
  client: {session: {get: client.session.get, messages: async () => ({data: []})}, tui: {}},
  session: {context: async ({sessionID}) => ({data: [{id: "u", type: "user", text: "go"},
    {id: "a" + used, type: "assistant", time: {created: used}, model: {providerID: "fixture", id: model},
     tokens: {input: used, output: 1, reasoning: 0, cache: {read: 0, write: 0}}}]})}},
  {initialize: false})
assert.deepEqual(await run(v2, "ses_v2", [150000, 150000, 175000, 175000, 1]),
  ["", "TOKEN_BUDGET=tight", "", "TOKEN_BUDGET=critical", ""])
v2.dispose()
console.log("OpenCode token budget V1 messages / V2 session.context paths passed")
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script], text=True,
                                capture_output=True, timeout=60, env={**self.env,
                                "PYTHONDONTWRITEBYTECODE": "1",
                                "AGENT_HOME": str(fixture), "PLUGIN_PATH": str(plugin_path)})
        # The plugin's detached turn helpers may still be writing under HOME.
        last, deadline = None, time.monotonic() + 5
        while time.monotonic() < deadline:
            now = sorted(str(path) for path in self.temp.rglob("*"))
            if now == last:
                break
            last = now
            time.sleep(0.5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_opencode_token_budget_never_borrows_an_earlier_usage_or_window(self):
        # RV-PR1506 P2-1: the latest assistant is chosen first and only its usage
        # counts. A latest response without usage (OpenCode 2.0.32 allows absent
        # `tokens`) or with a non-object one is unknown: zero bytes, never the
        # earlier 160K. A request whose model is unknown clears the remembered window.
        fixture = self.temp / "harness"
        (fixture / "core").mkdir(parents=True)
        (fixture / "core/CORE.md").touch()
        (fixture / "adapters/opencode/bin").mkdir(parents=True)
        preflight = fixture / "adapters/opencode/bin/preflight.sh"
        preflight.write_text("#!/bin/sh\nexit 0\n")
        preflight.chmod(0o755)
        (fixture / "utilities").mkdir()
        for name in ("context_alert.py", "token-budget.py", "session_tidy.py",
                     "session_card_format.py"):
            shutil.copy(ROOT / "utilities" / name, fixture / "utilities" / name)
        (fixture / "tools/fleet").mkdir(parents=True)
        for name in ("__init__.py", "token_budget.py", "token_accounting.py"):
            shutil.copy(ROOT / "tools/fleet" / name, fixture / "tools/fleet" / name)
        (fixture / "capabilities").mkdir()
        shutil.copy(ROOT / "capabilities/session-tidy.md", fixture / "capabilities/session-tidy.md")
        plugin_path = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = r"""
import assert from "node:assert/strict"
const { AgentHarnessGuards } = await import(process.env.PLUGIN_PATH)
const results = {}
for (const version of ["v1", "v2"]) for (const mode of ["latest-high", "latest-missing",
    "latest-invalid", "window-cleared"]) {
  const sid = `ses_${version}_${mode.replaceAll("-", "")}`
  const old = {id: "old", sessionID: sid, role: "assistant", type: "assistant", time: {created: 1},
    providerID: "fixture", modelID: "m", model: {providerID: "fixture", id: "m"},
    tokens: {input: 160000, output: 0, cache: {read: 0, write: 0}}}
  const latest = {...old, id: "latest", time: {created: 2}}
  if (mode === "latest-missing") delete latest.tokens
  if (mode === "latest-invalid") latest.tokens = "invalid"
  const client = {session: {get: async ({path}) => ({data: {id: path.id}}),
    messages: async () => ({data: [{info: latest}, {info: old}]})}, tui: {}}
  const ctx = {directory: process.env.HOME, client, ...(version === "v2"
    ? {nativeHost: "v2", session: {context: async () => ({data: [old, latest]})}} : {})}
  const plugin = await AgentHarnessGuards(ctx, {initialize: false})
  await plugin["chat.params"]({sessionID: sid, model: {providerID: "fixture", id: "m",
    limit: {context: 200000}}}, {})
  // The latest request's model is unknown: the earlier window must not survive it.
  if (mode === "window-cleared") await plugin["chat.params"]({sessionID: sid, model: {}}, {})
  await plugin["chat.message"]({sessionID: sid, messageID: sid + "-turn"},
    {message: {sessionID: sid, agent: "build"}, parts: [{type: "text", text: "go"}]})
  const output = {system: []}
  await plugin["experimental.chat.system.transform"]({sessionID: sid}, output)
  results[`${version} ${mode}`] = output.system.filter(text => text.startsWith("TOKEN_BUDGET="))
    .map(text => text.split(":")[0])
  plugin.dispose()
}
assert.deepEqual(results, {
  "v1 latest-high": ["TOKEN_BUDGET=tight"], "v1 latest-missing": [], "v1 latest-invalid": [],
  "v1 window-cleared": [],
  "v2 latest-high": ["TOKEN_BUDGET=tight"], "v2 latest-missing": [], "v2 latest-invalid": [],
  "v2 window-cleared": []})
console.log("OpenCode token budget reads only the latest usage and window (V1/V2)")
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], text=True,
                                capture_output=True, timeout=60, env={**self.env,
                                "PYTHONDONTWRITEBYTECODE": "1",
                                "AGENT_HOME": str(fixture), "PLUGIN_PATH": str(plugin_path)})
        # The plugin's detached turn helpers may still be writing under HOME.
        last, deadline = None, time.monotonic() + 5
        while time.monotonic() < deadline:
            now = sorted(str(path) for path in self.temp.rglob("*"))
            if now == last:
                break
            last = now
            time.sleep(0.5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
