#!/usr/bin/env python3
"""Ordinary filesystem cleanup followed by actual producer/reader work.

All artifacts, registries, configuration and session state are temporary.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_history as history
import artifact_reader as reader

spec = importlib.util.spec_from_file_location("freedom_fixture", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FX)
P = FX.P


class ArtifactFreedomTest(FX.ProducerTestBase):
    def setUp(self):
        inherited = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, inherited)
        super().setUp()
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.activate()

    def finished(self, key="freedom", slug="first"):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(begun, "plans/cycle/REPORT.md", b"# Result\noriginal\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"])
        return begun

    def events(self):
        return [json.loads(line) for path in (self.root / history.HISTORY_REL).glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def next_work(self, parent):
        route, route_file = self.route(slug="next", campaign_key="freedom")
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        parent_cycle_id=parent["cycle_id"])
        self.write_output(begun)
        self.close(route, route_file)
        self.assertEqual(P.finalize(self.root, cycle_id=begun["cycle_id"])["status"], "sealed")
        return begun

    def test_reader_and_memory_read_a_cross_campaign_hand_move_before_listing(self):
        first = self.finished()
        other = self.finished("destination", "other")
        old = Path(first["cycle_dir"])
        moved = Path(other["cycle_dir"]).parent / old.name
        old.rename(moved)
        buckets = reader.bucket_dirs(self.root, "plans")
        self.assertIn(moved / "artifacts/plans", [path for path, _ in buckets])
        env = dict(os.environ, AGENT_ARTIFACT_ROOT=str(self.root))
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/memory/mem.py"), "curate-artifacts"]
        result = subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        P.list_campaign_summaries(self.root)
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["campaign_id"], other["campaign_id"])
        self.assertTrue(any(row["operation"] == "move" and row["target"]["id"] == first["cycle_id"]
                            for row in self.events()))

    def test_deleted_campaign_and_parent_allow_the_next_route(self):
        first = self.finished()
        shutil.rmtree(Path(first["cycle_dir"]).parent)
        next_cycle = self.next_work(first)
        self.assertNotEqual(next_cycle["campaign_id"], first["campaign_id"])
        self.assertIsNone(P.read_cycle_record(self.root, next_cycle["cycle_id"])["parent_cycle_id"])
        self.assertTrue(any(row["operation"] == "delete" and row["target"]["id"] == first["campaign_id"]
                            for row in self.events()))

    def test_entire_campaigns_directory_and_locator_cache_are_disposable(self):
        first = self.finished()
        shutil.rmtree(self.root / "campaigns")
        self.next_work(first)
        self.assertTrue(P.read_cycle_record(self.root, first["cycle_id"])["deleted_at"])
        self.assertTrue(any(row["operation"] == "delete" and row["target"]["id"] == first["cycle_id"]
                            for row in self.events()))

    def test_deleted_binding_does_not_block_refresh_or_reclose(self):
        first = self.finished()
        folder = Path(first["cycle_dir"])
        (folder / ".cycle.json").unlink()
        (folder / "artifacts/plans/cycle/REPORT.md").write_text("# Result\nedited\n")
        result = P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertTrue(result["refreshed"], result)
        self.assertFalse((folder / ".cycle.json").exists())
        self.assertTrue(any(row["kind"] == "artifact" and row["operation"] == "update" for row in self.events()))

    def test_deleted_binding_and_payload_can_then_be_moved_once(self):
        for cross_campaign in (False, True):
            with self.subTest(cross_campaign=cross_campaign):
                first = self.finished(slug=f"binding-move-{cross_campaign}")
                other = self.finished("destination", f"other-{cross_campaign}")
                old = Path(first["cycle_dir"])
                (old / ".cycle.json").unlink()
                (old / "artifacts/plans/cycle/REPORT.md").unlink()
                parent = Path(other["cycle_dir"]).parent if cross_campaign else old.parent
                moved = parent / f"renamed-{old.name}"
                old.rename(moved)
                P.list_campaign_summaries(self.root)
                record = P.read_cycle_record(self.root, first["cycle_id"])
                self.assertEqual(record["locator"], moved.name)
                self.assertEqual(record["campaign_id"], other["campaign_id"] if cross_campaign else first["campaign_id"])
                moves = lambda: [row for row in self.events() if row["field"] == ("campaign" if cross_campaign else "path")
                                 and row["target"]["id"] == first["cycle_id"]]
                self.assertEqual(len(moves()), 1)
                P.list_campaign_summaries(self.root)
                self.assertEqual(len(moves()), 1)
                self.assertFalse((moved / "artifacts/plans/cycle/REPORT.md").exists())
                self.assertTrue(P.finalize(self.root, cycle_id=first["cycle_id"])["refreshed"])


if __name__ == "__main__":
    unittest.main()
