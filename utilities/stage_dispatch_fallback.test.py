#!/usr/bin/env python3
import importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock
os.environ.setdefault("HEARTING_CAPACITY_PROBE_CLI", os.devnull)  # never ask a real Claude login

ROOT=Path(__file__).resolve().parents[1]

_STATE_TMP = None
_STATE_OLD = None


def setUpModule():
    # The hard usage reader now consults Fleet's shared gauge cache; keep this
    # module off the developer's live ~/.local/state store.
    global _STATE_TMP, _STATE_OLD
    _STATE_TMP = tempfile.TemporaryDirectory()
    _STATE_OLD = os.environ.get("FLEET_USAGE_STATE_DIR")
    os.environ["FLEET_USAGE_STATE_DIR"] = _STATE_TMP.name


def tearDownModule():
    if _STATE_OLD is None:
        os.environ.pop("FLEET_USAGE_STATE_DIR", None)
    else:
        os.environ["FLEET_USAGE_STATE_DIR"] = _STATE_OLD
    _STATE_TMP.cleanup()


S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
F_SPEC=importlib.util.spec_from_file_location("fallback",ROOT/"utilities/stage-dispatch-fallback.py"); F=importlib.util.module_from_spec(F_SPEC); F_SPEC.loader.exec_module(F)

