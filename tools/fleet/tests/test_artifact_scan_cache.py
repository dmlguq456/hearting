"""Fleet inventory reuse must observe ordinary artifact edits and cleanup."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "utilities"))
import artifact_reader as reader
from fleet import model
from fleet.collectors import dispatch
from fleet.tests.test_fleet_hot_path import _build_root, CAMP, CYC


class InventoryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _build_root(Path(self.tmp.name) / "reports")
        self.cache = reader.ReadCache()
        self.scan = mock.patch.object(reader.artifact_locator, "scan_index",
                                      wraps=reader.artifact_locator.scan_index).start()
        self.addCleanup(mock.patch.stopall)

    def query(self):
        with reader.read_scope(self.cache):
            first = reader.glob_bucket(self.root, "plans", "*_hot-path")
            self.assertEqual(reader.glob_bucket(self.root, "plans", "*_hot-path"), first)
            return first

    def test_one_scan_per_tick_and_unchanged_tick_reuses_records(self):
        first = self.query()
        self.assertEqual(self.query(), first)
        self.assertEqual(self.scan.call_count, 1)
        # Generated INDEX is neither a prerequisite nor identity authority.
        index = self.root / "campaigns" / "INDEX.json"
        index.write_text("invalid user-edited derived index")
        self.assertEqual(self.query(), first)
        index.unlink()
        self.assertEqual(self.query(), first)

    def test_qa_payload_edits_and_deletion_are_live_without_record_rescan(self):
        plan = self.root / "plans" / "2026-09-08_hot-path"
        (plan / "plan").mkdir(parents=True)
        state = plan / "pipeline_state.yaml"
        job = {"slug": "hot-path", "artifact_root": str(self.root)}
        for qa in ("light", "thorough"):
            state.write_text("qa_level: " + qa + "\n")
            with reader.read_scope(self.cache):
                self.assertEqual(dispatch.resolve_plan_qa_artifact(job), qa)
        state.unlink()
        with reader.read_scope(self.cache):
            self.assertIsNone(dispatch.resolve_plan_qa_artifact(job))
        self.assertEqual(self.scan.call_count, 0)

    def test_cycle_qa_uses_explicit_identity_and_never_reads_manifest(self):
        route_id = "rt-" + "c" * 16
        cycle_id = "cyc_" + "d" * 32
        campaign_id = "camp_" + "e" * 32
        cycles = self.root / ".runtime" / "artifact-producer" / "v1" / "cycles"
        campaigns = self.root / ".runtime" / "artifact-producer" / "v1" / "campaigns"
        cycles.mkdir(parents=True)
        campaigns.mkdir(parents=True)
        (cycles / (cycle_id + ".json")).write_text(json.dumps({
            "cycle_id": cycle_id, "campaign_id": campaign_id, "route_id": route_id,
            "locator": "2026-10-20_hot-path",
        }))
        (campaigns / (campaign_id + ".json")).write_text(json.dumps({
            "campaign_id": campaign_id, "locator": "fleet-fixture",
        }))
        plan = self.root / "campaigns" / "fleet-fixture" / "2026-10-20_hot-path" / "artifacts" / "plans" / "2026-10-20_hot-path"
        (plan / "plan").mkdir(parents=True)
        state = plan / "pipeline_state.yaml"
        state.write_text("qa_level: standard\n")
        job = {"slug": "hot-path", "artifact_root": str(self.root),
               "route_id": route_id, "cycle_id": cycle_id}
        with mock.patch.object(dispatch, "_read_cycle_record", wraps=dispatch._read_cycle_record) as read:
            self.assertEqual(dispatch.resolve_plan_qa_artifact(job), "standard")
        self.assertEqual(read.call_count, 2)
        self.assertIsNone(dispatch.resolve_plan_qa_artifact(dict(job, route_id="rt-other")))
        self.assertIsNone(dispatch.resolve_plan_qa_artifact(dict(job, cycle_id="cyc_" + "f" * 32)))

    def _write_cycle(self, route_id, cycle_id, campaign_id="camp_" + "e" * 32, qa="standard"):
        producer = self.root / ".runtime" / "artifact-producer" / "v1"
        (producer / "cycles").mkdir(parents=True, exist_ok=True)
        (producer / "campaigns").mkdir(parents=True, exist_ok=True)
        locator = "2026-10-20_" + cycle_id[-6:]
        (producer / "cycles" / (cycle_id + ".json")).write_text(json.dumps({
            "cycle_id": cycle_id, "campaign_id": campaign_id, "route_id": route_id,
            "locator": locator,
        }))
        (producer / "campaigns" / (campaign_id + ".json")).write_text(json.dumps({
            "campaign_id": campaign_id, "locator": "fleet-fixture",
        }))
        plan = (self.root / "campaigns" / "fleet-fixture" / locator / "artifacts" / "plans"
                / "2026-10-20_hot-path")
        (plan / "plan").mkdir(parents=True)
        (plan / "pipeline_state.yaml").write_text("qa_level: %s\n" % qa)
        return plan

    def test_route_hint_selects_only_a_record_bound_to_that_route(self):
        route_id, other_route = "rt-" + "c" * 16, "rt-" + "9" * 16
        self._write_cycle(route_id, "cyc_" + "d" * 32)
        self._write_cycle(other_route, "cyc_" + "a" * 32, qa="light")
        job = {"slug": "hot-path", "artifact_root": str(self.root), "route_id": route_id}
        with mock.patch.object(dispatch, "_read_cycle_hints",
                               return_value={route_id: ["cyc_" + "d" * 32 + ".json"]}):
            self.assertEqual(dispatch.resolve_plan_qa_artifact(job), "standard")
        # A stale hint naming another route's record does not lend that route's QA.
        with mock.patch.object(dispatch, "_read_cycle_hints",
                               return_value={route_id: ["cyc_" + "a" * 32 + ".json"]}):
            self.assertIsNone(dispatch.resolve_plan_qa_artifact(job))

    def test_cycle_and_legacy_plan_together_are_not_one_exact_answer(self):
        route_id, cycle_id = "rt-" + "c" * 16, "cyc_" + "d" * 32
        self._write_cycle(route_id, cycle_id)
        job = {"slug": "hot-path", "artifact_root": str(self.root),
               "route_id": route_id, "cycle_id": cycle_id}
        self.assertEqual(dispatch.resolve_plan_qa_artifact(job), "standard")
        legacy = self.root / "plans" / "2026-09-08_hot-path"
        (legacy / "plan").mkdir(parents=True)
        (legacy / "pipeline_state.yaml").write_text("qa_level: light\n")
        self.assertIsNone(dispatch.resolve_plan_qa_artifact(job))
        # A row without route or cycle identity still reads the legacy plan alone.
        self.assertEqual(dispatch.resolve_plan_qa_artifact(
            {"slug": "hot-path", "artifact_root": str(self.root)}), "light")

    def test_cycle_and_campaign_rename_delete_and_recreate(self):
        first = self.query()[0]
        cycle = first.parents[2]
        moved = cycle.with_name("ordinary-moved-cycle")
        cycle.rename(moved)
        self.assertEqual(self.query()[0], moved / "artifacts" / "plans" / first.name)
        campaign = moved.parent.parent
        moved_campaign = campaign.with_name("ordinary-moved-campaign")
        campaign.rename(moved_campaign)
        self.assertTrue(self.query()[0].is_relative_to(moved_campaign))
        shutil.rmtree(moved_campaign)
        self.assertEqual(self.query(), [])
        _build_root(self.root)
        self.assertEqual(self.query(), [first])
        self.assertEqual(self.scan.call_count, 5)

    def test_record_edit_replacement_and_delete_invalidate(self):
        record = self.root / ".runtime" / "artifact-producer" / "v1" / "cycles" / (CYC + ".json")
        record.parent.mkdir(parents=True)
        data = {"cycle_id": CYC, "campaign_id": CAMP, "title": "first"}
        record.write_text(json.dumps(data))
        self.query()
        original = record.stat()
        data["title"] = "other"  # same byte length, even with mtime restored
        record.write_text(json.dumps(data))
        os.utime(record, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.query()
        self.assertEqual(self.cache.entries[str(self.root)][1][1][CYC]["title"], "other")
        replacement = record.with_suffix(".new")
        data["title"] = "third"
        replacement.write_text(json.dumps(data))
        replacement.replace(record)
        self.query()
        self.assertEqual(self.cache.entries[str(self.root)][1][1][CYC]["title"], "third")
        record.unlink()
        self.query()
        self.assertNotIn(CYC + ".json", [p.name for p in record.parent.iterdir()])
        self.assertEqual(self.scan.call_count, 4)

    def test_stat_failure_and_concurrent_edit_do_not_retain_stale_cache(self):
        self.query()
        with mock.patch.object(reader, "_record_stamp", side_effect=PermissionError()):
            self.query()
            self.query()
        self.assertEqual(self.scan.call_count, 3)
        self.assertEqual(self.cache.entries, {})
        stamps = reader._record_stamp(self.root)
        with mock.patch.object(reader, "_record_stamp", side_effect=[stamps, {"changed": None}]):
            self.query()
        self.assertEqual(self.cache.entries, {})
        self.query()
        self.assertEqual(self.scan.call_count, 5)

    def test_deleted_bucket_mid_tick_does_not_break_reader(self):
        with reader.read_scope(self.cache):
            bucket = reader.cycle_bucket_dirs(self.root, "plans")[0][0]
            shutil.rmtree(bucket)
            self.assertEqual(reader.glob_bucket(self.root, "plans", "*"), [])
        self.assertEqual(self.query(), [])


class CollectorCacheTest(unittest.TestCase):
    def test_proc_scan_does_not_resolve_plan_qa_before_retention(self):
        with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[
                "123 claude 00:01 claude -p /autopilot-code"]), \
             mock.patch.object(dispatch.procscan, "read_environ", return_value={}), \
             mock.patch.object(dispatch.os, "readlink", return_value="/fixture/owner"), \
             mock.patch.object(dispatch.procscan, "read_proc_start", return_value="123"), \
             mock.patch.object(dispatch, "_claude_job_model", return_value=None), \
             mock.patch.object(dispatch, "resolve_plan_qa_artifact", side_effect=AssertionError("early QA scan")):
            rows = dispatch._scan_processes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].qa_source, "default")

    def test_retained_live_row_resolves_qa_and_hidden_dead_row_does_not(self):
        live = model.DispatchJob(key="code", slug="live", source="proc", harness="codex")
        dead = model.DispatchJob(key="code", slug="old", source="proc", harness="codex")
        with mock.patch.object(dispatch, "_scan_processes", return_value=[live, dead]), \
             mock.patch.object(dispatch, "_candidate_jobs_paths", return_value=[]), \
             mock.patch.object(dispatch, "_dispatch_liveness", side_effect=["working", "dead"]), \
             mock.patch.object(dispatch, "resolve_plan_qa_artifact", return_value="light") as qa:
            rows = dispatch.collect()
        self.assertEqual(qa.call_count, 1)
        self.assertEqual(qa.call_args.args[0]["slug"], "live")
        self.assertEqual(rows[0].qa, "light")
        self.assertIsNone(rows[1].qa)


if __name__ == "__main__":
    unittest.main()
