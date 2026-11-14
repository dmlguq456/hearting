"""D-9 empty producer settlement uses the ordinary owner controller."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
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
    def fixture(self, harness="codex", f=None):
        if f is None:
            f = F.PRODUCER_FIXTURE.TerminalTransactionIntegrationTest()
            f.setUp()
            self.addCleanup(f.doCleanups)
            f.activate()
        owner = "att-" + uuid.uuid4().hex
        report = f.root / "inputs" / (owner + ".md")
        report.parent.mkdir(exist_ok=True)
        report.write_text("# Result\nVerified external report.\n")
        R = F.PRODUCER_FIXTURE.R
        route = R.compose_route(capability="autopilot-code", capability_mode="dev",
            shape="solo", graph=None, slug="no-lineage-fixture", cwd=R.ROOT, artifact_root=f.root,
            intensity="quick", unassigned=True,
            work_request={"text": owner, "owner_harness": harness},
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
        with jobs.open("a") as stream:
            stream.write(f"2026-11-14T00:00:00Z\topen\t{R.ROOT}\t{R.ROOT}\towner\t" +
            ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")
        cycle = P.begin(f.root, route_file=path, capability="autopilot-code", intensity="quick",
                        jobs=jobs, owner_attempt_id=owner)
        request = T.TerminalCommitRequest(path, owner, jobs, f.root)
        text = f"artifact: {report}\nverdict: PASS\nblocker: none"
        log = jobs.parent / (owner + ".jsonl")
        log.write_text("\n".join(json.dumps(r) for r in
            F.OwnerTerminalPlacementReplayTest.NATIVE[harness](text)) + "\n")
        jobs.write_text(jobs.read_text().replace(f"attempt_id={owner},worker_type=owner",
            f"attempt_id={owner},attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
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

    def test_no_lineage_requires_exact_identity_on_live_and_historical_reads(self):
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
        for verify in (P.verify_finalized_cycle, P.observe_finalized_cycle):
            result = verify(f.root, cycle_id=cycle["cycle_id"], expected_binding=binding)
            self.assertEqual(result["status"], "no-lineage")
            self.assertIs(result["lineage_committed"], False)
        self.assertEqual(T.settle_owner_completion(jobs, "done", meta).result, "completed")

    def assert_complete(self, jobs, meta):
        self.assertEqual(T.owner_completion_state(jobs, "done", meta).state, "complete")
        self.assertFalse(T.owner_completion_pending(jobs, "done", meta))
        self.assertIsNotNone(T.completed_owner_handoff(jobs, "done", meta))
        result = T.settle_owner_completion(jobs, "done", meta)
        self.assertEqual(result.result, "completed", result)

    def test_same_slug_reuse_preserves_completion_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            for second_output in (False, True):
                with self.subTest(harness=harness, second_output=second_output):
                    f, route, jobs, cycle, request, report, meta = self.fixture(harness)
                    self.assertEqual(T.settle_owner_completion(jobs, "done", meta).result, "completed")
                    self.assert_complete(jobs, meta)
                    _, route2, _, cycle2, _, _, meta2 = self.fixture(harness, f=f)
                    self.assertNotEqual(route["route_id"], route2["route_id"])
                    self.assertEqual(cycle["cycle_dir"], cycle2["cycle_dir"])
                    output = Path(cycle2["cycle_dir"]) / "artifacts" / "second.md"
                    output.write_text("second cycle output\n")
                    self.assert_complete(jobs, meta)
                    if not second_output:
                        output.unlink()
                    result = T.settle_owner_completion(jobs, "done", meta2)
                    self.assertEqual(result.result, "completed", result)
                    self.assert_complete(jobs, meta2)
                    self.assert_complete(jobs, meta)

    def test_ignored_entries_close_once_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            for kind in ("hidden", "symlink", "empty-directory"):
                with self.subTest(harness=harness, kind=kind):
                    f, route, jobs, cycle, request, report, meta = self.fixture(harness)
                    artifacts = Path(cycle["cycle_dir"]) / "artifacts"
                    if kind == "hidden":
                        (artifacts / ".hidden").write_text("ignored\n")
                    elif kind == "symlink":
                        (artifacts / "link").symlink_to(f.root / "missing")
                    else:
                        (artifacts / "empty").mkdir()
                    result = T.settle_owner_completion(jobs, "done", meta)
                    self.assertEqual(result.result, "completed", result)
                    self.assertEqual(P.read_cycle_record(f.root, cycle["cycle_id"])["state"], "no-lineage")
                    self.assert_complete(jobs, meta)

    def test_checkpoint_rejects_sealable_output_but_history_does_not_rescan(self):
        f, route, jobs, cycle, request, report, meta = self.checkpoint()
        directory = Path(cycle["cycle_dir"])
        (directory / "artifacts").mkdir(parents=True)
        output = directory / "artifacts" / "late.md"
        output.write_text("sealable output\n")
        result = T.settle_owner_completion(jobs, "done", meta)
        self.assertNotEqual(result.result, "completed", result)
        self.assertIn("no-lineage-output", result.detail)
        output.unlink()
        result = T.settle_owner_completion(jobs, "done", meta)
        self.assertEqual(result.result, "completed", result)
        (directory / "artifacts").mkdir(parents=True, exist_ok=True)
        output.write_text("later output does not undo completion\n")
        self.assert_complete(jobs, meta)

    def test_checkpoint_reuse_does_not_count_the_next_cycles_output(self):
        f, route, jobs, cycle, request, report, meta = self.checkpoint()
        _, _, _, cycle2, _, _, meta2 = self.fixture(f=f)
        self.assertEqual(cycle["cycle_dir"], cycle2["cycle_dir"])
        (Path(cycle2["cycle_dir"]) / "artifacts" / "second.md").write_text("second\n")
        result = T.settle_owner_completion(jobs, "done", meta)
        self.assertEqual(result.result, "completed", result)
        self.assert_complete(jobs, meta)
        self.assertEqual(T.settle_owner_completion(jobs, "done", meta2).result, "completed")
        self.assert_complete(jobs, meta)

    def assert_refused(self, f, cycle, binding):
        for verify in (P.verify_finalized_cycle, P.observe_finalized_cycle):
            with self.subTest(reader=verify.__name__):
                with self.assertRaises((P.ProducerError, OSError, ValueError, TypeError, AttributeError)):
                    verify(f.root, cycle_id=cycle["cycle_id"], expected_binding=binding)


if __name__ == "__main__":
    unittest.main()
