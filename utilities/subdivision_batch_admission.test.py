#!/usr/bin/env python3
"""SD-119 R4 acceptance A-1/A-2: route-leg-independent sub-session batch admission."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PATH = Path(__file__).with_name("subdivision_batch_admission.py")
SPEC = importlib.util.spec_from_file_location("subdivision_batch_admission", PATH)
SUBDIV = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SUBDIV
SPEC.loader.exec_module(SUBDIV)

BATCH_PATH = Path(__file__).with_name("dispatch-batch.py")
BATCH_SPEC = importlib.util.spec_from_file_location("dispatch_batch", BATCH_PATH)
BATCH = importlib.util.module_from_spec(BATCH_SPEC)
assert BATCH_SPEC.loader is not None
sys.modules[BATCH_SPEC.name] = BATCH
BATCH_SPEC.loader.exec_module(BATCH)


def _execute_node() -> dict:
    return {
        "id": "execute",
        "dispatch_depth": 2,
        "completion_gate": "code-execute",
        "write_scope": ["source/**"],
        "subdivision": {
            "min_intensity": "strong",
            "max_slices": 4,
            "disjointness": "exact-fixed-files",
        },
    }


class AdmissionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "source").mkdir(parents=True)
        self.route_path = self.base / "route.json"
        self.node = _execute_node()
        self.route = {
            "route_id": "rt-fixture",
            "route_hash": "sha256:fixture",
            "cwd": str(self.base),
            "effective_intensity": "strong",
            "nodes": [self.node],
        }
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")

    def _manifest(self, count: int = 2) -> Path:
        sessions = []
        for n in range(1, count + 1):
            brief = self.base / f"b{n}.md"
            brief.write_text("slice brief\n", encoding="utf-8")
            sessions.append({
                "subsession_id": f"ss-slice-{n}",
                "attempt_id": f"att-slice-{n}{'a' * 26}",
                "adapter": "codex",
                "slug": f"slice-{n}",
                "phase_brief": str(brief),
                "fixed_files": [str(self.base / "source" / f"{n}.py")],
                "narrow_verify": "true",
                "expected_round_trips": 2,
            })
        manifest_path = self.base / "chain.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "stage-session-chain",
            "chain_id": "ssc-fixture",
            "mode": "parallel",
            "worktree": str(self.base),
            "route_file": str(self.route_path),
            "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"],
            "route_node": "execute",
            "completion_gate": "code-execute",
            "sessions": sessions,
        }), encoding="utf-8")
        return manifest_path


class AdmissionGateTest(AdmissionFixture):
    """A-1: `execute` admits a manifest with zero `parallel_group` membership."""

    def test_execute_node_admits_two_slice_manifest_without_parallel_group(self):
        self.assertFalse(SUBDIV.has_route_leg_group(self.route, "execute"))
        recorded = []
        tokens = ["a" * 32, "b" * 32]

        def fake_reserve(governor, governor_root, pending, *, manifest, manifest_digest):
            self.assertEqual(len(pending), 2)
            self.assertEqual(manifest["batch_manifest_sha256"], manifest_digest)
            return tokens

        result = SUBDIV.admit_batch(
            route=self.route, node=self.node, manifest_path=self._manifest(2),
            governor=Path("governor"), governor_root=Path("governor-root"),
            reserve=fake_reserve,
            record_baseline=lambda route, node_id, manifest: recorded.append((node_id, manifest["_manifest_sha256"])),
        )
        self.assertEqual(result.tokens, tokens)
        self.assertEqual(len(result.sessions), 2)
        self.assertEqual(recorded, [("execute", result.manifest_digest)])

    def test_sd_open_53_default_baseline_follows_the_admission_jobs(self):
        """SD-OPEN-53 (v77 review c1): the default `record_baseline` inside
        `admit_batch` wrote to the inherited/default state root while the audit
        read the caller's `jobs` root."""
        import os
        from unittest import mock
        pinned = self.base / "pinned" / "jobs.log"
        inherited = self.base / "inherited" / "jobs.log"
        for path in (pinned, inherited):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(inherited)}):
            result = SUBDIV.admit_batch(
                route=self.route, node=self.node, manifest_path=self._manifest(2),
                governor=Path("governor"), governor_root=Path("governor-root"),
                reserve=lambda *a, **k: ["a" * 32, "b" * 32], jobs=pinned,
            )
            baseline = SUBDIV.ROUTE_MODULE.subdivision_baseline_path(
                self.route["route_id"], "execute", result.manifest_digest, jobs=pinned)
            self.assertTrue(baseline.is_file())
            self.assertEqual(baseline.parent.parent.parent, pinned.parent / "completion")
            self.assertFalse((inherited.parent / "completion").exists())
            self.assertIsNotNone(SUBDIV.ROUTE_MODULE.load_subdivision_baseline(
                self.route, "execute", result.manifest, jobs=pinned))

    def test_dispatch_batch_parallel_group_path_unused(self):
        # A-1: the whole point is that `parallel_nodes` (2..4-member cardinality)
        # is never reached for a node with zero route-leg membership. F-3
        # (impl-review round 1): the live entry point is fail-closed until R5
        # lands, so `admit_batch` itself is also never reached -- both are
        # asserted as `AssertionError`-raising side effects, not just unused
        # return values, and the receipt is the typed refusal.
        manifest_path = self._manifest(2)
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "parallel_nodes",
                side_effect=AssertionError("parallel_nodes must not be called for subdivision admission"),
            ))
            # SD-103 narrowing: a worktree-base manifest now REACHES admission on
            # the dispatch-batch path; the typed verdict admit_batch returns is
            # what the receipt carries. `parallel_nodes` (route-leg expansion)
            # must still never be consulted for a subdivision manifest.
            stack.enter_context(mock.patch.object(
                BATCH.SUBDIVISION_ADMISSION, "admit_batch",
                side_effect=SUBDIV.SubdivisionAdmissionError("disjointness-unproven", "fixture"),
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry", return_value=type("R", (), {"path": self.base / "jobs.log"})()
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_model_governor_root", return_value=self.base / "gov"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
            }))
            argv = [
                "--route", str(self.route_path), "--parallel-group", "execute",
                "--action", "dry-run", "--slug-prefix", "fixture", "--parent", "owner",
                "--jobs", str(self.base / "jobs.log"),
                "--subdivision-manifest", str(manifest_path),
            ]
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)
        self.assertEqual(rc, 0, output.getvalue())
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "subdivision-batch-refused")
        self.assertEqual(receipt["reason"], "disjointness-unproven")
        self.assertEqual(receipt["admitted_rows"], 0)
        self.assertEqual(receipt["admitted_models"], 0)

    def test_legacy_group_call_still_raises_parallel_group_cardinality(self):
        # Non-subdivision calls against a group with the wrong width are
        # untouched: `parallel_nodes` is called and raises exactly as before.
        route = dict(self.route)
        route["nodes"] = [self.node]  # "execute" is not in any parallel_group
        output = io.StringIO()
        argv = [
            "--route", str(self.route_path), "--parallel-group", "execute",
            "--action", "dry-run", "--slug-prefix", "fixture", "--parent", "owner",
            "--jobs", str(self.base / "jobs.log"),
        ]
        with mock.patch.object(BATCH, "load_route", return_value=route):
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)
        self.assertNotEqual(rc, 0)
        self.assertIn("parallel-group-cardinality", output.getvalue())


