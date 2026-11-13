"""Owned tool activity survives sandbox wrappers and supervisor waiting."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import model
from fleet.collectors import dispatch, procscan


ATTEMPT = "att-owned-exec-fixture"


def owned_child(comm="node"):
    return dict(pid=108, comm=comm, etime_s=120, proc_start="208",
                root_pid=100, root_start="200", attempt_id=ATTEMPT,
                ownership_verified=True,
                ancestry=[dict(pid=100, ppid=1, start="200"),
                          dict(pid=108, ppid=100, start="208")])


class OwnedExecutionTest(unittest.TestCase):
    def fixture(self):
        comms = ("codex", "codex-linux-san", "bwrap", "codex-linux-sandbox",
                 "sh", "sh", "python3", "node", "node")
        tree = {100 + i: (1 if i == 0 else 99 + i,
                         3600 if i == 0 else 200 - i, name)
                for i, name in enumerate(comms)}
        ids = {pid: (row[0], str(pid + 100)) for pid, row in tree.items()}
        return tree, ids

    def scan(self, tree, ids, env=None):
        wrapper = procscan._exec_is_wrapper
        with patch.object(procscan, "_exec_identity", side_effect=ids.get), \
             patch.object(procscan, "read_environ", side_effect=(env or
                          (lambda pid: {"AGENT_DISPATCH_ATTEMPT_ID": ATTEMPT}))), \
             patch.object(procscan, "_exec_is_wrapper",
                          side_effect=lambda pid, comm: pid == 106 or wrapper(pid, comm)):
            return procscan.exec_child(100, tree, procscan.children_index(tree),
                                      expected_start="200", attempt_id=ATTEMPT)

    def test_reported_sandbox_chain_reaches_node_with_exact_ancestry(self):
        tree, ids = self.fixture()
        work = self.scan(tree, ids)
        self.assertEqual(work["comm"], "node")
        self.assertTrue(work["ownership_verified"])
        self.assertEqual(work["attempt_id"], ATTEMPT)
        self.assertEqual([r["pid"] for r in work["ancestry"]], list(range(100, 108)))

    def test_foreign_attempt_unreadable_or_reparented_child_is_not_owned(self):
        tree, ids = self.fixture()
        for bad in (None, (999, "204")):
            altered = dict(ids)
            altered[104] = bad
            self.assertIsNone(self.scan(tree, altered))
        env = lambda pid: {"AGENT_DISPATCH_ATTEMPT_ID": "foreign" if pid == 104 else ATTEMPT}
        self.assertIsNone(self.scan(tree, ids, env))

    def test_pid_reuse_during_path_validation_is_rejected(self):
        tree, ids = self.fixture()
        seen = {}
        def identity(pid):
            seen[pid] = seen.get(pid, 0) + 1
            return (ids[pid][0], "reused") if pid == 101 and seen[pid] > 1 else ids.get(pid)
        with patch.object(procscan, "_exec_identity", side_effect=identity), \
             patch.object(procscan, "read_environ", return_value={"AGENT_DISPATCH_ATTEMPT_ID": ATTEMPT}):
            self.assertIsNone(procscan.exec_child(100, tree, procscan.children_index(tree),
                              expected_start="200", attempt_id=ATTEMPT))

    def test_empty_sandbox_and_runtime_service_do_not_count_as_work(self):
        for comm in ("bwrap", "codex-linux-san", "codex-code-mode"):
            tree = {100: (1, 3600, "codex"), 101: (100, 200, comm)}
            with patch.object(procscan, "read_environ", return_value={}):
                self.assertIsNone(procscan.exec_child(100, tree, procscan.children_index(tree)))
        # A boot-time stdio MCP server is not a turn's execution.
        tree = {100: (1, 3600, "codex"), 101: (100, 3599, "node")}
        with patch.object(procscan, "read_environ", return_value={}):
            self.assertIsNone(procscan.exec_child(100, tree, procscan.children_index(tree)))

    def test_no_native_status_or_timestamp_does_not_hide_owned_execution(self):
        ev = dict(harness="codex", pid=100, proc_start="200", pid_alive=True,
                  exec_child=owned_child(), status=None, mtime=None)
        state, evidence = model.classify_session(ev, 5000)
        self.assertEqual(state, "working")
        self.assertEqual(evidence["source"], "owned-exec")
        for change in ({"proc_start": "other"}, {"exec_child": owned_child("sleep")},
                       {"exec_child": {"pid": 108, "comm": "node", "etime_s": 120}}):
            self.assertEqual(model.classify_session({**ev, **change}, 5000)[0], "unknown")

    def test_attempt_waiting_and_tool_running_are_distinct(self):
        ev = dict(pid=99, proc_start="199", attempt_id=ATTEMPT,
                  exec_child=owned_child(), observed_liveness={"state": "parked-supervised",
                  "reason": "supervisor-deliverable", "process_state": "live"})
        result = model.classify_attempt_evidence(ev, 5000)
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["observed_liveness"]["state"], "parked-supervised")
        self.assertEqual(model.classify_attempt_evidence({**ev, "exec_child": None}, 5000)["state"], "idle")
        foreign = {**owned_child(), "attempt_id": "foreign"}
        self.assertEqual(model.classify_attempt_evidence({**ev, "exec_child": foreign}, 5000)["state"], "idle")
        terminal = {**ev, "observed_liveness": {"state": "terminal"}}
        self.assertEqual(model.classify_attempt_evidence(terminal, 5000)["state"], "done")

    def test_exact_session_execution_attaches_before_job_classification(self):
        session = model.Session(harness="codex", pid=100, proc_start="200",
                                attempt_id=ATTEMPT, exec_child=owned_child(), liveness="working")
        job = model.DispatchJob(key="code", slug="owned", pid=99, proc_start="199",
                                attempt_id=ATTEMPT, status="open", source="jobs")
        job._dispatch_context_owned = True
        dispatch._attach_execution_evidence([job], [session])
        with patch.object(dispatch, "_attempt_terminal_observation", return_value=None), \
             patch.object(dispatch, "_attempt_heartbeat", return_value=None), \
             patch.object(dispatch, "_job_transcript_signal", return_value=None), \
             patch.object(procscan, "read_proc_start", return_value="199"), \
             patch.object(dispatch.os.path, "exists", return_value=True):
            dispatch._dispatch_liveness(job, 5000, track=False)
        self.assertEqual(job.state_evidence["inputs"]["exec_child"], session.exec_child)
        other = copy.copy(session)
        other.attempt_id = "foreign"
        job.exec_child = None
        dispatch._attach_execution_evidence([job], [other])
        self.assertIsNone(job.exec_child)

    @unittest.skipUnless(sys.platform.startswith("linux"), "local /proc identity fixture")
    def test_real_process_sandbox_descendants_are_owned_and_cleanly_exit(self):
        script = '''import ctypes,os,sys
names=["codex-linux-san","bwrap","node"]
for name in names:
 child=os.fork()
 if child:
  os.waitpid(child,0);sys.exit(0)
 ctypes.CDLL(None).prctl(15,name.encode(),0,0,0)
print("ready",flush=True)
sys.stdin.read()
'''
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID=ATTEMPT))
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0])
            self.assertEqual(child.stdout.readline().strip(), "ready")
            start = procscan.read_proc_start(child.pid)
            tree = procscan.proc_tree()
            work = procscan.exec_child(child.pid, tree, procscan.children_index(tree),
                                      min_age=0, expected_start=start, attempt_id=ATTEMPT)
            self.assertIsNotNone(work)
            self.assertEqual(work["comm"], "node")
            self.assertEqual(len(work["ancestry"]), 4)
            self.assertTrue(work["ownership_verified"])
        finally:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
        self.assertEqual(child.returncode, 0)


if __name__ == "__main__":
    unittest.main()
