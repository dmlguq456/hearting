#!/usr/bin/env python3
import argparse,ast,importlib.util,io,json,os,subprocess,sys,tempfile,types,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[3]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
WH_S=importlib.util.spec_from_file_location("opencode_dispatch_headless",Path(__file__).with_name("dispatch-headless.py")); WH=importlib.util.module_from_spec(WH_S); WH_S.loader.exec_module(WH)
from dispatch_contract import ROUTE_IDENTITY_METADATA_KEYS


class OpenCodeGenerationBindingTest(unittest.TestCase):
    def test_codex_specific_nested_runtime_preparation_is_removed(self):
        self.assertFalse(hasattr(WH, "prepare_nested_runtime"))


def isolated_dispatch_env(**updates):
    inherited = {
        key: value for key, value in os.environ.items()
        if key not in {"AGENT_DISPATCH_JOBS", "AGENT_DISPATCH_ATTEMPT_ID",
                       "AGENT_MODEL_GOVERNOR_ROOT"}
        and not key.startswith(("AGENT_ARTIFACT_", "AGENT_ROUTE_", "AGENT_OWNER_ROUTE_"))
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
   args=[sys.executable,str(ROOT/"adapters/opencode/bin/dispatch-headless.py"),"--register","--worktree",str(repo),"--slug","opencode-sd45","--capability","autopilot-code","--capability-mode","dev","--worker-mode",node["unit"],"--qa","standard","--intensity","strong","--dispatch-depth","2","--parent","owner","--parent-harness","opencode","--parent-transport","headless","--parent-sandbox","adapter-default","--nested-eligibility","supported","--eligibility-source","opencode-fixture","--fallback-ordinal","1","--route-file",str(path),"--route-id",route["route_id"],"--route-hash",route["route_hash"],"--route-node",node["id"],"--unit",node["unit"],"--registry-digest",route["registry_digest"],"--write-scope",";".join(node["write_scope"]),"--completion-gate",node["completion_gate"],"--model-role",node["role"],"--model-profile",node["model_profile"],"--jobs",str(jobs),"--log-dir",str(logs)]
   env=isolated_dispatch_env(AGENT_HOME=str(ROOT),AGENT_ARTIFACT_ROOT=str(art),AGENT_DISPATCH_JOBS=str(jobs),OPENCODE_CONFIG_CONTENT="{}")
   # Dry-run now checks the same predecessor evidence as start. Supply
   # every realized strong-profile review leg through the official writer.
   for predecessor in node["depends_on"]:
    evidence=base/f"{predecessor}.md"; evidence.write_text("fixture review PASS\n")
    completed=subprocess.run([sys.executable,str(ROOT/"utilities/capability-route.py"),"complete","--route",str(path),"--node",predecessor,"--evidence",str(evidence),"--attempt-id",f"att-inline-{predecessor}-fixture","--dispatch-depth","2","--transport","interactive","--execution-surface","inline","--registered-worker","0","--fallback-hop","inline"],text=True,capture_output=True,env=env)
    self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
   dry=args.copy(); dry[dry.index("--register")]="--dry-run"; ok=subprocess.run(dry,text=True,capture_output=True,env=env); self.assertEqual(ok.returncode,0,ok.stdout+ok.stderr); self.assertIn(f"unit={node['unit']}",ok.stdout)
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
    # Parent runtime selects the carrier independently of this child adapter.
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

    def test_depth_2_child_without_controller_gets_checked_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = delivery_args(dispatch_depth=2)
            self.assertEqual(WH.resolve_parent_completion_delivery(args), "poll-fallback")
            self.assertEqual(args.parent_completion_reason, "parent-supervisor-unavailable")

    def test_child_process_marker_does_not_prove_a_controller(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}, clear=True):
            args = delivery_args()
            self.assertEqual(WH.resolve_parent_completion_delivery(args), "poll-fallback")

    def test_frame_worker_type_takes_the_same_delivery_path_as_owner(self):
        # W2 (frame-bootstrap-layer, 2026-09-10): resolve_parent_completion_delivery
        # is verified not to read args.worker_type at all -- only action/
        # dispatch_depth/launch_lifecycle/execution_surface/registered_worker/
        # parent identity decide the branch. This proves it by calling the
        # real function with worker_type=frame and worker_type=owner/review
        # and asserting they land on the exact same delivery kind and reason.
        with mock.patch.dict(os.environ, {}, clear=True):
            owner_args = delivery_args(worker_type="owner")
            frame_args = delivery_args(worker_type="frame")
            review_args = delivery_args(worker_type="review")
            owner_delivery = WH.resolve_parent_completion_delivery(owner_args)
            frame_delivery = WH.resolve_parent_completion_delivery(frame_args)
            review_delivery = WH.resolve_parent_completion_delivery(review_args)
        self.assertEqual(frame_delivery, owner_delivery)
        self.assertEqual(frame_delivery, review_delivery)
        self.assertEqual(frame_delivery, "claude-parent-runtime")
        self.assertEqual(frame_args.parent_completion_reason, owner_args.parent_completion_reason)

    def test_frame_worker_type_under_non_claude_parent_yields_bounded_wait(self):
        # A depth-1 `frame` under an OpenCode depth-0 parent whose runtime does
        # not name the `opencode-turn` carrier (a server that predates it) keeps
        # `parent_next=bounded-wait` with a real `parent_next_command`. Both the
        # delivery resolution and the receipt-line rendering are the real
        # functions, not text parsed off a printed line.
        with mock.patch.dict(os.environ, {}, clear=True):
            args = delivery_args(worker_type="frame", parent_harness="opencode")
            delivery = WH.resolve_parent_completion_delivery(args)
        self.assertEqual(delivery, "poll-fallback")
        self.assertEqual(args.parent_completion_reason, "parent-identity-unmatched")
        lines = WH.parent_next_receipt_lines(delivery, "att-frame-oc-1", agent_home=str(ROOT))
        fields = dict(line.split("=", 1) for line in lines)
        self.assertEqual(fields["parent_next"], "bounded-wait")
        self.assertNotEqual(fields["parent_next_command"], "-")
        self.assertIn("att-frame-oc-1", fields["parent_next_command"])

    def test_an_opencode_parent_with_the_plugin_carrier_ends_its_turn(self):
        env = {"OPENCODE_SESSION_ID": "oc-parent", "AGENT_PARENT_COMPLETION_CARRIER": "opencode-turn:oc-parent"}
        for worker in ("owner", "frame", "review"):
            with self.subTest(worker=worker), mock.patch.dict(os.environ, env, clear=True):
                args = delivery_args(worker_type=worker, parent_harness="opencode", parent_session_id="oc-parent")
                delivery = WH.resolve_parent_completion_delivery(args)
                args.parent_completion_delivery = delivery
                WH.validate_interactive_parent_launch(args)
            self.assertEqual((delivery, args.parent_completion_reason), ("opencode-turn", "opencode-plugin-turn"))
            lines = WH.parent_next_receipt_lines(delivery, "att-oc-1", agent_home=str(ROOT))
            fields = dict(line.split("=", 1) for line in lines)
            self.assertEqual(fields["parent_next"], "end-turn")

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
            env = isolated_dispatch_env(
                AGENT_HOME=str(ROOT), AGENT_ARTIFACT_ROOT=str(art),
                OPENCODE_CONFIG_CONTENT="{}", AGENT_DISPATCH_CURRENT_HARNESS="claude",
            )
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
            env = isolated_dispatch_env(
                AGENT_HOME=str(ROOT), AGENT_ARTIFACT_ROOT=str(art),
                OPENCODE_CONFIG_CONTENT="{}",
            )
            result = subprocess.run(cmd, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        # dispatch_depth defaults to 1 here, so no binding is attempted --
        # the parser accepted the flag but there is nothing to resolve yet.
        self.assertIn("parent_attempt_id=-", result.stdout)


class OpenCodeSupervisedOwner(unittest.TestCase):
    """Every registered depth-1 owner runs under the session supervisor, with or without a sealed route binding."""

    def args(self, **overrides):
        base = dict(
            dispatch_depth=1, worker_type="owner", intensity="quick", attempt_id="att-quick", owner_route_binding=None,
            worktree="/tmp/fixture-worktree", jobs_path=Path("/tmp/jobs.log"), agent="build",
            route_file=None, route_id=None, route_hash=None, route_node=None,
            resolved_model_settings={"source": "profile", "model": "opencode-go/glm-5.3-flash", "variant": "runtime-default"},
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def command(self, **overrides):
        return WH.shell_command(self.args(**overrides), Path("/tmp/p.txt"), Path("/tmp/l.log"))

    def test_selection_covers_bound_and_quick_depth_one_owners_only(self):
        binding = types.SimpleNamespace(route_file="/tmp/b.json", route_id="rt-b", route_hash="sha256:b")
        self.assertTrue(WH._supervised_owner(self.args()))
        self.assertTrue(WH._supervised_owner(self.args(owner_route_binding=binding)))
        self.assertTrue(WH._supervised_owner(self.args(owner_route_binding=binding, intensity="strong")))
        self.assertTrue(WH._supervised_owner(self.args(attempt_id=None)))
        # A standard+ owner without a sealed binding keeps its one-shot launch.
        for overrides in (dict(dispatch_depth=2), dict(worker_type="stage"), dict(worker_type="review", dispatch_depth=2),
                          dict(intensity="standard"), dict(intensity="strong")):
            with self.subTest(**overrides):
                self.assertFalse(WH._supervised_owner(self.args(**overrides)))
                self.assertRegex(self.command(**overrides), r"^(cd -- \S+ && )?opencode run")

    def test_quick_owner_command_is_the_session_supervisor_on_the_same_model(self):
        command = self.command()
        self.assertIn("claude-session-supervisor.py --runtime-harness opencode", command)
        self.assertIn("--parent-attempt-id att-quick", command)
        self.assertIn("--opencode-agent build --model opencode-go/glm-5.3-flash --variant runtime-default", command)
        self.assertNotIn("--route-file", command)
        self.assertNotIn("--enable-terminal-commit", command)

    def test_route_arguments_are_the_binding_or_a_whole_one_shot_tuple(self):
        route = dict(route_file="/tmp/r.json", route_id="rt-1", route_hash="sha256:1", route_node="one-shot")
        self.assertIn("--route-file /tmp/r.json --route-id rt-1 --route-hash sha256:1", self.command(**route))
        for missing in ("route_file", "route_id", "route_hash"):
            self.assertNotIn("--route-", self.command(**{**route, missing: None}))
        self.assertNotIn("--route-", self.command(**{**route, "route_node": "execute"}))
        binding = types.SimpleNamespace(route_file="/tmp/b.json", route_id="rt-b", route_hash="sha256:b")
        self.assertIn("--route-file /tmp/b.json --route-id rt-b --route-hash sha256:b",
                      self.command(owner_route_binding=binding, **route))

    def test_registration_opens_input_only_for_a_supervised_owner(self):
        with mock.patch("dispatch_owner_input.initialize_owner_input") as init:
            WH.initialize_supervised_owner_input(self.args(), Path("/tmp/jobs.log"))
            init.assert_called_once_with(Path("/tmp/jobs.log"), "att-quick", "opencode-next-turn")
        with mock.patch("dispatch_owner_input.initialize_owner_input") as init:
            WH.initialize_supervised_owner_input(self.args(dispatch_depth=2), Path("/tmp/jobs.log"))
            init.assert_not_called()
        with mock.patch("dispatch_owner_input.initialize_owner_input", side_effect=OSError("disk")):
            WH.initialize_supervised_owner_input(self.args(), Path("/tmp/jobs.log"))


class OpenCodePermissionDefault(unittest.TestCase):
    # Item 1(b): headless "ask" auto-rejects and truncates the session; "deny"
    # returns a structured tool error instead. Measured 2026-08-07
    # (dev_logs/section4_permission_deny_experiment.md).
    def setUp(self):
        # The assertions below exercise V1's singular `permission` schema.
        # Select it explicitly instead of inheriting the host CLI version.
        self.runtime = mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"},
                                       clear=False)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

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

    def test_read_roots_are_readable_but_not_editable(self):
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": ""}, clear=False):
            config = json.loads(WH.scoped_external_directory_config(
                "/tmp/fixture-artifact-root", None, (),
                execution_access_read_roots=("/tmp/fixture-read",),
            ))
        rules = config["permission"]["external_directory"]
        self.assertEqual(rules["/tmp/fixture-read"], "allow")
        self.assertEqual(rules["/tmp/fixture-read/**"], "allow")
        self.assertEqual(rules["*"], "deny")
        self.assertIsNone(rules.get("/tmp/fixture-other"))
        edit = config["permission"]["edit"]
        self.assertEqual(edit["/tmp/fixture-read"], "deny")
        self.assertEqual(edit["/tmp/fixture-read/**"], "deny")

    def test_read_root_covered_by_write_side_keeps_its_write_rule(self):
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": ""}, clear=False):
            config = json.loads(WH.scoped_external_directory_config(
                "/tmp/fixture-artifact-root", None, ("/tmp/fixture-read/sub",),
                execution_access_read_roots=("/tmp/fixture-read", "/tmp/fixture-artifact-root"),
            ))
        rules = config["permission"]["external_directory"]
        self.assertEqual(rules["/tmp/fixture-read"], "allow")
        edit = config["permission"]["edit"]
        self.assertEqual(edit["/tmp/fixture-read"], "deny")
        self.assertEqual(edit["/tmp/fixture-read/**"], "deny")
        # The writable child wins over the broader read deny (native last-match).
        self.assertEqual(edit["/tmp/fixture-read/sub"], "allow")
        self.assertEqual(edit["/tmp/fixture-read/sub/**"], "allow")
        # A read root identical to a writable root adds no deny at all.
        self.assertNotIn("/tmp/fixture-artifact-root", edit)

    def test_a_read_root_inside_the_worktree_stays_writable_and_a_holding_one_stays_denied(self):
        # BC: a read root inside the worktree denied the owner's own edits while its grant
        # recorded it writable; a read root holding a writable root keeps its deny (hearting-verify-cc).
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": ""}, clear=False):
            config = json.loads(WH.scoped_external_directory_config(
                "/tmp/fixture-artifact-root", None, ("/data/out",),
                execution_access_read_roots=("/tmp/fixture-wt/docs", "/data"),
                worktree="/tmp/fixture-wt", selected_agent="build",
            ))
        for permission in (config["permission"], config["agent"]["build"]["permission"]):
            edit = permission["edit"]
            for pattern in ("/tmp/fixture-wt/docs", "/tmp/fixture-wt/docs/**", "docs", "docs/**"):
                self.assertNotIn(pattern, edit)
            self.assertEqual(edit["/data/**"], "deny")
            self.assertEqual(permission["external_directory"]["/data/**"], "allow")
        edit = config["permission"]["edit"]
        self.assertEqual((edit["/data/out"], edit["/data/out/**"]), ("allow", "allow"))

    def test_read_roots_overlay_selected_agent_without_new_defaults(self):
        before = {"permission": {"external_directory": {"*": "deny"}, "edit": {"*": "ask"}},
                  "agent": {"build": {"permission": {"bash": "ask"}}}}
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(before)}, clear=False):
            after = json.loads(WH.scoped_external_directory_config(
                "/tmp/fixture-artifact-root", None, ("/tmp/fixture-read/sub",),
                execution_access_read_roots=("/tmp/fixture-read",),
                selected_agent="build",
            ))
        local = after["agent"]["build"]["permission"]
        self.assertEqual(local["external_directory"]["/tmp/fixture-read"], "allow")
        self.assertEqual(local["external_directory"]["/tmp/fixture-read/**"], "allow")
        self.assertEqual(local["edit"]["/tmp/fixture-read"], "deny")
        self.assertEqual(local["edit"]["/tmp/fixture-read/**"], "deny")
        self.assertEqual(local["edit"]["/tmp/fixture-read/sub"], "allow")
        self.assertEqual(local["edit"]["/tmp/fixture-read/sub/**"], "allow")
        self.assertEqual(local["bash"], "ask")
        self.assertNotIn("read", local)

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