import contextlib
import review_source_subject as SUBJECT


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
 def test_spawn_wait_default_does_not_extend_launch_receipt_observation(self):
  self.assertEqual(F.DIRECT_TIMEOUT_DEFAULT,300.0)
  args=SimpleNamespace(direct_timeout=300.0,progress_window_seconds=300.0,
                       watchdog_max_windows=12)
  self.assertEqual(F.launch_confirm_deadline_seconds(args),45.0)
  args.direct_timeout=10.0
  self.assertEqual(F.launch_confirm_deadline_seconds(args),10.0)

 def test_wrapper_stderr_cannot_replace_typed_stdout_receipt(self):
  result=SimpleNamespace(stdout='check=failed\nreason=unsafe-git-operation\ndetail=cherry-pick\n',
                         stderr='check=ok\nreason=launched\nstarted=1\n')
  fields=F.wrapper_fields(result)
  self.assertEqual(fields['check'],'failed')
  self.assertEqual(fields['reason'],'unsafe-git-operation')
  self.assertNotIn('started',fields)

 def degradation_rows(self,path):
  route=json.loads(path.read_text())
  ledger=self.jobs.parent/"degradations"/f"{route['route_id']}.jsonl"
  return [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
 def seed_terminal_row(self,route,node,attempt_id,*,note,failure_class):
  metadata=(f"attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
            f"route_id={route['route_id']},route_node={node['id']},attempt_id={attempt_id},"
            f"worker_type=stage,launch_claimed=1,launch_fence=registry-v1,launch_started=1,"
            f"note={note},failure_class={failure_class}")
  with self.jobs.open("a",encoding="utf-8") as handle:
   handle.write(f"now\tdone\t{self.repo}\t{self.repo}\tfixture\t{metadata}\n")

 def test_interrupted_direct_attempt_has_receipt_and_ledger(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
          "candidates":[self.tuple("codex","supported")]},
         {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
   result=SimpleNamespace(returncode=130,stdout="worker_failure=interrupted\n",stderr="")
   out=io.StringIO()
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",return_value=result), contextlib.redirect_stdout(out):
    code,_=self.run_inline(path)
   receipt=self.cli_verdict(out.getvalue())
   rows=json.loads(receipt["candidate_outcomes"])
   self.assertEqual((code,receipt["reason"],receipt["candidate_outcomes_omitted"]),(130,"interrupted","0"))
   self.assertEqual([(r["reason"],r["attempted"]) for r in rows],[("interrupted",True)])
   self.assertEqual(receipt["last_direct_failure_attempt_id"],rows[0]["attempt_id"])
   self.assertEqual(self.degradation_rows(path)[-1]["candidate_outcomes"],rows)
   self.assertEqual(self.degradation_rows(path)[-1]["last_direct_failure"],"interrupted:exit-130")

 def test_direct_timeout_with_registered_attempt_remains_unknown(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
          "candidates":[self.tuple("codex","supported")]},
         {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
   out=io.StringIO()
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",side_effect=subprocess.TimeoutExpired(["fixture"],1)), \
        mock.patch.object(F,"settle_direct_timeout_attempt",return_value="unknown"), contextlib.redirect_stdout(out):
    code,_=self.run_inline(path)
   receipt=self.cli_verdict(out.getvalue())
   rows=json.loads(receipt["candidate_outcomes"])
   self.assertEqual((code,receipt["reason"],receipt["child_spawned"]),
                    (76,"direct-launch-outcome-unknown","unknown"))
   self.assertEqual(receipt["last_direct_failure_attempt_id"],rows[0]["attempt_id"])
   self.assertEqual(receipt["last_direct_failure_reason"],"direct-timeout")
   self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
   saved=self.degradation_rows(path)[-1]
   self.assertEqual(saved["candidate_outcomes"],rows)
   self.assertEqual(saved["last_direct_failure"],"direct-timeout:exit-timeout")

 def test_timeout_settlement_closes_only_the_exact_proven_unstarted_row(self):
  with self.dispatch_env():
   path,route,node=self.standard_route()
   attempt="att-timeout-proven-never-started"
   metadata=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
             "execution_surface=registered-headless,registered_worker=1,"
             "fallback_hop=same-harness-headless,"
             f"route_id={route['route_id']},"
             f"route_hash={route['route_hash']},route_node={node['id']},"
             f"attempt_id={attempt},launch_claimed=0")
   self.jobs.write_text(
    f"now\topen\t{self.repo}\t{self.repo}\tstage\t{metadata}\n",encoding="utf-8")
   state=F.settle_direct_timeout_attempt(self.jobs,route,node,attempt)
   self.assertEqual(state,"unstarted")
   fields=self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")
   closed=F.parse_registry_metadata(fields[5])
   self.assertEqual(fields[1],"done")
   self.assertEqual(closed["note"],"never-launched")
   self.assertEqual(closed["launch_outcome"],"never-launched")
   self.assertEqual(closed["reconcile_reason"],F.DIRECT_TIMEOUT_UNSTARTED_REASON)
   self.assertEqual(F.retry_proven_unstarted_identity(
    self.jobs,route["route_id"],node["id"],attempt),F.retry_attempt_identity(attempt))

   protected=(
    {"launch_claimed":"1"},
    {"launch_claimed":"0","pid":"123"},
    {"launch_claimed":"0","launch_started":"1"},
    {},
   )
   for index,extra in enumerate(protected,1):
    with self.subTest(extra=extra):
     candidate=f"att-timeout-protected-{index}"
     row_meta=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
               "execution_surface=registered-headless,registered_worker=1,"
               "fallback_hop=same-harness-headless,"
               f"route_id={route['route_id']},"
               f"route_hash={route['route_hash']},route_node={node['id']},"
               f"attempt_id={candidate},"+
               ",".join(f"{key}={value}" for key,value in extra.items()))
     self.jobs.write_text(
      f"now\topen\t{self.repo}\t{self.repo}\tstage\t{row_meta}\n",encoding="utf-8")
     self.assertEqual(F.settle_direct_timeout_attempt(
      self.jobs,route,node,candidate),"unknown")
     self.assertIn("\topen\t",self.jobs.read_text(encoding="utf-8"))

 def test_terminal_conflict_never_selects_a_no_start_successor(self):
  import base64
  with self.dispatch_env():
   _path,route,node=self.standard_route()
   attempt="att-conflicting-no-start"
   for reviewed in (False,True):
    history={"late-result":{"note":"completed-marker","failure_class":"pass"}}
    if reviewed: history["late-result"]["review_sha256"]="f"*64
    encoded=base64.b64encode(json.dumps(history).encode()).decode()
    metadata=(f"attempt_id={attempt},route_id={route['route_id']},route_node={node['id']},"
              "launch_claimed=0,launch_outcome=never-launched,note=dead-capacity,"
              f"failure_class=capacity,terminal_conflict=1,terminal_conflicts_b64={encoded}")
    self.jobs.write_text(f"now\tdone\t{self.repo}\t{self.repo}\tstage\t{metadata}\n")
    before=self.jobs.read_bytes()
    self.assertEqual(F.retry_proven_unstarted_identity(
     self.jobs,route["route_id"],node["id"],attempt),attempt)
    self.assertEqual(self.jobs.read_bytes(),before)

 def test_watchdog_fallback_and_fail_closed_preserve_current_attempt(self):
  real_load=F.load_node
  def load_single_node(*args):
   route,node=real_load(*args)
   return route,{**node,"parallel_group":None,"replica_group":None}
  for state,expected in (("fallback",79),("fail-closed",76)):
   with self.subTest(state=state), self.dispatch_env():
    path=self.route(same_status="supported")
    hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
           "candidates":[self.tuple("codex","supported")]},
          {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
    result=SimpleNamespace(returncode=0,stdout="check=ok\nworker_failure=-\n",stderr="")
    patches=(mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)),
             mock.patch.object(F,"load_node",side_effect=load_single_node),
             mock.patch.object(F,"run_wrapper",return_value=result),
             mock.patch.object(F,"watch_launched_attempt",return_value=(state,{"action":"registry-terminal","note":"dead-no-progress"})))
    code,out,err=self.start(path,patches=patches)
    self.assertEqual(code,expected,out+err)
    receipt=self.cli_verdict(out)
    rows=json.loads(receipt["candidate_outcomes"])
    self.assertEqual(len(rows),1)
    self.assertEqual(rows[0]["attempt_id"],receipt["last_direct_failure_attempt_id"])
    self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
    self.assertEqual(receipt["last_direct_failure_reason"],rows[0]["reason"])
    self.assertEqual(self.degradation_rows(path)[-1]["candidate_outcomes"],rows)
    self.assertEqual(self.degradation_rows(path)[-1]["last_direct_failure"],
                     f"{rows[0]['reason']}:exit-0")

 def test_capacity_retry_outcomes_from_direct_and_pending_callers(self):
  for pending in (False,True):
   for outcome in ("success","failure","interrupted","timeout","unknown"):
    with self.subTest(pending=pending,outcome=outcome), self.dispatch_env():
     path=self.route(same_status="supported")
     row=self.tuple("codex","supported")
     hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row]},
           {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
     capacity=SimpleNamespace(returncode=0,stdout="check=ok\nearly_death=capacity\nmodel=old\n",stderr="")
     retry={"success":SimpleNamespace(returncode=0,stdout="check=ok\nmodel=new\n",stderr=""),
            "failure":SimpleNamespace(returncode=75,stdout="check=failed\nreason=retry-refused\ndetail=fixture\n",stderr=""),
            "interrupted":SimpleNamespace(returncode=130,stdout="worker_failure=interrupted\n",stderr=""),
            "timeout":subprocess.TimeoutExpired(["fixture"],1),
            "unknown":subprocess.TimeoutExpired(["fixture"],1)}[outcome]
     calls=[retry] if pending else [capacity,retry]
     prior={"capacity":[{"model":"old",**dict(zip(
       ("parent_harness","parent_transport","parent_sandbox","child_harness","launch_authority"),
       F.tuple_key(row).split("/")))}] if pending else [],"retries":[],"cooled":set()}
     prior_ledger_count=len(self.degradation_rows(path))
     out=io.StringIO()
     with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
          mock.patch.object(F,"capacity_context",return_value=prior), \
          mock.patch.object(F,"allowed_capacity_settings",return_value=True), \
          mock.patch.object(F,"capacity_attempt_identity",return_value="att-retry"), \
          mock.patch.object(F,"registry_has_attempt",side_effect=lambda _jobs,aid: outcome=="unknown" and aid=="att-retry"), \
          mock.patch.object(F,"run_wrapper",side_effect=calls), contextlib.redirect_stdout(out):
      code,_=self.run_inline(path,"--capacity-model","new","--capacity-reasoning","high")
     receipt=self.cli_verdict(out.getvalue())
     rows=json.loads(receipt["candidate_outcomes"])
     self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
     self.assertEqual(rows[-1]["attempt_id"],"att-retry")
     self.assertEqual(rows[-1]["reason"],{"success":"launched","failure":"retry-refused",
       "interrupted":"interrupted","timeout":"capacity-launch-timeout",
       "unknown":"capacity-launch-outcome-unknown"}[outcome])
     if not pending:
      self.assertEqual(rows[0]["reason"],"capacity")
      self.assertNotEqual(rows[0]["attempt_id"],rows[-1]["attempt_id"])
     if outcome=="success":
      self.assertEqual(code,0)
      self.assertEqual(len(self.degradation_rows(path)),prior_ledger_count)
      if pending:
       self.assertNotIn("last_direct_failure_reason",receipt)
      else:
       self.assertEqual(receipt["last_direct_failure_reason"],"capacity")
     else:
      self.assertEqual(code,{"failure":79,"interrupted":130,"timeout":79,"unknown":76}[outcome])
      self.assertEqual(receipt["last_direct_failure_attempt_id"],"att-retry")
      self.assertEqual(receipt["last_direct_failure_reason"],rows[-1]["reason"])
      saved=self.degradation_rows(path)[-1]
      self.assertEqual(saved["candidate_outcomes"],rows)
      self.assertEqual(saved["candidate_outcomes_omitted"],0)
      self.assertEqual(saved["last_direct_failure"],f"{rows[-1]['reason']}:exit-{rows[-1]['exit']}")

 def test_capacity_retry_typed_stops_keep_original_refusal(self):
  for pending in (False,True):
   for reason,code in (("unsafe-git-operation",65),
                       ("completion-evidence-superseded",65),
                       ("prior-attempt-still-live",78)):
    with self.subTest(pending=pending,reason=reason), self.dispatch_env():
     path=self.route(same_status="supported")
     row=self.tuple("codex","supported")
     key=F.tuple_key(row)
     hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row]},
           {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
     capacity=SimpleNamespace(returncode=0,stdout="check=ok\nearly_death=capacity\nmodel=old\n",stderr="")
     raw_stderr=("check=ok\nreason=forged-stderr-reason\nattempt_id=att-forged\n"
                 'candidate_outcomes=[{"detail":"PROMPT_FIXTURE_SENTINEL"}]\n')
     refusal=SimpleNamespace(returncode=code,stdout=f"check=failed\nreason={reason}\ndetail=cherry-pick\nnext_action=preserve-route-state\n",stderr=raw_stderr)
     prior={"capacity":[{"model":"old",**dict(zip(
       ("parent_harness","parent_transport","parent_sandbox","child_harness","launch_authority"),
       key.split("/")))}] if pending else [],"retries":[],"cooled":set()}
     out=io.StringIO(); err=io.StringIO()
     with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
          mock.patch.object(F,"capacity_context",return_value=prior), \
          mock.patch.object(F,"allowed_capacity_settings",return_value=True), \
          mock.patch.object(F,"capacity_attempt_identity",return_value="att-retry-stop"), \
          mock.patch.object(F,"run_wrapper",side_effect=[refusal] if pending else [capacity,refusal]) as wrapper, \
          contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      actual,_=self.run_inline(path,"--capacity-model","new","--capacity-reasoning","high")
     receipt=self.cli_verdict(out.getvalue())
     rows=json.loads(receipt["candidate_outcomes"])
     self.assertEqual(actual,code)
     self.assertEqual(err.getvalue(),raw_stderr)
     self.assertNotIn("reason=forged-stderr-reason",out.getvalue())
     self.assertEqual(wrapper.call_count,1 if pending else 2)
     self.assertEqual(receipt["reason"],reason)
     self.assertEqual(receipt["detail"],"cherry-pick")
     self.assertEqual(receipt["child_spawned"],"0")
     self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
     self.assertNotIn("selected_hop=inline",out.getvalue())
     if code==65:self.assertEqual(receipt["next_action"],"preserve-route-state")
     self.assertEqual((rows[-1]["attempted"],rows[-1]["attempt_id"],rows[-1]["reason"]),
                      (True,"att-retry-stop",reason))
     self.assertEqual(receipt["last_direct_failure_attempt_id"],"att-retry-stop")
     self.assertEqual(receipt["last_direct_failure_reason"],reason)
     saved=self.degradation_rows(path)[-1]
     self.assertEqual((saved["kind"],saved["reason"],saved["candidate_outcomes"],
                       saved["candidate_outcomes_omitted"],saved["last_direct_failure"]),
                      ("leg-failure",reason,rows,0,f"{reason}:exit-{code}"))
     self.assertNotIn("PROMPT_FIXTURE_SENTINEL",json.dumps((rows,saved)))

 def test_capacity_retry_launch_errors_are_unattempted(self):
  for pending in (False,True):
   for error in (OSError,ValueError):
    with self.subTest(pending=pending,error=error.__name__), self.dispatch_env():
     path=self.route(same_status="supported")
     row=self.tuple("codex","supported")
     key=F.tuple_key(row)
     hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row]},
           {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
     capacity=SimpleNamespace(returncode=0,stdout="check=ok\nearly_death=capacity\nmodel=old\n",stderr="")
     prior={"capacity":[{"model":"old",**dict(zip(
       ("parent_harness","parent_transport","parent_sandbox","child_harness","launch_authority"),
       key.split("/")))}] if pending else [],"retries":[],"cooled":set()}
     out=io.StringIO()
     with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
          mock.patch.object(F,"capacity_context",return_value=prior), \
          mock.patch.object(F,"allowed_capacity_settings",return_value=True), \
          mock.patch.object(F,"capacity_attempt_identity",return_value="att-retry-error"), \
          mock.patch.object(F,"run_wrapper",side_effect=[error("fixture")] if pending else [capacity,error("fixture")]) as wrapper, \
          contextlib.redirect_stdout(out):
      code,_=self.run_inline(path,"--capacity-model","new","--capacity-reasoning","high")
     receipt=self.cli_verdict(out.getvalue())
     rows=json.loads(receipt["candidate_outcomes"])
     self.assertEqual(code,79)
     self.assertEqual(wrapper.call_count,1 if pending else 2)
     self.assertEqual((rows[-1]["reason"],rows[-1]["detail"],rows[-1]["attempted"],rows[-1]["attempt_id"]),
                      ("capacity-launch-error",error.__name__,False,""))
     self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
     self.assertEqual(receipt.get("last_direct_failure_reason"),None if pending else "capacity")
     saved=self.degradation_rows(path)[-1]
     self.assertEqual(saved["candidate_outcomes"],rows)
     self.assertEqual(saved["candidate_outcomes_omitted"],0)
     self.assertEqual(saved.get("last_direct_failure"),None if pending else "capacity:exit-0")

 def test_capacity_retry_cleanup_incomplete_keeps_bounded_attempt_history(self):
  for pending in (False,True):
   with self.subTest(pending=pending),self.dispatch_env():
    path=self.route(same_status="supported")
    route=json.loads(path.read_text())
    node=next(n for n in route["nodes"] if n["id"]=="plan")
    row=self.tuple("codex","supported")
    key=F.tuple_key(row)
    hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row]},
          {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
    capacity=SimpleNamespace(returncode=0,
      stdout="check=ok\nearly_death=capacity\nmodel=old\n",stderr="")
    raw_stderr=("check=ok\nreason=forged-capacity-reason\nattempt_id=att-forged\n"
                'candidate_outcomes=[{"detail":"PROMPT_FIXTURE_SENTINEL"}]\n')
    attempt_id=f"att-capacity-cleanup-{'pending' if pending else 'direct'}"
    self.seed_terminal_row(route,node,attempt_id,note="dead-worker-fail",failure_class="fail")
    cleanup_result=SimpleNamespace(returncode=0,
      stdout=f"check=ok\nstatus=start\nattempt_id={attempt_id}\n",
      stderr=raw_stderr,cleanup_incomplete=True)
    prior={"capacity":[{"model":"old",**dict(zip(
      ("parent_harness","parent_transport","parent_sandbox","child_harness","launch_authority"),
      key.split("/")))}] if pending else [],"retries":[],"cooled":set()}
    calls=[cleanup_result] if pending else [capacity,cleanup_result]
    out=io.StringIO(); err=io.StringIO()
    with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
         mock.patch.object(F,"capacity_context",return_value=prior), \
         mock.patch.object(F,"allowed_capacity_settings",return_value=True), \
         mock.patch.object(F,"capacity_attempt_identity",return_value=attempt_id), \
         mock.patch.object(F,"run_wrapper",side_effect=calls), \
         contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
     code,_=self.run_inline(path,"--capacity-model","new","--capacity-reasoning","high")
    receipt=self.cli_verdict(out.getvalue())
    candidates=json.loads(receipt["candidate_outcomes"])
    self.assertEqual(code,76)
    self.assertEqual((receipt["reason"],receipt["cleanup"],receipt["terminal_verdict"],
                      receipt["attempt_id"],receipt["child_spawned"]),
                     ("foreground-output-incomplete","incomplete","FAIL",attempt_id,"1"))
    self.assertEqual(candidates[-1]["attempt_id"],attempt_id)
    self.assertEqual(candidates[-1]["reason"],"foreground-output-incomplete")
    self.assertEqual(receipt["last_direct_failure_attempt_id"],attempt_id)
    self.assertEqual(err.getvalue(),raw_stderr)
    self.assertNotIn("reason=forged-capacity-reason\n",out.getvalue())
    ledger=self.degradation_rows(path)[-1]
    self.assertEqual(ledger["candidate_outcomes"],candidates)
    self.assertEqual(ledger["last_direct_failure"],"foreground-output-incomplete:exit-0")
    self.assertNotIn("PROMPT_FIXTURE_SENTINEL",json.dumps((candidates,ledger)))

 def test_capacity_retry_command_error_and_generic_exit_65_descend(self):
  for pending in (False,True):
   for outcome in ("command-error","generic-65"):
    with self.subTest(pending=pending,outcome=outcome),self.dispatch_env():
     path=self.route(same_status="supported")
     row=self.tuple("codex","supported")
     key=F.tuple_key(row)
     hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row]},
           {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
     prior={"capacity":[{"model":"old",**dict(zip(
       ("parent_harness","parent_transport","parent_sandbox","child_harness","launch_authority"),
       key.split("/")))}] if pending else [],"retries":[],"cooled":set()}
     capacity=SimpleNamespace(returncode=0,stdout="check=ok\nearly_death=capacity\nmodel=old\n",stderr="")
     generic=SimpleNamespace(returncode=65,stdout="check=failed\nreason=retry-refused\ndetail=ordinary\n",stderr="")
     real_command=F.wrapper_command
     def build(*args):
      if len(args)>6 and args[6]:raise ValueError("fixture")
      return real_command(*args)
     out=io.StringIO()
     with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
          mock.patch.object(F,"capacity_context",return_value=prior), \
          mock.patch.object(F,"allowed_capacity_settings",return_value=True), \
          mock.patch.object(F,"capacity_attempt_identity",return_value="att-retry-generic"), \
          mock.patch.object(F,"wrapper_command",side_effect=build if outcome=="command-error" else real_command), \
          mock.patch.object(F,"run_wrapper",side_effect=[generic] if pending else [capacity,generic]) as wrapper, \
          contextlib.redirect_stdout(out):
      code,_=self.run_inline(path,"--capacity-model","new","--capacity-reasoning","high")
     receipt=self.cli_verdict(out.getvalue())
     rows=json.loads(receipt["candidate_outcomes"])
     self.assertEqual(code,79)
     self.assertEqual(receipt["selected_hop"],"inline")
     self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
     self.assertEqual(wrapper.call_count,(0 if pending else 1) if outcome=="command-error" else (1 if pending else 2))
     self.assertEqual(rows[-1]["reason"],"capacity-launch-error" if outcome=="command-error" else "retry-refused")
     self.assertEqual(rows[-1]["attempted"],outcome=="generic-65")
     self.assertEqual(self.degradation_rows(path)[-1]["candidate_outcomes"],rows)

 def test_direct_typed_stops_report_last_failed_attempt(self):
  for reason,code in (("unsafe-git-operation",65),("prior-attempt-still-live",78)):
   with self.subTest(reason=reason),self.dispatch_env():
    path=self.route(same_status="supported")
    hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
           "candidates":[self.tuple("codex","supported")]},
          {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
    raw_stderr=("check=ok\nreason=forged-direct-reason\nattempt_id=att-forged\n"
                'candidate_outcomes=[{"detail":"PROMPT_FIXTURE_SENTINEL"}]\n')
    result=SimpleNamespace(returncode=code,stdout=f"check=failed\nreason={reason}\ndetail=cherry-pick\n",stderr=raw_stderr)
    out=io.StringIO(); err=io.StringIO()
    with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
         mock.patch.object(F,"run_wrapper",return_value=result) as wrapper, \
         contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
     actual,_=self.run_inline(path)
    receipt=self.cli_verdict(out.getvalue())
    rows=json.loads(receipt["candidate_outcomes"])
    self.assertEqual((actual,wrapper.call_count,receipt["reason"],receipt["child_spawned"]),
                     (code,1,reason,"0"))
    self.assertEqual(err.getvalue(),raw_stderr)
    self.assertNotIn("reason=forged-direct-reason",out.getvalue())
    self.assertEqual(len(rows),1)
    self.assertEqual(receipt["last_direct_failure_attempt_id"],rows[0]["attempt_id"])
    self.assertEqual(receipt["last_direct_failure_reason"],reason)
    self.assertEqual(receipt["last_direct_failure_detail"],"cherry-pick")
    self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
    saved=self.degradation_rows(path)[-1]
    self.assertEqual(saved["candidate_outcomes"],rows)
    self.assertEqual(saved["last_direct_failure"],f"{reason}:exit-{code}")
    self.assertNotIn("PROMPT_FIXTURE_SENTINEL",json.dumps((rows,saved)))

 def test_capacity_retry_watchdog_decisions_keep_retry_identity(self):
  real_load=F.load_node
  def load_single_node(*args):
   route,node=real_load(*args)
   return route,{**node,"parallel_group":None,"replica_group":None}
  for state,expected in (("fallback",79),("fail-closed",76)):
   with self.subTest(state=state), self.dispatch_env():
    path=self.route(same_status="supported")
    hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
           "candidates":[self.tuple("codex","supported")]},
          {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
    initial=SimpleNamespace(returncode=0,stdout="check=ok\nearly_death=capacity\nmodel=old\n",stderr="")
    retry=SimpleNamespace(returncode=0,stdout="check=ok\nmodel=new\n",stderr="")
    patches=(mock.patch.object(F,"load_node",side_effect=load_single_node),
             mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)),
             mock.patch.object(F,"capacity_context",return_value={"capacity":[],"retries":[],"cooled":set()}),
             mock.patch.object(F,"allowed_capacity_settings",return_value=True),
             mock.patch.object(F,"capacity_attempt_identity",return_value="att-retry-watch"),
             mock.patch.object(F,"run_wrapper",side_effect=[initial,retry]),
             mock.patch.object(F,"watch_launched_attempt",return_value=(state,{"action":"registry-terminal","note":"dead-no-progress"})))
    code,out,err=self.start(path,"--capacity-model","new","--capacity-reasoning","high",patches=patches)
    self.assertEqual(code,expected,out+err)
    receipt=self.cli_verdict(out)
    rows=json.loads(receipt["candidate_outcomes"])
    self.assertEqual([r["reason"] for r in rows],
                     ["capacity","dead-no-progress" if state=="fallback" else "capacity-watchdog-fail-closed"])
    self.assertNotEqual(rows[0]["attempt_id"],rows[1]["attempt_id"])
    self.assertEqual(rows[1]["attempt_id"],"att-retry-watch")
    self.assertEqual(receipt["last_direct_failure_attempt_id"],"att-retry-watch")
    self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
    saved=self.degradation_rows(path)[-1]
    self.assertEqual(saved["candidate_outcomes"],rows)
    self.assertEqual(saved["last_direct_failure"],f"{rows[-1]['reason']}:exit-0")

 def test_pending_capacity_reuse_and_unlaunched_alternative_have_outcomes(self):
  for state in ("open","terminal","unpaired"):
   with self.subTest(state=state), self.dispatch_env():
    path=self.route(same_status="supported")
    row=self.tuple("codex","supported")
    key=F.tuple_key(row)
    pending={"model":"old",**dict(zip(
      ("parent_harness","parent_transport","parent_sandbox","child_harness","launch_authority"),
      key.split("/")))}
    retry={"attempt_id":"att-prior-retry","_status":"open","note":""} if state=="open" else {
      "attempt_id":"att-prior-retry","_status":"done","note":"dead-no-progress","worker_exit":"75"}
    context={"capacity":[pending],"retries":[] if state=="unpaired" else [retry],"cooled":set()}
    hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row]},
          {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
    out=io.StringIO()
    with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
         mock.patch.object(F,"capacity_context",return_value=context), \
         mock.patch.object(F,"capacity_cascade_next",return_value=None), \
         mock.patch.object(F,"run_wrapper") as wrapper, contextlib.redirect_stdout(out):
     code,_=self.run_inline(path)
    wrapper.assert_not_called()
    receipt=self.cli_verdict(out.getvalue())
    rows=json.loads(receipt["candidate_outcomes"])
    self.assertEqual(len(rows),1)
    self.assertEqual(receipt["candidate_outcomes_omitted"],"0")
    if state=="open":
     self.assertEqual(code,0)
     self.assertEqual(receipt["attempt_id"],"att-prior-retry")
     self.assertEqual((rows[0]["reason"],rows[0]["attempted"],rows[0]["attempt_id"]),
                      ("capacity-retry-existing",True,"att-prior-retry"))
     self.assertEqual(self.degradation_rows(path),[])
    else:
     self.assertEqual(code,79)
     expected="dead-no-progress" if state=="terminal" else "capacity-alternative-unpaired"
     self.assertEqual(rows[0]["reason"],expected)
     self.assertEqual(rows[0]["attempted"],state=="terminal")
     if state=="terminal":
      self.assertEqual(receipt["last_direct_failure_attempt_id"],"att-prior-retry")
     else:
      self.assertNotIn("last_direct_failure_reason",receipt)
     saved=self.degradation_rows(path)[-1]
     self.assertEqual(saved["candidate_outcomes"],rows)
     self.assertEqual(saved["candidate_outcomes_omitted"],0)

 def test_multiple_process_failures_then_parent_skip_preserve_last_real_cause(self):
  with self.dispatch_env(AGENT_DISPATCH_CURRENT_HARNESS="codex",
                         AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                         AGENT_DISPATCH_CURRENT_SANDBOX="workspace-write"):
   path=self.route(same_status="supported")
   rows=[self.tuple("codex","supported"),self.tuple("claude","supported"),
         {**self.tuple("opencode","supported"),"parent_harness":"claude"}]
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":rows[:1]},
         {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":rows[1:]},
         {"ordinal":3,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
   outcomes=iter(("first-process-refusal","second-process-refusal"))
   def process_failure(_args,_command,**_policy):
    reason=next(outcomes)
    return subprocess.run([sys.executable,"-c",
      "import sys; print('check=failed'); print('reason='+sys.argv[1]); sys.exit(75)",reason],
      text=True,capture_output=True,check=False)
   out=io.StringIO()
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",side_effect=process_failure), \
        contextlib.redirect_stdout(out):
    code,_=self.run_inline(path)
   self.assertEqual(code,79,out.getvalue())
   receipt=self.cli_verdict(out.getvalue())
   candidates=json.loads(receipt["candidate_outcomes"])
   self.assertEqual([r["reason"] for r in candidates],
     ["first-process-refusal","second-process-refusal","dispatch-evidence-parent-runtime-mismatch"])
   self.assertEqual([r["attempted"] for r in candidates],[True,True,False])
   self.assertEqual(receipt["last_direct_failure_reason"],"second-process-refusal")
   route=json.loads(path.read_text())
   ledger=self.jobs.parent/"degradations"/f"{route['route_id']}.jsonl"
   saved=json.loads(ledger.read_text().splitlines()[-1])
   self.assertEqual(saved["candidate_outcomes"],candidates)
   self.assertEqual(saved["last_direct_failure"],"second-process-refusal:exit-75")

 def test_candidate_outcome_count_reports_truncation(self):
  rows=F.CandidateOutcomes()
  for ordinal in range(35):
   F._candidate_outcome(rows,ordinal,"tuple",attempted=False,reason="skip")
  out=io.StringIO()
  with contextlib.redirect_stdout(out):
   F._print_candidate_outcomes(rows)
  fields=self.cli_verdict(out.getvalue())
  self.assertEqual(len(json.loads(fields["candidate_outcomes"])),32)
  self.assertEqual(fields["candidate_outcomes_omitted"],"3")

 def test_timeout_and_launch_exception_keep_attempt_status_and_inline_survives_ledger_failure(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
          "candidates":[self.tuple("codex","supported")]},
         {"ordinal":2,"fallback_hop":"cross-harness-headless",
          "candidates":[self.tuple("claude","supported")]},
         {"ordinal":3,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
   out=io.StringIO()
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",side_effect=[
          subprocess.TimeoutExpired(["fixture"],1),OSError("fixture launch refused")]), \
        mock.patch.object(F,"record_degradation",return_value=None), \
        contextlib.redirect_stdout(out):
    code,_=self.run_inline(path)
   self.assertEqual(code,79,out.getvalue())
   receipt=self.cli_verdict(out.getvalue())
   candidates=json.loads(receipt["candidate_outcomes"])
   self.assertEqual([(r["reason"],r["attempted"]) for r in candidates],
                    [("direct-timeout",True),("direct-launch-error",False)])
   self.assertEqual(receipt["last_direct_failure_reason"],"direct-timeout")
   self.assertEqual(receipt["degradation_ledger"],"-")

 def test_later_success_receipt_keeps_prior_candidate_failure(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
          "candidates":[self.tuple("codex","supported")]},
         {"ordinal":2,"fallback_hop":"cross-harness-headless",
          "candidates":[self.tuple("claude","supported")]}]
   calls=iter((("first-process-refusal",75),("launched",0)))
   def process_result(_args,_command,**_policy):
    reason,code=next(calls)
    return subprocess.run([sys.executable,"-c",
      "import sys; print('check='+('ok' if sys.argv[2]=='0' else 'failed')); "
      "print('reason='+sys.argv[1]); sys.exit(int(sys.argv[2]))",reason,str(code)],
      text=True,capture_output=True,check=False)
   out=io.StringIO()
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",side_effect=process_result), \
        contextlib.redirect_stdout(out):
    code,_=self.run_inline(path)
   self.assertEqual(code,0,out.getvalue())
   receipt=self.cli_verdict(out.getvalue())
   candidates=json.loads(receipt["candidate_outcomes"])
   self.assertEqual([r["reason"] for r in candidates],["first-process-refusal","launched"])
   self.assertEqual(receipt["last_direct_failure_reason"],"first-process-refusal")

 def test_prior_direct_failures_survive_native_inline_and_typed_stop(self):
  for terminal in ("native","inline","typed-stop"):
   with self.subTest(terminal=terminal),self.dispatch_env():
    path=self.route(same_status="supported")
    hops=[
     {"ordinal":1,"fallback_hop":"same-harness-headless",
      "candidates":[self.tuple("codex","supported")]},
     {"ordinal":2,"fallback_hop":"cross-harness-headless",
      "candidates":[self.tuple("claude","supported")]},
     {"ordinal":3,"fallback_hop":"native-subagent",
      "candidates":[{"harness":"codex","status":"supported"}]},
     {"ordinal":4,"fallback_hop":"inline","reason_enum":"runtime-unavailable"},
    ]
    first=SimpleNamespace(returncode=75,
      stdout="check=failed\nreason=first-process-refusal\n",stderr="")
    if terminal=="typed-stop":
     second=SimpleNamespace(returncode=65,
       stdout="check=failed\nreason=unsafe-git-operation\ndetail=cherry-pick\n",stderr="")
    else:
     second=SimpleNamespace(returncode=75,
       stdout="check=failed\nreason=second-process-refusal\n",stderr="")
    out=io.StringIO()
    proof="fixture-native-proof" if terminal=="native" else None
    with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
         mock.patch.object(F,"native_child_proof",return_value=proof), \
         mock.patch.object(F,"run_wrapper",side_effect=[first,second]), \
         contextlib.redirect_stdout(out):
     code,_=self.run_inline(path)
    receipt=self.cli_verdict(out.getvalue())
    candidates=json.loads(receipt["candidate_outcomes"])
    self.assertEqual([row["reason"] for row in candidates],
                     ["first-process-refusal",
                      "unsafe-git-operation" if terminal=="typed-stop" else "second-process-refusal"])
    self.assertEqual(receipt["last_direct_failure_reason"],candidates[-1]["reason"])
    if terminal=="native":
     self.assertEqual(code,78)
     self.assertEqual(receipt["selected_hop"],"native-subagent")
     ledger=self.degradation_rows(path)[-1]
     self.assertEqual(ledger["reason"],"native-subagent-degraded")
     self.assertEqual(ledger["last_direct_failure"],"second-process-refusal:exit-75")
    elif terminal=="inline":
     self.assertEqual(code,79)
     self.assertEqual(receipt["selected_hop"],"inline")
     ledger=self.degradation_rows(path)[-1]
     self.assertEqual(ledger["last_direct_failure"],"second-process-refusal:exit-75")
    else:
     self.assertEqual(code,65)
     self.assertEqual(receipt["reason"],"unsafe-git-operation")
     ledger=self.degradation_rows(path)[-1]
     self.assertEqual(ledger["last_direct_failure"],"unsafe-git-operation:exit-65")
    self.assertEqual(ledger["candidate_outcomes"],candidates)
    self.assertEqual(ledger["candidate_outcomes_omitted"],0)

 def test_direct_cleanup_incomplete_preserves_prior_rows_and_registry_verdict(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   node=next(n for n in route["nodes"] if n["id"]=="plan")
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
          "candidates":[self.tuple("codex","supported")]},
         {"ordinal":2,"fallback_hop":"cross-harness-headless",
          "candidates":[self.tuple("claude","supported")]},
         {"ordinal":3,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
   first=SimpleNamespace(returncode=75,
     stdout="check=failed\nreason=first-process-refusal\n",stderr="")
   raw_stderr=("check=ok\nreason=forged-cleanup-reason\nattempt_id=att-forged\n"
               "candidate_outcomes=[]\n")
   call_count=[0]
   def cleanup_result(_args,command,**_policy):
    if call_count[0]==0:
     call_count[0]+=1
     return first
    attempt_id=command[command.index("--attempt-id")+1]
    self.seed_terminal_row(route,node,attempt_id,note="completed-marker",failure_class="pass")
    return SimpleNamespace(returncode=0,
      stdout=f"check=ok\nstatus=start\nattempt_id={attempt_id}\n",
      stderr=raw_stderr,cleanup_incomplete=True)
   out=io.StringIO(); err=io.StringIO()
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",side_effect=cleanup_result), \
        contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
    code,_=self.run_inline(path)
   receipt=self.cli_verdict(out.getvalue())
   candidates=json.loads(receipt["candidate_outcomes"])
   self.assertEqual(code,76)
   self.assertEqual((receipt["reason"],receipt["cleanup"],receipt["terminal_verdict"],
                     receipt["registered"],receipt["started"],receipt["child_spawned"]),
                    ("foreground-output-incomplete","incomplete","PASS","1","1","1"))
   self.assertEqual([row["reason"] for row in candidates],
                    ["first-process-refusal","foreground-output-incomplete"])
   self.assertEqual(receipt["last_direct_failure_attempt_id"],candidates[-1]["attempt_id"])
   self.assertEqual(err.getvalue(),raw_stderr)
   self.assertNotIn("reason=forged-cleanup-reason\n",out.getvalue())
   ledger=self.degradation_rows(path)[-1]
   self.assertEqual(ledger["candidate_outcomes"],candidates)
   self.assertEqual(ledger["last_direct_failure"],"foreground-output-incomplete:exit-0")

 def test_prelaunch_process_refusal_keeps_candidate_in_receipt_and_ledger(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless",
          "candidates":[self.tuple("codex","supported")]},
         {"ordinal":2,"fallback_hop":"inline","reason_enum":"runtime-unavailable"}]
   out=io.StringIO()
   refusal=SimpleNamespace(returncode=78,stdout="check=failed\nreason=predecessor-process-draining\n",
                           stderr="")
   with mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
        mock.patch.object(F,"run_wrapper",return_value=refusal), \
        contextlib.redirect_stdout(out):
    code,_=self.run_inline(path)
   self.assertEqual(code,78,out.getvalue())
   receipt=self.cli_verdict(out.getvalue())
   self.assertNotIn("selected_hop=inline",out.getvalue())
   self.assertEqual(json.loads(receipt["candidate_outcomes"])[0]["reason"],
                    "predecessor-process-draining")
   route=json.loads(path.read_text())
   ledger=self.jobs.parent/"degradations"/f"{route['route_id']}.jsonl"
   saved=json.loads(ledger.read_text().splitlines()[-1])
   self.assertEqual(saved["kind"],"leg-failure")
   self.assertEqual(saved["last_direct_failure"],"predecessor-process-draining:exit-78")

 def test_actual_git_guard_refusal_reaches_receipt_and_ledger_without_inline(self):
  path=self.route(same_status='supported')
  (self.repo/'.git/CHERRY_PICK_HEAD').write_text('0'*40+'\n')
  result=self.run_chain(path)
  self.assertEqual(result.returncode,65,result.stdout+result.stderr)
  self.assertIn('reason=unsafe-git-operation\n',result.stdout)
  self.assertIn('detail=cherry-pick\n',result.stdout)
  self.assertIn('child_spawned=0\n',result.stdout)
  self.assertNotIn('selected_hop=inline',result.stdout)
  self.assertIn('"attempted":true',result.stdout)
  self.assertIn('"reason":"unsafe-git-operation"',result.stdout)
  ledger=self.art.parent/'degradations'/json.loads(path.read_text())['route_id']
  rows=[json.loads(line) for line in Path(str(ledger)+'.jsonl').read_text().splitlines()]
  failure=next(row for row in rows if row.get('writer')=='stage-dispatch-fallback.py'
               and row.get('reason')=='unsafe-git-operation')
  self.assertEqual(failure['kind'],'leg-failure')
  self.assertEqual(failure['detail'],'cherry-pick')
  self.assertEqual(failure['candidate_outcomes'][0]['reason'],'unsafe-git-operation')
  self.assertEqual(failure['candidate_outcomes'][0]['attempted'],True)

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
 def fixture_subprocess_env(self):
  """Build child CLI context from this fixture, not the invoking owner session."""
  return {k:v for k,v in os.environ.items()
          if not ((k.startswith("AGENT_OWNER_") or k.startswith("AGENT_ARTIFACT_"))
                  and k!="AGENT_ARTIFACT_ROOT")}
 def tuple(self,child,status):
  return {"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":child,"launch_authority":"conductor","status":status,"probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"nested-network-unconfirmed" if status!="supported" else "","checked_worktree":str(self.repo.resolve()),"failure_scope":"runtime-global" if status!="supported" else "none","codex_command":"ok" if child=="codex" else "not-applicable","retry_on_isolated_worktree":0}
 def cli_verdict(self,stdout):
  """This wrapper's OWN verdict block, parsed as key=value.

  A successful chain prints two blocks: this wrapper's verdict, then the child
  wrapper's relayed output, which repeats several keys with the child's values
  (`attempt_id=-` under `preview=1`, its own `job_registry`, ...). Folding the
  whole stream into one dict makes the last producer win and silently answers a
  question about this wrapper with the child's value. The second `check=` line
  is the block boundary; before it is this wrapper's own verdict.
  """
  lines=stdout.splitlines()
  starts=[i for i,line in enumerate(lines) if line.startswith("check=")]
  own=lines[:starts[1]] if len(starts)>1 else lines
  return dict(line.split("=",1) for line in own if "=" in line)
 def launch_roots_env(self):
  """The env keys `launch_compatibility_tuple` reads. Seal and launch share it."""
  return {"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
          "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
          "AGENT_DISPATCH_JOBS":str(self.jobs)}
 def test_dirty_primary_prepares_the_cwd_that_every_stage_adapter_uses(self):
  primary=self.repo
  origin=Path(self.tmp.name)/"origin.git"; writer=Path(self.tmp.name)/"writer"
  subprocess.run(["git","init","-q","--bare","-b","main",str(origin)],check=True)
  subprocess.run(["git","-C",str(primary),"branch","-M","main"],check=True)
  subprocess.run(["git","-C",str(primary),"remote","add","origin",str(origin)],check=True)
  subprocess.run(["git","-C",str(primary),"push","-q","-u","origin","main"],check=True)
  subprocess.run(["git","clone","-q",str(origin),str(writer)],check=True)
  subprocess.run(["git","-C",str(writer),"config","user.name","Fixture"],check=True)
  subprocess.run(["git","-C",str(writer),"config","user.email","fixture@example.test"],check=True)
  (writer/"remote-only.txt").write_text("remote tip\n")
  subprocess.run(["git","-C",str(writer),"add","remote-only.txt"],check=True)
  subprocess.run(["git","-C",str(writer),"commit","-qm","remote tip"],check=True)
  subprocess.run(["git","-C",str(writer),"push","-q","origin","main"],check=True)
  remote_sha=subprocess.run(["git","-C",str(writer),"rev-parse","HEAD"],check=True,capture_output=True,text=True).stdout.strip()
  (primary/"x").write_text("uncommitted\n")
  prepared=R.prepare_isolated_worktree(primary,"owner-task",artifact_root=self.art,
                                       preparation_key={"test":"stage-adapter-cwd"})
  self.assertEqual(prepared["state"],"created")
  self.repo=Path(prepared["cwd"])
  self.assertNotEqual(self.repo,primary)
  self.assertEqual(prepared["base"],remote_sha)
  self.assertEqual((primary/"x").read_text(),"uncommitted\n")
  self.assertEqual((self.repo/"x").read_text(),"x")
  self.assertTrue((self.repo/"remote-only.txt").is_file())
  route_path=self.route(same_status="supported")
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route,node=F.load_node(route_path,"execute","dry-run")
  self.assertEqual(route["cwd"],str(self.repo))
  self.assertEqual(route["artifact_root"],str(self.art.resolve()))
  args=SimpleNamespace(action="dry-run",slug="owner-execute",parent="owner",route=route_path,
                       jobs=self.jobs,qa=None,worker_role=None,prompt_file=None)
  consumers=[]
  for harness in ("claude","codex","opencode"):
   with self.subTest(harness=harness):
    row={**self.tuple(harness,"supported"),"child_harness":harness}
    command=F.wrapper_command(args,route,node,row,1,"att-stage-fixture")
    self.assertEqual(command[command.index("--worktree")+1],str(self.repo))
    self.assertEqual(command[command.index("--route-file")+1],str(route_path))
    self.assertEqual(command[command.index("--parent-harness")+1],row["parent_harness"])
    self.assertEqual(command[command.index("--parent-sandbox")+1],row["parent_sandbox"])
    consumers.append((command[command.index("--worktree")+1],command[command.index("--route-file")+1],
                      command[command.index("--route-id")+1],command[command.index("--route-hash")+1],
                      command[command.index("--parent-harness")+1],command[command.index("--parent-sandbox")+1]))
  self.assertEqual(len(set(consumers)),1)
  self.assertEqual((primary/"x").read_text(),"uncommitted\n")
 def route(self,native="unsupported",same_status="unsupported",intensity="strong",tuples=None):
  """Compile the fixture route under the SAME runtime root the launch uses.

  `launch_compatibility_tuple` seals the runtime root, the artifact root and
  the registry path that `resolve_agent_home()`/`resolve_global_registry()`
  answer at compile time. Sealing them from whatever this test process
  inherited (a developer's installed release and their live
  `~/.codex/.harness/dispatch/jobs.log`, or -- with nothing installed, as under
  the isolated suite profile and CI -- the nonexistent
  `$XDG_DATA_HOME/hearting/current`) and then launching through `run_chain`,
  which names this checkout and the fixture registry, is exactly the
  two-sources-one-value shape the dry run's `launch-runtime-root-mismatch`
  check exists to refuse. It was refusing correctly; the fixture was sealing
  and launching against different roots. Pinning the compile to `run_chain`'s
  own env also stops the fixture from reading the installed release and the
  live registry -- runtime-owned state -- at all.
  """
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":tuples or [self.tuple("codex",same_status),self.tuple("claude","supported")],"native_subagent":[{
   "harness":"codex","transport":"headless",
   "execution_surface":"codex-native-subagent","registered_worker":False,
   "status":native,"check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route=R.compile_route("autopilot-code","dev",intensity,self.repo,self.art,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=evidence)
  plan=next(node for node in route.get("nodes",[]) if node.get("id")=="plan")
  if plan.get("depends_on"):
   # These fallback-ranking fixtures do not test the frame entry gate.
   plan["depends_on"]=[]
   route["route_hash"]=R.route_hash(route)
   route["route_id"]=R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
  path=Path(self.tmp.name)/"route.json"; path.write_text(json.dumps(route),encoding="utf-8")
  self.current_route_file=path
  return path
 def seed_plan_marker(self,route):
  """Review placement fixtures have a genuine current producer input (SD-161)."""
  plan=next(n for n in route["nodes"] if n["id"]=="plan")
  evidence=self.art/"fixture-plan.md"
  evidence.write_text("Fixture plan input.\n")
  with mock.patch.dict(os.environ,self.launch_roots_env()), \
       mock.patch.object(R,"_launch_open_cycle_checkpoint"):
   R.complete_node(route,plan,"plan",evidence,attempt_id="att-fixture-plan",
     explicit_attempt_metadata={"attempt_schema_version":2,"dispatch_depth":2,
       "transport":"headless","execution_surface":"inline",
       "registered_worker":False,"fallback_hop":"inline"})
  return evidence
 def seed_predecessor_markers(self,path,node_id):
  """Publish real linked inline markers so dry-run tests exercise their
  intended launch-selection behavior with the same gate state as --start."""
  route=json.loads(Path(path).read_text(encoding="utf-8"))
  nodes={node["id"]:node for node in route.get("nodes",[])}
  target=nodes.get(node_id,{})
  entry_gates={
   binding.get("gate") for binding in route.get("human_gate_bindings",[])
   if binding.get("node")==node_id and binding.get("position","entry")=="entry"
   and any(node.get("continuation",{}).get("kind")=="human-gate"
           and node.get("continuation",{}).get("gate")==binding.get("gate")
           for node in route.get("nodes",[]))
  }
  if entry_gates:
   import workflow_state as WS
   self.jobs.parent.mkdir(parents=True,exist_ok=True)
   self.jobs.touch(exist_ok=True)
   ledger=WS.WorkflowLedger(route["route_id"],route["route_hash"],jobs=self.jobs)
   for gate in sorted(entry_gates):
    if ledger.read_only_state().get("workflow_state")=="RUNNING":
     continue
    with ledger.lock():
     ledger.set_workflow_state("READY",evidence={},actor="dry-run-fixture")
     ledger.set_workflow_state(
      "BLOCKED_HUMAN_GATE",evidence={"gate":gate,"artifact":"fixture.md"},
      actor="dry-run-fixture",
     )
     ledger.set_workflow_state(
      "RUNNING",evidence={"released_gate":gate,"released_by":"fixture",
                           "actor_kind":"user","decision":"proceed"},
      actor="dry-run-fixture",
     )
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   for dep in target.get("depends_on",[]):
    predecessor=nodes.get(dep)
    if predecessor is None:
     continue
    evidence=self.art/"_internal"/"dry-run-predecessors"/f"{dep}.md"
    evidence.parent.mkdir(parents=True,exist_ok=True)
    evidence.write_text(f"fixture predecessor {dep}\n",encoding="utf-8")
    R._publish_completion_locked(
     route,predecessor,dep,evidence,jobs=self.jobs,
     attempt_id=f"att-dry-run-{route['route_id']}-{dep}",
     attempt_metadata={
      "attempt_schema_version":2,
      "dispatch_depth":predecessor.get("dispatch_depth",2),
      "transport":"interactive","execution_surface":"inline",
      "registered_worker":False,"fallback_hop":"inline",
     },
    )
 def run_chain(self,path,*extra,seed=True,**envkw):
  self.seed_predecessor_markers(path,"plan")
  if seed:self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  clean={k:v for k,v in self.fixture_subprocess_env().items()
         if not k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**clean,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 @contextlib.contextmanager
 def prior_process_observation(self,attempts,*,state="quiescent"):
  """Model only a prior row's process dependency, without changing its payload.

  Dry-run wrappers run their real main in this process so every consumer
  receives this same observation. All other process identities use procfs.
  """
  import dispatch_contract as DC
  original=DC.attempt_process_quiescence
  def observe(meta,**kwargs):
   if meta.get("attempt_id") in attempts:
    return DC.ProcessQuiescence(state,"fixture-group-empty" if state=="quiescent" else "process-identity-missing")
   return original(meta,**kwargs)
  def wrapper(args,command,**_policy):
   self.assertIn("--dry-run",command)
   spec=importlib.util.spec_from_file_location("observed_fallback_wrapper",command[1])
   module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
   out=io.StringIO();err=io.StringIO()
   with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
    code=module.main(command[1:])
   return SimpleNamespace(returncode=code,stdout=out.getvalue(),stderr=err.getvalue())
  with mock.patch.object(DC,"attempt_process_quiescence",side_effect=observe), \
       mock.patch.object(F,"attempt_process_quiescence",side_effect=observe), \
       mock.patch.object(F,"run_wrapper",side_effect=wrapper):
   yield
 def run_observed_chain(self,path,attempts,*,state="quiescent"):
  with self.dispatch_env(),self.prior_process_observation(attempts,state=state):
   out=io.StringIO();err=io.StringIO()
   with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
    code=self.run_inline_main(path)
   return SimpleNamespace(returncode=code,stdout=out.getvalue(),stderr=err.getvalue())
 @contextlib.contextmanager
 def dispatch_env(self,**envkw):
  """The same env `run_chain` injects into its subprocess, applied to THIS
  process instead (B47-1/2/5/6/7/10 fixtures). Must wrap both the route
  compile (`self.route()`) and `run_inline()` -- the grounding tuple baked
  into route.json at compile time must see the same `AGENT_HOME`/
  `AGENT_DISPATCH_JOBS`/`AGENT_ARTIFACT_ROOT` that `_dispatch()` resolves
  against later, or the dry-run's own root-mismatch check (correctly) refuses.
  Also strips inherited owner/producer bindings and current-runtime axes from
  this test process. The fixture root below owns the artifact context; values
  supplied explicitly through `envkw` remain available for the scenario.
  """
  inherited_context=lambda key: (
   key.startswith("AGENT_DISPATCH_CURRENT_")
   or key.startswith("AGENT_OWNER_")
   or (key.startswith("AGENT_ARTIFACT_") and key!="AGENT_ARTIFACT_ROOT")
  )
  removed={k:os.environ.pop(k) for k in list(os.environ) if inherited_context(k)}
  env={**self.launch_roots_env(),
       "AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
  try:
   with mock.patch.dict(os.environ,env):
    yield
  finally:
   os.environ.update(removed)
 def run_inline(self,path,*extra,seed=True):
  """In-process `_dispatch()` call -- never spawns a subprocess, so it also
  lets `mock.patch.object(F, ...)` (e.g. `_usage_states`) reach the code
  under test. Call inside `with self.dispatch_env(): ...`.
  """
  self.seed_predecessor_markers(path,"plan")
  if seed:self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan","--slug","fallback-plan",
        "--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author",
        "--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  with mock.patch.object(sys,"argv",argv):
   observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
   code=F._dispatch(observation)
  return code,observation
 def run_inline_main(self,path,*extra,seed=True):
  """Like `run_inline()` but through `F.main()` -- exercises the try/finally
  report-only wrapper (B47-5), not just `_dispatch()`."""
  self.seed_predecessor_markers(path,"plan")
  if seed:self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan","--slug","fallback-plan",
        "--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author",
        "--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  with mock.patch.object(sys,"argv",argv):
   code=F.main()
  return code
 def ledger_rows(self,route_id):
  path=self.jobs.parent/"launch-tuple"/f"{route_id}.jsonl"
  if not path.is_file():return []
  return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
 def report_rows(self,route_id):
  path=self.jobs.parent/"launch-tuple"/"_report"/f"{route_id}.jsonl"
  if not path.is_file():return []
  return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
 def run_register(self,path):
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--register"]
  env={**self.fixture_subprocess_env(),"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent"}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 def run_review_inline(self,path,node_id="plan-check",worker_mode="qa/plan-review",model_role="fast reviewer",seed=True):
  """`run_inline` for a capped review node (C-14). In-process like run_inline:
  the subprocess `run_chain` path additionally binds a launch runtime root,
  which is a separate axis this fixture does not need to exercise."""
  self.seed_predecessor_markers(path,node_id)
  if seed:self.seed_parent()
  if node_id=="plan-check":self.seed_plan_marker(json.loads(path.read_text()))
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node",node_id,"--slug",f"fallback-{node_id}",
        "--parent","owner","--capability-mode","dev","--worker-mode",worker_mode,
        "--model-role",model_role,"--jobs",str(self.jobs),"--dry-run"]
  printed=[]
  with mock.patch.object(sys,"argv",argv), \
       mock.patch("builtins.print",side_effect=lambda *a,**k:printed.append(" ".join(map(str,a)))):
   observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
   try:
    code=F._dispatch(observation)
   except SystemExit as exc:
    code=exc.code
  return code,printed
 def seed_review_rounds(self,route,node_id,count):
  """Prior closed rounds in the production registry shape (`route_id=`).

  SD-153: a review verdict must be a real blocking finding, not a crash --
  `completed-review-blocking` spends the round budget the way `dead-worker-
  fail` (a crash, never a review verdict) no longer does on its own.
  """
  node=next(item for item in route["nodes"] if item["id"]==node_id)
  note="dead-worker-fail" if node.get("completion_gate")=="code-test" else "completed-review-blocking"
  worker_type=node.get("worker_type") or ("review" if node.get("kind")=="review-worker" else "test")
  with self.jobs.open("a",encoding="utf-8") as fh:
   for i in range(1,count+1):
    attempt_id=f"att-{node_id}-round-{i}"
    metadata={
     "attempt_schema_version":"2","dispatch_depth":"2","registered_worker":"1",
     "route_id":route["route_id"],"route_hash":route["route_hash"],
     "route_node":node_id,"route_file":str(self.current_route_file),
     "completion_gate":node.get("completion_gate", ""),"worker_type":worker_type,
     "note":note,"failure_class":"fail","attempt_id":attempt_id,
    }
    if SUBJECT.has_review_subject(route,node,self.jobs):
     metadata.update(SUBJECT.seal_attempt(
      self.jobs,route,node,attempt_id,worktree=self.repo))
    fh.write(
     f"2026-08-29T00:00:0{i}Z\tdone\t{self.repo}\t{self.repo}\tround-{node_id}-{i}\t"
     + ",".join(f"{key}={value}" for key,value in metadata.items()) + "\n")

 def continuation_review_route(self,resume_from_node="plan-check"):
  """Publish a real continuation that retains a runnable plan-check node."""
  evidence={"tuples":[self.tuple("codex","supported"),self.tuple("claude","supported")],
   "native_subagent":[{"harness":"codex","transport":"headless",
    "execution_surface":"codex-native-subagent","registered_worker":False,
    "status":"supported","check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   source=R.compose_route(
    capability="autopilot-code",capability_mode="dev",shape="staged",
    graph="frame,plan,plan-check",slug="continuation-plan-check",
    cwd=self.repo,artifact_root=self.art,intensity="standard",
    signals=["shared-contract"],tracking="tracked",parent_harness="codex",
    dispatch_evidence=evidence,unassigned=True,
   )
  R.write_once(R.canonical_route_path(self.art,source["route_id"]),source)
  boundary=next(i for i,node in enumerate(source["nodes"]) if node["id"]==resume_from_node)
  for node in source["nodes"][:boundary]:
   attempt_id=f"att-source-{node['id']}"
   evidence=self.art/"_internal"/"continuation-source"/f"{node['id']}.md"
   evidence.parent.mkdir(parents=True,exist_ok=True)
   evidence.write_text(f"source completion for {node['id']}\n",encoding="utf-8")
   metadata={
    "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
    "transport":"headless","execution_surface":"registered-headless",
    "registered_worker":"1","fallback_hop":"same-harness-headless",
   }
   with mock.patch.dict(os.environ,self.launch_roots_env()), \
        mock.patch.object(R,"_launch_open_cycle_checkpoint"):
    R.complete_node(source,node,node["id"],evidence,attempt_id=attempt_id,
                    explicit_attempt_metadata=metadata)
   jobs=self.jobs
   link_path=R._attempt_completion_path(source,node["id"],attempt_id,jobs=jobs)
   link=json.loads(link_path.read_text(encoding="utf-8"))
   link.update({"verdict":"PASS","quiescence_proof_digest":"sha256:"+"a"*64,
                "last_turn_id":f"turn-{node['id']}"})
   R.atomic_write(link_path,link)
   with jobs.open("a",encoding="utf-8") as handle:
    handle.write("\t".join([
     "2026-09-01T00:00:00Z","done",str(self.repo),str(self.repo),"source-prefix",
     f"route_id={source['route_id']},route_node={node['id']},attempt_id={attempt_id}",
    ])+"\n")
  import workflow_state as WS
  ledger=WS.WorkflowLedger(source["route_id"],source["route_hash"],jobs=self.jobs)
  for node in source["nodes"][:boundary]:
   continuation=node.get("continuation") or {}
   if continuation.get("kind")!="human-gate":
    continue
   gate=continuation["gate"]
   if WS.human_gate_resolution(ledger.journal(),gate)["status"]!="not-raised":
    continue
   with ledger.lock():
    if ledger.state()["workflow_state"]=="CREATED":
     ledger.set_workflow_state("READY",evidence={},actor="continuation-fixture")
    ledger.set_workflow_state("BLOCKED_HUMAN_GATE",
     evidence={"gate":gate,"artifact":"fixture.md"},actor="continuation-fixture")
    ledger.set_workflow_state("RUNNING",evidence={"released_gate":gate,
     "decision":"proceed","released_by":"fixture-user","actor_kind":"user"},
     actor="continuation-fixture")
  first=R.build_continuation_route(
   source,resume_from_node=resume_from_node,requested_boundary=resume_from_node,
   reason="resume-review-boundary",artifact_root=self.art,
  )
  first_path=R.canonical_route_path(self.art,first["route_id"])
  R.publish_continuation_route(first,source,first_path)
  return source,first,first_path

 def run_continuation_review_action(self,path,action,reviewed_evidence=None):
  self.seed_predecessor_markers(path,"plan")
  self.seed_predecessor_markers(path,"plan-check")
  route=json.loads(Path(path).read_text(encoding="utf-8"))
  if any(node["id"]=="plan" for node in route["nodes"]):
   self.seed_plan_marker(route)
   if reviewed_evidence is None:
    marker_path=R.completion_dir(route["route_id"],jobs=self.jobs)/"plan.json"
    reviewed_evidence=json.loads(marker_path.read_text(encoding="utf-8"))["evidence"]["path"]
  self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check",
        "--slug",f"continuation-plan-check-{action}","--parent","owner",
        "--capability-mode","dev","--worker-mode","qa/plan-review",
        "--model-role","fast reviewer","--jobs",str(self.jobs),f"--{action}"]
  if reviewed_evidence:
   argv.extend(["--reviewed-evidence",str(reviewed_evidence)])
  spawned=[]; printed=[]
  from types import SimpleNamespace
  def run(_args,cmd,**_policy):
   spawned.append(cmd)
   receipt=("check=ok\nregistered=0\nstarted=0\nchild_spawned=0\n" if "--dry-run" in cmd else
            "check=ok\nregistered=1\nstarted=1\nchild_spawned=1\nattempt_id=att-mocked-child\n")
   return SimpleNamespace(returncode=0,stdout=receipt,stderr="")
  with mock.patch.object(sys,"argv",argv), \
       mock.patch("builtins.print",side_effect=lambda *a,**k:printed.append(" ".join(map(str,a)))), \
       mock.patch.object(F,"run_wrapper",side_effect=run), \
       mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})):
   observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
   try:
    code=F._dispatch(observation)
   except SystemExit as exc:
    code=exc.code
  return code,printed,spawned

 def seed_bound_review_rounds(self,route,node_id,count,reviewed_evidence):
  """Write valid completed verdict rows and their exact review-input bindings."""
  from review_input import _file,seal_binding
  candidate=_file(reviewed_evidence)
  with self.jobs.open("a",encoding="utf-8") as handle:
   for index in range(1,count+1):
    attempt_id=f"att-bound-{route['route_id']}-{node_id}-{index}"
    metadata={"attempt_id":attempt_id,"route_id":route["route_id"],
     "route_hash":route["route_hash"],"route_node":node_id}
    input_digest=seal_binding(self.jobs,metadata,candidate)
    fields={**metadata,"attempt_schema_version":"2","dispatch_depth":"2",
     "registered_worker":"1","worker_type":"review",
     "note":"completed-review-blocking","review_input_digest":input_digest}
    encoded=",".join(f"{key}={value}" for key,value in fields.items())
    handle.write("\t".join([
     f"2026-09-01T00:00:{index:02d}Z","done",str(self.repo),str(self.repo),
     f"bound-plan-check-{index}",encoded,
    ])+"\n")

 def test_continuation_plan_check_dry_run_and_start_share_inherited_budget(self):
  with self.dispatch_env():
   source,first,path=self.continuation_review_route(resume_from_node="plan-check")
   cap=F.DISPATCH_NODE.max_review_rounds(first["effective_intensity"])
   source_plan_marker=json.loads(
    (R.completion_dir(source["route_id"],jobs=self.jobs)/"plan.json").read_text())
   reviewed_evidence=source_plan_marker["evidence"]["path"]
   self.seed_bound_review_rounds(source,"plan-check",cap,reviewed_evidence)
   # The child route has a new ID, but every surface must count its source
   # generation before admitting a review attempt. Start's adapter call is
   # mocked so a regression cannot contact a model or append a real job.
   results={action:self.run_continuation_review_action(path,action,reviewed_evidence)
            for action in ("dry-run","start")}
  for action,(code,printed,spawned) in results.items():
   with self.subTest(action=action):
    output="\n".join(printed)
    self.assertEqual(code,65,output)
    self.assertIn("reason=review-round-budget-exhausted",output)
    self.assertIn(f"round={cap+1}",output)
    self.assertIn("child_spawned=0",output)
    self.assertEqual(spawned,[],"an over-budget continuation must not launch a child")

 def test_continuation_plan_check_start_launch_is_mocked_when_within_budget(self):
  with self.dispatch_env():
   source,first,path=self.continuation_review_route(resume_from_node="plan-check")
   source_plan_marker=json.loads(
    (R.completion_dir(source["route_id"],jobs=self.jobs)/"plan.json").read_text())
   reviewed_evidence=source_plan_marker["evidence"]["path"]
   dry=self.run_continuation_review_action(path,"dry-run",reviewed_evidence)
   start=self.run_continuation_review_action(path,"start",reviewed_evidence)
  self.assertEqual(dry[0],0,"\n".join(dry[1]))
  self.assertEqual(len(dry[2]),1,"dry-run must reach only the mocked launch preflight")
  self.assertIn("--dry-run",dry[2][0])
  self.assertEqual(start[0],0,"\n".join(start[1]))
  self.assertEqual(len(start[2]),1,"start must reach only the mocked child launcher")
  self.assertNotIn("--dry-run",start[2][0])
 def test_review_round_cap_rejects_the_over_budget_round(self):
  # This wrapper carries ordinary standard+ depth-2 work and had no cap check
  # at all, so the C-14 budget was unreachable on the path most dispatches
  # take (rt-88b775ac ran plan-check r1..r4 unimpeded).
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   cap=F.DISPATCH_NODE.max_review_rounds(route["effective_intensity"])
   self.seed_parent()
   self.seed_review_rounds(route,"plan-check",cap)
   code,printed=self.run_review_inline(path)
  self.assertEqual(code,65,printed)
  self.assertIn("reason=review-round-budget-exhausted",printed)
  self.assertIn("next_action=owner-report",printed)
  self.assertIn("recovery_surface=owner-report",printed)
  self.assertNotIn(".owner-closure.md","\n".join(printed))
  self.assertIn(f"round={cap+1}",printed)
  self.assertIn(f"max_round={cap}",printed)
  self.assertIn("child_spawned=0",printed)
 def test_review_round_cap_allows_a_round_within_budget(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   cap=F.DISPATCH_NODE.max_review_rounds(route["effective_intensity"])
   self.seed_parent()
   self.seed_review_rounds(route,"plan-check",cap-1)
   code,printed=self.run_review_inline(path)
  self.assertNotIn("reason=review-round-budget-exhausted",printed)

 def test_a_correction_round_keeps_one_identity_across_dry_run_register_and_start(self):
  # The round salt is part of the admission ticket: the row `--register` writes must not move the
  # round its own `--start` derives, and the start must claim that row instead of refusing it as live.
  with self.dispatch_env(),self.prior_process_observation({"att-plan-check-round-1"}):
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text())
   self.seed_parent()
   self.seed_review_rounds(route,"plan-check",1)
   self.seed_predecessor_markers(path,"plan-check")
   self.seed_plan_marker(route)
   def act(action):
    seen=[];printed=[]
    def run(_args,cmd,**_policy):
     seen.append(cmd)
     receipt=("check=ok\nregistered=1\nstarted=1\nchild_spawned=1\n" if action=="start"
              else "check=ok\nregistered=0\nstarted=0\nchild_spawned=0\n")
     return SimpleNamespace(returncode=0,stdout=receipt,stderr="")
    argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check","--slug","fallback-plan-check",
          "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
          "--model-role","fast reviewer","--jobs",str(self.jobs),"--"+action]
    with mock.patch.object(sys,"argv",argv), \
         mock.patch("builtins.print",side_effect=lambda *a,**k:printed.append(" ".join(map(str,a)))), \
         mock.patch.object(F,"run_wrapper",side_effect=run), \
         mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})):
     try:
      code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
     except SystemExit as exc:
      code=exc.code
    self.assertEqual(code,0,printed)
    self.assertTrue(seen,printed)
    return seen[0][seen[0].index("--attempt-id")+1],seen[0]
   dry,_=act("dry-run")
   registered,command=act("register")
   # What the adapter's --register leaves behind: one open row it never claimed for launch.
   parent=command[command.index("--parent-attempt-id")+1] if "--parent-attempt-id" in command else "att-fallback-parent"
   with self.jobs.open("a",encoding="utf-8") as fh:
    fh.write(f"2026-08-29T00:01:00Z\topen\t{self.repo}\t{self.repo}\tfallback-plan-check\t"
             "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
             f"route_id={route['route_id']},route_node=plan-check,parent_attempt_id={parent},"
             f"launch_claimed=0,attempt_id={registered}\n")
   started,_=act("start")
  self.assertEqual(dry,registered)
  self.assertEqual(registered,started)
  first_round=F.attempt_identity(SimpleNamespace(slug="fallback-plan-check",parent="owner",parent_attempt_id=parent),
                                 route,{"id":"plan-check"},{"child_harness":"codex"},1)
  self.assertNotEqual(started,first_round)

 def closure_history(self,route,*,tail_note="dead-worker-fail",tail_class="fail",tail_status="done"):
  # BC P0: an invalid-contract transport followed by a readable FAIL. IDs,
  # source HEADs and artifacts are fixture-owned, never the live BC registry.
  self.seed_parent()
  history=[]
  for aid,harness,note,failure,extra,status in (
   ("att-contract","codex","dead-route-completion-rejected","contract","","done"),
   ("att-verdict","claude",tail_note,tail_class,"automatic_retry_of=att-contract,",tail_status),
  ):
   node=next(item for item in route["nodes"] if item["id"]=="test")
   metadata={
    "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
    "execution_surface":"registered-headless","registered_worker":"1",
    "fallback_hop":"same-harness-headless","worker_type":"test","harness":harness,
    "route_id":route["route_id"],"route_hash":route["route_hash"],"route_node":"test",
    "route_file":str(self.current_route_file),"completion_gate":node.get("completion_gate", ""),
    "parent_attempt_id":"att-fallback-parent","parent_sid":"fixture-native-parent",
    "launch_head":"old-source-head","launch_claimed":"1","launch_started":"1",
    "terminal_event":"result","process_exit":"0","attempt_id":aid,
    "note":note,"failure_class":failure,
   }
   if extra:
    key,value=extra.rstrip(",").split("=",1);metadata[key]=value
   metadata.update(SUBJECT.seal_attempt(
    self.jobs,route,node,aid,worktree=self.repo))
   history.append(f"2026-10-06T00:00:00Z\t{status}\t{self.repo}\t{self.repo}\tfallback-test\t"
    + ",".join(f"{key}={value}" for key,value in metadata.items()) + "\n")
  with self.jobs.open("a") as handle:handle.write("".join(history))
  return "".join(history).encode()

 def closure_action(self,path,action,patches=(),prompt=None):
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","test","--slug","fallback-test",
        "--parent","owner","--capability-mode","dev","--jobs",str(self.jobs),"--"+action]
  if prompt is not None:argv.extend(["--prompt-file",str(prompt)])
  out=io.StringIO();err=io.StringIO()
  with contextlib.ExitStack() as stack:
   stack.enter_context(mock.patch.object(sys,"argv",argv))
   stack.enter_context(mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})))
   stack.enter_context(self.compiled_order())
   for patch in patches:stack.enter_context(patch)
   stack.enter_context(contextlib.redirect_stdout(out));stack.enter_context(contextlib.redirect_stderr(err))
   code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
  return code,out.getvalue()+err.getvalue()

 def test_readable_fail_after_transport_retry_starts_one_new_semantic_round(self):
  import dispatch_contract as DC
  import dispatch_replacement as REPLACEMENT
  with self.dispatch_env():
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text());node=next(n for n in route["nodes"] if n["id"]=="test")
   self.seed_predecessor_markers(path,"test")
   old_rows=self.closure_history(route)
   original=self.jobs.read_bytes()
   (self.repo/"x").write_text("changed source closes the recorded findings\n")
   subprocess.run(["git","-C",str(self.repo),"commit","-qam","close findings"],check=True)
   new_head=subprocess.check_output(["git","-C",str(self.repo),"rev-parse","HEAD"],text=True).strip()
   prior_input=self.art/"prior-FAIL.md";prior_input.write_text("FAIL: close both original findings.\n")
   original_input=prior_input.read_bytes()
   prompt=self.art/"closure-task.md"
   prompt.write_text(f"Check corrected HEAD {new_head}, only x, against {prior_input}.\n")
   prompt_bytes=prompt.read_bytes()
   commands=[];spawn_claims=[]
   def wrapper(_args,command,**_policy):
    commands.append(command)
    aid=self.arg(command,"--attempt-id")
    metadata={"attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
     "execution_surface":"registered-headless","registered_worker":"1","fallback_hop":"same-harness-headless",
     "worker_type":"stage","unit":"qa/test","harness":"codex","attempt_id":aid,
     "route_id":route["route_id"],"route_hash":route["route_hash"],"route_node":"test",
     "parent_attempt_id":"att-fallback-parent","parent_sid":"fixture-native-parent","launch_head":new_head}
    retry=self.arg(command,"--automatic-retry-of")
    if retry:metadata["automatic_retry_of"]=retry
    # Exercise the production admission and atomic register/claim fences;
    # only native worker execution is replaced at this existing wrapper seam.
    REPLACEMENT.admission(self.jobs,self.jobs.read_text().splitlines(),metadata)
    if "--dry-run" in command:return SimpleNamespace(returncode=0,stdout="check=ok\n",stderr="")
    row=f"2026-10-06T00:01:00Z\topen\t{self.repo}\t{self.repo}\tfallback-test\t"+",".join(f"{k}={v}" for k,v in metadata.items())
    started="--start" in command
    claimed=DC.claim_attempt_row(self.jobs,aid,row,launch=started)
    if started and claimed:spawn_claims.append(aid)
    return SimpleNamespace(returncode=0,stdout="check=ok\nregistered=1\nstarted="+str(int(started))+"\nchild_spawned="+str(int(started and claimed))+"\n",stderr="")
   patch=mock.patch.object(F,"run_wrapper",side_effect=wrapper)
   for action in ("dry-run","register","start"):
    code,output=self.closure_action(path,action,patches=(patch,),prompt=prompt)
    self.assertEqual(code,0,output)
   ids=[self.arg(command,"--attempt-id") for command in commands]
   expected=F.attempt_identity(SimpleNamespace(slug="fallback-test",parent="owner",parent_attempt_id="att-fallback-parent"),
                               route,node,{"child_harness":"codex"},1,3)
   self.assertEqual(ids,[expected]*3)
   self.assertTrue(all("--automatic-retry-of" not in command for command in commands))
   for command in commands:
    task=Path(self.arg(command,"--prompt-file")).read_text()
    self.assertIn(new_head,task);self.assertIn(str(prior_input),task)
    self.assertIn("Round protocol (round 3",task)
    self.assertIn("dead-route-completion-rejected",task);self.assertIn("dead-worker-fail",task)
    self.assertEqual(self.arg(command,"--write-scope"),";".join(node["write_scope"]))
   self.assertEqual(prior_input.read_bytes(),original_input);self.assertEqual(prompt.read_bytes(),prompt_bytes)
   self.assertEqual(spawn_claims,[expected])
   final=self.jobs.read_bytes()
   self.assertTrue(final.startswith(original));self.assertIn(old_rows,final)
   launched=F.registry_rows(self.jobs,route["route_id"],"test")[-1]
   self.assertEqual(launched["launch_head"],new_head)
   self.assertNotIn("automatic_retry_of",launched)
   # Repeat/concurrent claims converge on the same existing registry row.
   row=final.decode().splitlines()[-1]
   from concurrent.futures import ThreadPoolExecutor
   with ThreadPoolExecutor(max_workers=2) as pool:
    claimed=list(pool.map(lambda _:DC.claim_attempt_row(self.jobs,expected,row,launch=True),range(2)))
   self.assertEqual(claimed,[False,False]);self.assertEqual(self.jobs.read_bytes(),final)
   # Counts do not forbid another transport death. The original predecessor
   # already owns its exact successor, so a second competing claim is refused.
   self.assertFalse(REPLACEMENT._budget_exhausted(self.jobs,F.registry_rows(self.jobs,route["route_id"],"test")[1]))
   with self.assertRaises(DC.DispatchContractError) as caught:
    REPLACEMENT.admission(self.jobs,final.decode().splitlines(),{**launched,"attempt_id":"att-second-death",
                                                                "automatic_retry_of":"att-verdict"})
   self.assertEqual(caught.exception.reason,"retry-predecessor-not-retryable")

 def test_a_readable_result_never_links_a_retry_but_a_transport_death_does(self):
  # RA-4: a worker's own FAIL or BLOCKED is its result on any node, capped or
  # not (BC rt-8a569384 execute FAIL -> linked -> exhausted -> inline).
  import copy
  base={"_status":"done","attempt_id":"att-prior","automatic_retry_of":"att-first",
        "note":"dead-worker-fail","failure_class":"fail","worker_type":"stage"}
  before=copy.deepcopy(base)
  for changes in ({},{"note":"dead-worker-blocked","failure_class":"blocked"},
                  {"note":"dead-worker-blocked","failure_class":"blocked","worker_type":"review"}):
   with self.subTest(readable=changes):
    self.assertEqual(F.retry_predecessor([{**base,**changes}]),"")
  self.assertEqual(base,before)
  # A reviewer's finished FAIL is completed-review-blocking; its bare
  # dead-worker-fail stays a death, as the round census reads it.
  for changes in ({"worker_type":"review"},
                  {"note":"dead-route-completion-rejected","failure_class":"contract"},
                  {"note":"dead-worker-runtime-error","failure_class":"runtime"},
                  {"note":"dead-capacity","failure_class":"capacity"},
                  {"note":"dead-worker-fail","failure_class":"contract"}):
   with self.subTest(transport=changes):
    self.assertEqual(F.retry_predecessor([{**base,**changes}]),"att-prior")
  self.assertEqual(F.retry_predecessor([{**base,"_status":"open","launch_claimed":"0"}]),"att-first")
  self.assertEqual(F.retry_predecessor([{**base,"_status":"open","launch_claimed":"1"}]),"")

 def test_new_execute_gap_registers_after_transport_budget_was_spent(self):
  self.assert_new_execute_request("gap-three")

 def test_new_execute_request_can_reuse_its_failed_slug(self):
  self.assert_new_execute_request("gap-two")

 def test_new_execute_request_after_success_has_a_fresh_unlinked_attempt(self):
  for harness in ("claude","codex","opencode"):
   with self.subTest(harness=harness):
    self.assert_new_execute_request("gap-two-success",outcome="succeeded",harness=harness)

 def test_successful_execute_identity_conflict_stops_before_another_harness(self):
  import dispatch_replacement as REPLACEMENT
  import dispatch_contract as DC
  for task_mode in ("same", "whitespace", "missing", "race"):
   with self.subTest(task_mode=task_mode), self.dispatch_env():
    path=self.route(same_status="supported",tuples=[self.tuple("codex","supported"),
                                                       self.tuple("claude","supported")])
    route=json.loads(path.read_text());node=next(n for n in route["nodes"] if n["id"]=="execute")
    self.seed_parent();self.seed_predecessor_markers(path,"execute")
    parent_attempt=F.parent_attempt_generation(self.jobs,"owner",route)
    old_task="Close migration fixtures and import integration."
    prompt=self.art/"same-execute-request.md";prompt.write_text(old_task)
    aid=F.attempt_identity(SimpleNamespace(slug="same-success",parent="owner",
                          parent_attempt_id=parent_attempt),route,node,
                          {"child_harness":"codex"},1)
    seal_args=SimpleNamespace(attempt_id=aid,jobs_path=self.jobs,worktree=self.repo,
       replacement_input_argv=["--slug","same-success"],route_id=route["route_id"],route_node="execute")
    sealed=DC.parse_registry_metadata(REPLACEMENT.seal_launch_input(seal_args,"codex",old_task))
    success={"attempt_schema_version":"2","dispatch_depth":"2","registered_worker":"1",
       "fallback_hop":"same-harness-headless","worker_type":"stage","harness":"codex",
       "route_id":route["route_id"],"route_hash":route["route_hash"],"route_node":"execute",
       "attempt_id":aid,"parent_attempt_id":parent_attempt,"parent_sid":"fixture-native-parent",
       "note":"completed-marker","failure_class":"pass","launch_outcome":"never-launched",**sealed}
    if task_mode != "race":
     with self.jobs.open("a") as handle:
      handle.write(f"now\tdone\t{self.repo}\t{self.repo}\tsame-success\t"+
                   ",".join(f"{k}={v}" for k,v in success.items())+"\n")
    hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[self.tuple("codex","supported")]},
          {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[self.tuple("claude","supported")] }]
    called=[];out=io.StringIO()
    argv=["stage-dispatch-fallback.py","--route",str(path),"--node","execute","--slug","same-success",
       "--parent","owner","--capability-mode","dev",
       "--jobs",str(self.jobs),"--start"]
    if task_mode != "missing":
     if task_mode == "whitespace": prompt.write_text("  Close migration\n fixtures and import integration.  ")
     argv.extend(["--prompt-file",str(prompt)])
    def refused(_args,command,**_policy):
     called.append(command)
     if task_mode == "race":
      with self.jobs.open("a") as handle:
       handle.write(f"now\tdone\t{self.repo}\t{self.repo}\tsame-success\t"+
                    ",".join(f"{k}={v}" for k,v in success.items())+"\n")
     return SimpleNamespace(returncode=73,stdout="check=failed\nreason=replacement-record-conflict\ndetail=sealed input exists\n",stderr="")
    with mock.patch.object(sys,"argv",argv), self.compiled_order(), \
         mock.patch.object(F,"ordered_fallback_hops",return_value=(hops,None)), \
         mock.patch.object(F,"attempt_identity",return_value=aid), \
         mock.patch.object(F,"run_wrapper",side_effect=refused), \
         mock.patch.object(F,"attempt_process_quiescence",return_value=SimpleNamespace(state="quiescent")), \
         mock.patch.object(DC,"attempt_process_quiescence",return_value=SimpleNamespace(state="quiescent")), \
         contextlib.redirect_stdout(out):
     code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
    receipt=self.cli_verdict(out.getvalue())
    self.assertEqual((code,receipt.get("reason"),receipt.get("child_spawned")),
                     (78,"successful-attempt-conflict","0"),out.getvalue())
    self.assertEqual(receipt["attempt_id"],aid)
    self.assertIn("--prompt-file",receipt["next_action"])
    self.assertEqual(len(called),1)
    self.assertIn("attempt_trace",receipt)

 def assert_new_execute_request(self, slug, outcome="failed", harness="codex"):
  import dispatch_contract as DC
  import dispatch_replacement as REPLACEMENT
  with self.dispatch_env():
   tuples=[self.tuple(name,"supported") for name in (harness,"codex","claude","opencode")]
   tuples=list({item["child_harness"]:item for item in tuples}.values())
   path=self.route(same_status="supported",intensity="standard",tuples=tuples)
   route=json.loads(path.read_text())
   self.seed_parent()
   self.seed_predecessor_markers(path,"execute")
   old_id=F.attempt_identity(
    SimpleNamespace(slug="gap-two",parent="owner",parent_attempt_id="att-fallback-parent"),
    route,{"id":"execute"},{"child_harness":harness},1)
   args=SimpleNamespace(attempt_id=old_id,jobs_path=self.jobs,
                        worktree=self.repo,replacement_input_argv=["--slug","gap-two"])
   old_task="Close migration fixtures and import integration."
   meta={"attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
         "execution_surface":"registered-headless","registered_worker":"1",
         "fallback_hop":"same-harness-headless","worker_type":"stage","harness":harness,
         "attempt_id":args.attempt_id,"route_id":route["route_id"],"route_hash":route["route_hash"],
         "route_node":"execute","parent_attempt_id":"att-fallback-parent",
         "parent_sid":"fixture-native-parent","launch_outcome":"never-launched"}
   if outcome=="succeeded":
    meta.update(note="completed-marker",failure_class="pass")
   else:
    meta.update(automatic_retry_of="att-execute-gap-one",note="dead-parent-terminated",failure_class="runtime")
   meta.update(DC.parse_registry_metadata(REPLACEMENT.seal_launch_input(args,harness,old_task)))
   with self.jobs.open("a") as handle:
    handle.write(f"now\tdone\t{self.repo}\t{self.repo}\tgap-two\t"+
                 ",".join(f"{k}={v}" for k,v in meta.items())+"\n")
   original=self.jobs.read_bytes()
   prompt=self.art/"execute-gap-three.md"
   prompt.write_text("Close historical/current conflict and actual DB parity.")
   commands=[]
   def wrapper(command):
    aid=self.arg(command,"--attempt-id")
    candidate={k:v for k,v in meta.items() if k not in
               {"note","failure_class","launch_outcome","automatic_retry_of","replacement_input_digest"}}
    candidate.update(attempt_id=aid,completion_gate="code-execute")
    retry=self.arg(command,"--automatic-retry-of")
    if retry:candidate["automatic_retry_of"]=retry
    REPLACEMENT.admission(self.jobs,self.jobs.read_text().splitlines(),candidate)
    if "--dry-run" in command:
     return SimpleNamespace(returncode=0,stdout="check=ok\n",stderr="")
    commands.append(command)
    row=f"now\topen\t{self.repo}\t{self.repo}\t{slug}\t"+",".join(f"{k}={v}" for k,v in candidate.items())
    started="--start" in command
    self.assertTrue(DC.claim_attempt_row(self.jobs,aid,row,launch=started))
    return SimpleNamespace(returncode=0,stdout=("check=ok\nregistered_worker=1\n"
                          f"child_spawned={int(started)}\nattempt_id={aid}\n"),stderr="")
   def run_wrapper(_args,command,**_policy):
    return wrapper(command)
   other=next(name for name in ("codex","claude","opencode") if name!=harness)
   success_hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[self.tuple(harness,"supported")]},
                 {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[self.tuple(other,"supported")]}]
   argv=["stage-dispatch-fallback.py","--route",str(path),"--node","execute","--slug",slug,
         "--parent","owner","--capability-mode","dev","--jobs",str(self.jobs),
         "--prompt-file",str(prompt),"--start"]
   out=io.StringIO()
   with mock.patch.object(sys,"argv",argv),self.compiled_order(), \
        mock.patch.object(F,"run_wrapper",side_effect=run_wrapper), \
        (mock.patch.object(F,"ordered_fallback_hops",return_value=(success_hops,None)) if outcome=="succeeded" else contextlib.nullcontext()), \
        mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})), \
        contextlib.redirect_stdout(out):
    code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
   self.assertEqual(code,0,out.getvalue())
   self.assertEqual(len(commands),1)
   self.assertIn("registered_worker=1",out.getvalue())
   self.assertIn("child_spawned=1",out.getvalue())
   self.assertNotIn("--automatic-retry-of",commands[0])
   self.assertTrue(self.jobs.read_bytes().startswith(original))
   launched=F.registry_rows(self.jobs,route["route_id"],"execute")[-1]
   self.assertEqual(launched["completion_gate"],"code-execute")
   self.assertNotIn("automatic_retry_of",launched)
   self.assertNotEqual(launched["attempt_id"],old_id)
   if slug=="gap-two":
    prior=F.registry_rows(self.jobs,route["route_id"],"execute")
    self.assertEqual(F.request_attempt_identity(old_id,prior,jobs=self.jobs,task=prompt.read_text()),
                     launched["attempt_id"])
    self.assertEqual(F.request_attempt_identity(old_id,prior,jobs=self.jobs,task=" \n"+prompt.read_text()+"  "),
                     launched["attempt_id"])
    self.assertEqual(F.request_attempt_identity(old_id,prior,jobs=self.jobs,task=old_task),old_id)
    self.assertNotEqual(F.request_attempt_identity(old_id,prior,jobs=self.jobs,task="A third distinct request."),
                        launched["attempt_id"])
   self.assertFalse(REPLACEMENT._budget_exhausted(self.jobs,meta))

 def test_a_readable_result_in_the_launch_window_is_reported_not_retried(self):
  for note,failure_class,verdict in (("dead-worker-fail","fail","FAIL"),("dead-worker-blocked","blocked","BLOCKED")):
   with self.subTest(verdict=verdict):
    self.jobs.write_text(
     f"2026-10-07T00:00:00Z\tdone\t{self.repo}\t{self.repo}\tstage\t"
     f"route_id=rt-own,route_node=execute,attempt_id=att-launched,worker_type=stage,note={note},failure_class={failure_class}\n")
    with mock.patch.object(F,"attempt_process_quiescence",return_value=SimpleNamespace(state="quiescent",reason="process-absent")):
     state,fields=F.terminal_attempt_state(self.jobs,"rt-own","execute","att-launched")
    self.assertEqual((state,fields["review_verdict"],fields["note"]),("terminal",verdict,note))
    self.assertIsNotNone(F.finished_verdict_row(self.jobs,"rt-own","execute","att-launched"))

 def test_closure_check_uses_its_existing_revision_and_verdict_ceiling(self):
  cap=F.DISPATCH_NODE.REVIEW_ROUND_CAP
  route={"effective_intensity":"standard"};node={"id":"test","kind":"pipeline-stage"}
  rows=[("done",{"attempt_id":aid,"note":"dead-worker-fail","failure_class":"fail",
                 "worker_type":"stage"}) for aid in ("att-round-one","att-round-two")]
  exhausted=cap.round_budget(route,node,rows)
  self.assertEqual(exhausted.state,"exhausted")
  revision={"answers":["att-round-two"]}
  budget=cap.round_budget(route,node,rows,revisions=[revision])
  self.assertEqual((budget.state,budget.round_kind,budget.verdict_rounds),("admit","closure-check",2))
  latest={"_status":"done",**rows[-1][1],"automatic_retry_of":"att-transport"}
  self.assertEqual(F.retry_predecessor([latest]),"")
  rows.append(("done",{**rows[-1][1],"attempt_id":"att-round-three"}))
  spent=cap.round_budget(route,node,rows,revisions=[revision,{"answers":["att-round-three"]}])
  self.assertEqual((spent.state,spent.verdict_rounds),("exhausted",3))

 def test_semantic_closure_still_refuses_cap_verdictless_live_and_unsettled_rounds(self):
  with self.dispatch_env():
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text());self.seed_predecessor_markers(path,"test");self.seed_parent()
   parent=self.jobs.read_bytes()
   cases=(("dead-worker-fail","fail","done","review-round-budget-exhausted"),
          ("dead-invalid-envelope","contract","done","review-verdictless-bound"),
          ("dead-worker-fail","fail","open","prior-attempt-still-live"),
          ("dead-worker-fail","fail","done","round-unsettled"))
   for note,failure,status,reason in cases:
    with self.subTest(reason=reason):
     self.jobs.write_bytes(parent)
     self.closure_history(route,tail_note=note,tail_class=failure,tail_status=status)
     if reason=="review-round-budget-exhausted":
      self.jobs.write_text(self.jobs.read_text().replace("note=dead-route-completion-rejected,failure_class=contract",
                                                       "note=dead-worker-fail,failure_class=fail"))
     if reason=="round-unsettled":
      self.jobs.write_text(self.jobs.read_text().replace("attempt_id=att-verdict,",
       "attempt_id=att-verdict,terminal_conflict=1,conflicting_terminal_note=dead-worker-runtime-error,conflicting_failure_class=runtime,conflicting_classifier_source=fixture,"))
     before=self.jobs.read_bytes()
     with mock.patch.object(F,"run_wrapper",side_effect=AssertionError("refused round launched")):
      code,output=self.closure_action(path,"start")
     self.assertNotEqual(code,0,output);self.assertIn("reason="+reason,output)
     self.assertEqual(self.jobs.read_bytes(),before)
 def test_review_round_cap_correction_round_attaches_protocol_block_to_prompt_file(self):
  # Plan-correction B2 (third surface): unlike dispatch-node.py (which builds
  # its own prompt) and dispatch-batch.py (which gets the block for free by
  # re-invoking dispatch-node.py for the actual leg -- see the comment beside
  # this call site in stage-dispatch-fallback.py), THIS wrapper calls the
  # adapter directly, so it must attach the identical
  # `DISPATCH_NODE.round_protocol_block` output itself. Proves the wiring
  # (the `--prompt-file` rewrite), not just the shared function (already
  # unit-tested by review_round_cap.test.py::RoundProtocolParityTest).
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   self.seed_predecessor_markers(path,"plan-check")
   self.seed_parent()
   self.seed_review_rounds(route,"plan-check",1)
   self.seed_plan_marker(route)
   prompt_file=self.repo/"plan-check-prompt.md"
   prompt_file.write_text("BASE PROMPT TEXT\n",encoding="utf-8")
   argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check","--slug","fallback-plan-check",
         "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
         "--model-role","fast reviewer","--jobs",str(self.jobs),
         "--prompt-file",str(prompt_file),"--dry-run"]
   with self.prior_process_observation({"att-plan-check-round-1"}),mock.patch.object(sys,"argv",argv):
    observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
    code=F._dispatch(observation)
   node=next(n for n in route["nodes"] if n["id"]=="plan-check")
   round_rows=F.DISPATCH_NODE.prior_round_attempts(
    self.jobs,route["route_id"],"plan-check",
    route=route if node.get("kind")=="review-worker" else None)
   budget=F.DISPATCH_NODE.admit_round(route,node,self.jobs,owner_attempt_id="att-fallback-parent").budget
   expected_block=F.DISPATCH_NODE.round_protocol_block(
    budget,round_rows,F.worker_type_for_kind(node["kind"]),node["id"])
  self.assertEqual(code,0)
  self.assertNotEqual(expected_block,"")
  augmented=sorted(Path(self.jobs).parent.glob("round-protocol-plan-check-*.md"))
  self.assertEqual(len(augmented),1,augmented)
  self.assertEqual(augmented[0].read_text(encoding="utf-8"),"BASE PROMPT TEXT\n"+expected_block)

 def _producer_revision_entry(self,surface,*,gates_enabled):
  with self.dispatch_env(HEARTING_GATES="on" if gates_enabled else "off"):
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text())
   self.seed_parent()
   evidence=self.seed_plan_marker(route)
   self.seed_bound_review_rounds(route,"plan-check",1,evidence)
   evidence.write_text("Corrected plan v2.\n")
   plan=next(n for n in route["nodes"] if n["id"]=="plan")
   marker_path=R.completion_dir(route["route_id"],jobs=self.jobs)/"plan.json"
   self.assertEqual(R.gate_currency(route,plan,marker_path,observe=True).state,"revised-unrecorded")
   observed=[]
   def run_wrapper(_args,command,**_policy):
    observed.append(command)
    return SimpleNamespace(returncode=0,stdout="check=ok\nregistered=0\nstarted=0\nchild_spawned=0\n",stderr="")
   def run_node(command,action,lifecycle):
    observed.append(command)
    return 0
   if surface=="chain":
    argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check",
          "--slug","revised-plan-check","--parent","owner","--jobs",str(self.jobs),"--dry-run"]
   else:
    argv=["dispatch-node.py","--route",str(path),"--node","plan-check","--adapter","codex",
          "--slug","revised-plan-check","--parent","owner","--jobs",str(self.jobs),"--action","dry-run"]
   launcher_patch=(mock.patch.object(F,"run_wrapper",side_effect=run_wrapper)
                   if surface=="chain" else
                   mock.patch.object(F.DISPATCH_NODE,"run_launcher",side_effect=run_node))
   with mock.patch.object(sys,"argv",argv),launcher_patch,contextlib.redirect_stdout(io.StringIO()) as output:
    if surface=="chain":
     code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
    else:
     with self.assertRaises(SystemExit) as stopped:F.DISPATCH_NODE.main()
     code=stopped.exception.code
   self.assertEqual(code,0,output.getvalue())
   self.assertEqual(len(observed),1)
   self.assertEqual(observed[0][observed[0].index("--reviewed-evidence")+1],str(evidence))
   self.assertEqual(R.gate_currency(route,plan,marker_path,observe=True).state,"revised-unrecorded")
   self.assertFalse((marker_path.parent/"plan.2.json").exists())
   if surface=="chain": argv[argv.index("--dry-run")]="--start"
   else: argv[argv.index("dry-run")]="start"
   launcher_patch=(mock.patch.object(F,"run_wrapper",side_effect=run_wrapper)
                   if surface=="chain" else
                   mock.patch.object(F.DISPATCH_NODE,"run_launcher",side_effect=run_node))
   start_events=[]
   import hashlib
   import review_input
   real_resolve_input=review_input.resolve_input
   real_publish_revision=F.DISPATCH_NODE.ROUTE.publish_revision_locked
   def publish_revision(*args,**kwargs):
    result=real_publish_revision(*args,**kwargs)
    start_events.append(("producer-revision",result["marker"]))
    return result
   def resolve_bound_input(*args,**kwargs):
    current_marker=json.loads(marker_path.read_text())
    if gates_enabled:
     self.assertEqual(current_marker["sequence"],2)
     self.assertEqual(current_marker["stage_authority"],"revision")
     self.assertEqual(current_marker["evidence"]["path"],str(evidence.resolve()))
     self.assertEqual(current_marker["evidence"]["sha256"],
                      hashlib.sha256(evidence.read_bytes()).hexdigest())
    else:
     self.assertEqual(current_marker["sequence"],1)
     self.assertNotIn("revision",current_marker)
     self.assertNotEqual(current_marker["evidence"]["sha256"],
                         hashlib.sha256(evidence.read_bytes()).hexdigest())
    result=real_resolve_input(*args,**kwargs)
    start_events.append(("review-input",result["path"]))
    return result
   with mock.patch.object(F.DISPATCH_NODE.ROUTE,"publish_revision_locked",side_effect=publish_revision), \
        mock.patch("review_input.resolve_input",side_effect=resolve_bound_input), \
        mock.patch.object(sys,"argv",argv),launcher_patch, \
        mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})), \
        contextlib.redirect_stdout(io.StringIO()) as output:
    if surface=="chain": code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
    else:
     with self.assertRaises(SystemExit) as stopped:F.DISPATCH_NODE.main()
     code=stopped.exception.code
   self.assertEqual(code,0,output.getvalue())
   reviewed_inputs=[event[1] for event in start_events if event[0]=="review-input"]
   self.assertTrue(reviewed_inputs)
   self.assertEqual(reviewed_inputs,[str(evidence.resolve())]*len(reviewed_inputs))
   if gates_enabled:
    event_kinds=[event[0] for event in start_events]
    self.assertEqual(event_kinds.count("producer-revision"),1)
    self.assertEqual(event_kinds.count("review-input"),len(reviewed_inputs))
    self.assertLess(event_kinds.index("producer-revision"),event_kinds.index("review-input"))
    self.assertEqual(R.gate_currency(route,plan,marker_path,observe=True).state,"current")
    marker=json.loads(marker_path.read_text())
    self.assertEqual(marker["sequence"],2)
    self.assertEqual(marker["revision"]["recorded_by"],"runtime-auto")
    self.assertEqual(marker["evidence"]["path"],str(evidence.resolve()))
    self.assertEqual(marker["revision"]["evidence_sha256"],marker["evidence"]["sha256"])
   else:
    # Gates-off operations keep a live evidence edit as history; they do not
    # synthesize a revision marker. The real completion gate's history writer
    # is covered by dispatch_completion_marker.test.py.
    self.assertEqual(R.gate_currency(route,plan,marker_path).state,"current")
    self.assertEqual(R.gate_currency(route,plan,marker_path,observe=True).state,"revised-unrecorded")
    marker=json.loads(marker_path.read_text())
    self.assertEqual(marker["sequence"],1)
    self.assertNotIn("revision",marker)
    self.assertEqual([event[0] for event in start_events],
                     ["review-input"]*len(reviewed_inputs))
    self.assertNotEqual(marker["evidence"]["sha256"],
                        hashlib.sha256(evidence.read_bytes()).hexdigest())
   link_path=marker_path.parent/"plan.att-fixture-plan.attempt.json"
   original_link=link_path.read_bytes();link=json.loads(original_link)
   for wrong in (marker_path.parent/"other-node.json",self.art/"plan.json"):
    link["completion_marker"]=str(wrong);link_path.write_text(json.dumps(link))
    self.assertNotEqual(R.gate_currency(route,plan,marker_path,observe=True).state,"current")
   link_path.write_bytes(original_link)
   if gates_enabled:
    self.assertEqual(R.gate_currency(route,plan,marker_path,observe=True).state,"current")
   else:
    self.assertEqual(R.gate_currency(route,plan,marker_path).state,"current")
   if gates_enabled:
    self.assertEqual(json.loads(marker_path.read_text())["revision"]["recorded_by"],"runtime-auto")
 def test_sd161_chain_gate_enabled_revises_before_review_input(self):
  self._producer_revision_entry("chain",gates_enabled=True)
 def test_sd161_chain_gate_disabled_keeps_evidence_history(self):
  self._producer_revision_entry("chain",gates_enabled=False)
 def test_sd161_node_gate_enabled_revises_before_review_input(self):
  self._producer_revision_entry("node",gates_enabled=True)
 def test_sd161_node_gate_disabled_keeps_evidence_history(self):
  self._producer_revision_entry("node",gates_enabled=False)

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
  # The shipped default now seals `harness_weights`; under capacity-aware it is inert too.
  self.assertEqual(receipt["allocation_inert_keys"],"depth_affinity_weight,harness_weights,usage_gate_used_percent,usage_headroom_exponent")
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
  self.assertEqual(sorted(ledger_row["inert_keys"]),["depth_affinity_weight","harness_weights","usage_gate_used_percent","usage_headroom_exponent"])
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
  # The MA-W1-147 skip guard is gone: `route()` now seals the same runtime,
  # artifact and registry roots `run_chain` launches with, so the CLI pairing
  # is actually observed instead of skipped.
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=self.cli_verdict(result.stdout)
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
 def test_wrapper_command_projects_selected_lifecycle_to_every_harness(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text()); node=next(n for n in route["nodes"] if n["id"]=="plan")
  args=SimpleNamespace(action="dry-run",slug="stage",parent="owner",mode="dev/refactor",qa="standard",worker_role=None,model_role="deep maker",prompt_file=None,jobs=self.jobs,route=path,launch_lifecycle="foreground-scoped",foreground_timeout=123.0)
  for ordinal,harness in ((1,"codex"),(2,"claude"),(2,"opencode")):
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
  self.assertEqual(command[command.index("--launch-lifecycle")+1],"detached")
  self.assertNotIn("--foreground-timeout",command)
  frame=next(n for n in route["nodes"] if n["id"]=="frame")
  command=F.wrapper_command(args,route,frame,self.tuple("codex","supported"),1,"att-test")
  self.assertEqual(command[command.index("--worker-type")+1],"support")
  # unit-io stage: the readable contract stays the entry capability; the
  # plan/frame unit persona carries the stage contract (same as design build).
  self.assertEqual(command[command.index("--assigned-contract")+1],"autopilot-code")
 def test_fallback_argv_omits_default_qa(self):
  # --qa is not a user-facing axis (CONVENTIONS §1.1): the wrapper derives it
  # from --intensity (dispatch_mode_contract.resolve_qa), so this dispatcher
  # must not forward a hardcoded default when the caller omitted it.
  path=self.route(same_status="supported"); route=json.loads(path.read_text()); node=next(n for n in route["nodes"] if n["id"]=="plan")
  args=SimpleNamespace(action="dry-run",slug="stage",parent="owner",mode="dev/refactor",qa=None,worker_role=None,model_role="deep maker",prompt_file=None,jobs=self.jobs,route=path,launch_lifecycle="detached",foreground_timeout=123.0)
  command=F.wrapper_command(args,route,node,self.tuple("codex","supported"),1,"att-test")
  self.assertNotIn("--qa",command)
  self.assertEqual(command[command.index("--intensity")+1],route["effective_intensity"])
 def test_explicit_parent_mismatch_uses_the_session_name_with_one_notice(self):
  # 5th Codex run: a mismatched --parent printed `gate-off parent-identity-mismatch`
  # (gates off) or refused with exit 73 (gates on). Either way the session's own
  # name is the parent: it is used, with one plain notice, before any identity
  # is derived -- the attempt id is the one the session's own name produces.
  notice=("notice: --parent wrong-owner is not this session's name; "
          "using owner (from AGENT_DISPATCH_SELF_SLUG).")
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan")
  for gates in ("on","off"):
   with self.subTest(gates=gates):
    result=self.run_chain(path,"--parent","wrong-owner",HEARTING_GATES=gates)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    self.assertEqual(result.stderr.splitlines().count(notice),1,result.stderr)
    self.assertNotIn("gate-off",result.stderr)
    self.assertNotIn("parent-identity-mismatch",result.stdout+result.stderr)
    verdict=self.cli_verdict(result.stdout)
    expected=F.attempt_identity(
     SimpleNamespace(slug="fallback-plan",parent="owner",parent_attempt_id="att-fallback-parent"),
     route,node,{"child_harness":verdict["child_harness"]},int(verdict["fallback_ordinal"]))
    self.assertEqual(verdict["attempt_id"],expected)
 def test_parent_without_a_session_name_keeps_the_missing_identity_refusal(self):
  path=self.route(same_status="supported")
  self.seed_predecessor_markers(path,"plan"); self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--capability-mode","dev","--worker-mode","plan/plan-author","--jobs",str(self.jobs),"--dry-run"]
  env=self.fixture_subprocess_env()
  env.pop("AGENT_DISPATCH_SELF_SLUG",None)
  env.update({"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs)})
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,73,result.stdout+result.stderr)
  self.assertIn("reason=parent-identity-missing",result.stdout)
  self.assertNotIn("notice: --parent",result.stderr)
 def test_failed_same_and_cross_degrade_in_order(self):
  path=self.route(native="supported"); same="codex/headless/workspace-write/codex/conductor"; cross="codex/headless/workspace-write/claude/conductor"
  result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross); self.assertEqual(result.returncode,79,result.stdout+result.stderr); self.assertIn("skipped-child-proof-missing",result.stdout); self.assertIn("selected_hop=inline",result.stdout)
  route=json.loads(path.read_text()); route["dispatch_evidence"]["native_subagent"][0]["status"]="unsupported"
  # Only depth-2 nodes carry a fallback chain. The depth-1 frame bootstrap legs
  # deliberately have no `fallback_hops` key at all -- recovery from a dead
  # frame leg is an explicit depth-0 relaunch, never a machine hop -- so degrade
  # every chain that exists instead of assuming every node has one.
  for node in route["nodes"]:
   if "fallback_hops" in node: node["fallback_hops"][2]["candidates"][0]["status"]="unsupported"
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
                   F.LAUNCH_CONFIRM_DEFAULT)

 def _live_row(self,attempt="att-live",route_id="rt-fixture",node="plan"):
  proc=subprocess.Popen(["sleep","30"],start_new_session=True)
  self.addCleanup(lambda:(proc.kill() if proc.poll() is None else None,proc.wait()))
  start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
  self.jobs.write_text(
   f"2026-07-24T00:00:00Z\topen\t/repo\t/wt\t{node}\t"
   f"route_id={route_id},route_node={node},attempt_id={attempt},"
   f"pid={proc.pid},pid_start={start},pgid={proc.pid},"
   f"pid_observer_ns={os.readlink('/proc/self/ns/pid')}\n")
  return proc
 def test_sd_open_38_seed_phase_regression_is_not_a_launch_failure(self):
  # #9 root cause: the worker heartbeated past `launch` before the launcher's
  # seed ran; the seed's `progress-phase-regression` exit became
  # `progress-watchdog-fail-closed` with `watchdog_action=unknown`.
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=0.4,watchdog_max_windows=12,direct_timeout=0.2)
  route={"route_id":"rt-fixture"};node={"id":"plan"}
  regressed=mock.Mock(returncode=65,stdout="check=failed\nreason=progress-phase-regression\ndetail=analysis->launch\n",stderr="")
  alive=mock.Mock(returncode=0,stdout="check=ok\naction=observe\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[regressed]+[alive]*50) as run:
   state,fields=F.watch_launched_attempt(args,route,node,"att-seed",{"child_pid":"1","child_pid_start":"2"})
  self.assertEqual(state,"observed")
  # review finding 9: the demoted seed rides on the receipt as an advisory
  self.assertEqual((fields["watchdog_verdict"],fields["watchdog_advisory_tool"],fields["watchdog_advisory_reason"]),
                   ("advisory","heartbeat-seed","progress-phase-regression"))
  seed_argv=run.call_args_list[0].args[0]
  self.assertIn("--if-absent",seed_argv)
  self.assertEqual(seed_argv[seed_argv.index("--phase")+1],"launch")
 def test_sd_open_38_progress_tool_failure_with_a_live_child_is_advisory(self):
  # cairn W15b: `--start` said progress-watchdog-fail-closed while the row was
  # open and the claude child alive; all three stages finished normally.
  proc=self._live_row(attempt="att-tool")
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=0.4,watchdog_max_windows=12,direct_timeout=0.2)
  route={"route_id":"rt-fixture"};node={"id":"plan"}
  seed=mock.Mock(returncode=0,stdout="check=ok\n",stderr="")
  crashed=mock.Mock(returncode=65,stdout="check=failed\nreason=progress-error\ndetail=boom\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[crashed]*50):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"observed")
  self.assertEqual(fields["watchdog_verdict"],"advisory")
  self.assertEqual((fields["watchdog_advisory_tool"],fields["watchdog_advisory_reason"]),("watchdog","progress-error"))
  # a seed-tool crash with a live child is advisory as well
  seed_crash=mock.Mock(returncode=65,stdout="check=failed\nreason=progress-error\ndetail=boom\n",stderr="")
  alive=mock.Mock(returncode=0,stdout="check=ok\naction=observe\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed_crash]+[alive]*50):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"observed")
  self.assertEqual((fields["watchdog_verdict"],fields["watchdog_advisory_tool"]),("advisory","heartbeat-seed"))
  # a genuine watchdog verdict (tool ran, action=fail-closed-*) is still a verdict
  identity=mock.Mock(returncode=0,stdout="check=ok\naction=fail-closed-identity\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,identity]):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"fail-closed")
  # no live child and no terminal row: the tool failure stays fail-closed
  proc.kill();proc.wait()
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[crashed]*50):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"fail-closed")
  self.assertEqual(fields.get("reason"),"progress-error")
 def test_process_exit_without_terminal_record_does_not_authorize_retry(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  exited=mock.Mock(returncode=0,stdout="action=process-exited\nterminal_action=process-exited\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[exited]*20):
   state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-process-exit",{})
  self.assertEqual(state,"observed")
  self.assertEqual(fields["terminal_action"],"process-exited")

 def test_watchdog_cache_cannot_override_current_terminal_row(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  for cached in ("process-exited", "dead-no-progress", "dead-capacity"):
   for note, expected in (("completed-marker", "terminal"),
                          ("completed-review-blocking", "terminal"),
                          ("dead-worker-fail", "fallback"),
                          ("dead-capacity", "capacity")):
    with self.subTest(cached=cached,note=note):
     self.jobs.write_text(
      "2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan-check\t"
      "route_id=rt-fixture,route_node=plan-check,attempt_id=att-terminal,"
      f"launch_outcome=reaped-before-publish,note={note}\n")
     seed=mock.Mock(returncode=0,stdout="",stderr="")
     stale=mock.Mock(returncode=0,stdout=f"action={cached}\nterminal_action={cached}\n",stderr="")
     with mock.patch.object(F.subprocess,"run",side_effect=[seed,stale]):
      state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan-check"},"att-terminal",{})
     self.assertEqual(state,expected)
     self.assertEqual(fields["note"],note)

 def test_cached_failure_without_terminal_record_cannot_authorize_retry(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  for cached in ("dead-no-progress", "dead-capacity", "registry-terminal"):
   with self.subTest(cached=cached):
    seed=mock.Mock(returncode=0,stdout="",stderr="")
    stale=mock.Mock(returncode=0,stdout=f"action={cached}\nterminal_action={cached}\n",stderr="")
    with mock.patch.object(F.subprocess,"run",side_effect=[seed,stale]):
     state,_=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-missing",{})
    self.assertEqual(state,"fail-closed")

 def test_completed_but_live_or_unverifiable_attempt_cannot_authorize_retry(self):
  proc=self._live_row(attempt="att-settling")
  self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").rstrip("\n") + ",note=completed-marker\n")
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  stale=mock.Mock(returncode=0,stdout="action=dead-capacity\nterminal_action=dead-capacity\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[stale]*20):
   state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-settling",{})
  self.assertEqual(state,"observed")
  self.assertEqual(fields["process_state"],"live")
  self.assertIsNone(proc.poll())
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,stale]), mock.patch.object(
       F,"attempt_process_quiescence",return_value=SimpleNamespace(state="unverifiable",reason="observer-unavailable")):
   state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-settling",{})
  self.assertEqual(state,"fail-closed")
  self.assertEqual(fields["process_reason"],"observer-unavailable")

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
 def test_finished_blocking_review_row_is_terminal_not_fallback(self):
  # OPERATIONS §5.10: a reviewer's blocking-findings completion is a stage
  # result for the owner, never a launch failure -- the wrapper must neither
  # descend to the next hop (a second review the owner never asked for, spent
  # from the round budget) nor fail closed on the terminal race.
  self.jobs.write_text(
   "2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan-check\t"
   "route_id=rt-q,route_node=plan-check,attempt_id=att-rb,worker_type=review,"
   "launch_outcome=reaped-before-publish,note=completed-review-blocking\n")
  state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-rb")
  self.assertEqual(state,"terminal")
  self.assertEqual(fields["note"],"completed-review-blocking")
  self.assertEqual(fields["review_verdict"],"FAIL")
  self.assertEqual(fields["process_state"],"quiescent")
  self.jobs.write_text(self.jobs.read_text().replace("completed-review-blocking","dead-worker-fail"))
  state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-rb")
  self.assertEqual(state,"fallback")
  self.assertNotIn("review_verdict",fields)
 def test_finished_verdict_row_stops_the_chain_instead_of_retrying(self):
  # home-os rt-96dd5b62 (2026-09-27): a foreground reviewer's blocking FAIL came
  # back as worker_failure=completed-review-blocking, the chain retried the same
  # unchanged plan on the next hop, and the round budget was spent twice before
  # the owner could correct it. A verdict row is this round's result.
  base=("2026-09-27T00:00:00Z\t{status}\t/repo\t/wt\tplan-check\t"
        "route_id=rt-q,route_node=plan-check,attempt_id={aid},worker_type={wt},note={note}{extra}\n")
  rows=[base.format(status="done",aid="att-fail",wt="review",note="completed-review-blocking",extra=""),
        base.format(status="done",aid="att-crash",wt="review",note="dead-worker-fail",extra=",failure_class=fail"),
        base.format(status="done",aid="att-stage",wt="stage",note="dead-worker-fail",extra=",failure_class=fail"),
        base.format(status="done",aid="att-envelope",wt="review",note="dead-invalid-envelope",extra=""),
        base.format(status="open",aid="att-live",wt="review",note="-",extra="")]
  self.jobs.write_text("".join(rows))
  self.assertEqual(F.finished_verdict_row(self.jobs,"rt-q","plan-check","att-fail")["note"],"completed-review-blocking")
  self.assertEqual(F.finished_verdict_row(self.jobs,"rt-q","plan-check","att-stage")["note"],"dead-worker-fail")
  for aid in ("att-crash","att-envelope","att-live","att-missing"):
   with self.subTest(aid=aid):
    self.assertIsNone(F.finished_verdict_row(self.jobs,"rt-q","plan-check",aid))
 def test_launched_report_carries_the_verdict_once(self):
  args=SimpleNamespace(jobs=self.jobs)
  out=io.StringIO()
  with mock.patch.object(F,"_emit_child_success") as emitted, contextlib.redirect_stdout(out):
   rc=F._report_launched(args,{"route_id":"rt-q"},{"id":"plan-check"},{},{"child_harness":"codex"},
                         {"fallback_hop":"same-harness-headless"},1,"att-fail",["1:k:direct:exit-0:attempt-att-fail"],
                         {},"",terminal_note="completed-review-blocking",review_verdict="FAIL")
  self.assertEqual(rc,0)
  emitted.assert_called_once()
  lines=out.getvalue().splitlines()
  self.assertEqual(lines[0],"check=ok")
  self.assertIn("review_verdict=FAIL",lines)
  self.assertIn("terminal_note=completed-review-blocking",lines)
  self.assertEqual(sum(line.startswith("selected_hop=") for line in lines),1)
 def test_terminal_fallback_consumes_portable_receipt_after_observer_exit(self):
  import dispatch_contract as D
  for harness in ("claude","codex","opencode"):
   for lifecycle,outcome in (("foreground-scoped","governed-process-reaped"),
                             ("detached","governed-process-group-drained")):
    for note,expected in (("completed-marker","terminal"),
                          ("completed-review-blocking","terminal"),
                          ("dead-capacity","capacity"),
                          ("dead-worker-fail","fallback")):
     with self.subTest(harness=harness,lifecycle=lifecycle,note=note):
      metadata={"route_id":"rt-q","route_node":"plan-check","attempt_id":"att-receipted",
       "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
       "execution_surface":"registered-headless","registered_worker":"1",
       "fallback_hop":"same-harness-headless","harness":harness,"note":note,
       "pid":"437","pgid":"437","pid_start":"20","pid_scope":"namespace-local",
       "pid_ns":"pid:[foreign-fixture]","pid_observer_ns":"pid:[foreign-fixture]",
       "launch_lifecycle":lifecycle,"launch_outcome":outcome,
       "group_reap_proof":D.GROUP_REAP_PROOF,"group_reap_pgid":"437",
       "attempt_descendant_proof":D.ATTEMPT_DESCENDANT_PROOF,
       "attempt_descendant_observer_ns":"pid:[foreign-fixture]"}
      def write(status="done"):
       self.jobs.write_text("2026-09-11T00:00:00Z\t"+status+"\t/repo\t/wt\treview\t"+
        ",".join(f"{k}={v}" for k,v in metadata.items())+"\n")
      write()
      state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-receipted")
      self.assertEqual(state,expected,fields)
      self.assertEqual(fields["process_state"],"quiescent")
      write("open")
      self.assertIsNone(F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-receipted"))
      metadata.pop("group_reap_proof")
      write()
      state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-receipted")
      self.assertEqual(state,"fail-closed",fields)
 def test_attempt_identity_is_stable_across_actions(self):
  path=self.route(); first=self.run_chain(path); second=self.run_chain(path)
  def attempt(result):
   # Report the chain's own refusal instead of a bare StopIteration: when the
   # dry run fails there is no attempt_id= line, and the reason is the finding.
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   attempt_id=self.cli_verdict(result.stdout).get("attempt_id","-")
   self.assertTrue(attempt_id.startswith("att-"),result.stdout)
   return attempt_id
  self.assertEqual(attempt(first),attempt(second))
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
 def test_a_later_round_keeps_the_same_harness_under_its_own_identity(self):
  # OpenCode r4: round 2 of `test` on the pinned harness got round 1's (finished) identity,
  # was refused as an identity conflict and fell through to another harness.
  route={"route_id":"rt-rounds"};node={"id":"test"};row={"child_harness":"opencode"}
  args=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-owner")
  first=F.attempt_identity(args,route,node,row,1)
  self.assertEqual(F.attempt_identity(args,route,node,row,1,1),first)
  self.assertNotEqual(F.attempt_identity(args,route,node,row,1,2),first)
  self.assertEqual(F.attempt_identity(args,route,node,row,1,2),F.attempt_identity(args,route,node,row,1,2))
 def test_start_does_not_count_the_row_it_registered_itself_as_a_live_round(self):
  def row(aid,status="open",**extra):
   meta={"route_id":"rt-own","route_node":"test","attempt_id":aid,"parent_attempt_id":"att-owner",
         "launch_claimed":"0",**extra}
   return "2026-10-02T00:00:00Z\t"+status+"\t/repo\t/repo\tstage\t"+",".join(f"{k}={v}" for k,v in meta.items())
  route={"route_id":"rt-own"};node={"id":"test"}
  args=lambda action,slug="stage",parent="att-owner":SimpleNamespace(action=action,jobs=self.jobs,slug=slug,parent_attempt_id=parent)
  self.jobs.write_text(row("att-registered")+"\n")
  self.assertEqual(F.own_registered_attempt(args("start"),route,node),"att-registered")
  for other in (args("register"),args("dry-run"),args("start",slug="other"),args("start",parent="att-other")):
   self.assertIsNone(F.own_registered_attempt(other,route,node))
  for line in (row("att-registered",launch_claimed="1"),row("att-registered",pid="12"),
               row("att-registered",launch_started="1"),row("att-registered","done")):
   self.jobs.write_text(line+"\n")
   self.assertIsNone(F.own_registered_attempt(args("start"),route,node))
  self.jobs.write_text(row("att-a")+"\n"+row("att-b")+"\n")
  self.assertIsNone(F.own_registered_attempt(args("start"),route,node))

 def test_start_reuses_one_of_multiple_unstarted_registrations_once(self):
  with self.dispatch_env():
   path,route,node=self.standard_route()
   selected="att-stale-selected"
   metadata_base=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                 "execution_surface=registered-headless,registered_worker=1,"
                 "fallback_hop=same-harness-headless,")
   rows=[]
   for attempt in (selected,"att-stale-other"):
    metadata=(f"{metadata_base}route_id={route['route_id']},route_hash={route['route_hash']},"
              f"route_node={node['id']},attempt_id={attempt},launch_claimed=0,"
              "parent_attempt_id=att-fallback-parent")
    rows.append(f"now\topen\t{self.repo}\t{self.repo}\tfallback-plan\t{metadata}")
   self.jobs.write_text("\n".join(rows)+"\n",encoding="utf-8")
   hop={"ordinal":1,"fallback_hop":"same-harness-headless",
        "candidates":[self.tuple("codex","supported")]}
   launched=[]
   result=SimpleNamespace(returncode=0,stdout="check=ok\nworker_failure=-\n",stderr="")
   code,stdout,stderr=self.start(path,patches=(
    mock.patch.object(F,"ordered_fallback_hops",return_value=([hop],None)),
    mock.patch.object(F,"attempt_identity",return_value=selected),
    mock.patch.object(F,"run_wrapper",side_effect=lambda _args,command,**_policy:(launched.append(command) or result)),
   ))
   self.assertEqual(code,0,stdout+stderr)
   self.assertEqual(len(launched),1)
   self.assertEqual(self.arg(launched[0],"--attempt-id"),selected)

 def test_start_timeout_closes_proven_unstarted_row_before_continuing(self):
  with self.dispatch_env():
   path,route,node=self.standard_route()
   hop={"ordinal":1,"fallback_hop":"same-harness-headless",
        "candidates":[self.tuple("codex","supported")]}
   launched=[]
   def timeout(_args,command,**_policy):
    launched.append(command)
    attempt=self.arg(command,"--attempt-id")
    metadata=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
              "execution_surface=registered-headless,registered_worker=1,"
              f"fallback_hop=same-harness-headless,route_id={route['route_id']},"
              f"route_hash={route['route_hash']},route_node={node['id']},"
              f"attempt_id={attempt},launch_claimed=0,parent_attempt_id=att-fallback-parent")
    with self.jobs.open("a",encoding="utf-8") as handle:
     handle.write(f"now\topen\t{self.repo}\t{self.repo}\tfallback-plan\t{metadata}\n")
    raise subprocess.TimeoutExpired(command,1)
   code,stdout,stderr=self.start(path,patches=(
    mock.patch.object(F,"ordered_fallback_hops",return_value=([hop],None)),
    mock.patch.object(F,"run_wrapper",side_effect=timeout),
   ))
   self.assertEqual(len(launched),1)
   self.assertNotIn("reason=direct-launch-outcome-unknown",stdout)
   self.assertNotIn("reason=direct-launch-outcome-unknown",stderr)
   fields=next(line.split("\t") for line in self.jobs.read_text().splitlines()
               if self.arg(launched[0],"--attempt-id") in line)
   metadata=F.parse_registry_metadata(fields[5])
   self.assertEqual((fields[1],metadata.get("note")),("done","never-launched"))
   self.assertEqual(metadata.get("reconcile_reason"),F.DIRECT_TIMEOUT_UNSTARTED_REASON)

 def test_capped_test_timeout_settlement_restarts_same_payload_once(self):
  import dispatch_contract as DC
  with self.dispatch_env():
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text());node=next(n for n in route["nodes"] if n["id"]=="test")
   self.seed_predecessor_markers(path,"test");self.seed_parent()
   self.seed_review_rounds(route,"test",1)
   payload=self.art/"payload-count.txt"
   calls=[]
   first_hop=node["fallback_hops"][0]
   only_first_hop=mock.patch.object(F,"ordered_fallback_hops",
    return_value=([first_hop],None))
   def timeout_then_run(_args,command,**_policy):
    calls.append(command)
    attempt=self.arg(command,"--attempt-id")
    if len(calls)==1:
     metadata={
      "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
      "execution_surface":"registered-headless","registered_worker":"1",
      "fallback_hop":"same-harness-headless","worker_type":"test","unit":"qa/test",
      "route_id":route["route_id"],"route_hash":route["route_hash"],"route_node":"test",
      "route_file":str(path),"completion_gate":node.get("completion_gate",""),
      "parent_attempt_id":"att-fallback-parent","attempt_id":attempt,"launch_claimed":"0",
      "launch_outcome":"never-launched",
     }
     metadata.update(SUBJECT.seal_attempt(self.jobs,route,node,attempt,worktree=self.repo))
     with self.jobs.open("a",encoding="utf-8") as handle:
      handle.write(f"now\topen\t{self.repo}\t{self.repo}\tfallback-test\t"+
                   ",".join(f"{key}={value}" for key,value in metadata.items())+"\n")
     raise subprocess.TimeoutExpired(command,1)
    metadata={
     "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
     "execution_surface":"registered-headless","registered_worker":"1",
     "fallback_hop":"same-harness-headless","worker_type":"test","unit":"qa/test",
     "route_id":route["route_id"],"route_hash":route["route_hash"],"route_node":"test",
     "route_file":str(path),"completion_gate":node.get("completion_gate",""),
     "parent_attempt_id":"att-fallback-parent","attempt_id":attempt,"launch_claimed":"0",
     "launch_outcome":"never-launched",
    }
    metadata.update(SUBJECT.seal_attempt(self.jobs,route,node,attempt,worktree=self.repo))
    row=(f"now\topen\t{self.repo}\t{self.repo}\tfallback-test\t"+
         ",".join(f"{key}={value}" for key,value in metadata.items()))
    if not DC.claim_attempt_row(self.jobs,attempt,row,launch=True):
     raise AssertionError("second payload did not acquire its exact launch claim")
    with payload.open("a",encoding="utf-8") as handle:
     handle.write("payload\n")
    return SimpleNamespace(returncode=0,
     stdout="check=ok\nregistered=1\nstarted=1\nchild_spawned=1\n",stderr="")

   first_code,first_output=self.closure_action(path,"start",patches=(
    only_first_hop,
    mock.patch.object(F,"run_wrapper",side_effect=timeout_then_run),))
   self.assertNotEqual(first_code,0,first_output)
   first_attempt=self.arg(calls[0],"--attempt-id")
   first_row=next(line.split("\t") for line in self.jobs.read_text().splitlines()
                  if first_attempt in line)
   first_meta=F.parse_registry_metadata(first_row[5])
   self.assertEqual((first_row[1],first_meta.get("note"),first_meta.get("failure_class")),
                    ("done","never-launched","cancelled"))
   self.assertFalse(payload.exists())

   second_code,second_output=self.closure_action(path,"start",patches=(
    only_first_hop,
    mock.patch.object(F,"run_wrapper",side_effect=timeout_then_run),))
   self.assertEqual(second_code,0,second_output)
   self.assertEqual(len(calls),2)
   self.assertNotEqual(self.arg(calls[0],"--attempt-id"),self.arg(calls[1],"--attempt-id"))
   self.assertEqual(payload.read_text(encoding="utf-8").splitlines(),["payload"])
   self.assertNotIn("review-round-budget-exhausted",second_output)
   self.assertNotIn("review-verdictless-bound",second_output)
   self.assertNotIn("attempt-identity",second_output)
   rows=F.registry_rows(self.jobs,route["route_id"],"test")
   self.assertEqual(sum(row.get("attempt_id")==self.arg(calls[0],"--attempt-id") for row in rows),1)
   self.assertEqual(sum(row.get("attempt_id")==self.arg(calls[1],"--attempt-id") for row in rows),1)
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
 def test_an_owner_names_only_the_node_and_the_action(self):
  path=self.route(same_status="supported")
  explicit=self.run_chain(path)
  self.seed_predecessor_markers(path,"plan")
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--capability-mode","dev",
       "--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--action","dry-run"]
  clean={k:v for k,v in self.fixture_subprocess_env().items()
         if not k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**clean,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),"AGENT_DISPATCH_JOBS":str(self.jobs),
       "AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent","AGENT_OWNER_ROUTE_FILE":str(path)}
  implied=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual((implied.returncode,explicit.returncode),(0,0),implied.stdout+implied.stderr)
  self.assertIn("selected_hop=",implied.stdout)
  self.assertEqual([l for l in implied.stdout.splitlines() if l.startswith("selected_hop=")],
                   [l for l in explicit.stdout.splitlines() if l.startswith("selected_hop=")])
  # Without --route the owner's current route is used; this fixture owner row carries no
  # route binding, so the launch route in AGENT_OWNER_ROUTE_FILE is not trusted in its place.
  unbound=subprocess.run([a for a in cmd if a not in ("--route",str(path))],text=True,capture_output=True,env=env)
  self.assertEqual(unbound.returncode,64,unbound.stdout+unbound.stderr)
  self.assertIn("reason=owner-route-default-unresolved",unbound.stdout)
 def test_a_group_member_start_is_its_group_batch(self):
  seen=[]
  args=SimpleNamespace(route=Path("/r/route.json"),node="test-a",parent="owner",qa="standard",jobs=None,
                       prompt_file=None,reviewed_evidence=None)
  with mock.patch.object(F.subprocess,"run",side_effect=lambda argv,check: seen.append(argv) or SimpleNamespace(returncode=0)), \
       contextlib.redirect_stderr(io.StringIO()):
   self.assertEqual(F.start_parallel_group(args,"verify-pair"),0)
  self.assertEqual(seen[0][1:],[str(ROOT/"utilities/dispatch-batch.py"),"--route","/r/route.json","--parallel-group","verify-pair",
                                "--action","start","--parent","owner","--qa","standard"])
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
  result=self.run_observed_chain(path,{"att-prior000000"}); self.assertEqual(result.returncode,0,result.stdout+result.stderr); self.assertIn("selected_hop=cross-harness-headless",result.stdout); self.assertIn("skipped-prior-unchanged-failure",result.stdout)
  before=self.jobs.read_bytes()
  unknown=self.run_observed_chain(path,{"att-prior000000"},state="unverifiable")
  self.assertEqual(unknown.returncode,78,unknown.stdout+unknown.stderr)
  self.assertIn("prior-attempt-unverifiable",unknown.stdout)
  self.assertIn("child_spawned=0",unknown.stdout)
  self.assertEqual(self.jobs.read_bytes(),before)
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
  result=self.run_observed_chain(path,{"att-worker-fail","att-worker-dead"})
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertRegex(result.stdout,r"selected_hop=(same|cross)-harness-headless")
  self.assertNotIn("skipped-prior-unchanged-failure",result.stdout)
  before=self.jobs.read_bytes()
  unknown=self.run_observed_chain(path,{"att-worker-fail","att-worker-dead"},state="unverifiable")
  self.assertEqual(unknown.returncode,78,unknown.stdout+unknown.stderr)
  self.assertIn("prior-attempt-unverifiable",unknown.stdout)
  self.assertIn("child_spawned=0",unknown.stdout)
  self.assertEqual(self.jobs.read_bytes(),before)
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
  self.seed_predecessor_markers(path,node)
  self.seed_parent()
  if node=="plan-check":self.seed_plan_marker(json.loads(path.read_text()))
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node",node,"--slug","fallback-"+node,"--parent","owner","--capability-mode","dev","--jobs",str(self.jobs),"--"+action,*extra]
  clean={k:v for k,v in self.fixture_subprocess_env().items()
         if not k.startswith("AGENT_DISPATCH_CURRENT_")}
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
  self.assertNotIn("last_direct_failure_reason=",dry.stdout)
  self.assertIn('"attempted":false',dry.stdout)
  self.assertIn('"reason":"dispatch-evidence-parent-runtime-mismatch"',dry.stdout)
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
 def test_harness_weights_reach_the_stage_fallback_ranking(self):
  # OpenCode sits in the same band as the peers and the declared order would
  # put it first at equal headroom; `allocation.harness_weights` moves it back.
  node=self._depth_affinity_node(2)
  node["harness_policy"]={"primary":["claude","codex","opencode"],"relief":[],
                          "last_resort":[],"promote_relief_below":0}
  def rank(weights):
   route=self._depth_affinity_route(["opencode","claude","codex"],affinity=False)
   if weights:
    route["dispatch_allocation"]["harness_weights"]=weights
   with mock.patch.object(F,"_usage_states",return_value={
       "claude":"ok","codex":"ok","opencode":"ok"}), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
        "claude":80,"codex":80,"opencode":80}):
    _hops,context=F.ordered_fallback_hops(route,node,self.jobs)
   return context["rank"]
  self.assertEqual(rank(None)[0],"opencode")
  self.assertEqual(rank({"opencode":0.3})[0],"claude")
  self.assertEqual(rank({"opencode":0.3})[-1],"opencode")
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
 def _pinned_balanced_node(self):
  return {
   "kind":"pipeline-stage","harness_affinity":"claude",
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
 def _pinned_balanced_route(self,pin="claude",target="worker"):
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}}
  if pin:
   route["selection_pins"]={"contract_version":1,target:{"harness":pin,"model":None,"effort":None}}
  return route
 def test_a_sealed_worker_pin_stays_first_even_when_the_usage_gate_would_drop_it(self):
  # The same node and scores as the affinity test above, but the route seals `--pin worker=claude`:
  # claude is gated (5% headroom, not a limit) and still the first hop; the others keep their order.
  node=self._pinned_balanced_node()
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":5,"codex":5,"opencode":80}):
   hops,context=F.ordered_fallback_hops(self._pinned_balanced_route(),node,self.jobs)
   self.assertEqual(context["rank"][0],"claude")
   self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
   self.assertEqual(set(context["rank"]),{"claude","codex","opencode"})
   # no pin, or a pin for another target: today's order (the gated affinity does not lead)
   for route in (self._pinned_balanced_route(pin=None),
                 self._pinned_balanced_route(pin="claude",target="owner"),
                 self._pinned_balanced_route(pin="claude",target="frame")):
    with self.subTest(route=sorted(route.get("selection_pins",{}))):
     _hops,other=F.ordered_fallback_hops(route,node,self.jobs)
     self.assertEqual(other["rank"][0],"opencode")
 def test_a_sealed_worker_pin_is_not_moved_by_automatic_model_eligibility(self):
  # Provider/model eligibility filters automatic placement only. A sealed pin
  # keeps today's harness-level usage check; its launcher refuses only a
  # confirmed rejection and reports the provider's own error.
  node={**self._pinned_balanced_node(),"model_profile":"deep"}
  hold="limited(provider-model-unverified:openai-provider-model-unverified)"
  def states(jobs,profile=None,recheck=False):
   return {"claude":"ok","codex":"ok","opencode":hold if profile else "ok"}
  with mock.patch.object(F,"_usage_states",side_effect=states), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":50,"codex":50,"opencode":50}):
   hops,context=F.ordered_fallback_hops(self._pinned_balanced_route(pin="opencode"),node,self.jobs)
   self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
   self.assertEqual(context["limited"],[])
   _hops,unpinned=F.ordered_fallback_hops(self._pinned_balanced_route(pin=None),node,self.jobs)
   self.assertEqual(unpinned["limited"],["opencode"])
 def test_provider_model_hold_skips_an_opencode_candidate_at_launch(self):
  # The allocation rank reads the hold once; the launch loop reads it again
  # per candidate, so a compiled order without an allocation policy cannot
  # launch an OpenCode model that automatic placement may not select.
  import dispatch_capacity_evidence as CE
  hold="limited(provider-model-unverified:openai-provider-model-unverified)"
  with self.dispatch_env():
   path=self.route(tuples=[self.tuple("codex","unsupported"),self.tuple("claude","unsupported"),
                           self.tuple("opencode","supported")])
   route=json.loads(path.read_text(encoding="utf-8"))
   del route["dispatch_allocation"]
   route["route_hash"]=R.route_hash(route)
   route["route_id"]=R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
   path.write_text(json.dumps(route),encoding="utf-8")
   for held in (True,False):
    with self.subTest(held=held), mock.patch.object(
        CE,"model_hold",side_effect=lambda harness,profile,**_: hold if held and harness=="opencode" else None):
     out=io.StringIO()
     with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
      self.run_inline_main(path)
     trace=next(line for line in out.getvalue().splitlines() if line.startswith("attempt_trace="))
     self.assertEqual(f"opencode/conductor:skipped-usage-{hold}" in trace,held,trace)
 def test_a_sealed_worker_pin_that_is_at_its_limit_goes_to_the_tail_as_today(self):
  node=self._pinned_balanced_node()
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"limited","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":80,"codex":50,"opencode":50}):
   hops,context=F.ordered_fallback_hops(self._pinned_balanced_route(),node,self.jobs)
  self.assertNotIn("claude",context["rank"][:2])
  self.assertEqual(context["limited"],["claude"])
  skipped=[h["candidates"][0] for h in hops if h["candidates"] and h["candidates"][0].get("_allocation_skip")]
  self.assertEqual([(c["child_harness"],c["_allocation_skip"]) for c in skipped],[("claude","usage-limited")])
 def test_a_pinned_review_on_a_non_peer_harness_is_a_degraded_sole_gate_not_a_refusal(self):
  parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"workspace-write"}
  node={
   "kind":"review-worker","harness_affinity":"opencode",
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
  route={"dispatch_allocation":{"strategy":"balanced","window":30,"usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]},
   "owner_harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":35},
   "selection_pins":{"contract_version":1,"worker":{"harness":"opencode","model":None,"effort":None}},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":35}},
   ]}
  scores={"claude":80,"codex":80,"opencode":5}   # the pinned opencode is gated, the peers are not
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value=scores):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["sole_gate"],"degraded")
  # the same route without the pin keeps the quality-peer harness first
  del route["selection_pins"]
  node["harness_affinity"]="diverse"
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value=scores):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
  self.assertEqual(context["sole_gate"],"ok")
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
 def test_sd160_same_family_affinity_keeps_sole_gate_diagnostic(self):
  node=self._gate_node(affinity="opencode")
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
 def _counts(self,**counts):
  return {h:counts.get(h,0) for h in ("claude","codex","opencode")}
 def _sd160_review_context(self,*,codex=71,counts=(3,27),affinity="unspecified",
                           limited=(),kind="review-worker",legacy_cross=False):
  parent={"parent_harness":"codex","parent_transport":"headless",
          "parent_sandbox":"workspace-write"}
  node=self._gate_node(kind=kind,affinity=affinity)
  node.update(id="review",unit="research/plan-review",dispatch_depth=2,
              model_profile="balanced-deep",parent_cross_preference=legacy_cross)
  node["harness_policy"]={"primary":["claude","codex"],"relief":[],
                          "last_resort":[],"promote_relief_below":0}
  node["fallback_hops"]=[
   {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
    {**parent,"child_harness":"codex","status":"supported"}]},
   {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
    {**parent,"child_harness":"claude","status":"supported"}]},
  ]
  route=self._gate_route()
  route["dispatch_allocation"].update(strategy="balanced",usage_gate_used_percent=85,
    depth_affinity={"owner":"claude","worker":"codex"},
    depth_affinity_weight=0.65,usage_headroom_exponent=2)
  before=json.dumps((route,node),sort_keys=True)
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states(limited)), \
       mock.patch.object(F,"attempt_counts",return_value=self._counts(claude=counts[0],codex=counts[1])), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"claude":8.,"codex":float(codex),"opencode":0.}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(json.dumps((route,node),sort_keys=True),before)
  return route,node,hops,context

 def test_sd160_real_incidents_keep_capacity_order_for_independent_review(self):
  for codex,counts,affinity in [(71,(3,27),"unspecified"),(77,(9,21),"diverse")]:
   for kind,legacy in [("review-worker",False),("pipeline-stage",True)]:
    with self.subTest(codex=codex,kind=kind,legacy=legacy):
     _,_,hops,context=self._sd160_review_context(codex=codex,counts=counts,
       affinity=affinity,kind=kind,legacy_cross=legacy)
     self.assertEqual(context["rank"],["codex","claude"])
     self.assertEqual(hops[0]["fallback_hop"],"same-harness-headless")
     self.assertEqual(context["parent_cross"],"not-applicable")
     self.assertEqual(context["sole_gate"],"ok")

 def test_sd160_review_affinity_cannot_lift_gated_harness(self):
  _,_,_,context=self._sd160_review_context(affinity="claude")
  self.assertEqual(context["rank"],["codex","claude"])
  # Both candidates gated: preserve the maximum-headroom scarcity fallback.
  _,_,_,context=self._sd160_review_context(codex=12,affinity="claude")
  self.assertEqual(context["rank"],["codex","claude"])

 def test_sd160_review_hard_limit_still_excludes_roomy_harness(self):
  _,_,hops,context=self._sd160_review_context(limited=("codex",))
  self.assertEqual(context["rank"],["claude","codex"])
  self.assertEqual(hops[1]["candidates"][0]["_allocation_skip"],"usage-limited")
  self.assertEqual(context["sole_gate"],"ok")

 def test_sd160_final_fallback_receipt_records_actual_child_without_cross_degradation(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan-check")
  _,_,_,context=self._sd160_review_context()
  # Model a failed ranked head followed by a successful later candidate.
  # Historical context must not revive obsolete same-harness degradation.
  context.update(parent_cross="degraded",parent_cross_cause="affinity-pinned")
  args=SimpleNamespace(action="start",slug="actual-review",jobs=self.jobs,route=path)
  for harness,sole in [("claude","ok"),("codex","ok"),("opencode","degraded")]:
   with self.subTest(harness=harness):
    out=io.StringIO()
    with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(self.jobs)}), contextlib.redirect_stdout(out):
     F._emit_child_success(args,route,node,context,self.tuple(harness,"supported"),
       attempt_id="att-final-"+harness,fallback_hop="same-harness-headless" if harness=="codex" else "cross-harness-headless")
    receipt=self.cli_verdict(out.getvalue())
    self.assertEqual(receipt["parent_cross"],"not-applicable")
    self.assertEqual(receipt["parent_cross_cause"],"-")
    self.assertEqual(receipt["sole_gate"],sole)
    saved=[json.loads(line) for line in Path(receipt["allocation_ledger"]).read_text().splitlines()]
    actual=next(row for row in saved if row["attempt_id"]=="att-final-"+harness)
    self.assertEqual(actual["child_harness"],harness)
    self.assertEqual(actual["unit"],node["unit"])
    self.assertEqual(actual["capacity"],context["capacity"])
    self.assertEqual(actual["parent_cross"],"not-applicable")
    self.assertEqual(actual["sole_gate"],sole)
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  rows=[json.loads(line) for line in ledger.read_text().splitlines()]
  self.assertFalse([row for row in rows if row.get("reason")=="parent-cross-same-harness"])
  self.assertEqual(len([row for row in rows if row.get("reason")=="sole-gate-non-peer-harness"]),1)
 def test_sd160_existing_eligibility_quality_and_allocation_precedence(self):
  parent={"parent_harness":"opencode","parent_transport":"headless",
          "parent_sandbox":"workspace-write"}
  def ranked(node,route,*,counts=None,identity=parent,states=None):
   with mock.patch.object(F,"_usage_states",
                          return_value=states or self._usage_states()), \
        mock.patch.object(F,"attempt_counts",
                          return_value=counts or self._counts()):
    _hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=identity)
   return context

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
   unpinned=self._gate_node()
   unpinned["harness_policy"]=pinned["harness_policy"]
   context=ranked(unpinned,self._gate_route(),counts=lower,identity=None)
   self.assertNotEqual(context["rank"][0],"codex")

  with self.subTest(step=2,rule="hard eligibility"):
   node=self._gate_node(affinity="codex")
   node["fallback_hops"][1]["candidates"][0]["status"]="unsupported"
   context=ranked(node,self._gate_route())
   self.assertNotIn("codex",context["rank"])
   self.assertEqual(context["rank"][0],"claude")

  with self.subTest(step=3,rule="same-family affinity"):
   context=ranked(self._gate_node(affinity="opencode"),self._gate_route())
   self.assertEqual(context["rank"][0],"opencode")
   self.assertEqual(context["parent_cross"],"not-applicable")
   self.assertEqual(context["parent_cross_cause"],"-")

  with self.subTest(step=4,rule="quality peer over non-peer"):
   context=ranked(self._gate_node(),self._gate_route(),
                  counts=self._counts(claude=5,codex=6,opencode=0))
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   self.assertEqual(context["parent_cross"],"not-applicable")

  with self.subTest(step=5,rule="band over least-recent"):
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
   node=self._gate_node()
   context=ranked(node,self._gate_route(),
                  counts=self._counts(claude=3,codex=1,opencode=0),
                  identity=None)
   self.assertEqual(context["rank"],["opencode","codex","claude"])

  with self.subTest(step=7,rule="declared order tie-break"):
   route=self._gate_route()
   context=ranked(node,route,identity=None)
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   route=self._gate_route()
   route["dispatch_allocation"]["harness_order"]=["opencode","codex","claude"]
   context=ranked(node,route,identity=None)
   self.assertEqual(context["rank"],["opencode","codex","claude"])
 def test_sd160_quality_peer_order_preserves_allocation_order(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(selected,["claude","codex","opencode"])
  self.assertEqual([h for h in selected if h in {"claude","codex"}],
                   ["claude","codex"])
  self.assertEqual([h for h in selected if h not in {"claude","codex"}],
                   ["opencode"])
 def test_sd160_affinity_head_is_normal_on_owner_family(self):
  node=self._gate_node(affinity="opencode")
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
 def test_sd160_cross_usage_limited_does_not_degrade_parent_family(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",
       return_value=self._usage_states(limited=("claude","codex"))):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
 def test_sd160_same_harness_receipt_and_allocation_ledger_at_the_cli(self):
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
        "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("codex","supported"),self.tuple("claude","unsupported")],
            "native_subagent":[{"harness":"codex","transport":"headless",
                                "execution_surface":"codex-native-subagent",
                                "registered_worker":False,"status":"unsupported",
                                "check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,
     signals=["shared-contract"],transport="headless",tracking="tracked",
     tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"evidence-pair-route.json"
  path.write_text(json.dumps(route),encoding="utf-8")
  self.seed_predecessor_markers(path,"plan-check")
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),
       "--route",str(path),"--node","plan-check","--slug","fb-evidence-pair",
       "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
       "--model-role","fast reviewer","--jobs",str(self.jobs),"--dry-run"]
  env={**self.fixture_subprocess_env(),"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
       "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",
       "AGENT_DISPATCH_CURRENT_HARNESS":"codex",
       "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
       "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=self.cli_verdict(result.stdout)
  self.assertEqual(receipt["child_harness"],"codex")
  self.assertEqual(receipt["parent_cross"],"not-applicable")
  self.assertEqual(receipt["parent_cross_cause"],"-")
  self.assertEqual(receipt["sole_gate"],"ok")
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  rows=[json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
  self.assertFalse([row for row in rows if row.get("reason")=="parent-cross-same-harness"])
  allocation=Path(self.tmp.name)/"allocation"/f"{route['route_id']}.jsonl"
  saved=[json.loads(line) for line in allocation.read_text().splitlines()]
  self.assertEqual(saved[-1]["child_harness"],"codex")
  self.assertEqual(saved[-1]["parent_cross"],"not-applicable")
  self.assertEqual(saved[-1]["sole_gate"],"ok")
 def test_ac12_sole_gate_receipt_and_ledger_evidence_pair_at_the_cli(self):
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
        "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("opencode","supported"),
                      self.tuple("codex","unsupported"),
                      self.tuple("claude","unsupported")],
            "native_subagent":[{"harness":"codex","transport":"headless",
                                "execution_surface":"codex-native-subagent",
                                "registered_worker":False,"status":"unsupported",
                                "check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,
     signals=["shared-contract"],transport="headless",tracking="tracked",
     tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"sole-gate-route.json"
  path.write_text(json.dumps(route),encoding="utf-8")
  self.seed_predecessor_markers(path,"plan-check")
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),
       "--route",str(path),"--node","plan-check","--slug","fb-sole-gate",
       "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
       "--model-role","fast reviewer","--jobs",str(self.jobs),"--dry-run"]
  env={**self.fixture_subprocess_env(),"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
       "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",
       "AGENT_DISPATCH_CURRENT_HARNESS":"codex",
       "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
       "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=self.cli_verdict(result.stdout)
  self.assertEqual(receipt["sole_gate"],"degraded")
  self.assertEqual(receipt["child_harness"],"opencode")
  self.assertEqual(receipt["parent_cross"],"not-applicable")
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
  self.assertIsNone(F._persist_parent_cross_ledger(None, route, node, None))
 def test_ac18_non_target_node_keeps_six_repeat_rotation(self):
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
 def test_sd160_opencode_owner_retains_quality_peer_sole_gate(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["sole_gate"],"ok")
  self.assertEqual(context["quality_peer_families"],["claude","codex"])
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
 def test_ac100b_sole_gate_degraded_when_no_quality_peer_eligible(self):
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
 def test_sd160_sole_gate_reorder_can_choose_owner_family_normally(self):
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
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
  self.assertEqual(context["sole_gate"],"ok")
 def test_g3_verdicts_recomputed_for_actual_launched_child(self):
  context={
   "parent_cross":"ok","parent_cross_cause":"-","sole_gate":"ok",
   "affinity":None,"rank":["claude","codex"],
   "eligible":["claude","codex"],"limited":[],
   "owner_family":"claude","quality_peer_set":frozenset({"claude","codex"}),
  }
  reopened=F._recompute_verdicts_for_child(context,"opencode")
  self.assertEqual(reopened["parent_cross"],"not-applicable")
  self.assertEqual(reopened["sole_gate"],"degraded")
  same_family=F._recompute_verdicts_for_child(context,"claude")
  self.assertEqual(same_family["parent_cross"],"not-applicable")
  self.assertEqual(same_family["sole_gate"],"ok")
  self.assertEqual(same_family["parent_cross_cause"],"-")
  cross=F._recompute_verdicts_for_child(context,"codex")
  self.assertEqual(cross["parent_cross"],"not-applicable")
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
  self.assertNotIn("last_direct_failure_reason=",dry.stdout)
  self.assertIn('"attempted":false',dry.stdout)
  self.assertIn('"reason":"dispatch-evidence-parent-runtime-mismatch"',dry.stdout)
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

 # SD-154/B-2 defect #2 -----------------------------------------------------
 def test_a_sd154_2_route_state_refusal_does_not_descend_to_inline(self):
  """A wrapper that reports a route-state reason (13.59.3 rule 6) stops with
  `child_spawned=0` -- it never falls to `selected_hop=inline`/
  `runtime-unavailable`, the luna observation's actual mis-classification.

  Uses the revised-evidence member of the set (not the gate's own missing-
  dependency reason, which `dispatch_completion_marker.test.py`'s static
  guardian keeps out of every file but `dispatch_contract.py` and the
  adapters' generic relay).
  """
  for stub_reason in (
      "completion-evidence-revised-unrecorded",
      "completion-evidence-superseded",
  ):
   with self.subTest(reason=stub_reason):
    self.assertIn(stub_reason,F.ROUTE_STATE_REFUSAL_REASONS)
    path=self.route()
    real_run=subprocess.run
    def fake_run(cmd,*args,**kwargs):
     if any("dispatch-headless.py" in str(part) for part in cmd):
      return SimpleNamespace(
       returncode=65,
       stdout=f"check=failed\nreason={stub_reason}\ndetail=plan\nnext_action=repair-route-state\n",
       stderr="",
      )
     return real_run(cmd,*args,**kwargs)
    buf=io.StringIO()
    with self.dispatch_env():
     with mock.patch.object(F.subprocess,"run",side_effect=fake_run):
      with contextlib.redirect_stdout(buf):
       try:
        code,_observation=self.run_inline(path)
       except SystemExit as exc:
        code=exc.code
    output=buf.getvalue()
    fields=F.output_fields(output)
    self.assertEqual(code,65,output)
    self.assertEqual(fields.get("reason"),stub_reason,output)
    self.assertEqual(fields.get("child_spawned"),"0",output)
    self.assertEqual(fields.get("next_action"),"repair-route-state",output)
    self.assertNotIn("selected_hop=inline",output)
    self.assertNotIn("runtime-unavailable",output)

 # M1's real (not stubbed) `completion_marker_gate` half lives in
 # `dispatch_completion_marker.test.py::CompletionMarkerTest::
 # test_a_sd154_2_real_gate_reports_next_action_for_route_state_refusal`,
 # which runs the real wrapper `--start` subprocess -- this file's own
 # helpers drive the wrapper as `--dry-run` with real predecessor markers,
 # so they share its read-only currency/readiness gate. This class's test
 # above proves the OTHER half of the same regression: a fallback chain that
 # receives that exact reason from a wrapper never descends to inline.

 # --- 5th Codex run fixes (items 3/1/7), on the `plan` stage of a standard route.
 # The wrapper launch is replaced at its one seam: `subprocess.run` for a
 # detached start, `run_forwarding_termination` for a foreground-scoped one.
 PARENT=SimpleNamespace(slug="fallback-plan",parent="owner",parent_attempt_id="att-fallback-parent")
 def standard_route(self):
  path=self.route(same_status="supported",intensity="standard")
  route=json.loads(path.read_text())
  return path,route,next(n for n in route["nodes"] if n["id"]=="plan")
 def compiled_order(self):
  # The stage's compiled hop order, without the usage/capacity ranking. `recheck_seen`
  # records whether each ranking was a launch decision (--start) or not (--dry-run).
  self.recheck_seen=getattr(self,"recheck_seen",[])
  def order(route,node,jobs,parent_identity=None,recheck=False):
   self.recheck_seen.append(recheck)
   return F.apply_worker_pin(route,list(node["fallback_hops"]),parent_identity),None
  return mock.patch.object(F,"ordered_fallback_hops",side_effect=order)
 def start(self,path,*extra,lifecycle=None,patches=()):
  self.seed_predecessor_markers(path,"plan"); self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan","--slug","fallback-plan",
        "--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author",
        "--model-role","deep maker","--jobs",str(self.jobs),"--start",*extra]
  out=io.StringIO(); err=io.StringIO()
  with contextlib.ExitStack() as stack:
   stack.enter_context(mock.patch.object(sys,"argv",argv))
   stack.enter_context(mock.patch.object(F,"select_launch_lifecycle",return_value=lifecycle or F.DETACHED))
   stack.enter_context(mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})))
   for patch in patches: stack.enter_context(patch)
   stack.enter_context(contextlib.redirect_stdout(out)); stack.enter_context(contextlib.redirect_stderr(err))
   code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
  return code,out.getvalue(),err.getvalue()
 def wrapper_run(self,calls,receipt,code=0):
  real_run=subprocess.run
  def run(cmd,**kwargs):
   if any(str(part).endswith("/bin/dispatch-headless.py") for part in cmd):
    calls.append(cmd)
    return SimpleNamespace(returncode=code,stdout=receipt(cmd) if callable(receipt) else receipt,stderr="")
   return real_run(cmd,**kwargs)
  return mock.patch.object(F.subprocess,"run",side_effect=run)
 @staticmethod
 def arg(cmd,flag):
  return cmd[cmd.index(flag)+1] if flag in cmd else None
 def forwarded(self,calls,**result):
  ForwardedRun=sys.modules["dispatch_lifecycle"].ForwardedRun
  def run(cmd,**kwargs):
   calls.append((cmd,kwargs))
   return ForwardedRun(**{"returncode":0,"stdout":"","stderr":"",**result})
  return mock.patch.object(F,"run_forwarding_termination",side_effect=run)

 # --- item 3: a stopped foreground call stops the chain -------------------
 def test_a_stopped_foreground_start_reports_interrupted_and_tries_no_next_hop(self):
  import signal
  receipt=("check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"
           "worker_exit=-2\nworker_failure=interrupted\n")
  incomplete_output=receipt+"INTERRUPTED_RAW_SENTINEL argv=INTERRUPTED_ARGV_SENTINEL\n"
  cases=(
   (dict(stdout=receipt,received_signal=signal.SIGINT),130,False),
   (dict(stdout=incomplete_output,received_signal=signal.SIGTERM,cleanup_incomplete=True,returncode=-9),143,True),
   # the wrapper alone was stopped (this call saw no signal)
   (dict(stdout=receipt),130,False),
  )
  for result,expected_code,incomplete in cases:
   with self.subTest(result=result), self.dispatch_env():
    path,_route,_node=self.standard_route()
    calls=[]
    code,out,err=self.start(path,lifecycle=F.FOREGROUND_SCOPED,
                            patches=(self.compiled_order(),self.forwarded(calls,**result)))
    self.assertEqual(code,expected_code,out+err)
    self.assertEqual(len(calls),1,"a stopped call must not try the next hop")
    cmd,kwargs=calls[0]
    self.assertTrue(kwargs["capture"])
    self.assertEqual(self.arg(cmd,"--launch-lifecycle"),"foreground-scoped")
    own=self.cli_verdict(out)
    self.assertEqual((own["check"],own["reason"],own["interrupted"]),("failed","interrupted","1"))
    self.assertEqual(own.get("cleanup"),"incomplete" if incomplete else None)
    self.assertIn("worker_failure=interrupted",out.splitlines())
    if incomplete:
     self.assertNotIn("INTERRUPTED_RAW_SENTINEL",out)
     self.assertNotIn("INTERRUPTED_ARGV_SENTINEL",out)
    self.assertNotIn("selected_hop",out)
    self.assertEqual(err.splitlines()[0],F.FOREGROUND_NOTICE)
 def test_incomplete_forwarded_output_preserves_receipt_without_settling_or_retrying(self):
  receipt=("check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"
           "terminal_verdict=PASS\nterminal_note=completed\n")
  raw_stdout=receipt+"PROMPT_RAW_SENTINEL argv=ARGV_RAW_SENTINEL env=ENV_RAW_SENTINEL\n"*2048
  raw_stderr="STDERR_RAW_SENTINEL argv=STDERR_ARGV_SENTINEL env=STDERR_ENV_SENTINEL\n"*2048
  with self.dispatch_env():
   path,_route,_node=self.standard_route()
   calls=[]
   no_watch=mock.patch.object(F,"watch_launched_attempt",side_effect=AssertionError("must not settle"))
   code,out,err=self.start(
    path,lifecycle=F.FOREGROUND_SCOPED,
    patches=(self.compiled_order(),self.forwarded(
     calls,stdout=raw_stdout,stderr=raw_stderr,cleanup_incomplete=True),no_watch))
  fields=self.cli_verdict(out)
  self.assertEqual(code,76,out+err)
  self.assertEqual(fields.get("reason"),"foreground-output-incomplete",out)
  self.assertEqual(fields.get("child_spawned"),"unknown",out)
  self.assertEqual(fields.get("registered"),"unknown",out)
  self.assertEqual(fields.get("started"),"unknown",out)
  self.assertEqual(fields.get("terminal_verdict"),"unknown",out)
  self.assertEqual(fields.get("cleanup"),"incomplete",out)
  self.assertEqual(fields.get("wrapper_returncode"),"0",out)
  self.assertNotIn("wrapper_stdout_json",fields)
  self.assertNotIn("wrapper_stderr_json",fields)
  for sentinel in ("PROMPT_RAW_SENTINEL","ARGV_RAW_SENTINEL","ENV_RAW_SENTINEL",
                   "STDERR_RAW_SENTINEL","STDERR_ARGV_SENTINEL","STDERR_ENV_SENTINEL"):
   self.assertNotIn(sentinel,out)
  self.assertLess(len(out),4096)
  self.assertNotIn("selected_hop=",out)
  self.assertEqual(len(calls),1)
  self.assertEqual(err.splitlines()[0],F.FOREGROUND_NOTICE)

 def test_incomplete_forwarded_output_retains_exact_terminal_pass_and_fail(self):
  cases=(
   ("completed-marker","pass","PASS"),
   ("dead-worker-fail","fail","FAIL"),
  )
  for note,failure_class,verdict in cases:
   with self.subTest(verdict=verdict),self.dispatch_env():
    self.jobs.write_text("",encoding="utf-8")
    path,route,node=self.standard_route()
    calls=[]
    ForwardedRun=sys.modules["dispatch_lifecycle"].ForwardedRun
    def run(cmd,**kwargs):
     calls.append((cmd,kwargs))
     aid=self.arg(cmd,"--attempt-id")
     metadata=(
      f"attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
      f"route_id={route['route_id']},route_node={node['id']},attempt_id={aid},"
      f"worker_type=stage,launch_claimed=1,launch_fence=registry-v1,launch_started=1,"
      f"note={note},failure_class={failure_class}")
     with self.jobs.open("a",encoding="utf-8") as handle:
      handle.write(f"now\tdone\t{self.repo}\t{self.repo}\tfixture\t{metadata}\n")
     receipt=(f"check=ok\nstatus=start\nattempt_id={aid}\nregistered=1\nstarted=1\n"
              f"child_spawned=1\nterminal_verdict={verdict}\n")
     return ForwardedRun(returncode=0,stdout=receipt,stderr="fixture-stderr\n",
                         cleanup_incomplete=True)
    no_watch=mock.patch.object(F,"watch_launched_attempt",side_effect=AssertionError("must not settle"))
    code,out,err=self.start(
     path,lifecycle=F.FOREGROUND_SCOPED,
     patches=(self.compiled_order(),mock.patch.object(F,"run_forwarding_termination",side_effect=run),no_watch))
    fields=self.cli_verdict(out)
    self.assertEqual(code,76,out+err)
    self.assertEqual(fields.get("reason"),"foreground-output-incomplete",out)
    self.assertEqual(fields.get("cleanup"),"incomplete",out)
    self.assertEqual(fields.get("registered"),"1",out)
    self.assertEqual(fields.get("started"),"1",out)
    self.assertEqual(fields.get("child_spawned"),"1",out)
    self.assertEqual(fields.get("route_id"),route["route_id"],out)
    self.assertEqual(fields.get("route_node"),node["id"],out)
    self.assertEqual(fields.get("terminal_verdict"),verdict,out)
    self.assertEqual(fields.get("terminal_note"),note,out)
    self.assertEqual(fields.get("attempt_id"),self.arg(calls[0][0],"--attempt-id"),out)
    self.assertEqual(fields.get("wrapper_returncode"),"0",out)
    self.assertNotIn("wrapper_stdout_json",fields)
    self.assertNotIn("wrapper_stderr_json",fields)
    self.assertNotIn("selected_hop=",out)
    self.assertEqual(len(calls),1)

 def test_incomplete_forwarded_output_retains_proven_launch_without_terminal_row(self):
  with self.dispatch_env():
   path,route,node=self.standard_route()
   calls=[]
   ForwardedRun=sys.modules["dispatch_lifecycle"].ForwardedRun
   def run(cmd,**kwargs):
    calls.append((cmd,kwargs))
    aid=self.arg(cmd,"--attempt-id")
    metadata=(f"attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
              f"route_id={route['route_id']},route_node={node['id']},attempt_id={aid},"
              "worker_type=stage,launch_claimed=1,launch_fence=registry-v1,launch_started=1")
    with self.jobs.open("a",encoding="utf-8") as handle:
     handle.write(f"now\trunning\t{self.repo}\t{self.repo}\tfixture\t{metadata}\n")
    return ForwardedRun(returncode=0,stdout=(f"check=ok\nstatus=start\nattempt_id={aid}\n"
                         "registered=1\nstarted=1\nchild_spawned=1\n"),
                        stderr="",cleanup_incomplete=True)
   no_watch=mock.patch.object(F,"watch_launched_attempt",side_effect=AssertionError("must not settle"))
   code,out,err=self.start(
    path,lifecycle=F.FOREGROUND_SCOPED,
    patches=(self.compiled_order(),mock.patch.object(F,"run_forwarding_termination",side_effect=run),no_watch))
  fields=self.cli_verdict(out)
  self.assertEqual(code,76,out+err)
  self.assertEqual((fields.get("registered"),fields.get("started"),fields.get("child_spawned")),
                   ("1","1","1"),out)
  self.assertEqual(fields.get("terminal_verdict"),"unknown",out)
  self.assertEqual(fields.get("cleanup"),"incomplete",out)
  self.assertEqual(fields.get("attempt_id"),self.arg(calls[0][0],"--attempt-id"),out)
  self.assertEqual(len(calls),1)

 def test_incomplete_forwarded_output_does_not_bind_malformed_or_mismatched_receipts(self):
  cases=(
   "check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\nterminal_verdict=PASS\n",
   "check=ok\nstatus=start\nattempt_id=att-stale\nroute_id=rt-foreign\nroute_node=plan\n"
   "registered=1\nstarted=1\nchild_spawned=1\nterminal_verdict=PASS\n",
  )
  for receipt in cases:
   with self.subTest(receipt=receipt),self.dispatch_env():
    path,_route,_node=self.standard_route()
    calls=[]
    no_watch=mock.patch.object(F,"watch_launched_attempt",side_effect=AssertionError("must not settle"))
    code,out,err=self.start(
     path,lifecycle=F.FOREGROUND_SCOPED,
     patches=(self.compiled_order(),self.forwarded(
      calls,stdout=receipt,stderr="fixture-stderr\n",cleanup_incomplete=True),no_watch))
   fields=self.cli_verdict(out)
   self.assertEqual(code,76,out+err)
   self.assertEqual((fields.get("registered"),fields.get("started"),fields.get("child_spawned")),
                    ("unknown","unknown","unknown"),out)
   self.assertEqual(fields.get("terminal_verdict"),"unknown",out)
   self.assertEqual(fields.get("cleanup"),"incomplete",out)
   self.assertNotEqual(fields.get("attempt_id"),"att-stale")
   self.assertEqual(len(calls),1)

 def test_a_foreground_start_prints_the_notice_first_and_the_parent_notice_second(self):
  ok=("check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"
      "worker_exit=0\nworker_failure=-\n")
  with self.dispatch_env():
   path,_route,_node=self.standard_route()
   calls=[]
   code,out,err=self.start(path,"--parent","wrong-owner",lifecycle=F.FOREGROUND_SCOPED,
                           patches=(self.compiled_order(),self.forwarded(calls,stdout=ok)))
  self.assertEqual(code,0,out+err)
  self.assertEqual(err.splitlines()[:2],[
   F.FOREGROUND_NOTICE,
   "notice: --parent wrong-owner is not this session's name; using owner (from AGENT_DISPATCH_SELF_SLUG).",
  ])
  self.assertEqual(self.arg(calls[0][0],"--parent"),"owner")
 def test_a_start_ranks_as_a_launch_decision_that_may_recheck_a_held_login(self):
  ok=("check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"
      "worker_exit=0\nworker_failure=-\n")
  with self.dispatch_env():
   path,_route,_node=self.standard_route()
   calls=[]
   code,out,err=self.start(path,lifecycle=F.FOREGROUND_SCOPED,
                           patches=(self.compiled_order(),self.forwarded(calls,stdout=ok)))
  self.assertEqual(code,0,out+err)
  self.assertEqual(self.recheck_seen,[True])
 def test_a_detached_start_keeps_the_plain_launch_and_no_notice(self):
  with self.dispatch_env():
   path,_route,_node=self.standard_route()
   calls=[]
   forwarded=mock.patch.object(F,"run_forwarding_termination",side_effect=AssertionError("detached"))
   code,out,err=self.start(path,patches=(self.compiled_order(),forwarded,self.wrapper_run(
    calls,"check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n")))
  self.assertEqual(code,0,out+err)
  self.assertEqual(len(calls),1)
  self.assertNotIn(F.FOREGROUND_NOTICE,err)
 def test_dry_run_never_prints_the_foreground_notice(self):
  with self.dispatch_env():
   path,_route,_node=self.standard_route()
   self.seed_predecessor_markers(path,"plan"); self.seed_parent()
   argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan","--slug","fallback-plan",
         "--parent","owner","--jobs",str(self.jobs),"--dry-run"]
   err=io.StringIO()
   with mock.patch.object(sys,"argv",argv), mock.patch.object(F,"select_launch_lifecycle",return_value=F.FOREGROUND_SCOPED), \
        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
    F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
  self.assertNotIn(F.FOREGROUND_NOTICE,err.getvalue())

 # --- item 1: a provably dead claimed row is settled before the start -------
 def open_claimed_row(self,route,node,harness="codex"):
  old=F.attempt_identity(self.PARENT,route,node,{"child_harness":harness},1)
  self.seed_parent()
  with self.jobs.open("a",encoding="utf-8") as fh:
   fh.write(f"2026-10-02T00:00:00Z\topen\t{self.repo}\t{self.repo}\tfallback-plan\t"
            "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
            f"route_id={route['route_id']},route_node=plan,parent_attempt_id=att-fallback-parent,"
            f"pid_scope=namespace-local,launch_claimed=1,attempt_id={old}\n")
  return old
 def test_a_dead_claimed_row_is_closed_and_the_same_tuple_starts_a_new_attempt(self):
  with self.dispatch_env():
   path,route,node=self.standard_route()
   old=self.open_claimed_row(route,node)
   settled=[]
   def reconcile(jobs,row):
    settled.append(row.attempt_id)
    text=jobs.read_text(encoding="utf-8").replace(
     f"launch_claimed=1,attempt_id={old}",f"launch_claimed=1,attempt_id={old},note=dead-namespace-absent")
    jobs.write_text(text.replace(f"\topen\t{self.repo}\t{self.repo}\tfallback-plan",
                                 f"\tdone\t{self.repo}\t{self.repo}\tfallback-plan"),encoding="utf-8")
    return {"attempt_id":row.attempt_id,"closed":True,"reason":"dead-namespace-absent"}
   cleanup=[]; calls=[]
   code,out,err=self.start(path,patches=(
    self.compiled_order(),
    mock.patch.object(F,"attempt_process_quiescence",return_value=SimpleNamespace(state="quiescent",reason="namespace-extinct")),
    mock.patch("dispatch_completion_join.exact_attempt_row",side_effect=lambda jobs,attempt:SimpleNamespace(attempt_id=attempt)),
    mock.patch("dispatch_completion_join.reconcile_exact_dead_attempt",side_effect=reconcile),
    mock.patch("dispatch_completion_join.resolve_attempt_cleanup",side_effect=lambda jobs,attempt,apply=False:cleanup.append((attempt,apply))),
    self.wrapper_run(calls,"check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"),
   ))
  self.assertEqual(code,0,out+err)
  self.assertEqual(settled,[old]); self.assertEqual(cleanup,[(old,True)])
  self.assertEqual(len(calls),1)
  self.assertEqual(self.arg(calls[0],"--automatic-retry-of"),old)
  self.assertEqual(self.arg(calls[0],"--attempt-id"),F.retry_attempt_identity(old))
  self.assertNotEqual(F.retry_attempt_identity(old),old)
  self.assertEqual(self.arg(calls[0],"--child-harness") or self.arg(calls[0],"--parent-harness"),"codex")
 def test_a_live_claimed_row_is_left_alone_and_reused(self):
  with self.dispatch_env():
   path,route,node=self.standard_route()
   old=self.open_claimed_row(route,node)
   calls=[]
   code,out,err=self.start(path,patches=(
    self.compiled_order(),
    mock.patch.object(F,"attempt_process_quiescence",return_value=SimpleNamespace(state="live",reason="process-live")),
    mock.patch("dispatch_completion_join.reconcile_exact_dead_attempt",side_effect=AssertionError("live row closed")),
    self.wrapper_run(calls,"check=ok\nstatus=start\nduplicate_attempt=1\nlaunch_state=existing-active\n"
                           "registered=0\nstarted=0\nchild_spawned=0\n"),
   ))
  self.assertEqual(code,0,out+err)
  self.assertEqual(self.arg(calls[0],"--attempt-id"),old)
  self.assertIsNone(self.arg(calls[0],"--automatic-retry-of"))
  self.assertIn("open",self.jobs.read_text())
 def test_an_unsettled_dead_row_starts_nothing_new(self):
  # The exact reconcile could not close it (it turned live, or proof failed):
  # the start goes on exactly as before and names the same claimed attempt.
  with self.dispatch_env():
   path,route,node=self.standard_route()
   old=self.open_claimed_row(route,node)
   calls=[]
   code,out,err=self.start(path,patches=(
    self.compiled_order(),
    mock.patch.object(F,"attempt_process_quiescence",return_value=SimpleNamespace(state="quiescent",reason="namespace-extinct")),
    mock.patch("dispatch_completion_join.exact_attempt_row",side_effect=lambda jobs,attempt:SimpleNamespace(attempt_id=attempt)),
    mock.patch("dispatch_completion_join.reconcile_exact_dead_attempt",return_value={"closed":False,"reason":"still-live"}),
    mock.patch("dispatch_completion_join.resolve_attempt_cleanup",side_effect=AssertionError("not closed")),
    self.wrapper_run(calls,"check=ok\nstatus=start\nduplicate_attempt=1\nlaunch_state=existing-dead\n"
                           "registered=0\nstarted=0\nchild_spawned=0\n"),
   ))
  self.assertEqual(self.arg(calls[0],"--attempt-id"),old)
  self.assertIsNone(self.arg(calls[0],"--automatic-retry-of"))
 def test_timeout_retry_preserves_progress_predecessor(self):
  # A timeout is a runtime failure, not a spent launch tuple: the next
  # start retries the same tuple as a new attempt bound to the stopped one.
  with self.dispatch_env():
   path,route,node=self.standard_route()
   old=F.attempt_identity(self.PARENT,route,node,{"child_harness":"codex"},1)
   self.seed_parent()
   with self.jobs.open("a",encoding="utf-8") as fh:
    fh.write(f"2026-10-02T00:00:00Z\tdone\t{self.repo}\t{self.repo}\tfallback-plan\t"
             "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
             f"route_id={route['route_id']},route_node=plan,parent_attempt_id=att-fallback-parent,"
             "parent_harness=codex,parent_transport=headless,parent_sandbox=workspace-write,"
             "child_harness=codex,launch_authority=conductor,"
             f"launch_claimed=1,attempt_id={old},note=dead-timeout,failure_class=runtime,"
             "reconcile_reason=timeout\n")
   self.assertEqual(F.registry_failures(self.jobs,route["route_id"],"plan"),{})
   calls=[]
   code,out,err=self.start(path,patches=(
    self.compiled_order(),
    self.wrapper_run(calls,"check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"),
   ))
  self.assertEqual(code,0,out+err)
  self.assertEqual(self.arg(calls[0],"--attempt-id"),F.retry_attempt_identity(old))
  self.assertEqual(self.arg(calls[0],"--automatic-retry-of"),old)
  # A retry that was `--register`ed first keeps the same successor at `--start`.
  successor=F.retry_attempt_identity(old)
  with self.dispatch_env():
   with self.jobs.open("a",encoding="utf-8") as fh:
    fh.write(f"2026-10-02T00:00:01Z\topen\t{self.repo}\t{self.repo}\tfallback-plan\t"
             "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
             f"route_id={route['route_id']},route_node=plan,parent_attempt_id=att-fallback-parent,"
             f"automatic_retry_of={old},launch_claimed=0,attempt_id={successor}\n")
   calls=[]
   code,out,err=self.start(path,patches=(
    self.compiled_order(),
    self.wrapper_run(calls,"check=ok\nstatus=start\nregistered=1\nstarted=1\nchild_spawned=1\n"),
   ))
  self.assertEqual(code,0,out+err)
  self.assertEqual(self.arg(calls[0],"--attempt-id"),successor)
  self.assertEqual(self.arg(calls[0],"--automatic-retry-of"),old)

 # --- item 7: a pinned step is never moved to another tool -----------------
 def pin_node(self,parent="codex"):
  row=lambda child:{"parent_harness":parent,"parent_transport":"headless","parent_sandbox":"workspace-write",
                    "child_harness":child,"launch_authority":"conductor","status":"supported"}
  return {"kind":"pipeline-stage","harness_affinity":"unspecified",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],"last_resort":[],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[row(parent)]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[row(h) for h in ("claude","codex","opencode") if h!=parent]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[{"harness":parent,"status":"supported"}]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ]}
 def pin_route(self,pin,allocation):
  route={}
  if allocation:
   route["dispatch_allocation"]={"strategy":"balanced","window":30,"usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}
  if pin:
   route["selection_pins"]={"contract_version":1,"worker":{"harness":pin,"model":None,"effort":None}}
  return route
 def ordered(self,route,node,parent_identity=None):
  with mock.patch.object(F,"_usage_states",return_value={"claude":"ok","codex":"ok","opencode":"ok"}), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"claude":50,"codex":50,"opencode":50}):
   return F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent_identity)
 @staticmethod
 def launchable(hops):
  return [row["child_harness"] for hop in hops if hop["fallback_hop"] in F.HEADLESS_HOPS
          for row in hop["candidates"] if not row.get("_worker_pin_skip")]
 @staticmethod
 def skipped(hops):
  return ([row["child_harness"] for hop in hops if hop["fallback_hop"] in F.HEADLESS_HOPS
           for row in hop["candidates"] if row.get("_worker_pin_skip")],
          [hop["fallback_hop"] for hop in hops if hop.get("_worker_pin_skip")])
 def test_a_worker_pin_keeps_only_its_harness_on_both_ordering_paths(self):
  for allocation in (False,True):
   with self.subTest(allocation=allocation):
    hops,context=self.ordered(self.pin_route("opencode",allocation),self.pin_node("codex"))
    self.assertEqual(self.launchable(hops),["opencode"])
    self.assertEqual(sorted(self.skipped(hops)[0]),["claude","codex"])
    # the owner (codex) is not the pinned tool: its native/inline hops go too
    self.assertEqual(self.skipped(hops)[1],["native-subagent","inline"])
    self.assertEqual(context is None,not allocation)
    # an owner on the pinned harness keeps its native-subagent and inline hops
    hops,_context=self.ordered(self.pin_route("codex",allocation),self.pin_node("codex"),
                               parent_identity={"parent_harness":"codex"})
    self.assertEqual(self.launchable(hops),["codex"])
    self.assertEqual(self.skipped(hops)[1],[])
 def test_no_worker_pin_keeps_the_existing_order_on_both_paths(self):
  for allocation in (False,True):
   with self.subTest(allocation=allocation):
    route=self.pin_route(None,allocation); node=self.pin_node("codex")
    hops,_context=self.ordered(route,node)
    self.assertEqual(self.skipped(hops),([],[]))
    if not allocation:
     self.assertEqual(hops,node["fallback_hops"])
    self.assertEqual(sorted(self.launchable(hops)),["claude","codex","opencode"])
    # a pin for another target is not a worker pin
    route["selection_pins"]={"contract_version":1,"owner":{"harness":"claude","model":None,"effort":None}}
    self.assertEqual(self.skipped(self.ordered(route,node)[0]),([],[]))
 def test_a_failed_pinned_harness_stops_with_worker_pin_unavailable(self):
  with self.dispatch_env():
   path,route,_node=self.standard_route()
   calls=[]
   code,out,err=self.start(path,patches=(
    mock.patch.object(F,"sealed_pin_harness",side_effect=lambda route,worker_type=None:"claude"),
    mock.patch.object(F,"_usage_states",return_value={"claude":"ok","codex":"ok","opencode":"ok"}),
    mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"claude":50,"codex":50,"opencode":50}),
    self.wrapper_run(calls,"check=failed\nreason=launch-error\nchild_spawned=0\n",code=73),
   ))
  self.assertEqual(code,79,out+err)
  self.assertEqual([self.arg(cmd,"--fallback-ordinal") and cmd[1].split("/")[-3] for cmd in calls],["claude"])
  own=self.cli_verdict(out)
  self.assertEqual(own["reason"],"worker-pin-unavailable")
  self.assertEqual(own["detail"],"the worker is pinned to claude; claude cannot take this step now "
                                 "(launch-error), and a pinned step is not moved to another tool")
  trace=own["attempt_trace"]
  self.assertIn("/codex/conductor:skipped-worker-pin",trace)
  self.assertIn(":native-subagent:skipped-worker-pin",trace)
  self.assertIn(":inline:skipped-worker-pin",trace)
  self.assertNotIn("selected_hop",out)
 def test_a_pinned_owner_harness_still_reaches_its_inline_hop(self):
  with self.dispatch_env():
   path,route,_node=self.standard_route()
   calls=[]
   code,out,err=self.start(path,patches=(
    mock.patch.object(F,"sealed_pin_harness",side_effect=lambda route,worker_type=None:"codex"),
    mock.patch.object(F,"_usage_states",return_value={"claude":"ok","codex":"ok","opencode":"ok"}),
    mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"claude":50,"codex":50,"opencode":50}),
    self.wrapper_run(calls,"check=failed\nreason=launch-error\nchild_spawned=0\n",code=73),
   ))
  self.assertEqual(code,79,out+err)
  self.assertEqual([cmd[1].split("/")[-3] for cmd in calls],["codex"])
  self.assertIn("selected_hop=inline",out)
  self.assertIn("/claude/conductor:skipped-worker-pin",out)


