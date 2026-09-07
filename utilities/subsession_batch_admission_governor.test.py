#!/usr/bin/env python3
"""SD-119 M-5: the sub-session full-N reservation, proven against a REAL governor.

Every earlier test of this surface injected a fake `reserve` callable, so the two
defects that made a live 2-slice execute impossible -- the batch issuer fence and
the route-leg manifest shape -- both passed green for a whole cycle. These tests
therefore refuse to mock `reserve`, the issuer, the manifest contract or the
governor: they spawn the real `dispatch-batch.py`, which spawns the real
`model-worker-governor.py`, and then read the governor's own state file. Only the
model worker itself is ever replaced by a fixture, and only where a slice would
actually start.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import subsession_batch_contract as CONTRACT  # noqa: E402
import subdivision_batch_admission as SUBDIV  # noqa: E402
import dispatch_contract as DISPATCH  # noqa: E402

_BATCH_SPEC = importlib.util.spec_from_file_location(
    "dispatch_batch_for_subsession_test", ROOT / "utilities" / "dispatch-batch.py"
)
BATCH = importlib.util.module_from_spec(_BATCH_SPEC)
_BATCH_SPEC.loader.exec_module(BATCH)

_ROUTE_SPEC = importlib.util.spec_from_file_location(
    "capability_route_for_subsession_test", ROOT / "utilities" / "capability-route.py"
)
ROUTE = importlib.util.module_from_spec(_ROUTE_SPEC)
_ROUTE_SPEC.loader.exec_module(ROUTE)

GOVERNOR = ROOT / "utilities" / "model-worker-governor.py"
DISPATCH_BATCH = ROOT / "utilities" / "dispatch-batch.py"
PARENT_ATTEMPT = "att-fixture-parent-subsession-batch"
OWNER_SLUG = "fixture-subsession-owner"


class SubsessionGovernorFixture(unittest.TestCase):
    """One real route + real 2-slice chain manifest bound to a temp governor root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="subsession-governor-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.artifact_root = self.tmp / "artifacts"
        self.artifact_root.mkdir(parents=True)
        self.jobs = self.tmp / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.governor_root = self.artifact_root / ".runtime" / "model-worker-governor"
        self.route_path = self._compose_route()
        self.route = json.loads(self.route_path.read_text(encoding="utf-8"))
        # The slices name real files in the route's sealed worktree because the
        # fixed-file fence proves existence, containment and write-scope before
        # any reservation. A dry-run admission never writes to them.
        self.slice_files = ["utilities/subsession_batch_contract.py", "utilities/replica_batch_contract.py"]
        self.manifest_path = self._chain_manifest()

    # -- fixture construction -------------------------------------------------

    def _compose_route(self) -> Path:
        """Seal the fixture route through the production compose path.

        Hand-building a route JSON would drift from whatever the registry recipe
        currently declares and would quietly stop proving anything. `compose`
        with pre-supplied `--dispatch-evidence` skips only the live harness
        readiness probe (which must not run inside a dispatched worker); every
        validate-and-seal step the real route goes through still runs.
        """

        evidence = self.tmp / "dispatch-evidence.json"
        evidence.write_text(json.dumps({
            "tuples": [{
                "child_harness": "claude", "failure_class": "",
                "launch_authority": "conductor", "parent_harness": "claude",
                "parent_sandbox": "adapter-default", "parent_transport": "headless",
                "probe_source": "fixture", "probe_time": "2026-09-07T00:00:00Z",
                "status": "supported", "checked_worktree": str(ROOT),
                "codex_command": "not-applicable", "failure_scope": "none",
                "retry_on_isolated_worktree": 0,
            }],
            "native_subagent": [],
        }), encoding="utf-8")
        subprocess.run(
            [
                sys.executable, str(ROOT / "utilities" / "capability-route.py"), "compose",
                "--slug", "subsession-governor-fixture", "--shape", "staged",
                "--graph", "execute", "--capability", "autopilot-code",
                "--capability-mode", "dev", "--intensity", "standard",
                "--cwd", str(ROOT), "--artifact-root", str(self.artifact_root),
                "--jobs", str(self.jobs), "--dispatch-evidence", str(evidence),
                "--spec-read", "skip",
                "--drift-verdict", "no-spec-impact: subsession governor fixture",
            ],
            cwd=str(ROOT), text=True, capture_output=True, env=self._env(), check=False,
        )
        routes = sorted((self.artifact_root / ".runtime" / "routes").glob("rt-*.json"))
        self.assertEqual(len(routes), 1, "compose did not seal exactly one fixture route")
        return routes[0]

    def _chain_manifest(self, count: int = 2) -> Path:
        sessions = []
        for index in range(1, count + 1):
            brief = self.tmp / f"brief-{index}.md"
            brief.write_text(f"slice {index}\n", encoding="utf-8")
            sessions.append({
                "subsession_id": f"ss-fixture-slice-{index}",
                "attempt_id": f"att-fixture-slice-{index}-0000",
                "adapter": "claude", "slug": f"fixture-slice-{index}",
                "phase_brief": str(brief), "narrow_verify": "true",
                "expected_round_trips": 1,
                "fixed_files": [self.slice_files[index - 1]],
            })
        manifest = {
            "schema_version": 1, "kind": "stage-session-chain",
            "chain_id": "ssc-fixture-execute-0001", "mode": "parallel",
            "route_file": str(self.route_path), "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "route_node": "execute",
            "completion_gate": "code-execute", "worktree": str(ROOT),
            "sessions": sessions,
        }
        path = self.tmp / "chain.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _env(self) -> dict:
        env = os.environ.copy()
        # Explicitly PIN every root this run may touch. Unsetting them would not
        # isolate the test -- it would select the production default.
        env.update({
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.governor_root),
            "AGENT_DISPATCH_SELF_SLUG": OWNER_SLUG,
            "AGENT_DISPATCH_ATTEMPT_ID": PARENT_ATTEMPT,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

    def _run_dispatch_batch(self, action: str = "dry-run") -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, str(DISPATCH_BATCH),
                "--route", str(self.route_path), "--parallel-group", "execute",
                "--subdivision-manifest", str(self.manifest_path),
                "--action", action, "--slug-prefix", "execute",
                "--parent", OWNER_SLUG, "--jobs", str(self.jobs),
            ],
            cwd=str(ROOT), text=True, capture_output=True, env=self._env(), check=False,
        )

    def _governor_state(self) -> dict:
        state = self.governor_root / "state.json"
        return json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}

    def _batch_manifest(self) -> dict:
        sessions = json.loads(self.manifest_path.read_text(encoding="utf-8"))["sessions"]
        for index, session in enumerate(sessions, 1):
            session["index"] = index
        manifest, _digest = SUBDIV.build_subsession_batch_manifest(
            sessions, route=self.route, node_id="execute",
            chain_id="ssc-fixture-execute-0001",
            chain_manifest_sha256="a" * 64, parent_attempt_id=PARENT_ATTEMPT,
        )
        return manifest


