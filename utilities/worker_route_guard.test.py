#!/usr/bin/env python3
import contextlib, importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
R=load("route",ROOT/"utilities/capability-route.py"); G=load("guard",ROOT/"utilities/worker-route-guard.py")
ALL=["atomic-outcome","known-scope","no-shared-contract","no-resource-run","no-artifact-handoff","no-independent-verifier","focused-verification"]

def dispatch(worktree):
 return {"tuples":[{"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":"codex","launch_authority":"conductor","status":"supported","probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(Path(worktree).resolve()),"failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0}],"native_subagent":[]}

class WorkerRouteGuardTest(unittest.TestCase):
 def route(self):
  # A guarded worker needs a branch, but the source running this suite may be
  # an exact-SHA (detached) CI checkout. Own that worktree precondition instead
  # of borrowing the caller's branch or mocking away the real Git guard.
  temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
  self.route_worktree=Path(temp.name)/"worktree"; self.route_worktree.mkdir()
  subprocess.run(["git","init","-q","--initial-branch=guard-fixture",str(self.route_worktree)],check=True)
  subprocess.run(["git","-C",str(self.route_worktree),"-c","user.name=Fixture","-c","user.email=fixture@example.com","commit","--allow-empty","-qm","base"],check=True)
  gate={"spec_read":{"satisfied":True,"source":"prd-sha256"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor"}}
  return R.compile_route("autopilot-code","dev","strong",self.route_worktree,self.route_worktree,predicates=ALL,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(self.route_worktree))

 def reseal(self,route):
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  return route

 def test_launch_tuple_absent_and_incompatible_fail_closed(self):
  for case in ("absent","incompatible"):
   with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
    route=self.route()
    if case=="absent":
     route.pop("launch_compatibility_tuple")
     expected="launch-compatibility-tuple-required"
    else:
     route["launch_compatibility_tuple"]["runtime_root"]["binding_digest"]="sha256:"+"0"*64
     expected="launch-runtime-root-mismatch"
    self.reseal(route); path=Path(td)/"route.json"; path.write_text(json.dumps(route))
    # Plain material/spec callers remain compatible with legacy route records.
    if case=="absent":
     _,node,_=G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree)
     self.assertEqual(node["id"],"execute")
    with self.assertRaises(G.WorkerRouteError) as ctx:
     G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree,launch_phase="start")
    self.assertEqual(ctx.exception.reason,expected)
    if case=="incompatible":
     detail=json.loads(str(ctx.exception))
     self.assertIn(route["launch_compatibility_tuple"]["runtime_root"]["path"],detail["recovery"])
     self.assertIn("AGENT_HOME=",detail["recovery"])
     self.assertIn("runtime projection",detail["recovery"])
 def test_malformed_runtime_root_preserves_typed_refusal(self):
  for runtime in (7,[],None,{"path":7},{"path":[]},{"path":"relative/root"}):
   with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as td:
    route=self.route(); route["launch_compatibility_tuple"]["runtime_root"]=runtime
    self.reseal(route); path=Path(td)/"route.json"; path.write_text(json.dumps(route))
    with self.assertRaises(G.WorkerRouteError) as caught:
     G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree,launch_phase="start")
    self.assertEqual(caught.exception.reason,"launch-runtime-root-mismatch")
    detail=json.loads(str(caught.exception))
    self.assertIn("runtime_root",detail["mismatches"])
    self.assertIn("hearting/current",detail["recovery"])
    self.assertIn("AGENT_HOME=",detail["recovery"])
 def test_valid_and_scope_bound(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); path.write_text(json.dumps(route))
   _,node,git=G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree,"autopilot-code","strong",";".join(next(x for x in route["nodes"] if x["id"]=="execute")["write_scope"]),route["route_id"],route["route_hash"],route["registry_digest"])
   self.assertEqual(node["id"],"execute")
   self.assertEqual(git["branch"],"guard-fixture")
   self.assertRaisesRegex(G.WorkerRouteError,"expected=",G.validate_route_contract,path,"execute",self.route_worktree,self.route_worktree,"autopilot-code","strong","spec/**")
 def test_detached_spec_worktree_reports_safe_branch_recovery(self):
  with tempfile.TemporaryDirectory() as td:
   repo=Path(td)/"spec worktree"; repo.mkdir()
   def git(*args):
    return subprocess.run(["git","-C",str(repo),*args],check=True,capture_output=True,text=True)
   git("init","-q"); git("-c","user.name=Fixture","-c","user.email=fixture@example.com","commit","--allow-empty","-qm","base")
   git("checkout","--detach","-q")
   with self.assertRaises(G.WorkerRouteError) as caught: G._git_state(repo)
   self.assertEqual(caught.exception.reason,"unsafe-git-state")
   self.assertIn("git switch -c <new-branch>",str(caught.exception))
   self.assertIn("spec",str(caught.exception))
   git("switch","-c","spec-recovered")
   self.assertEqual(G._git_state(repo)["branch"],"spec-recovered")
 def test_hash_and_reselection_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); route["cwd"]="/tmp"; path.write_text(json.dumps(route))
   self.assertRaisesRegex(G.WorkerRouteError,"stale or modified",G.validate_route_contract,path,"execute",self.route_worktree,self.route_worktree)
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); path.write_text(json.dumps(route))
   self.assertRaisesRegex(G.WorkerRouteError,"expected=autopilot-code",G.validate_route_contract,path,"execute",self.route_worktree,self.route_worktree,"code-execute")
 def test_source_commit_mismatch_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   repo=Path(td)/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
   subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True)
   (repo/"x").write_text("a"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","a"],check=True)
   gate={"spec_read":{"satisfied":True,"source":"prd"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor"}}
   route=R.compile_route("autopilot-code","dev","strong",repo,repo,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(repo)); path=Path(td)/"route.json"; path.write_text(json.dumps(route))
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx: G.validate_route_contract(path,"execute",repo,repo)
   self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def _lineage_repo(self,td,*,lineage_rows=True):
  repo=Path(td)/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True)
  (repo/"x").write_text("a"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","a"],check=True)
  # Seal a tmpdir registry, never the operator's live one, and give the route the
  # shape production always has: a source route that reached a continuation has
  # dispatched something, so its lineage HAS rows. A registry holding no rows for
  # the lineage is indistinguishable from a truncated or foreign one, so the
  # continuation declines to re-pin there (SD-128 (4)).
  jobs=Path(td)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True,exist_ok=True)
  jobs.touch()
  previous=os.environ.get("AGENT_DISPATCH_JOBS")
  os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
  def restore():
   if previous is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
   else: os.environ["AGENT_DISPATCH_JOBS"]=previous
  self.addCleanup(restore)
  gate={"spec_read":{"satisfied":True,"source":"prd"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor"}}
  route=R.compile_route("autopilot-code","dev","strong",repo,repo,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(repo))
  if lineage_rows:
   # `frame` ran; `execute` has not -- so nothing declines the rebind.
   self._write_registry_row(
    Path(route["launch_compatibility_tuple"]["jobs_path"]["path"]),
    route["route_id"],"frame","att-frame-prior",
   )
  path=Path(td)/"route.json"; path.write_text(json.dumps(route))
  return repo,route,path

 def test_post_execute_descendant_head_passes(self):
  # SD-65 (a): a node depending on execute (test/report) accepts a HEAD that is
  # a first-parent descendant of source_commit -- execute's own commit advanced HEAD.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   _,node,_=G.validate_route_contract(path,"test",repo,repo)
   self.assertEqual(node["id"],"test")
   _,node,_=G.validate_route_contract(path,"report",repo,repo)
   self.assertEqual(node["id"],"report")

 def test_post_execute_diverged_head_rejected(self):
  # SD-65 (b): a rewritten (amended) root commit produces a HEAD unrelated to
  # route["source_commit"] -- not an ancestor at all -- and stays fail-closed
  # even for a post-execute node.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   subprocess.run(["git","-C",str(repo),"commit","--amend","-qm","a-rewritten"],check=True)
   with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx: G.validate_route_contract(path,"test",repo,repo)
   self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def test_pre_mutation_node_moved_head_rejected(self):
  # SD-65 (c): the plan node precedes execute (the first mutation node) and keeps the
  # exact-match requirement even though HEAD is a descendant of source_commit.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx: G.validate_route_contract(path,"plan",repo,repo)
   self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def test_execute_node_itself_moved_head_rejected(self):
  # SD-65: the first mutation node (execute) is grouped with the pre-mutation nodes --
  # it must still observe HEAD == source_commit before it starts mutating.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx: G.validate_route_contract(path,"execute",repo,repo)
   self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def _continuation(self,route,artifact_root):
  first=route["nodes"][0]["id"]
  return R.build_continuation_route(route,resume_from_node=first,requested_boundary=first,reason="resume-after-fast-forward",artifact_root=artifact_root)

 def test_c_continuation_after_fast_forward_dispatches_pre_mutation_nodes(self):
  # Defect C (route rt-d7541f1033ae677f): a continuation built after depth-0
  # fast-forwarded the worktree inherited the source route's compile-time pin while
  # sealing the new HEAD as grounding, so plan/plan-check/execute were refused with
  # route-source-commit-mismatch. The continuation must rebind the pin to HEAD and
  # every pre-mutation node must validate against it.
  with tempfile.TemporaryDirectory() as td:
   repo,source,_=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","fast-forward","-q"],check=True)
   head=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
   self.assertNotEqual(head,source["source_commit"])
   continuation=self._continuation(source,repo)
   self.assertEqual(continuation["source_commit"],head)
   rebind=continuation["source_commit_rebind"]
   self.assertEqual(rebind["inherited_source_commit"],source["source_commit"])
   self.assertEqual(rebind["rebound_source_commit"],head)
   self.assertEqual(rebind["basis"],"first-parent-descendant")
   self.assertEqual(continuation["launch_compatibility_tuple"]["grounding_roots"]["cwd"]["release_id"],head)
   path=Path(td)/"continuation.json"; path.write_text(json.dumps(continuation))
   for node_id in ("plan","plan-check","execute","test"):
    _,node,git=G.validate_route_contract(path,node_id,repo,repo)
    self.assertEqual(node["id"],node_id); self.assertEqual(git["head"],head)

 def test_c_continuation_on_unchanged_head_keeps_inherited_pin(self):
  with tempfile.TemporaryDirectory() as td:
   repo,source,_=self._lineage_repo(td)
   continuation=self._continuation(source,repo)
   self.assertEqual(continuation["source_commit"],source["source_commit"])
   self.assertNotIn("source_commit_rebind",continuation)
   path=Path(td)/"continuation.json"; path.write_text(json.dumps(continuation))
   _,node,_=G.validate_route_contract(path,"plan",repo,repo); self.assertEqual(node["id"],"plan")

 def test_c_declined_continuation_refuses_the_whole_pre_mutation_prefix(self):
  # Round 3, S1: a decline is not "one node is refused". It keeps the inherited
  # pin for the WHOLE route, so every node at or before the mutation node is
  # refused and the continuation cannot start -- it does not run up to execute
  # and stop there. Post-mutation nodes still pass on the SD-65 descendant
  # branch. Measured here rather than asserted in prose.
  with tempfile.TemporaryDirectory() as td:
   repo,source,_=self._lineage_repo(td,lineage_rows=False)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","fast-forward","-q"],check=True)
   head=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
   continuation=self._continuation(source,repo)
   # No lineage rows anywhere: the registry cannot prove the mutation node never
   # ran, so the pin stays inherited.
   self.assertEqual(continuation["source_commit"],source["source_commit"])
   self.assertNotIn("source_commit_rebind",continuation)
   path=Path(td)/"declined.json"; path.write_text(json.dumps(continuation))
   refused=[]
   accepted=[]
   for node in continuation["nodes"]:
    try:
     G.validate_route_contract(path,node["id"],repo,repo)
    except G.WorkerRouteError as exc:
     self.assertEqual(exc.reason,"route-source-commit-mismatch")
     refused.append(node["id"])
    else:
     accepted.append(node["id"])
   self.assertIn("plan",refused)
   self.assertIn("execute",refused)
   self.assertIn("test",accepted)
   self.assertNotEqual(head,source["source_commit"])
   # Every refused node is at or before the mutation node; nothing after it.
   ids=[node["id"] for node in continuation["nodes"]]
   self.assertLess(max(ids.index(n) for n in refused),min(ids.index(n) for n in accepted))

 def test_c_continuation_on_diverged_head_refused_typed(self):
  # A rewritten HEAD is off the history the source route bound: no node of such a
  # continuation could dispatch, so it is refused before any route file exists.
  with tempfile.TemporaryDirectory() as td:
   repo,source,_=self._lineage_repo(td)
   subprocess.run(["git","-C",str(repo),"commit","--amend","-qm","a-rewritten"],check=True)
   with self.assertRaisesRegex(ValueError,"continuation-source-commit-diverged"):
    self._continuation(source,repo)

 def _write_registry_row(self,jobs_path,route_id,node_id,attempt_id,status="done"):
  pipe=f"route_id={route_id},route_node={node_id},attempt_id={attempt_id}"
  line="\t".join(["2026-07-19T00:00:00Z",status,"repo","worktree","slug",pipe])
  with jobs_path.open("a",encoding="utf-8") as fh: fh.write(line+"\n")

 @contextlib.contextmanager
 def _bound_jobs(self,value):
  prior=os.environ.get("AGENT_DISPATCH_JOBS")
  if value is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
  else: os.environ["AGENT_DISPATCH_JOBS"]=value
  try: yield
  finally:
   if prior is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
   else: os.environ["AGENT_DISPATCH_JOBS"]=prior

 def test_mutation_retry_descendant_with_prior_attempt_passes(self):
  # A1: execute node, moved HEAD is a first-parent descendant, and a different
  # prior same-route/execute attempt row qualifies the SD-67 evidence branch.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   jobs=Path(td)/"jobs.log"; jobs.write_text("")
   self._write_registry_row(jobs,route["route_id"],"execute","att-prior")
   with self._bound_jobs(str(jobs)):
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
   self.assertEqual(node["id"],"execute")

 def test_mutation_first_launch_descendant_without_prior_attempt_rejected(self):
  # A2: same descendant HEAD but no qualifying registry row -- exact-match rejection stands.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   jobs=Path(td)/"jobs.log"; jobs.write_text("")
   with self._bound_jobs(str(jobs)):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
   self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def test_mutation_retry_diverged_head_rejected(self):
  # A3a: a qualifying registry row cannot authorize an amended/unrelated HEAD.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   subprocess.run(["git","-C",str(repo),"commit","--amend","-qm","a-rewritten"],check=True)
   jobs=Path(td)/"jobs.log"; jobs.write_text("")
   self._write_registry_row(jobs,route["route_id"],"execute","att-prior")
   with self._bound_jobs(str(jobs)):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
   self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def test_mutation_retry_registry_unavailable_rejected(self):
  # A3b: unset, missing-file, and unreadable/malformed registry bindings all fail closed.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   with self.subTest("env-unset"), self._bound_jobs(None):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")
   with self.subTest("missing-file"), self._bound_jobs(str(Path(td)/"absent.log")):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")
   with self.subTest("relative-path"), self._bound_jobs("relative/jobs.log"):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")
   malformed=Path(td)/"malformed.log"; malformed.write_text("not-six-fields\tonly-two\n")
   with self.subTest("malformed-rows"), self._bound_jobs(str(malformed)):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")

 def test_mutation_retry_current_attempt_only_rejected(self):
  # EX: the only matching row is the current launch's own identity -- self-evidence excluded.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   jobs=Path(td)/"jobs.log"; jobs.write_text("")
   self._write_registry_row(jobs,route["route_id"],"execute","att-current")
   with self._bound_jobs(str(jobs)):
    with self.assertRaisesRegex(G.WorkerRouteError,"expected=.* observed=") as ctx:
     G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(ctx.exception.reason,"route-source-commit-mismatch")
    self._write_registry_row(jobs,route["route_id"],"execute","att-prior")
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(node["id"],"execute")

 def test_non_git_cwd_boundary_unchanged(self):
  # SD-65 (d): non-git cwd keeps existing non-git handling (head="unversioned"),
  # which never equals a real source_commit and never matches the mutating-scope path.
  with tempfile.TemporaryDirectory() as td:
   nongit=Path(td)/"nongit"; nongit.mkdir()
   state=G._git_state(nongit)
   self.assertEqual(state,{"repository":"non-git","operation":"none","branch":"non-git","head":"unversioned"})

class ContinuationRetryLineageTest(WorkerRouteGuardTest):
 """SD-133: an SD-67 retry carried by a continuation is adjudicated, not refused.

 A continuation gets a new `route_id`, so the prior attempt that IS the retry
 evidence lives under its ancestor's id. `_qualifying_retry_evidence` looked
 only under the route's own id, so the evidence could never be found: the node
 was refused `route-source-commit-mismatch` and the operator's only recourse was
 to re-dispatch in place on the original route. Reproduced on main.
 """

 def _lineage_fixture(self, td):
  repo, source, _path = self._lineage_repo(td, lineage_rows=False)
  jobs = Path(os.environ["AGENT_DISPATCH_JOBS"])
  execute = next(n for n in source["nodes"] if n["id"] == "execute")
  self.assertIn("execute", source.get("resume_retry_boundaries", ()))
  # execute ran once under the SOURCE route and its commit advanced HEAD.
  self._write_registry_row(jobs, source["route_id"], "execute", "att-execute-prior")
  (repo/"x").write_text("b")
  subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
  continuation = self._continuation(source, repo)
  # The pin stays inherited: this is the SD-67 decline, working as designed.
  self.assertEqual(continuation["source_commit"], source["source_commit"])
  self.assertNotEqual(continuation["route_id"], source["route_id"])
  path = Path(td)/"continuation.json"
  path.write_text(json.dumps(continuation))
  return repo, source, continuation, path, execute

 def test_the_ancestors_attempt_is_found_through_the_lineage(self):
  with tempfile.TemporaryDirectory() as td:
   repo, source, continuation, path, _execute = self._lineage_fixture(td)
   self.assertIn(source["route_id"], R.continuation_lineage_route_ids(continuation))
   # Under the continuation's own id alone there is nothing -- which is why
   # main refuses.
   self.assertEqual(
    G.FALLBACK.registry_rows(
     Path(os.environ["AGENT_DISPATCH_JOBS"]), continuation["route_id"], "execute"),
    [])
   self.assertTrue(G._qualifying_retry_evidence(continuation, "execute", None))

 def test_the_mutation_node_now_validates_on_the_continuation(self):
  with tempfile.TemporaryDirectory() as td:
   repo, _source, _continuation, path, _execute = self._lineage_fixture(td)
   _route, node, _git = G.validate_route_contract(path, "execute", repo, repo)
   self.assertEqual(node["id"], "execute")

 def test_without_the_ancestor_attempt_the_node_is_still_refused(self):
  # The gate did not go away: remove the evidence and the same call refuses.
  with tempfile.TemporaryDirectory() as td:
   repo, _source, _continuation, path, _execute = self._lineage_fixture(td)
   Path(os.environ["AGENT_DISPATCH_JOBS"]).write_text("", encoding="utf-8")
   with self.assertRaises(G.WorkerRouteError) as ctx:
    G.validate_route_contract(path, "execute", repo, repo)
   self.assertEqual(ctx.exception.reason, "route-source-commit-mismatch")

 def test_an_attempt_on_a_different_node_does_not_authorise_this_one(self):
  # The widening is about WHERE the evidence may live, not WHAT counts as
  # evidence. An ancestor's attempt on `plan` must not license an `execute`
  # retry -- without the node filter it would, because the lineage read
  # returns every row for every ancestor route.
  with tempfile.TemporaryDirectory() as td:
   repo, source, _continuation, path, _execute = self._lineage_fixture(td)
   jobs = Path(os.environ["AGENT_DISPATCH_JOBS"])
   jobs.write_text("", encoding="utf-8")
   self._write_registry_row(jobs, source["route_id"], "plan", "att-plan-prior")
   with self.assertRaises(G.WorkerRouteError) as ctx:
    G.validate_route_contract(path, "execute", repo, repo)
   self.assertEqual(ctx.exception.reason, "route-source-commit-mismatch")

 def test_the_immediate_parent_arrives_by_two_paths(self):
  # For the FIRST generation only, `source_route_id` and the single
  # `supersession_edges` entry both name the same predecessor, so dropping
  # either alone changes nothing here. That redundancy does not extend to
  # grandparents -- see the next test, which is the majority shape in
  # production.
  with tempfile.TemporaryDirectory() as td:
   _repo, source, continuation, _path, _execute = self._lineage_fixture(td)
   self.assertEqual(continuation["source_route_id"], source["route_id"])
   self.assertIn(source["route_id"],
                 [edge.get("from_route_id")
                  for edge in continuation.get("supersession_edges", [])])

 def test_a_grandparents_attempt_reaches_only_through_supersession_edges(self):
  # The second generation is where the edges stop being redundant: the
  # grandparent is named by NO `source_route_id` on this record, only by an
  # inherited edge. I claimed this branch was un-catchable; it is not, and it
  # is the majority path -- 16 of 31 production continuation records carry two
  # or more edges (independent review, 2026-09-06).
  with tempfile.TemporaryDirectory() as td:
   repo, grandparent, first, _path, _execute = self._lineage_fixture(td)
   # Resume at the first node, as the first generation did: a later resume
   # point would need reused-evidence markers for the skipped prefix, which is
   # a different contract and not what this test is about.
   head_node = first["nodes"][0]["id"]
   second = R.build_continuation_route(
    first, resume_from_node=head_node, requested_boundary=head_node,
    reason="second-generation", artifact_root=first["artifact_root"])
   self.assertEqual(second["source_route_id"], first["route_id"])
   self.assertNotEqual(second["source_route_id"], grandparent["route_id"])
   # The grandparent is reachable only through the inherited edges.
   edges = [edge.get("from_route_id") for edge in second.get("supersession_edges", [])]
   self.assertIn(grandparent["route_id"], edges)
   self.assertIn(grandparent["route_id"], R.continuation_lineage_route_ids(second))
   # And its attempt is what admits the node two generations later.
   self.assertTrue(G._qualifying_retry_evidence(second, "execute", None))
   path = Path(td)/"second.json"
   path.write_text(json.dumps(second))
   _route, node, _git = G.validate_route_contract(path, "execute", repo, repo)
   self.assertEqual(node["id"], "execute")

 def test_a_node_outside_resume_retry_boundaries_is_still_refused(self):
  # SD-67's first condition is untouched: only a declared boundary may retry.
  with tempfile.TemporaryDirectory() as td:
   repo, _source, continuation, path, _execute = self._lineage_fixture(td)
   stripped = json.loads(json.dumps(continuation))
   stripped["resume_retry_boundaries"] = [
    n for n in stripped.get("resume_retry_boundaries", []) if n != "execute"]
   # Re-seal so the record still verifies with the narrowed boundary set.
   stripped["route_hash"] = R.route_hash(stripped)
   stripped["route_id"] = "rt-" + stripped["route_hash"].split(":",1)[1][:16]
   narrowed = Path(td)/"narrowed.json"
   narrowed.write_text(json.dumps(stripped))
   with self.assertRaises(G.WorkerRouteError) as ctx:
    G.validate_route_contract(narrowed, "execute", repo, repo)
   self.assertEqual(ctx.exception.reason, "route-source-commit-mismatch")


class QualifyingSubsessionLineageDeferredTest(unittest.TestCase):
 """C4 (S3a): the AND predicate's two success atoms via verdict_pass/success_note."""

 def _row(self, *, route_id, node_id, attempt_id, status, extra):
  meta = {"route_id": route_id, "route_node": node_id, "attempt_id": attempt_id}
  meta.update(extra)
  pipe = ",".join(f"{k}={v}" for k, v in meta.items())
  return "\t".join(["2026-09-24T00:00:00Z", status, "repo", "worktree", "slug", pipe])

 def _current_row(self, route_id, node_id, chain, count=2, index=2):
  return self._row(route_id=route_id, node_id=node_id, attempt_id="att-current", status="open", extra={
   "stage_authority": "0", "subsession_purpose": "planned", "subsession_mode": "serial",
   "session_chain_id": chain, "subsession_index": str(index), "subsession_count": str(count),
  })

 def test_pending_deferred_predecessor_does_not_qualify(self):
  with tempfile.TemporaryDirectory() as td:
   jobs = Path(td) / "jobs.log"
   route_id, node_id, chain = "rt-lineage-fixture", "execute", "ssc-fixture"
   predecessor = self._row(route_id=route_id, node_id=node_id, attempt_id="att-pred", status="done", extra={
    "stage_authority": "0", "subsession_purpose": "planned", "session_chain_id": chain,
    "subsession_count": "2", "subsession_index": "1",
    "note": "completion-deferred", "failure_class": "infrastructure",
    "classifier_source": "registered-wrapper-completion-transient-v1",
   })
   jobs.write_text(self._current_row(route_id, node_id, chain) + "\n" + predecessor + "\n", encoding="utf-8")
   with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
    self.assertFalse(G._qualifying_subsession_lineage(route_id, node_id, "att-current"))

 def test_marker_bound_deferred_predecessor_qualifies(self):
  with tempfile.TemporaryDirectory() as td:
   jobs = Path(td) / "jobs.log"
   route_id, node_id, chain = "rt-lineage-fixture", "execute", "ssc-fixture"
   predecessor = self._row(route_id=route_id, node_id=node_id, attempt_id="att-pred", status="done", extra={
    "stage_authority": "0", "subsession_purpose": "planned", "session_chain_id": chain,
    "subsession_count": "2", "subsession_index": "1",
    "note": "completed-marker", "failure_class": "infrastructure",
    "classifier_source": "registered-wrapper-completion-transient-v1",
    "completion_marker": "/artifacts/.runtime/completions/execute.json",
   })
   jobs.write_text(self._current_row(route_id, node_id, chain) + "\n" + predecessor + "\n", encoding="utf-8")
   with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
    self.assertTrue(G._qualifying_subsession_lineage(route_id, node_id, "att-current"))


if __name__=="__main__": unittest.main()
