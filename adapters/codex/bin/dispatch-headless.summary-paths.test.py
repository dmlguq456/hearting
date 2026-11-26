#!/usr/bin/env python3
"""Narrow title-producer filesystem grants for nested Codex owners."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import importlib.util
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
WH_SPEC = importlib.util.spec_from_file_location(
    "codex_dispatch_headless_summary_paths", Path(__file__).with_name("dispatch-headless.py"))
WH = importlib.util.module_from_spec(WH_SPEC)
WH_SPEC.loader.exec_module(WH)


class SummaryWritableRootsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env_patch = mock.patch.dict(os.environ, {
            "HOME": str(self.base / "home"),
            "XDG_STATE_HOME": str(self.base / "state"),
        }, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def summary_roots(self):
        from dispatch_summary import summary_writable_dirs
        return summary_writable_dirs()

    def args(self, **overrides):
        base = dict(
            dispatch_depth=1, action="start", worker_type="owner", intensity="standard",
            nested_headless_network=True, nested_headless_network_eligible=True,
            jobs_path=self.base / "dispatch" / "jobs.log", agent_home=self.base / "agent",
            worktree=str(self.base / "repo"), artifact_root=str(self.base / "artifacts"), route_id=None,
            route_node=None, attempt_id="att-fixture", command_attempt_id="att-fixture",
            report_bundle_root=None, owner_route_binding=None, max_continuations=None,
            execution_access_grant=None, approval="never", sandbox="workspace-write",
            resolved_model_settings={"source": "inherit", "context_window": "800000"},
            registered_worker=0, route_file=None, spec_read_required=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_xdg_defaults_match_title_producer_and_query_is_pure(self):
        roots = self.summary_roots()
        expected = (
            self.base / "state" / "agent-fleet" / "titles",
            self.base / "state" / "hearting" / "fleet-title-workdir",
            self.base / "state" / "agent-fleet" / "provider-order",
        )
        self.assertEqual(roots, expected)
        self.assertFalse(any(path.exists() for path in roots))
        from fleet import refresh_title, titles
        self.assertEqual(Path(titles.state_root()).resolve(), roots[0])
        self.assertEqual(refresh_title._text_workdir().resolve(), roots[1])
        self.assertEqual(refresh_title._provider_order_cache("codex", "home", "jobs").parent,
                         roots[2])

    def test_title_override_changes_title_and_workdir_but_not_provider_cache(self):
        override = self.base / "custom" / "titles"
        with mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": str(override)}):
            roots = self.summary_roots()
            self.assertEqual(roots, (
                override.resolve(),
                (override / "fleet-title-workdir").resolve(),
                (self.base / "state" / "agent-fleet" / "provider-order").resolve(),
            ))
            self.assertFalse(any(path.exists() for path in roots))
            from fleet import refresh_title, titles
            self.assertEqual(Path(titles.state_root()).resolve(), roots[0])
            self.assertEqual(refresh_title._text_workdir().resolve(), roots[1])
            self.assertEqual(refresh_title._provider_order_cache("codex", "home", "jobs").parent,
                             roots[2])

    def test_owner_grant_creates_missing_roots_and_keeps_agent_fleet_narrow(self):
        args = self.args()
        with mock.patch.object(WH, "progress_writable_dirs", return_value=()), \
             mock.patch.object(WH, "route_bound_worker_writable_dirs", return_value=()), \
             mock.patch.object(WH, "spec_read_marker_required", return_value=False), \
             mock.patch.object(WH, "_core_grounding_dir", return_value=self.base / "core-grounding"):
            WH.ensure_owner_writable_dirs(args)
            granted = WH.nested_owner_writable_dirs(args)
        roots = self.summary_roots()
        self.assertTrue(all(path.is_dir() for path in roots))
        self.assertTrue(set(roots).issubset(set(granted)))
        self.assertNotIn(self.base / "state" / "agent-fleet", granted)
        self.assertFalse(any(path.name in {"actions", "interactions", "capability-grounding"}
                             for path in granted))

    def test_non_owner_eligibility_gets_no_summary_write_roots(self):
        args = self.args(nested_headless_network=False, nested_headless_network_eligible=False)
        with mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": str(self.base / "never-created")}):
            self.assertEqual(WH.nested_owner_writable_dirs(args), ())
            with mock.patch.object(WH, "progress_writable_dirs", return_value=()), \
                 mock.patch.object(WH, "route_bound_worker_writable_dirs", return_value=()), \
                 mock.patch.object(WH, "spec_read_marker_required", return_value=False):
                WH.ensure_owner_writable_dirs(args)
            self.assertFalse((self.base / "never-created").exists())

    def test_create_failure_stays_typed_and_stops_before_launch(self):
        args = self.args()
        provider_root = self.summary_roots()[-1]
        mkdir = Path.mkdir

        def fail_provider(path, *argv, **kwargs):
            if path == provider_root:
                raise PermissionError("fixture denied")
            return mkdir(path, *argv, **kwargs)

        with mock.patch.object(WH, "progress_writable_dirs", return_value=()), \
             mock.patch.object(WH, "route_bound_worker_writable_dirs", return_value=()), \
             mock.patch.object(WH, "spec_read_marker_required", return_value=False), \
             mock.patch.object(WH, "_core_grounding_dir", return_value=self.base / "core-grounding"), \
             mock.patch.object(Path, "mkdir", fail_provider):
            with self.assertRaises(WH.DispatchContractError) as ctx:
                WH.ensure_owner_writable_dirs(args)
        self.assertEqual(ctx.exception.reason, "owner-writable-root-uncreatable")
        self.assertIn(str(provider_root), ctx.exception.detail)

    def test_grant_query_and_both_codex_builders_use_the_execution_default_roots(self):
        args = self.args()
        roots = self.summary_roots()
        with mock.patch.object(WH, "progress_writable_dirs", return_value=()), \
             mock.patch.object(WH, "route_bound_worker_writable_dirs", return_value=()), \
             mock.patch.object(WH, "spec_read_marker_required", return_value=False), \
             mock.patch.object(WH, "_core_grounding_dir", return_value=self.base / "core-grounding"):
            WH.ensure_owner_writable_dirs(args)
            defaults = WH.adapter_default_roots(args, WH.nested_owner_writable_dirs(args))
            from execution_access import ExecutionAccessRequest, build_grant
            request = ExecutionAccessRequest(
                writable_roots=roots, read_roots=(), network_required=False,
                network_reason="", network_hosts=(), enforcement_required="any",
                justification=(), request_sha256="fixture-digest",
                source_path=self.base / "execution-access.json",
            )
            grant = build_grant(
                request, runtime="codex-exec", default_writable_roots=defaults,
                effective_sandbox="workspace-write",
            )
            self.assertEqual(set(grant.absorbed_writable_roots), set(roots))
            self.assertEqual(grant.additional_writable_roots, ())
            args.execution_access_grant = grant
            args.resolved_completion_delivery = "one-shot"
            exec_command = WH.shell_command(args, self.base / "prompt", self.base / "log")
            args.resolved_completion_delivery = "app-server-supervised"
            server_command = WH.shell_command(args, self.base / "prompt", self.base / "log")
        self.assertTrue(set(roots).issubset(set(defaults)))
        self.assertEqual(set(grant.absorbed_writable_roots), set(roots))
        for command, flag in ((exec_command, "--add-dir"), (server_command, "--writable-root")):
            tokens = shlex.split(command)
            projected = {Path(tokens[i + 1]) for i, token in enumerate(tokens[:-1]) if token == flag}
            self.assertTrue(set(roots).issubset(projected))
            self.assertNotIn(self.base / "state" / "agent-fleet", projected)


if __name__ == "__main__":
    unittest.main()
