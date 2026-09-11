#!/usr/bin/env python3
import argparse,importlib.util,io,json,os,subprocess,sys,tempfile,types,unittest
import uuid
from contextlib import redirect_stdout
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[3]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
WH_S=importlib.util.spec_from_file_location("codex_dispatch_headless",Path(__file__).with_name("dispatch-headless.py")); WH=importlib.util.module_from_spec(WH_S); WH_S.loader.exec_module(WH)
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


class CodexSD45InternalProbe(unittest.TestCase):
    def test_absent_evidence_binds_supported_and_marks_internal(self):
        args = probe_args()
        row = dict(parent_harness="claude", parent_transport="headless", parent_sandbox="default",
                   child_harness="codex", launch_authority="conductor", status="supported",
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
            child_harness="codex", launch_authority=args.launch_authority,
            status=args.nested_eligibility, source=args.eligibility_source,
        )  # must not raise

    def test_unsupported_probe_result_fails_closed_with_no_launch(self):
        args = probe_args()
        row = dict(parent_harness="claude", parent_transport="headless", parent_sandbox="default",
                   child_harness="codex", launch_authority="conductor", status="unsupported",
                   probe_source="direct-headless-check", failure_class="exit-1")
        with mock.patch.object(WH.subprocess, "run", return_value=fake_probe_result(**row)):
            WH.bind_internal_eligibility_probe(args)
        self.assertEqual(args.nested_eligibility, "unsupported")
        self.assertEqual(args.eligibility_probe, "internal")
        with self.assertRaises(WH.DispatchContractError) as ctx:
            WH.validate_nested_eligibility(
                dispatch_depth=args.dispatch_depth, action=args.action, parent_harness=args.parent_harness,
                parent_transport=args.parent_transport, parent_sandbox=args.parent_sandbox,
                child_harness="codex", launch_authority=args.launch_authority,
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
        args = probe_args(parent_sandbox="unknown")
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
        row = dict(parent_harness="opencode", parent_transport="headless", parent_sandbox="default",
                   child_harness="codex", launch_authority="conductor", status="supported",
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


class CodexSandboxMountShape(unittest.TestCase):
    def args(self, transport):
        return argparse.Namespace(
            sandbox="workspace-write",
            launch_lifecycle="foreground-scoped",
            dispatch_depth=2,
            parent_harness="codex",
            parent_transport=transport,
            parent_sandbox="workspace-write",
        )

    def test_tracked_file_shape_fails_before_sandbox_launch(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
            worktree = Path(tmp)
            target = worktree / ".codex"
            target.write_text("")
            invalid = WH.invalid_codex_mount_target(
                self.args("codex-exec-headless"), worktree
            )
        self.assertEqual(invalid, target)

    def test_canonical_nested_headless_disables_inner_mount_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
            worktree = Path(tmp)
            (worktree / ".codex").write_text("")
            args = self.args("headless")
            self.assertEqual(WH.effective_runtime_sandbox(args), "danger-full-access")
            self.assertIsNone(WH.invalid_codex_mount_target(args, worktree))

    def test_directory_shape_is_valid_with_workspace_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
            worktree = Path(tmp)
            (worktree / ".codex").mkdir()
            self.assertIsNone(
                WH.invalid_codex_mount_target(
                    self.args("codex-exec-headless"), worktree
                )
            )

    def test_dry_run_rejects_file_before_registry_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worktree = base / "repo"
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            (worktree / ".codex").write_text("")
            jobs = base / "jobs.log"
            # `codex_command=ok` describes the Codex CLI being callable, which is
            # not what this test is about -- the assertion under test is that a
            # bad mount target is a worktree-local failure. A CI runner has no
            # Codex on PATH, so use the same stub-on-PATH pattern
            # _preflight_check() already relies on and keep the verdict a
            # property of the worktree.
            bin_dir = base / "stub-bin"
            bin_dir.mkdir()
            stub = bin_dir / "codex"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
            env = {**os.environ, "AGENT_HOME": str(ROOT)}
            env["PATH"] = "%s:%s" % (bin_dir, env.get("PATH", ""))
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "adapters/codex/bin/dispatch-headless.py"),
                    "--dry-run", "--worktree", str(worktree), "--slug", "mount-shape",
                    "--capability", "autopilot-code", "--mode", "debug",
                    "--model", "gpt-test", "--reasoning", "low",
                    "--jobs", str(jobs),
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("invalid-worktree-codex-mount-target", output)
            self.assertIn("failure_scope=exact-worktree", output)
            self.assertIn("codex_command=ok", output)
            self.assertIn("retry_on_isolated_worktree=1", output)
            self.assertIn("child_spawned=0", output)
            self.assertFalse(jobs.exists())

    def _preflight_check(self, worktree, extra_env=None):
        """Run `preflight.sh headless --check` with a stub `codex` on PATH.

        The mount shape is a property of the worktree, so the probe must reach a
        verdict without a real Codex install deciding the outcome first.
        """
        bin_dir = worktree.parent / "stub-bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "codex"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENT_DISPATCH_CHILD", "CODEX_DISPATCH_SANDBOX_FORCE")}
        env["AGENT_HOME"] = str(ROOT)
        env["PATH"] = "%s:%s" % (bin_dir, env.get("PATH", ""))
        env.update(extra_env or {})
        return subprocess.run(
            [str(ROOT / "adapters/codex/bin/preflight.sh"), "headless", "--check", str(worktree)],
            text=True, capture_output=True, env=env, timeout=60,
        )

    def _worktree_with_mount(self, tmp, make_target):
        worktree = Path(tmp) / "repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        make_target(worktree / ".codex")
        return worktree

    def test_eligibility_probe_rejects_file_shape_before_any_attempt(self):
        # SD-48: the checked tuple must not report `supported` for a shape the
        # wrapper will refuse at spawn — that burns an attempt per hop.
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree_with_mount(tmp, lambda target: target.write_text(""))
            result = self._preflight_check(worktree)
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn("reason=invalid-worktree-codex-mount-target", result.stdout)
        self.assertIn("failure_scope=exact-worktree", result.stdout)
        self.assertIn("codex_command=ok", result.stdout)
        self.assertIn("retry_on_isolated_worktree=1", result.stdout)

    def test_eligibility_probe_rejects_symlink_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree_with_mount(
                tmp, lambda target: target.symlink_to(Path(tmp) / "absent")
            )
            result = self._preflight_check(worktree)
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn("reason=invalid-worktree-codex-mount-target", result.stdout)

    def test_untracked_primary_file_does_not_poison_final_isolated_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            isolated = Path(tmp) / "isolated"
            primary.mkdir()
            subprocess.run(["git", "init", "-q", str(primary)], check=True)
            subprocess.run(["git", "-C", str(primary), "config", "user.email", "fixture@example.com"], check=True)
            subprocess.run(["git", "-C", str(primary), "config", "user.name", "Fixture"], check=True)
            (primary / "tracked").write_text("fixture\n")
            subprocess.run(["git", "-C", str(primary), "add", "tracked"], check=True)
            subprocess.run(["git", "-C", str(primary), "commit", "-qm", "fixture"], check=True)
            (primary / ".codex").write_text("")
            subprocess.run(
                ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "isolated", str(isolated)],
                check=True,
            )
            try:
                primary_result = self._preflight_check(primary)
                isolated_mount = WH.invalid_codex_mount_target(
                    self.args("codex-exec-headless"), isolated
                )
            finally:
                subprocess.run(
                    ["git", "-C", str(primary), "worktree", "remove", "--force", str(isolated)],
                    check=True,
                )
        self.assertEqual(primary_result.returncode, 65, primary_result.stdout + primary_result.stderr)
        self.assertIn("failure_scope=exact-worktree", primary_result.stdout)
        self.assertIn("codex_command=ok", primary_result.stdout)
        self.assertIn("retry_on_isolated_worktree=1", primary_result.stdout)
        self.assertIsNone(isolated_mount)

    def test_eligibility_probe_honors_the_disabled_inner_sandbox_signal(self):
        # `AGENT_DISPATCH_CHILD=1` is one of the two signals that make
        # effective_runtime_sandbox `danger-full-access`, where the wrapper
        # itself accepts the shape. The probe must not be stricter than the
        # wrapper it speaks for.
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree_with_mount(tmp, lambda target: target.write_text(""))
            result = self._preflight_check(worktree, {"AGENT_DISPATCH_CHILD": "1"})
        self.assertNotIn("invalid-worktree-codex-mount-target", result.stdout + result.stderr)


