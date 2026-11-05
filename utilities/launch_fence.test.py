#!/usr/bin/env python3

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from dispatch_contract import parse_registry_metadata, process_launch_identity
from unittest import mock
from split_module_fixture import patch_split


FENCE = Path(__file__).with_name("launch-fence.py")
SPEC = importlib.util.spec_from_file_location("launch_fence", FENCE)
L = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = L
SPEC.loader.exec_module(L)


class LaunchFenceTest(unittest.TestCase):
    def test_runtime_root_mismatch_is_reported_without_payload_exec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            route_path = Path(temp_dir, "route.json")
            route_path.write_text("{}", encoding="utf-8")
            gate_read, gate_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            failure_read, failure_write = os.pipe()
            try:
                with mock.patch.object(L, "set_parent_death_signal"), \
                     mock.patch.object(L.os, "getppid", return_value=123), \
                     patch_split(L.ROUTE, "verify_route", return_value={}), \
                     mock.patch.object(
                         L.ROUTE, "revalidate_launch_compatibility",
                         return_value=(False, {
                             "runtime_root": {"expected": "sealed", "observed": "drifted"}
                         }),
                     ), \
                     mock.patch.object(L, "mark_attempt_launch_started") as mark, \
                     mock.patch.object(L.os, "execvpe") as execvpe:
                    result = L.cli([
                        "--parent-pid", "123", "--gate-fd", str(gate_read),
                        "--failure-fd", str(failure_write),
                        "--jobs", str(Path(temp_dir, "jobs.log")),
                        "--attempt-id", "att-fence-drift",
                        "--route-file", str(route_path),
                        "--launch-phase", "start", "--", "payload",
                    ])
                failure_write = -1
                self.assertEqual(result, 70)
                failure = json.loads(os.read(failure_read, 16384))
                self.assertEqual(failure["schema_version"], 1)
                self.assertEqual(failure["reason"], "launch-runtime-root-mismatch")
                self.assertIn("runtime_root", failure["detail"])
                mark.assert_not_called()
                execvpe.assert_not_called()
            finally:
                os.close(failure_read)
                if failure_write >= 0:
                    os.close(failure_write)

    def test_malformed_runtime_root_preserves_failure_receipt_and_zero_exec(self):
        for runtime in (7, [], None, {"path": 7}, {"path": []}, {"path": "relative/root"}):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as td:
                route = {"launch_compatibility_tuple": {"runtime_root": runtime}}
                route_path = Path(td, "route.json")
                route_path.write_text(json.dumps(route), encoding="utf-8")
                gate_read, gate_write = os.pipe()
                os.write(gate_write, b"1")
                os.close(gate_write)
                failure_read, failure_write = os.pipe()
                try:
                    with mock.patch.object(L, "set_parent_death_signal"), \
                         mock.patch.object(L.os, "getppid", return_value=123), \
                         patch_split(L.ROUTE, "verify_route", return_value=route), \
                         mock.patch.object(L.ROUTE, "revalidate_launch_compatibility",
                                           return_value=(False, {"runtime_root": {"expected": runtime}})), \
                         mock.patch.object(L, "mark_attempt_launch_started") as mark, \
                         mock.patch.object(L.os, "execvpe") as payload:
                        result = L.cli([
                            "--parent-pid", "123", "--gate-fd", str(gate_read),
                            "--failure-fd", str(failure_write), "--jobs", str(Path(td, "jobs.log")),
                            "--attempt-id", "att-malformed-root", "--route-file", str(route_path),
                            "--launch-phase", "start", "--", "payload",
                        ])
                    failure_write = -1
                    self.assertEqual(result, 70)
                    failure = json.loads(os.read(failure_read, 16384))
                    self.assertEqual(failure["reason"], "launch-runtime-root-mismatch")
                    detail = json.loads(failure["detail"].split(" ", 1)[1])
                    self.assertIn("hearting/current", detail["recovery"])
                    mark.assert_not_called()
                    payload.assert_not_called()
                finally:
                    os.close(failure_read)
                    if failure_write >= 0:
                        os.close(failure_write)

    def test_launch_tuple_absent_and_incompatible_never_commit_or_exec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            route_path = Path(temp_dir, "route.json")
            route_path.write_text("{}", encoding="utf-8")
            for case, compatibility, reason in (
                ("absent", (True, {"tuple": "absent-legacy"}),
                 "launch-compatibility-tuple-required"),
                ("incompatible", (False, {"runtime_root": {"expected": "a", "observed": "b"}}),
                 "launch-runtime-root-mismatch"),
            ):
                with self.subTest(case=case), \
                     mock.patch.object(L, "set_parent_death_signal"), \
                     mock.patch.object(L.os, "getppid", return_value=123), \
                     mock.patch.object(L.os, "read", return_value=b"1"), \
                     mock.patch.object(L.os, "close"), \
                     patch_split(L.ROUTE, "verify_route", return_value={}), \
                     mock.patch.object(
                         L.ROUTE, "revalidate_launch_compatibility",
                         return_value=compatibility,
                     ), \
                     mock.patch.object(L, "mark_attempt_launch_started") as mark, \
                     mock.patch.object(L.os, "execvpe") as execvpe:
                    with self.assertRaisesRegex(ValueError, reason):
                        L.main([
                            "--parent-pid", "123", "--gate-fd", "9",
                            "--jobs", str(Path(temp_dir, "jobs.log")),
                            "--attempt-id", f"att-{case}",
                            "--route-file", str(route_path),
                            "--launch-phase", "start", "--", "payload",
                        ])
                mark.assert_not_called()
                execvpe.assert_not_called()

    def test_eof_before_publication_executes_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir, "marker")
            read_fd, write_fd = os.pipe()
            proc = subprocess.Popen(
                [
                    sys.executable, str(FENCE),
                    "--parent-pid", str(os.getpid()),
                    "--gate-fd", str(read_fd),
                    "--", sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
                ],
                pass_fds=(read_fd,),
            )
            os.close(read_fd)
            os.close(write_fd)
            self.assertEqual(proc.wait(timeout=5), 70)
            self.assertFalse(marker.exists())

    def test_release_executes_command_with_same_process_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir, "marker")
            read_fd, write_fd = os.pipe()
            proc = subprocess.Popen(
                [
                    sys.executable, str(FENCE),
                    "--parent-pid", str(os.getpid()),
                    "--gate-fd", str(read_fd),
                    "--", sys.executable, "-c",
                    (
                        "import os; from pathlib import Path; "
                        f"Path({str(marker)!r}).write_text(str(os.getpid()))"
                    ),
                ],
                pass_fds=(read_fd,),
            )
            os.close(read_fd)
            os.write(write_fd, b"1")
            os.close(write_fd)
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(marker.read_text(), str(proc.pid))

    def test_registry_bound_release_marks_started_before_payload_exec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            marker = base / "marker"
            jobs = base / "jobs.log"
            attempt = "att-fence-registry"
            read_fd, write_fd = os.pipe()
            proc = subprocess.Popen(
                [
                    sys.executable, str(FENCE),
                    "--parent-pid", str(os.getpid()),
                    "--gate-fd", str(read_fd),
                    "--jobs", str(jobs), "--attempt-id", attempt,
                    "--", sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
                ],
                pass_fds=(read_fd,),
                start_new_session=True,
            )
            os.close(read_fd)
            identity = process_launch_identity(proc.pid)
            metadata = (
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,"
                f"attempt_id={attempt},launch_claimed=1,launch_fence=registry-v1,"
                + ",".join(f"{key}={value}" for key, value in identity.items())
            )
            jobs.write_text(
                f"2026-07-24T00:00:00Z\topen\t/repo\t/wt\tworker\t{metadata}\n",
                encoding="utf-8",
            )
            os.write(write_fd, b"1")
            os.close(write_fd)
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(marker.read_text(), "ok")
            row = jobs.read_text(encoding="utf-8").strip().split("\t", 5)[5]
            self.assertEqual(
                parse_registry_metadata(row)["launch_started"], "1"
            )

    def test_failed_registry_launch_persists_original_error_without_worker_log(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                jobs = base / "jobs.log"
                attempt = f"att-failed-fence-{harness}"
                log = base / "nested" / "logs" / f"worker.{attempt}.{harness}.jsonl"
                marker = base / "payload-ran"
                gate_read, gate_write = os.pipe()
                failure_read, failure_write = os.pipe()
                proc = subprocess.Popen([
                    sys.executable, str(FENCE), "--parent-pid", str(os.getpid()),
                    "--gate-fd", str(gate_read), "--failure-fd", str(failure_write),
                    "--jobs", str(jobs), "--attempt-id", attempt, "--",
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ], pass_fds=(gate_read, failure_write), start_new_session=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                os.close(gate_read)
                os.close(failure_write)
                self.addCleanup(lambda proc=proc: proc.poll() is None and proc.kill())
                try:
                    identity = process_launch_identity(proc.pid)
                    metadata = (
                        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                        "execution_surface=registered-headless,registered_worker=1,"
                        "fallback_hop=same-harness-headless,"
                        f"attempt_id={attempt},harness={harness},log_file={log},"
                        "launch_claimed=1,launch_fence=registry-v1,"
                        "model_source=profile+diversity,model_source=profile+diversity,"
                        + ",".join(f"{key}={value}" for key, value in identity.items()))
                    before = f"2026-10-25T00:00:00Z\topen\t/repo\t/wt\tworker\t{metadata}\n"
                    jobs.write_text(before)
                    os.write(gate_write, b"1")
                    os.close(gate_write)
                    gate_write = -1
                    stdout, stderr = proc.communicate(timeout=10)
                    failure = json.loads(os.read(failure_read, 16384))
                    self.assertEqual(proc.returncode, 70)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "launch-fence: model_source\n")
                    self.assertEqual(failure["detail"], "model_source")
                    saved = log.with_suffix(".launch-fence.txt").read_text()
                    self.assertIn("reason=attempt-immutable-metadata-duplicate", saved)
                    self.assertIn("Traceback (most recent call last)", saved)
                    self.assertIn("dispatch_contract.DispatchContractError: model_source", saved)
                    self.assertLessEqual(len(saved.encode()), 65536 + 168)
                    self.assertFalse(log.exists())
                    self.assertFalse(marker.exists())
                    self.assertEqual(jobs.read_text(), before)
                finally:
                    if gate_write >= 0:
                        os.close(gate_write)
                    os.close(failure_read)

    def test_unexpected_fence_exception_is_saved_and_reraised_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td, "jobs.log")
            log = Path(td, "logs", "worker.att-unexpected.codex.jsonl")
            jobs.write_text(f"now\topen\t/repo\t/wt\tworker\t"
                            f"attempt_id=att-unexpected,harness=codex,log_file={log}\n")
            error = RuntimeError("unexpected fence failure " + "한" * 100000 + "END")
            with mock.patch.object(L, "main", side_effect=error), \
                 self.assertRaises(RuntimeError) as caught:
                L.cli(["--jobs", str(jobs), "--attempt-id", "att-unexpected"])
            self.assertIs(caught.exception, error)
            saved = log.with_suffix(".launch-fence.txt").read_text()
            self.assertLessEqual(len(saved.encode()), 65536)
            self.assertTrue(saved.endswith("END\n"))
            self.assertFalse(log.exists())

    def test_unwritable_diagnostics_do_not_change_fence_failure_channel(self):
        import dispatch_launch_evidence as evidence
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td, "jobs.log")
            log = Path(td, "logs", "worker.att-fail.codex.jsonl")
            jobs.write_text(f"now\topen\t/repo\t/wt\tworker\t"
                            f"attempt_id=att-fail,harness=codex,log_file={log}\n")
            failure_read, failure_write = os.pipe()
            try:
                with mock.patch.object(L, "main", side_effect=ValueError("original failure")), \
                     mock.patch.object(evidence, "_write", side_effect=PermissionError("read-only")), \
                     mock.patch("sys.stderr"):
                    result = L.cli(["--jobs", str(jobs), "--attempt-id", "att-fail",
                                    "--failure-fd", str(failure_write)])
                failure_write = -1
                self.assertEqual(result, 70)
                self.assertEqual(json.loads(os.read(failure_read, 16384))["detail"],
                                 "original failure")
            finally:
                os.close(failure_read)
                if failure_write >= 0:
                    os.close(failure_write)

    def test_foreground_release_retains_selected_parent_death_signal(self):
        with mock.patch.object(L, "set_parent_death_signal") as pdeath, \
             mock.patch.object(L.os, "getppid", return_value=123), \
             mock.patch.object(L.os, "read", return_value=b"1"), \
             mock.patch.object(L.os, "close"), \
             mock.patch.object(L.os, "execvpe") as execvpe:
            result = L.main([
                "--parent-pid", "123", "--gate-fd", "9",
                "--post-release-parent-death-signal", "term",
                "--", "payload",
            ])
        self.assertEqual(result, 70)
        self.assertEqual(
            pdeath.call_args_list,
            [mock.call(signal.SIGKILL), mock.call(signal.SIGTERM)],
        )
        execvpe.assert_called_once()

    def test_parent_loss_during_post_release_arm_never_executes(self):
        with mock.patch.object(L, "set_parent_death_signal") as pdeath, \
             mock.patch.object(L.os, "getppid", side_effect=(123, 1)), \
             mock.patch.object(L.os, "read", return_value=b"1"), \
             mock.patch.object(L.os, "close"), \
             mock.patch.object(L.os, "execvpe") as execvpe:
            result = L.main([
                "--parent-pid", "123", "--gate-fd", "9",
                "--post-release-parent-death-signal", "kill",
                "--", "payload",
            ])
        self.assertEqual(result, 70)
        self.assertEqual(
            pdeath.call_args_list,
            [mock.call(signal.SIGKILL), mock.call(signal.SIGKILL)],
        )
        execvpe.assert_not_called()

    def test_detached_disarms_parent_death_before_launch_started_commit(self):
        events = []
        with mock.patch.object(
            L, "set_parent_death_signal",
            side_effect=lambda signum: events.append(("pdeath", signum)),
        ), mock.patch.object(L.os, "getppid", return_value=123), \
             mock.patch.object(L.os, "read", return_value=b"1"), \
             mock.patch.object(L.os, "close"), \
             mock.patch.object(
                 L, "mark_attempt_launch_started",
                 side_effect=lambda *_args: events.append(("started",)),
             ), mock.patch.object(
                 L.os, "execvpe",
                 side_effect=lambda *_args: events.append(("exec",)),
             ):
            result = L.main([
                "--parent-pid", "123", "--gate-fd", "9",
                "--jobs", "/tmp/jobs.log", "--attempt-id", "att-order",
                "--post-release-parent-death-signal", "none",
                "--", "payload",
            ])
        self.assertEqual(result, 70)
        self.assertEqual(events, [
            ("pdeath", signal.SIGKILL),
            ("pdeath", 0),
            ("started",),
            ("exec",),
        ])


if __name__ == "__main__":
    unittest.main()
