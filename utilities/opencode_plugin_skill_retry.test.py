#!/usr/bin/env python3
"""OpenCode V2 plugin: `skill` tool reminder/grounding, command grounding, worker retry bound.

PG-11 (SD-11 stage-dispatch reminder decided in `execute.before`, delivered with the skill's
result), PG-12 (capability-grounding marker for `skill` and `/autopilot-*` command starts) and
PG-18 (a registered worker vetoes a long provider back-off) on the registered V2 hooks, with the
helper processes stubbed so nothing outside the temporary HOME is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "adapters" / "opencode" / "plugins" / "hearting-guards.js"
WORKER_MARKERS = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                  "AGENT_DISPATCH_INTENSITY", "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH",
                  "AGENT_DISPATCH_REGISTERED_WORKER")
BASE_ENV = {key: value for key, value in os.environ.items() if key not in WORKER_MARKERS}

HARNESS = r'''
import childProcess from "node:child_process"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"
const reminders = []
const spawned = []
childProcess.spawnSync = (command, args, options) => {
  const script = String(args?.[0] || "")
  if (script.endsWith("stage-dispatch-reminder.sh")) {
    reminders.push(args.slice(1))
    const skill = args[args.indexOf("--skill") + 1]
    const text = skill.startsWith("code-") ? "REMINDER-FOR " + skill : ""
    return { status: 0, stdout: text ? JSON.stringify({ hookSpecificOutput: { additionalContext: text } }) : "", stderr: "" }
  }
  return { status: 0, stdout: args?.includes?.("pending") ? "[]" : "{}", stderr: "" }
}
childProcess.spawn = (command, args) => {
  spawned.push([command, ...(args || [])])
  return { on() {}, unref() {}, stdin: { on() {}, end() {} }, stdout: { on() {} }, stderr: { on() {} } }
}
syncBuiltinESMExports()
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
const hooks = {}
const titles = { ses_worker: "hearting-worker-att-probe", ses_main: "Main" }
const ctx = {
  location: { directory: process.cwd() },
  session: { hook: async (name, fn) => { hooks["session." + name] = fn },
    get: async ({ sessionID }) => ({ id: sessionID, title: titles[sessionID] || "Main",
      parentID: null, directory: process.cwd() }) },
  tool: { hook: async (name, fn) => { hooks["tool." + name] = fn } },
  shell: { hook: async () => {} },
  permission: { hook: async () => {} },
  event: { subscribe: async function* ({ signal }) {
    await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }))
  } },
}
const cleanup = await plugin.setup(ctx)
const out = {}
try {
  await (CASE)
} finally { await cleanup() }
console.log(JSON.stringify({ ...out, reminders, spawned }))
'''


class OpenCodePluginSkillRetryTest(unittest.TestCase):
    def run_case(self, case: str, **env_extra):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required")
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(BASE_ENV, HOME=temporary, XDG_STATE_HOME=str(Path(temporary) / "state"),
                       XDG_CONFIG_HOME=str(Path(temporary) / "config"), AGENT_HOME=str(ROOT),
                       OPENCODE_VERSION="2.0.32", PLUGIN_PATH=str(PLUGIN), **env_extra)
            result = subprocess.run(
                [node, "--input-type=module", "-e", HARNESS.replace("(CASE)", "(async () => {" + case + "})()")],
                cwd=temporary, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout.strip().splitlines()[-1])

    def grounding(self, data):
        return [row for row in data["spawned"]
                if any(str(part).endswith("capability-grounding.sh") for part in row)]

    def test_stage_skill_reminder_rides_the_skill_result(self):
        data = self.run_case('''
          const event = { tool: "skill", sessionID: "ses_main", input: { id: "code-execute" } }
          await hooks["tool.execute.before"](event)
          const after = { ...event, status: "completed",
            result: { content: [{ type: "text", text: "SKILL BODY" }], metadata: {} } }
          await hooks["tool.execute.after"](after)
          out.content = after.result.content.map(part => part.text)
        ''')
        self.assertEqual(data["reminders"][0][:2], ["--skill", "code-execute"])
        self.assertEqual(data["content"][0], "SKILL BODY")
        self.assertIn("REMINDER-FOR code-execute", data["content"][-1])
        # A stage skill is not an entry capability: no grounding marker.
        self.assertEqual(self.grounding(data), [])

    def test_worker_session_also_receives_the_reminder(self):
        data = self.run_case('''
          const event = { tool: "skill", sessionID: "ses_worker", input: { id: "code-test" } }
          await hooks["tool.execute.before"](event)
          const after = { ...event, status: "completed",
            result: { content: [{ type: "text", text: "BODY" }], metadata: {} } }
          await hooks["tool.execute.after"](after)
          out.content = after.result.content.map(part => part.text)
        ''')
        self.assertIn("REMINDER-FOR code-test", data["content"][-1])

    def test_non_stage_skill_gets_no_reminder_text(self):
        data = self.run_case('''
          const event = { tool: "skill", sessionID: "ses_main", input: { id: "session-tidy" } }
          await hooks["tool.execute.before"](event)
          const after = { ...event, status: "completed",
            result: { content: [{ type: "text", text: "BODY" }], metadata: {} } }
          await hooks["tool.execute.after"](after)
          out.content = after.result.content.map(part => part.text)
        ''')
        self.assertEqual(data["content"][0], "BODY")
        self.assertFalse(any("REMINDER" in text for text in data["content"]), data["content"])

    def test_entry_skill_load_records_capability_grounding(self):
        data = self.run_case('''
          const event = { tool: "skill", sessionID: "ses_main", input: { id: "autopilot-code" } }
          await hooks["tool.execute.before"](event)
          await hooks["tool.execute.after"]({ ...event, status: "completed",
            result: { content: [{ type: "text", text: "BODY" }], metadata: {} } })
          await hooks["tool.execute.after"]({ ...event, sessionID: "ses_worker", status: "completed",
            result: { content: [{ type: "text", text: "BODY" }], metadata: {} } })
        ''')
        rows = self.grounding(data)
        self.assertEqual(len(rows), 1, data["spawned"])
        self.assertIn("ses_main", rows[0])
        self.assertEqual(rows[0][rows[0].index("--capability") + 1], "autopilot-code")

    def test_command_start_records_grounding_from_its_arguments_line(self):
        body = ("Use the OpenCode adapter realization of portable capability `autopilot-code`.\\n"
                "4. map them to `--mode dev|debug <task> [--intensity direct|quick|standard]`.\\n"
                "User arguments from OpenCode: `fix the parser --mode debug strong`\\n")
        data = self.run_case(f'''
          await hooks["session.prompt"]({{ sessionID: "ses_main", messageID: "msg_1",
            prompt: {{ text: "{body}" }} }})
        ''')
        rows = self.grounding(data)
        self.assertEqual(len(rows), 1, data["spawned"])
        row = rows[0]
        self.assertEqual(row[row.index("--capability") + 1], "autopilot-code")
        # Read from the person's arguments, never from the template's argument shape.
        self.assertEqual(row[row.index("--mode") + 1], "debug")
        self.assertEqual(row[row.index("--intensity") + 1], "strong")

    RETRY_CASE = '''
          const hook = hooks["session.retry"]
          const make = (sessionID, delay) => ({ sessionID, agent: "build", attempt: 2,
            model: { providerID: "p", id: "m" },
            error: { type: "provider.rate-limit", message: "Rate limit exceeded", status: 429 },
            decision: { retry: true, delay } })
          const events = [make("ses_worker", 900000), make("ses_worker", 2000),
            make("ses_main", 60000), make("ses_main", 60001)]
          // An ordinary session renamed to the worker title and back is still ordinary.
          titles.ses_main = "hearting-worker-notes"
          const renamed = make("ses_main", 900000)
          await hook(renamed)
          titles.ses_main = "Main"
          const reverted = make("ses_main", 900000)
          for (const event of events) await hook(event)
          await hook(reverted)
          out.decisions = [...events, renamed, reverted].map(event => event.decision)
        '''

    def test_registered_worker_vetoes_a_long_provider_back_off_only(self):
        data = self.run_case(self.RETRY_CASE, AGENT_DISPATCH_REGISTERED_WORKER="1")
        long_veto, short, at_ceiling, over, renamed, reverted = data["decisions"]
        self.assertEqual(long_veto, {"retry": False})
        self.assertEqual(short, {"retry": True, "delay": 2000})
        self.assertEqual(at_ceiling, {"retry": True, "delay": 60000})
        # In a registered dispatch's own process every session is that worker's.
        self.assertEqual(over, {"retry": False})
        self.assertEqual(renamed, {"retry": False})
        self.assertEqual(reverted, {"retry": False})

    def test_unregistered_run_keeps_native_retry_whatever_its_title(self):
        # RV-PR1460 P2-1: `opencode run --title hearting-worker-notes` with no jobs and no worker
        # environment kept retrying on base and must keep doing so.
        data = self.run_case(self.RETRY_CASE)
        long_worker_title, short, at_ceiling, over, renamed, reverted = data["decisions"]
        self.assertEqual(long_worker_title, {"retry": True, "delay": 900000})
        self.assertEqual(over, {"retry": True, "delay": 60001})
        self.assertEqual(renamed, {"retry": True, "delay": 900000})
        self.assertEqual(reverted, {"retry": True, "delay": 900000})

    def test_quoted_command_body_is_not_a_capability_start(self):
        # RV-PR1460 P2-2: the generated body's first line quoted in an explanation request.
        quoted = ("Please explain this documentation; do not start work.\n```\n"
                  "Use the OpenCode adapter realization of portable capability `autopilot-code`.\n"
                  "User arguments from OpenCode: `--mode dev --intensity quick`\n```\n")
        slash_later = "see the docs, then maybe\n/autopilot-code --mode dev"
        data = self.run_case(f'''
          for (const [index, text] of [{json.dumps(quoted)}, {json.dumps(slash_later)}].entries()) {{
            await hooks["session.prompt"]({{ sessionID: "ses_main", messageID: "msg_q" + index,
              prompt: {{ text }} }})
          }}
        ''')
        self.assertEqual(self.grounding(data), [], data["spawned"])

    def test_typed_slash_command_prompt_is_a_start(self):
        data = self.run_case('''
          await hooks["session.prompt"]({ sessionID: "ses_main", messageID: "msg_s",
            prompt: { text: "  /autopilot-code --mode dev --intensity quick fix it" } })
        ''')
        rows = self.grounding(data)
        self.assertEqual(len(rows), 1, data["spawned"])
        self.assertEqual(rows[0][rows[0].index("--intensity") + 1], "quick")


class CapabilityGroundingMarkerTest(unittest.TestCase):
    def record(self, payload):
        with tempfile.TemporaryDirectory() as temporary:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": temporary,
                   "AGENT_HOME": str(ROOT),
                   "FLEET_CAPABILITY_GROUNDING_DIR": str(Path(temporary) / "g")}
            subprocess.run(["sh", str(ROOT / "hooks" / "capability-grounding-marker.sh")],
                           input=json.dumps(payload), env=env, text=True, check=True, timeout=20)
            directory = Path(temporary) / "g"
            return {path.name: path.read_text() for path in directory.glob("*")} \
                if directory.is_dir() else {}

    def test_user_prompt_submit_slash_start_is_grounded(self):
        found = self.record({"hook_event_name": "UserPromptSubmit", "session_id": "sid-a",
                             "prompt": "/autopilot-code --mode dev --intensity quick fix it"})
        self.assertEqual(found["sid-a"].splitlines()[:3],
                         ["capability=autopilot-code", "mode=dev", "intensity=quick"])

    def test_ordinary_prompts_are_not_grounded(self):
        quoted = ("Please explain this; do not start.\n```\n/autopilot-code --mode dev "
                  "--intensity quick\n```\n")
        for prompt in ("please run /autopilot-code later", "/autopilot-codex x", "hello", quoted,
                       "> /autopilot-code --mode dev"):
            self.assertEqual(self.record({"hook_event_name": "UserPromptSubmit",
                                          "session_id": "sid-b", "prompt": prompt}), {}, prompt)

    def test_model_skill_call_is_still_grounded(self):
        found = self.record({"tool_name": "Skill", "session_id": "sid-c",
                             "tool_input": {"skill": "autopilot-lab", "args": "standard"}})
        self.assertIn("capability=autopilot-lab", found["sid-c"])


if __name__ == "__main__":
    unittest.main()
