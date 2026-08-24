#!/usr/bin/env python3
import contextlib, importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
R=load("route",ROOT/"utilities/capability-route.py"); G=load("guard",ROOT/"utilities/worker-route-guard.py")
ALL=["atomic-outcome","known-scope","no-shared-contract","no-resource-run","no-artifact-handoff","no-independent-verifier","focused-verification"]

def dispatch(worktree):
 return {"tuples":[{"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":"codex","launch_authority":"conductor","status":"supported","probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(Path(worktree).resolve()),"failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0}],"native_subagent":[]}

class WorkerRouteGuardTest(unittest.TestCase):
 def route(self):
  gate={"spec_read":{"satisfied":True,"source":"prd-sha256"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor"}}
  return R.compile_route("autopilot-code","dev","strong",ROOT,ROOT,predicates=ALL,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(ROOT))

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
     _,node,_=G.validate_route_contract(path,"execute",ROOT,ROOT)
     self.assertEqual(node["id"],"execute")
    with self.assertRaises(G.WorkerRouteError) as ctx:
     G.validate_route_contract(path,"execute",ROOT,ROOT,launch_phase="start")
    self.assertEqual(ctx.exception.reason,expected)
 def test_valid_and_scope_bound(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); path.write_text(json.dumps(route))
   _,node,_=G.validate_route_contract(path,"execute",ROOT,ROOT,"autopilot-code","strong",";".join(next(x for x in route["nodes"] if x["id"]=="execute")["write_scope"]),route["route_id"],route["route_hash"],route["registry_digest"])
   self.assertEqual(node["id"],"execute")
   self.assertRaisesRegex(G.WorkerRouteError,"expected=",G.validate_route_contract,path,"execute",ROOT,ROOT,"autopilot-code","strong","spec/**")
 def test_hash_and_reselection_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); route["cwd"]="/tmp"; path.write_text(json.dumps(route))
   self.assertRaisesRegex(G.WorkerRouteError,"stale or modified",G.validate_route_contract,path,"execute",ROOT,ROOT)
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"route.json"; route=self.route(); path.write_text(json.dumps(route))
   self.assertRaisesRegex(G.WorkerRouteError,"expected=autopilot-code",G.validate_route_contract,path,"execute",ROOT,ROOT,"code-execute")
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

 def _lineage_repo(self,td):
  repo=Path(td)/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True)
  (repo/"x").write_text("a"); subprocess.run(["git","-C",str(repo),"add","x"],check=True); subprocess.run(["git","-C",str(repo),"commit","-qm","a"],check=True)
  gate={"spec_read":{"satisfied":True,"source":"prd"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor"}}
  route=R.compile_route("autopilot-code","dev","strong",repo,repo,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(repo))
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

if __name__=="__main__": unittest.main()