class GovernorReservationTest(AdmissionFixture):
    """A-2: full-N atomic reservation -- all or nothing, one shared identity."""

    def test_insufficient_governor_slots_yields_zero_rows_zero_models(self):
        recorded = []

        def failing_reserve(*_args, **_kwargs):
            raise SUBDIV.SubdivisionAdmissionError("governor-capacity-insufficient", "cap reached")

        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=self.route, node=self.node, manifest_path=self._manifest(2),
                governor=Path("governor"), governor_root=Path("governor-root"),
                reserve=failing_reserve,
                record_baseline=lambda *a, **k: recorded.append((a, k)),
            )
        self.assertEqual(caught.exception.reason, "governor-capacity-insufficient")
        # baseline (checkpoint 4) is never reached -- no row, no model.
        self.assertEqual(recorded, [])

    def test_sufficient_slots_share_one_reservation_identity(self):
        tokens = ["c" * 32, "d" * 32, "e" * 32]

        def fake_reserve(governor, governor_root, pending, *, manifest, manifest_digest):
            self.assertEqual(len(pending), 3)
            return tokens

        result = SUBDIV.admit_batch(
            route=self.route, node=self.node, manifest_path=self._manifest(3),
            governor=Path("governor"), governor_root=Path("governor-root"),
            reserve=fake_reserve,
            record_baseline=lambda *a, **k: None,
        )
        self.assertEqual(result.tokens, tokens)
        self.assertEqual(len(set(result.tokens)), 3)
        # every admitted slice is keyed to the same manifest identity.
        self.assertEqual(result.reservation_identity, result.manifest_digest)