class FullNReservationThroughRealGovernorTest(SubsessionGovernorFixture):

    def test_full_n_reservation_succeeds_through_real_dispatch_batch_and_governor(self):
        """The whole point of the cycle: no mock anywhere on this path.

        `dispatch-batch.py` is the process the governor's issuer fence names, so
        delegating to it is what makes the capability obtainable at all; the
        typed sub-session manifest is what makes the reservation verifiable.
        Before this fix the same call died at exit 75 (issuer) or
        `invalid parallel batch manifest shape` (contract).
        """
        result = self._run_dispatch_batch("dry-run")
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["state"], "subdivision-batch-admitted", result.stderr)
        self.assertEqual(envelope["slice_count"], 2)
        self.assertEqual(envelope["chain_id"], "ssc-fixture-execute-0001")

        reservations = self._governor_state().get("reservations", {})
        self.assertEqual(len(reservations), 2, reservations)
        kinds = {row["reservation_kind"] for row in reservations.values()}
        self.assertEqual(kinds, {"subsession-batch"})
        self.assertEqual(
            {row["batch_attempt_id"] for row in reservations.values()},
            {"att-fixture-slice-1-0000", "att-fixture-slice-2-0000"},
        )
        for row in reservations.values():
            self.assertEqual(row["batch_chain_id"], "ssc-fixture-execute-0001")
            self.assertEqual(row["batch_group"], "ssc-fixture-execute-0001")
            self.assertEqual(row["batch_route_id"], self.route["route_id"])
            self.assertEqual(row["batch_parent_attempt_id"], PARENT_ATTEMPT)
            self.assertEqual(row["batch_declared_size"], 2)
            self.assertEqual(row["batch_admission_count"], 2)
            self.assertEqual(row["batch_route_node"], "execute")
            # Route-leg-only fields must not be invented for a slice.
            self.assertNotIn("batch_independence", row)
            self.assertNotIn("batch_parallel_leg_index", row)
        # One batch, one manifest identity; distinct per-slice leg digests.
        self.assertEqual(len({row["batch_manifest_sha256"] for row in reservations.values()}), 1)
        self.assertEqual(len({row["batch_leg_sha256"] for row in reservations.values()}), 2)
        self.assertEqual(len({row["batch_fixed_files_sha256"] for row in reservations.values()}), 2)

    def test_tokens_are_swept_once_the_reserving_process_exits(self):
        """This is WHY the whole admit->register->start run had to move.

        `_state_change` drops every reservation whose owner pid is no longer
        live, so reserving from a short-lived helper and starting the slices
        afterwards would lose the tokens before any slice could claim them.
        Reserving inside the same `dispatch-batch` process that then registers
        and starts is what keeps them alive; a dry-run has no such process, so
        its tokens are correctly reclaimed here.
        """
        self.assertEqual(json.loads(self._run_dispatch_batch("dry-run").stdout)["state"],
                         "subdivision-batch-admitted")
        token = sorted(self._governor_state()["reservations"])[0]
        checked = subprocess.run(
            [sys.executable, str(GOVERNOR), "--root", str(self.governor_root),
             "reservation-check", "--token", token, "--class", "dispatch"],
            text=True, capture_output=True, env=self._env(), check=False,
        )
        self.assertEqual(json.loads(checked.stdout)["state"], "absent")

    def test_subsession_fields_are_carried_by_the_reservation_key_contract(self):
        """`BATCH_RESERVATION_KEYS` gates what `reservation-check` echoes back and
        what `claim_reservation` copies onto the claim. A subsession field absent
        from that tuple is silently dropped, which is exactly how a claim-time
        binding check degrades into a no-op."""
        spec = importlib.util.spec_from_file_location(
            "model_worker_governor_for_subsession_test", GOVERNOR
        )
        governor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(governor)
        for key in ("batch_attempt_id", "batch_chain_id", "batch_subsession_id",
                    "batch_subsession_index", "batch_fixed_files_sha256",
                    "batch_chain_manifest_sha256", "batch_manifest_sha256",
                    "batch_leg_sha256", "reservation_kind", "batch_group"):
            self.assertIn(key, governor.BATCH_RESERVATION_KEYS)


