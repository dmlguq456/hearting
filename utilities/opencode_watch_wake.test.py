#!/usr/bin/env python3
"""OpenCode steward watch-wake: an idle OpenCode session is woken by its watch receipt.

A synthetic OpenCode (the plugin run under Node with fake SDK calls) and a stub
``peer-steward.py`` in a private runtime copy. No real model, session or ledger.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SID = "ses-oc-steward"
WATCH = "0123456789abcdef"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["STUB_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "session": os.environ.get("OPENCODE_SESSION_ID"),
                          "harness": os.environ.get("AGENT_DISPATCH_CALLER_HARNESS")}) + "\n")
watches = os.path.join(state, "peer-watches")
if sys.argv[1:2] == ["status"]:
    rows = [] if os.path.exists(os.path.join(watches, "%(watch)s.ack.json")) else [{
        "watch_id": "%(watch)s", "state": "receipt", "receipt_state": "idle", "target": "worker",
        "agent": {"harness": "codex", "session_id": "s2", "name": "worker", "pane": "p1"},
        "receipt": "/r/%(watch)s.receipt.json"}]
    print(json.dumps({"watch_root": watches, "watches": rows}))
elif sys.argv[1:2] == ["ack"]:
    with open(os.path.join(watches, sys.argv[2] + ".ack.json"), "w") as out:
        out.write("{}")
    print("ack=created")
''' % {"watch": WATCH}

JS = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(PLUGIN));
const prompts = [];
const accept = ACCEPT;
const session = {
  messages: async () => ({data: []}), prompt: async () => ({data: null}),
  promptAsync: async (request) => {
    prompts.push(request);
    return accept ? {data: undefined, response: {ok: true, status: 204}}
                  : {error: {name: "NotFound"}, response: {ok: false, status: 404}};
  },
};
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async () => {}}, session}, directory: process.cwd()});
const out = {env: {}};
await hooks["shell.env"]({sessionID: SID}, out);
const settle = async (ms) => { await new Promise(r => setTimeout(r, ms)); };
for (let round = 0; round < ROUNDS; round++) {
  await hooks.event({event: {type: "session.idle", properties: {sessionID: SID}}});
  await settle(300);
}
hooks.dispose();
console.log(JSON.stringify({prompts}));
'''


class OpenCodeWatchWakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.rt, self.state = base / "rt", base / "state"
        (self.state / "peer-watches").mkdir(parents=True)
        (self.state / "jobs.log").touch()
        plugins = self.rt / "adapters/opencode/plugins"
        plugins.mkdir(parents=True)
        shutil.copy2(ROOT / "adapters/opencode/plugins/hearting-guards.js", plugins)
        (self.rt / "adapters/opencode/bin").symlink_to(ROOT / "adapters/opencode/bin", target_is_directory=True)
        for name in ("core", "hooks", "tools", "profiles"):
            if (ROOT / name).exists():
                (self.rt / name).symlink_to(ROOT / name, target_is_directory=True)
        (self.rt / "utilities").mkdir()
        for path in (ROOT / "utilities").iterdir():
            if path.name != "peer-steward.py":
                (self.rt / "utilities" / path.name).symlink_to(path)
        stub = self.rt / "utilities/peer-steward.py"
        stub.write_text(STUB)
        stub.chmod(0o755)

    def arm_receipt(self, steward_sid=SID, harness="opencode"):
        watches = self.state / "peer-watches"
        (watches / f"{WATCH}.json").write_text(json.dumps(
            {"watch_id": WATCH, "steward": {"harness": harness, "session_id": steward_sid}}))
        (watches / f"{WATCH}.receipt.json").write_text(json.dumps({"state": "idle"}))

    def run_plugin(self, *, accept=True, rounds=2):
        js = JS.replace("PLUGIN", json.dumps(str(self.rt / "adapters/opencode/plugins/hearting-guards.js"))
                        ).replace("ACCEPT", "true" if accept else "false").replace(
            "SID", json.dumps(SID)).replace("ROUNDS", str(rounds))
        env = dict(os.environ, HARNESS_STATE_ROOT=str(self.state), AGENT_DISPATCH_JOBS=str(self.state / "jobs.log"),
                   AGENT_HOME=str(self.rt), STUB_STATE=str(self.state))
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH"):
            env.pop(key, None)
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=env, cwd=str(self.state),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    def calls(self):
        path = self.state / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        # The plugin's own start-up `__ensure-obligations` is not the watch pass.
        return [row for row in rows if row["argv"][:1] in (["status"], ["ack"])]

    def test_an_idle_session_is_woken_once_with_its_watch_receipt_and_acks_it(self):
        self.arm_receipt()
        result = self.run_plugin()
        self.assertEqual(len(result["prompts"]), 1)
        request = result["prompts"][0]
        self.assertEqual(request["path"], {"id": SID})
        text = request["body"]["parts"][0]["text"]
        self.assertTrue(text.startswith("[peer-steward] undelivered watch receipts:"), text)
        self.assertIn(f"watch_id={WATCH}", text)
        acks = [call for call in self.calls() if call["argv"][:1] == ["ack"]]
        self.assertEqual([call["argv"] for call in acks], [["ack", WATCH, "--carrier", "opencode-turn"]])
        self.assertTrue(all(call["session"] == SID and call["harness"] == "opencode" for call in self.calls()))

    def test_a_receipt_of_another_session_or_harness_spawns_nothing_and_wakes_nothing(self):
        for case, kwargs in (("other-session", {"steward_sid": "ses-other"}),
                             ("other-harness", {"harness": "claude"}), ("none", None)):
            with self.subTest(case):
                (self.state / "calls.jsonl").unlink(missing_ok=True)
                for leftover in (self.state / "peer-watches").iterdir():
                    leftover.unlink()
                if kwargs is not None:
                    self.arm_receipt(**kwargs)
                result = self.run_plugin()
                self.assertEqual(result["prompts"], [])
                self.assertEqual(self.calls(), [])

    def test_a_prompt_the_session_did_not_take_is_not_acked_and_is_retried_only_twice(self):
        self.arm_receipt()
        result = self.run_plugin(accept=False, rounds=4)
        self.assertEqual(len(result["prompts"]), 2)
        self.assertEqual([call for call in self.calls() if call["argv"][:1] == ["ack"]], [])
        self.assertFalse((self.state / "peer-watches" / f"{WATCH}.ack.json").exists())

    def test_an_acked_receipt_is_not_sent_again(self):
        self.arm_receipt()
        (self.state / "peer-watches" / f"{WATCH}.ack.json").write_text("{}")
        result = self.run_plugin()
        self.assertEqual((result["prompts"], self.calls()), ([], []))


if __name__ == "__main__":
    unittest.main()
