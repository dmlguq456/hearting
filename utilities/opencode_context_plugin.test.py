from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "node"
_TOOLS_DIR = str(ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from fleet import projection, render
from fleet.collectors import dispatch as fleet_dispatch
from fleet.model import DispatchJob
import fixture_processes


def fixture_tempdir(stack, *, prefix):
    raw = stack.enter_context(tempfile.TemporaryDirectory(prefix=prefix))
    stack.callback(fixture_processes.reap, raw)
    return raw


class OpenCodeContextPluginTest(unittest.TestCase):
    def test_first_prompt_owns_card_and_evidence_and_auxiliary_calls_only_replay(self):
        with tempfile.TemporaryDirectory(prefix="opencode-first-prompt-") as raw:
            script = '''import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
const calls = [];
childProcess.spawnSync = (command, args) => {
  calls.push(args);
  if (args.includes("local-evidence")) return { status: 0, stdout: "same local evidence" };
  if (args.includes("--event") && args.includes("prompt"))
    return { status: 0, stdout: JSON.stringify({additionalContext: "same card", deferredConsumed: []}) };
  return { status: 0, stdout: "", stderr: "" };
};
childProcess.spawn = () => ({on() {}, unref() {}, stdin: {end() {}}});
syncBuiltinESMExports();
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH));
const hooks = await AgentHarnessGuards({directory: process.env.HOME, worktree: process.env.HOME,
  client: {}}, {initialize: false});
const sid = "first-prompt-only";
const probes = () => calls.filter(a => a.includes("local-evidence")).length;
const cards = () => calls.filter(a => a.includes("--event") && !a.includes("--human-only"));
await hooks.event({event: {type: "session.created", properties: {info: {id: sid}}}});
const title = {system: []};
await hooks["experimental.chat.system.transform"]({sessionID: sid, model: {}}, title);
assert.equal(probes(), 0);
assert.equal(cards().length, 0);
const turn = id => hooks["chat.message"]({sessionID: sid, messageID: id},
  {parts: [{type: "text", text: "hi"}]});
await turn("m1");
assert.equal(probes(), 1);
assert.equal(cards().length, 1);
for (let i = 0; i < 2; i++) {
  const output = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID: sid, model: {}}, output);
  assert.ok(output.system.includes("same local evidence"));
  assert.ok(output.system.includes("same card"));
}
await turn("m2");
assert.equal(probes(), 1);
assert.ok(cards().every(a => a[a.indexOf("--event") + 1] === "prompt"));
hooks.dispose?.({silent: true});
'''
            env = {**os.environ, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "HOME": raw, "XDG_CONFIG_HOME": str(Path(raw) / ".config"),
                   "XDG_DATA_HOME": str(Path(raw) / ".data"), "XDG_STATE_HOME": str(Path(raw) / ".state"),
                   "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "", "AGENT_DISPATCH_CHILD": "",
                   "AGENT_DISPATCH_DEPTH": "", "OPENCODE_DISPATCH_SLUG": "", "FLEET_TITLE_REFRESH": "",
                   "HERDR_PANE_ID": ""}
            result = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                    env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_completion_uses_the_existing_opencode_prompt_and_history_path(self):
        if not NODE:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory(prefix="opencode-runtime-origin-") as raw:
            script = f'''import assert from "node:assert/strict";
import childProcess from "node:child_process";
import {{ syncBuiltinESMExports }} from "node:module";
import {{ pathToFileURL }} from "node:url";
const spawnSyncCalls = [], spawnCalls = [];
childProcess.spawnSync = (command, args, options = {{}}) => {{
  spawnSyncCalls.push({{ command, args, options }});
  const isPeerPending = String(args?.[0] || "").endsWith("peer-message.py") && args?.[1] === "pending";
  return {{ status: 0, stdout: isPeerPending ? "[]" : "", stderr: "" }};
}};
childProcess.spawn = (command, args, options = {{}}) => ({{
  on() {{}}, unref() {{}}, stdin: {{ end(body) {{ spawnCalls.push({{ command, args, options, body }}); }} }}
}});
syncBuiltinESMExports();
const {{ AgentHarnessGuards }} = await import(pathToFileURL(process.env.PLUGIN_PATH));
const hooks = await AgentHarnessGuards({{ directory: process.env.HOME,
  worktree: process.env.HOME, client: {{}} }}, {{ initialize: false }});
const prompt = "[notice] task complete\\n\\n(runtime-from: runtime [?] Hearting runtime ; ref=" + "a".repeat(32) + ")";
await hooks["chat.message"]({{ sessionID: "opencode-runtime", messageID: "native-runtime-message" }},
  {{ parts: [{{ type: "text", text: prompt }}] }});
const receive = spawnCalls.find((call) => call.args[0].endsWith("peer-message.py")
  && call.args[1] === "receive");
assert.ok(receive, "runtime-from is accepted by the OpenCode receiver prefilter");
assert.equal(receive.body, prompt);
const guidance = spawnSyncCalls.find((call) => call.args[0] === "-c"
  && call.args[1].includes("receiver_guidance"));
assert.ok(guidance, "the common reader checks the runtime envelope on this prompt");
assert.equal(JSON.parse(guidance.options.input).prompt, prompt);
assert.equal(guidance.options.input.includes("peer-from"), false);
hooks.dispose?.({{ silent: true }});
'''
            env = {**os.environ, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "HOME": raw, "XDG_CONFIG_HOME": str(Path(raw) / ".config"),
                   "XDG_DATA_HOME": str(Path(raw) / ".data"), "XDG_STATE_HOME": str(Path(raw) / ".state"),
                   "AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                   "AGENT_SESSION_ROLE": "", "AGENT_DISPATCH_CHILD": "",
                   "AGENT_DISPATCH_DEPTH": "", "OPENCODE_DISPATCH_SLUG": "",
                   "FLEET_TITLE_REFRESH": "", "HERDR_PANE_ID": ""}
            result = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                    env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_idle_observer_is_detached_before_summary_checkpoint_and_peer_consumers(self):
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        helper_start = source.index("function spawnIdleObserver(")
        helper_end = source.index("\n}", helper_start) + 2
        helper = source[helper_start:helper_end]
        event_start = source.index('if (event && event.type === "session.idle")')
        event_end = source.index('if (event && event.type === "session.deleted")', event_start)
        idle = source[event_start:event_end]

        self.assertIn('spawn("python3"', helper)
        self.assertIn("detached: true", helper)
        self.assertIn("child.stdin.end", helper)
        self.assertIn("child.unref()", helper)
        self.assertIn("isWorkerSession()", helper)
        self.assertNotIn("spawnSync", idle)
        self.assertIn("spawnIdleObserver(eventSid, baseDir(ctx))", idle)
        self.assertLess(idle.index("spawnIdleObserver"), idle.index('spawnSummary(eventSid, "final")'))
        self.assertLess(idle.index('spawnSummary(eventSid, "final")'), idle.index("spawnCheckpoint(eventSid)"))
        self.assertIn("pendingPeerDelivery(ctx, eventSid)", idle)
        self.assertIn("completionCarrier.idle(eventSid).catch", idle)

    def test_held_idle_observer_does_not_hold_the_opencode_event_consumer(self):
        with ExitStack() as stack:
            raw = fixture_tempdir(stack, prefix="opencode-held-idle-")
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            observer_started = root / "observer-started"
            observer_pid = root / "observer.pid"
            summary_started = root / "summary-started"
            checkpoint_started = root / "checkpoint-started"
            release = root / "release"
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *session_tidy_idle.py*) echo $$ > \"$OBSERVER_PID_FILE\"; touch \"$OBSERVER_STARTED\"; "
                "while [ ! -e \"$OBSERVER_RELEASE\" ]; do /bin/sleep 0.02; done ;;\n"
                "  *session_summary_trigger.py*) touch \"$SUMMARY_STARTED\" ;;\n"
                "  *artifact_checkpoint_trigger.py*) touch \"$CHECKPOINT_STARTED\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps(plugin.as_uri())};
