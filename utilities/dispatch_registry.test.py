#!/usr/bin/env python3
import contextlib, hashlib, importlib.util, io, json, os, subprocess, sys, tempfile, time, types, unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]; SCRIPT=ROOT/"utilities/dispatch-registry.py"
sys.path[:0]=[str(ROOT),str(ROOT/"utilities")]
from dispatch_contract import (attempt_process_quiescence,  # noqa: E402
                               attempt_tagged_descendants,
                               observed_attempt_liveness,
                               parse_registry_metadata,
                               post_exit_receipt_reason)
CURRENT_ATTEMPT_CONTRACT=(
 "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
 "execution_surface=registered-headless,registered_worker=1,"
 "fallback_hop=same-harness-headless"
)
def currentize_registry(path):
 if not path.is_file(): return
 rows=[]
 for line in path.read_text().splitlines():
  fields=line.split("\t")
  if len(fields)==6 and "attempt_schema_version=" not in fields[5]:
   fields[5]+=("," if fields[5] else "")+CURRENT_ATTEMPT_CONTRACT
  rows.append("\t".join(fields))
 path.write_text("\n".join(rows)+("\n" if rows else ""))
class RegistryTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name); self.jobs=self.base/"jobs.log"
  self.proc=subprocess.Popen(["sleep","60"]); start=(Path("/proc")/str(self.proc.pid)/"stat").read_text().split()[21]
  rows=[
   f"2026-07-16T00:00:00Z\topen\t/r\t/w\tactive\troute_id=r1,route_node=test,attempt_id=att-active0001,parent_sid=s1,pid={self.proc.pid},pid_start={start}",
   "2026-07-16T00:00:01Z\topen\t/r\t/w\tdead\troute_id=r1,route_node=report,attempt_id=att-dead000001,parent_sid=s1,pid=99999999,pid_start=1",
   "2026-07-16T00:00:02Z\topen\t/r\t/w\tother\troute_id=r2,route_node=test,attempt_id=att-other00001,parent_sid=s2,pid=99999998,pid_start=1"]
  self.jobs.write_text("\n".join(rows)+"\n")
  currentize_registry(self.jobs)
 def tearDown(self):
  if self.proc.poll() is None:self.proc.kill()
  self.proc.wait();self.tmp.cleanup()
 def invoke(self,*args):
  currentize_registry(self.jobs)
  return subprocess.run([sys.executable,str(SCRIPT),*args,"--jobs",str(self.jobs),"--agent-home",str(self.base)],capture_output=True,text=True)
 def test_current_filters_before_totals(self):
  r=self.invoke("current","--route","r1");self.assertEqual(r.returncode,0,r.stdout+r.stderr);data=json.loads(r.stdout)
  self.assertEqual(data["total"],2);self.assertEqual({x["slug"] for x in data["rows"]},{"active","dead"})
 def test_reconcile_closes_only_selected_exact_dead(self):
  before=self.jobs.read_text();dry=self.invoke("reconcile","--attempt","att-dead000001");self.assertEqual(json.loads(dry.stdout)["closed"],0);self.assertEqual(self.jobs.read_text(),before)
  applied=self.invoke("reconcile","--attempt","att-dead000001","--apply");self.assertEqual(json.loads(applied.stdout)["closed"],1)
  text=self.jobs.read_text();self.assertIn("note=dead-exact-pid",text);self.assertIn("\topen\t/r\t/w\tactive\t",text);self.assertIn("\topen\t/r\t/w\tother\t",text)
  again=self.invoke("reconcile","--attempt","att-dead000001","--apply");self.assertEqual(json.loads(again.stdout)["closed"],0)
 def test_remote_less_repo_closes_namespace_row_with_verified_outer_pid_reuse(self):
  repo=self.base/"local-only-repo"
  subprocess.run(["git","init","-q",str(repo)],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.email","test@example.invalid"],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.name","Dispatch Test"],check=True)
  (repo/"tracked.txt").write_text("tracked\n")
  subprocess.run(["git","-C",str(repo),"add","tracked.txt"],check=True)
  subprocess.run(["git","-C",str(repo),"commit","-qm","initial"],check=True)
  self.assertEqual(
   subprocess.run(["git","-C",str(repo),"remote"],capture_output=True,text=True,check=True).stdout.strip(),
   "",
  )
  namespace=os.readlink("/proc/self/ns/pid")
  attempt="att-outer-pid-reused"
  with self.jobs.open("a") as out:
   out.write(
    f"2026-08-06T00:00:00Z\topen\t{repo}\t{repo}\touter-reused\t"
    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,route_id=rt-local,route_node=report,"
    f"attempt_id={attempt},pid=437,pid_start=1,pid_scope=namespace-local,"
    f"pid_host={os.getpid()},pid_host_start=1,pid_host_ns={namespace},"
    "pid_host_proof=nspid-procfs-root-v1\n"
   )
  applied=self.invoke("reconcile","--attempt",attempt,"--apply")
  self.assertEqual(applied.returncode,0,applied.stdout+applied.stderr)
  record=json.loads(applied.stdout)
  self.assertEqual(record["closed"],1,record)
  self.assertEqual(record["decisions"][0]["category"],"exact-dead")
  self.assertNotIn("no-upstream-configured",record["decisions"][0]["reason"])
  self.assertIn("note=dead-exact-pid",self.jobs.read_text())
 def test_apply_reconcile_repairs_missing_summary_owner_for_live_exact_attempt(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_summary_test",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  rows=module.read_rows(self.jobs)
  args=types.SimpleNamespace(
   session=None,route=None,node=None,attempt="att-active0001",job=None,
   apply=True,audit=None,jobs=self.jobs,agent_home=self.base,
   integration_ref=None,now=time.time(),cascade_grace=0,cascade_kill_wait=0)
  repaired={"state":"started","reason":"reattached","summary_owner":"dispatch-v1"}
  stream=io.StringIO()
  with mock.patch.object(module,"classify",return_value=("live","exact-pid",None)), \
       mock.patch.object(module,"ensure_attempt_owner",return_value=repaired) as ensure, \
       contextlib.redirect_stdout(stream):
   self.assertEqual(module.reconcile(rows,args),0)
  ensure.assert_called_once_with(self.jobs,"att-active0001")
  decision=json.loads(stream.getvalue())["decisions"][0]
  self.assertEqual(decision["summary_owner"],repaired)
 def test_terminal_handoff_closes_namespace_attempt_without_watchdog(self):
  attempt="att-sandbox-terminal";route="rt-sandbox";node="refs";log=self.base/"exact.jsonl"
  events=[
   {"type":"item.completed","item":{"type":"command_execution","exit_code":1,
    "aggregated_output":"bwrap: Can't bind mount /bindfile on /newroot/w/.codex: Unable to mount source on destination: No such file or directory\n"}},
   {"type":"item.completed","item":{"type":"agent_message",
    "text":"artifact: -\nverdict: BLOCKED\nblocker: sandbox unavailable"}},
   {"type":"turn.completed"},
  ]
  log.write_text("\n".join(json.dumps(event) for event in events)+"\n")
  artifact_root=self.base/".agent_reports";artifact_root.mkdir(exist_ok=True)
  with self.jobs.open("a") as out:
   out.write(f"2026-07-16T00:00:03Z\topen\t/r\t{self.base}\tsandbox\t"
              f"route_id={route},route_node={node},attempt_id={attempt},"
              f"pid=437,pid_start=1,pid_scope=namespace-local,harness=codex,"
              f"artifact_root={artifact_root},log_file={log}\n")
  currentize_registry(self.jobs)
  liveness=subprocess.run(
   [sys.executable,str(ROOT/"adapters/codex/bin/dispatch-liveness.py"),str(self.jobs)],
   capture_output=True,text=True,
   env={**os.environ,"AGENT_HOME":str(self.base),"AGENT_ARTIFACT_ROOT":str(artifact_root),
        "CODEX_SESSIONS":str(self.base/"missing")},
  )
  self.assertEqual(liveness.returncode,3,liveness.stdout+liveness.stderr)
  self.assertIn("EXITED   sandbox",liveness.stdout)
  self.assertIn("dead-sandbox-init",liveness.stdout)
  self.assertNotIn("ALIVE    sandbox",liveness.stdout)
  applied=self.invoke("reconcile","--attempt",attempt,"--apply")
  record=json.loads(applied.stdout)
  self.assertEqual(record["closed"],1)
  self.assertEqual(record["decisions"][0]["category"],"terminal-handoff")
  self.assertIn("note=dead-sandbox-init",self.jobs.read_text())
 def test_codex_terminal_pass_is_completed_and_stays_open(self):
  attempt="att-pass-terminal";log=self.base/"pass.jsonl";artifact_root=self.base/".agent_reports";artifact_root.mkdir(exist_ok=True)
  events=[
   {"type":"item.completed","item":{"type":"command_execution","exit_code":0,"aggregated_output":"RAW_COMMAND_SENTINEL"}},
   {"type":"item.completed","item":{"type":"agent_message","text":"artifact: -\nverdict: PASS\nblocker: none"}},
   {"type":"turn.completed"},
  ]
  log.write_text("\n".join(json.dumps(event) for event in events)+"\n")
  with self.jobs.open("a") as out:
   out.write(f"2026-07-16T00:00:04Z\topen\t/r\t{self.base}\tpass-terminal\t"
             f"route_id=rt-pass,route_node=test,attempt_id={attempt},pid=99999996,pid_start=1,"
             f"harness=codex,artifact_root={artifact_root},log_file={log}\n")
  currentize_registry(self.jobs)
  result=subprocess.run(
   [sys.executable,str(ROOT/"adapters/codex/bin/dispatch-liveness.py"),str(self.jobs)],
   capture_output=True,text=True,
   env={**os.environ,"AGENT_HOME":str(self.base),"AGENT_ARTIFACT_ROOT":str(artifact_root),
        "CODEX_SESSIONS":str(self.base/"missing")},
  )
  self.assertEqual(result.returncode,3,result.stdout+result.stderr)
  self.assertIn("COMPLETED pass-terminal - exact turn.completed PASS; harvest required",result.stdout)
  self.assertNotIn("RAW_COMMAND_SENTINEL",result.stdout+result.stderr)
  self.assertIn(f"\topen\t/r\t{self.base}\tpass-terminal\t",self.jobs.read_text())
 def test_claude_terminal_pass_is_completed_and_stays_open(self):
  attempt="att-claude-pass";log=self.base/"pass.claude.jsonl";artifact_root=self.base/".agent_reports";artifact_root.mkdir(exist_ok=True)
  events=[
   {"type":"system","subtype":"init"},
   {"type":"result","subtype":"success","result":"artifact: -\nverdict: PASS\nblocker: none"},
  ]
  log.write_text("\n".join(json.dumps(event) for event in events)+"\n")
  with self.jobs.open("a") as out:
   out.write(f"2026-07-16T00:00:04Z\topen\t/r\t{self.base}\tclaude-pass\t"
             f"route_id=rt-claude-pass,route_node=test,attempt_id={attempt},pid=99999995,pid_start=1,"
             f"harness=claude,artifact_root={artifact_root},log_file={log}\n")
  currentize_registry(self.jobs)
  result=subprocess.run(
   [sys.executable,str(ROOT/"adapters/codex/bin/dispatch-liveness.py"),str(self.jobs)],
   capture_output=True,text=True,
   env={**os.environ,"AGENT_HOME":str(self.base),"AGENT_ARTIFACT_ROOT":str(artifact_root),
        "CODEX_SESSIONS":str(self.base/"missing")},
  )
  self.assertEqual(result.returncode,3,result.stdout+result.stderr)
  self.assertIn("COMPLETED claude-pass - exact Claude result PASS; harvest required",result.stdout)
  self.assertIn(f"\topen\t/r\t{self.base}\tclaude-pass\t",self.jobs.read_text())
 def test_codex_preflight_projects_current_and_dry_reconcile(self):
  pre=ROOT/"adapters/codex/bin/preflight.sh"
  current=subprocess.run([str(pre),"dispatch-current","--jobs",str(self.jobs),"--route","r1","--agent-home",str(self.base)],capture_output=True,text=True,env={**os.environ,"AGENT_HOME":str(ROOT)})
  self.assertEqual(current.returncode,0,current.stdout+current.stderr);self.assertEqual(json.loads(current.stdout)["total"],2)
  before=self.jobs.read_text();dry=subprocess.run([str(pre),"dispatch-reconcile","--jobs",str(self.jobs),"--route","r1","--agent-home",str(self.base)],capture_output=True,text=True,env={**os.environ,"AGENT_HOME":str(ROOT)})
  self.assertEqual(dry.returncode,0,dry.stdout+dry.stderr);self.assertEqual(self.jobs.read_text(),before)
 def test_current_hides_older_attempt_and_all_preserves_history(self):
  with self.jobs.open("a") as out:
   out.write("2026-07-16T00:00:03Z\tdone\t/r\t/w\told\troute_id=r3,route_node=test,attempt_id=att-old-history\n")
   out.write("2026-07-16T00:00:04Z\topen\t/r\t/w\tnew\troute_id=r3,route_node=test,attempt_id=att-new-history\n")
  current=json.loads(self.invoke("current","--route","r3").stdout);history=json.loads(self.invoke("current","--route","r3","--all").stdout)
  self.assertEqual([row["slug"] for row in current["rows"]],["new"])
  self.assertEqual([row["slug"] for row in history["rows"]],["old","new"])
 def test_preflight_liveness_ignores_superseded_open_attempt(self):
  start=(Path("/proc")/str(self.proc.pid)/"stat").read_text().split()[21]
  with self.jobs.open("a") as out:
   out.write("2026-07-16T00:00:03Z\topen\t/r\t/w\told-dead\troute_id=r4,route_node=test,attempt_id=att-old-dead,pid=99999997,pid_start=1,harness=codex\n")
   out.write(f"2026-07-16T00:00:04Z\topen\t/r\t/w\tnew-live\troute_id=r4,route_node=test,attempt_id=att-new-live,pid={self.proc.pid},pid_start={start},harness=codex\n")
  pre=ROOT/"adapters/codex/bin/preflight.sh"
  result=subprocess.run([str(pre),"liveness",str(self.jobs),"--route","r4"],capture_output=True,text=True,env={**os.environ,"AGENT_HOME":str(ROOT)})
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertIn("new-live",result.stdout);self.assertNotIn("old-dead",result.stdout)
 def test_namespace_local_attempt_state_uses_exact_heartbeat(self):
  heartbeat_dir=self.base/".dispatch/heartbeats";heartbeat_dir.mkdir(parents=True)
  attempt="att-namespace-state";route="r-namespace";node="test"
  heartbeat={"attempt_id":attempt,"route_id":route,"route_node":node,
             "phase":"tool","sequence":3,"updated_at":time.time()}
  (heartbeat_dir/f"{attempt}.json").write_text(json.dumps(heartbeat))
  args=("attempt-state","--pid","437","--pid-start","1","--pid-scope","namespace-local",
        "--attempt",attempt,"--route",route,"--node",node)
  live=self.invoke(*args);self.assertEqual(live.returncode,0,live.stdout+live.stderr);self.assertIn("state=working",live.stdout)
  heartbeat["phase"]="terminal";(heartbeat_dir/f"{attempt}.json").write_text(json.dumps(heartbeat))
  done=self.invoke(*args);self.assertEqual(done.returncode,0,done.stdout+done.stderr);self.assertIn("state=done",done.stdout)
  with self.jobs.open("a") as out:
   out.write(f"2026-07-16T00:00:05Z\topen\t/r\t/w\tnamespace\troute_id={route},route_node={node},attempt_id={attempt},pid=437,pid_start=1,pid_scope=namespace-local\n")
  applied=self.invoke("reconcile","--attempt",attempt,"--apply")
  record=json.loads(applied.stdout);self.assertEqual(record["closed"],1);self.assertEqual(record["decisions"][0]["category"],"terminal-heartbeat")
  self.assertIn("note=completed-terminal-heartbeat",self.jobs.read_text())
 def test_codex_liveness_rejects_visible_namespace_pid_without_proof(self):
  import importlib.util
  path=ROOT/"adapters/codex/bin/dispatch-liveness.py"
  spec=importlib.util.spec_from_file_location("dispatch_liveness_test",path)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  start=(Path("/proc")/str(self.proc.pid)/"stat").read_text().split()[21]
  state=module.recorded_attempt_state(
   {"attempt_id":"att-visible-no-route","pid":str(self.proc.pid),"pid_start":start,
    "pid_scope":"namespace-local"},time.time(),self.base)
  self.assertEqual(state["state"],"unknown")
  self.assertEqual(state["source"],"namespace")
  self.assertFalse(state["pid_authoritative"])
 def test_codex_liveness_accepts_namespace_bound_outer_pid(self):
  import importlib.util
  path=ROOT/"adapters/codex/bin/dispatch-liveness.py"
  spec=importlib.util.spec_from_file_location("dispatch_liveness_bound_test",path)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  start=(Path("/proc")/str(self.proc.pid)/"stat").read_text().split()[21]
  namespace=os.readlink("/proc/self/ns/pid")
  state=module.recorded_attempt_state(
   {"attempt_id":"att-bound-no-route","pid":"7","pid_start":start,
    "pid_scope":"namespace-local","pid_observer_ns":"pid:[inner]",
    "pid_host":str(self.proc.pid),"pid_host_start":start,
    "pid_host_ns":namespace,"pid_host_proof":"nspid-procfs-root-v1"},
   time.time(),self.base)
  self.assertEqual(state["state"],"working")
  self.assertEqual(state["source"],"proc")
  self.assertTrue(state["pid_authoritative"])
  self.assertEqual(state["pid_identity_source"],"host")

 def test_cascade_accepts_only_namespace_bound_outer_identity(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_bound_pid",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  process=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   start=(Path("/proc")/str(process.pid)/"stat").read_text().split()[21]
   state,pid,expected=module._cascade_process_state({
    "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
    "execution_surface":"registered-headless","registered_worker":"1",
    "fallback_hop":"same-harness-headless",
    "pid":"7","pid_start":start,"pid_scope":"namespace-local",
    "pid_observer_ns":"pid:[inner]","pid_host":str(process.pid),
    "pid_host_start":start,"pid_host_ns":os.readlink("/proc/self/ns/pid"),
    "pid_host_proof":"nspid-procfs-root-v1","pgid_host":str(process.pid),
   })
   self.assertEqual((state,pid,expected),("live-group",process.pid,start))
  finally:
   if process.poll() is None:process.kill()
   process.wait()

 def test_invalid_contract_child_has_no_cascade_signal_authority(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_invalid_contract",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  attempt="att-invalid-cascade"
  self.jobs.write_text(
   "2026-07-16T00:00:00Z\topen\t/r\t/w\tchild\t"
   "attempt_schema_version=2,dispatch_depth=2,transport=bogus,"
   "execution_surface=registered-headless,registered_worker=1,"
   "fallback_hop=same-harness-headless,parent=owner,"
   f"parent_attempt_id=att-owner-invalid,attempt_id={attempt},"
   "pid=437,pid_start=42,pgid=437\n")
  owner={"repo":"/r","worktree":"/w","slug":"owner",
         "meta":{"attempt_id":"att-owner-invalid"}}
  args=type("Args",(),{"jobs":self.jobs,"agent_home":self.base,
       "cascade_grace":0.0,"cascade_kill_wait":0.0})()
  with mock.patch.object(module,"_signal_exact_group") as send:
   decisions=module.cascade_orphan_children(owner,None,args)
  self.assertEqual(decisions[0]["status"],"contract-unverifiable")
  send.assert_not_called()

 def test_cascade_uses_quiescence_selected_identity_not_metadata_order(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_identity_source",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  identity=type("Identity",(),{
   "source":"host","pid":1437,"expected_start":"42"})()
  process=type("Process",(),{
   "state":"live","reason":"host-pid-live","identity":identity})()
  metadata={
   "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
   "execution_surface":"registered-headless","registered_worker":"1",
   "fallback_hop":"same-harness-headless","pid":"437","pid_start":"42",
   "pid_host":"1437","pid_host_start":"42","pgid_host":"1437",
  }
  with mock.patch.object(module,"attempt_process_quiescence",return_value=process):
   state=module._cascade_process_state(metadata)
  self.assertEqual(state,("live-group",1437,"42"))

 def test_unverifiable_group_scan_never_satisfies_cascade_wait(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_unknown_group",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  group=type("Group",(),{"state":"unverifiable"})()
  with mock.patch.object(
      module,"process_observation",return_value=("missing","", "")), \
       mock.patch.object(module,"process_group_observation",return_value=group):
   self.assertFalse(module._wait_exact_group_end(437,"42",0.0))

 def test_terminal_revalidation_veto_prevents_stale_snapshot_signal(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_terminal_race",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  proc=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
   attempt="att-terminal-race"
   self.jobs.write_text(
    "2026-07-16T00:00:00Z\topen\t/r\t/w\tchild\t"
    f"{CURRENT_ATTEMPT_CONTRACT},parent=owner,parent_attempt_id=att-owner-race,"
    f"attempt_id={attempt},pid={proc.pid},pid_start={start},pgid={proc.pid}\n")
   owner={"repo":"/r","worktree":"/w","slug":"owner",
          "meta":{"attempt_id":"att-owner-race"}}
   args=type("Args",(),{"jobs":self.jobs,"agent_home":self.base,
        "cascade_grace":0.0,"cascade_kill_wait":0.0})()
   with mock.patch.object(
       module,"_close_cascade_child",return_value=(False,"no-terminal-evidence")), \
        mock.patch.object(
         module,"_cascade_terminal_note",
         return_value=("completed-marker","completed-marker-linkage")), \
        mock.patch.object(module,"_signal_exact_group") as send:
    decisions=module.cascade_orphan_children(owner,None,args)
   self.assertEqual(decisions[0]["status"],"terminal:completed-marker")
   send.assert_not_called()
   self.assertIsNone(proc.poll())
  finally:
   if proc.poll() is None:proc.kill()
   proc.wait()

 def test_already_terminal_close_result_never_falls_through_to_signal(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_closed_race",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  self.jobs.write_text(
   "2026-07-16T00:00:00Z\topen\t/r\t/w\tchild\t"
   f"{CURRENT_ATTEMPT_CONTRACT},parent=owner,parent_attempt_id=att-owner-closed,"
   "attempt_id=att-child-closed,pid=437,pid_start=42,pgid=437\n")
  owner={"repo":"/r","worktree":"/w","slug":"owner",
         "meta":{"attempt_id":"att-owner-closed"}}
  args=type("Args",(),{"jobs":self.jobs,"agent_home":self.base,
       "cascade_grace":0.0,"cascade_kill_wait":0.0})()
  with mock.patch.object(
      module,"_close_cascade_child",return_value=(False,"already-terminal")), \
       mock.patch.object(module,"_signal_exact_group") as send:
   decisions=module.cascade_orphan_children(owner,None,args)
  self.assertEqual(decisions[0]["status"],"already-terminal")
  send.assert_not_called()

 def test_teardown_claim_takeover_requires_exact_dead_holder(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_claim_recovery",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  child=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   start=(Path("/proc")/str(child.pid)/"stat").read_text().split()[21]
   attempt="att-claim-recovery"
   base=(
    f"{CURRENT_ATTEMPT_CONTRACT},parent=owner,parent_attempt_id=att-owner-claim,"
    f"attempt_id={attempt},pid={child.pid},pid_start={start},pgid={child.pid}")
   owner={"repo":"/r","worktree":"/w","slug":"owner",
          "meta":{"attempt_id":"att-owner-claim"}}
   args=type("Args",(),{"jobs":self.jobs,"agent_home":self.base,
        "cascade_grace":0.0,"cascade_kill_wait":0.0})()
   self.jobs.write_text(
    "2026-07-16T00:00:00Z\topen\t/r\t/w\tchild\t"+base+","
    "teardown_claim=old,teardown_claimed_at=then,"
    "teardown_claim_pid=99999991,teardown_claim_pid_start=1\n")
   with mock.patch.object(module,"_cascade_terminal_note",return_value=(None,None)):
    token,snapshot,status=module._claim_cascade_signal(
     args,owner,attempt,None)
   self.assertEqual(status,"claimed")
   self.assertTrue(token.startswith("cascade-att-owner-claim-"))
   self.assertEqual(snapshot["pid"],child.pid)
   metadata=module.parse_meta(self.jobs.read_text().strip().split("\t",5)[5])
   self.assertEqual(metadata["teardown_claim"],token)
   self.assertEqual(metadata["teardown_claim_pid"],str(os.getpid()))
   self.assertTrue(module._release_cascade_claim(self.jobs,attempt,token))

   holder_start=(Path("/proc")/str(os.getpid())/"stat").read_text().split()[21]
   self.jobs.write_text(
    "2026-07-16T00:00:00Z\topen\t/r\t/w\tchild\t"+base+","
    "teardown_claim=live,teardown_claimed_at=now,"
    f"teardown_claim_pid={os.getpid()},teardown_claim_pid_start={holder_start}\n")
   with mock.patch.object(module,"_cascade_terminal_note",return_value=(None,None)):
    token,_snapshot,status=module._claim_cascade_signal(
     args,owner,attempt,None)
   self.assertIsNone(token)
   self.assertEqual(status,"teardown-in-progress")
  finally:
   if child.poll() is None:child.kill()
   child.wait()


 def ghost_row(self,attempt,extra=""):
  """A namespace-local row whose PID is unreadable here, with a fresh heartbeat.

  This is the shape that stayed open forever: classify_attempt_evidence read
  the freshness, answered `working`, and reconcile had no note to close on.
  """
  observer=os.readlink("/proc/self/ns/pid")
  heartbeats=self.base/".dispatch/heartbeats";heartbeats.mkdir(parents=True,exist_ok=True)
  (heartbeats/f"{attempt}.json").write_text(json.dumps(
   {"attempt_id":attempt,"route_id":"r-ghost","route_node":"execute",
    "phase":"tool","sequence":3,"updated_at":time.time()}))
  return (f"2026-07-16T00:00:09Z\topen\t/r\t/w\tghost\t"
          f"route_id=r-ghost,route_node=execute,attempt_id={attempt},"
          f"pid=99999996,pid_start=1,pid_scope=namespace-local,"
          f"pid_ns=pid:[inner],pid_observer_ns={observer}{extra}")

 # B-P1. A fresh heartbeat no longer keeps a row open once the scan for its own
 # tag is provably empty, and the closure says which rule closed it.
 def test_fresh_heartbeat_row_closes_when_no_tagged_process_survives(self):
  attempt="att-ghost000001"
  self.jobs.write_text(self.ghost_row(attempt)+"\n")
  dry=json.loads(self.invoke("reconcile","--attempt",attempt).stdout)
  self.assertEqual(dry["closed"],0)
  self.assertEqual(dry["decisions"][0]["category"],"exact-dead")
  applied=json.loads(self.invoke("reconcile","--attempt",attempt,"--apply").stdout)
  self.assertEqual(applied["closed"],1)
  text=self.jobs.read_text()
  self.assertIn("note=dead-namespace-absent",text)
  self.assertNotIn("note=dead-exact-pid",text)

 # B-N2. The same row with one of its own tagged processes actually running
 # stays active. This is the "no regression on a healthy worker" control.
 def test_live_tagged_process_keeps_the_ghost_shaped_row_active(self):
  attempt="att-ghost000002"
  child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],
                         env={**os.environ,"AGENT_DISPATCH_ATTEMPT_ID":attempt})
  try:
   self.jobs.write_text(self.ghost_row(attempt)+"\n")
   record=json.loads(self.invoke("reconcile","--attempt",attempt,"--apply").stdout)
   self.assertEqual(record["closed"],0)
   self.assertEqual(record["decisions"][0]["category"],"active")
   self.assertIn("\topen\t",self.jobs.read_text())
  finally:
   child.terminate();child.wait(timeout=5)

 # B-N1. Authoritative liveness is stronger evidence than any scan and is
 # consulted first, so a live recorded process is never closed by this rule.
 def test_authoritative_live_identity_is_never_closed_by_the_scan_rule(self):
  attempt="att-ghost000003"
  start=(Path("/proc")/str(self.proc.pid)/"stat").read_text().split()[21]
  self.jobs.write_text(
   f"2026-07-16T00:00:09Z\topen\t/r\t/w\tghost\troute_id=r-ghost,route_node=execute,"
   f"attempt_id={attempt},pid={self.proc.pid},pid_start={start}\n")
  record=json.loads(self.invoke("reconcile","--attempt",attempt,"--apply").stdout)
  self.assertEqual(record["closed"],0)
  self.assertEqual(record["decisions"][0]["category"],"active")

 # B-N3. A scan this observer cannot perform is not a death.
 def test_unscannable_namespace_row_is_not_closed(self):
  attempt="att-ghost000004"
  row=self.ghost_row(attempt).replace(
   os.readlink("/proc/self/ns/pid"),"pid:[elsewhere]")
  self.jobs.write_text(row+"\n")
  record=json.loads(self.invoke("reconcile","--attempt",attempt,"--apply").stdout)
  self.assertEqual(record["closed"],0)
  self.assertNotIn("note=dead-namespace-absent",self.jobs.read_text())

 def test_explicit_receiptless_namespace_cancel_is_typed_not_completion(self):
  attempt="att-receiptless-cancel"
  empty_log=self.base/"receiptless.codex.jsonl"
  empty_log.write_text(json.dumps({"type":"system","subtype":"init"})+"\n")
  self.jobs.write_text(self.ghost_row(
   attempt,
   extra=(",pgid=99999996,pid_ns=pid:[extinct],"
          "pid_observer_ns=pid:[extinct],harness=codex,"
          f"log_file={empty_log}"),
  )+"\n")
  heartbeat=self.base/".dispatch/heartbeats"/f"{attempt}.json"
  heartbeat_value=json.loads(heartbeat.read_text())
  heartbeat_value["updated_at"]=time.time()-3600
  heartbeat.write_text(json.dumps(heartbeat_value))
  dry=json.loads(self.invoke(
   "reconcile","--attempt",attempt,"--cancel-receiptless-namespace").stdout)
  self.assertEqual(dry["closed"],0)
  self.assertTrue(dry["decisions"][0]["eligible"],dry)
  applied=json.loads(self.invoke(
   "reconcile","--attempt",attempt,"--cancel-receiptless-namespace","--apply").stdout)
  self.assertEqual(applied["closed"],1,applied)
  text=self.jobs.read_text()
  self.assertIn("note=cancelled-receipt-unavailable",text)
  self.assertIn("failure_class=cancelled",text)
  self.assertIn("operator-receiptless-cancel-v1",text)
  self.assertNotIn("completed-marker",text)
  self.assertNotIn("group_reap_proof",text)
  self.assertNotIn("launch_outcome",text)

 def test_receiptless_cancel_refuses_fresh_exact_heartbeat(self):
  attempt="att-receiptless-heartbeat"
  empty_log=self.base/"receiptless-heartbeat.codex.jsonl"
  empty_log.write_text(json.dumps({"type":"system","subtype":"init"})+"\n")
  self.jobs.write_text(self.ghost_row(
   attempt,
   extra=(",pgid=99999996,pid_ns=pid:[extinct],"
          "pid_observer_ns=pid:[extinct],harness=codex,"
          f"log_file={empty_log}"),
  )+"\n")
  applied=json.loads(self.invoke(
   "reconcile","--attempt",attempt,"--cancel-receiptless-namespace","--apply").stdout)
  self.assertEqual(applied["closed"],0)
  self.assertEqual(applied["decisions"][0]["reason"],"attempt-evidence-active")
  self.assertIn("\topen\t",self.jobs.read_text())

 def test_receiptless_cancel_refuses_live_tagged_attempt(self):
  attempt="att-receiptless-live"
  empty_log=self.base/"receiptless-live.codex.jsonl"
  empty_log.write_text(json.dumps({"type":"system","subtype":"init"})+"\n")
  child=subprocess.Popen(
   [sys.executable,"-c","import time; time.sleep(30)"],
   env={**os.environ,"AGENT_DISPATCH_ATTEMPT_ID":attempt},
  )
  try:
   self.jobs.write_text(self.ghost_row(
    attempt,
    extra=(",pgid=99999996,pid_ns=pid:[extinct],"
           "pid_observer_ns=pid:[extinct],harness=codex,"
           f"log_file={empty_log}"),
   )+"\n")
   applied=json.loads(self.invoke(
    "reconcile","--attempt",attempt,"--cancel-receiptless-namespace","--apply").stdout)
   self.assertEqual(applied["closed"],0)
   self.assertEqual(applied["decisions"][0]["reason"],"attempt-descendant-live")
   self.assertIn("\topen\t",self.jobs.read_text())
  finally:
   child.terminate();child.wait(timeout=5)

 def test_receiptless_cancel_requires_exact_attempt_filter(self):
  result=self.invoke("reconcile","--route","r-ghost","--cancel-receiptless-namespace","--apply")
  self.assertEqual(result.returncode,64)
  self.assertIn("exact-attempt-cancel-required",result.stdout)


class ArtifactProofReceiptSealTest(unittest.TestCase):
 """A PASS worker whose post-exit receipt can never be issued must be recoverable.

 The detached drain receipt needs `attempt-tagged-empty-v1`. One process that
 escapes the governed process group while carrying the attempt tag makes that
 proof unobtainable forever, so `reconcile` answers `terminal-draining` and
 `dispatch_completion_join` answers `process-unverifiable` for a worker that
 finished and wrote its artifact -- with no checked path back. The seal supplies
 the one substitute that is evidence about the worker itself: its own final
 artifact-heartbeat digest, matched against the artifact on disk.
 """

 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
  self.home=self.base/"home";self.home.mkdir()
  self.jobs=self.base/"jobs.log"
  self.repo=self.base/"repo";self.repo.mkdir()
  for command in (["git","init","-q"],["git","config","user.email","t@example.invalid"],
                  ["git","config","user.name","Fixture"]):
   subprocess.run(command,cwd=self.repo,check=True)
  (self.repo/"tracked.txt").write_text("tracked\n")
  subprocess.run(["git","add","tracked.txt"],cwd=self.repo,check=True)
  subprocess.run(["git","commit","-qm","init"],cwd=self.repo,check=True)
  self.artifact_root=self.repo/".agent_reports"
  self.artifact=self.artifact_root/"plans"/"fixture"/"plan.md"
  self.artifact.parent.mkdir(parents=True)
  self.artifact.write_text("final worker output\n")
  self.digest=hashlib.sha256(self.artifact.read_bytes()).hexdigest()
  self.observer=os.readlink("/proc/self/ns/pid")
  self.attempt="att-artifact-proof-1"
  self.log=self.base/"worker.codex.jsonl"
  self.write_log(f"artifact: {self.artifact}\nverdict: PASS\nblocker: none")
  self.write_heartbeat(phase="artifact",evidence=f"sha256:{self.digest}")

 def tearDown(self):
  self.tmp.cleanup()

 def write_log(self,final_message):
  self.log.write_text(
   json.dumps({"type":"item.completed",
               "item":{"type":"agent_message","text":final_message}})+"\n"
   +json.dumps({"type":"turn.completed"})+"\n")

 def write_heartbeat(self,*,phase,evidence,attempt=None,route_id="r-proof",
                     route_node="execute"):
  heartbeats=self.home/".dispatch"/"heartbeats";heartbeats.mkdir(parents=True,exist_ok=True)
  name=(attempt or self.attempt).replace("/","_")
  (heartbeats/f"{name}.json").write_text(json.dumps(
   {"schema_version":1,"attempt_id":attempt or self.attempt,"route_id":route_id,
    "route_node":route_node,"phase":phase,"sequence":7,"kind":"artifact",
    "evidence":evidence,"updated_at":time.time()}))

 def write_row(self,*,status="done",extra="",pid=99999996):
  self.jobs.write_text(
   f"2026-08-19T00:00:00Z\t{status}\t{self.repo}\t{self.repo}\tproof\t"
   f"route_id=r-proof,route_node=execute,route_hash=h-proof,"
   f"attempt_id={self.attempt},pid={pid},pid_start=1,"
   f"pid_scope=namespace-local,pid_ns={self.observer},"
   f"pid_observer_ns={self.observer},pgid={pid},launch_lifecycle=detached,"
   f"artifact_root={self.artifact_root},log_file={self.log}{extra}\n")
  currentize_registry(self.jobs)

 def invoke(self,*args):
  # Hermetic: an inherited AGENT_ARTIFACT_ROOT would redirect the terminal
  # inspector's root resolution away from this fixture's repository.
  env={key:value for key,value in os.environ.items()
       if key not in {"AGENT_ARTIFACT_ROOT","AGENT_DISPATCH_ATTEMPT_ID"}}
  return subprocess.run(
   [sys.executable,str(SCRIPT),*args,"--jobs",str(self.jobs),
    "--agent-home",str(self.home)],
   capture_output=True,text=True,env=env)

 def seal(self,*extra):
  result=self.invoke("reconcile","--attempt",self.attempt,
                     "--seal-artifact-proof-receipt",*extra)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  return json.loads(result.stdout)

 def test_dry_run_reports_eligible_and_writes_nothing(self):
  self.write_row()
  before=self.jobs.read_text()
  record=self.seal()
  self.assertEqual(record["sealed"],0)
  self.assertTrue(record["decisions"][0]["eligible"],record)
  self.assertEqual(record["decisions"][0]["artifact_sha256"],self.digest)
  self.assertEqual(self.jobs.read_text(),before)

 def test_seal_makes_a_closed_row_reach_a_terminal_verdict(self):
  self.write_row()
  metadata=parse_registry_metadata(self.jobs.read_text().strip().split("\t",5)[5])
  before=observed_attempt_liveness("done",metadata,terminal_receipt_gate=True)
  self.assertEqual(before.state,"unverifiable")
  self.assertEqual(before.process_reason,"post-exit-receipt-incomplete")

  record=self.seal("--apply")
  self.assertEqual(record["sealed"],1,record)
  self.assertTrue(record["decisions"][0]["revalidated"])
  text=self.jobs.read_text()
  self.assertIn("post_exit_receipt_substitute=artifact-proof-v1",text)
  self.assertIn(f"artifact_proof_sha256={self.digest}",text)
  self.assertIn("artifact_proof_verdict=PASS",text)
  # The seal is not completion: it invents no marker, verdict, or reap proof.
  self.assertNotIn("completed-marker",text)
  self.assertNotIn("group_reap_proof",text)
  self.assertNotIn("attempt_descendant_proof",text)
  self.assertNotIn("launch_outcome",text)

  sealed=parse_registry_metadata(text.strip().split("\t",5)[5])
  self.assertEqual(post_exit_receipt_reason(sealed),
                   "receipt-superseded-by-artifact-proof")
  after=observed_attempt_liveness("done",sealed,terminal_receipt_gate=True)
  self.assertEqual(after.state,"terminal")
  self.assertEqual(after.reason,"registry-closed")

 def test_seal_survives_a_live_tagged_process_that_outlived_the_worker(self):
  """The exact shape that made the receipt unissuable: a leaked tagged process."""
  child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],
                         env={**os.environ,"AGENT_DISPATCH_ATTEMPT_ID":self.attempt})
  try:
   self.write_row()
   metadata=parse_registry_metadata(self.jobs.read_text().strip().split("\t",5)[5])
   self.assertEqual(
    attempt_tagged_descendants(metadata).state,"populated",
    "fixture precondition: a tagged process must be visible")
   self.assertEqual(self.seal("--apply")["sealed"],1)
   sealed=parse_registry_metadata(self.jobs.read_text().strip().split("\t",5)[5])
   self.assertEqual(
    observed_attempt_liveness("done",sealed,terminal_receipt_gate=True).state,
    "terminal")
   # Without the seal the same live tag still vetoes quiescence -- and now
   # reports the descendant as live process evidence instead of merely
   # withholding terminal progression, matching every other populated-scan
   # case in the precedence ladder.
   unsealed={key:value for key,value in sealed.items()
             if not key.startswith("artifact_proof_")
             and key!="post_exit_receipt_substitute"}
   self.assertEqual(
    attempt_process_quiescence(unsealed,terminal_receipt=True).state,
    "live")
  finally:
   child.terminate();child.wait(timeout=5)

 def test_refuses_when_the_artifact_no_longer_matches_the_heartbeat(self):
  self.write_row()
  self.artifact.write_text("someone edited the artifact after the worker died\n")
  record=self.seal("--apply")
  self.assertEqual(record["sealed"],0)
  self.assertEqual(record["decisions"][0]["reason"],"artifact-digest-mismatch")
  self.assertNotIn("artifact-proof-v1",self.jobs.read_text())

 def test_refuses_without_an_artifact_phase_heartbeat(self):
  self.write_row()
  self.write_heartbeat(phase="tool",evidence=f"sha256:{self.digest}")
  record=self.seal("--apply")
  self.assertEqual(record["sealed"],0)
  self.assertEqual(record["decisions"][0]["reason"],"heartbeat-phase-tool")
  self.assertNotIn("artifact-proof-v1",self.jobs.read_text())

 def test_refuses_a_non_pass_terminal_envelope(self):
  self.write_row()
  self.write_log(f"artifact: {self.artifact}\nverdict: FAIL\nblocker: something broke")
  record=self.seal("--apply")
  self.assertEqual(record["sealed"],0)
  self.assertEqual(record["decisions"][0]["reason"],"terminal-verdict-FAIL")
  self.assertNotIn("artifact-proof-v1",self.jobs.read_text())

 def test_refuses_a_live_governed_process(self):
  live=subprocess.Popen(["sleep","60"])
  try:
   start=(Path("/proc")/str(live.pid)/"stat").read_text().split()[21]
   self.write_row(status="open",pid=live.pid,
                  extra="")
   self.jobs.write_text(self.jobs.read_text().replace("pid_start=1",f"pid_start={start}"))
   record=self.seal("--apply")
   self.assertEqual(record["sealed"],0)
   self.assertTrue(record["decisions"][0]["reason"].startswith("governed-process-"),
                   record)
   self.assertNotIn("artifact-proof-v1",self.jobs.read_text())
  finally:
   live.terminate();live.wait(timeout=5)

 def test_refuses_a_foreign_observer_namespace(self):
  self.write_row()
  self.jobs.write_text(
   self.jobs.read_text().replace(f"pid_observer_ns={self.observer}",
                                 "pid_observer_ns=pid:[elsewhere]"))
  record=self.seal("--apply")
  self.assertEqual(record["sealed"],0)
  self.assertEqual(record["decisions"][0]["reason"],"observer-namespace-mismatch")
  self.assertNotIn("artifact-proof-v1",self.jobs.read_text())

 def test_refuses_when_a_real_receipt_already_exists(self):
  self.write_row(extra=(",launch_outcome=governed-process-group-drained,"
                        "group_reap_proof=pgid-empty-v1,group_reap_pgid=99999996,"
                        "attempt_descendant_proof=attempt-tagged-empty-v1,"
                        f"attempt_descendant_observer_ns={self.observer}"))
  record=self.seal("--apply")
  self.assertEqual(record["sealed"],0)
  self.assertEqual(record["decisions"][0]["reason"],"post-exit-receipt-present")

 def test_requires_an_exact_attempt_filter(self):
  self.write_row()
  result=self.invoke("reconcile","--route","r-proof","--seal-artifact-proof-receipt","--apply")
  self.assertEqual(result.returncode,64)
  self.assertIn("exact-attempt-seal-required",result.stdout)

 def test_the_two_recovery_modes_are_mutually_exclusive(self):
  self.write_row()
  result=self.invoke("reconcile","--attempt",self.attempt,
                     "--seal-artifact-proof-receipt","--cancel-receiptless-namespace")
  self.assertEqual(result.returncode,64)
  self.assertIn("receipt-recovery-mode-conflict",result.stdout)


class MixedRegistryTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.home=self.base/"home";self.jobs=self.base/"jobs.log"
  bare=self.base/"remote.git";subprocess.run(["git","init","--bare","-q",str(bare)],check=True)
  self.primary=self.base/"primary";subprocess.run(["git","clone","-q",str(bare),str(self.primary)],check=True)
  subprocess.run(["git","-C",str(self.primary),"config","user.email","fixture@example.com"],check=True)
  subprocess.run(["git","-C",str(self.primary),"config","user.name","Fixture"],check=True)
  (self.primary/"base.txt").write_text("base")
  subprocess.run(["git","-C",str(self.primary),"add","base.txt"],check=True)
  subprocess.run(["git","-C",str(self.primary),"commit","-qm","base"],check=True)
  subprocess.run(["git","-C",str(self.primary),"branch","-M","main"],check=True)
  subprocess.run(["git","-C",str(self.primary),"push","-qu","origin","main"],check=True)
  self.merged=self.base/"merged";self.unsafe=self.base/"unsafe"
  subprocess.run(["git","-C",str(self.primary),"worktree","add","-q","-b","merged-fixture",str(self.merged),"main"],check=True)
  subprocess.run(["git","-C",str(self.primary),"worktree","add","-q","-b","unsafe-fixture",str(self.unsafe),"main"],check=True)
  (self.unsafe/"unsafe.txt").write_text("unmerged")
  subprocess.run(["git","-C",str(self.unsafe),"add","unsafe.txt"],check=True)
  subprocess.run(["git","-C",str(self.unsafe),"-c","user.email=fixture@example.com","-c","user.name=Fixture","commit","-qm","unsafe"],check=True)
  self.proc=subprocess.Popen(["sleep","60"]);start=(Path("/proc")/str(self.proc.pid)/"stat").read_text().split()[21]
  old="2020-01-01T00:00:00Z";repo=str(self.primary)
  rows=[
   f"{old}\topen\t{repo}\t/x\tactive\tparent_sid=s1,route_id=r1,route_node=active,route_hash=h1,attempt_id=att-active-mixed,pid={self.proc.pid},pid_start={start}",
   f"{old}\topen\t{repo}\t/x\tdead\tparent_sid=s1,route_id=r1,route_node=dead,route_hash=h1,attempt_id=att-dead-mixed,pid=99999991,pid_start=1",
   f"{old}\topen\t{repo}\t{self.merged}\tmerged\tparent_sid=s1,route_id=r1,route_node=merged,route_hash=h1,attempt_id=att-merged-mixed",
   f"{old}\topen\t{repo}\t/x\tstale\tparent_sid=s1,route_id=r1,route_node=stale,route_hash=h-stale,registry_digest=gd-stale,attempt_id=att-stale-mixed,completion_gate=code-test",
   f"{old}\topen\t{repo}\t{self.unsafe}\tunsafe\tparent_sid=s1,route_id=r1,route_node=unsafe,route_hash=h1,attempt_id=att-unsafe-mixed",
   f"{old}\topen\t{repo}\t/x\tunrelated\tparent_sid=s2,route_id=r2,route_node=other,route_hash=h2,attempt_id=att-other-mixed,pid=99999992,pid_start=1",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  currentize_registry(self.jobs)
  evidence=self.base/"stale-evidence.md";evidence.write_text("complete")
  marker_dir=self.home/".dispatch/completion/r1";marker_dir.mkdir(parents=True)
  marker={"schema_version":2,"route_id":"r1","route_hash":"h-stale","registry_digest":"gd-stale",
   "node_id":"stale","attempt_id":"att-stale-mixed","dispatch_depth":2,"transport":"headless",
   "execution_surface":"registered-headless","registered_worker":True,
   "fallback_hop":"same-harness-headless","completion_gate":"code-test","sequence":1,
   "evidence":{"path":str(evidence),"sha256":hashlib.sha256(evidence.read_bytes()).hexdigest()},
   "completed_at":"2026-07-16T00:00:00Z"}
  (marker_dir/"stale.json").write_text(json.dumps(marker))
  (marker_dir/"stale.1.json").write_text(json.dumps(marker))
  link={"schema_version":2,"route_id":"r1","node_id":"stale","attempt_id":"att-stale-mixed",
   "dispatch_depth":2,"transport":"headless","execution_surface":"registered-headless",
   "registered_worker":True,"fallback_hop":"same-harness-headless",
   "evidence_sha256":marker["evidence"]["sha256"],
   "completion_marker":str(marker_dir/"stale.json"),
   "completion_marker_history":str(marker_dir/"stale.1.json")}
  (marker_dir/"stale.att-stale-mixed.attempt.json").write_text(json.dumps(link))
  wd=self.home/".dispatch/watchdog";wd.mkdir(parents=True)
  (wd/"att-stale-mixed.json").write_text(json.dumps({"quiet_windows":2,"observed_at":time.time()+10,"last_progress_at":0}))
 def tearDown(self):
  if self.proc.poll() is None:self.proc.kill()
  self.proc.wait();subprocess.run(["git","-C",str(self.primary),"worktree","remove","--force",str(self.merged)],capture_output=True)
  subprocess.run(["git","-C",str(self.primary),"worktree","remove","--force",str(self.unsafe)],capture_output=True)
  self.tmp.cleanup()
 def invoke(self,*args):
  currentize_registry(self.jobs)
  return subprocess.run([sys.executable,str(SCRIPT),*args,"--jobs",str(self.jobs),"--agent-home",str(self.home)],capture_output=True,text=True)
 def test_mixed_current_and_guarded_reconcile(self):
  current=json.loads(self.invoke("current","--session","s1").stdout)
  self.assertEqual(current["total"],5);self.assertTrue(all(row["meta"].get("parent_sid")=="s1" for row in current["rows"]))
  self.assertEqual(json.loads(self.invoke("current","--job","dead").stdout)["total"],1)
  before=self.jobs.read_text();dry=json.loads(self.invoke("reconcile","--route","r1").stdout)
  self.assertEqual(dry["closed"],0);self.assertEqual(self.jobs.read_text(),before)
  applied=json.loads(self.invoke("reconcile","--route","r1","--apply").stdout)
  categories={item["slug"]:item["category"] for item in applied["decisions"]}
  self.assertEqual(categories,{"active":"active","dead":"exact-dead","merged":"merged","stale":"stale-terminal","unsafe":"unsafe"})
  text=self.jobs.read_text();self.assertIn("note=dead-exact-pid",text);self.assertIn("note=cleanup-merged",text);self.assertIn("note=dead-stale-terminal",text)
  self.assertIn("\topen\t"+str(self.primary)+"\t"+str(self.unsafe)+"\tunsafe\t",text)
  self.assertIn("\topen\t"+str(self.primary)+"\t/x\tunrelated\t",text)
  again=json.loads(self.invoke("reconcile","--route","r1","--apply").stdout);self.assertEqual(again["closed"],0)

 def test_marker_repair_rejects_noncanonical_completion_pointer(self):
  spec=importlib.util.spec_from_file_location("dispatch_registry_pointer",SCRIPT)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  row=next(
   item for item in module.read_rows(self.jobs)
   if item["meta"].get("attempt_id")=="att-stale-mixed"
  )
  self.assertTrue(module._marker_backed_repair(row,self.home))
  link_path=self.home/".dispatch/completion/r1/stale.att-stale-mixed.attempt.json"
  link=json.loads(link_path.read_text())
  link["completion_marker"]=str(self.home/".dispatch/completion/r1/forged.json")
  link_path.write_text(json.dumps(link))
  self.assertFalse(module._marker_backed_repair(row,self.home))
 def test_concurrent_reconcile_adds_one_terminal_note(self):
  row="2020-01-01T00:00:00Z\topen\t/r\t/x\trace\tparent_sid=s3,route_id=rc,route_node=n,attempt_id=att-race-mixed,pid=99999990,pid_start=1\n"
  with self.jobs.open("a") as out:out.write(row)
  currentize_registry(self.jobs)
  cmd=[sys.executable,str(SCRIPT),"reconcile","--attempt","att-race-mixed","--apply","--jobs",str(self.jobs),"--agent-home",str(self.home)]
  procs=[subprocess.Popen(cmd,stdout=subprocess.PIPE,text=True) for _ in range(4)]
  results=[json.loads(p.communicate(timeout=10)[0]) for p in procs]
  self.assertEqual(sum(result["closed"] for result in results),1)
  self.assertEqual(self.jobs.read_text().count("att-race-mixed"),1)
  self.assertEqual(self.jobs.read_text().count("note=dead-exact-pid"),1)
class OrphanReconcileTest(unittest.TestCase):
 """SD-64/71 post-exit orphan-conductor reconcile classification."""
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.home=self.base/"home";self.jobs=self.base/"jobs.log"
  self.route_id="rt-orphan-fixture"
  route={"route_id":self.route_id,"nodes":[
   {"id":"plan","depends_on":[]},{"id":"execute","depends_on":["plan"]},
   {"id":"test","depends_on":["execute"]},{"id":"report","depends_on":["test"]}]}
  self.route_file=self.base/"route.json";self.route_file.write_text(json.dumps(route))
  self.marker_dir=self.home/".dispatch/completion"/self.route_id;self.marker_dir.mkdir(parents=True)
 def tearDown(self): self.tmp.cleanup()
 def mark(self,node_id):
  (self.marker_dir/f"{node_id}.json").write_text(json.dumps({"node_id":node_id}))
 def owner_row(self,slug,attempt_id,pid,pid_start,extra="",include_route=True):
  route_meta=f"route_id={self.route_id},route_file={self.route_file}," if include_route else ""
  meta=(f"attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        f"execution_surface=registered-headless,registered_worker=1,"
        f"fallback_hop=same-harness-headless,{route_meta}worker_type=owner,"
        f"attempt_id={attempt_id},pid={pid},pid_start={pid_start}")
  if extra: meta+=","+extra
  return f"2026-07-16T00:00:00Z\topen\t/r\t/w\t{slug}\t{meta}"
 def invoke(self,*args):
  currentize_registry(self.jobs)
  return subprocess.run([sys.executable,str(SCRIPT),*args,"--jobs",str(self.jobs),"--agent-home",str(self.home)],capture_output=True,text=True)
 def test_dead_owner_with_open_child_is_orphaned(self):
  self.mark("plan")
  live=subprocess.Popen(["sleep","60"],start_new_session=True);start=(Path("/proc")/str(live.pid)/"stat").read_text().split()[21]
  try:
   rows=[
    self.owner_row("owner","att-owner-dead",99999995,1),
    f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\troute_id={self.route_id},route_node=execute,attempt_id=att-child-live,parent=owner,parent_attempt_id=att-owner-dead,pid={live.pid},pid_start={start},pgid={live.pid}",
   ]
   self.jobs.write_text("\n".join(rows)+"\n")
   dry=json.loads(self.invoke("reconcile","--attempt","att-owner-dead").stdout)
   self.assertEqual(dry["decisions"][0]["proposed_note"],"dead-parent-orphaned")
   applied=json.loads(self.invoke("reconcile","--attempt","att-owner-dead","--apply").stdout)
   self.assertEqual(applied["closed"],1)
   text=self.jobs.read_text()
   self.assertIn("note=dead-parent-orphaned",text)
   self.assertIn("\tdone\t/r\t/w\tchild\t",text)
   self.assertIn("note=dead-parent-terminated",text)
   self.assertIsNotNone(live.poll())
  finally:
   live.kill();live.wait()
 def test_codex_terminal_post_exit_orphan_reconcile(self):
  self.mark("plan")
  artifact_root=self.base/".agent_reports";artifact_root.mkdir()
  log=self.base/"owner.codex.jsonl"
  raw_sentinel="RAW_TERMINAL_ORPHAN_SENTINEL"
  events=[
   {"type":"item.completed","item":{"type":"command_execution","exit_code":0,"aggregated_output":raw_sentinel}},
   {"type":"item.completed","item":{"type":"agent_message","text":"artifact: -\nverdict: PASS\nblocker: none"}},
   {"type":"turn.completed"},
  ]
  log.write_text("\n".join(json.dumps(event) for event in events)+"\n")
  owner_attempt="att-owner-terminal-pass"
  sibling=(f"2026-07-16T00:00:01Z\topen\t/r\t{self.base}\tchild\t"
           f"route_id={self.route_id},route_file={self.route_file},route_node=execute,"
           "attempt_id=att-child-terminal-pass,parent=owner,pid=99999989,pid_start=1")
  owner=(f"2026-07-16T00:00:00Z\topen\t/r\t{self.base}\towner\t"
         f"route_id={self.route_id},route_file={self.route_file},worker_type=owner,"
         f"attempt_id={owner_attempt},pid=99999990,pid_start=1,harness=codex,"
         f"artifact_root={artifact_root},log_file={log}")
  self.jobs.write_text(owner+"\n"+sibling+"\n")
  currentize_registry(self.jobs)
  liveness=subprocess.run(
   [sys.executable,str(ROOT/"adapters/codex/bin/dispatch-liveness.py"),str(self.jobs)],
   capture_output=True,text=True,
   env={**os.environ,"AGENT_HOME":str(self.home),"AGENT_ARTIFACT_ROOT":str(artifact_root),
        "CODEX_SESSIONS":str(self.base/"missing")},
  )
  self.assertEqual(liveness.returncode,3,liveness.stdout+liveness.stderr)
  self.assertIn("ORPHANED owner",liveness.stdout)
  self.assertNotIn("COMPLETED owner",liveness.stdout)
  self.assertNotIn(raw_sentinel,liveness.stdout+liveness.stderr)
  before_lines=self.jobs.read_text().splitlines()
  sibling_before=next(line for line in before_lines if "\tchild\t" in line)
  applied=self.invoke("reconcile","--attempt",owner_attempt,"--apply")
  record=json.loads(applied.stdout)
  self.assertEqual(record["closed"],1)
  self.assertEqual(record["decisions"][0]["category"],"orphan")
  self.assertEqual(record["decisions"][0]["proposed_note"],"dead-parent-orphaned")
  self.assertNotIn(raw_sentinel,applied.stdout+applied.stderr)
  after=self.jobs.read_text()
  self.assertIn("\tdone\t/r\t"+str(self.base)+"\towner\t",after)
  self.assertIn("note=dead-parent-orphaned",after)
  self.assertEqual(next(line for line in after.splitlines() if "\tchild\t" in line),sibling_before)
  again=self.invoke("reconcile","--attempt",owner_attempt,"--apply")
  self.assertEqual(json.loads(again.stdout)["closed"],0)
  self.assertEqual(self.jobs.read_text(),after)
 def test_real_owner_without_route_derives_from_open_child_and_surfaces_boundary(self):
  self.mark("plan")
  rows=[
   self.owner_row("owner","att-owner-derived",99999990,1,include_route=False),
   f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\t"
   f"route_id={self.route_id},route_file={self.route_file},route_node=execute,"
   "attempt_id=att-child-derived,parent=owner,pid=99999989,pid_start=1",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  status=self.invoke("orphan-status","--attempt","att-owner-derived")
  self.assertEqual(status.returncode,0,status.stdout+status.stderr)
  self.assertIn("orphan=1",status.stdout);self.assertIn(f"route_id={self.route_id}",status.stdout)
  self.assertIn("resume_boundary=execute",status.stdout)
  scan=self.invoke("orphan-scan")
  self.assertEqual(scan.returncode,0,scan.stdout+scan.stderr)
  self.assertIn("orphaned_conductor_jobs=1",scan.stdout)
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-derived","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["category"],"orphan")
  self.assertIn("\topen\t/r\t/w\tchild\t",self.jobs.read_text(),
                "even an exact-dead child remains open for depth-0 diagnosis")
 def test_terminal_child_route_context_detects_unstarted_successor(self):
  self.mark("plan");self.mark("execute")
  rows=[
   self.owner_row("owner","att-owner-terminal-child",99999988,1,include_route=False),
   f"2026-07-16T00:00:01Z\tdone\t/r\t/w\tchild\t"
   f"route_id={self.route_id},route_file={self.route_file},route_node=execute,"
   "attempt_id=att-child-terminal,parent=owner,note=completed-marker",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-terminal-child","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["category"],"orphan")
 def test_conflicting_child_route_context_fails_closed(self):
  other_route=self.base/"other-route.json"
  other_route.write_text(json.dumps({"route_id":"rt-other","nodes":[{"id":"plan","depends_on":[]}]}))
  rows=[
   self.owner_row("owner","att-owner-conflict",99999987,1,include_route=False),
   f"2026-07-16T00:00:01Z\tdone\t/r\t/w\tchild-a\t"
   f"route_id={self.route_id},route_file={self.route_file},route_node=plan,"
   "attempt_id=att-child-a,parent=owner",
   "2026-07-16T00:00:02Z\topen\t/r\t/w\tchild-b\t"
   f"route_id=rt-other,route_file={other_route},route_node=plan,"
   "attempt_id=att-child-b,parent=owner,pid=99999986,pid_start=1",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  result=json.loads(self.invoke("reconcile","--attempt","att-owner-conflict").stdout)
  self.assertNotEqual(result["decisions"][0]["category"],"orphan")
 def test_same_slug_replacement_owner_and_child_are_byte_identical(self):
  self.mark("plan")
  old_child=subprocess.Popen(["sleep","60"],start_new_session=True)
  replacement_owner=subprocess.Popen(["sleep","60"])
  replacement_child=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   old_start=(Path("/proc")/str(old_child.pid)/"stat").read_text().split()[21]
   owner_start=(Path("/proc")/str(replacement_owner.pid)/"stat").read_text().split()[21]
   new_start=(Path("/proc")/str(replacement_child.pid)/"stat").read_text().split()[21]
   rows=[
    self.owner_row("owner","att-owner-old",99999981,1),
    f"2026-07-16T00:00:01Z\topen\t/r\t/w\told-child\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-old,parent=owner,parent_attempt_id=att-owner-old,pid={old_child.pid},pid_start={old_start},pgid={old_child.pid}",
    self.owner_row("owner","att-owner-new",replacement_owner.pid,owner_start),
    f"2026-07-16T00:00:03Z\topen\t/r\t/w\tnew-child\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-new,parent=owner,parent_attempt_id=att-owner-new,pid={replacement_child.pid},pid_start={new_start}",
   ]
   self.jobs.write_text("\n".join(rows)+"\n")
   currentize_registry(self.jobs)
   before=self.jobs.read_text().splitlines()
   new_owner_before=next(line for line in before if "att-owner-new" in line)
   new_child_before=next(line for line in before if "att-child-new" in line)
   applied=json.loads(self.invoke("reconcile","--attempt","att-owner-old","--apply").stdout)
   self.assertEqual(applied["decisions"][0]["cascade"][0]["status"],"dead-parent-terminated")
   after=self.jobs.read_text().splitlines()
   self.assertEqual(next(line for line in after if "att-owner-new" in line),new_owner_before)
   self.assertEqual(next(line for line in after if "att-child-new" in line),new_child_before)
   self.assertIsNone(replacement_owner.poll());self.assertIsNone(replacement_child.poll())
   stable=self.jobs.read_bytes()
   self.invoke("reconcile","--attempt","att-owner-old","--apply")
   self.assertEqual(self.jobs.read_bytes(),stable)
  finally:
   for proc in (old_child,replacement_owner,replacement_child):
    if proc.poll() is None:proc.kill()
    proc.wait()
 def test_pid_reuse_closes_exact_row_without_signalling_replacement(self):
  self.mark("plan")
  unrelated=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   actual=(Path("/proc")/str(unrelated.pid)/"stat").read_text().split()[21]
   wrong=str(int(actual)+1)
   rows=[
    self.owner_row("owner","att-owner-reuse",99999980,1),
    f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-reuse,parent=owner,parent_attempt_id=att-owner-reuse,pid={unrelated.pid},pid_start={wrong}",
   ]
   self.jobs.write_text("\n".join(rows)+"\n")
   applied=json.loads(self.invoke("reconcile","--attempt","att-owner-reuse","--apply").stdout)
   self.assertEqual(applied["decisions"][0]["cascade"][0]["status"],"dead-parent-exited")
   self.assertIn("note=dead-parent-exited",self.jobs.read_text())
   self.assertIsNone(unrelated.poll())
  finally:
   if unrelated.poll() is None:unrelated.kill()
   unrelated.wait()
 def test_registered_but_unstarted_child_closes_without_process_identity(self):
  self.mark("plan")
  rows=[
   self.owner_row("owner","att-owner-unstarted",99999976,1),
   f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-unstarted,parent=owner,parent_attempt_id=att-owner-unstarted,launch_claimed=0",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-unstarted","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["cascade"][0]["status"],"dead-parent-exited")
  self.assertIn("note=dead-parent-exited",self.jobs.read_text())
 def test_claimed_child_without_process_identity_remains_open(self):
  self.mark("plan")
  rows=[
   self.owner_row("owner","att-owner-claimed",99999975,1),
   f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-claimed,parent=owner,parent_attempt_id=att-owner-claimed,launch_claimed=1",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-claimed","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["cascade"][0]["status"],"launch-indeterminate")
  self.assertIn("\topen\t/r\t/w\tchild\t",self.jobs.read_text())
  self.assertNotIn("note=dead-parent-exited",self.jobs.read_text())
 def test_non_group_leader_and_namespace_local_without_outer_pid_fail_closed(self):
  self.mark("plan")
  nongroup=subprocess.Popen(["sleep","60"])
  try:
   start=(Path("/proc")/str(nongroup.pid)/"stat").read_text().split()[21]
   rows=[
    self.owner_row("owner","att-owner-unsafe",99999979,1),
    f"2026-07-16T00:00:01Z\topen\t/r\t/w\tnongroup\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-nongroup,parent=owner,parent_attempt_id=att-owner-unsafe,pid={nongroup.pid},pid_start={start}",
    f"2026-07-16T00:00:02Z\topen\t/r\t/w\tnamespace\troute_id={self.route_id},route_file={self.route_file},route_node=test,attempt_id=att-child-namespace,parent=owner,parent_attempt_id=att-owner-unsafe,pid=437,pid_start=1,pid_scope=namespace-local",
   ]
   self.jobs.write_text("\n".join(rows)+"\n")
   applied=json.loads(self.invoke("reconcile","--attempt","att-owner-unsafe","--apply").stdout)
   statuses={item["attempt_id"]:item["status"] for item in applied["decisions"][0]["cascade"]}
   self.assertEqual(statuses["att-child-nongroup"],"non-group-leader")
   self.assertEqual(statuses["att-child-namespace"],"scope-unverifiable")
   self.assertIn("\topen\t/r\t/w\tnongroup\t",self.jobs.read_text())
   self.assertIn("\topen\t/r\t/w\tnamespace\t",self.jobs.read_text())
   self.assertIsNone(nongroup.poll())
  finally:
   if nongroup.poll() is None:nongroup.kill()
   nongroup.wait()
 def test_remounted_proc_outer_pid_claim_never_signals_unrelated_group(self):
  self.mark("plan")
  unrelated=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   start=(Path("/proc")/str(unrelated.pid)/"stat").read_text().split()[21]
   inner="pid:[inner-remounted]"
   rows=[
    self.owner_row("owner","att-owner-remounted",99999974,1),
    f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\t"
    f"route_id={self.route_id},route_file={self.route_file},route_node=execute,"
    "attempt_id=att-child-remounted,parent=owner,"
    "parent_attempt_id=att-owner-remounted,pid=7,pid_start=42,"
    f"pid_scope=namespace-local,pid_observer_ns={inner},"
    f"pid_host={unrelated.pid},pid_host_start={start},pid_host_ns={inner},"
    "pid_host_proof=nspid-procfs-root-v1",
   ]
   self.jobs.write_text("\n".join(rows)+"\n")
   applied=json.loads(self.invoke(
    "reconcile","--attempt","att-owner-remounted","--apply").stdout)
   cascade=applied["decisions"][0]["cascade"]
   self.assertEqual(cascade[0]["status"],"scope-unverifiable")
   self.assertIn("\topen\t/r\t/w\tchild\t",self.jobs.read_text())
   self.assertIsNone(unrelated.poll())
  finally:
   if unrelated.poll() is None:unrelated.kill()
   unrelated.wait()
 def test_claude_result_failure_outranks_parent_death_note(self):
  self.mark("plan")
  log=self.base/"child.claude.jsonl"
  log.write_text(json.dumps({"type":"result","subtype":"success","is_error":False,
   "result":"artifact: -\nverdict: FAIL\nblocker: fixture failure"})+"\n")
  rows=[
   self.owner_row("owner","att-owner-claude",99999978,1),
   f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\troute_id={self.route_id},route_file={self.route_file},route_node=execute,attempt_id=att-child-claude,parent=owner,parent_attempt_id=att-owner-claude,pid=99999977,pid_start=1,harness=claude,log_file={log}",
  ]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-claude","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["cascade"][0]["status"],"dead-worker-fail")
  text=self.jobs.read_text();self.assertIn("note=dead-worker-fail",text)
  self.assertNotIn("note=dead-parent-exited",next(line for line in text.splitlines() if "att-child-claude" in line))
 def test_live_conductor_completed_route_live_child_is_never_orphaned(self):
  for node in ("plan","execute","test","report"): self.mark(node)
  live_owner=subprocess.Popen(["sleep","60"]);owner_start=(Path("/proc")/str(live_owner.pid)/"stat").read_text().split()[21]
  live_child=subprocess.Popen(["sleep","60"]);child_start=(Path("/proc")/str(live_child.pid)/"stat").read_text().split()[21]
  try:
   rows=[
    self.owner_row("owner","att-owner-live",live_owner.pid,owner_start),
    f"2026-07-16T00:00:01Z\topen\t/r\t/w\tchild\troute_id={self.route_id},route_node=report,attempt_id=att-child-live2,parent=owner,pid={live_child.pid},pid_start={child_start}",
   ]
   self.jobs.write_text("\n".join(rows)+"\n")
   result=json.loads(self.invoke("reconcile","--attempt","att-owner-live").stdout)
   self.assertEqual(result["decisions"][0]["category"],"active")
   self.assertIsNone(result["decisions"][0]["proposed_note"])
  finally:
   live_owner.kill();live_owner.wait();live_child.kill();live_child.wait()
 def test_unstarted_successor_with_no_open_child_is_orphaned(self):
  self.mark("plan");self.mark("execute")  # test/report incomplete; report depends on test (unmarked) so only test is a ready un-started successor
  rows=[self.owner_row("owner","att-owner-dead2",99999994,1)]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-dead2","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["category"],"orphan")
  self.assertIn("note=dead-parent-orphaned",self.jobs.read_text())
 def test_dead_owner_with_completed_route_is_not_orphaned(self):
  for node in ("plan","execute","test","report"): self.mark(node)
  rows=[self.owner_row("owner","att-owner-dead3",99999993,1)]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-dead3","--apply").stdout)
  self.assertEqual(applied["decisions"][0]["category"],"exact-dead")
  self.assertNotEqual(applied["decisions"][0]["proposed_note"],"dead-parent-orphaned")
 def test_unreadable_route_record_fails_closed(self):
  rows=[self.owner_row("owner","att-owner-dead4",99999992,1,extra=f"route_file={self.base/'missing.json'}")]
  self.jobs.write_text("\n".join(rows)+"\n")
  applied=json.loads(self.invoke("reconcile","--attempt","att-owner-dead4","--apply").stdout)
  self.assertNotEqual(applied["decisions"][0]["category"],"orphan")


if __name__=="__main__":unittest.main()
