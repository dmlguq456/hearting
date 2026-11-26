#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import sys
import tempfile
import dataclasses
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "utilities"))

from fleet import fleet, render  # noqa: E402
from fleet.collectors import resource_runs  # noqa: E402
from fleet.model import ResourceJob  # noqa: E402
import resource_run_registry  # noqa: E402
import resource_placement  # noqa: E402
import resource_progress  # noqa: E402


def flatten(lines):
    return "\n".join("".join(text for text, _key in line) for line in lines if line)


class ResourceRunFleetTest(unittest.TestCase):
    def row(self, run_id, state="working"):
        return ResourceJob(
            run_id=run_id, cwd="/work/project", project="project",
            elapsed_min=12, liveness=state, pid=42, starttime="11",
            command_hash="a" * 64, registry_status="running",
            registry_path="/work/project/_internal/resource-runs.json",
            log_path="/work/project/train.log", log_updated_at=1722744000,
            route="/routes/lab.json", node="full-run", config_ref="path:config.yaml",
            config_sha256="sha256:" + "b" * 64, source_commit="c" * 40,
            source_dirty=False,
        )

    def tearDown(self):
        render.set_show_all(False)
        render.set_process_view(False)

    def test_json_uses_separate_type_and_all_restores_terminal_rows(self):
        live, ended = self.row("gpu-0"), self.row("gpu-1", "exited")
        default = json.loads(fleet._snapshot_json([], [], [live, ended]))
        self.assertEqual([row["run_id"] for row in default["resource_jobs"]], ["gpu-0"])
        row = default["resource_jobs"][0]
        self.assertEqual((row["job_type"], row["resource_class"]), ("resource", "lab"))
        for key in (
            "run_id", "cwd", "project", "elapsed_min", "liveness",
            "log_path", "log_updated_at", "route", "node", "config_ref",
            "config_sha256", "source_commit", "source_dirty",
        ):
            self.assertIn(key, row)
        self.assertNotIn("gpu-0", [job.get("slug") for job in default["jobs"]])
        all_rows = json.loads(fleet._snapshot_json(
            [], [], [live, ended], show_all=True))
        self.assertEqual({row["run_id"] for row in all_rows["resource_jobs"]},
                         {"gpu-0", "gpu-1"})

    def test_top_level_lab_summary_is_absent_in_both_views(self):
        rows = [self.row("gpu-%d" % i) for i in range(5)]
        rows += [self.row("old", "exited"), self.row("stale", "stale")]
        with mock.patch.object(render, "_COMPUTE_HOSTS", None), \
                mock.patch.object(render.time, "time", return_value=1722744000):
            for process in (False, True):
                render.set_process_view(process)
                for show_all in (False, True):
                    render.set_show_all(show_all)
                    for section in ("fleet", "dispatch", "both"):
                        for width in (60, 120):
                            with self.subTest(process=process, show_all=show_all,
                                              section=section, width=width):
                                baseline = render._build_lines(
                                    [], [], section, width < 80, 0, term_width=width)
                                actual = render._build_lines(
                                    [], [], section, width < 80, 0,
                                    term_width=width, resources=rows)
                                self.assertEqual(actual, baseline)
                                self.assertNotIn("LAB RESOURCES", flatten(actual))
        self.assertEqual(len(rows), 7)

    def test_gpu_commands_remain_visible_without_training_or_lab_summary(self):
        snapshot = {"configured": True, "hosts": [{
            "host": "gpu", "reachable": True, "gpus": [{
                "index": 0, "name": "NVIDIA RTX 4090", "util_pct": 42,
                "memory_used_mib": 12288, "memory_total_mib": 24576,
                "processes": [{"pid": 42, "proc_start": "11",
                    "used_memory_mib": 12288, "command": "python run.py --train",
                    "progress": {"line": "raw JSON", "age_s": 0,
                        "summary": "training-updates · baseline · successful 17808"}}],
            }],
        }]}
        before = json.dumps(snapshot)
        with mock.patch.object(render, "_COMPUTE_HOSTS", snapshot), \
                mock.patch.object(render, "_ROUTE_FOLD", {render._GPU_FOLD_ALL: False}), \
                mock.patch.object(render, "_COMPUTE_HOSTS_SET_AT", render.time.monotonic()):
            for process in (False, True):
                render.set_process_view(process)
                with self.subTest(process=process):
                    text = flatten(render._build_lines(
                        [], [], "both", False, 0, term_width=120,
                        resources=[self.row("gpu-0")]))
                    self.assertIn("python run.py --train", text)
                    self.assertNotIn("training-updates", text)
                    self.assertNotIn("LAB RESOURCES", text)
                    self.assertNotIn("raw JSON", text)
        self.assertEqual(json.dumps(snapshot), before)

    def test_json_preserves_raw_command_and_resource_progress_without_gpu_join(self):
        job = self.row("gpu-0")
        job.training_progress = {"phase": "training-updates", "attempt": 9,
                                 "loss": 0.5, "pid": 42, "starttime": "11"}
        snapshot = {"configured": True, "hosts": [{"host": "here", "self": True,
                    "reachable": True, "gpus": [{"index": 0, "processes": [{
                        "pid": 42, "proc_start": 11, "pgid": 42,
                        "command": "/usr/bin/python /work/scripts/train.py --config /work/x.yaml",
                    }]}]}]}
        before = json.loads(json.dumps(snapshot))
        output = json.loads(fleet._snapshot_json([], [], [job], compute_host_snapshot=snapshot))
        self.assertEqual(output["compute_hosts"], before)
        self.assertEqual(snapshot, before)
        self.assertEqual(output["resource_jobs"][0]["training_progress"], job.training_progress)

    def test_collector_keeps_multiple_runs_in_one_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.json"
            reg = root / "resource-runs.json"
            identity = resource_run_registry.proc_identity(os.getpid())
            reg.write_text(json.dumps({
                "schema_version": 1,
                "runs": {
                    "gpu-0": {**identity, "cwd": "/work/project", "status": "running"},
                    "gpu-1": {**identity, "cwd": "/work/project", "status": "running"},
                },
            }))
            resource_run_registry.register_registry(reg, index)
            with mock.patch.dict(os.environ, {"AGENT_RESOURCE_RUN_INDEX": str(index)}):
                rows = resource_runs.collect()
        self.assertEqual([row.run_id for row in rows], ["gpu-0", "gpu-1"])
        self.assertTrue(all(row.project == "project" for row in rows))
        self.assertTrue(all(row.liveness == "working" for row in rows))

    def test_compute_execution_row_survives_scan_and_json_without_observation(self):
        execution = {
            "kind": "compute-hosts", "run_id": "compute-1", "host": "gpu-a",
            "receipt_state": "finished", "meta_sha256": "m" * 64,
            "exit_sha256": "e" * 64, "receipt_sha256": "r" * 64,
            "outputs_sha256": "o" * 64, "contract_sha256": "c" * 64,
        }
        registry_row = {
            "execution": execution, "cwd": "/work/project", "status": "succeeded",
            "resource_policy": "supervised-owner", "started_at": 100,
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.json"
            reg = root / "resource-runs.json"
            reg.write_text(json.dumps({
                "schema_version": 1, "runs": {"resource-1": registry_row},
            }))
            resource_run_registry.register_registry(reg, index)
            identity_reader = mock.Mock(side_effect=AssertionError("local PID probe"))
            with mock.patch.dict(os.environ, {"AGENT_RESOURCE_RUN_INDEX": str(index)}), \
                    mock.patch.object(resource_run_registry, "compute_execution_observation",
                                      side_effect=AssertionError("remote compute observation")) as remote, \
                    mock.patch.object(resource_placement, "observe",
                                      side_effect=AssertionError("GPU placement observation")) as placement, \
                    mock.patch.object(resource_progress, "collect",
                                      side_effect=AssertionError("remote progress observation")) as progress, \
                    mock.patch.object(resource_progress, "remote_candidates",
                                      side_effect=AssertionError("remote candidate observation")) as candidates:
                self.assertEqual(
                    resource_run_registry.classify_identity(registry_row, identity_reader),
                    ("stale", None, "compute-hosts-recorded-state-non-authoritative"),
                )
                rows = resource_runs.collect()
                payload = json.loads(fleet._snapshot_json([], [], rows, show_all=True))

        self.assertEqual(len(rows), 1, resource_runs.collect.last_diagnostics)
        row = rows[0]
        self.assertEqual(row.liveness, "exited")
        self.assertEqual(row.execution, execution)
        self.assertEqual(row.compute_host, "gpu-a")
        self.assertEqual(row.compute_run_id, "compute-1")
        json_row = payload["resource_jobs"][0]
        self.assertEqual(json_row["execution"], execution)
        self.assertEqual(json_row["compute_host"], "gpu-a")
        self.assertEqual(json_row["compute_run_id"], "compute-1")
        identity_reader.assert_not_called()
        remote.assert_not_called()
        placement.assert_not_called()
        progress.assert_not_called()
        candidates.assert_not_called()

    def test_every_field_the_scanner_emits_is_a_field_the_row_accepts(self):
        """Producer and consumer key sets, pinned together.

        The collector projects each scanned run into `ResourceJob` inside a
        per-row `try`, so one unexpected keyword drops that row into
        diagnostics nobody reads -- and since the extra field is present on
        *every* run, Fleet showed no resource runs at all. Five producer
        fields had accumulated that way (artifact_root, route_file,
        route_hash, route_id, route_node) before anyone noticed, because the
        only symptom was an empty section (2026-09-10). A new field must fail
        here, loudly, instead.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.json"
            reg = root / "resource-runs.json"
            identity = resource_run_registry.proc_identity(os.getpid())
            reg.write_text(json.dumps({
                "schema_version": 1,
                "runs": {"gpu-0": {**identity, "cwd": "/work/project", "status": "running"}},
            }))
            resource_run_registry.register_registry(reg, index)
            emitted, _ = resource_run_registry.scan(index_path=str(index))
        self.assertTrue(emitted, "the scanner produced no run to compare against")
        accepted = {field.name for field in dataclasses.fields(ResourceJob)}
        unaccepted = sorted(set(emitted[0]) - accepted)
        self.assertEqual(unaccepted, [], f"ResourceJob rejects scanner field(s): {unaccepted}")


if __name__ == "__main__":
    unittest.main()
