#!/usr/bin/env python3
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


if __name__ == "__main__":
    unittest.main()
