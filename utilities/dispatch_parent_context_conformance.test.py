#!/usr/bin/env python3
"""Real-wrapper parent-context isolation conformance for registered Codex jobs."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADLESS = ROOT / "adapters/codex/bin/dispatch-headless.py"
PY_LIVENESS = ROOT / "adapters/codex/bin/dispatch-liveness.py"
SH_LIVENESS = ROOT / "utilities/dispatch-liveness.sh"
WAIT = ROOT / "utilities/dispatch-wait.sh"
HARVEST = ROOT / "adapters/codex/bin/dispatch-harvest.py"
INSTALL_PROJECTION = ROOT / "adapters/codex/bin/install-runtime-projection.sh"

RAW_SENTINELS = (
    "RAW_COMMAND_SENTINEL",
    "RAW_AGENT_SENTINEL",
    "FINAL_MESSAGE_SENTINEL",
    "RAW_STDERR_SENTINEL",
    "ARTIFACT_BODY_SENTINEL",
)


def fields(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class ParentContextConformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / "x").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.artifact_root = self.base / ".agent_reports"
        self.artifact_root.mkdir()
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.agent_home = ROOT
        self.codex_home = self.base / "codex-home"
        projection = subprocess.run(
            [str(INSTALL_PROJECTION), "--skills-mode", "native"],
            text=True,
            capture_output=True,
            env={**os.environ, "AGENT_HOME": str(ROOT), "CODEX_HOME": str(self.codex_home)},
        )
        self.assertEqual(projection.returncode, 0, projection.stdout + projection.stderr)
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        self._write_fake_codex()
        self.captures = []

    def tearDown(self):
        self.temp.cleanup()

    def _write_fake_codex(self):
        script = self.fake_bin / "codex"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "verdict=os.environ['FAKE_CODEX_VERDICT']\n"
            "artifact=os.environ['FAKE_CODEX_ARTIFACT']\n"
            "blocker='none' if verdict=='PASS' else 'BLOCKER_DETAIL_PAYLOAD\\t'+'한'*400\n"
            "events=[\n"
            " {'type':'turn.started'},\n"
            " {'type':'item.completed','item':{'type':'command_execution','exit_code':0,'aggregated_output':'RAW_COMMAND_SENTINEL'}},\n"
            " {'type':'item.completed','item':{'type':'command_execution','exit_code':1,'aggregated_output':'FAILURE_DIAGNOSTIC_PAYLOAD\\n\\x01'+'界'*400}},\n"
            " {'type':'item.completed','item':{'type':'agent_message','text':'RAW_AGENT_SENTINEL'}},\n"
            " {'type':'item.completed','item':{'type':'agent_message','text':f'artifact: {artifact}\\nverdict: {verdict}\\nblocker: {blocker}'}},\n"
            " {'type':'turn.completed'},\n"
            "]\n"
            "for event in events: print(json.dumps(event), flush=True)\n"
            "print('RAW_STDERR_SENTINEL', file=sys.stderr, flush=True)\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def env(self, verdict, artifact):
        env = {
            **os.environ,
            "PATH": str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "AGENT_HOME": str(self.agent_home),
            "CODEX_HOME": str(self.codex_home),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_MODEL_GOVERNOR_ROOT": str(
                self.artifact_root / ".runtime" / "model-worker-governor"
            ),
            "FAKE_CODEX_VERDICT": verdict,
            "FAKE_CODEX_ARTIFACT": str(artifact),
            "CODEX_DISPATCH_EARLY_EXIT_WATCH": "0",
        }
        for key in (
            "AGENT_DISPATCH_JOBS", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
            "AGENT_DISPATCH_PARENT_SESSION_ID", "AGENT_DISPATCH_PARENT_CWD",
        ):
            env.pop(key, None)
        return env

    def wrapper(self, verdict, *, slug=None, attempt=None, run_id=None):
        run_id = run_id or verdict.lower()
        slug = slug or f"conformance-{verdict.lower()}"
        attempt = attempt or f"att-conformance-{verdict.lower()}"
        artifact = self.artifact_root / f"{run_id}-FINAL_MESSAGE_SENTINEL.md"
        artifact.write_text("ARTIFACT_BODY_SENTINEL\n")
        jobs = self.base / f"{run_id}.jobs.log"
        decoy = (
            f"2026-07-22T00:00:00Z\tdone\t{self.repo}\t{self.repo}\tdecoy\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,attempt_id=att-decoy,harness=codex,note=fixture\n"
        )
        jobs.write_text(decoy)
        before = jobs.read_bytes()
        command = [
            sys.executable, str(HEADLESS), "--start",
            "--worktree", str(self.repo), "--slug", slug,
            "--capability", "autopilot-code", "--capability-mode", "dev",
            "--qa", "standard", "--intensity", "standard",
            "--dispatch-depth", "1", "--worker-type", "owner",
            "--unit", "_kernel/owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model", "gpt-test", "--reasoning", "low",
            "--jobs", str(jobs), "--log-dir", str(self.logs),
            "--prompt-text", "fixture prompt", "--attempt-id", attempt,
            "--sandbox", "danger-full-access",
            "--completion-delivery", "poll",
            "--launch-lifecycle", "foreground-scoped", "--foreground-timeout", "30",
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, env=self.env(verdict, artifact), timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = fields(result.stdout)
        self.assertEqual(receipt["attempt_id"], attempt)
        self.assertEqual(receipt["handoff_state"], "valid")
        self.assertEqual(receipt["handoff_verdict"], verdict)
        self.assertEqual(receipt["artifact_state"], "readable")
        self.assertEqual(receipt["blocker_reason"], "none" if verdict == "PASS" else "worker-reported")
        log = Path(receipt["log_file"])
        log_text = log.read_text()
        for sentinel in RAW_SENTINELS[:-1]:
            self.assertIn(sentinel, log_text)
        self.assertIn("ARTIFACT_BODY_SENTINEL", artifact.read_text())
        for sentinel in RAW_SENTINELS:
            self.assertNotIn(sentinel, result.stdout + result.stderr)
        decoded = base64.urlsafe_b64decode(
            receipt["artifact_path_b64"] + "=" * (-len(receipt["artifact_path_b64"]) % 4)
        ).decode()
        self.assertEqual(decoded, str(artifact))
        self.assertIn("ARTIFACT_BODY_SENTINEL", Path(decoded).read_text())
        rows = jobs.read_text().splitlines()
        self.assertEqual(rows[0] + "\n", before.decode())
        target = next(row for row in rows if f"attempt_id={attempt}" in row)
        expected_status = "open" if verdict == "PASS" else "done"
        self.assertIn(f"\t{expected_status}\t", target)
        if verdict == "FAIL":
            self.assertIn("note=dead-worker-fail", target)
        if verdict == "BLOCKED":
            self.assertIn("note=dead-worker-blocked", target)
        return {
            "verdict": verdict, "slug": slug, "attempt": attempt, "artifact": artifact,
            "jobs": jobs, "log": log, "receipt": result, "target": target,
            "env": self.env(verdict, artifact),
        }

    def run_parent_surface(self, command, env, name, expected=3):
        result = subprocess.run(command, text=True, capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.captures.append((name + ".stdout", result.stdout))
        self.captures.append((name + ".stderr", result.stderr))
        return result

    def test_real_wrapper_lifecycle_and_parent_context_isolation(self):
        runs = {verdict: self.wrapper(verdict) for verdict in ("PASS", "FAIL", "BLOCKED")}
        for verdict, run in runs.items():
            self.captures.append((f"wrapper-{verdict}.stdout", run["receipt"].stdout))
            self.captures.append((f"wrapper-{verdict}.stderr", run["receipt"].stderr))

            if verdict == "PASS":
                jobs = run["jobs"]
                py_live = self.run_parent_surface(
                    [sys.executable, str(PY_LIVENESS), str(jobs)], run["env"], "pass-python-liveness"
                )
                self.assertIn("COMPLETED", py_live.stdout)
                sh_live = self.run_parent_surface(
                    ["bash", str(SH_LIVENESS), str(jobs)], run["env"], "pass-shell-liveness"
                )
                self.assertIn("COMPLETED", sh_live.stdout)
                wait = self.run_parent_surface(
                    ["sh", str(WAIT), "--jobs", str(jobs), "--interval", "1", "--max", "5"],
                    run["env"], "pass-wait", expected=2,
                )
                pending = json.loads(wait.stdout.splitlines()[-1])
                self.assertEqual(pending["state"], "pending")
                children = {child["attempt_id"]: child for child in pending["children"]}
                self.assertEqual(set(children), {run["attempt"], "att-decoy"})
                self.assertEqual(children[run["attempt"]]["status"], "open")
                self.assertEqual(children[run["attempt"]]["readiness"], "pending")
                self.assertEqual(children[run["attempt"]]["reason"], "terminal-commit-pending")
                self.assertEqual(children[run["attempt"]]["required_action"], "complete-open")
                self.assertEqual(children["att-decoy"]["reason"], "process-unverifiable")
                harvest = self.run_parent_surface(
                    [sys.executable, str(HARVEST), "--jobs", str(jobs),
                     "--attempt-id", run["attempt"], "--status", "open"],
                    run["env"], "pass-harvest", expected=0,
                )
                self.assertIn("terminal_verdict=PASS", harvest.stdout)
                self.assertIn(f"\topen\t", run["jobs"].read_text())
                completion = self.agent_home / ".dispatch/completion"
                self.assertFalse(any(completion.rglob(f"*{run['attempt']}*")) if completion.exists() else False)
            else:
                # Closed real failures are read only by the exact-attempt harvest
                # selector. Liveness uses a separately labeled current/open row
                # with the same exact wrapper-shaped JSONL. The readiness-only
                # wait surface does not reinterpret transcript content; a gone
                # process whose row remains open awaits a terminal commit.
                # Pending evidence is exit 2; exit 3 requires a committed failure.
                real_before = run["jobs"].read_bytes()
                harvest = self.run_parent_surface(
                    [sys.executable, str(HARVEST), "--jobs", str(run["jobs"]),
                     "--attempt-id", run["attempt"], "--status", "done"],
                    run["env"], f"{verdict.lower()}-closed-harvest", expected=0,
                )
                self.assertIn(f"terminal_verdict={verdict}", harvest.stdout)
                self.assertEqual(run["jobs"].read_bytes(), real_before)

                supplemental = self.base / f"supplemental-{verdict.lower()}.jobs.log"
                supplemental_attempt = f"att-supplemental-{verdict.lower()}"
                supplemental_slug = f"supplemental-{verdict.lower()}"
                row = run["target"].replace("\tdone\t", "\topen\t", 1)
                row_fields = row.split("\t")
                row_fields[4] = supplemental_slug
                row_fields[5] = ",".join(
                    f"attempt_id={supplemental_attempt}"
                    if item == f"attempt_id={run['attempt']}" else item
                    for item in row_fields[5].split(",")
                )
                supplemental.write_text("\t".join(row_fields) + "\n")
                py_live = self.run_parent_surface(
                    [sys.executable, str(PY_LIVENESS), str(supplemental)],
                    run["env"], f"{verdict.lower()}-supplemental-python",
                )
                self.assertIn(f"turn.completed {verdict}", py_live.stdout)
                sh_live = self.run_parent_surface(
                    ["bash", str(SH_LIVENESS), str(supplemental)],
                    run["env"], f"{verdict.lower()}-supplemental-shell",
                )
                self.assertIn(f"turn.completed {verdict}", sh_live.stdout)
                wait = self.run_parent_surface(
                    ["sh", str(WAIT), "--jobs", str(supplemental), "--interval", "1", "--max", "5"],
                    run["env"], f"{verdict.lower()}-supplemental-wait", expected=2,
                )
                pending = json.loads(wait.stdout.splitlines()[-1])
                self.assertEqual(pending["state"], "pending")
                self.assertEqual(len(pending["children"]), 1)
                child = pending["children"][0]
                self.assertEqual(child["attempt_id"], supplemental_attempt)
                self.assertEqual(child["status"], "open")
                self.assertEqual(child["readiness"], "pending")
                self.assertEqual(child["reason"], "terminal-commit-pending")
                self.assertEqual(child["required_action"], "complete-open")
                self.assertIn("\topen\t", supplemental.read_text())
                self.assertEqual(run["jobs"].read_bytes(), real_before)

                detailed = self.run_parent_surface(
                    [sys.executable, str(HARVEST), "--jobs", str(run["jobs"]),
                     "--attempt-id", run["attempt"], "--status", "done", "--failure-detail"],
                    run["env"], f"{verdict.lower()}-detail", expected=0,
                )
                detail_fields = fields(detailed.stdout)
                for key in ("blocker_detail_excerpt", "failure_diagnostic_excerpt"):
                    self.assertLessEqual(len(detail_fields[key].encode("utf-8")), 512)
                self.assertEqual(detail_fields["blocker_detail_truncated"], "1")
                self.assertEqual(detail_fields["failure_diagnostic_truncated"], "1")

        # Negative scan is deliberately internal and runs while the deterministic
        # TemporaryDirectory capture tree still exists.
        for name, capture in self.captures:
            for sentinel in RAW_SENTINELS:
                self.assertNotIn(sentinel, capture, f"{sentinel} leaked through {name}")
        print(f"checked_parent_captures={len(self.captures)}")

    def test_same_slug_retries_keep_exact_attempt_logs_and_verdicts(self):
        first = self.wrapper(
            "PASS", slug="conformance-retry", attempt="att-conformance-retry-first",
            run_id="retry-first",
        )
        second = self.wrapper(
            "FAIL", slug="conformance-retry", attempt="att-conformance-retry-second",
            run_id="retry-second",
        )
        self.assertNotEqual(first["log"], second["log"])
        self.assertIn("verdict: PASS", first["log"].read_text())
        self.assertNotIn("verdict: FAIL", first["log"].read_text())
        self.assertIn("verdict: FAIL", second["log"].read_text())

        harvested = self.run_parent_surface(
            [sys.executable, str(HARVEST), "--jobs", str(first["jobs"]),
             "--attempt-id", first["attempt"], "--status", "open"],
            first["env"], "same-slug-first-attempt-harvest", expected=0,
        )
        self.assertIn("terminal_verdict=PASS", harvested.stdout)
        self.assertNotIn("terminal_verdict=FAIL", harvested.stdout)
        for sentinel in RAW_SENTINELS:
            self.assertNotIn(sentinel, harvested.stdout + harvested.stderr)

    def test_fake_claude_baseline_is_log_only_for_all_verdicts(self):
        fake = self.fake_bin / "claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "v=os.environ['FAKE_CLAUDE_VERDICT']\n"
            "print('RAW_COMMAND_SENTINEL')\n"
            "print(f'artifact: -\\nverdict: {v}\\nblocker: '+('none' if v=='PASS' else 'private'))\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        for verdict in ("PASS", "FAIL", "BLOCKED"):
            log = self.base / f"claude-{verdict.lower()}.log"
            with log.open("w") as handle:
                completed = subprocess.run(
                    [str(fake), "-p"], text=True, stdout=handle, stderr=subprocess.STDOUT,
                    env={**os.environ, "FAKE_CLAUDE_VERDICT": verdict},
                )
            self.assertEqual(completed.returncode, 0)
            receipt = f"adapter=claude\nstatus=start\nslug=baseline-{verdict.lower()}\n"
            self.assertIn("RAW_COMMAND_SENTINEL", log.read_text())
            self.assertIn(f"verdict: {verdict}", log.read_text())
            self.assertNotIn("RAW_COMMAND_SENTINEL", receipt)
            self.assertNotIn(f"verdict: {verdict}", receipt)


if __name__ == "__main__":
    unittest.main()
