#!/usr/bin/env python3
import argparse,importlib.util,io,json,os,shutil,subprocess,sys,tempfile,types,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[3]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
WH_S=importlib.util.spec_from_file_location("claude_dispatch_headless",Path(__file__).with_name("dispatch-headless.py")); WH=importlib.util.module_from_spec(WH_S); WH_S.loader.exec_module(WH)
from dispatch_contract import ROUTE_IDENTITY_METADATA_KEYS


def isolated_dispatch_env(**updates):
    inherited = {
        key: value for key, value in os.environ.items()
        if key not in {"AGENT_DISPATCH_JOBS", "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_MODEL_GOVERNOR_ROOT"}
        and not key.startswith(("AGENT_ROUTE_", "AGENT_OWNER_ROUTE_"))
    }
    inherited.update(updates)
    return inherited


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


class ClaudeSD45InternalProbe(unittest.TestCase):
    def test_absent_evidence_binds_supported_and_marks_internal(self):
        args = probe_args()
        row = dict(parent_harness="claude", parent_transport="headless", parent_sandbox="default",
                   child_harness="claude", launch_authority="conductor", status="supported",
                   probe_source="direct-command-check", failure_class="")
        with mock.patch.object(WH.subprocess, "run", return_value=fake_probe_result(**row)) as run:
            WH.bind_internal_eligibility_probe(args)
        run.assert_called_once()
        self.assertIn("--child-harness", run.call_args.args[0])
        self.assertEqual(args.nested_eligibility, "supported")
        self.assertEqual(args.eligibility_source, "direct-command-check")
        self.assertEqual(args.eligibility_probe, "internal")
        WH.validate_nested_eligibility(
            dispatch_depth=args.dispatch_depth, action=args.action, parent_harness=args.parent_harness,
            parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
            child_harness="claude", launch_authority=args.launch_authority,
            status=args.nested_eligibility, source=args.eligibility_source,
        )  # must not raise

    def test_unsupported_probe_result_fails_closed_with_no_launch(self):
        args = probe_args()
        row = dict(parent_harness="claude", parent_transport="headless", parent_sandbox="default",
                   child_harness="claude", launch_authority="conductor", status="unsupported",
                   probe_source="direct-auth-check", failure_class="auth-unavailable")
        with mock.patch.object(WH.subprocess, "run", return_value=fake_probe_result(**row)):
            WH.bind_internal_eligibility_probe(args)
        self.assertEqual(args.nested_eligibility, "unsupported")
        self.assertEqual(args.eligibility_probe, "internal")
        with self.assertRaises(WH.DispatchContractError) as ctx:
            WH.validate_nested_eligibility(
                dispatch_depth=args.dispatch_depth, action=args.action, parent_harness=args.parent_harness,
                parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
                child_harness="claude", launch_authority=args.launch_authority,
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
        args = probe_args(parent_transport="unknown")
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
                   child_harness="claude", launch_authority="conductor", status="supported",
                   probe_source="direct-command-check", failure_class="")
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


# Same reason as utilities/dispatch_owner.test.py: the selector refuses
# (`claude-session-resume-indeterminate`, exit 69) when `claude` is absent
# rather than guess, and CI has no such binary.
CLAUDE_CLI = shutil.which("claude")
NEEDS_CLAUDE_CLI = "no claude binary: the selector's session-resume probe cannot be proven here"


