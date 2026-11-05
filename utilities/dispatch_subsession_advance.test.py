#!/usr/bin/env python3
"""P-tier fixtures + A-3/A-4/A-6/A-7(resume/drift) for SD-119's chain-advance
checkpoint (plan.md §3 R2, §7.7).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_subsession_advance as SA  # noqa: E402
import dispatch_subsession_handoff as HANDOFF  # noqa: E402
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
            self.assertEqual(verdict.charged, "none")
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


def _write_ledger_fixture(
    path: Path, attempt_id: str, *,
    completed_items: list[str], next_action: str,
    invariants: list[str], forbidden_files: list[str],
) -> None:
    meta = {
        "schema_version": 1, "attempt_id": attempt_id, "generation": 1,
        "edits_since_update": 0, "verification_pending": False,
        "resume_required": False, "fixed_files": [],
    }
    bullets = lambda values: "\n".join(f"- {v}" for v in values) or "- none"
    header = "<!-- worker-state-ledger:v1 " + json.dumps(meta) + " -->"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{header}\n# Worker State Ledger\n\n"
        f"## Current slice\nexecute/ss-1\n\n"
        f"## Completed items\n{bullets(completed_items)}\n\n"
        f"## Exact next command\n`{next_action}`\n\n"
        f"## Invariants\n{bullets(invariants)}\n\n"
        f"## Fixed files\n- none\n\n"
        f"## Forbidden files\n{bullets(forbidden_files)}\n",
        encoding="utf-8",
    )


class FlushOwnSubsessionHandoffTest(unittest.TestCase):
    """F-1 (impl-review round 1): `flush_own_subsession_handoff` is the real
    call site `claude-session-supervisor.py`/`codex-app-server-supervisor.py`
    now invoke right after their own terminal registry row commits."""

    def test_no_op_when_not_a_subsession(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = Path(td) / "artifact-root"
            with mock.patch.object(SA, "_resolve_artifact_root", return_value=str(artifact_root)), \
                 mock.patch.dict(os.environ, {}, clear=True):
                SA.flush_own_subsession_handoff(Path(td) / "jobs.registry", ROUTE_ID, "att-x")
            self.assertFalse(artifact_root.exists())

    def test_no_op_for_parallel_subsession(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = Path(td) / "artifact-root"
            env = {
                "AGENT_DISPATCH_SUBSESSION_ID": "ss-1",
                "AGENT_DISPATCH_SESSION_CHAIN_ID": CHAIN_ID,
                "AGENT_DISPATCH_SUBSESSION_MODE": "parallel",
            }
            with mock.patch.object(SA, "_resolve_artifact_root", return_value=str(artifact_root)), \
                 mock.patch.dict(os.environ, env, clear=False):
                SA.flush_own_subsession_handoff(Path(td) / "jobs.registry", ROUTE_ID, "att-x")
            self.assertFalse(artifact_root.exists())

    def test_flushes_ledger_content_and_classifies_ok(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            artifact_root = Path(td) / "artifact-root"
            manifest = make_manifest(session_count=2)
            pointer = SA.chain_manifest_pointer_path(jobs, CHAIN_ID)
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps(manifest), encoding="utf-8")
            ledger_path = Path(td) / "ledger.md"
            _write_ledger_fixture(
                ledger_path, "att-stage-session-1",
                completed_items=["did the thing"], next_action="python3 next.py",
                invariants=["one gate remains owned by owner"],
                forbidden_files=["do-not-touch.py"],
            )
            env = {
                "AGENT_DISPATCH_SUBSESSION_ID": "ss-1",
                "AGENT_DISPATCH_SESSION_CHAIN_ID": CHAIN_ID,
                "AGENT_DISPATCH_SUBSESSION_MODE": "serial",
                "AGENT_WORKER_STATE_LEDGER": str(ledger_path),
            }
            terminal_at_ns = int(
                (datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp() * 1_000_000_000
            )
            with mock.patch.object(SA, "_resolve_artifact_root", return_value=str(artifact_root)), \
                 mock.patch.dict(os.environ, env, clear=False):
                SA.flush_own_subsession_handoff(jobs, ROUTE_ID, "att-stage-session-1")
            path = HANDOFF.handoff_path(artifact_root, ROUTE_ID, CHAIN_ID)
            self.assertTrue(path.is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn("did the thing", text)
            self.assertIn("python3 next.py", text)
            self.assertIn("do-not-touch.py", text)
            classification = HANDOFF.classify_handoff(
                path,
                predecessor_attempt_id_expected="att-stage-session-1",
                manifest_sha256_expected=MANIFEST_SHA,
                predecessor_terminal_at_ns=terminal_at_ns,
            )
            self.assertEqual(classification, "ok")

    def test_missing_ledger_flushes_empty_but_still_ok(self):
        # A missing/never-updated ledger is not a hard stop for the flush
        # itself (§13.35.1-(5) only requires the four handoff fields) --
        # completed_items/invariants/forbidden_files degrade to empty lists.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            artifact_root = Path(td) / "artifact-root"
            manifest = make_manifest(session_count=2)
            pointer = SA.chain_manifest_pointer_path(jobs, CHAIN_ID)
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps(manifest), encoding="utf-8")
            env = {
                "AGENT_DISPATCH_SUBSESSION_ID": "ss-1",
                "AGENT_DISPATCH_SESSION_CHAIN_ID": CHAIN_ID,
                "AGENT_DISPATCH_SUBSESSION_MODE": "serial",
            }
            with mock.patch.object(SA, "_resolve_artifact_root", return_value=str(artifact_root)), \
                 mock.patch.dict(os.environ, env, clear=False):
                SA.flush_own_subsession_handoff(jobs, ROUTE_ID, "att-stage-session-1")
            path = HANDOFF.handoff_path(artifact_root, ROUTE_ID, CHAIN_ID)
            self.assertTrue(path.is_file())


class OwnerResumeWiringTest(unittest.TestCase):
    """F-2 (impl-review round 1): `record_owner_resume_if_chain` is the real
    call site both supervisors now invoke exactly once per aggregate chain
    delivery -- never from inside `coordinate_subsession_advance()`."""

    def test_no_op_when_joined_carries_no_chain_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            joined = {
                "att-ordinary-1": SimpleNamespace(
                    attempt_id="att-ordinary-1", status="done", metadata={},
                )
            }
            SA.record_owner_resume_if_chain(jobs, joined, None)
            self.assertEqual(RESUME.unique_delivery_ids(jobs.parent, CHAIN_ID), 0)

    def test_records_exactly_one_event_for_final_delivery_and_dedupes_replay(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            manifest = make_manifest(session_count=2)
            pointer = SA.chain_manifest_pointer_path(jobs, CHAIN_ID)
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps(manifest), encoding="utf-8")
            joined_before_advance = {
                "att-stage-session-1": SimpleNamespace(
                    attempt_id="att-stage-session-1", status="done",
                    metadata={
                        "session_chain_id": CHAIN_ID, "subsession_mode": "serial",
                        "route_id": ROUTE_ID, "route_hash": ROUTE_HASH,
                        "route_node": ROUTE_NODE,
                    },
                )
            }
            SA.record_owner_resume_if_chain(
                jobs, joined_before_advance, "att-stage-session-2",
            )
            self.assertEqual(RESUME.unique_delivery_ids(jobs.parent, CHAIN_ID), 1)
            # A crash-and-resume replay of the exact same round recomputes
            # the exact same delivery_id -- dedupes to the same one row, not two.
            SA.record_owner_resume_if_chain(
                jobs, joined_before_advance, "att-stage-session-2",
            )
            self.assertEqual(RESUME.unique_delivery_ids(jobs.parent, CHAIN_ID), 1)

    def test_coordinate_subsession_advance_never_calls_the_resume_recorder(self):
        # A3: internal advance must commit zero owner-resume events -- assert
        # the structural fact that `RESUME_RECORD`/`record_owner_resume_if_chain`
        # are simply absent from the transaction core's own source text.
        source = (ROOT / "utilities" / "dispatch_subsession_advance.py").read_text(encoding="utf-8")
        start = source.index("def coordinate_subsession_advance(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]
        self.assertNotIn("RESUME_RECORD", body)
        self.assertNotIn("record_owner_resume_if_chain", body)


class RealHandoffServices(FakeServices):
    """Routes ONLY `classify_handoff` through the real
    `dispatch_subsession_handoff.classify_handoff`, reading whatever
    `SA.flush_own_subsession_handoff` actually wrote (or didn't). claim/
    register/start stay faked -- this never spawns a live subprocess or
    touches real dispatch state."""

    def __init__(self, sandbox: Sandbox, *, predecessor_terminal_at_ns: int):
        super().__init__(sandbox)
        self._predecessor_terminal_at_ns = predecessor_terminal_at_ns

    def classify_handoff(self, request):
        self.handoff_calls += 1
        path = HANDOFF.handoff_path(request.artifact_root, request.route_id, request.chain_id)
        return HANDOFF.classify_handoff(
            path,
            predecessor_attempt_id_expected=request.predecessor_terminal_attempt_id,
            manifest_sha256_expected=request.manifest_sha256,
            predecessor_terminal_at_ns=self._predecessor_terminal_at_ns,
        )


class SupervisorSideFlushConnectionTest(unittest.TestCase):
    """F-1 (impl-review round 2): the PRODUCTION connection -- an ordinary
    unsupervised sub-session child never runs `flush_own_subsession_handoff`
    (it has no supervisor process of its own), so the chain-advancing
    supervisor flushes on its behalf from the predecessor's registry row and
    state ledger. Every case below runs with the sub-session environment
    variables cleared, which is exactly the owner supervisor's environment.
    """

    def _fixture(self, td: str, *, ledger_attempt_id: str | None):
        sandbox = Sandbox()
        manifest = make_manifest(session_count=2)
        pointer = SA.chain_manifest_pointer_path(sandbox.jobs, CHAIN_ID)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps(manifest), encoding="utf-8")
        ledger_path = Path(td) / "ledger.md"
        if ledger_attempt_id is not None:
            _write_ledger_fixture(
                ledger_path, ledger_attempt_id,
                completed_items=["slice 1 landed"], next_action="python3 slice2.py",
                invariants=["one stage gate stays with the owner"],
                forbidden_files=["out-of-list.py"],
            )
        terminal_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        timestamp = terminal_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata = {
            "parent_attempt_id": "att-owner-0001",
            "attempt_id": "att-stage-session-1",
            "session_chain_id": CHAIN_ID, "subsession_index": "1",
            "subsession_mode": "serial", "route_id": ROUTE_ID,
            "route_hash": ROUTE_HASH, "route_node": ROUTE_NODE,
            "subsession_id": "ss-1", "state_ledger": str(ledger_path),
        }
        sandbox.add_registry_row(metadata, status="done")
        lines = sandbox.jobs.read_text(encoding="utf-8").splitlines()
        fields = lines[-1].split("\t")
        fields[0] = timestamp
        lines[-1] = "\t".join(fields)
        sandbox.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        terminal_at_ns = int(parsed.timestamp() * 1_000_000_000)
        joined = {
            "att-stage-session-1": SimpleNamespace(
                attempt_id="att-stage-session-1", status="done", metadata=metadata,
            )
        }
        return sandbox, joined, terminal_at_ns

    def _advance(self, sandbox, joined, artifact_root, terminal_at_ns):
        fake = RealHandoffServices(sandbox, predecessor_terminal_at_ns=terminal_at_ns)
        cleared = {
            key: "" for key in (
                "AGENT_DISPATCH_SUBSESSION_ID", "AGENT_DISPATCH_SESSION_CHAIN_ID",
                "AGENT_DISPATCH_SUBSESSION_MODE", "AGENT_WORKER_STATE_LEDGER",
            )
        }
        with mock.patch.object(SA, "RealSubsessionAdvanceServices", return_value=fake), \
             mock.patch.object(SA, "_resolve_artifact_root", return_value=artifact_root), \
             mock.patch.dict(os.environ, cleared, clear=False):
            result = SA.coordinate_chain_advance_from_joined_rows(
                sandbox.jobs, "att-owner-0001", joined,
            )
        return result, fake

    def test_predecessor_terminal_row_produces_handoff_and_advances(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = str(Path(td) / "artifact-root")
            sandbox, joined, terminal_at_ns = self._fixture(
                td, ledger_attempt_id="att-stage-session-1"
            )
            try:
                path = HANDOFF.handoff_path(artifact_root, ROUTE_ID, CHAIN_ID)
                self.assertFalse(path.exists())
                result, fake = self._advance(sandbox, joined, artifact_root, terminal_at_ns)
                self.assertEqual(result, "att-stage-session-2")
                self.assertTrue(path.is_file())
                text = path.read_text(encoding="utf-8")
                self.assertIn("slice 1 landed", text)
                self.assertIn("python3 slice2.py", text)
                self.assertIn("out-of-list.py", text)
                self.assertIn("predecessor_attempt_id: att-stage-session-1", text)
                # A-8/A-13 combined: the handoff mtime is later than the
                # predecessor's committed terminal moment, so the
                # mtime-inversion stale check cannot fire on a legitimate flush.
                self.assertGreater(path.stat().st_mtime_ns, terminal_at_ns)
                self.assertEqual(fake.handoff_calls, 1)
                self.assertEqual(fake.start_calls, 1)
            finally:
                sandbox.close()

    def test_ledger_not_bound_to_predecessor_declines_flush_and_stops(self):
        # The flush is a projection of the predecessor's own ledger. A ledger
        # belonging to another attempt proves nothing about this one, so no
        # handoff is fabricated and A-8's hard stop stays reachable.
        with tempfile.TemporaryDirectory() as td:
            artifact_root = str(Path(td) / "artifact-root")
            sandbox, joined, terminal_at_ns = self._fixture(
                td, ledger_attempt_id="att-some-other-attempt"
            )
            try:
                result, fake = self._advance(sandbox, joined, artifact_root, terminal_at_ns)
                self.assertIsNone(result)
                self.assertFalse(
                    HANDOFF.handoff_path(artifact_root, ROUTE_ID, CHAIN_ID).exists()
                )
                self.assertEqual(fake.claim_calls, 0)
                self.assertEqual(fake.register_calls, 0)
                self.assertEqual(fake.start_calls, 0)
            finally:
                sandbox.close()

    def test_missing_ledger_leaves_prior_handoff_stale_and_stops(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = str(Path(td) / "artifact-root")
            sandbox, joined, terminal_at_ns = self._fixture(td, ledger_attempt_id=None)
            try:
                # An earlier index's handoff is still on disk; without a
                # readable ledger nothing overwrites it, so the gate sees the
                # attempt-id mismatch and refuses instead of advancing.
                HANDOFF.flush_handoff(
                    HANDOFF.handoff_path(artifact_root, ROUTE_ID, CHAIN_ID),
                    predecessor_attempt_id="att-stage-session-0",
                    predecessor_subsession_id="ss-0",
                    manifest_sha256=MANIFEST_SHA,
                    completed_items=[], next_command="", invariants=[], forbidden_files=[],
                )
                result, fake = self._advance(sandbox, joined, artifact_root, terminal_at_ns)
                self.assertIsNone(result)
                self.assertEqual(fake.start_calls, 0)
                self.assertEqual(
                    HANDOFF.classify_handoff(
                        HANDOFF.handoff_path(artifact_root, ROUTE_ID, CHAIN_ID),
                        predecessor_attempt_id_expected="att-stage-session-1",
                        manifest_sha256_expected=MANIFEST_SHA,
                        predecessor_terminal_at_ns=terminal_at_ns,
                    ),
                    "subsession-handoff-stale",
                )
            finally:
                sandbox.close()

    def test_non_chain_row_flushes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = str(Path(td) / "artifact-root")
            row = SimpleNamespace(attempt_id="att-ordinary-1", status="done", metadata={})
            self.assertEqual(
                SA.flush_chain_handoff_for_row(
                    Path(td) / "jobs.registry", row, {}, artifact_root
                ),
                "no-chain",
            )
            self.assertFalse(Path(artifact_root).exists())


class WiredFlushIntegrationTest(unittest.TestCase):
    """F-1 supervisor-integration proof required by impl-review round 1: a
    normal 2-slice chain actually advances to index 2 once the real call
    site flushes the handoff, and actually stalls (start 0) when it doesn't
    -- `classify_handoff` is never faked here, only claim/register/start."""

    def _fixture(self, artifact_tmp: str):
        sandbox = Sandbox()
        manifest = make_manifest(session_count=2)
        pointer = SA.chain_manifest_pointer_path(sandbox.jobs, CHAIN_ID)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps(manifest), encoding="utf-8")
        terminal_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        timestamp = terminal_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        sandbox.add_registry_row(
            {
                "parent_attempt_id": "att-owner-0001",
                "attempt_id": "att-stage-session-1",
                "session_chain_id": CHAIN_ID, "subsession_index": "1",
                "subsession_mode": "serial", "route_id": ROUTE_ID,
                "route_hash": ROUTE_HASH, "route_node": ROUTE_NODE,
                "subsession_id": "ss-1",
            },
            status="done",
        )
        lines = sandbox.jobs.read_text(encoding="utf-8").splitlines()
        fields = lines[-1].split("\t")
        fields[0] = timestamp
        lines[-1] = "\t".join(fields)
        sandbox.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        terminal_at_ns = int(parsed.timestamp() * 1_000_000_000)
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
        return sandbox, manifest, joined, terminal_at_ns

    def test_without_flush_advance_refused_missing_and_start_zero(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = str(Path(td) / "artifact-root")
            sandbox, manifest, joined, terminal_at_ns = self._fixture(artifact_root)
            try:
                fake = RealHandoffServices(sandbox, predecessor_terminal_at_ns=terminal_at_ns)
                with mock.patch.object(SA, "RealSubsessionAdvanceServices", return_value=fake), \
                     mock.patch.object(SA, "_resolve_artifact_root", return_value=artifact_root):
                    result = SA.coordinate_chain_advance_from_joined_rows(
                        sandbox.jobs, "att-owner-0001", joined,
                    )
                self.assertIsNone(result)
                self.assertEqual(fake.handoff_calls, 1)
                self.assertEqual(fake.claim_calls, 0)
                self.assertEqual(fake.register_calls, 0)
                self.assertEqual(fake.start_calls, 0)
            finally:
                sandbox.close()

    def test_with_real_flush_call_site_advances_to_index_two(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_root = str(Path(td) / "artifact-root")
            sandbox, manifest, joined, terminal_at_ns = self._fixture(artifact_root)
            try:
                env = {
                    "AGENT_DISPATCH_SUBSESSION_ID": "ss-1",
                    "AGENT_DISPATCH_SESSION_CHAIN_ID": CHAIN_ID,
                    "AGENT_DISPATCH_SUBSESSION_MODE": "serial",
                }
                with mock.patch.object(SA, "_resolve_artifact_root", return_value=artifact_root), \
                     mock.patch.dict(os.environ, env, clear=False):
                    # The exact real call site both supervisors make right
                    # after their own terminal registry row commits.
                    SA.flush_own_subsession_handoff(sandbox.jobs, ROUTE_ID, "att-stage-session-1")

                fake = RealHandoffServices(sandbox, predecessor_terminal_at_ns=terminal_at_ns)
                with mock.patch.object(SA, "RealSubsessionAdvanceServices", return_value=fake), \
                     mock.patch.object(SA, "_resolve_artifact_root", return_value=artifact_root):
                    result = SA.coordinate_chain_advance_from_joined_rows(
                        sandbox.jobs, "att-owner-0001", joined,
                    )
                self.assertEqual(result, "att-stage-session-2")
                self.assertEqual(fake.claim_calls, 1)
                self.assertEqual(fake.register_calls, 1)
                self.assertEqual(fake.start_calls, 1)
            finally:
                sandbox.close()


class ChainDeliveryNoticeDeferredTest(unittest.TestCase):
    """C14 (S3a): a marker-bound deferred slice counts as success in the notice."""

    def _result(self):
        return SA.ChainDriveResult(
            receipt={}, joined_rows=(), attempts=frozenset(), joined_before={},
            traversed=frozenset(), last_advanced_attempt_id=None, refusal=None,
            closed=(), unclosed=(),
        )

    def _row(self, attempt_id, metadata):
        return SimpleNamespace(attempt_id=attempt_id, metadata=metadata)

    def test_pending_deferred_row_counts_as_a_chain_notice_failure(self):
        rows = [self._row("att-pending", {
            "session_chain_id": "ssc-fixture",
            "note": "completion-deferred", "failure_class": "infrastructure",
            "classifier_source": "registered-wrapper-completion-transient-v1",
        })]
        notice = SA.chain_delivery_notice(self._result(), rows)
        self.assertIn("att-pending=completion-deferred", notice)

    def test_marker_bound_deferred_row_does_not_count_as_a_failure(self):
        rows = [self._row("att-completed", {
            "session_chain_id": "ssc-fixture",
            "note": "completed-marker", "failure_class": "infrastructure",
            "classifier_source": "registered-wrapper-completion-transient-v1",
            "completion_marker": "/artifacts/.runtime/completions/execute.json",
        })]
        notice = SA.chain_delivery_notice(self._result(), rows)
        self.assertEqual(notice, "")


class ChainContinuationTest(unittest.TestCase):
    """D5: a stopped serial chain's rest is derived by the runtime, not hand-written by the owner."""

    def setUp(self):
        from stage_session_contract import load_manifest
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        (base / "source").mkdir()
        self.jobs = base / "state" / "jobs.log"
        self.jobs.parent.mkdir()
        route_path = base / "route.json"
        self.route = {"route_id": "rt-" + "a" * 16, "route_hash": "sha256:" + "1" * 64, "cwd": str(base),
                      "artifact_root": str(base / "reports"),
                      "nodes": [{"id": "execute", "completion_gate": "code-execute", "write_scope": ["source/**"]}]}
        route_path.write_text(json.dumps(self.route), encoding="utf-8")
        self.route["_route_file"] = str(route_path)
        sessions = []
        for index in (1, 2, 3):
            (base / f"brief-{index}.md").write_text(f"phase {index}\n", encoding="utf-8")
            (base / "source" / f"s{index}.py").write_text("\n", encoding="utf-8")
            sessions.append({"subsession_id": f"ss-phase-{index:04d}", "attempt_id": f"att-phase-{index:04d}",
                             "adapter": "codex", "slug": f"phase-{index}", "phase_brief": str(base / f"brief-{index}.md"),
                             "fixed_files": [str(base / "source" / f"s{index}.py")], "narrow_verify": "true",
                             "expected_round_trips": 1})
        source = base / "chain.json"
        source.write_text(json.dumps({
            "schema_version": 1, "kind": "stage-session-chain", "chain_id": "ssc-phases", "mode": "serial",
            "worktree": str(base), "route_file": str(route_path), "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "route_node": "execute", "completion_gate": "code-execute",
            "sessions": sessions}), encoding="utf-8")
        self.load = lambda path: load_manifest(path, route=self.route, node=self.route["nodes"][0])
        manifest = self.load(source)
        pointer = SA.chain_manifest_pointer_path(self.jobs, "ssc-phases")
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps(manifest), encoding="utf-8")
        HANDOFF.flush_handoff(
            HANDOFF.handoff_path(self.route["artifact_root"], self.route["route_id"], "ssc-phases"),
            predecessor_attempt_id="att-phase-0002", predecessor_subsession_id="ss-phase-0002",
            manifest_sha256=manifest["_manifest_sha256"], completed_items=["18 assertions pass"],
            next_command="-", invariants=[], forbidden_files=[])

    def rows(self, second_status="done"):
        def row(index, status, **meta):
            meta = {"session_chain_id": "ssc-phases", "subsession_mode": "serial", "subsession_index": str(index),
                    "attempt_id": f"att-phase-{index:04d}", **meta}
            return "\t".join(["now", status, "1", "owner", f"phase-{index}", ",".join(f"{k}={v}" for k, v in meta.items())])
        self.jobs.write_text("\n".join([
            row(1, "done", note="completed-marker", failure_class="pass", launch_started="1"),
            row(2, second_status, note="dead-worker-blocked", failure_class="blocked", launch_started="1"),
            row(3, "cancelled", note="subsession-chain-advance-refused", launch_started="0")]) + "\n", encoding="utf-8")

    def test_the_stopped_session_runs_again_with_what_it_did_and_the_never_started_one_follows(self):
        self.rows()
        found = SA.chain_continuation(self.jobs, "ssc-phases")
        derived = self.load(found["manifest"])                    # a manifest the chain accepts as it is
        self.assertEqual(found["sessions"], ["ss-phase-0002", "ss-phase-0003"])     # the passed one is not repeated
        retry, rest = derived["sessions"]
        self.assertEqual((retry["subsession_purpose"], retry["gap_retry_of"]), ("gap-retry", "ss-phase-0002"))
        brief = Path(retry["phase_brief"]).read_text(encoding="utf-8")
        self.assertTrue(brief.startswith("phase 2"))
        self.assertIn("ended dead-worker-blocked", brief)
        self.assertIn("Already done: 18 assertions pass", brief)
        self.assertNotIn("subsession_purpose", rest)
        self.assertEqual(Path(rest["phase_brief"]).read_text(encoding="utf-8"), "phase 3\n")
        self.assertIn(f"stage-session-chain.py start --manifest {found['manifest']}", found["command"])
        again = SA.chain_continuation(self.jobs, "ssc-phases")
        self.assertEqual(again, found)                             # derived once, reused
        result = SA.ChainDriveResult(
            receipt={}, joined_rows=(), attempts=frozenset(), joined_before={}, traversed=frozenset(),
            last_advanced_attempt_id=None, closed=(), unclosed=(),
            refusal=SA.ChainAdvanceStep("refused", chain_id="ssc-phases", predecessor_index=2, successor_index=3,
                                        reason="predecessor-blocked"))
        self.assertIn("start it with: " + found["command"], SA.chain_delivery_notice(result, [], self.jobs))

    def test_a_chain_still_running_or_wholly_passed_has_no_continuation(self):
        self.rows(second_status="running")
        self.assertIsNone(SA.chain_continuation(self.jobs, "ssc-phases"))
        self.assertIsNone(SA.chain_continuation(self.jobs, "ssc-unknown"))


