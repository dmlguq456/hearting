#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
F_SPEC=importlib.util.spec_from_file_location("fallback",ROOT/"utilities/stage-dispatch-fallback.py"); F=importlib.util.module_from_spec(F_SPEC); F_SPEC.loader.exec_module(F)

class FallbackTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); base=Path(self.tmp.name); self.repo=base/"repo"; self.repo.mkdir()
  subprocess.run(["git","init","-q",str(self.repo)],check=True); subprocess.run(["git","-C",str(self.repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(self.repo),"config","user.name","Fixture"],check=True)
  (self.repo/"x").write_text("x"); subprocess.run(["git","-C",str(self.repo),"add","x"],check=True); subprocess.run(["git","-C",str(self.repo),"commit","-qm","init"],check=True)
  self.art=base/".agent_reports"; self.art.mkdir(); self.jobs=base/"jobs.log"
  self.owner=subprocess.Popen(["sleep","60"])
 def tearDown(self):
  if self.owner.poll() is None:self.owner.kill()
  self.owner.wait();self.tmp.cleanup()
 def seed_parent(self,harness="codex",sandbox="workspace-write"):
  """Append the live dispatch-depth-1 owner row the depth-2 launch resolves.

  Appends rather than short-circuits on an existing file: a dry-run now
  resolves this row exactly as --start does, so a test that writes its own
  registry rows still needs the parent present.
  """
  if "worker_type=owner" in (self.jobs.read_text() if self.jobs.exists() else ""):return
  start=(Path("/proc")/str(self.owner.pid)/"stat").read_text().split()[21]
  with self.jobs.open("a",encoding="utf-8") as fh:
   fh.write(
    f"2026-07-23T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
    f"harness={harness},runtime_sandbox={sandbox},"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=owner,"
    f"attempt_id=att-fallback-parent,pid={self.owner.pid},pid_start={start}\n")
 def tuple(self,child,status):
  return {"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":child,"launch_authority":"conductor","status":status,"probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"nested-network-unconfirmed" if status!="supported" else "","checked_worktree":str(self.repo.resolve()),"failure_scope":"runtime-global" if status!="supported" else "none","codex_command":"ok" if child=="codex" else "not-applicable","retry_on_isolated_worktree":0}
 def route(self,native="unsupported",same_status="unsupported"):
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("codex",same_status),self.tuple("claude","supported")],"native_subagent":[{
   "harness":"codex","transport":"headless",
   "execution_surface":"codex-native-subagent","registered_worker":False,
   "status":native,"check_source":"fixture"}]}
  route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"route.json"; path.write_text(json.dumps(route),encoding="utf-8"); return path
 def run_chain(self,path,*extra,seed=True,**envkw):
  if seed:self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",**envkw}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 def run_register(self,path):
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--register"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent"}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 def test_cross_harness_direct_precedes_inline(self):
  result=self.run_chain(self.route()); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertIn("selected_hop=cross-harness-headless",result.stdout); self.assertIn("child_harness=claude",result.stdout)
  self.assertIn("launch_authority=conductor",result.stdout); self.assertIn("broker_lifecycle=retired",result.stdout)
 def test_wrapper_command_projects_selected_lifecycle_to_codex_and_claude(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text()); node=next(n for n in route["nodes"] if n["id"]=="plan")
  args=SimpleNamespace(action="dry-run",slug="stage",parent="owner",mode="dev/refactor",qa="standard",worker_role=None,model_role="deep maker",prompt_file=None,jobs=self.jobs,route=path,launch_lifecycle="foreground-scoped",foreground_timeout=123.0)
  for ordinal,harness in ((1,"codex"),(2,"claude")):
   row=self.tuple(harness,"supported")
   command=F.wrapper_command(args,route,node,row,ordinal,"att-test")
   self.assertEqual(command[command.index("--worker-type")+1],"stage")
   self.assertEqual(command[command.index("--assigned-contract")+1],"code-plan")
   self.assertNotIn("--worker-role",command)
   self.assertIn("--launch-lifecycle",command)
   self.assertEqual(command[command.index("--launch-lifecycle")+1],"foreground-scoped")
   self.assertEqual(command[command.index("--foreground-timeout")+1],"123.0")
  args.launch_lifecycle="detached"
  command=F.wrapper_command(args,route,node,self.tuple("codex","supported"),1,"att-test")
  self.assertNotIn("--foreground-timeout",command)
  command=F.wrapper_command(args,route,node,self.tuple("opencode","supported"),1,"att-test")
  self.assertNotIn("--launch-lifecycle",command)
  frame=next(n for n in route["nodes"] if n["id"]=="frame")
  command=F.wrapper_command(args,route,frame,self.tuple("codex","supported"),1,"att-test")
  self.assertEqual(command[command.index("--worker-type")+1],"support")
  # unit-io stage: the readable contract stays the entry capability; the
  # plan/frame unit persona carries the stage contract (same as design build).
  self.assertEqual(command[command.index("--assigned-contract")+1],"autopilot-code")
 def test_explicit_parent_mismatch_fails_before_registration(self):
  path=self.route(same_status="supported")
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","wrong-owner","--capability-mode","dev","--worker-mode","plan/plan-author","--jobs",str(self.jobs),"--register"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"real-owner"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,73,result.stdout+result.stderr)
  self.assertIn("reason=parent-identity-mismatch",result.stdout)
  self.assertFalse(self.jobs.exists())
 def test_failed_same_and_cross_degrade_in_order(self):
  path=self.route(native="supported"); same="codex/headless/workspace-write/codex/conductor"; cross="codex/headless/workspace-write/claude/conductor"
  result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross); self.assertEqual(result.returncode,79,result.stdout+result.stderr); self.assertIn("skipped-child-proof-missing",result.stdout); self.assertIn("selected_hop=inline",result.stdout)
  route=json.loads(path.read_text()); route["dispatch_evidence"]["native_subagent"][0]["status"]="unsupported"
  for node in route["nodes"]: node["fallback_hops"][2]["candidates"][0]["status"]="unsupported"
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]; path.write_text(json.dumps(route))
  result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross); self.assertEqual(result.returncode,79,result.stdout+result.stderr); self.assertIn("selected_hop=inline",result.stdout)
  self.assertIn("route_reuse=required",result.stdout)
  self.assertIn("route_id="+route["route_id"],result.stdout)
 def test_process_exit_without_marker_advances_fallback(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2)
  route={"route_id":"rt-fixture"}
  node={"id":"plan"}
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  exited=mock.Mock(returncode=0,stdout="action=process-exited\nterminal_action=process-exited\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,exited]):
   state,fields=F.watch_launched_attempt(
    args,route,node,"att-process-exit",{"child_pid":"1","child_pid_start":"2"})
  self.assertEqual(state,"fallback")
  self.assertEqual(fields["terminal_action"],"process-exited")
 def test_completed_row_is_draining_until_exact_process_exits(self):
  proc=subprocess.Popen(["sleep","30"],start_new_session=True)
  try:
   start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
   self.jobs.write_text(
    "2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan\t"
    "route_id=rt-q,route_node=plan,attempt_id=att-q,"
    f"pid={proc.pid},pid_start={start},pgid={proc.pid},"
    f"pid_observer_ns={os.readlink('/proc/self/ns/pid')},note=completed-marker\n")
   state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan","att-q")
   self.assertEqual(state,"draining")
   self.assertEqual(fields["process_state"],"live")
   proc.terminate();proc.wait(timeout=5)
   state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan","att-q")
   self.assertEqual(state,"terminal")
   self.assertEqual(fields["process_state"],"quiescent")
  finally:
   if proc.poll() is None:proc.kill()
   proc.wait()
 def test_attempt_identity_is_stable_across_actions(self):
  path=self.route(); first=self.run_chain(path); second=self.run_chain(path)
  def attempt(out): return next(line.split("=",1)[1] for line in out.splitlines() if line.startswith("attempt_id="))
  self.assertEqual(attempt(first.stdout),attempt(second.stdout))
 def test_parallel_register_is_rejected_without_creating_a_row(self):
  path=self.route(); first=self.run_register(path); second=self.run_register(path)
  self.assertEqual(first.returncode,65,first.stdout+first.stderr)
  self.assertEqual(second.returncode,65,second.stdout+second.stderr)
  self.assertIn("reason=parallel-group-batch-required",first.stdout)
  self.assertEqual(len(self.jobs.read_text().splitlines()),1)
  self.assertIn("att-fallback-parent",self.jobs.read_text())
 def test_registry_prevents_explicitly_classified_tuple_retry(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  pipe=f"capability=autopilot-code,route_id={route['route_id']},route_node=plan,parent=owner,attempt_id=att-prior000000,parent_harness=codex,parent_transport=headless,parent_sandbox=workspace-write,child_harness=codex,launch_authority=conductor,note=dead-launch-error,failure_class=launch-tuple"
  self.jobs.write_text(f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.repo}\tfallback-plan\t{pipe}\n")
  result=self.run_chain(path); self.assertEqual(result.returncode,0,result.stdout+result.stderr); self.assertIn("selected_hop=cross-harness-headless",result.stdout); self.assertIn("skipped-prior-unchanged-failure",result.stdout)
 def test_registry_worker_deaths_do_not_spend_a_launch_tuple(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  base=(f"capability=autopilot-code,route_id={route['route_id']},route_node=plan,"
        "parent=owner,parent_harness=codex,parent_transport=headless,"
        "parent_sandbox=workspace-write,child_harness=codex,launch_authority=conductor")
  self.jobs.write_text(
   f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.repo}\tworker-fail\t"
   f"{base},attempt_id=att-worker-fail,note=dead-worker-fail,failure_class=fail\n"
   f"2026-07-16T00:00:01Z\tdone\t/repo\t{self.repo}\tworker-dead\t"
   f"{base},attempt_id=att-worker-dead,note=dead-exact-pid\n",
   encoding="utf-8")
  self.assertEqual(F.registry_failures(self.jobs,route["route_id"],"plan"),{})
  result=self.run_chain(path)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertRegex(result.stdout,r"selected_hop=(same|cross)-harness-headless")
  self.assertNotIn("skipped-prior-unchanged-failure",result.stdout)
 def test_invalid_model_role_is_structured_and_preserved(self):
  path=self.route(same_status="supported")
  cross="codex/headless/workspace-write/claude/conductor"
  result=self.run_chain(path,"--model-role","not-a-role","--failed-tuple",cross)
  self.assertEqual(result.returncode,64,result.stdout+result.stderr)
  self.assertIn("reason=route-model-role-override",result.stdout)
  self.assertIn("expected=deep maker",result.stdout)
  self.assertNotIn("Traceback",result.stdout+result.stderr)
 def test_legacy_route_is_read_only(self):
  path=self.route(); route=json.loads(path.read_text()); route["broker_contract_version"]=2; route.pop("dispatch_contract_version")
  for row in route["dispatch_evidence"]["tuples"]: row["launch_authority"]="ancestor-broker"; row["broker_root"]="/tmp/legacy"
  for node in route["nodes"]:
   for hop in node.get("fallback_hops",[])[:2]:
    for row in hop.get("candidates",[]): row["launch_authority"]="ancestor-broker"; row["broker_root"]="/tmp/legacy"
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]; path.write_text(json.dumps(route))
  result=self.run_chain(path); self.assertEqual(result.returncode,76,result.stdout+result.stderr); self.assertIn("reason=legacy-broker-route-read-only",result.stdout)
 def run_node(self,path,node,action,*extra,**envkw):
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node",node,"--slug","fallback-"+node,"--parent","owner","--capability-mode","dev","--jobs",str(self.jobs),"--"+action,*extra]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 def hop(self,result):
  return next((line.split("=",1)[1] for line in result.stdout.splitlines()
               if line.startswith(("selected_hop=","reason="))),"-")
 def test_dry_run_and_start_agree_on_a_wrong_parent_runtime(self):
  # 2026-08-04 cairn: dry-run reported `check=ok,
  # selected_hop=same-harness-headless` for a route whose sealed parent could
  # never resolve, and only --start descended to inline. The sealed harness
  # here is codex while the running owner is claude -- the transport-axis
  # incident with the harness field substituted, which still compiles.
  path=self.route(same_status="supported")
  wrong={"AGENT_DISPATCH_CURRENT_HARNESS":"claude",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"adapter-default"}
  dry=self.run_node(path,"plan-check","dry-run",**wrong)
  reg=self.run_node(path,"plan-check","register",**wrong)
  self.assertEqual((dry.returncode,self.hop(dry)),(reg.returncode,self.hop(reg)),
                   dry.stdout+reg.stdout)
  self.assertEqual(dry.returncode,79,dry.stdout+dry.stderr)
  self.assertIn("selected_hop=inline",dry.stdout)
  self.assertIn("dispatch-evidence-parent-runtime-mismatch",dry.stdout)
  self.assertNotIn("check=ok",dry.stdout)
  # the ledger must name the real cause, not the inline hop's compile-time
  # `runtime-unavailable` constant
  self.assertIn("last_direct_failure_reason=dispatch-evidence-parent-runtime-mismatch",dry.stdout)
 def test_dry_run_resolves_the_live_parent_attempt_like_start(self):
  # Identity matches the tuple, but no owner row with that identity is live:
  # only --start used to notice.
  path=self.route(same_status="supported")
  self.seed_parent(harness="claude",sandbox="adapter-default")
  right={"AGENT_DISPATCH_CURRENT_HARNESS":"codex",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write",
         "HARNESS_CAPACITY_SCORES":"claude:80,codex:20"}
  dry=self.run_node(path,"plan-check","dry-run",**right)
  self.assertNotIn("check=ok",dry.stdout)
  self.assertIn("parent-attempt-not-found",dry.stdout)
  self.assertEqual(dry.returncode,79,dry.stdout+dry.stderr)
 def test_matching_parent_runtime_uses_balanced_checked_headless_band(self):
  path=self.route(same_status="supported")
  right={"AGENT_DISPATCH_CURRENT_HARNESS":"codex",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write",
         "HARNESS_CAPACITY_SCORES":"claude:80,codex:20"}
  dry=self.run_node(path,"plan-check","dry-run",**right)
  self.assertEqual(dry.returncode,0,dry.stdout+dry.stderr)
  self.assertIn("selected_hop=cross-harness-headless",dry.stdout)
  self.assertIn("child_harness=claude",dry.stdout)
  self.assertIn("attempt_count.codex=1",dry.stdout)

 def test_three_harness_stage_ranking_uses_only_recent_attempt_counts(self):
  node={
   "harness_affinity":"diverse",
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{
   "strategy":"least-recent-attempts","window":30,
   "harness_order":["claude","codex","opencode"],
  }}
  self.jobs.write_text(
   "2026-08-09T00:00:00Z\tdone\t/r\t/w\ta\t"
   "attempt_schema_version=2,registered_worker=1,attempt_id=att-count-a,harness=claude\n"
   "2026-08-09T00:00:01Z\tdone\t/r\t/w\tb\t"
   "attempt_schema_version=2,registered_worker=1,attempt_id=att-count-b,harness=codex\n",
   encoding="utf-8")
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:3]]
  self.assertEqual(selected,["opencode","claude","codex"])
  self.assertEqual(context["counts"],{"claude":1,"codex":1,"opencode":0})
 def test_capacity_aware_stage_keeps_opencode_outside_primary_band(self):
  node={
   "harness_affinity":"diverse",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"capacity-aware","window":30,
                                 "harness_order":["claude","codex","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":60,"codex":80,"opencode":100}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:3]]
  self.assertEqual(selected,["codex","claude","opencode"])
  self.assertFalse(context["relief_promoted"])
 def _shadowed_claude_node(self):
  # D7's live case reproduced on the third resolver: ordinal 1 seals a
  # foreign-parent (claude) same-harness claude row that would otherwise
  # shadow ordinal 2's checked codex-parent claude row.
  return {
   "harness_affinity":"diverse",
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported",
      "parent_harness":"claude","parent_transport":"headless","parent_sandbox":"workspace-write"},
    ]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported",
      "parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write"},
     {"child_harness":"codex","status":"supported",
      "parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write"},
    ]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
 def test_foreign_parent_row_does_not_claim_the_harness_slot(self):
  node=self._shadowed_claude_node()
  route={"dispatch_allocation":{
   "strategy":"least-recent-attempts","window":30,
   "harness_order":["claude","codex","opencode"],
  }}
  actual_parent={"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write"}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=actual_parent)
  ranked_by_harness={h["candidates"][0]["child_harness"]:h["candidates"][0] for h in hops[:len(context["rank"])]}
  self.assertEqual(ranked_by_harness["claude"]["parent_harness"],"codex")
  trailing=[c for h in hops[len(context["rank"]):] for c in h.get("candidates",[]) if c]
  self.assertTrue(any(
   c.get("parent_harness")=="claude" and c.get("child_harness")=="claude" for c in trailing
  ))
 def test_parent_identity_none_preserves_todays_chain(self):
  node=self._shadowed_claude_node()
  route={"dispatch_allocation":{
   "strategy":"least-recent-attempts","window":30,
   "harness_order":["claude","codex","opencode"],
  }}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  ranked_by_harness={h["candidates"][0]["child_harness"]:h["candidates"][0] for h in hops[:len(context["rank"])]}
  self.assertEqual(ranked_by_harness["claude"]["parent_harness"],"claude")
 def test_foreign_only_evidence_still_traces_the_parent_runtime_mismatch(self):
  # Keeping the foreign row in the trailing band (rather than dropping it) is
  # what keeps this trace meaningful: the sealed evidence is for parent codex,
  # the live owner runs as claude.
  path=self.route(same_status="supported")
  wrong={"AGENT_DISPATCH_CURRENT_HARNESS":"claude",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"adapter-default"}
  dry=self.run_node(path,"plan-check","dry-run",**wrong)
  self.assertEqual(dry.returncode,79,dry.stdout+dry.stderr)
  self.assertIn("skipped-dispatch-evidence-parent-runtime-mismatch",dry.stdout)
  self.assertIn("last_direct_failure_reason=dispatch-evidence-parent-runtime-mismatch",dry.stdout)
 def test_registry_infrastructure_failure_is_not_a_candidate_failure(self):
  # An unwritable registry is a hard stop at --start. Treating it as one more
  # exhausted candidate would descend to inline and recreate the divergence
  # the dry-run parent check exists to remove.
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan-check")
  row=node["fallback_hops"][0]["candidates"][0]
  args=SimpleNamespace(action="dry-run",parent="owner",jobs=self.jobs,
                       inherited_jobs=str(self.jobs))
  with mock.patch.object(F,"resolve_live_parent_attempt",
                         side_effect=F.DispatchContractError("global-registry-unwritable","x")):
   reason=F.parent_runtime_failure(args,route,row,None)
  self.assertEqual(reason,"global-registry-unwritable")
  self.assertNotIn(reason,F.CANDIDATE_SCOPED_PARENT_FAILURES)
 def test_partial_parent_runtime_identity_fails_closed(self):
  path=self.route(same_status="supported")
  result=self.run_node(path,"plan-check","dry-run",
                       AGENT_DISPATCH_CURRENT_HARNESS="codex")
  self.assertEqual(result.returncode,73,result.stdout+result.stderr)
  self.assertIn("reason=dispatch-evidence-parent-runtime-incomplete",result.stdout)
 def test_native_hop_accepts_only_a_live_route_owned_exact_child(self):
  path=self.route(native="supported");route=json.loads(path.read_text());attempt="att-nativeproof001"
  proc=subprocess.Popen(["sleep","30"])
  try:
   start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
   pipe=(f"attempt_schema_version=2,route_id={route['route_id']},route_node=plan,attempt_id={attempt},"
         f"dispatch_depth=2,transport=headless,harness=codex,execution_surface=codex-native-subagent,"
         f"registered_worker=0,fallback_hop=native-subagent,"
         f"pid={proc.pid},pid_start={start}")
   self.jobs.write_text(f"2026-07-16T00:00:00Z\topen\t/repo\t{self.repo}\tnative\t{pipe}\n")
   same="codex/headless/workspace-write/codex/conductor";cross="codex/headless/workspace-write/claude/conductor"
   result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross,"--native-attempt-id",attempt)
   self.assertEqual(result.returncode,78,result.stdout+result.stderr)
   self.assertIn("child_proof=registry-exact-pid",result.stdout)
  finally:
   proc.terminate();proc.wait()
 def test_direct_env_strips_owner_route_binding_but_keeps_node_binding(self):
  extra={"AGENT_OWNER_ROUTE_FILE":"/tmp/owner-route.json","AGENT_OWNER_ROUTE_ID":"rt-owner",
         "AGENT_OWNER_ROUTE_HASH":"sha256:owner","AGENT_DISPATCH_BROKER_TOKEN":"x",
         "AGENT_ROUTE_FILE":"/tmp/node-route.json","AGENT_ROUTE_ID":"rt-node"}
  with mock.patch.dict(os.environ,extra):
   result=F.direct_env()
  self.assertNotIn("AGENT_OWNER_ROUTE_FILE",result)
  self.assertNotIn("AGENT_OWNER_ROUTE_ID",result)
  self.assertNotIn("AGENT_OWNER_ROUTE_HASH",result)
  self.assertNotIn("AGENT_DISPATCH_BROKER_TOKEN",result)
  self.assertEqual(result["AGENT_ROUTE_FILE"],"/tmp/node-route.json")
  self.assertEqual(result["AGENT_ROUTE_ID"],"rt-node")

 def test_prelaunch_process_block_reasons_are_consumed_by_every_launcher(self):
  """The sibling-gate reasons must reach exit 78 / child_spawned=0 everywhere.

  They do not share the `predecessor-process-` prefix the launchers used to
  match on, so a prefix test would have silently dropped them to exit 65 --
  "the wrapper refused" instead of "nothing spawned, waiting may help". Every
  launcher matches the shared tuple instead, and none may go back.
  """
  for reason in ("prior-attempt-still-live","prior-attempt-unverifiable",
                 "predecessor-process-draining","predecessor-process-unverifiable"):
   self.assertIn(reason,F.PRELAUNCH_PROCESS_BLOCK_REASONS)
  launchers=[ROOT/"utilities/stage-dispatch-fallback.py",ROOT/"utilities/dispatch-batch.py"]
  launchers+=[ROOT/"adapters"/name/"bin/dispatch-headless.py"
              for name in ("claude","codex","opencode")]
  for path in launchers:
   source=path.read_text(encoding="utf-8")
   self.assertIn("PRELAUNCH_PROCESS_BLOCK_REASONS",source,path)
   self.assertNotIn('startswith("predecessor-process-")',source,path)

if __name__=="__main__": unittest.main()
