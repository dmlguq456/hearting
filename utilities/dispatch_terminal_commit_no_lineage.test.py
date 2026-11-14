"""D-9 empty producer settlement uses the ordinary owner controller."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import artifact_producer as P
import dispatch_terminal_commit as T
from dispatch_completion_join import exact_attempt_row

spec = importlib.util.spec_from_file_location(
    "terminal_fixture", Path(__file__).with_name("dispatch_terminal_commit.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


class NoLineageSettlementTest(unittest.TestCase):
    def fixture(self, harness="codex"):
        f = F.PRODUCER_FIXTURE.TerminalTransactionIntegrationTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        owner = "att-" + uuid.uuid4().hex
        report = f.root / "inputs" / "report.md"
        report.parent.mkdir()
        report.write_text("# Result\nVerified external report.\n")
        f.activate()
        R = F.PRODUCER_FIXTURE.R
        route = R.compose_route(capability="autopilot-code", capability_mode="dev",
            shape="solo", graph=None, slug="no-lineage-fixture", cwd=R.ROOT, artifact_root=f.root,
            intensity="quick", unassigned=True,
            registered_headless_evidence=F.PRODUCER_FIXTURE.registered_headless())
        path = Path(F.PRODUCER_FIXTURE.L.admit_runtime_route(f.root, route).route_file)
        jobs = f.jobs
        node = route["nodes"][0]
        metadata = dict(attempt_id=owner, worker_type="owner", unit="_kernel/owner",
            dispatch_depth="1", registered_worker="1", harness=harness,
            capability="autopilot-code", capability_mode="dev", intensity="quick",
            route_file=str(path), route_id=route["route_id"], route_hash=route["route_hash"],
            route_node=node["id"], registry_digest=route["registry_digest"],
            write_scope=";".join(node["write_scope"]), completion_gate=node["completion_gate"])
        jobs.write_text(f"2026-11-14T00:00:00Z\topen\t{R.ROOT}\t{R.ROOT}\towner\t" +
            ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")
        cycle = P.begin(f.root, route_file=path, capability="autopilot-code", intensity="quick",
                        jobs=jobs, owner_attempt_id=owner)
        request = T.TerminalCommitRequest(path, owner, jobs, f.root)
        text = f"artifact: {report}\nverdict: PASS\nblocker: none"
        log = jobs.parent / "owner.jsonl"
        log.write_text("\n".join(json.dumps(r) for r in
            F.OwnerTerminalPlacementReplayTest.NATIVE[harness](text)) + "\n")
        jobs.write_text(jobs.read_text().replace("worker_type=owner",
            f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
        f._closed_owner(jobs, owner)
        env = mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs),
                                          "AGENT_ARTIFACT_ROOT": str(f.root)})
        env.start()
        self.addCleanup(env.stop)
        return f, route, jobs, cycle, request, report, exact_attempt_row(jobs, owner).metadata

    def test_empty_cycle_completes_once_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                f, route, jobs, cycle, request, report, meta = self.fixture(harness)
                result = T.settle_owner_completion(jobs, "done", meta)
                self.assertEqual(result.result, "completed", result)
                record = P.read_cycle_record(f.root, cycle["cycle_id"])
                self.assertEqual(record["state"], "no-lineage")
                self.assertIsNone(record.get("manifest_digest"))
                self.assertFalse(Path(cycle["cycle_dir"]).exists())
                self.assertEqual(T.owner_completion_state(jobs, "done", meta).state, "complete")
                self.assertEqual(T.settle_owner_completion(jobs, "done", meta).result, "completed")
                import work_intent
                summary = (T._commit_state_path(request).parent / work_intent.SUMMARY_NAME).read_text()
                self.assertIn(str(report), summary)
                self.assertNotIn("artifacts sealed", summary.lower())
                fleet_spec = importlib.util.spec_from_file_location("empty_route_visibility",
                    Path(__file__).resolve().parents[1] / "tools/fleet/route_visibility.py")
                fleet = importlib.util.module_from_spec(fleet_spec)
                fleet_spec.loader.exec_module(fleet)
                view = fleet.describe(route)
                self.assertEqual(view["stages"][0]["state"], "done")
                self.assertIn("gate ✓", fleet.summary_text(view))
                self.assertIn("one-shot done", str(fleet.detail_lines(view)))

    def checkpoint(self):
        data = self.fixture()
        f, route, jobs, cycle, request, report, meta = data
        interrupted = T.settle_terminal_commit(request,
            T.TerminalCommitServices(crash_after="finalize-after"))
        self.assertEqual(interrupted.detail, "crash-after-finalize", interrupted)
        self.assertEqual(json.loads(T._commit_state_path(request).read_text())["state"],
                         "producer-finalized")
        self.assertEqual(T.owner_completion_state(jobs, "done", meta).state, "pending")
        return data

    def test_checkpoint_retry_rejects_changed_terminal_bytes_and_verdict(self):
        f, route, jobs, cycle, request, report, meta = self.checkpoint()
        original = report.read_bytes()
        report.write_bytes(original + b"tampered")
        self.assertNotEqual(T.settle_owner_completion(jobs, "done", meta).result, "completed")
        report.write_bytes(original)
        log = Path(meta["log_file"])
        native = log.read_text()
        log.write_text(native.replace("verdict: PASS", "verdict: FAIL"))
        rejected = T.settle_owner_completion(jobs, "done", meta)
        self.assertTrue(rejected is None or rejected.result != "completed", rejected)
        log.write_text(native)
        first = T.settle_owner_completion(jobs, "done", meta)
        self.assertEqual(first.result, "completed", first)
        commit = T._commit_state_path(request).read_bytes()
        second = T.settle_owner_completion(jobs, "done", meta)
        self.assertEqual(second.result, "completed", second)
        self.assertEqual(T._commit_state_path(request).read_bytes(), commit)

    def test_no_lineage_requires_exact_identity_and_absence_on_live_and_historical_reads(self):
        f, route, jobs, cycle, request, report, meta = self.checkpoint()
        binding = T.load_producer_binding(artifact_root=f.root, route_id=route["route_id"],
            owner_attempt_id=request.owner_attempt_id).binding
        record_path = P.cycle_record_path(f.root, cycle["cycle_id"])
        original = record_path.read_bytes()
        record = json.loads(original)
        for field, value in (("state", "unknown"), ("state", "open"), ("sealed_on", None),
                             ("manifest_digest", "sha256:unexpected"), ("producer_id", "prod_" + "0" * 32)):
            with self.subTest(field=field, value=value):
                record_path.write_text(json.dumps({**record, field: value}))
                self.assert_refused(f, cycle, binding)
                record_path.write_bytes(original)
        wrong_binding = {**binding, "cycle_record_digest": "sha256:" + "0" * 64}
        self.assert_refused(f, cycle, wrong_binding)
        for invalid in (None, b"{", b"[]"):
            with self.subTest(record=invalid):
                record_path.unlink()
                if invalid is not None:
                    record_path.write_bytes(invalid)
                self.assert_refused(f, cycle, binding)
                record_path.write_bytes(original)
        directory = Path(cycle["cycle_dir"])
        for kind in ("empty-directory", "manifest", "output", "dangling-symlink"):
            with self.subTest(kind=kind):
                if kind == "dangling-symlink":
                    directory.symlink_to(directory.parent / "absent-target")
                else:
                    directory.mkdir()
                    if kind != "empty-directory":
                        (directory / ("manifest.json" if kind == "manifest" else "output.md")).write_text("{}")
                self.assert_refused(f, cycle, binding)
                if directory.is_symlink():
                    directory.unlink()
                else:
                    for path in directory.iterdir():
                        path.unlink()
                    directory.rmdir()
        for verify in (P.verify_finalized_cycle, P.observe_finalized_cycle):
            result = verify(f.root, cycle_id=cycle["cycle_id"], expected_binding=binding)
            self.assertEqual(result["status"], "no-lineage")
            self.assertIs(result["lineage_committed"], False)
        self.assertEqual(T.settle_owner_completion(jobs, "done", meta).result, "completed")

    def assert_refused(self, f, cycle, binding):
        for verify in (P.verify_finalized_cycle, P.observe_finalized_cycle):
            with self.subTest(reader=verify.__name__):
                with self.assertRaises((P.ProducerError, OSError, ValueError, TypeError, AttributeError)):
                    verify(f.root, cycle_id=cycle["cycle_id"], expected_binding=binding)


if __name__ == "__main__":
    unittest.main()
