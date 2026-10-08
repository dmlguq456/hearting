#!/usr/bin/env python3
import contextlib, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"utilities"))
import dispatch_contract as DC
sys.path.insert(0,str(ROOT/"tools"))
import fixture_processes  # noqa: E402

ADAPTERS={
 "codex":([sys.executable,str(ROOT/"adapters/codex/bin/dispatch-headless.py")],["--model","gpt-test","--reasoning","low"]),
 "claude":([sys.executable,str(ROOT/"adapters/claude/bin/dispatch-headless.py")],["--model","claude-test","--effort","low"]),
 "opencode":([sys.executable,str(ROOT/"adapters/opencode/bin/dispatch-headless.py")],["--model","provider/test","--variant","low"]),
}

# The fake workers exit at once; this only bounds a hung one. Two seconds was
# shorter than a process start on a loaded shared host, so a healthy launch was
# killed (worker_exit=-15, worker_failure=timeout) and the test failed at random.
FAKE_WORKER_TIMEOUT="30"


class AdapterV11Test(unittest.TestCase):
 def test_owner_binding_tuple_refusal_details_reach_all_wrappers(self):
  for harness in ADAPTERS:
   for depth,worker,raw in ((2,"stage",False),(1,"stage",False),(1,"owner",True),(2,"stage",True)):
    with self.subTest(harness=harness,depth=depth,worker=worker,raw=raw), tempfile.TemporaryDirectory() as td:
     root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
     wrapper=self.load_wrapper(harness)
     argv=["dispatch-headless.py","--register","--worktree",str(repo),"--slug","binding-detail",
           "--capability","autopilot-code","--capability-mode","dev","--intensity","standard",
           "--dispatch-depth",str(depth),"--worker-type",worker,"--owner","autopilot-code",
           "--assigned-contract","autopilot-code" if worker=="owner" else "code-execute",
           "--jobs",str(jobs),"--log-dir",str(logs),*ADAPTERS[harness][1]]
     if worker=="stage": argv.extend(["--worker-mode","dev/backend","--unit","dev/backend"])
     if depth==2:
      argv.extend(["--parent","owner","--parent-harness",harness,"--parent-transport","headless",
                   "--parent-sandbox","fixture","--nested-eligibility","supported","--eligibility-source","fixture"])
     if raw: argv.extend(["--route-file",str(root/"raw-route.json")])
     env={"PATH":os.environ.get("PATH",""),"HOME":str(root),"AGENT_HOME":str(ROOT),
          "AGENT_ARTIFACT_ROOT":str(art),"AGENT_DISPATCH_JOBS":str(jobs),
          "OPENCODE_CONFIG_CONTENT":"{}","XDG_STATE_HOME":str(root/"state")}
     stream=io.StringIO()
     with mock.patch.dict(os.environ,env,clear=True), \
          mock.patch.object(wrapper,"binding_from_environment",return_value=object()), \
          redirect_stdout(stream):
      result=wrapper.main(argv)
     output=stream.getvalue()
     self.assertEqual(result,65,output)
     self.assertIn("reason=owner-route-binding-tuple-invalid",output)
     self.assertIn(f"invalid_dispatch_depth={int(depth!=1)}",output)
     self.assertIn(f"invalid_worker_type={int(worker!='owner')}",output)
     self.assertIn(f"invalid_route_file_present={int(raw)}",output)
     self.assertIn("stage-dispatch-fallback.py",output)
     self.assertIn("--parallel-group",output)
     self.assertIn("--action start",output)
     self.assertIn("child_spawned=0",output)
     self.assertFalse(jobs.exists(),output)

 def setUp(self):
  self.parent_procs=[]
  self.ambient_worker_env={
   key:value for key,value in os.environ.items()
   if key.startswith("AGENT_DISPATCH_")
   or key.startswith("AGENT_OWNER_ROUTE_")
   or key.startswith("AGENT_ROUTE_")
   or key.startswith("AGENT_ARTIFACT_")
   or key in {"AGENT_MODEL_GOVERNOR_ROOT", "AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN"}
  }
  for key in self.ambient_worker_env: os.environ.pop(key,None)
 def tearDown(self):
  for proc in self.parent_procs:
   if proc.poll() is None: proc.kill()
   proc.wait()
  for key in list(os.environ):
   if (key.startswith("AGENT_DISPATCH_") or key.startswith("AGENT_OWNER_ROUTE_")
       or key.startswith("AGENT_ROUTE_") or key.startswith("AGENT_ARTIFACT_")
       or key in {"AGENT_MODEL_GOVERNOR_ROOT", "AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN"}):
    os.environ.pop(key,None)
  os.environ.update(self.ambient_worker_env)
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
 def test_a_sd156_1_claude_wrapper_registered_launch_writes_source_lineage_row_fields(self):
  """A-SD156-1, claude wrapper leg: a REAL registered `--register` launch
  (real `worker-route-guard.py validate` subprocess, real git repo, real
  route compiled by `capability-route.py`) on a first-parent descendant HEAD
  must write `launch_head`/`source_commit_sealed` onto the registry row --
  the fact `dispatch_contract.test.py::SourceLineageRowFieldsTest` only
  proves for the merge helper in isolation, never through an actual wrapper
  launch (the previous execute attempt's own documented gap)."""
  import importlib.util as _ilu
  route_spec=_ilu.spec_from_file_location("a_sd156_1_route_module",ROOT/"utilities/capability-route.py")
  ROUTE_MODULE=_ilu.module_from_spec(route_spec); route_spec.loader.exec_module(ROUTE_MODULE)
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
   agent_home=root/"agent-home"; (agent_home/"core").mkdir(parents=True)
   (agent_home/"core"/"CORE.md").write_text("fixture\n",encoding="utf-8")
   fakebin=root/"bin"; fakebin.mkdir()
   fake=fakebin/"claude"; fake.write_text("#!/bin/sh\nexec sleep 60\n",encoding="utf-8"); fake.chmod(0o755)
   env={"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
        "AGENT_HOME":str(agent_home),"AGENT_ARTIFACT_ROOT":str(art),
        "OPENCODE_CONFIG_CONTENT":"{}","HOME":str(root/"home")}
   (root/"home").mkdir()
   evidence_rows=[{
    "parent_harness":h,"parent_transport":"headless",
    "parent_sandbox":ROUTE_MODULE.WRAPPER_PARENT_SANDBOXES[h][0],
    "child_harness":h,"launch_authority":"conductor","status":"supported",
    "probe_source":f"{h}-fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"",
    "checked_worktree":str(repo.resolve()),"failure_scope":"none",
    "codex_command":"ok" if h=="codex" else "not-applicable","retry_on_isolated_worktree":0,
   } for h in ("codex","claude","opencode")]
   gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
         "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
   with mock.patch.dict(os.environ,env,clear=True):
    ROUTE_MODULE._forget_launch_path(ROOT)
    route=ROUTE_MODULE.compile_route(
     "autopilot-code","dev","standard",repo,art,
     signals=[],transport="headless",tracking="tracked",
     tracked_gate_evidence=gate,dispatch_evidence={"tuples":evidence_rows,"native_subagent":[]},
    )
   sealed=route["source_commit"]
   route_path=root/"route.json"; route_path.write_text(json.dumps(route),encoding="utf-8")
   # X (sealed) is compile()'s own HEAD; commit a first-parent child Y before
   # this node's worker guard validates -- exactly A-SD156-1's fixture shape.
   (repo/"y").write_text("y",encoding="utf-8")
   subprocess.run(["git","-C",str(repo),"add","y"],check=True)
   subprocess.run(["git","-C",str(repo),"commit","-qm","descendant commit"],check=True)
   observed=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
   self.assertNotEqual(observed,sealed)
   proc=subprocess.Popen(["sleep","60"]); self.parent_procs.append(proc)
   start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
   jobs.write_text(
    f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=owner,harness=claude,"
    "runtime_sandbox=adapter-default,"
    f"attempt_id=att-parent-fixture,pid={proc.pid},pid_start={start}\n")
   env["AGENT_DISPATCH_ATTEMPT_ID"]="att-parent-fixture"
   env["AGENT_DISPATCH_CHILD"]="1"
   node=next(n for n in route["nodes"] if n["id"]=="plan")
   cmd=[sys.executable,str(ROOT/"adapters/claude/bin/dispatch-headless.py"),
    "--register","--worktree",str(repo),"--slug","claude-plan-sd156-1",
    "--capability","autopilot-code","--capability-mode",route["capability_mode"],
    "--worker-mode",node["unit"],
    "--intensity",route["effective_intensity"],"--dispatch-depth","2","--parent","owner",
    "--worker-role","code-plan","--owner","autopilot-code",
    "--jobs",str(jobs),"--log-dir",str(logs),
    "--parent-harness","claude","--parent-transport","headless","--parent-sandbox","adapter-default",
    "--launch-authority","conductor","--nested-eligibility","supported",
    "--eligibility-source","claude-fixture","--fallback-ordinal","1",
    "--route-file",str(route_path),"--route-id",route["route_id"],
    "--route-hash",route["route_hash"],"--route-node","plan",
    "--registry-digest",route["registry_digest"],
    "--write-scope",";".join(node["write_scope"]),
    "--unit",node.get("unit",""),
    "--model-role",node["role"],"--model-profile",node["model_profile"],
   ]
   result=subprocess.run(cmd,text=True,capture_output=True,env=env)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   lines=[l for l in jobs.read_text(encoding="utf-8").splitlines() if "claude-plan-sd156-1" in l]
   self.assertEqual(len(lines),1,lines)
   row=lines[0]
   self.assertIn(f"launch_head={observed}",row)
   self.assertIn(f"source_commit_sealed={sealed}",row)
   self.assertIn("source_commit_distance=1",row)

 def test_launch_home_row_field_is_the_resolved_release_never_the_current_symlink(self):
  # SD-115 axis 4 (a): a wrapper launched under a mutable pointer (the real
  # shape is `<share>/hearting/current`, mirrored here as a plain symlink to
  # ROOT) must seal the row with the pointer's resolved target, not the
  # pointer text itself -- otherwise the row can never say which release it
  # was actually sealed against once the pointer moves on.
  for harness in ("codex", "claude", "opencode"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
    current=root/"current"; current.symlink_to(ROOT)
    env={**os.environ,"AGENT_HOME":str(current),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"OPENCODE_CONFIG_CONTENT":"{}"}
    self.seed_parent(jobs,repo,harness=harness)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-parent-fixture"
    registered=subprocess.run(self.command(harness,"register",repo,jobs,logs),text=True,capture_output=True,env=env)
    self.assertEqual(registered.returncode,0,registered.stdout+registered.stderr)
    row=jobs.read_text(encoding="utf-8")
    self.assertIn(f"launch_home={ROOT}",row)
    self.assertNotIn(f"launch_home={current}",row)
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
   fakebin=root/"bin"; fakebin.mkdir()
   fake_codex=fakebin/"codex"
   fake_codex.write_text("#!/bin/sh\n[ \"$1\" = app-server ] && [ \"$2\" = --help ]\n",encoding="utf-8")
   fake_codex.chmod(0o755)
   env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
        "CLAUDE_CONFIG_DIR":str(claude_config),"AGENT_DISPATCH_JOBS":str(jobs),
        "PATH":str(fakebin)+os.pathsep+os.environ.get("PATH","")}
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
   canonical_state_root=DC.dispatch_state_root(jobs)
   self.assertIn(f"--writable-root {canonical_state_root}",result.stdout)
   self.assertNotIn(f"--writable-root {ROOT / '.dispatch'}",result.stdout)
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
   "command_attempt_id":"att-stage-1","jobs_path":Path("/state/jobs.log"),
   "sandbox":"workspace-write","resolved_model_settings":{"source":"inherit"},"approval":"inherit"})()
  command=wrapper.shell_command(args,Path("/prompt"),Path("/log"))
  self.assertIn(f"--add-dir {DC.dispatch_state_root(args.jobs_path)}",command)
  self.assertNotIn(f"--add-dir {ROOT / '.dispatch'}",command)
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
 def test_nested_codex_home_links_auth_but_keeps_mutable_state_external(self):
  # `prepare_nested_codex_home` runs the *installed* runtime's projection
  # installer on purpose: the nested home's identity must follow the canonical
  # AGENT_HOME, not the source worktree holding the wrapper. So the fixture has
  # to name a projection root of its own. Without one this test read whatever
  # release the developer happened to have installed -- runtime-owned state as a
  # fixture -- and any environment without one (the isolated suite profile,
  # CI) resolved `$XDG_DATA_HOME/hearting/current` and died on the missing
  # installer. This checkout is a valid harness root, so pin AGENT_HOME to it
  # and keep HOME/CODEX_HOME inside the tempdir.
  #
  # The env is an EXPLICIT minimal dict, not `{**os.environ, ...}`: with a
  # `{**os.environ}` base, `clear=True` clears nothing and the ambient shell
  # rides through. That matters concretely -- install-runtime-projection.sh
  # skips the managed launcher for a non-default CODEX_HOME only *while*
  # HARNESS_BIN_DIR is unset, so an ambient HARNESS_BIN_DIR would make this
  # test install a launcher into the developer's real bin directory. PATH is
  # named because the script shells out to python3; nothing else is inherited.
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); source=root/"source"; source.mkdir(); worktree=root/"worktree"; worktree.mkdir()
   (source/"auth.json").write_text("{}\n",encoding="utf-8")
   (source/"config.toml").write_text("model = \"fixture\"\n",encoding="utf-8")
   fixture_home=root/"home"; fixture_home.mkdir()
   state=root/"dispatch"; state.mkdir(); jobs=state/"jobs.log"; jobs.write_text("")
   spec=importlib.util.spec_from_file_location("codex_dispatch_home",ROOT/"adapters/codex/bin/dispatch-headless.py")
   wrapper=importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
   env={"PATH":os.environ.get("PATH",""),"HOME":str(fixture_home),
        "AGENT_HOME":str(ROOT),"CODEX_HOME":str(source),"AGENT_DISPATCH_JOBS":str(jobs),
        "PYTHONDONTWRITEBYTECODE":"1"}
   with mock.patch.dict(os.environ,env,clear=True):
    home=wrapper.prepare_nested_codex_home(worktree,source,jobs=jobs)
    agent_home=wrapper.resolve_agent_home().resolve()
   self.assertEqual(agent_home,ROOT.resolve())
   self.assertTrue((home/"auth.json").is_symlink())
   self.assertEqual((home/"auth.json").resolve(),(source/"auth.json").resolve())
   self.assertTrue((home/"config.toml").is_symlink())
   self.assertTrue((home/"hearting").is_symlink())
   self.assertEqual((home/"hearting").resolve(),agent_home)
   self.assertTrue(home.is_relative_to(state/"homes"/"codex"))
   # Mutable runtime state stays outside the source repository: the credential is a link
   # out, never a copy, nothing was written into the source home, and the
   # fixture HOME is still empty -- the launcher branch really was skipped.
   self.assertFalse((home/"auth.json").resolve().is_relative_to(worktree))
   self.assertEqual(
    sorted(p.name for p in source.iterdir()),["auth.json","config.toml"])
   self.assertEqual(sorted(p.name for p in fixture_home.iterdir()),[])
 def test_a_release_switch_during_an_owner_leaves_its_children_on_its_release(self):
  # BC 2026-10-07: the owner-only home was linked to release R1 while the owner tree carried the
  # moving pointer; after the pointer moved to R2, every child start failed the projection check.
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); source=root/"source"; source.mkdir(); worktree=root/"worktree"; worktree.mkdir()
   (source/"auth.json").write_text("{}\n",encoding="utf-8")
   fixture_home=root/"home"; fixture_home.mkdir()
   state=root/"dispatch"; state.mkdir(); jobs=state/"jobs.log"; jobs.write_text("")
   later=root/"release-2"; (later/"core").mkdir(parents=True); (later/"core"/"CORE.md").write_text("fixture\n")
   pointer=root/"pointer"; pointer.symlink_to(ROOT)
   spec=importlib.util.spec_from_file_location("codex_dispatch_switch",ROOT/"adapters/codex/bin/dispatch-headless.py")
   wrapper=importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
   env={"PATH":os.environ.get("PATH",""),"HOME":str(fixture_home),
        "AGENT_HOME":str(pointer),"CODEX_HOME":str(source),"AGENT_DISPATCH_JOBS":str(jobs),
        "PYTHONDONTWRITEBYTECODE":"1"}
   with mock.patch.dict(os.environ,env,clear=True):
    self.assertEqual(wrapper.resolve_agent_home(),pointer)
    # What `main` sets: the release the pointer names now, for the whole tree.
    args=SimpleNamespace(agent_home=wrapper.sealed_launch_home(wrapper.resolve_agent_home()))
    self.assertEqual(args.agent_home,ROOT.resolve())
    args.nested_codex_home=wrapper.prepare_nested_codex_home(
     worktree,source,jobs=jobs,projection_root=args.agent_home)
   pointer.unlink(); pointer.symlink_to(later)            # the release switch
   child={**wrapper.child_runtime_homes(args,None),"AGENT_HOME":str(args.agent_home)}
   self.assertEqual(child,{"CODEX_HOME":str(args.nested_codex_home),"AGENT_HOME":str(ROOT.resolve())})
   self.assertEqual(wrapper.child_runtime_homes(SimpleNamespace(nested_codex_home=None),root/"profile"),
                    {"CODEX_HOME":str(root/"profile")})
   self.assertEqual(wrapper.child_runtime_homes(SimpleNamespace(nested_codex_home=None),None),{})
   def check(agent_home):
    return subprocess.run([str(ROOT/"adapters/codex/bin/check-runtime-projection.sh")],
     env={**env,"AGENT_HOME":agent_home,"CODEX_HOME":child["CODEX_HOME"],"AGENT_SESSION_ROLE":"worker",
          "CODEX_RUNTIME_PROJECTION_FAST":"1","CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY":"1"},
     capture_output=True,text=True,check=False)
   kept=check(child["AGENT_HOME"])
   self.assertEqual(kept.returncode,0,kept.stdout+kept.stderr)
   # An owner of the new release in the same worktree gets its own home and never re-links
   # this one; liveness reads the sessions of both, and of the older worktree-only home.
   with mock.patch.dict(os.environ,env,clear=True):
    other=wrapper.nested_codex_home_path(worktree,jobs,later)
    legacy=wrapper.nested_codex_home_path(worktree,jobs)
   self.assertNotEqual(other,args.nested_codex_home)
   self.assertEqual({other.parent,legacy.parent},{args.nested_codex_home.parent})
   spec=importlib.util.spec_from_file_location("codex_liveness_switch",ROOT/"adapters/codex/bin/dispatch-liveness.py")
   live=importlib.util.module_from_spec(spec); spec.loader.exec_module(live)
   other.mkdir(parents=True)
   with mock.patch.dict(os.environ,env,clear=True):
    stores=live.sessions_dirs_for("transport=headless","s",ROOT,root/"default",str(worktree),jobs=jobs)
   self.assertTrue({args.nested_codex_home/"sessions",other/"sessions",legacy/"sessions"} <= set(stores),stores)
   moved=check(str(pointer))                              # what the child used to get
   self.assertNotEqual(moved.returncode,0,moved.stdout)
   self.assertIn("check=hearting:failed",moved.stdout)
   # A projection failure now names its reason for the launcher's receipt.
   out=io.StringIO()
   with mock.patch.object(wrapper.subprocess,"run",return_value=subprocess.CompletedProcess(
     [],1,moved.stdout,"")),contextlib.redirect_stdout(out):
    rc=wrapper.check_runtime_projection(str(worktree),False)
   self.assertEqual(rc,1)
   lines=out.getvalue().splitlines()
   self.assertIn("reason=codex-runtime-projection-mismatch",lines)
   self.assertTrue(any(line.startswith("detail=check=hearting:failed") for line in lines))
   named=io.StringIO()
   with mock.patch.object(wrapper.subprocess,"run",return_value=subprocess.CompletedProcess(
     [],69,"check=failed\nreason=codex-home-unset\n","")),contextlib.redirect_stdout(named):
    self.assertEqual(wrapper.check_runtime_projection(str(worktree),False),69)
   self.assertEqual([l for l in named.getvalue().splitlines() if l.startswith("reason=")],["reason=codex-home-unset"])
 def test_nested_codex_home_foreign_worktree_uses_existing_state_scope(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); source=root/"source"; source.mkdir(); worktree=root/"nas"; worktree.mkdir()
   (source/"auth.json").write_text("{}\n")
   (source/"config.toml").write_text('model = "fixture"\n')
   home=root/"user"; home.mkdir(); state=root/"dispatch"; state.mkdir(); jobs=state/"jobs.log"; jobs.write_text("")
   preferred=worktree/".dispatch"/"nested-codex-home"; preferred.mkdir(parents=True); preferred.chmod(0o700)
   sentinel=preferred/"preserve"; sentinel.write_text("foreign directory contents")
   before=(sentinel.read_bytes(),preferred.stat().st_mode,sorted(p.name for p in preferred.iterdir()))
   wrapper=self.load_wrapper("codex")
   original_stat=Path.stat
   def mapped_stat(path,*args,**kwargs):
    info=original_stat(path,*args,**kwargs)
    if path in (preferred,worktree):
     fields=list(info); fields[4]=os.geteuid()+1; return os.stat_result(fields)
    return info
   env={"PATH":os.environ.get("PATH",""),"HOME":str(home),"AGENT_HOME":str(ROOT),
        "CODEX_HOME":str(source),"AGENT_DISPATCH_JOBS":str(jobs),"PYTHONDONTWRITEBYTECODE":"1"}
   with mock.patch.dict(os.environ,env,clear=True), mock.patch.object(Path,"stat",mapped_stat):
    predicted=wrapper.nested_codex_home_path(worktree,jobs,ROOT)
    chosen=wrapper.prepare_nested_codex_home(worktree,source,jobs=jobs)
    self.assertEqual(chosen,predicted)
    self.assertEqual(wrapper.prepare_nested_codex_home(worktree,source,jobs=jobs),chosen)
    args=type("Args",(),{"nested_headless_network":True,"jobs_path":jobs})()
    self.assertTrue(any(chosen.is_relative_to(p) for p in wrapper.nested_owner_writable_dirs(args)))
   self.assertTrue(chosen.is_relative_to(state/"homes"/"codex"))
   self.assertEqual(chosen.stat().st_uid,os.geteuid())
   self.assertEqual((sentinel.read_bytes(),preferred.stat().st_mode,sorted(p.name for p in preferred.iterdir())),before)
   self.assertEqual((chosen/"auth.json").resolve(),source/"auth.json")
   self.assertEqual((chosen/"config.toml").resolve(),source/"config.toml")
   self.assertEqual(sorted(p.name for p in source.iterdir()),["auth.json","config.toml"])
   self.assertEqual(jobs.read_text(),"")

 def test_nested_codex_home_missing_on_foreign_mount_selects_without_writes(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); worktree=root/"nas"; worktree.mkdir(); state=root/"dispatch"; state.mkdir()
   wrapper=self.load_wrapper("codex"); original_stat=Path.stat
   def mapped_stat(path,*args,**kwargs):
    info=original_stat(path,*args,**kwargs)
    if path==worktree:
     fields=list(info); fields[4]=os.geteuid()+1; return os.stat_result(fields)
    return info
   with mock.patch.object(Path,"stat",mapped_stat):
    chosen=wrapper.nested_codex_home_path(worktree,state/"jobs.log")
   self.assertTrue(chosen.is_relative_to(state))
   self.assertEqual(list(worktree.iterdir()),[])
   self.assertEqual(list(state.iterdir()),[])

 def test_external_runtime_home_keeps_exact_attempt_session_and_repository_clean(self):
  import hashlib
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); state=root/"dispatch"; state.mkdir()
   jobs=state/"jobs.log"; jobs.write_text(""); default=root/"default-sessions"
   wrapper=self.load_wrapper("codex")
   before=subprocess.run(["git","-C",str(repo),"status","--porcelain","--untracked-files=all"],
                         text=True,capture_output=True,check=True).stdout
   with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)},clear=False):
    selected=wrapper.nested_codex_home_path(repo,jobs)
    self.assertIsNone(wrapper.ensure_runtime_home_projection(repo))
    live=importlib.util.spec_from_file_location("codex_liveness_external_fixture",
         ROOT/"adapters/codex/bin/dispatch-liveness.py")
    module=importlib.util.module_from_spec(live); live.loader.exec_module(module)
    sid="session-exact-attempt-17"; attempt="att-exact-attempt-17"
    session_dir=selected/"sessions"; session_dir.mkdir(parents=True)
    transcript=session_dir/f"{sid}.jsonl"
    transcript.write_text(json.dumps({"type":"session_meta","payload":{"id":sid,"cwd":str(repo),
                           "attempt_id":attempt,"pid":os.getpid()}})+"\n",encoding="utf-8")
    dirs=module.sessions_dirs_for("", "worker", ROOT, default, str(repo),jobs=jobs)
    self.assertEqual(dirs[0],selected/"sessions")
    self.assertIn(repo/".dispatch"/"codex-home"/"sessions",dirs)
    self.assertEqual(module.locate_latest_for_worktree_dirs(dirs,str(repo)),transcript)
    observed=json.loads(transcript.read_text().splitlines()[0])["payload"]
    self.assertEqual(transcript.stem,sid)
    self.assertEqual(observed["attempt_id"],attempt)
    self.assertEqual(observed["pid"],os.getpid())
    self.assertEqual(module.transcript_cwd(transcript),str(repo))
    exact=module.recorded_attempt_state({"attempt_id":attempt,"pid":str(os.getpid()),
        "pid_start":module.process_start_ticks(os.getpid())},time.time(),ROOT)
    self.assertEqual(exact["state"],"working")
    self.assertEqual(exact["pid"],os.getpid())
    self.assertEqual(module.sessions_dirs_for("profile=lab", "worker", ROOT, default,
                     str(repo),jobs=jobs),[module.resolve_dispatch_state_root(ROOT,explicit_jobs=jobs)/"homes"/"worker.lab"/"sessions"])
   after=subprocess.run(["git","-C",str(repo),"status","--porcelain","--untracked-files=all"],
                        text=True,capture_output=True,check=True).stdout
   self.assertEqual(after,before)
   self.assertFalse((repo/".dispatch").exists())

 def test_nested_codex_home_foreign_fallback_and_symlink_escape_stay_untouched(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); worktree=root/"nas"; worktree.mkdir(); state=root/"dispatch"; state.mkdir(); outside=root/"outside"; outside.mkdir()
   preferred=worktree/".dispatch"/"nested-codex-home"; preferred.parent.mkdir(); preferred.symlink_to(outside,target_is_directory=True)
   (state/"homes").symlink_to(outside,target_is_directory=True)
   wrapper=self.load_wrapper("codex")
   with self.assertRaisesRegex(DC.DispatchContractError,"no user-owned runtime home"):
    wrapper.nested_codex_home_path(worktree,state/"jobs.log")
   self.assertTrue(preferred.is_symlink()); self.assertTrue((state/"homes").is_symlink())
   self.assertEqual(list(outside.iterdir()),[])
   (state/"homes").unlink()  # owned fixture cleanup only
   original_stat=Path.stat
   def mapped_stat(path,*args,**kwargs):
    info=original_stat(path,*args,**kwargs)
    if path==state:
     fields=list(info); fields[4]=os.geteuid()+1; return os.stat_result(fields)
    return info
   with mock.patch.object(Path,"stat",mapped_stat), self.assertRaisesRegex(DC.DispatchContractError,"no user-owned runtime home"):
    wrapper.nested_codex_home_path(worktree,state/"jobs.log")
   self.assertEqual(list(state.iterdir()),[])
 def test_foreign_nested_home_dryrun_receipt_and_both_launch_argv_share_state_scope(self):
  for supervised in (True,False):
   with self.subTest(supervised=supervised),tempfile.TemporaryDirectory() as td:
    root=Path(td);repo,art=self.fixture(root);state=root/"state";state.mkdir();jobs=state/"jobs.log";logs=state/"logs"
    wrapper=self.load_wrapper("codex"); original_stat=Path.stat
    def mapped_stat(path,*args,**kwargs):
     info=original_stat(path,*args,**kwargs)
     if path==repo:
      fields=list(info);fields[4]=os.geteuid()+1;return os.stat_result(fields)
     return info
    argv=["--dry-run","--worktree",str(repo),"--slug","nas-owner","--capability","autopilot-code",
          "--capability-mode","dev","--intensity","standard","--dispatch-depth","1","--worker-type","owner",
          "--unit","_kernel/owner","--assigned-contract","autopilot-code","--model","gpt-test",
          "--reasoning","low","--log-dir",str(logs),"--jobs",str(jobs)]
    env={"PATH":os.environ.get("PATH",""),"HOME":str(root/"user"),"AGENT_HOME":str(ROOT),
         "AGENT_ARTIFACT_ROOT":str(art),"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CALLER_HARNESS":"codex"}
    out=io.StringIO()
    with mock.patch.dict(os.environ,env,clear=True),mock.patch.object(Path,"stat",mapped_stat), \
         mock.patch.object(wrapper,"codex_app_server_available",return_value=supervised),contextlib.redirect_stdout(out):
     expected=wrapper.nested_codex_home_path(repo,jobs,ROOT)
     code=wrapper.main(argv)
    self.assertEqual(code,0,out.getvalue())
    fields=dict(line.split("=",1) for line in out.getvalue().splitlines() if "=" in line)
    self.assertEqual(fields["nested_codex_home"],str(expected))
    tokens=__import__('shlex').split(fields["command"])
    flag="--writable-root" if supervised else "--add-dir"
    grants=[Path(tokens[i+1]) for i,t in enumerate(tokens[:-1]) if t==flag]
    self.assertTrue(any(expected.is_relative_to(p) for p in grants),grants)
    self.assertIn(state,grants)
    self.assertIn(str(repo),tokens)
    self.assertFalse(expected.exists())
    self.assertFalse((repo/".dispatch"/"nested-codex-home").exists())
 def test_detached_selection_is_promoted_before_launch_without_failure_exposure(self):
  for harness in ("codex","claude"):
   for repetition in range(4):
    with self.subTest(harness=harness,repetition=repetition), tempfile.TemporaryDirectory() as td:
     root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"; fakebin=root/"bin"; fakebin.mkdir()
     fake=fakebin/harness; fake.write_text("#!/bin/sh\n[ \"$1\" = app-server ] && exit 69\nexit 0\n",encoding="utf-8"); fake.chmod(0o755)
     self.seed_parent(jobs,repo,harness=harness)
     command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout",FAKE_WORKER_TIMEOUT]
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
     # A one-element NSpid vector (this test's own procfs view) proves only
     # local identity, never an outer host mapping; neither wrapper may
     # publish a pid_host* claim from it.
     self.assertNotIn("pid_host=",row);self.assertNotIn("pid_host_start=",row)
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
          "--foreground-timeout",FAKE_WORKER_TIMEOUT,"--prompt-text","ok"]
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
  base=[bwrap,"--die-with-parent","--unshare-pid","--ro-bind","/","/",
        "--proc","/proc","--dev","/dev","--tmpfs","/tmp"]
  tmpdir=os.environ.get("TMPDIR","").rstrip("/")
  if tmpdir and not tmpdir.startswith("/tmp") and os.path.isdir(tmpdir):
   base += ["--bind",tmpdir,tmpdir]
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
 def test_row_rewrites_never_truncate_the_registry_a_reader_holds(self):
  # A lock-free reader (the owner supervisor's first row lookup) opened the
  # registry just before the launcher recorded the child's pid. An in-place
  # rewrite truncated that file under it and the owner died on startup with
  # attempt-row-not-unique. The rewrite must replace the file instead.
  for harness in ("codex","claude","opencode"):
   for action in ("annotate","close"):
    with self.subTest(harness=harness,action=action), tempfile.TemporaryDirectory() as td:
     jobs=Path(td)/"jobs.log"; worktree="/fixture/worktree"; slug="owner"
     contract=("attempt_schema_version=2,dispatch_depth=1,transport=headless,"
               "execution_surface=registered-headless,registered_worker=1,"
               "fallback_hop=same-harness-headless")
     original=f"2026-07-20T00:00:00Z\topen\t/repo\t{worktree}\t{slug}\t{contract},attempt_id=att-a\n"
     jobs.write_text(original,encoding="utf-8")
     wrapper=self.load_wrapper(harness)
     with jobs.open(encoding="utf-8") as reader:
      if action=="annotate":
       self.assertTrue(wrapper.annotate_job_row(jobs,slug,worktree,"pid=42","att-a"))
      else:
       self.assertTrue(wrapper.close_job_row(jobs,slug,worktree,"timeout","","att-a"))
      self.assertEqual(reader.read(),original)
     current=jobs.read_text(encoding="utf-8")
     self.assertIn("pid=42" if action=="annotate" else "note=dead-timeout",current)
     self.assertEqual(len(current.splitlines()),1)
 def test_launch_receipt_states_the_parent_next_action(self):
  # The parent's whole model-visible delivery contract is `parent_next`
  # (`utilities/parent_next_directive.py`). Pin it at the wrapper, not just at
  # the mapping: a receipt that stops printing it silently returns the rule to
  # prose, which is the failure mode this replaced. Two wrappers actually
  # launch here (review round 2, M-3): the mapping is shared, but "is it
  # printed, with agent_home already resolved" is per wrapper.
  for harness, fake_cli in (("codex","codex"),("opencode","opencode"),("claude","claude")):
   with self.subTest(harness=harness):
    self._assert_receipt_states_next(harness, fake_cli)

 def _assert_receipt_states_next(self, harness, fake_cli):
  with tempfile.TemporaryDirectory() as td:
   try:
    self._receipt_states_next_in(td, harness, fake_cli)
   finally:
    # The detached launch leaves its own helpers running; stop them before the
    # temp dir goes, or a late registry write races its removal.
    fixture_processes.reap(td)

 def _receipt_states_next_in(self, td, harness, fake_cli):
  root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
  fakebin=root/"bin"; fakebin.mkdir(); count=root/"child-count"
  fake=fakebin/fake_cli
  fake.write_text("#!/bin/sh\nprintf 'child\\n' >> \"$FAKE_CHILD_COUNT\"\n",encoding="utf-8")
  fake.chmod(0o755)
  command=self.command(harness,"start",repo,jobs,logs)
  self.seed_parent(jobs,repo,harness=harness)
  spec=importlib.util.spec_from_file_location(f"{harness}_dispatch_next",ROOT/f"adapters/{harness}/bin/dispatch-headless.py")
  wrapper=importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
  argv=["dispatch-headless.py",*command[2:]]
  env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
       "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
       "AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_CHILD":"1",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture",
       "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN":"1",
       "XDG_STATE_HOME":str(root/"state"),
       "FAKE_CHILD_COUNT":str(count)}
  resolution=wrapper.reconcile_launch_lifecycle(
   wrapper.DETACHED,{"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN":"1"},
   evidence={"lifecycle_selector_source":"host-like"})
  buffer=io.StringIO()
  # Not every wrapper defines every projection helper (opencode has no
  # `ensure_runtime_home_projection`): patch what exists, never invent one.
  optional=[mock.patch.object(wrapper,name,return_value=value)
            for name,value in (("check_runtime_projection",0),
                               ("ensure_runtime_home_projection",None))
            if hasattr(wrapper,name)]
  with contextlib.ExitStack() as stack:
   stack.enter_context(mock.patch.dict(os.environ,env,clear=True))
   for patch in optional: stack.enter_context(patch)
   stack.enter_context(mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution))
   stack.enter_context(redirect_stdout(buffer))
   code=wrapper.main(argv)
  receipt=buffer.getvalue()
  self.assertEqual(code,0,receipt)
  self.assertIn("child_spawned=1",receipt)
  fields=dict(line.split("=",1) for line in receipt.splitlines() if "=" in line)
  sys.path.insert(0,str(ROOT/"utilities"))
  import parent_next_directive as pnd
  delivery=fields.get("parent_completion_delivery","")
  # The delivery this launch actually resolved must be a classified one, and
  # the three printed lines must be exactly what the shared contract renders
  # for it -- an assertion that admits both actions would pass while a real
  # carrier silently degraded to `delivery-unrecognized`.
  self.assertIn(delivery,set(pnd.CARRIER_DELIVERIES)|set(pnd.WAIT_DELIVERIES),receipt)
  expected=pnd.receipt_lines(delivery,fields.get("attempt_id"),agent_home=ROOT)
  self.assertEqual(
   [f"parent_next={fields.get('parent_next')}",
    f"parent_next_reason={fields.get('parent_next_reason')}",
    f"parent_next_command={fields.get('parent_next_command')}"],
   expected,receipt)
 def test_concurrent_codex_start_launches_exactly_one_child(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
   fakebin=root/"bin"; fakebin.mkdir(); count=root/"child-count"
   fake=fakebin/"codex"
   fake.write_text("#!/bin/sh\n[ \"$1\" = app-server ] && exit 69\nprintf 'child\\n' >> \"$FAKE_CHILD_COUNT\"\n",encoding="utf-8")
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
    command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout",FAKE_WORKER_TIMEOUT]
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
    command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout",FAKE_WORKER_TIMEOUT]
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

 def test_governor_reservation_transfer_records_deterministic_launch_outcome(self):
  # Phase 8: a governor-reservation-transfer terminal row must always carry a
  # deterministic post-exit launch_outcome (never left unverified), so the
  # checked post-exit-receipt fallback is not poisoned by an incomplete row.
  for harness in ("codex","claude","opencode"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
    fakebin=root/"bin"; fakebin.mkdir()
    fake=fakebin/harness
    fake.write_text("#!/bin/sh\nexec sleep 60\n",encoding="utf-8"); fake.chmod(0o755)
    wrapper=self.load_wrapper(harness)
    import json
    from dispatch_capacity_evidence import launch_scope
    quota_home=root/"quota-home"; quota_home.mkdir()
    (quota_home/".claude.json").write_text(json.dumps({"oauthAccount": {
     "accountUuid":"fixture-account", "organizationUuid":"fixture-org"}}))
    if harness=="opencode":
     attempt_id="att-opencode-governor-transfer"
     argv=["dispatch-headless.py","--start","--worktree",str(repo),"--slug","opencode-owner",
           "--capability","autopilot-code","--capability-mode","dev","--intensity","standard",
           "--dispatch-depth","1","--worker-type","owner","--unit","_kernel/owner",
           "--assigned-contract","autopilot-code","--owner-harness","opencode",
           "--model","provider/test","--variant","low","--jobs",str(jobs),
           "--log-dir",str(logs),"--attempt-id",attempt_id,
           "--foreground-timeout","5","--prompt-text","ok"]
     env_extra={"OPENCODE_CONFIG_CONTENT":"{}"}
    else:
     attempt_id=f"att-{harness}-fixture-0001"
     self.seed_parent(jobs,repo,harness=harness)
     command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout","5"]
     argv=["dispatch-headless.py",*command[2:]]
     env_extra={"AGENT_DISPATCH_CHILD":"1","AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture"}
    resolution=wrapper.reconcile_launch_lifecycle(
     wrapper.DETACHED,{},evidence={
      "lifecycle_selector_source":"pid1-class",
      "lifecycle_nspid_width":"1",
      "lifecycle_pid1_class":"non-system-init",
     })
    env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
         "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"XDG_STATE_HOME":str(root/"state"),
         "CLAUDE_CONFIG_DIR":str(quota_home), **env_extra}
    env.pop("AGENT_MODEL_GOVERNOR_ROOT",None)
    stream=io.StringIO()
    patches=[mock.patch.dict(os.environ,env,clear=True),
             mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution),
             mock.patch.object(
              wrapper,"wait_governor_reservation_claim",
              side_effect=wrapper.DispatchContractError("reservation-owner-mismatch")),
             mock.patch.object(wrapper,"read_launch_fence_failure",return_value=(None,True))]
    if hasattr(wrapper,"check_runtime_projection"): patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
    if hasattr(wrapper,"ensure_runtime_home_projection"): patches.append(mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None))
    for patch in patches: patch.start()
    try:
     with redirect_stdout(stream): code=wrapper.main(argv)
    finally:
     for patch in reversed(patches): patch.stop()
    output=stream.getvalue()
    self.assertEqual(code,75,output)
    self.assertIn("reason=reservation-owner-mismatch",output)
    row=jobs.read_text(encoding="utf-8")
    self.assertIn("note=dead-governor-reservation-transfer",row)
    self.assertIn("launch_outcome=governed-process-reaped",row)
    self.assertIn("group_reap_proof="+DC.GROUP_REAP_PROOF,row)
    fields=[line.split("\t") for line in row.splitlines() if line.strip()]
    metadata=next(
     DC.parse_registry_metadata(f[5])
     for f in fields
     if len(f)==6 and DC.row_has_attempt(f[5],attempt_id)
    )
    if harness=="claude":
     self.assertEqual(metadata.get("quota_scope"),launch_scope(harness,env)["quota_scope"])
    else:
     self.assertNotIn("quota_scope",metadata)
    self.assertNotEqual(DC.post_exit_receipt_reason(metadata),"")
    quiescence=DC.attempt_process_quiescence(metadata,terminal_receipt=True)
    self.assertNotEqual(quiescence.reason,"post-exit-receipt-incomplete")

 def test_governor_reservation_transfer_records_never_launched_when_fence_not_released(self):
  # Phase 8 gap fix: a BlockingIOError on the non-blocking fence read means
  # the write end is still open (the child never reached/released the
  # fence), so the row must record the stronger, honest
  # launch_outcome=never-launched -- never the reaped-and-proved-empty
  # claim -- even though the process group is provably empty after kill.
  for harness in ("codex","claude","opencode"):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    root=Path(td); repo,art=self.fixture(root); jobs=root/"jobs.log"; logs=root/"logs"
    fakebin=root/"bin"; fakebin.mkdir()
    fake=fakebin/harness
    fake.write_text("#!/bin/sh\nexec sleep 60\n",encoding="utf-8"); fake.chmod(0o755)
    wrapper=self.load_wrapper(harness)
    if harness=="opencode":
     attempt_id="att-opencode-governor-transfer-never-launched"
     argv=["dispatch-headless.py","--start","--worktree",str(repo),"--slug","opencode-owner",
           "--capability","autopilot-code","--capability-mode","dev","--intensity","standard",
           "--dispatch-depth","1","--worker-type","owner","--unit","_kernel/owner",
           "--assigned-contract","autopilot-code","--owner-harness","opencode",
           "--model","provider/test","--variant","low","--jobs",str(jobs),
           "--log-dir",str(logs),"--attempt-id",attempt_id,
           "--foreground-timeout","5","--prompt-text","ok"]
     env_extra={"OPENCODE_CONFIG_CONTENT":"{}"}
    else:
     attempt_id=f"att-{harness}-fixture-never-launched-0001"
     self.seed_parent(jobs,repo,harness=harness)
     command=self.command(harness,"start",repo,jobs,logs)+["--foreground-timeout","5"]
     argv=["dispatch-headless.py",*command[2:]]
     env_extra={"AGENT_DISPATCH_CHILD":"1","AGENT_DISPATCH_ATTEMPT_ID":"att-parent-fixture"}
    resolution=wrapper.reconcile_launch_lifecycle(
     wrapper.DETACHED,{},evidence={
      "lifecycle_selector_source":"pid1-class",
      "lifecycle_nspid_width":"1",
      "lifecycle_pid1_class":"non-system-init",
     })
    env={**os.environ,"PATH":str(fakebin)+os.pathsep+os.environ.get("PATH",""),
         "AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(art),
         "AGENT_DISPATCH_JOBS":str(jobs),"XDG_STATE_HOME":str(root/"state"),
         **env_extra}
    env.pop("AGENT_MODEL_GOVERNOR_ROOT",None)
    stream=io.StringIO()
    patches=[mock.patch.dict(os.environ,env,clear=True),
             mock.patch.object(wrapper,"reconcile_launch_lifecycle",return_value=resolution),
             mock.patch.object(
              wrapper,"wait_governor_reservation_claim",
              side_effect=wrapper.DispatchContractError("reservation-owner-mismatch")),
             mock.patch.object(wrapper,"read_launch_fence_failure",return_value=(None,False))]
    if hasattr(wrapper,"check_runtime_projection"): patches.append(mock.patch.object(wrapper,"check_runtime_projection",return_value=0))
    if hasattr(wrapper,"ensure_runtime_home_projection"): patches.append(mock.patch.object(wrapper,"ensure_runtime_home_projection",return_value=None))
    for patch in patches: patch.start()
    try:
     with redirect_stdout(stream): code=wrapper.main(argv)
    finally:
     for patch in reversed(patches): patch.stop()
    output=stream.getvalue()
    self.assertEqual(code,75,output)
    self.assertIn("reason=reservation-owner-mismatch",output)
    row=jobs.read_text(encoding="utf-8")
    self.assertIn("note=dead-governor-reservation-transfer",row)
    self.assertIn("launch_outcome=never-launched",row)
    self.assertNotIn("launch_outcome=governed-process-reaped",row)

if __name__=="__main__": unittest.main()
