#!/usr/bin/env python3
"""Offline checks of the `panel-route-running` scenario's process scope (11-27).

The scenario reads the panel producer and Fleet inside its fixture, but only over the processes it
started. A registry row's PID counts as one of them only while it is valid in this namespace and
still carries the recorded start; the reading itself lists only those processes and opens no other
process's environment (an audit hook counts and refuses any such open). Two `sleep` processes this
test starts stand in for the scenario's own process and a foreign one; made-up ids only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import harness_behavior_cases_b as cases_b
import harness_behavior_cases_panel_route as cases

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.fleet.collectors import procscan  # noqa: E402

READ = r"""
from tools.fleet.collectors import collect_all
collect_all(include_report_verification=False)
quiet = len(foreign_environ)
try:
    open("/proc/%s/environ" % args["foreign"], "rb").close()
    refused = False
except PermissionError:
    refused = True
print(json.dumps({"during_read": quiet, "refused": refused, "counted": len(foreign_environ)}))
"""


class ProcessScope(unittest.TestCase):
    def setUp(self):
        self.ours = self.start()
        self.foreign = self.start()

    def start(self):
        child = subprocess.Popen(["sleep", "120"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        return child.pid, str(procscan.read_proc_start(child.pid))

    def rows(self):
        (ours, ours_start), (foreign, foreign_start) = self.ours, self.foreign
        return [{"status": "running", "slug": "hb-solo-review-owner", "attempt_id": "att-ours",
                 "pid": str(ours), "pid_start": ours_start},
                # a PID now carrying another start: a different process than the one the row recorded
                {"status": "done", "slug": "hb-solo-review-owner-0", "attempt_id": "att-before",
                 "pid": str(foreign), "pid_start": str(int(foreign_start) + 1)},
                {"status": "running", "slug": "hb-solo-review-stage", "attempt_id": "att-none"}]

    def scope(self):
        scenario = cases.PanelRouteRunning()
        scenario.unit_rows = self.rows
        return scenario.launched_pids(SimpleNamespace())

    def test_a_registry_pid_joins_only_with_its_own_start(self):
        pids = self.scope()
        self.assertIn(self.ours[0], pids)
        self.assertNotIn(self.foreign[0], pids)

    def test_the_reading_opens_no_environment_outside_its_processes(self):
        with tempfile.TemporaryDirectory(prefix="pr-scope-") as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text("".join(
                "2026-11-27T00:00:00Z\t%s\trepo\t%s\t%s\t%s\n" % (
                    row["status"], tmp, row["slug"],
                    ",".join(f"{k}={v}" for k, v in row.items() if k not in ("status", "slug")))
                for row in self.rows()))
            env = {**os.environ, "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs)}
            done = subprocess.run(
                [sys.executable, "-B", "-c", cases_b._PRELUDE + cases.GUARD + READ,
                 json.dumps({"pids": self.scope(), "foreign": self.foreign[0]})],
                env=env, cwd=tmp, text=True, capture_output=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        # The whole Fleet read opened no other process's environment; an explicit try is counted and refused.
        self.assertEqual(seen, {"during_read": 0, "refused": True, "counted": 1})


if __name__ == "__main__":
    unittest.main()
