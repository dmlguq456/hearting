#!/usr/bin/env python3
import importlib.util, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
S=importlib.util.spec_from_file_location("fallback",ROOT/"utilities/stage-dispatch-fallback.py")
F=importlib.util.module_from_spec(S);S.loader.exec_module(F)
import sys;sys.path.insert(0,str(ROOT/"utilities"))
from model_config import parse_config

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
  def watch(command,**kwargs):
   if "watchdog" not in command:return seed
   with self.jobs.open("a") as out:
    out.write("2026-09-13T00:00:00Z\tdone\t/r\t/w\ts\t"
     "route_id=r,route_node=test,attempt_id=att-late-capacity,note=dead-capacity,"
     "failure_class=capacity,launch_outcome=reaped-before-publish\n")
   return observed
  with mock.patch.object(F.subprocess,"run",side_effect=watch):
   state,fields=F.watch_launched_attempt(self.args,self.route,self.node,"att-late-capacity",{"child_pid":"1","child_pid_start":"1"})
  self.assertEqual(state,"capacity");self.assertEqual(fields["failure_class"],"capacity")
  # A watchdog's cached word without the exact settled row is no retry grant.
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,observed]):
   state,_=F.watch_launched_attempt(self.args,self.route,self.node,"att-uncommitted-capacity",{"child_pid":"1","child_pid_start":"1"})
  self.assertEqual(state,"fail-closed")
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
 @staticmethod
 def shipped_conf(harness):
  # The shipped adapter file itself, not the user's runtime copy.
  return parse_config(ROOT/f"adapters/{harness}/config/models.conf")
 def pin_conf(self,conf):
  patcher=mock.patch.object(F,"_adapter_models_conf",side_effect=lambda harness:conf)
  patcher.start();self.addCleanup(patcher.stop)
 def test_capacity_cascade_from_config_and_failover_model_proved(self):
  # config cascade is model-granularity: deep exhausted -> next declared model.
  # Expectations derive from the shipped cascade itself (efforts are user-tunable defaults).
  codex=F.capacity_cascade("codex")
  self.assertEqual(F.capacity_cascade_next("codex",codex[0][0]),codex[1])
  conf=self.shipped_conf("claude");self.pin_conf(conf)
  declared=[tuple(e.split(":",1)) for e in conf["CFG_TIER_DEEP_FAILOVER_CASCADE"].split()]
  restricted=conf["CFG_MAIN_SESSION_ONLY_MODELS"].split()
  claude=F.capacity_cascade("claude")
  self.assertEqual(claude,[d for d in declared if d[0] not in restricted])
  self.assertGreaterEqual(len(claude),2)
  # 2026-09-09 shipped default == user runtime: fable is main-session-only, so the
  # cascade head is the deep tier model (opus) and the walk is opus->sonnet.
  self.assertEqual(claude[0][0],conf["CFG_TIER_DEEP_MODEL"]);self.assertEqual(restricted,["fable"])
  self.assertEqual([m for m,_ in claude],["opus","sonnet"])
  for i,(model,paired) in enumerate(claude):
   nxt=claude[i+1] if i+1<len(claude) else None
   self.assertEqual(F.capacity_cascade_next("claude",model),nxt)
   self.assertEqual(F.capacity_cascade_next("claude",f"claude-{model}-5"),nxt)  # concrete id form
   # every declared cascade member is proved by declaration with its own paired effort
   self.assertTrue(F.allowed_capacity_settings("claude",model,paired))
   self.assertFalse(F.allowed_capacity_settings("claude",model,"not-a-real-effort"))
 def test_main_only_model_never_enters_cascade_or_capacity_settings(self):
  # A config that declares a main-only model keeps it out of the cascade and out
  # of every capacity setting. The shipped default declares fable too, so the
  # second block pins another alias: the filter reads the config, not a name.
  conf={**self.shipped_conf("claude"),"CFG_MAIN_SESSION_ONLY_MODELS":"fable"};self.pin_conf(conf)
  claude=F.capacity_cascade("claude");models=[m for m,_ in claude]
  self.assertNotIn("fable",models);self.assertEqual(models,["opus","sonnet"])
  self.assertEqual(F.capacity_cascade_next("claude","fable"),claude[0])  # legacy row: first eligible
  self.assertEqual(F.capacity_cascade_next("claude","claude-fable-5"),claude[0])
  self.assertFalse(F.allowed_capacity_settings("claude","fable","xhigh"))
  self.assertFalse(F.allowed_capacity_settings("claude","claude-fable-5","xhigh"))
  self.assertTrue(F.allowed_capacity_settings("claude",*claude[0]))
  self.pin_conf({**self.shipped_conf("claude"),"CFG_MAIN_SESSION_ONLY_MODELS":"fable opus"})
  narrowed=F.capacity_cascade("claude")
  self.assertEqual([m for m,_ in narrowed],["sonnet"])
  self.assertFalse(F.allowed_capacity_settings("claude","opus","xhigh"))
 def test_unset_capacity_model_derives_alternative_from_cascade(self):
  self.args.capacity_model=None  # no explicit alternative -> derive from config cascade
  cascade=F.capacity_cascade("codex")  # the exhausted model is the cascade head, whatever it is
  self.failed={**self.failed,"model":cascade[0][0]}
  with mock.patch.object(F,"wrapper_command",return_value=["fake"]),\
       mock.patch.object(F.subprocess,"run",side_effect=self.fake_retry()):
   state,fields,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
  self.assertEqual(state,"success");self.assertEqual(fields["model"],cascade[1][0])
 def capacity_retry_from(self,failed_model,expected):
  self.args.capacity_model=None;self.args.capacity_effort=None
  self.row={**self.row,"child_harness":"claude"}
  self.failed={"attempt_id":"att-initial0001","model":failed_model}
  self.jobs.write_text("2026-07-16T00:00:00Z\tdone\t/r\t/w\ts\t"
   f"route_id=r,route_node=test,attempt_id=att-initial0001,model={failed_model},"
   "child_harness=claude,note=dead-capacity\n")
  completed=subprocess.CompletedProcess([],0,stdout=f"check=ok\nmodel={expected[0]}\nearly_death=-\nduplicate_attempt=0\n",stderr="")
  with mock.patch.object(F,"wrapper_command",return_value=["fake"]) as command,\
       mock.patch.object(F.subprocess,"run",return_value=completed):
   state,fields,_=F.capacity_retry(self.args,self.route,self.node,self.row,1,self.failed,[])
  self.assertEqual((state,fields["model"]),("success",expected[0]))
  self.assertEqual(command.call_args.args[6],expected)
 def test_deep_tier_capacity_death_retries_on_the_next_cascade_model(self):
  # shipped default: the deep tier model heads the cascade, so its capacity death
  # walks one model down (opus -> sonnet), never up into a main-only model.
  conf=self.shipped_conf("claude");self.pin_conf(conf)
  cascade=F.capacity_cascade("claude")
  self.assertEqual(cascade[0][0],conf["CFG_TIER_DEEP_MODEL"])
  self.capacity_retry_from(f"claude-{cascade[0][0]}-5",cascade[1])
 def test_legacy_main_only_fable_retry_launches_first_eligible_candidate(self):
  # migration-only: a row recorded under a main-only policy resumes at the first
  # eligible cascade member without admitting fable into the cascade.
  self.pin_conf({**self.shipped_conf("claude"),"CFG_MAIN_SESSION_ONLY_MODELS":"fable"})
  cascade=F.capacity_cascade("claude");self.assertNotIn("fable",[m for m,_ in cascade])
  self.capacity_retry_from("claude-fable-5",cascade[0])
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
 def test_top_profile_never_fails_over_and_astra_never_enters_capacity_settings(self):
  # SD-59 keeps the top exception profile out of the cascade in both directions.
  self.args.capacity_model=None;self.args.capacity_reasoning=None
  node={**self.node,"model_profile":"top"};attempts=[]
  with mock.patch.object(F,"wrapper_command",return_value=["fake"]) as launched:
   state,fields,reason=F.capacity_retry(self.args,self.route,node,self.row,1,self.failed,attempts)
  self.assertEqual((state,fields,reason),("descend",{},"capacity-alternative-top-profile"))
  self.assertTrue(attempts and attempts[0].endswith("capacity-alternative-top-profile"));launched.assert_not_called()
  # the codex key (parity, 2026-09-10) keeps a hyphenated top model out of every capacity setting
  conf=self.shipped_conf("codex");self.assertEqual(conf["CFG_MAIN_SESSION_ONLY_MODELS"].split(),["gpt-6-astra"])
  self.assertNotIn("gpt-6-astra",[m for m,_ in F.capacity_cascade("codex")])
  self.assertFalse(F.allowed_capacity_settings("codex","gpt-6-astra","xhigh"))
  self.assertEqual(F.capacity_cascade_next("codex","gpt-6-astra"),F.capacity_cascade("codex")[0])
if __name__=="__main__":unittest.main()