class IssuerFenceStaysClosedTest(SubsessionGovernorFixture):

    def test_in_process_reserve_batch_is_still_refused_by_the_issuer_fence(self):
        """The fix is "call from the right process", NOT "widen the fence".

        This is the exact call `stage-session-chain.py` used to make in-process.
        It must still fail, or the delegation would be pointless ceremony.
        """
        with self.assertRaises(BATCH.BatchError) as caught:
            BATCH.reserve_batch(
                GOVERNOR, self.governor_root,
                [{"attempt_id": "att-fixture-slice-1-0000"},
                 {"attempt_id": "att-fixture-slice-2-0000"}],
                manifest=self._batch_manifest(),
                manifest_digest=CONTRACT.verify_manifest(self._batch_manifest())[1],
            )
        self.assertEqual(caught.exception.reason, "model-worker-governor-denied")
        self.assertIn("issuer is not dispatch-batch", caught.exception.detail)
        self.assertEqual(self._governor_state().get("reservations", {}), {})

    def test_structural_denial_is_recorded_as_scope_unproven_not_capacity(self):
        """Defect 3: the blanket catch reported both hard contract failures as
        load. A reader of the decision ledger would chase capacity forever."""
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.reserve_full_n(
                GOVERNOR, self.governor_root,
                [{"attempt_id": "att-fixture-slice-1-0000", "subsession_id": "ss-fixture-slice-1",
                  "index": 1, "adapter": "claude", "fixed_files": ["utilities/a.py"]},
                 {"attempt_id": "att-fixture-slice-2-0000", "subsession_id": "ss-fixture-slice-2",
                  "index": 2, "adapter": "claude", "fixed_files": ["utilities/b.py"]}],
                route=self.route, node_id="execute", manifest_digest="a" * 64,
                chain_id="ssc-fixture-execute-0001", parent_attempt_id=PARENT_ATTEMPT,
                reserve=BATCH.reserve_batch,
            )
        self.assertEqual(caught.exception.reason, "scope-unproven")
        self.assertIn("issuer is not dispatch-batch", caught.exception.detail)

    def test_only_a_real_capacity_shortfall_is_recorded_as_capacity(self):
        def shortfall(*args, **kwargs):
            raise BATCH.BatchError(
                "governor-atomic-admission-shortfall", "global model-worker cap reached"
            )

        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.reserve_full_n(
                GOVERNOR, self.governor_root,
                [{"attempt_id": "att-fixture-slice-1-0000", "subsession_id": "ss-fixture-slice-1",
                  "index": 1, "adapter": "claude", "fixed_files": ["utilities/a.py"]},
                 {"attempt_id": "att-fixture-slice-2-0000", "subsession_id": "ss-fixture-slice-2",
                  "index": 2, "adapter": "claude", "fixed_files": ["utilities/b.py"]}],
                route=self.route, node_id="execute", manifest_digest="a" * 64,
                chain_id="ssc-fixture-execute-0001", parent_attempt_id=PARENT_ATTEMPT,
                reserve=shortfall,
            )
        self.assertEqual(caught.exception.reason, "governor-capacity-insufficient")


