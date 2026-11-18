import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

FLEET_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FLEET_ROOT.parent))

from fleet.collectors import dispatch  # noqa: E402

CALLER, CALLED = "att-" + "a" * 32, "att-" + "b" * 32
UNIT = ("capability=unit,worker_type=review,unit=qa/code-review,dispatch_depth=1,intensity=quick,"
        "route_node=unit,workflow_completion=runtime-v1")


class CalledWorkerHangTest(unittest.TestCase):
    """A unit worker a worker started hangs below that worker (the registry names the caller)."""

    def jobs(self, *rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            stamp = datetime.now(timezone.utc).isoformat()
            path.write_text("".join("\t".join((stamp, "open", "repo", "/tmp/wt", slug, meta)) + "\n" for slug, meta in rows),
                            encoding="utf-8")
            jobs, malformed = dispatch._scan_jobs_log(path, set())
        self.assertEqual(0, malformed)
        return {job.slug: job for job in jobs}

    def test_the_called_worker_is_a_child_of_its_caller_in_the_job_list(self):
        jobs = self.jobs(("caller-unit", f"attempt_id={CALLER},{UNIT}"),
                         ("called-unit", f"attempt_id={CALLED},caller_attempt_id={CALLER},{UNIT}"))
        called = jobs["called-unit"]
        self.assertEqual("caller-unit", called.parent_slug)
        self.assertGreaterEqual(called.depth, 2)
        self.assertTrue(called.is_child)
        self.assertIsNone(jobs["caller-unit"].parent_slug)
        self.assertEqual(1, jobs["caller-unit"].depth)

    def test_a_leg_the_depth_zero_session_started_hangs_where_it_did_before(self):
        jobs = self.jobs(("plain-unit", f"attempt_id={CALLER},parent_sid=session-1,{UNIT}"))
        plain = jobs["plain-unit"]
        self.assertIsNone(plain.parent_slug)
        self.assertEqual(1, plain.depth)
        self.assertEqual("session-1", plain.parent_sid)

    def test_a_caller_that_is_not_in_the_registry_leaves_the_row_alone(self):
        jobs = self.jobs(("called-unit", f"attempt_id={CALLED},caller_attempt_id={CALLER},{UNIT}"))
        self.assertIsNone(jobs["called-unit"].parent_slug)
        self.assertEqual(1, jobs["called-unit"].depth)


if __name__ == "__main__":
    unittest.main()