class PermissionAndFenceTest(AdmissionFixture):
    def test_no_subdivision_permission_is_not_eligible(self):
        node = dict(self.node)
        del node["subdivision"]
        route = dict(self.route)
        route["nodes"] = [node]
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=route, node=node, manifest_path=self._manifest(2),
                governor=Path("g"), governor_root=Path("gr"),
                reserve=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reserve")),
            )
        self.assertEqual(caught.exception.reason, "subdivision-not-permitted")

    def test_intensity_below_min_is_not_eligible(self):
        route = dict(self.route)
        route["effective_intensity"] = "standard"
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=route, node=self.node, manifest_path=self._manifest(2),
                governor=Path("g"), governor_root=Path("gr"),
                reserve=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reserve")),
            )
        self.assertEqual(caught.exception.reason, "intensity-below-min")

    def test_overlapping_fixed_files_refused_before_reservation(self):
        manifest_path = self._manifest(2)
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["sessions"][1]["fixed_files"] = raw["sessions"][0]["fixed_files"]
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=self.route, node=self.node, manifest_path=manifest_path,
                governor=Path("g"), governor_root=Path("gr"),
                reserve=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reserve")),
            )
        self.assertEqual(caught.exception.reason, "disjointness-unproven")


CHAIN_PATH = Path(__file__).with_name("stage-session-chain.py")
CHAIN_SPEC = importlib.util.spec_from_file_location("stage_session_chain_for_admission_test", CHAIN_PATH)
CHAIN = importlib.util.module_from_spec(CHAIN_SPEC)
assert CHAIN_SPEC.loader is not None
sys.modules[CHAIN_SPEC.name] = CHAIN
CHAIN_SPEC.loader.exec_module(CHAIN)


