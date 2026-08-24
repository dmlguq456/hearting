#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"utilities"))
import dispatch_contract as D

SPEC=importlib.util.spec_from_file_location(
 "dispatch_recovery",ROOT/"utilities"/"dispatch-recovery.py")
R=importlib.util.module_from_spec(SPEC);sys.modules[SPEC.name]=R
SPEC.loader.exec_module(R)

CURRENT=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
         "execution_surface=registered-headless,registered_worker=1,"
         "fallback_hop=same-harness-headless,worker_type=stage")


def row(status,slug,metadata):
 pipe=CURRENT+","+",".join(f"{key}={value}" for key,value in metadata.items())
 return f"2026-08-25T00:00:00Z\t{status}\t/repo\t/worktree\t{slug}\t{pipe}"


def source_metadata(attempt="att-source-recovery"):
 return {
  "route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
  "route_node":"execute","attempt_id":attempt,
  "pid":"41","pid_start":"900","pgid":"41",
  "pid_scope":"namespace-local","pid_observer_ns":"pid:[401]",
  "pid_ns":"pid:[401]","launch_lifecycle":"detached",
  "launch_outcome":"governed-process-group-drained",
  "group_reap_proof":D.GROUP_REAP_PROOF,"group_reap_pgid":"41",
  "attempt_descendant_proof":D.ATTEMPT_DESCENDANT_PROOF,
  "attempt_descendant_observer_ns":"pid:[401]",
 }


