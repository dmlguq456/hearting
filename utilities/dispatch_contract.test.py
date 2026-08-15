#!/usr/bin/env python3
import fcntl, hashlib, json, os, subprocess, sys, tempfile, time, unittest
from unittest import mock
from pathlib import Path

P=Path(__file__).with_name("dispatch_contract.py")
sys.path.insert(0,str(P.parent))
import dispatch_contract as D
from replica_batch_contract import build_manifest

CURRENT="attempt_schema_version=2,dispatch_depth=2,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless"

class DispatchContractTest(unittest.TestCase):
 def test_versioned_source_registry_fallback_matrix(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)
   runtime=base/"runtime"
   bundle=runtime/".harness"/"bundles"/"bundle-id"/"source"
   release=base/".local"/"share"/"hearting"/"releases"/"v2.41.0"
   checkout=base/"hearting-checkout"
   bundle.mkdir(parents=True);release.mkdir(parents=True);checkout.mkdir()

   selected=D.resolve_global_registry(bundle,None,1,"start",{})
   self.assertEqual(selected.path,runtime/".harness"/"dispatch"/"jobs.log")
   self.assertEqual(selected.source,"activation-runtime")
   self.assertEqual(
    D.resolve_dispatch_state_root(bundle,environ={}),
    runtime/".harness"/"dispatch")

   # State-root chain (3): the shared managed release keeps its release-relative
   # registry — rotation succession (_cleanup_releases, release-lifecycle T-1/T-2)
   # carries that state into the successor release, so fail-closing here would
   # break the established succession contract.
   self.assertEqual(
    D.resolve_global_registry(release,None,1,"start",{}).path,
    (release/".dispatch"/"jobs.log").resolve())
   self.assertEqual(
    D.resolve_dispatch_state_root(release,environ={}),
    (release/".dispatch").resolve())

   internal=bundle/".dispatch"/"jobs.log"
   for resolver in (
    lambda: D.resolve_global_registry(checkout,str(internal),1,"start",{}),
    lambda: D.resolve_global_registry(
     checkout,None,1,"start",{"AGENT_DISPATCH_JOBS":str(internal)}),
    lambda: D.resolve_dispatch_state_root(checkout,explicit_jobs=internal,environ={}),
    lambda: D.resolve_dispatch_state_root(
     checkout,environ={"AGENT_DISPATCH_JOBS":str(internal)}),
   ):
    with self.assertRaises(D.DispatchContractError) as caught:
     resolver()
    self.assertEqual(caught.exception.reason,"versioned-source-registry-fallback")

   maintained=checkout/".dispatch"/"jobs.log"
   self.assertEqual(
    D.resolve_global_registry(checkout,None,1,"start",{}).path,maintained)
   self.assertEqual(
    D.resolve_dispatch_state_root(checkout,environ={}),maintained.parent)

 def test_codex_standard_owner_network_profile_is_exactly_scoped(self):
  self.assertTrue(D.codex_standard_owner_network_enabled(
   dispatch_depth=1, worker_type="owner", intensity="standard",
   sandbox="workspace-write"))
  for changed in (
   {"dispatch_depth":2}, {"worker_type":"stage"}, {"intensity":"quick"},
   {"sandbox":"danger-full-access"},
  ):
   values={"dispatch_depth":1, "worker_type":"owner", "intensity":"standard",
           "sandbox":"workspace-write"}
   values.update(changed)
   self.assertFalse(D.codex_standard_owner_network_enabled(**values), changed)

 def owner_row(self,attempt,pid,start,slug="owner",extra=""):
  return (f"2026-07-23T00:00:00Z\topen\t/repo\t/wt\t{slug}\t"
          "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
          "execution_surface=registered-headless,registered_worker=1,"
          "fallback_hop=same-harness-headless,worker_type=owner,"
          f"attempt_id={attempt},pid={pid},pid_start={start}{extra}")

 def parent_row(self,attempt,pid,start,*,status="open",worktree="/wt",
                repo="/repo",slug="owner",extra=""):
  return (f"2026-08-13T00:00:00Z\t{status}\t{repo}\t{worktree}\t{slug}\t"
          "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
          "execution_surface=registered-headless,registered_worker=1,"
          "fallback_hop=same-harness-headless,worker_type=owner,"
          f"attempt_id={attempt},pid={pid},pid_start={start}{extra}")

 # F-1: parent_completion_window's typed `source` pins the fail-closed axes
 # (§F-1.2) — one fixture per source value, absent/not-open/ambiguous/
 # contract-invalid/foreign-slug/foreign-worktree/pid-start-mismatch fail
 # closed, only live-process/live-supervisor-lease defer.
 def test_parent_completion_window_source_axes(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log"
   child_fields=["2026-08-13T00:00:00Z","open","/repo","/wt","leg","meta"]

   jobs.write_text("")
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,{}).source,
    "parent-attempt-absent")

   child_meta={"parent_attempt_id":"att-parent","parent":"owner"}
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,child_meta).source,
    "parent-attempt-absent")

   jobs.write_text(self.parent_row("att-parent",999999,"1",status="done")+"\n")
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,child_meta).source,
    "parent-attempt-not-open")

   jobs.write_text(
    self.parent_row("att-parent",999999,"1")+"\n"+
    self.parent_row("att-parent",999998,"1")+"\n")
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,child_meta).source,
    "parent-attempt-ambiguous")

   jobs.write_text(
    "2026-08-13T00:00:00Z\topen\t/repo\t/wt\towner\t"
    "attempt_schema_version=1,attempt_id=att-parent\n")
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,child_meta).source,
    "parent-contract-invalid")

   jobs.write_text(self.parent_row("att-parent",999999,"1",slug="different")+"\n")
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,child_meta).source,
    "parent-identity-foreign")

   jobs.write_text(
    self.parent_row("att-parent",999999,"1",worktree="/wt-foreign")+"\n")
   self.assertEqual(
    D.parent_completion_window(jobs,child_fields,child_meta).source,
    "parent-identity-foreign")

   proc=subprocess.Popen(["sleep","30"])
   try:
    jobs.write_text(self.parent_row("att-parent",proc.pid,"1")+"\n")
    window=D.parent_completion_window(jobs,child_fields,child_meta)
    self.assertFalse(window.deferred)
    self.assertTrue(window.source.startswith("parent-not-live:"),window.source)

    start=D.process_start_ticks(proc.pid)
    jobs.write_text(self.parent_row("att-parent",proc.pid,start)+"\n")
    self.assertEqual(
     D.parent_completion_window(jobs,child_fields,child_meta),
     D.ParentCompletionWindow(True,"parent-live:process"))
   finally:
    proc.kill();proc.wait(timeout=5)

   lease=D.supervisor_lease_path(jobs,"att-parent")
   lease.parent.mkdir(parents=True)
   holder=lease.open("a+")
   fcntl.flock(holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:
    nonce="a"*64
    extra=(",harness=codex,runtime_sandbox=workspace-write,"
           "completion_delivery=app-server-supervised,"
           f"supervisor_lease={D.SUPERVISOR_LEASE_KIND},"
           f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce},"
           "pid_scope=namespace-local,"
           "pid_observer_ns=pid:[outer],pid_ns=pid:[outer]")
    jobs.write_text(self.parent_row("att-parent",424242,"1",extra=extra)+"\n")
    holder.seek(0);holder.truncate()
    holder.write(f"kind={D.SUPERVISOR_LEASE_KIND}\nattempt_id=att-parent\nnonce={nonce}\n")
    holder.flush()
    with mock.patch.object(D,"process_namespace_identity",return_value="pid:[inner]"):
     window=D.parent_completion_window(jobs,child_fields,child_meta)
    self.assertEqual(window,D.ParentCompletionWindow(True,"parent-live:supervisor-lease"))
   finally:
    fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close()

 def test_live_parent_binding_is_attempt_exact_and_same_slug_safe(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   first=subprocess.Popen(["sleep","60"])
   second=subprocess.Popen(["sleep","60"])
   try:
    first_start=D.process_start_ticks(first.pid);second_start=D.process_start_ticks(second.pid)
    jobs.write_text(self.owner_row("att-parent-first",first.pid,first_start)+"\n"+
                    self.owner_row("att-parent-second",second.pid,second_start)+"\n")
    with self.assertRaises(D.DispatchContractError) as caught:
     D.resolve_live_parent_attempt(jobs,parent_slug="owner",repo="/repo",worktree="/wt")
    self.assertEqual(caught.exception.reason,"parent-attempt-ambiguous")
    binding=D.resolve_live_parent_attempt(
     jobs,parent_slug="owner",repo="/repo",worktree="/wt",
     expected_attempt_id="att-parent-second")
    self.assertEqual(binding.attempt_id,"att-parent-second")
    self.assertEqual((binding.observed_pid,binding.observed_pid_start),(second.pid,second_start))
   finally:
    for proc in (first,second):
     if proc.poll() is None:proc.kill()
     proc.wait()

 def test_dead_or_identity_missing_parent_prevents_claim(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   jobs.write_text(self.owner_row("att-parent-dead",99999971,"1")+"\n")
   with self.assertRaises(D.DispatchContractError) as caught:
    D.resolve_live_parent_attempt(
     jobs,parent_slug="owner",repo="/repo",worktree="/wt",
     expected_attempt_id="att-parent-dead")
   self.assertEqual(caught.exception.reason,"parent-attempt-not-live")

 def test_supervised_lease_is_live_only_when_exact_held_and_pid_namespace_unverifiable(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";attempt="att-parent-lease"
   parent=subprocess.Popen(["sleep","60"])
   lease=D.supervisor_lease_path(jobs,attempt)
   lease.parent.mkdir(parents=True)
   holder=lease.open("a+")
   fcntl.flock(holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:
    start=D.process_start_ticks(parent.pid)
    nonce="a"*64
    extra=(",harness=codex,runtime_sandbox=workspace-write,"
           "completion_delivery=app-server-supervised,"
           f"supervisor_lease={D.SUPERVISOR_LEASE_KIND},"
           f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce},"
           "pid_scope=namespace-local,"
           "pid_observer_ns=pid:[outer],pid_ns=pid:[outer]")
    jobs.write_text(self.owner_row(attempt,parent.pid,start,extra=extra)+"\n")
    holder.seek(0);holder.truncate()
    holder.write(f"kind={D.SUPERVISOR_LEASE_KIND}\nattempt_id={attempt}\nnonce={nonce}\n")
    holder.flush()
    with mock.patch.object(D,"process_namespace_identity",return_value="pid:[inner]"):
     binding=D.resolve_live_parent_attempt(
      jobs,parent_slug="owner",repo="/repo",worktree="/wt",
      expected_attempt_id=attempt)
    self.assertEqual(binding.liveness_source,"supervisor-lease")
    self.assertIsNone(binding.observed_pid)
    self.assertTrue(D.parent_attempt_binding_is_live(jobs,binding))
    sealed_row=jobs.read_text()
    jobs.write_text(sealed_row.replace(
     f"supervisor_lease_nonce={nonce}",f"supervisor_lease_nonce={'e'*64}"))
    self.assertFalse(D.parent_attempt_binding_is_live(jobs,binding))
    jobs.write_text(sealed_row)

    fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close();holder=None
    with mock.patch.object(D,"process_namespace_identity",return_value="pid:[inner]"):
     with self.assertRaises(D.DispatchContractError) as released:
      D.resolve_live_parent_attempt(
       jobs,parent_slug="owner",repo="/repo",worktree="/wt",
       expected_attempt_id=attempt)
    self.assertEqual(released.exception.reason,"parent-attempt-not-live")

    foreign_holder=lease.open("w+")
    foreign_holder.write("kind=flock-v1\nattempt_id=att-foreign\nnonce="+("f"*64)+"\n")
    foreign_holder.flush()
    fcntl.flock(foreign_holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
     with mock.patch.object(D,"process_namespace_identity",return_value="pid:[inner]"):
      with self.assertRaises(D.DispatchContractError) as foreign:
       D.resolve_live_parent_attempt(
        jobs,parent_slug="owner",repo="/repo",worktree="/wt",
        expected_attempt_id=attempt)
     self.assertEqual(foreign.exception.reason,"parent-attempt-not-live")
    finally:
     fcntl.flock(foreign_holder.fileno(),fcntl.LOCK_UN);foreign_holder.close()

    target=base/"foreign-lease";target.touch()
    lease.unlink();lease.symlink_to(target)
    target_holder=target.open("a+")
    fcntl.flock(target_holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
     with mock.patch.object(D,"process_namespace_identity",return_value="pid:[inner]"):
      with self.assertRaises(D.DispatchContractError) as symlinked:
       D.resolve_live_parent_attempt(
        jobs,parent_slug="owner",repo="/repo",worktree="/wt",
        expected_attempt_id=attempt)
     self.assertEqual(symlinked.exception.reason,"parent-attempt-not-live")
    finally:
     fcntl.flock(target_holder.fileno(),fcntl.LOCK_UN);target_holder.close()
   finally:
    if holder is not None:
     fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close()
    if parent.poll() is None:parent.kill()
    parent.wait()

 def test_supervised_lease_never_overrides_positive_pid_reuse(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";attempt="att-parent-reused"
   lease=D.supervisor_lease_path(jobs,attempt);lease.parent.mkdir(parents=True)
   holder=lease.open("a+");fcntl.flock(holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:
    nonce="b"*64
    extra=(",harness=codex,runtime_sandbox=workspace-write,"
           "completion_delivery=app-server-supervised,"
           f"supervisor_lease={D.SUPERVISOR_LEASE_KIND},"
           f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce}")
    jobs.write_text(self.owner_row(attempt,437,"20",extra=extra)+"\n")
    holder.write(f"kind={D.SUPERVISOR_LEASE_KIND}\nattempt_id={attempt}\nnonce={nonce}\n")
    holder.flush()
    with mock.patch.object(
        D,"_proc_observation",return_value=("present","different","S")):
     with self.assertRaises(D.DispatchContractError) as caught:
      D.resolve_live_parent_attempt(
       jobs,parent_slug="owner",repo="/repo",worktree="/wt",
       expected_attempt_id=attempt)
    self.assertEqual(caught.exception.reason,"parent-attempt-not-live")
   finally:
    fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close()

 def test_supervisor_lease_file_is_preserved_for_recovery_exception(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";attempt="att-parent-recovery"
   lease=D.supervisor_lease_path(jobs,attempt);nonce="d"*64
   extra=(",harness=codex,runtime_sandbox=workspace-write,"
          "completion_delivery=app-server-supervised,"
          f"supervisor_lease={D.SUPERVISOR_LEASE_KIND},"
          f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce}")
   jobs.write_text(self.owner_row(attempt,437,"20",extra=extra)+"\n")
   manager=D.hold_supervisor_lease(jobs,attempt,lease)
   manager.__enter__()
   error=RuntimeError("recovery")
   manager.__exit__(RuntimeError,error,error.__traceback__)
   self.assertTrue(lease.is_file())
   self.assertFalse(D.supervisor_lease_is_held(jobs,D.parse_registry_metadata(
    jobs.read_text().split("\t",5)[5])))
   self.assertTrue(D.remove_supervisor_lease(lease))

 def test_claude_and_codex_share_parked_supervisor_liveness(self):
  for harness,delivery in (
   ("claude","session-resume-supervised"),
   ("codex","app-server-supervised"),
  ):
   with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td:
    base=Path(td);jobs=base/"jobs.log";attempt=f"att-{harness}-parked"
    lease=D.supervisor_lease_path(jobs,attempt);lease.parent.mkdir(parents=True)
    nonce=("a" if harness=="claude" else "b")*64
    extra=(f",harness={harness},runtime_sandbox=workspace-write,"
           f"completion_delivery={delivery},"
           f"supervisor_lease={D.SUPERVISOR_LEASE_KIND},"
           f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce}")
    row=self.owner_row(attempt,99999991,"1",extra=extra)
    jobs.write_text(row+"\n")
    holder=lease.open("a+")
    holder.write(f"kind={D.SUPERVISOR_LEASE_KIND}\nattempt_id={attempt}\nnonce={nonce}\n")
    holder.flush();fcntl.flock(holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
     metadata=D.parse_registry_metadata(row.split("\t",5)[5])
     observed=D.observed_supervised_owner_liveness(
      jobs,"open",metadata,supervisor_phase="parked")
     self.assertEqual(observed.state,"parked-supervised")
     self.assertEqual(observed.reason,"supervisor-parked")
    finally:
     fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close()
    stale=D.observed_supervised_owner_liveness(
     jobs,"open",metadata,supervisor_phase="parked")
    self.assertNotEqual(stale.state,"parked-supervised")

 def test_parent_repo_identity_canonicalizes_primary_and_linked_but_keeps_worktree_exact(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);primary=base/"primary";linked=base/"linked"
   subprocess.run(["git","init","-q",str(primary)],check=True)
   subprocess.run(["git","-C",str(primary),"config","user.name","test"],check=True)
   subprocess.run(["git","-C",str(primary),"config","user.email","test@example.com"],check=True)
   (primary/"seed").write_text("seed\n")
   subprocess.run(["git","-C",str(primary),"add","seed"],check=True)
   subprocess.run(["git","-C",str(primary),"commit","-qm","seed"],check=True)
   subprocess.run(["git","-C",str(primary),"worktree","add","-q","-b","linked",str(linked)],check=True)
   proc=subprocess.Popen(["sleep","60"],start_new_session=True)
   try:
    start=D.process_start_ticks(proc.pid);jobs=base/"jobs.log"
    row=self.owner_row("att-parent-canonical",proc.pid,start)
    row=row.replace("\t/repo\t/wt\t",f"\t{primary}\t{linked}\t")
    jobs.write_text(row+"\n")
    binding=D.resolve_live_parent_attempt(
     jobs,parent_slug="owner",repo=str(linked),worktree=str(linked),
     expected_attempt_id="att-parent-canonical")
    self.assertEqual(binding.repository_identity,
                     D.canonical_repository_identity(primary))
    with self.assertRaises(D.DispatchContractError) as wrong_worktree:
     D.resolve_live_parent_attempt(
      jobs,parent_slug="owner",repo=str(linked),worktree=str(primary),
      expected_attempt_id="att-parent-canonical")
    self.assertEqual(wrong_worktree.exception.reason,"parent-attempt-not-found")
    foreign=base/"foreign";subprocess.run(["git","init","-q",str(foreign)],check=True)
    with self.assertRaises(D.DispatchContractError) as wrong_repo:
     D.resolve_live_parent_attempt(
      jobs,parent_slug="owner",repo=str(foreign),worktree=str(linked),
      expected_attempt_id="att-parent-canonical")
    self.assertEqual(wrong_repo.exception.reason,"parent-attempt-not-found")
   finally:
    if proc.poll() is None:proc.kill()
    proc.wait()

 def test_observed_liveness_and_terminal_reconcile_are_exact_and_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   meta=("attempt_schema_version=2,dispatch_depth=1,transport=headless,"
         "execution_surface=registered-headless,registered_worker=1,"
         "fallback_hop=same-harness-headless,worker_type=owner,"
         "attempt_id=att-reconcile,launch_outcome=reaped-before-publish")
   jobs.write_text("2026-07-24T00:00:00Z\topen\t/r\t/w\towner\t"+meta+"\n")
   parsed=D.parse_registry_metadata(meta)
   without=D.observed_attempt_liveness("open",parsed)
   with_envelope=D.observed_attempt_liveness(
    "open",parsed,terminal_envelope=True)
   self.assertEqual((without.state,without.reason),
                    ("reconcile-needed","process-exited"))
   self.assertEqual((with_envelope.state,with_envelope.reason),
                    ("reconcile-needed","terminal-observed"))
   self.assertEqual(
    D.reconcile_attempt_terminal(
     jobs,"att-reconcile","dead-capacity",
     evidence={"failure_class":"capacity","terminal_event":"claude-result"}),
    "closed")
   self.assertEqual(
    D.reconcile_attempt_terminal(jobs,"att-reconcile","dead-runtime-exit"),
    "already-terminal")
   text=jobs.read_text()
   self.assertIn("\tdone\t/r\t/w\towner\t",text)
   self.assertIn("note=dead-capacity",text)
   self.assertIn("failure_class=capacity",text)

 def test_launch_identity_records_outer_pid_start_and_group(self):
  proc=subprocess.Popen(["sleep","60"],start_new_session=True)
  try:
   identity=D.process_launch_identity(proc.pid)
   self.assertEqual(identity["pid"],str(proc.pid))
   self.assertEqual(identity["pid_start"],D.process_start_ticks(proc.pid))
   self.assertEqual(identity.get("pid_host"),str(proc.pid))
   self.assertEqual(identity.get("pid_host_start"),identity["pid_start"])
   expected_host_ns=D.process_namespace_identity(1) or identity.get("pid_observer_ns")
   self.assertEqual(identity.get("pid_host_ns"),expected_host_ns)
   self.assertEqual(identity.get("pid_host_proof"),D.PID_HOST_NAMESPACE_PROOF)
   self.assertEqual(identity.get("pgid"),str(proc.pid))
  finally:
   proc.kill();proc.wait()

 def test_supervisor_handoff_repairs_lower_authority_foreground_verdict(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   meta=("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
         "execution_surface=registered-headless,registered_worker=1,"
         "fallback_hop=same-harness-headless,attempt_id=att-race,"
         "note=dead-worker-fail,failure_class=fail,"
         "detected_by=foreground-terminal-handoff,"
         "classifier_source=foreground-tail-v1")
   jobs.write_text("2026-08-06T00:00:00Z\tdone\t/r\t/w\tstage\t"+meta+"\n")
   result=D.reconcile_attempt_terminal(
    jobs,"att-race","completed-supervisor",
    evidence={
     "failure_class":"pass",
     "classifier_source":"supervisor-terminal-v1",
     "detected_by":"completion-supervisor",
     "terminal_event":"turn.completed",
     "reconcile_reason":"exact-final-handoff",
    })
   self.assertEqual(result,"repaired-terminal")
   text=jobs.read_text()
   self.assertIn("note=completed-supervisor",text)
   self.assertIn("prior_failure_class=fail",text)
   self.assertIn("terminal_conflict=1",text)

 def test_equal_authority_verdict_conflict_fails_closed(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   meta=("attempt_schema_version=2,dispatch_depth=1,transport=headless,"
         "execution_surface=registered-headless,registered_worker=1,"
         "fallback_hop=same-harness-headless,"
         "attempt_id=att-conflict,note=completed-supervisor,"
         "failure_class=pass,classifier_source=supervisor-terminal-v1")
   jobs.write_text("2026-08-06T00:00:00Z\tdone\t/r\t/w\towner\t"+meta+"\n")
   result=D.reconcile_attempt_terminal(
    jobs,"att-conflict","dead-worker-fail",
    evidence={"failure_class":"fail","classifier_source":"supervisor-terminal-v1"})
   self.assertEqual(result,"terminal-conflict")
   self.assertIn("note=dead-terminal-conflict",jobs.read_text())

 def test_remounted_proc_nspid_is_bound_to_inner_namespace(self):
  inner_namespace="pid:[inner-remounted]"
  def namespace_identity(pid="self"):
   return None if pid == 1 else inner_namespace
  with mock.patch.object(D,"process_namespace_identity",side_effect=namespace_identity), \
       mock.patch.object(D,"process_start_ticks",return_value="42"), \
       mock.patch.object(D,"process_namespace_pids",return_value=(7,)), \
       mock.patch.object(D.os,"getpgid",return_value=7):
   identity=D.process_launch_identity(7)
  self.assertEqual(identity["pid_host"],"7")
  self.assertEqual(identity["pid_host_ns"],inner_namespace)
  self.assertEqual(identity["pid_host_proof"],D.PID_HOST_NAMESPACE_PROOF)
  with mock.patch.object(D,"process_namespace_identity",return_value="pid:[outer]"), \
       mock.patch.object(D,"_proc_observation") as observation:
   result=D.attempt_process_quiescence({**identity,"pid_scope":"namespace-local"})
  self.assertEqual((result.state,result.reason),
                   ("unverifiable","process-namespace-unverifiable"))
  observation.assert_not_called()

 def test_exact_process_quiescence_is_live_then_ready_only_after_reap(self):
  proc=subprocess.Popen(
   [sys.executable,"-c","import time; time.sleep(30)"],
   start_new_session=True)
  try:
   metadata=D.process_launch_identity(proc.pid)
   live=D.attempt_process_quiescence(metadata)
   self.assertEqual(live.state,"live")
   self.assertEqual(live.pid,proc.pid)
   proc.terminate();proc.wait(timeout=5)
   gone=D.attempt_process_quiescence(metadata)
   self.assertEqual(gone.state,"quiescent")
   self.assertIn("pid-gone",gone.reason)
  finally:
   if proc.poll() is None:proc.kill()
   proc.wait()

 def test_process_quiescence_fail_closed_and_terminal_edge_matrix(self):
  self.assertEqual(
   D.attempt_process_quiescence({}).state,"unverifiable")
  self.assertEqual(
   D.attempt_process_quiescence({"launch_outcome":"never-launched"}).state,
   "quiescent")
  with mock.patch.object(D,"process_namespace_identity",return_value="pid:[other]"):
   mismatch=D.attempt_process_quiescence({
    "pid":str(os.getpid()),"pid_start":D.process_start_ticks(os.getpid()),
    "pid_scope":"namespace-local","pid_observer_ns":"pid:[source]",
   })
  self.assertEqual((mismatch.state,mismatch.reason),
                   ("unverifiable","process-namespace-unverifiable"))
  host_metadata={
   "pid":"437","pid_start":"20","pid_scope":"namespace-local",
   "pid_observer_ns":"pid:[source]","pid_host":"1437","pid_host_start":"20",
   "pid_host_ns":"pid:[observer]",
  }
  with mock.patch.object(D,"process_namespace_identity",return_value="pid:[observer]"), \
       mock.patch.object(D,"_proc_observation",return_value=("present","20","S")):
   no_proof=D.attempt_process_quiescence(host_metadata)
   legacy_proof=D.attempt_process_quiescence({
    **host_metadata,"pid_host_proof":"nspid-outermost"})
   proven=D.attempt_process_quiescence({
    **host_metadata,"pid_host_proof":D.PID_HOST_NAMESPACE_PROOF})
  self.assertEqual(no_proof.state,"unverifiable")
  self.assertEqual(legacy_proof.state,"unverifiable")
  self.assertEqual((proven.state,proven.pid),("live",1437))
  with mock.patch.object(D,"_proc_observation",return_value=("present","different","S")):
   reused=D.attempt_process_quiescence({
    "pid":str(os.getpid()),"pid_start":"original","pid_scope":"host-visible",
   })
  self.assertEqual((reused.state,reused.reason),("quiescent","local-pid-reused"))

 def test_foreground_reap_note_cannot_override_a_live_recorded_process(self):
  metadata={
   "pid":str(os.getpid()),
   "pid_start":D.process_start_ticks(os.getpid()),
   "launch_outcome":"governed-process-reaped",
  }
  result=D.attempt_process_quiescence(metadata)
  self.assertEqual(result.state,"live")

 def test_process_identity_and_group_observation_fail_closed_on_unknown(self):
  with mock.patch.object(
      D,"_proc_observation",return_value=("inaccessible","", "")):
   self.assertFalse(D.process_identity_is_live(123,"42"))
  with mock.patch.object(
      D,"_proc_observation",return_value=("missing","", "")):
   self.assertFalse(D.process_identity_is_live(123,"42"))
  with mock.patch.object(D.Path,"iterdir",side_effect=PermissionError(13,"denied")):
   observation=D.process_group_observation(77)
  self.assertEqual(observation.state,"unverifiable")
  self.assertIn("procfs-enumeration",observation.reason)

 def test_known_group_member_outranks_partial_procfs_scan_failure(self):
  entries=(Path("/proc/101"),Path("/proc/102"))
  stat="101 (worker) "+" ".join(["S","1","77"]+["0"]*16+["42"])
  original_read_text=D.Path.read_text
  def read_text(path,*args,**kwargs):
   if str(path)=="/proc/101/stat": return stat
   if str(path)=="/proc/102/stat": raise PermissionError(13,"denied")
   return original_read_text(path,*args,**kwargs)
  with mock.patch.object(D.Path,"iterdir",return_value=entries), \
       mock.patch.object(D.Path,"read_text",new=read_text):
   observation=D.process_group_observation(77)
  self.assertEqual(observation.state,"populated")
  self.assertEqual(observation.members,((101,"42","S"),))
  self.assertIn("procfs-member:102",observation.reason)

 def test_foreground_reap_receipt_is_namespace_portable_but_never_hides_live(self):
  receipt={
   "pid":"437","pid_start":"42","pgid":"437",
   "pid_scope":"namespace-local","pid_observer_ns":"pid:[source]",
   "pid_ns":"pid:[source]","launch_lifecycle":"foreground-scoped",
   "launch_outcome":"governed-process-reaped",
   "group_reap_proof":D.GROUP_REAP_PROOF,"group_reap_pgid":"437",
  }
  with mock.patch.object(D,"process_namespace_identity",return_value="pid:[other]"):
   reaped=D.attempt_process_quiescence(receipt)
  self.assertEqual(
   (reaped.state,reaped.reason),
   ("quiescent","governed-process-group-reaped"))
  with mock.patch.object(D,"process_namespace_identity",return_value="pid:[source]"), \
       mock.patch.object(D,"_proc_observation",return_value=("present","42","S")):
   live=D.attempt_process_quiescence(receipt)
   terminal_live=D.attempt_process_quiescence(receipt,terminal_receipt=True)
  self.assertEqual((live.state,live.reason),("live","local-pid-live"))
  self.assertEqual((terminal_live.state,terminal_live.reason),
                   ("live","local-pid-live"))

 def test_terminal_namespace_local_quiescence_waits_for_complete_receipt(self):
  base={
   "pid":"437","pid_start":"42","pgid":"437",
   "pid_scope":"namespace-local","pid_observer_ns":D.process_namespace_identity(),
   "pid_ns":D.process_namespace_identity(),"attempt_id":"att-receipt-race",
   "registered_worker":"1",
  }
  with mock.patch.object(D,"_proc_observation",return_value=("missing","", "")), \
       mock.patch.object(D,"process_group_observation",
                         return_value=D.ProcessGroupObservation("empty")), \
       mock.patch.object(D,"attempt_tagged_descendants",
                         return_value=D.ProcessGroupObservation("empty")):
   local=D.attempt_process_quiescence(base)
   pending=D.attempt_process_quiescence(base,terminal_receipt=True)
   partial=D.attempt_process_quiescence(
    dict(base,launch_outcome="governed-process-reaped"),terminal_receipt=True)
   complete=D.attempt_process_quiescence(dict(
    base,launch_lifecycle="foreground-scoped",
    launch_outcome="governed-process-reaped",group_reap_proof=D.GROUP_REAP_PROOF,
    group_reap_pgid="437"),terminal_receipt=True)
  self.assertEqual(local.state,"quiescent")
  self.assertEqual((pending.state,pending.reason),
                   ("unverifiable","post-exit-receipt-incomplete"))
  self.assertEqual((partial.state,partial.reason),
                   ("unverifiable","post-exit-receipt-incomplete"))
  self.assertEqual((complete.state,complete.reason),
                   ("quiescent","governed-process-group-reaped"))

 def test_terminal_receipt_gate_dominates_namespace_unavailability(self):
  metadata={
   "pid":"437","pid_start":"42","pgid":"437",
   "pid_scope":"namespace-local","pid_observer_ns":"pid:[source]",
   "pid_ns":"pid:[source]","attempt_id":"att-receipt-unavailable",
   "registered_worker":"1",
  }
  with mock.patch.object(D,"_proc_observation",
                         return_value=("inaccessible","", "")):
   result=D.attempt_process_quiescence(metadata,terminal_receipt=True)
  self.assertEqual((result.state,result.reason),
                   ("unverifiable","post-exit-receipt-incomplete"))

 def test_host_visible_terminal_quiescence_does_not_require_portable_receipt(self):
  metadata={"pid":"437","pid_start":"42","pgid":"437",
            "pid_scope":"host-visible","attempt_id":"att-host-visible",
            "registered_worker":"1"}
  with mock.patch.object(D,"_proc_observation",return_value=("missing","", "")), \
       mock.patch.object(D,"process_group_observation",
                         return_value=D.ProcessGroupObservation("empty")), \
       mock.patch.object(D,"attempt_tagged_descendants",
                         return_value=D.ProcessGroupObservation("empty")):
   result=D.attempt_process_quiescence(metadata,terminal_receipt=True)
  self.assertEqual((result.state,result.reason),("quiescent","local-pid-gone"))

 def test_missing_leader_requires_complete_owned_group_observation(self):
  metadata={"pid":"437","pid_start":"42","pgid":"437"}
  unknown=D.ProcessGroupObservation("unverifiable",reason="denied")
  with mock.patch.object(D,"_proc_observation",return_value=("missing","", "")), \
       mock.patch.object(D,"process_group_observation",return_value=unknown):
   result=D.attempt_process_quiescence(metadata)
  self.assertEqual(
   (result.state,result.reason),
   ("unverifiable","local-process-group-unverifiable"))
  with mock.patch.object(D,"_proc_observation",return_value=("missing","", "")):
   no_group=D.attempt_process_quiescence({"pid":"437","pid_start":"42"})
  self.assertEqual(no_group.state,"unverifiable")
  with mock.patch.object(D,"_proc_observation",return_value=("missing","", "")), \
       mock.patch.object(D.os,"killpg") as killpg:
   authority=D.signal_exact_process_group(437,"42",__import__("signal").SIGTERM)
  self.assertEqual(authority,"leader-gone")
  killpg.assert_not_called()

 def test_launch_identity_rejects_procfs_pid_namespace_mismatch(self):
  def namespace(pid="self"):
   return "pid:[inner]" if pid==437 else "pid:[observer]"
  with mock.patch.object(D,"process_namespace_identity",side_effect=namespace), \
       mock.patch.object(D,"process_start_ticks",return_value="42") as start, \
       mock.patch.object(D,"process_namespace_pids",return_value=(1437,437)) as nspid, \
       mock.patch.object(D.os,"getpgid",return_value=437):
   identity=D.process_launch_identity(437)
  self.assertNotIn("pid_start",identity)
  self.assertNotIn("pid_host",identity)
  start.assert_not_called();nspid.assert_not_called()

 def test_conflicting_local_and_host_identity_proofs_have_no_authority(self):
  metadata={
   "pid":"437","pid_start":"42","pgid":"437",
   "pid_observer_ns":"pid:[observer]","pid_ns":"pid:[observer]",
   "pid_host":"1437","pid_host_start":"42","pgid_host":"1437",
   "pid_host_ns":"pid:[observer]","pid_host_proof":D.PID_HOST_NAMESPACE_PROOF,
  }
  with mock.patch.object(D,"process_namespace_identity",return_value="pid:[observer]"), \
       mock.patch.object(D,"_proc_observation") as observation:
   self.assertEqual(D.authoritative_process_identities(metadata),())
   result=D.attempt_process_quiescence(metadata)
  self.assertEqual(result.state,"unverifiable")
  observation.assert_not_called()

 def test_replica_expectation_rejects_register_and_binds_exact_start(self):
  with tempfile.TemporaryDirectory() as td:
   route_path=Path(td)/"route.json"
   fallback=[{"fallback_hop":"same-harness-headless","ordinal":1,
              "candidates":[{"child_harness":"codex","status":"supported"}]},
             {"fallback_hop":"cross-harness-headless","ordinal":2,
              "candidates":[{"child_harness":"claude","status":"supported"},
                            {"child_harness":"opencode","status":"supported"}]}]
   route={"route_id":"rt-replica","nodes":[
    {"id":"plan","dispatch_depth":2,"parallel_group":"plan","replica_group":"plan",
     "model_profile":"deep","perspective":"primary-plan","parallel_leg_index":0,"fallback_hops":fallback},
    {"id":"plan-alternative","dispatch_depth":2,"parallel_group":"plan","replica_group":"plan",
     "model_profile":"balanced-deep","perspective":"independent-plan","parallel_leg_index":1,"fallback_hops":fallback},
   ]}
   route_path.write_text(__import__("json").dumps(route))
   with self.assertRaises(D.DispatchContractError) as caught:
    D.replica_batch_expectation(route_path,"plan","register")
   self.assertEqual(caught.exception.reason,"parallel-group-batch-required")
   expected=D.replica_batch_expectation(
    route_path,"plan","start",attempt_id="att-replica-start",
    parent_attempt_id="att-parent",harness="codex",
    fallback_hop="same-harness-headless",fallback_ordinal=1)
   self.assertEqual(expected["batch_group"],"plan")
   self.assertEqual(expected["batch_attempt_id"],"att-replica-start")
   self.assertEqual(expected["batch_parent_attempt_id"],"att-parent")
   self.assertIn(
    {"harness":"opencode","fallback_hop":"cross-harness-headless",
     "fallback_ordinal":2},
    expected["_batch_allowed_members"]["plan-alternative"])

   manifest,manifest_digest,leg_digests=build_manifest(
    replica_group="plan",route_id="rt-replica",parent_attempt_id="att-parent",
    independence="cross-harness",members=[
     {"assignment_sha256":"sha256:"+"a"*64,"attempt_id":"att-replica-start",
     "route_node":"plan","harness":"codex",
      "fallback_hop":"same-harness-headless","fallback_ordinal":1,
      "model_profile":"deep","perspective":"primary-plan","parallel_leg_index":0,
      "leg_class":"peer"},
     {"assignment_sha256":"sha256:"+"a"*64,"attempt_id":"att-replica-peer",
      "route_node":"plan-alternative","harness":"opencode",
      "fallback_hop":"cross-harness-headless","fallback_ordinal":2,
      "model_profile":"balanced-deep","perspective":"independent-plan","parallel_leg_index":1,
      "leg_class":"peer"},
    ],required_independence_axes=["cross-harness","model-profile","perspective"],
    realized_independence_axes=["cross-harness","model-profile","perspective"])
   expected=D.replica_batch_expectation(
    route_path,"plan","start",attempt_id="att-replica-start",
    parent_attempt_id="att-parent",harness="codex",
    fallback_hop="same-harness-headless",fallback_ordinal=1,
    assignment_sha256="sha256:"+"a"*64)
   payload={**{key:value for key,value in expected.items() if not key.startswith("_")},
            "batch_admission_count":2,"batch_independence":"cross-harness",
            "batch_manifest":manifest,"batch_manifest_sha256":manifest_digest,
            "batch_leg_sha256":leg_digests["att-replica-start"]}
   D._validate_replica_reservation(payload,expected)
   tampered={**payload,"batch_assignment_sha256":"sha256:"+"b"*64}
   with self.assertRaises(D.DispatchContractError):
    D._validate_replica_reservation(tampered,expected)

   proof={
    "agent_home":"/agent-home","attempt_id":"att-replica-peer",
    "jobs":"/agent-home/.dispatch/jobs.log",
    "manifest_sha256":manifest_digest,"reason":"host-pid-live",
    "route":"/route.json","state":"active",
   }
   proof_set=[proof]
   proof_digest="sha256:"+__import__("hashlib").sha256(
    __import__("json").dumps(
     proof_set,separators=(",",":"),sort_keys=True).encode()).hexdigest()
   partial={
    **payload,"batch_admission_count":1,
    "batch_peer_count":1,"batch_peer_set":proof_set,
    "batch_peer_set_sha256":proof_digest,
   }
   D._validate_replica_reservation(partial,expected)
   for mutation in (
    lambda value:value.pop("batch_peer_set"),
    lambda value:value["batch_peer_set"][0].update(attempt_id="att-wrong-peer"),
    lambda value:value["batch_peer_set"][0].update(reason="tampered"),
   ):
    broken=__import__("copy").deepcopy(partial)
    mutation(broken)
    with self.assertRaises(D.DispatchContractError):
     D._validate_replica_reservation(broken,expected)

 def test_replica_token_cannot_authorize_non_replica_start(self):
  with self.assertRaises(D.DispatchContractError) as caught:
   D._validate_replica_reservation({"reservation_kind":"parallel-batch"},None)
  self.assertEqual(caught.exception.reason,"parallel-group-reservation-mismatch")

 def test_replica_reservation_mismatch_is_rejected_before_claim(self):
  expected={
   "reservation_kind":"parallel-batch","batch_declared_size":2,
   "batch_group":"plan","batch_route_id":"rt-replica",
   "batch_parent_attempt_id":"att-parent","batch_attempt_id":"att-leg",
   "batch_route_node":"plan","batch_harness":"codex",
   "batch_fallback_hop":"same-harness-headless","batch_fallback_ordinal":1,
  }
  payload={
   **expected,"state":"unclaimed",
   "batch_route_node":"plan-replica",
   "batch_manifest_sha256":"sha256:"+"a"*64,
   "batch_leg_sha256":"sha256:"+"b"*64,
  }
  with mock.patch.object(D,"_governor_json",return_value=payload):
   with self.assertRaises(D.DispatchContractError) as caught:
    D.reserve_governor_token(
     Path("/governor"),Path("/root"),"dispatch",
     provided_token="a"*32,expected_reservation=expected)
  self.assertEqual(caught.exception.reason,"parallel-group-reservation-mismatch")

 def test_process_group_descendant_keeps_attempt_live_after_leader_exit(self):
  proc=subprocess.Popen(
   [sys.executable,"-c",
    "import subprocess,time; subprocess.Popen(['sleep','0.6']); time.sleep(0.1)"],
   start_new_session=True)
  identity=D.process_launch_identity(proc.pid)
  proc.wait(timeout=5)
  draining=D.attempt_process_quiescence(identity)
  self.assertEqual((draining.state,draining.reason),
                   ("live","local-process-group-live"))
  __import__("time").sleep(0.7)
  self.assertEqual(D.attempt_process_quiescence(identity).state,"quiescent")

 def test_mutation_api_rejects_immutable_identity_and_conflicting_outcome(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   jobs.write_text(
    "2026-07-24T00:00:00Z\topen\t/repo\t/wt\tworker\t"+CURRENT+
    ",attempt_id=att-mutation-test,launch_claimed=1\n",encoding="utf-8")
   with self.assertRaises(D.DispatchContractError) as caught:
    D.annotate_attempt_row(jobs,"att-mutation-test",{"attempt_id":"att-forged"})
   self.assertEqual(caught.exception.reason,"attempt-immutable-metadata-mutation")
   self.assertTrue(D.annotate_attempt_row(
    jobs,"att-mutation-test",{"launch_outcome":"never-launched"}))
   with self.assertRaises(D.DispatchContractError) as caught:
    D.annotate_attempt_row(
     jobs,"att-mutation-test",{"launch_outcome":"governed-process-reaped"})
   self.assertEqual(caught.exception.reason,"attempt-launch-outcome-conflict")

 def test_semantic_completion_readiness_blocks_live_process_and_conflicting_retry(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   proc=subprocess.Popen(
    [sys.executable,"-c","import time; time.sleep(30)"],
    start_new_session=True)
   try:
    identity=D.process_launch_identity(proc.pid)
    metadata=(
     f"{CURRENT},route_id=rt-ready,route_node=plan,attempt_id=att-ready-main,"
     "note=completed-marker,"+
     ",".join(f"{key}={value}" for key,value in identity.items())
    )
    jobs.write_text(
     f"2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan\t{metadata}\n",
     encoding="utf-8")
    route={"route_id":"rt-ready"}; node={"id":"plan","kind":"pipeline-stage"}
    marker={"attempt_id":"att-ready-main","registered_worker":True}
    draining=D.completion_attempt_readiness(route,node,marker,jobs)
    self.assertEqual(draining.state,"draining")
    proc.terminate();proc.wait(timeout=5)
    ready=D.completion_attempt_readiness(route,node,marker,jobs)
    self.assertEqual(ready.state,"ready")
    with jobs.open("a",encoding="utf-8") as handle:
     handle.write(
      f"2026-07-24T00:00:01Z\topen\t/repo\t/wt\tplan-retry\t{CURRENT},"
      "route_id=rt-ready,route_node=plan,attempt_id=att-ready-retry\n")
    conflict=D.completion_attempt_readiness(route,node,marker,jobs)
    self.assertEqual((conflict.state,conflict.reason),
                     ("draining","conflicting-active-retry"))
   finally:
    if proc.poll() is None:proc.kill()
    proc.wait()

 def test_claimed_spawn_rechecks_parent_and_publishes_identity_under_lock(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   parent=subprocess.Popen(["sleep","60"])
   child=None
   try:
    start=D.process_start_ticks(parent.pid)
    owner=self.owner_row("att-parent-atomic",parent.pid,start)
    child_row=("2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t"
               f"{CURRENT},worker_type=stage,parent=owner,"
               "parent_attempt_id=att-parent-atomic,attempt_id=att-child-atomic")
    jobs.write_text(owner+"\n")
    binding=D.resolve_live_parent_attempt(
     jobs,parent_slug="owner",repo="/repo",worktree="/wt",
     expected_attempt_id="att-parent-atomic")
    self.assertTrue(D.claim_attempt_row(jobs,"att-child-atomic",child_row,launch=False))
    child,identity=D.spawn_claimed_attempt(
     jobs,"att-child-atomic",parent_binding=binding,
     spawn=lambda gate_fd: subprocess.Popen(
      [sys.executable, str(Path(__file__).with_name("launch-fence.py")),
       "--parent-pid", str(os.getpid()), "--gate-fd", str(gate_fd), "--",
       "sleep", "60"],
      pass_fds=(gate_fd,), start_new_session=True),
     launch_metadata={"launch_lifecycle":"detached"})
    meta=D.parse_registry_metadata(jobs.read_text().splitlines()[1].split("\t")[5])
    self.assertEqual(meta["pid"],str(child.pid))
    self.assertEqual(meta["pid_start"],identity["pid_start"])
    self.assertEqual(meta["pgid"],str(child.pid))
    self.assertEqual(meta["launch_lifecycle"],"detached")
   finally:
    for proc in (child,parent):
     if proc is not None and proc.poll() is None:proc.kill()
     if proc is not None:proc.wait()

 def test_claimed_spawn_starts_zero_children_when_parent_dies_before_final_check(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   parent=subprocess.Popen(["sleep","60"])
   start=D.process_start_ticks(parent.pid)
   jobs.write_text(self.owner_row("att-parent-race",parent.pid,start)+"\n")
   binding=D.resolve_live_parent_attempt(
    jobs,parent_slug="owner",repo="/repo",worktree="/wt",
    expected_attempt_id="att-parent-race")
   child_row=("2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t"
              f"{CURRENT},worker_type=stage,parent=owner,"
              "parent_attempt_id=att-parent-race,attempt_id=att-child-race")
   self.assertTrue(D.claim_attempt_row(jobs,"att-child-race",child_row,launch=False))
   parent.kill();parent.wait()
   spawned=[]
   with self.assertRaises(D.DispatchContractError) as caught:
    D.spawn_claimed_attempt(
     jobs,"att-child-race",parent_binding=binding,
     spawn=lambda gate_fd: spawned.append(True))
   self.assertEqual(caught.exception.reason,"parent-attempt-not-live")
   self.assertEqual(spawned,[])

 def test_pre_release_observer_is_attached_before_gate_and_committed_atomically(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";marker=base/"payload-started"
   attempt="att-summary-atomic"
   row=(f"2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t{CURRENT},"
        f"attempt_id={attempt}")
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row,launch=False))
   observed=[]
   def spawn(gate_fd):
    return subprocess.Popen(
     [sys.executable,str(Path(__file__).with_name("launch-fence.py")),
      "--parent-pid",str(os.getpid()),"--gate-fd",str(gate_fd),"--",
      sys.executable,"-c",f"from pathlib import Path;Path({str(marker)!r}).write_text('yes')"],
     pass_fds=(gate_fd,),start_new_session=True)
   def attach(identity):
    self.assertFalse(marker.exists())
    observed.append(dict(identity))
    return {"summary_owner":"dispatch-v1","summary_owner_pid":"777"}
   proc,identity=D.spawn_claimed_attempt(
    jobs,attempt,parent_binding=None,spawn=spawn,pre_release=attach)
   proc.wait(timeout=5)
   self.assertTrue(marker.exists())
   self.assertEqual(observed[0]["pid"],str(proc.pid))
   meta=D.parse_registry_metadata(jobs.read_text().strip().split("\t",5)[5])
   self.assertEqual(meta["summary_owner"],"dispatch-v1")
   self.assertEqual(meta["summary_owner_pid"],"777")
   self.assertEqual(meta["launch_claimed"],"1")
   self.assertEqual(identity["summary_owner"],"dispatch-v1")

 def test_pre_release_failure_aborts_fenced_payload_and_leaves_claim_retryable(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";marker=base/"must-not-run";children=[]
   attempt="att-summary-failure"
   row=(f"2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t{CURRENT},"
        f"attempt_id={attempt}")
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row,launch=False))
   def spawn(gate_fd):
    proc=subprocess.Popen(
     [sys.executable,str(Path(__file__).with_name("launch-fence.py")),
      "--parent-pid",str(os.getpid()),"--gate-fd",str(gate_fd),"--",
      sys.executable,"-c",f"from pathlib import Path;Path({str(marker)!r}).write_text('bad')"],
     pass_fds=(gate_fd,),start_new_session=True)
    children.append(proc);return proc
   with self.assertRaises(D.DispatchContractError) as caught:
    D.spawn_claimed_attempt(
     jobs,attempt,parent_binding=None,spawn=spawn,
     pre_release=lambda _identity: (_ for _ in ()).throw(RuntimeError("owner failed")))
   self.assertEqual(caught.exception.reason,"attempt-pre-release-callback-failed")
   self.assertFalse(marker.exists())
   self.assertIsNotNone(children[0].poll())
   meta=D.parse_registry_metadata(jobs.read_text().strip().split("\t",5)[5])
   self.assertEqual(meta["launch_claimed"],"0")
   self.assertNotIn("summary_owner",meta)

 def test_incomplete_launch_identity_never_releases_fence(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";marker=base/"marker"
   attempt="att-incomplete-launch"
   row=(f"2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t{CURRENT},"
        f"attempt_id={attempt}")
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row,launch=False))
   child=[]
   def spawn(gate_fd):
    proc=subprocess.Popen(
     [sys.executable,str(Path(__file__).with_name("launch-fence.py")),
      "--parent-pid",str(os.getpid()),"--gate-fd",str(gate_fd),"--",
      sys.executable,"-c",f"from pathlib import Path;Path({str(marker)!r}).write_text('bad')"],
     pass_fds=(gate_fd,),start_new_session=True)
    child.append(proc);return proc
   with mock.patch.object(D,"process_launch_identity",side_effect=lambda pid:{"pid":str(pid)}):
    with self.assertRaises(D.DispatchContractError) as caught:
     D.spawn_claimed_attempt(
      jobs,attempt,parent_binding=None,spawn=spawn,
      launch_metadata={"launch_lifecycle":"detached"})
   self.assertEqual(caught.exception.reason,"attempt-launch-identity-incomplete")
   self.assertFalse(marker.exists())
   self.assertIsNotNone(child[0].poll())

 def test_parent_death_after_spawn_prevents_fence_release(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";marker=base/"marker"
   parent=subprocess.Popen(["sleep","60"]);child=[]
   try:
    start=D.process_start_ticks(parent.pid)
    jobs.write_text(self.owner_row("att-parent-post-spawn",parent.pid,start)+"\n")
    binding=D.resolve_live_parent_attempt(
     jobs,parent_slug="owner",repo="/repo",worktree="/wt",
     expected_attempt_id="att-parent-post-spawn")
    attempt="att-child-post-spawn"
    row=(f"2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t{CURRENT},"
         "worker_type=stage,parent=owner,parent_attempt_id=att-parent-post-spawn,"
         f"attempt_id={attempt}")
    self.assertTrue(D.claim_attempt_row(jobs,attempt,row,launch=False))
    def spawn(gate_fd):
     proc=subprocess.Popen(
      [sys.executable,str(Path(__file__).with_name("launch-fence.py")),
       "--parent-pid",str(os.getpid()),"--gate-fd",str(gate_fd),"--",
       sys.executable,"-c",f"from pathlib import Path;Path({str(marker)!r}).write_text('bad')"],
      pass_fds=(gate_fd,),start_new_session=True)
     child.append(proc);return proc
    with mock.patch.object(
        D,"process_identity_is_live",side_effect=(True,False)):
     with self.assertRaises(D.DispatchContractError) as caught:
      D.spawn_claimed_attempt(
       jobs,attempt,parent_binding=binding,spawn=spawn,
       launch_metadata={"launch_lifecycle":"detached"})
    self.assertEqual(caught.exception.reason,"parent-attempt-not-live-after-spawn")
    self.assertFalse(marker.exists())
    self.assertIsNotNone(child[0].poll())
   finally:
    if parent.poll() is None:parent.kill()
    parent.wait()
    for proc in child:
     if proc.poll() is None:proc.kill()
     proc.wait()

 def test_supervisor_lease_release_after_spawn_prevents_fence_release(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";marker=base/"marker"
   parent=subprocess.Popen(["sleep","60"]);children=[]
   attempt="att-parent-lease-race";nonce="c"*64
   lease=D.supervisor_lease_path(jobs,attempt);lease.parent.mkdir(parents=True)
   holder=lease.open("w+");fcntl.flock(holder.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:
    start=D.process_start_ticks(parent.pid)
    extra=(",harness=codex,runtime_sandbox=workspace-write,"
           "completion_delivery=app-server-supervised,"
           f"supervisor_lease={D.SUPERVISOR_LEASE_KIND},"
           f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce},"
           "pid_scope=namespace-local,pid_observer_ns=pid:[outer],"
           "pid_ns=pid:[outer]")
    jobs.write_text(self.owner_row(attempt,parent.pid,start,extra=extra)+"\n")
    holder.write(f"kind={D.SUPERVISOR_LEASE_KIND}\nattempt_id={attempt}\nnonce={nonce}\n")
    holder.flush()
    with mock.patch.object(D,"process_namespace_identity",return_value="pid:[inner]"):
     binding=D.resolve_live_parent_attempt(
      jobs,parent_slug="owner",repo="/repo",worktree="/wt",
      expected_attempt_id=attempt)
     child_attempt="att-child-lease-race"
     row=(f"2026-07-23T00:00:01Z\topen\t/repo\t/wt\tchild\t{CURRENT},"
          f"worker_type=stage,parent=owner,parent_attempt_id={attempt},"
          f"attempt_id={child_attempt}")
     self.assertTrue(D.claim_attempt_row(jobs,child_attempt,row,launch=False))
     def spawn(gate_fd):
      proc=subprocess.Popen(
       [sys.executable,str(Path(__file__).with_name("launch-fence.py")),
        "--parent-pid",str(os.getpid()),"--gate-fd",str(gate_fd),"--",
        sys.executable,"-c",f"from pathlib import Path;Path({str(marker)!r}).write_text('bad')"],
       pass_fds=(gate_fd,),start_new_session=True)
      children.append(proc)
      fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close()
      return proc
     with self.assertRaises(D.DispatchContractError) as caught:
      D.spawn_claimed_attempt(
       jobs,child_attempt,parent_binding=binding,spawn=spawn,
       launch_metadata={"launch_lifecycle":"foreground-scoped"})
    self.assertEqual(caught.exception.reason,"parent-attempt-not-live-after-spawn")
    self.assertFalse(marker.exists())
    self.assertIsNotNone(children[0].poll())
   finally:
    if not holder.closed:
     fcntl.flock(holder.fileno(),fcntl.LOCK_UN);holder.close()
    if parent.poll() is None:parent.kill()
    parent.wait()
    for proc in children:
     if proc.poll() is None:proc.kill()
     proc.wait()

 def test_dead_unstarted_registry_fence_is_atomically_retryable(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log";attempt="att-unstarted-recovery"
   proc=subprocess.Popen(["sleep","0.05"],start_new_session=True)
   identity=D.process_launch_identity(proc.pid)
   metadata=(
    f"{CURRENT},attempt_id={attempt},launch_claimed=1,"
    "launch_fence=registry-v1,launch_lifecycle=detached,"+
    ",".join(f"{key}={value}" for key,value in identity.items())
   )
   jobs.write_text(
    f"2026-07-24T00:00:00Z\topen\t/repo\t/wt\tworker\t{metadata}\n",
    encoding="utf-8")
   proc.wait(timeout=5)
   self.assertTrue(D.recover_unstarted_attempt(jobs,attempt))
   recovered=D.parse_registry_metadata(
    jobs.read_text(encoding="utf-8").strip().split("\t",5)[5])
   self.assertEqual(recovered["launch_claimed"],"0")
   self.assertNotIn("pid",recovered)
   self.assertTrue(D.attempt_launch_is_available(jobs,attempt))

 def test_launcher_sigkill_after_registration_leaves_retryable_unclaimed_row(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td);jobs=base/"jobs.log";ready=base/"ready"
   attempt="att-register-crash-retry"
   row=(
    f"2026-07-24T00:00:00Z\topen\t/repo\t/wt\tworker\t{CURRENT},"
    f"attempt_id={attempt},launch_fence=registry-v1"
   )
   launcher=subprocess.Popen([
    sys.executable,"-c",
    (
     "import time;from pathlib import Path;"
     "from dispatch_contract import claim_attempt_row;"
     f"claim_attempt_row(Path({str(jobs)!r}),{attempt!r},{row!r},launch=False);"
     f"Path({str(ready)!r}).write_text('ready');time.sleep(60)"
    )],env={**os.environ,"PYTHONPATH":str(P.parent)})
   try:
    deadline=time.monotonic()+5
    while not ready.exists() and time.monotonic()<deadline:
     time.sleep(0.01)
    self.assertTrue(ready.exists())
    launcher.kill();launcher.wait(timeout=5)
    metadata=D.parse_registry_metadata(
     jobs.read_text(encoding="utf-8").strip().split("\t",5)[5])
    self.assertEqual(metadata["launch_claimed"],"0")
    self.assertNotIn("pid",metadata)
    self.assertTrue(D.attempt_launch_is_available(jobs,attempt))
   finally:
    if launcher.poll() is None:launcher.kill()
    launcher.wait()

 def test_started_registry_fence_can_never_be_reset_for_retry(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log";attempt="att-started-no-recovery"
   proc=subprocess.Popen(["sleep","0.05"],start_new_session=True)
   identity=D.process_launch_identity(proc.pid)
   metadata=(
    f"{CURRENT},attempt_id={attempt},launch_claimed=1,"
    "launch_fence=registry-v1,launch_started=1,launch_lifecycle=detached,"+
    ",".join(f"{key}={value}" for key,value in identity.items())
   )
   jobs.write_text(
    f"2026-07-24T00:00:00Z\topen\t/repo\t/wt\tworker\t{metadata}\n",
    encoding="utf-8")
   proc.wait(timeout=5)
   self.assertFalse(D.recover_unstarted_attempt(jobs,attempt))
   self.assertIn("launch_claimed=1",jobs.read_text(encoding="utf-8"))

 def test_nested_registry_is_inherited_and_immutable(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); global_jobs=(root/"global/jobs.log").resolve(); local=(root/"cycle/jobs.log").resolve()
   selected=D.resolve_global_registry(root,str(global_jobs),1,"start",{})
   self.assertEqual(selected.path,global_jobs)
   inherited=D.resolve_global_registry(root,str(global_jobs),2,"start",{"AGENT_DISPATCH_JOBS":str(global_jobs)})
   self.assertTrue(inherited.inherited)
   with self.assertRaisesRegex(D.DispatchContractError,"explicit=.*inherited"):
    D.resolve_global_registry(root,str(local),2,"start",{"AGENT_DISPATCH_JOBS":str(global_jobs)})
   with self.assertRaises(D.DispatchContractError) as caught:
    D.resolve_global_registry(root,str(local),2,"start",{})
   self.assertEqual(caught.exception.reason,"global-registry-unset")
 def test_managed_parent_registry_is_immutable_at_depth_one(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); canonical=root/"managed/jobs.log"; split=root/"bundle/.dispatch/jobs.log"
   env={"AGENT_CODEX_MANAGED_GATEWAY":"1",
        "AGENT_CODEX_MANAGED_PARENT_RUNTIME":"codex",
        "AGENT_DISPATCH_JOBS":str(canonical)}
   selected=D.resolve_global_registry(root,str(canonical),1,"start",env)
   self.assertEqual(selected.path,canonical.resolve())
   with self.assertRaises(D.DispatchContractError) as caught:
    D.resolve_global_registry(root,str(split),1,"start",env)
   self.assertEqual(caught.exception.reason,"managed-parent-registry-immutable")
 def test_unwritable_registry_is_structured(self):
  with self.assertRaises(D.DispatchContractError) as caught:
   D.ensure_global_registry_writable(Path("/proc/1/stage-dispatch-v11/jobs.log"))
  self.assertEqual(caught.exception.reason,"global-registry-unwritable")
 def test_attempt_id_and_nested_unknown(self):
  self.assertTrue(D.new_attempt_id().startswith("att-"))
  with self.assertRaises(D.DispatchContractError) as caught:
   D.validate_nested_eligibility(dispatch_depth=2,action="start",parent_harness="codex",parent_transport="headless",parent_sandbox="workspace-write",child_harness="codex",launch_authority="conductor",status="unknown",source="fixture")
  self.assertEqual(caught.exception.reason,"nested-child-spawn-unknown")
 def test_runtime_surface_label_is_rejected_as_parent_transport(self):
  with self.assertRaises(D.DispatchContractError) as caught:
   D.validate_nested_eligibility(dispatch_depth=2,action="start",parent_harness="codex",parent_transport="codex-exec-headless",parent_sandbox="workspace-write",child_harness="codex",launch_authority="conductor",status="supported",source="fixture")
  self.assertEqual(caught.exception.reason,"invalid-parent-transport")
 def test_attempt_namespaces_reject_unknowns_before_claim(self):
  base=dict(attempt_schema_version=2,dispatch_depth=1,transport="headless",
            execution_surface="registered-headless",registered_worker=True,
            fallback_hop="same-harness-headless")
  for field,value,reason in (
      ("transport","detached-process","invalid-transport"),
      ("execution_surface","mystery","invalid-execution-surface"),
      ("fallback_hop","mystery","invalid-fallback-hop"),
      ("registered_worker","maybe","invalid-registered-worker")):
   metadata=dict(base);metadata[field]=value
   with self.subTest(field=field),self.assertRaises(D.DispatchContractError) as caught:
    D.validate_attempt_metadata(metadata)
   self.assertEqual(caught.exception.reason,reason)
 def test_current_attempt_rejects_every_bare_depth_alias(self):
  base=dict(attempt_schema_version=2,dispatch_depth=1,transport="headless",
            execution_surface="registered-headless",registered_worker=True,
            fallback_hop="same-harness-headless")
  for field in ("depth","owner_depth","max_depth"):
   metadata=dict(base);metadata[field]=1
   with self.subTest(field=field),self.assertRaises(D.DispatchContractError) as caught:
    D.validate_attempt_metadata(metadata)
   self.assertEqual(caught.exception.reason,"bare-dispatch-depth-field")
 def test_direct_and_runtime_native_attempt_axes_are_independent(self):
  D.validate_attempt_metadata(dict(
   attempt_schema_version=2,dispatch_depth=0,transport="interactive",
   execution_surface="inline",registered_worker=False,fallback_hop=""))
  for surface in ("codex-native-subagent","claude-subagent"):
   with self.subTest(surface=surface):
    D.validate_attempt_metadata(dict(
     attempt_schema_version=2,dispatch_depth=2,transport="headless",
     execution_surface=surface,registered_worker=False,fallback_hop="native-subagent"))
  with self.assertRaises(D.DispatchContractError) as caught:
   D.validate_attempt_metadata(dict(
    attempt_schema_version=2,dispatch_depth=2,transport="interactive",
    execution_surface="claude-agent-team-teammate",registered_worker=False,
    fallback_hop="native-subagent"))
  self.assertEqual(caught.exception.reason,"teammate-not-dispatch-attempt")
 def test_launch_broker_is_retired(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); jobs=root/"jobs.log"
   with self.assertRaises(D.DispatchContractError) as caught:
    D.ensure_launch_broker(root,jobs,dispatch_depth=1,action="start",intensity="strong")
   self.assertEqual(caught.exception.reason,"launch-broker-retired")
 def test_atomic_attempt_claim_is_exact_and_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"; attempt="att-123456789abc"; prefix="att-123456789abc-extra"
   row=f"2026-07-16T00:00:00Z\topen\t/repo\t/wt\tstage\t{CURRENT},capability=code-plan,attempt_id={prefix}"
   self.assertTrue(D.claim_attempt_row(jobs,prefix,row))
   exact=f"2026-07-16T00:00:01Z\topen\t/repo\t/wt\tstage\t{CURRENT},capability=code-plan,attempt_id={attempt}"
   self.assertTrue(D.claim_attempt_row(jobs,attempt,exact))
   self.assertFalse(D.claim_attempt_row(jobs,attempt,exact))
   self.assertTrue(D.claim_attempt_row(jobs,attempt,exact,launch=True))
   self.assertFalse(D.claim_attempt_row(jobs,attempt,exact,launch=True))
   self.assertIn("launch_claimed=1",jobs.read_text())
   self.assertEqual(len(jobs.read_text().splitlines()),2)
 def test_preclaim_gate_and_launch_claim_share_registry_lock(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"; attempt="att-preclaim00001"
   row=f"2026-07-16T00:00:00Z\topen\t/repo\t/wt\tstage\t{CURRENT},capability=code-plan,attempt_id={attempt}"
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row))
   before=jobs.read_bytes()
   observed=[]
   def reject(lines):
    observed.extend(lines)
    raise D.DispatchContractError("predecessor-process-draining","fixture")
   with self.assertRaises(D.DispatchContractError) as caught:
    D.claim_attempt_row(jobs,attempt,row,launch=True,preclaim=reject)
   self.assertEqual(caught.exception.reason,"predecessor-process-draining")
   self.assertEqual(observed,before.decode().splitlines())
   self.assertEqual(jobs.read_bytes(),before)
   self.assertNotIn("launch_claimed=1",jobs.read_text())
 def test_existing_attempt_id_rejects_conflicting_immutable_identity_without_mutation(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"; attempt="att-conflict00001"
   row=f"2026-07-16T00:00:00Z\topen\t/repo\t/wt-a\tstage\t{CURRENT},route_id=rt-a,route_node=plan,attempt_id={attempt}"
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row))
   before=jobs.read_bytes()
   conflict=f"2026-07-16T00:00:01Z\topen\t/repo\t/wt-b\tstage\t{CURRENT},route_id=rt-b,route_node=execute,attempt_id={attempt}"
   with self.assertRaises(D.DispatchContractError) as caught:
    D.claim_attempt_row(jobs,attempt,conflict,launch=True)
   self.assertEqual(caught.exception.reason,"attempt-identity-conflict")
   self.assertEqual(jobs.read_bytes(),before)
 def test_standard_route_candidate_requires_exact_checked_launch_tuple(self):
  with tempfile.TemporaryDirectory() as td:
   route_path=Path(td)/"route.json"
   candidate={
    "parent_harness":"claude","parent_transport":"headless",
    "parent_sandbox":"workspace-write","child_harness":"codex",
    "launch_authority":"conductor","status":"supported"}
   route={
    "schema_version":2,"effective_intensity":"standard",
    "nodes":[{"id":"execute","dispatch_depth":2,"fallback_hops":[
     {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[]},
     {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[candidate]},
     {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
     {"ordinal":4,"fallback_hop":"inline","candidates":[]}]}]}
   route_path.write_text(__import__("json").dumps(route))
   with self.assertRaises(D.DispatchContractError) as caught:
    D.headless_attempt_policy(
     route_file=str(route_path),route_node="execute",intensity="standard",
     harness="codex",dispatch_depth=2,parent_slug="owner",
     execution_surface="registered-headless",registered_worker=True,
     fallback_hop="cross-harness-headless",fallback_ordinal=2,
     parent_harness="codex",parent_transport="headless",
     parent_sandbox="workspace-write",launch_authority="conductor")
   self.assertEqual(caught.exception.reason,"route-fallback-candidate-mismatch")
 def test_concurrent_attempt_claim_has_one_winner(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"; attempt="att-concurrent1234"
   code=("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
         "import dispatch_contract as D;"
         "print(int(D.claim_attempt_row(Path(sys.argv[2]),sys.argv[3],sys.argv[4],launch=True)))")
   row=f"2026-07-16T00:00:00Z\topen\t/repo\t/wt\tstage\t{CURRENT},capability=code-plan,attempt_id={attempt}"
   procs=[subprocess.Popen([sys.executable,"-c",code,str(P.parent),str(jobs),attempt,row],text=True,stdout=subprocess.PIPE) for _ in range(8)]
   winners=[p.communicate(timeout=10)[0].strip() for p in procs]
   self.assertEqual(winners.count("1"),1,winners)
   self.assertEqual(len(jobs.read_text().splitlines()),1)
 def test_register_to_start_transition_is_crash_atomic(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"; attempt="att-crashatomic123"
   row=f"2026-07-16T00:00:00Z\topen\t/repo\t/wt\tstage\t{CURRENT},capability=code-plan,attempt_id={attempt}"
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row))
   before=jobs.read_text()
   with mock.patch.object(D.os,"replace",side_effect=OSError("fixture-crash")):
    with self.assertRaises(OSError): D.claim_attempt_row(jobs,attempt,row,launch=True)
   self.assertEqual(jobs.read_text(),before)
   self.assertFalse(list(Path(td).glob(".jobs.log.claim-*")))
 def test_capacity_retry_claim_is_exclusive_per_route_node(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   code=("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
         "import dispatch_contract as D;a=sys.argv[3];"
         "row=f'2026-07-16T00:00:00Z\\topen\\t/r\\t/w\\ts\\tattempt_schema_version=2,dispatch_depth=2,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,route_id=r,route_node=n,attempt_id={a},capacity_retry=1';"
         "print(int(D.claim_attempt_row(Path(sys.argv[2]),a,row,launch=True,exclusive_metadata={'route_id':'r','route_node':'n','capacity_retry':'1'})))")
   procs=[subprocess.Popen([sys.executable,"-c",code,str(P.parent),str(jobs),f"att-capacity{i:04d}"],text=True,stdout=subprocess.PIPE) for i in range(8)]
   winners=[p.communicate(timeout=10)[0].strip() for p in procs]
   self.assertEqual(winners.count("1"),1,winners)
   self.assertEqual(len(jobs.read_text().splitlines()),1)
 def test_conditional_close_revalidates_under_lock(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log";attempt="att-revalidate001"
   row=f"2026-07-16T00:00:00Z\topen\t/r\t/w\ts\t{CURRENT},route_id=r,route_node=n,attempt_id={attempt}"
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row))
   self.assertFalse(D.close_attempt_row_if(jobs,attempt,"dead-test",lambda _fields:False))
   self.assertIn("\topen\t",jobs.read_text())
   self.assertTrue(D.close_attempt_row_if(jobs,attempt,"dead-test",lambda _fields:True))
   self.assertIn("note=dead-test",jobs.read_text())

 def test_teardown_claim_excludes_ordinary_close_and_owner_closes_atomically(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log";attempt="att-teardown-cas01"
   row=f"2026-07-16T00:00:00Z\topen\t/r\t/w\ts\t{CURRENT},attempt_id={attempt}"
   self.assertTrue(D.claim_attempt_row(jobs,attempt,row))
   self.assertTrue(D.annotate_attempt_row_if(
    jobs,attempt,{"teardown_claim":"claim-1","teardown_claimed_at":"now",
                  "teardown_claim_pid":str(os.getpid()),
                  "teardown_claim_pid_start":D.process_start_ticks(os.getpid())},
    lambda _fields:True))
   self.assertFalse(D.close_attempt_row(jobs,attempt,"ordinary-completion"))
   self.assertFalse(D.close_attempt_row_if(
    jobs,attempt,"ordinary-reconcile",lambda _fields:True))
   self.assertFalse(D.close_attempt_row_if(
    jobs,attempt,"wrong-owner",lambda _fields:True,teardown_claim="claim-2"))
   self.assertTrue(D.close_attempt_row_if(
    jobs,attempt,"dead-parent-terminated",lambda _fields:True,
    teardown_claim="claim-1"))
   metadata=D.parse_registry_metadata(jobs.read_text().split("\t")[5])
   self.assertEqual(metadata.get("teardown_claim"),"")
   self.assertEqual(metadata.get("teardown_claim_pid"),"")
   self.assertEqual(metadata["note"],"dead-parent-terminated")
 def test_quick_attempts_are_serial_and_exhaust_exactly(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   def row(attempt,stamp):
    return (f"{stamp}\topen\t/r\t/w\tquick\t{CURRENT},route_id=rt-q,"
            f"route_node=one-shot,attempt_id={attempt}")
   exclusive={"route_id":"rt-q","route_node":"one-shot"}
   first="att-quick000001"; second="att-quick000002"; third="att-quick000003"
   self.assertTrue(D.claim_attempt_row(jobs,first,row(first,"2026-07-20T00:00:00Z"),
                                       launch=True,exclusive_live_metadata=exclusive,
                                       terminal_attempt_limit=2))
   self.assertFalse(D.claim_attempt_row(jobs,second,row(second,"2026-07-20T00:00:01Z"),
                                        launch=True,exclusive_live_metadata=exclusive,
                                        terminal_attempt_limit=2))
   self.assertTrue(D.close_attempt_row(jobs,first,"dead-fixture"))
   self.assertTrue(D.claim_attempt_row(jobs,second,row(second,"2026-07-20T00:00:02Z"),
                                       launch=True,exclusive_live_metadata=exclusive,
                                       terminal_attempt_limit=2))
   self.assertTrue(D.close_attempt_row(jobs,second,"dead-fixture"))
   with self.assertRaises(D.DispatchContractError) as caught:
    D.claim_attempt_row(jobs,third,row(third,"2026-07-20T00:00:03Z"),
                        launch=True,exclusive_live_metadata=exclusive,
                        terminal_attempt_limit=2)
   self.assertEqual(caught.exception.reason,"quick-registered-headless-exhausted")
   self.assertEqual(len(jobs.read_text().splitlines()),2)
 # Item 4: a failed terminal note in replacement_notes gets its own budget
 # (default 1) separate from terminal_attempt_limit, so one dead-protocol/
 # dead-permission-reject terminal allows exactly one same-route relaunch
 # without touching the ordinary success-exhaustion behavior.
 def test_replacement_attempt_budget_is_separate_from_success_exhaustion(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   def row(attempt,stamp):
    return (f"{stamp}\topen\t/r\t/w\tquick\t{CURRENT},route_id=rt-r,"
            f"route_node=one-shot,attempt_id={attempt}")
   exclusive={"route_id":"rt-r","route_node":"one-shot"}
   notes=frozenset({"dead-protocol","dead-permission-reject"})
   first="att-repl000001"; second="att-repl000002"; third="att-repl000003"
   self.assertTrue(D.claim_attempt_row(jobs,first,row(first,"2026-07-21T00:00:00Z"),
                                       launch=True,exclusive_live_metadata=exclusive,
                                       terminal_attempt_limit=1,
                                       replacement_attempt_limit=1,replacement_notes=notes))
   self.assertTrue(D.close_attempt_row(jobs,first,"dead-protocol"))
   # replacement 1 of 1: same route relaunches once after the failed terminal.
   self.assertTrue(D.claim_attempt_row(jobs,second,row(second,"2026-07-21T00:00:01Z"),
                                       launch=True,exclusive_live_metadata=exclusive,
                                       terminal_attempt_limit=1,
                                       replacement_attempt_limit=1,replacement_notes=notes))
   self.assertTrue(D.close_attempt_row(jobs,second,"dead-permission-reject"))
   # replacement budget now exhausted (2 replacement terminals > limit 1).
   with self.assertRaises(D.DispatchContractError) as caught:
    D.claim_attempt_row(jobs,third,row(third,"2026-07-21T00:00:02Z"),
                        launch=True,exclusive_live_metadata=exclusive,
                        terminal_attempt_limit=1,
                        replacement_attempt_limit=1,replacement_notes=notes)
   self.assertEqual(caught.exception.reason,"quick-replacement-attempts-exhausted")
 def test_replacement_notes_do_not_relax_post_success_exhaustion(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   def row(attempt,stamp):
    return (f"{stamp}\topen\t/r\t/w\tquick\t{CURRENT},route_id=rt-s,"
            f"route_node=one-shot,attempt_id={attempt}")
   exclusive={"route_id":"rt-s","route_node":"one-shot"}
   notes=frozenset({"dead-protocol","dead-permission-reject"})
   first="att-succ000001"; second="att-succ000002"
   self.assertTrue(D.claim_attempt_row(jobs,first,row(first,"2026-07-22T00:00:00Z"),
                                       launch=True,exclusive_live_metadata=exclusive,
                                       terminal_attempt_limit=1,
                                       replacement_attempt_limit=1,replacement_notes=notes))
   # a success note is not in replacement_notes, so it still counts against
   # the ordinary (unrelaxed) terminal_attempt_limit -- duplicate launch
   # after success stays refused exactly as before item 4.
   self.assertTrue(D.close_attempt_row(jobs,first,"completed-marker"))
   with self.assertRaises(D.DispatchContractError) as caught:
    D.claim_attempt_row(jobs,second,row(second,"2026-07-22T00:00:01Z"),
                        launch=True,exclusive_live_metadata=exclusive,
                        terminal_attempt_limit=1,
                        replacement_attempt_limit=1,replacement_notes=notes)
   self.assertEqual(caught.exception.reason,"quick-registered-headless-exhausted")
 def test_open_running_duplicate_launch_still_refused_with_replacement_budget(self):
  with tempfile.TemporaryDirectory() as td:
   jobs=Path(td)/"jobs.log"
   def row(attempt,stamp):
    return (f"{stamp}\topen\t/r\t/w\tquick\t{CURRENT},route_id=rt-o,"
            f"route_node=one-shot,attempt_id={attempt}")
   exclusive={"route_id":"rt-o","route_node":"one-shot"}
   notes=frozenset({"dead-protocol"})
   first="att-open000001"; second="att-open000002"
   self.assertTrue(D.claim_attempt_row(jobs,first,row(first,"2026-07-23T00:00:00Z"),
                                       launch=True,exclusive_live_metadata=exclusive,
                                       terminal_attempt_limit=1,
                                       replacement_attempt_limit=1,replacement_notes=notes))
   # first row is still open|running -- immediate refusal, unaffected by the
   # replacement budget existing at all.
   self.assertFalse(D.claim_attempt_row(jobs,second,row(second,"2026-07-23T00:00:01Z"),
                                        launch=True,exclusive_live_metadata=exclusive,
                                        terminal_attempt_limit=1,
                                        replacement_attempt_limit=1,replacement_notes=notes))
 def test_orphan_watch_launch_is_exact_and_detached(self):
  fake=mock.Mock(pid=4321)
  with mock.patch.object(D.subprocess,"Popen",return_value=fake) as popen:
   watcher=D.launch_orphan_watch(
    Path("/tmp/jobs.log"),Path("/tmp/agent-home"),"att-watch-contract",1234,"5678")
  self.assertEqual(watcher,4321)
  argv=popen.call_args.args[0]
  self.assertIn("dispatch-orphan-watch.py",argv[1])
  self.assertIn("att-watch-contract",argv)
  self.assertEqual(popen.call_args.kwargs["cwd"],"/")
  self.assertTrue(popen.call_args.kwargs["start_new_session"])
  with self.assertRaises(D.DispatchContractError) as caught:
   D.launch_orphan_watch(Path("/tmp/jobs.log"),Path("/tmp/home"),"",0,"")
  self.assertEqual(caught.exception.reason,"orphan-watch-identity-invalid")
 def test_legacy_reconcile_is_read_only(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); local=root/"local.log"; global_jobs=root/"global.log"
   rows=[]
   for i in range(6):
    pipe=f"capability=code-plan,route_id=rt-1,route_node=plan,parent=owner,attempt_id=att-{i:012d},note=dead-network"
    rows.append(f"2026-07-15T00:00:0{i}Z\tdone\t/repo\t/wt\tstage-r{i}\t{pipe}\n")
   local.write_text("".join(rows),encoding="utf-8")
   self.assertEqual(D.reconcile_local_registry(global_jobs,local),(0,6))
   self.assertEqual(global_jobs.read_text(encoding="utf-8"),"")
 def test_current_reconcile_is_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); local=root/"local.log"; global_jobs=root/"global.log"
   rows=[]
   for i in range(2):
    attempt=f"att-current{i:05d}"
    pipe=f"{CURRENT},route_id=rt-1,route_node=plan,parent=owner,attempt_id={attempt},note=dead-network"
    rows.append(f"2026-07-15T00:00:0{i}Z\tdone\t/repo\t/wt\tstage-r{i}\t{pipe}\n")
   local.write_text("".join(rows),encoding="utf-8")
   self.assertEqual(D.reconcile_local_registry(global_jobs,local),(2,0))
   self.assertEqual(D.reconcile_local_registry(global_jobs,local),(0,0))
   copied=global_jobs.read_text(encoding="utf-8").splitlines()
   self.assertEqual(len(copied),2)
   self.assertTrue(all("reconciled_from=" in row for row in copied))

 def test_parent_sandbox_table_matches_what_the_wrappers_export(self):
  # WRAPPER_PARENT_SANDBOXES is a copy of literals owned by the adapters. It
  # used to gate only the probe, so drift was cheap; it now also gates route
  # compile, where a stale copy would reject correctly probed evidence.
  import re
  root=P.parent.parent
  for harness in D.WRAPPER_PARENT_HARNESSES:
   source=(root/"adapters"/harness/"bin"/"dispatch-headless.py").read_text(encoding="utf-8")
   export=re.search(r'"AGENT_DISPATCH_CURRENT_SANDBOX":\s*([^,\n]+)',source)
   self.assertIsNotNone(export,harness)
   value=export.group(1).strip()
   with self.subTest(harness=harness):
    if value.startswith('"'):
     self.assertEqual(set(D.WRAPPER_PARENT_SANDBOXES[harness]),{value.strip('"')})
    else:
     # codex resolves dynamically: the --sandbox choices plus the nested
     # danger-full-access downgrade in effective_runtime_sandbox().
     choices=re.search(r'"--sandbox",\s*\n\s*choices=\(([^)]*)\)',source)
     self.assertIsNotNone(choices,harness)
     labels=set(re.findall(r'"([a-z-]+)"',choices.group(1)))
     body=source.split("def effective_runtime_sandbox",1)[1].split("\ndef ",1)[0]
     labels |= set(re.findall(r'return\s+"([a-z-]+)"',body))
     self.assertEqual(set(D.WRAPPER_PARENT_SANDBOXES[harness]),labels)

 def test_depth2_parent_transport_must_be_registered_headless(self):
  # The depth-2 parent is the depth-1 owner; `interactive` is canonical
  # vocabulary for the depth-0 caller and a contradiction at this call site.
  with self.assertRaises(D.DispatchContractError) as caught:
   D.validate_nested_eligibility(dispatch_depth=2,action="start",parent_harness="claude",
    parent_transport="interactive",parent_sandbox="adapter-default",child_harness="claude",
    launch_authority="conductor",status="supported",source="fixture")
  self.assertEqual(caught.exception.reason,"parent-transport-not-registered-headless")
  D.validate_nested_eligibility(dispatch_depth=1,action="start",parent_harness="claude",
   parent_transport="interactive",parent_sandbox="adapter-default",child_harness="claude",
   launch_authority="conductor",status="supported",source="fixture")

 # A-P1. The leader leaves a tagged descendant in its own session, so the
 # recorded process group empties while the attempt is still running. The
 # asserts on _attempt_process_quiescence_impl are the point: they pin that this
 # fixture is red without the seam and green with it, permanently.
 def test_escaped_attempt_descendant_refuses_a_false_quiescent_verdict(self):
  attempt="att-escaped-descendant-fixture"
  script=("import os,subprocess,sys\n"
          "env=dict(os.environ,AGENT_DISPATCH_ATTEMPT_ID=sys.argv[1])\n"
          "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],\n"
          "                 env=env,start_new_session=True)\n")
  proc=subprocess.Popen([sys.executable,"-c",script,attempt],start_new_session=True)
  identity=dict(D.process_launch_identity(proc.pid),attempt_id=attempt)
  proc.wait(timeout=10)
  probe=None
  for _ in range(50):
   probe=D.attempt_tagged_descendants(identity)
   if probe.state=="populated":break
   time.sleep(0.1)
  try:
   self.assertEqual(probe.state,"populated",probe.reason)
   # Without the repair the recorded group is empty and this reads as done.
   self.assertEqual(D._attempt_process_quiescence_impl(identity).state,"quiescent")
   verdict=D.attempt_process_quiescence(identity)
   self.assertEqual((verdict.state,verdict.reason),
                    ("live","attempt-descendant-live"))
   self.assertEqual(verdict.pid,probe.members[0][0])
  finally:
   for pid,_start,_state in probe.members if probe else ():
    try:os.kill(pid,9)
    except OSError:pass

 # A-N1. A confirmed death still advances, with its original reason intact.
 def test_confirmed_death_without_tagged_processes_stays_quiescent(self):
  proc=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],
                        start_new_session=True)
  identity=dict(D.process_launch_identity(proc.pid),
                attempt_id="att-confirmed-death-fixture")
  proc.terminate();proc.wait(timeout=5)
  self.assertEqual(D.attempt_tagged_descendants(identity).state,"empty")
  verdict=D.attempt_process_quiescence(identity)
  self.assertEqual(verdict.state,"quiescent")
  self.assertEqual(verdict.reason,
                   D._attempt_process_quiescence_impl(identity).reason)

 # A-N2 is test_process_group_descendant_keeps_attempt_live_after_leader_exit:
 # a survivor inside the recorded group is already `live`, so it never reaches
 # the post-processing seam at all. Left where it is rather than duplicated.

 # A-N3. An empty scan is only evidence of absence in the namespace that could
 # have seen the process. A reap receipt recorded elsewhere remains unusable in
 # ordinary liveness queries; only an exact terminal readiness gate may consume
 # a complete receipt that includes the launcher's tagged-descendant proof.
 def test_empty_scan_from_a_foreign_namespace_is_unverifiable(self):
  pid=str(os.getpid())
  receipt={
   "pid":pid,"pgid":pid,"pid_start":D.process_start_ticks(os.getpid()),
   "pid_ns":"pid:[foreign]","pid_observer_ns":"pid:[foreign]",
   "launch_lifecycle":"foreground-scoped",
   "launch_outcome":"governed-process-reaped",
   "group_reap_proof":D.GROUP_REAP_PROOF,"group_reap_pgid":pid,
   "attempt_id":"att-foreign-namespace-fixture",
  }
  self.assertEqual(D._attempt_process_quiescence_impl(receipt).state,"quiescent")
  probe=D.attempt_tagged_descendants(receipt)
  self.assertEqual((probe.state,probe.reason),
                   ("unverifiable","observer-namespace-mismatch"))
  verdict=D.attempt_process_quiescence(receipt)
  self.assertEqual((verdict.state,verdict.reason),
                   ("unverifiable","attempt-descendant-unverifiable"))
  terminal=D.attempt_process_quiescence(receipt,terminal_receipt=True)
  self.assertEqual((terminal.state,terminal.reason),
                   ("quiescent","governed-process-group-reaped"))
  # A canonical terminal registry row is itself the exact terminal gate. Its
  # complete post-exit receipt must survive the observer namespace exit rather
  # than being revived by a stale summary/UI heartbeat.
  observed=D.observed_attempt_liveness("done",receipt)
  self.assertEqual((observed.state,observed.process_state),
                   ("terminal","quiescent"))
  self.assertEqual(observed.process_reason,"governed-process-group-reaped")
  still_open=D.observed_attempt_liveness("open",receipt)
  self.assertEqual((still_open.state,still_open.process_state),
                   ("unverifiable","unverifiable"))
  # An equivalent receipt observed from its own namespace keeps its verdict, so
  # the seam did not break the ordinary foreground-reap path.
  proc=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],
                        start_new_session=True)
  reaped=str(proc.pid)
  start=D.process_start_ticks(proc.pid)
  proc.terminate();proc.wait(timeout=5)
  here=D.process_namespace_identity()
  local=dict(receipt,pid=reaped,pgid=reaped,pid_start=start,
             group_reap_pgid=reaped,pid_ns=here,pid_observer_ns=here)
  self.assertEqual(D.attempt_tagged_descendants(local).state,"empty")
  self.assertEqual(D.attempt_process_quiescence(local).state,"quiescent")

 # Whether an empty scan proves absence is a different question from whether a
 # recorded PID number means anything here, and the two must not be conflated:
 # a namespace-local row is unreadable by PID yet fully scannable from the
 # namespace that watched it launch.
 def test_scan_authority_is_not_pid_identity_authority(self):
  here=D.process_namespace_identity()
  ghost={"pid":"7","pid_start":"1","pid_scope":"namespace-local",
         "pid_ns":"pid:[inner]","pid_observer_ns":here}
  self.assertFalse(D.local_identity_namespace_authority(ghost))
  self.assertTrue(D.attempt_scan_namespace_authority(ghost))
  # Proven procfs-root observation sees every descendant, whoever recorded it.
  outer=dict(ghost,pid_observer_ns="pid:[inner]",pid_host="7",
             pid_host_ns=here,pid_host_proof=D.PID_HOST_NAMESPACE_PROOF)
  self.assertTrue(D.attempt_scan_namespace_authority(outer))
  # A sibling namespace's empty scan is invisibility, not absence.
  self.assertFalse(D.attempt_scan_namespace_authority(
   dict(ghost,pid_observer_ns="pid:[elsewhere]")))
  # Rows predating the observer field stay readable if they were host-visible.
  self.assertTrue(D.attempt_scan_namespace_authority({"pid":"7"}))
  self.assertFalse(D.attempt_scan_namespace_authority(
   {"pid":"7","pid_scope":"namespace-local"}))

 def sibling_gate_case(self,td,note,sibling_metadata,attempt="att-gate-newcomer",
                       status="done"):
  """Run completion_marker_gate for a node whose only sibling row is `note`."""
  route={"dispatch_contract_version":3,"route_id":"rt-sibling-gate",
         "nodes":[{"id":"execute","depends_on":[]}]}
  path=Path(td)/"route.json"; path.write_text(json.dumps(route),encoding="utf-8")
  pipe=",".join(f"{k}={v}" for k,v in {
   **{"route_id":"rt-sibling-gate","route_node":"execute","note":note},
   **sibling_metadata}.items())
  lines=[f"2026-08-07T00:00:00Z\t{status}\t/repo\t/wt\texecute\t{pipe}"]
  # The newcomer's own claimed row is already in the registry when the gate
  # runs at launch, and it has no pid yet -- it must not block itself.
  lines.append(
   "2026-08-07T00:00:01Z\topen\t/repo\t/wt\texecute\t"
   f"route_id=rt-sibling-gate,route_node=execute,attempt_id={attempt}")
  D.completion_marker_gate(str(path),"execute","start",Path(td),Path(td)/"jobs.log",
                           registry_lines=lines,attempt_id=attempt)

 # A-P2. A sibling attempt whose row was closed by a false death verdict, but
 # whose tagged descendant is still running, stops the launch before any spawn.
 def test_live_sibling_attempt_blocks_the_node_before_any_spawn(self):
  attempt="att-live-sibling-fixture"
  script=("import os,subprocess,sys\n"
          "env=dict(os.environ,AGENT_DISPATCH_ATTEMPT_ID=sys.argv[1])\n"
          "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],\n"
          "                 env=env,start_new_session=True)\n")
  proc=subprocess.Popen([sys.executable,"-c",script,attempt],start_new_session=True)
  sibling=dict(D.process_launch_identity(proc.pid),attempt_id=attempt)
  proc.wait(timeout=10)
  probe=None
  for _ in range(50):
   probe=D.attempt_tagged_descendants(sibling)
   if probe.state=="populated":break
   time.sleep(0.1)
  try:
   self.assertEqual(probe.state,"populated",probe.reason)
   with tempfile.TemporaryDirectory() as td:
    with self.assertRaises(D.DispatchContractError) as caught:
     self.sibling_gate_case(td,"dead-worker-fail",sibling)
   self.assertEqual(caught.exception.reason,"prior-attempt-still-live")
   self.assertIn(attempt,caught.exception.detail)
   # The one exit code every caller maps to "nothing spawned, waiting may help".
   self.assertIn("prior-attempt-still-live",D.PRELAUNCH_PROCESS_BLOCK_REASONS)
  finally:
   for pid,_start,_state in probe.members if probe else ():
    try:os.kill(pid,9)
    except OSError:pass

 # A-N4/A-N5. An ordinary retry follows a sibling that really did stop, whether
 # it was cooled off for capacity or gave up on its own. This is the control
 # that proves the gate does not wedge the pipeline.
 def test_quiescent_sibling_never_blocks_an_ordinary_retry(self):
  proc=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],
                        start_new_session=True)
  sibling=dict(D.process_launch_identity(proc.pid),
               attempt_id="att-quiescent-sibling-fixture")
  proc.terminate();proc.wait(timeout=5)
  self.assertEqual(D.attempt_process_quiescence(sibling).state,"quiescent")
  for note in ("dead-capacity","dead-no-progress","dead-worker-fail"):
   with tempfile.TemporaryDirectory() as td:
    self.sibling_gate_case(td,note,sibling)

 def test_terminal_foreign_namespace_receipt_allows_successor_and_retry(self):
  pid=str(os.getpid())
  receipt={
   "pid":pid,"pgid":pid,"pid_start":D.process_start_ticks(os.getpid()),
   "pid_ns":"pid:[extinct]","pid_observer_ns":"pid:[extinct]",
   "launch_lifecycle":"foreground-scoped",
   "launch_outcome":"governed-process-reaped",
   "group_reap_proof":D.GROUP_REAP_PROOF,"group_reap_pgid":pid,
   "attempt_descendant_proof":"attempt-tagged-empty-v1",
   "attempt_descendant_observer_ns":"pid:[extinct]",
  }
  for predecessor_harness,successor_harness in (
      ("claude","codex"),("codex","claude")):
   with self.subTest(
       predecessor=predecessor_harness,successor=successor_harness), \
       tempfile.TemporaryDirectory() as td:
    base=Path(td)
    route_id=f"rt-58b1f678fad8f3f8-{predecessor_harness}-{successor_harness}"
    predecessor_receipt=dict(receipt)
    if predecessor_harness == "claude":
     predecessor_receipt.update(
      launch_lifecycle="detached",
      launch_outcome="governed-process-group-drained",
     )
    route={
     "dispatch_contract_version":3,"route_id":route_id,
     "nodes":[
      {"id":"plan","depends_on":[],"harness_affinity":predecessor_harness},
      {"id":"execute","depends_on":["plan"],
       "harness_affinity":successor_harness},
     ],
    }
    route_path=base/"route.json"
    route_path.write_text(json.dumps(route),encoding="utf-8")
    marker_dir=base/".dispatch"/"completion"/route_id
    marker_dir.mkdir(parents=True)
    (marker_dir/"plan.json").write_text(json.dumps({
     "attempt_id":"att-terminal-predecessor","registered_worker":True,
    }),encoding="utf-8")
    pipe=",".join(f"{key}={value}" for key,value in {
     **{"route_id":route_id,"route_node":"plan",
        "attempt_id":"att-terminal-predecessor","note":"completed-marker",
        "harness":predecessor_harness},
     **predecessor_receipt,
    }.items())
    rows=[
     "2026-08-09T00:00:00Z\tdone\t/repo\t/wt\tplan\t"
     f"{CURRENT},{pipe}"
    ]
    with mock.patch.object(D,"completion_marker_is_current",return_value=True):
     D.completion_marker_gate(
      str(route_path),"execute","start",base,base/"jobs.log",
      registry_lines=rows,attempt_id="att-successor-new",
     )

  sibling=dict(receipt,attempt_id="att-terminal-sibling")
  with tempfile.TemporaryDirectory() as td:
   self.sibling_gate_case(td,"completed-marker",sibling)
  with tempfile.TemporaryDirectory() as td:
   with self.assertRaises(D.DispatchContractError) as caught:
    self.sibling_gate_case(td,"completed-marker",sibling,status="open")
  self.assertEqual(caught.exception.reason,"prior-attempt-unverifiable")

 def _auxiliary_group_route(self,base,route_id="rt-aux-arbitration"):
  """A realized auxiliary-bearing group (owner-merge arbiter) plus one consumer."""
  def leg(index,suffix,leg_class):
   node_id="plan-check" if index==0 else f"plan-check-{suffix}"
   return {"id":node_id,"depends_on":["plan"],"kind":"review-worker",
           "completion_gate":"code-plan","dispatch_depth":2,
           "parallel_group":"plan-check","parallel_leg_index":index,
           "parallel_anchor":"plan-check","leg_class":leg_class}
  route={"dispatch_contract_version":3,"route_id":route_id,
         "route_hash":"sha256:"+"7"*64,"registry_digest":"sha256:"+"8"*64,
         "nodes":[{"id":"plan","depends_on":[],"kind":"pipeline-stage",
                   "completion_gate":"code-plan-draft","dispatch_depth":2},
                  leg(0,"anchor","peer"),leg(1,"alternative","peer"),
                  leg(2,"simplicity","auxiliary"),
                  {"id":"execute","kind":"pipeline-stage","dispatch_depth":2,
                   "completion_gate":"code-execute",
                   "depends_on":["plan-check","plan-check-alternative",
                                 "plan-check-simplicity"]}]}
  path=base/"route.json"; path.write_text(json.dumps(route),encoding="utf-8")
  marker_dir=base/".dispatch"/"completion"/route_id
  marker_dir.mkdir(parents=True,exist_ok=True)
  for node in route["nodes"]:
   (marker_dir/f"{node['id']}.json").write_text(json.dumps({
    "attempt_id":f"att-{node['id']}","registered_worker":True,
   }),encoding="utf-8")
  return route,path,marker_dir

 # G1 (d) 1: `execute` depends on every leg of an auxiliary-bearing group whose
 # arbiter is the owner's merge record. Completion markers alone are not the
 # whole gate -- without a registered arbitration the start is refused with its
 # own typed reason, before the wrapper spawns anything.
 def test_unarbitrated_auxiliary_group_refuses_the_dependent_start(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)
   route,path,marker_dir=self._auxiliary_group_route(base)
   ready=D.AttemptReadiness("ready","fixture-ready","att-predecessor")
   with mock.patch.object(D,"completion_marker_is_current",return_value=True), \
        mock.patch.object(D,"completion_attempt_readiness",return_value=ready), \
        mock.patch.object(D,"_sibling_attempt_gate"):
    with self.assertRaises(D.DispatchContractError) as caught:
     D.completion_marker_gate(str(path),"execute","start",base,base/"jobs.log",
                              registry_lines=[],attempt_id="att-execute-new")
    self.assertEqual(caught.exception.reason,"auxiliary-arbitration-missing")
    self.assertEqual(caught.exception.detail,"plan-check")
    # a member's OWN start is not gated by its group's arbitration: the
    # arbitration cannot exist until the group has joined (G1 regression).
    D.completion_marker_gate(str(path),"plan-check","start",base,base/"jobs.log",
                             registry_lines=[],attempt_id="att-anchor-new")
    # registering the owner merge record opens the dependent start
    evidence=base/"merge_record.md"
    evidence.write_text("---\nauxiliary_findings_considered:\n  - adopted\n---\n",
                        encoding="utf-8")
    record={"schema_version":1,"route_id":route["route_id"],
            "route_hash":route["route_hash"],
            "registry_digest":route["registry_digest"],
            "group_id":"plan-check","anchor_node":"plan-check",
            "arbiter":"owner-merge",
            "member_nodes":["plan-check","plan-check-alternative","plan-check-simplicity"],
            "auxiliary_nodes":["plan-check-simplicity"],
            "auxiliary_findings_considered":["adopted"],
            "evidence":{"path":str(evidence),
                        "sha256":hashlib.sha256(evidence.read_bytes()).hexdigest()}}
    (marker_dir/"plan-check.arbitration.json").write_text(
     json.dumps(record,indent=2),encoding="utf-8")
    D.completion_marker_gate(str(path),"execute","start",base,base/"jobs.log",
                             registry_lines=[],attempt_id="att-execute-new")

 # B4: the gate is HANDED its state root so the writer and every reader are
 # structurally forced onto one root. A record it was never handed must not open
 # the spawn -- `_arbitration_observation`'s `path=None` fallback re-resolves the
 # root from the environment, which made the gate fail OPEN across a state-root
 # rotation (`dispatch_state_root_rotation` makes that rotation real).
 def test_arbitration_under_another_state_root_does_not_open_the_start(self):
  def probe(where):
   with tempfile.TemporaryDirectory() as td:
    gate_home=Path(td)/"gate_home"; env_home=Path(td)/"env_home"
    gate_home.mkdir(); env_home.mkdir()
    gate_jobs=gate_home/".dispatch"/"jobs.log"
    gate_jobs.parent.mkdir(parents=True,exist_ok=True); gate_jobs.touch()
    env_jobs=env_home/".dispatch"/"jobs.log"
    env_jobs.parent.mkdir(parents=True,exist_ok=True); env_jobs.touch()
    route,path,_marker_dir=self._auxiliary_group_route(gate_home)
    target=(gate_home if where=="handed" else env_home)/".dispatch"/"completion"/route["route_id"]
    target.mkdir(parents=True,exist_ok=True)
    evidence=Path(td)/"merge_record.md"
    evidence.write_text("---\nauxiliary_findings_considered:\n  - adopted\n---\n",
                        encoding="utf-8")
    record={"schema_version":1,"route_id":route["route_id"],
            "route_hash":route["route_hash"],
            "registry_digest":route["registry_digest"],
            "group_id":"plan-check","anchor_node":"plan-check","arbiter":"owner-merge",
            "member_nodes":["plan-check","plan-check-alternative","plan-check-simplicity"],
            "auxiliary_nodes":["plan-check-simplicity"],
            "auxiliary_findings_considered":["adopted"],
            "evidence":{"path":str(evidence),
                        "sha256":hashlib.sha256(evidence.read_bytes()).hexdigest()}}
    (target/"plan-check.arbitration.json").write_text(json.dumps(record,indent=2),
                                                      encoding="utf-8")
    ready=D.AttemptReadiness("ready","fixture-ready","att-predecessor")
    # the environment points at a DIFFERENT dispatch state root than the one
    # the gate was handed -- exactly what a rotation leaves behind
    with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(env_jobs)}), \
         mock.patch.object(D,"completion_marker_is_current",return_value=True), \
         mock.patch.object(D,"completion_attempt_readiness",return_value=ready), \
         mock.patch.object(D,"_sibling_attempt_gate"):
     D.completion_marker_gate(str(path),"execute","start",gate_home,gate_jobs,
                              registry_lines=[],attempt_id="att-execute-new")
  with self.assertRaises(D.DispatchContractError) as caught:
   probe("env")
  self.assertEqual(caught.exception.reason,"auxiliary-arbitration-missing")
  # and the same gate still opens on a record under the root it WAS handed,
  # so this is a one-root rule and not a blanket refusal
  probe("handed")

 # M3: a route-integrity failure and "the owner has not merged yet" are different
 # events with different responses -- `arbitrate` resolves the second and raises
 # on the first at the same point with the same error. The start gate used to
 # call both `auxiliary-arbitration-missing`, sending the operator to a command
 # that cannot help, while `terminal_gate_observation` already used the accurate
 # name. The two consumers now agree.
 def test_unresolvable_arbiter_is_named_apart_from_an_unmerged_group(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)
   route,path,_marker_dir=self._auxiliary_group_route(base)
   ready=D.AttemptReadiness("ready","fixture-ready","att-predecessor")
   with mock.patch.object(D,"completion_marker_is_current",return_value=True), \
        mock.patch.object(D,"completion_attempt_readiness",return_value=ready), \
        mock.patch.object(D,"_sibling_attempt_gate"):
    # a resolvable group that nobody has arbitrated yet
    with self.assertRaises(D.DispatchContractError) as caught:
     D.completion_marker_gate(str(path),"execute","start",base,base/"jobs.log",
                              registry_lines=[],attempt_id="att-execute-new")
    self.assertEqual(caught.exception.reason,"auxiliary-arbitration-missing")
    # the same group, but its arbiter cannot be resolved at all
    route_module=D._route_module()
    with mock.patch.object(route_module,"owner_merge_auxiliary_groups",
                           return_value={"plan-check":
                                         "auxiliary-arbiter-ambiguous:plan-check:a,b"}):
     with self.assertRaises(D.DispatchContractError) as caught:
      D.completion_marker_gate(str(path),"execute","start",base,base/"jobs.log",
                               registry_lines=[],attempt_id="att-execute-new")
    self.assertEqual(caught.exception.reason,"auxiliary-arbiter-unresolved")
    self.assertIn("auxiliary-arbiter-ambiguous",caught.exception.detail)
    # the name matches the one the terminal gate already publishes
    self.assertEqual(
     route_module._arbitration_observation(
      route,"plan-check","auxiliary-arbiter-ambiguous:plan-check:a,b")["reason"],
     "auxiliary-arbiter-unresolved")

 # A row that never recorded a governed process cannot have leaked one, and
 # judging it unverifiable would wedge the node permanently.
 def test_sibling_row_without_a_recorded_process_is_not_a_claimant(self):
  with tempfile.TemporaryDirectory() as td:
   self.sibling_gate_case(td,"dead-claim-abandoned",
                          {"attempt_id":"att-never-launched-fixture"})

 # D-1. A legacy row carries no attempt id, so there is nothing to scan for and
 # its existing verdict is left exactly as it was.
 def test_legacy_row_without_attempt_id_keeps_its_verdict(self):
  proc=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],
                        start_new_session=True)
  identity=D.process_launch_identity(proc.pid)
  proc.terminate();proc.wait(timeout=5)
  self.assertNotIn("attempt_id",identity)
  self.assertEqual(D.attempt_tagged_descendants(identity).state,"unverifiable")
  self.assertEqual(D.attempt_process_quiescence(identity),
                   D._attempt_process_quiescence_impl(identity))

if __name__=="__main__": unittest.main()
