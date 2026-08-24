#!/usr/bin/env python3
import argparse,importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[3]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
WH_S=importlib.util.spec_from_file_location("opencode_dispatch_headless",Path(__file__).with_name("dispatch-headless.py")); WH=importlib.util.module_from_spec(WH_S); WH_S.loader.exec_module(WH)


def probe_args(**overrides):
    base = dict(
        dispatch_depth=2, action="start", nested_eligibility="unknown", eligibility_source="",
        eligibility_failure_class="", parent_harness="claude", parent_transport="headless",
        parent_sandbox="default", launch_authority="conductor", worktree="/tmp/fixture-worktree",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def fake_probe_result(**row):
    return mock.Mock(stdout=json.dumps(row), returncode=0 if row.get("status") == "supported" else 69)


class OpenCodeSD45InternalProbe(unittest.TestCase):
    def test_absent_evidence_binds_supported_and_marks_internal(self):
        args = probe_args()
        row = dict(parent_harness="claude", parent_transport="headless", parent_sandbox="default",
                   child_harness="opencode", launch_authority="conductor", status="supported",
                   probe_source="direct-auth+headless-check", failure_class="")
        with mock.patch.object(WH.subprocess, "run", return_value=fake_probe_result(**row)) as run:
            WH.bind_internal_eligibility_probe(args)
        run.assert_called_once()
        self.assertIn("--child-harness", run.call_args.args[0])
        self.assertEqual(args.nested_eligibility, "supported")
        self.assertEqual(args.eligibility_source, "direct-auth+headless-check")
        self.assertEqual(args.eligibility_probe, "internal")
        WH.validate_nested_eligibility(
            dispatch_depth=args.dispatch_depth, action=args.action, parent_harness=args.parent_harness,
            parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
            child_harness="opencode", launch_authority=args.launch_authority,
            status=args.nested_eligibility, source=args.eligibility_source,
        )  # must not raise

    def test_unsupported_probe_result_fails_closed_with_no_launch(self):
        args = probe_args()
        row = dict(parent_harness="claude", parent_transport="headless", parent_sandbox="default",
                   child_harness="opencode", launch_authority="conductor", status="unsupported",
                   probe_source="direct-headless-check", failure_class="exit-1")
        with mock.patch.object(WH.subprocess, "run", return_value=fake_probe_result(**row)):
            WH.bind_internal_eligibility_probe(args)
        self.assertEqual(args.nested_eligibility, "unsupported")
        self.assertEqual(args.eligibility_probe, "internal")
        with self.assertRaises(WH.DispatchContractError) as ctx:
            WH.validate_nested_eligibility(
                dispatch_depth=args.dispatch_depth, action=args.action, parent_harness=args.parent_harness,
                parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
                child_harness="opencode", launch_authority=args.launch_authority,
                status=args.nested_eligibility, source=args.eligibility_source,
            )
        self.assertEqual(ctx.exception.reason, "nested-child-spawn-unsupported")

    def test_explicit_evidence_skips_internal_probe(self):
        args = probe_args(nested_eligibility="unsupported", eligibility_source="caller-supplied")
        args.eligibility_probe = "-"
        with mock.patch.object(WH.subprocess, "run") as run:
            WH.bind_internal_eligibility_probe(args)
        run.assert_not_called()
        self.assertEqual(args.eligibility_probe, "-")
        self.assertEqual(args.nested_eligibility, "unsupported")

    def test_unknown_parent_identity_skips_probe_and_stays_fail_closed(self):
        args = probe_args(parent_harness="")
        args.eligibility_probe = "-"
        with mock.patch.object(WH.subprocess, "run") as run:
            WH.bind_internal_eligibility_probe(args)
        run.assert_not_called()
        self.assertEqual(args.eligibility_probe, "-")
        self.assertEqual(args.nested_eligibility, "unknown")

    def test_malformed_json_leaves_unknown_and_fails_closed(self):
        args = probe_args()
        with mock.patch.object(WH.subprocess, "run", return_value=mock.Mock(stdout="not json", returncode=1)):
            WH.bind_internal_eligibility_probe(args)
        self.assertEqual(args.nested_eligibility, "unknown")
        self.assertEqual(args.eligibility_probe, "internal")

    def test_identity_mismatched_probe_row_leaves_unknown_and_fails_closed(self):
        args = probe_args()
        row = dict(parent_harness="codex", parent_transport="headless", parent_sandbox="default",
                   child_harness="opencode", launch_authority="conductor", status="supported",
                   probe_source="direct-auth+headless-check", failure_class="")
        with mock.patch.object(WH.subprocess, "run", return_value=fake_probe_result(**row)):
            WH.bind_internal_eligibility_probe(args)
        self.assertEqual(args.nested_eligibility, "unknown")
        self.assertEqual(args.eligibility_probe, "internal")

    def test_depth1_never_probes(self):
        args = probe_args(dispatch_depth=1)
        with mock.patch.object(WH.subprocess, "run") as run:
            WH.bind_internal_eligibility_probe(args)
        run.assert_not_called()

    def test_register_action_never_probes(self):
        args = probe_args(action="register")
        with mock.patch.object(WH.subprocess, "run") as run:
            WH.bind_internal_eligibility_probe(args)
        run.assert_not_called()


class OpenCodeSD45(unittest.TestCase):
 def test_route_consumer_and_capability_reselection_refusal(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td); repo=base/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True); subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True); (repo/"x").write_text("x"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","init"],check=True)
   art=base/".agent_reports"; art.mkdir(); jobs=base/"jobs.log"; logs=base/"logs"; gate={"spec_read":{"satisfied":True,"source":"opencode-fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"opencode-fixture"}}
   dispatch={"tuples":[{"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"adapter-default","child_harness":"opencode","launch_authority":"conductor","status":"supported","probe_source":"opencode-fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(repo.resolve()),"failure_scope":"none","codex_command":"not-applicable","retry_on_isolated_worktree":0}],"native_subagent":[]}
   with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_ARTIFACT_ROOT":str(art)},clear=False):
    route=R.compile_route("autopilot-code","dev","strong",repo,art,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch)
   path=base/"route.json"; path.write_text(json.dumps(route)); node=next(x for x in route["nodes"] if x["id"]=="execute")
   args=[sys.executable,str(ROOT/"adapters/opencode/bin/dispatch-headless.py"),"--register","--worktree",str(repo),"--slug","opencode-sd45","--capability","autopilot-code","--capability-mode","dev","--worker-mode",node["unit"],"--qa","standard","--intensity","strong","--dispatch-depth","2","--parent","owner","--parent-harness","opencode","--parent-transport","headless","--parent-sandbox","adapter-default","--nested-eligibility","supported","--eligibility-source","opencode-fixture","--fallback-ordinal","1","--route-file",str(path),"--route-id",route["route_id"],"--route-hash",route["route_hash"],"--route-node","execute","--unit",node["unit"],"--registry-digest",route["registry_digest"],"--write-scope",";".join(node["write_scope"]),"--completion-gate",node["completion_gate"],"--model-role",node["role"],"--model-profile",node["model_profile"],"--jobs",str(jobs),"--log-dir",str(logs)]
   env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),"AGENT_DISPATCH_JOBS":str(jobs),"OPENCODE_CONFIG_CONTENT":"{}"}
   dry=args.copy(); dry[dry.index("--register")]="--dry-run"; ok=subprocess.run(dry,text=True,capture_output=True,env=env); self.assertEqual(ok.returncode,0,ok.stderr); self.assertIn(f"unit={node['unit']}",ok.stdout)
   blocked=subprocess.run(args,text=True,capture_output=True,env=env); self.assertEqual(blocked.returncode,73); self.assertIn("reason=live-parent-not-found",blocked.stdout+blocked.stderr); self.assertFalse(jobs.exists() and jobs.read_text(encoding="utf-8").strip()); self.assertFalse(logs.exists())
   bad=dry.copy(); bad[bad.index("autopilot-code")]="autopilot-lab"; bad[bad.index("dev")]="eval"; denied=subprocess.run(bad,text=True,capture_output=True,env=env); self.assertEqual(denied.returncode,65); self.assertIn("route-capability-mode-mismatch",denied.stdout+denied.stderr)
   legacy=[sys.executable,str(ROOT/"adapters/opencode/bin/dispatch-headless.py"),"--dry-run","--worktree",str(repo),"--slug","opencode-legacy-scope","--capability","autopilot-code","--mode","dev","--qa","standard","--write-scope","source/**","--model","provider/test","--variant","low"]
   compatible=subprocess.run(legacy,text=True,capture_output=True,env=env); self.assertEqual(compatible.returncode,0,compatible.stderr); self.assertIn("status=dry-run",compatible.stdout)

 def test_w1c_leg_class_projection(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td); route_file=base/"route.json"
   route_file.write_text(json.dumps({"nodes":[
    {"id":"plan","leg_class":"peer","auxiliary_check":None},
    {"id":"plan-simplicity","leg_class":"auxiliary","auxiliary_check":"simplicity-check"},
   ]}))
   peer=argparse.Namespace(route_file=str(route_file),route_node="plan")
   aux=argparse.Namespace(route_file=str(route_file),route_node="plan-simplicity")
   missing=argparse.Namespace(route_file=None,route_node="plan")
   self.assertEqual(WH._route_node_leg_fields(peer),("peer","-"))
   self.assertEqual(WH._route_node_leg_fields(aux),("auxiliary","simplicity-check"))
   self.assertEqual(WH._route_node_leg_fields(missing),("-","-"))
def delivery_args(**overrides):
    base = dict(
        action="start", dispatch_depth=1, launch_lifecycle=WH.DETACHED,
        execution_surface="registered-headless", registered_worker=True,
        parent_session_id="sess-parent-1", parent_harness="claude",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class OpenCodeParentCompletionDelivery(unittest.TestCase):
    # Item 2: opencode's own parent-runtime completion delivery contract,
    # ported from adapters/codex/bin/dispatch-headless.py minus the
    # Codex-only managed single-ingress gateway branch.
    def test_direct_registered_claude_parent_gets_claude_parent_runtime(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = delivery_args()
            self.assertEqual(WH.resolve_parent_completion_delivery(args), "claude-parent-runtime")
            self.assertEqual(args.parent_completion_reason, "claude-async-rewake-resume")

    def test_direct_registered_non_claude_parent_gets_poll_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = delivery_args(parent_harness="opencode")
            self.assertEqual(WH.resolve_parent_completion_delivery(args), "poll-fallback")
            self.assertEqual(args.parent_completion_reason, "parent-identity-unmatched")

    def test_depth_2_child_gets_parent_runtime_supervised(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = delivery_args(dispatch_depth=2)
            self.assertEqual(WH.resolve_parent_completion_delivery(args), "parent-runtime-supervised")
            self.assertEqual(args.parent_completion_reason, "parent-attempt-owned")

    def test_child_process_marker_forces_parent_runtime_supervised(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}, clear=True):
            args = delivery_args()
            self.assertEqual(WH.resolve_parent_completion_delivery(args), "parent-runtime-supervised")

    def test_register_action_stdout_carries_the_delivery_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
            (repo / "x").write_text("x")
            subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
            art = base / ".agent_reports"; art.mkdir()
            jobs = base / "jobs.log"; logs = base / "logs"
            cmd = [
                sys.executable, str(ROOT / "adapters/opencode/bin/dispatch-headless.py"),
                "--dry-run", "--worktree", str(repo), "--slug", "oc-delivery-fixture",
                "--capability", "autopilot-code", "--mode", "dev", "--qa", "standard",
                "--write-scope", "source/**", "--model", "provider/test", "--variant", "low",
                "--parent-session-id", "sess-parent-2", "--parent-harness", "claude",
            ]
            # Filter AGENT_DISPATCH_JOBS and AGENT_MODEL_GOVERNOR_ROOT: an
            # inherited ambient value for either points at this session's
            # real artifact root, not the tmp fixture root, and would fail
            # resolve_model_governor_root's split-brain check unrelated to
            # what this test is checking.
            env = {
                **{
                    k: v for k, v in os.environ.items()
                    if k not in {"AGENT_DISPATCH_JOBS", "AGENT_MODEL_GOVERNOR_ROOT"}
                },
                "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(art),
                "OPENCODE_CONFIG_CONTENT": "{}", "AGENT_DISPATCH_CURRENT_HARNESS": "claude",
            }
            result = subprocess.run(cmd, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("parent_completion_delivery=", result.stdout)
        self.assertIn("parent_completion_reason=", result.stdout)


class OpenCodeParentBindingDryRun(unittest.TestCase):
    # Item 5-1: the exact parent-binding machinery is ported and wired, but
    # unreachable while the dispatch_depth==2 fail-closed gate stays in
    # place (item 5-2/gate lift). At dispatch_depth==1, --parent-attempt-id
    # must at least parse and echo through stdout as "-" (no binding
    # attempted at depth 1) without erroring -- that's what this locks.
    def test_dry_run_accepts_parent_attempt_id_and_echoes_it(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
            (repo / "x").write_text("x")
            subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
            art = base / ".agent_reports"; art.mkdir()
            cmd = [
                sys.executable, str(ROOT / "adapters/opencode/bin/dispatch-headless.py"),
                "--dry-run", "--worktree", str(repo), "--slug", "oc-parent-binding-fixture",
                "--capability", "autopilot-code", "--mode", "dev", "--qa", "standard",
                "--write-scope", "source/**", "--model", "provider/test", "--variant", "low",
                "--parent-attempt-id", "att-parent-fixture-1",
            ]
            env = {
                **{
                    k: v for k, v in os.environ.items()
                    if k not in {"AGENT_DISPATCH_JOBS", "AGENT_MODEL_GOVERNOR_ROOT"}
                },
                "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(art),
                "OPENCODE_CONFIG_CONTENT": "{}",
            }
            result = subprocess.run(cmd, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        # dispatch_depth defaults to 1 here, so no binding is attempted --
        # the parser accepted the flag but there is nothing to resolve yet.
        self.assertIn("parent_attempt_id=-", result.stdout)


class OpenCodePermissionDefault(unittest.TestCase):
    # Item 1(b): headless "ask" auto-rejects and truncates the session; "deny"
    # returns a structured tool error instead. Measured 2026-08-07
    # (dev_logs/section4_permission_deny_experiment.md).
    def test_default_external_directory_rule_is_deny_not_ask(self):
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": ""}, clear=False):
            config = json.loads(WH.scoped_external_directory_config("/tmp/fixture-artifact-root"))
        self.assertEqual(config["permission"]["external_directory"]["*"], "deny")

    def test_explicit_caller_rule_is_preserved(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"external_directory": {"*": "allow"}}})},
            clear=False,
        ):
            config = json.loads(WH.scoped_external_directory_config("/tmp/fixture-artifact-root"))
        self.assertEqual(config["permission"]["external_directory"]["*"], "allow")

    def test_report_bundle_root_is_narrowly_allowed(self):
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": ""}, clear=False):
            config = json.loads(WH.scoped_external_directory_config(
                "/tmp/fixture-artifact-root", "/tmp/fixture-report-bundles"
            ))
        rules = config["permission"]["external_directory"]
        self.assertEqual(rules["/tmp/fixture-report-bundles"], "allow")
        self.assertEqual(rules["/tmp/fixture-report-bundles/**"], "allow")
        self.assertEqual(rules["*"], "deny")

    def test_report_bundle_root_resolver_is_publish_stage_only(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"REPORT_BUNDLE_ROOT": str(Path(td) / "store")}, clear=False,
        ):
            (Path(td) / "store").mkdir(); route = Path(td) / "route.json"
            route.write_text(json.dumps({"capability": "autopilot-lab", "nodes": [{
                "id": "publish", "kind": "capability-owner", "unit": "_kernel/owner",
                "completion_gate": "lab-publish", "dispatch_depth": 1,
            }]}))
            self.assertEqual(WH.resolve_report_bundle_root(str(route), "publish"), Path(td) / "store")
            for node in ("setup", "media", "report", "independent-verify", "sync"):
                with self.subTest(node=node): self.assertIsNone(WH.resolve_report_bundle_root(str(route), node))
            self.assertIsNone(WH.resolve_report_bundle_root(None, "publish"))


class OpenCodeLaunchFenceFailure(unittest.TestCase):
    def test_typed_fence_failure_channel_preserves_root_mismatch(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps({
            "schema_version": 1,
            "reason": "launch-runtime-root-mismatch",
            "detail": "sealed root drifted",
        }).encode("utf-8"))
        os.close(write_fd)
        self.assertEqual(
            WH.read_launch_fence_failure(read_fd),
            {
                "schema_version": 1,
                "reason": "launch-runtime-root-mismatch",
                "detail": "sealed root drifted",
            },
        )


if __name__=="__main__": unittest.main()
