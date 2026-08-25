#!/usr/bin/env python3
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
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as D

WATCH = ROOT / "utilities" / "dispatch-reap-watch.py"
CURRENT = (
    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=stage"
)
OWNER = (
    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=owner"
)


def _row(status, repo, worktree, slug, meta_str):
    return f"2026-08-13T00:00:00Z\t{status}\t{repo}\t{worktree}\t{slug}\t{meta_str}"


def _registry_with_parent_and_leg(
    base, parent_proc, leg_proc, *, parent_open=True, foreign=None
):
    """Write a two-row registry sharing worktree/repo/slug and parent linkage.

    `foreign` selects one deliberately-broken parent axis for the
    not-a-completion-window fixtures: "worktree", "pid_start", or None
    (leaves ``parent_open`` alone to model a plain terminal parent row).
    """

    parent_identity = D.process_launch_identity(parent_proc.pid)
    leg_identity = D.process_launch_identity(leg_proc.pid)
    repo, worktree, slug = "/repo", "/wt", "owner"

    parent_meta = dict(parent_identity)
    parent_meta["attempt_id"] = "att-parent"
    parent_meta["launch_lifecycle"] = "registered"
    if foreign == "pid_start":
        parent_meta["pid_start"] = "1"

    leg_meta = dict(leg_identity)
    leg_meta["attempt_id"] = "att-leg"
    leg_meta["launch_lifecycle"] = "detached"
    leg_meta["parent"] = slug
    leg_meta["parent_attempt_id"] = "att-parent"
    leg_meta["log_file"] = str(base / "missing.jsonl")

    parent_worktree = "/wt-foreign" if foreign == "worktree" else worktree
    parent_status = "open" if parent_open else "done"

    jobs = base / "jobs.log"
    jobs.write_text(
        _row(
            parent_status,
            repo,
            parent_worktree,
            slug,
            OWNER + "," + ",".join(f"{k}={v}" for k, v in parent_meta.items()),
        )
        + "\n"
        + _row(
            "open",
            repo,
            worktree,
            "leg",
            CURRENT + "," + ",".join(f"{k}={v}" for k, v in leg_meta.items()),
        )
        + "\n",
        encoding="utf-8",
    )
    return jobs


