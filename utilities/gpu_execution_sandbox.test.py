#!/usr/bin/env python3
"""GPU device policy stays scoped and agrees across probe, tuple and execution."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import gpu_execution_sandbox as G
import execution_access as E
import owner_write_advisory as A

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C = load("gpu_codex", "adapters/codex/bin/dispatch-headless.py")
R = load("gpu_route", "utilities/capability-route.py")
D = load("gpu_node", "utilities/dispatch-node.py")
P = load("gpu_readiness", "utilities/dispatch-readiness.py")
S = load("gpu_supervisor", "utilities/codex-app-server-supervisor.py")


class GpuSandboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.artifact = self.root / "artifact"
        self.home = self.root / "home"
        self.state = self.root / "state" / "dispatch"
        self.install = self.root / "install"
        self.data = self.root / "approved-data"
        for path in (self.worktree, self.artifact, self.home, self.state, self.install, self.data):
            path.mkdir(parents=True)
        self.env = {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
                    "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_STATE_HOME": str(self.root / "state")}
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.context = E.AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
            dispatch_state_root=self.state, agent_home=self.install, environ=self.env)
        self.route = {"capability": "autopilot-lab", "owner_dispatch_depth": 1,
                      "effective_intensity": "standard", "cwd": str(self.worktree),
                      "selection": {"promotion_signals": [{"signal": "gpu"}]},
                      "nodes": [{"id": "scaffold", "kind": "pipeline-stage", "resource_class": "normal"},
                                {"id": "full-run", "kind": "resource-runner", "resource_class": "long-running"}]}
        self.route_file = self.root / "route.json"
        self.request_file = self.root / "request.json"

    def args(self, **changes):
        self.route_file.write_text(json.dumps(self.route))
        values = dict(dispatch_depth=1, worker_type="owner", intensity="standard",
                      route_file=str(self.route_file), route_node=None, owner_route_binding=None,
                      sandbox="workspace-write", replacement_input_argv=[],
                      launch_lifecycle="detached", parent_harness="codex", parent_transport="headless",
                      parent_sandbox="danger-full-access", worktree=str(self.worktree),
                      artifact_root=str(self.artifact), agent_home=str(self.install),
                      jobs_path=self.state / "jobs.log", attempt_id="att-gpu", command_attempt_id=None,
                      resolved_completion_delivery="one-shot", report_bundle_root=None, max_continuations=12,
                      route_id=None, nested_headless_network=False, write_scope=None,
                      resolved_model_settings={"source": "inherit"}, approval="never", execution_access_grant=None)
        values.update(changes)
        return argparse.Namespace(**values)

    def request(self, **changes):
        data = {"schema_version": 1, "writable_roots": [str(self.data)], "read_roots": [],
                "network": {"required": False, "reason": "", "hosts": []},
                "enforcement_required": "any", "justification": {str(self.data): "approved data"}}
        data.update(changes)
        self.request_file.write_text(json.dumps(data))
        return E.load_request(self.request_file, context=self.context)

    def test_typed_scope_does_not_infer_task_prose_or_change_normal_children(self):
        self.assertEqual(G.select(self.route)["sandbox"], "danger-full-access")
        self.assertFalse(G.select(self.route, owner=False, node="scaffold")["gpu_scope"])
        for cap in ("autopilot-code", "route-frame"):
            route = {**self.route, "capability": cap}
            self.assertEqual(G.select(route)["sandbox"], "workspace-write")
        route = {**self.route, "nodes": [self.route["nodes"][0]], "selection": {},
                 "work_request": {"text": "GPU NVIDIA training"}}
        self.assertFalse(G.select(route)["gpu_scope"])
        self.route["nodes"][0]["resource_class"] = "gpu"
        self.assertTrue(G.select(self.route, owner=False, node="scaffold")["gpu_scope"])
        args = self.args(dispatch_depth=2, worker_type="stage", route_node="full-run")
        C.apply_gpu_execution_sandbox(args)
        self.assertEqual(C.effective_runtime_sandbox(args), "danger-full-access")

    def test_signal_free_lab_execution_and_validation_workers_use_gpu_sandbox(self):
        self.route["selection"] = {}
        self.route["nodes"] = [
            {"id": name, "kind": "review-worker", "resource_class": "normal"}
            for name in ("smoke", "run-verify", "plan", "report", "handoff")
        ] + [{"id": "full-run", "kind": "resource-runner", "resource_class": "long-running"},
             {"id": "smoke-alternative", "parallel_anchor": "smoke", "kind": "review-worker"},
             {"id": "frame-gpu", "parallel_anchor": "smoke", "kind": "frame-worker"}]
        choice = G.select(self.route)
        self.assertEqual(choice["gpu_resource_nodes"],
                         ["smoke", "run-verify", "full-run", "smoke-alternative"])
        for node in self.route["nodes"]:
            with self.subTest(node=node["id"]):
                gpu = node["id"] in choice["gpu_resource_nodes"]
                args = self.args(dispatch_depth=2, worker_type="review", route_node=node["id"],
                                 parent_harness="claude", parent_sandbox="adapter-default")
                C.apply_gpu_execution_sandbox(args)
                self.assertEqual(C.effective_runtime_sandbox(args),
                                 "danger-full-access" if gpu else "workspace-write")
                for delivery in ("one-shot", "app-server-supervised"):
                    args.resolved_completion_delivery = delivery
                    self.assertIn("--sandbox " + args.sandbox,
                                  C.shell_command(args, self.root / "prompt", self.root / "log"))
                policy = S.sandbox_policy(argparse.Namespace(sandbox=args.sandbox, network_access=False,
                    worktree=str(self.worktree), writable_root=[]))
                self.assertEqual(policy["type"], "dangerFullAccess" if gpu else "workspaceWrite")

    def test_same_cycle_suffix_keeps_sealed_owner_but_normal_child_default(self):
        choice = G.select(self.route)
        suffix = {**self.route, "continuation_contract_version": 1,
                  "codex_execution_sandbox": choice, "nodes": [self.route["nodes"][0]]}
        self.assertEqual(G.select(suffix), choice)
        self.assertFalse(G.select(suffix, owner=False, node="scaffold")["gpu_scope"])
        self.assertEqual(G.select({**suffix, "capability": "autopilot-code"})["sandbox"], "workspace-write")
        self.assertEqual(G.select(suffix, requested="read-only")["sandbox"], "read-only")

    def test_existing_override_precedence_is_shared(self):
        choice = G.select(self.route, requested="read-only", environ={
            "CODEX_DISPATCH_SANDBOX": "danger-full-access", "CODEX_DISPATCH_SANDBOX_FORCE": "workspace-write"})
        self.assertEqual((choice["sandbox"], choice["source"]), ("workspace-write", "forced-env"))
        self.assertEqual(G.select(self.route, requested="read-only", environ={})["sandbox"], "read-only")
        with mock.patch.dict(os.environ, {"CODEX_DISPATCH_SANDBOX_FORCE": "read-only"}):
            args = self.args()
            C.apply_gpu_execution_sandbox(args)
            self.assertEqual(args.sandbox, "read-only")
            self.assertFalse(C.nested_headless_network_enabled(args))
        normal = self.args()
        normal.gpu_execution_scope = False
        normal.sandbox = "danger-full-access"
        self.assertFalse(C.nested_headless_network_enabled(normal))
        readonly_child = self.args(dispatch_depth=2, worker_type="stage", route_node="full-run",
            launch_lifecycle=C.FOREGROUND_SCOPED, parent_sandbox="workspace-write", sandbox="read-only",
            replacement_input_argv=["--sandbox", "read-only"])
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
            C.apply_gpu_execution_sandbox(readonly_child)
            self.assertEqual(C.effective_runtime_sandbox(readonly_child), "read-only")

    def evidence(self, choice, owners=("codex",)):
        with mock.patch.object(P.NESTED, "command_check", return_value=("supported", "fixture", "")), \
             mock.patch.object(P.NESTED, "prospective_owner_registry_check", return_value=(True, "")):
            return P.generate(worktree=self.worktree, jobs=self.state / "jobs.log",
                owner_harnesses=list(owners), child_harnesses=["codex"], codex_execution_selection=choice)

    def test_gpu_readiness_sealed_parent_and_actual_exec_app_server_agree(self):
        # BC's actual shape: scaffold remains normal; full-run declares GPU need.
        for env in ({}, {"CODEX_DISPATCH_SANDBOX_FORCE": "workspace-write"}):
            with mock.patch.dict(os.environ, env):
                choice = G.select(self.route)
                evidence = self.evidence(choice)
                row = evidence["tuples"][0]
                self.assertEqual(row["status"], "supported")
                self.assertEqual(row["parent_sandbox"], choice["sandbox"])
                self.route["codex_execution_sandbox"] = choice
                self.route["dispatch_evidence"] = evidence
                node = {"id": "scaffold", "dispatch_depth": 2,
                        "fallback_hops": R._fallback_chain(evidence, expected_worktree=self.worktree, require_scope=True)}
                args = self.args()
                C.apply_gpu_execution_sandbox(args)
                parent = {"parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": args.sandbox}
                D.resolve_checked_tuple(self.route, node, "codex", parent)
                self.assertTrue(C.nested_headless_network_enabled(args))
                args.worker_type = "stage"  # Isolate existing argv builders from owner supervision.
                for delivery in ("one-shot", "app-server-supervised"):
                    args.resolved_completion_delivery = delivery
                    command = C.shell_command(args, self.root / "prompt", self.root / "log")
                    self.assertIn("--sandbox " + choice["sandbox"], command)
                policy = S.sandbox_policy(argparse.Namespace(sandbox=args.sandbox, network_access=True,
                    worktree=str(self.worktree), writable_root=[str(self.data)]))
                self.assertEqual(policy["type"], "dangerFullAccess" if args.sandbox == "danger-full-access" else "workspaceWrite")
            self.route.pop("codex_execution_sandbox")

    def test_original_workspace_tuple_full_parent_failure_is_preserved(self):
        choice = {**G.select(self.route), "sandbox": "workspace-write"}
        evidence = self.evidence(choice)
        self.route["dispatch_evidence"] = evidence
        node = {"dispatch_depth": 2, "fallback_hops": R._fallback_chain(evidence, expected_worktree=self.worktree, require_scope=True)}
        with self.assertRaisesRegex(D.DispatchNodeError, "dispatch-evidence-parent-runtime-mismatch"):
            D.resolve_checked_tuple(self.route, node, "codex", {"parent_harness": "codex",
                "parent_transport": "headless", "parent_sandbox": "danger-full-access"})
        with self.assertRaises(C.DispatchContractError) as raised:
            C.apply_gpu_execution_sandbox(self.args())
        self.assertEqual(raised.exception.reason, "dispatch-evidence-parent-runtime-mismatch")

    def test_normal_compose_seals_the_same_gpu_choice_used_by_readiness(self):
        observed = []
        def readiness(cwd, jobs, parent, children, *, gpu_route=None):
            choice = G.select(gpu_route)
            observed.append(choice)
            return self.evidence(choice)
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}), \
             mock.patch.object(R, "_compose_readiness", side_effect=readiness):
            route = R.compose_route(capability="autopilot-lab", capability_mode="setup",
                shape="staged", graph=None, slug="gpu-fixture", cwd=str(self.worktree),
                artifact_root=str(self.artifact), signals=[], spec_read="fixture",
                campaign_key="gpu-fixture", parent_harness="codex", children=["codex"], jobs=self.state / "jobs.log")
        self.assertEqual(len(observed), 1)
        self.assertEqual(route["codex_execution_sandbox"], observed[0])
        self.assertEqual(route["dispatch_evidence"]["tuples"][0]["parent_sandbox"], "danger-full-access")
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}):
            R.verify_route(route, self.worktree)
        self.route = route
        args = self.args()
        C.apply_gpu_execution_sandbox(args)
        self.assertEqual(args.sandbox, "danger-full-access")

    def compose_with_evidence(self, evidence, **changes):
        values = dict(capability="autopilot-lab", capability_mode="setup", shape="staged",
            graph=None, slug="gpu-frame-fixture", cwd=str(self.worktree),
            artifact_root=str(self.artifact), signals=["gpu"], spec_read="fixture",
            unassigned=True, parent_harness="codex", children=["codex"],
            jobs=self.state / "jobs.log", dispatch_evidence=evidence)
        values.update(changes)
        return R.compose_route(**values)

    def test_gpu_leg_checks_new_codex_choice_without_relabeling_frame_evidence(self):
        previous = self.evidence({**G.select(self.route), "sandbox": "workspace-write"},
                                 owners=("codex", "claude"))
        original = copy.deepcopy(previous)
        observed = []
        def readiness(cwd, jobs, parent, children, *, gpu_route=None):
            self.assertEqual((parent, children), ("codex", ["codex"]))
            choice = G.select(gpu_route)
            observed.append(choice)
            return self.evidence(choice)
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}), \
             mock.patch.object(R, "_compose_readiness", side_effect=readiness):
            route = self.compose_with_evidence(previous)
            R.verify_route(route, self.worktree)
        self.assertEqual(previous, original)
        self.assertEqual(len(observed), 1)
        self.assertEqual(route["codex_execution_sandbox"], observed[0])
        rows = route["dispatch_evidence"]["tuples"]
        self.assertEqual(next(r for r in rows if r["parent_harness"] == "codex")["parent_sandbox"],
                         "danger-full-access")
        self.assertEqual(next(r for r in rows if r["parent_harness"] == "claude"),
                         next(r for r in R._validate_dispatch_evidence(original)["tuples"]
                              if r["parent_harness"] == "claude"))

    def test_normal_and_other_parent_frame_evidence_is_reused(self):
        for owners, changes in ((("claude",), {}), (("codex",), {
                "capability": "autopilot-code", "capability_mode": "dev",
                "graph": "execute,test,report", "signals": []})):
            with self.subTest(owners=owners, changes=changes):
                evidence = self.evidence({**G.select(self.route), "sandbox": "workspace-write"}, owners=owners)
                original = copy.deepcopy(evidence)
                probe = {**evidence, "candidates": []}
                supplied = R._leg_evidence({"shape": "staged", "capability": changes.get("capability", "autopilot-lab")},
                                           lambda: probe)
                self.assertEqual(supplied["dispatch_evidence"]["tuples"], original["tuples"])
                with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}), \
                     mock.patch.object(R, "_compose_readiness") as readiness:
                    route = self.compose_with_evidence(supplied["dispatch_evidence"], **changes)
                    R.verify_route(route, self.worktree)
                readiness.assert_not_called()
                self.assertEqual(route["dispatch_evidence"]["tuples"],
                                 R._validate_dispatch_evidence(original)["tuples"])
                self.assertEqual(evidence, original)

    def test_gpu_choice_does_not_replace_foreign_or_failed_scope_evidence(self):
        for reason in ("foreign", "exact-worktree"):
            evidence = self.evidence({**G.select(self.route), "sandbox": "workspace-write"})
            row = evidence["tuples"][0]
            if reason == "foreign":
                row["checked_worktree"] = str(self.data)
                expected = "dispatch-evidence-worktree-mismatch"
            else:
                row.update(status="unsupported", failure_scope="exact-worktree",
                           retry_on_isolated_worktree=1, codex_command="ok")
                expected = "dispatch-evidence-exact-worktree-reprobe-required"
            original = copy.deepcopy(evidence)
            with self.subTest(reason=reason), mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)}), \
                 mock.patch.object(R, "_compose_readiness") as readiness:
                with self.assertRaisesRegex(ValueError, expected):
                    self.compose_with_evidence(evidence)
                readiness.assert_not_called()
                self.assertEqual(evidence, original)

    def test_scoped_any_grant_is_logical_and_canonical_record_is_honest(self):
        request = self.request(network={"required": True, "reason": "approved transfer", "hosts": ["fixture.invalid:22"]})
        grant = E.build_grant(request, runtime="codex-exec", effective_sandbox="danger-full-access",
                              gpu_resource_scope=True, network_available=True)
        self.assertEqual((grant.file_enforcement, grant.network_enforcement), ("none", "none"))
        self.assertEqual(grant.network, "granted-unenforced")
        self.assertIn("file-enforcement-none", grant.unmet)
        self.assertIn("network-enforcement-none", grant.unmet)
        path, _ = E.publish_effective_grant(jobs=self.state / "jobs.log", attempt_id="att-gpu",
            route_id="rt-gpu", route_hash="sha256:" + "a" * 64, runtime="codex-exec",
            sandbox="danger-full-access", grant=grant, default_writable_roots=[self.worktree], network_allowed=True)
        record = json.loads(path.read_text())
        self.assertEqual(record["boundary"], "logical-request")
        self.assertFalse(record["os_filesystem_enforced"])
        self.assertFalse(record["os_network_enforced"])
        self.assertEqual(record["unmet"], list(grant.unmet))

    def test_implicit_inventory_request_is_any_and_explicit_strict_stays_strict(self):
        inventory = self.root / "compute-hosts.yaml"
        run_root = self.root / "inventory-runs"
        inventory.write_text(f"schema_version: 1\nrun_root: {run_root}\nhosts:\n  fixture:\n    ssh_host: local\n")
        route = {**self.route, "route_id": "rt-implicit", "route_hash": "sha256:" + "a" * 64,
                 "artifact_root": str(self.artifact), "work_request": {"text": "Read-only GPU query"}}
        with mock.patch.dict(os.environ, {"COMPUTE_HOSTS_CONFIG": str(inventory)}):
            prepared = E.prepare_task_request(route, self.state / "jobs.log")
            request = E.load_request(prepared, context=self.context)
            self.assertEqual(request.enforcement_required, "any")
            self.assertEqual(request.writable_roots, (run_root,))
            grant = E.build_grant(request, runtime="codex-exec", effective_sandbox="danger-full-access", gpu_resource_scope=True)
            self.assertEqual(grant.file_enforcement, "none")
            self.request(enforcement_required="os-sandbox")
            original = self.request_file.read_bytes()
            route = {**route, "route_id": "rt-explicit", "route_hash": "sha256:" + "b" * 64}
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file)}):
                prepared = E.prepare_task_request(route, self.state / "jobs.log")
            strict = E.load_request(prepared, context=self.context)
            self.assertEqual(strict.enforcement_required, "os-sandbox")
            self.assertEqual(self.request_file.read_bytes(), original)
            with self.assertRaises(E.ExecutionAccessError):
                E.build_grant(strict, runtime="codex-exec", effective_sandbox="danger-full-access", gpu_resource_scope=True)
        self.assertFalse(run_root.exists())

    def test_strict_readonly_general_full_and_parent_outside_still_refuse(self):
        for sandbox, scoped, enforcement in (("danger-full-access", True, "os-sandbox"),
                ("read-only", True, "any"), ("danger-full-access", False, "any")):
            with self.assertRaises(E.ExecutionAccessError):
                E.build_grant(self.request(enforcement_required=enforcement), runtime="codex-exec",
                              effective_sandbox=sandbox, gpu_resource_scope=scoped)
        request = self.request()
        with self.assertRaisesRegex(E.ExecutionAccessError, "execution-access-exceeds-parent"):
            E.bind_request(str(self.request_file), environ=self.env, context=self.context,
                is_child=True, parent=E.ParentGrant(writable_roots=(self.worktree,)), runtime="codex-exec",
                effective_sandbox="danger-full-access", gpu_resource_scope=True)
        request = self.request(network={"required": True, "reason": "approved transfer", "hosts": []})
        with self.assertRaisesRegex(E.ExecutionAccessError, "execution-access-exceeds-parent:network"):
            E.assert_within_parent(request, E.ParentGrant(writable_roots=(self.data,), network_allowed=False), is_child=True)

    def test_compose_and_applied_receipts_disclose_enforcement(self):
        self.route["work_request"] = {"owner_harness": "codex"}
        for applied in (False, True):
            rows = A.advisories(self.route, owner_harness="codex",
                sandbox="danger-full-access" if applied else None, gpu_selection=G.select(self.route))
            row = next(r for r in rows if r["code"] == G.RECEIPT_CODE)
            self.assertIn("OS enforcement 없음", row["message"])
            self.assertEqual(row["file_enforcement"], "none")
            self.assertEqual(A.receipt_advisories(A.RECEIPT_KEY + json.dumps(row)), [row])


if __name__ == "__main__":
    unittest.main()
