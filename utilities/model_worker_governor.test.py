#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PATH = Path(__file__).with_name("model-worker-governor.py")
SPEC = importlib.util.spec_from_file_location("model_worker_governor", PATH)
GOVERNOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GOVERNOR)
import replica_batch_contract as CONTRACT
from replica_batch_contract import build_manifest


def _run_as_non_group_actor(case):
    """Keep API/witness tests independent of host-wide procfs churn.

    The runner makes the suite a group leader. An unrelated PID disappearing
    during its /proc scan correctly blocks group return. Exercise these
    non-group API cases in a real child, as _legacy_migration_return does;
    dedicated group-drain/incomplete-observation cases retain their coverage.
    """
    if os.getpid() != os.getpgrp():
        return False
    test_name = f"{type(case).__name__}.{case._testMethodName}"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), test_name],
        capture_output=True, text=True, timeout=60,
    )
    case.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return True


def _legacy_migration_return(root):
    """Exercise the original non-group API in a real child, not a mocked PGID.

    The isolated suite runner owns a process group. Its one-shot release may
    conservatively retain a group lease on incomplete procfs observation;
    legacy_run returning its own token cannot prove that earlier return.
    Group drain remains covered by the actual run and namespace tests.
    """
    pid, pgid = os.getpid(), os.getpgrp()
    if pid == pgid:
        raise AssertionError("legacy contract actor must not own a process group")
    checked = GOVERNOR.check(root, "dispatch", total=2, budget=2)
    after_check = json.loads(Path(root, "state.json").read_text())
    token = GOVERNOR.acquire(root, "dispatch", total=2, budget=2)
    acquired = json.loads(Path(root, "state.json").read_text())["leases"][token]
    returned = GOVERNOR.release(root, token)
    after_return = json.loads(Path(root, "state.json").read_text())
    return {
        "pid": pid, "pgid": pgid, "checked": checked,
        "after_check": after_check, "token": token, "acquired": acquired,
        "returned": returned, "after_return": after_return,
    }


