"""Regressions derived from the live BC/TF parked owners and idle [1d]."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import model, render
from fleet.collectors import procscan


class ResourceNowTest(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads((Path(__file__).parent / "fixtures/resource_now_bc_tf.json").read_text())

    def owner(self, row, harness):
        child = model.ResourceJob(run_id="run", node=row["node"], liveness="working",
            pid=row["pid"], starttime=row["starttime"], process_group=row["process_group"],
            elapsed_min=row["elapsed_min"], command=row["command"],
            log_updated_at=row["log_updated_at"], parent_attempt_id="att-owner",
            state_evidence={"reason": "exact-identity-match"})
        return model.DispatchJob(key="lab", slug="owner", harness=harness, liveness="idle",
            attempt_id="att-owner", resource_children=[child],
            resource_wait={"state": "resource-parked", "nodes": [child.node], "run_ids": ["run"]},
            summary="previous model turn"), child

    def test_all_three_live_shapes_use_one_now_path_on_every_harness(self):
        for row in self.rows:
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(node=row["node"], harness=harness):
                    owner, child = self.owner(row, harness)
                    snapshot = {"hosts": [{"host": "mgpu-a1", "self": True, "reachable": True,
                        "gpus": ([] if row["gpu"] is None else [{"index": 0, "name": "RTX 6000",
                            "processes": [{"pid": child.pid, "proc_start": child.starttime,
                                "pgid": child.process_group, "command": "python train.py"}]}])}]}
                    with mock.patch.object(render, "_fresh_compute_hosts", return_value=(snapshot, 0)), \
                         mock.patch.object(render.time, "time", return_value=1791512200):
                        text = render._resource_now_text(owner)
                        now = render._plain(render._context_detail_row(owner, term_width=180)[0])
                    expected_name = {"full-run": "full run", "resume-run": "resume run",
                                     "eval-run": "eval run"}[child.node]
                    self.assertIn(expected_name, now)
                    location = ("mgpu-a1:0" if row["gpu"] == 0 else
                                "mgpu-a1:CPU" if row["node"] == "eval-run" else
                                'host/GPU unknown')
                    self.assertIn(location, now)
                    self.assertIn('log ', now)
                    self.assertIn(model.fmt_min(child.elapsed_min), now)
                    self.assertIn(".py", text)
                    self.assertNotIn("previous model turn", now)
                    self.assertEqual(owner.summary, "previous model turn")

    def test_declared_progress_log_absence_multiple_runs_and_narrow_clipping(self):
        owner, child = self.owner(self.rows[0], "codex")
        child.progress = {"completed": 7, "total": 40, "unit": "epoch", "age_s": 3}
        child.log_updated_at = None
        with mock.patch.object(render, "_fresh_compute_hosts", return_value=(None, 0)):
            full = render._resource_now_text(owner)
            self.assertIn("7/40 epoch", full)
            self.assertIn('no log', full)
            self.assertIn('host/GPU unknown', full)
            for width in (60, 80, 100, 168):
                line = render._context_detail_row(owner, term_width=width)[0]
                self.assertLessEqual(sum(render._dw(t) for t, _ in line), width)
            owner.resource_children = []
            self.assertEqual(render._resource_now_text(owner), "waiting")
            owner.resource_wait = None
            owner.state_evidence = {"inputs": {"observed_liveness": {"state": "parked-supervised"}}}
            self.assertEqual(render._resource_now_text(owner), "waiting")

    def test_working_model_and_unrelated_resource_keep_their_own_now(self):
        owner, child = self.owner(self.rows[0], "opencode")
        owner.liveness, owner.resource_wait = "working", None
        self.assertIsNone(render._resource_now_text(owner))
        owner.liveness, owner.resource_wait = "idle", {"run_ids": ["another"]}
        self.assertEqual(render._resource_now_text(owner), "waiting")

    def test_log_age_survives_long_resource_now_for_live_and_exited_work(self):
        now = 1791512200
        for harness in ("claude", "codex", "opencode"):
            owner, child = self.owner(self.rows[0], harness)
            child.display_title = "길게 작성된 자원 작업 이름 " * 20
            child.log_updated_at = now - 90
            with mock.patch.object(render, "_fresh_compute_hosts", return_value=(None, 0)), \
                 mock.patch.object(render.time, "time", return_value=now):
                for state in ("working", "exited"):
                    child.liveness = state
                    child.exit_code = 0 if state == "exited" else None
                    owner.summary = None
                    for width in (80, 100, 168):
                        row = render._plain(render._context_detail_row(owner, term_width=width)[0])
                        self.assertTrue(row.endswith(" · 1m ago"), row)
                        self.assertLessEqual(render._dw(row), width)

    def test_now_and_resource_row_share_verified_host_without_gpu_snapshot(self):
        for harness in ("claude", "codex", "opencode"):
            owner, child = self.owner(self.rows[0], harness)
            child.local_placement = {"hostname": "mgpu-a1.example.test", "processes": [
                {"pid": child.pid, "starttime": child.starttime}]}
            child.log_path, child.log_updated_at = "/logs/empty.log", None
            child.log_size = 0
            with mock.patch.object(render, "_fresh_compute_hosts", return_value=(None, 0)):
                now = render._resource_now_text(owner)
                row = render._plain(render._resource_child_rows(owner, term_width=180)[0])
            for text in (now, row):
                self.assertIn("mgpu-a1.example.test", text)
                self.assertNotIn('host/GPU unknown', text)
            self.assertIn('no output', now)
            self.assertNotIn('log ', now)
            child.log_size = None
            self.assertEqual(render._resource_log_age(child), 'output unknown')

    def test_remote_gpu_join_uses_exact_attempt_not_directory_or_local_pid(self):
        owner, child = self.owner(self.rows[0], "opencode")
        snapshot = {"hosts": [{"host": "gpu", "self": False, "reachable": True,
            "gpus": [{"index": 1, "name": "RTX 4090", "processes": [
                {"pid": 42, "proc_start": "99", "command": "python remote.py",
                 "owner": {"kind": "job", "id": "att-owner"}}]}]}]}
        with mock.patch.object(render, "_fresh_compute_hosts", return_value=(snapshot, 0)):
            self.assertIn("gpu:1", render._resource_now_text(owner))
            child.parent_attempt_id = "foreign"
            self.assertNotIn("gpu:1", render._resource_now_text(owner))

    def test_exited_resource_keeps_its_result_when_resumed_owner_has_no_now(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                owner, child = self.owner(self.rows[2], harness)
                owner.liveness, owner.resource_wait, owner.summary = "working", None, None
                child.liveness, child.exit_code, child.ended_at = "exited", 1, 1791514929
                now = render._resource_now_text(owner)
                self.assertIn("eval run eval_v54.py", now)
                self.assertIn("failed(exit 1)", now)
                self.assertIn('log ', now)
                owner.summary = "model handling the result"
                self.assertIsNone(render._resource_now_text(owner))
                owner.summary, owner.exec_child = None, {"name": "python3"}
                self.assertIsNone(render._resource_now_text(owner))
                owner.exec_child, owner.exec_tool = None, {"name": "exec_command"}
                self.assertIsNone(render._resource_now_text(owner))
                owner.exec_tool, owner.liveness = None, "idle"
                owner.state_evidence = {"inputs": {"observed_liveness": {"state": "parked-supervised"}}}
                owner.summary = "previous model turn before resource wait"
                self.assertIn("failed(exit 1)", render._resource_now_text(owner))
                owner.resource_wait = {"run_ids": [child.run_id]}
                self.assertIn("failed(exit 1)", render._resource_now_text(owner))
                self.assertEqual(child.exit_code, 1)


class SnapshotMaintenanceTest(unittest.TestCase):
    def helper(self, argv, parent="opencode"):
        with mock.patch.object(procscan, "_ppid_of", return_value=1082532), \
             mock.patch.object(procscan, "_comm_of", return_value=parent), \
             mock.patch.object(procscan, "read_environ", return_value={"HOME": "/home/operator"}), \
             mock.patch("builtins.open", mock.mock_open(read_data=("\0".join(argv) + "\0").encode())):
            return procscan._exec_is_helper(1071477, "git")

    def test_actual_snapshot_gc_is_plumbing_but_user_git_is_work(self):
        argv = ["git", "--git-dir", "/home/operator/.local/share/opencode/snapshot/project/worktree",
                "--work-tree", "/work/KWS_Net", "gc", "--prune=7.days"]
        self.assertTrue(self.helper(argv))
        self.assertFalse(self.helper(argv, parent="bash"))
        self.assertFalse(self.helper(["git", "status"]))
        self.assertFalse(self.helper(["git", "--git-dir", "/work/KWS_Net/.git", "gc"]))
        self.assertFalse(self.helper(["git", "--git-dir", "/home/operator/.local/share/opencode/snapshot-other/x", "gc"]))

    def test_headless_harness_child_does_not_badge_depth_zero(self):
        tree = {100: (1, 3600, "opencode"), 101: (100, 300, "opencode"),
                102: (101, 200, "python3")}
        with mock.patch.object(procscan, "read_environ", return_value={}):
            self.assertIsNone(procscan.exec_child(100, tree, procscan.children_index(tree)))


if __name__ == "__main__":
    unittest.main()