class LaunchTupleReportOnlyTest(unittest.TestCase):
 """SD-114 (2)-C: P1/P2/P3 producer wiring + report-only stage 1
 byte-identical guarantee. Uses `FallbackTest.run_inline`/`dispatch_env`
 in-process (never a subprocess) because these fixtures patch `F` internals,
 which only reaches an in-process call. (The `AGENT_HOME`-vs-installed-release
 grounding digest mismatch this class used to route around was a fixture
 defect, not a runtime one: `route()`/`dispatch_env` now seal and launch
 against one root, so `run_chain`-based tests hold too.)"""

 # A fully-specified but harness-mismatched parent identity forces every
 # candidate down either P2 (codex, unsupported by the default fixture) or
 # P3 (claude, supported but sealed to a different runtime) regardless of
 # live capacity/usage-based re-ranking -- both hop kinds are always
 # visited by the candidate loop, so this is deterministic across runs.
 WRONG_PARENT = {"AGENT_DISPATCH_CURRENT_HARNESS": "opencode",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}

 def setUp(self):
  self.base = FallbackTest("test_cross_harness_direct_precedes_inline")
  self.base.setUp()
  self.addCleanup(self.base.tearDown)

 def _route_and_ledger(self, **usage_kwargs):
  with self.base.dispatch_env(**self.WRONG_PARENT):
   path = self.base.route(**usage_kwargs)
   route_id = json.loads(path.read_text())["route_id"]
  return path, route_id

 # `_allocation_skip` is only ever assigned to a row that already matched
 # the sealed parent identity (`ordered_fallback_hops`'s `headless` dict) --
 # a mismatched parent routes the row to `trailing_rows` before usage
 # limiting is even considered, so P1's fixture needs a MATCHING identity
 # (unlike P2/P3, which need the mismatch). Both harnesses are marked
 # usage-limited so nothing ever reaches a real wrapper subprocess spawn.
 RIGHT_PARENT = {"AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}

 def test_b47_1_allocation_skip_producer_fires(self):
  # P1: a live-usage-limited candidate has exact pre-launch tuple evidence.
  with self.base.dispatch_env(**self.RIGHT_PARENT):
   path = self.base.route(same_status="supported")
   route_id = json.loads(path.read_text())["route_id"]
  with self.base.dispatch_env(**self.RIGHT_PARENT), \
       mock.patch.object(F, "_usage_states",
                         return_value=self.base._usage_states(limited=("codex", "claude"))):
   code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  by_key = {r["tuple_key"]: r for r in rows}
  self.assertEqual(by_key["codex/headless/workspace-write/codex/conductor"]["rejection_class"],
                    "allocation-skip")
  self.assertEqual(by_key["codex/headless/workspace-write/codex/conductor"]["evidence_ref"],
                    "usage-limited")
  self.assertEqual(observation.unrecorded, 0)

 def test_b47_1_candidate_unsupported_producer_fires(self):
  # P2: codex is `status="unsupported"` by the default fixture.
  path, route_id = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  by_class = {r["rejection_class"]: r for r in rows}
  self.assertIn("candidate-unsupported", by_class)
  self.assertEqual(by_class["candidate-unsupported"]["tuple_key"],
                    "codex/headless/workspace-write/codex/conductor")
  self.assertEqual(observation.unrecorded, 0)

 def test_b47_1_sealed_parent_not_live_producer_fires(self):
  # P3: claude is supported, but WRONG_PARENT seals a different runtime, so
  # `parent_runtime_failure()` returns the CANDIDATE_SCOPED
  # `dispatch-evidence-parent-runtime-mismatch`.
  path, route_id = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  by_class = {r["rejection_class"]: r for r in rows}
  self.assertIn("sealed-parent-not-live", by_class)
  self.assertEqual(by_class["sealed-parent-not-live"]["tuple_key"],
                    "codex/headless/workspace-write/claude/conductor")
  self.assertEqual(by_class["sealed-parent-not-live"]["evidence_ref"],
                    "dispatch-evidence-parent-runtime-mismatch")
  self.assertEqual(observation.unrecorded, 0)

 def test_b47_2_registry_failures_returns_nonempty(self):
  # Regression pin (unrelated legacy axis, §5.4): `registry_failures()`
  # itself must remain fully functional and unchanged by this cycle.
  route = json.loads(self.base.route().read_text())
  pipe = (f"capability=autopilot-code,route_id={route['route_id']},route_node=plan,"
          "parent=owner,attempt_id=att-prior000000,parent_harness=codex,"
          "parent_transport=headless,parent_sandbox=workspace-write,"
          "child_harness=codex,launch_authority=conductor,note=dead-launch-error,"
          "failure_class=launch-tuple")
  self.base.jobs.write_text(
   f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.base.repo}\tfallback-plan\t{pipe}\n")
  failures = F.registry_failures(self.base.jobs, route["route_id"], "plan")
  self.assertNotEqual(failures, {})
  self.assertIn("codex/headless/workspace-write/codex/conductor", failures)

 def test_b47_5_report_only_stdout_byte_identical(self):
  path, route_id = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   self.assertEqual(self.base.ledger_rows(route_id), [])
   buf1 = io.StringIO()
   with contextlib.redirect_stdout(buf1):
    code1, observation1 = self.base.run_inline(path)
   self.assertNotIn("launch-tuple", buf1.getvalue())
   buf2 = io.StringIO()
   with contextlib.redirect_stdout(buf2):
    code2, observation2 = self.base.run_inline(path)
  self.assertEqual(code1, code2)
  self.assertEqual(buf1.getvalue(), buf2.getvalue(),
                    "selection output must be byte-identical whether or not "
                    "the launch-tuple ledger already marks candidates spent")
  self.assertEqual(set(observation2.spent),
                    {"codex/headless/workspace-write/codex/conductor",
                     "codex/headless/workspace-write/claude/conductor"})

 def test_b47_5_report_written_on_unarmed_preprocessing_failure_and_armed_chain_exhausted(self):
  # gap1 correction 3 (owner_arbitration.md 🟡-2 / plan.md §10-R5): this name
  # used to say "every exit path" but only ever drove two of the source's
  # twelve (plan.md §4 (2)-C's E1-E12 table). It exercises exactly:
  #  - E1, unarmed: a genuine preprocessing failure (parent identity partially
  #    exported, raised before `observation.arm()` is ever reached) writes
  #    zero report rows.
  #  - E12, armed: a fully-armed chain-exhausted exit writes exactly one.
  # It does NOT drive:
  #  - E8 (normal success, `return 0` after a real wrapper spawn): every
  #    fixture in this class deliberately usage-limits or parent-mismatches
  #    every candidate so nothing ever reaches a real wrapper subprocess
  #    spawn (see the class docstring) -- reaching E8 needs `FallbackTest`'s
  #    subprocess-spawning fixtures (e.g. `run_chain`), not this in-process one.
  #  - E10 (native-subagent, `return 78`): every route this class compiles
  #    passes `native="unsupported"` (the `route()` default), so the
  #    same/cross-harness-headless hops never exhaust into the
  #    native-subagent hop. Reaching E10 needs a `native="supported"` route,
  #    which no fixture here builds.
  with self.base.dispatch_env(AGENT_DISPATCH_CURRENT_HARNESS="codex"):
   path = self.base.route(same_status="supported")
   route_id = json.loads(path.read_text())["route_id"]
   code = self.base.run_inline_main(path)
  self.assertEqual(code, 73)
  self.assertEqual(self.base.report_rows(route_id), [])

  path2, route_id2 = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   code2 = self.base.run_inline_main(path2)
  self.assertEqual(code2, 79)
  reports = self.base.report_rows(route_id2)
  self.assertEqual(len(reports), 1)
  self.assertEqual(reports[0]["route_id"], route_id2)
  self.assertEqual(reports[0]["spent_seen"], 0)

 def test_b47_5_stage_two_or_join_absent(self):
  source = (Path(F_SPEC.origin)).read_text(encoding="utf-8")
  self.assertNotIn("LAUNCH_TUPLE.spent_tuples", source[:source.index("def registry_failures")])
  registry_failures_source = source[
   source.index("def registry_failures"):source.index("def registry_rows")
  ]
  self.assertNotIn("spent_tuples", registry_failures_source)
  self.assertNotIn("LAUNCH_TUPLE", registry_failures_source)
  self.assertNotIn("launch-tuple/", registry_failures_source)
  # `failed_tuples` gains new members through exactly the same call sites
  # as before this cycle -- three `.add(` calls and the initial
  # `set(...) | set(...)` construction, never through `observation.spent`.
  self.assertEqual(source.count("failed_tuples.add("), 3)
  self.assertNotIn("failed_tuples |=", source)
  self.assertNotIn("failed_tuples.update(", source)

 def test_b47_6_unknown_failure_class_suppresses_nothing(self):
  # An arbitrary/unknown `rejection_class` string persisted directly to the
  # ledger (bypassing the writer's own closed-enum refusal) must still
  # suppress zero candidates -- stage 1 never reads the ledger for
  # selection at all, regardless of its content.
  path, route_id = self._route_and_ledger()
  state_root = self.base.jobs.parent
  root = state_root / "launch-tuple"
  root.mkdir(parents=True)
  garbage = {"schema_version": 1, "route_id": route_id, "route_node": "plan",
             "route_hash": json.loads(path.read_text())["route_hash"],
             "owner_attempt_id": "att-fallback-parent",
             "tuple_key": "codex/headless/workspace-write/codex/conductor",
             "rejection_class": "totally-unknown-value",
             "evidence_ref": "garbage", "observed_at": time.time(),
             "event_id": "lt-garbage"}
  (root / f"{route_id}.jsonl").write_text(json.dumps(garbage) + "\n", encoding="utf-8")
  with self.base.dispatch_env(**self.WRONG_PARENT):
   buf = io.StringIO()
   with contextlib.redirect_stdout(buf):
    code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  self.assertIn("codex/headless/workspace-write/codex/conductor", observation.spent)
  self.assertIn("skipped-nested-network-unconfirmed", buf.getvalue())
  self.assertNotIn("skipped-totally-unknown-value", buf.getvalue())

 def test_b47_7_axis_absent_corpus_selection_unchanged(self):
  # gap1 correction 2 (owner_arbitration.md 🟡-1 / plan.md §4 (2)-C): B47-7's
  # predicate is "a legacy registry corpus with the `launch_tuple_verdict`
  # axis absent entirely selects identically once that axis exists." That
  # corpus is `registry_failures()`'s own axis -- pre-revision
  # `failure_class=launch-tuple` rows, same shape as B47-2's fixture -- never
  # B47-5's spent-tuple ledger. This builds and asserts on that corpus on its
  # own instead of re-invoking test_b47_5_report_only_stdout_byte_identical,
  # which carries no registry-failure row and so never exercises this axis.
  path, route_id = self._route_and_ledger()
  same_key = "codex/headless/workspace-write/codex/conductor"
  pipe = (f"capability=autopilot-code,route_id={route_id},route_node=plan,"
          "parent=owner,attempt_id=att-prior-legacy0,parent_harness=codex,"
          "parent_transport=headless,parent_sandbox=workspace-write,"
          "child_harness=codex,launch_authority=conductor,note=dead-launch-error,"
          "failure_class=launch-tuple")
  with self.base.jobs.open("a", encoding="utf-8") as fh:
   fh.write(f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.base.repo}\tfallback-plan\t{pipe}\n")
  legacy_failures = F.registry_failures(self.base.jobs, route_id, "plan")
  self.assertIn(same_key, legacy_failures)

  with self.base.dispatch_env(**self.WRONG_PARENT):
   self.assertEqual(self.base.ledger_rows(route_id), [],
                     "launch_tuple_verdict axis must be absent for the first run")
   buf1 = io.StringIO()
   with contextlib.redirect_stdout(buf1):
    code1, observation1 = self.base.run_inline(path)
   buf2 = io.StringIO()
   with contextlib.redirect_stdout(buf2):
    code2, observation2 = self.base.run_inline(path)
  self.assertNotEqual(self.base.ledger_rows(route_id), [],
                       "the second run must have populated the launch_tuple_verdict axis")
  self.assertEqual(code1, code2)
  self.assertEqual(buf1.getvalue(), buf2.getvalue(),
                    "the legacy registry corpus's selection must stay unchanged "
                    "once the launch_tuple_verdict axis exists alongside it")
  for output in (buf1.getvalue(), buf2.getvalue()):
   self.assertIn("skipped-prior-unchanged-failure", output)
  for observation in (observation1, observation2):
   self.assertIn(same_key, observation.failed_tuples)

 def test_b47_10_consumer_only_skip_writes_no_evidence(self):
  path, route_id = self._route_and_ledger()
  cross_key = "codex/headless/workspace-write/claude/conductor"
  with self.base.dispatch_env():
   code, observation = self.base.run_inline(path, "--failed-tuple", cross_key)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  self.assertEqual({r["tuple_key"] for r in rows},
                    {"codex/headless/workspace-write/codex/conductor"})
  self.assertEqual({r["rejection_class"] for r in rows}, {"candidate-unsupported"})



class D5FallbackSelectionTest(unittest.TestCase):
 def test_disabled_admitted_pin_survives_ordering_and_wrapper_recheck(self):
  parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"adapter-default","launch_authority":"conductor"}
  node={"id":"execute","harness_policy":{"primary":["codex"],"relief":[],"last_resort":[]},
        "fallback_hops":[{"ordinal":2,"fallback_hop":"cross-harness-headless", "candidates":[{**parent,"child_harness":"codex","status":"supported"}]}]}
  route={"selection_pins":{"worker":{"harness":"codex"}}}
  args=SimpleNamespace(action="dry-run",direct_timeout=1)
  with tempfile.TemporaryDirectory() as td:
   config=Path(td)/"defaults.yaml"
   config.write_text((ROOT/"profiles/dispatch-defaults.yaml").read_text().replace(
       "  enabled: [claude, codex, opencode]","  enabled: [claude, opencode]"))
   diagnostic=io.StringIO()
   with mock.patch.dict(os.environ,{"DISPATCH_DEFAULTS_CONFIG":str(config)}), \
        mock.patch.object(F.subprocess,"run",return_value=SimpleNamespace(returncode=0)) as spawn, \
        contextlib.redirect_stderr(diagnostic):
    hops,_=F.ordered_fallback_hops(route,node,Path(td)/"jobs",parent_identity=parent)
    self.assertFalse(hops[0]["candidates"][0].get("_allocation_skip"))
    result=F.run_wrapper(args,[sys.executable,str(ROOT/"adapters/codex/bin/dispatch-headless.py")],route=route,node=node)
   self.assertEqual(result.returncode,0)
   spawn.assert_called_once()
   self.assertEqual(diagnostic.getvalue().count("policy_note=disabled-since-admission"),1)

 def test_all_strategies_trailing_and_native_candidates_use_live_enabled(self):
  with tempfile.TemporaryDirectory() as td:
   config=Path(td)/"defaults.yaml"
   config.write_text((ROOT/"profiles/dispatch-defaults.yaml").read_text().replace(
    "  enabled: [claude, codex, opencode]","  enabled: [claude, opencode]"))
   parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"adapter-default","launch_authority":"conductor"}
   rows=[{**parent,"child_harness":h,"status":"supported"} for h in ("codex","claude","opencode")]
   hops=[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":rows},
         {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[{**rows[0],"parent_harness":"opencode"}]},
         {"ordinal":3,"fallback_hop":"native-subagent","candidates":[{"harness":"codex","status":"supported"}]},
         {"ordinal":4,"fallback_hop":"inline"}]
   node={"id":"execute","dispatch_depth":2,"model_profile":"deep","fallback_hops":hops,
         "harness_policy":{"primary":["codex","claude"],"relief":["opencode"],"last_resort":[],"promote_relief_below":0}}
   with mock.patch.dict(os.environ,{"DISPATCH_DEFAULTS_CONFIG":str(config)}), \
        mock.patch.object(F,"attempt_counts",return_value={h:0 for h in ("claude","codex","opencode")}), \
        mock.patch.object(F,"_usage_states",return_value={h:"ok" for h in ("claude","codex","opencode")}), \
        mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"codex":99,"claude":80,"opencode":70}):
    for strategy in (None,"config-order","balanced","capacity-aware","least-recent-attempts"):
     route={} if strategy is None else {"dispatch_allocation":{"strategy":strategy,"window":30,"harness_order":["codex","claude","opencode"]}}
     before=json.dumps((route,node),sort_keys=True)
     ordered,context=F.ordered_fallback_hops(route,node,Path(td)/"jobs.log",parent_identity=parent)
     candidates=[r for hop in ordered if hop["fallback_hop"] in F.HEADLESS_HOPS for r in hop.get("candidates",[])]
     self.assertTrue(all(r.get("_allocation_skip")=="disabled-by-user-policy" for r in candidates if r["child_harness"]=="codex"))
     self.assertEqual(next(r["child_harness"] for r in candidates if not r.get("_allocation_skip")),"claude")
     self.assertFalse(next(h for h in ordered if h["fallback_hop"]=="native-subagent")["candidates"])
     self.assertEqual(json.dumps((route,node),sort_keys=True),before)
     if context:self.assertNotIn("codex",context["rank"])

 def test_enabled_checked_worker_pin_outside_original_bands_and_pool_stays_exact(self):
  with tempfile.TemporaryDirectory() as td:
   config=Path(td)/"defaults.yaml"
   config.write_text((ROOT/"profiles/dispatch-defaults.yaml").read_text().replace("  enabled: [claude, codex, opencode]","  enabled: [claude, opencode]"))
   parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"adapter-default","launch_authority":"conductor"}
   node={"id":"execute","dispatch_depth":2,"model_profile":"deep",
    "harness_policy":{"primary":["claude"],"relief":[],"last_resort":[],"promote_relief_below":0},
    "fallback_hops":[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[{**parent,"child_harness":"claude","status":"supported"}]},
                     {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[{**parent,"child_harness":"opencode","status":"supported"}]}]}
   before=json.dumps(node,sort_keys=True)
   with mock.patch.dict(os.environ,{"DISPATCH_DEFAULTS_CONFIG":str(config)}), \
        mock.patch.object(F,"attempt_counts",return_value={"claude":0,"codex":0,"opencode":0}), \
        mock.patch.object(F,"_usage_states",return_value={"claude":"ok","codex":"ok","opencode":"ok"}), \
        mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"claude":5,"codex":99,"opencode":80}):
    for strategy in ("balanced","capacity-aware","least-recent-attempts"):
     route={"dispatch_allocation":{"strategy":strategy,"window":30,"harness_order":["claude"]},
            "selection_pins":{"contract_version":1,"worker":{"harness":"opencode"}}}
     hops,_=F.ordered_fallback_hops(route,node,Path(td)/"jobs",parent_identity=parent)
     self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
     self.assertFalse(hops[0]["candidates"][0].get("_worker_pin_skip"))
     del route["selection_pins"]
     hops,_=F.ordered_fallback_hops(route,node,Path(td)/"jobs",parent_identity=parent)
     self.assertTrue(all(row.get("_allocation_skip") for hop in hops for row in hop.get("candidates",[]) if row["child_harness"]=="opencode"))
   self.assertEqual(json.dumps(node,sort_keys=True),before)

 def test_legacy_owner_order_is_not_a_new_worker_prohibition(self):
  with tempfile.TemporaryDirectory() as td:
   config=Path(td)/"defaults.yaml"
   parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"adapter-default","launch_authority":"conductor"}
   node={"id":"execute","model_profile":"deep","fallback_hops":[{"ordinal":1,"fallback_hop":"same-harness-headless",
    "candidates":[{**parent,"child_harness":h,"status":"supported"} for h in ("codex","claude")]}]}
   with mock.patch.dict(os.environ,{"DISPATCH_DEFAULTS_CONFIG":str(config)}), \
        mock.patch.object(F,"attempt_counts",return_value={"claude":0,"codex":0,"opencode":0}), \
        mock.patch.object(F,"_usage_states",return_value={"claude":"ok","codex":"ok","opencode":"ok"}):
    for version,strategy in ((1,"config-order"),(2,"least-recent-attempts")):
     config.write_text(f"schema_version: {version}\ndepth1_owner: [claude]\nopencode:\n  relief_only: {str(version == 1).lower()}\n"+
      ("allocation:\n  strategy: least-recent-attempts\n  window: 30\n" if version==2 else "")+"capabilities:\n")
     route={"dispatch_allocation":{"strategy":strategy,"window":30,"harness_order":["claude"]}}
     hops,context=F.ordered_fallback_hops(route,node,Path(td)/"jobs",parent_identity=parent)
     self.assertFalse(any(row.get("_allocation_skip") for hop in hops for row in hop.get("candidates",[])))
     if context:self.assertEqual(set(context["rank"]),{"claude","codex"})

 def test_real_wrapper_boundary_rechecks_policy_for_all_adapters_and_capacity_commands(self):
  with tempfile.TemporaryDirectory() as td:
   config=Path(td)/"defaults.yaml"
   baseline=(ROOT/"profiles/dispatch-defaults.yaml").read_text()
   args=SimpleNamespace(action="dry-run",launch_lifecycle=F.DETACHED,direct_timeout=1,foreground_timeout=1)
   parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"adapter-default","launch_authority":"conductor"}
   node={"id":"execute","fallback_hops":[{"ordinal":1,"fallback_hop":"cross-harness-headless",
    "candidates":[{**parent,"child_harness":"codex","status":"supported"}]}]}
   config.write_text(baseline)
   with mock.patch.dict(os.environ,{"DISPATCH_DEFAULTS_CONFIG":str(config)}):
    hops,_=F.ordered_fallback_hops({},node,Path(td)/"jobs",parent_identity=parent)
    self.assertEqual(hops[0]["candidates"][0]["child_harness"],"codex")
    config.write_text(baseline.replace("  enabled: [claude, codex, opencode]","  enabled: [claude, opencode]"))
    for harness in ("claude","codex","opencode"):
     # Both ordinary and SD-59 model-alternative commands share run_wrapper.
     command=[sys.executable,str(ROOT/f"adapters/{harness}/bin/dispatch-headless.py"),"--dry-run","--capacity-retry","1"]
     with mock.patch.object(F.subprocess,"run",return_value=SimpleNamespace(returncode=0,stdout="",stderr="")) as spawn, \
          mock.patch.object(F,"run_forwarding_termination") as foreground:
      result=F.run_wrapper(args,command)
     self.assertEqual(spawn.call_count,0 if harness=="codex" else 1)
     foreground.assert_not_called()
     if harness=="codex":
      self.assertEqual(result.returncode,65)
      self.assertIn("explicit-adapter-disabled-by-user-policy",result.stdout)
      self.assertEqual(F._typed_stop_code("explicit-adapter-disabled-by-user-policy",direct=True),65)
    config.unlink()
    with mock.patch.object(F.subprocess,"run") as spawn:
     result=F.run_wrapper(args,[sys.executable,str(ROOT/"adapters/claude/bin/dispatch-headless.py"),"--dry-run"])
    spawn.assert_not_called()
    self.assertIn("reason=defaults-invalid",result.stdout)
    self.assertEqual(F._typed_stop_code("defaults-invalid",direct=True),65)

if __name__=="__main__": unittest.main()
