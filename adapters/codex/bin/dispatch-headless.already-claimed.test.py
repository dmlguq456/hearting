#!/usr/bin/env python3
"""A start that loses the launch-claim race reports the earlier attempt's state."""
import importlib.util
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402

NOTE_DEAD = "the earlier attempt is no longer running; run the same --start again and a replacement starts"
NOTE_UNVERIFIED = ("this place cannot see whether the earlier attempt is still running; the runtime "
                   "checks it and replaces it if it died — end the turn and wait")


class AlreadyClaimedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # The harness CLI only has to exist on PATH: the launch itself is mocked,
        # and CI runners have no codex binary installed.
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        (self.fakebin / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.fakebin / "codex").chmod(0o755)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.art = self.root / ".agent_reports"
        self.art.mkdir()
        self.jobs = self.root / "jobs.log"
        self.parent = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (self.parent.kill(), self.parent.wait()))
        start = (Path("/proc") / str(self.parent.pid) / "stat").read_text().split()[21]
        self.jobs.write_text(
            f"2026-07-23T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,harness=codex,"
            f"runtime_sandbox=fixture,attempt_id=att-parent-fixture,pid={self.parent.pid},pid_start={start}\n")
        spec = importlib.util.spec_from_file_location(
            "codex_already_claimed", ROOT / "adapters/codex/bin/dispatch-headless.py")
        self.wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wrapper)

    def invoke(self, action, claimed_elsewhere=False):
        argv = ["dispatch-headless.py", f"--{action}", "--worktree", str(self.repo), "--slug", "codex-claimed",
                "--capability", "autopilot-code", "--capability-mode", "dev", "--worker-mode", "dev/backend",
                "--intensity", "standard", "--dispatch-depth", "2", "--parent", "owner",
                "--worker-role", "code-plan", "--owner", "autopilot-code", "--jobs", str(self.jobs),
                "--log-dir", str(self.root / "logs"), "--attempt-id", "att-codex-claimed-0001",
                "--parent-harness", "codex", "--parent-transport", "headless", "--parent-sandbox", "fixture",
                "--launch-authority", "conductor", "--nested-eligibility", "supported",
                "--eligibility-source", "codex-fixture", "--fallback-ordinal", "1",
                "--model", "gpt-test", "--reasoning", "low"]
        env = {"PATH": str(self.fakebin) + os.pathsep + os.environ.get("PATH", ""), "HOME": str(self.root), "AGENT_HOME": str(ROOT),
               "AGENT_ARTIFACT_ROOT": str(self.art), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture", "XDG_STATE_HOME": str(self.root / "state")}
        lost_claim = self.wrapper.DispatchContractError(
            "attempt-launch-already-claimed", "att-codex-claimed-0001")
        stream = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(stream), \
                mock.patch.object(self.wrapper, "check_runtime_projection", return_value=0), \
                mock.patch.object(self.wrapper, "ensure_runtime_home_projection", return_value=None), \
                mock.patch.object(self.wrapper, "spawn_claimed_attempt",
                                  side_effect=lost_claim if claimed_elsewhere else None):
            code = self.wrapper.main(argv)
        return code, stream.getvalue()

    def test_duplicate_claim_reports_state_reason_and_plain_note(self):
        code, first = self.invoke("register")
        self.assertEqual(code, 0, first)
        self.assertIn("registered=1", first)
        for state, reason, note in (
                ("existing-active", "process-live", None),
                ("existing-dead", "namespace-extinct", NOTE_DEAD),
                ("existing-unverified", "process-unverifiable", NOTE_UNVERIFIED),
                ("existing-completed", "attempt-closed", None)):
            with self.subTest(state=state), patch_split(
                    self.wrapper, "existing_attempt_launch_state", return_value=(state, reason)):
                code, out = self.invoke("start", claimed_elsewhere=True)
                lines = out.splitlines()
                self.assertEqual(code, 0, out)
                self.assertIn("status=start", lines)
                for line in ("duplicate_attempt=1", f"launch_state={state}", "registered=0",
                             "started=0", "child_spawned=0", f"reason={reason}"):
                    self.assertIn(line, lines)
                notes = [ln for ln in lines if ln.startswith("note=")]
                self.assertEqual(notes, [f"note={note}"] if note else [])


if __name__ == "__main__":
    unittest.main()