class ParallelSliceReservationBindingTest(SubsessionGovernorFixture):
    """`dispatch-node` must refuse a parallel slice start it cannot bind.

    impl-review round 1 (blocking): the first version swallowed the governor's
    answer in a bare `except Exception: pass`, so a missing, unreadable or
    foreign reservation let the slice launch anyway -- the binding check was a
    no-op on exactly the paths it existed for. These cases drive the real
    `dispatch-node.py` against the real governor and stop before any wrapper.
    """

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "utilities"))
        from stage_session_contract import load_manifest  # noqa: PLC0415
        import subdivision_batch_admission as ADMISSION  # noqa: PLC0415

        manifest = load_manifest(self.manifest_path, route=self.route, node=self.route["nodes"][0])
        ADMISSION.persist_chain_manifest(self.jobs, manifest)
        self.session = manifest["sessions"][0]
        self.chain_id = manifest["chain_id"]

    def _start(self, token: str | None) -> subprocess.CompletedProcess:
        env = self._env()
        if token is None:
            env.pop("AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN", None)
        else:
            env["AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN"] = token
        session = self.session
        return subprocess.run(
            [
                sys.executable, str(ROOT / "utilities" / "dispatch-node.py"),
                "--route", str(self.route_path), "--node", "execute",
                "--adapter", session["adapter"], "--action", "start",
                "--slug", session["slug"], "--parent", OWNER_SLUG,
                "--jobs", str(self.jobs), "--prompt-text", "fixture",
                "--subsession-id", session["subsession_id"],
                "--subsession-index", str(session["index"]),
                "--subsession-count", str(session["count"]),
                "--subsession-mode", "parallel",
                "--session-chain-id", self.chain_id,
                "--phase-brief", session["phase_brief"],
                "--stage-authority", "0",
                "--narrow-verify", session["narrow_verify"],
                "--expected-round-trips", str(session["expected_round_trips"]),
                "--attempt-id", session["attempt_id"],
                *[flag for file in session["fixed_files"] for flag in ("--fixed-file", file)],
            ],
            cwd=str(ROOT), text=True, capture_output=True, env=env, check=False,
        )

    def _reason(self, result: subprocess.CompletedProcess) -> str:
        for line in result.stdout.splitlines():
            if line.startswith("reason="):
                return line.split("=", 1)[1]
        return f"(no reason; rc={result.returncode}) {result.stdout[-200:]}"

    def test_parallel_slice_without_a_batch_token_is_refused(self):
        result = self._start(None)
        self.assertEqual(self._reason(result), "subsession-reservation-required")
        self.assertIn("child_spawned=0", result.stdout)

    def test_parallel_slice_with_an_absent_token_is_refused(self):
        result = self._start("0" * 32)
        self.assertEqual(self._reason(result), "subsession-reservation-unverifiable")
        self.assertIn("child_spawned=0", result.stdout)

    def test_parallel_slice_with_a_malformed_token_is_refused(self):
        result = self._start("not-a-token")
        self.assertEqual(self._reason(result), "subsession-reservation-unverifiable")
        self.assertIn("child_spawned=0", result.stdout)

    def test_parallel_slice_with_an_ordinary_non_batch_token_is_refused(self):
        """A real, live, valid reservation that is simply not this batch's.

        Minted through the real governor and owned by this test process so it
        survives the reservation sweep for the length of the check.
        """
        minted = subprocess.run(
            [sys.executable, str(GOVERNOR), "--root", str(self.governor_root),
             "reserve", "--class", "dispatch", "--count", "1", "--pid", str(os.getpid())],
            text=True, capture_output=True, env=self._env(), check=False,
        )
        self.assertEqual(minted.returncode, 0, minted.stderr)
        token = json.loads(minted.stdout)["tokens"][0]
        result = self._start(token)
        self.assertEqual(self._reason(result), "subsession-reservation-binding-mismatch")
        self.assertIn("reservation_kind", result.stdout)
        self.assertIn("child_spawned=0", result.stdout)