class CodexOwnerRegistryProjection(unittest.TestCase):
    def test_missing_registry_writable_root_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                nested_headless_network=True,
                jobs_path=Path(tmp) / "registry" / "jobs.log",
            )
            with mock.patch.object(WH, "nested_owner_writable_dirs", return_value=()):
                with self.assertRaises(WH.DispatchContractError) as ctx:
                    WH.validate_nested_owner_registry_projection(args)
        self.assertEqual(ctx.exception.reason, "owner-registry-sandbox-unwritable")


class CodexRouteBoundWorkerGrant(unittest.TestCase):
    """Plan-check round-1 Finding 1/2: nested_owner_writable_dirs() only fires
    for an owner with nested_headless_network -- an ordinary registered
    dispatch_depth==2 worker got zero .core-grounding grant from it, so
    core-read-marker.sh's mkdir died EROFS for every plain depth-2 Codex
    worker. route_bound_worker_writable_dirs() must grant .core-grounding
    unconditionally whenever route_id is set, independent of the owner-only
    network-widening gate."""

    def _depth2_args(self, tmp):
        agent_home = Path(tmp) / "agent-home"
        (agent_home / "core").mkdir(parents=True)
        (agent_home / "core" / "CORE.md").write_text("fixture\n")
        return argparse.Namespace(
            worktree=str(Path(tmp) / "repo"),
            artifact_root=str(Path(tmp) / "artifacts"),
            report_bundle_root=None,
            agent_home=agent_home,
            jobs_path=Path(tmp) / "jobs.log",
            dispatch_depth=2,
            route_id="rt-fixture",
            attempt_id="att-fixture",
            nested_headless_network=False,
            worker_type="stage",
            sandbox="workspace-write",
            approval="never",
            resolved_completion_delivery="one-shot",
            resolved_model_settings={"source": "inherit"},
            owner_route_binding=None,
            max_continuations=None,
        )

    def test_ordinary_depth2_worker_gets_core_grounding_independent_of_network_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._depth2_args(tmp)
            self.assertEqual(WH.nested_owner_writable_dirs(args), ())
            WH.ensure_owner_writable_dirs(args)
            granted = WH.route_bound_worker_writable_dirs(args)
        self.assertEqual(granted, (args.agent_home.resolve() / ".core-grounding",))

    def test_routeless_registered_workers_can_record_required_spec_reads(self):
        # Cairn att-4726c1b6: depth-1 review without route/network widening.
        # The portable kernel's read obligation is not review-only.
        for worker_type in ("review", "stage", "support", "owner"):
            for delivery, flag in (("one-shot", "--add-dir"),
                                   ("app-server-supervised", "--writable-root")):
                with self.subTest(worker_type=worker_type, delivery=delivery):
                    with tempfile.TemporaryDirectory() as tmp:
                        args = self._depth2_args(tmp)
                        args.dispatch_depth = 1
                        args.route_id = None
                        args.worker_type = worker_type
                        args.registered_worker = 1
                        args.execution_surface = "registered-headless"
                        args.resolved_completion_delivery = delivery
                        WH.ensure_owner_writable_dirs(args)
                        command = WH.shell_command(args, Path(tmp)/"p", Path(tmp)/"log")
                        marker_dir = args.agent_home / ".spec-grounding"
                        self.assertIn(f"{flag} {marker_dir}", command)
                        self.assertTrue(marker_dir.is_dir())
                        self.assertNotIn(f"{flag} {args.agent_home} ", command)
                        self.assertFalse((args.agent_home / ".core-grounding").exists())

    def test_unregistered_routeless_launch_does_not_gain_spec_write_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._depth2_args(tmp)
            args.route_id = None
            args.registered_worker = 0
            args.execution_surface = "registered-headless"
            WH.ensure_owner_writable_dirs(args)
            command = WH.shell_command(args, Path(tmp)/"p", Path(tmp)/"log")
            self.assertNotIn(str(args.agent_home / ".spec-grounding"), command)
            self.assertFalse((args.agent_home / ".spec-grounding").exists())

    def test_ordinary_depth2_worker_add_dir_list_includes_core_grounding(self):
        # The direct verification requirement (c): build the args for a plain
        # dispatch_depth=2 worker (not owner, no nested_headless_network) and
        # assert .core-grounding is present in the actual --add-dir set the
        # wrapper hands to `codex exec`.
        with tempfile.TemporaryDirectory() as tmp:
            args = self._depth2_args(tmp)
            WH.ensure_owner_writable_dirs(args)
            command = WH.shell_command(args, Path(tmp) / "p.txt", Path(tmp) / "l.log")
            core_grounding = str((args.agent_home.resolve() / ".core-grounding"))
            self.assertIn(core_grounding, command)
            self.assertIn(f"--add-dir {core_grounding}", command)
            self.assertTrue((args.agent_home / ".core-grounding").is_dir())

    def test_owner_network_widened_worker_also_still_gets_core_grounding(self):
        # .spec-grounding's shape (unconditional grant, D-B) must not regress
        # for the owner-network case either -- .core-grounding stays granted
        # via route_bound_worker_writable_dirs() regardless of the owner path.
        with tempfile.TemporaryDirectory() as tmp:
            args = self._depth2_args(tmp)
            args.dispatch_depth = 1
            args.worker_type = "owner"
            args.nested_headless_network = True
            WH.ensure_owner_writable_dirs(args)
            command = WH.shell_command(args, Path(tmp) / "p.txt", Path(tmp) / "l.log")
        core_grounding = str((args.agent_home.resolve() / ".core-grounding"))
        self.assertIn(f"--add-dir {core_grounding}", command)

    def test_route_bound_grant_empty_without_route_id_or_owner_network(self):
        # Only a launch that is neither route-bound nor an owner-network owner
        # gets no read-guard grant. An owner without a route_id keeps its
        # SD-72 unconditional .core-grounding/.spec-grounding grants (F-3).
        with tempfile.TemporaryDirectory() as tmp:
            args = self._depth2_args(tmp)
            args.route_id = None
            args.nested_headless_network = False
            self.assertEqual(WH.route_bound_worker_writable_dirs(args), ())

    def test_owner_network_without_route_id_keeps_read_guard_grants(self):
        # Review F-3: a standard+ codex owner launched without a route_id must
        # not lose the .core-grounding/.spec-grounding grants SD-72 states
        # unconditionally, or the portable read-guard hooks die on EROFS.
        with tempfile.TemporaryDirectory() as tmp:
            args = self._depth2_args(tmp)
            args.route_id = None
            args.dispatch_depth = 1
            args.worker_type = "owner"
            args.nested_headless_network = True
            WH.ensure_owner_writable_dirs(args)
            granted = WH.route_bound_worker_writable_dirs(args)
            self.assertIn(
                (args.agent_home / ".core-grounding").resolve(), granted
            )
            command = WH.shell_command(
                args, Path(tmp) / "p.txt", Path(tmp) / "l.log"
            )
            spec_grounding = str(args.agent_home.resolve() / ".spec-grounding")
            self.assertIn(spec_grounding, command)


