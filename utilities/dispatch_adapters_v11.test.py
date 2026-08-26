#!/usr/bin/env python3
import importlib.util, io, os, shutil, subprocess, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]

ADAPTERS={
 "codex":([sys.executable,str(ROOT/"adapters/codex/bin/dispatch-headless.py")],["--model","gpt-test","--reasoning","low"]),
 "claude":([sys.executable,str(ROOT/"adapters/claude/bin/dispatch-headless.py")],["--model","claude-test","--effort","low"]),
 "opencode":([sys.executable,str(ROOT/"adapters/opencode/bin/dispatch-headless.py")],["--model","provider/test","--variant","low"]),
}

class AdapterV11Test(unittest.TestCase):
 def setUp(self): self.parent_procs=[]
 def tearDown(self):
  for proc in self.parent_procs:
   if proc.poll() is None: proc.kill()
   proc.wait()
 def seed_parent(self,jobs,repo,attempt="att-parent-fixture",harness="codex",sandbox="fixture"):
  proc=subprocess.Popen(["sleep","60"]);self.parent_procs.append(proc)
  start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
  jobs.write_text(
   f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
   "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
   "execution_surface=registered-headless,registered_worker=1,"
   f"fallback_hop=same-harness-headless,worker_type=owner,harness={harness},"
   f"runtime_sandbox={sandbox},"
   f"attempt_id={attempt},pid={proc.pid},pid_start={start}\n")
  return attempt
 def load_wrapper(self,harness):
  spec=importlib.util.spec_from_file_location(f"{harness}_dispatch_fixture",ROOT/f"adapters/{harness}/bin/dispatch-headless.py")
  wrapper=importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper); return wrapper
 def fixture(self,root):
  repo=root/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True)
  (repo/"x").write_text("x"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","init"],check=True)
  art=root/".agent_reports"; art.mkdir(); return repo,art
 def command(self,harness,action,repo,jobs,logs,status="supported"):
  wrapper,model=ADAPTERS[harness]
  return wrapper+[f"--{action}","--worktree",str(repo),"--slug",f"{harness}-v11","--capability","autopilot-code","--capability-mode","dev","--worker-mode","dev/backend","--intensity","standard","--dispatch-depth","2","--parent","owner","--worker-role","code-plan","--owner","autopilot-code","--jobs",str(jobs),"--log-dir",str(logs),"--attempt-id",f"att-{harness}-fixture-0001","--parent-harness",harness,"--parent-transport","headless","--parent-sandbox","fixture","--launch-authority","conductor","--nested-eligibility",status,"--eligibility-source",f"{harness}-fixture","--fallback-ordinal","1"]+model
 def run_parent_callback_cell(self,harness,force_foreground):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
   fakebin=root/"bin"; fakebin.mkdir(); fake=fakebin/harness
   fake.write_text("#!/bin/sh\nexec sleep 60\n",encoding="utf-8"); fake.chmod(0o755)
   self.seed_parent(jobs,repo,harness=harness)
   wrapper=self.load_wrapper(harness)
   command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout","5"]
   argv=["dispatch-headless.py",*command[2:]]
   env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
        "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
        "AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CHILD":"1",
        "AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture",
        "OPENCODE_CONFIG_CONTENT":"{}","XDG_STATE_HOME":str(root/"state")}
   env.pop("AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN",None)
   calls=[]
   def parent_is_live(*_args):
    calls.append(True); return False
   patches=[mock.patch.dict(os.environ,env,clear=True),
            mock.patch.object(wrapper,"parent_attempt_binding_is_live",parent_is_live)]
   if force_foreground:
    resolution=wrapper.reconcile_launch_lifecycle(
     wrapper.DETACHED,{},evidence={
      "lifecycle_selector_source":"nspid-vector",
      "lifecycle_nspid_width":"2",
      "lifecycle_pid1_class":"non-system-init",
     })
    patches.append(mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution))
   if hasattr(wrapper,"check_runtime_projection"):
    patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
   if hasattr(wrapper,"ensure_runtime_home_projection"):
    patches.append(mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None))
   if hasattr(wrapper,"launch_summary_owner"):
    patches.append(mock.patch.object(
     wrapper,"launch_summary_owner",return_value={"summary_owner":"test-fixture"}))
   stream=io.StringIO()
   for patch in patches: patch.start()
   try:
    with redirect_stdout(stream): code=wrapper.main(argv)
   finally:
    for patch in reversed(patches): patch.stop()
   return code,stream.getvalue(),jobs.read_text(encoding="utf-8"),len(calls)
 def test_sibling_registry_rows_and_nested_refusal(self):
  for harness in ("codex", "claude", "opencode"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
    env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"OPENCODE_CONFIG_CONTENT":"{}"}
    self.seed_parent(jobs,repo,harness=harness)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-parent-fixture"
    registered=subprocess.run(self.command(harness,"register",repo,jobs,logs),text=True,capture_output=True,env=env)
    self.assertEqual(registered.returncode,0,registered.stdout+registered.stderr)
    row=jobs.read_text(encoding="utf-8")
    self.assertIn(f"harness={harness}",row); self.assertIn("attempt_id=att-",row)
    self.assertIn("capability_mode=dev",row); self.assertIn("worker_mode=dev/backend",row)
    self.assertNotIn(",mode=",row)
    self.assertIn("nested_eligibility=supported",row); self.assertIn("fallback_ordinal=1",row)
    self.assertIn("parent_attempt_id=att-parent-fixture",row)
    self.assertIn("parent_pid=",row);self.assertIn("parent_pid_start=",row)
    self.assertIn(f"launch_home={ROOT}",row)
    duplicate=subprocess.run(self.command(harness,"register",repo,jobs,logs),text=True,capture_output=True,env=env)
    self.assertEqual(duplicate.returncode,0,duplicate.stdout+duplicate.stderr)
    self.assertIn("duplicate_attempt=1",duplicate.stdout); self.assertIn("registered=0",duplicate.stdout)
    self.assertEqual(len(jobs.read_text(encoding="utf-8").splitlines()),2)
    denied=subprocess.run(self.command(harness,"start",repo,jobs,logs,status="unknown"),text=True,capture_output=True,env=env)
    self.assertEqual(denied.returncode,69,denied.stdout+denied.stderr)
    self.assertIn("reason=nested-child-spawn-unknown",denied.stdout)
    unwritable=Path("/proc/1/stage-dispatch-v11")/f"{harness}.jobs.log"
    blocked_env={**env}; blocked_env.pop("AGENT_DISPATCH_JOBS",None)
    blocked=subprocess.run(self.command(harness,"register",repo,unwritable,logs),text=True,capture_output=True,env=blocked_env)
    self.assertEqual(blocked.returncode,73,blocked.stdout+blocked.stderr)
    self.assertIn("reason=global-registry-unwritable",blocked.stdout)
    self.assertIn("child_spawned=0",blocked.stdout)
 def test_all_wrapper_previews_are_visibly_non_attempts(self):
  for harness in ADAPTERS:
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
    env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"OPENCODE_CONFIG_CONTENT":"{}"}
    self.seed_parent(jobs,repo,harness=harness)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-parent-fixture"
    before=jobs.read_text(encoding="utf-8")
    result=subprocess.run(self.command(harness,"dry-run",repo,jobs,logs),
                          text=True,capture_output=True,env=env)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    self.assertIn("preview=1",result.stdout)
    self.assertIn("attempt_id=-",result.stdout)
    self.assertIn("launch_state=preview-only",result.stdout)
    self.assertIn("registered=0",result.stdout)
    self.assertIn("started=0",result.stdout)
    self.assertIn("child_spawned=0",result.stdout)
    self.assertEqual(jobs.read_text(encoding="utf-8"),before)
 def test_opencode_depth_two_fails_closed_without_a_live_parent(self):
  # register only, mirroring the codex/claude sibling contract test above:
  # --start also probes the real local opencode runtime projection
  # (adapters/opencode/bin/preflight.sh headless --check) before parent
  # binding is resolved, which is an environment prerequisite orthogonal to
  # exact-parent-binding and not something a unit fixture should fake.
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
   env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
        "AGENT_DISPATCH_JOBS":str(jobs),"OPENCODE_CONFIG_CONTENT":"{}"}
   result=subprocess.run(self.command("opencode","register",repo,jobs,logs),
                         text=True,capture_output=True,env=env)
   self.assertEqual(result.returncode,73,result.stdout+result.stderr)
   self.assertIn("reason=live-parent-not-found",result.stdout)
   self.assertIn("child_spawned=0",result.stdout)
   self.assertFalse(jobs.exists() and jobs.read_text().strip())
 def test_codex_owner_gets_scoped_nested_network_only_at_depth_one(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); logs=root/"logs";jobs=root/"jobs.log"
   claude_config=root/"claude"; (claude_config/"session-env").mkdir(parents=True)
   command=[sys.executable,str(ROOT/"adapters/codex/bin/dispatch-headless.py"),"--dry-run",
            "--worktree",str(repo),"--slug","codex-owner","--capability","autopilot-code",
            "--capability-mode","dev","--intensity","standard","--dispatch-depth","1","--worker-type","owner",
            "--unit","_kernel/owner","--assigned-contract","autopilot-code",
            "--model","gpt-test","--reasoning","low","--log-dir",str(logs),
            "--jobs",str(jobs)]
   env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
        "CLAUDE_CONFIG_DIR":str(claude_config),"AGENT_DISPATCH_JOBS":str(jobs)}
   for runtime_key in (
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
    "OPENCODE_SESSION_ID", "AGENT_DISPATCH_CALLER_HARNESS",
    "AGENT_DISPATCH_CURRENT_HARNESS",
   ):
    env.pop(runtime_key,None)
   result=subprocess.run(command,text=True,capture_output=True,env=env)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertIn("nested_headless_network=1",result.stdout)
   self.assertIn("completion_delivery=app-server-supervised",result.stdout)
   self.assertIn(f"supervisor_lease_file={root / 'supervisor-state' / 'preview-only.lease'}",result.stdout)
   self.assertIn(f"--lease-file {root / 'supervisor-state' / 'preview-only.lease'}",result.stdout)
   self.assertIn("preview=1",result.stdout)
   self.assertIn("attempt_id=-",result.stdout)
   self.assertIn("--network-access",result.stdout)
   self.assertIn(f"--writable-root {ROOT / '.dispatch'}",result.stdout)
   self.assertIn(f"--writable-root {jobs.parent.resolve()}",result.stdout)
   if (ROOT/".core-grounding").is_dir():
    self.assertIn(f"--writable-root {ROOT / '.core-grounding'}",result.stdout)
   self.assertIn(f"--writable-root {claude_config / 'session-env'}",result.stdout)
   self.assertIn("nested_owner_writable_dirs=",result.stdout)
   self.assertIn("nested_codex_home=",result.stdout)
   self.assertIn("broker_lifecycle=retired",result.stdout)
   self.assertIn("child_spawned=0",result.stdout)
   registered_command=command.copy()
   registered_command[registered_command.index("--dry-run")]="--register"
   registered=subprocess.run(
    registered_command,text=True,capture_output=True,
    env={**env,"AGENT_DISPATCH_JOBS":str(jobs)})
   self.assertEqual(registered.returncode,0,registered.stdout+registered.stderr)
   self.assertIn("child_spawned=0",registered.stdout)
   row=jobs.read_text(encoding="utf-8")
   self.assertIn("supervisor_lease=flock-v1",row)
   self.assertRegex(row,r"supervisor_lease_nonce=[0-9a-f]{64}(?:,|$)")
 def test_codex_and_claude_refuse_depth_two_before_any_row_without_live_parent(self):
  for harness in ("codex","claude"):
   with self.subTest(harness=harness),tempfile.TemporaryDirectory() as td:
    root=Path(td);repo,art=self.fixture(root);jobs=root/"jobs.log";logs=root/"logs"
    env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs)}
    result=subprocess.run(self.command(harness,"register",repo,jobs,logs),
                          text=True,capture_output=True,env=env)
    self.assertEqual(result.returncode,73,result.stdout+result.stderr)
    self.assertIn("reason=live-parent-not-found",result.stdout)
    self.assertIn("child_spawned=0",result.stdout)
    self.assertFalse(jobs.exists() and jobs.read_text().strip())
 def test_route_bound_depth_two_codex_gets_heartbeat_scope_without_network(self):
  spec=importlib.util.spec_from_file_location("codex_dispatch_scope",ROOT/"adapters/codex/bin/dispatch-headless.py")
  wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
  args=type("Args",(),{
   "worktree":"/work/repo","artifact_root":"/artifacts","nested_headless_network":False,
   "agent_home":ROOT,"dispatch_depth":2,"route_id":"rt-1","attempt_id":"att-stage-1",
   "sandbox":"workspace-write","resolved_model_settings":{"source":"inherit"},"approval":"inherit"})()
  command=wrapper.shell_command(args,Path("/prompt"),Path("/log"))
  self.assertIn(f"--add-dir {ROOT / '.dispatch'}",command)
  self.assertNotIn("network_access=true",command)
 def test_foreground_codex_child_reuses_checked_outer_sandbox(self):
  wrapper=self.load_wrapper("codex")
  args=type("Args",(),{
   "worktree":"/work/repo","artifact_root":"/artifacts","nested_headless_network":False,
   "agent_home":ROOT,"dispatch_depth":2,"route_id":"rt-1","attempt_id":"att-stage-1",
   "sandbox":"workspace-write","launch_lifecycle":"foreground-scoped",
   "parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write",
   "resolved_model_settings":{"source":"inherit"},"approval":"inherit"})()
  with mock.patch.dict(os.environ,{"AGENT_DISPATCH_CHILD":"1"},clear=False):
   command=wrapper.shell_command(args,Path("/prompt"),Path("/log"))
   self.assertEqual(wrapper.effective_runtime_sandbox(args),"danger-full-access")
  self.assertIn("--sandbox danger-full-access",command)
 def test_background_governor_does_not_hold_orchestrator_capture_pipes(self):
  for harness in ADAPTERS:
   with self.subTest(harness=harness):
    source=(ROOT/f"adapters/{harness}/bin/dispatch-headless.py").read_text(encoding="utf-8")
    marker=("return subprocess.Popen" if "return subprocess.Popen" in source
            else "proc = subprocess.Popen")
    start=source.index(marker)
    end=source.index("except OSError",start)
    launch=source[start:end]
    self.assertIn("stdin=subprocess.DEVNULL",launch)
    self.assertIn("stdout=subprocess.DEVNULL",launch)
    self.assertIn("stderr=subprocess.DEVNULL",launch)
    if harness in ("codex","claude"):
     self.assertIn('"pid_scope"] = "namespace-local"',source)
    else:
     self.assertIn('launch_metadata["pid_scope"] = "namespace-local"',source)
    self.assertIn('os.environ.get("AGENT_DISPATCH_CHILD") == "1"',source)
 def test_all_three_wrappers_install_the_same_detached_reap_observer(self):
  for harness in ADAPTERS:
   with self.subTest(harness=harness):
    source=(ROOT/f"adapters/{harness}/bin/dispatch-headless.py").read_text(
     encoding="utf-8")
    self.assertIn("launch_reap_watch",source)
    self.assertIn("if args.launch_lifecycle == DETACHED:",source)
    self.assertIn('{"reap_watch": "post-exit", "reap_watch_pid":',source)
 def test_parallel_batch_contract_projects_to_all_three_wrappers(self):
  for harness in ADAPTERS:
   with self.subTest(harness=harness):
    source=(ROOT/f"adapters/{harness}/bin/dispatch-headless.py").read_text(
     encoding="utf-8")
    self.assertIn("REPLICA_RESERVATION_ROW_KEYS",source)
    self.assertIn("replica_batch_expectation",source)
    self.assertIn("expected_reservation=args.replica_batch_expectation",source)
 def test_nested_codex_home_links_auth_but_keeps_mutable_state_local(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); source=root/"source"; source.mkdir(); worktree=root/"worktree"; worktree.mkdir()
   (source/"auth.json").write_text("{}\n",encoding="utf-8")
   (source/"config.toml").write_text("model = \"fixture\"\n",encoding="utf-8")
   spec=importlib.util.spec_from_file_location("codex_dispatch_home",ROOT/"adapters/codex/bin/dispatch-headless.py")
   wrapper=importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
   home=wrapper.prepare_nested_codex_home(worktree,source)
   self.assertTrue((home/"auth.json").is_symlink())
   self.assertEqual((home/"auth.json").resolve(),(source/"auth.json").resolve())
   self.assertTrue((home/"config.toml").is_symlink())
   self.assertTrue((home/"hearting").is_symlink())
   self.assertEqual((home/"hearting").resolve(),wrapper.resolve_agent_home().resolve())
   self.assertEqual(home.parent,worktree/".dispatch")
 def test_detached_selection_is_promoted_before_launch_without_failure_exposure(self):
  for harness in ("codex","claude"):
   for repetition in range(4):
    with self.subTest(harness=harness,repetition=repetition), tempfile.TemporaryDirectory() as td:
     root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"; fakebin=root/"bin"; fakebin.mkdir()
     fake=fakebin/harness; fake.write_text("#!/bin/sh\nexit 0\n",encoding="utf-8"); fake.chmod(0o755)
     self.seed_parent(jobs,repo,harness=harness)
     command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout","2"]
     wrapper=self.load_wrapper(harness); argv=["dispatch-headless.py",*command[2:]]
     resolution=wrapper.reconcile_launch_lifecycle(
      wrapper.DETACHED,{},evidence={
       "lifecycle_selector_source":"pid1-class",
       "lifecycle_nspid_width":"1",
       "lifecycle_pid1_class":"non-system-init",
      })
     env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CHILD":"1","AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture","XDG_STATE_HOME":str(root/"state")}
     stream=io.StringIO()
     patches=[mock.patch.dict(os.environ,env,clear=True),mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution)]
     if hasattr(wrapper,"check_runtime_projection"): patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
     if hasattr(wrapper,"ensure_runtime_home_projection"): patches.append(mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None))
     for patch in patches: patch.start()
     try:
      with redirect_stdout(stream): code=wrapper.main(argv)
     finally:
      for patch in reversed(patches): patch.stop()
     output=stream.getvalue()
     self.assertEqual(code,0,output)
     self.assertIn("launch_lifecycle_requested=detached",output)
     self.assertIn("launch_lifecycle=foreground-scoped",output)
     self.assertIn("launch_lifecycle_reselection=promoted-wrapper-scope",output)
     self.assertIn("launch_lifecycle_override=absent",output)
     self.assertIn("worker_exit=0",output)
     self.assertIn("worker_failure=-",output)
     self.assertNotIn("nested-sandbox-lifetime",output)
     row=jobs.read_text(encoding="utf-8")
     self.assertIn("launch_lifecycle_requested=detached",row)
     self.assertIn("launch_lifecycle=foreground-scoped",row)
     self.assertIn("launch_lifecycle_reselection=promoted-wrapper-scope",row)
     self.assertIn("launch_lifecycle_override=absent",row)
     self.assertNotIn("dead-nested-sandbox-lifetime",row)
     self.assertIn("parent_attempt_id=att-parent-fixture",row)
     self.assertIn("pid_host=",row);self.assertIn("pid_host_start=",row)
     self.assertIn("pgid=",row)
     if harness=="claude":
      self.assertIn("--output-format stream-json",output)
      self.assertIn("--no-session-persistence",output)
     self.assertIn("\topen\t",row)
 def test_opencode_depth_one_uses_same_prelaunch_lifecycle_promotion(self):
  for repetition in range(3):
   with self.subTest(repetition=repetition), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"; fakebin=root/"bin"; fakebin.mkdir()
    fake=fakebin/"opencode"; fake.write_text("#!/bin/sh\nexit 0\n",encoding="utf-8"); fake.chmod(0o755)
    wrapper=self.load_wrapper("opencode")
    argv=["dispatch-headless.py","--start","--worktree",str(repo),"--slug","opencode-owner",
          "--capability","autopilot-code","--capability-mode","dev","--intensity","standard",
          "--dispatch-depth","1","--worker-type","owner","--unit","_kernel/owner",
          "--assigned-contract","autopilot-code","--owner-harness","opencode",
          "--model","provider/test","--variant","low","--jobs",str(jobs),
          "--log-dir",str(logs),"--attempt-id",f"att-opencode-owner-{repetition}",
          "--foreground-timeout","2","--prompt-text","ok"]
    resolution=wrapper.reconcile_launch_lifecycle(
     wrapper.DETACHED,{},evidence={
      "lifecycle_selector_source":"pid1-class",
      "lifecycle_nspid_width":"1",
      "lifecycle_pid1_class":"non-system-init",
     })
    env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
         "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"OPENCODE_CONFIG_CONTENT":"{}",
         "XDG_STATE_HOME":str(root/"state")}
    stream=io.StringIO()
    patches=[mock.patch.dict(os.environ,env,clear=True),
             mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution)]
    if hasattr(wrapper,"check_runtime_projection"): patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
    for patch in patches: patch.start()
    try:
     with redirect_stdout(stream): code=wrapper.main(argv)
    finally:
     for patch in reversed(patches): patch.stop()
    output=stream.getvalue(); row=jobs.read_text(encoding="utf-8")
    self.assertEqual(code,0,output)
    self.assertIn("launch_lifecycle_requested=detached",output)
    self.assertIn("launch_lifecycle=foreground-scoped",output)
    self.assertIn("launch_lifecycle_reselection=promoted-wrapper-scope",output)
    self.assertIn("launch_lifecycle_override=absent",output)
    self.assertIn("worker_exit=0",output)
    self.assertNotIn("dead-nested-sandbox-lifetime",row)
    self.assertIn("launch_outcome=governed-process-reaped",row)
 def test_all_wrappers_exercise_parent_binding_callback_in_foreground_scope(self):
  for harness in ADAPTERS:
   with self.subTest(harness=harness):
    code,output,row,calls=self.run_parent_callback_cell(harness,True)
    self.assertEqual(code,0,output)
    self.assertGreaterEqual(calls,1,output)
    self.assertIn("launch_lifecycle=foreground-scoped",output)
    self.assertIn("worker_failure=parent-terminated",output)
    self.assertIn("note=dead-parent-terminated",row)
    self.assertIn("launch_outcome=governed-process-reaped",row)
 def test_opencode_parent_callback_runs_in_real_bubblewrap_pid_namespace(self):
  if os.environ.get("HEARTING_BWRAP_PID_NS") == "1":
   code,output,row,calls=self.run_parent_callback_cell("opencode",False)
   self.assertEqual(code,0,output)
   self.assertGreaterEqual(calls,1,output)
   self.assertIn("lifecycle_selector_source=pid1-class",row)
   self.assertIn("lifecycle_nspid_width=1",row)
   self.assertIn("pid_scope=namespace-local",row)
   self.assertIn("launch_lifecycle=foreground-scoped",output)
   self.assertIn("worker_failure=parent-terminated",output)
   self.assertIn("note=dead-parent-terminated",row)
   self.assertIn("launch_outcome=governed-process-reaped",row)
   return
  bwrap=shutil.which("bwrap")
  if not bwrap:
   self.skipTest("bubblewrap is unavailable")
  with tempfile.TemporaryDirectory() as dev_dir:
   Path(dev_dir,"null").write_bytes(b"")
   base=[bwrap,"--die-with-parent","--unshare-pid","--ro-bind","/","/",
         "--proc","/proc","--bind",dev_dir,"/dev","--bind","/tmp","/tmp"]
   probe=subprocess.run([*base,"true"],text=True,capture_output=True)
   if probe.returncode:
    self.skipTest("bubblewrap PID namespaces are unavailable: "+probe.stderr.strip())
   env={**os.environ,"HEARTING_BWRAP_PID_NS":"1"}
   result=subprocess.run(
    [*base,sys.executable,str(Path(__file__).resolve()),
     "AdapterV11Test.test_opencode_parent_callback_runs_in_real_bubblewrap_pid_namespace"],
    cwd=ROOT,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
 def test_exact_attempt_row_closure_is_isolated_for_both_wrappers(self):
  for harness in ("codex","claude"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    jobs=Path(td)/"jobs.log"; worktree="/fixture/worktree"; slug="stage"
    contract=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
              "execution_surface=registered-headless,registered_worker=1,"
              "fallback_hop=same-harness-headless")
    jobs.write_text(
     f"2026-07-20T00:00:00Z\topen\t/repo\t{worktree}\t{slug}\t{contract},attempt_id=att-a\n"
     f"2026-07-20T00:00:01Z\topen\t/repo\t{worktree}\t{slug}\t{contract},attempt_id=att-b\n",encoding="utf-8")
    wrapper=self.load_wrapper(harness)
    self.assertTrue(wrapper.close_job_row(jobs,slug,worktree,"timeout","","att-a"))
    rows=jobs.read_text(encoding="utf-8").splitlines()
    self.assertIn("\tdone\t",rows[0]); self.assertIn("note=dead-timeout",rows[0])
    self.assertIn("\topen\t",rows[1]); self.assertNotIn("note=",rows[1])
 def test_concurrent_codex_start_launches_exactly_one_child(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
   fakebin=root/"bin"; fakebin.mkdir(); count=root/"child-count"
   fake=fakebin/"codex"
   fake.write_text("#!/bin/sh\nprintf 'child\\n' >> \"$FAKE_CHILD_COUNT\"\n",encoding="utf-8")
   fake.chmod(0o755)
   command=self.command("codex","start",repo,jobs,logs)
   self.seed_parent(jobs,repo,harness="codex")
   spec=importlib.util.spec_from_file_location("codex_dispatch_concurrency",ROOT/"adapters/codex/bin/dispatch-headless.py")
   wrapper=importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
   argv=["dispatch-headless.py",*command[2:]]
   env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
        "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
        "AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CHILD":"1",
        "AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture",
        "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN":"1",
        "XDG_STATE_HOME":str(root/"state"),
        "FAKE_CHILD_COUNT":str(count)}
   # T-3: declare the scope explicitly (host-like, override honored) instead
   # of inheriting the test host's /proc, which may not be host-like in a
   # container — keeping the concurrency assertions as the real subject.
   resolution=wrapper.reconcile_launch_lifecycle(
    wrapper.DETACHED,{"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN":"1"},
    evidence={"lifecycle_selector_source":"host-like"})
   codes=[]
   def invoke(): codes.append(wrapper.main(argv))
   with mock.patch.dict(os.environ,env,clear=True), \
        mock.patch.object(wrapper,"check_runtime_projection",return_value=0), \
        mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None), \
        mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution):
    threads=[threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=20)
   self.assertEqual(sorted(codes),[0,0],codes)
   for _ in range(50):
    if count.exists(): break
    import time; time.sleep(.02)
   self.assertTrue(count.is_file(),codes)
   self.assertEqual(count.read_text(encoding="utf-8").splitlines(),["child"])
   self.assertEqual(len(jobs.read_text(encoding="utf-8").splitlines()),2)
   self.assertIn("launch_claimed=1",jobs.read_text(encoding="utf-8"))
   self.assertIn("pid_scope=namespace-local",jobs.read_text(encoding="utf-8"))
   self.assertIn("launch_lifecycle=detached",jobs.read_text(encoding="utf-8"))

 def test_all_wrappers_report_override_rejected_for_transient_scope(self):
  # W-1
  for harness in ("codex","claude"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"; fakebin=root/"bin"; fakebin.mkdir()
    fake=fakebin/harness; fake.write_text("#!/bin/sh\nexit 0\n",encoding="utf-8"); fake.chmod(0o755)
    self.seed_parent(jobs,repo,harness=harness)
    command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout","2"]
    wrapper=self.load_wrapper(harness); argv=["dispatch-headless.py",*command[2:]]
    resolution=wrapper.reconcile_launch_lifecycle(
     wrapper.DETACHED,{"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN":"1"},
     evidence={"lifecycle_selector_source":"pid1-class",
               "lifecycle_nspid_width":"1","lifecycle_pid1_class":"non-system-init"})
    env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
         "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CHILD":"1",
         "AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture",
         "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN":"1",
         "XDG_STATE_HOME":str(root/"state")}
    stream=io.StringIO()
    patches=[mock.patch.dict(os.environ,env,clear=True),
             mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution)]
    if hasattr(wrapper,"check_runtime_projection"): patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
    if hasattr(wrapper,"ensure_runtime_home_projection"): patches.append(mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None))
    for patch in patches: patch.start()
    try:
     with redirect_stdout(stream): code=wrapper.main(argv)
    finally:
     for patch in reversed(patches): patch.stop()
    output=stream.getvalue()
    self.assertEqual(code,0,output)
    self.assertIn("launch_lifecycle_override=rejected",output)
    self.assertIn("launch_lifecycle_reselection=override-rejected-transient-scope",output)
    row=jobs.read_text(encoding="utf-8")
    self.assertIn("launch_lifecycle_override=rejected",row)
    self.assertIn("launch_lifecycle_reselection=override-rejected-transient-scope",row)
 def test_all_wrappers_report_override_absent_without_override_env(self):
  # W-2
  for harness in ("codex","claude"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"; fakebin=root/"bin"; fakebin.mkdir()
    fake=fakebin/harness; fake.write_text("#!/bin/sh\nexit 0\n",encoding="utf-8"); fake.chmod(0o755)
    self.seed_parent(jobs,repo,harness=harness)
    command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout","2"]
    wrapper=self.load_wrapper(harness); argv=["dispatch-headless.py",*command[2:]]
    resolution=wrapper.reconcile_launch_lifecycle(
     wrapper.DETACHED,{},evidence={
      "lifecycle_selector_source":"pid1-class",
      "lifecycle_nspid_width":"1",
      "lifecycle_pid1_class":"non-system-init",
     })
    env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
         "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CHILD":"1",
         "AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture",
         "XDG_STATE_HOME":str(root/"state")}
    stream=io.StringIO()
    patches=[mock.patch.dict(os.environ,env,clear=True),
             mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution)]
    if hasattr(wrapper,"check_runtime_projection"): patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
    if hasattr(wrapper,"ensure_runtime_home_projection"): patches.append(mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None))
    for patch in patches: patch.start()
    try:
     with redirect_stdout(stream): code=wrapper.main(argv)
    finally:
     for patch in reversed(patches): patch.stop()
    output=stream.getvalue()
    self.assertEqual(code,0,output)
    self.assertIn("launch_lifecycle_override=absent",output)

if __name__=="__main__": unittest.main()
