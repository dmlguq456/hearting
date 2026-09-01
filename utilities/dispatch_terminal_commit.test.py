#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import dispatch_terminal_commit as terminal
import route_identity


class FakeServices:
    def __init__(self, *, fail_finalize: bool = False) -> None:
        self.close_count = 0
        self.finalize_count = 0
        self.fail_finalize = fail_finalize

    def close_route(self, eligibility):
        self.close_count += 1
        return {
            "schema_version": 3,
            "route_id": eligibility.route_id,
            "route_hash": eligibility.route_hash,
            "terminal_gate_proven": True,
        }

    def finalize_producer(self, _eligibility):
        self.finalize_count += 1
        if self.fail_finalize:
            raise terminal.TerminalCommitError("producer-finalize-failed")
        return {
            "status": "sealed",
            "manifest_path": "/manifest.json",
            "manifest_digest": "sha256:manifest",
        }


class TerminalCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "artifacts-root"
        self.root.mkdir()
        self.route_path = self.root / ".runtime" / "routes" / "route.json"
        self.route_path.parent.mkdir(parents=True)
        self.evidence = self.root / "campaigns" / "camp" / "cycles" / "cyc" / "artifacts" / "plans" / "final_report.md"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_text("material report\n", encoding="utf-8")
        self.route = self._route(capability="unit-test")
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        self.marker = self.root / "completion" / "report.json"
        self.marker.parent.mkdir()
        self.marker.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"],
                    "node_id": "report",
                    "attempt_id": "att-child",
                    "evidence": {
                        "path": str(self.evidence),
                        "sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        self.rows = [
            SimpleNamespace(
                status="done",
                attempt_id="att-child",
                metadata={
                    "failure_class": "pass",
                    "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"],
                    "route_node": "report",
                    "completion_marker": str(self.marker),
                },
            )
        ]

    def _route(self, *, capability: str) -> dict:
        route = {
            "schema_version": 2,
            "capability": capability,
            "artifact_root": str(self.root),
            "cwd": str(Path(self.temp.name)),
            "nodes": [{"id": "report", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["report"]},
        }
        route["route_hash"] = route_identity.route_hash(route)
        route["route_id"] = route_identity.route_id_from_hash(route["route_hash"])
        route["owner_attempt_id"] = "-"
        route["route_family_key"] = "sha256:family"
        return route

    def eligibility(self, *, producer_required=False):
        return terminal.classify_terminal_eligibility(
            route=self.route,
            route_file=self.route_path,
            owner_attempt_id="att-owner",
            rows=self.rows,
            producer_required=producer_required,
        )

    def bind_required_producer(self):
        self.route = self._route(capability="autopilot-code")
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        marker["route_id"] = self.route["route_id"]
        marker["route_hash"] = self.route["route_hash"]
        self.marker.write_text(json.dumps(marker), encoding="utf-8")
        self.rows[0].metadata["route_id"] = self.route["route_id"]
        self.rows[0].metadata["route_hash"] = self.route["route_hash"]
        cycle = {
            "campaign_id": "camp-one",
            "cycle_id": "cyc-one",
            "producer_id": "prod-one",
            "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"],
            "state": "open",
        }
        cycle_path = (
            self.root
            / ".runtime"
            / "artifact-producer"
            / "v1"
            / "cycles"
            / "cyc-one.json"
        )
        cycle_path.parent.mkdir(parents=True, exist_ok=True)
        cycle_path.write_text(json.dumps(cycle), encoding="utf-8")
        binding = terminal.publish_producer_binding(
            self.root,
            route=self.route,
            route_file=self.route_path,
            owner_attempt_id="att-owner",
            campaign_id="camp-one",
            cycle_id="cyc-one",
            producer_id="prod-one",
            cycle_record=cycle,
        )
        return cycle, cycle_path, binding

    def test_a49_2_canonical_hash_and_mismatch_mutates_nothing(self):
        self.assertEqual(route_identity.route_hash(self.route), self.route["route_hash"])
        legacy = dict(self.route)
        legacy.pop("route_hash")
        legacy.pop("route_id")
        legacy_hash = "sha256:" + hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertNotEqual(legacy_hash, self.route["route_hash"])
        broken = dict(self.route)
        broken["route_hash"] = "sha256:" + "0" * 64
        verdict = terminal.classify_terminal_eligibility(
            route=broken,
            route_file=self.route_path,
            owner_attempt_id="att-owner",
            rows=self.rows,
            producer_required=False,
        )
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.reason, "route-identity-unverified")
        self.assertFalse((self.root / terminal.RECORD_ROOT_REL / "terminal-commits").exists())
        unsafe = terminal.classify_terminal_eligibility(
            route=self.route,
            route_file=self.route_path,
            owner_attempt_id="att-../../foreign",
            rows=self.rows,
            producer_required=False,
        )
        self.assertEqual((unsafe.eligible, unsafe.reason), (False, "route-identity-unverified"))

    def test_a49_1_success_and_duplicate_replay_are_singletons(self):
        eligibility = self.eligibility()
        self.assertTrue(eligibility.eligible, eligibility)
        services = FakeServices()
        first = terminal.settle_terminal_commit(eligibility, services=services)
        second = terminal.settle_terminal_commit(eligibility, services=services)
        self.assertTrue(first.success)
        self.assertEqual(first.envelope, second.envelope)
        self.assertNotIn("artifact: -", first.envelope)
        self.assertEqual((services.close_count, services.finalize_count), (1, 0))
        self.assertEqual(
            len(list((self.root / terminal.RECORD_ROOT_REL / "terminal-envelopes").glob("*.txt"))),
            1,
        )

    def test_a49_12_symlink_envelope_never_becomes_success(self):
        eligibility = self.eligibility()
        commit_id = terminal.terminal_commit_id(eligibility)
        envelope = terminal.terminal_envelope_path(self.root, commit_id)
        envelope.parent.mkdir(parents=True)
        target = Path(self.temp.name) / "foreign-envelope.txt"
        target.write_text(
            f"artifact: {self.evidence}\nverdict: PASS\nblocker: none",
            encoding="utf-8",
        )
        envelope.symlink_to(target)
        result = terminal.settle_terminal_commit(
            eligibility, services=FakeServices()
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "transaction-conflict")

    def test_a49_1_bound_producer_success_finalizes_once(self):
        self.bind_required_producer()
        eligibility = self.eligibility(producer_required=True)
        self.assertTrue(eligibility.eligible, eligibility)
        services = FakeServices()
        first = terminal.settle_terminal_commit(eligibility, services=services)
        replay = terminal.settle_terminal_commit(eligibility, services=services)
        self.assertTrue(first.success)
        self.assertTrue(first.continuation_saved)
        self.assertEqual(first.envelope, replay.envelope)
        self.assertEqual((services.close_count, services.finalize_count), (1, 1))

    def test_a49_3_crash_after_route_close_replays_forward(self):
        eligibility = self.eligibility()
        services = FakeServices()
        with self.assertRaises(terminal.TerminalCommitCrash):
            terminal.settle_terminal_commit(
                eligibility, services=services, crash_after_state="route-closed"
            )
        result = terminal.settle_terminal_commit(eligibility, services=services)
        self.assertTrue(result.success)
        self.assertEqual((services.close_count, services.finalize_count), (1, 0))

    def test_a49_3_claim_and_envelope_crashes_replay_forward(self):
        for crash_state in ("claimed", "envelope-written"):
            with self.subTest(crash_state=crash_state):
                root = self.root / crash_state
                root.mkdir()
                route = self._route(capability="unit-test")
                route["artifact_root"] = str(root)
                route["route_hash"] = route_identity.route_hash(route)
                route["route_id"] = route_identity.route_id_from_hash(route["route_hash"])
                evidence = root / "final_report.md"
                evidence.write_text("material\n", encoding="utf-8")
                marker = root / "marker.json"
                marker.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "route_id": route["route_id"],
                            "route_hash": route["route_hash"],
                            "node_id": "report",
                            "attempt_id": "att-child",
                            "evidence": {
                                "path": str(evidence),
                                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                rows = [
                    SimpleNamespace(
                        status="done",
                        attempt_id="att-child",
                        metadata={
                            "failure_class": "pass",
                            "route_id": route["route_id"],
                            "route_hash": route["route_hash"],
                            "route_node": "report",
                            "completion_marker": str(marker),
                        },
                    )
                ]
                eligibility = terminal.classify_terminal_eligibility(
                    route=route,
                    route_file=root / "route.json",
                    owner_attempt_id="att-owner",
                    rows=rows,
                    producer_required=False,
                )
                services = FakeServices()
                with self.assertRaises(terminal.TerminalCommitCrash):
                    terminal.settle_terminal_commit(
                        eligibility, services=services, crash_after_state=crash_state
                    )
                recovered = terminal.settle_terminal_commit(eligibility, services=services)
                self.assertTrue(recovered.success)
                self.assertEqual(services.close_count, 1)
                self.assertEqual(
                    len(list((root / terminal.RECORD_ROOT_REL / "terminal-envelopes").glob("*.txt"))),
                    1,
                )

    def test_a49_3_sealed_cycle_recovers_only_existing_forward_commit(self):
        cycle, cycle_path, _binding = self.bind_required_producer()
        eligibility = self.eligibility(producer_required=True)
        self.assertTrue(eligibility.eligible, eligibility)
        services = FakeServices()
        with self.assertRaises(terminal.TerminalCommitCrash):
            terminal.settle_terminal_commit(
                eligibility, services=services, crash_after_state="route-closed"
            )
        sealed = dict(cycle)
        sealed["state"] = "sealed"
        sealed["manifest_path"] = "/manifest.json"
        sealed["manifest_digest"] = "sha256:manifest"
        cycle_path.write_text(json.dumps(sealed), encoding="utf-8")
        recovered = self.eligibility(producer_required=True)
        self.assertTrue(recovered.eligible, recovered)
        result = terminal.settle_terminal_commit(recovered, services=services)
        self.assertTrue(result.success)
        self.assertEqual((services.close_count, services.finalize_count), (1, 1))

        # A sealed producer without an exact prior commit remains ineligible.
        terminal.terminal_commit_path(
            self.root, terminal.terminal_commit_id(recovered)
        ).unlink()
        refused = self.eligibility(producer_required=True)
        self.assertFalse(refused.eligible)
        self.assertEqual(refused.reason, "producer-binding-mismatch")

    def test_a49_9_producer_binding_cas_and_matrix(self):
        cycle, _cycle_path, _binding = self.bind_required_producer()
        missing = self.eligibility(producer_required=True)
        self.assertTrue(missing.eligible)
        first = terminal.load_producer_binding(
            self.root, self.route["route_id"], "att-owner"
        )
        replay = terminal.publish_producer_binding(
            self.root,
            route=self.route,
            route_file=self.route_path,
            owner_attempt_id="att-owner",
            campaign_id="camp-one",
            cycle_id="cyc-one",
            producer_id="prod-one",
            cycle_record=cycle,
        )
        self.assertEqual(first, replay)
        valid = self.eligibility(producer_required=True)
        self.assertTrue(valid.eligible, valid)
        foreign = dict(cycle)
        foreign["producer_id"] = "prod-foreign"
        with self.assertRaises(terminal.TerminalCommitError) as raised:
            terminal.publish_producer_binding(
                self.root,
                route=self.route,
                route_file=self.route_path,
                owner_attempt_id="att-owner",
                campaign_id="camp-one",
                cycle_id="cyc-one",
                producer_id="prod-foreign",
                cycle_record=foreign,
            )
        self.assertIn(raised.exception.reason, {"producer-binding-mismatch", "transaction-conflict"})

    def test_a49_9_missing_foreign_and_stale_bindings_mutate_nothing(self):
        self.route = self._route(capability="autopilot-code")
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        marker.update(route_id=self.route["route_id"], route_hash=self.route["route_hash"])
        self.marker.write_text(json.dumps(marker), encoding="utf-8")
        self.rows[0].metadata.update(
            route_id=self.route["route_id"], route_hash=self.route["route_hash"]
        )
        missing = self.eligibility(producer_required=True)
        self.assertEqual((missing.eligible, missing.reason), (False, "producer-binding-required"))
        cycle, cycle_path, binding = self.bind_required_producer()
        binding_path = terminal.producer_binding_path(
            self.root, self.route["route_id"], "att-owner"
        )
        for key, value in (
            ("owner_attempt_id", "att-foreign"),
            ("route_id", "rt-foreign"),
            ("cycle_id", "cyc-foreign"),
        ):
            with self.subTest(key=key):
                changed = dict(binding)
                changed[key] = value
                binding_path.write_text(json.dumps(changed), encoding="utf-8")
                refused = self.eligibility(producer_required=True)
                self.assertEqual((refused.eligible, refused.reason), (False, "producer-binding-mismatch"))
                self.assertFalse((self.root / terminal.RECORD_ROOT_REL / "terminal-commits").exists())
        binding_path.write_text(json.dumps(binding), encoding="utf-8")
        stale = dict(cycle)
        stale["state"] = "sealed"
        cycle_path.write_text(json.dumps(stale), encoding="utf-8")
        refused = self.eligibility(producer_required=True)
        self.assertEqual((refused.eligible, refused.reason), (False, "producer-binding-mismatch"))

    def test_a49_10_and_12_marker_process_and_material_matrix(self):
        original_rows = list(self.rows)
        self.rows.append(SimpleNamespace(status="open", attempt_id="att-sibling", metadata={}))
        refused = self.eligibility()
        self.assertEqual((refused.eligible, refused.reason), (False, "child-not-quiescent"))
        self.rows = original_rows

        self.rows[0].metadata["failure_class"] = "fail"
        refused = self.eligibility()
        self.assertEqual((refused.eligible, refused.reason), (False, "terminal-marker-not-current"))
        self.rows[0].metadata["failure_class"] = "pass"

        original = self.evidence.read_bytes()
        self.evidence.write_bytes(b"replaced\n")
        refused = self.eligibility()
        self.assertEqual((refused.eligible, refused.reason), (False, "terminal-marker-not-current"))
        self.evidence.write_bytes(original)

        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        marker["evidence"]["path"] = str(Path(self.temp.name) / "outside.md")
        self.marker.write_text(json.dumps(marker), encoding="utf-8")
        refused = self.eligibility()
        self.assertEqual((refused.eligible, refused.reason), (False, "terminal-marker-not-current"))
        self.assertFalse((self.root / terminal.RECORD_ROOT_REL / "terminal-commits").exists())

    def test_a49_11_live_review_lease_blocks_before_mutation(self):
        _cycle, _cycle_path, _binding = self.bind_required_producer()
        import artifact_producer

        artifact_producer.review_lease_acquire(
            self.root, cycle_id="cyc-one", attempt_id="att-reviewer"
        )
        refused = self.eligibility(producer_required=True)
        self.assertEqual((refused.eligible, refused.reason), (False, "producer-binding-mismatch"))
        self.assertFalse((self.root / terminal.RECORD_ROOT_REL / "terminal-commits").exists())

    def test_a49_4_finalize_failure_stays_route_closed_without_pass(self):
        eligibility = self.eligibility()
        eligibility = terminal.TerminalEligibility(
            **{**eligibility.__dict__, "producer_required": True, "producer_binding": {"cycle_id": "cyc"}, "producer_binding_digest": "sha256:binding"}
        )
        services = FakeServices(fail_finalize=True)
        result = terminal.settle_terminal_commit(eligibility, services=services)
        self.assertFalse(result.success)
        self.assertEqual((result.state, result.reason), ("route-closed", "producer-finalize-failed"))
        self.assertEqual((services.close_count, services.finalize_count), (1, 1))
        self.assertFalse(terminal.terminal_envelope_path(self.root, result.commit_id).exists())

    def test_a49_5_to_8_claim_conversion_and_cleanup_capability(self):
        state_root = Path(self.temp.name) / "dispatch"
        claim = terminal.claim_terminal_handoff(
            state_root,
            parent_attempt_id="att-owner",
            child_attempt_ids=["att-child"],
            continuation_ordinal=7,
            route_hash=self.route["route_hash"],
        )
        self.assertEqual(claim["budget_delta"], {"gross": 0, "stall": 0, "reserved": 0})
        self.assertEqual((claim["reservation_count"], claim["prompt_count"]), (0, 0))
        charges = []

        def charge():
            charges.append(1)
            return True

        converted = terminal.convert_terminal_handoff_claim(
            state_root,
            claim_id=claim["claim_id"],
            prompt="real cleanup prompt",
            charge=charge,
            artifact_root=str(self.root),
            allowed_write_roots=[str(self.evidence.parent)],
            allowed_read_roots=[str(self.root)],
            allowed_commands=["recover exact"],
        )
        replay = terminal.convert_terminal_handoff_claim(
            state_root,
            claim_id=claim["claim_id"],
            prompt="real cleanup prompt",
            charge=charge,
            artifact_root=str(self.root),
            allowed_write_roots=[str(self.evidence.parent)],
            allowed_read_roots=[str(self.root)],
            allowed_commands=["recover exact"],
        )
        self.assertEqual(len(charges), 1)
        self.assertEqual(converted, replay)
        self.assertEqual(converted["reservation_count"], 1)
        self.assertTrue(
            terminal.cleanup_tool_allowed(
                converted,
                tool_name="Write",
                tool_input={"file_path": str(self.evidence)},
            )
        )
        self.assertTrue(
            terminal.cleanup_tool_allowed(
                converted, tool_name="Bash", tool_input={"command": "recover exact"}
            )
        )
        for command in ("git apply patch", "preflight.sh dispatch --start", "edit jobs.log"):
            self.assertFalse(
                terminal.cleanup_tool_allowed(
                    converted, tool_name="Bash", tool_input={"command": command}
                )
            )
        self.assertFalse(
            terminal.cleanup_tool_allowed(
                converted,
                tool_name="Write",
                tool_input={"file_path": str(Path(self.temp.name) / "source.py")},
            )
        )

    def test_a49_8_supervisor_capability_names_one_exact_recovery_surface(self):
        self.bind_required_producer()
        args = SimpleNamespace(
            route_file=str(self.route_path),
            parent_attempt_id="att-owner",
            jobs=str(Path(self.temp.name) / "jobs.log"),
        )
        capability = terminal.supervisor_cleanup_capability(args)
        self.assertEqual(len(capability["allowed_commands"]), 1)
        command = capability["allowed_commands"][0]
        self.assertIn("dispatch_terminal_commit.py recover", command)
        for forbidden in (
            "capability-route.py close",
            "artifact_producer.py recover",
            "artifact_producer.py finalize",
        ):
            self.assertNotIn(forbidden, command)

    def test_claim_runtime_success_completes_with_zero_charge(self):
        state_root = Path(self.temp.name) / "dispatch"
        claim = terminal.claim_terminal_handoff(
            state_root,
            parent_attempt_id="att-owner",
            child_attempt_ids=["att-child"],
            continuation_ordinal=8,
            route_hash=self.route["route_hash"],
        )
        completed = terminal.complete_terminal_handoff_claim(
            state_root,
            claim_id=claim["claim_id"],
            terminal_commit_id_value="sha256:commit",
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["budget_delta"], {"gross": 0, "stall": 0, "reserved": 0})
        self.assertEqual((completed["reservation_count"], completed["prompt_count"]), (0, 0))
        self.assertEqual(completed["continuation_saved"], 1)

    def test_a49_7_failed_charge_stays_zero_and_tuple_conversion_seals_exact_prompt(self):
        state_root = Path(self.temp.name) / "dispatch"
        claim = terminal.claim_terminal_handoff(
            state_root,
            parent_attempt_id="att-owner",
            child_attempt_ids=["att-child"],
            continuation_ordinal=9,
            route_hash=self.route["route_hash"],
        )
        refused = terminal.convert_terminal_handoff_claim(
            state_root,
            claim_id=claim["claim_id"],
            prompt="unused",
            charge=lambda: False,
            artifact_root=str(self.root),
            allowed_write_roots=[str(self.evidence.parent)],
            allowed_read_roots=[str(self.root)],
        )
        self.assertEqual(refused["state"], "claimed")
        self.assertEqual(refused["budget_delta"], {"gross": 0, "stall": 0, "reserved": 0})
        converted = terminal.convert_terminal_handoff_claim(
            state_root,
            claim_id=claim["claim_id"],
            prompt="",
            charge=lambda: (True, "exact delivered cleanup prompt"),
            artifact_root=str(self.root),
            allowed_write_roots=[str(self.evidence.parent)],
            allowed_read_roots=[str(self.root)],
        )
        self.assertEqual(converted["state"], "converted")
        self.assertEqual(converted["prompt_intent"], "exact delivered cleanup prompt")
        self.assertEqual(converted["reservation_count"], 1)


if __name__ == "__main__":
    unittest.main()
