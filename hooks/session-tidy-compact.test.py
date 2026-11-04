#!/usr/bin/env python3
"""Compact hook contract checks with isolated state and a fake detached runner."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import session_tidy as st  # noqa: E402
import session_tidy_runner as runner  # noqa: E402
sys.path.append(str(Path(__file__).resolve().parents[1] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402

# Markers a dispatched test runner inherits; a main-session case must not see them.
WORKER_MARKERS = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                  "AGENT_DISPATCH_STAGE_AUTHORITY", "AGENT_DISPATCH_SUBSESSION_ID",
                  "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")
_NODE_STDIN_EOF = None


def node_stdin_reaches_eof() -> bool:
    """Whether Node's spawnSync ``input`` reaches the child as EOF here.

    Some tool sandboxes deliver the bytes but never close the child's stdin, so any
    helper that reads its payload to the end waits until the spawn timeout. The plugin
    fixtures below need a normal host to say anything about the plugin.
    """
    global _NODE_STDIN_EOF
    if _NODE_STDIN_EOF is None:
        probe = ('const r = require("node:child_process").spawnSync("cat", [], '
                 '{ input: "x", encoding: "utf8", timeout: 2000, killSignal: "SIGKILL" }); '
                 'process.stdout.write(r.status === 0 && r.stdout === "x" ? "eof" : "hang")')
        try:
            result = subprocess.run(["node", "-e", probe], text=True, capture_output=True, timeout=10)
            _NODE_STDIN_EOF = result.stdout == "eof"
        except (OSError, subprocess.SubprocessError):
            _NODE_STDIN_EOF = False
    return _NODE_STDIN_EOF


class CompactHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-tidy-compact-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.env = {"XDG_STATE_HOME": str(self.state), "FLEET_NOW_LANG": "Korean"}   # Korean card words
        envpatch = mock.patch.dict("os.environ", self.env)
        envpatch.start()
        self.addCleanup(envpatch.stop)
        for key in WORKER_MARKERS:
            os.environ.pop(key, None)
        self.seat = st.Seat("pane", "fixture-seat", pane="w1:p1", harness="claude", project_key="fixture")
        self.resolve = mock.patch.object(
            st, "resolve_seat",
            side_effect=lambda harness, _cwd=None, _env=None, sid=None, _source="", **_kwargs:
                st.session_seat(self.seat, harness, sid or ""))
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.enqueue = mock.patch.object(runner, "enqueue_item", return_value={"id": "fixture"})
        self.enqueue_mock = self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

    def session_seat(self, sid, harness="claude"):
        return st.session_seat(self.seat, harness, sid)

    def confirmed_handover(self, predecessor, successor, harness="claude"):
        seat = self.session_seat(successor, harness)
        now = st.now_epoch()
        with st.session_state_lock(seat):
            st.record_event(seat, harness, successor, "start", source="clear",
                            cwd=str(self.cwd), now=now)
            st._append_ledger_entry(seat, {"ts": now + 0.001, "harness": harness,
                                           "sid": successor, "event": "handover",
                                           "from": {"harness": harness, "sid": predecessor},
                                           "source": "clear"})



















    def test_worker_hooks_emit_nothing_and_never_queue(self):
        worker = {**self.env, "AGENT_SESSION_ROLE": "worker"}
        self.assertEqual(st.run_hook("claude", "compact", "sid-worker", phase="before", env=worker), "")
        self.assertEqual(st.run_hook("claude", "compact", "sid-worker", phase="after",
                                     compact_summary="summary", env=worker), "")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.enqueue_mock.call_count, 0)

    def test_runtime_bridge_before_contract_and_native_worker_exclusion(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "XDG_STATE_HOME": str(self.state),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        bridge = ROOT / "utilities/worker-state-hook.py"

        def invoke(harness, payload, **extra):
            call_env = {**env, **extra}
            return subprocess.run([sys.executable, str(bridge), "compact-before", "--harness", harness],
                                  input=json.dumps(payload), cwd=self.cwd, env=call_env,
                                  text=True, capture_output=True, timeout=8)

        for harness in ("claude", "codex", "opencode"):
            result = invoke(harness, {"session_id": "sid-bridge", "cwd": str(self.cwd)})
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = ""
            self.assertEqual(result.stdout.strip(), expected)
            worker = invoke(harness, {"session_id": "sid-worker", "cwd": str(self.cwd)},
                            AGENT_SESSION_ROLE="worker")
            self.assertEqual((worker.returncode, worker.stdout), (0, ""))
        for harness, payload in (("claude", {"session_id": "claude-child", "agent_id": "agent-1"}),
                                 ("codex", {"session_id": "codex-child", "agent_type": "subagent"})):
            result = invoke(harness, payload)
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_runtime_bridge_after_forwards_payload_and_keeps_a_bounded_booking_reason(self):
        fake_home = self.root / "fake-tidy-home"
        (fake_home / "utilities").mkdir(parents=True)
        capture = self.root / "tidy-calls.jsonl"
        (fake_home / "utilities/session_tidy.py").write_text(
            "import json,sys\n"
            "argv=sys.argv[1:]; body=sys.stdin.read()\n"
            f"open({str(capture)!r}, 'a').write(json.dumps({{'argv': argv, 'stdin': body}}) + '\\n')\n"
            "if '--phase' in argv and argv[argv.index('--phase')+1] == 'after' and json.loads(body or '{}').get('trigger') == 'manual':\n"
            " print('booking=skipped reason=trigger-manual')\n"
            "else: print('ignored after output')\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(fake_home), "XDG_STATE_HOME": str(self.state),
                    "PYTHONDONTWRITEBYTECODE": "1", "CODEX_THREAD_ID": "inherited-other-session"})
        bridge = ROOT / "utilities/worker-state-hook.py"
        payloads = {
            "claude": {"session_id": "sid-c", "cwd": str(self.cwd), "transcript_path": "c.jsonl",
                       "compact_summary": "claude summary"},
            "codex": {"session_id": "sid-x", "cwd": str(self.cwd), "transcript_path": "x.jsonl",
                      "trigger": "manual"},
            "opencode": {"sessionID": "sid-o", "cwd": str(self.cwd), "summary": "opencode summary"},
        }
        for harness, payload in payloads.items():
            result = subprocess.run([sys.executable, str(bridge), "compact-after", "--harness", harness],
                                    input=json.dumps(payload), env=env, text=True,
                                    capture_output=True, timeout=8)
            expected_status = "booking=skipped reason=trigger-manual\n" if harness == "codex" else ""
            self.assertEqual((result.returncode, result.stdout), (0, expected_status), result.stderr)
        calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)
        expected = {
            "claude": ("sid-c", "c.jsonl", "claude summary"),
            "codex": ("sid-x", "x.jsonl", ""),
            "opencode": ("sid-o", None, "opencode summary"),
        }
        for call in calls:
            argv = call["argv"]
            harness = argv[argv.index("--harness") + 1]
            sid, transcript, summary = expected[harness]
            self.assertEqual(argv[:6], ["hook", "--harness", harness, "--event", "compact", "--phase"])
            self.assertEqual(argv[6], "after")
            self.assertEqual(argv[argv.index("--session-id") + 1], sid)
            self.assertEqual(argv[argv.index("--cwd") + 1], str(self.cwd))
            if transcript:
                self.assertEqual(argv[argv.index("--transcript") + 1], transcript)
            else:
                self.assertNotIn("--transcript", argv)
            expected_input = {"summary": summary}
            if transcript:
                expected_input["transcript"] = transcript
            if harness == "codex":
                expected_input["trigger"] = "manual"
            self.assertEqual(json.loads(call["stdin"]), expected_input)

    def test_main_wrapper_discards_partial_output_and_fails_open_on_timeout(self):
        fake_home = self.root / "fake-home"
        utility_dir = fake_home / "utilities"
        utility_dir.mkdir(parents=True)
        helper = utility_dir / "worker-state-hook.py"
        helper.write_text(
            "import os,sys,time\n"
            "print('partial ledger output', flush=True)\n"
            "if sys.argv[1:2] == ['compact-after'] and os.environ.get('FAKE_COMPACT_MODE') == 'reason':\n"
            " print('booking=skipped reason=trigger-manual', flush=True); raise SystemExit(0)\n"
            "if os.environ.get('FAKE_COMPACT_MODE') == 'timeout': time.sleep(8)\n"
            "raise SystemExit(2 if os.environ.get('AGENT_DISPATCH_STAGE_AUTHORITY') == '0' else 1)\n",
            encoding="utf-8")
        wrapper = ROOT / "hooks/worker-state-compact.py"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(fake_home), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "HERDR_PANE_ID": ""})
        partial = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                                 env={**env, "FAKE_COMPACT_MODE": "partial"}, text=True,
                                 input="{}", capture_output=True, timeout=8)
        self.assertEqual((partial.returncode, partial.stdout), (0, ""))
        timed = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                               env={**env, "FAKE_COMPACT_MODE": "timeout"}, text=True,
                               input="{}", capture_output=True, timeout=8)
        self.assertEqual((timed.returncode, timed.stdout), (0, ""))
        reason = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", "claude"],
                                env={**env, "FAKE_COMPACT_MODE": "reason"}, text=True,
                                input="{}", capture_output=True, timeout=8)
        self.assertEqual((reason.returncode, reason.stdout), (0, ""))
        self.assertIn("booking=skipped reason=trigger-manual", reason.stderr)
        sub = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", "claude"],
                             env={**env, "AGENT_DISPATCH_STAGE_AUTHORITY": "0"}, text=True,
                             input="{}", capture_output=True, timeout=8)
        self.assertEqual(sub.returncode, 2)
        self.assertEqual(sub.stdout, "partial ledger output\n")

    def _opencode_fixture(self, scenario, extra_env=None):
        """Run the real plugin against a fake v1 client; a python3 shim records every helper call."""
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        if not node_stdin_reaches_eof():
            self.skipTest("node spawnSync input never reaches EOF in this sandbox; run on a normal host")
        fake_bin = self.root / f"bin-{scenario}"
        fake_bin.mkdir()
        calls = self.root / f"calls-{scenario}.jsonl"
        instructions = self.root / "instructions.txt"
        instructions.write_text(st.COMPACT_CARD_INSTRUCTIONS + "\n", encoding="utf-8")
        shim = fake_bin / "python3"
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -c '\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "name = os.path.basename(argv[0]) if argv else \"\"\n"
            "body = sys.stdin.read() if name == \"worker-state-hook.py\" else \"\"\n"
            f"open({json.dumps(str(calls))}, \"a\").write(json.dumps({{\"argv\": argv, \"stdin\": body}}) + \"\\n\")\n"
            "if name == \"worker-state-hook.py\":\n"
            "    if os.environ.get(\"FAKE_LEDGER_FAIL\") == \"1\":\n"
            "        sys.stderr.write(\"ledger missing\\n\"); raise SystemExit(2)\n"
            "    if argv[1] == \"compact-before\":\n"
            f"        sys.stdout.write(open({json.dumps(str(instructions))}).read())\n"
            "    elif argv[1] == \"compact-after\" and json.loads(body or \"{}\").get(\"trigger\") == \"auto\":\n"
            "        print(\"booking=scheduled\")\n"
            "    elif argv[1] == \"runtime-status\":\n"
            "        print(\"booking=pending\")\n"
            "' \"$@\"\n", encoding="utf-8")
        shim.chmod(0o700)
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = f"""import {{ AgentHarnessGuards }} from {json.dumps(plugin.as_uri())}
const scenario = {json.dumps(scenario)}
const sid = "ses_fixture"
const old = {{ info: {{ id: "old-summary", sessionID: sid, role: "assistant", summary: true, finish: "stop", time: {{ created: Date.now() - 5000 }} }}, parts: [{{ type: "text", text: "old" }}] }}
const compactParent = {{ info: {{ id: "compact-parent", sessionID: sid, role: "user", time: {{ created: Date.now() - 1 }} }},
  parts: [{{ id: "compact-part", type: "compaction", auto: true, tail_start_id: "retained-start" }}] }}