class ClaudeSD45(unittest.TestCase):
 @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
 def test_route_consumer_and_missing_evidence_refusal(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td); repo=base/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True); subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True); (repo/"x").write_text("x"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","init"],check=True)
   art=base/".agent_reports"; art.mkdir(); jobs=base/"jobs.log"; logs=base/"logs"; gate={"spec_read":{"satisfied":True,"source":"claude-fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"claude-fixture"}}
   dispatch={"tuples":[{"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"adapter-default","child_harness":"claude","launch_authority":"conductor","status":"supported","probe_source":"claude-fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(repo.resolve()),"failure_scope":"none","codex_command":"not-applicable","retry_on_isolated_worktree":0}],"native_subagent":[]}
   with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_ARTIFACT_ROOT":str(art)},clear=False):
    route=R.compile_route("autopilot-code","dev","strong",repo,art,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch)
   path=base/"route.json"; path.write_text(json.dumps(route)); node=next(x for x in route["nodes"] if x["id"]=="execute")
   parent=subprocess.Popen(["sleep","60"]);self.addCleanup(parent.wait);self.addCleanup(parent.kill);parent_start=(Path("/proc")/str(parent.pid)/"stat").read_text().split()[21];jobs.write_text(f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\tattempt_schema_version=2,dispatch_depth=1,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,worker_type=owner,harness=claude,runtime_sandbox=adapter-default,attempt_id=att-sd45-parent,pid={parent.pid},pid_start={parent_start}\n")
   args=[sys.executable,str(ROOT/"adapters/claude/bin/dispatch-headless.py"),"--register","--worktree",str(repo),"--slug","claude-sd45","--capability","autopilot-code","--capability-mode","dev","--worker-mode",node["unit"],"--qa","standard","--intensity","strong","--dispatch-depth","2","--parent","owner","--parent-harness","claude","--parent-transport","headless","--parent-sandbox","adapter-default","--nested-eligibility","supported","--eligibility-source","claude-fixture","--fallback-ordinal","1","--route-file",str(path),"--route-id",route["route_id"],"--route-hash",route["route_hash"],"--route-node","execute","--unit",node["unit"],"--registry-digest",route["registry_digest"],"--write-scope",";".join(node["write_scope"]),"--completion-gate",node["completion_gate"],"--model-role",node["role"],"--model-profile",node["model_profile"],"--jobs",str(jobs),"--log-dir",str(logs)]
   env=isolated_dispatch_env(AGENT_HOME=str(ROOT),AGENT_ARTIFACT_ROOT=str(art),AGENT_DISPATCH_JOBS=str(jobs),AGENT_DISPATCH_ATTEMPT_ID="att-sd45-parent"); ok=subprocess.run(args,text=True,capture_output=True,env=env); self.assertEqual(ok.returncode,0,ok.stdout+ok.stderr); output=dict(line.split("=",1) for line in ok.stdout.splitlines() if "=" in line); prompt=Path(output["prompt_file"]).read_text(); self.assertIn("consume the immutable record",prompt); self.assertNotIn("status -> prompt-signal -> mode -> route\n",prompt); self.assertIn("async_wait_policy=deny-proven",jobs.read_text()); self.assertIn(f"unit={node['unit']}",jobs.read_text()); self.assertIn(f"unit={node['unit']}",ok.stdout)
   broken=json.loads(path.read_text()); del broken["tracked_gate_evidence"]; broken["route_hash"]=R.route_hash(broken); broken["route_id"]="rt-"+broken["route_hash"].split(":",1)[1][:16]; path.write_text(json.dumps(broken)); bad=args.copy(); bad[bad.index(route["route_id"])]=broken["route_id"]; bad[bad.index(route["route_hash"])]=broken["route_hash"]; denied=subprocess.run(bad,text=True,capture_output=True,env=env); self.assertEqual(denied.returncode,65); self.assertIn("tracked gate evidence",denied.stderr)
   legacy=[sys.executable,str(ROOT/"adapters/claude/bin/dispatch-headless.py"),"--dry-run","--worktree",str(repo),"--slug","claude-legacy-scope","--capability","autopilot-code","--mode","dev","--qa","standard","--write-scope","source/**","--model","claude-test","--effort","low"]
   compatible=subprocess.run(legacy,text=True,capture_output=True,env=env); self.assertEqual(compatible.returncode,0,compatible.stderr); self.assertIn("status=dry-run",compatible.stdout)

 def test_w1c_leg_class_projection(self):
  with tempfile.TemporaryDirectory() as td:
   route_file=Path(td)/"route.json"
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


def _shell_command_args(**overrides):
    base = dict(
        worker_type="owner", intensity="strong", artifact_root="/tmp/fixture-artifacts",
        worktree="/tmp/fixture-worktree",
        agent_home=Path("/tmp/fixture-agent-home"),
        jobs_path=Path("/tmp/jobs.log"),
        completion_gate=None, assigned_contract=None, unit=None,
        capability_mode="dev", worker_mode=None, mode=None,
        resolved_model_settings={"source": "inherit", "role": "-", "model": None, "effort": None},
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class ClaudeSD78CompletionDelivery(unittest.TestCase):
    """SD-78 completion resume plus deterministic async-tool denial."""

    def test_owner_standard_plus_gets_exactly_the_proven_names_never_bash(self):
        for intensity in ("standard", "strong", "thorough", "adversarial"):
            with self.subTest(intensity=intensity):
                args = _shell_command_args(intensity=intensity)
                deny = WH._async_deny_tools(args)
                self.assertEqual(deny, WH.PROVEN_ASYNC_DENY)
                self.assertNotIn("Bash", deny)
                command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
                self.assertIn("--disallowedTools", command)
                for name in WH.PROVEN_ASYNC_DENY:
                    self.assertIn(name, command)
                self.assertNotIn("--disallowedTools Bash", command)

    def test_lab_shell_projects_configured_report_bundle_root(self):
        args = _shell_command_args(
            capability="autopilot-lab",
            report_bundle_root=Path("/tmp/fixture-report-bundles"),
        )
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("--add-dir /tmp/fixture-report-bundles", command)

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

    def test_stage_direct_and_quick_launches_get_the_same_runtime_deny(self):
        cases = (("stage", "strong"), ("owner", "direct"), ("owner", "quick"))
        for worker_type, intensity in cases:
            with self.subTest(worker_type=worker_type, intensity=intensity):
                args = _shell_command_args(worker_type=worker_type, intensity=intensity)
                self.assertEqual(WH._async_deny_tools(args), WH.PROVEN_ASYNC_DENY)
                command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
                self.assertIn("--disallowedTools", command)
                self.assertNotIn("Bash", WH._async_deny_tools(args))
                self.assertEqual(WH._async_wait_policy(args), "deny-proven")

    def test_empty_proven_names_emits_no_flag(self):
        args = _shell_command_args()
        with mock.patch.object(WH, "PROVEN_ASYNC_DENY", ()):
            self.assertEqual(WH._async_deny_tools(args), ())
            command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
            self.assertNotIn("--disallowedTools", command)
            self.assertEqual(WH._async_wait_policy(args), "unsupported")

    def test_owner_prompt_carries_runtime_join_clause(self):
        args = _shell_command_args()
        args.resolved_completion_delivery = "session-resume-supervised"
        args.route_id = args.route_node = args.attempt_id = None
        args.worker_role = None
        args.profile = None
        args.parent_slug = args.parent_session_id = args.capability_owner = args.owner_harness = None
        args.route_file = None
        args.capability = "autopilot-code"
        args.capability_mode = "dev"; args.worker_mode = None; args.mode = None
        args.qa = "thorough"
        args.dispatch_depth = 1
        task_spec = importlib.util.spec_from_file_location(
            "claude_dispatch_headless_task", Path(WH.__file__).with_name("dispatch-headless.py"))
        with mock.patch.object(WH, "task_prompt", return_value=("do the thing", "cli")):
            prompt, _source = WH.dispatch_prompt(args)
        self.assertTrue(prompt.startswith("Runtime-owned completion join (SD-78):"))
        self.assertIn("same Claude session once", prompt)
        self.assertIn("Do not call dispatch-wait", prompt)
        self.assertIn("a supervised owner yields the current turn", prompt)
        self.assertNotIn("poll in the current turn", prompt)

    def test_supervised_shell_uses_session_bridge_without_no_persistence(self):
        args = _shell_command_args(
            resolved_completion_delivery="session-resume-supervised",
            jobs_path=Path("/tmp/jobs.log"), attempt_id="att-parent",
        )
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("claude-session-supervisor.py", command)
        self.assertNotIn("--no-session-persistence", command)
        self.assertIn("--parent-attempt-id att-parent", command)
        self.assertIn(
            "--state-file /tmp/supervisor-state/att-parent.json",
            command,
        )

    def test_supervised_state_path_rejects_attempt_path_escape(self):
        args = _shell_command_args(attempt_id="att-../../outside")
        with self.assertRaises(WH.DispatchContractError):
            WH.completion_state_path(args)

    def test_auto_prefers_resume_and_forced_unavailable_fails_closed(self):
        # SD-OPEN-63: renamed claude_session_resume_available() -> probe_claude_session_resume(),
        # which now returns a ClaudeResumeProbe record instead of a bare bool.
        args = argparse.Namespace(
            completion_delivery="auto", dispatch_depth=1, worker_type="owner",
            intensity="strong",
        )
        supported = WH.ClaudeResumeProbe("supported", "ok", 0, 21504, ("--resume", "--session-id"), "help-file")
        with mock.patch.object(WH, "probe_claude_session_resume", return_value=supported):
            self.assertEqual(WH.resolve_completion_delivery(args), "session-resume-supervised")
        args.completion_delivery = "supervised"
        args.completion_probe = None
        unsupported = WH.ClaudeResumeProbe("unsupported", "flags-absent-in-complete-help", 0, 21504, (), "help-file")
        with mock.patch.object(WH, "probe_claude_session_resume", return_value=unsupported):
            with self.assertRaises(WH.DispatchContractError):
                WH.resolve_completion_delivery(args)

    def test_stage_prompt_never_carries_the_clause(self):
        args = _shell_command_args(worker_type=None, intensity="strong")
        args.resolved_completion_delivery = "session-resume-supervised"
        args.route_id = "rt-fixture"; args.route_node = "execute"; args.attempt_id = "att-fixture"
        args.worker_role = "code-execute"
        args.profile = None
        args.parent_slug = args.parent_session_id = args.capability_owner = args.owner_harness = None
        args.route_file = None
        args.capability = "autopilot-code"
        args.capability_mode = "dev"; args.worker_mode = None; args.mode = None
        args.qa = "thorough"
        args.dispatch_depth = 2
        with mock.patch.object(WH, "task_prompt", return_value=("do the thing", "cli")):
            prompt, _source = WH.dispatch_prompt(args)
        self.assertNotIn("Runtime-owned completion join", prompt)


class ClaudeChildParentRuntimeDelivery(unittest.TestCase):
    @staticmethod
    def parent_args(**overrides):
        values = dict(
            action="start",
            dispatch_depth=1,
            launch_lifecycle=WH.DETACHED,
            execution_surface="registered-headless",
            registered_worker=1,
            parent_harness="codex",
            parent_session_id="thread-codex-parent",
            parent_slug=None,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_codex_parent_selects_gateway_for_claude_child(self):
        args = self.parent_args()
        binding = object()
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": args.parent_session_id,
                "AGENT_DISPATCH_CHILD": "0",
                "AGENT_CODEX_MANAGED_GATEWAY": "1",
                "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "codex",
            },
            clear=True,
        ), mock.patch.object(
            WH, "probe_managed_codex_parent", return_value=binding
        ):
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(
            args.parent_completion_delivery, WH.MANAGED_PARENT_DELIVERY
        )
        self.assertIs(args.managed_gateway_binding, binding)
        WH.validate_interactive_parent_launch(args)

    def test_unmanaged_codex_parent_is_identified_then_blocked(self):
        args = self.parent_args(
            parent_harness="claude",
            parent_session_id="synthetic",
            parent_slug="synthetic-owner",
        )
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "thread-real",
                "AGENT_DISPATCH_CALLER_HARNESS": "codex",
                "AGENT_DISPATCH_CHILD": "0",
            },
            clear=True,
        ):
            WH._bind_runtime_parent(args)
            WH.bind_parent_completion_delivery(args)
            with self.assertRaises(WH.DispatchContractError) as raised:
                WH.validate_interactive_parent_launch(args)
        self.assertEqual(args.parent_harness, "codex")
        self.assertEqual(args.parent_session_id, "thread-real")
        self.assertEqual(args.parent_completion_delivery, "poll-fallback")
        self.assertEqual(raised.exception.reason, "managed-entry-required")

    def test_low_level_operator_can_explicitly_select_finite_poll_recovery(self):
        args = self.parent_args(
            allow_unmanaged_parent_poll=True,
            parent_completion_delivery="poll-fallback",
        )
        WH.validate_interactive_parent_launch(args)
        self.assertEqual(
            args.parent_completion_reason, "operator-authorized-unmanaged-poll"
        )

    def test_claude_parent_keeps_claude_runtime_wake_adapter(self):
        args = self.parent_args(
            parent_harness="claude",
            parent_session_id="claude-session",
        )
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_CODE_SESSION_ID": "claude-session"},
            clear=True,
        ), mock.patch.object(WH, "probe_managed_codex_parent") as probe:
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(
            args.parent_completion_delivery, "claude-parent-runtime"
        )
        self.assertEqual(
            args.parent_completion_reason, "claude-async-rewake-resume"
        )
        probe.assert_not_called()

    def test_codex_caller_identity_overrides_synthetic_direct_parent(self):
        args = self.parent_args(
            parent_harness="claude",
            parent_session_id="synthetic",
            parent_slug="synthetic-owner",
        )
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "thread-real",
                "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            },
            clear=True,
        ):
            WH._bind_runtime_parent(args)
        self.assertEqual(args.parent_session_id, "thread-real")
        self.assertEqual(args.parent_harness, "codex")
        self.assertIsNone(args.parent_slug)

    def test_claude_caller_identity_overrides_synthetic_codex_parent(self):
        args = self.parent_args(
            parent_harness="codex",
            parent_session_id="synthetic-codex-thread",
            parent_slug="synthetic-codex-owner",
        )
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CODE_SESSION_ID": "claude-session-real",
                "AGENT_DISPATCH_CALLER_HARNESS": "claude",
            },
            clear=True,
        ):
            WH._bind_runtime_parent(args)
            WH.bind_parent_completion_delivery(args)
            WH.validate_interactive_parent_launch(args)
        self.assertEqual(args.parent_harness, "claude")
        self.assertEqual(args.parent_session_id, "claude-session-real")
        self.assertIsNone(args.parent_slug)
        self.assertEqual(args.parent_completion_delivery, "claude-parent-runtime")

    def test_frame_worker_type_takes_the_same_delivery_path_as_owner(self):
        # W2 (frame-bootstrap-layer, 2026-09-10): resolve_parent_completion_delivery
        # is verified not to read args.worker_type at all -- only action/
        # dispatch_depth/execution_surface/registered_worker/parent identity
        # decide the branch. This proves it by calling the real function with
        # worker_type=frame and worker_type=owner/review and asserting they
        # land on the exact same delivery kind and reason.
        with mock.patch.dict(
            os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-session"}, clear=True,
        ), mock.patch.object(WH, "probe_managed_codex_parent") as probe:
            owner_args = self.parent_args(
                parent_harness="claude", parent_session_id="claude-session", worker_type="owner",
            )
            frame_args = self.parent_args(
                parent_harness="claude", parent_session_id="claude-session", worker_type="frame",
            )
            review_args = self.parent_args(
                parent_harness="claude", parent_session_id="claude-session", worker_type="review",
            )
            WH.bind_parent_completion_delivery(owner_args)
            WH.bind_parent_completion_delivery(frame_args)
            WH.bind_parent_completion_delivery(review_args)
        self.assertEqual(frame_args.parent_completion_delivery, owner_args.parent_completion_delivery)
        self.assertEqual(frame_args.parent_completion_delivery, review_args.parent_completion_delivery)
        self.assertEqual(frame_args.parent_completion_delivery, "claude-parent-runtime")
        self.assertEqual(frame_args.parent_completion_reason, owner_args.parent_completion_reason)
        probe.assert_not_called()

    def test_depth_two_child_never_uses_root_gateway(self):
        args = self.parent_args(
            dispatch_depth=2,
            parent_harness="codex",
            parent_session_id="thread-codex-parent",
        )
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "thread-codex-parent",
                "AGENT_CODEX_MANAGED_GATEWAY": "1",
            },
            clear=True,
        ), mock.patch.object(WH, "probe_managed_codex_parent") as probe:
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(
            args.parent_completion_delivery, "parent-runtime-supervised"
        )
        probe.assert_not_called()


