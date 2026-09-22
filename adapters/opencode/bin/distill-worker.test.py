#!/usr/bin/env python3
"""Synthetic CLI responses; real OpenCode prompt, worker, applier, and memory CLI.

These contract regressions do not claim real-model verification. Subprocesses
have a private HOME/XDG/store/config and no runtime credentials.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
MEM = ROOT / "tools/memory/mem.py"
TYPES = {"decision", "user-correction", "unresolved-obligation", "artifact-pointer"}
CAPSULE = {"headline", "aliases", "entities", "topics", "artifact_refs"}


def frontmatter_mapping(text, section):
    lines = text.split("---", 2)[1].splitlines()
    active = False
    result = {}
    for line in lines:
        if line == section + ":":
            active = True
            continue
        if active and line and not line.startswith(" "):
            break
        if active and line.startswith("  "):
            raw_key, raw_value = line.strip().split(":", 1)
            key = json.loads(raw_key) if raw_key.startswith('"') else raw_key
            value = raw_value.strip()
            result[key] = False if value == "false" else value
    return result


class CurateContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-curate-contract-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        home = self.base / "home"
        home.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        self.store = self.base / "store"
        self.capture = self.base / "prompt.txt"
        self.calls = self.base / "invocation.json"
        self.output = self.base / "actions.jsonl"
        self.sid = "synthetic-opencode-curate"
        self.legacy_agent = self.store / ".opencode-distill-workdir/.opencode/agent/distiller.md"
        self.legacy_agent.parent.mkdir(parents=True)
        self.legacy_text = "---\ntools:\n  bash: false\n---\nlegacy finite deny\n"
        self.legacy_agent.write_text(self.legacy_text)
        source = self.base / "export.json"
        source.write_text(json.dumps({"messages": [{
            "info": {"id": "synthetic-u1", "role": "user"},
            "parts": [{"type": "text", "text":
                       "SYNTHETICCURATE: deployment region corrected to ap-northeast-2."}]
        }]}))
        cli = self.base / "opencode"
        cli.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
from pathlib import Path
Path(os.environ["TEST_PROMPT"]).write_text(sys.stdin.read())
Path(os.environ["TEST_CALLS"]).write_text(json.dumps({"argv":sys.argv[1:],
    "worker":os.environ.get("AGENT_SESSION_ROLE"), "distill":os.environ.get("MEM_DISTILL")}))
sys.stdout.write(Path(os.environ["TEST_OUTPUT"]).read_text())
''')
        cli.chmod(0o700)
        self.env = {
            "PATH": os.defpath, "HOME": str(home), "AGENT_HOME": str(ROOT),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_DATA_HOME": str(self.base / "data"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "MEM_STORE": str(self.store), "MEM_PROJECTS": str(self.base / "projects"),
            "CODEX_SESSIONS": str(self.base / "sessions"),
            "MEM_WRITE_EVENTS": str(self.base / "events/write.jsonl"),
            "MEM_RECALL_EVENTS": str(self.base / "events/recall.jsonl"),
            "MEM_RECALL_RECEIPTS": str(self.base / "events/receipts"),
            "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0",
            "MEM_SYNC_DIR": str(self.base / "exchange"),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.base / "governor"),
            "AGENT_ARTIFACT_ROOT": str(self.project / ".agent_reports"),
            "OPENCODE_EXPORT_FILE": str(source), "OPENCODE_BIN": str(cli),
            "OPENCODE_DISTILL_ENABLE": "1", "OPENCODE_DISTILL_APPLY": "1",
            "OPENCODE_DISTILL_TIMEOUT": "8", "TEST_PROMPT": str(self.capture),
            "TEST_CALLS": str(self.calls), "TEST_OUTPUT": str(self.output),
        }
        self.mem("index")

    def mem(self, *args):
        result = subprocess.run([sys.executable, str(MEM), *args], env=self.env,
                                cwd=self.project, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout

    def run_worker(self, actions, expected_rc=0):
        self.output.write_text("".join(json.dumps(row) + "\n" for row in actions))
        proc = subprocess.Popen(["sh", str(ROOT / "adapters/opencode/bin/distill-worker.sh"),
                                 self.sid, str(self.project), "curate"], env=self.env,
                                cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, expected_rc, (out, err))
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=5)
        invocation = json.loads(self.calls.read_text())
        self.assertEqual(invocation["worker"], "worker")
        self.assertEqual(invocation["distill"], "1")
        self.assertIn("--pure", invocation["argv"])
        self.assertIn("distiller", invocation["argv"])
        workdir = self.store / ".opencode-distill-workdir-v2"
        self.assertEqual(Path(invocation["argv"][invocation["argv"].index("--dir") + 1]), workdir)
        self.assertEqual(self.legacy_agent.read_text(), self.legacy_text)
        agent = (workdir / ".opencode/agent/distiller.md").read_text()
        self.assertIs(frontmatter_mapping(agent, "tools")["*"], False)
        self.assertEqual(frontmatter_mapping(agent, "permission")["*"], "deny")
        # Parse the contract actually delivered to the CLI, not source text.
        schema = next(json.loads(line.strip()) for line in self.capture.read_text().splitlines()
                      if line.strip().startswith('{"action":"add"'))
        self.assertEqual(set(schema["type"].split("|")), TYPES)
        self.assertTrue(CAPSULE <= set(schema))
        self.assertNotIn("Type is a descriptive label", self.capture.read_text())
        self.assertIn("artifact-pointer requires artifact_refs", self.capture.read_text())
        return err

    def action(self, kind, token, refs=None):
        return {"action": "add", "tier": "durable", "type": kind,
                "body": token + " deployment region corrected to ap-northeast-2.",
                "headline": token + " deployment", "aliases": [token, "deployment region"],
                "entities": [token], "topics": ["deployment"], "artifact_refs": refs or []}

    def test_declared_storage_purposes_apply_with_retrievable_capsules(self):
        actions = [self.action(kind, "CURATEVALID" + str(i), ["decisions/deployment.md"]
                               if kind == "artifact-pointer" else [])
                   for i, kind in enumerate(sorted(TYPES))]
        self.run_worker(actions)
        for action in actions:
            token = action["entities"][0]
            with self.subTest(kind=action["type"]):
                rows = json.loads(self.mem("recall", token, "--full", "--json"))["results"]
                matches = [row for row in rows if row["body"] == action["body"]]
                self.assertEqual(len(matches), 1, rows)
                self.assertEqual(matches[0]["type"], action["type"])
                self.assertTrue(matches[0]["id"])
                shown = self.mem("show", matches[0]["id"])
                for field in ("headline", "aliases", "entities", "topics"):
                    self.assertIn(field + ":", shown)
                self.assertIn(action["headline"], shown)
                for ref in action["artifact_refs"]:
                    self.assertIn(ref, shown)
        self.assertEqual((self.store / (".distill-state-" + self.sid)).read_text().strip(), "synthetic-u1")
        self.assertEqual(self.mem("distill", self.sid, "--source", "opencode"), "")
        events = [json.loads(line) for line in Path(self.env["MEM_WRITE_EVENTS"]).read_text().splitlines()]
        self.assertEqual(sum(row.get("actor") == "curator" for row in events), len(actions))



if __name__ == "__main__":
    unittest.main()