const hooks = await AgentHarnessGuards({{ directory: process.cwd(), worktree: process.cwd(), client: {{}} }});
const started = performance.now();
await hooks.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "sid-held-observer" }} }} }});
console.log(JSON.stringify({{ elapsed_ms: performance.now() - started }}));
'''
            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "PYTHONPYCACHEPREFIX": str(root / "cache" / "pycache"),
                "AGENT_HOME": str(ROOT),
                "OPENCODE_VERSION": "1.19.0",
                "OBSERVER_PID_FILE": str(observer_pid),
                "OBSERVER_STARTED": str(observer_started),
                "SUMMARY_STARTED": str(summary_started),
                "CHECKPOINT_STARTED": str(checkpoint_started),
                "OBSERVER_RELEASE": str(release),
            }
            try:
                run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                     env=env, text=True, capture_output=True, timeout=2)
                self.assertEqual(run.returncode, 0, run.stderr)
                deadline = time.monotonic() + 0.75
                while time.monotonic() < deadline and not (observer_started.exists() and summary_started.exists()):
                    time.sleep(0.01)
                self.assertTrue(observer_started.exists(), "observer child did not start")
                self.assertTrue(summary_started.exists(), "summary waited for the held observer")
                self.assertFalse(release.exists(), "fixture observer unexpectedly exited before release")
            finally:
                release.touch()
                if observer_pid.exists():
                    pid = int(observer_pid.read_text(encoding="utf-8").strip())
                    deadline = time.monotonic() + 0.75
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.01)

    def test_real_plugin_config_hook_caps_only_loaded_provider_and_hides_auth_values(self):
        with ExitStack() as stack:
            raw = fixture_tempdir(stack, prefix="opencode-context-hook-")
            home = Path(raw)
            xconfig, xdata, xstate, xcache = [home / name for name in ("config", "data", "state", "cache")]
            runtime = xconfig / "opencode"
            (runtime / "agent-config").mkdir(parents=True)
            shutil.copyfile(ROOT / "adapters/opencode/config/models.conf", runtime / "agent-config/models.conf")
            (runtime / "agent-config/models.conf").write_text(
                (runtime / "agent-config/models.conf").read_text().replace(
                    "CFG_MODEL_CONTEXT_WINDOW=800000", "CFG_MODEL_CONTEXT_WINDOW=600000"))
            (xdata / "opencode").mkdir(parents=True)
            (xdata / "opencode/auth.json").write_text(json.dumps({"openai": {"secret": "PRIVATE_AUTH_VALUE"}}))
            (xstate / "opencode").mkdir(parents=True)
            (xstate / "opencode/model.json").write_text(json.dumps({"recent": [
                {"providerID": "recent-provider", "modelID": "second"}]}))
            (xcache / "opencode").mkdir(parents=True)
            catalog = {
                "openai": {"env": ["OPENAI_API_KEY"], "models": {
                    "gpt": {"limit": {"context": 1000000, "input": 922000, "output": 8192}},
                    "second": {"limit": {"context": 900000, "input": 850000, "output": 7000}},
                    "small": {"limit": {"context": 300000, "input": 250000, "output": 4096}}}},
                "provider-without-input": {"models": {"tiny": {
                    "limit": {"context": 500000, "output": 4096}}}},
                "recent-provider": {"models": {"second": {
                    "limit": {"context": 900000, "input": 850000, "output": 7000}}}},
                "unloaded-catalog-only": {"models": {"ghost": {
                    "limit": {"context": 1000000, "input": 900000, "output": 8192}}}},
            }
            (xcache / "opencode/models.json").write_text(json.dumps(catalog))
            config = {
                "model": "openai/gpt", "plugin": ["keep"],
                "compaction": {"auto": True, "reserved": 5000},
                "provider": {"openai": {"options": {"keep": True}, "models": {
                    "gpt": {"variants": {"low": {"temperature": 0.1}},
                            "limit": {"context": 1000000, "input": 922000, "output": 8192}},
                    "second": {"variants": {"high": {"temperature": 0.8}},
                               "limit": {"context": 280000, "input": 170000, "output": 4096}},
                    "without-input": {"limit": {"context": 500000, "output": 4096}}}}},
                "disabled_providers": [],
            }
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
import {{ readFileSync, readdirSync }} from "node:fs";
const hooks = await AgentHarnessGuards({{ directory: process.cwd(), worktree: process.cwd(), client: {{}} }});
const original = {json.dumps(config)};
const before = JSON.parse(JSON.stringify(original));
const start = performance.now();
const after = await hooks.config(before);
await hooks["chat.params"]({{ sessionID: "ses_context123", model: {{ providerID: "openai", id: "gpt",
  limit: {{ context: 600000, input: 600000, output: 8192 }} }}, provider: {{ info: {{ id: "openai" }} }} }},
  {{ maxOutputTokens: 4096 }});
const policy = JSON.parse(readFileSync(process.env.XDG_STATE_HOME + "/agent-fleet/context-policy/opencode/attempt-att-context.json", "utf8"));
delete process.env.AGENT_DISPATCH_ATTEMPT_ID;
await hooks["chat.params"]({{ sessionID: "ses_interactive", model: {{ providerID: "openai", id: "gpt",
  limit: {{ context: 600000, input: 600000, output: 8192 }} }}, provider: {{ info: {{ id: "openai" }} }} }},
  {{ maxOutputTokens: 4096 }});
const policyDir = process.env.XDG_STATE_HOME + "/agent-fleet/context-policy/opencode";
const interactiveName = readdirSync(policyDir).find((name) => name.startsWith(process.pid + "-") && name.endsWith(".json"));
const interactivePolicy = JSON.parse(readFileSync(policyDir + "/" + interactiveName, "utf8"));
const again = await hooks.config(JSON.parse(JSON.stringify(after)));
const disabled = await hooks.config({{ ...JSON.parse(JSON.stringify(original)), disabled_providers: ["openai", "recent-provider"] }});
const restricted = await hooks.config({{ ...JSON.parse(JSON.stringify(original)), enabled_providers: ["openai"] }});
console.log(JSON.stringify({{ after, again, disabled, restricted, policy, interactivePolicy, elapsed_ms: performance.now() - start }}));
'''
            env = {**os.environ, "PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(xconfig),
                   "XDG_DATA_HOME": str(xdata), "XDG_STATE_HOME": str(xstate),
                   "XDG_CACHE_HOME": str(home / "cache"), "HEARTING_OPENCODE_MODELS_CACHE": str(xcache / "opencode"),
                   "PYTHONPYCACHEPREFIX": str(xcache / "pycache"),
                   "FLEET_SESSION_REGISTRY_DIR": str(xstate / "agent-fleet" / "sessions"),
                   "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker",
                   "AGENT_DISPATCH_ATTEMPT_ID": "att-context", "OPENCODE_VERSION": "1.19.0"}
            run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("PRIVATE_AUTH_VALUE", run.stdout + run.stderr)
            result = json.loads(run.stdout.strip().splitlines()[-1])
            policy = result["policy"]
            self.assertEqual((policy["session_id"], policy["attempt_id"], policy["model"]),
                             ("ses_context123", "att-context", "openai/gpt"))
            self.assertEqual(policy["policy"]["state"], "applied")
            self.assertEqual(policy["policy"]["effective_window_tokens"], 600000)
            self.assertEqual(policy["policy"]["applied_trigger_tokens"], 595000)
            self.assertEqual(policy["policy"]["output_allowance_tokens"], 4096)
            self.assertEqual(policy["policy"]["compaction_reserve_tokens"], 5000)
            self.assertEqual(result["interactivePolicy"]["session_id"], "ses_interactive")
            self.assertIsNone(result["interactivePolicy"]["attempt_id"])
            self.assertEqual(result["interactivePolicy"]["policy"]["state"], "applied")
            # The same producer record passes through the registered collector and
            # normalizer under its exact attempt/session, with the PRD's dispatch
            # surface remaining JSON-only.
            attempt_log = home / "attempt.att-context.opencode.jsonl"
            attempt_log.write_text(json.dumps({"type": "step_finish",
                "sessionID": "ses_context123", "part": {"tokens": {
                    "total": 300000, "input": 280000, "output": 1000,
                    "cache": {"read": 19000, "write": 0}}}}) + "\n", encoding="utf-8")
            os.utime(attempt_log, (time.time() + 2, time.time() + 2))
            job = DispatchJob(key="code", slug="worker", harness="opencode",
                              attempt_id="att-context", liveness="working")
            job.model = "openai/gpt"
            job.cwd = str(home)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xstate),
                    "FLEET_SESSION_REGISTRY_DIR": str(xstate / "agent-fleet" / "sessions")}), \
                    mock.patch.object(fleet_dispatch, "_owned_attempt_log_path",
                                      return_value=str(attempt_log)):
                fleet_dispatch._enrich_opencode_attempt_session(job)
                projection.attach_projections([], [job], now=time.time())
            self.assertEqual(job.active_context_tokens, 300000)
            self.assertEqual(job.context_display_window_tokens, 595000)
            self.assertEqual(job.context.used_pct, 50)
            payload = job.to_dict()
            self.assertEqual(payload["context_policy"]["attempt_id"], "att-context")
            self.assertIn("50%", "".join(part for row in
                render._dispatch_summary_detail_row(job, term_width=168) for part, _key in row))
            after = result["after"]
            gpt = after["provider"]["openai"]["models"]["gpt"]
            self.assertEqual(gpt["limit"], {"context": 600000, "input": 600000, "output": 8192}, run.stderr + run.stdout)
            self.assertEqual(gpt["variants"], {"low": {"temperature": 0.1}})
            self.assertEqual(after["provider"]["openai"]["options"], {"keep": True})
            self.assertEqual(result["again"], after)
            self.assertEqual(after["provider"]["openai"]["models"]["second"]["limit"],
                             {"context": 280000, "input": 170000, "output": 4096})
            self.assertEqual(after["provider"]["openai"]["models"]["without-input"]["limit"],
                             {"context": 500000, "output": 4096})
            self.assertEqual(after["provider"]["recent-provider"]["models"]["second"]["limit"],
                             {"context": 600000, "input": 600000, "output": 7000})
            self.assertNotIn("unloaded-catalog-only", after["provider"])
            # Native limits already under the budget: no entry, so OpenCode keeps the catalog record.
            self.assertNotIn("small", after["provider"]["openai"]["models"])
            self.assertEqual(result["disabled"]["provider"]["openai"], config["provider"]["openai"])
            self.assertNotIn("recent-provider", result["disabled"]["provider"])
            self.assertNotIn("recent-provider", result["restricted"]["provider"])
            self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_helper_failure_keeps_original_plugin_config(self):
        with ExitStack() as stack:
            raw = fixture_tempdir(stack, prefix="opencode-context-failure-")
            home = Path(raw)
            runtime = home / "config/opencode"
            (runtime / "agent-config").mkdir(parents=True)
            for name in ("data", "state", "cache"):
                (home / name).mkdir(parents=True, exist_ok=True)
            (runtime / "agent-config/models.conf").write_text("broken config\n")
            config = {"model": "openai/gpt", "provider": {"openai": {"models": {}}}}
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
const hooks = await AgentHarnessGuards({{ directory: process.cwd(), worktree: process.cwd(), client: {{}} }});
const config = {json.dumps(config)}; const result = await hooks.config(config);
console.log(JSON.stringify({{ same: result === config, config: result }}));
'''
            env = {**os.environ, "PATH": "/usr/bin:/bin", "HOME": str(home),
                   "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
                   "XDG_STATE_HOME": str(home / "state"), "XDG_CACHE_HOME": str(home / "cache"),
                   "PYTHONPYCACHEPREFIX": str(home / "cache" / "pycache"),
                   "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker"}
            run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout.strip().splitlines()[-1])["same"])


if __name__ == "__main__":
    unittest.main()
