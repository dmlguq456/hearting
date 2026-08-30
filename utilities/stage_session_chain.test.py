#!/usr/bin/env python3

import importlib.util
import io
import json
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
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(CHAIN, "load_manifest", return_value=manifest), \
                mock.patch.object(CHAIN.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(CHAIN, "resolve_global_registry") as registry, \
                mock.patch.object(CHAIN, "run_checked", side_effect=launch), \
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
        # The pre-v48 defect was `runtime_joins` being a constant printed
        # beside this baseline. `check` is a dry run, so it emits the
        # baseline only; the measured value has exactly one producer.
        metrics = CHAIN.continuation_metrics(3)
        self.assertEqual(metrics["baseline_runtime_joins"], 3)
        self.assertNotIn("runtime_joins", metrics)
        # Structural: the only place this module assigns a `runtime_joins`
        # value is `chain_census`, and it assigns it from the census call.
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