class SubsessionManifestContractTest(unittest.TestCase):
    """The typed contract itself: forged shapes must never reach the governor."""

    def _members(self, count=2):
        return [{
            "attempt_id": f"att-slice-{i}", "subsession_id": f"ss-slice-{i}",
            "subsession_index": i, "route_node": "execute", "harness": "claude",
            "fixed_files_sha256": f"{i}" * 64, "stage_authority": 0,
        } for i in range(1, count + 1)]

    def _manifest(self, **over):
        manifest, digest, legs = CONTRACT.build_manifest(
            chain_id="ssc-x", route_id="rt-x", route_node="execute",
            parent_attempt_id="att-parent", chain_manifest_sha256="a" * 64,
            members=self._members(), **over,
        )
        return manifest, digest, legs

    def test_canonical_manifest_round_trips(self):
        manifest, digest, legs = self._manifest()
        self.assertEqual(CONTRACT.verify_manifest(manifest)[1], digest)
        self.assertEqual(sorted(legs), ["att-slice-1", "att-slice-2"])

    def test_member_digest_is_bound_to_the_whole_batch_identity(self):
        """A leg digest that names only its own row is replayable in any chain
        sharing a route node."""
        _m1, _d1, legs_a = self._manifest()
        members = self._members()
        other, _digest, legs_b = CONTRACT.build_manifest(
            chain_id="ssc-other", route_id="rt-x", route_node="execute",
            parent_attempt_id="att-parent", chain_manifest_sha256="a" * 64,
            members=members,
        )
        self.assertNotEqual(legs_a["att-slice-1"], legs_b["att-slice-1"])

    def test_forged_shapes_are_refused(self):
        manifest, _digest, _legs = self._manifest()
        forgeries = {
            "extra key": {**manifest, "independence": "cross-harness"},
            "missing key": {k: v for k, v in manifest.items() if k != "route_id"},
            "size mismatch": {**manifest, "declared_size": 3},
            "wrong kind": {**manifest, "kind": "parallel-batch"},
            "empty parent": {**manifest, "parent_attempt_id": ""},
            "bad chain prefix": {**manifest, "chain_id": "chain-x"},
            "bad chain digest": {**manifest, "chain_manifest_sha256": "zz"},
        }
        for name, forged in forgeries.items():
            with self.subTest(name), self.assertRaises(CONTRACT.SubsessionBatchContractError):
                CONTRACT.verify_manifest(forged)

    def test_member_level_forgeries_are_refused(self):
        base, _digest, _legs = self._manifest()

        def mutate(**over):
            copy = json.loads(json.dumps(base))
            copy["members"][0].update(over)
            return copy

        cases = {
            "stage authority claimed": mutate(stage_authority=1),
            "stage authority as bool": mutate(stage_authority=False),
            "duplicate index": mutate(subsession_index=2),
            "zero index": mutate(subsession_index=0),
            "foreign route node": mutate(route_node="test"),
            "unsupported harness": mutate(harness="gemini"),
            "bad fixed-file digest": mutate(fixed_files_sha256="0" * 63),
            "bad subsession prefix": mutate(subsession_id="slice-1"),
        }
        for name, forged in cases.items():
            with self.subTest(name), self.assertRaises(CONTRACT.SubsessionBatchContractError):
                CONTRACT.verify_manifest(forged)

    def test_width_is_bounded_two_to_four(self):
        for count in (1, 5):
            with self.subTest(count=count), self.assertRaises(CONTRACT.SubsessionBatchContractError):
                CONTRACT.build_manifest(
                    chain_id="ssc-x", route_id="rt-x", route_node="execute",
                    parent_attempt_id="att-parent", chain_manifest_sha256="a" * 64,
                    members=self._members(count),
                )


