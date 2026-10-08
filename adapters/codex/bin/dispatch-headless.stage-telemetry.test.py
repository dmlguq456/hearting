#!/usr/bin/env python3
"""Narrow regressions for one-turn stage telemetry transport selection."""
import argparse
import importlib.util
from pathlib import Path
import shlex
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "codex_dispatch_stage_telemetry", Path(__file__).with_name("dispatch-headless.py"))
WH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WH)


def args_for(worker_type="stage", **overrides):
    values = dict(
        worktree="/tmp/stage/repo", artifact_root="/tmp/stage/artifacts",
        report_bundle_root=None, jobs_path=Path("/tmp/stage/jobs.log"),
        route_id="rt-stage", route_node="execute", attempt_id="att-stage",
        command_attempt_id="att-stage", dispatch_depth=2, worker_type=worker_type,
        intensity="standard", completion_delivery="auto", approval="never",
        sandbox="workspace-write", nested_headless_network=False,
        resolved_model_settings={"source": "resolved", "model": "model-test",
                                "reasoning": "medium"},
        execution_access_grant=argparse.Namespace(
            additional_writable_roots=(Path("/tmp/stage/access-root"),)),
        agent_home=Path("/tmp/stage/home"), max_continuations=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class StageTelemetryTransportTest(unittest.TestCase):
    def test_probe_selects_telemetry_without_changing_one_shot_completion(self):
        for available, expected in ((True, "app-server-one-turn"), (False, "raw-exec")):
            args = args_for()
            with mock.patch.object(WH, "codex_app_server_available", return_value=available) as probe:
                self.assertEqual(WH.resolve_completion_delivery(args), "one-shot")
            probe.assert_called_once_with()
            self.assertEqual(args.resolved_stage_telemetry_transport, expected)

    def test_app_server_stage_keeps_exact_roots_and_excludes_owner_state(self):
        args = args_for()
        args.resolved_stage_telemetry_transport = "app-server-one-turn"
        args.resolved_completion_delivery = "one-shot"
        roots = (Path("/tmp/stage/progress"),)
        route_roots = (Path("/tmp/stage/route-state"),)
        git_roots = (Path("/tmp/stage/git-dir"),)
        with mock.patch.object(WH, "progress_writable_dirs", return_value=roots), \
             mock.patch.object(WH, "nested_owner_writable_dirs", return_value=()), \
             mock.patch.object(WH, "spec_read_marker_required", return_value=False), \
             mock.patch.object(WH, "route_bound_worker_writable_dirs", return_value=route_roots), \
             mock.patch.object(WH, "linked_worktree_git_writable_dirs", return_value=git_roots), \
             mock.patch.object(WH, "registry_writable_launch", return_value=True), \
             mock.patch.object(WH, "commit_profile_config", return_value=None), \
             mock.patch.object(WH, "effective_runtime_sandbox", return_value="workspace-write"):
            command = WH.shell_command(args, Path("/tmp/stage/prompt.txt"),
                                      Path("/tmp/stage/attempt.jsonl"))
        tokens = shlex.split(command)
        self.assertIn("--one-turn", tokens)
        for expected in (args.worktree, args.artifact_root, str(WH.dispatch_state_root(args.jobs_path)),
                         str(route_roots[0]), str(git_roots[0]),
                         "/tmp/stage/access-root"):
            self.assertIn(expected, tokens)
        for forbidden in ("--parent-attempt-id", "--lease-file", "--state-file",
                          "--route-file", "--max-continuations"):
            self.assertNotIn(forbidden, tokens)
        self.assertIn("--model", tokens)
        self.assertIn("--reasoning", tokens)
        self.assertEqual(tokens[tokens.index("--approval") + 1], "never")
        self.assertIn("codex-app-server-supervisor.py", command)
        self.assertNotIn("codex-exec", command)

    def test_raw_unavailable_fallback_remains_exec_json_and_review_uses_same_probe(self):
        args = args_for(worker_type="review", resolved_model_settings={"source": "inherit"},
                        worker_runtime_env={"HEARTING_CODEX_WORKER_OVERRIDES": '["features.apps=false","features.multi_agent=false"]'})
        with mock.patch.object(WH, "codex_app_server_available", return_value=False) as probe:
            self.assertEqual(WH.resolve_completion_delivery(args), "one-shot")
        probe.assert_called_once_with()
        self.assertEqual(args.resolved_stage_telemetry_transport, "raw-exec")
        with mock.patch.object(WH, "progress_writable_dirs", return_value=()), \
             mock.patch.object(WH, "nested_owner_writable_dirs", return_value=()), \
             mock.patch.object(WH, "spec_read_marker_required", return_value=False), \
             mock.patch.object(WH, "route_bound_worker_writable_dirs", return_value=()), \
             mock.patch.object(WH, "linked_worktree_git_writable_dirs", return_value=()), \
             mock.patch.object(WH, "registry_writable_launch", return_value=False), \
             mock.patch.object(WH, "commit_profile_config", return_value=None), \
             mock.patch.object(WH, "effective_runtime_sandbox", return_value="workspace-write"):
            command = WH.shell_command(args, Path("/tmp/stage/prompt.txt"),
                                      Path("/tmp/stage/attempt.jsonl"))
        tokens = shlex.split(command)
        self.assertEqual(tokens[tokens.index("exec") + 1], "--ephemeral")
        self.assertIn("--json", tokens)
        self.assertIn("features.apps=false", tokens)
        self.assertIn("features.multi_agent=false", tokens)
        self.assertNotIn("--one-turn", tokens)

    def test_stage_cannot_select_supervised_completion(self):
        args = args_for(completion_delivery="supervised")
        with self.assertRaises(WH.DispatchContractError):
            WH.resolve_completion_delivery(args)


if __name__ == "__main__":
    unittest.main()
