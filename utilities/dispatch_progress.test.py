#!/usr/bin/env python3
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("progress", ROOT / "utilities/dispatch-progress.py")
P = importlib.util.module_from_spec(spec); spec.loader.exec_module(P)
from tools.fleet import model


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.jobs = self.base / "jobs.log"; self.home = self.base / "home"
        self.proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        start = (Path("/proc") / str(self.proc.pid) / "stat").read_text().split()[21]
        self.proc_start = start
        self.pid_namespace = os.readlink("/proc/self/ns/pid")
        self.attempt = "att-0123456789abcdef"
        self.scope=self.base/"scope";self.scope.mkdir()
        pipe = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,"
            f"capability=code-test,route_id=rt-1,route_node=test,attempt_id={self.attempt},"
            f"pid={self.proc.pid},pid_start={start},pgid={self.proc.pid},"
            f"pid_observer_ns={self.pid_namespace},write_scope={self.scope}"
        )
        self.jobs.write_text(f"2026-07-16T00:00:00Z\topen\t/repo\t{self.base}\tstage\t{pipe}\n")

    def tearDown(self):
        if self.proc.poll() is None: self.proc.kill()
        self.proc.wait(); self.tmp.cleanup()

    def args(self, **kw):
        values = dict(jobs=self.jobs, agent_home=self.home, attempt_id=self.attempt,
                      route_id="rt-1", route_node="test", phase="launch", kind="registry",
                      evidence="pid-annotated", progress_window_seconds=10,
                      watchdog_max_windows=2, apply=False)
        values.update(kw); return type("Args", (), values)()

    def test_warning_then_exact_interrupt(self):
        P.heartbeat(self.args(), 0)
        first = P.watchdog(self.args(), 10)
        self.assertEqual((first["warning"], first["action"]), (1, "warning"))
        second = P.watchdog(self.args(apply=True), 20)
        self.assertEqual(second["terminal_action"], "dead-no-progress")
        self.proc.wait(timeout=3)
        self.assertIn("note=dead-no-progress", self.jobs.read_text())

    def _verification_lease(self, process, *, deadline=100):
        lease = self.base / "verification-leases" / f"{self.attempt}.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        start = P.process_start_ticks(process.pid)
        command = (Path("/proc") / str(process.pid) / "cmdline").read_bytes().rstrip(b"\0")
        lease.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "attempt_id": self.attempt,
                    "route_id": "rt-1",
                    "route_node": "test",
                    "command_digest": hashlib.sha256(command).hexdigest(),
                    "pid": process.pid,
                    "pid_start": start,
                    "pgid": process.pid,
                    "started_at": 0,
                    "deadline": deadline,
                }
            ),
            encoding="utf-8",
        )
        return lease

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait()

    def test_live_bounded_verification_lease_pauses_quiet_windows(self):
        verification = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(self._stop_process, verification)
        self._verification_lease(verification)
        P.heartbeat(self.args(), 0)
        first = P.watchdog(self.args(), 10)
        second = P.watchdog(self.args(), 20)
        self.assertEqual(first["action"], "background-active")
        self.assertEqual(second["quiet_windows"], 0)
        (self.base / "verification-leases" / f"{self.attempt}.json").unlink()
        resumed = P.watchdog(self.args(), 30)
        self.assertEqual((resumed["action"], resumed["quiet_windows"]), ("warning", 1))

    def test_expired_verification_lease_does_not_exempt_stall(self):
        verification = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(self._stop_process, verification)
        self._verification_lease(verification, deadline=5)
        P.heartbeat(self.args(), 0)
        state = P.watchdog(self.args(), 10)
        self.assertEqual((state["action"], state["quiet_windows"]), ("warning", 1))

    def test_foreign_and_digest_mismatched_verification_lease_fail_closed(self):
        verification = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(self._stop_process, verification)
        lease = self._verification_lease(verification)
        P.heartbeat(self.args(), 0)
        value = json.loads(lease.read_text(encoding="utf-8"))
        value["attempt_id"] = "att-foreign"
        lease.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(P.watchdog(self.args(), 10)["action"], "warning")
        P.heartbeat(
            self.args(phase="tool", kind="tool", evidence="digest-round"), 20
        )
        value["attempt_id"] = self.attempt
        value["command_digest"] = "d" * 64
        lease.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(P.watchdog(self.args(), 30)["action"], "observe")
        self.assertEqual(P.watchdog(self.args(), 40)["action"], "warning")

    def test_pid_reused_verification_lease_fails_closed(self):
        verification = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(self._stop_process, verification)
        lease = self._verification_lease(verification)
        value = json.loads(lease.read_text(encoding="utf-8"))
        value["pid_start"] = "1"
        lease.write_text(json.dumps(value), encoding="utf-8")
        P.heartbeat(self.args(), 0)
        self.assertEqual(P.watchdog(self.args(), 10)["action"], "warning")

    def test_checked_verification_helper_registers_and_removes_exact_lease(self):
        helper = ROOT / "utilities/verification-background-lease.py"
        lease = self.base / "verification-leases" / f"{self.attempt}.json"
        runner = subprocess.Popen(
            [
                sys.executable,
                str(helper),
                "--timeout",
                "2",
                "--",
                sys.executable,
                "-c",
                "import time; time.sleep(0.3)",
            ],
            env={
                **os.environ,
                "AGENT_DISPATCH_ATTEMPT_ID": self.attempt,
                "AGENT_DISPATCH_JOBS": str(self.jobs),
            },
        )
        self.addCleanup(self._stop_process, runner)
        for _ in range(100):
            if lease.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(lease.is_file())
        value = json.loads(lease.read_text(encoding="utf-8"))
        self.assertEqual(value["attempt_id"], self.attempt)
        self.assertEqual(value["route_id"], "rt-1")
        self.assertIsNotNone(P.verification_lease(self.args(), time.time()))
        self.assertEqual(runner.wait(timeout=3), 0)
        self.assertFalse(lease.exists())

    def test_speech_is_not_progress_and_heartbeat_resets(self):
        P.heartbeat(self.args(), 0)
        P.watchdog(self.args(), 10)
        (self.home / "speech.log").parent.mkdir(parents=True, exist_ok=True)
        (self.home / "speech.log").write_text("still working")
        self.assertEqual(P.watchdog(self.args(), 20)["quiet_windows"], 2)
        reset = P.heartbeat(self.args(phase="tool", kind="tool", evidence="call-1"), 21)
        self.assertEqual(reset["sequence"], 2)
        self.assertEqual(P.watchdog(self.args(), 21)["quiet_windows"], 0)

    def test_pid_reuse_fails_closed_and_single_classifier(self):
        text = self.jobs.read_text().replace("pid_start=", "pid_start=999")
        self.jobs.write_text(text)
        self.assertEqual(P.inspect(self.args(), 0)["state"], "dead")
        self.assertIs(P.classify_attempt_evidence, model.classify_attempt_evidence)
        self.assertEqual(P.ATTEMPT_CLASSIFIER_SOURCE, model.ATTEMPT_CLASSIFIER_SOURCE)

    def test_signal_denied_fails_closed_without_closing_live_row(self):
        P.heartbeat(self.args(), 0)
        P.watchdog(self.args(), 10)
        with mock.patch.object(P.os, "killpg", side_effect=PermissionError):
            state = P.watchdog(self.args(apply=True), 20)
        self.assertEqual(state["action"], "fail-closed-signal-denied")
        row = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\topen\t", row)
        self.assertNotIn("note=dead-no-progress", row)

    def test_namespace_local_pid_collision_heartbeat_never_grants_signal_authority(self):
        self.jobs.write_text(self.jobs.read_text().replace(
            (
                f"pid={self.proc.pid},pid_start={self.proc_start},pgid={self.proc.pid},"
                f"pid_observer_ns={self.pid_namespace},"
            ),
            (
                f"pid={self.proc.pid},pid_start={self.proc_start},"
                "pid_scope=namespace-local,pid_observer_ns=pid:[inner],"
            ),
        ))
        P.heartbeat(self.args(), 1)
        first = P.watchdog(self.args(), 11)
        self.assertEqual(first["verdict"], "working")
        with mock.patch.object(P.os, "killpg") as killpg:
            state = P.watchdog(self.args(apply=True), 21)
        killpg.assert_not_called()
        self.assertEqual(state["action"], "fail-closed-identity")
        self.assertIsNone(self.proc.poll())
        self.assertIn("\topen\t", self.jobs.read_text())

    def test_namespace_bound_outer_identity_can_interrupt_exact_group(self):
        self.jobs.write_text(self.jobs.read_text().replace(
            (
                f"pid={self.proc.pid},pid_start={self.proc_start},pgid={self.proc.pid},"
                f"pid_observer_ns={self.pid_namespace},"
            ),
            (
                f"pid=7,pid_start={self.proc_start},pid_scope=namespace-local,"
                "pid_observer_ns=pid:[inner],"
                f"pid_host={self.proc.pid},pid_host_start={self.proc_start},"
                f"pid_host_ns={self.pid_namespace},pid_host_proof=nspid-procfs-root-v1,"
                f"pgid={self.proc.pid},pgid_host={self.proc.pid},"
            ),
        ))
        P.heartbeat(self.args(), 0)
        P.watchdog(self.args(), 10)
        state = P.watchdog(self.args(apply=True), 20)
        self.proc.wait(timeout=3)
        self.assertEqual(state["action"], "interrupted")
        self.assertEqual(state["signalled_pid"], self.proc.pid)
        self.assertIn("note=dead-no-progress", self.jobs.read_text())

    def test_pgid_change_during_adjacent_revalidation_sends_no_signal(self):
        P.heartbeat(self.args(), 0)
        P.watchdog(self.args(), 10)
        with mock.patch.object(
            P.os, "getpgid", side_effect=[self.proc.pid, self.proc.pid + 1]
        ), mock.patch.object(P.os, "killpg") as killpg:
            state = P.watchdog(self.args(apply=True), 20)
        killpg.assert_not_called()
        self.assertEqual(state["action"], "fail-closed-identity")
        self.assertEqual(state["process_reason"], "progress-signal-group-unverifiable")
        self.assertIsNone(self.proc.poll())
        self.assertIn("\topen\t", self.jobs.read_text())

    def test_scoped_file_change_resets_quiet_counter(self):
        P.heartbeat(self.args(), 0)
        self.assertEqual(P.watchdog(self.args(), 10)["quiet_windows"], 1)
        (self.scope/"result.txt").write_text("durable result")
        state=P.watchdog(self.args(), 20)
        self.assertEqual(state["quiet_windows"],0)
        self.assertEqual(state["action"],"observe")

    def test_relative_glob_and_route_cycle_output_are_real_progress(self):
        cycle = self.base / "artifacts" / "plans" / "cycle"
        internal = cycle / "_internal"; internal.mkdir(parents=True)
        route = {"nodes": [{"id": "test", "outputs": ["plan.md"]}]}
        route_file = internal / "route.json"; route_file.write_text(__import__("json").dumps(route))
        text = self.jobs.read_text().replace(
            f"write_scope={self.scope}",
            f"write_scope=scope/**,artifact_root={self.base / 'artifacts'},route_file={route_file}",
        )
        self.jobs.write_text(text)
        P.heartbeat(self.args(), 0)
        self.assertEqual(P.watchdog(self.args(), 0)["quiet_windows"], 0)
        self.assertEqual(P.watchdog(self.args(), 10)["quiet_windows"], 1)
        (self.scope / "relative.txt").write_text("progress")
        self.assertEqual(P.watchdog(self.args(), 20)["quiet_windows"], 0)
        self.assertEqual(P.watchdog(self.args(), 30)["quiet_windows"], 1)
        (cycle / "plan.md").write_text("durable plan")
        self.assertEqual(P.watchdog(self.args(), 40)["quiet_windows"], 0)

    def test_delayed_capacity_line_closes_exact_attempt(self):
        P.heartbeat(self.args(), 0)
        P.watchdog(self.args(apply=True), 0)
        logs = self.base / "logs"; logs.mkdir(parents=True)
        (logs / "stage.codex.jsonl").write_text("Selected model is at capacity\n")
        state = P.watchdog(self.args(apply=True), 1)
        self.assertEqual(state["terminal_action"], "dead-capacity")
        self.proc.wait(timeout=3)
        self.assertIn("note=dead-capacity", self.jobs.read_text())

    def test_capacity_scan_is_bound_to_the_exact_attempt_log(self):
        logs = self.base / "logs"; logs.mkdir(parents=True)
        old_log = logs / "stage.att-old.codex.jsonl"
        exact_log = logs / "stage.att-current.codex.jsonl"
        old_log.write_text("Selected model is at capacity\n")
        exact_log.write_text("normal startup\n")
        self.jobs.write_text(self.jobs.read_text().replace(
            f"write_scope={self.scope}", f"write_scope={self.scope},log_file={exact_log}"))
        P.heartbeat(self.args(), 0)
        state = P.watchdog(self.args(apply=True), 0)
        self.assertEqual(state["action"], "observe")
        self.assertIn("\topen\t", self.jobs.read_text())

    def test_natural_process_exit_is_terminal_not_identity_failure(self):
        P.heartbeat(self.args(),0);self.proc.terminate();self.proc.wait(timeout=3)
        state=P.watchdog(self.args(),10)
        self.assertEqual(state["terminal_action"],"process-exited")
        self.assertNotIn("fail-closed",state["action"])

    def test_registry_terminal_is_cached_but_withheld_while_process_drains(self):
        text = self.jobs.read_text().replace("\topen\t", "\tdone\t")
        self.jobs.write_text(text.rstrip("\n") + ",note=completed-marker\n")
        draining = P.watchdog(self.args(), 1)
        self.assertEqual(draining["action"], "draining")
        self.assertEqual(draining["semantic_terminal_action"], "registry-terminal")
        self.assertEqual(draining["terminal_action"], "")
        self.proc.terminate(); self.proc.wait(timeout=3)
        ready = P.watchdog(self.args(), 2)
        self.assertEqual(ready["terminal_action"], "registry-terminal")

    def test_exit_zero_blocked_sandbox_init_closes_exact_attempt(self):
        log = self.base / "stage.attempt.codex.jsonl"
        rows = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {
                "type": "command_execution", "exit_code": 1,
                "aggregated_output": (
                    "bwrap: Can't bind mount /bindfile123 on "
                    "/newroot/work/repo/.codex: Unable to mount source on "
                    "destination: No such file or directory\n"
                ),
            }},
            {"type": "item.completed", "item": {
                "type": "agent_message",
                "text": (
                    "artifact: -\nverdict: BLOCKED\n"
                    "blocker: Runtime sandbox cannot start commands."
                ),
            }},
            {"type": "turn.completed"},
        ]
        log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.jobs.write_text(self.jobs.read_text().replace(
            f"write_scope={self.scope}",
            f"write_scope={self.scope},log_file={log}",
        ))
        P.heartbeat(self.args(), 0)
        self.proc.terminate()
        self.proc.wait(timeout=3)
        state = P.watchdog(self.args(apply=True), 10)
        self.assertEqual(state["terminal_action"], "dead-sandbox-init")
        row = self.jobs.read_text()
        self.assertNotIn("\topen\t", row)
        self.assertIn("note=dead-sandbox-init", row)

    def test_codex_preflight_projects_stage_heartbeat(self):
        command=[str(ROOT/"adapters/codex/bin/preflight.sh"),"stage-heartbeat",
         "--attempt-id",self.attempt,"--route-id","rt-1","--route-node","test",
         "--jobs",str(self.jobs),"--agent-home",str(self.home),"--phase","analysis",
         "--kind","registry","--evidence","analysis-entered"]
        result=subprocess.run(command,text=True,capture_output=True,env={**os.environ,"AGENT_HOME":str(ROOT)})
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(P.inspect(self.args(),1)["classifier_source"],model.ATTEMPT_CLASSIFIER_SOURCE)


if __name__ == "__main__": unittest.main()