class CodexSD45(unittest.TestCase):
 def test_route_consumer_and_scope_refusal(self):
  [cache.clear() for cache in (R._LAUNCH_ROOT_IDENTITY_CACHE, R._LAUNCH_CONTENT_DIGEST_CACHE, R._LAUNCH_SOURCE_REVISION_CACHE)]
  with tempfile.TemporaryDirectory() as td:
   base=Path(td); repo=base/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True); subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True); (repo/"x").write_text("x"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","init"],check=True)
   art=base/".agent_reports"; art.mkdir(); jobs=base/"jobs.log"; logs=base/"logs"; gate={"spec_read":{"satisfied":True,"source":"codex-fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"codex-fixture"}}
   dispatch={"tuples":[{"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":"codex","launch_authority":"conductor","status":"supported","probe_source":"codex-fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(repo.resolve()),"failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0}],"native_subagent":[]}
   with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_ARTIFACT_ROOT":str(art)},clear=False):
    route=R.compile_route("autopilot-code","dev","strong",repo,art,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch)
   path=base/"route.json"; path.write_text(json.dumps(route)); node=next(x for x in route["nodes"] if x["id"]=="execute")
   parent=subprocess.Popen(["sleep","60"]);self.addCleanup(parent.wait);self.addCleanup(parent.kill);parent_start=(Path("/proc")/str(parent.pid)/"stat").read_text().split()[21];jobs.write_text(f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\tattempt_schema_version=2,dispatch_depth=1,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,worker_type=owner,harness=codex,runtime_sandbox=workspace-write,attempt_id=att-sd45-parent,pid={parent.pid},pid_start={parent_start}\n")
   args=[sys.executable,str(ROOT/"adapters/codex/bin/dispatch-headless.py"),"--register","--worktree",str(repo),"--slug","codex-sd45","--capability","autopilot-code","--capability-mode","dev","--worker-mode",node["unit"],"--qa","standard","--intensity","strong","--dispatch-depth","2","--parent","owner","--parent-harness","codex","--parent-transport","headless","--parent-sandbox","workspace-write","--nested-eligibility","supported","--eligibility-source","codex-fixture","--fallback-ordinal","1","--route-file",str(path),"--route-id",route["route_id"],"--route-hash",route["route_hash"],"--route-node","execute","--unit",node["unit"],"--registry-digest",route["registry_digest"],"--write-scope",";".join(node["write_scope"]),"--completion-gate",node["completion_gate"],"--model-role",node["role"],"--model-profile",node["model_profile"],"--jobs",str(jobs),"--log-dir",str(logs)]
   env=isolated_dispatch_env(AGENT_HOME=str(ROOT),AGENT_ARTIFACT_ROOT=str(art),AGENT_DISPATCH_JOBS=str(jobs),AGENT_DISPATCH_ATTEMPT_ID="att-sd45-parent"); ok=subprocess.run(args,text=True,capture_output=True,env=env); self.assertEqual(ok.returncode,0,ok.stdout+ok.stderr); prompt=next(logs.glob("codex-sd45*.codex.prompt.txt")).read_text(); self.assertIn("consume the assigned route only",prompt); self.assertNotIn("preflight.sh route autopilot-code",prompt); self.assertIn(f"unit={node['unit']}",jobs.read_text()); self.assertIn(f"unit={node['unit']}",ok.stdout)
   bad=args.copy(); bad[bad.index(";".join(node["write_scope"]))]="spec/**"; denied=subprocess.run(bad,text=True,capture_output=True,env=env); self.assertEqual(denied.returncode,65); self.assertIn("route-node-scope-mismatch",denied.stderr)
   for field,value in (("resolver_version","unsupported"),("judgment_floor","forged")):
    forged=json.loads(json.dumps(route));next(n for n in forged["nodes"] if n["id"]=="execute")["profile_selection"][field]=value
    forged["route_hash"]=R.route_hash(forged);forged["route_id"]="rt-"+forged["route_hash"].split(":")[1][:16]
    bad_path=base/(field+".json");bad_path.write_text(json.dumps(forged))
    forged_args=args.copy();forged_args[forged_args.index("--register")]="--start"
    for flag,new_value in (("--slug","tamper-"+field),("--route-file",str(bad_path)),("--route-id",forged["route_id"]),("--route-hash",forged["route_hash"])):
     forged_args[forged_args.index(flag)+1]=new_value
    before=jobs.read_bytes();denied=subprocess.run(forged_args,text=True,capture_output=True,env=env)
    self.assertNotEqual(denied.returncode,0,denied.stdout+denied.stderr)
    self.assertIn("child_spawned=0",denied.stdout+denied.stderr)
    self.assertEqual(jobs.read_bytes(),before)
   legacy=[sys.executable,str(ROOT/"adapters/codex/bin/dispatch-headless.py"),"--dry-run","--worktree",str(repo),"--slug","codex-legacy-scope","--capability","autopilot-code","--mode","dev","--qa","standard","--write-scope","source/**","--model","gpt-test","--reasoning","low","--sandbox","danger-full-access"]
   compatible=subprocess.run(legacy,text=True,capture_output=True,env=env); self.assertEqual(compatible.returncode,0,compatible.stdout+compatible.stderr); self.assertIn("status=dry-run",compatible.stdout)

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


def _prompt_args(**overrides):
    base = dict(
        worker_type="owner", intensity="strong", worktree="/tmp/fixture-worktree",
        route_id=None, route_node=None, attempt_id=None, route_file=None,
        worker_role=None, profile=None, capability="autopilot-code",
        capability_mode="dev", worker_mode=None, mode=None,
        qa="thorough", dispatch_depth=1, parent_slug=None, parent_session_id=None,
        capability_owner=None, owner_harness=None, write_scope=None,
        completion_gate=None, assigned_contract=None, unit=None, model_role=None,
        agent_home=Path("/tmp/fixture-agent-home"), artifact_root="/tmp/fixture-artifacts",
        jobs_path=Path("/tmp/fixture-agent-home/.dispatch/jobs.log"),
        sandbox="workspace-write", approval="never",
        nested_headless_network=False,
        resolved_model_settings={
            "source": "inherit", "role": "-", "model": None, "reasoning": None
        },
        resolved_completion_delivery="app-server-supervised",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class CodexSD78CompletionDelivery(unittest.TestCase):
    """SD-78: standard+ owners park outside the model and resume once."""

    def test_owner_prompt_carries_runtime_join_clause(self):
        args = _prompt_args()
        with mock.patch.object(WH, "task_prompt", return_value=("do the thing", "cli")):
            prompt, _source = WH.dispatch_prompt(args)
        self.assertTrue(prompt.startswith("Runtime-owned completion join (SD-78):"))
        self.assertIn("runtime_wait: registered-children", prompt)
        self.assertIn("joins all exact parent_attempt_id children outside the model", prompt)
        self.assertIn("Do not call dispatch-wait", prompt)
        self.assertIn("a supervised owner yields the current turn", prompt)
        self.assertNotIn("poll in the current turn", prompt)

    def test_explicit_poll_mode_is_disclosed_as_fallback(self):
        args = _prompt_args(resolved_completion_delivery="poll-fallback")
        with mock.patch.object(WH, "task_prompt", return_value=("do the thing", "cli")):
            prompt, _source = WH.dispatch_prompt(args)
        self.assertTrue(prompt.startswith("Checked polling fallback"))
        self.assertIn("not runtime completion parity", prompt)

    def test_supervised_shell_uses_app_server_and_attempt_scoped_state(self):
        args = _prompt_args(attempt_id="att-parent")
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("codex-app-server-supervisor.py", command)
        self.assertIn("--parent-attempt-id att-parent", command)
        self.assertIn(
            "--state-file /tmp/fixture-agent-home/.dispatch/supervisor-state/att-parent.json",
            command,
        )
        self.assertIn(
            "--lease-file /tmp/fixture-agent-home/.dispatch/supervisor-state/att-parent.lease",
            command,
        )

    def test_lab_shell_projects_only_configured_report_bundle_root(self):
        args = _prompt_args(
            attempt_id="att-parent",
            capability="autopilot-lab",
            report_bundle_root=Path("/tmp/fixture-report-bundles"),
        )
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn("--writable-root /tmp/fixture-report-bundles", command)

    def test_depth1_owner_can_write_its_heartbeat_without_the_state_root(self):
        # 2026-09-04: a depth-1 owner matched neither branch that grants the
        # dispatch state root, so under workspace-write `preflight.sh
        # stage-heartbeat` died `[Errno 30] Read-only file system` on
        # `<state root>/watchdog/<attempt>.lock` and the owner reported BLOCKED
        # with its work already finished.
        args = _prompt_args(attempt_id="att-owner", dispatch_depth=1)
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        state_root = "/tmp/fixture-agent-home/.dispatch"
        self.assertIn(f"--writable-root {state_root}/heartbeats", command)
        self.assertIn(f"--writable-root {state_root}/watchdog", command)
        # Progress only: the registry, logs and supervisor state stay closed.
        self.assertNotIn(f"--writable-root {state_root} ", command + " ")

    def test_exec_builder_grants_the_same_progress_directories(self):
        # The `codex exec` builder serves one-shot and poll-fallback delivery and
        # emits `--add-dir`, not `--writable-root`; it was missed the first time
        # and kept the read-only heartbeat failure on those paths.
        state_root = "/tmp/fixture-agent-home/.dispatch"
        for delivery in ("one-shot", "poll-fallback"):
            args = _prompt_args(
                attempt_id="att-owner", dispatch_depth=1,
                resolved_completion_delivery=delivery,
            )
            command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
            self.assertIn(f"--add-dir {state_root}/heartbeats", command, delivery)
            self.assertIn(f"--add-dir {state_root}/watchdog", command, delivery)
            self.assertNotIn(f"--add-dir {state_root} ", command + " ", delivery)
        widened = _prompt_args(
            attempt_id="att-owner", nested_headless_network=True,
            resolved_completion_delivery="one-shot",
        )
        command = WH.shell_command(widened, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        self.assertIn(f"--add-dir {state_root} ", command + " ")
        self.assertNotIn(f"--add-dir {state_root}/heartbeats", command)

    def test_owner_network_widening_still_grants_the_whole_state_root(self):
        args = _prompt_args(attempt_id="att-owner", nested_headless_network=True)
        command = WH.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        state_root = "/tmp/fixture-agent-home/.dispatch"
        self.assertIn(f"--writable-root {state_root} ", command + " ")
        # and does not also enumerate the narrow pair, which it already contains
        self.assertNotIn(f"--writable-root {state_root}/heartbeats", command)

    def test_report_bundle_root_resolver_is_publish_stage_only(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(Path(td) / "config"), "REPORT_BUNDLE_ROOT": str(Path(td) / "store")},
            clear=False,
        ):
            (Path(td) / "store").mkdir()
            route = Path(td) / "route.json"
            route.write_text(json.dumps({"capability": "autopilot-lab", "nodes": [{
                "id": "publish", "kind": "capability-owner", "unit": "_kernel/owner",
                "completion_gate": "lab-publish", "dispatch_depth": 1,
            }]}))
            self.assertEqual(WH.resolve_report_bundle_root(str(route), "publish"), Path(td) / "store")
            for node in ("setup", "media", "report", "independent-verify", "sync"):
                with self.subTest(node=node): self.assertIsNone(WH.resolve_report_bundle_root(str(route), node))
            self.assertIsNone(WH.resolve_report_bundle_root(None, "publish"))

    def test_supervised_state_path_rejects_attempt_path_escape(self):
        args = _prompt_args(attempt_id="att-../../outside")
        with self.assertRaises(WH.DispatchContractError):
            WH.completion_state_path(args)

    def test_stage_prompt_never_carries_the_clause(self):
        args = _prompt_args(worker_type=None, intensity="strong", dispatch_depth=2,
                            route_id="rt-fixture", route_node="execute", attempt_id="att-fixture",
                            worker_role="code-execute")
        with mock.patch.object(WH, "task_prompt", return_value=("do the thing", "cli")):
            prompt, _source = WH.dispatch_prompt(args)
        self.assertNotIn("Runtime-owned completion join", prompt)

    def parent_args(self, **overrides):
        values = dict(
            action="start",
            dispatch_depth=1,
            launch_lifecycle=WH.DETACHED,
            execution_surface="registered-headless",
            registered_worker=1,
            parent_harness="codex",
            parent_session_id="thread-native-parent",
            require_hook_trust=False,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def uuidv7_for_ms(value):
        return str(uuid.UUID(int=(value << 80) | (7 << 76) | (0x8 << 60)))

    @staticmethod
    def _popen_delegating_to_git(fake_proc):
        """Only the codex-spawn Popen call (identified by launch-fence.py, the
        actual spawn_worker command) is faked; every other subprocess.run call
        inside main() (git, artifact-root.sh, ...) still needs a real Popen or
        it breaks unrelated to the sentinel proof this test is checking."""
        real_popen = subprocess.Popen

        def _side_effect(cmd, *a, **kw):
            if isinstance(cmd, list) and any("launch-fence.py" in str(part) for part in cmd):
                return fake_proc
            return real_popen(cmd, *a, **kw)

        return _side_effect


    def test_unmanaged_interactive_parent_is_identified_then_blocked(self):
        args = self.parent_args()
        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": args.parent_session_id, "AGENT_DISPATCH_CHILD": "0"},
            clear=True,
        ):
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(args.parent_completion_delivery, "poll-fallback")
        self.assertEqual(
            args.parent_completion_reason, "interactive-auto-wake-unsupported"
        )
        with self.assertRaises(WH.DispatchContractError) as raised:
            WH.validate_interactive_parent_launch(args)
        self.assertEqual(raised.exception.reason, "managed-entry-required")
        self.assertFalse(args.require_hook_trust)

    def test_actual_codex_caller_overrides_synthetic_claude_parent_metadata(self):
        args = self.parent_args(
            parent_harness="claude",
            parent_session_id="synthetic-claude-session",
            parent_slug="synthetic-claude-owner",
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
        self.assertIsNone(args.parent_slug)
        self.assertEqual(args.parent_completion_delivery, "poll-fallback")
        self.assertEqual(raised.exception.reason, "managed-entry-required")

    def test_actual_claude_caller_overrides_synthetic_codex_parent_metadata(self):
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
                "AGENT_DISPATCH_CHILD": "0",
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

    def test_low_level_operator_can_explicitly_select_finite_poll_recovery(self):
        args = self.parent_args(allow_unmanaged_parent_poll=True)
        args.parent_completion_delivery = "poll-fallback"
        WH.validate_interactive_parent_launch(args)
        self.assertEqual(
            args.parent_completion_reason, "operator-authorized-unmanaged-poll"
        )

    def test_managed_interactive_parent_selects_single_ingress_gateway(self):
        args = self.parent_args()
        binding = mock.Mock(thread_advanced=False)
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
        ) as probe:
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(
            args.parent_completion_delivery, WH.MANAGED_PARENT_DELIVERY
        )
        self.assertEqual(
            args.parent_completion_reason, "managed-single-ingress-live"
        )
        self.assertIs(args.managed_gateway_binding, binding)
        WH.validate_interactive_parent_launch(args)
        probe.assert_called_once_with(
            parent_harness="codex",
            parent_session_id=args.parent_session_id,
        )

    def test_managed_interactive_parent_resolves_witnessed_fork_successor(self):
        args = self.parent_args()
        inherited = args.parent_session_id
        binding = mock.Mock(
            thread_advanced=True,
            thread_id="thread-fork-successor",
        )
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": inherited,
                "AGENT_DISPATCH_CHILD": "0",
                "AGENT_CODEX_MANAGED_GATEWAY": "1",
                "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "codex",
            },
            clear=True,
        ), mock.patch.object(
            WH, "probe_managed_codex_parent", return_value=binding
        ) as probe:
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(
            args.parent_completion_delivery, WH.MANAGED_PARENT_DELIVERY
        )
        self.assertEqual(args.parent_session_id, "thread-fork-successor")
        self.assertEqual(
            args.parent_completion_reason, "managed-thread-advanced"
        )
        probe.assert_called_once_with(
            parent_harness="codex", parent_session_id=inherited
        )

    def test_managed_probe_failure_is_typed_poll_fallback(self):
        args = self.parent_args()
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": args.parent_session_id,
                "AGENT_DISPATCH_CHILD": "0",
                "AGENT_CODEX_MANAGED_GATEWAY": "1",
            },
            clear=True,
        ), mock.patch.object(
            WH,
            "probe_managed_codex_parent",
            side_effect=WH.ManagedDispatchError("managed-gateway-not-ready"),
        ):
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(args.parent_completion_delivery, "poll-fallback")
        self.assertEqual(
            args.parent_completion_reason, "managed-gateway-not-ready"
        )
        with self.assertRaises(WH.DispatchContractError) as raised:
            WH.validate_interactive_parent_launch(args)
        self.assertEqual(raised.exception.reason, "managed-entry-required")

    def test_claude_parent_keeps_claude_wake_adapter_for_codex_child(self):
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

    def test_managed_sidecar_is_exact_singleton_and_registry_bounded(self):
        args = self.parent_args()
        args.parent_completion_delivery = WH.MANAGED_PARENT_DELIVERY
        args.managed_gateway_binding = object()
        args.attempt_id = "att-managed"
        sidecar = argparse.Namespace(
            pid=4242,
            sealed_batch_id="batch-managed",
            log_file=Path("/tmp/managed.jsonl"),
        )
        with mock.patch.object(
            WH, "launch_managed_completion_sidecar", return_value=sidecar
        ) as launch, mock.patch.object(
            WH, "annotate_attempt_row", return_value=True
        ) as annotate:
            WH.launch_parent_completion_sidecar(args, Path("/tmp/jobs.log"))
        launch.assert_called_once_with(
            binding=args.managed_gateway_binding,
            jobs=Path("/tmp/jobs.log"),
            parent_session_id=args.parent_session_id,
            attempt_ids={"att-managed"},
        )
        self.assertEqual(args.managed_sidecar_state, "running")
        self.assertEqual(args.managed_sidecar_pid, 4242)
        metadata = annotate.call_args.args[2]
        self.assertNotIn("raw", "".join(metadata))

    def test_interactive_registration_does_not_force_hook_trust(self):
        args = self.parent_args(action="register")
        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": args.parent_session_id, "AGENT_DISPATCH_CHILD": "0"},
            clear=True,
        ):
            WH.bind_parent_completion_delivery(args)
        self.assertEqual(args.parent_completion_delivery, "poll-fallback")
        with self.assertRaises(WH.DispatchContractError):
            WH.validate_interactive_parent_launch(args)
        self.assertFalse(args.require_hook_trust)

    def test_wrapper_has_no_native_stop_stamp_or_state_writer(self):
        source = Path(WH.__file__).read_text(encoding="utf-8")
        self.assertNotIn('PARENT_STOP_DELIVERY = "codex-stop-hook"', source)
        self.assertNotIn("register_parent_stop_attempt", source)
        self.assertNotIn("register_parent_session_attempt", source)

    def test_owner_direct_intensity_never_carries_the_clause(self):
        args = _prompt_args(intensity="direct")
        with mock.patch.object(WH, "task_prompt", return_value=("do the thing", "cli")):
            prompt, _source = WH.dispatch_prompt(args)
        self.assertNotIn("Runtime-owned completion join", prompt)

    def test_auto_prefers_checked_app_server_and_forced_mode_fails_closed(self):
        args = argparse.Namespace(
            completion_delivery="auto", dispatch_depth=1, worker_type="owner",
            intensity="strong",
        )
        with mock.patch.object(WH, "codex_app_server_available", return_value=True):
            self.assertEqual(WH.resolve_completion_delivery(args), "app-server-supervised")
        args.completion_delivery = "supervised"
        with mock.patch.object(WH, "codex_app_server_available", return_value=False):
            with self.assertRaises(WH.DispatchContractError):
                WH.resolve_completion_delivery(args)


