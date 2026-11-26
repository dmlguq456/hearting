#!/usr/bin/env python3
"""Scoped lab verification intent and native profile regressions."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verification_runtime as V
import codex_permission_profile as P


class VerificationRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.wt, self.artifact, self.runtime = (self.base / name for name in ("source", "검토", "runtime"))
        for path in (self.wt, self.artifact, self.runtime):
            path.mkdir()
        node = {"id": "independent-verify", "kind": "review-worker", "unit": "qa/test",
                "completion_gate": "lab-independent-verify", "commit_expected": False,
                "inputs": ["report/index.html", "report/report_manifest.json", "report/media/**"],
                "outputs": ["reviews/eval-verdict.json"], "parallel_group": "independent-verify",
                "write_scope": ["reviews/eval/**"]}
        self.route = {"capability": "autopilot-lab", "cwd": str(self.wt),
                      "artifact_root": str(self.artifact), "route_id": "rt-fixture",
                      "route_hash": "sha256:" + "1" * 64, "nodes": [node]}
        self.path = self.base / "route.json"
        sibling = copy.deepcopy(node)
        sibling.update(id="independent-verify-alternative", outputs=["reviews-alternative/eval-verdict.json"],
                       write_scope=["reviews/eval-alternative/**"])
        self.route["nodes"].append(sibling)
        self.env = {"AGENT_ARTIFACT_OUTPUT_DIR": str(self.artifact), "AGENT_RUNTIME_DIR": str(self.runtime),
                    "HARNESS_STATE_ROOT": str(self.base / "state")}
        import worker_bootstrap as W
        def scope(route, node_id, *a, **kw):
            node = next(n for n in route["nodes"] if n["id"] == node_id)
            return W.NodeScope(str(self.artifact), str(self.wt), tuple(node["outputs"]),
                               tuple(node.get("write_scope", ())), "env")
        patcher = mock.patch.object(W, "resolve_node_scope", side_effect=scope)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.save()
        self.args = SimpleNamespace(route_file=str(self.path), route_node=node["id"],
                                    route_id=self.route["route_id"], route_hash=self.route["route_hash"],
                                    worktree=str(self.wt))

    def save(self):
        self.path.write_text(json.dumps(self.route), encoding="utf-8")

    def profile(self):
        V.prepare_review_directories(V.review_scope(self.route, "independent-verify", self.path, self.env))
        with mock.patch.object(P, "local_verification_available", return_value=True):
            return P.verification_profile_config(str(self.wt),
                [str(self.wt), str(self.artifact), str(self.runtime)], str(self.path), "independent-verify", environ=self.env)

    def test_legacy_lab_contract_derives_local_execution_without_resealing(self):
        original = copy.deepcopy(self.route)
        self.assertEqual(V.runtime_requirements(self.route, "independent-verify"), {"loopback-listen"})
        self.assertEqual(self.route, original)
        self.assertNotIn("runtime_requirements", self.route["nodes"][0])

    def test_other_tests_receive_no_browser_permission(self):
        for field, value in (("unit", "qa/code-review"), ("commit_expected", True),
                             ("completion_gate", "code-test"), ("inputs", ["data.json"])):
            with self.subTest(field=field):
                route = copy.deepcopy(self.route)
                route["nodes"][0][field] = value
                self.assertIsNone(V.lab_browser_node(route, "independent-verify"))
                self.assertEqual(V.runtime_requirements(route, "independent-verify"), set())
        self.route["capability"] = "autopilot-code"
        self.assertIsNone(V.lab_browser_node(self.route, "independent-verify"))

    def test_explicit_unknown_requirement_is_preserved_for_existing_refusal(self):
        self.route["nodes"][0]["runtime_requirements"] = ["unsupported-transport"]
        self.assertEqual(V.runtime_requirements(self.route, "independent-verify"),
                         {"loopback-listen", "unsupported-transport"})

    def test_binding_uses_child_route_and_rejects_wrong_parent_or_worktree(self):
        self.assertEqual(V.bound_lab_context(self.args, {})["artifact_root"], str(self.artifact))
        for key, value in (("route_id", "rt-other"), ("route_hash", "sha256:" + "2" * 64),
                           ("worktree", str(self.base / "other")), ("route_node", "report")):
            with self.subTest(key=key):
                args = copy.copy(self.args)
                setattr(args, key, value)
                self.assertIsNone(V.bound_lab_context(args, {}))

    def test_supervisor_uses_existing_route_environment(self):
        args = SimpleNamespace(route_file=str(self.path), worktree=str(self.wt))
        env = {"AGENT_ROUTE_NODE": "independent-verify", "AGENT_ROUTE_ID": self.route["route_id"],
               "AGENT_ROUTE_HASH": self.route["route_hash"]}
        self.assertEqual(V.bound_lab_context(args, env)["node_id"], "independent-verify")
        env["AGENT_ROUTE_HASH"] = "sha256:" + "3" * 64
        self.assertIsNone(V.bound_lab_context(args, env))

    def test_source_readonly_canonical_and_scratch_write_proxy_only(self):
        config = self.profile()
        body = config["permissions"][P.LOCAL_VERIFICATION_PROFILE_NAME]
        self.assertEqual(body["extends"], ":read-only")
        self.assertEqual(body["filesystem"], {str(self.wt): "read", str(self.artifact / "reviews"): "write",
            str(self.artifact / "reviews/eval"): "write", str(self.runtime): "write",
            str(self.artifact / "reviews/eval-alternative"): "read"})
        self.assertEqual(config["features"], {"network_proxy": True})
        self.assertIs(body["network"]["enabled"], True)
        self.assertEqual(body["network"]["domains"], {"localhost": "allow", "127.0.0.1": "allow", "::1": "allow"})
        self.assertFalse(body["network"]["allow_upstream_proxy"])
        self.assertFalse(body["network"]["enable_socks5_udp"])
        self.assertFalse(any("dangerously" in key or "unix" in key for key in body["network"]))

    def test_inline_toml_retains_unicode_paths_and_proxy_feature(self):
        arguments = P.config_arguments(self.profile())
        parsed = {}
        for index in range(0, len(arguments), 2):
            self.assertEqual(arguments[index], "-c")
            parsed.update(tomllib.loads(arguments[index + 1]))
        self.assertTrue(parsed["features"]["network_proxy"])
        self.assertIn(str(self.artifact / "reviews"), parsed["permissions"][P.LOCAL_VERIFICATION_PROFILE_NAME]["filesystem"])
        self.assertIn("검토", " ".join(arguments))

    def test_no_local_profile_for_ordinary_code_or_missing_native_support(self):
        with mock.patch.object(P, "local_verification_available", return_value=False):
            self.assertIsNone(P.verification_profile_config(str(self.wt), [str(self.artifact)], str(self.path), "independent-verify"))
        self.route["capability"] = "autopilot-code"
        self.save()
        with mock.patch.object(P, "local_verification_available") as available:
            self.assertIsNone(P.verification_profile_config(str(self.wt), [str(self.artifact)], str(self.path), "independent-verify"))
            available.assert_not_called()

    def test_source_is_never_relabelled_as_canonical_artifact_write_root(self):
        self.route["artifact_root"] = str(self.wt)
        self.save()
        with mock.patch.object(P, "local_verification_available", return_value=True):
            self.assertIsNone(P.verification_profile_config(str(self.wt), [str(self.artifact)],
                str(self.path), "independent-verify", environ=self.env))

    def test_shared_hint_preserves_canonical_verdict_and_scope_without_extra_input(self):
        with mock.patch.object(V, "headless_browser_executable", return_value="/existing/headless_shell"):
            hint = V.assignment_hint(self.route, "independent-verify")
        for text in ("canonical review output is writable", "optional artifact sink", "localhost only",
                     "outbound denied", "/existing/headless_shell", "temporary profile"):
            self.assertIn(text, hint)
        self.assertEqual(V.assignment_hint(self.route, "report"), "")

    def test_headless_resolution_uses_only_existing_executables_and_never_full_chrome(self):
        cache = self.base / "cache"
        for revision in (1161, 1243):
            binary = cache / "ms-playwright" / f"chromium_headless_shell-{revision}" / "chrome-linux" / "headless_shell"
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture")
            binary.chmod(0o700)
        full = cache / "ms-playwright" / "chromium-9999" / "chrome-linux" / "chrome"
        full.parent.mkdir(parents=True)
        full.write_text("not a headless-shell candidate")
        full.chmod(0o700)
        with mock.patch.object(Path, "home", return_value=self.base / "empty-home"):
            executable = V.headless_browser_executable({"XDG_CACHE_HOME": str(cache)})
        self.assertIn("chromium_headless_shell-1243", executable)
        self.assertNotIn("chromium-9999", executable)

    def test_unproven_native_version_or_proxy_has_checked_fallback(self):
        cases = (("0.162.0", True, True, False), ("0.163.0", False, True, False),
                 ("0.163.0", True, False, False), ("0.163.0", True, True, True))
        for version, proxy, server, expected in cases:
            with self.subTest(version=version, proxy=proxy, server=server):
                P.local_verification_available.cache_clear()
                replies = [subprocess.CompletedProcess([], 0, f"codex-cli {version}", ""),
                           subprocess.CompletedProcess([], 0, "network_proxy experimental false" if proxy else "other flag", ""),
                           subprocess.CompletedProcess([], 0 if server else 1)]
                with mock.patch.object(P, "named_profiles_available", return_value=True), \
                     mock.patch.object(P.subprocess, "run", side_effect=replies):
                    self.assertIs(P.local_verification_available(), expected)
        P.local_verification_available.cache_clear()


    def test_alternative_canonical_output_is_kept_even_when_missing_from_write_scope(self):
        scope = V.review_scope(self.route, "independent-verify-alternative", self.path, self.env)
        self.assertEqual(set(scope["write_roots"]), {str(self.artifact / "reviews-alternative"),
                                                   str(self.artifact / "reviews/eval-alternative")})
        self.assertNotIn(str(self.artifact), scope["write_roots"])

    def test_scope_rejects_escape_and_symlink_without_touching_bundle(self):
        for pattern in ("../outside/**", "/outside/**", "**"):
            with self.subTest(pattern=pattern):
                self.route["nodes"][0]["write_scope"] = [pattern]
                with self.assertRaises(ValueError):
                    V.review_scope(self.route, "independent-verify", self.path, self.env)
        self.route["nodes"][0]["write_scope"] = ["reviews/eval/**"]
        (self.artifact / "reviews").symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            V.review_scope(self.route, "independent-verify", self.path, self.env)

    def test_three_harnesses_require_the_checked_local_sandbox_tool(self):
        for harness in ("codex", "claude", "opencode"):
            for ready in (True, False):
                with self.subTest(harness=harness, ready=ready), \
                     mock.patch.object(P, "local_verification_available", return_value=ready):
                    self.assertEqual(V.supported_requirements(self.route, "independent-verify", harness),
                                     {"loopback-listen"} if ready else set())

    def test_shared_runner_skips_only_exact_native_attempt_and_wraps_tool_harnesses(self):
        import execution_access as E
        env = {**self.env, "AGENT_ROUTE_FILE": str(self.path), "AGENT_ROUTE_NODE": "independent-verify",
               "AGENT_ROUTE_ID": self.route["route_id"], "AGENT_ROUTE_HASH": self.route["route_hash"],
               "AGENT_DISPATCH_WORKTREE": str(self.wt), "AGENT_DISPATCH_JOBS": str(self.base / "jobs.log"),
               "AGENT_DISPATCH_ATTEMPT_ID": "att-current"}
        for harness, runtime in (("codex", "codex-exec"), ("claude", "claude-cli"), ("opencode", "opencode")):
            with self.subTest(harness=harness):
                scope = V.effective_scope(self.args, [str(self.wt), str(self.artifact), str(self.runtime)], harness, env)
                grant = E.ParentGrant(writable_roots=(self.artifact, self.runtime), runtime=runtime,
                                      verification_scope=scope)
                with mock.patch.object(E, "load_parent_effective_grant", return_value=grant) as load, \
                     mock.patch.object(P, "local_verification_available", return_value=True):
                    command, child = V.verification_command(["python3", "check.py"], env)
                self.assertEqual(load.call_args.kwargs["parent_attempt_id"], "att-current")
                if harness == "codex":
                    self.assertEqual(command, ["python3", "check.py"])
                else:
                    self.assertEqual(command[:2], ["codex", "sandbox"])
                    self.assertEqual(command[-3:], ["--", "python3", "check.py"])
                    self.assertIn(P.LOCAL_VERIFICATION_PROFILE_NAME, command)
                self.assertTrue(Path(child["TMPDIR"]).is_dir())
                self.assertIn(str(self.runtime), child["TMPDIR"])
        with mock.patch.object(E, "load_parent_effective_grant", return_value=E.ParentGrant(())):
            with self.assertRaisesRegex(ValueError, "loopback-only-unsupported"):
                V.verification_command(["true"], env)
        self.assertEqual(V.verification_command(["true"], {}), (["true"], {}))

    def test_native_supervisor_selects_profile_on_thread_and_turn(self):
        path = Path(__file__).with_name("codex-app-server-supervisor.py")
        spec = importlib.util.spec_from_file_location("lab_supervisor_test", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        captured = {}
        class Server:
            def __init__(self, command, cwd, env):
                captured["command"], captured["env"] = command, env
                self.events = [{"method": "turn/completed", "params": {
                    "turn": {"id": "turn-1", "status": "completed"}}}]
            def request(self, method, params):
                captured[method] = params
                if method == "thread/start": return {"thread": {"id": "thread-1"}}
                if method == "turn/start": return {"turn": {"id": "turn-1"}}
                return {}
            def notification(self, method): pass
            def next_event(self): return self.events.pop(0)
            def close(self): captured["closed"] = True
        args = module.parser().parse_args(["--one-turn", "--worktree", str(self.wt),
            "--route-file", str(self.path), "--sandbox", "workspace-write",
            "--writable-root", str(self.artifact), "--writable-root", str(self.runtime)])
        env = {**self.env, "AGENT_ROUTE_NODE": "independent-verify"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(P, "local_verification_available", return_value=True), \
             mock.patch.object(module, "turn_permissions_available", return_value=True), \
             mock.patch.object(module, "AppServer", Server), \
             mock.patch.object(module.sys, "stdin", io.StringIO("review prompt")), \
             mock.patch.object(module.sys, "stdout", io.StringIO()):
            self.assertEqual(module.run_one_turn(args), 0)
        self.assertEqual(captured["thread/start"]["permissions"], P.LOCAL_VERIFICATION_PROFILE_NAME)
        self.assertEqual(captured["turn/start"]["permissions"], P.LOCAL_VERIFICATION_PROFILE_NAME)
        self.assertNotIn("sandbox", captured["thread/start"])
        self.assertNotIn("sandboxPolicy", captured["turn/start"])
        self.assertIn(str(self.runtime), captured["env"]["TMPDIR"])
        self.assertTrue(captured["closed"])
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(module, "verification_profile_config", return_value=None):
            with self.assertRaisesRegex(module.SupervisorError, "loopback-only-unsupported"):
                module.configure_permission_profiles(args, ["codex", "app-server"])


    def test_canonical_spec_marker_grant_survives_without_opening_state_parent(self):
        from harness_state_roots import spec_grounding_dir
        marker = spec_grounding_dir(None, self.env)
        lookalike = self.base / "unrelated" / "spec-grounding"
        scope = V.review_scope(self.route, "independent-verify", self.path, self.env)
        supplied = [str(self.artifact), str(self.runtime), str(marker), str(lookalike)]
        roots = V.scoped_write_roots(scope, supplied, self.env)
        self.assertIn(str(marker), roots)
        for denied in (marker.parent, lookalike, self.wt, self.artifact):
            self.assertNotIn(str(denied), roots)
        with mock.patch.object(P, "local_verification_available", return_value=True):
            profile = P.verification_profile_config(str(self.wt), supplied, str(self.path),
                "independent-verify", environ=self.env)
        fs = profile["permissions"][P.LOCAL_VERIFICATION_PROFILE_NAME]["filesystem"]
        self.assertEqual(fs[str(marker)], "write")
        self.assertNotIn(str(marker.parent), fs)
        self.assertNotIn(str(lookalike), fs)
        self.assertNotIn(str(marker), V.scoped_write_roots(scope, [str(self.runtime)], self.env))

    def test_registered_launch_defaults_declare_the_same_marker_for_three_harnesses(self):
        from execution_access import adapter_default_roots
        marker = self.base / "state" / "spec-grounding"
        with mock.patch.dict(os.environ, self.env):
            for harness in ("codex", "claude", "opencode"):
                args = SimpleNamespace(harness=harness, route_id=self.route["route_id"],
                                       worktree=str(self.wt), artifact_root=str(self.artifact))
                roots = adapter_default_roots(args, [marker])
                self.assertEqual(roots.count(marker), 1)
                self.assertNotIn(marker.parent, roots)
            self.assertNotIn(marker, adapter_default_roots(SimpleNamespace(worktree=str(self.wt))))

    def test_marker_resolution_honors_passed_state_environment(self):
        from harness_state_roots import spec_grounding_dir
        with mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(self.base / "wrong")}):
            self.assertEqual(spec_grounding_dir(None, self.env), self.base / "state/spec-grounding")
            self.assertEqual(spec_grounding_dir(None, {"XDG_STATE_HOME": str(self.base / "xdg")}),
                             self.base / "xdg/hearting/spec-grounding")


class ApprovedTestProjectionTest(unittest.TestCase):
    """U10: one decision for localhost-only scope; lab and declared requirements keep refusing."""

    def route(self):
        nodes = [{"id": "execute", "completion_gate": "code-execute", "commit_expected": True},
                 {"id": "test", "completion_gate": "code-test", "commit_expected": False},
                 {"id": "review", "completion_gate": "code-review", "commit_expected": False}]
        return {"capability": "autopilot-code", "nodes": nodes}

    def test_approved_nodes_derive_from_the_sealed_gate_without_reseal(self):
        route = self.route()
        original = copy.deepcopy(route)
        self.assertEqual(V.approved_test_node(route, "execute")["id"], "execute")
        self.assertEqual(V.approved_test_node(route, "test")["id"], "test")
        self.assertIsNone(V.approved_test_node(route, "review"))
        self.assertIsNone(V.approved_test_node(route, "missing"))
        self.assertEqual(route, original)

    def test_projection_matrix(self):
        route = self.route()
        route["nodes"][2]["runtime_requirements"] = ["loopback-listen"]
        with mock.patch.object(P, "local_verification_available", return_value=True):
            self.assertEqual(V.local_network_projection(route, "execute", "codex",
                                                        sandbox="workspace-write"), "native-profile")
            self.assertEqual(V.local_network_projection(route, "test", "codex",
                                                        sandbox="workspace-write"), "native-profile")
            for sandbox in ("read-only", "danger-full-access", None):
                self.assertEqual(V.local_network_projection(route, "execute", "codex",
                                                            sandbox=sandbox), "unavailable")
            # Declared on a read-only review node: not realizable, so it is refused.
            self.assertEqual(V.local_network_projection(route, "review", "codex",
                                                        sandbox="read-only"), "unavailable")
            self.assertEqual(V.supported_requirements(route, "review", "codex",
                                                      sandbox="read-only"), set())
            # Claude/OpenCode: nothing is claimed outside the checked lab command.
            for harness in ("claude", "opencode"):
                self.assertIsNone(V.local_network_projection(route, "execute", harness))
                self.assertEqual(V.local_network_projection(route, "review", harness), "unavailable")
        with mock.patch.object(P, "local_verification_available", return_value=False):
            self.assertEqual(V.local_network_projection(route, "execute", "codex",
                                                        sandbox="workspace-write"), "unavailable")
            self.assertEqual(V.supported_requirements(route, "execute", "codex",
                                                      sandbox="workspace-write"), set())

    def test_lab_projection_ignores_the_launch_sandbox_and_keeps_its_refusal(self):
        lab = VerificationRuntimeTest("test_legacy_lab_contract_derives_local_execution_without_resealing")
        lab.setUp()
        self.addCleanup(lab.tmp.cleanup)
        with mock.patch.object(P, "local_verification_available", return_value=True):
            for harness, expected in (("codex", "native-profile"),
                                      ("claude", "verification-command"),
                                      ("opencode", "verification-command")):
                self.assertEqual(V.local_network_projection(
                    lab.route, "independent-verify", harness), expected)
                self.assertEqual(V.supported_requirements(
                    lab.route, "independent-verify", harness), {"loopback-listen"})
        with mock.patch.object(P, "local_verification_available", return_value=False):
            for harness in ("codex", "claude", "opencode"):
                self.assertEqual(V.supported_requirements(
                    lab.route, "independent-verify", harness), set())


if __name__ == "__main__":
    unittest.main()