class FakeServices(R.RecoveryServices):
 def __init__(self,*,remaining=1,publish_admitted=True,start_admitted=True,
              terminal_mode="manual",auto_finish=False):
  self.remaining=remaining;self.publish_admitted=publish_admitted
  self.start_admitted=start_admitted;self.terminal_mode=terminal_mode
  self.auto_finish=auto_finish
  self.cancel_calls=0;self.publish_calls=0;self.start_calls=0
  self.process=None;self.retry_attempt_id=""

 def cancel_receiptless(self,request):
  self.cancel_calls+=1
  source=R.exact_attempt(request.jobs,request.original_attempt_id)
  unavailable=D.ProcessGroupObservation("unverifiable",reason="foreign")
  with mock.patch.object(
   D,"process_group_observation",return_value=unavailable,
  ),mock.patch.object(
   D,"attempt_tagged_descendants",return_value=unavailable,
  ),mock.patch.object(
   D,"attempt_scan_namespace_authority",return_value=False,
  ):
   proof=D.prove_attempt_quiescence(source.metadata,max_wait_seconds=0)
  receipt=D.seal_cancellation_quiescence_receipt(
   request.jobs,request.original_attempt_id,proof)
  closed=D.close_attempt_row_if(
   request.jobs,request.original_attempt_id,"cancelled-receipt-unavailable",
   lambda _fields:True,
   evidence={
    "failure_class":"cancelled","receipt_state":"unavailable",
    "marker_state":"absent",
    "reconcile_reason":"automatic-cancelled-receipt-unavailable",
    "classifier_source":"automatic-receipt-unavailable-v1",
   })
  return {"closed":int(closed),"receipt_digest":receipt}

 def remaining_cascade(self,request,source,recovery_identity):
  return self.remaining

 def continuation_payload(self,request,recovery_identity):
  path=R.continuation_record_path(request.jobs,recovery_identity)
  if not path.is_file(): return None
  raw=path.read_bytes();payload=json.loads(raw)
  return {
   "admitted":True,"recovery_id":recovery_identity,
   "continuation_id":payload["continuation_id"],
   "continuation_path":str(path),
   "continuation_digest":"sha256:"+hashlib.sha256(raw).hexdigest(),
   "gap_leg_id":payload["partial_group_continuation"]["gap_leg_id"],
   "realized_peer_attempt_ids":payload["realized_peer_attempt_ids"],
  }

 def observe_continuation(self,request,recovery_identity):
  return self.continuation_payload(request,recovery_identity)

 def publish_continuation(self,request,source,recovery_identity):
  self.publish_calls+=1
  if not self.publish_admitted:
   return {"admitted":False,"reason":"continuation-admission-impossible"}
  path=R.continuation_record_path(request.jobs,recovery_identity)
  path.parent.mkdir(parents=True,exist_ok=True)
  payload={
   "continuation_contract_version":1,"recovery_id":recovery_identity,
   "continuation_id":"cont-"+recovery_identity[4:20],
   "source_route_id":source.metadata["route_id"],
   "source_route_hash":source.metadata["route_hash"],
   "partial_group_continuation":{"gap_leg_id":"execute/gap-1"},
   "realized_peer_attempt_ids":["att-successful-peer"],
  }
  path.write_bytes(R._canonical_bytes(payload)+b"\n")
  return self.continuation_payload(request,recovery_identity)

 def observe_start(self,request,continuation,claim):
  return R.ProductionRecoveryServices().observe_start(
   request,continuation,claim)

 def start_gap(self,request,continuation,claim):
  self.start_calls+=1
  if not self.start_admitted:
   return {"admitted":False,"reason":"gap-admission-impossible"}
  self.retry_attempt_id=claim.retry_attempt_id
  self.process=subprocess.Popen(
   [sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True)
  identity=D.process_launch_identity(self.process.pid)
  metadata={
   **identity,"route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
   "route_node":"execute/gap-1","attempt_id":claim.retry_attempt_id,
   "batch_route_node":"execute/gap-1",
   "parent_attempt_id":request.original_attempt_id,
   "recovery_id":claim.recovery_id,"retry_ordinal":"1",
   "launch_lifecycle":"detached","launch_fence":"registry-v1",
  }
  registered=D.claim_attempt_row(
   request.jobs,claim.retry_attempt_id,
   row("open","execute-gap-1",metadata),launch=True)
  D.mark_attempt_launch_started(
   request.jobs,claim.retry_attempt_id,self.process.pid)
  observed=self.observe_start(request,continuation,claim)
  self.assert_start_evidence(observed,registered)
  if self.auto_finish:
   self.finish_success(request.jobs)
  return observed

 def assert_start_evidence(self,observed,registered):
  if not registered or observed is None or observed["process_count"] != 1:
   raise AssertionError((registered,observed))

 def finish_success(self,jobs):
  if self.process is not None and self.process.poll() is None:
   self.process.terminate();self.process.wait(timeout=5)
  if self.retry_attempt_id:
   D.close_attempt_row(
    jobs,self.retry_attempt_id,"completed-marker",
    evidence={"failure_class":"pass","classifier_source":"test-wrapper-v1"})

 def observe_terminal(self,request,claim):
  if self.terminal_mode=="receipt-unavailable":
   return {"outcome":"blocked","reason":"receipt-unavailable-retry-exhausted"}
  return R.ProductionRecoveryServices().observe_terminal(request,claim)

 def cleanup(self):
  if self.process is not None and self.process.poll() is None:
   self.process.terminate();self.process.wait(timeout=5)


class DispatchRecoveryTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
  self.jobs=self.base/"jobs.log";self.route=self.base/"route.json"
  self.jobs_environment=mock.patch.dict(
   os.environ,{"AGENT_DISPATCH_JOBS":str(self.jobs.resolve())})
  self.jobs_environment.start()
  self.route.write_text("{}\n",encoding="utf-8")
  self.attempt="att-source-recovery"
  self.source=source_metadata(self.attempt)
  self.peer=row("done","successful-peer",{
   "route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
   "route_node":"successful-peer","attempt_id":"att-successful-peer",
   "note":"completed-marker","failure_class":"pass",
  })
  self.jobs.write_text(
   row("open","execute",self.source)+"\n"+self.peer+"\n",encoding="utf-8")
  self.request=R.RecoveryRequest(
   self.jobs,self.attempt,self.route,"execute","execute",cancellation_wait=0)

 def tearDown(self):
  self.jobs_environment.stop()
  self.temp.cleanup()

 def record(self,result):
  return json.loads(Path(result.record_path).read_text(encoding="utf-8"))

 def test_happy_exact_gap_spawns_once_and_replay_preserves_peer(self):
  services=FakeServices()
  peer_before=self.peer
  try:
   first=R.coordinate_recovery(self.request,services)
   self.assertEqual((first.state,first.child_spawned),("in-progress",1))
   self.assertEqual((services.cancel_calls,services.publish_calls,services.start_calls),(1,1,1))
   rows=self.jobs.read_text().splitlines()
   self.assertEqual(len(rows),3)
   self.assertIn(peer_before,rows)
   retry=R.exact_attempt(self.jobs,first.retry_attempt_id)
   self.assertEqual(retry.metadata["launch_started"],"1")
   self.assertTrue(D.process_identity_is_live(
    int(retry.metadata["pid"]),retry.metadata["pid_start"]))

   replay=R.coordinate_recovery(self.request,services)
   self.assertEqual(replay.retry_attempt_id,first.retry_attempt_id)
   self.assertEqual(services.start_calls,1)
   services.finish_success(self.jobs)
   terminal=R.coordinate_recovery(self.request,services)
   self.assertEqual(terminal.state,"terminal")
   final=R.coordinate_recovery(self.request,services)
   self.assertEqual(final.recovery_id,terminal.recovery_id)
   self.assertEqual(services.start_calls,1)
   record=self.record(final)
   self.assertEqual(tuple(
    phase for phase in R.PHASES if phase in record["phases"]),R.PHASES)
   self.assertEqual(len([
    line for line in self.jobs.read_text().splitlines()
    if "attempt_id=att-successful-peer" in line]),1)
  finally:
   services.cleanup()

 def test_every_phase_checkpoint_recovers_without_second_mutation(self):
  for target in R.PHASES:
   with self.subTest(target=target),tempfile.TemporaryDirectory() as td:
    base=Path(td);jobs=base/"jobs.log";route_file=base/"route.json"
    route_file.write_text("{}\n");jobs.write_text(
     row("open","execute",source_metadata())+"\n"+self.peer+"\n")
    request=R.RecoveryRequest(
     jobs,"att-source-recovery",route_file,"execute","execute",
     cancellation_wait=0)
    services=FakeServices(auto_finish=True)
    def crash(phase):
     if phase==target: raise R.InjectedRecoveryCrash(phase)
    try:
     with self.assertRaises(R.InjectedRecoveryCrash):
      R.coordinate_recovery(request,services,checkpoint=crash)
     recovered=R.coordinate_recovery(request,services)
     self.assertEqual(recovered.state,"terminal")
     self.assertLessEqual(services.cancel_calls,1)
     self.assertLessEqual(services.publish_calls,1)
     self.assertLessEqual(services.start_calls,1)
     record=json.loads(Path(recovered.record_path).read_text())
     self.assertEqual(tuple(
      phase for phase in R.PHASES if phase in record["phases"]),R.PHASES)
     retry_ids={
      R.exact_attempt(jobs,item).metadata["attempt_id"]
      for item in (recovered.retry_attempt_id,)
     }
     self.assertEqual(len(retry_ids),1)
     self.assertEqual(len(jobs.read_text().splitlines()),3)
    finally:
     services.cleanup()

 def test_intent_is_durable_before_cancellation_mutation(self):
  services=FakeServices(auto_finish=True)
  def crash(phase):
   if phase=="intent-observed": raise R.InjectedRecoveryCrash(phase)
  try:
   with self.assertRaises(R.InjectedRecoveryCrash):
    R.coordinate_recovery(self.request,services,checkpoint=crash)
   self.assertEqual(services.cancel_calls,0)
   self.assertEqual(R.exact_attempt(self.jobs,self.attempt).status,"open")
   intents=list((self.jobs.parent/"recovery"/"intents").glob("*.json"))
   self.assertEqual(len(intents),1)
   intent=json.loads(intents[0].read_text())
   self.assertEqual(intent["current_phase"],"intent-observed")
   recovered=R.coordinate_recovery(self.request,services)
   self.assertEqual(recovered.state,"terminal")
   self.assertEqual(services.cancel_calls,1)
  finally:
   services.cleanup()

 def test_zero_remaining_cascade_seals_one_permanent_block(self):
  services=FakeServices(remaining=0)
  result=R.coordinate_recovery(self.request,services)
  self.assertEqual((result.state,result.reason),(
   "blocked","receipt-unavailable-retry-exhausted"))
  replay=R.coordinate_recovery(self.request,services)
  self.assertEqual(replay.recovery_id,result.recovery_id)
  self.assertEqual(services.start_calls,0)
  source=R.exact_attempt(self.jobs,self.attempt)
  self.assertEqual(source.metadata["note"],"receipt-unavailable-retry-exhausted")
  record=self.record(replay)
  self.assertEqual(len([
   phase for phase in record["phases"] if phase=="terminal-or-blocked"]),1)

 def test_continuation_admission_failure_blocks_without_claim_or_peer_mutation(self):
  services=FakeServices(publish_admitted=False)
  peer_before=self.peer
  result=R.coordinate_recovery(self.request,services)
  self.assertEqual((result.state,result.reason),(
   "blocked","continuation-admission-impossible"))
  self.assertEqual(services.start_calls,0)
  source=R.exact_attempt(self.jobs,self.attempt)
  self.assertEqual(source.metadata["recovery_id"],result.recovery_id)
  self.assertEqual(source.metadata["failure_class"],"blocked")
  self.assertEqual(source.metadata["note"],"receipt-unavailable-retry-exhausted")
  self.assertEqual(source.metadata["start_permitted"],"0")
  self.assertIn(peer_before,self.jobs.read_text().splitlines())
  replay=R.coordinate_recovery(self.request,services)
  self.assertEqual(replay.state,"blocked")
  self.assertEqual(services.publish_calls,1)

 def test_retry_receipt_unavailable_blocks_and_never_restarts(self):
  services=FakeServices(terminal_mode="receipt-unavailable")
  try:
   result=R.coordinate_recovery(self.request,services)
   self.assertEqual((result.state,result.reason),(
    "blocked","receipt-unavailable-retry-exhausted"))
   self.assertEqual(services.start_calls,1)
   replay=R.coordinate_recovery(self.request,services)
   self.assertEqual(replay.state,"blocked")
   self.assertEqual(services.start_calls,1)
   source=R.exact_attempt(self.jobs,self.attempt)
   self.assertEqual(source.metadata["failure_class"],"blocked")
   self.assertEqual(source.metadata["note"],"receipt-unavailable-retry-exhausted")
   self.assertEqual(source.metadata["start_permitted"],"0")
  finally:
   services.cleanup()

 def production_fixture(self):
  artifact=self.base/"artifacts"
  route_path=artifact/".runtime"/"routes"/"rt-recovery.json"
  route_path.parent.mkdir(parents=True,exist_ok=True)
  nodes=[
   {
    "id":"successful-peer","parallel_group":"recovery-group",
    "replica_group":"recovery-group","parallel_leg_index":0,
    "model_profile":"balanced-deep","perspective":"primary",
    "leg_class":"peer",
   },
   {
    "id":"execute","parallel_group":"recovery-group",
    "replica_group":"recovery-group","parallel_leg_index":1,
    "model_profile":"light","perspective":"independent",
    "leg_class":"peer",
   },
  ]
  route={
   "route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
   "cwd":"/worktree","artifact_root":str(artifact),
   "launch_compatibility_tuple":{
    "jobs_path":{"path":str(self.jobs.resolve())},
   },
   "nodes":nodes,
   "parallel_groups":[{
    "id":"recovery-group","kind":"replica","join_policy":"all",
    "independence_axes":["cross-harness","model-profile","perspective"],
    "width":2,"members":["successful-peer","execute"],
   }],
  }
  route_path.write_bytes(R._canonical_bytes(route)+b"\n")
  members=[
   {
    "assignment_sha256":"sha256:"+"a"*64,
    "attempt_id":"att-successful-peer","route_node":"successful-peer",
    "harness":"codex","fallback_hop":"same-harness-headless",
    "fallback_ordinal":1,"model_profile":"balanced-deep",
    "perspective":"primary","parallel_leg_index":0,"leg_class":"peer",
   },
   {
    "assignment_sha256":"sha256:"+"a"*64,
    "attempt_id":self.attempt,"route_node":"execute",
    "harness":"claude","fallback_hop":"cross-harness-headless",
    "fallback_ordinal":2,"model_profile":"light",
    "perspective":"independent","parallel_leg_index":1,"leg_class":"peer",
   },
  ]
  manifest,digest,legs=R.build_manifest(
   parallel_group="recovery-group",route_id="rt-recovery",
   parent_attempt_id="att-owner",independence="cross-harness",
   members=members,
   required_independence_axes=["cross-harness","model-profile","perspective"],
   realized_independence_axes=["cross-harness","model-profile","perspective"],
  )
  def metadata(member):
   return {
    "route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
    "route_node":member["route_node"],"attempt_id":member["attempt_id"],
    "parent":"owner","parent_attempt_id":"att-owner",
    "harness":member["harness"],"child_harness":member["harness"],
    "fallback_ordinal":str(member["fallback_ordinal"]),
    "batch_declared_size":"2","batch_admission_count":"2",
    "batch_group":"recovery-group","batch_route_id":"rt-recovery",
    "batch_parent_attempt_id":"att-owner",
    "batch_attempt_id":member["attempt_id"],
    "batch_route_node":member["route_node"],
    "batch_harness":member["harness"],
    "batch_fallback_hop":member["fallback_hop"],
    "batch_fallback_ordinal":str(member["fallback_ordinal"]),
    "batch_independence":"cross-harness",
    "batch_model_profile":member["model_profile"],
    "batch_perspective":member["perspective"],
    "batch_parallel_leg_index":str(member["parallel_leg_index"]),
    "batch_leg_class":"peer","batch_auxiliary_check":"-",
    "batch_assignment_sha256":member["assignment_sha256"],
    "batch_manifest_sha256":digest,
    "batch_leg_sha256":legs[member["attempt_id"]],
   }
  peer={**metadata(members[0]),"note":"completed-marker","failure_class":"pass"}
  source={
   **source_metadata(self.attempt),**metadata(members[1]),
   "cancellation_quiescence_receipt":D.ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
   "cancellation_receipt_digest":"sha256:"+"9"*64,
   "quiescence_pgid_proof":D.GROUP_REAP_PROOF,
   "quiescence_descendant_proof":D.ATTEMPT_DESCENDANT_PROOF,
   "note":"cancelled-receipt-unavailable","failure_class":"cancelled",
   "classifier_source":"automatic-receipt-unavailable-v1",
   "reconcile_reason":"automatic-cancelled-receipt-unavailable",
   "receipt_state":"unavailable","marker_state":"absent",
  }
  self.jobs.write_text(
   row("done","execute",source)+"\n"
   +row("done","successful-peer",peer)+"\n",encoding="utf-8")
  request=R.RecoveryRequest(
   self.jobs,self.attempt,route_path,"execute","execute",cancellation_wait=0)
  recovery_id=D.recovery_id(
   source_route_id="rt-recovery",source_route_hash="sha256:"+"4"*64,
   node_or_group_leg="execute",original_attempt_id=self.attempt,
   cancellation_receipt_digest="sha256:"+"9"*64,
  )
  return request,recovery_id,manifest,digest,legs

 def official_continuation(self,request,manifest,digest,legs):
  realized=[{
   "node_id":"successful-peer","terminal_attempt_id":"att-successful-peer",
   "marker_path":str(self.base/"peer-marker.json"),
   "marker_digest":"sha256:"+"1"*64,"verdict":"PASS",
   "quiescence_proof_digest":"sha256:"+"2"*64,
   "output_evidence_digest":"sha256:"+"3"*64,
   "contract_hash":"sha256:"+"4"*64,
  }]
  peer_digest="sha256:"+hashlib.sha256(R._canonical_bytes(realized)).hexdigest()
  replacement_identity="sha256:"+"6"*64
  partial={
   "contract_version":1,"source_group_id":"recovery-group",
   "source_batch_manifest_digest":digest,
   "leg_manifest_digests":{
    member["route_node"]:legs[member["attempt_id"]]
    for member in manifest["members"]
   },
   "original_group_cardinality":2,"join_policy":"all",
   "failed_source_attempt_id":self.attempt,"gap_leg_id":"execute",
   "realized_peer_set":realized,
   "reused_peer_set_proof_digest":peer_digest,
   "replacement_leg_identity":replacement_identity,
   "replacement_attempt_id":"att-"+replacement_identity.split(":",1)[1][:48],
  }
  return {
   "continuation_contract_version":1,
   "route_id":"rt-continuation","route_hash":"sha256:"+"5"*64,
   "artifact_root":str(Path(request.route_file).parents[2]),
   "source_route_id":"rt-recovery","source_route_hash":"sha256:"+"4"*64,
   "resume_from_node":"execute","requested_boundary":"execute",
   "reason":"receipt-unavailable-recovery",
   "continuation_id":"cont-production","partial_group_continuation":partial,
  }

 def publish_production_fixture(self,services,request,recovery_id,manifest,digest,legs):
  official=self.official_continuation(request,manifest,digest,legs)
  route_path=(
   Path(official["artifact_root"])/".runtime"/"routes"
   /f"{official['route_id']}.json")
  manifests=[]
  def run(command,**_kwargs):
   manifest_path=Path(command[command.index("--partial-group-manifest")+1])
   manifests.append((manifest_path,json.loads(manifest_path.read_text())))
   if not route_path.exists():
    route_path.write_bytes(R._canonical_bytes(official)+b"\n")
   return SimpleNamespace(
    returncode=0,stdout=json.dumps(official)+"\n",stderr="")
  with mock.patch.object(
   R,"_preview_continuation_route",return_value=official,
  ),mock.patch.object(R.subprocess,"run",side_effect=run) as invoked:
   continuation=services.publish_continuation(
    request,R.exact_attempt(self.jobs,self.attempt),recovery_id)
  self.assertEqual(manifests[0][1],manifest)
  self.assertFalse(manifests[0][0].exists())
  command=invoked.call_args.args[0]
  self.assertEqual(command,[
   sys.executable,str(ROOT/"utilities"/"capability-route.py"),"continuation",
   "--source-route",str(request.route_file),
   "--resume-from-node","execute","--requested-boundary","execute",
   "--reason","receipt-unavailable-recovery",
   "--artifact-root",official["artifact_root"],
   "--partial-group-manifest",str(manifests[0][0]),
   "--source-group-id","recovery-group",
   "--failed-source-attempt-id",self.attempt,"--gap-leg-id","execute",
   "--output",str(route_path),
  ])
  return continuation,official,route_path

 def test_production_official_continuation_and_exact_gap_command_boundaries(self):
  request,recovery_id,manifest,digest,legs=self.production_fixture()
  services=R.ProductionRecoveryServices()
  continuation,official,route_path=self.publish_production_fixture(
   services,request,recovery_id,manifest,digest,legs)
  envelope_path=R.continuation_record_path(self.jobs,recovery_id)
  envelope=json.loads(envelope_path.read_text())
  self.assertNotIn("recovery_id",official)
  self.assertEqual(envelope["route_path"],str(route_path))
  self.assertEqual(
   envelope["route_digest"],"sha256:"+hashlib.sha256(route_path.read_bytes()).hexdigest())
  self.assertEqual(services.observe_continuation(request,recovery_id),continuation)

  envelope_path.unlink()
  replay,_same,_same_path=self.publish_production_fixture(
   services,request,recovery_id,manifest,digest,legs)
  self.assertEqual(replay,continuation)

  claim=D.claim_recovery_retry(
   self.jobs,recovery_id=recovery_id,source_route_id="rt-recovery",
   source_route_hash="sha256:"+"4"*64,node_or_group_leg="execute",
   original_attempt_id=self.attempt,remaining_cascade=1)
  def batch_run(command,**_kwargs):
   identity=D.process_launch_identity(os.getpid())
   replacement={
    **identity,"route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
    "route_node":"execute","attempt_id":claim.retry_attempt_id,
    "parent":"owner","parent_attempt_id":"att-owner",
    "batch_route_node":"execute","batch_group":"recovery-group",
    "launch_started":"1",
    "launch_lifecycle":"detached","launch_fence":"registry-v1",
   }
   self.jobs.write_text(
    self.jobs.read_text()+row("open","replacement",replacement)+"\n")
   receipt={
    "schema_version":2,"state":"idempotent-mixed","action":"start",
    "parallel_group":"recovery-group","continuation_id":"cont-production",
    "replacement_attempt_id":claim.retry_attempt_id,
    "original_group_cardinality":2,"reused_peer_count":1,
    "newly_started":1,"existing":1,
    "legs":[
     {
      "node":"successful-peer","attempt_id":"att-successful-peer",
      "launch_state":"existing","reason":"reused-successful-peer",
      "registered":"0","started":"0","child_spawned":"0",
     },
     {
      "node":"execute","attempt_id":claim.retry_attempt_id,
      "launch_state":"started","registered":"1","started":"1",
      "child_spawned":"1",
     },
    ],
   }
   return SimpleNamespace(returncode=0,stdout=json.dumps(receipt)+"\n",stderr="")
  with mock.patch.dict(os.environ,{
   "AGENT_DISPATCH_SELF_SLUG":"wrong-owner",
   "AGENT_DISPATCH_ATTEMPT_ID":"att-owner",
  }),mock.patch.object(R.subprocess,"run") as not_called:
   with self.assertRaises(R.RecoveryError) as mismatch:
    services.start_gap(request,continuation,claim)
   self.assertEqual(mismatch.exception.reason,"recovery-parent-identity-mismatch")
   not_called.assert_not_called()
  with mock.patch.dict(os.environ,{
   "AGENT_DISPATCH_SELF_SLUG":"owner",
   "AGENT_DISPATCH_ATTEMPT_ID":"att-owner",
  }),mock.patch.object(R.subprocess,"run",side_effect=batch_run) as invoked:
   started=services.start_gap(request,continuation,claim)
  self.assertEqual(
   (started["registered"],started["started"],started["child_spawned"]),(1,1,1))
  self.assertEqual(invoked.call_args.args[0],[
   sys.executable,str(ROOT/"utilities"/"dispatch-batch.py"),
   "--route",str(request.route_file),
   "--continuation",str(route_path),
   "--parallel-group","recovery-group","--action","start",
   "--jobs",str(self.jobs),"--slug-prefix","execute","--parent","owner",
  ])

 def test_production_route_drift_and_command_receipts_fail_closed(self):
  request,recovery_id,manifest,digest,legs=self.production_fixture()
  services=R.ProductionRecoveryServices()
  continuation,_official,route_path=self.publish_production_fixture(
   services,request,recovery_id,manifest,digest,legs)
  route_path.write_bytes(route_path.read_bytes()+b" ")
  with self.assertRaises(R.RecoveryError) as drift:
   services.observe_continuation(request,recovery_id)
  self.assertEqual(drift.exception.reason,"recovery-continuation-route-drift")
  self.assertTrue(continuation["admitted"])

  for completed,reason in (
   (SimpleNamespace(returncode=70,stdout="",stderr="failed"),
    "continuation-command-failed"),
   (SimpleNamespace(returncode=0,stdout="not-json\n",stderr=""),
    "continuation-command-receipt-invalid"),
  ):
   with self.subTest(reason=reason):
    request,recovery_id,_manifest,_digest,_legs=self.production_fixture()
    official=self.official_continuation(
     request,_manifest,_digest,_legs)
    with mock.patch.object(
     R,"_preview_continuation_route",return_value=official,
    ),mock.patch.object(R.subprocess,"run",return_value=completed):
     with self.assertRaises(R.RecoveryError) as caught:
      services.publish_continuation(
       request,R.exact_attempt(self.jobs,self.attempt),recovery_id)
    self.assertEqual(caught.exception.reason,reason)

 def test_production_source_manifest_and_group_mismatches_fail_closed(self):
  services=R.ProductionRecoveryServices()
  cases=(
   ("source-route", "recovery-source-route-binding-mismatch"),
   ("manifest", "recovery-source-manifest-row-census-mismatch"),
   ("group", "recovery-source-group-mismatch"),
  )
  for mutation,reason in cases:
   with self.subTest(mutation=mutation):
    request,recovery_id,_manifest,digest,_legs=self.production_fixture()
    if mutation=="source-route":
     route=json.loads(Path(request.route_file).read_text())
     route["route_hash"]="sha256:"+"7"*64
     Path(request.route_file).write_bytes(R._canonical_bytes(route)+b"\n")
    else:
     registry=self.jobs.read_text()
     if mutation=="manifest":
      registry=registry.replace(
       f"batch_manifest_sha256={digest}",
       "batch_manifest_sha256=sha256:"+"8"*64,1)
     else:
      registry=registry.replace(
       "batch_group=recovery-group","batch_group=foreign-group",1)
     self.jobs.write_text(registry)
    with mock.patch.object(R,"_preview_continuation_route") as preview, \
         mock.patch.object(R.subprocess,"run") as command:
     with self.assertRaises(R.RecoveryError) as caught:
      services.publish_continuation(
       request,R.exact_attempt(self.jobs,self.attempt),recovery_id)
    self.assertEqual(caught.exception.reason,reason)
    preview.assert_not_called()
    command.assert_not_called()


if __name__=="__main__": unittest.main()