class ForegroundReviewStartPathTest(unittest.TestCase):
    def setUp(self):
        # This suite exercises admission and watcher ordering, not a user's
        # installed runtime. Keep that external readiness boundary isolated.
        projection = mock.patch.object(WH, "check_runtime_projection", return_value=0)
        projection.start()
        self.addCleanup(projection.stop)

    def _owner_route_fixture(self, worktree, artifacts):
        gate = {
            "spec_read": {"satisfied": True, "source": "codex-fixture"},
            "drift_verdict": "within-spec",
            "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "codex-fixture"},
        }
        dispatch = {"tuples": [{
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "danger-full-access", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "codex-fixture", "probe_time": "2026-07-16T00:00:00Z",
            "failure_class": "", "checked_worktree": str(worktree.resolve()),
            "failure_scope": "none", "codex_command": "ok",
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
            def sidecar(args, _jobs):
                args.managed_sidecar_state = "not-started"; args.managed_sidecar_reason = "-"
                args.managed_sidecar_pid = args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            def wait(*_args, **_kwargs):
                order.append("wait")
                return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)
            def seal(*args, **kwargs):
                order.append("seal")
                self.assertEqual(kwargs, {"exit_code": 0, "failure": "", "group_empty": True})
                metadata = next(
                    WH.parse_registry_metadata(line.split("\t", 5)[5])
                    for line in jobs.read_text(encoding="utf-8").splitlines()
                    if "attempt_id=att-codex-foreground" in line
                )
                self.assertEqual(args[1], metadata["attempt_id"])
                self.assertEqual(str(args[2]), metadata["pid"])
                self.assertEqual(str(args[3]), metadata["pid_start"])
                self.assertEqual(str(args[4]), metadata["pgid"])
                return seal_token
            def reap(*args, **kwargs):
                order.append("reap"); self.assertIs(kwargs["foreground_seal"], seal_token)
                self.assertEqual(args[1], "att-codex-foreground")
                return 4242
            real_annotate = WH.annotate_attempt_row
            def annotate(jobs_path, attempt_id, values):
                if "reap_watch" in values: order.append("annotate")
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
                    "--model", "test", "--reasoning", "low", "--completion-delivery", "poll",
                    "--attempt-id", "att-codex-foreground",
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
            def sidecar(args, _jobs):
                args.managed_sidecar_state = "not-started"; args.managed_sidecar_reason = "-"
                args.managed_sidecar_pid = args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            def wait(*_args, **_kwargs):
                order.append("wait")
                return types.SimpleNamespace(exit_code=0, failure="", group_empty=True)
            def seal(*args, **kwargs):
                order.append("seal")
                self.assertEqual(kwargs, {"exit_code": 0, "failure": "", "group_empty": True})
                metadata = next(
                    WH.parse_registry_metadata(line.split("\t", 5)[5])
                    for line in jobs.read_text(encoding="utf-8").splitlines()
                    if "attempt_id=att-codex-foreground" in line
                )
                self.assertEqual(args[1], metadata["attempt_id"])
                self.assertEqual(str(args[2]), metadata["pid"])
                self.assertEqual(str(args[3]), metadata["pid_start"])
                self.assertEqual(str(args[4]), metadata["pgid"])
                return seal_token
            def reap(*args, **kwargs):
                order.append("reap"); self.assertIs(kwargs["foreground_seal"], seal_token)
                self.assertEqual(args[1], "att-codex-foreground")
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
                    "--model", "test", "--reasoning", "low", "--completion-delivery", "poll",
                    "--attempt-id", "att-codex-foreground",
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
                        pass
                    elif key.startswith("batch_"):
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
                            "--model", "test", "--reasoning", "low", "--completion-delivery", "poll",
                            "--attempt-id", f"att-codex-identity-{ordinal}", *extra,
                        ])
                    self.assertEqual(result, 0)
                    self.assertEqual(entered[3].call_count, 1)
                    self.assertEqual(entered[4].call_count, 1)
                    final_metadata = WH.parse_registry_metadata(
                        next(
                            line.split("\t", 5)[5]
                            for line in jobs.read_text(encoding="utf-8").splitlines()
                            if f"attempt_id=att-codex-identity-{ordinal}" in line
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
                    self.assertEqual(watcher_args[1], f"att-codex-identity-{ordinal}")
                    self.assertEqual(str(watcher_args[2]), final_metadata["pid"])
                    self.assertEqual(str(watcher_args[3]), final_metadata["pid_start"])
                    self.assertEqual(str(watcher_args[4]), final_metadata["pgid"])
                    self.assertIsNone(watcher_call.call_args.kwargs.get("foreground_seal"))
                    self.assertIn("reap_watch=post-exit", jobs.read_text(encoding="utf-8"))


class CodexTerminalReceipt(unittest.TestCase):
    def test_receipt_fields_cover_valid_invalid_and_absent_without_raw_content(self):
        cases = (
            ({"state": "valid", "source": "exact-turn-completed", "verdict": "PASS",
              "artifact_state": "readable", "artifact_path_b64": "L3NhZmU",
              "blocker_reason": "none", "private": "RAW_COMMAND_SENTINEL"},
             ("PASS", "readable", "none")),
            ({"state": "valid", "source": "exact-turn-completed", "verdict": "FAIL",
              "artifact_state": "none", "blocker_reason": "worker-reported",
              "blocker_detail_excerpt": "RAW_AGENT_SENTINEL"},
             ("FAIL", "none", "worker-reported")),
            ({"state": "valid", "source": "exact-turn-completed", "verdict": "BLOCKED",
              "artifact_state": "none", "blocker_reason": "worker-reported"},
             ("BLOCKED", "none", "worker-reported")),
            ({"state": "invalid", "source": "exact-turn-completed", "verdict": "-",
              "artifact_state": "outside-root", "blocker_reason": "contract-violation",
              "reason": "RAW_FINAL_MESSAGE_SENTINEL"},
             ("-", "outside-root", "contract-violation")),
            (None, ("-", "unchecked", "-")),
        )
        for value, expected in cases:
            with self.subTest(expected=expected):
                fields = WH.terminal_receipt_fields(value)
                self.assertEqual(
                    (fields["handoff_verdict"], fields["artifact_state"],
                     fields["blocker_reason"]), expected
                )
                rendered = "\n".join(f"{key}={item}" for key, item in fields.items())
                self.assertNotIn("RAW_COMMAND_SENTINEL", rendered)
                self.assertNotIn("RAW_AGENT_SENTINEL", rendered)
                self.assertNotIn("RAW_FINAL_MESSAGE_SENTINEL", rendered)
                self.assertEqual(
                    set(fields),
                    {"handoff_state", "handoff_source", "handoff_verdict",
                     "artifact_state", "artifact_readable", "artifact_path_b64",
                     "blocker_reason"},
                )

    def test_pass_receipt_has_no_failure_detail_fields(self):
        fields = WH.terminal_receipt_fields({
            "state": "valid", "source": "exact-turn-completed", "verdict": "PASS",
            "artifact_state": "none", "blocker_reason": "none",
            "blocker_detail_excerpt": "must-not-render",
            "failure_diagnostic_excerpt": "must-not-render",
        })
        self.assertNotIn("blocker_detail_excerpt", fields)
        self.assertNotIn("failure_diagnostic_excerpt", fields)


class CodexLaunchFenceFailure(unittest.TestCase):
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
