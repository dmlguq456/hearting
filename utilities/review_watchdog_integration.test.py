#!/usr/bin/env python3
"""Short real-process checks for the common review watchdog contract."""

from __future__ import annotations

import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dispatch_lifecycle import begin_finite_watchdog
from review_watchdog import (
    _reap_child,
    _receipt,
    _run_watchdog,
    _timeout_authority,
    launch_review_watchdog,
)


class ReviewWatchdogIntegrationTest(unittest.TestCase):
    def _launch(self, marker: Path, seconds: float = 2.0, lease_release_spec=None, attempt_id="att-watchdog-integration", nonce="a" * 64, jobs=None):
        gate_read, gate_write = os.pipe()
        code = (
            "import os, pathlib; os.read(int(os.environ['REVIEW_GATE']), 1); "
            "pathlib.Path(os.environ['REVIEW_MARKER']).write_text('payload')"
        )
        handle = launch_review_watchdog(
            [sys.executable, "-c", code],
            gate_fd=gate_read,
            budget=begin_finite_watchdog(seconds),
            attempt_id=attempt_id,
            nonce=nonce,
            env={
                **os.environ,
                "REVIEW_GATE": str(gate_read),
                "REVIEW_MARKER": str(marker),
            },
            lease_release_spec=lease_release_spec,
            jobs=jobs,
        )
        os.close(gate_read)
        return handle, gate_write

    @staticmethod
    def _write_timeout_row(jobs: Path, receipt: dict[str, object]) -> None:
        watchdog = receipt["watchdog"]
        child = receipt["child"]
        values = {
            "attempt_id": receipt["attempt_id"], "launch_claimed": "1",
            "review_admission": "prepared", "pid": watchdog["pid"],
            "pid_start": watchdog["pid_start"], "pgid": watchdog["pgid"],
            "pid_ns": watchdog["pid_ns"], "pid_observer_ns": watchdog["pid_observer_ns"],
            "review_fence_pid": child["pid"], "review_fence_pid_start": child["pid_start"],
            "review_fence_pgid": child["pgid"], "review_fence_pid_ns": child["pid_ns"],
            "review_fence_pid_observer_ns": child["pid_observer_ns"],
            "review_watchdog_budget_digest": receipt["budget_digest"],
            "review_readiness_digest": receipt["receipt_digest"],
            "review_governed_lease_nonce": receipt["nonce"],
            "review_governed_lease": "summary-flock-v1",
        }
        jobs.write_text(
            "ts\topen\trepo\tworktree\treview\t" +
            ",".join(f"{key}={value}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )

    def _seed_lease(self, root: Path, cycle: str, attempt: str) -> Path:
        path = root / ".runtime" / "artifact-producer" / "v1" / "review-leases" / cycle / f"{attempt}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "schema_version": 2, "cycle_id": cycle, "attempt_id": attempt,
            "acquired_at": "2026-09-11T00:00:00Z", "deadline": "2099-01-01T00:00:00Z",
            "released_at": None, "expired": False,
        }), encoding="utf-8")
        return path

    def test_ready_is_before_payload_and_commit_does_not_restart_budget(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            handle, gate_write = self._launch(marker, 2.0)
            receipt = handle.read_ready(1.0)
            self.assertEqual(receipt["attempt_id"], "att-watchdog-integration")
            self.assertFalse(marker.exists())
            handle.commit()
            os.close(gate_write)
            self.assertEqual(handle.process.wait(timeout=2), 0)
            self.assertEqual(marker.read_text(), "payload")

    def test_abort_closes_gate_and_reaps_without_payload(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            handle, gate_write = self._launch(marker, 2.0)
            handle.read_ready(1.0)
            handle.abort()
            os.close(gate_write)
            self.assertEqual(handle.process.wait(timeout=2), 125)
            self.assertFalse(marker.exists())

    def test_timeout_before_admission_is_finite_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            jobs = Path(td) / "jobs.log"
            handle, gate_write = self._launch(marker, 0.15, jobs=jobs)
            receipt = handle.read_ready(1.0)
            self._write_timeout_row(jobs, receipt)
            self.assertEqual(handle.process.wait(timeout=2), 124)
            os.close(gate_write)
            self.assertFalse(marker.exists())

    def test_malformed_control_is_abort_not_commit(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            handle, gate_write = self._launch(marker, 2.0)
            handle.read_ready(1.0)
            os.write(handle.control_fd, b"COMMIT\nTRAILING")
            os.close(handle.control_fd)
            handle.control_fd = -1
            os.close(gate_write)
            self.assertEqual(handle.process.wait(timeout=2), 125)
            self.assertFalse(marker.exists())

    def test_control_eof_is_abort_after_reap_proof(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            handle, gate_write = self._launch(marker, 2.0)
            handle.read_ready(1.0)
            handle.close_control()
            os.close(gate_write)
            self.assertEqual(handle.process.wait(timeout=2), 125)
            self.assertFalse(marker.exists())

    def test_child_exit_before_control_does_not_wait_to_budget(self):
        gate_read, gate_write = os.pipe()
        handle = launch_review_watchdog(
            [sys.executable, "-c", "raise SystemExit(7)"],
            gate_fd=gate_read,
            budget=begin_finite_watchdog(2.0),
            attempt_id="att-watchdog-early-exit",
            nonce="b" * 64,
        )
        os.close(gate_read)
        os.close(gate_write)
        self.assertEqual(handle.process.wait(timeout=1), 7)

    def test_signal_exit_preserves_child_signal_at_watchdog_boundary(self):
        for signum in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signum=signum), tempfile.TemporaryDirectory() as td:
                gate_read, gate_write = os.pipe()
                code = (
                    "import os,signal; "
                    f"os.kill(os.getpid(), {int(signum)})"
                )
                handle = launch_review_watchdog(
                    [sys.executable, "-c", code], gate_fd=gate_read,
                    budget=begin_finite_watchdog(2.0),
                    attempt_id=f"att-signal-{int(signum)}", nonce="a" * 64,
                )
                os.close(gate_read)
                os.close(gate_write)
                self.assertEqual(handle.process.wait(timeout=2), -int(signum))

    def test_reap_proof_is_required_for_every_cleanup_failure(self):
        class FakeChild:
            pid = 77
            returncode = None

            def __init__(self, waits):
                self.waits = iter(waits)

            def poll(self):
                return None

            def wait(self, timeout=None):
                result = next(self.waits)
                if isinstance(result, BaseException):
                    raise result
                self.returncode = result
                return result

        meta = {"pid": "77", "pid_start": "start"}
        with mock.patch("review_watchdog.signal_exact_process_group", return_value="error"), \
             mock.patch("review_watchdog.process_group_observation"):
            self.assertFalse(_reap_child(FakeChild([0, 0]), meta))
        with mock.patch("review_watchdog.signal_exact_process_group", side_effect=["signalled", "error"]):
            self.assertFalse(_reap_child(
                FakeChild([subprocess.TimeoutExpired("wait", .1)]), meta,
            ))
        with mock.patch("review_watchdog.signal_exact_process_group", return_value="signalled"):
            timeout = subprocess.TimeoutExpired("wait", .1)
            self.assertFalse(_reap_child(FakeChild([timeout, timeout, timeout]), meta))
        with mock.patch("review_watchdog.signal_exact_process_group", return_value="signalled"), \
             mock.patch("review_watchdog.process_group_observation", return_value=mock.Mock(state="populated")):
            self.assertFalse(_reap_child(FakeChild([0, 0]), meta))

    def _run_with_failed_reap(self, control_token=None):
        readiness_read, readiness_write = os.pipe()
        control_read, control_write = os.pipe()
        gate_read, gate_write = os.pipe()
        child = mock.Mock(pid=77, returncode=None)
        child.poll.return_value = None
        identity = {
            "pid": "123", "pid_start": "watchdog", "pgid": "123",
            "pid_ns": "ns", "pid_observer_ns": "observer",
        }
        child_identity = {
            "pid": "77", "pid_start": "child", "pgid": "77",
            "pid_ns": "ns", "pid_observer_ns": "observer",
        }
        budget = begin_finite_watchdog(.001 if control_token is None else 2.0)
        try:
            if control_token is not None:
                os.write(control_write, control_token)
            with mock.patch("review_watchdog._set_parent_death_signal", return_value=True), \
                 mock.patch("review_watchdog.subprocess.Popen", return_value=child), \
                 mock.patch("review_watchdog._identity", side_effect=[identity, child_identity]), \
                 mock.patch("review_watchdog._reap_child", return_value=False), \
                 mock.patch("review_watchdog._timeout_authority", return_value=True), \
                 mock.patch("review_watchdog._release_review_lease", return_value=True) as releaser:
                result = _run_watchdog(
                    attempt_id="att-failed-reap", budget=budget,
                    readiness_fd=readiness_write, control_fd=control_read,
                    gate_fd=gate_read, child_argv=["unused"], nonce="b" * 64,
                )
                releaser.assert_not_called()
            return result
        finally:
            for fd in (readiness_read, control_write, gate_write):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_readiness_failure_releases_lease_only_after_cleanup_proof(self):
        for reaped in (False, True):
            with self.subTest(reaped=reaped):
                ready_read, ready_write = os.pipe()
                control_read, control_write = os.pipe()
                gate_read, gate_write = os.pipe()
                identity = {"pid": "77", "pid_start": "123", "pgid": "77"}
                calls = []
                try:
                    with mock.patch("review_watchdog._identity", return_value=identity), \
                         mock.patch("review_watchdog.subprocess.Popen", return_value=mock.Mock(pid=77)), \
                         mock.patch("review_watchdog._write_once", side_effect=BrokenPipeError), \
                         mock.patch("review_watchdog._reap_child", side_effect=lambda *a, **k: calls.append("reap") or reaped), \
                         mock.patch("review_watchdog._release_review_lease", side_effect=lambda *a: calls.append("release") or True):
                        result = _run_watchdog(
                            attempt_id="att-readiness-failure", budget=begin_finite_watchdog(2),
                            readiness_fd=ready_write, control_fd=control_read, gate_fd=gate_read,
                            child_argv=["unused"], nonce="a" * 64,
                        )
                    self.assertEqual(result, 70 if reaped else 126)
                    self.assertEqual(calls, ["reap", "release"] if reaped else ["reap"])
                finally:
                    for fd in (ready_read, control_write, gate_write):
                        os.close(fd)

    def test_failed_reap_never_returns_timeout_or_abort(self):
        self.assertEqual(self._run_with_failed_reap(), 126)
        self.assertEqual(self._run_with_failed_reap(b"ABORT\n"), 126)

    def test_budget_rejects_contradictory_deadline_and_preserves_origin(self):
        budget = begin_finite_watchdog(1.25, origin_monotonic_ns=100, origin_epoch=200.0)
        self.assertEqual(budget.deadline_monotonic_ns, 1_250_000_100)
        with self.assertRaises(ValueError):
            type(budget)(1.25, 100, 1_250_000_101, 200.0)
        with self.assertRaises(ValueError):
            type(budget)(1.25, 100, 1_250_000_100, float("nan"))

    def test_timeout_reaps_child_and_releases_exact_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "marker"
            lease = self._seed_lease(root, "cyc-timeout", "att-timeout")
            gate_read, gate_write = os.pipe()
            code = "import os,time; os.read(int(os.environ['REVIEW_GATE']),1); time.sleep(5)"
            handle = launch_review_watchdog(
                [sys.executable, "-c", code], gate_fd=gate_read,
                budget=begin_finite_watchdog(.2), attempt_id="att-timeout",
                nonce="c" * 64,
                env={**os.environ, "REVIEW_GATE": str(gate_read)},
                lease_release_spec={"root": str(root), "cycle_id": "cyc-timeout", "attempt_id": "att-timeout"},
                jobs=root / "jobs.log",
            )
            os.close(gate_read)
            receipt = handle.read_ready(1.0)
            self._write_timeout_row(root / "jobs.log", receipt)
            handle.commit()
            os.close(gate_write)
            self.assertEqual(handle.process.wait(timeout=3), 124)
            self.assertIsNotNone(json.loads(lease.read_text(encoding="utf-8"))["released_at"])

    def test_normal_exit_releases_exact_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lease = self._seed_lease(root, "cyc-normal", "att-normal")
            handle, gate_write = self._launch(
                root / "marker", 2.0,
                lease_release_spec={"root": str(root), "cycle_id": "cyc-normal", "attempt_id": "att-normal"},
                attempt_id="att-normal",
            )
            handle.read_ready(1.0)
            handle.commit()
            os.close(gate_write)
            handle.process.wait(timeout=2)
            self.assertIsNotNone(json.loads(lease.read_text(encoding="utf-8"))["released_at"])

    def test_timeout_identity_mutation_is_no_signal(self):
        budget = begin_finite_watchdog(1.0, origin_monotonic_ns=100, origin_epoch=200.0)
        receipt = {"budget_digest": budget.digest, "nonce": "a" * 64}
        self.assertFalse(_timeout_authority(
            {"pid": "1", "pid_start": "x", "pgid": "1", "pid_ns": "n", "pid_observer_ns": "n"},
            {"pid": "2", "pid_start": "y", "pgid": "2", "pid_ns": "n", "pid_observer_ns": "n"},
            receipt, budget,
        ))

    def test_nonce_is_required_exactly_and_ready_reuses_caller_nonce(self):
        with tempfile.TemporaryDirectory() as td:
            gate_read, gate_write = os.pipe()
            try:
                with self.assertRaises(ValueError):
                    launch_review_watchdog(
                        [sys.executable, "-c", "pass"], gate_fd=gate_read,
                        budget=begin_finite_watchdog(1),
                        attempt_id="att-nonce", nonce="short",
                    )
                handle = launch_review_watchdog(
                    [sys.executable, "-c", "import os; os.read(int(os.environ['G']), 1)"],
                    gate_fd=gate_read, budget=begin_finite_watchdog(1),
                    attempt_id="att-nonce", nonce="d" * 64,
                    env={**os.environ, "G": str(gate_read)},
                )
                receipt = handle.read_ready(1)
                self.assertEqual(receipt["nonce"], "d" * 64)
                handle.abort()
                os.close(gate_write)
                handle.process.wait(timeout=2)
                mismatch_read, mismatch_write = os.pipe()
                mismatch = launch_review_watchdog(
                    [sys.executable, "-c", "import os; os.read(int(os.environ['G']), 1)"],
                    gate_fd=mismatch_read, budget=begin_finite_watchdog(1),
                    attempt_id="att-nonce-mismatch", nonce="d" * 64,
                    env={**os.environ, "G": str(mismatch_read)},
                )
                os.close(mismatch_read)
                mismatch.nonce = "e" * 64
                with self.assertRaises(ValueError):
                    mismatch.read_ready(1)
                mismatch.abort()
                os.close(mismatch_write)
                mismatch.process.wait(timeout=2)
            finally:
                for fd in (gate_read, gate_write):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_timeout_authority_requires_complete_unchanged_prepared_row(self):
        budget = begin_finite_watchdog(1.0, origin_monotonic_ns=100, origin_epoch=200.0)
        identity = {"pid": str(os.getpid()), "pid_start": "start", "pgid": str(os.getpid()), "pid_ns": "ns", "pid_observer_ns": "observer"}
        child = dict(identity)
        child["pid_start"] = "child-start"
        receipt = _receipt("att-row", identity, child, budget, "e" * 64)
        required = {
            "attempt_id": "att-row", "launch_claimed": "1", "review_admission": "prepared",
            "pid": identity["pid"], "pid_start": identity["pid_start"], "pgid": identity["pgid"],
            "pid_ns": identity["pid_ns"], "pid_observer_ns": identity["pid_observer_ns"],
            "review_fence_pid": child["pid"], "review_fence_pid_start": child["pid_start"],
            "review_fence_pgid": child["pgid"], "review_fence_pid_ns": child["pid_ns"],
            "review_fence_pid_observer_ns": child["pid_observer_ns"],
            "review_watchdog_budget_digest": budget.digest,
            "review_readiness_digest": receipt["receipt_digest"],
            "review_governed_lease_nonce": "e" * 64,
            "review_governed_lease": "summary-flock-v1",
        }
        row = "ts\topen\trepo\tworktree\treview\t" + ",".join(f"{k}={v}" for k, v in required.items())
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(row + "\n", encoding="utf-8")
            with mock.patch("review_watchdog._identity", side_effect=[identity, child]):
                self.assertTrue(_timeout_authority(identity, child, receipt, budget, jobs))
            for key in required:
                missing = dict(required)
                missing.pop(key)
                jobs.write_text("ts\topen\trepo\tworktree\treview\t" + ",".join(f"{k}={v}" for k, v in missing.items()) + "\n", encoding="utf-8")
                with mock.patch("review_watchdog._identity", side_effect=[identity, child]):
                    self.assertFalse(_timeout_authority(identity, child, receipt, budget, jobs), key)
            changed = dict(required, review_readiness_digest="sha256:changed")
            jobs.write_text("ts\topen\trepo\tworktree\treview\t" + ",".join(f"{k}={v}" for k, v in changed.items()) + "\n", encoding="utf-8")
            with mock.patch("review_watchdog._identity", side_effect=[identity, child]), \
                 mock.patch("review_watchdog.signal_exact_process_group") as signaler:
                self.assertFalse(_timeout_authority(identity, child, receipt, budget, jobs))
            signaler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