const fresh = {{ info: {{ id: "new-summary", sessionID: sid, role: "assistant", parentID: "compact-parent", summary: true, finish: "stop", time: {{ created: 0 }} }}, parts: [{{ type: "text", text: "fresh summary" }}, {{ type: "image", text: "ignored" }}] }}
const calls = []
const ctx = {{ directory: {json.dumps(str(self.cwd))}, worktree: {json.dumps(str(self.cwd))}, client: {{ session: {{
  get: async (request) => {{
    calls.push(["get", request.path.id])
    if (scenario === "unknown") throw new Error("lookup failed")
    return {{ data: {{ id: request.path.id, parentID: request.path.id === "ses_child" ? sid : null }} }}
  }},
  messages: async (request) => {{
    calls.push(["messages", request.path.id, request.query, Boolean(request.signal)])
    fresh.info.time.created = Date.now()
    return {{ data: [old, compactParent, fresh] }}
  }},
  ...(["status-idle", "status-missing"].includes(scenario) ? {{ status: async (request) => {{
    calls.push(["status", sid, request.query])
    if (scenario === "status-missing") return {{ data: {{}} }}
    return {{ data: {{ [sid]: {{ type: "idle" }} }} }}
  }} }} : {{}}),
}} }} }}
const plugin = await AgentHarnessGuards(ctx)
const result = {{ calls }}
const output = {{ context: [], prompt: "keep this prompt" }}
if (scenario === "status-missing") {{
  await plugin.event({{ event: {{ type: "session.status", properties: {{ sessionID: sid, status: {{ type: "idle" }} }} }} }})
  result.preBookingIdleEmitted = true
}}
try {{
  await plugin["experimental.session.compacting"]({{ sessionID: sid }}, output)
  result.before = "ok"
}} catch (error) {{ result.before = String(error.message || error) }}
result.output = output
try {{
  await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: sid }} }} }})
  result.after = "ok"
}} catch (error) {{ result.after = String(error.message || error) }}
if (scenario === "main") {{
  result.childOutput = {{ context: [], prompt: "child prompt" }}
  await plugin["experimental.session.compacting"]({{ sessionID: "ses_child" }}, result.childOutput)
  await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: "ses_child" }} }} }})
}}
console.log(JSON.stringify(result))
"""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "MEM_STORE": str(self.root / "mem"),
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")})
        env.update(extra_env or {})
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=self.cwd,
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] \
            if calls.exists() else []
        hooks = [(Path(c["argv"][0]).name, c["argv"][1:], c["stdin"]) for c in recorded if c["argv"]]
        return json.loads(result.stdout.strip().splitlines()[-1]), hooks





    def test_opencode_sub_session_ledger_failure_still_fails_closed(self):
        data, hooks = self._opencode_fixture(
            "subsession", {"AGENT_DISPATCH_STAGE_AUTHORITY": "0", "FAKE_LEDGER_FAIL": "1"})
        self.assertIn("ledger missing", data["before"])
        self.assertIn("ledger missing", data["after"])
        self.assertEqual(data["output"]["context"], [])
        self.assertEqual(data["calls"], [])
        self.assertEqual([h[1] for h in hooks if h[0] == "worker-state-hook.py"],
                         [["compact-before"], ["compact-after"]])

    def test_completed_idle_notice_is_native_output_once_with_no_model_context(self):
        for harness, wrapper in (("claude", ROOT / "hooks/worker-state-compact.py"),
                                  ("codex", ROOT / "adapters/codex/hooks/worker-state-compact.py")):
            sid = "notice-" + harness
            seat = self.session_seat(sid, harness)
            st.write_notice(seat, "Idle tidy: card saved, conversation compacted",
                            author_harness=harness, author_sid=sid, notice_id="idle:fixture")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "HERDR_"))}
            env.update({"AGENT_HOME": str(ROOT), "XDG_STATE_HOME": str(self.state),
                        "XDG_CONFIG_HOME": str(self.root / "config")})
            payload = json.dumps({"session_id": sid, "cwd": str(self.cwd)})
            result = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", harness],
                                    input=payload, text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = json.loads(result.stdout)
            self.assertEqual(rendered, {"systemMessage":"Idle tidy: card saved, conversation compacted"})
            again = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", harness],
                                   input=payload, text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(again.stdout, "")

    def test_natural_compaction_is_ledger_only_for_every_adapter(self):
        import session_tidy_clear as clear
        with mock.patch.object(clear, "_start_helper") as helper:
            for harness in ("claude", "codex", "opencode"):
                sid = "private-" + harness
                self.assertEqual(st.run_hook(harness, "compact", sid, phase="before", cwd=str(self.cwd)), "")
                self.assertEqual(st.run_hook(harness, "compact", sid, phase="after", cwd=str(self.cwd),
                                            compact_summary="runtime summary",
                                            compact_context={"trigger":"auto"}), "")
                seat = self.session_seat(sid, harness)
                self.assertIsNone(st.read_latest_card(seat))
                self.assertIsNone(clear.read_reservation(seat.key))
                self.assertTrue(any(r["event"] == "compact" for r in st._read_ledger_lines(seat)))
        self.enqueue_mock.assert_not_called()
        helper.assert_not_called()

    def test_opencode_preserves_native_prompt_and_observes_without_a_second_summary_read(self):
        data, hooks = self._opencode_fixture("main")
        self.assertEqual(data["output"], {"context": [], "prompt": "keep this prompt"})
        self.assertFalse(any(c[0] == "messages" for c in data["calls"]))
        calls = [h for h in hooks if h[0] == "worker-state-hook.py"]
        self.assertEqual([h[1] for h in calls], [["compact-after", "--harness", "opencode"]])
        payload = json.loads(calls[0][2])
        self.assertEqual(payload["sessionID"], "ses_fixture")
        self.assertNotIn("summary", payload)
        self.assertEqual(data["childOutput"], {"context": [], "prompt": "child prompt"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