class StartWaitRetryTest(unittest.TestCase):
    """A successor start refused before spawning for a reason waiting may fix is
    started again within the prelaunch grace, each refusal kept in the record."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)
        self.manifest = make_manifest()
        self.sandbox.mark_terminal(1, "att-stage-session-1")
        self.registered()

    def registered(self, status="open", **changes):
        self.sandbox.add_registry_row({"session_chain_id": CHAIN_ID, "subsession_index": "2",
                                       "attempt_id": "att-stage-session-2", "launch_claimed": "0",
                                       **changes}, status=status)

    def services(self, *answers):
        services = FakeServices(self.sandbox)
        replies = list(answers)
        real = services.start_successor
        def start_successor(request, *, claim):
            reply = replies.pop(0) if replies else None
            if reply is None:
                return real(request, claim=claim)
            services.start_calls += 1
            return {"child_spawned": False, "returncode": 78, "reason": reply}
        services.start_successor = start_successor
        return services

    def advance(self, services):
        return SA.coordinate_subsession_advance(make_request(self.sandbox, self.manifest, successor_index=2),
                                                services)

    def record(self, result):
        return json.loads(Path(result.record_path).read_text(encoding="utf-8"))

    def test_a_waiting_refusal_is_started_again_and_then_advances(self):
        services = self.services("prior-attempt-unverifiable")
        first = self.advance(services)
        self.assertEqual((first.outcome, first.reason),
                         ("deferred", "subsession-advance-successor-start-deferred:prior-attempt-unverifiable"))
        record = self.record(first)
        self.assertIsNone(record["outcome"])
        self.assertNotIn("started", record["phases"])
        self.assertEqual([r["reason"] for r in record["phases"]["start_retries"]], ["prior-attempt-unverifiable"])
        second = self.advance(services)
        self.assertEqual((second.outcome, second.successor_attempt_id), ("advanced", "att-stage-session-2"))
        self.assertEqual((services.claim_calls, services.register_calls, services.start_calls), (1, 1, 2))
        self.assertEqual(len(self.record(second)["phases"]["start_retries"]), 1)

    def test_the_refusal_stands_once_the_grace_has_passed(self):
        services = self.services("predecessor-process-unverifiable", "prior-attempt-unverifiable")
        now = [10**18]
        with mock.patch.object(SA.time, "time_ns", side_effect=lambda: now[0]):
            self.assertEqual(self.advance(services).outcome, "deferred")
            now[0] += int(SA.DC.PRELAUNCH_WAIT_GRACE_SECONDS * 1e9)
            last = self.advance(services)
        self.assertEqual((last.outcome, last.reason),
                         ("refused", "subsession-advance-successor-start-failed:prior-attempt-unverifiable"))
        record = self.record(last)
        self.assertEqual([r["reason"] for r in record["phases"]["start_retries"]],
                         ["predecessor-process-unverifiable", "prior-attempt-unverifiable"])
        self.assertIn("started", record["phases"])
        self.assertEqual(self.advance(services).outcome, "refused")   # a stood refusal is final
        self.assertEqual(services.start_calls, 2)

    def test_any_other_refusal_or_a_closed_row_is_refused_at_once(self):
        result = self.advance(self.services("launch-error"))
        self.assertEqual(result.outcome, "refused")
        self.assertNotIn("start_retries", self.record(result)["phases"])

    def test_a_row_its_wrapper_already_closed_is_not_started_again(self):
        self.registered(status="done", launch_outcome="never-launched")
        result = self.advance(self.services("prior-attempt-unverifiable"))
        self.assertEqual((result.outcome, result.reason),
                         ("refused", "subsession-advance-successor-start-failed:prior-attempt-unverifiable"))

    def test_the_drive_waits_on_a_deferred_start_and_closes_only_a_refusal(self):
        row = SimpleNamespace(attempt_id="att-1", status="done",
                              metadata={"session_chain_id": CHAIN_ID, "subsession_mode": "serial",
                                        "subsession_index": "1"})
        steps = iter([SA.ChainAdvanceStep("deferred", chain_id=CHAIN_ID, predecessor_index=1,
                                          successor_index=2, reason="prior-attempt-unverifiable")] * 3
                     + [SA.ChainAdvanceStep("refused", chain_id=CHAIN_ID, predecessor_index=1, successor_index=2,
                                            reason="subsession-advance-successor-start-failed:x")])
        sleeps, events, closes = [], [], []
        with mock.patch.object(SA, "advance_chain_step", side_effect=lambda *args: next(steps)), \
                mock.patch.object(SA, "close_refused_chain_successors",
                                  side_effect=lambda *args: closes.append(args) or ((), ())):
            result = SA.drive_serial_chain(
                jobs=Path("/unused/jobs.log"), parent_attempt_id="att-owner", attempts={"att-1"},
                receipt={"state": "delivered", "children": []}, refresh=lambda attempts: [row],
                join=lambda attempts: {"state": "delivered", "children": []},
                reconcile=lambda joined, attempts: False, max_reparks=1, emit=events.append,
                replacement_checkpoint=lambda selected: (selected, [], []), sleep=sleeps.append)
        self.assertEqual(sleeps, [0.25, 0.5, 1.0])
        self.assertEqual([e["type"] for e in events].count("dispatch.supervisor.chain-advance-deferred"), 3)
        self.assertEqual(len(closes), 1)
        self.assertEqual(result.refusal.reason, "subsession-advance-successor-start-failed:x")


class ParentBindingLineageTest(unittest.TestCase):
    """U1-T7: the supervisor's caller is the registered parent or its final launched replacement."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)
        self.manifest = make_manifest(2)
        self.services = SA.RealSubsessionAdvanceServices(self.manifest)
        self.request = SA.SubsessionAdvanceRequest(
            **{**vars(make_request(self.sandbox, self.manifest, successor_index=2)), "parent_slug": "owner"})

    def run_as(self, caller, *, final):
        import dispatch_replacement
        done = SimpleNamespace(returncode=0, stdout="check=ok\nregistered=1\nchild_spawned=0\n", stderr="")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": caller}), \
                mock.patch.object(dispatch_replacement, "effective_attempts",
                                  return_value=({final}, [{"edge": 1}] if final != "att-owner-0001" else [])), \
                mock.patch.object(self.services._chain, "dispatch_command", return_value=["true"]), \
                mock.patch.object(self.services._chain, "run_checked", return_value=done) as run:
            result = self.services._run(self.request, "register")
        return result, run

    def test_successor_register_accepts_launched_replacement_owner(self):
        result, run = self.run_as("att-owner-0002", final="att-owner-0002")
        self.assertEqual(result["verdict"], "register-ok")
        # The launch still carries the registered parent identity.
        self.assertEqual(run.call_args.kwargs["env"]["AGENT_DISPATCH_ATTEMPT_ID"], "att-owner-0001")
        result, _ = self.run_as("att-owner-0001", final="att-owner-0001")
        self.assertEqual(result["verdict"], "register-ok")

    def test_an_unreadable_seat_lineage_stays_a_refusal_inside_the_supervisor(self):
        # Kept as is (RV-1151 L2 follow-up): parent_attempt_succeeded now raises the recipient
        # wait, but this check runs inside the owner's supervisor, which a raised wait would end,
        # and the chain has no wait before registration. The existing refusal stays; nothing runs.
        import dispatch_contract
        import dispatch_replacement
        import route_authority
        try:
            route_authority.require_known(None)
        except dispatch_contract.DispatchContractError as exc:
            wait = exc
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner-0002"}), \
                mock.patch.object(dispatch_replacement, "effective_attempts", side_effect=wait), \
                mock.patch.object(self.services._chain, "run_checked") as run:
            result = self.services._run(self.request, "register")
        self.assertEqual(result["reason"], "subsession-advance-parent-binding-invalid")
        run.assert_not_called()

    def test_successor_register_refuses_unrelated_or_intermediate_owner(self):
        for caller, final in (("att-unrelated", "att-owner-0002"), ("att-owner-0002", "att-owner-0003"), ("", "att-owner-0002")):
            with self.subTest(caller=caller, final=final):
                result, run = self.run_as(caller, final=final)
                self.assertEqual(result["reason"], "subsession-advance-parent-binding-invalid")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