class ParallelEntryFailClosedTest(unittest.TestCase):
    """F-3 (impl-review round 1): both LIVE parallel entry points refuse
    `scope-unproven` before `admit_batch()` is ever reached, and neither
    writes a registry row or spawns a model process, until R5 lands."""

    def _manifest(self, td, base=None):
        path = Path(td) / "chain.json"
        session = {"subsession_id": "ss-x1", "fixed_files": ["a.py"]}
        if base is not None:
            session["base"] = base
        path.write_text(json.dumps({"sessions": [session]}), encoding="utf-8")
        return path

    def test_stage_session_chain_parallel_branch_refuses_artifact_base_before_admit_batch(self):
        """SD-103 narrowing: the gate still refuses -- before admit_batch and
        before any row -- exactly the slice R5 is for (a non-worktree base)."""
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            manifest = self._manifest(td, base={"base": "artifact", "path": "plans/x"})
            args = type("Args", (), {"action": "register", "manifest": str(manifest), "parent": "owner"})()
            with mock.patch.object(
                CHAIN.SUBDIVISION_ADMISSION, "admit_batch",
                side_effect=AssertionError("admit_batch must not be reached for an artifact-base slice"),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = CHAIN._run_parallel_subdivision({}, {}, args, jobs=jobs)
            self.assertEqual(rc, 65)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["state"], "subdivision-batch-refused")
            self.assertEqual(receipt["reason"], "scope-unproven")
            self.assertEqual(receipt["admitted_rows"], 0)
            self.assertEqual(receipt["admitted_models"], 0)
            # No child row: the fixture's jobs registry stays exactly empty.
            self.assertEqual(jobs.read_text(encoding="utf-8"), "")

    def test_gate_refuses_only_non_worktree_base(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
                SUBDIV.raise_if_parallel_entry_fail_closed(self._manifest(td, base="artifact"))
            self.assertEqual(caught.exception.reason, "scope-unproven")
            self.assertIn("ss-x1", caught.exception.detail)
            # worktree-base, unspecified base, unreadable manifest and no manifest
            # all pass here; admit_batch types everything else.
            self.assertIsNone(SUBDIV.raise_if_parallel_entry_fail_closed(self._manifest(td, base="worktree")))
            self.assertIsNone(SUBDIV.raise_if_parallel_entry_fail_closed(self._manifest(td, base={"base": "worktree"})))
            self.assertIsNone(SUBDIV.raise_if_parallel_entry_fail_closed(self._manifest(td)))
            self.assertIsNone(SUBDIV.raise_if_parallel_entry_fail_closed(Path(td) / "missing.json"))
            self.assertIsNone(SUBDIV.raise_if_parallel_entry_fail_closed())

    def test_worktree_base_manifest_reaches_admit_batch(self):
        """The live parallel branch now reaches admission for worktree slices;
        admit_batch's own typed verdict is what the caller sees."""
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            manifest = self._manifest(td)
            args = type("Args", (), {"action": "register", "manifest": str(manifest), "parent": "owner"})()
            reached = []
            def _admit(**kwargs):
                reached.append(kwargs["manifest_path"])
                raise SUBDIV.SubdivisionAdmissionError("disjointness-unproven", "fixture")
            with mock.patch.object(CHAIN.SUBDIVISION_ADMISSION, "admit_batch", side_effect=_admit):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = CHAIN._run_parallel_subdivision({}, {}, args, jobs=jobs)
            self.assertEqual(reached, [str(manifest)])
            self.assertEqual(rc, 65)
            self.assertEqual(json.loads(output.getvalue())["reason"], "disjointness-unproven")

    def test_start_admitted_batch_persists_manifest_before_first_start(self):
        """F3 precondition: the sealed pointer exists before any slice start."""
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.touch()
            manifest = {"chain_id": "ssc-persist-1", "sessions": [
                {"subsession_id": "ss-p1", "attempt_id": "att-persist-0001", "index": 1},
                {"subsession_id": "ss-p2", "attempt_id": "att-persist-0002", "index": 2},
            ]}
            admission = SUBDIV.AdmissionResult(
                tokens=["t1", "t2"], manifest=manifest, manifest_digest="sha256:0",
                sessions=manifest["sessions"], node_id="execute", reservation_identity="r",
            )
            seen = []
            def _run(cmd, env):
                pointer = jobs.parent / "session_chains" / "ssc-persist-1.json"
                seen.append((cmd[cmd.index("--action") + 1] if "--action" in cmd else "?", pointer.is_file()))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(SUBDIV, "dispatch_command", side_effect=lambda m, s, action, parent, j: ["x", "--action", action]):
                results = SUBDIV.start_admitted_batch(
                    admission, parent="owner", jobs=jobs, governor_reservation_env="TOKEN", run=_run,
                )
            self.assertEqual([r["started"] for r in results], [1, 1])
            self.assertEqual(seen, [("register", False), ("register", False), ("start", True), ("start", True)])
            sealed = json.loads((jobs.parent / "session_chains" / "ssc-persist-1.json").read_text())
            self.assertEqual(sealed["chain_id"], "ssc-persist-1")


class StartAdmittedBatchPartialFailureTest(AdmissionFixture):
    """F-4 (impl-review round 1): a mid-batch registration failure starts
    ZERO slices, including ones that themselves registered cleanly."""

    def _admission(self, count: int) -> "SUBDIV.AdmissionResult":
        manifest_path = self._manifest(count)
        recorded: list = []
        tokens = [chr(ord("a") + i) * 32 for i in range(count)]
        return SUBDIV.admit_batch(
            route=self.route, node=self.node, manifest_path=manifest_path,
            governor=Path("g"), governor_root=Path("gr"),
            reserve=lambda *a, **k: tokens,
            record_baseline=lambda *a, **k: recorded.append(a),
        )

    def test_third_slice_register_failure_starts_no_slice_at_all(self):
        admission = self._admission(3)
        calls: list[list[str]] = []
        cancelled: list[str] = []

        def fake_run(cmd, env):
            calls.append(cmd)
            action = cmd[cmd.index("--action") + 1]
            slug = cmd[cmd.index("--slug") + 1]
            if action == "register" and slug == "slice-3":
                return subprocess.CompletedProcess(cmd, 65, "", "register-failed")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        def fake_cancel(jobs, attempt_id):
            cancelled.append(attempt_id)
            return 1

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, cancel_row=fake_cancel,
        )
        self.assertEqual([row["started"] for row in results], [0, 0, 0])
        # F-4 (impl-review round 2): the contract is all-or-nothing on BOTH
        # counters. Slices 1/2 registered cleanly, so they are cancel-marked
        # and reported `registered: 0` -- the receipt and the registry agree
        # that this batch admitted nothing.
        self.assertEqual([row["registered"] for row in results], [0, 0, 0])
        self.assertEqual([row["cancelled"] for row in results], [1, 1, 0])
        self.assertEqual(
            [row["refusal_reason"] for row in results],
            [SUBDIV.BATCH_REGISTRATION_INCOMPLETE] * 3,
        )
        self.assertEqual(
            cancelled,
            [session["attempt_id"] for session in admission.sessions[:2]],
        )
        self.assertTrue(all(cmd[cmd.index("--action") + 1] == "register" for cmd in calls))
        self.assertEqual(len(calls), 3)

    def test_first_slice_register_failure_cancels_nothing(self):
        admission = self._admission(3)
        cancelled: list[str] = []

        def fake_run(cmd, env):
            return subprocess.CompletedProcess(cmd, 65, "", "register-failed")

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, cancel_row=lambda jobs, attempt: cancelled.append(attempt) or 1,
        )
        self.assertEqual([row["registered"] for row in results], [0, 0, 0])
        self.assertEqual([row["started"] for row in results], [0, 0, 0])
        self.assertEqual([row["cancelled"] for row in results], [0, 0, 0])
        self.assertEqual(cancelled, [])

    def test_cancel_failure_is_reported_not_raised(self):
        admission = self._admission(2)

        def fake_run(cmd, env):
            slug = cmd[cmd.index("--slug") + 1]
            if slug == "slice-2":
                return subprocess.CompletedProcess(cmd, 65, "", "register-failed")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        def cancel_fails(jobs, attempt_id):
            return 0

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, cancel_row=cancel_fails,
        )
        self.assertEqual([row["registered"] for row in results], [0, 0])
        self.assertEqual([row["cancelled"] for row in results], [0, 0])

    def test_all_registers_succeed_then_all_slices_start(self):
        admission = self._admission(2)

        def fake_run(cmd, env):
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run,
        )
        self.assertEqual([row["started"] for row in results], [1, 1])



