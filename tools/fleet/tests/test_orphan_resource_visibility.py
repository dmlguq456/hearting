"""Live declared resources survive the removal of their model owner row."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import projection, render
from fleet.model import DispatchJob, ResourceJob, Session, WorkProjection


def text(lines):
    return "\n".join(render._plain(line) for line in lines if line)


class OrphanResourceVisibilityTest(unittest.TestCase):
    def setUp(self):
        # Keep the #406 command/progress formatting checks in the expanded
        # view; the bundle's own tests cover default command folding.
        fold = mock.patch.object(render, "_ROUTE_FOLD", {render._GPU_FOLD_ALL: False})
        fold.start()
        self.addCleanup(fold.stop)
        self.child = ResourceJob(
            run_id="paired-training", cwd="/work/project", project="project",
            parent_attempt_id="att-ended-owner", node="full-run", liveness="working",
            pid=490, starttime="10", process_group=490, elapsed_min=1620)
        self.snapshot = {"configured": True, "hosts": [{
            "host": "gpu-host", "self": True, "reachable": True, "gpus": [{
                "index": 0, "name": "NVIDIA RTX A6000", "processes": [{
                    "pid": pid, "proc_start": 20, "pgid": 490,
                    "used_memory_mib": 1024, "cwd": "/work/project",
                    "elapsed_s": 1620 * 60,
                    "command": "python train.py --config /cfg/" + name + ".yaml",
                } for pid, name in ((501, "s_baseline"), (502, "s_strided"))],
            }],
        }]}
        self.addCleanup(render.set_compute_hosts, None)
        self.addCleanup(render.set_process_view, False)
        self.addCleanup(render.set_show_all, False)
        render.set_show_all(False)
        render.set_compute_hosts(self.snapshot)

    def lines(self, children=None, jobs=(), sessions=(), process=False, width=240,
              section="both", live_order=None):
        render.set_process_view(process)
        return render._build_lines(
            list(sessions), list(jobs), section, width < 80, 0, term_width=width,
            layout="wide" if width >= 100 else "stack", live_order=live_order,
            resources=[self.child] if children is None else children,
            usage_snapshots={}, governor=None)

    def test_missing_owner_keeps_both_training_names_in_one_project_gpu_line(self):
        before = copy.deepcopy(self.snapshot)
        for process in (False, True):
            with self.subTest(process=process):
                lines = self.lines(process=process)
                output = text(lines)
                header = next(i for i, row in enumerate(lines)
                              if row and "project/" in render._plain(row))
                gpu = [(i, render._plain(row)) for i, row in enumerate(lines)
                       if row and "● GPU gpu-host:0" in render._plain(row)]
                self.assertEqual(len(gpu), 1)
                self.assertGreater(gpu[0][0], header)
                for name in ("s_baseline", "s_strided", "full run", "1d 3h"):
                    self.assertIn(name, gpu[0][1])
                self.assertNotIn("미등록", gpu[0][1])
                self.assertNotIn("att-ended-owner", output)
                self.assertNotIn("no active route", output)
        self.assertEqual(self.snapshot, before)

    def test_same_cwd_session_does_not_gain_resource_ownership_for_any_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                session = Session(
                    harness=harness, session_id="other-main", pid=900, proc_start="90",
                    cwd="/work/project", title="main", liveness="idle",
                    work_projection=WorkProjection(source="none"))
                output = text(self.lines(sessions=[session]))
                self.assertEqual(output.count("project/"), 1)
                self.assertEqual(output.count("● GPU gpu-host:0"), 1)
                self.assertIn("full run", output)
                self.assertEqual(session.work_projection.source, "none")
                self.assertFalse(getattr(session, "resource_children", None))

    def test_exact_live_owner_shows_resource_in_now_and_one_gpu_row_in_both_views(self):
        for harness in ("claude", "codex", "opencode"):
            for process in (False, True):
                with self.subTest(harness=harness, process=process):
                    owner = DispatchJob(
                        key="code", slug="live-owner", cwd="/work/project", pid=480,
                        proc_start="9", attempt_id=self.child.parent_attempt_id,
                        harness=harness, intensity="standard", liveness="working",
                        worker_type="owner", work_projection=WorkProjection(source="none"))
                    projection._attach_resource_children([owner], [self.child])
                    output = text(self.lines(jobs=[owner], process=process))
                    self.assertIn("live-owner", output)
                    gpu_rows = [row for row in output.splitlines()
                                if "● GPU gpu-host:0" in row]
                    self.assertEqual(len(gpu_rows), 1)
                    self.assertIn("full run", gpu_rows[0])
                    self.assertIn("2 GB", gpu_rows[0])
                    now_rows = [row for row in output.splitlines()
                                if "full run" in row and "● GPU" not in row]
                    self.assertEqual(len(now_rows), 1)
                    self.assertIn("gpu-host:0", now_rows[0])
                    self.assertIn('no log', now_rows[0])
                    self.assertEqual(owner.resource_children, [self.child])

    def test_probe_absence_or_expiry_preserves_resource_liveness_and_elapsed(self):
        for process in (False, True):
            for expired in (False, True):
                with self.subTest(process=process, expired=expired):
                    render.set_compute_hosts(self.snapshot if expired else None)
                    with mock.patch.object(render, "_COMPUTE_HOSTS_SET_AT", time.monotonic() - 100):
                        output = text(self.lines(process=process))
                    self.assertIn("resource full run", output)
                    self.assertIn("● resource full run  1d 3h", output)
                    self.assertNotIn("● GPU", output)

    def test_terminal_or_stale_identity_removes_resource_fallback(self):
        render.set_compute_hosts(None)
        for process in (False, True):
            for state in ("exited", "stale"):
                with self.subTest(process=process, state=state):
                    output = text(self.lines(children=[replace(self.child, liveness=state)],
                                             process=process))
                    self.assertNotIn("full run", output)
                    self.assertNotIn("project/", output)
        self.assertNotIn("full run", text(self.lines(children=[])))

    def test_gpu_pid_reuse_never_joins_the_resource_line(self):
        child = replace(self.child, pid=501, starttime="999", process_group=None)
        rows, resource_rows = render._gpu_and_resource_rows([child], commands=True)
        self.assertEqual(rows, [])
        self.assertIn("resource full run", text(resource_rows))

    def test_remote_exact_parent_joins_once_without_a_live_owner(self):
        host = self.snapshot["hosts"][0]
        host["self"] = False
        for process in host["gpus"][0]["processes"]:
            process["owner"] = {"kind": "job", "id": self.child.parent_attempt_id}
        for process in (False, True):
            with self.subTest(process=process):
                output = text(self.lines(process=process))
                self.assertEqual(output.count("● GPU gpu-host:0"), 1)
                self.assertEqual(output.count("full run"), 1)
        for process in host["gpus"][0]["processes"]:
            process["owner"]["id"] = "att-unrelated"
        self.assertEqual(render._resource_gpu_resources(self.child, self.snapshot), [])

    def test_two_resources_sharing_gpu_do_not_double_count_process_memory(self):
        second = replace(self.child, run_id="second-run", node="second-node")
        output = text(self.lines(children=[self.child, second]))
        self.assertEqual(output.count("● GPU gpu-host:0"), 1)
        self.assertIn("2 GB", output)
        self.assertIn("full run", output)
        self.assertIn("second node", output)
        self.assertEqual(output.count("s_baseline"), 2)  # top command + project label

    def test_owner_session_gpu_overlap_keeps_only_unshown_fallback_processes(self):
        for harness in ("claude", "codex", "opencode"):
            for complete in (False, True):
                for process in (False, True):
                    with self.subTest(harness=harness, complete=complete, process=process):
                        owner = DispatchJob(
                            key="code", slug="visible-owner", cwd="/work/project", pid=480,
                            proc_start="9", attempt_id="att-other", harness=harness,
                            intensity="standard", liveness="working", worker_type="owner",
                            work_projection=WorkProjection(source="none"))
                        owner._runtime_session_id = "exact-session"
                        for i, proc in enumerate(self.snapshot["hosts"][0]["gpus"][0]["processes"]):
                            proc["session_owner"] = ({"kind": "session", "harness": harness,
                                                      "id": "exact-session"}
                                                     if complete or i == 0 else None)
                        output = text(self.lines(jobs=[owner], process=process))
                        rows = [row for row in output.splitlines() if "● GPU gpu-host:0" in row]
                        self.assertEqual(len(rows), 1 if complete else 2)
                        self.assertEqual(output.count("full run"), 1)
                        if complete:
                            self.assertIn("2 GB", rows[0])
                            self.assertIn("resource full run", output)
                        else:
                            self.assertTrue(all("1 GB" in row for row in rows))
                            fallback = next(row for row in rows if "full run" in row)
                            self.assertIn("s_strided", fallback)
                            self.assertNotIn("s_baseline", fallback)

    def test_attached_resource_overlap_preserves_orphan_progress_without_duplicate_gpu(self):
        for complete in (False, True):
            for process in (False, True):
                with self.subTest(complete=complete, process=process):
                    attached = replace(self.child, run_id="attached-run", node="owner-node",
                                       parent_attempt_id="att-live-owner")
                    if not complete:
                        attached = replace(attached, pid=501, starttime="20", process_group=None)
                    owner = DispatchJob(
                        key="code", slug="live-owner", cwd="/work/project", pid=480,
                        proc_start="9", attempt_id=attached.parent_attempt_id, harness="codex",
                        intensity="standard", liveness="working", worker_type="owner",
                        work_projection=WorkProjection(source="none"))
                    projection._attach_resource_children([owner], [attached, self.child])
                    output = text(self.lines(children=[attached, self.child], jobs=[owner],
                                             process=process))
                    rows = [row for row in output.splitlines() if "● GPU gpu-host:0" in row]
                    self.assertEqual(len(rows), 1 if complete else 2)
                    self.assertEqual(sum(row.count("owner node") for row in rows), 1)
                    now_rows = [row for row in output.splitlines()
                                if "owner node" in row and "● GPU" not in row]
                    self.assertEqual(len(now_rows), 1)
                    self.assertIn("gpu-host:0", now_rows[0])
                    self.assertEqual(output.count("full run"), 1)
                    if complete:
                        self.assertIn("resource full run", output)
                        self.assertIn("2 GB", rows[0])
                    else:
                        self.assertTrue(all("1 GB" in row for row in rows))
                        fallback = next(row for row in rows if "full run" in row)
                        self.assertIn("s_strided", fallback)
                        self.assertNotIn("s_baseline", fallback)

    def test_fallback_groups_share_process_exclusions_and_keep_each_resource_row(self):
        other = replace(self.child, run_id="other-project-run", cwd="/work/other-project",
                        project="other-project", node="other-node")
        for process in (False, True):
            with self.subTest(process=process):
                output = text(self.lines(children=[self.child, other], process=process))
                self.assertEqual(output.count("● GPU gpu-host:0"), 1)
                self.assertEqual(output.count("2 GB"), 1)
                self.assertEqual(output.count("full run"), 1)
                self.assertEqual(output.count("other node"), 1)
                self.assertEqual(output.count("resource "), 1)

    def test_resource_only_project_is_hot_unfolded_and_section_filtered(self):
        group = {"sessions": [], "jobs": [], "resources": [self.child]}
        self.assertEqual(render._group_activity_rank(group), 0)
        emission = render._group_emission(group, True, True)
        self.assertFalse(emission["empty"])
        self.assertFalse(emission["fold"])
        self.assertIn("full run", text(self.lines(section="dispatch")))
        self.assertNotIn("full run", text(self.lines(section="fleet")))
        self.assertFalse(render._orphan_resource_groups([replace(self.child, parent_attempt_id=None)], []))

    def test_fallback_rows_fit_each_supported_width(self):
        for process in (False, True):
            for width in (60, 100, 240):
                with self.subTest(process=process, width=width):
                    rows = [row for row in self.lines(process=process, width=width)
                            if row and "● GPU" in render._plain(row)]
                    self.assertEqual(len(rows), 1)
                    self.assertLessEqual(render._dw(render._plain(rows[0])), width)
                    self.assertIn("full run", render._plain(rows[0]))


if __name__ == "__main__":
    unittest.main()
