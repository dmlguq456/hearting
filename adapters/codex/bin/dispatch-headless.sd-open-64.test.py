#!/usr/bin/env python3
"""SD-OPEN-64 regression: a registered route-bound attempt must get the
dispatch state root regardless of dispatch depth, so its own `complete`/`close`
never hits `[Errno 30] Read-only file system` on `completion/<route_id>` or
`jobs.log`. Exercises the real `shell_command()` command assembly (both the
app-server-supervised and plain `codex exec` builders) -- no mock of
`registry_writable_launch` itself.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
WH_S = importlib.util.spec_from_file_location(
    "codex_dispatch_headless_sd64", Path(__file__).with_name("dispatch-headless.py")
)
WH = importlib.util.module_from_spec(WH_S)
WH_S.loader.exec_module(WH)


def _args(**overrides):
    base = dict(
        worktree="/tmp/sd64-fixture/repo", artifact_root="/tmp/sd64-fixture/artifacts",
        jobs_path=Path("/tmp/sd64-fixture/state/jobs.log"), route_id=None,
        route_node=None, attempt_id="att-fixture", command_attempt_id="att-fixture",
        dispatch_depth=1, nested_headless_network=False, worker_type="owner",
        intensity="quick", completion_delivery="auto", agent_home=Path("/tmp/sd64-fixture/home"),
        report_bundle_root=None, owner_route_binding=None, max_continuations=None,
        execution_access_grant=None, approval="never", sandbox="workspace-write",
        resolved_model_settings={"source": "inherit", "context_window": "800000"},
    )
    base.update(overrides)
    ns = argparse.Namespace(**base)
    # The fixture pins the plain-exec builder; supervised delivery is set explicitly where it is under test.
    with mock.patch.object(WH, "codex_app_server_available", return_value=False):
        ns.resolved_completion_delivery = WH.resolve_completion_delivery(ns)
    ns.completion_delivery_reason = getattr(ns, "completion_delivery_reason", "not-applicable")
    return ns


def _state_root_str(args):
    return str(WH.dispatch_state_root(args.jobs_path))


def _grant_dirs(command: str) -> list[str]:
    tokens = command.split()
    dirs = []
    for i, tok in enumerate(tokens):
        if tok in ("--add-dir", "--writable-root") and i + 1 < len(tokens):
            dirs.append(tokens[i + 1])
    return dirs


class SD64GrantMatrix(unittest.TestCase):
    # (a) quick depth-1 owner + route -> completion dir (state root) grant exists
    def test_a_quick_depth1_owner_with_route_gets_state_root(self):
        args = _args(intensity="quick", worker_type="owner", dispatch_depth=1, route_id="rt-fixtureA")
        cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn(_state_root_str(args), _grant_dirs(cmd))

    # (b) standard depth-1 owner without route, without nested network -> narrow (no state root)
    def test_b_standard_depth1_owner_without_route_stays_narrow(self):
        args = _args(intensity="standard", worker_type="owner", dispatch_depth=1, route_id=None,
                     command_attempt_id=None, nested_headless_network=False)
        cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertNotIn(_state_root_str(args), _grant_dirs(cmd))

    # (c) depth-2 route-bound worker -> unchanged (already had state root)
    def test_c_depth2_route_bound_worker_unchanged(self):
        args = _args(intensity="standard", worker_type="stage", dispatch_depth=2, route_id="rt-fixtureC")
        cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn(_state_root_str(args), _grant_dirs(cmd))

    # (d) review worker, route-bound -> gets state root too (route_id + command_attempt_id, depth irrelevant)
    def test_d_review_worker_route_bound_gets_state_root(self):
        args = _args(intensity="standard", worker_type="review", dispatch_depth=2, route_id="rt-fixtureD")
        cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn(_state_root_str(args), _grant_dirs(cmd))

    def test_d2_stage_one_turn_and_raw_builders_keep_the_same_exact_grants(self):
        roots = (Path("/tmp/sd64-fixture/execution-access"),)
        raw = _args(intensity="standard", worker_type="stage", dispatch_depth=2,
                    route_id="rt-stage-grants",
                    execution_access_grant=argparse.Namespace(additional_writable_roots=roots))
        raw_cmd = WH.shell_command(raw, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        one_turn = _args(intensity="standard", worker_type="stage", dispatch_depth=2,
                         route_id="rt-stage-grants",
                         execution_access_grant=argparse.Namespace(additional_writable_roots=roots))
        one_turn.resolved_completion_delivery = "one-shot"
        one_turn.resolved_stage_telemetry_transport = "app-server-one-turn"
        app_cmd = WH.shell_command(one_turn, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertEqual(set(_grant_dirs(raw_cmd)), set(_grant_dirs(app_cmd)))
        self.assertIn(str(roots[0]), _grant_dirs(app_cmd))
        self.assertIn("--one-turn", app_cmd)
        self.assertNotIn("--parent-attempt-id", app_cmd)

    # (e) no route_id at all -> still heartbeats/watchdog only
    def test_e_no_route_id_stays_heartbeats_watchdog_only(self):
        args = _args(intensity="quick", worker_type="owner", dispatch_depth=1, route_id=None,
                     command_attempt_id=None)
        cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        dirs = _grant_dirs(cmd)
        self.assertNotIn(_state_root_str(args), dirs)
        root = WH.dispatch_state_root(args.jobs_path)
        self.assertIn(str(root / "heartbeats"), dirs)
        self.assertIn(str(root / "watchdog"), dirs)

    # (f) nested_headless_network owner -> unchanged (already had state root)
    def test_f_nested_headless_network_owner_unchanged(self):
        args = _args(intensity="strong", worker_type="owner", dispatch_depth=1, route_id=None,
                     command_attempt_id=None, nested_headless_network=True)
        cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn(_state_root_str(args), _grant_dirs(cmd))

    # (g) app-server supervisor builder and codex exec builder produce the same grant set
    def test_g_both_builders_agree_on_grant_set(self):
        exec_args = _args(intensity="quick", worker_type="owner", dispatch_depth=1, route_id="rt-fixtureG")
        exec_cmd = WH.shell_command(exec_args, Path("/tmp/p.txt"), Path("/tmp/l.log"))

        supervised_args = _args(
            intensity="strong", worker_type="owner", dispatch_depth=1, route_id="rt-fixtureG",
            completion_delivery="poll",  # keep resolver deterministic; grant path is independent of delivery
        )
        supervised_args.resolved_completion_delivery = "app-server-supervised"
        supervised_cmd = WH.shell_command(supervised_args, Path("/tmp/p.txt"), Path("/tmp/l.log"))

        self.assertIn(_state_root_str(exec_args), _grant_dirs(exec_cmd))
        self.assertIn(_state_root_str(supervised_args), _grant_dirs(supervised_cmd))
        self.assertTrue(WH.registry_writable_launch(exec_args))
        self.assertTrue(WH.registry_writable_launch(supervised_args))

    # (g2) a quick owner keeps the same grants, sandbox, network and model under the supervisor
    def test_g2_quick_owner_command_is_the_same_under_the_supervisor(self):
        settings = {"source": "profile", "model": "gpt-6-luna", "reasoning": "low", "context_window": "800000"}
        for extra in (dict(route_id="rt-fixtureG2"), dict(route_id=None, command_attempt_id=None),
                      dict(nested_headless_network=True)):
            with self.subTest(**extra):
                exec_args = _args(intensity="quick", resolved_model_settings=settings, **extra)
                self.assertEqual(exec_args.resolved_completion_delivery, "one-shot")
                supervised_args = _args(intensity="quick", resolved_model_settings=settings, **extra)
                supervised_args.resolved_completion_delivery = "app-server-supervised"
                exec_cmd = WH.shell_command(exec_args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
                supervised_cmd = WH.shell_command(supervised_args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
                self.assertEqual(sorted(_grant_dirs(exec_cmd)), sorted(_grant_dirs(supervised_cmd)))
                self.assertEqual(WH.effective_runtime_sandbox(exec_args), WH.effective_runtime_sandbox(supervised_args))
                for command in (exec_cmd, supervised_cmd):
                    self.assertIn("gpt-6-luna", command)
                self.assertEqual("--network-access" in supervised_cmd.split(),
                                 "sandbox_workspace_write.network_access=true" in exec_cmd)
                self.assertNotIn("--route-file", supervised_cmd)

    # (g3) the supervisor takes a quick owner's own one-shot route tuple, only whole
    def test_g3_supervisor_route_tuple_is_complete_or_absent(self):
        route = dict(route_file="/tmp/r.json", route_id="rt-1", route_hash="sha256:1", route_node="one-shot")
        args = _args(intensity="quick", **route)
        args.resolved_completion_delivery = "app-server-supervised"
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("--route-file /tmp/r.json --route-id rt-1 --route-hash sha256:1", command)
        for key in ("route_file", "route_id", "route_hash"):
            partial = _args(intensity="quick", **{**route, key: None})
            partial.resolved_completion_delivery = "app-server-supervised"
            self.assertNotIn("--route-", WH.shell_command(partial, Path("/tmp/p.txt"), Path("/tmp/l.log")))
        other = _args(intensity="quick", **{**route, "route_node": "execute"})
        other.resolved_completion_delivery = "app-server-supervised"
        self.assertNotIn("--route-", WH.shell_command(other, Path("/tmp/p.txt"), Path("/tmp/l.log")))

    # (h) no branch ever grants /home wholesale, Path.home(), or danger-full-access
    def test_h_no_forbidden_broad_grant_anywhere(self):
        cases = [
            _args(intensity="quick", worker_type="owner", dispatch_depth=1, route_id="rt-fixtureH1"),
            _args(intensity="standard", worker_type="stage", dispatch_depth=2, route_id="rt-fixtureH2"),
            _args(intensity="strong", worker_type="owner", dispatch_depth=1, route_id=None,
                  command_attempt_id=None, nested_headless_network=True),
            _args(intensity="quick", worker_type="owner", dispatch_depth=1, route_id=None,
                  command_attempt_id=None),
        ]
        for args in cases:
            with self.subTest(args=vars(args)):
                cmd = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
                self.assertNotIn("danger-full-access", cmd)
                for grant in _grant_dirs(cmd):
                    # a *scoped* subdirectory under home (e.g. nested-network's
                    # `.claude/session-env`) is expected and pre-existing; only a
                    # bare home-root or filesystem-root grant is forbidden here.
                    self.assertNotEqual(grant, str(Path.home()))
                    self.assertNotEqual(grant, "/home")
                    self.assertNotEqual(grant, "/")


class SD64ParityLock(unittest.TestCase):
    """Claude uses tool-permission --add-dir grants (PRD SD-OPEN-60 names it
    tool-permission-graded, not an OS sandbox); OpenCode uses
    scoped_external_directory_config. Neither is this cycle's diff target --
    lock that this SD-64 change never touches those adapters."""

    def test_claude_adapter_untouched_by_sd64_grant_change(self):
        text = (ROOT / "adapters/claude/bin/dispatch-headless.py").read_text()
        self.assertNotIn("registry_writable_launch", text)

    def test_opencode_adapter_untouched_by_sd64_grant_change(self):
        text = (ROOT / "adapters/opencode/bin/dispatch-headless.py").read_text()
        self.assertNotIn("registry_writable_launch", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
