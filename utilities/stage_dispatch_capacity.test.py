#!/usr/bin/env python3
import importlib.util, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
S=importlib.util.spec_from_file_location("fallback",ROOT/"utilities/stage-dispatch-fallback.py")
F=importlib.util.module_from_spec(S);S.loader.exec_module(F)

class CapacityTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.jobs=Path(self.tmp.name)/"jobs.log"
  self.args=type("Args",(),{"slug":"s","parent":"p","parent_attempt_id":"att-parent",
   "jobs":self.jobs,
   "capacity_model":"gpt-5.6-luna","capacity_reasoning":"medium",
   "capacity_effort":None,"capacity_variant":None,"direct_timeout":2,
   "action":"register","progress_window_seconds":0,"watchdog_max_windows":2})()
  self.route={"route_id":"r","route_hash":"sha256:x"};self.node={"id":"test"}
  self.row={"child_harness":"codex","parent_harness":"codex","parent_transport":"headless",
   "parent_sandbox":"workspace-write","launch_authority":"conductor"}
  self.failed={"attempt_id":"att-initial0001","model":"gpt-5.6-sol"}
  self.jobs.write_text("2026-07-16T00:00:00Z\tdone\t/r\t/w\ts\t"
   "route_id=r,route_node=test,attempt_id=att-initial0001,model=gpt-5.6-sol,"
   "parent_harness=codex,parent_transport=headless,parent_sandbox=workspace-write,"
   "child_harness=codex,launch_authority=conductor,note=dead-capacity\n")
 def tearDown(self):self.tmp.cleanup()
 def test_retry_identity_is_distinct_stable_and_model_bound(self):
  original=F.attempt_identity(self.args,self.route,self.node,self.row,1)
  retry=F.capacity_attempt_identity(self.args,self.route,self.node,self.row,1,"gpt-5.6-luna")
  self.assertNotEqual(original,retry);self.assertEqual(retry,F.capacity_attempt_identity(self.args,self.route,self.node,self.row,1,"gpt-5.6-luna"))
  self.assertNotEqual(retry,F.capacity_attempt_identity(self.args,self.route,self.node,self.row,1,"gpt-5.6-sol"))
 def test_allowed_pair_and_same_model_zero_contract(self):
  # Derive expectations from the config cascade — efforts are user-tunable
  # defaults, so literal (model,effort) pins here would break on every tune.
  cascade=F.capacity_cascade("codex")
  self.assertGreaterEqual(len(cascade),2)
  for model,paired in cascade:
   self.assertTrue(F.allowed_capacity_settings("codex",model,paired))
  self.assertFalse(F.allowed_capacity_settings("codex",cascade[1][0],"not-a-real-effort"))
  failed,alternative=cascade[0][0],cascade[1][0]
  self.assertEqual(int(alternative==failed),0)
 def fake_retry(self,early="-"):
  def run(*_args,**_kwargs):
   attempt=F.capacity_attempt_identity(self.args,self.route,self.node,self.row,1,"gpt-5.6-luna/medium")
   status="done" if early=="capacity" else "open"
   note=",note=dead-capacity" if early=="capacity" else ""
   with self.jobs.open("a") as out:
    out.write(f"2026-07-16T00:00:01Z\t{status}\t/r\t/w\ts\t"
     f"route_id=r,route_node=test,attempt_id={attempt},model=gpt-5.6-luna,"
     "capacity_retry=1,prior_attempt_id=att-initial0001,cooled_model=gpt-5.6-sol,"
     f"selection_source=orchestrator-explicit{note}\n")
   return subprocess.CompletedProcess([],0,stdout=f"check=ok\nmodel=gpt-5.6-luna\nearly_death={early}\nattempt_id={attempt}\nduplicate_attempt=0\n",stderr="")
  return run
 def test_one_different_model_retry_succeeds_and_is_persisted(self):
  trace=[]
  with mock.patch.object(F,"allowed_capacity_settings",return_value=True),\
       mock.patch.object(F,"wrapper_command",return_value=["fake"]),\
       mock.patch.object(F.subprocess,"run",side_effect=self.fake_retry()):
   state,fields,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,trace)
  self.assertEqual(state,"success");self.assertEqual(fields["model"],"gpt-5.6-luna")
  rows=F.registry_rows(self.jobs,"r","test");self.assertEqual(len(rows),2)
  self.assertEqual(rows[-1]["capacity_retry"],"1");self.assertEqual(rows[-1]["cooled_model"],"gpt-5.6-sol")
 def test_second_capacity_descends_and_never_launches_third(self):
  with mock.patch.object(F,"allowed_capacity_settings",return_value=True),\
       mock.patch.object(F,"wrapper_command",return_value=["fake"]),\
       mock.patch.object(F.subprocess,"run",side_effect=self.fake_retry("capacity")) as launched:
   state,_,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
   self.assertEqual(state,"descend");self.assertEqual(launched.call_count,1)
  with mock.patch.object(F.subprocess,"run") as launched:
   state,_,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
   self.assertEqual(state,"descend");launched.assert_not_called()
 def test_same_or_unproved_model_is_rejected_before_launch(self):
  self.args.capacity_model="gpt-5.6-sol"
  with mock.patch.object(F,"wrapper_command") as command:
   state,_,reason=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
  self.assertEqual((state,reason),("descend","capacity-alternative-cooled"));command.assert_not_called()
 def test_watchdog_capacity_is_routed_into_failover(self):
  seed=subprocess.CompletedProcess([],0,stdout="check=ok\n",stderr="")
  observed=subprocess.CompletedProcess([],0,stdout="action=dead-capacity\nterminal_action=dead-capacity\nfailure_class=capacity\nmodel=gpt-5.6-sol\n",stderr="")
  self.args.action="start";self.args.progress_window_seconds=10
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,observed]):
   state,fields=F.watch_launched_attempt(self.args,self.route,self.node,"att-late-capacity",{"child_pid":"1","child_pid_start":"1"})
  self.assertEqual(state,"capacity");self.assertEqual(fields["failure_class"],"capacity")
 def test_terminal_row_wins_when_launch_heartbeat_loses_completion_race(self):
  seed=subprocess.CompletedProcess(
   [],65,stdout="check=failed\nreason=heartbeat-phase-regression\n",stderr="")
  self.jobs.write_text(
   "2026-07-20T00:00:00Z\tdone\t/r\t/w\ts\t"
   "route_id=r,route_node=test,attempt_id=att-completed-race,"
   "note=completed-marker,launch_outcome=reaped-before-publish\n")
  self.args.action="start";self.args.progress_window_seconds=10
  with mock.patch.object(F.subprocess,"run",return_value=seed) as run:
   state,fields=F.watch_launched_attempt(
    self.args,self.route,self.node,"att-completed-race",
    {"child_pid":"1","child_pid_start":"1"})
  self.assertEqual(state,"terminal")
  self.assertEqual(fields["terminal_action"],"registry-terminal")
  run.assert_called_once()
 def test_launch_heartbeat_failure_without_terminal_evidence_stays_closed(self):
  seed=subprocess.CompletedProcess(
   [],65,stdout="check=failed\nreason=heartbeat-phase-regression\n",stderr="")
  with mock.patch.object(F.subprocess,"run",return_value=seed):
   state,fields=F.watch_launched_attempt(
    self.args,self.route,self.node,"att-no-terminal-evidence",
    {"child_pid":"1","child_pid_start":"1"})
  self.assertEqual(state,"fail-closed")
  self.assertEqual(fields["reason"],"heartbeat-phase-regression")
 def test_capacity_cascade_from_config_and_failover_model_proved(self):
  # config cascade is model-granularity: deep exhausted -> next declared model.
  # Expectations derive from the cascade itself (efforts are user-tunable defaults).
  codex=F.capacity_cascade("codex")
  self.assertEqual(F.capacity_cascade_next("codex",codex[0][0]),codex[1])
  claude=F.capacity_cascade("claude")
  self.assertEqual(claude[0],("opus","xhigh"))
  self.assertEqual(F.capacity_cascade_next("claude","fable"),claude[0])
  self.assertEqual(F.capacity_cascade_next("claude","claude-fable-5"),claude[0])
  self.assertEqual(F.capacity_cascade_next("claude",claude[0][0]),claude[1])
  self.assertEqual(F.capacity_cascade_next("claude","claude-opus-4-8"),claude[1])
  self.assertIsNone(F.capacity_cascade_next("claude",claude[-1][0]))  # end of cascade
  # failover-only model (opus) is proved by cascade declaration, not model-map.
  models=[m for m,_ in claude]
  self.assertIn("opus",models)
  self.assertNotIn("fable",models)
  self.assertTrue(F.allowed_capacity_settings("claude",*claude[models.index("opus")]))
  self.assertFalse(F.allowed_capacity_settings("claude","fable","xhigh"))
  self.assertFalse(F.allowed_capacity_settings("claude","claude-fable-5","xhigh"))
  self.assertFalse(F.allowed_capacity_settings("claude","opus","not-a-real-effort"))
 def test_unset_capacity_model_derives_alternative_from_cascade(self):
  self.args.capacity_model=None  # no explicit alternative -> derive from config cascade
  with mock.patch.object(F,"wrapper_command",return_value=["fake"]),\
       mock.patch.object(F.subprocess,"run",side_effect=self.fake_retry()):
   state,fields,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
  self.assertEqual(state,"success");self.assertEqual(fields["model"],"gpt-5.6-luna")
 def test_legacy_fable_capacity_retry_launches_first_eligible_opus_candidate(self):
  self.args.capacity_model=None;self.args.capacity_effort=None
  self.row={**self.row,"child_harness":"claude"}
  self.failed={"attempt_id":"att-initial0001","model":"claude-fable-5"}
  self.jobs.write_text("2026-07-16T00:00:00Z\tdone\t/r\t/w\ts\t"
   "route_id=r,route_node=test,attempt_id=att-initial0001,model=claude-fable-5,"
   "child_harness=claude,note=dead-capacity\n")
  completed=subprocess.CompletedProcess([],0,stdout="check=ok\nmodel=opus\nearly_death=-\nduplicate_attempt=0\n",stderr="")
  with mock.patch.object(F,"wrapper_command",return_value=["fake"]) as command,\
       mock.patch.object(F.subprocess,"run",return_value=completed):
   state,fields,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
  self.assertEqual((state,fields["model"]),("success","opus"))
  self.assertEqual(command.call_args.args[6],("opus","xhigh"))
 def test_balanced_all_gated_stage_candidates_choose_maximum_headroom(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location("capacity",ROOT/"utilities/harness-capacity.py")
  capacity=importlib.util.module_from_spec(spec); spec.loader.exec_module(capacity)
  chosen,band,_,_=capacity.select(
   {"primary":["claude","codex","opencode"],"relief":[],"last_resort":[],"promote_relief_below":0},
   {"claude":"ok","codex":"ok","opencode":"ok"},
   {"claude":0,"codex":0,"opencode":0}, ["claude","codex","opencode"],
   {"claude":9,"codex":4,"opencode":1}, strategy="balanced")
  self.assertEqual((chosen,band),("claude","primary"))
if __name__=="__main__":unittest.main()
