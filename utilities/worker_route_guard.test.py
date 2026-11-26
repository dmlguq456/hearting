#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

 def test_valid_and_scope_bound(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); path.write_text(json.dumps(route))
   _,node,git=G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree,"autopilot-code","strong",";".join(next(x for x in route["nodes"] if x["id"]=="execute")["write_scope"]),route["route_id"],route["route_hash"],route["registry_digest"])
   self.assertEqual(node["id"],"execute")
   self.assertEqual(git["branch"],"guard-fixture")
   self.assertRaisesRegex(G.WorkerRouteError,"observed-outside-sealed-node",G.validate_route_contract,path,"execute",self.route_worktree,self.route_worktree,"autopilot-code","strong","reports/**")
 def test_launch_tuple_absent_fails_closed_at_launch(self):
  for case in ("absent",):
   with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
    route=self.route()
    if case=="absent":
     route.pop("launch_compatibility_tuple")
     expected="launch-compatibility-tuple-required"
    self.reseal(route); path=Path(td)/"route.json"; path.write_text(json.dumps(route))
    # Plain material/spec callers remain compatible with legacy route records.
    if case=="absent":
     # This no-phase read is only the legacy material/spec compatibility
     # check; launch refusal below must exercise the actual start boundary.
     _,node,_=G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree)
     self.assertEqual(node["id"],"execute")
    with self.assertRaises(G.WorkerRouteError) as ctx:
     # Keep incompatible and absent tuple cases on the same real launch path.
     G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree,launch_phase="start")
    self.assertEqual(ctx.exception.reason,expected)
 def test_non_dict_runtime_root_preserves_typed_refusal(self):
  for runtime in (7,[],None):
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
 def test_guard_accepts_subset_and_notation_of_sealed_scope(self):
  # N05: the sealed scope names the generic and the component spec path; a re-check that passes one
  # of them, or writes them with another separator, is the same approved work.
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route()
   node=next(x for x in route["nodes"] if x["id"]=="execute")
   node["write_scope"]=["spec/**","spec/<component>/**","checklist.md","reviews/audit-report.md","dev_logs/**"]
   self.reseal(route); path.write_text(json.dumps(route))
   def check(scope):
    return G.validate_route_contract(path,"execute",self.route_worktree,self.route_worktree,"autopilot-code","strong",scope)
   for scope in (";".join(node["write_scope"]),",".join(node["write_scope"]),"spec/<component>/**","spec/<component>/**,spec/**",
                 "spec/**; checklist.md","spec/prd/**","reviews/audit-report.md","reviews/audit-report.r2.md"):
    with self.subTest(scope=scope): check(scope)
   # Outside the sealed node, still refused; the detail names what was outside.
   for scope in ("source/**","spec/**;source/**","reports/other.md","reviews/audit-report.r2.txt","reviews/other.r2.md","plans/**"):
    with self.subTest(refused=scope), self.assertRaises(G.WorkerRouteError) as caught: check(scope)
    self.assertEqual(caught.exception.reason,"route-node-scope-mismatch")
    self.assertIn("observed-outside-sealed-node",str(caught.exception))
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
 def test_source_commit_descendant_head_passes_regardless_of_node_or_evidence(self):
  # SD-156: a first-parent descendant HEAD passes for any node, with no
  # registry row required -- the guard asks one lineage question
  # (`ROUTE.source_lineage_verdict`), not "is this node's position/evidence
  # exempt". A genuinely diverged HEAD is still refused
  # (`test_post_execute_diverged_head_rejected`/`test_mutation_retry_diverged_head_rejected`).
  with tempfile.TemporaryDirectory() as td:
   repo=Path(td)/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
   subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True)
   (repo/"x").write_text("a"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","a"],check=True)
   gate={"spec_read":{"satisfied":True,"source":"prd"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor"}}
   route=R.compile_route("autopilot-code","dev","strong",repo,repo,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(repo)); path=Path(td)/"route.json"; path.write_text(json.dumps(route))
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   _,node,_=G.validate_route_contract(path,"execute",repo,repo)
   self.assertEqual(node["id"],"execute")

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

 def test_pre_mutation_node_on_descendant_head_passes(self):
  # SD-65's exact-match requirement for pre-mutation nodes is retired by
  # SD-156: `plan` precedes `execute` (the first mutation node), but a
  # first-parent descendant HEAD passes for it exactly as it does downstream.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   _,node,_=G.validate_route_contract(path,"plan",repo,repo)
   self.assertEqual(node["id"],"plan")

 def test_execute_node_itself_on_descendant_head_passes(self):
  # SD-156: the first mutation node (execute) no longer requires HEAD ==
  # source_commit before it starts mutating -- a first-parent descendant is
  # the same mid-cycle-progress shape SD-107 already accepted for the
  # grounding tuple.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   _,node,_=G.validate_route_contract(path,"execute",repo,repo)
   self.assertEqual(node["id"],"execute")

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

 def test_c_continuation_with_no_lineage_rows_still_repins_and_every_node_passes(self):
  # SD-128's decline guard is retired by SD-156: a continuation always
  # re-pins to a descendant HEAD now, regardless of what the registry does or
  # does not prove about a prior attempt on a mutation node -- there is no
  # longer a registry-lineage question this builder must answer at all.
  with tempfile.TemporaryDirectory() as td:
   repo,source,_=self._lineage_repo(td,lineage_rows=False)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","fast-forward","-q"],check=True)
   head=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
   continuation=self._continuation(source,repo)
   self.assertEqual(continuation["source_commit"],head)
   self.assertEqual(continuation["source_commit_rebind"]["inherited_source_commit"],source["source_commit"])
   path=Path(td)/"continuation.json"; path.write_text(json.dumps(continuation))
   for node in continuation["nodes"]:
    _,validated,_=G.validate_route_contract(path,node["id"],repo,repo)
    self.assertEqual(validated["id"],node["id"])

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

 def test_mutation_first_launch_descendant_without_prior_attempt_passes(self):
  # A2, retired by SD-156: a descendant HEAD passes even with no prior
  # registry row -- there is no separate "first launch vs. retry" branch
  # left, only the lineage verdict.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   jobs=Path(td)/"jobs.log"; jobs.write_text("")
   with self._bound_jobs(str(jobs)):
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
   self.assertEqual(node["id"],"execute")

 def test_mutation_retry_passes_regardless_of_registry_availability(self):
  # A3b, retired by SD-156: the registry is no longer consulted for this gate
  # at all, so an unset, missing, relative, or malformed `AGENT_DISPATCH_JOBS`
  # binding no longer matters -- a descendant HEAD passes in every case.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   with self.subTest("env-unset"), self._bound_jobs(None):
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(node["id"],"execute")
   with self.subTest("missing-file"), self._bound_jobs(str(Path(td)/"absent.log")):
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(node["id"],"execute")
   with self.subTest("relative-path"), self._bound_jobs("relative/jobs.log"):
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(node["id"],"execute")
   malformed=Path(td)/"malformed.log"; malformed.write_text("not-six-fields\tonly-two\n")
   with self.subTest("malformed-rows"), self._bound_jobs(str(malformed)):
    _,node,_=G.validate_route_contract(path,"execute",repo,repo,current_attempt="att-current")
    self.assertEqual(node["id"],"execute")

 def test_mutation_retry_passes_with_only_the_current_attempts_own_row(self):
  # EX, retired by SD-156: a registry row -- even the current launch's own --
  # is no longer evidence this gate reads at all.
  with tempfile.TemporaryDirectory() as td:
   repo,route,path=self._lineage_repo(td)
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-am","b","-q"],check=True)
   jobs=Path(td)/"jobs.log"; jobs.write_text("")
   self._write_registry_row(jobs,route["route_id"],"execute","att-current")
   with self._bound_jobs(str(jobs)):
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
 """SD-133/SD-67's retry-evidence lookup is retired by SD-156 (A73).

 A continuation's mutation node used to need a registry attempt found through
 its lineage (`_qualifying_retry_evidence`) before a moved HEAD would pass.
 SD-156 deletes that lookup: the continuation builder re-pins to any
 descendant HEAD unconditionally (`_continuation_source_commit`), so the node
 validates on the continuation with no registry evidence of any kind -- this
 is A73-1's surviving assertion, kept because it is the one case the old
 lookup existed to serve. A73-2/3/4 (evidence-shaped refusals) and A73-5
 (multi-generation lineage walk) have no successor here: they asserted the
 deleted lookup's own behaviour, which `WorkerRouteGuardTest`'s
 `test_c_continuation_with_no_lineage_rows_still_repins_and_every_node_passes`
 and `test_c_continuation_after_fast_forward_dispatches_pre_mutation_nodes`
 already cover from the surviving side (no lineage rows, still passes).
 """

 def test_the_mutation_node_now_validates_on_the_continuation(self):
  with tempfile.TemporaryDirectory() as td:
   repo, source, _path = self._lineage_repo(td, lineage_rows=False)
   # execute ran once under the SOURCE route and its commit advanced HEAD --
   # no registry row is written for it here, unlike the pre-SD-156 fixture.
   (repo/"x").write_text("b")
   subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
   continuation = self._continuation(source, repo)
   self.assertNotEqual(continuation["route_id"], source["route_id"])
   self.assertIn("source_commit_rebind", continuation)
   path = Path(td)/"continuation.json"
   path.write_text(json.dumps(continuation))
   _route, node, _git = G.validate_route_contract(path, "execute", repo, repo)
   self.assertEqual(node["id"], "execute")


if __name__=="__main__": unittest.main()
