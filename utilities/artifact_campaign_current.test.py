#!/usr/bin/env python3
"""Current campaign observation and exact optional goal completion fixtures."""
import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import artifact_campaign as C
import artifact_identity as I
import artifact_admission as A
import artifact_meta as M
import artifact_producer as P
import dispatch_terminal_commit as T


class CurrentExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.campaign = "camp_" + "c" * 32
        self.root_id, self.repo_id = "root_" + "a" * 32, "repo_" + "b" * 32
        self.identity = self.root / A.ADMISSION_REL / "root-identity.json"
        self.identity.parent.mkdir(parents=True)
        self.identity.write_text(json.dumps(I.RootIdentity(1, self.root_id, self.repo_id,
                                  "2026-10-10T00:00:00Z", "artifact-producer/v1").to_payload()))
        self.directory = self.root / "campaigns/example"
        self.directory.mkdir(parents=True)
        self.record = self.directory / "campaign.json"
        self.record.write_text(json.dumps({"campaign_id": self.campaign, "state": "active"}))

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def export(self):
        before = self.snapshot()
        result = C.export_current(self.root)
        self.assertEqual(self.snapshot(), before)
        return result

    def mark(self, **changes):
        doc = {"contract": M.META_CONTRACT, "schema_version": 1,
               "artifact_root_id": self.root_id, "repository_id": self.repo_id,
               "campaign_id": self.campaign, "campaign": {"presentation_kind": "archive_bundle"}}
        doc.update(changes)
        (self.directory / "meta.json").write_text(json.dumps(doc))

    def test_valid_absent_and_input_bytes(self):
        result = self.export()
        row = result["campaigns"][0]
        self.assertEqual((result["status"], row["state"], row["presentation_status"]),
                         ("valid", "active", "absent"))
        for item in row["inputs"]:
            if "sha256" in item:
                raw = (self.root / item["path"]).read_bytes()
                self.assertEqual(item["sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertIn(self.identity.relative_to(self.root).as_posix(), [i["path"] for i in row["inputs"]])
        self.assertIn({"path": "campaigns/example/campaign.satisfied.json", "missing": True}, row["inputs"])

    def test_missing_and_malformed_identity_with_nonempty_campaign(self):
        self.identity.unlink()
        result = self.export()
        self.assertEqual((result["status"], result["reason"]), ("missing", "root-identity-missing"))
        self.assertIsNone(result["campaigns"][0]["state"])
        self.identity.write_text("{")
        result = self.export()
        self.assertEqual((result["status"], result["reason"]), ("invalid", "root-identity-invalid"))
        self.assertIn("sha256", next(i for i in result["inputs"] if i["path"].endswith("root-identity.json")))

    def test_special_inputs_are_errors_without_waiting_for_a_fifo_writer(self):
        original = self.record.read_bytes()
        self.record.unlink()
        os.mkfifo(self.record)
        row = self.export()["campaigns"][0]
        self.assertEqual((row["status"], row["reason"], row["state"]),
                         ("invalid", "campaign-input-kind-or-size", None))
        self.assertEqual(P.list_campaign_summaries(self.root), [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = P.main(["campaign-status", "--artifact-root", str(self.root),
                           "--campaign", self.campaign])
        self.assertNotEqual(code, 0)
        self.assertIn("campaign-input-kind-or-size", output.getvalue())
        self.record.unlink()
        self.record.write_bytes(original)
        os.mkfifo(self.directory / "meta.json")
        row = self.export()["campaigns"][0]
        self.assertEqual((row["status"], row["state"]), ("valid", "active"))
        self.assertEqual((row["presentation_status"], row["presentation_reason"]),
                         ("invalid", "campaign-input-kind-or-size"))

    def test_missing_and_malformed_record_are_distinct(self):
        self.record.unlink()
        self.assertEqual(self.export()["campaigns"][0]["status"], "missing")
        self.record.write_text("{")
        row = self.export()["campaigns"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertIn("sha256", next(i for i in row["inputs"] if i["path"].endswith("campaign.json")))

    def test_raw_state_and_enum_boundary(self):
        for state in (None, "", "completed", [], True):
            with self.subTest(state=state):
                self.record.write_text(json.dumps({"campaign_id": self.campaign, "state": state}))
                row = self.export()["campaigns"][0]
                self.assertEqual(row["record_state"], state)
                self.assertEqual(row["status"], "invalid")
                self.assertIsNone(row["state"])
        self.record.write_text(json.dumps({"campaign_id": self.campaign}))
        row = self.export()["campaigns"][0]
        self.assertIsNone(row["record_state"])
        self.assertEqual(row["state"], "active")

    def test_record_schema_and_ordinary_index_file(self):
        (self.root / "campaigns/index.json").write_text("{}")
        self.assertEqual(self.export()["status"], "valid")
        for value in (True, "1", 2, None):
            self.record.write_text(json.dumps({"campaign_id": self.campaign, "schema_version": value}))
            self.assertEqual(self.export()["campaigns"][0]["reason"], "campaign-schema-invalid")
        self.record.write_text(json.dumps({"campaign_id": self.campaign, "contract": "unknown/v1"}))
        self.assertEqual(self.export()["campaigns"][0]["reason"], "campaign-schema-invalid")

    def test_duplicate_and_foreign_ids(self):
        other = self.root / "campaigns/second"
        other.mkdir()
        (other / "campaign.json").write_bytes(self.record.read_bytes())
        result = self.export()
        self.assertTrue(all(r["reason"] == "campaign-duplicate-id" and r["state"] is None for r in result["campaigns"]))
        (other / "campaign.json").write_text(json.dumps({"campaign_id": "camp_" + "d" * 32,
                                                      "repository_id": "repo_" + "f" * 32}))
        self.assertEqual(self.export()["status"], "conflict")

    def test_presentation_is_independent_and_exact(self):
        self.mark()
        row = self.export()["campaigns"][0]
        self.assertEqual((row["state"], row["presentation_status"]), ("active", "valid"))
        self.mark(repository_id="repo_" + "f" * 32)
        row = self.export()["campaigns"][0]
        self.assertEqual((row["status"], row["presentation_status"]), ("valid", "invalid"))
        self.assertEqual(row["presentation_reason"], "presentation-repository-mismatch")
        for value in (None, [], {}, 3, "archive"):
            self.mark(campaign={"presentation_kind": value})
            self.assertEqual(self.export()["campaigns"][0]["presentation_status"], "invalid")
        self.mark(cycles={"cyc_" + "e" * 32: {"presentation_kind": "archive_bundle"}})
        self.assertEqual(self.export()["campaigns"][0]["presentation_reason"], "presentation-cycle-not-allowed")
        self.mark()
        self.record.write_text(json.dumps({"campaign_id": self.campaign, "state": "completed"}))
        row = self.export()["campaigns"][0]
        self.assertEqual((row["status"], row["presentation_status"]), ("invalid", "valid"))

    def test_metadata_schema_is_integer_and_lifecycle_remains_independent(self):
        for version in (1.0, True, "1", None, 2):
            self.mark(schema_version=version)
            row = self.export()["campaigns"][0]
            self.assertEqual((row["status"], row["state"]), ("valid", "active"))
            self.assertEqual((row["presentation_status"], row["presentation_reason"]),
                             ("invalid", "presentation-contract-unknown"))

    def test_capture_caches_consumed_bytes_and_detects_restore(self):
        reads = C.CampaignReads(self.root)
        original = self.record.read_bytes()
        self.assertEqual(reads.read_json(self.root, self.record)[1], original)
        self.record.write_text('{"campaign_id":"changed"}')
        self.assertEqual(reads.read_json(self.root, self.record)[1], original)
        self.record.write_bytes(original)
        self.assertIn("campaigns/example/campaign.json", reads.check_mutation())

    def test_mutation_of_every_consumed_input_is_conflict(self):
        self.mark()
        original = C.CampaignReads.check_mutation
        actions = (
            lambda: self.record.write_text(self.record.read_text() + " "),
            lambda: self.identity.write_text(self.identity.read_text() + " "),
            lambda: (self.directory / "meta.json").write_text("{}"),
            lambda: (self.directory / C.EVENTS_DIR).mkdir(exist_ok=True),
            lambda: (self.root / "campaigns/new").mkdir(),
        )
        for action in actions:
            with self.subTest(action=action):
                def changed(reader):
                    action()
                    return original(reader)
                with mock.patch.object(C.CampaignReads, "check_mutation", changed):
                    result = C.export_current(self.root)
                self.assertEqual((result["status"], result["reason"]), ("conflict", "input-changed"))
                self.assertTrue(all(r["status"] != "valid" or r["locator"] != "example" for r in result["campaigns"]))

    def test_observation_time_is_not_part_of_digest(self):
        first, second = self.export(), self.export()
        self.assertEqual(first["inputs"], second["inputs"])


class GoalParsingTests(unittest.TestCase):
    def test_only_explicit_owner_primary_judgment_is_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "report.md"
            goal = {"campaign_id": "camp_" + "a" * 32, "verdict": "satisfied"}
            (Path(tmp) / "campaign_goal.json").write_text(json.dumps(goal))
            primary.write_text("example\n```json\n" + json.dumps(goal) + "\n```\n")
            self.assertEqual(T.read_owner_campaign_goal(str(primary)), (None, "no-judgment"))
            primary.write_text("```campaign-goal\n" + json.dumps(goal) + "\n```\n")
            self.assertEqual(T.read_owner_campaign_goal(str(primary))[0]["campaign_id"], goal["campaign_id"])
            primary.write_text(primary.read_text() * 2)
            self.assertEqual(T.read_owner_campaign_goal(str(primary)), (None, "ambiguous-goal"))
            for value in ({**goal, "verdict": "PASS"}, {**goal, "campaign_id": "wrong"},
                          {**goal, "unexpected": True}, {**goal, "reason": []}):
                self.assertEqual(T.parse_campaign_goal(value), (None, "malformed-goal"))


spec = importlib.util.spec_from_file_location("current_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


class GoalCompletionTests(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        route, route_file, self.cycle = self.begin(campaign_key="explicit-goal")
        self.write_output(self.cycle)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.cycle["cycle_id"])
        self.path = Path(self.cycle["cycle_dir"]).parent / "campaign.json"
        self.goal = {"campaign_id": self.cycle["campaign_id"], "verdict": "satisfied", "reason": "goal met"}
        self.request = T.TerminalCommitRequest(route_file, "att-current-goal", self.jobs, self.root,
                                             campaign_goal=self.goal)
        self.binding = {"cycle_id": self.cycle["cycle_id"]}

    def prepare(self):
        T.prepare_campaign_goal(self.request, self.binding, "goal-test-commit")

    def settle(self):
        return T.settle_campaign_goal(self.request, self.binding)

    def test_same_path_foreign_repository_does_not_validate_close(self):
        self.prepare()
        self.assertEqual(self.settle()["status"], "satisfied")
        identity = self.root / A.ADMISSION_REL / "root-identity.json"
        doc = json.loads(identity.read_text())
        doc["repository_id"] = "repo_" + "f" * 32
        identity.write_text(json.dumps(doc))
        exported = C.export_current(self.root)
        row = next(row for row in exported["campaigns"] if row["campaign_id"] == self.cycle["campaign_id"])
        self.assertEqual((row["status"], row["reason"]), ("conflict", "campaign-rename-binding-mismatch"))
        self.assertIsNone(row["state"])

    def test_transient_head_read_retries_before_terminal_claim(self):
        with mock.patch.object(C, "completion_head", side_effect=OSError("temporary unavailable")):
            with self.assertRaisesRegex(T.TerminalCommitError, "goal-input-unavailable"):
                self.prepare()
        self.assertFalse(T.campaign_goal_path(self.request).exists())
        self.prepare()
        self.assertEqual(self.settle()["status"], "satisfied")
        self.assertEqual(self.settle()["status"], "satisfied")
        self.assertEqual(C.campaign_state(self.root, self.path).last_sequence, 1)

    def test_explicit_goal_closes_once_and_no_judgment_does_not_close(self):
        self.assertEqual(self.settle()["reason"], "no-judgment")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")
        self.prepare()
        result = self.settle()
        self.assertEqual(result["status"], "satisfied")
        self.assertEqual(self.settle()["event_id"], result["event_id"])
        self.assertEqual(C.campaign_state(self.root, self.path).last_sequence, 1)

    def test_failure_retains_pending_duty_and_replay_completes(self):
        self.prepare()
        with mock.patch.object(C, "_publish_event", side_effect=OSError("fixture interrupted")):
            self.assertEqual(self.settle()["status"], "pending")
        intent = json.loads(T.campaign_goal_path(self.request).read_text())
        self.assertIsNotNone(intent["event"])
        self.assertEqual(self.settle()["status"], "satisfied")
        self.assertEqual(C.campaign_state(self.root, self.path).last_sequence, 1)

    def test_crash_after_event_before_outcome_then_begin_does_not_reclose(self):
        self.prepare()
        real = C.close_for_completion
        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("after event before outcome")
        with mock.patch.object(C, "close_for_completion", interrupted):
            self.assertEqual(self.settle()["status"], "pending")
        # Normal begin, not a fabricated reopen, creates the next event.
        route = F.compile_for("direct", self.root, slug="next-goal", gate_source="next-goal")
        bound = F.L.admit_runtime_route(self.root, route)
        P.begin(self.root, route_file=Path(bound.route_file), capability="autopilot-code", intensity="direct",
                campaign_id=self.cycle["campaign_id"])
        self.assertEqual(C.campaign_state(self.root, self.path).last_sequence, 2)
        self.assertEqual(self.settle()["status"], "satisfied")
        state = C.campaign_state(self.root, self.path)
        self.assertEqual((state.state, state.last_sequence), ("active", 2))

    def test_cycle_pass_and_foreign_judgment_do_not_close_parent(self):
        foreign = T.TerminalCommitRequest(self.request.route_file, self.request.owner_attempt_id,
                    self.jobs, self.root, campaign_goal={**self.goal, "campaign_id": "camp_" + "f" * 32})
        T.prepare_campaign_goal(foreign, self.binding, "goal-test-commit")
        self.assertEqual(T.settle_campaign_goal(foreign)["reason"], "campaign-mismatch")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")

    def test_open_member_blocks_close_without_losing_duty(self):
        route = F.compile_for("direct", self.root, slug="unfinished-goal", gate_source="unfinished-goal")
        bound = F.L.admit_runtime_route(self.root, route)
        P.begin(self.root, route_file=Path(bound.route_file), capability="autopilot-code", intensity="direct",
                campaign_id=self.cycle["campaign_id"])
        self.prepare()
        result = self.settle()
        self.assertEqual((result["status"], result["reason"]), ("pending", "campaign-cycle-provisional-active"))
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")

    def test_official_mark_writer_preserves_lifecycle_history_and_noop(self):
        immutable = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                     if p.is_file() and p.name in ("manifest.json", "campaign.json", "000001.json")}
        def mark():
            return M.run_write(self.root, lambda ws: M.op_set(ws, self.cycle["campaign_id"], None,
                               {"presentation_kind": "archive_bundle"}), actor_by="agent",
                               reason="approved classification fixture")
        mark()
        for rel, raw in immutable.items():
            self.assertEqual((self.root / rel).read_bytes(), raw)
        doc = json.loads((self.path.parent / "meta.json").read_text())
        current = C.lifecycle.read_root_identity(self.root)
        self.assertEqual((doc["artifact_root_id"], doc["repository_id"], doc["campaign_id"]),
                         (current.artifact_root_id, current.repository_id, self.cycle["campaign_id"]))
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(mark()["status"], "no-change")
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes()
                                 for p in self.root.rglob("*") if p.is_file()})
        for by, cycle, reason in (("model", None, "presentation-model-forbidden"),
                                 ("agent", self.cycle["cycle_id"], "presentation-cycle-not-allowed")):
            with self.assertRaisesRegex(M.MetaError, reason):
                M.run_write(self.root, lambda ws: M.op_set(ws, self.cycle["campaign_id"], cycle,
                            {"presentation_kind": "archive_bundle"}), actor_by=by)

    def test_export_detects_new_event_and_mutation_of_captured_event(self):
        self.prepare()
        self.assertEqual(self.settle()["status"], "satisfied")
        directory = self.path.parent / C.EVENTS_DIR
        event = next(directory.glob("*.json"))
        captured = event.read_bytes()
        real_check = C.CampaignReads.check_mutation
        for addition in (True, False):
            with self.subTest(addition=addition):
                extra = directory / "000002.json"
                def mutate(reads):
                    if addition:
                        extra.write_text("{}")
                    else:
                        event.write_bytes(captured + b" ")
                    return real_check(reads)
                with mock.patch.object(C.CampaignReads, "check_mutation", mutate):
                    result = C.export_current(self.root)
                row = result["campaigns"][0]
                self.assertEqual((result["status"], result["reason"]), ("conflict", "input-changed"))
                self.assertEqual((row["status"], row["state"]), ("conflict", None))
                item = next(i for i in row["inputs"] if i["path"] == event.relative_to(self.root).as_posix())
                self.assertEqual(item["sha256"], "sha256:" + hashlib.sha256(captured).hexdigest())
                extra.unlink(missing_ok=True)
                event.write_bytes(captured)

    def test_all_queries_keep_pending_projection_and_bytes_unchanged(self):
        with mock.patch.object(C, "_materialize", side_effect=OSError("projection interrupted")):
            with self.assertRaisesRegex(C.CampaignError, "committed-recovery-required"):
                C.close(self.root, self.path, reason="explicit goal")
        before = {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        status = C.status(self.root, self.cycle["campaign_id"])
        self.assertEqual((status["state"], status["projection_pending"]), ("satisfied", True))
        rows = P.list_campaign_summaries(self.root, active_only=False)
        row = next(r for r in rows if r["campaign_id"] == self.cycle["campaign_id"])
        self.assertTrue(row["projection_pending"])
        exported = C.export_current(self.root)
        self.assertEqual(exported["campaigns"][0]["state"], "satisfied")
        self.assertTrue(exported["campaigns"][0]["projection_pending"])
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                                 for p in self.root.rglob("*") if p.is_file()})


class NativeOwnerGoalTests(unittest.TestCase):
    def test_metadata_goal_without_primary_judgment_does_not_close(self):
        from dispatch_completion_join import exact_attempt_row
        fixture = F.TerminalTransactionIntegrationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        route, path, jobs, owner, cycle, _review, _request = fixture._prepare_fixture("codex")
        primary = fixture.write_output(cycle, rel="owner-report.md",
            data=b"Only this cycle passed. Campaign goal remains unjudged.\n")
        text = f"artifact: {primary}\nverdict: PASS\nblocker: none"
        log = jobs.parent / "owner.jsonl"
        log.write_text("\n".join(json.dumps(row) for row in (
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed"})) + "\n")
        jobs.write_text(jobs.read_text().replace("worker_type=owner",
            f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
        fixture._closed_owner(jobs, owner)
        metadata = exact_attempt_row(jobs, owner).metadata
        metadata["owner_handoff"] = {"primary": str(primary), "campaign_goal": {
            "campaign_id": cycle["campaign_id"], "verdict": "satisfied"}}
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            request = T._completion_request(jobs, "done", metadata)
            self.assertIsNone(request.campaign_goal)
            result = T.settle_owner_completion(jobs, "done", metadata)
            self.assertEqual(result.result, "completed", result)
            campaign_path = C.campaign_path(fixture.root, cycle["campaign_id"], heal=False)
            self.assertEqual(C.campaign_state(fixture.root, campaign_path).state, "active")

    def test_normal_completion_consumes_exact_owner_artifact_for_all_harnesses(self):
        from dispatch_completion_join import exact_attempt_row
        native = {
            "claude": lambda text: [{"type": "result", "subtype": "success", "is_error": False, "result": text}],
            "codex": lambda text: [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                                   {"type": "turn.completed"}],
            "opencode": lambda text: [{"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": text}},
                                      {"type": "step_finish", "sessionID": "ses_test",
                                       "part": {"type": "step-finish", "reason": "stop"}}],
        }
        for harness in native:
            with self.subTest(harness=harness):
                fixture = F.TerminalTransactionIntegrationTest()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                route, path, jobs, owner, cycle, review, _request = fixture._prepare_fixture(harness)
                goal = {"campaign_id": cycle["campaign_id"], "verdict": "satisfied", "reason": "explicit final judgment"}
                primary = fixture.write_output(cycle, rel="owner-report.md",
                    data=("verified owner result\n```campaign-goal\n" + json.dumps(goal) + "\n```\n").encode())
                text = f"artifact: {primary}\nverdict: PASS\nblocker: none"
                log = jobs.parent / "owner.jsonl"
                log.write_text("\n".join(json.dumps(row) for row in native[harness](text)) + "\n")
                jobs.write_text(jobs.read_text().replace("worker_type=owner",
                    f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
                fixture._closed_owner(jobs, owner)
                metadata = exact_attempt_row(jobs, owner).metadata
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                    request = T._completion_request(jobs, "done", metadata)
                    self.assertEqual(request.campaign_goal["campaign_id"], cycle["campaign_id"])
                    # Interrupt the ordinary controller after transaction settlement
                    # but before the optional goal outcome; the original duty remains.
                    with mock.patch.object(T, "settle_campaign_goal", side_effect=OSError("controller interrupted")):
                        interrupted = T.settle_owner_completion(jobs, "done", metadata)
                    self.assertEqual(interrupted.result, "recoverable")
                    self.assertEqual(T.owner_completion_state(jobs, "done", metadata).state, "pending")
                    result = T.settle_owner_completion(jobs, "done", metadata)
                    self.assertEqual(result.result, "completed", result)
                    self.assertEqual(result.campaign_goal["status"], "satisfied", result)
                    campaign_path = C.campaign_path(fixture.root, cycle["campaign_id"], heal=False)
                    self.assertEqual(C.campaign_state(fixture.root, campaign_path).last_sequence, 1)
                    replay = T.settle_owner_completion(jobs, "done", metadata)
                    self.assertEqual(replay.campaign_goal["event_id"], result.campaign_goal["event_id"])
                    self.assertEqual(C.campaign_state(fixture.root, campaign_path).last_sequence, 1)


if __name__ == "__main__":
    unittest.main()
