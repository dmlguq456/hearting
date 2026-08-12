#!/usr/bin/env python3
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from dispatch_degradation import record_degradation

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


class DegradationWriterTest(unittest.TestCase):
    def setUp(self):
        # record_degradation() resolves the dispatch state root ahead of
        # agent_home/.dispatch, preferring an inherited AGENT_DISPATCH_JOBS --
        # clear it so these agent-home-relative fixtures aren't sensitive to
        # the invoking shell's real registry.
        self._prior_jobs = os.environ.pop("AGENT_DISPATCH_JOBS", None)
        self.addCleanup(self._restore_jobs)

    def _restore_jobs(self):
        if self._prior_jobs is not None:
            os.environ["AGENT_DISPATCH_JOBS"] = self._prior_jobs

    def test_depth_zero_is_not_written(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(record_degradation(agent_home=home, dispatch_depth=0,
                                                  writer="stage-dispatch-fallback.py"))
            self.assertFalse(os.path.exists(os.path.join(home, ".dispatch")))

    def test_schema_bounds_and_route_shard(self):
        with tempfile.TemporaryDirectory() as home:
            path = record_degradation(agent_home=home, route_id="rt-test", route_node="exec",
                                      route_hash="sha256:test", dispatch_depth=1,
                                      fallback_hop="inline", execution_surface="inline",
                                      writer="stage-dispatch-fallback.py",
                                      reason="r" * 200, detail="d" * 600,
                                      attempt_trace="t" * 3000)
            with open(path, encoding="utf-8") as stream:
                row = json.loads(stream.readline())
            self.assertEqual(set(("schema_version", "kind", "ts", "route_id", "route_node",
                                  "route_hash", "dispatch_depth", "fallback_hop",
                                  "execution_surface", "writer")) - set(row), set())
            self.assertLessEqual(len(row["reason"]), 160)
            self.assertLessEqual(len(row["detail"]), 512)
            self.assertLessEqual(len(row["attempt_trace"]), 2048)
            self.assertTrue(row["event_id"].startswith("dg-"))
            self.assertTrue(row["reason"].endswith("…"))
            self.assertTrue(row["detail"].endswith("…"))
            self.assertTrue(row["attempt_trace"].endswith("…"))

    def test_failures_are_fail_open_and_return_none(self):
        cases = ("readonly", "serialization", "lock")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as home:
                if case == "readonly":
                    with open(os.path.join(home, ".dispatch"), "w", encoding="utf-8"):
                        pass
                    result = record_degradation(agent_home=home, route_id="rt-ro",
                                                route_node="execute", route_hash="h",
                                                dispatch_depth=2, fallback_hop="inline",
                                                execution_surface="inline",
                                                writer="stage-dispatch-fallback.py")
                elif case == "serialization":
                    result = record_degradation(agent_home=home, route_id="rt-json",
                                                route_node="execute", route_hash="h",
                                                dispatch_depth=2, fallback_hop="inline",
                                                execution_surface="inline",
                                                writer="stage-dispatch-fallback.py",
                                                last_direct_failure=object())
                else:
                    root = os.path.join(home, ".dispatch", "degradations")
                    os.makedirs(root)
                    lock_path = os.path.join(root, "rt-lock.jsonl.lock")
                    holder = subprocess.Popen([sys.executable, "-c", (
                        "import fcntl,time,sys; f=open(sys.argv[1],'a+'); "
                        "fcntl.flock(f,fcntl.LOCK_EX); time.sleep(.5)"), lock_path])
                    try:
                        time.sleep(.05)
                        result = record_degradation(agent_home=home, route_id="rt-lock",
                                                    route_node="execute", route_hash="h",
                                                    dispatch_depth=2, fallback_hop="inline",
                                                    execution_surface="inline",
                                                    writer="stage-dispatch-fallback.py")
                    finally:
                        holder.wait(timeout=2)
                self.assertIsNone(result)

    def test_invalid_required_record_is_skipped_by_consumer(self):
        from fleet.collectors import dispatch
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, ".dispatch", "degradations")
            os.makedirs(root)
            valid = {"schema_version": 1, "kind": "degradation", "ts": 1,
                     "route_id": "rt-valid", "route_node": "execute", "route_hash": "h",
                     "dispatch_depth": 2, "fallback_hop": "inline",
                     "execution_surface": "inline", "writer": "stage-dispatch-fallback.py"}
            invalid = dict(valid)
            del invalid["route_hash"]
            with open(os.path.join(root, "rt-valid.jsonl"), "w", encoding="utf-8") as stream:
                stream.write(json.dumps(invalid) + "\n")
                stream.write(json.dumps(valid) + "\n")
            rows = dispatch._scan_degradations(["rt-valid"], agent_home=home)
            self.assertEqual(len(rows["rt-valid"]), 1)
            self.assertEqual(rows["rt-valid"][0]["route_hash"], "h")

    def test_subprocess_append_over_4096_bytes_has_no_torn_lines(self):
        with tempfile.TemporaryDirectory() as home:
            code = """import sys
from dispatch_degradation import record_degradation
record_degradation(agent_home=sys.argv[1], route_id='rt-race', route_node='execute', route_hash='h', dispatch_depth=2, fallback_hop='inline', execution_surface='inline', writer='stage-dispatch-fallback.py', detail='x'*5000, attempt_id=sys.argv[2])
"""
            processes = [subprocess.Popen([sys.executable, "-c", code, home, str(i)],
                                          cwd=os.path.dirname(__file__),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                         for i in range(12)]
            for process in processes:
                self.assertEqual(process.wait(timeout=5), 0)
            path = os.path.join(home, ".dispatch", "degradations", "rt-race.jsonl")
            with open(path, "rb") as stream:
                lines = stream.readlines()
            self.assertEqual(len(lines), 12)
            for line in lines:
                self.assertTrue(line.endswith(b"\n"))
                self.assertEqual(json.loads(line)["route_id"], "rt-race")


if __name__ == "__main__":
    unittest.main()
