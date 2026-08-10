#!/usr/bin/env python3
import json
import os
import sys
import tempfile
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

    def test_tui_is_two_line_summary_and_terminal_toggle(self):
        rows = [
            self.row("gpu-0"), self.row("gpu-1"),
            self.row("old", "exited"), self.row("stale", "stale"),
        ]
        compact = render._resource_rows(rows, "dispatch")
        self.assertEqual(len(compact), 2)
        text = flatten(compact)
        for value in ("LAB RESOURCES", "2 visible", "working 2", "exited 0", "stale 0",
                      "project/gpu-0", "project/gpu-1", "full-run", "12m"):
            self.assertIn(value, text)
        self.assertNotIn("old", text)
        self.assertNotIn("train.log", text)  # detailed provenance remains JSON-only.
        render.set_show_all(True)
        all_compact = render._resource_rows(rows, "dispatch")
        self.assertEqual(len(all_compact), 2)
        shown = flatten(all_compact)
        self.assertIn("4 visible", shown)
        self.assertIn("working 2", shown)
        self.assertIn("exited 1", shown)
        self.assertIn("stale 1", shown)
        self.assertIn("old", shown)
        self.assertIn("+1 more", shown)

    def test_tui_caps_run_summaries_at_three(self):
        rows = [self.row("gpu-%d" % i) for i in range(5)]
        compact = render._resource_rows(rows, "both")
        self.assertEqual(len(compact), 2)
        text = flatten(compact)
        for run_id in ("gpu-0", "gpu-1", "gpu-2"):
            self.assertIn("project/" + run_id, text)
        self.assertNotIn("project/gpu-3", text)
        self.assertNotIn("project/gpu-4", text)
        self.assertIn("+2 more", text)

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


if __name__ == "__main__":
    unittest.main()
