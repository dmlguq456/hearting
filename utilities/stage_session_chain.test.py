#!/usr/bin/env python3

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_subsession_resume_record as RESUME  # noqa: E402

PATH = Path(__file__).with_name("stage-session-chain.py")
SPEC = importlib.util.spec_from_file_location("stage_session_chain", PATH)
CHAIN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CHAIN)


class StageSessionChainLaunchPhaseTest(unittest.TestCase):
    def test_actions_forward_exact_launch_phase_before_any_mutation(self):
        expected = {
            "check": "dry-run",
            "register": "register",
            "start": "start",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            route_path = base / "route.json"
            route_path.write_text(json.dumps({"nodes": [{"id": "execute"}]}))
            envelope = base / "chain.json"
            envelope.write_text(json.dumps({
                "route_file": str(route_path), "route_node": "execute"
            }))
            manifest = {
                "route_file": str(route_path), "route_node": "execute",
                "worktree": str(base), "chain_id": "chain-fixture",
                "mode": "serial", "sessions": [],
            }
            for action, launch_phase in expected.items():
                with self.subTest(action=action), \
                     mock.patch.object(CHAIN, "load_manifest", return_value=manifest), \
                     mock.patch.object(
                         CHAIN.subprocess, "run", return_value=mock.Mock(returncode=64)
                     ) as verify, \
                     mock.patch.object(CHAIN, "resolve_global_registry") as registry, \
                     mock.patch.object(CHAIN, "run_checked") as dispatch, \
                     mock.patch.object(sys, "argv", [
                         "stage-session-chain.py", action,
                         "--manifest", str(envelope), "--parent", "owner",
                     ]):
                    result = CHAIN.main()
                self.assertEqual(result, 64)
                command = verify.call_args.args[0]
                self.assertEqual(
                    command[command.index("--launch-phase") + 1], launch_phase
                )
                registry.assert_not_called()
                dispatch.assert_not_called()


class StageSessionChainStartTest(unittest.TestCase):
    def _manifest(self, base: Path, session_count: int = 3) -> dict:
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
            "route_file": str(base / "route.json"), "route_node": "execute",
            "route_id": "rt-fixture", "route_hash": "sha256:" + "1" * 64,
            "worktree": str(base), "chain_id": "chain-fixture", "mode": "serial",
            "sessions": sessions, "_manifest_path": str(base / "chain.json"),
            "_manifest_sha256": "deadbeef",
        }

    def _run_start(self, base: Path, manifest: dict):
        envelope = base / "chain.json"
        envelope.write_text(json.dumps({
            "route_file": manifest["route_file"], "route_node": "execute",
        }))
        (base / "route.json").write_text(json.dumps({"nodes": [{"id": "execute"}]}))
        started: list[str] = []

        def launch(command):
            started.append(command[command.index("--action") + 1] + ":" +
                            command[command.index("--attempt-id") + 1])
            attempt_id = command[command.index("--attempt-id") + 1]
            stdout = (
                f"check=ok\nattempt_id={attempt_id}\nregistered=1\nstarted=1\n"
                "duplicate_attempt=0\nchild_spawned=1\n"
            ) if command[command.index("--action") + 1] == "start" else ""
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with mock.patch.object(CHAIN, "load_manifest", return_value=manifest), \
                mock.patch.object(CHAIN.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(CHAIN, "resolve_global_registry") as registry, \
                mock.patch.object(CHAIN, "probe_owner_supervision", return_value=mock.Mock(state="held", reason="")), \
                mock.patch.object(CHAIN, "run_checked", side_effect=launch), \
                mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner"}), \
                mock.patch.object(sys, "argv", [
                    "stage-session-chain.py", "start",
                    "--manifest", str(envelope), "--parent", "owner",
                    "--jobs", str(base / "jobs.log"),
                ]):
            registry.return_value = mock.Mock(path=str(base / "jobs.log"))
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                result = CHAIN.main()
                printed = out.getvalue()
        return result, started, printed

    def test_start_spawns_exactly_one_child_and_prints_eight_evidence_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            manifest = self._manifest(base)
            result, started, printed = self._run_start(base, manifest)
            self.assertEqual(result, 0)
            self.assertEqual(
                started,
                [
                    "register:att-stage-session-1", "register:att-stage-session-2",
                    "register:att-stage-session-3", "start:att-stage-session-1",
                ],
            )
            lines = [line for line in printed.splitlines() if line]
            self.assertEqual(lines, [
                "chain_id=chain-fixture",
                "chain_manifest_sha256=deadbeef",
                "registered_sessions=3",
                "registered=1",
                "started=1",
                "started_subsession_index=1",
                "child_spawned=1",
                "runtime_wait=registered-children",
            ])

    def test_start_exits_zero_with_no_foreground_wait(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            manifest = self._manifest(base)
            result, _, _ = self._run_start(base, manifest)
            self.assertEqual(result, 0)
            self.assertFalse(hasattr(CHAIN, "supervise"))
            self.assertFalse(hasattr(CHAIN, "readiness"))

    def test_run_action_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            envelope = base / "chain.json"
            envelope.write_text(json.dumps({
                "route_file": str(base / "route.json"), "route_node": "execute",
            }))
            with mock.patch.object(sys, "argv", [
                "stage-session-chain.py", "run",
                "--manifest", str(envelope), "--parent", "owner",
            ]), self.assertRaises(SystemExit) as ctx:
                CHAIN.main()
            self.assertNotEqual(ctx.exception.code, 0)

    def _main_probe(self, base: Path, probe_state: str, *, initial_receipt=None, unclosed=False):
        base.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(base)
        envelope = base / "chain.json"
        envelope.write_text(json.dumps({
            "route_file": manifest["route_file"], "route_node": "execute",
        }))
        (base / "route.json").write_text(json.dumps({"nodes": [{"id": "execute"}]}))
        jobs = base / "jobs.log"
        jobs.touch()
        receipt = initial_receipt or (
            "check=ok\nattempt_id=att-stage-session-1\nregistered=1\nstarted=1\n"
            "duplicate_attempt=0\nchild_spawned=1\n"
        )
        closed = mock.Mock(
            cancelled=("att-stage-session-2",), already_closed=(),
            unclosed=("att-stage-session-1",) if unclosed else (),
            unclosed_delivery=("poll-fallback",) if unclosed else (),
        )

        def launch(command):
            action = command[command.index("--action") + 1]
            attempt_id = command[command.index("--attempt-id") + 1]
            if action == "register":
                stdout = (
                    f"check=ok\nattempt_id={attempt_id}\nregistered=1\nstarted=0\n"
                    "duplicate_attempt=0\nchild_spawned=0\n"
                )
            else:
                stdout = receipt
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        env = {key: value for key, value in os.environ.items() if key != "AGENT_DISPATCH_ATTEMPT_ID"}
        if probe_state != "unsupervised":
            env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-owner"
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(CHAIN, "load_manifest", return_value=manifest), \
             mock.patch.object(CHAIN.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(CHAIN, "resolve_global_registry", return_value=mock.Mock(path=jobs)), \
             mock.patch.object(CHAIN, "probe_owner_supervision", return_value=mock.Mock(state=probe_state, reason="fixture-probe")), \
             mock.patch.object(CHAIN, "run_checked", side_effect=launch), \
             mock.patch.object(CHAIN, "close_refused_chain_rows", return_value=closed), \
             mock.patch.object(sys, "argv", [
                 "stage-session-chain.py", "start", "--manifest", str(envelope),
                 "--parent", "owner", "--jobs", str(jobs),
             ]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            result = CHAIN.main()
        return result, out.getvalue()

    def test_unsupervised_and_supervision_unproven_are_strict_main_refusals(self):
        with tempfile.TemporaryDirectory() as td:
            for state, reason in (
                ("unsupervised", "subsession-chain-advance-unsupervised"),
                ("unproven", "subsession-chain-advance-supervision-unproven"),
            ):
                with self.subTest(state=state):
                    result, printed = self._main_probe(Path(td) / state, state)
                    self.assertEqual(result, 65)
                    self.assertIn(f"reason={reason}", printed)
                    self.assertIn("fallback=single-session-required", printed)
                    self.assertIn("registered=0", printed)

    def test_invalid_initial_start_receipt_and_unclosed_rows_emit_parent_next(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            base.mkdir(exist_ok=True)
            result, printed = self._main_probe(
                base, "held",
                initial_receipt=(
                    "check=ok\nattempt_id=att-stage-session-1\nregistered=1\nstarted=1\n"
                    "duplicate_attempt=0\nchild_spawned=0\n"
                ),
                unclosed=True,
            )
            self.assertEqual(result, 65)
            self.assertIn("reason=subsession-chain-initial-start-refused", printed)
            self.assertIn("start_verdict=not-spawned", printed)
            self.assertIn("unclosed_rows=1", printed)
            self.assertIn("parent_next=bounded-wait", printed)
            self.assertIn("parent_next_reason=explicit-poll-fallback", printed)
            self.assertIn("parent_next_command=", printed)

            invalid_result, invalid_printed = self._main_probe(
                base / "invalid",
                "held",
                initial_receipt="check=ok\nattempt_id=wrong\n",
            )
            self.assertEqual(invalid_result, 65)
            self.assertIn("start_verdict=receipt-field-missing:registered", invalid_printed)


class PlanSlicesTest(unittest.TestCase):
    """SD-103 cheap path: one command turns the plan's slice list into a proven
    parallel manifest, or a typed refusal."""

    def _fixture(self, td, *, overlap=False, min_intensity="standard"):
        import subprocess
        root = Path(td)
        worktree = root / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        (worktree / "source").mkdir()
        route = {
            "route_id": "rt-plan", "route_hash": "sha256:" + "3" * 64, "cwd": str(worktree),
            "effective_intensity": "standard",
            "nodes": [{
                "id": "execute", "dispatch_depth": 2, "completion_gate": "code-execute",
                "kind": "pipeline-stage", "write_scope": ["source/**", "dev_logs/**"],
                "subdivision": {"min_intensity": min_intensity, "max_slices": 4, "disjointness": "exact-fixed-files"},
            }],
        }
        route_path = root / "route.json"
        route_path.write_text(json.dumps(route), encoding="utf-8")
        second = "source/a.py" if overlap else "source/b.py"
        slices = [
            {"id": "model", "fixed_files": ["source/a.py"], "brief": "change the model", "narrow_verify": "python -m unittest a"},
            {"id": "engine", "fixed_files": [second, "source/c.py"], "brief": "change the engine", "narrow_verify": "python -m unittest b", "expected_round_trips": 3, "adapter": "codex"},
        ]
        slices_path = root / "slices.json"
        slices_path.write_text(json.dumps(slices), encoding="utf-8")
        return route_path, worktree, slices_path, root / "out" / "chain.json"

    def test_plan_slices_writes_a_proven_manifest_and_briefs(self):
        with tempfile.TemporaryDirectory() as td:
            route_path, worktree, slices_path, output = self._fixture(td)
            receipt = CHAIN.plan_slices(route_path=route_path, node_id="execute", worktree=worktree,
                                        slices_path=slices_path, output_path=output)
            self.assertEqual(receipt["planned"], "ok"); self.assertEqual(receipt["sessions"], 2)
            self.assertEqual(receipt["fixed_files"], 3)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["mode"], "parallel"); self.assertEqual(manifest["route_node"], "execute")
            self.assertTrue(manifest["chain_id"].startswith("ssc-execute-"))
            ids = [s["subsession_id"] for s in manifest["sessions"]]
            self.assertEqual(ids, [f"ss-{manifest['chain_id'][4:]}-model", f"ss-{manifest['chain_id'][4:]}-engine"])
            self.assertEqual([s["adapter"] for s in manifest["sessions"]], ["claude", "codex"])
            self.assertEqual([s["expected_round_trips"] for s in manifest["sessions"]], [2, 3])
            self.assertEqual([s["node"] for s in manifest["sessions"]], ["execute-slice-1", "execute-slice-2"])
            for session in manifest["sessions"]:
                self.assertTrue(Path(session["phase_brief"]).is_file())
                self.assertIn("commit nothing", Path(session["phase_brief"]).read_text(encoding="utf-8"))
            # deterministic ids: same inputs, same chain id
            again = CHAIN.plan_slices(route_path=route_path, node_id="execute", worktree=worktree,
                                      slices_path=slices_path, output_path=output)
            self.assertEqual(again["chain_id"], receipt["chain_id"])
            # the proven manifest is exactly what admission re-proves
            proven = CHAIN.load_manifest(output, route=json.loads(route_path.read_text()) | {"_route_file": str(route_path)},
                                         node=json.loads(route_path.read_text())["nodes"][0])
            self.assertEqual(len(proven["sessions"]), 2)

    def test_plan_slices_worktree_must_be_the_sealed_cwd(self):
        """Round-1 B2: a foreign worktree is refused; omitting it uses the route's cwd."""
        with tempfile.TemporaryDirectory() as td:
            route_path, worktree, slices_path, output = self._fixture(td)
            other = Path(td) / "elsewhere"; other.mkdir()
            with self.assertRaisesRegex(CHAIN.StageSessionError, "plan-slices-worktree-mismatch"):
                CHAIN.plan_slices(route_path=route_path, node_id="execute", worktree=other,
                                  slices_path=slices_path, output_path=output)
            self.assertFalse(output.exists())
            receipt = CHAIN.plan_slices(route_path=route_path, node_id="execute",
                                        slices_path=slices_path, output_path=output)
            self.assertEqual(json.loads(output.read_text())["worktree"], str(worktree.resolve()))
            self.assertEqual(receipt["planned"], "ok")

    def test_load_manifest_pins_worktree_to_the_route_cwd(self):
        """Round-2 M1: the common loader, not only plan-slices, refuses a foreign worktree."""
        with tempfile.TemporaryDirectory() as td:
            route_path, worktree, slices_path, output = self._fixture(td)
            CHAIN.plan_slices(route_path=route_path, node_id="execute", slices_path=slices_path, output_path=output)
            route = json.loads(route_path.read_text()) | {"_route_file": str(route_path)}
            node = route["nodes"][0]
            manifest = json.loads(output.read_text())
            foreign = Path(td) / "foreign"; (foreign / "source").mkdir(parents=True)
            for session in manifest["sessions"]:
                session["fixed_files"] = [
                    str(foreign / (Path(f).relative_to(worktree.resolve()) if Path(f).is_absolute() else Path(f)))
                    for f in session["fixed_files"]
                ]
            manifest["worktree"] = str(foreign)
            tampered = Path(td) / "tampered.json"; tampered.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(CHAIN.StageSessionError, "manifest-worktree-mismatch"):
                CHAIN.load_manifest(tampered, route=route, node=node)
            # without a route the loader cannot pin it (the node alone carries the permission)
            self.assertEqual(CHAIN.load_manifest(tampered, node=node)["worktree"], str(foreign.resolve()))

    def test_plan_slices_refuses_overlap_and_leaves_no_files(self):
        with tempfile.TemporaryDirectory() as td:
            route_path, worktree, slices_path, output = self._fixture(td, overlap=True)
            with self.assertRaisesRegex(CHAIN.StageSessionError, "parallel-fixed-file-overlap"):
                CHAIN.plan_slices(route_path=route_path, node_id="execute", worktree=worktree,
                                  slices_path=slices_path, output_path=output)
            self.assertFalse(output.exists())
            self.assertEqual([p.name for p in output.parent.iterdir()] if output.parent.exists() else [], [])

    def test_plan_slices_cli_refusal_is_typed(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            route_path, worktree, slices_path, output = self._fixture(td, overlap=True)
            result = subprocess.run(
                [sys.executable, str(PATH), "plan-slices", "--route", str(route_path), "--worktree", str(worktree),
                 "--slices", str(slices_path), "--output", str(output)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 65, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["planned"], "refused"); self.assertEqual(receipt["fallback"], "single-session-required")
            self.assertIn("parallel-fixed-file-overlap", receipt["reason"])


class RuntimeJoinsCensusTest(unittest.TestCase):
    """F-2 (impl-review round 2): `runtime_joins` is derived from the unique
    owner-resume delivery census and from nothing else -- not from the
    session count, not from a receipt constant."""

    CHAIN_ID = "chain-census-fixture"

    def _jobs(self, base: Path) -> Path:
        jobs = base / "jobs.log"
        jobs.touch()
        return jobs

    def _record(self, jobs: Path, delivery_id: str) -> None:
        RESUME.record_resume(
            jobs.parent,
            route_id="rt-fixture0000000", route_hash="sha256:" + "1" * 64,
            route_node="execute", chain_id=self.CHAIN_ID,
            manifest_sha256="deadbeef", delivery_id=delivery_id,
        )

    def _advance_record(self, jobs: Path, ordinal: int) -> None:
        directory = jobs.parent / "subsession_advance"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"ssadv-{ordinal}.json").write_text(json.dumps({
            "chain_id": self.CHAIN_ID, "outcome": "advanced",
        }), encoding="utf-8")

    def test_census_is_zero_before_any_delivery_not_the_session_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = self._jobs(Path(temp_dir))
            census = CHAIN.chain_census(jobs, self.CHAIN_ID, 3)
            self.assertEqual(census["runtime_joins"], 0)
            self.assertEqual(census["subsession_advances"], 0)
            self.assertEqual(census["expected_subsession_advances"], 2)
            self.assertEqual(census["runtime_joins_source"], RESUME.EVENT_TYPE)

    def test_three_slice_chain_reports_one_join_and_two_advances(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = self._jobs(Path(temp_dir))
            self._advance_record(jobs, 1)
            self._advance_record(jobs, 2)
            self._record(jobs, "delivery-aggregate")
            census = CHAIN.chain_census(jobs, self.CHAIN_ID, 3)
            self.assertEqual(census["runtime_joins"], 1)
            self.assertEqual(
                census["subsession_advances"], census["expected_subsession_advances"]
            )

    def test_replayed_delivery_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = self._jobs(Path(temp_dir))
            self._record(jobs, "delivery-aggregate")
            self._record(jobs, "delivery-aggregate")
            self.assertEqual(CHAIN.chain_census(jobs, self.CHAIN_ID)["runtime_joins"], 1)

    def test_check_projection_never_declares_runtime_joins(self):
        # ``check`` is a dry run, so it exposes only its baseline. The
        # measured runtime value is exercised by the census tests above, and
        # this structural guard keeps the producer unique.
        metrics = CHAIN.continuation_metrics(3)
        self.assertEqual(metrics["baseline_runtime_joins"], 3)
        self.assertNotIn("runtime_joins", metrics)
        source = (ROOT / "utilities" / "stage-session-chain.py").read_text(encoding="utf-8")
        start = source.index("def chain_census(")
        end = source.index("\ndef ", start + 1)
        census_body = source[start:end]
        self.assertIn('"runtime_joins": runtime_joins', census_body)
        self.assertIn("RESUME_RECORD.unique_delivery_ids", census_body)
        outside = source[:start] + source[end:]
        self.assertNotIn('"runtime_joins":', outside)
        self.assertNotIn("runtime_joins=", outside)

    def test_census_action_prints_measured_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            jobs = self._jobs(base)
            manifest = {
                "route_file": str(base / "route.json"), "route_node": "execute",
                "worktree": str(base), "chain_id": self.CHAIN_ID, "mode": "serial",
                "sessions": [{"index": 1}, {"index": 2}], "_manifest_sha256": "deadbeef",
            }
            envelope = base / "chain.json"
            envelope.write_text(json.dumps({
                "route_file": manifest["route_file"], "route_node": "execute",
            }))
            (base / "route.json").write_text(json.dumps({"nodes": [{"id": "execute"}]}))
            self._record(jobs, "delivery-aggregate")
            self._advance_record(jobs, 1)
            with mock.patch.object(CHAIN, "load_manifest", return_value=manifest), \
                    mock.patch.object(
                        CHAIN.subprocess, "run", return_value=mock.Mock(returncode=0)
                    ) as verify, \
                    mock.patch.object(CHAIN, "resolve_global_registry") as registry, \
                    mock.patch.object(CHAIN, "run_checked") as dispatch, \
                    mock.patch.object(sys, "argv", [
                        "stage-session-chain.py", "census",
                        "--manifest", str(envelope), "--parent", "owner",
                        "--jobs", str(jobs),
                    ]):
                registry.return_value = mock.Mock(path=str(jobs))
                with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    result = CHAIN.main()
                    printed = out.getvalue()
            self.assertEqual(result, 0)
            payload = json.loads(printed)
            self.assertEqual(payload["runtime_joins"], 1)
            self.assertEqual(payload["subsession_advances"], 1)
            self.assertEqual(payload["expected_subsession_advances"], 1)
            # Read-only: a census never registers or starts anything.
            dispatch.assert_not_called()
            command = verify.call_args.args[0]
            self.assertEqual(command[command.index("--launch-phase") + 1], "dry-run")


if __name__ == "__main__":
    unittest.main()