FENCE = "`" * 3


def _plan_text(*blocks: str, prefix: str = "") -> str:
    return "# plan\n" + prefix + "".join(f"\n{FENCE}slices\n{b}\n{FENCE}\n" for b in blocks)


class FailedStartClosesUnclaimedRowTest(AdmissionFixture):
    """F14: a slice whose start failed must not leave its never-claimed row open."""

    SLICE = (
        "subsession_mode=parallel,subsession_index=1,subsession_count=2,subsession_purpose=planned,"
        "expected_round_trips=2,parallel_group=execute,phase_brief=/b.md,state_ledger=/l.yaml,"
        f"phase_brief_sha256={'a' * 64},fixed_files_sha256={'b' * 64},narrow_verify_sha256={'c' * 64}"
    )

    def _row(self, attempt: str, claimed: str) -> str:
        return (
            "2026-10-02T00:00:01Z\topen\t/repo\t/repo\towner\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=stage,"
            f"attempt_id={attempt},parent=owner,parent_attempt_id=att-owner,route_id=rt-1,route_node=execute,"
            f"subsession_id=ss-{attempt},stage_authority=0,session_chain_id=ssc-aaaa,{self.SLICE},"
            f"launch_claimed={claimed}"
        )

    def _status(self, jobs: Path, attempt: str) -> str:
        return next(line.split("\t")[1] for line in jobs.read_text().splitlines() if f"attempt_id={attempt}," in line + ",")

    def test_closes_only_a_never_claimed_row(self):
        jobs = self.base / "jobs.log"
        jobs.write_text(self._row("att-open", "0") + "\n" + self._row("att-claimed", "1") + "\n")
        self.assertEqual(SUBDIV._close_unclaimed_row(jobs, "att-open"), 1)
        self.assertEqual(SUBDIV._close_unclaimed_row(jobs, "att-claimed"), 0)
        self.assertEqual(self._status(jobs, "att-open"), "done")
        self.assertEqual(self._status(jobs, "att-claimed"), "open")
        self.assertIn(f"note={SUBDIV.BATCH_START_FAILED}", jobs.read_text())

    def test_failed_start_closes_that_slice_and_reports_it(self):
        admission = StartAdmittedBatchPartialFailureTest._admission(self, 2)
        closed: list[str] = []

        def fake_run(cmd, env):
            action = cmd[cmd.index("--action") + 1]
            slug = cmd[cmd.index("--slug") + 1]
            if action == "start" and slug == "slice-1":
                return subprocess.CompletedProcess(cmd, 65, "", "start-failed")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, close_unclaimed=lambda jobs, attempt: closed.append(attempt) or 1,
        )
        self.assertEqual([row["started"] for row in results], [0, 1])
        self.assertEqual([row["closed_unclaimed"] for row in results], [1, 0])
        self.assertEqual(closed, [admission.sessions[0]["attempt_id"]])


