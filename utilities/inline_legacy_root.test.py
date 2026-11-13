#!/usr/bin/env python3
"""Direct routes in an artifact root whose cutover is inactive with legacy content."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer

CLI = [sys.executable, str(ROOT / "utilities/capability-route.py")]

# Reconstruct an actual pre-journal compiler seal before compose admits or
# starts it. The old reader/finish assertions below retain their v1 meaning;
# fresh public CLI routes are tested separately with no compiler override.
LEGACY_COMPOSE = '''import importlib.util,sys
spec=importlib.util.spec_from_file_location('legacy_contract_cli',sys.argv[1])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
compose=module.compose_route
def historical(**kwargs):
    route=compose(**kwargs)
    route.pop('artifact_storage_contract_version',None)
    route['route_hash']=module.route_hash(route)
    route['route_id']='rt-'+route['route_hash'].split(':',1)[1][:16]
    return route
module.compose_route=historical
sys.argv=sys.argv[1:]
raise SystemExit(module.main())
'''


class LegacyRootDirectRouteTest(unittest.TestCase):
    historical = True
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legacy-root-direct-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo, self.root, self.jobs = base / "repo", base / "repo/.agent_reports", base / "jobs.log"
        self.repo.mkdir()
        self.jobs.write_text("", encoding="utf-8")
        git = ["git", "-C", str(self.repo)]
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
        subprocess.run(git + ["config", "user.name", "t"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(git + ["add", "README.md"], check=True)
        subprocess.run(git + ["commit", "-qm", "base"], check=True)
        origin = base / "origin.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(origin)], check=True)
        subprocess.run(git + ["remote", "add", "origin", str(origin)], check=True)
        legacy = self.root / "analysis_project/summary.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy\n", encoding="utf-8")
        self.assertEqual(artifact_producer.classify_root(self.root)["state"], "inactive-with-legacy")
        self.env = {**os.environ, "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "AGENT_DISPATCH_DEPTH": "0", "CODEX_THREAD_ID": "legacy-root-test-session",
                    "XDG_STATE_HOME": str(base / "state"), "AGENT_ARTIFACT_CHECKPOINT": "off",
                    "HEARTING_WORKFLOW_GROUP_REVIEW": "off"}
        for key in ("AGENT_DISPATCH_ATTEMPT_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID",
                    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_ARTIFACT_ROOT"):
            self.env.pop(key, None)
        prompt = base / "task.md"
        prompt.write_text("Fix the README.\n", encoding="utf-8")
        self.compose = CLI + ["compose", "--slug", "legacy-direct", "--campaign-key", "legacy-direct",
                              "--shape", "direct", "--cwd", str(self.repo), "--artifact-root", str(self.root),
                              "--prompt-file", str(prompt), "--spec-read", "fixture",
                              "--drift-verdict", "within-spec", "--artifact-guard", "fixture",
                              "--parent-harness", "codex"]

    def run_cli(self, argv):
        if self.historical and len(argv) > 2 and argv[2] == "compose":
            argv = [sys.executable, "-c", LEGACY_COMPOSE, *argv[1:]]
        return subprocess.run(argv, cwd=self.repo, env=self.env, capture_output=True, text=True)

    def test_compose_start_returns_legacy_compat_env_not_a_traceback(self):
        started = self.run_cli(self.compose + ["--start"])
        self.assertNotIn("Traceback", started.stderr)
        self.assertEqual(started.returncode, 0, started.stderr[-2000:])
        receipt = json.loads(started.stdout.strip().splitlines()[-1])
        self.assertEqual(receipt["state"], "inline", receipt)
        self.assertEqual(receipt["artifact_env"], {"AGENT_ARTIFACT_ROOT": str(self.root.resolve())})
        route = json.loads(Path(receipt["route_file"]).read_text(encoding="utf-8"))
        self.assertNotEqual(Path(route["cwd"]).resolve(), self.repo.resolve())
        self.assertEqual(route["artifact_root"], str(self.root.resolve()))
        self.assertFalse((self.root / "campaigns").exists())   # nothing activated or issued

    def finish_command(self):
        composed = self.run_cli(self.compose)
        self.assertEqual(composed.returncode, 0, composed.stderr[-2000:])
        route_file = json.loads(composed.stdout)["route_file"]
        (self.repo / "README.md").write_text("fixed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "fix"], check=True)
        evidence = self.root / "analysis_project/evidence.md"
        evidence.write_text("done\n", encoding="utf-8")
        summary = Path(self.temp.name) / "summary.md"
        summary.write_text("Fixed the README.\n", encoding="utf-8")
        return CLI + ["finish", "--route", route_file, "--evidence", str(evidence),
                      "--summary-file", str(summary)], Path(route_file)

    def test_finish_closes_a_legacy_direct_route_with_a_proven_gate(self):
        command, _route = self.finish_command()
        finished = self.run_cli(command)
        self.assertEqual(finished.returncode, 0, finished.stderr[-2000:])
        receipt = json.loads(finished.stdout)
        self.assertIs(receipt["outcome"]["terminal_gate_proven"], True, receipt)

    def test_start_preserves_explicit_inactive_fallback_denial(self):
        self.env["AGENT_ARTIFACT_INACTIVE_FALLBACK"] = "deny"
        composed = self.run_cli(self.compose)
        self.assertEqual(composed.returncode, 0, composed.stderr)
        route_file = json.loads(composed.stdout)["route_file"]
        # Use the real CLI entrypoint for the refusal, including its typed
        # ProducerError handler, after reconstructing the historical seal.
        denied = self.run_cli(CLI + ["start", "--route", route_file, "--jobs", str(self.jobs)])
        self.assertEqual(denied.returncode, 64, denied.stderr)
        self.assertIn("cutover-inactive-fallback-denied", denied.stderr)
        self.assertNotIn("Traceback", denied.stderr)
        self.assertFalse((self.root / "campaigns").exists())

    def test_legacy_finish_preserves_registered_caller_and_route_refusals(self):
        command, route = self.finish_command()
        self.env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-" + "a" * 48
        denied = self.run_cli(command)
        self.assertEqual(denied.returncode, 64, denied.stderr)
        self.assertIn("finish-registered-caller-ineligible", denied.stderr)
        self.env.pop("AGENT_DISPATCH_ATTEMPT_ID")
        route_id = json.loads(route.read_text())["route_id"]
        self.jobs.write_text("field\tfield\tfield\tfield\tfield\troute_id=" + route_id + "\n")
        denied = self.run_cli(command)
        self.assertEqual(denied.returncode, 64, denied.stderr)
        self.assertIn("finish-registered-route-ineligible", denied.stderr)
        self.assertFalse(route.with_suffix(".outcome.json").exists())

    def test_legacy_finish_preserves_closed_route_and_missing_evidence_refusals(self):
        command, route = self.finish_command()
        evidence = Path(command[command.index("--evidence") + 1])
        original = evidence.read_bytes()
        evidence.unlink()
        denied = self.run_cli(command)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("completion evidence missing", denied.stderr)
        self.assertFalse(route.with_suffix(".outcome.json").exists())
        evidence.write_bytes(original)
        finished = self.run_cli(command)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        outcome = route.with_suffix(".outcome.json")
        before = outcome.read_bytes()
        denied = self.run_cli(command)
        self.assertEqual(denied.returncode, 64, denied.stderr)
        self.assertIn("finish-route-already-closed", denied.stderr)
        self.assertEqual(outcome.read_bytes(), before)


class JournalRootDirectRouteTest(unittest.TestCase):
    historical = False
    setUp = LegacyRootDirectRouteTest.setUp
    run_cli = LegacyRootDirectRouteTest.run_cli

    def test_fresh_start_and_finish_use_a_journal_without_legacy_writes_or_activation(self):
        # "deny" forbids writing the legacy fallback buckets. A fresh journal
        # neither enables that fallback nor moves the existing legacy payload.
        self.env["AGENT_ARTIFACT_INACTIVE_FALLBACK"] = "deny"
        started = self.run_cli(self.compose + ["--start"])
        self.assertEqual(started.returncode, 0, started.stderr)
        receipt = json.loads(started.stdout.strip().splitlines()[-1])
        route_file = Path(receipt["route_file"])
        route = json.loads(route_file.read_text())
        self.assertEqual(route["artifact_storage_contract_version"], 2)
        self.assertEqual(receipt["artifact_env"]["AGENT_ARTIFACT_CYCLE_ID"], "")
        execution = Path(route["cwd"])
        (execution / "README.md").write_text("fixed\n")
        subprocess.run(["git", "-C", str(execution), "commit", "-qam", "fix"], check=True)
        summary = Path(self.temp.name) / "summary.md"
        summary.write_text("Fixed the README.\n")
        finished = self.run_cli(CLI + ["finish", "--route", str(route_file),
            "--evidence", str(execution / "README.md"), "--summary-file", str(summary)])
        self.assertEqual(finished.returncode, 0, finished.stderr)
        done = json.loads(finished.stdout)
        self.assertEqual(done["schema"], "finish_receipt_v2")
        self.assertEqual(len(done["entries"]), 1)
        self.assertEqual((self.root / "analysis_project/summary.md").read_text(), "legacy\n")
        self.assertFalse(artifact_producer.cutover_path(self.root).exists())
        self.assertEqual(list((self.root / "campaigns").glob("*/*/.cycle.json")), [])


if __name__ == "__main__":
    unittest.main()
