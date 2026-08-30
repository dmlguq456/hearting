#!/usr/bin/env python3
import argparse,importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[3]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
WH_S=importlib.util.spec_from_file_location("claude_dispatch_headless",Path(__file__).with_name("dispatch-headless.py")); WH=importlib.util.module_from_spec(WH_S); WH_S.loader.exec_module(WH)


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


class ClaudeSD45(unittest.TestCase):
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
   env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_ATTEMPT_ID":"att-sd45-parent"}; ok=subprocess.run(args,text=True,capture_output=True,env=env); self.assertEqual(ok.returncode,0,ok.stdout+ok.stderr); output=dict(line.split("=",1) for line in ok.stdout.splitlines() if "=" in line); prompt=Path(output["prompt_file"]).read_text(); self.assertIn("consume the immutable record",prompt); self.assertNotIn("status -> prompt-signal -> mode -> route\n",prompt); self.assertIn("async_wait_policy=deny-proven",jobs.read_text()); self.assertIn(f"unit={node['unit']}",jobs.read_text()); self.assertIn(f"unit={node['unit']}",ok.stdout)
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
        args = argparse.Namespace(
            completion_delivery="auto", dispatch_depth=1, worker_type="owner",
            intensity="strong",
        )
        with mock.patch.object(WH, "claude_session_resume_available", return_value=True):
            self.assertEqual(WH.resolve_completion_delivery(args), "session-resume-supervised")
        args.completion_delivery = "supervised"
        with mock.patch.object(WH, "claude_session_resume_available", return_value=False):
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


if __name__=="__main__": unittest.main()
