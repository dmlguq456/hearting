#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import resource_run_registry as registry  # noqa: E402


class StatusParityTest(unittest.TestCase):
    def test_claude_codex_opencode_share_resource_counts(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            index = base / "index.json"
            runs = base / "runs.json"
            live = registry.proc_identity(os.getpid())
            runs.write_text(json.dumps({
                "schema_version": 1,
                "runs": {
                    "live": {**live, "status": "exited"},
                    "stale": {**live, "command_hash": "0" * 64, "status": "running"},
                    "exited": {
                        "pid": 99999999, "starttime": "1",
                        "command_hash": "1" * 64, "status": "running",
                    },
                },
            }))
            registry.register_registry(runs, index)
            env = {**os.environ, "AGENT_RESOURCE_RUN_INDEX": str(index)}
            jobs = base / "jobs.log"
            jobs.write_text("2026-10-07T00:00:00Z\tqueued\t/r\t/w\tunknown\tattempt_id=att-" + "a"*32 +
                            ",owner_route_id=rt-" + "b"*16 + ",worker_type=owner\n")
            env["AGENT_DISPATCH_JOBS"] = str(jobs)
            commands = {
                "claude": [str(ROOT / "utilities" / "harness-status.sh"), str(ROOT), "test"],
                "codex": [str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"),
                          "status", str(ROOT), "test"],
                "opencode": [str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh"),
                             "status", str(ROOT), "test"],
            }
            for adapter, command in commands.items():
                current_env = {**env, "AGENT_ADAPTER": adapter}
                result = subprocess.run(
                    command, text=True, capture_output=True, env=current_env, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                line = next(line for line in result.stdout.splitlines() if line.startswith("headless_evidence="))
                evidence = json.loads(line.split("=", 1)[1])
                self.assertEqual(evidence["shown"], 1)
                self.assertEqual(evidence["rows"][0]["state"], "unknown")
                self.assertEqual(evidence["rows"][0]["completion"], "unverified")
                for expected in (
                    "resource_run_live=1",
                    "resource_run_stale=1",
                    "resource_run_exited=1",
                    "resource_run_malformed=0",
                ):
                    self.assertIn(expected, result.stdout, (adapter, result.stdout))


if __name__ == "__main__":
    unittest.main()
