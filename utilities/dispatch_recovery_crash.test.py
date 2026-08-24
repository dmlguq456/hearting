#!/usr/bin/env python3
"""AT9 crash matrix for the durable receiptless-recovery coordinator."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT=Path(__file__).resolve().parents[1]
BASE_SPEC=importlib.util.spec_from_file_location(
 "dispatch_recovery_fixture_base",ROOT/"utilities"/"dispatch_recovery.test.py")
BASE=importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name]=BASE
BASE_SPEC.loader.exec_module(BASE)

D=BASE.D
R=BASE.R


CRASH_MATRIX=(
 ("after-intent-before-cancellation","intent-observed"),
 ("before-receipt-commit",None),
 ("after-cancellation-mutation-before-receipt-phase",None),
 ("after-receipt-before-continuation","cancellation-receipt-committed"),
 ("after-continuation-before-claim","continuation-published"),
 ("after-claim-before-start","retry-claimed"),
 ("after-start","wrapper-started"),
 ("before-blocked-close",None),
 ("after-blocked-close","terminal-or-blocked"),
)


class CrashMatrixServices(BASE.FakeServices):
 def __init__(self,crash_point):
  super().__init__(terminal_mode="receipt-unavailable")
  self.crash_point=crash_point
  self.before_receipt_crashed=False
  self.before_blocked_crashed=False

 def cancel_receiptless(self,request):
  if self.crash_point=="before-receipt-commit" and not self.before_receipt_crashed:
   self.before_receipt_crashed=True
   raise R.InjectedRecoveryCrash(self.crash_point)
  result=super().cancel_receiptless(request)
  if (self.crash_point=="after-cancellation-mutation-before-receipt-phase"
      and not self.before_receipt_crashed):
   self.before_receipt_crashed=True
   raise R.InjectedRecoveryCrash(self.crash_point)
  return result

 def observe_terminal(self,request,claim):
  if self.crash_point=="before-blocked-close" and not self.before_blocked_crashed:
   self.before_blocked_crashed=True
   raise R.InjectedRecoveryCrash(self.crash_point)
  return super().observe_terminal(request,claim)


class DispatchRecoveryCrashMatrixTest(unittest.TestCase):
 def fixture(self,base):
  jobs=base/"jobs.log";route=base/"route.json"
  route.write_text("{}\n",encoding="utf-8")
  source=BASE.source_metadata("att-source-recovery")
  peer=BASE.row("done","successful-peer",{
   "route_id":"rt-recovery","route_hash":"sha256:"+"4"*64,
   "route_node":"successful-peer","attempt_id":"att-successful-peer",
   "note":"completed-marker","failure_class":"pass",
  })
  jobs.write_text(
   BASE.row("open","execute",source)+"\n"+peer+"\n",encoding="utf-8")
  request=R.RecoveryRequest(
   jobs,"att-source-recovery",route,"execute","execute",cancellation_wait=0)
  return jobs,peer,request

 def durable_records(self,jobs):
  root=jobs.parent/"recovery"
  return [
   json.loads(path.read_text(encoding="utf-8"))
   for path in root.glob("rec-*.json")
  ] if root.is_dir() else []

 def assert_at9_terminal(self,jobs,peer,services,result):
  lines=jobs.read_text(encoding="utf-8").splitlines()
  rows=[]
  for raw in lines:
   fields=raw.split("\t")
   if len(fields)==6:
    rows.append((fields,D.parse_registry_metadata(fields[5])))
  retry_rows=[
   (fields,metadata) for fields,metadata in rows
   if metadata.get("attempt_id","").startswith("att-retry-")]
  self.assertLessEqual(len(retry_rows),1)
  self.assertEqual(len(retry_rows),1)
  retry_id=retry_rows[0][1]["attempt_id"]
  source=next(
   metadata for _fields,metadata in rows
   if metadata.get("attempt_id")=="att-source-recovery")
  self.assertEqual(source["retry_attempt_id"],retry_id)
  self.assertEqual(result.retry_attempt_id,retry_id)
  self.assertEqual(services.start_calls,1)
  self.assertEqual(len(lines),3)
  self.assertIn(peer,lines)

  records=self.durable_records(jobs)
  self.assertEqual(len(records),1)
  record=records[0]
  terminal=record["phases"].get("terminal-or-blocked")
  self.assertIsNotNone(terminal)
  self.assertEqual(record["current_phase"],"terminal-or-blocked")
  self.assertEqual(terminal["evidence"]["outcome"],"blocked")
  self.assertEqual(
   terminal["evidence"]["reason"],"receipt-unavailable-retry-exhausted")
  self.assertEqual(terminal["evidence"]["retry_attempt_id"],retry_id)
  self.assertEqual(sum(
   "terminal-or-blocked" in item["phases"] for item in records),1)
  intents=list((jobs.parent/"recovery"/"intents").glob("*.json"))
  self.assertEqual(len(intents),1)
  self.assertEqual(
   json.loads(intents[0].read_text())["current_phase"],"intent-observed")

  started=record["phases"].get("wrapper-started",{}).get("evidence",{})
  self.assertEqual(started.get("child_spawned"),1)
  self.assertEqual(started.get("retry_attempt_id"),retry_id)
  self.assertEqual(started.get("row_count"),1)
  self.assertIn(started.get("process_count"),(0,1))

 def test_at9_all_crash_boundaries_replay_to_one_blocked_retry(self):
  for crash_point,checkpoint_phase in CRASH_MATRIX:
   with self.subTest(crash_point=crash_point),tempfile.TemporaryDirectory() as td:
    jobs,peer,request=self.fixture(Path(td))
    services=CrashMatrixServices(crash_point)
    checkpoint_crashed=False
    def checkpoint(phase):
     nonlocal checkpoint_crashed
     if phase==checkpoint_phase and not checkpoint_crashed:
      checkpoint_crashed=True
      raise R.InjectedRecoveryCrash(crash_point)
    try:
     with self.assertRaises(R.InjectedRecoveryCrash):
      R.coordinate_recovery(request,services,checkpoint=checkpoint)
     if crash_point=="after-intent-before-cancellation":
      self.assertEqual(services.cancel_calls,0)
     result=R.coordinate_recovery(request,services)
     self.assertEqual((result.state,result.reason),(
      "blocked","receipt-unavailable-retry-exhausted"))
     replay=R.coordinate_recovery(request,services)
     self.assertEqual(replay.recovery_id,result.recovery_id)
     self.assertEqual(replay.retry_attempt_id,result.retry_attempt_id)
     self.assertEqual(services.cancel_calls,1)
     self.assertEqual(services.publish_calls,1)
     self.assert_at9_terminal(jobs,peer,services,replay)
    finally:
     services.cleanup()


if __name__=="__main__": unittest.main()
