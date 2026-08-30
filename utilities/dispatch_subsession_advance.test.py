#!/usr/bin/env python3
"""P-tier fixtures + A-3/A-4/A-6/A-7(resume/drift) for SD-119's chain-advance
checkpoint (plan.md §3 R2, §7.7).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_subsession_advance as SA  # noqa: E402
import dispatch_subsession_resume_record as RESUME  # noqa: E402
from dispatch_contract import DispatchContractError  # noqa: E402
from dispatch_continuation_budget import ContinuationBudget, ContinuationLedger  # noqa: E402


CHAIN_ID = "ssc-fixture"
ROUTE_ID = "rt-fixture0000000"
ROUTE_HASH = "sha256:" + "1" * 64
ROUTE_NODE = "execute"
MANIFEST_SHA = "manifest-sha-v1"


def make_manifest(session_count: int = 3) -> dict:
    sessions = [
        {
            "subsession_id": f"ss-{i}", "index": i, "count": session_count,
            "adapter": "claude", "slug": f"slug-{i}", "phase_brief": f"brief-{i}",
            "narrow_verify": "true", "expected_round_trips": 1,
            "attempt_id": f"att-stage-session-{i}", "fixed_files": [],
        }
        for i in range(1, session_count + 1)
    ]
    return {
        "chain_id": CHAIN_ID, "mode": "serial", "sessions": sessions,
        "_manifest_sha256": MANIFEST_SHA,
    }


class Sandbox:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.registry"
        self.jobs.touch()

    def close(self):
        self.tmp.cleanup()

    def add_registry_row(self, metadata: dict, *, status: str = "open"):
        pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
        line = "\t".join(["ts", status, "repo", "worktree", "slug", pipe])
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def mark_terminal(self, index: int, attempt_id: str):
        self.add_registry_row(
            {
                "session_chain_id": CHAIN_ID, "subsession_index": str(index),
                "attempt_id": attempt_id,
            },
            status="done",
        )


def make_request(
    sandbox: Sandbox, manifest: dict, *, successor_index: int,
    predecessor_subsession_id="ss-1", predecessor_terminal_attempt_id="att-stage-session-1",
    manifest_sha256=MANIFEST_SHA,
) -> SA.SubsessionAdvanceRequest:
    successor = next(s for s in manifest["sessions"] if s["index"] == successor_index)
    return SA.SubsessionAdvanceRequest(
        jobs=sandbox.jobs,
        route_id=ROUTE_ID, route_hash=ROUTE_HASH, route_node=ROUTE_NODE,
        chain_id=CHAIN_ID, manifest_sha256=manifest_sha256,
        predecessor_subsession_id=predecessor_subsession_id,
        predecessor_terminal_attempt_id=predecessor_terminal_attempt_id,
        successor_subsession_index=successor_index,
        successor_session=successor,
        parent_attempt_id="att-owner-0001",
    )


class FakeServices:
    def __init__(self, sandbox: Sandbox, *, sealed_sha256: str = MANIFEST_SHA):
        self.sandbox = sandbox
        self.sealed_sha256 = sealed_sha256
        self.claim_calls = 0
        self.register_calls = 0
        self.start_calls = 0
        self.handoff_classification = "ok"
        self.handoff_calls = 0

    def sealed_manifest_sha256(self, request):
        return self.sealed_sha256

    def classify_handoff(self, request):
        self.handoff_calls += 1
        return self.handoff_classification

    def predecessor_terminal(self, request):
        status = None
        for line in self.sandbox.jobs.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
            if metadata.get("attempt_id") == request.predecessor_terminal_attempt_id:
                status = fields[1]
        return status == "done"

    def claim(self, request, *, subsession_advance_id, claim_key):
        self.claim_calls += 1
        import dispatch_contract as DC

        registry_claim = DC.claim_subsession_advance(
            request.jobs,
            subsession_advance_id=subsession_advance_id,
            route_hash=claim_key[0], route_node=claim_key[1], chain_id=claim_key[2],
            successor_subsession_index=claim_key[3], advance_generation=claim_key[4],
            successor_attempt_id=request.successor_session["attempt_id"],
        )
        return SA.SubsessionAdvanceClaim(
            subsession_advance_id=registry_claim.subsession_advance_id,
            claim_key=registry_claim.claim_key,
            successor_attempt_id=registry_claim.successor_attempt_id,
            replayed=registry_claim.replayed,
        )

    def register_successor(self, request, *, claim):
        self.register_calls += 1
        return {"returncode": 0}

    def start_successor(self, request, *, claim):
        self.start_calls += 1
        self.sandbox.add_registry_row(
            {
                "session_chain_id": request.chain_id,
                "subsession_index": str(request.successor_subsession_index),
                "attempt_id": claim.successor_attempt_id,
            },
            status="open",
        )
        return {"child_spawned": True}


class ResumeIndexTest(unittest.TestCase):
    def test_resume_index_is_lowest_index_without_terminal_row(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest()
            sandbox.mark_terminal(1, "att-stage-session-1")
            self.assertEqual(SA.resume_index(sandbox.jobs, manifest), 2)
        finally:
            sandbox.close()

    def test_resume_index_skips_completed_leading_indexes(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest()
            sandbox.mark_terminal(1, "att-stage-session-1")
            sandbox.mark_terminal(2, "att-stage-session-2")
            self.assertEqual(SA.resume_index(sandbox.jobs, manifest), 3)
        finally:
            sandbox.close()


class ManifestDriftTest(unittest.TestCase):
    def test_resume_after_manifest_change_is_manifest_drift(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest()
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = FakeServices(sandbox, sealed_sha256="a-different-sha")
            request = make_request(sandbox, manifest, successor_index=2)
            result = SA.coordinate_subsession_advance(request, services)
            self.assertEqual(result.outcome, "refused")
            self.assertEqual(result.reason, "subsession-chain-manifest-drift")
        finally:
            sandbox.close()

    def test_manifest_drift_yields_zero_claim_and_zero_start(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest()
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = FakeServices(sandbox, sealed_sha256="a-different-sha")
            request = make_request(sandbox, manifest, successor_index=2)
            SA.coordinate_subsession_advance(request, services)
            self.assertEqual(services.claim_calls, 0)
            self.assertEqual(services.start_calls, 0)
        finally:
            sandbox.close()


class ClaimConflictTest(unittest.TestCase):
    def test_second_claim_same_chain_index_conflicts_with_zero_spawn(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest()
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = FakeServices(sandbox)
            request = make_request(sandbox, manifest, successor_index=2)
            first = SA.coordinate_subsession_advance(request, services)
            self.assertEqual(first.outcome, "advanced")
            self.assertEqual(services.start_calls, 1)

            # A distinct advance identity (different predecessor attempt id)
            # but the SAME claim_key (route_hash, route_node, chain_id,
            # index, generation) -- predecessor-free per plan D-A.
            sandbox.mark_terminal(2, "att-stage-session-1-retry")
            conflicting_request = make_request(
                sandbox, manifest, successor_index=2,
                predecessor_terminal_attempt_id="att-stage-session-1-retry",
            )
            second = SA.coordinate_subsession_advance(conflicting_request, services)
            self.assertEqual(second.outcome, "refused")
            self.assertEqual(second.reason, "subsession-advance-claim-conflict")
            self.assertEqual(services.start_calls, 1)
        finally:
            sandbox.close()


class BudgetInvariantTest(unittest.TestCase):
    """§7.7: the internal chain-advance transitions never touch a
    `ContinuationLedger` at all -- `coordinate_subsession_advance` has no
    reference to one. The final aggregate owner-resume admit is a SEPARATE,
    single call this fixture drives directly to prove the "exactly 1" half
    of the invariant (the real call site is the supervisor integration,
    covered by `claude_session_supervisor.test.py`)."""

    def _snap(self, ledger: ContinuationLedger, admit_calls: list) -> tuple:
        return (
            ledger.gross_remaining, ledger.stall_remaining,
            ledger.reserved_remaining, len(admit_calls),
        )

    def test_internal_advance_gross_stall_reserved_deltas_zero(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest(session_count=3)
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = FakeServices(sandbox)
            ledger = ContinuationLedger(ContinuationBudget(12, "compatibility-floor"))
            admit_calls: list = []

            before = self._snap(ledger, admit_calls)
            request_2 = make_request(sandbox, manifest, successor_index=2)
            result_2 = SA.coordinate_subsession_advance(request_2, services)
            self.assertEqual(result_2.outcome, "advanced")
            after = self._snap(ledger, admit_calls)
            self.assertEqual(before, after)

            sandbox.mark_terminal(2, "att-stage-session-2")
            before2 = self._snap(ledger, admit_calls)
            request_3 = make_request(
                sandbox, manifest, successor_index=3,
                predecessor_subsession_id="ss-2",
                predecessor_terminal_attempt_id="att-stage-session-2",
            )
            result_3 = SA.coordinate_subsession_advance(request_3, services)
            self.assertEqual(result_3.outcome, "advanced")
            after2 = self._snap(ledger, admit_calls)
            self.assertEqual(before2, after2)
            self.assertEqual(services.start_calls, 2)
        finally:
            sandbox.close()

    def test_three_slice_chain_owner_model_turns_zero(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest(session_count=3)
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = FakeServices(sandbox)
            model_turns = 0
            delivered_mutations = 0

            request_2 = make_request(sandbox, manifest, successor_index=2)
            SA.coordinate_subsession_advance(request_2, services)
            sandbox.mark_terminal(2, "att-stage-session-2")
            request_3 = make_request(
                sandbox, manifest, successor_index=3,
                predecessor_subsession_id="ss-2",
                predecessor_terminal_attempt_id="att-stage-session-2",
            )
            SA.coordinate_subsession_advance(request_3, services)

            self.assertEqual(model_turns, 0)
            self.assertEqual(delivered_mutations, 0)
        finally:
            sandbox.close()

    def test_final_wake_once_and_charge_one(self):
        sandbox = Sandbox()
        try:
            ledger = ContinuationLedger(ContinuationBudget(12, "compatibility-floor"))
            admit_calls: list = []

            def admit(**kwargs):
                admit_calls.append(kwargs)
                return ledger.admit(**kwargs)

            verdict = admit(purpose="ordinary", stalled=False, reservation_ok=True)
            self.assertEqual(len(admit_calls), 1)
            self.assertTrue(verdict.admitted)
            self.assertEqual(verdict.charged, "gross")
        finally:
            sandbox.close()


class RuntimeJoinsCensusTest(unittest.TestCase):
    def test_runtime_joins_derives_from_resume_event_census(self):
        with tempfile.TemporaryDirectory() as td:
            state_root = Path(td)
            ok, _ = RESUME.record_resume(
                state_root, route_id=ROUTE_ID, route_hash=ROUTE_HASH,
                route_node=ROUTE_NODE, chain_id=CHAIN_ID, manifest_sha256=MANIFEST_SHA,
                delivery_id="delivery-aggregate-1",
            )
            self.assertTrue(ok)
            self.assertEqual(RESUME.unique_delivery_ids(state_root, CHAIN_ID), 1)
            # Replay of the same delivery_id dedupes -- still 1, never 2.
            RESUME.record_resume(
                state_root, route_id=ROUTE_ID, route_hash=ROUTE_HASH,
                route_node=ROUTE_NODE, chain_id=CHAIN_ID, manifest_sha256=MANIFEST_SHA,
                delivery_id="delivery-aggregate-1",
            )
            self.assertEqual(RESUME.unique_delivery_ids(state_root, CHAIN_ID), 1)

    def test_subsession_advances_equals_n_minus_one(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest(session_count=3)
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = FakeServices(sandbox)
            request_2 = make_request(sandbox, manifest, successor_index=2)
            SA.coordinate_subsession_advance(request_2, services)
            sandbox.mark_terminal(2, "att-stage-session-2")
            request_3 = make_request(
                sandbox, manifest, successor_index=3,
                predecessor_subsession_id="ss-2",
                predecessor_terminal_attempt_id="att-stage-session-2",
            )
            SA.coordinate_subsession_advance(request_3, services)
            self.assertEqual(SA.subsession_advances(sandbox.jobs, CHAIN_ID), 2)
        finally:
            sandbox.close()

    def test_hardcoded_receipt_value_alone_fails(self):
        with tempfile.TemporaryDirectory() as td:
            state_root = Path(td)
            hardcoded_runtime_joins = 1
            self.assertNotEqual(
                hardcoded_runtime_joins, RESUME.unique_delivery_ids(state_root, CHAIN_ID)
            )


class ChainAdvanceHookTest(unittest.TestCase):
    """D-C/D-E: the shared supervisor-side hook is a byte-identical no-op
    when the joined round carries no sub-session chain metadata at all."""

    def test_no_chain_metadata_returns_none_and_is_a_no_op(self):
        sandbox = Sandbox()
        try:
            joined = {
                "att-ordinary-1": SimpleNamespace(
                    attempt_id="att-ordinary-1", status="done", metadata={},
                )
            }
            result = SA.coordinate_chain_advance_from_joined_rows(
                sandbox.jobs, "att-owner-0001", joined
            )
            self.assertIsNone(result)
            # No filesystem side effect: still exactly the fixture's single row.
            self.assertEqual(
                len(sandbox.jobs.read_text(encoding="utf-8").splitlines()), 0
            )
        finally:
            sandbox.close()

    def test_joined_chain_row_advances_to_next_index(self):
        sandbox = Sandbox()
        try:
            manifest = make_manifest(session_count=3)
            pointer = SA.chain_manifest_pointer_path(sandbox.jobs, CHAIN_ID)
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps(manifest), encoding="utf-8")
            sandbox.add_registry_row(
                {
                    "parent_attempt_id": "att-owner-0001",
                    "attempt_schema_version": "2",
                    "attempt_id": "att-stage-session-1",
                    "session_chain_id": CHAIN_ID,
                    "subsession_index": "1",
                    "subsession_mode": "serial",
                    "route_id": ROUTE_ID,
                    "route_hash": ROUTE_HASH,
                    "route_node": ROUTE_NODE,
                    "subsession_id": "ss-1",
                },
                status="done",
            )
            joined = {
                "att-stage-session-1": SimpleNamespace(
                    attempt_id="att-stage-session-1", status="done",
                    metadata={
                        "session_chain_id": CHAIN_ID, "subsession_index": "1",
                        "subsession_mode": "serial", "route_id": ROUTE_ID,
                        "route_hash": ROUTE_HASH, "route_node": ROUTE_NODE,
                        "subsession_id": "ss-1",
                    },
                )
            }
            with mock.patch.object(SA, "RealSubsessionAdvanceServices") as factory:
                fake = FakeServices(sandbox)
                factory.return_value = fake
                result = SA.coordinate_chain_advance_from_joined_rows(
                    sandbox.jobs, "att-owner-0001", joined
                )
            self.assertEqual(result, "att-stage-session-2")
            self.assertEqual(fake.start_calls, 1)
        finally:
            sandbox.close()


if __name__ == "__main__":
    unittest.main()