class ClaudeLaunchFenceFailure(unittest.TestCase):
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
            (
                {
                    "schema_version": 1,
                    "reason": "launch-runtime-root-mismatch",
                    "detail": "sealed root drifted",
                },
                True,
            ),
        )

    def test_open_write_end_reports_fence_not_released(self):
        # The write end is still open and nothing has been written yet --
        # the non-blocking read must hit BlockingIOError, proving the fence
        # was genuinely never released (no payload can have executed).
        read_fd, write_fd = os.pipe()
        try:
            self.assertEqual(
                WH.read_launch_fence_failure(read_fd), (None, False)
            )
        finally:
            os.close(write_fd)

    def test_closed_write_end_with_no_payload_reports_fence_released(self):
        # An EOF read (write end already closed, nothing written) proves the
        # fence was released with no failure payload.
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        self.assertEqual(WH.read_launch_fence_failure(read_fd), (None, True))


class ForegroundReviewStartPathTest(unittest.TestCase):
    def _owner_route_fixture(self, worktree, artifacts):
        gate = {
            "spec_read": {"satisfied": True, "source": "claude-fixture"},
            "drift_verdict": "within-spec",
            "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "claude-fixture"},
        }
        dispatch = {"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless",
            "parent_sandbox": "adapter-default", "child_harness": "claude",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "claude-fixture", "probe_time": "2026-07-16T00:00:00Z",
            "failure_class": "", "checked_worktree": str(worktree.resolve()),
            "failure_scope": "none", "codex_command": "not-applicable",
            "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(artifacts.parent / "jobs.log")}, clear=False):
            route = R.compile_route(
                "autopilot-code", "debug", "standard", worktree, artifacts,
                signals=["shared-contract"], transport="headless", tracking="tracked",
                tracked_gate_evidence=gate, dispatch_evidence=dispatch,
            )
        path = artifacts / "owner-route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return path, route

    def test_real_start_waits_seals_then_launches_reaper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); worktree = root / "worktree"; worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
            (worktree / "README").write_text("isolated\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "fixture"], check=True)
            jobs = root / "jobs.log"; artifacts = root / "artifacts"; artifacts.mkdir()
            environment = isolated_dispatch_env(
                AGENT_DISPATCH_JOBS=str(jobs), AGENT_DISPATCH_PARENT_SESSION_ID="session-test",
                AGENT_DISPATCH_CURRENT_HARNESS="claude", AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                AGENT_DISPATCH_CURRENT_SANDBOX="default", AGENT_DISPATCH_CALLER_HARNESS="claude",
                AGENT_DISPATCH_OWNER_HARNESS="claude", CODEX_THREAD_ID="", CODEX_SESSION_ID="",
            )
            order = []
            seal_token = object()
            def sidecar(args, _jobs):
                args.managed_sidecar_state = "not-started"
                args.managed_sidecar_reason = args.managed_sidecar_pid = "-"
                args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            def wait(*_args, **_kwargs):
                order.append("wait")
                return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)
            def seal(*args, **kwargs):
                order.append("seal")
                self.assertEqual(kwargs, {"exit_code": 0, "failure": "", "group_empty": True})
                metadata = next(
                    WH.parse_registry_metadata(line.split("\t", 5)[5])
                    for line in jobs.read_text(encoding="utf-8").splitlines()
                    if "attempt_id=att-claude-foreground" in line
                )
                self.assertEqual(args[1], metadata["attempt_id"])
                self.assertEqual(str(args[2]), metadata["pid"])
                self.assertEqual(str(args[3]), metadata["pid_start"])
                self.assertEqual(str(args[4]), metadata["pgid"])
                return seal_token
            def reap(*args, **kwargs):
                order.append("reap")
                self.assertIs(kwargs["foreground_seal"], seal_token)
                self.assertEqual(args[1], "att-claude-foreground")
                return 4242
            real_annotate = WH.annotate_attempt_row
            def annotate(jobs_path, attempt_id, values):
                if "reap_watch" in values:
                    order.append("annotate")
                return real_annotate(jobs_path, attempt_id, values)
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(WH, "resolve_artifact_root", return_value=str(artifacts)), \
                    mock.patch.object(WH.shutil, "which", return_value="/bin/runtime"), \
                    mock.patch.object(WH, "launch_parent_completion_sidecar", side_effect=sidecar), \
                    mock.patch.object(WH, "attach_summary_owner", return_value={}), \
                    mock.patch.object(WH, "shell_command", return_value="true"), \
                    mock.patch.object(WH, "wait_governor_reservation_claim", return_value={}), \
                    mock.patch.object(WH, "wait_foreground", side_effect=wait), \
                    mock.patch.object(WH, "seal_foreground_result", side_effect=seal) as seal_call, \
                    mock.patch.object(WH, "launch_reap_watch", side_effect=reap) as reap_call, \
                    mock.patch.object(WH, "annotate_attempt_row", side_effect=annotate), \
                    mock.patch.object(WH.subprocess, "check_output", wraps=WH.subprocess.check_output) as git_read:
                result = WH.main([
                    "dispatch-headless.py", "--start", "--worktree", str(worktree), "--jobs", str(jobs),
                    "--slug", "review", "--capability", "autopilot-code", "--capability-mode", "debug",
                    "--worker-mode", "dev/backend", "--worker-type", "review", "--launch-lifecycle", "foreground-scoped",
                    "--model", "test", "--effort", "low", "--completion-delivery", "poll",
                    "--attempt-id", "att-claude-foreground",
                ])
            self.assertEqual(result, 0)
            self.assertEqual(order, ["wait", "seal", "reap", "annotate"])
            seal_call.assert_called_once(); reap_call.assert_called_once()
            self.assertIs(reap_call.call_args.kwargs["foreground_seal"], seal_token)
            self.assertFalse(any("HEAD" in " ".join(map(str, call.args[0])) for call in git_read.call_args_list))
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("reap_watch=post-exit,reap_watch_pid=4242", row)

    def test_commit_failure_preserves_common_outcome_and_closes_exact_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); worktree = root / "worktree"; worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
            (worktree / "README").write_text("isolated\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "fixture"], check=True)
            jobs = root / "jobs.log"; artifacts = root / "artifacts"; artifacts.mkdir()
            environment = isolated_dispatch_env(
                AGENT_DISPATCH_JOBS=str(jobs), AGENT_DISPATCH_PARENT_SESSION_ID="session-test",
                AGENT_DISPATCH_CURRENT_HARNESS="claude", AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                AGENT_DISPATCH_CURRENT_SANDBOX="default", AGENT_DISPATCH_CALLER_HARNESS="claude",
                AGENT_DISPATCH_OWNER_HARNESS="claude", CODEX_THREAD_ID="", CODEX_SESSION_ID="",
            )
            order = []
            seal_token = object()
            def sidecar(args, _jobs):
                args.managed_sidecar_state = "not-started"
                args.managed_sidecar_reason = args.managed_sidecar_pid = "-"
                args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            def wait(*_args, **_kwargs):
                order.append("wait")
                return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)
            def seal(*args, **kwargs):
                order.append("seal")
                self.assertEqual(kwargs, {"exit_code": 0, "failure": "", "group_empty": True})
                metadata = next(
                    WH.parse_registry_metadata(line.split("\t", 5)[5])
                    for line in jobs.read_text(encoding="utf-8").splitlines()
                    if "attempt_id=att-claude-foreground" in line
                )
                self.assertEqual(args[1], metadata["attempt_id"])
                self.assertEqual(str(args[2]), metadata["pid"])
                self.assertEqual(str(args[3]), metadata["pid_start"])
                self.assertEqual(str(args[4]), metadata["pgid"])
                return seal_token
            def reap(*args, **kwargs):
                order.append("reap")
                self.assertIs(kwargs["foreground_seal"], seal_token)
                self.assertEqual(args[1], "att-claude-foreground")
                return 4242
            real_annotate = WH.annotate_attempt_row
            def annotate(jobs_path, attempt_id, values):
                if "reap_watch" in values:
                    order.append("annotate")
                return real_annotate(jobs_path, attempt_id, values)
            from dispatch_contract import PostClaimAdmission, ReviewAdmissionCleanup
            cleanup = ReviewAdmissionCleanup(
                watchdog_group="empty", fenced_child_group="empty", readiness="closed-removed",
                review_lease="released", governed_witness="unlocked",
                payload_marker="may-have-started", status="verified-post-release-reaped")
            admission = PostClaimAdmission({"review_admission": "prepared"},
                abort=lambda _reason: cleanup,
                commit=lambda: (_ for _ in ()).throw(OSError("commit-close-fault")))
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(WH, "acquire_review_lease_after_claim", return_value=admission), \
                    mock.patch.object(WH, "cancel_governor_reservation", wraps=WH.cancel_governor_reservation) as cancel, \
                    mock.patch.object(WH, "resolve_artifact_root", return_value=str(artifacts)), \
                    mock.patch.object(WH.shutil, "which", return_value="/bin/runtime"), \
                    mock.patch.object(WH, "launch_parent_completion_sidecar", side_effect=sidecar), \
                    mock.patch.object(WH, "attach_summary_owner", return_value={}), \
                    mock.patch.object(WH, "shell_command", return_value="true"), \
                    mock.patch.object(WH, "wait_governor_reservation_claim", return_value={}), \
                    mock.patch.object(WH, "wait_foreground", side_effect=wait), \
                    mock.patch.object(WH, "seal_foreground_result", side_effect=seal) as seal_call, \
                    mock.patch.object(WH, "launch_reap_watch", side_effect=reap) as reap_call, \
                    mock.patch.object(WH, "annotate_attempt_row", side_effect=annotate), \
                    mock.patch.object(WH.subprocess, "check_output", wraps=WH.subprocess.check_output) as git_read:
                result = WH.main([
                    "dispatch-headless.py", "--start", "--worktree", str(worktree), "--jobs", str(jobs),
                    "--slug", "review", "--capability", "autopilot-code", "--capability-mode", "debug",
                    "--worker-mode", "dev/backend", "--worker-type", "review", "--launch-lifecycle", "foreground-scoped",
                    "--model", "test", "--effort", "low", "--completion-delivery", "poll",
                    "--attempt-id", "att-claude-foreground",
                ])
            self.assertEqual(result, 73)
            seal_call.assert_not_called(); reap_call.assert_not_called()
            cancel.assert_called_once()
            lines = jobs.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            fields = lines[0].split("\t")
            self.assertEqual(fields[1], "done")
            metadata = WH.parse_registry_metadata(fields[5])
            self.assertEqual(metadata["launch_outcome"], "post-release-failed")
            self.assertEqual(metadata["review_admission"], "commit-failed")
            self.assertEqual(metadata["review_admission_cleanup"], "verified-post-release-reaped-v1")

    def test_owner_route_keys_are_real_entry_negatives_before_wait_or_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); worktree = root / "worktree"; worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
            (worktree / "README").write_text("isolated\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "fixture"], check=True)
            jobs = root / "jobs.log"; artifacts = root / "artifacts"; artifacts.mkdir()
            route_file, route = self._owner_route_fixture(worktree, artifacts)
            environment = isolated_dispatch_env(
                AGENT_DISPATCH_JOBS=str(jobs), AGENT_DISPATCH_PARENT_SESSION_ID="session-test",
                AGENT_DISPATCH_CURRENT_HARNESS="claude", AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                AGENT_DISPATCH_CURRENT_SANDBOX="default", AGENT_DISPATCH_CALLER_HARNESS="claude",
                AGENT_DISPATCH_OWNER_HARNESS="claude", CODEX_THREAD_ID="", CODEX_SESSION_ID="",
            )
            for ordinal, key in enumerate(ROUTE_IDENTITY_METADATA_KEYS):
                with self.subTest(key=key):
                    env = dict(environment)
                    extra = []
                    expected_value = str(route_file) if key == "route_file" else "bound"
                    if key == "route_file":
                        extra.extend(["--route-file", str(route_file)])
                    elif key.startswith("owner_route_"):
                        # Exercise the owner-binding seam with exactly one
                        # truthy projected field; the other two remain empty.
                        pass
                    elif key.startswith("batch_"):
                        # The reservation seam supplies one batch identity key,
                        # without a parallel-group tuple or lifecycle override.
                        pass
                    else:
                        extra.extend(["--" + key.replace("_", "-"), "bound"])

                    owner_binding = types.SimpleNamespace(route_file="", route_id="", route_hash="")
                    if key.startswith("owner_route_"):
                        setattr(owner_binding, key.removeprefix("owner_"), "bound")
                    reservation = ({"batch_group": "fixture-group", key: "bound"}
                                   if key.startswith("batch_") else {})

                    real_claim = WH.claim_attempt_row
                    real_append = WH.append_job
                    def append_with_binding(jobs_path, args):
                        if key.startswith("owner_route_"):
                            args.owner_route_binding = owner_binding
                            self.assertEqual(getattr(args.owner_route_binding, key.removeprefix("owner_")), "bound")
                        return real_append(jobs_path, args)
                    def claim_and_check(jobs_path, attempt_id, row, **kwargs):
                        metadata = WH.parse_registry_metadata(row.split("\t", 5)[5])
                        active = [candidate for candidate in ROUTE_IDENTITY_METADATA_KEYS if metadata.get(candidate)]
                        self.assertEqual(active, [key], metadata)
                        self.assertEqual(metadata[key], expected_value)
                        claimed_metadata.clear()
                        claimed_metadata.update(metadata)
                        return real_claim(jobs_path, attempt_id, row, **kwargs)

                    claimed_metadata = {}

                    patches = [
                        mock.patch.object(WH, "validate_route_record", return_value=0),
                        mock.patch.object(WH, "completion_marker_gate"),
                        mock.patch.object(WH, "reconcile_launch_lifecycle", return_value=types.SimpleNamespace(
                            requested="detached", effective="detached", reselection="retained-test-scope",
                            override="absent", metadata=lambda: {},
                        )),
                        mock.patch.object(WH, "claim_attempt_row", side_effect=claim_and_check),
                        mock.patch.object(WH, "append_job", side_effect=append_with_binding),
                        mock.patch.object(WH, "replica_batch_expectation", return_value=None),
                        mock.patch.object(WH, "reserve_governor_token", return_value=("token", reservation)),
                        mock.patch.object(WH, "cancel_governor_reservation"),
                        mock.patch.object(WH, "wait_governor_reservation_claim", return_value={}),
                        mock.patch.object(WH, "wait_foreground"),
                        mock.patch.object(WH, "seal_foreground_result"),
                        mock.patch("model_profile.selection_receipt", return_value={}),
                        # The fixture injects route identity axes independently, not a real frame route.
                        mock.patch.object(WH, "owner_frame_launch_gate"),
                    ]
                    if key == "route_file":
                        patches.append(mock.patch.object(WH, "headless_attempt_policy", return_value={
                            "fallback_hop": "same-harness-headless",
                            "fallback_ordinal": 0,
                            "quick": False,
                            "terminal_attempt_limit": None,
                            "replacement_attempt_limit": 0,
                            "replacement_notes": frozenset(),
                        }))
                    if key.startswith("owner_route_"):
                        patches.extend([
                            mock.patch.object(WH, "binding_from_environment", return_value=owner_binding),
                            mock.patch.object(WH, "owner_binding_tuple_failure_fields", return_value={}),
                        ])
                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
                        stack.enter_context(mock.patch.object(WH, "resolve_artifact_root", return_value=str(artifacts)))
                        stack.enter_context(mock.patch.object(WH.shutil, "which", return_value="/bin/runtime"))
                        stack.enter_context(mock.patch.object(WH, "attach_summary_owner", return_value={}))
                        stack.enter_context(mock.patch.object(WH, "shell_command", return_value="true"))
                        entered = [stack.enter_context(patcher) for patcher in patches]
                        watcher_call = stack.enter_context(mock.patch.object(WH, "launch_reap_watch"))
                        result = WH.main([
                            "dispatch-headless.py", "--start", "--worktree", str(worktree), "--jobs", str(jobs),
                            "--slug", f"review-{ordinal}", "--capability", "autopilot-code", "--capability-mode", "debug",
                            "--worker-mode", "dev/backend", "--worker-type", "review", "--launch-lifecycle", "foreground-scoped",
                            "--model", "test", "--effort", "low", "--completion-delivery", "poll",
                            "--attempt-id", f"att-claude-identity-{ordinal}", *extra,
                        ])
                    self.assertEqual(result, 0)
                    self.assertEqual(entered[3].call_count, 1)
                    self.assertEqual(entered[4].call_count, 1)
                    final_metadata = WH.parse_registry_metadata(
                        next(
                            line.split("\t", 5)[5]
                            for line in jobs.read_text(encoding="utf-8").splitlines()
                            if f"attempt_id=att-claude-identity-{ordinal}" in line
                        )
                    )
                    self.assertEqual(
                        [candidate for candidate in ROUTE_IDENTITY_METADATA_KEYS if final_metadata.get(candidate)],
                        [key],
                    )
                    self.assertEqual(final_metadata[key], expected_value)
                    self.assertEqual(entered[9].call_count, 0)
                    self.assertEqual(entered[10].call_count, 0)
                    watcher_call.assert_called_once()
                    watcher_args = watcher_call.call_args.args
                    self.assertEqual(watcher_args[0], jobs)
                    self.assertEqual(watcher_args[1], f"att-claude-identity-{ordinal}")
                    self.assertEqual(str(watcher_args[2]), final_metadata["pid"])
                    self.assertEqual(str(watcher_args[3]), final_metadata["pid_start"])
                    self.assertEqual(str(watcher_args[4]), final_metadata["pgid"])
                    self.assertIsNone(watcher_call.call_args.kwargs.get("foreground_seal"))
                    self.assertIn("reap_watch=post-exit", jobs.read_text(encoding="utf-8"))


class ClaudeHeadlessPermissionPosture(unittest.TestCase):
    """core/OPERATIONS.md §5.10 registered headless permission posture."""

    HARNESS = {"/tmp/fixture-agent-home/utilities/" + n for n in WH.HARNESS_ALLOWLIST_UTILITIES}

    def _ns(self, mode, **overrides):
        base = dict(permission_mode=mode, agent_home=Path("/tmp/fixture-agent-home"),
                    worktree="/tmp/fixture-worktree", artifact_root="/tmp/fixture-artifacts",
                    worker_type="stage")
        base.update(overrides)
        return argparse.Namespace(**base)

    def _resolve(self, mode, env=None, **overrides):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": td, **(env or {})}), \
             mock.patch.object(WH, "_MANAGED_SETTINGS_PATHS", ()):
            return WH.resolve_permission_posture(self._ns(mode, **overrides))

    def _harness_rules(self, rules):
        return {r.split("/utilities/")[1].split(" ")[0].rstrip(")") for r in rules if "/utilities/" in r}

    def test_bypass_posture_pins_the_start_mode_and_still_carries_the_allow_rules(self):
        posture = self._resolve("bypass")
        self.assertEqual((posture["mode"], posture["mode_flag"]), ("bypass", "bypassPermissions"))
        self.assertTrue(posture["allowed_tools"])           # M5: rules ride along under bypass
        args = _shell_command_args(resolved_permission_posture=posture)
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("--permission-mode bypassPermissions", command)
        self.assertIn("--allowedTools", command)
        self.assertIn("--disallowedTools", command)        # SD-71/78 deny applies in every posture

    def test_allowlist_posture_pins_accept_edits_and_grants_what_a_worker_needs(self):
        posture = self._resolve("allowlist")
        self.assertEqual((posture["mode"], posture["reason"], posture["mode_flag"]),
                         ("allowlist", "launch-explicit", "acceptEdits"))
        rules = posture["allowed_tools"]
        self.assertEqual(self._harness_rules(rules), set(WH.HARNESS_ALLOWLIST_UTILITIES))
        self.assertIn("Edit(//tmp/fixture-worktree/**)", rules)        # Edit also governs Write
        self.assertIn("Edit(//tmp/fixture-artifacts/**)", rules)
        self.assertIn("Bash(git status *)", rules)
        self.assertIn("Bash(python3 -m unittest *)", rules)
        self.assertNotIn("Bash", rules)                                 # never a bare Bash grant
        self.assertNotIn("Bash(git commit *)", rules)                   # stage workers are no-commit
        self.assertFalse([r for r in rules if r.startswith("Bash(git push")])
        owner = self._resolve("allowlist", worker_type="owner")
        self.assertIn("Bash(git commit *)", owner["allowed_tools"])     # SD-69 commit-expected owner
        args = _shell_command_args(resolved_permission_posture=posture)
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("--permission-mode acceptEdits", command)
        self.assertNotIn("bypassPermissions", command)
        self.assertIn("--allowedTools", command)
        self.assertIn("capability-route.py", command)

    def test_supervised_turn_carries_the_posture_to_the_session_bridge(self):
        for mode, flag in (("bypass", "--permission-mode bypassPermissions"),
                           ("allowlist", "--permission-mode acceptEdits")):
            with self.subTest(mode=mode):
                posture = self._resolve(mode)
                args = _shell_command_args(
                    resolved_completion_delivery="session-resume-supervised",
                    attempt_id="att-fixture", resolved_permission_posture=posture,
                )
                command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
                self.assertIn("claude-session-supervisor.py", command)
                self.assertIn(flag, command)
                self.assertIn("--allowed-tool", command)

    def test_config_default_is_bypass_from_the_shipped_profile(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root demotes bypass by contract; covered separately")
        shipped = str(WH.ROOT / "profiles" / "dispatch-defaults.yaml")
        posture = self._resolve("config", env={"DISPATCH_DEFAULTS_CONFIG": shipped})
        self.assertEqual((posture["mode"], posture["reason"]), ("bypass", "config"))
        self.assertEqual(posture["inherited_default_mode"], "-")

    def test_config_opt_out_selects_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            shipped = (WH.ROOT / "profiles" / "dispatch-defaults.yaml").read_text(encoding="utf-8")
            user = Path(td) / "dispatch-defaults.yaml"
            user.write_text(shipped.replace("claude_permission_mode: bypass", "claude_permission_mode: allowlist"),
                            encoding="utf-8")
            posture = self._resolve("config", env={"DISPATCH_DEFAULTS_CONFIG": str(user)})
        self.assertEqual((posture["mode"], posture["reason"]), ("allowlist", "config"))

    def test_invalid_config_falls_back_to_the_shipped_default_with_a_typed_reason(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root demotes bypass by contract; covered separately")
        with tempfile.TemporaryDirectory() as td:
            broken = Path(td) / "dispatch-defaults.yaml"
            broken.write_text("schema_version: 3\nheadless: nonsense\n", encoding="utf-8")
            posture = self._resolve("config", env={"DISPATCH_DEFAULTS_CONFIG": str(broken)})
        self.assertEqual((posture["mode"], posture["reason"]), ("bypass", "config-invalid-shipped-default"))

    def test_m5_disable_bypass_is_honoured_from_every_settings_scope(self):
        disable = json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}})
        for scope in ("user", "project", "project-local"):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as cfg, \
                 tempfile.TemporaryDirectory() as wt, \
                 mock.patch.object(WH, "_MANAGED_SETTINGS_PATHS", ()):
                target = {
                    "user": Path(cfg) / "settings.json",
                    "project": Path(wt) / ".claude" / "settings.json",
                    "project-local": Path(wt) / ".claude" / "settings.local.json",
                }[scope]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(disable, encoding="utf-8")
                with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                    posture = WH.resolve_permission_posture(self._ns("bypass", worktree=wt))
                self.assertEqual((posture["mode"], posture["reason"], posture["mode_flag"]),
                                 ("allowlist", "settings-disable-bypass", "acceptEdits"))
                self.assertTrue(posture["allowed_tools"])

    def test_m5_managed_scope_is_read_too(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cfg:
            managed = Path(td) / "managed-settings.json"
            managed.write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}}))
            with mock.patch.object(WH, "_MANAGED_SETTINGS_PATHS", (managed,)), \
                 mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                posture = WH.resolve_permission_posture(self._ns("bypass", worktree=cfg))
        self.assertEqual(posture["reason"], "settings-disable-bypass")

    def test_inherited_default_mode_is_recorded_from_the_highest_precedence_scope(self):
        with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as wt, \
             mock.patch.object(WH, "_MANAGED_SETTINGS_PATHS", ()):
            (Path(cfg) / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "auto"}}))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                posture = WH.resolve_permission_posture(self._ns("bypass", worktree=wt))
                self.assertEqual(posture["inherited_default_mode"], "auto")   # the harness template value
                self.assertEqual(posture["mode"], "bypass")                    # the pin still wins
                project = Path(wt) / ".claude"; project.mkdir()
                (project / "settings.local.json").write_text(json.dumps({"permissions": {"defaultMode": "plan"}}))
                posture = WH.resolve_permission_posture(self._ns("bypass", worktree=wt))
                self.assertEqual(posture["inherited_default_mode"], "plan")   # project-local outranks user
        args = _shell_command_args(resolved_permission_posture=posture, capability="autopilot-code")
        self.assertEqual(posture["mode_flag"], "bypassPermissions")

    def test_root_demotes_bypass_to_allowlist(self):
        with mock.patch.object(WH.os, "geteuid", return_value=0, create=True):
            posture = self._resolve("bypass")
        self.assertEqual((posture["mode"], posture["reason"], posture["mode_flag"]),
                         ("allowlist", "root-refuses-bypass", "acceptEdits"))

    def test_wrapper_parser_accepts_the_posture_flag(self):
        parser = WH.parser()
        args = parser.parse_args(["--dry-run", "--worktree", "/tmp/x", "--slug", "s", "--capability", "autopilot-code",
                                  "--permission-mode", "allowlist"])
        self.assertEqual(args.permission_mode, "allowlist")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_DISPATCH_PERMISSION_MODE", None)
            args = WH.parser().parse_args(["--dry-run", "--worktree", "/tmp/x", "--slug", "s", "--capability", "autopilot-code"])
        self.assertEqual(args.permission_mode, "config")

    def test_minor7_foreground_close_seals_the_review_artifact_for_a_finished_review(self):
        terminal = {"failure_class": "fail", "terminal_event": "result", "artifact_path_b64": "abc"}
        sealed = WH._foreground_terminal_evidence(terminal, WH.REVIEW_BLOCKING_NOTE, "/tmp/l.jsonl")
        self.assertEqual(sealed["review_artifact_b64"], "abc")
        self.assertEqual(sealed["failure_class"], "fail")          # verdict axis untouched
        dead = WH._foreground_terminal_evidence(terminal, "dead-worker-fail", "/tmp/l.jsonl")
        self.assertNotIn("review_artifact_b64", dead)


if __name__=="__main__": unittest.main()