class GovernorTest(unittest.TestCase):
    def manifest(self, second_harness="claude"):
        return build_manifest(
            replica_group="plan",
            route_id="rt-governor",
            parent_attempt_id="att-parent-governor",
            independence="cross-harness",
            members=[
                {
                    "assignment_sha256": "sha256:" + "a" * 64,
                    "attempt_id": "att-plan-one",
                    "route_node": "plan",
                    "harness": "codex",
                    "fallback_hop": "same-harness-headless",
                    "fallback_ordinal": 1,
                    "model_profile": "balanced-deep",
                    "perspective": "primary-plan",
                    "parallel_leg_index": 0,
                    "leg_class": "peer",
                },
                {
                    "assignment_sha256": "sha256:" + "a" * 64,
                    "attempt_id": "att-plan-two",
                    "route_node": "plan-replica",
                    "harness": second_harness,
                    "fallback_hop": "cross-harness-headless",
                    "fallback_ordinal": 2,
                    "model_profile": "light",
                    "perspective": "independent-plan",
                    "parallel_leg_index": 1,
                    "leg_class": "peer",
                },
            ],
            required_independence_axes=["cross-harness", "model-profile", "perspective"],
            realized_independence_axes=["cross-harness", "model-profile", "perspective"],
        )

    def reserve_batch(self, root, count, batch):
        """Exercise batch semantics with an explicitly verified API capability."""

        with mock.patch.object(
            GOVERNOR, "_batch_issuer_is_current_parent", return_value=True
        ):
            issuer = GOVERNOR._issue_batch_issuer_capability(os.getpid())
        return GOVERNOR.reserve(
            root,
            "dispatch",
            count,
            batch=batch,
            batch_issuer=issuer,
        )

    def _dispatch_cap(self):
        """Effective dispatch admissions: whichever of the class/global cap binds first.

        Derived instead of hardcoded so raising a cap does not turn these
        boundedness assertions into false failures.
        """
        return min(GOVERNOR.CLASS_LIMITS["dispatch"], GOVERNOR.DEFAULT_TOTAL_LIMIT)

    def test_caps_release_and_kill_switch(self):
        if _run_as_non_group_actor(self):
            return
        with tempfile.TemporaryDirectory() as temp_dir:
            cap = self._dispatch_cap()
            tokens = [GOVERNOR.acquire(temp_dir, "dispatch") for _ in range(cap)]
            with self.assertRaisesRegex(ValueError, "global model-worker cap|class cap"):
                GOVERNOR.acquire(temp_dir, "dispatch")
            returned = GOVERNOR.release(temp_dir, tokens.pop())
            self.assertTrue(returned["release_proven"], returned)
            tokens.append(GOVERNOR.acquire(temp_dir, "dispatch"))
            Path(temp_dir, "KILL_SWITCH").touch()
            with self.assertRaisesRegex(ValueError, "kill switch"):
                GOVERNOR.acquire(temp_dir, "title")

    def test_fifty_attempts_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            admitted = 0
            for _ in range(50):
                try:
                    GOVERNOR.acquire(temp_dir, "dispatch")
                    admitted += 1
                except ValueError:
                    pass
            self.assertEqual(admitted, self._dispatch_cap())

    def test_check_does_not_consume_start_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for _ in range(10):
                GOVERNOR.check(temp_dir, "dispatch", budget=1)
            token = GOVERNOR.acquire(temp_dir, "dispatch", budget=1)
            GOVERNOR.release(temp_dir, token)
            with self.assertRaisesRegex(ValueError, "start budget"):
                GOVERNOR.acquire(temp_dir, "dispatch", budget=1)

    def test_reserve_is_all_or_none_for_class_total_and_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease = GOVERNOR.acquire(temp_dir, "dispatch", total=3, budget=10)
            tokens = GOVERNOR.reserve(
                temp_dir, "dispatch", 2, total=3, budget=10
            )
            self.assertEqual(len(tokens), 2)

            before = json.loads(Path(temp_dir, "state.json").read_text())
            with self.assertRaisesRegex(ValueError, "global.*cap|class cap"):
                GOVERNOR.reserve(temp_dir, "dispatch", 1, total=3, budget=10)
            after = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(after["reservations"], before["reservations"])

            for token in tokens:
                GOVERNOR.cancel_reservation(temp_dir, token)
            GOVERNOR.release(temp_dir, lease)
            with self.assertRaisesRegex(ValueError, "start budget"):
                GOVERNOR.reserve(temp_dir, "dispatch", 2, total=5, budget=1)
            self.assertEqual(
                json.loads(Path(temp_dir, "state.json").read_text())["reservations"],
                {},
            )

    def test_competing_multi_slot_reservations_never_partially_admit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Each contender alone fits; together they overcommit whatever the
            # current cap is, which is the condition this case exists to test.
            slice_count = self._dispatch_cap() // 2 + 1
            command = [
                sys.executable,
                str(PATH),
                "--root",
                temp_dir,
                "reserve",
                "--class",
                "dispatch",
                "--count",
                str(slice_count),
                "--pid",
                str(os.getpid()),
            ]
            contenders = [
                subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
                for _ in range(2)
            ]
            results = [process.communicate(timeout=5) for process in contenders]
            self.assertEqual(sorted(process.returncode for process in contenders), [0, 75])
            admitted = [
                json.loads(stdout)
                for process, (stdout, _) in zip(contenders, results)
                if process.returncode == 0
            ]
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0]["count"], slice_count)
            state = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(len(state["reservations"]), slice_count)

    def test_claim_transfers_reserved_capacity_and_cancel_never_releases_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            claimed, unclaimed = GOVERNOR.reserve(
                temp_dir, "dispatch", 2, total=5, budget=2
            )
            with self.assertRaisesRegex(ValueError, "start budget"):
                GOVERNOR.acquire(temp_dir, "title", total=5, budget=2)

            lease = GOVERNOR.claim_reservation(temp_dir, claimed, "dispatch")
            state = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(lease, claimed)
            self.assertIn(claimed, state["leases"])
            self.assertNotIn(claimed, state["reservations"])
            self.assertEqual(len(state["starts"]), 1)
            with self.assertRaisesRegex(ValueError, "class mismatch"):
                GOVERNOR.claim_reservation(temp_dir, unclaimed, "title")
            self.assertIn(
                unclaimed,
                json.loads(Path(temp_dir, "state.json").read_text())["reservations"],
            )
            with self.assertRaisesRegex(ValueError, "already claimed"):
                GOVERNOR.cancel_reservation(temp_dir, claimed)
            self.assertIn(
                claimed,
                json.loads(Path(temp_dir, "state.json").read_text())["leases"],
            )

            self.assertTrue(GOVERNOR.cancel_reservation(temp_dir, unclaimed))
            self.assertFalse(GOVERNOR.cancel_reservation(temp_dir, unclaimed))
            GOVERNOR.release(temp_dir, lease)
            replacement = GOVERNOR.acquire(temp_dir, "title", total=5, budget=2)
            GOVERNOR.release(temp_dir, replacement)

    def test_bound_replica_batch_provenance_is_atomic_and_survives_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, digest, legs = self.manifest()
            tokens = self.reserve_batch(
                temp_dir,
                2,
                {
                    "manifest": manifest,
                    "selected_attempt_ids": ["att-plan-one", "att-plan-two"],
                },
            )
            first = GOVERNOR.reservation_check(temp_dir, tokens[0])
            second = GOVERNOR.reservation_check(temp_dir, tokens[1])
            self.assertEqual(first["reservation_kind"], "parallel-batch")
            self.assertEqual(first["batch_declared_size"], 2)
            self.assertEqual(first["batch_admission_count"], 2)
            self.assertEqual(first["batch_manifest_sha256"], digest)
            self.assertEqual(first["batch_leg_sha256"], legs["att-plan-one"])
            self.assertEqual(second["batch_leg_sha256"], legs["att-plan-two"])
            GOVERNOR.claim_reservation(temp_dir, tokens[0], "dispatch")
            claimed = GOVERNOR.reservation_check(temp_dir, tokens[0])
            self.assertEqual(claimed["state"], "claimed")
            for key in GOVERNOR.BATCH_RESERVATION_KEYS:
                if key in first:
                    self.assertEqual(claimed[key], first[key])

    def test_opencode_parallel_manifest_reserves_and_survives_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, digest, legs = self.manifest(second_harness="opencode")
            tokens = self.reserve_batch(
                temp_dir,
                2,
                {
                    "manifest": manifest,
                    "selected_attempt_ids": ["att-plan-one", "att-plan-two"],
                },
            )
            second = GOVERNOR.reservation_check(temp_dir, tokens[1])
            self.assertEqual(second["reservation_kind"], "parallel-batch")
            self.assertEqual(second["batch_harness"], "opencode")
            self.assertEqual(second["batch_manifest_sha256"], digest)
            self.assertEqual(second["batch_leg_sha256"], legs["att-plan-two"])
            GOVERNOR.claim_reservation(temp_dir, tokens[1], "dispatch")
            claimed = GOVERNOR.reservation_check(temp_dir, tokens[1])
            self.assertEqual(claimed["state"], "claimed")
            self.assertEqual(claimed["batch_harness"], "opencode")
            self.assertEqual(claimed["batch_manifest_sha256"], digest)

    def test_bound_replica_partial_recovery_reserves_one_declared_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, digest, legs = self.manifest()
            route_path = Path(temp_dir, "route.json")
            route_path.write_text(json.dumps({
                "route_id": "rt-governor",
                "cwd": temp_dir,
                "nodes": [
                    {"id": "plan", "parallel_group": "plan", "replica_group": "plan"},
                    {"id": "plan-replica", "parallel_group": "plan", "replica_group": "plan"},
                ],
            }), encoding="utf-8")
            jobs = Path(temp_dir, "jobs.log")
            raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
            start = raw[raw.rfind(")") + 2 :].split()[19]
            metadata = (
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,harness=codex,child_harness=codex,"
                "route_id=rt-governor,route_node=plan,parent_attempt_id=att-parent-governor,"
                "fallback_ordinal=1,attempt_id=att-plan-one,launch_claimed=1,"
                "parallel_group=plan,replica_group=plan,"
                "reservation_kind=parallel-batch,batch_declared_size=2,batch_group=plan,"
                "batch_route_id=rt-governor,batch_parent_attempt_id=att-parent-governor,"
                "batch_attempt_id=att-plan-one,batch_route_node=plan,batch_harness=codex,"
                "batch_fallback_hop=same-harness-headless,batch_fallback_ordinal=1,"
                "batch_model_profile=balanced-deep,batch_perspective=primary-plan,"
                "batch_parallel_leg_index=0,"
                "batch_leg_class=peer,batch_auxiliary_check=-,"
                "batch_independence=cross-harness,batch_assignment_sha256=sha256:" + "a" * 64 + ","
                f"batch_manifest_sha256={digest},batch_leg_sha256={legs['att-plan-one']},"
                f"pid={os.getpid()},pid_start={start},"
                f"pid_observer_ns={os.readlink('/proc/self/ns/pid')}"
            )
            jobs.write_text(
                f"2026-07-24T00:00:00Z\topen\t{temp_dir}\t{temp_dir}\tpeer\t{metadata}\n",
                encoding="utf-8",
            )
            token = self.reserve_batch(
                temp_dir,
                1,
                {
                    "manifest": manifest,
                    "selected_attempt_ids": ["att-plan-two"],
                    "peers": [{
                        "agent_home": temp_dir,
                        "attempt_id": "att-plan-one",
                        "jobs": str(jobs),
                        "route": str(route_path),
                    }],
                },
            )[0]
            receipt = GOVERNOR.reservation_check(temp_dir, token)
            self.assertEqual(receipt["batch_declared_size"], 2)
            self.assertEqual(receipt["batch_admission_count"], 1)
            self.assertEqual(receipt["batch_manifest_sha256"], digest)
            self.assertEqual(receipt["batch_leg_sha256"], legs["att-plan-two"])
            self.assertEqual(receipt["batch_attempt_id"], "att-plan-two")
            self.assertEqual(receipt["batch_peer_count"], 1)
            proof = receipt["batch_peer_set"][0]
            self.assertEqual(proof["attempt_id"], "att-plan-one")
            self.assertEqual(proof["manifest_sha256"], digest)
            self.assertEqual(proof["state"], "active")
            encoded = json.dumps(
                [proof], separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            self.assertEqual(
                receipt["batch_peer_set_sha256"],
                "sha256:" + __import__("hashlib").sha256(encoded).hexdigest(),
            )

    def test_bound_replica_partial_recovery_without_peer_proof_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, _digest, _legs = self.manifest()
            with self.assertRaisesRegex(ValueError, "N-1 peer set"):
                self.reserve_batch(
                    temp_dir,
                    1,
                    {
                        "manifest": manifest,
                        "selected_attempt_ids": ["att-plan-two"],
                    },
                )

    def test_partial_continuation_supersedes_only_gap_and_reuses_retry_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source, source_digest, source_legs = self.manifest()
            replacement_members = json.loads(json.dumps(source["members"]))
            replacement_members[1]["attempt_id"] = "att-retry-verbatim"
            replacement, replacement_digest, replacement_legs = build_manifest(
                parallel_group="plan",
                route_id="rt-governor",
                parent_attempt_id="att-parent-governor",
                independence="cross-harness",
                members=replacement_members,
                required_independence_axes=[
                    "cross-harness", "model-profile", "perspective"
                ],
                realized_independence_axes=[
                    "cross-harness", "model-profile", "perspective"
                ],
            )
            realized = [{
                "node_id": "plan",
                "terminal_attempt_id": "att-plan-one",
                "marker_path": str(Path(temp_dir, "plan.json")),
                "marker_digest": "sha256:" + "1" * 64,
                "verdict": "PASS",
                "quiescence_proof_digest": "sha256:" + "2" * 64,
                "output_evidence_digest": "sha256:" + "3" * 64,
                "contract_hash": "sha256:" + "4" * 64,
            }]
            peer_digest = GOVERNOR._record_digest(realized)
            identity = GOVERNOR._record_digest({
                "source_route_id": "rt-governor",
                "source_route_hash": "sha256:source-route",
                "source_group_id": "plan",
                "failed_source_attempt_id": "att-plan-two",
                "gap_leg_id": "plan-replica",
                "reused_peer_set_proof_digest": peer_digest,
            })
            partial = {
                "contract_version": 1,
                "source_group_id": "plan",
                "source_batch_manifest_digest": source_digest,
                "leg_manifest_digests": {
                    member["route_node"]: source_legs[member["attempt_id"]]
                    for member in source["members"]
                },
                "original_group_cardinality": 2,
                "join_policy": "all",
                "failed_source_attempt_id": "att-plan-two",
                "gap_leg_id": "plan-replica",
                "realized_peer_set": realized,
                "reused_peer_set_proof_digest": peer_digest,
                "replacement_leg_identity": identity,
                "replacement_attempt_id": "att-" + identity.split(":", 1)[1][:48],
            }
            continuation = {
                "continuation_contract_version": 1,
                "continuation_id": "cont-governor-at5",
                "source_route_id": "rt-governor",
                "source_route_hash": "sha256:source-route",
                "partial_group_continuation": partial,
            }
            seal = {
                "schema_version": 1,
                "continuation_id": "cont-governor-at5",
                "source_route_id": "rt-governor",
                "source_route_hash": "sha256:source-route",
                "source_group_id": "plan",
                "source_batch_manifest_digest": source_digest,
                "failed_source_attempt_id": "att-plan-two",
                "gap_leg_id": "plan-replica",
                "reused_peer_set_proof_digest": peer_digest,
                "replacement_leg_identity": identity,
                "replacement_attempt_id": "att-retry-verbatim",
                "retry_claim_reused": True,
            }
            route_path = Path(temp_dir, "route.json")
            route_path.write_text(json.dumps({
                "route_id": "rt-governor",
                "route_hash": "sha256:source-route",
                "registry_digest": "sha256:source-registry",
                "cwd": temp_dir,
                "nodes": [
                    {
                        "id": "plan", "parallel_group": "plan", "replica_group": "plan",
                        "dispatch_depth": 2, "completion_gate": "plan-gate",
                    },
                    {
                        "id": "plan-replica", "parallel_group": "plan",
                        "replica_group": "plan", "dispatch_depth": 2,
                        "completion_gate": "plan-replica-gate",
                    },
                ],
            }), encoding="utf-8")
            raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
            start = raw[raw.rfind(")") + 2 :].split()[19]

            def metadata(member, *, note="", retry=False):
                value = (
                    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    f"fallback_hop={member['fallback_hop']},harness={member['harness']},"
                    f"child_harness={member['harness']},route_id=rt-governor,"
                    # A modern route is identified by (route_id, route_hash,
                    # route_node) at every depth: `completion_attempt_readiness`
                    # refuses a row whose hash does not match the route it is
                    # asked about. Only a hash-less legacy row may omit it, so a
                    # fixture row without it is not "a peer row", it is an
                    # unverifiable one.
                    "route_hash=sha256:source-route,"
                    f"route_node={member['route_node']},"
                    "parent_attempt_id=att-parent-governor,"
                    f"fallback_ordinal={member['fallback_ordinal']},"
                    f"attempt_id={member['attempt_id']},launch_claimed=1,"
                    "parallel_group=plan,replica_group=plan,"
                    "reservation_kind=parallel-batch,batch_declared_size=2,"
                    "batch_group=plan,batch_route_id=rt-governor,"
                    "batch_parent_attempt_id=att-parent-governor,"
                    f"batch_attempt_id={member['attempt_id']},"
                    f"batch_route_node={member['route_node']},"
                    f"batch_harness={member['harness']},"
                    f"batch_fallback_hop={member['fallback_hop']},"
                    f"batch_fallback_ordinal={member['fallback_ordinal']},"
                    f"batch_model_profile={member['model_profile']},"
                    f"batch_perspective={member['perspective']},"
                    f"batch_parallel_leg_index={member['parallel_leg_index']},"
                    "batch_leg_class=peer,batch_auxiliary_check=-,"
                    "batch_independence=cross-harness,"
                    f"batch_assignment_sha256={member['assignment_sha256']},"
                    f"batch_manifest_sha256={source_digest},"
                    f"batch_leg_sha256={source_legs[member['attempt_id']]}"
                )
                if note:
                    value += f",note={note}"
                if retry:
                    value += (
                        ",recovery_id=rec-at5,retry_ordinal=1,"
                        "retry_attempt_id=att-retry-verbatim"
                    )
                return value

            peer, gap = source["members"]
            peer_metadata = metadata(peer) + (
                f",pid={os.getpid()},pid_start={start},"
                f"pid_observer_ns={os.readlink('/proc/self/ns/pid')}"
            )
            gap_metadata = metadata(
                gap, note="cancelled-receipt-unavailable", retry=True
            )
            jobs = Path(temp_dir, "jobs.log")
            jobs.write_text(
                f"2026-07-24T00:00:00Z\topen\t{temp_dir}\t{temp_dir}\tpeer\t"
                f"{peer_metadata}\n"
                f"2026-07-24T00:01:00Z\tdone\t{temp_dir}\t{temp_dir}\tgap\t"
                f"{gap_metadata}\n",
                encoding="utf-8",
            )
            batch = {
                "manifest": replacement,
                "selected_attempt_ids": ["att-retry-verbatim"],
                "peers": [{
                    "agent_home": temp_dir,
                    "attempt_id": "att-plan-one",
                    "jobs": str(jobs),
                    "route": str(route_path),
                }],
                "source_manifest": source,
                "continuation": continuation,
                "replacement_seal": seal,
            }
            with self.assertRaisesRegex(
                ValueError, "partial continuation peer is not immutable terminal success"
            ):
                self.reserve_batch(temp_dir, 1, batch)
            self.assertFalse(Path(temp_dir, "state.json").exists())

            evidence = Path(temp_dir, "peer-output.md")
            evidence.write_text("peer output\n", encoding="utf-8")
            evidence_digest = __import__("hashlib").sha256(evidence.read_bytes()).hexdigest()
            completion = Path(temp_dir, "completion", "rt-governor")
            completion.mkdir(parents=True)
            marker_path = completion / "plan.json"
            history_path = completion / "plan.1.json"
            marker = {
                "schema_version": 2,
                "sequence": 1,
                "route_id": "rt-governor",
                "route_hash": "sha256:source-route",
                "registry_digest": "sha256:source-registry",
                "node_id": "plan",
                "completion_gate": "plan-gate",
                "attempt_id": "att-plan-one",
                "dispatch_depth": 2,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
                "evidence": {"path": str(evidence), "sha256": evidence_digest},
            }
            marker_json = json.dumps(marker, sort_keys=True)
            marker_path.write_text(marker_json, encoding="utf-8")
            history_path.write_text(marker_json, encoding="utf-8")
            (completion / "plan.att-plan-one.attempt.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "route_id": "rt-governor",
                    "node_id": "plan",
                    "attempt_id": "att-plan-one",
                    "dispatch_depth": 2,
                    "transport": "headless",
                    "execution_surface": "registered-headless",
                    "registered_worker": True,
                    "fallback_hop": "same-harness-headless",
                    "evidence_sha256": evidence_digest,
                    "completion_marker": str(marker_path),
                    "completion_marker_history": str(history_path),
                }, sort_keys=True),
                encoding="utf-8",
            )
            dead_pid = "99999999"
            # DR-1 (2026-08-28): an explicit non-pass class still refuses even with a
            # current marker on disk.
            failed_peer_metadata = metadata(peer, note="completed-marker") + (
                ",failure_class=blocked,"
                f"pid={dead_pid},pid_start=1,pgid={dead_pid},"
                f"pid_observer_ns={os.readlink('/proc/self/ns/pid')}"
            )
            jobs.write_text(
                f"2026-07-24T00:00:00Z\tdone\t{temp_dir}\t{temp_dir}\tpeer\t"
                f"{failed_peer_metadata}\n"
                f"2026-07-24T00:01:00Z\tdone\t{temp_dir}\t{temp_dir}\tgap\t"
                f"{gap_metadata}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "partial continuation peer is not immutable terminal success"
            ):
                self.reserve_batch(temp_dir, 1, batch)
            # DR-1: the ordinary completion path leaves no failure_class on the row
            # (live registry 45:1), so an ABSENT class must defer to the marker-verified
            # proof and be accepted as immutable terminal success.
            terminal_peer_metadata = metadata(peer, note="completed-marker") + (
                f",pid={dead_pid},pid_start=1,pgid={dead_pid},"
                f"pid_observer_ns={os.readlink('/proc/self/ns/pid')}"
            )
            jobs.write_text(
                f"2026-07-24T00:00:00Z\tdone\t{temp_dir}\t{temp_dir}\tpeer\t"
                f"{terminal_peer_metadata}\n"
                f"2026-07-24T00:01:00Z\tdone\t{temp_dir}\t{temp_dir}\tgap\t"
                f"{gap_metadata}\n",
                encoding="utf-8",
            )
            token = self.reserve_batch(temp_dir, 1, batch)[0]
            receipt = GOVERNOR.reservation_check(temp_dir, token)
            self.assertEqual(receipt["batch_attempt_id"], "att-retry-verbatim")
            self.assertEqual(receipt["batch_manifest_sha256"], replacement_digest)
            self.assertEqual(
                receipt["batch_leg_sha256"],
                replacement_legs["att-retry-verbatim"],
            )
            self.assertEqual(receipt["batch_admission_count"], 1)
            self.assertEqual(receipt["batch_peer_count"], 1)
            self.assertEqual(
                receipt["batch_peer_set"][0]["manifest_sha256"],
                replacement_digest,
            )
            self.assertEqual(
                receipt["batch_peer_set"][0]["attempt_id"],
                "att-plan-one",
            )

            before = json.loads(Path(temp_dir, "state.json").read_text())
            tampered = json.loads(json.dumps(batch))
            tampered["continuation"]["partial_group_continuation"][
                "source_batch_manifest_digest"
            ] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(
                ValueError, "partial continuation source binding mismatch"
            ):
                self.reserve_batch(temp_dir, 1, tampered)
            self.assertEqual(
                json.loads(Path(temp_dir, "state.json").read_text())["reservations"],
                before["reservations"],
            )

    def test_batch_reserve_python_api_requires_verified_issuer_capability(self):
        manifest, _digest, _legs = self.manifest()
        batch = {
            "manifest": manifest,
            "selected_attempt_ids": ["att-plan-one", "att-plan-two"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "issuer capability invalid"):
                GOVERNOR.reserve(temp_dir, "dispatch", 2, batch=batch)
            self.assertFalse(Path(temp_dir, "state.json").exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "issuer capability invalid"):
                GOVERNOR.reserve(
                    temp_dir,
                    "dispatch",
                    2,
                    batch=batch,
                    batch_issuer=object(),
                )
            self.assertFalse(Path(temp_dir, "state.json").exists())

    def test_cli_rejects_batch_manifest_minted_outside_dispatch_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, _digest, _legs = self.manifest()
            result = subprocess.run(
                [
                    sys.executable, str(PATH), "--root", temp_dir,
                    "reserve", "--class", "dispatch", "--count", "2",
                    "--pid", str(os.getpid()),
                    "--batch-manifest", json.dumps(manifest),
                    "--batch-attempt-id", "att-plan-one",
                    "--batch-attempt-id", "att-plan-two",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 75)
            self.assertIn("issuer is not dispatch-batch", result.stderr)
            state_path = Path(temp_dir, "state.json")
            self.assertFalse(state_path.exists())

    def test_cli_rejects_dispatch_batch_path_as_decoy_argv_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, _digest, _legs = self.manifest()
            parent_code = """
import json, os, subprocess, sys
governor, root, manifest = sys.argv[1:4]
result = subprocess.run(
    [sys.executable, governor, "--root", root, "reserve", "--class", "dispatch",
     "--count", "2", "--pid", str(os.getpid()), "--batch-manifest", manifest,
     "--batch-attempt-id", "att-plan-one", "--batch-attempt-id", "att-plan-two"],
    capture_output=True, text=True, check=False,
)
print(json.dumps({"returncode": result.returncode, "stderr": result.stderr}))
"""
            decoy = str(PATH.with_name("dispatch-batch.py").resolve())
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    str(PATH),
                    temp_dir,
                    json.dumps(manifest),
                    decoy,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["returncode"], 75)
            self.assertIn("issuer is not dispatch-batch", receipt["stderr"])
            self.assertFalse(Path(temp_dir, "state.json").exists())

    def test_governor_waits_for_same_group_descendant_before_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable, str(PATH), "--root", temp_dir,
                    "run", "--class", "dispatch", "--",
                    sys.executable, "-c",
                    (
                        "import subprocess; "
                        "subprocess.Popen(['sleep','0.45'])"
                    ),
                ],
                start_new_session=True,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(elapsed, 0.35)
            state = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(state["leases"], {})

    def test_procfs_denial_never_prunes_or_releases_an_owned_lease(self):
        lease = {
            "class": "dispatch",
            "pid": os.getpid(),
            "starttime": GOVERNOR.process_starttime(os.getpid()),
            "group_owned": True,
            "pgid": os.getpid(),
        }
        with mock.patch.object(
            GOVERNOR, "process_observation",
            return_value=("inaccessible", "", ""),
        ), mock.patch.object(
            GOVERNOR, "process_group_observation",
            return_value=GOVERNOR.ProcessGroupObservation(
                "unverifiable", reason="permission-denied"
            ),
        ):
            self.assertTrue(GOVERNOR.lease_is_active(lease))
            with tempfile.TemporaryDirectory() as temp_dir:
                Path(temp_dir, "state.json").write_text(json.dumps({
                    "schema_version": 2,
                    "claims": {},
                    "leases": {"a" * 32: lease},
                    "reservations": {},
                    "starts": [],
                }), encoding="utf-8")
                GOVERNOR.release(temp_dir, "a" * 32)
                state = json.loads(Path(temp_dir, "state.json").read_text())
                self.assertIn("a" * 32, state["leases"])

    def test_generic_reservation_never_gains_replica_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = GOVERNOR.reserve(temp_dir, "dispatch", 1)[0]
            receipt = GOVERNOR.reservation_check(temp_dir, token)
            self.assertTrue(
                all(key not in receipt for key in GOVERNOR.BATCH_RESERVATION_KEYS)
            )

    def test_v1_state_and_original_acquire_check_release_contract_migrate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "state.json").write_text(
                json.dumps({"schema_version": 1, "leases": {}, "starts": []})
            )
            # Inherit this suite's group: the new PID is a non-leader even
            # when tools/run-tests.py launched the suite with setsid().
            actor = subprocess.run(
                [
                    sys.executable, "-c",
                    "import json,runpy,sys; sys.path.insert(0,sys.argv[1]); "
                    "module=runpy.run_path(sys.argv[2]); "
                    "print(json.dumps(module['_legacy_migration_return'](sys.argv[3])))",
                    str(PATH.parent), str(Path(__file__).resolve()), temp_dir,
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(actor.returncode, 0, actor.stderr)
            observed = json.loads(actor.stdout)
            self.assertNotEqual(observed["pid"], observed["pgid"])
            self.assertEqual(observed["pgid"], os.getpgrp())
            self.assertIsNone(observed["checked"])
            self.assertEqual(observed["after_check"]["schema_version"], 2)
            self.assertEqual(observed["after_check"]["leases"], {})
            self.assertEqual(observed["after_check"]["starts"], [])
            self.assertRegex(observed["token"], r"^[0-9a-f]{32}$")
            self.assertEqual(observed["acquired"]["pid"], observed["pid"])
            self.assertNotIn("group_owned", observed["acquired"])
            self.assertEqual(observed["returned"], {
                "status": "released", "release_proven": True, "occupied": False,
            })
            self.assertNotIn(observed["token"], observed["after_return"]["leases"])
            self.assertEqual(observed["after_return"]["leases"], {})
            self.assertEqual(len(observed["after_return"]["starts"]), 1)
            legacy_run = subprocess.run(
                [
                    sys.executable,
                    str(PATH),
                    "--root",
                    temp_dir,
                    "run",
                    "--class",
                    "dispatch",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(legacy_run.returncode, 0, legacy_run.stderr)
            state = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["claims"], {})
            self.assertEqual(state["leases"], {})
            self.assertEqual(state["reservations"], {})
            self.assertEqual(len(state["starts"]), 2)

    def test_stale_owner_reservation_remains_occupied_without_authorized_return(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            owner = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
            )
            try:
                stale = GOVERNOR.reserve(
                    temp_dir, "dispatch", 2, owner.pid, total=3, budget=3
                )
            finally:
                owner.communicate(input=b"", timeout=5)

            for token in stale:
                self.assertEqual(GOVERNOR.reservation_check(temp_dir, token)["state"], "unclaimed")
            fresh = GOVERNOR.reserve(temp_dir, "dispatch", 1, total=3, budget=3)
            state = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(set(stale) | set(fresh), set(state["reservations"]))

    def test_run_claims_reservation_and_strips_bearer_from_model_child(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = GOVERNOR.reserve(temp_dir, "dispatch", 1)[0]
            env = dict(os.environ)
            env[GOVERNOR.RESERVATION_ENV] = token
            result = subprocess.run(
                [
                    sys.executable,
                    str(PATH),
                    "--root",
                    temp_dir,
                    "run",
                    "--class",
                    "dispatch",
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os,sys; "
                        f"sys.exit(int({GOVERNOR.RESERVATION_ENV!r} in os.environ))"
                    ),
                ],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(Path(temp_dir, "state.json").read_text())
            self.assertEqual(state["reservations"], {})
            self.assertEqual(state["leases"], {})
            self.assertEqual(len(state["starts"]), 1)
            receipt = GOVERNOR.reservation_check(temp_dir, token)
            self.assertEqual(receipt["state"], "claimed")
            self.assertFalse(receipt["lease_active"])

    def test_claim_acknowledgement_survives_owner_exit_while_runner_is_live(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            owner = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
            )
            token = GOVERNOR.reserve(temp_dir, "dispatch", 1, owner.pid)[0]
            env = dict(os.environ)
            env[GOVERNOR.RESERVATION_ENV] = token
            runner = subprocess.Popen(
                [
                    sys.executable,
                    str(PATH),
                    "--root",
                    temp_dir,
                    "run",
                    "--class",
                    "dispatch",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.read()",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                deadline = time.monotonic() + 5
                while True:
                    receipt = GOVERNOR.reservation_check(temp_dir, token)
                    if receipt["state"] == "claimed":
                        break
                    self.assertEqual(receipt["state"], "unclaimed")
                    if time.monotonic() >= deadline:
                        self.fail("governor runner did not claim reservation")
                    time.sleep(0.01)

                self.assertTrue(receipt["lease_active"])
                self.assertEqual(receipt["claimant_pid"], runner.pid)
                self.assertEqual(
                    receipt["claimant_starttime"],
                    GOVERNOR.process_starttime(runner.pid),
                )
                owner.communicate(input=b"", timeout=5)
                after_owner_exit = GOVERNOR.reservation_check(temp_dir, token)
                self.assertEqual(after_owner_exit["state"], "claimed")
                self.assertTrue(after_owner_exit["lease_active"])
            finally:
                _, runner_stderr = runner.communicate(input=b"", timeout=5)
                self.assertEqual(runner.returncode, 0, runner_stderr.decode())
                if owner.poll() is None:
                    owner.terminate()
                    owner.wait(timeout=5)

            self.assertEqual(GOVERNOR.reservation_check(temp_dir, token)["state"], "claimed")

    def test_reservation_cli_is_bounded_json_and_checks_exact_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [
                    sys.executable,
                    str(PATH),
                    "--root",
                    temp_dir,
                    "reserve",
                    "--class",
                    "dispatch",
                    "--count",
                    "2",
                    "--pid",
                    str(os.getpid()),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(
                set(receipt), {"class", "count", "owner_pid", "tokens"}
            )
            self.assertEqual(receipt["count"], 2)
            checked = subprocess.run(
                [
                    sys.executable,
                    str(PATH),
                    "--root",
                    temp_dir,
                    "reservation-check",
                    "--class",
                    "dispatch",
                    "--pid",
                    str(os.getpid()),
                    "--token",
                    receipt["tokens"][0],
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout)["state"], "unclaimed")
            absent = subprocess.run(
                [
                    sys.executable,
                    str(PATH),
                    "--root",
                    temp_dir,
                    "reservation-check",
                    "--token",
                    "0" * 32,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(absent.returncode, 75)
            self.assertEqual(json.loads(absent.stdout)["state"], "absent")

    def test_artifact_root_is_the_worker_writable_default(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"AGENT_ARTIFACT_ROOT": temp_dir},
            clear=False,
        ):
            os.environ.pop("AGENT_MODEL_GOVERNOR_ROOT", None)
            self.assertEqual(
                GOVERNOR.default_root(),
                Path(temp_dir) / ".runtime" / "model-worker-governor",
            )


class GovernorIdentityRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self):
        # Fixture-owned abandoned handles only, never an operational governor.
        for key,h in list(GOVERNOR._LEASE_WITNESSES.items()):
            if key[0]==str(self.root):
                GOVERNOR.close_witness(h);GOVERNOR._LEASE_WITNESSES.pop(key,None)
        self.temp.cleanup()
    def state(self):
        p=self.root/"state.json";return json.loads(p.read_text()) if p.exists() else {}

    def test_owner_to_claimant_witness_transfer_and_proven_receipt_retention(self):
        if _run_as_non_group_actor(self):
            return
        h=GOVERNOR.create_witness(self.root,"reservation")
        try:
            t=GOVERNOR.reserve(self.root,"dispatch",1,witness_binding=h.binding())[0]
            GOVERNOR.claim_reservation(self.root,t,"dispatch")
            row=self.state()["claims"][t]
            self.assertEqual(row["owner_witness"],h.binding())
            self.assertEqual(GOVERNOR.observe_witness(self.root,row["claimant_witness"]).state,"live")
            returned = GOVERNOR.release(self.root,t)
            self.assertTrue(returned["release_proven"], returned)
            state=self.state();self.assertTrue(state["claims"][t]["release_proven"])
            self.assertEqual(state["leases"],{})
            future=state["claims"][t]["released_at"]+GOVERNOR.CLAIM_RECEIPT_SECONDS+1
            with mock.patch.object(GOVERNOR.time,"time",return_value=future):
                self.assertEqual(GOVERNOR.reservation_check(self.root,t)["state"],"claimed")
            GOVERNOR.close_witness(h)
            with mock.patch.object(GOVERNOR.time,"time",return_value=future):
                self.assertEqual(GOVERNOR.reservation_check(self.root,t)["state"],"absent")
        finally:GOVERNOR.close_witness(h)

    def test_claim_owner_conflict_and_capture_failure_commit_nothing(self):
        h=GOVERNOR.create_witness(self.root,"reservation")
        try:
            t=GOVERNOR.reserve(self.root,"dispatch",1,witness_binding=h.binding())[0]
            before=(self.root/"state.json").read_bytes();GOVERNOR.close_witness(h)
            with self.assertRaisesRegex(ValueError,"owner witness conflict"):
                GOVERNOR.claim_reservation(self.root,t,"dispatch")
            self.assertEqual(before,(self.root/"state.json").read_bytes())
            with mock.patch.object(GOVERNOR,"capture_local_identity",side_effect=GOVERNOR.IdentityCaptureError("capture-error: fixture")):
                with self.assertRaises(GOVERNOR.IdentityCaptureError):GOVERNOR.acquire(self.root,"dispatch")
            self.assertEqual(before,(self.root/"state.json").read_bytes())
        finally:GOVERNOR.close_witness(h)

    def test_invalid_new_owner_binding_refuses_before_admission(self):
        with self.assertRaisesRegex(ValueError,"owner witness conflict"):
            GOVERNOR.reserve(self.root,"dispatch",1,witness_binding={})
        self.assertFalse((self.root/"state.json").exists())

    def test_cancel_claim_race_has_one_winner_and_at_most_one_start(self):
        import threading
        for _ in range(8):
            t=GOVERNOR.reserve(self.root,"dispatch",1)[0]
            before=len(self.state()["starts"]);barrier=threading.Barrier(2);results=[]
            def act(name):
                barrier.wait()
                try:
                    result=GOVERNOR.cancel_reservation(self.root,t) if name=="cancel" else GOVERNOR.claim_reservation(self.root,t,"dispatch")
                    results.append((name,result))
                except ValueError:results.append((name,None))
            threads=[threading.Thread(target=act,args=(x,)) for x in ("cancel","claim")]
            for x in threads:x.start()
            for x in threads:x.join(5);self.assertFalse(x.is_alive())
            self.assertEqual(sum(v is not None for _,v in results),1)
            st=self.state();won=t in st["leases"]
            self.assertEqual(len(st["starts"])-before,int(won))
            if won:
                with self.assertRaisesRegex(ValueError,"already claimed"):GOVERNOR.claim_reservation(self.root,t,"dispatch")
                self.assertEqual(len(self.state()["starts"]),len(st["starts"]))
                self.assertTrue(GOVERNOR.release(self.root,t)["release_proven"])

    def test_lost_fd_and_unproven_timestamp_never_return_capacity(self):
        t=GOVERNOR.reserve(self.root,"dispatch",1)[0];GOVERNOR.claim_reservation(self.root,t,"dispatch")
        h=GOVERNOR._LEASE_WITNESSES[GOVERNOR._witness_key(self.root,t)];os.close(h.fd);h._closed=True
        result=GOVERNOR.release(self.root,t)
        self.assertEqual(result["status"],"blocked");self.assertTrue(result["occupied"])
        state=self.state();self.assertIsNone(state["claims"][t]["released_at"])
        # Synthetic legacy timestamp fixture; this is not a live incident.
        state["claims"][t]["released_at"]=1;state["claims"][t].pop("release_proven",None)
        (self.root/"state.json").write_text(json.dumps(state))
        with mock.patch.object(GOVERNOR.time,"time",return_value=time.time()+100000):
            self.assertTrue(GOVERNOR.reservation_check(self.root,t)["lease_active"])
        self.assertIn(t,self.state()["claims"])

    def test_legacy_schema_return_and_missing_authority_limits(self):
        for schema in (1,2):
            token="a"*32;other="b"*32
            state={"schema_version":schema,"leases":{token:{"class":"dispatch","pid":99999999,"starttime":"1"},other:{"class":"dispatch","pid":99999999,"starttime":"1","pgid":99999999,"group_owned":True}},"starts":[],"claims":{},"reservations":{}}
            (self.root/"state.json").write_text(json.dumps(state))
            self.assertTrue(GOVERNOR.release(self.root,token)["release_proven"])
            result=GOVERNOR.release(self.root,other)
            self.assertEqual(result["reason"],"legacy-identity-unverifiable")
            self.assertTrue(result["occupied"])
            self.assertIn(other,self.state()["leases"])

    def test_incomplete_own_group_blocks_without_release_receipt(self):
        t=GOVERNOR.reserve(self.root,"dispatch",1)[0]
        with mock.patch.object(GOVERNOR,"_owned_group_metadata",return_value={"group_owned":True,"pgid":os.getpid()}):
            GOVERNOR.claim_reservation(self.root,t,"dispatch")
        with mock.patch.object(GOVERNOR.os,"getpgrp",return_value=os.getpid()), mock.patch.object(GOVERNOR,"capture_local_identity",return_value=self.state()["leases"][t]["claimant_identity"]), mock.patch.object(GOVERNOR,"process_group_observation",return_value=GOVERNOR.ProcessGroupObservation("unverifiable",reason="fixture-enumeration-race")):
            result=GOVERNOR.release(self.root,t)
        self.assertEqual(result["reason"],"group-observation-incomplete")
        self.assertIsNone(self.state()["claims"][t]["released_at"])
        self.assertNotIn("release_proven",self.state()["claims"][t])


class TopLegWidthCapTest(unittest.TestCase):
    """Part C: at most one `top`-profile leg per parallel group.

    Two structural reasons, and the test pins both ends of them. Unbounded
    top-tier fan-out is a cost and exposure risk; and if every leg in a group ran
    `top` there would be no profile difference left, so the `model-profile`
    independence axis could not be derived as realized at all.
    """

    def member(self, index, *, profile, harness, node):
        return {
            "assignment_sha256": "sha256:" + "a" * 64,
            "attempt_id": f"att-frame-{index}",
            "route_node": node,
            "harness": harness,
            "fallback_hop": "same-harness-headless" if index == 0 else "cross-harness-headless",
            "fallback_ordinal": index + 1,
            "model_profile": profile,
            "perspective": f"framing-{index}",
            "parallel_leg_index": index,
            "leg_class": "peer",
        }

    def build(self, profiles, realized):
        return build_manifest(
            parallel_group="frame",
            route_id="rt-top-cap",
            parent_attempt_id="att-parent-frame",
            independence="cross-harness",
            members=[
                self.member(index, profile=profile,
                            harness=("codex", "claude")[index],
                            node=("frame", "frame-alternative")[index])
                for index, profile in enumerate(profiles)
            ],
            required_independence_axes=["cross-harness", "model-profile", "perspective"],
            realized_independence_axes=realized,
        )

    def test_top_is_a_supported_profile_at_all(self):
        self.assertIn("top", CONTRACT.SUPPORTED_PROFILES)
        self.assertEqual(CONTRACT.MAX_TOP_LEGS, 1)

    def test_exactly_one_top_leg_seals_and_two_are_refused(self):
        manifest, digest, legs = self.build(
            ["top", "light"], ["cross-harness", "model-profile", "perspective"])
        self.assertEqual([member["model_profile"] for member in manifest["members"]],
                         ["top", "light"])
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(len(legs), 2)
        # ...and the sealed manifest round-trips through the verifier, which
        # rebuilds it through the same cap
        self.assertEqual(CONTRACT.verify_manifest(manifest)[1], digest)
        with self.assertRaises(CONTRACT.ReplicaBatchContractError) as refused:
            # two `top` legs leave no profile difference, so `model-profile` is
            # honestly absent from the realized axes -- the cap is what refuses
            # this, not an axis-accounting error
            self.build(["top", "top"], ["cross-harness", "perspective"])
        self.assertEqual(str(refused.exception), "top-leg-width-exceeded")

    def test_a_group_with_no_top_leg_is_untouched(self):
        manifest, _digest, _legs = self.build(
            ["balanced-deep", "light"], ["cross-harness", "model-profile", "perspective"])
        self.assertNotIn("top", {member["model_profile"] for member in manifest["members"]})


class GovernorReclaimTest(unittest.TestCase):
    """A dead claimant's lease must be returnable, and only by kernel proof.

    2026-09-10: `release` needs the witness handle issued in the calling
    process, so a claimant that died without releasing held its slot forever.
    Twelve such deaths lock the governor; ten of twelve were measured, and an
    approved route was refused twice.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        for key, handle in list(GOVERNOR._LEASE_WITNESSES.items()):
            if key[0] == str(self.root):
                GOVERNOR.close_witness(handle)
                GOVERNOR._LEASE_WITNESSES.pop(key, None)
        self.temp.cleanup()

    def state(self):
        path = self.root / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _dead_claimant_lease(self, worker_class="dispatch"):
        """One lease whose claimant process really exited without releasing."""
        script = (
            "import importlib.util, json, sys\n"
            # the governor imports its siblings by bare name
            f"sys.path.insert(0, {str(PATH.parent)!r})\n"
            f"spec = importlib.util.spec_from_file_location('g', {str(PATH)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "sys.modules['g'] = m\n"
            "spec.loader.exec_module(m)\n"
            f"h = m.create_witness({str(self.root)!r}, 'reservation')\n"
            f"t = m.reserve({str(self.root)!r}, {worker_class!r}, 1, witness_binding=h.binding())[0]\n"
            f"m.claim_reservation({str(self.root)!r}, t, {worker_class!r})\n"
            "print(t)\n"
        )
        source = self.root / f"claimant-{worker_class}.py"
        source.write_text(script, encoding="utf-8")
        # `start_new_session=True` matters, not hygiene: the runner executes
        # suites in parallel and `_local_return_proof` asks whether the
        # caller's process group has drained. A child left in the shared group
        # makes a *sibling* suite's release refuse `group-descendants-live` --
        # a correct refusal about the wrong process (CI 2026-09-10 lost
        # `test_owner_to_claimant_witness_transfer...` this way while the same
        # run passed here). Its own session keeps this fixture's children out
        # of every other suite's observation.
        done = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=120, start_new_session=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.strip()

    def test_a_dead_claimants_lease_is_reclaimed_and_a_live_one_is_not(self):
        dead = self._dead_claimant_lease()
        live_handle = GOVERNOR.create_witness(self.root, "reservation")
        live = GOVERNOR.reserve(self.root, "dispatch", 1, witness_binding=live_handle.binding())[0]
        GOVERNOR.claim_reservation(self.root, live, "dispatch")
        self.assertEqual(set(self.state()["leases"]), {dead, live})

        # `release` cannot help the dead one: the proof it needs died with it
        blocked = GOVERNOR.release(self.root, dead)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["occupied"])

        result = GOVERNOR.reclaim(self.root)
        self.assertEqual(result["reclaimed_count"], 1, result)
        self.assertEqual(result["reclaimed"][0]["token"], dead)
        self.assertEqual(result["reclaimed"][0]["reason"], "exclusive-lock-acquired")
        self.assertEqual([row["token"] for row in result["retained"]], [live])
        self.assertEqual(result["retained"][0]["reason"], "exact-witness-lock-held")
        self.assertEqual(list(self.state()["leases"]), [live])

        # the receipt says it was reclaimed, not handed back
        claim = self.state()["claims"][dead]
        self.assertFalse(claim["release_proven"])
        self.assertEqual(claim["reclaimed_by"], "witness-proof")
        self.assertIsNotNone(claim["released_at"])

        # The live lease is untouched and still refuses reclamation. Its
        # *release* is deliberately not asserted here: a group-owned lease can
        # only be returned by a lone group leader with a drained group, which
        # is a property of whatever process runs this suite, not of reclaim
        # (the file's own `_legacy_migration_return` documents the same
        # sensitivity). `GovernorIdentityRegressionTest` owns that assertion.
        self.assertEqual(GOVERNOR.reclaim(self.root, token=live)["reclaimed_count"], 0)
        self.assertEqual(list(self.state()["leases"]), [live])

    def test_a_witness_that_proves_nothing_keeps_its_lease(self):
        dead = self._dead_claimant_lease()
        witness = self.state()["leases"][dead]["claimant_witness"]
        path = self.root / witness["relative_path"]

        # replaced file: same path, different inode -> not the recorded witness
        path.unlink()
        path.write_text("not the sealed payload", encoding="utf-8")
        result = GOVERNOR.reclaim(self.root)
        self.assertEqual(result["reclaimed_count"], 0, result)
        self.assertIn(result["retained"][0]["reason"],
                      {"witness-replaced", "witness-binding-mismatch", "witness-payload-mismatch"})
        self.assertEqual(list(self.state()["leases"]), [dead])

        # and a lease with no witness at all is never guessed at
        GOVERNOR._state_change(self.root, lambda data, now: data["leases"][dead].pop("claimant_witness"))
        result = GOVERNOR.reclaim(self.root)
        self.assertEqual(result["reclaimed_count"], 0, result)
        self.assertEqual(result["retained"][0]["reason"], "no-identity-witness")
        self.assertEqual(list(self.state()["leases"]), [dead])

    def test_a_full_governor_names_the_recovery_path_instead_of_dead_ending(self):
        dead = self._dead_claimant_lease()
        with self.assertRaises(ValueError) as refused:
            GOVERNOR.check(self.root, "dispatch", total=1, budget=8)
        message = str(refused.exception)
        self.assertIn("global model-worker cap reached", message)
        self.assertIn("reclaim", message)
        self.assertIn("status", message)
        # The hint names the path without probing for it: computing the count
        # here meant opening every witness and taking a lock inside the state
        # lock, on the refusal path, which perturbed this very suite. The count
        # belongs to `status`; the refusal only points at it.
        self.assertNotIn("lease(s) are held", message)
        self.assertEqual(list(self.state()["leases"]), [dead])
        # and `status`, an explicit call, is where the number comes from
        self.assertEqual(GOVERNOR.reclaimable(self.root, self.state()), 1)

    def test_one_token_can_be_reclaimed_without_touching_its_peers(self):
        first = self._dead_claimant_lease()
        second = self._dead_claimant_lease("title")
        result = GOVERNOR.reclaim(self.root, token=first)
        self.assertEqual([row["token"] for row in result["reclaimed"]], [first])
        self.assertEqual(result["retained"], [])
        self.assertEqual(list(self.state()["leases"]), [second])

    def test_full_acquire_reclaims_once_outside_admission_transaction(self):
        dead = self._dead_claimant_lease()
        original = GOVERNOR._state_change
        in_transaction = False

        def tracked(root, operation):
            nonlocal in_transaction
            self.assertFalse(in_transaction, "nested governor transaction")
            in_transaction = True
            try:
                return original(root, operation)
            finally:
                in_transaction = False

        with mock.patch.object(GOVERNOR, "_state_change", side_effect=tracked), \
             mock.patch.object(GOVERNOR, "reclaim", wraps=GOVERNOR.reclaim) as reclaim:
            fresh = GOVERNOR.acquire(self.root, "dispatch", total=1, budget=8)
        reclaim.assert_called_once_with(self.root)
        self.assertEqual(list(self.state()["leases"]), [fresh])
        self.assertEqual(self.state()["claims"][dead]["reclaimed_by"], "witness-proof")

    def test_full_reserve_reclaims_before_atomic_batch_retry(self):
        dead = self._dead_claimant_lease()
        with mock.patch.object(GOVERNOR, "reclaim", wraps=GOVERNOR.reclaim) as reclaim:
            tokens = GOVERNOR.reserve(self.root, "dispatch", 2, total=2, budget=8)
        reclaim.assert_called_once_with(self.root)
        self.assertEqual(set(self.state()["reservations"]), set(tokens))
        self.assertNotIn(dead, self.state()["leases"])

    def test_live_full_leases_still_refuse_both_admission_paths(self):
        live = GOVERNOR.acquire(self.root, "dispatch", total=1, budget=8)
        for admit in (lambda: GOVERNOR.acquire(self.root, "dispatch", total=1, budget=8),
                      lambda: GOVERNOR.reserve(self.root, "dispatch", 1, total=1, budget=8)):
            with mock.patch.object(GOVERNOR, "reclaim", wraps=GOVERNOR.reclaim) as reclaim:
                with self.assertRaisesRegex(ValueError, "global model-worker cap"):
                    admit()
            reclaim.assert_called_once_with(self.root)
            self.assertEqual(list(self.state()["leases"]), [live])

    def test_no_witness_scan_for_room_budget_validation_or_check(self):
        with mock.patch.object(GOVERNOR, "reclaim", side_effect=AssertionError("unexpected reclaim")), \
             mock.patch.object(GOVERNOR, "reclaimable", side_effect=AssertionError("diagnostic scan")):
            GOVERNOR.acquire(self.root, "dispatch", total=3, budget=8)
            GOVERNOR.reserve(self.root, "dispatch", 1, total=3, budget=8)
            with self.assertRaisesRegex(ValueError, "rolling"):
                GOVERNOR.acquire(self.root, "dispatch", total=3, budget=2)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                GOVERNOR.reserve(self.root, "dispatch", 0)
            with self.assertRaisesRegex(ValueError, "cap"):
                GOVERNOR.check(self.root, "dispatch", total=1)
            with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKERS_DISABLED": "1"}):
                with self.assertRaisesRegex(ValueError, "kill switch"):
                    GOVERNOR.acquire(self.root, "dispatch")

    def test_class_cap_triggers_reclaim_but_start_budget_is_not_refunded(self):
        self._dead_claimant_lease("title")
        with mock.patch.object(GOVERNOR, "class_limit", return_value=1), \
             mock.patch.object(GOVERNOR, "reclaim", wraps=GOVERNOR.reclaim) as reclaim:
            with self.assertRaisesRegex(ValueError, "rolling"):
                GOVERNOR.acquire(self.root, "title", total=8, budget=1)
        reclaim.assert_called_once_with(self.root)
        self.assertEqual(self.state()["leases"], {})
        self.assertEqual(len(self.state()["starts"]), 1)

    def test_reclaimed_capacity_lost_to_contender_does_not_loop(self):
        self._dead_claimant_lease()
        original = GOVERNOR.reclaim

        def contested(root):
            result = original(root)
            GOVERNOR.acquire(root, "title", total=1, budget=8)
            return result

        with mock.patch.object(GOVERNOR, "reclaim", side_effect=contested) as reclaim:
            with self.assertRaisesRegex(ValueError, "global model-worker cap"):
                GOVERNOR.reserve(self.root, "dispatch", 1, total=1, budget=8)
        reclaim.assert_called_once_with(self.root)
        self.assertEqual(self.state()["reservations"], {})


if __name__ == "__main__":
    unittest.main()
