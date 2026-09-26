#!/usr/bin/env python3
"""Campaign closure/reopen stream tests on isolated fixture roots."""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_producer as P
import artifact_manifest as M

spec_path = Path(__file__).with_name("artifact_producer.test.py")
import importlib.util
spec = importlib.util.spec_from_file_location("campaign_producer_fixture", spec_path)
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


class CampaignTest(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        route, route_file, self.result = self.begin(campaign_key="campaign-closure")
        self.output = self.write_output(self.result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.result["cycle_id"])
        self.campaign = self.result["campaign_id"]
        self.path = Path(self.result["cycle_dir"]).parent / "campaign.json"
        self.manifest_path = Path(self.result["cycle_dir"]) / "manifest.json"
        self.manifest_bytes = self.manifest_path.read_bytes()

    def _begin(self, *, key=None, campaign=None, parent=None, slug="reopen-cycle"):
        route = F.compile_for("direct", self.root, slug=slug, gate_source=slug)
        binding = F.L.admit_runtime_route(self.root, route)
        return P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                       intensity="direct", campaign_key=key,
                       campaign_id=campaign, parent_cycle_id=parent)

    def _close(self, reason="completion criterion met"):
        return C.close(self.root, self.path, reason=reason)

    def _provisional_child(self, closed):
        route = F.compile_for("direct", self.root, slug="provisional-cycle", gate_source="provisional-input")
        binding = F.L.admit_runtime_route(self.root, route)
        route_file = Path(binding.route_file)
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_id=self.campaign)
        self.write_output(child)
        P.finalize(self.root, cycle_id=child["cycle_id"], allow_open_route=True)
        if closed == "proven":
            self.close(route, route_file)
        elif closed == "unproven":
            F.R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        return route, route_file, child

    def test_runtime_session_identity_order_and_explicit_reopen(self):
        cases = (
            ({"CLAUDE_CODE_SESSION_ID": "claude-session"}, "claude", "claude-session"),
            ({"CODEX_THREAD_ID": "codex-thread", "CODEX_SESSION_ID": "codex-session"},
             "codex", "codex-thread"),
            ({"CODEX_SESSION_ID": "codex-session"}, "codex", "codex-session"),
            ({"OPENCODE_SESSION_ID": "opencode-session"}, "opencode", "opencode-session"),
            ({}, None, None),
        )
        keys = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")
        for index, (values, harness, session) in enumerate(cases):
            with self.subTest(values=values), mock.patch.dict(os.environ, {"AGENT_HARNESS": ""}):
                # Ensure the four runtime identity sources are cleared before each case.
                for key in keys:
                    os.environ.pop(key, None)
                os.environ.update(values)
                if index:
                    C.reopen(self.root, self.path, reason="identity test")
                actor_id, closed_by = C._agent_actor()
                expected = "agent:unknown" if harness is None else f"agent:{harness}:{session}"
                self.assertEqual(actor_id, expected)
                result = self._close("identity provenance test")
                event = json.loads((self.path.parent / C.EVENTS_DIR /
                                    f"{C.campaign_state(self.root, self.path).last_sequence:06d}.json").read_text())
                self.assertEqual(event["actor"]["id"], expected)
                self.assertEqual(event["payload"]["closure"]["closed_by"],
                                 {"harness": harness, "session_id": session})
        # Explicit reopen uses the same derived actor and appends its own event.
        with mock.patch.dict(os.environ, {"AGENT_HARNESS": ""}):
            os.environ["CLAUDE_CODE_SESSION_ID"] = "explicit-reopen-session"
            C.reopen(self.root, self.path, reason="explicit campaign-reopen")
            event = json.loads((self.path.parent / C.EVENTS_DIR /
                                f"{C.campaign_state(self.root, self.path).last_sequence:06d}.json").read_text())
            self.assertEqual(event["event_type"], "campaign.reopened")
            self.assertEqual(event["actor"]["id"], "agent:claude:explicit-reopen-session")

    def _write_v1_close(self, path=None):
        path = Path(path or self.path)
        record = json.loads(path.read_text())
        snapshot = C._snapshot(self.root, path)
        harness, session = "codex", "legacy-session"
        actor_id = "native-user:" + C.hashlib.sha256((harness + ":" + session).encode()).hexdigest()
        approval = {"harness": harness, "session_id": session, "actor_id": actor_id,
                    "statement": "campaign-" + "satisfy " + record["campaign_id"] + " " + C.digest(snapshot),
                    "decision": "accepted", "native_message_digest": "sha256:" + "a" * 64}
        first = snapshot["cycles"][0]
        event = {"stream_id": "strm_" + "1" * 32, "stream_sequence": 1,
                 "event_type": "campaign.satisfied", "target_id": record["campaign_id"],
                 "actor": {"kind": "user", "id": actor_id}, "recorded_at": "2026-09-26T00:00:00Z",
                 "provenance": {"source_manifest_id": first["manifest_id"],
                                "source_revision_id": first["manifest_revision_id"],
                                "producer_route_id": first["route_id"], "schema_version": 1,
                                "algorithm_version": C.CONTRACT_V1, "source_digest": C.digest(snapshot)},
                 "evidence_ids": [],
                 "payload": {"contract": C.CONTRACT_V1, "root": str(self.root),
                             "snapshot": snapshot, "approval": approval}}
        event["event_id"] = C._id("evt", event)
        raw = C.canonical(event) + b"\n"
        (path.parent / C.LEGACY_EVENT_NAME).write_bytes(raw)
        return raw

    def test_a24_1_agent_closes_with_reason_and_v2_event(self):
        with self.assertRaisesRegex(C.CampaignError, "campaign-close-reason-required"):
            C.close(self.root, self.path)
        result = self._close("the planned output is complete")
        self.assertEqual(result["status"], "satisfied")
        event = json.loads((self.path.parent / "campaign.events/000001.json").read_text())
        self.assertEqual(event["event_type"], "campaign.satisfied")
        self.assertEqual(event["actor"]["kind"], "producer")
        self.assertEqual(event["payload"]["closure"]["reason"], "the planned output is complete")
        self.assertEqual(json.loads(self.path.read_text())["state"], "satisfied")
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")

    def test_status_reports_reason_need_commands_and_event_history(self):
        report = C.status(self.root, self.path)
        self.assertTrue(report["closable"])
        self.assertTrue(report["reason_required"])
        self.assertIn("--reason", report["close_command"])
        self._close()
        report = C.status(self.root, self.path)
        self.assertTrue(report["satisfied"])
        self.assertEqual(len(report["events"]), 1)
        self.assertTrue(report["reopen_command"])

    def test_same_key_multiple_closed_campaigns_is_ambiguous(self):
        rows = [{"key": "same", "state": "satisfied", "campaign_id": "camp_" + "1" * 32},
                {"key": "same", "state": "satisfied", "campaign_id": "camp_" + "2" * 32}]
        self.assertEqual(P.classify_campaign_key(rows, "same"),
                         {"mode": "blocked", "code": "campaign-key-reopen-ambiguous"})

    def test_a24_2_close_keeps_snapshot_readiness_gates(self):
        route = F.compile_for("direct", self.root, slug="open-cycle", gate_source="open")
        binding = F.L.admit_runtime_route(self.root, route)
        open_cycle = P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                             intensity="direct", campaign_id=self.campaign)
        with self.assertRaisesRegex(C.CampaignError, "campaign-cycle-not-sealed"):
            C.close(self.root, self.path, reason="done")
        refusal = C.status(self.root, self.path)["close_refusal"]
        self.assertEqual(refusal["reason"], "campaign-cycle-not-sealed")
        self.assertIn(open_cycle["cycle_id"], refusal["detail"])
        self.assertIn("seal-or-abandon-cycle-before-closing-campaign", refusal["detail"])
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")
        self.assertTrue(Path(open_cycle["cycle_dir"]).exists())

    def test_a24_2_each_close_refusal_has_typed_detail_and_next_step(self):
        # Open-route refusal carries the actionable route completion instructions.
        self._provisional_child(None)
        refusal = C.status(self.root, self.path)["close_refusal"]
        self.assertEqual(refusal["reason"], "campaign-cycle-provisional-active")
        self.assertIn("next=", refusal["detail"])
        self.assertIn("campaign-status", refusal["detail"])

        # The remaining refusal classes retain their precise integrity code and
        # identify the cycle or state that must be repaired before closing.
        record = json.loads(self.path.read_text())
        record["cycles"].append("cyc_" + "f" * 32)
        P._write_campaign(self.root, record, exclusive=False)
        refusal = C.status(self.root, self.path)["close_refusal"]
        self.assertEqual(refusal["reason"], "campaign-membership-drift")
        self.assertIn("reconcile-campaign-membership-with-producer-records", refusal["detail"])
        record["cycles"].remove("cyc_" + "f" * 32)
        P._write_campaign(self.root, record, exclusive=False)

        original = C.admission.load_index
        def altered(root):
            value = original(root)
            value.cycles[self.result["cycle_id"]]["manifest_digest"] = "sha256:" + "0" * 64
            return value
        with mock.patch.object(C.admission, "load_index", side_effect=altered):
            refusal = C.status(self.root, self.path)["close_refusal"]
        self.assertEqual(refusal["reason"], "campaign-index-mismatch")
        self.assertIn(self.result["cycle_id"], refusal["detail"])
        self.assertIn("inspect-manifest-cycle-record-and-index-before-retry", refusal["detail"])

    def test_a24_3_key_id_and_parent_begin_reopen_same_campaign(self):
        selectors = ({"key": "campaign-closure"}, {"campaign": self.campaign},
                     {"parent": self.result["cycle_id"]})
        for index, selector in enumerate(selectors):
            self._close()
            result = self._begin(**selector, slug=f"reopen-selector-{index}")
            self.assertEqual(result["campaign_id"], self.campaign)
            self.assertTrue(result["campaign_reopened"])
            self.assertTrue(result["campaign_reopen_event_id"])
            P.finalize(self.root, cycle_id=result["cycle_id"], state="abandoned",
                       abandon_reason="operator-decision", allow_open_route=True)

    def test_a24_4_close_reopen_close_has_valid_append_only_sequence(self):
        self._close()
        v1 = (self.path.parent / "campaign.events/000001.json").read_bytes()
        result = self._begin(campaign=self.campaign)
        self.assertTrue(result["campaign_reopened"])
        reopened_bytes = (self.path.parent / "campaign.events/000002.json").read_bytes()
        self.assertEqual(json.loads(reopened_bytes)["payload"]["reopens_event_id"],
                         json.loads(v1)["event_id"])
        P.finalize(self.root, cycle_id=result["cycle_id"], state="abandoned",
                   abandon_reason="operator-decision", allow_open_route=True)
        C.close(self.root, self.path, reason="second completion")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "satisfied")
        self.assertEqual((self.path.parent / "campaign.events/000001.json").read_bytes(), v1)
        self.assertEqual((self.path.parent / "campaign.events/000002.json").read_bytes(), reopened_bytes)
        self.assertEqual([p.name for p in sorted((self.path.parent / "campaign.events").glob("*.json"))],
                         ["000001.json", "000002.json", "000003.json"])

    def test_a24_5_v1_event_is_sequence_one_and_legacy_bytes_are_unchanged(self):
        old = self.path.parent / C.LEGACY_EVENT_NAME
        raw = self._write_v1_close()
        folded = C.campaign_state(self.root, self.path)
        self.assertEqual((folded.state, folded.last_sequence), ("satisfied", 1))
        C.reopen(self.root, self.path, reason="resume legacy campaign")
        self.assertTrue((self.path.parent / "campaign.events/000002.json").exists())
        self.assertEqual(old.read_bytes(), raw)

    def test_a24_6_metadata_write_and_projection_recovery(self):
        with mock.patch.object(C, "_materialize", side_effect=OSError("crash after event")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-committed-recovery-required"):
                self._close()
        self.assertTrue(C.campaign_state(self.root, self.path).projection_pending)
        C.recover(self.root, self.path)
        record = json.loads(self.path.read_text()); record["title"] = "updated title"
        P._write_campaign(self.root, record, exclusive=False)
        self.assertEqual(json.loads(self.path.read_text())["title"], "updated title")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "satisfied")

    def test_a24_7_compose_listing_and_locator_use_fold(self):
        self._close()
        rows = P.list_campaign_summaries(self.root, active_only=False)
        row = next(item for item in rows if item["campaign_id"] == self.campaign)
        self.assertEqual(row["state"], "satisfied")
        route = F.compile_for("direct", self.root, slug="compose-closed", gate_source="compose",
                              campaign_key="campaign-closure")
        selection = F.R.compose_campaign_selection(route)
        self.assertEqual(selection["mode"], "reopen")
        self.assertIn("닫힌 캠페인 재개", F.R._compose_campaign_line(selection))
        _, view = C.locator.scan_index(self.root)
        self.assertEqual(view[self.campaign]["status"], "satisfied")

    def test_a24_8_abandoned_campaign_is_not_reopened(self):
        record = json.loads(self.path.read_text())
        for state in ("abandoned", "superseded"):
            record["state"] = state
            P._write_campaign(self.root, record, exclusive=False)
            with self.assertRaisesRegex(P.ProducerError, "campaign-not-active"):
                self._begin(campaign=self.campaign, slug="refuse-" + state)

    def test_a24_9_two_process_closers_publish_one_event(self):
        with mock.patch.object(C.os, "link", side_effect=OSError("precommit")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-not-committed"):
                C.close(self.root, self.path, reason="retry after failed publication")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")
        args = [sys.executable, P.__file__, "campaign-close", "--artifact-root", str(self.root),
                "--campaign", str(self.path), "--reason", "concurrent completion"]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _n: subprocess.run(args, text=True, capture_output=True), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = list((self.path.parent / "campaign.events").glob("*.json"))
        self.assertEqual([path.name for path in events], ["000001.json"])
        route = F.compile_for("direct", self.root, slug="parallel-reopen", gate_source="parallel-reopen")
        binding = F.L.admit_runtime_route(self.root, route)
        script = ("import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                  "import artifact_producer as P; r=P.begin(Path(sys.argv[2]),route_file=Path(sys.argv[3]), "
                  "capability='autopilot-code',intensity='direct',campaign_key='campaign-closure'); "
                  "print(json.dumps(r))")
        begin_args = [sys.executable, "-c", script, str(Path(P.__file__).parent), str(self.root), binding.route_file]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            begins = list(pool.map(lambda _n: subprocess.run(begin_args, text=True, capture_output=True), range(2)))
        for result in begins:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reopened = [json.loads(result.stdout) for result in begins]
        self.assertEqual(sum(row.get("campaign_reopened") is True for row in reopened), 1)
        self.assertEqual([path.name for path in sorted((self.path.parent / "campaign.events").glob("*.json"))],
                         ["000001.json", "000002.json"])
        for row in P.list_cycle_records(self.root):
            if row.get("campaign_id") == self.campaign and row.get("state") == "open":
                P.finalize(self.root, cycle_id=row["cycle_id"], state="abandoned",
                           abandon_reason="operator-decision", allow_open_route=True)
        race_route = F.compile_for("direct", self.root, slug="close-begin-race", gate_source="close-begin-race")
        race_binding = F.L.admit_runtime_route(self.root, race_route)
        close_args = [sys.executable, P.__file__, "campaign-close", "--artifact-root", str(self.root),
                      "--campaign", str(self.path), "--reason", "close begin race"]
        begin_args = [sys.executable, "-c", script, str(Path(P.__file__).parent), str(self.root),
                      race_binding.route_file]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            close_future = pool.submit(subprocess.run, close_args, text=True, capture_output=True)
            begin_future = pool.submit(subprocess.run, begin_args, text=True, capture_output=True)
            close_result, begin_result = close_future.result(), begin_future.result()
        self.assertIn(close_result.returncode, (0, 65), close_result.stdout + close_result.stderr)
        self.assertEqual(begin_result.returncode, 0, begin_result.stdout + begin_result.stderr)
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")

    def test_legacy_formatted_seal_and_canonical_index_are_verified_separately(self):
        raw = json.dumps(json.loads(self.manifest_bytes), indent=4, ensure_ascii=False).encode()
        self.manifest_path.write_bytes(raw)
        record = P.read_cycle_record(self.root, self.result["cycle_id"])
        record["manifest_digest"] = "sha256:" + C.hashlib.sha256(raw).hexdigest()
        P._write_cycle_record(self.root, record, exclusive=False)
        report = C.status(self.root, self.path)
        self.assertNotIn("close_refusal", report)
        self.assertNotEqual(report["cycles"][0]["manifest_digest"], report["cycles"][0]["index_digest"])
        self._close("legacy seal remains byte-preserved")
        self.assertEqual(self.manifest_path.read_bytes(), raw)

    def test_index_disagreement_does_not_become_success(self):
        original = C.admission.load_index
        def altered(root):
            value = original(root)
            value.cycles[self.result["cycle_id"]]["manifest_digest"] = "sha256:" + "0" * 64
            return value
        with mock.patch.object(C.admission, "load_index", side_effect=altered):
            report = C.status(self.root, self.path)
        self.assertEqual(report["close_refusal"]["reason"], "campaign-index-mismatch")
        self.assertIn("inspect-manifest-cycle-record-and-index-before-retry",
                      report["close_refusal"]["detail"])

    def test_campaign_runlog_is_digest_bound_metadata_not_a_cycle(self):
        source_cycle = "cyc_" + "f" * 32
        body = b"# aggregate run log\n"
        runlog = self.path.parent / "RUNLOG.md"
        runlog.write_bytes(body)
        record = json.loads(self.path.read_text())
        record["runlog"] = {"contract": "campaign-runlog/v1", "path": "RUNLOG.md",
                            "sha256": "sha256:" + C.hashlib.sha256(body).hexdigest(),
                            "source_cycle_id": source_cycle, "source_locator": "experiments/_RUNLOG.md"}
        P._write_campaign(self.root, record, exclusive=False)
        report = C.status(self.root, self.path)
        self.assertEqual([row["cycle_id"] for row in report["cycles"]], [self.result["cycle_id"]])
        runlog.write_bytes(b"drift\n")
        report = C.status(self.root, self.path)
        self.assertEqual(report["close_refusal"]["reason"], "campaign-runlog-digest-mismatch")

    def test_legacy_cycle_layout_closes_without_moving_or_rewriting_sealed_bytes(self):
        cid, old = self.result["cycle_id"], self.manifest_path.parent
        target = old.parent / "cycles" / cid
        target.parent.mkdir()
        old.rename(target)
        (target / ".cycle.json").unlink()
        record = P.read_cycle_record(self.root, cid)
        record.pop("locator")
        P._write_cycle_record(self.root, record, exclusive=False)
        index = C.admission.load_index(self.root)
        index.cycles[cid]["cycle_path"] = str(target.relative_to(self.root))
        C.admission._write_index(self.root, index)
        C.locator.rebuild_indexes(self.root)
        self._close("legacy layout close")
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "manifest.json").read_bytes(), self.manifest_bytes)

    def test_artifact_drift_open_cycle_and_unlisted_member_are_blocked(self):
        self.output.write_bytes(b"drifted bytes")
        self.assertEqual(C.status(self.root, self.path)["close_refusal"]["reason"],
                         "campaign-artifact-mismatch")
        self.output.write_bytes(b"plan body\n")
        child = self._begin(campaign=self.campaign, slug="unsealed-member")
        self.assertEqual(C.status(self.root, self.path)["close_refusal"]["reason"],
                         "campaign-cycle-not-sealed")
        record = json.loads(self.path.read_text())
        record["cycles"].remove(child["cycle_id"])
        P._write_campaign(self.root, record, exclusive=False)
        self.assertEqual(C.status(self.root, self.path)["close_refusal"]["reason"],
                         "campaign-membership-drift")

    def test_abandoned_is_sealed_not_success_and_residual_is_not_a_gate(self):
        residual = self.path.parent / "retained-notes" / "unclassified.txt"
        residual.parent.mkdir()
        residual.write_text("not a declared cycle")
        child = self._begin(campaign=self.campaign, slug="abandoned-member")
        self.write_output(child, data=b"Abandoned work; no success claimed\n")
        P.finalize(self.root, cycle_id=child["cycle_id"], state="abandoned",
                   abandon_reason="route-unrecoverable", allow_open_route=True)
        report = C.status(self.root, self.path)
        self.assertEqual(sorted(row["state"] for row in report["cycles"]), ["abandoned", "completed"])
        self._close("abandoned work is sealed, not successful")
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["cycle_state"], "abandoned")
        self.assertEqual(residual.read_text(), "not a declared cycle")

    def test_invalid_event_is_refused_before_commit(self):
        record = json.loads(self.path.read_text())
        record["goal"] = "goal" * 20000
        P._write_campaign(self.root, record, exclusive=False)
        before = self.path.read_bytes()
        with self.assertRaises(C.CampaignError) as caught:
            self._close("oversized event must be rejected")
        self.assertEqual(caught.exception.code, "campaign-event-invalid")
        self.assertIn("oversized-payload", str(caught.exception.detail))
        self.assertFalse((self.path.parent / C.EVENTS_DIR / "000001.json").exists())
        self.assertEqual(before, self.path.read_bytes())

    def test_committed_event_corruption_and_native_malformed_input_are_typed(self):
        self._close("create stream event")
        event_path = self.path.parent / C.EVENTS_DIR / "000001.json"
        event = json.loads(event_path.read_text())
        event["recorded_at"] = "2026-09-14T00:00:00Z"
        raw = C.canonical(event) + b"\n"
        event_path.write_bytes(raw)
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-event-invalid")
        self.assertEqual(raw, event_path.read_bytes())

    def test_crash_before_publish_and_concurrent_retry(self):
        with mock.patch.object(C.os, "link", side_effect=OSError("precommit")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-not-committed"):
                self._close("retry after failed publication")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")
        self.assertFalse((self.path.parent / C.EVENTS_DIR / "000001.json").exists())
        args = [sys.executable, P.__file__, "campaign-close", "--artifact-root", str(self.root),
                "--campaign", str(self.path), "--reason", "concurrent retry"]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _n: subprocess.run(args, text=True, capture_output=True), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([p.name for p in sorted((self.path.parent / C.EVENTS_DIR).glob("*.json"))],
                         ["000001.json"])

    def test_symlink_payload_and_symlink_event_are_not_followed(self):
        data = self.output.read_bytes()
        foreign = Path(self._tmp.name) / "foreign-payload"
        foreign.write_bytes(data)
        self.output.unlink()
        self.output.symlink_to(foreign)
        self.assertEqual(C.status(self.root, self.path)["close_refusal"]["reason"], "campaign-symlink")
        self.output.unlink()
        self.output.write_bytes(data)
        self._close("stream symlink test")
        event_dir = self.path.parent / C.EVENTS_DIR
        event = event_dir / "000001.json"
        event.unlink()
        event.symlink_to(foreign)
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-symlink")
        event.unlink()
        event_dir.rename(self.path.parent / "campaign.events.saved")
        (self.path.parent / C.EVENTS_DIR).symlink_to(self.path.parent / "campaign.events.saved",
                                                       target_is_directory=True)
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-symlink")

        # Legacy v1 event links are independently refused before reading targets.
        other_route = F.compile_for("direct", self.root, slug="v1-link", gate_source="v1-link")
        other_binding = F.L.admit_runtime_route(self.root, other_route)
        other = P.begin(self.root, route_file=Path(other_binding.route_file), capability="autopilot-code",
                        intensity="direct", campaign_key="v1-link")
        self.write_output(other)
        self.close(other_route, Path(other_binding.route_file))
        P.finalize(self.root, cycle_id=other["cycle_id"])
        other_path = Path(other["cycle_dir"]).parent / "campaign.json"
        (other_path.parent / C.LEGACY_EVENT_NAME).symlink_to(foreign)
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, other_path)
        self.assertEqual(caught.exception.code, "campaign-symlink")
        self.assertEqual(foreign.read_bytes(), data)

    def test_readiness_drift_while_waiting_for_lock_preserves_state(self):
        original = C.admission._acquire_lock
        def acquire(*args, **kwargs):
            fd = original(*args, **kwargs)
            self.output.write_bytes(b"changed while waiting")
            return fd
        with mock.patch.object(C.admission, "_acquire_lock", side_effect=acquire):
            with self.assertRaisesRegex(C.CampaignError, "campaign-artifact-mismatch"):
                self._close("close after lock")
        self.assertFalse((self.path.parent / C.EVENTS_DIR / "000001.json").exists())

    def test_proven_campaign_rows_and_digest_are_unchanged(self):
        report = C.status(self.root, self.path)
        row = report["cycles"][0]
        self.assertEqual(set(row) - {"disposition"}, {"cycle_id", "state", "manifest_digest", "index_digest",
                                                      "route_id", "manifest_id", "manifest_revision_id"})
        self.assertEqual(row["state"], "completed")
        snapshot = C._snapshot(self.root, self.path)
        without_disposition = [{k: v for k, v in item.items() if k != "disposition"}
                               for item in report["cycles"]]
        self.assertEqual(snapshot["cycles"], without_disposition)
        self.assertNotIn("unproven_cycles", report)

    def test_route_closed_unproven_cycle_is_disclosed_and_closable(self):
        _route, _route_file, child = self._provisional_child("unproven")
        manifest_before = (Path(child["cycle_dir"]) / "manifest.json").read_bytes()
        record_before = P.read_cycle_record(self.root, child["cycle_id"])
        report = C.status(self.root, self.path)
        row = next(row for row in report["cycles"] if row["cycle_id"] == child["cycle_id"])
        self.assertEqual(row["disposition"], C.PROVISIONAL_DISPOSITION)
        self.assertIs(row["terminal_gate_proven"], False)
        self.assertTrue(row["route_outcome_digest"].startswith("sha256:"))
        self._close("close with disclosed unproven route")
        event = json.loads((self.path.parent / C.EVENTS_DIR / "000001.json").read_text())
        snap_row = next(r for r in event["payload"]["snapshot"]["cycles"] if r["cycle_id"] == child["cycle_id"])
        for key in ("route_closed", "terminal_gate_proven", "terminal_gate_reasons", "route_outcome_digest"):
            self.assertIn(key, snap_row)
        self.assertNotIn("disposition", snap_row)
        self.assertEqual((Path(child["cycle_dir"]) / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"]), record_before)

    def test_route_closed_with_proof_stays_active_and_sealed_unproven(self):
        _route, _route_file, child = self._provisional_child("proven")
        report = C.status(self.root, self.path)
        row = next(row for row in report["cycles"] if row["cycle_id"] == child["cycle_id"])
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["disposition"], C.PROVISIONAL_DISPOSITION)
        self.assertIs(row["terminal_gate_proven"], True)
        self.assertEqual(row["terminal_gate_reasons"], [])
        self.assertEqual(report["unproven_cycles"]["without_terminal_proof"], 0)

    def test_open_route_provisional_cycle_is_refused_with_next_step(self):
        route, route_file, _child = self._provisional_child(None)
        refusal = C.status(self.root, self.path)["close_refusal"]
        self.assertEqual(refusal["reason"], "campaign-cycle-provisional-active")
        self.assertIn("open=1", refusal["detail"])
        self.assertIn(f"route={route['route_id']}", refusal["detail"])
        self.assertIn("route_state=open", refusal["detail"])
        self.assertIn("complete", refusal["detail"])
        self.assertIn("--allow-unproven", refusal["detail"])
        self.assertNotIn("finalize", refusal["detail"])
        F.R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        self.assertNotIn("close_refusal", C.status(self.root, self.path))

    def test_outcome_identity_mismatch_is_typed(self):
        route, _route_file, _child = self._provisional_child("unproven")
        outcome_path = C.lifecycle.canonical_outcome_path(self.root, route["route_id"])
        outcome = json.loads(outcome_path.read_text())
        outcome["route_hash"] = "sha256:" + "0" * 64
        outcome_path.write_text(json.dumps(outcome))
        self.assertEqual(C.status(self.root, self.path)["close_refusal"]["reason"],
                         "campaign-cycle-route-outcome-mismatch")

    def test_integrity_errors_win_over_open_route_refusal(self):
        self._provisional_child(None)
        self.output.write_bytes(b"bad bytes")
        self.assertEqual(C.status(self.root, self.path)["close_refusal"]["reason"],
                         "campaign-artifact-mismatch")

    def test_cli_and_idempotency_preserve_sealed_bytes_and_reject_new_cycle(self):
        args = ["campaign-close", "--artifact-root", str(self.root), "--campaign", str(self.path),
                "--reason", "CLI completion"]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(P.main(args), 0)
            first_result = json.loads(stdout.getvalue())
        event = (self.path.parent / C.EVENTS_DIR / "000001.json").read_bytes()
        self.assertEqual(self._close("second close is idempotent")["event_id"], first_result["event_id"])
        self.assertEqual((self.path.parent / C.EVENTS_DIR / "000001.json").read_bytes(), event)
        self.assertEqual(self.manifest_path.read_bytes(), self.manifest_bytes)
        reopened = self._begin(key="campaign-closure", slug="new-cycle-reopens")
        self.assertEqual(reopened["campaign_id"], self.campaign)
        self.assertTrue(reopened["campaign_reopened"])

    def test_abandoned_empty_cycle_record_is_detached_not_membership_drift(self):
        route = F.compile_for("direct", self.root, slug="empty-abandon", gate_source="empty-abandon")
        binding = F.L.admit_runtime_route(self.root, route)
        empty = P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                        intensity="direct", campaign_key="campaign-closure")
        outcome = P.finalize(self.root, cycle_id=empty["cycle_id"], state="abandoned",
                             abandon_reason="operator-decision")
        self.assertEqual(outcome["status"], "no-lineage")
        record = P.read_cycle_record(self.root, empty["cycle_id"])
        self.assertEqual((record["state"], record["campaign_id"]), ("abandoned", self.campaign))
        self.assertNotIn(empty["cycle_id"], json.loads(self.path.read_text())["cycles"])
        self.assertFalse(C.is_member_record(record))
        report = C.status(self.root, self.path)
        self.assertEqual(report["state"], "active")
        self.assertEqual([row["cycle_id"] for row in report["cycles"]], [self.result["cycle_id"]])
        self.assertEqual([row["cycle_id"] for row in report["detached_cycles"]], [empty["cycle_id"]])
        self.assertNotIn("detached", json.dumps(C._snapshot(self.root, self.path)))
        self._close("detached attempt does not affect closure")
        self.assertEqual(P.read_cycle_record(self.root, empty["cycle_id"]), record)

    def test_a24_11_no_approval_surface_remains(self):
        tokens = ("verify_" + "approval", "--approval-" + "session",
                  "awaiting-user-" + "acceptance", "campaign-" + "satisfy")
        root = Path(__file__).resolve().parent.parent
        hits = []
        for folder in (root / "utilities", root / "docs", root / "core"):
            for path in folder.rglob("*"):
                if path.is_file():
                    try: text = path.read_text(encoding="utf-8")
                    except (UnicodeError, OSError): continue
                    if any(token in text for token in tokens): hits.append(str(path))
        self.assertEqual(hits, [])

    def test_a24_12_old_reader_compatibility_boundaries(self):
        if subprocess.run(["git", "cat-file", "-e", "307a4b3a^{commit}"],
                          cwd=Path(__file__).resolve().parents[1], capture_output=True).returncode:
            self.skipTest("base revision is unavailable in this shallow checkout")
        archive = Path(self._tmp.name) / "old-reader.tar"
        old_root = Path(self._tmp.name) / "old-reader"
        old_root.mkdir()
        with archive.open("wb") as stream:
            subprocess.run(["git", "archive", "307a4b3a", "utilities"],
                           cwd=Path(__file__).resolve().parents[1], check=True, stdout=stream)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(old_root)], check=True)
        old_utilities = old_root / "utilities"
        script = ("import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                  "import artifact_producer as P; root=Path(sys.argv[2]); action=sys.argv[3]; "
                  "campaign=sys.argv[4]; "
                  "exec(\"try:\\n r=P.read_campaign(root,campaign) if action=='read' else "
                  "P.begin(root,route_file=Path(sys.argv[5]),capability='autopilot-code',intensity='direct',campaign_id=campaign)"
                  "\
 print('state='+str((r or {}).get('state')))\
except Exception as e: print('code='+str(getattr(e,'code',type(e).__name__)))\")")
        self._write_v1_close()
        C.reopen(self.root, self.path, reason="legacy reopen")
        self._begin(campaign=self.campaign, slug="legacy-reader-reopen")
        env = {**os.environ, "HOME": str(Path(self._tmp.name) / "home"),
               "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
               "AGENT_ARTIFACT_CHECKPOINT": "off"}
        result = subprocess.run([sys.executable, "-c", script, str(old_utilities), str(self.root),
                                 "read", self.campaign, "-"], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("campaign-projection-conflict", result.stdout)

        other_route, other_file, other = self.begin(campaign_key="legacy-reader-v2")
        self.write_output(other); self.close(other_route, other_file)
        P.finalize(self.root, cycle_id=other["cycle_id"])
        other_path = Path(other["cycle_dir"]).parent / "campaign.json"
        C.close(self.root, other_path, reason="v2 closed campaign")
        next_route = F.compile_for("direct", self.root, slug="legacy-reader-old-begin", gate_source="old-begin")
        next_binding = F.L.admit_runtime_route(self.root, next_route)
        result = subprocess.run([sys.executable, "-c", script, str(old_utilities), str(self.root),
                                 "begin", other["campaign_id"], next_binding.route_file],
                               text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("campaign-not-active", result.stdout)



class OpenRefusalDetailTest(unittest.TestCase):
    def test_open_refusal_detail_is_bounded(self):
        pending = [(f"cyc_{i:032x}", f"rt-{i:016x}") for i in range(12)]
        detail = C._open_refusal_detail(pending)
        self.assertTrue(detail.startswith("open=12 "))
        self.assertIn("+2 more", detail)
        self.assertEqual(detail.count("route_state=open"), 10)


if __name__ == "__main__":
    unittest.main()
