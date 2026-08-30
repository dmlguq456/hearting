#!/usr/bin/env python3

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


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


if __name__ == "__main__":
    unittest.main()