class ForegroundReviewStartPathTest(unittest.TestCase):
    def setUp(self):
        from fixtures.opencode_native_context import launch_context
        launch_version = mock.patch.object(
            WH.opencode_runtime, "probe_launch_version", return_value=(2, 0, 32))
        launch_version.start()
        self.addCleanup(launch_version.stop)
        native = mock.patch.object(WH.opencode_context, "prepare_launch_context", side_effect=launch_context)
        native.start()
        self.addCleanup(native.stop)
        # This suite exercises admission and watcher ordering, not a user's
        # installed runtime. Keep that external readiness boundary isolated.
        projection = mock.patch.object(WH, "check_runtime_projection", return_value=0)
        projection.start()
        self.addCleanup(projection.stop)

    def _owner_route_fixture(self, worktree, artifacts):
        gate = {
            "spec_read": {"satisfied": True, "source": "opencode-fixture"},
            "drift_verdict": "within-spec",
            "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "opencode-fixture"},
        }
        dispatch = {"tuples": [{
            "parent_harness": "opencode", "parent_transport": "headless",
            "parent_sandbox": "adapter-default", "child_harness": "opencode",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "opencode-fixture", "probe_time": "2026-07-16T00:00:00Z",
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
            order = []; seal_token = object()
            def seal(*args, **kwargs):
                order.append("seal"); self.assertEqual(kwargs, {"exit_code": 0, "failure": "", "group_empty": True})
                metadata = next(
                    WH.parse_registry_metadata(line.split("\t", 5)[5])
                    for line in jobs.read_text(encoding="utf-8").splitlines()
                    if "attempt_id=att-opencode-foreground" in line
                )
                self.assertEqual(args[1], metadata["attempt_id"])
                self.assertEqual(str(args[2]), metadata["pid"])
                self.assertEqual(str(args[3]), metadata["pid_start"])
                self.assertEqual(str(args[4]), metadata["pgid"])
                return seal_token
            def wait(*_args, **_kwargs):
                order.append("wait"); return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)
            def reap(*args, **kwargs):
                order.append("reap"); self.assertIs(kwargs["foreground_seal"], seal_token)
                self.assertEqual(args[1], "att-opencode-foreground")
                return 4242
            real_annotate = WH.annotate_attempt_row
            def annotate(jobs_path, attempt_id, values):
                if "reap_watch" in values: order.append("annotate")
                return real_annotate(jobs_path, attempt_id, values)
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(WH, "resolve_artifact_root", return_value=str(artifacts)), \
                    mock.patch.object(WH.shutil, "which", return_value="/bin/runtime"), \
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
                    "--model", "test", "--variant", "low",
                    "--attempt-id", "att-opencode-foreground",
                ])
            self.assertEqual(result, 0); self.assertEqual(order, ["wait", "seal", "reap", "annotate"])
            seal_call.assert_called_once(); reap_call.assert_called_once()
            self.assertIs(reap_call.call_args.kwargs["foreground_seal"], seal_token)
            self.assertFalse(any("HEAD" in " ".join(map(str, call.args[0])) for call in git_read.call_args_list))
            self.assertIn("reap_watch=post-exit,reap_watch_pid=4242", jobs.read_text(encoding="utf-8"))

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
            order = []; seal_token = object()
            def seal(*args, **kwargs):
                order.append("seal"); self.assertEqual(kwargs, {"exit_code": 0, "failure": "", "group_empty": True})
                metadata = next(
                    WH.parse_registry_metadata(line.split("\t", 5)[5])
                    for line in jobs.read_text(encoding="utf-8").splitlines()
                    if "attempt_id=att-opencode-foreground" in line
                )
                self.assertEqual(args[1], metadata["attempt_id"])
                self.assertEqual(str(args[2]), metadata["pid"])
                self.assertEqual(str(args[3]), metadata["pid_start"])
                self.assertEqual(str(args[4]), metadata["pgid"])
                return seal_token
            def wait(*_args, **_kwargs):
                order.append("wait"); return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)
            def reap(*args, **kwargs):
                order.append("reap"); self.assertIs(kwargs["foreground_seal"], seal_token)
                self.assertEqual(args[1], "att-opencode-foreground")
                return 4242
            real_annotate = WH.annotate_attempt_row
            def annotate(jobs_path, attempt_id, values):
                if "reap_watch" in values: order.append("annotate")
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
                    "--model", "test", "--variant", "low",
                    "--attempt-id", "att-opencode-foreground",
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
                AGENT_DISPATCH_CURRENT_HARNESS="opencode", AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                AGENT_DISPATCH_CURRENT_SANDBOX="default", AGENT_DISPATCH_CALLER_HARNESS="opencode",
                AGENT_DISPATCH_OWNER_HARNESS="opencode", CODEX_THREAD_ID="", CODEX_SESSION_ID="",
            )
            for ordinal, key in enumerate(ROUTE_IDENTITY_METADATA_KEYS):
                with self.subTest(key=key):
                    env = dict(environment)
                    extra = []
                    expected_value = str(route_file) if key in ("route_file", "owner_route_file") else "bound"
                    if key == "route_file":
                        extra.extend(["--route-file", str(route_file)])
                    elif key.startswith("owner_route_"):
                        pass
                    elif key.startswith("batch_"):
                        pass
                    else:
                        extra.extend(["--" + key.replace("_", "-"), "bound"])
                    owner_binding = types.SimpleNamespace(route_file="", route_id="", route_hash="")
                    if key.startswith("owner_route_"):
                        setattr(owner_binding, key.removeprefix("owner_"), expected_value)
                    reservation = ({"batch_group": "fixture-group", key: "bound"}
                                   if key.startswith("batch_") else {})

                    real_claim = WH.claim_attempt_row
                    real_append = WH.append_job
                    def append_with_binding(jobs_path, args):
                        if key.startswith("owner_route_"):
                            args.owner_route_binding = owner_binding
                        return real_append(jobs_path, args)
                    def claim_and_check(jobs_path, attempt_id, row, **kwargs):
                        metadata = WH.parse_registry_metadata(row.split("\t", 5)[5])
                        active = [candidate for candidate in ROUTE_IDENTITY_METADATA_KEYS if metadata.get(candidate)]
                        self.assertEqual(active, [key])
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
                            "--model", "test", "--variant", "low", "--attempt-id", f"att-opencode-identity-{ordinal}",
                            *extra,
                        ])
                    self.assertEqual(result, 0)
                    self.assertEqual(entered[3].call_count, 1)
                    self.assertEqual(entered[4].call_count, 1)
                    final_metadata = WH.parse_registry_metadata(
                        next(
                            line.split("\t", 5)[5]
                            for line in jobs.read_text(encoding="utf-8").splitlines()
                            if f"attempt_id=att-opencode-identity-{ordinal}" in line
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
                    self.assertEqual(watcher_args[1], f"att-opencode-identity-{ordinal}")
                    self.assertEqual(str(watcher_args[2]), final_metadata["pid"])
                    self.assertEqual(str(watcher_args[3]), final_metadata["pid_start"])
                    self.assertEqual(str(watcher_args[4]), final_metadata["pgid"])
                    self.assertIsNone(watcher_call.call_args.kwargs.get("foreground_seal"))
                    self.assertIn("reap_watch=post-exit", jobs.read_text(encoding="utf-8"))


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


class OpenCodeFrameCapacityDowngrade(unittest.TestCase):
    """D2 end to end on this adapter: argv -> profile -> register -> spawn claim. The flow itself lives
    in utilities/dispatch_replacement.test.py (`FrameCapacityFlow`); here the real wrapper binds it."""
    @staticmethod
    def flow(case):
        spec = importlib.util.spec_from_file_location("frame_capacity_flow", ROOT / "utilities/dispatch_replacement.test.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module.FrameCapacityFlow(WH, "opencode", case)

    def test_verified_transition_runs_deep_and_a_second_capacity_death_spawns_nothing(self):
        self.flow(self).run()

    def test_deep_without_a_claim_is_refused_by_the_profile_binding_and_by_registration(self):
        self.flow(self).run_claimless()

    def test_undeclared_top_collapses_without_config_rewrite_and_keeps_launch_guards(self):
        self.flow(self).run_opencode_undeclared_profile()


class ForegroundActivityWiring(unittest.TestCase):
    def test_both_foreground_branches_receive_shared_activity_context(self):
        source = Path(__file__).with_name("dispatch-headless.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "wait_foreground"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(any(k.arg == "activity_context" for k in call.keywords) for call in calls))
        self.assertIn('if args.launch_lifecycle == FOREGROUND_SCOPED and args.worker_type != "owner":\n            try:\n                from foreground_activity import build_activity_context', source)
        self.assertEqual(source.count('build_activity_context('), 1)
        self.assertIn('worker_type=args.worker_type, harness="opencode"', source)


if __name__=="__main__": unittest.main()