class ParallelPeerSliceIsNotAPriorAttemptTest(unittest.TestCase):
    """SD-79's ``_sibling_attempt_gate`` (utilities/dispatch_contract.py) refuses to
    launch over a previous attempt of the same node that still runs. This cycle
    added one exception: a still-live row is not a prior attempt when it is a
    declared parallel peer slice of the same sub-session batch (measured defect:
    slice 2 of a 2-way subdivision was refused ``prior-attempt-still-live`` by
    its own healthy sibling, slice 1). This test pins that exception and its
    control -- a live row that is *not* a declared parallel peer of the same
    chain must still be refused.
    """

    def _route(self, base: Path, route_id: str) -> Path:
        route = {
            "dispatch_contract_version": 3, "route_id": route_id,
            "nodes": [{"id": "execute", "depends_on": []}],
        }
        path = base / "route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return path

    def _row(self, status: str, metadata: dict) -> str:
        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
        return f"2026-09-07T00:00:00Z\t{status}\t/repo\t/wt\texecute\t{pipe}"

    def _own_claim_row(self, route_id: str, attempt_id: str, chain_id: str) -> str:
        # The newcomer's own freshly claimed row: no pid yet, but it does carry
        # the chain identity `_sibling_attempt_gate` reads to learn its own
        # `own_chain` before scanning for siblings.
        return self._row("open", {
            "route_id": route_id, "route_node": "execute",
            "attempt_id": attempt_id, "session_chain_id": chain_id,
            "subsession_mode": "parallel",
        })

    def test_live_parallel_peer_of_the_same_chain_does_not_block(self):
        route_id = "rt-peer-guard-same-chain"
        chain_id = "ssc-peer-guard-0001"
        newcomer = "att-peer-guard-newcomer"
        peer = "att-peer-guard-peer"
        identity = DISPATCH.process_launch_identity(os.getpid())
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            route_path = self._route(base, route_id)
            lines = [
                self._own_claim_row(route_id, newcomer, chain_id),
                self._row("open", {
                    "route_id": route_id, "route_node": "execute",
                    "attempt_id": peer, "session_chain_id": chain_id,
                    "subsession_mode": "parallel", **identity,
                }),
            ]
            # Must not raise: a live declared peer of the same parallel batch is
            # a concurrent member, not a predecessor attempt.
            DISPATCH.completion_marker_gate(
                str(route_path), "execute", "start", base, base / "jobs.log",
                registry_lines=lines, attempt_id=newcomer,
            )

    def test_live_row_with_a_different_chain_id_still_blocks(self):
        route_id = "rt-peer-guard-foreign-chain"
        chain_id = "ssc-peer-guard-0002"
        newcomer = "att-peer-guard-newcomer-2"
        foreign = "att-peer-guard-foreign-chain"
        identity = DISPATCH.process_launch_identity(os.getpid())
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            route_path = self._route(base, route_id)
            lines = [
                self._own_claim_row(route_id, newcomer, chain_id),
                self._row("open", {
                    "route_id": route_id, "route_node": "execute",
                    "attempt_id": foreign, "session_chain_id": "ssc-peer-guard-unrelated",
                    "subsession_mode": "parallel", **identity,
                }),
            ]
            with self.assertRaises(DISPATCH.DispatchContractError) as caught:
                DISPATCH.completion_marker_gate(
                    str(route_path), "execute", "start", base, base / "jobs.log",
                    registry_lines=lines, attempt_id=newcomer,
                )
            self.assertEqual(caught.exception.reason, "prior-attempt-still-live")
            self.assertIn(foreign, caught.exception.detail)

    def test_live_row_with_the_same_chain_but_not_parallel_mode_still_blocks(self):
        route_id = "rt-peer-guard-non-parallel"
        chain_id = "ssc-peer-guard-0003"
        newcomer = "att-peer-guard-newcomer-3"
        non_parallel = "att-peer-guard-non-parallel"
        identity = DISPATCH.process_launch_identity(os.getpid())
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            route_path = self._route(base, route_id)
            lines = [
                self._own_claim_row(route_id, newcomer, chain_id),
                self._row("open", {
                    "route_id": route_id, "route_node": "execute",
                    "attempt_id": non_parallel, "session_chain_id": chain_id,
                    "subsession_mode": "serial", **identity,
                }),
            ]
            with self.assertRaises(DISPATCH.DispatchContractError) as caught:
                DISPATCH.completion_marker_gate(
                    str(route_path), "execute", "start", base, base / "jobs.log",
                    registry_lines=lines, attempt_id=newcomer,
                )
            self.assertEqual(caught.exception.reason, "prior-attempt-still-live")
            self.assertIn(non_parallel, caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