class DispatchReapWatchTest(unittest.TestCase):
    def test_launcher_strips_attempt_tag_from_observer_environment(self):
        fake = mock.Mock(pid=37)
        with mock.patch.dict(
            os.environ, {D.ATTEMPT_DESCENDANT_ENV: "att-observer-self"}
        ), mock.patch.object(D.subprocess, "Popen", return_value=fake) as spawn:
            self.assertEqual(
                D.launch_reap_watch(
                    Path("/tmp/jobs.log"), "att-observer-self", 19, "200", 19
                ),
                37,
            )
        self.assertNotIn(D.ATTEMPT_DESCENDANT_ENV, spawn.call_args.kwargs["env"])

    def test_detached_watcher_waits_for_escaped_tagged_child_and_seals_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-detached-reap-watch"
            script = (
                "import os,subprocess,sys,time\n"
                "env=dict(os.environ,AGENT_DISPATCH_ATTEMPT_ID=sys.argv[1])\n"
                "subprocess.Popen(['sleep','0.45'],env=env,start_new_session=True)\n"
                "time.sleep(0.04)\n"
            )
            env = dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID=attempt)
            worker = subprocess.Popen(
                [sys.executable, "-c", script, attempt],
                env=env,
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\tdone\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata},note=completed-marker\n",
                encoding="utf-8",
            )
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ]
            )
            worker.wait(timeout=5)
            time.sleep(0.08)
            self.assertIsNone(watcher.poll(), "watcher ignored escaped descendant")
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("launch_outcome=governed-process-group-drained", row)
            self.assertIn(f"group_reap_proof={D.GROUP_REAP_PROOF}", row)
            self.assertIn(f"group_reap_pgid={identity['pgid']}", row)
            self.assertIn(
                f"attempt_descendant_proof={D.ATTEMPT_DESCENDANT_PROOF}", row
            )
            self.assertIn(
                f"attempt_descendant_observer_ns={identity['pid_observer_ns']}", row
            )
            self.assertFalse((base / "degradations").exists())

    def test_open_missing_result_is_closed_after_exact_group_drain(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-detached-missing-result"
            worker = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(base / "missing.jsonl"),
                    "route_id": "rt-reap-leg",
                    "route_hash": "sha256:reap-leg",
                    "route_node": "impl-review-alternative",
                    "fallback_ordinal": "1",
                    "parallel_group": "impl-review",
                    "batch_declared_size": "2",
                    "batch_parallel_leg_index": "1",
                    "harness": "codex",
                    "leg_class": "peer",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata}\n",
                encoding="utf-8",
            )
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ],
            )
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("\tdone\t", row)
            self.assertIn("note=dead-missing-result", row)
            self.assertIn("dispatch-reap-missing-result-v1", row)
            ledger = base / "degradations" / "rt-reap-leg.jsonl"
            with ledger.open(encoding="utf-8") as stream:
                events = [json.loads(line) for line in stream]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["writer"], "dispatch-reap-watch.py")
            self.assertEqual(events[0]["kind"], "leg-failure")
            self.assertEqual(events[0]["route_id"], "rt-reap-leg")
            self.assertEqual(events[0]["route_hash"], "sha256:reap-leg")
            self.assertEqual(events[0]["route_node"], "impl-review-alternative")
            self.assertEqual(events[0]["parallel_group"], "impl-review")
            self.assertEqual(events[0]["parallel_leg_index"], "1")
            self.assertEqual(events[0]["parallel_leg_count"], "2")
            self.assertEqual(events[0]["attempt_id"], attempt)
            self.assertEqual(events[0]["reason"], "dead-missing-result")

            replay = subprocess.run(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ],
                check=False,
            )
            self.assertEqual(replay.returncode, 0)
            with ledger.open(encoding="utf-8") as stream:
                replayed_events = [json.loads(line) for line in stream]
            self.assertEqual(replayed_events, events)

    def test_ledger_failure_does_not_change_missing_result_close(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-degradation-write-failure"
            worker = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(base / "missing.jsonl"),
                    "route_id": "rt-ledger-failure",
                    "route_hash": "sha256:ledger-failure",
                    "route_node": "test-alternative",
                    "parallel_group": "test",
                    "batch_declared_size": "2",
                    "batch_parallel_leg_index": "1",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata}\n",
                encoding="utf-8",
            )
            (base / "degradations").write_text("not-a-directory", encoding="utf-8")
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ]
            )
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("\tdone\t", row)
            self.assertIn("note=dead-missing-result", row)
            self.assertIn("dispatch-reap-missing-result-v1", row)
            self.assertEqual(
                (base / "degradations").read_text(encoding="utf-8"),
                "not-a-directory",
            )

    def test_live_parent_defers_missing_result_close(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = subprocess.Popen(["sleep", "5"])
            leg = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
                start_new_session=True,
            )
            try:
                jobs = _registry_with_parent_and_leg(base, parent, leg)
                leg_identity = D.process_launch_identity(leg.pid)
                watcher = subprocess.Popen(
                    [
                        sys.executable,
                        str(WATCH),
                        "--jobs", str(jobs),
                        "--attempt-id", "att-leg",
                        "--pid", str(leg.pid),
                        "--pid-start", leg_identity["pid_start"],
                        "--pgid", leg_identity["pgid"],
                        "--interval", "0.02",
                        "--parent-recheck-interval", "0.05",
                    ]
                )
                leg.wait(timeout=5)
                deadline = time.time() + 3
                row = ""
                while time.time() < deadline:
                    row = jobs.read_text(encoding="utf-8")
                    if "reap_close_deferred=parent-live:process" in row:
                        break
                    time.sleep(0.05)
                self.assertIn("reap_close_deferred=parent-live:process", row)
                self.assertIn("att-leg", row.splitlines()[1])
                self.assertEqual(row.splitlines()[1].split("\t")[1], "open")

                self.assertTrue(
                    D.close_attempt_row(jobs, "att-leg", "completed-marker")
                )
                self.assertEqual(watcher.wait(timeout=5), 0)
                row = jobs.read_text(encoding="utf-8")
                leg_row = next(
                    line for line in row.splitlines() if "att-leg" in line
                )
                self.assertEqual(leg_row.split("\t")[1], "done")
                self.assertIn("note=completed-marker", leg_row)
                self.assertNotIn("dead-missing-result", leg_row)
            finally:
                parent.kill()
                parent.wait(timeout=5)

    def test_dead_parent_closes_leg_as_missing_result(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = subprocess.Popen(["sleep", "5"])
            leg = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
                start_new_session=True,
            )
            jobs = _registry_with_parent_and_leg(base, parent, leg)
            leg_identity = D.process_launch_identity(leg.pid)
            leg.wait(timeout=5)
            parent.kill()
            parent.wait(timeout=5)
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", "att-leg",
                    "--pid", str(leg.pid),
                    "--pid-start", leg_identity["pid_start"],
                    "--pgid", leg_identity["pgid"],
                    "--interval", "0.02",
                    "--parent-recheck-interval", "0.05",
                ]
            )
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            leg_row = next(line for line in row.splitlines() if "att-leg" in line)
            self.assertEqual(leg_row.split("\t")[1], "done")
            self.assertIn("note=dead-missing-result", leg_row)
            self.assertIn("dispatch-reap-missing-result-v1", leg_row)

    def test_foreign_or_terminal_parent_row_is_not_a_completion_window(self):
        cases = {
            "terminal-parent": dict(parent_open=False),
            "foreign-worktree": dict(foreign="worktree"),
            "foreign-pid-start": dict(foreign="pid_start"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as td:
                    base = Path(td)
                    parent = subprocess.Popen(["sleep", "5"])
                    leg = subprocess.Popen(
                        ["sleep", "0.08"],
                        env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
                        start_new_session=True,
                    )
                    try:
                        jobs = _registry_with_parent_and_leg(base, parent, leg, **kwargs)
                        leg_identity = D.process_launch_identity(leg.pid)
                        watcher = subprocess.Popen(
                            [
                                sys.executable,
                                str(WATCH),
                                "--jobs", str(jobs),
                                "--attempt-id", "att-leg",
                                "--pid", str(leg.pid),
                                "--pid-start", leg_identity["pid_start"],
                                "--pgid", leg_identity["pgid"],
                                "--interval", "0.02",
                                "--parent-recheck-interval", "0.05",
                            ]
                        )
                        leg.wait(timeout=5)
                        self.assertEqual(watcher.wait(timeout=5), 0)
                        row = jobs.read_text(encoding="utf-8")
                        leg_row = next(
                            line for line in row.splitlines() if "att-leg" in line
                        )
                        self.assertEqual(leg_row.split("\t")[1], "done")
                        self.assertIn("note=dead-missing-result", leg_row)
                        self.assertNotIn("reap_close_deferred=parent-live", leg_row)
                    finally:
                        parent.kill()
                        parent.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