class ReadSlicesTest(unittest.TestCase):
    def _read(self, text: str, name: str = "plan.md"):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / name
            path.write_text(text, encoding="utf-8")
            return SUBDIV.read_slices(path)

    def test_read_slices_reads_exactly_one_fenced_block(self):
        good = '[{"id": "a", "files": ["x.py"], "verify": "true"}, {"files": ["y.py"], "fixed_files": ["y.py"], "narrow_verify": "true"}]'
        self.assertIsNone(self._read("# plan\nno block here\n"))
        self.assertEqual(len(self._read(_plan_text(good))), 2)
        self.assertEqual(len(self._read(_plan_text('{"slices": ' + good + "}"))), 2)
        for bad in (_plan_text(good, good), _plan_text("[not json"), _plan_text('{"a": 1}'), _plan_text('"text"')):
            with self.subTest(bad=bad[:40]), self.assertRaisesRegex(SUBDIV.StageSessionError, "plan-slices-invalid"):
                self._read(bad)

    def test_read_slices_ignores_blocks_nested_in_another_fence(self):
        good = '[{"id": "a", "files": ["x.py"], "verify": "true"}]'
        outer = "`" * 4
        example = f"\n{outer}text\n{FENCE}slices\n[broken\n{FENCE}\n{outer}\n"
        heredoc = f"\n{FENCE}bash\ncat <<'EOF'\n{FENCE}slices\n[broken\n{FENCE}\nEOF\n{FENCE}\n"
        self.assertEqual(self._read(_plan_text(good, prefix=example + heredoc)), [{"id": "a", "files": ["x.py"], "verify": "true"}])
        self.assertIsNone(self._read("# plan\n" + example + heredoc))

    def test_read_slices_reads_a_json_file(self):
        self.assertEqual(self._read('{"slices": []}', "slices.json"), [])
        with self.assertRaisesRegex(SUBDIV.StageSessionError, "plan-slices-invalid"):
            self._read("nope", "slices.json")
        with self.assertRaisesRegex(SUBDIV.StageSessionError, "plan-slices-invalid"):
            SUBDIV.read_slices(Path("/nonexistent/plan.md"))


