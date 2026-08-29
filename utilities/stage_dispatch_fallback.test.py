#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
F_SPEC=importlib.util.spec_from_file_location("fallback",ROOT/"utilities/stage-dispatch-fallback.py"); F=importlib.util.module_from_spec(F_SPEC); F_SPEC.loader.exec_module(F)

import contextlib


@contextlib.contextmanager
def dispatch_defaults_config_text(text):
 """Point the sealed dispatch-defaults loader at a fixture config."""
 with tempfile.TemporaryDirectory() as td:
  path=Path(td)/"dispatch-defaults.yaml"; path.write_text(text,encoding="utf-8")
  previous=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
  os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(path)
  try: yield path
  finally:
   if previous is None: os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
   else: os.environ["DISPATCH_DEFAULTS_CONFIG"]=previous


class FallbackTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); base=Path(self.tmp.name); self.repo=base/"repo"; self.repo.mkdir()
  subprocess.run(["git","init","-q",str(self.repo)],check=True); subprocess.run(["git","-C",str(self.repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(self.repo),"config","user.name","Fixture"],check=True)
  (self.repo/"x").write_text("x"); subprocess.run(["git","-C",str(self.repo),"add","x"],check=True); subprocess.run(["git","-C",str(self.repo),"commit","-qm","init"],check=True)
  self.art=base/".agent_reports"; self.art.mkdir(); self.jobs=base/"jobs.log"
  self.previous_dispatch_defaults=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
  os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(ROOT/"profiles/dispatch-defaults.yaml")
  self.owner=subprocess.Popen(["sleep","60"])
 def tearDown(self):
  if self.owner.poll() is None:self.owner.kill()
  self.owner.wait();self.tmp.cleanup()
  if self.previous_dispatch_defaults is None:
   os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
  else:
   os.environ["DISPATCH_DEFAULTS_CONFIG"]=self.previous_dispatch_defaults
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
  clean={k:v for k,v in os.environ.items() if not k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**clean,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
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
 def test_allocation_receipt_row_is_written_beside_the_stdout_verdict(self):
  # 2026-08-29: the rank/headroom verdict used to exist only on stdout, so a
  # configured policy could sit inert for weeks with no way to tell. Driving
  # `_emit_child_success` directly (the one success path every launch shape
  # shares) must print the verdict AND leave a ledger row keyed by the attempt,
  # carrying the sealed strategy, the preferred harness, and the inert-key
  # finding. The CLI pairing lives in the next test.
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan")
  allocation={**route["dispatch_allocation"],"strategy":"capacity-aware"}
  context={"strategy":"capacity-aware","window":30,"usage_gate_used_percent":85,
           "allocation":allocation,"preferred":"codex",
           "counts":{"claude":3,"codex":1,"opencode":0},"states":{"claude":"ok","codex":"ok","opencode":"unknown"},
           "rank":["claude","codex"],"capacity":{"claude":79.0,"codex":74.0,"opencode":None},
           "quality_band":"primary","relief_promoted":False,"parent_cross":"not-applicable",
           "parent_cross_cause":"-","sole_gate":"ok","affinity":"diverse","owner_family":None,
           "quality_peer_set":None,"eligible":["claude","codex"],"limited":[]}
  row=self.tuple("claude","supported")
  args=SimpleNamespace(action="dry-run",slug="fallback-plan",jobs=self.jobs,route=path)
  import io,contextlib as _cl
  out=io.StringIO()
  with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(self.jobs)}), _cl.redirect_stdout(out):
   F._emit_child_success(args,route,node,context,row,attempt_id="att-receipt",fallback_hop="cross-harness-headless")
  receipt=dict(line.split("=",1) for line in out.getvalue().splitlines() if "=" in line)
  self.assertEqual(receipt["allocation_rank"],"claude,codex")
  self.assertEqual(receipt["allocation_preferred"],"codex")
  self.assertEqual(receipt["allocation_inert_keys"],"depth_affinity_weight,usage_gate_used_percent,usage_headroom_exponent")
  self.assertTrue(receipt["allocation_receipt"].startswith("al-"),receipt)
  ledger=Path(self.tmp.name)/"allocation"/f"{route['route_id']}.jsonl"
  self.assertEqual(receipt["allocation_ledger"],str(ledger)); self.assertTrue(ledger.is_file())
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  self.assertEqual(len(rows),1,rows); ledger_row=rows[0]
  self.assertEqual(ledger_row["event_id"],receipt["allocation_receipt"])
  self.assertEqual(ledger_row["child_harness"],"claude"); self.assertEqual(ledger_row["attempt_id"],"att-receipt")
  self.assertEqual(ledger_row["route_node"],"plan"); self.assertEqual(ledger_row["unit"],"plan/plan-author")
  self.assertEqual(ledger_row["action"],"dry-run"); self.assertEqual(ledger_row["writer"],"stage-dispatch-fallback.py")
  self.assertEqual(ledger_row["strategy"],"capacity-aware"); self.assertEqual(ledger_row["preferred"],"codex")
  self.assertIs(ledger_row["preferred_honored"],False)
  self.assertEqual(sorted(ledger_row["inert_keys"]),["depth_affinity_weight","usage_gate_used_percent","usage_headroom_exponent"])
  self.assertEqual(ledger_row["rank"],["claude","codex"]); self.assertEqual(ledger_row["fallback_hop"],"cross-harness-headless")
  self.assertEqual(ledger_row["capacity"]["codex"],74.0); self.assertEqual(ledger_row["counts"]["claude"],3)
  # A missing allocation context (no sealed policy) still leaves the child evidence.
  out=io.StringIO()
  with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(self.jobs)}), _cl.redirect_stdout(out):
   F._emit_child_success(args,route,node,None,self.tuple("codex","supported"),attempt_id="att-bare",fallback_hop="same-harness-headless")
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  self.assertEqual(len(rows),2); self.assertEqual(rows[1]["child_harness"],"codex"); self.assertIsNone(rows[1]["strategy"])
  self.assertIn("allocation_receipt=",out.getvalue())
 def test_allocation_receipt_row_pairs_with_the_cli_verdict(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  result=self.run_chain(path)
  if "reason=launch-runtime-root-mismatch" in result.stdout:
   # Pre-existing whole-suite condition (baseline MA-W1-147): the in-process
   # compile and the CLI subprocess disagree on the grounding release id in
   # this environment, before any allocation code runs. Skip loudly rather
   # than pretend the CLI pairing was observed.
   self.skipTest("MA-W1-147 launch-runtime-root-mismatch precedes allocation in this environment")
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)
  self.assertIn("allocation_rank",receipt,result.stdout)
  self.assertEqual(receipt["allocation_preferred"],"codex")
  self.assertEqual(receipt["allocation_inert_keys"],"-")
  self.assertTrue(receipt["allocation_receipt"].startswith("al-"),receipt)
  ledger=Path(self.tmp.name)/"allocation"/f"{route['route_id']}.jsonl"
  self.assertEqual(receipt["allocation_ledger"],str(ledger))
  self.assertTrue(ledger.is_file())
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  self.assertEqual(len(rows),1,rows); row=rows[0]
  self.assertEqual(row["event_id"],receipt["allocation_receipt"])
  self.assertEqual(row["child_harness"],receipt["child_harness"])
  self.assertEqual(row["route_node"],"plan"); self.assertEqual(row["unit"],"plan/plan-author")
  self.assertEqual(row["action"],"dry-run"); self.assertEqual(row["writer"],"stage-dispatch-fallback.py")
  self.assertEqual(row["strategy"],route["dispatch_allocation"]["strategy"])
  self.assertEqual(row["preferred"],"codex"); self.assertEqual(row["inert_keys"],{})
  self.assertEqual(row["rank"],receipt["allocation_rank"].split(","))
  self.assertEqual(row["fallback_hop"],receipt["selected_hop"])
  self.assertEqual(row["attempt_id"],receipt["attempt_id"])
  self.assertIn(row["preferred_honored"],(True,False))
  self.assertEqual(row["preferred_honored"],row["child_harness"]=="codex")
  for harness in ("claude","codex"): self.assertIn(harness,row["counts"])
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
 def test_healthy_child_returns_a_launch_receipt_inside_the_confirm_window(self):
  # Regression for the 2026-08-14 candidate 6 defect: `--start` observed a
  # healthy detached child for the FULL no-progress budget
  # (progress_window_seconds * watchdog_max_windows = 300*12 = 1h by default),
  # so the owner's foreground call died before the launch receipt was printed.
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=300.0,
                       watchdog_max_windows=12,direct_timeout=45.0)
  self.assertEqual(F.launch_confirm_deadline_seconds(args),45.0)
  route={"route_id":"rt-fixture"}
  node={"id":"plan"}
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  alive=mock.Mock(returncode=0,stdout="action=observed\n",stderr="")
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=0.4,
                       watchdog_max_windows=12,direct_timeout=0.2)
  started=time.monotonic()
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[alive]*400):
   state,_=F.watch_launched_attempt(
    args,route,node,"att-healthy",{"child_pid":"1","child_pid_start":"2"})
  elapsed=time.monotonic()-started
  self.assertEqual(state,"observed")
  # Without the confirm bound this would run 0.4*12 = 4.8s, not <= ~0.2s.
  self.assertLess(elapsed,1.5)

 def test_confirm_window_never_exceeds_the_no_progress_budget(self):
  # A tiny explicit budget still wins over a larger spawn-confirm window, and a
  # disabled/non-positive confirm value falls back to the full budget.
  small=SimpleNamespace(progress_window_seconds=1.0,watchdog_max_windows=2,
                        direct_timeout=45.0)
  self.assertEqual(F.launch_confirm_deadline_seconds(small),2.0)
  disabled=SimpleNamespace(progress_window_seconds=1.0,watchdog_max_windows=2,
                           direct_timeout=0.0)
  self.assertEqual(F.launch_confirm_deadline_seconds(disabled),2.0)
  absent=SimpleNamespace(progress_window_seconds=300.0,watchdog_max_windows=12)
  self.assertEqual(F.launch_confirm_deadline_seconds(absent),
                   F.DIRECT_TIMEOUT_DEFAULT)

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
 def test_attempt_identity_includes_exact_parent_generation(self):
  route={"route_id":"rt-parent-generation"};node={"id":"plan"};row={"child_harness":"codex"}
  one=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-parent-one")
  two=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-parent-two")
  self.assertEqual(F.attempt_identity(one,route,node,row,1),F.attempt_identity(one,route,node,row,1))
  self.assertNotEqual(F.attempt_identity(one,route,node,row,1),F.attempt_identity(two,route,node,row,1))
  self.assertNotEqual(
   F.capacity_attempt_identity(one,route,node,row,1,"model-a"),
   F.capacity_attempt_identity(two,route,node,row,1,"model-a"),
  )
 def test_legacy_parent_generation_conflict_is_typed_without_reusing_identity(self):
  route={"route_id":"rt-parent-generation"};node={"id":"plan"};row={"child_harness":"codex"}
  old=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-parent-old")
  legacy=F.legacy_attempt_identity(old,route,node,row,1)
  self.jobs.write_text(
   "2026-08-13T00:00:00Z\tdone\t/repo\t/wt\tstage\t"
   f"attempt_schema_version=2,attempt_id={legacy},parent_attempt_id=att-parent-old\n",
   encoding="utf-8",
  )
  self.assertEqual(
   F.legacy_parent_generation_conflict(self.jobs,legacy,"att-parent-new"),
   "attempt-identity-parent-generation-conflict",
  )
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
  clean={k:v for k,v in os.environ.items() if not k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**clean,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
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
  # plan-check is now a parallel-group anchor (W3); use the non-group `test`
  # node so register/dry-run parity is exercised on a plain single checker.
  dry=self.run_node(path,"test","dry-run",**wrong)
  reg=self.run_node(path,"test","register",**wrong)
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
 def test_balanced_stage_fallback_orders_ungated_relief_before_a_gated_primary(self):
  node={
   "harness_affinity":"diverse",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
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
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":5,"codex":5,"opencode":80}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  # Ungated relief (opencode) leads; the gated primaries are demoted, not
  # dropped -- they stay reachable later in rank as fallback hops.
  self.assertEqual(context["rank"][0],"opencode")
  self.assertEqual(set(context["rank"]),{"claude","codex","opencode"})
 def _depth_affinity_node(self,depth):
  return {
   "harness_affinity":"diverse",
   "dispatch_depth":depth,
   "harness_policy":{"primary":["claude","codex"],"relief":[],
                     "last_resort":["opencode"],"promote_relief_below":0},
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
 def _depth_affinity_route(self,order,*,affinity=True):
  allocation={"strategy":"balanced","window":30,"usage_gate_used_percent":90,
              "harness_order":list(order)}
  if affinity:
   allocation.update({"depth_affinity":{"owner":"claude","worker":"codex"},
                      "depth_affinity_weight":0.65,"usage_headroom_exponent":2})
  return {"dispatch_allocation":allocation}
 def _depth_affinity_rank(self,order,depth,*,affinity=True):
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":80,"codex":80,"opencode":80}):
   _hops,context=F.ordered_fallback_hops(
    self._depth_affinity_route(order,affinity=affinity),
    self._depth_affinity_node(depth),self.jobs)
  return context["rank"]
 def test_depth_affinity_leads_at_its_own_depth_and_flips_at_the_other(self):
  # Equal headroom and an empty registry, so the neutral order is exactly the
  # declared one. The preference is read from the node's own dispatch_depth:
  # owner->claude at depth 1, worker->codex at depth 2. Both declared orders are
  # exercised so each depth is shown flipping a neutral head, not just agreeing
  # with it.
  self.assertEqual(self._depth_affinity_rank(["claude","codex","opencode"],2,
                                             affinity=False)[0],"claude")
  self.assertEqual(self._depth_affinity_rank(["claude","codex","opencode"],2)[0],"codex")
  self.assertEqual(self._depth_affinity_rank(["claude","codex","opencode"],1)[0],"claude")
  self.assertEqual(self._depth_affinity_rank(["codex","claude","opencode"],1,
                                             affinity=False)[0],"codex")
  self.assertEqual(self._depth_affinity_rank(["codex","claude","opencode"],1)[0],"claude")
  self.assertEqual(self._depth_affinity_rank(["codex","claude","opencode"],2)[0],"codex")
 def test_explicit_capacity_bias_beats_configured_depth_affinity(self):
  # D6/A1: `preferred_for_depth` returns None while a valid HARNESS_CAPACITY_BIAS
  # is set, so the configured preference is neutralized at its single source and
  # the resulting order is identical to the same inputs with the keys absent.
  # No consumer re-reads the env var for this feature, and no re-hoist was added.
  for order in (["claude","codex","opencode"],["codex","claude","opencode"]):
   for bias in ("claude","codex"):
    with mock.patch.dict(os.environ,{"HARNESS_CAPACITY_BIAS":bias}):
     configured=self._depth_affinity_rank(order,2)
     absent=self._depth_affinity_rank(order,2,affinity=False)
    self.assertEqual(configured,absent)
    self.assertEqual(configured[0],bias)
 def test_depth_affinity_cannot_lift_a_gated_harness_over_an_ungated_peer(self):
  # DP-24: the gate bit is the outermost element of the balanced sort key, so a
  # depth preference for a gated harness never crosses the class boundary.
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":80,"codex":5,"opencode":80}):
   _hops,context=F.ordered_fallback_hops(
    self._depth_affinity_route(["claude","codex","opencode"]),
    self._depth_affinity_node(2),self.jobs)
  self.assertEqual(context["rank"][0],"claude")
  self.assertEqual(context["rank"][-1],"codex")
  self.assertEqual(set(context["rank"]),{"claude","codex","opencode"})
 def test_balanced_stage_affinity_cannot_lift_a_gated_harness(self):
  node={
   "harness_affinity":"claude",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
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
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":5,"codex":5,"opencode":80}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  # The sealed affinity (claude, gated) is not lifted over the ungated
  # relief, but it is still hoisted to the head of its own gate class.
  self.assertNotEqual(context["rank"][0],"claude")
  self.assertEqual(context["rank"][0],"opencode")
  gated_tail=[h for h in context["rank"] if h!="opencode"]
  self.assertEqual(gated_tail[0],"claude")
 def test_balanced_stage_all_gated_affinity_cannot_beat_global_headroom(self):
  node={
   "harness_affinity":"claude",
   "harness_policy":{"primary":["claude"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":2,"opencode":9}):
   _hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  self.assertEqual(context["rank"][:2],["opencode","claude"])
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
 def _gate_node(self,kind="review-worker",affinity="diverse",profiles=("deep","balanced-deep")):
  parent={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"}
  return {
   "kind":kind,
   "harness_affinity":affinity,
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"codex","status":"supported"},
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
 def _gate_route(self,owner="opencode",limited=()):
  return {"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   },
   "owner_harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":35},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":35}},
    {"id":"test","model_profile":"light",
     "harness_policy":{"primary":["claude","codex","opencode"],"relief":[],
                       "last_resort":[],"promote_relief_below":35}},
   ]}
 def _usage_states(self,limited=()):
  return {h:("limited" if h in limited else "ok")
          for h in ("claude","codex","opencode")}
 def test_ac13_parent_cross_never_overrides_affinity_or_eligibility(self):
  # Precedence: sealed affinity (step 3) beats parent-cross (step 4), so an
  # owner-family affinity keeps its head and the tail alone is partitioned.
  node=self._gate_node(affinity="opencode")
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["parent_cross"],"degraded")
  self.assertEqual(context["parent_cross_cause"],"affinity-pinned")
 def _counts(self,**counts):
  return {h:counts.get(h,0) for h in ("claude","codex","opencode")}
 def test_ac13_selection_precedence_seven_steps(self):
  # AC 13: SD-101 declares seven ordered selection steps and the fixture set
  # only ever covered step 3 beating step 4. Each subtest below sets a LOWER
  # step to prefer a different harness and asserts the higher step still wins.
  parent={"parent_harness":"opencode","parent_transport":"headless",
          "parent_sandbox":"workspace-write"}
  def ranked(node,route,*,counts=None,identity=parent,states=None):
   with mock.patch.object(F,"_usage_states",
                          return_value=states or self._usage_states()), \
        mock.patch.object(F,"attempt_counts",
                          return_value=counts or self._counts()):
    _hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=identity)
   return context

  # 1 explicit target: an explicit per-node harness pin in dispatch-defaults is
  # sealed into the route as a literal `harness_affinity` at compile time, and
  # is the only user override SD-100 recognizes. This asserts PRECEDENCE, not
  # just that sealing happened: the pinned family stays the head while every
  # lower step -- band, capacity, least-recent, declared order -- is set to
  # prefer a different one, and the same node WITHOUT the pin follows them.
  with self.subTest(step=1,rule="explicit target"):
   with dispatch_defaults_config_text(
     "schema_version: 1\ndepth1_owner: [claude, codex]\nopencode:\n  relief_only: true\n"
     "capabilities:\n  autopilot-code:\n    plan: codex\n    execute: diverse\n"
     "    test: diverse\n    report: claude\n"):
    nodes=[{"id":"plan","dispatch_depth":2,"model_profile":"deep"}]
    R._seal_dispatch_defaults(nodes,"autopilot-code")
    self.assertEqual(nodes[0]["harness_affinity"],"codex")
   pinned=self._gate_node(affinity="codex")
   pinned["harness_policy"]={"primary":["claude"],"relief":["opencode"],
                             "last_resort":["codex"],"promote_relief_below":0}
   lower=self._counts(claude=1,codex=9,opencode=0)
   context=ranked(pinned,self._gate_route(),counts=lower,identity=None)
   self.assertEqual(context["rank"][0],"codex")
   # discriminating: without the seal the lower steps really do choose another
   unpinned=self._gate_node()
   unpinned["harness_policy"]=pinned["harness_policy"]
   context=ranked(unpinned,self._gate_route(),counts=lower,identity=None)
   self.assertNotEqual(context["rank"][0],"codex")

  with self.subTest(step=2,rule="hard eligibility"):
   # 2 hard eligibility over everything below it: a harness whose checked tuple
   # is not `supported` is not a candidate at all, so neither a sealed affinity
   # naming it nor the parent-cross partition can hoist it into the band.
   node=self._gate_node(affinity="codex")
   node["fallback_hops"][1]["candidates"][0]["status"]="unsupported"
   context=ranked(node,self._gate_route())
   self.assertNotIn("codex",context["rank"])
   self.assertEqual(context["rank"][0],"claude")

  with self.subTest(step=3,rule="affinity over parent-cross"):
   # 3 sealed literal affinity over parent-cross: the affinity names the OWNER
   # family, which step 4 would move to the tail; the head is kept and the
   # degradation is recorded instead of being reordered away.
   context=ranked(self._gate_node(affinity="opencode"),self._gate_route())
   self.assertEqual(context["rank"][0],"opencode")
   self.assertEqual(context["parent_cross"],"degraded")
   self.assertEqual(context["parent_cross_cause"],"affinity-pinned")

  with self.subTest(step=4,rule="parent-cross over band"):
   # 4 parent-cross over quality band / capacity and least-recent: claude and
   # codex are both cross+quality-peer, opencode is the owner family. Even with
   # opencode holding the fewest recent attempts (step 6 would put it first),
   # the cross block stays ahead of it.
   context=ranked(self._gate_node(),self._gate_route(),
                  counts=self._counts(claude=5,codex=6,opencode=0))
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   self.assertEqual(context["parent_cross"],"ok")

  with self.subTest(step=5,rule="band over least-recent"):
   # 5 quality band / capacity over least-recent: under the capacity-aware
   # strategy the node's own band is consulted before attempt counts, so a
   # last_resort harness with zero recent attempts stays behind the band.
   # `identity=None` removes step 4 from the picture so this isolates 5 vs 6 --
   # least-recent alone would rank [opencode, codex, claude].
   node=self._gate_node()
   node["harness_policy"]={"primary":["claude"],"relief":["codex"],
                           "last_resort":["opencode"],"promote_relief_below":0}
   route=self._gate_route()
   route["dispatch_allocation"]["strategy"]="capacity-aware"
   with mock.patch.object(F.CAPACITY,"capacity_scores",
                          return_value={"claude":90.0,"codex":90.0,"opencode":90.0}):
    context=ranked(node,route,counts=self._counts(claude=9,codex=4,opencode=0),
                   identity=None)
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   self.assertEqual(context["quality_band"],"primary")

  with self.subTest(step=6,rule="least-recent over declared order"):
   # 6 least-recent over declared order: the declared order is
   # [claude, codex, opencode], so codex only precedes claude because it holds
   # fewer recent attempts. Step 7 alone would have kept claude first.
   node=self._gate_node()
   context=ranked(node,self._gate_route(),
                  counts=self._counts(claude=3,codex=1,opencode=0),
                  identity=None)
   self.assertEqual(context["rank"],["opencode","codex","claude"])

  with self.subTest(step=7,rule="declared order tie-break"):
   # 7 declared order as the final tie-break: with every count equal, only the
   # declared order decides, and reversing it reverses the rank.
   route=self._gate_route()
   context=ranked(node,route,identity=None)
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   route=self._gate_route()
   route["dispatch_allocation"]["harness_order"]=["opencode","codex","claude"]
   context=ranked(node,route,identity=None)
   self.assertEqual(context["rank"],["opencode","codex","claude"])
 def test_ac14_stable_partition_preserves_block_internal_order(self):
  # Cross block [claude, codex] and non-cross [opencode] keep their internal
  # least-recent order; only the two blocks are concatenated.
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(context["parent_cross"],"ok")
  self.assertEqual(selected,["claude","codex","opencode"])
  self.assertEqual([h for h in selected if h in {"claude","codex"}],
                   ["claude","codex"])
  self.assertEqual([h for h in selected if h not in {"claude","codex"}],
                   ["opencode"])
 def test_ac15_affinity_head_is_kept_and_tail_only_partitioned(self):
  node=self._gate_node(affinity="opencode")
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["parent_cross"],"degraded")
  self.assertEqual(context["parent_cross_cause"],"affinity-pinned")
 def test_ac16_cross_usage_limited_degrades_with_closed_cause(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",
       return_value=self._usage_states(limited=("claude","codex"))):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"degraded")
  self.assertIn(context["parent_cross_cause"],
    {"affinity-pinned","cross-family-eligible-none",
     "cross-family-usage-limited","owner-family-only-peer"})
  self.assertEqual(context["parent_cross_cause"],"cross-family-usage-limited")
 def test_ac12_16_receipt_and_ledger_evidence_pair_at_the_cli(self):
  # AC 12/16 say a degradation must leave BOTH a receipt field and an SD-93
  # ledger record, and "두 증거가 모두 없으면 실패". The two existing fixtures
  # check `ordered_fallback_hops` and `_recompute_verdicts_for_child` as
  # functions; neither runs the CLI, so nothing held the PAIR together. This
  # drives the real process: only codex is hard-eligible and codex is also the
  # owner family, so the single gate-holding checker lands on the owner family.
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
        "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("codex","supported"),self.tuple("claude","unsupported")],
            "native_subagent":[{"harness":"codex","transport":"headless",
                                "execution_surface":"codex-native-subagent",
                                "registered_worker":False,"status":"unsupported",
                                "check_source":"fixture"}]}
  route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,
    signals=["shared-contract"],transport="headless",tracking="tracked",
    tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"evidence-pair-route.json"
  path.write_text(json.dumps(route),encoding="utf-8")
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),
       "--route",str(path),"--node","plan-check","--slug","fb-evidence-pair",
       "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
       "--model-role","fast reviewer","--jobs",str(self.jobs),"--dry-run"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
       "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",
       "AGENT_DISPATCH_CURRENT_HARNESS":"codex",
       "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
       "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)
  # evidence 1: the stdout receipt fields
  self.assertEqual(receipt["child_harness"],"codex")
  self.assertEqual(receipt["parent_cross"],"degraded")
  self.assertEqual(receipt["parent_cross_cause"],"cross-family-eligible-none")
  self.assertIn(receipt["parent_cross_cause"],
    {"affinity-pinned","cross-family-eligible-none",
     "cross-family-usage-limited","owner-family-only-peer"})
  # evidence 2: the SD-93 ledger record left by the SAME process run
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  self.assertTrue(ledger.is_file(),sorted(p.name for p in (Path(self.tmp.name)/"degradations").glob("*")) if (Path(self.tmp.name)/"degradations").is_dir() else "no ledger dir")
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  cross=[row for row in rows if row.get("reason")=="parent-cross-same-harness"]
  self.assertEqual(len(cross),1,rows)
  self.assertEqual(cross[0]["parent_cross"],"degraded")
  self.assertEqual(cross[0]["cause"],receipt["parent_cross_cause"])
  self.assertEqual(cross[0]["route_node"],"plan-check")
  self.assertEqual(cross[0]["writer"],"stage-dispatch-fallback.py")
 def test_ac12_sole_gate_receipt_and_ledger_evidence_pair_at_the_cli(self):
  # M5 / AC 12: the CLI evidence pair for the SOLE-GATE degradation was never
  # actually created -- the fixture above asserts `parent_cross`, which is AC 16.
  # AC 12 requires the `sole_gate` receipt field and the
  # `sole-gate-non-peer-harness` SD-93 record TOGETHER ("두 증거가 모두 없으면
  # 실패"), and every existing sole_gate assertion is on the `context` dict a
  # function returns, never on what a process leaves behind. This drives the
  # real process: only opencode is hard-eligible while the derived quality-peer
  # set is {claude, codex}, so the single gate-holding checker lands off the
  # quality-peer families and the assignment proceeds with the degradation
  # recorded (13.30.2 ② proviso). The owner family stays codex so the head is
  # NOT the owner and `parent_cross` reads "ok" -- this pins the sole-gate pair
  # on its own, not on the back of AC 16's.
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
        "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("opencode","supported"),
                      self.tuple("codex","unsupported"),
                      self.tuple("claude","unsupported")],
            "native_subagent":[{"harness":"codex","transport":"headless",
                                "execution_surface":"codex-native-subagent",
                                "registered_worker":False,"status":"unsupported",
                                "check_source":"fixture"}]}
  route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,
    signals=["shared-contract"],transport="headless",tracking="tracked",
    tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"sole-gate-route.json"
  path.write_text(json.dumps(route),encoding="utf-8")
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),
       "--route",str(path),"--node","plan-check","--slug","fb-sole-gate",
       "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
       "--model-role","fast reviewer","--jobs",str(self.jobs),"--dry-run"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
       "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",
       "AGENT_DISPATCH_CURRENT_HARNESS":"codex",
       "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
       "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)
  # evidence 1: the stdout receipt field
  self.assertEqual(receipt["sole_gate"],"degraded")
  self.assertEqual(receipt["child_harness"],"opencode")
  self.assertEqual(receipt["parent_cross"],"ok")
  # evidence 2: the SD-93 ledger record left by the SAME process run
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  self.assertTrue(ledger.is_file(),result.stdout)
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()]
  sole=[row for row in rows if row.get("reason")=="sole-gate-non-peer-harness"]
  self.assertEqual(len(sole),1,rows)
  self.assertEqual(sole[0]["sole_gate"],"degraded")
  self.assertEqual(sole[0]["leg_class"],"peer")
  self.assertEqual(sole[0]["route_node"],"plan-check")
  self.assertEqual(sole[0]["writer"],"stage-dispatch-fallback.py")
 def test_ac17_parent_identity_absent_marks_not_applicable(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  baseline=[]
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  baseline=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["sole_gate"],"ok")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops2,context2=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual([hop["candidates"][0]["child_harness"] for hop in hops2[:len(context2["rank"])]],
                   ["claude","codex","opencode"])
  # ledger write failures must never change exit/receipt/child (AC 17 / R5)
  self.assertIsNone(F._persist_parent_cross_ledger(None, route, node, None))
 def test_ac18_non_target_node_keeps_six_repeat_rotation(self):
  # A non-gate node (no review-worker kind, no parent_cross_preference) never
  # partitions: the SD-66 v39 six-repeat 3-harness rotation is preserved.
  parent={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"}
  node={
   "kind":"pipeline-stage",
   "harness_affinity":"diverse",
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"codex","status":"supported"},
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   }}
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"not-applicable")
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(set(selected),{"claude","codex","opencode"})
 def test_ac19_opencode_owner_prefers_cross_quality_peer(self):
  # OpenCode owner: cross candidates go to the hard-eligible {claude, codex}
  # and SD-100 ② is simultaneously satisfied (head is a quality-peer family).
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"ok")
  self.assertEqual(context["sole_gate"],"ok")
  self.assertEqual(context["quality_peer_families"],["claude","codex"])
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
 def test_ac100b_sole_gate_degraded_when_no_quality_peer_eligible(self):
  # SD-100 ② proviso: when no quality-peer family is hard-eligible the
  # assignment proceeds with sole-gate-non-peer-harness recorded. The deep and
  # balanced-deep primary bands have an empty intersection, so the derived
  # quality-peer set is empty.
  node=self._gate_node()
  node["harness_policy"]={"primary":["claude","codex"],"relief":["opencode"],
                          "last_resort":[],"promote_relief_below":0}
  route={"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   },
   "owner_harness_policy":{"primary":["claude"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":0},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":0}},
   ]}
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["quality_peer_families"],[])
  self.assertEqual(context["sole_gate"],"degraded")
 def test_g3_parent_cross_verdict_taken_after_sole_gate_reorder(self):
  # G3: the parent-cross verdict must describe the FINAL head after the SD-100
  # ② quality-peer reorder. Least-recent favors opencode, so the pre-reorder
  # head is opencode (owner=claude, codex unsupported) and the old code froze
  # parent_cross="ok" before the hoist moved claude (the owner family) to the
  # head. The verdict must be "degraded" with a closed cause.
  parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"workspace-write"}
  node={
   "kind":"review-worker",
   "harness_affinity":"diverse",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   },
   "owner_harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":35},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":35}},
   ]}
  self.jobs.write_text(
   "2026-08-09T00:00:00Z\tdone\t/r\t/w\ta\t"
   "attempt_schema_version=2,registered_worker=1,attempt_id=att-claude,harness=claude\n",
   encoding="utf-8")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
  self.assertEqual(context["parent_cross"],"degraded")
  self.assertEqual(context["parent_cross_cause"],"cross-family-eligible-none")
  self.assertEqual(context["sole_gate"],"ok")
 def test_g3_verdicts_recomputed_for_actual_launched_child(self):
  # G3: the receipt and ledger must describe the actually launched child, not
  # the ranked head. A head whose launch-tuple dies and a later hop that wins
  # (opencode -- non-quality-peer, or claude -- the owner family) must flip
  # sole_gate / parent_cross accordingly.
  context={
   "parent_cross":"ok","parent_cross_cause":"-","sole_gate":"ok",
   "affinity":None,"rank":["claude","codex"],
   "eligible":["claude","codex"],"limited":[],
   "owner_family":"claude","quality_peer_set":frozenset({"claude","codex"}),
  }
  reopened=F._recompute_verdicts_for_child(context,"opencode")
  self.assertEqual(reopened["parent_cross"],"ok")
  self.assertEqual(reopened["sole_gate"],"degraded")
  same_family=F._recompute_verdicts_for_child(context,"claude")
  self.assertEqual(same_family["parent_cross"],"degraded")
  self.assertEqual(same_family["sole_gate"],"ok")
  self.assertEqual(same_family["parent_cross_cause"],"owner-family-only-peer")
  cross=F._recompute_verdicts_for_child(context,"codex")
  self.assertEqual(cross["parent_cross"],"ok")
  self.assertEqual(cross["sole_gate"],"ok")
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