class SliceCommandTest(unittest.TestCase):
    MANIFEST = {"route_file": "/r.json", "route_node": "execute", "mode": "parallel",
                "chain_id": "ssc-execute-1", "worktree": "/work/linked"}
    SESSION = {"adapter": "claude", "slug": "execute-a", "subsession_id": "ss-1", "index": 1, "count": 2,
               "phase_brief": "/b.md", "narrow_verify": "true", "expected_round_trips": 2,
               "attempt_id": "att-1", "fixed_files": ["a.py"]}

    def test_dispatch_command_forwards_slice_worktree_and_inherited_parent_attempt(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner-X"}):
            command = SUBDIV.dispatch_command(self.MANIFEST, self.SESSION, "start", "owner", Path("/j.log"))
        self.assertEqual(command[command.index("--subsession-worktree") + 1], "/work/linked")
        self.assertEqual(command[-3:], ["--", "--parent-attempt-id", "att-owner-X"])
        self.assertLess(command.index("--fixed-file"), command.index("--"))
        env = {k: v for k, v in os.environ.items() if k != "AGENT_DISPATCH_ATTEMPT_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            command = SUBDIV.dispatch_command(self.MANIFEST, self.SESSION, "start", "owner", Path("/j.log"))
        self.assertNotIn("--", command)
        self.assertNotIn("--parent-attempt-id", command)

    def test_dispatch_command_forwards_the_declared_purpose(self):
        # A derived gap-retry chain (`derive_gap_retry_manifest`) used to launch
        # as `planned` because the purpose axis never left the manifest.
        for declared, expected in ((None, "planned"), ("planned", "planned"), ("gap-retry", "gap-retry")):
            session = dict(self.SESSION, **({} if declared is None else {"subsession_purpose": declared}))
            with self.subTest(declared=declared):
                command = SUBDIV.dispatch_command(self.MANIFEST, session, "register", "owner", Path("/j.log"))
                self.assertEqual(command[command.index("--subsession-purpose") + 1], expected)

    def test_start_env_sets_git_optional_locks_off(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "state" / "jobs.log"
            jobs.parent.mkdir()
            jobs.touch()
            manifest = {"chain_id": "ssc-env-1", "sessions": [
                {"subsession_id": "ss-e1", "attempt_id": "att-env-00001", "index": 1}]}
            admission = SUBDIV.AdmissionResult(
                tokens=["t1"], manifest=manifest, manifest_digest="sha256:0",
                sessions=manifest["sessions"], node_id="execute", reservation_identity="r")
            envs = []
            def _run(cmd, env):
                envs.append((cmd[cmd.index("--action") + 1], env.get("GIT_OPTIONAL_LOCKS"), env.get("TOKEN")))
                return mock.Mock(returncode=0, stdout="", stderr="")
            # tools/run-tests.py itself exports GIT_OPTIONAL_LOCKS=0; measure from a clean ambient.
            with mock.patch.dict(os.environ), \
                    mock.patch.object(SUBDIV, "dispatch_command", side_effect=lambda m, s, a, p, j: ["x", "--action", a]):
                os.environ.pop("GIT_OPTIONAL_LOCKS", None)
                SUBDIV.start_admitted_batch(admission, parent="o", jobs=jobs, governor_reservation_env="TOKEN", run=_run)
            self.assertEqual(envs, [("register", None, None), ("start", "0", "t1")])


class PlanSlicesBriefAndAdapterTest(unittest.TestCase):
    def _plan(self, td, slices, **kwargs):
        root = Path(td)
        (root / "wt").mkdir()
        route = {"route_id": "rt-b", "route_hash": "sha256:" + "4" * 64, "cwd": str(root / "wt"),
                 "effective_intensity": "standard",
                 "nodes": [{"id": "execute", "dispatch_depth": 2, "completion_gate": "code-execute",
                            "kind": "pipeline-stage", "write_scope": ["source/**"],
                            "subdivision": {"min_intensity": "standard", "max_slices": 4,
                                            "disjointness": "exact-fixed-files"}}]}
        (root / "route.json").write_text(json.dumps(route))
        plan = root / "plan.md"
        plan.write_text(_plan_text(json.dumps(slices)))
        out = root / "out" / "chain.json"
        receipt = SUBDIV.plan_slices(route_path=root / "route.json", node_id="execute", slices_path=plan,
                                     output_path=out, worktree=root / "wt", jobs=root / "jobs.log", **kwargs)
        return receipt, json.loads(out.read_text()), plan, root / "wt"

    SLICES = [{"id": "a", "files": ["utilities/a.py"], "verify": "true"},
              {"id": "b", "files": ["utilities/b.py"], "verify": "true", "brief": "only the b part"}]

    def test_phase_brief_carries_plan_worktree_and_no_git_write_rule(self):
        with tempfile.TemporaryDirectory() as td:
            _receipt, manifest, plan, wt = self._plan(td, self.SLICES)
            self.assertEqual(manifest["plan"], str(plan.resolve()))
            texts = [Path(s["phase_brief"]).read_text() for s in manifest["sessions"]]
            for text in texts:
                self.assertIn(f"plan: {plan}", text)
                self.assertIn(f"worktree: {wt.resolve()}", text)
                self.assertIn("fixed_files (exhaustive", text)
                self.assertIn("Never run git add, commit, checkout, restore, stash, reset or rollback", text)
                self.assertIn("GIT_OPTIONAL_LOCKS=0", text)
                self.assertIn("checklist.md", text)
            self.assertNotIn("only the b part", texts[0])
            self.assertIn("only the b part", texts[1])

    def test_plan_slices_is_stable_per_owner_and_distinct_for_replacement_owner(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(td)
            for name in ("first", "repeat", "replacement"):
                (root / name).mkdir()
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = "att-owner-a"
            first, manifest_a, _plan, _wt = self._plan(root / "first", self.SLICES)
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = "att-owner-a"
            repeated, manifest_a_repeat, _plan, _wt = self._plan(root / "repeat", self.SLICES)
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = "att-owner-b"
            replacement, manifest_b, _plan, _wt = self._plan(root / "replacement", self.SLICES)

            self.assertEqual(first["chain_id"], repeated["chain_id"])
            self.assertEqual(
                [row["attempt_id"] for row in manifest_a["sessions"]],
                [row["attempt_id"] for row in manifest_a_repeat["sessions"]],
            )
            self.assertNotEqual(first["chain_id"], replacement["chain_id"])
            self.assertNotEqual(
                [row["attempt_id"] for row in manifest_a["sessions"]],
                [row["attempt_id"] for row in manifest_b["sessions"]],
            )

    def test_slice_adapter_follows_route_allocation_without_pin(self):
        def adapters(loader, env, slices=None):
            with tempfile.TemporaryDirectory() as td, mock.patch.object(SUBDIV, "_load_sibling", loader), \
                    mock.patch.dict(os.environ, env, clear=False):
                _r, manifest, _p, _w = self._plan(td, slices or self.SLICES)
                return [s["adapter"] for s in manifest["sessions"]]
        hops = [{"fallback_hop": "same-harness-headless", "candidates": [{"child_harness": "claude", "status": "unsupported"}]},
                {"fallback_hop": "cross-harness-headless", "candidates": [{"child_harness": "codex", "status": "supported"}]}]
        fallback = mock.Mock(ordered_fallback_hops=mock.Mock(return_value=(hops, None)))
        self.assertEqual(adapters(lambda *a: fallback, {}), ["codex", "codex"])
        # a slice's own adapter always wins
        own = [dict(self.SLICES[0], adapter="opencode"), self.SLICES[1]]
        self.assertEqual(adapters(lambda *a: fallback, {}, own), ["opencode", "codex"])
        def broken(*_a):
            raise ImportError("no loader")
        self.assertEqual(adapters(broken, {"AGENT_DISPATCH_CURRENT_HARNESS": "opencode"}), ["opencode", "opencode"])
        env = {k: v for k, v in os.environ.items() if k != "AGENT_DISPATCH_CURRENT_HARNESS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(adapters(broken, {}), ["claude", "claude"])

if __name__ == "__main__":
    unittest.main()
