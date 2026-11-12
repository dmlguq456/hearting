#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from split_module_fixture import patch_split

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
R=load("route",ROOT/"utilities/capability-route.py")
TX=load("spec_transaction",ROOT/"utilities/spec-transaction.py")
# Owner finding D1 (2026-09-07): `spec-transaction.py` resolves its spec bucket from
# AGENT_ARTIFACT_* (not from --artifact-root), so a child inheriting a dispatch
# environment wrote a LIVE cycle bucket. Every child gets a scrubbed environment.
HERMETIC_ENV={k:v for k,v in os.environ.items() if not k.startswith((
 "AGENT_ARTIFACT_","AGENT_ROUTE_","AGENT_REVIEW_","AGENT_DISPATCH_"))}

def dispatch(worktree):
 return {"tuples":[{"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":"codex","launch_authority":"conductor","status":"supported","probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(Path(worktree).resolve()),"failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0}],"native_subagent":[]}

class SpecTransactionTest(unittest.TestCase):
 def fixture(self, root: Path, *, component=""):
  artifact=root/".agent_reports"; spec=artifact/"spec"/component; spec.mkdir(parents=True)
  subprocess.run(["git","init","-q",str(root)],check=True)
  subprocess.run(["git","-C",str(root),"config","user.email","fixture@example.com"],check=True)
  subprocess.run(["git","-C",str(root),"config","user.name","Fixture"],check=True)
  (root/"README").write_text("x\n"); subprocess.run(["git","-C",str(root),"add","README"],check=True); subprocess.run(["git","-C",str(root),"commit","-qm","init"],check=True)
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  route=R.compile_route("autopilot-spec","update","strong",root,artifact,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(root))
  route_path=root/"route.json"; route_path.write_text(json.dumps(route))
  return artifact,spec,route_path

 def command(self, root, artifact, route, code, *, spec_root=None, events=None):
  command=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(artifact),"--worktree",str(root),"--route",str(route),"--node","prd-transaction"]
  if spec_root is not None: command.extend(["--spec-root",str(spec_root)])
  if events is not None: command.extend(["--events",str(events)])
  return command+["--",sys.executable,"-c",code]

 def test_blocked_wait_rereads_and_snapshots_each_exact_preimage(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("v0\n"); events=root/"events.jsonl"
   release=root/"release-first"
   code=("import os,time; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')\n"
         "release=os.environ.get('RELEASE_FILE'); deadline=time.monotonic()+10\n"
         "while release and not Path(release).exists():\n"
         " if time.monotonic()>=deadline: raise TimeoutError('test release was not signalled')\n"
         " time.sleep(.01)\n")
   base=self.command(root,artifact,route,code,events=events)
   def wait_event(status):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
     if events.exists() and f'"status": "{status}"' in events.read_text(): return True
     time.sleep(.02)
    return False
   children=[]
   try:
    children.append(subprocess.Popen(base,env={**HERMETIC_ENV,"RELEASE_FILE":str(release)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
    self.assertTrue(wait_event("acquired"),"first transaction did not acquire the lock")
    children.append(subprocess.Popen(base,env=HERMETIC_ENV,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
    self.assertTrue(wait_event("BLOCKED"),"second transaction did not observe contention")
   finally:
    release.touch()
    results=[child.communicate(timeout=15) for child in children]
   for child,(out,err) in zip(children,results): self.assertEqual(child.returncode,0,out+err)
   rows=[json.loads(line) for line in events.read_text().splitlines()]
   self.assertTrue(any(row["status"]=="BLOCKED" for row in rows))
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"v0\n")
   self.assertEqual((spec/"_internal/versions/v2/prd.md").read_text(),"v1\n")
   self.assertEqual((spec/"prd.md").read_text(),"v2\n")

 def test_unchanged_and_new_prd_do_not_create_snapshot(self):
  for existing,code in ((True,"pass"),(False,"from pathlib import Path; import os; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('new\\n')")):
   with self.subTest(existing=existing), tempfile.TemporaryDirectory() as td:
    root=Path(td); artifact,spec,route=self.fixture(root)
    if existing: (spec/"prd.md").write_text("same\n")
    result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    self.assertFalse((spec/"_internal/versions").exists())

 def test_empty_version_directory_cannot_bypass_snapshot_file(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; root=Path(os.environ['AGENT_SPEC_ROOT']); (root/'_internal/versions'/('v'+os.environ['AGENT_SPEC_NEXT_VERSION'])).mkdir(parents=True,exist_ok=True); (root/'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"before\n")

 def test_mismatched_manual_snapshot_fails_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; root=Path(os.environ['AGENT_SPEC_ROOT']); snap=root/'_internal/versions'/('v'+os.environ['AGENT_SPEC_NEXT_VERSION']); snap.mkdir(parents=True,exist_ok=True); (snap/'prd.md').write_text('wrong\\n'); (root/'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,65,result.stdout+result.stderr)
   self.assertIn("version-snapshot-mismatch",result.stdout)

 def test_failed_command_still_snapshots_changed_preimage(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os,sys; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('partial\\n'); sys.exit(7)"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,7,result.stdout+result.stderr)
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"before\n")

 def test_code_spec_touch_is_bound_to_execute_scope(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,_=self.fixture(root)
   gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
   route=R.compile_route("autopilot-code","dev","standard",root,artifact,predicates=[],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(root))
   execute=next(n for n in route["nodes"] if n["id"]=="execute")
   plan=next(n for n in route["nodes"] if n["id"]=="plan")
   self.assertTrue(route["spec_touch"])
   self.assertIn("spec/**",execute["write_scope"])
   self.assertNotIn("spec/**",plan["write_scope"])
   path=root/"code-route.json"; path.write_text(json.dumps(route))
   result=subprocess.run([sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(artifact),"--worktree",str(root),"--route",str(path),"--node","plan","--",sys.executable,"-c","pass"],text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,65,result.stdout+result.stderr)
   self.assertIn("route-node-scope-mismatch",result.stdout)

  # -- the v{N} chain is canonical, not per-tree ------------------------------

 def test_next_version_continues_the_chain_across_legacy_and_shared(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; cycle_spec=artifact/"campaigns/c/cycles/y/artifacts/spec"; cycle_spec.mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),1)
   (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),169,"an empty cycle bucket must not restart at v1 while legacy holds the chain")
   (artifact/"shared/spec/ref_x/revisions/rrev_x/_internal/versions/v200").mkdir(parents=True)
   (artifact/"shared/spec/ref_y/revisions/rrev_y/_internal/versions/v201").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),202,"every shared reference and revision carries history")
   (cycle_spec/"_internal/versions/v300").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),301)
   (artifact/"spec/_internal/versions/v9_prd.md").mkdir(parents=True)   # not a v{N} directory name
   (artifact/"spec/_internal/versions/v400").write_text("file, not a version dir\n")
   self.assertEqual(TX.next_version(cycle_spec,artifact),301)

 def test_next_version_keeps_a_component_on_its_own_chain(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; cycle_spec=artifact/"campaigns/c/cycles/y/artifacts/spec"; (cycle_spec/"comp").mkdir(parents=True)
   (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
   (artifact/"spec/comp/_internal/versions/v5").mkdir(parents=True)
   (artifact/"shared/spec/ref_x/revisions/rrev_x/comp/_internal/versions/v7").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec/"comp",artifact,"comp"),8)
   self.assertEqual(TX.next_version(cycle_spec,artifact),169)
   trees=TX.version_history_trees(cycle_spec/"comp",artifact,"comp")
   self.assertTrue(all(t.name=="comp" for t in trees),trees)

 def test_version_history_trees_dedupes_the_legacy_root(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; legacy=artifact/"spec"; (legacy/"_internal/versions/v3").mkdir(parents=True)
   trees=TX.version_history_trees(legacy,artifact)
   self.assertEqual([t.resolve() for t in trees],[legacy.resolve()])
   self.assertEqual(TX.next_version(legacy,artifact),4)

 def test_chain_counts_residue_home_symlinked_bucket_and_legacy_cycles_layer(self):
  # Review round 1 (major 2, 3): trees the reader's enumeration cannot see still hold real v{N}.
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; open_bucket=artifact/"campaigns/2026-09-07_o/2026-09-07_o/artifacts/spec"; open_bucket.mkdir(parents=True)
   self.assertEqual(TX.next_version(open_bucket,artifact),1)
   # W7H --include-spec-top parked the legacy chain at artifacts/_internal/spec (no top-level spec/ left)
   (artifact/"campaigns/2026-09-05_support-residue/2026-06-16_support-residue-2/artifacts/_internal/spec/_internal/versions/v170").mkdir(parents=True)
   self.assertEqual(TX.next_version(open_bucket,artifact),171,"the residue home of a retired legacy spec/ is on the chain")
   # a sealed cycle whose bucket was moved and replaced by a symlink
   moved=Path(td)/"moved-bucket"; (moved/"_internal/versions/v180").mkdir(parents=True)
   link=artifact/"campaigns/2026-09-06_a/2026-09-06_a/artifacts/spec"; link.parent.mkdir(parents=True); link.symlink_to(moved)
   self.assertEqual(TX.next_version(open_bucket,artifact),181,"a symlinked bucket is followed")
   # a symlinked cycle directory
   moved_cycle=Path(td)/"moved-cycle"; (moved_cycle/"artifacts/spec/_internal/versions/v190").mkdir(parents=True)
   (artifact/"campaigns/2026-09-06_b").mkdir(); (artifact/"campaigns/2026-09-06_b/2026-09-06_b").symlink_to(moved_cycle)
   self.assertEqual(TX.next_version(open_bucket,artifact),191,"a symlinked cycle directory is followed")
   # the pre-W7I cycles/ layer
   (artifact/"campaigns/2026-08-01_old/cycles/cyc_1/artifacts/spec/_internal/versions/v200").mkdir(parents=True)
   self.assertEqual(TX.next_version(open_bucket,artifact),201,"the legacy cycles/ layer is on the chain")
   version,source=TX.chain_next(TX.version_history_trees(open_bucket,artifact))
   self.assertEqual((version,TX.version_source_layout(source,open_bucket,artifact)),(201,"cycle"))

 @unittest.skipIf(os.geteuid()==0,"permission bits do not bind root")
 def test_unwalkable_root_raises_a_typed_chain_error(self):
  # Review round 1 (blocking 1): the chain must fail closed, never count low.
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; open_bucket=artifact/"campaigns/2026-09-07_o/2026-09-07_o/artifacts/spec"; open_bucket.mkdir(parents=True)
   sealed=artifact/"campaigns/2026-09-06_a"; (sealed/"2026-09-06_a/artifacts/spec/_internal/versions/v9").mkdir(parents=True)
   os.chmod(sealed,0)
   try:
    with self.assertRaises(TX.VersionChainError) as ctx: TX.next_version(open_bucket,artifact)
    self.assertEqual(ctx.exception.reason,"version-chain-unenumerable")
   finally: os.chmod(sealed,0o755)
   self.assertEqual(TX.next_version(open_bucket,artifact),10)
   # owner finding F1: an unreadable `_internal/versions` of a walkable tree is typed as well
   versions=sealed/"2026-09-06_a/artifacts/spec/_internal/versions"; os.chmod(versions,0)
   try:
    with self.assertRaises(TX.VersionChainError): TX.next_version(open_bucket,artifact)
   finally: os.chmod(versions,0o755)

 @unittest.skipIf(os.geteuid()==0,"permission bits do not bind root")
 def test_unwalkable_root_is_a_typed_refusal(self):
  # Review round 1 (blocking 1, repro A): on a legacy-layout root nothing runs before the
  # transaction, so an unwalkable campaign dir used to surface as a bare traceback (exit 1)
  # inside the lock. In the cycle layout `check_write` refuses first (`cycle-unknown`) except
  # inside the lock-wait window, which this same path covers.
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   broken=artifact/"campaigns/2026-01-01_broken"; (broken/"2026-01-01_broken/artifacts/spec").mkdir(parents=True); os.chmod(broken,0)
   events=root/"ev.jsonl"
   try: result=subprocess.run(self.command(root,artifact,route,"pass",events=events),text=True,capture_output=True,env=HERMETIC_ENV)
   finally: os.chmod(broken,0o755)
   self.assertEqual(result.returncode,65,result.stdout+result.stderr)
   rows=[json.loads(l) for l in events.read_text().splitlines()]
   self.assertEqual([(r["status"],r["reason"]) for r in rows if r["status"]=="blocked"],[("blocked","version-chain-unenumerable")])
   self.assertIn("PermissionError",[r for r in rows if r["status"]=="blocked"][0]["detail"])
   self.assertFalse(any(r["status"] in ("acquired","released") for r in rows),rows)
   self.assertFalse((spec/"_internal").exists(),"no snapshot on a refused run")
   self.assertEqual((artifact/".pipeline-lock").read_text(),"","the owner line is cleared")
   self.assertEqual((spec/"prd.md").read_text(),"before\n")

 def test_component_spec_root_owns_its_version_sequence(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,component,route=self.fixture(root,component="component"); (component/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code,spec_root=component),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual((component/"_internal/versions/v1/prd.md").read_text(),"before\n")
   self.assertFalse((artifact/"spec/_internal/versions/v1").exists())

 BEGIN="<!-- BLUEPRINT-SUMMARY:BEGIN -->"; END="<!-- BLUEPRINT-SUMMARY:END -->"
 def prd_text(self, *body):
  return "\n".join(["# Title","",self.BEGIN,"- one item",self.END,"","## 1. Common",*body])+"\n"

 def write_code(self, text, *, exit_code=None):
  code=f"import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text({text!r},encoding='utf-8')"
  return code+(f"; import sys; sys.exit({exit_code})" if exit_code is not None else "")

 def run_readability(self, before, code):
  """Run one transaction; returns (result, receipt rows, spec dir, tmp) with the tmp dir kept alive by the caller's context."""
  td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
  root=Path(td.name); artifact,spec,route=self.fixture(root); events=root/"events.jsonl"
  if before is not None: (spec/"prd.md").write_text(before,encoding="utf-8")
  result=subprocess.run(self.command(root,artifact,route,code,events=events),text=True,capture_output=True,env=HERMETIC_ENV)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  return result,rows,spec

 def test_i1_clean_new_prd_records_an_empty_readability_receipt(self):
  result,rows,_spec=self.run_readability(None,self.write_code(self.prd_text("Plain text.")))
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  found=[r for r in rows if r["status"]=="readability"]
  self.assertEqual(len(found),1)
  self.assertEqual(found[0]["counts"]["total"],0); self.assertEqual(found[0]["base"],"none")
  self.assertEqual(found[0]["schema"],"prd-readability/1"); self.assertNotIn("error",found[0])
  self.assertNotIn("prd-readability",result.stderr)
  self.assertIn('"status": "readability"',result.stdout)

 def test_i2_new_lines_are_counted_apart_and_the_write_still_succeeds(self):
  before=self.prd_text("old violation \u2460 here")
  after=before+f"new violation \u2461 here\nroute rt-b890afc55528891c\n"
  result,rows,spec=self.run_readability(before,self.write_code(after))
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  event=next(r for r in rows if r["status"]=="readability")
  self.assertEqual(event["base"],"preimage")
  self.assertEqual((event["counts"]["new"],event["counts"]["existing"],event["counts"]["total"]),(2,1,3))
  self.assertEqual(event["counts"]["by_rule"],{"circled-char":2,"route-id":1})
  first=before.splitlines().index("old violation \u2460 here")+1
  self.assertEqual([(v["line"],v["age"],v["rule"]) for v in event["violations"]],
                   [(first+1,"new","circled-char"),(first+2,"new","route-id"),(first,"existing","circled-char")])
  notice=[l for l in result.stderr.splitlines() if l.startswith("prd-readability:")]
  self.assertEqual(len(notice),1); self.assertIn("3 warnings (new 2, existing 1)",notice[0])
  self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(encoding="utf-8"),before)
  self.assertEqual((spec/"prd.md").read_text(encoding="utf-8"),after)
  self.assertIn("snapshot",[r["status"] for r in rows])

 def test_i3_fenced_code_is_not_flagged(self):
  fence="`"*3
  noisy=f"route rt-b890afc55528891c commit 1600e12 \u2460"
  result,rows,_spec=self.run_readability(None,self.write_code(self.prd_text(fence,noisy,fence)))
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertEqual(next(r for r in rows if r["status"]=="readability")["counts"]["total"],0)
  self.assertNotIn("prd-readability",result.stderr)

 def test_i4_unchanged_prd_emits_no_readability_event(self):
  before=self.prd_text("old violation \u2460 here")
  result,rows,_spec=self.run_readability(before,"pass")
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertNotIn("readability",[r["status"] for r in rows])
  self.assertNotIn("prd-readability",result.stderr)

 def test_i5_child_exit_code_is_kept_when_the_prd_changed(self):
  result,rows,_spec=self.run_readability(None,self.write_code(self.prd_text("bad \u2460"),exit_code=7))
  self.assertEqual(result.returncode,7,result.stdout+result.stderr)
  self.assertEqual(next(r for r in rows if r["status"]=="readability")["counts"]["total"],1)
  self.assertEqual(rows[-1]["status"],"released"); self.assertEqual(rows[-1]["result"],7)

 def test_i6_checker_failure_becomes_an_error_field_not_an_exception(self):
  with tempfile.TemporaryDirectory() as td:
   broken=Path(td)/"broken.py"; broken.write_text("raise RuntimeError('boom')\n")
   original=TX.READABILITY_PATH
   try:
    for path in (Path(td)/"missing.py",broken):
     with self.subTest(path=path.name):
      TX.READABILITY_PATH=path
      event,notice=TX.readability_event(Path("prd.md"),None,b"# T\n",  "rt-x",3)
      self.assertEqual((event["status"],event["route_id"],event["version"]),("readability","rt-x",3))
      self.assertIn("error",event); self.assertNotIn("counts",event); self.assertLessEqual(len(event["error"]),200)
      self.assertTrue(notice.startswith("prd-readability: check skipped ("))
      json.dumps(event)
   finally:
    TX.READABILITY_PATH=original
  event,notice=TX.readability_event(Path("prd.md"),None,self.prd_text("ok").encode(),"rt-x",1)
  self.assertEqual(event["counts"]["total"],0); self.assertIsNone(notice)

 def test_i7_readability_precedes_released_which_stays_last(self):
  result,rows,_spec=self.run_readability(self.prd_text("a"),self.write_code(self.prd_text("b \u2460")))
  statuses=[r["status"] for r in rows]
  self.assertEqual(statuses[-2:],["readability","released"])
  self.assertEqual(statuses.count("released"),1)

class CycleLayoutTest(unittest.TestCase):
 """W7C cycle layout (defect K): the transaction seeds the empty cycle spec
 root from the latest shared revision so the snapshot comes from the tool,
 and a child that writes the legacy bucket is a typed failure."""

 def setUp(self):
  sys.path.insert(0,str(ROOT/"utilities"))
  import artifact_producer as P, artifact_lifecycle as L
  self.P,self.L=P,L
  self.PT=load("producer_test_for_spec_tx",ROOT/"utilities/artifact_producer.test.py")
  self._tmp=tempfile.TemporaryDirectory(); base=Path(self._tmp.name)
  self.repo=base/"repo"; self.repo.mkdir()
  subprocess.run(["git","init","-q",str(self.repo)],check=True)
  subprocess.run(["git","-C",str(self.repo),"config","user.email","f@x"],check=True)
  subprocess.run(["git","-C",str(self.repo),"config","user.name","F"],check=True)
  (self.repo/"README").write_text("x\n"); subprocess.run(["git","-C",str(self.repo),"add","README"],check=True); subprocess.run(["git","-C",str(self.repo),"commit","-qm","init"],check=True)
  self.artifact=self.repo/".agent_reports"; self.artifact.mkdir()
  home=base/"agent-home"; (home/"core").mkdir(parents=True); (home/"core"/"CORE.md").write_text("fixture\n")
  self._env={k:v for k,v in os.environ.items() if k=="AGENT_HOME" or k.startswith(("AGENT_DISPATCH_","AGENT_ROUTE_","AGENT_REVIEW_","AGENT_ARTIFACT_"))}
  os.environ["AGENT_HOME"]=str(home)
  for k in tuple(os.environ):
   if k.startswith(("AGENT_DISPATCH_","AGENT_ROUTE_","AGENT_REVIEW_","AGENT_ARTIFACT_")): os.environ.pop(k,None)
  P.activate(self.artifact,repository_id="repo_"+"a"*32,artifact_root_id="root_"+"b"*32,w7={"campaign_id":"camp_"+"c"*32})
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  spec_route=R.compile_route("autopilot-spec","update","strong",self.repo,self.artifact,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(self.repo),slug="spec-tx")
  self.spec_route=self.repo/"route.json"; self.spec_route.write_text(json.dumps(spec_route))

 def tearDown(self):
  for k,v in self._env.items():
   if v is None: os.environ.pop(k,None)
   else: os.environ[k]=v
  self._tmp.cleanup()

 def _cycle(self, slug):
  route=self.PT.compile_for("direct",self.artifact,"autopilot-code","dev",slug=slug)
  binding=self.L.admit_runtime_route(self.artifact,route)
  begun=self.P.begin(self.artifact,route_file=Path(binding.route_file),capability="autopilot-code",intensity="direct")
  return route,Path(binding.route_file),begun

 def _close(self, route, route_file):
  evidence=Path(self._tmp.name)/f"ev-{route['route_id']}.txt"; evidence.write_text("ok\n")
  for node in route["nodes"]:
   if node.get("terminal") is True: R.write_completion_marker(route,node,node["id"],evidence)
  R.close_route(route,route_file,commit="a"*40,summary="fixture")

 def _shared_v1(self):
  route,route_file,begun=self._cycle("seed-source")
  spec=Path(begun["cycle_dir"])/"artifacts"/"spec"; (spec/"_internal"/"versions"/"v1").mkdir(parents=True)
  (spec/"prd.md").write_text("v1\n"); (spec/"_internal"/"versions"/"v1"/"prd.md").write_text("v0\n"); (spec/"pipeline_state.yaml").write_text("s\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  return self.P.admit_shared(self.artifact,cycle_id=begun["cycle_id"],kind="spec",source="spec",key="spec")

 def _shared_components(self):
  r,f,b=self._cycle("component-base")
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  for rel,body in {"a/prd.md":"# A\n\n## Rule\nbefore\n", "a/extra.md":"keep\n",
                   "a/_internal/versions/v3/prd.md":"older a\n",
                   "b/prd.md":"# B\nbefore\n", "b/_internal/versions/v9/prd.md":"older b\n"}.items():
   target=spec/rel; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(body)
  self._close(r,f); self.P.finalize(self.artifact,cycle_id=b["cycle_id"])
  return self.P.admit_shared(self.artifact,cycle_id=b["cycle_id"],kind="spec",source="spec",key="spec")

 def _publish_components(self,r,f,b):
  self._close(r,f); self.P.finalize(self.artifact,cycle_id=b["cycle_id"])
  return self.P.admit_shared(self.artifact,cycle_id=b["cycle_id"],kind="spec",source="spec",key="spec")

 def test_normal_direct_component_transaction_snapshots_and_admits_its_exact_base(self):
  initial=self._shared_components()
  r=R.compile_route("autopilot-spec","update","direct",self.repo,self.artifact,
                    predicates=self.PT.ALL,transport=None,inline_reason="atomic-direct",
                    tracking="tracked",tracked_gate_evidence=self.PT.gate_evidence(),slug="component-input")
  binding=self.L.admit_runtime_route(self.artifact,r); f=Path(binding.route_file)
  b=self.P.begin(self.artifact,route_file=f,capability="autopilot-spec",intensity="direct")
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  self.assertFalse((spec/"a").exists()); self.assertFalse((spec/"b").exists())
  self.assertFalse((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).exists())
  env={**HERMETIC_ENV,"AGENT_ARTIFACT_CYCLE_DIR":b["cycle_dir"],"AGENT_ARTIFACT_ROOT":str(self.artifact)}
  command=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(self.artifact),
           "--worktree",str(self.repo),"--route",str(f),"--node","inline","--spec-root","a","--",
           sys.executable,"-c","import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('# A\\n\\n## Rule\\nafter\\n')"]
  result=subprocess.run(command,env=env,text=True,capture_output=True)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertFalse((spec/"b").exists())
  self.assertEqual((spec/"a/_internal/versions/v4/prd.md").read_text(),"# A\n\n## Rule\nbefore\n")
  self.assertEqual((spec/"a/_internal/versions/v3/prd.md").read_text(),"older a\n")
  before=self.P._spec_bytes(spec)
  published=self._publish_components(r,f,b)
  output=self.P._verified_shared_spec(self.artifact,published["shared_reference_id"],published["shared_reference_revision_id"])[0]
  self.assertEqual(output["b/prd.md"],b"# B\nbefore\n")
  self.assertIn(b"after\n",output["a/prd.md"])
  self.assertEqual(self.P._spec_bytes(spec),before)
  self.assertEqual(published["spec_merge"]["source_files"],self.P._spec_inventory(before))
  retry=self.P.admit_shared(self.artifact,cycle_id=b["cycle_id"],kind="spec",source="spec",key="spec")
  self.assertEqual(retry["status"],"reused")
  self.assertEqual(retry["shared_reference_revision_id"],published["shared_reference_revision_id"])

 def test_initial_scoped_publication_admits_only_its_selected_component(self):
  r,f,b=self._cycle("component-initial"); spec=Path(b["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  (spec/"a").mkdir(); (spec/"a/prd.md").write_text("# A\nfirst\n")
  before=self.P._spec_bytes(spec)
  published=self._publish_components(r,f,b)
  output=self.P._verified_shared_spec(self.artifact,published["shared_reference_id"],published["shared_reference_revision_id"])[0]
  self.assertEqual(output["a/prd.md"],b"# A\nfirst\n")
  self.assertFalse(any(p.startswith("b/") for p in output))
  self.assertEqual(self.P._spec_bytes(spec),before)
  retry=self.P.admit_shared(self.artifact,cycle_id=b["cycle_id"],kind="spec",source="spec",key="spec")
  self.assertEqual(retry["shared_reference_revision_id"],published["shared_reference_revision_id"])

 def test_initial_scoped_publication_refuses_foreign_payload_before_publication(self):
  r,f,b=self._cycle("component-initial-foreign"); spec=Path(b["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  for component in ("a","b"):
   target=spec/component/"prd.md"; target.parent.mkdir(parents=True); target.write_text("# "+component+"\n")
  with self.assertRaises(self.P.ProducerError) as caught:self._publish_components(r,f,b)
  self.assertEqual(caught.exception.code,"source-manifest-mismatch")
  self.assertIsNone(self.P.find_reference_by_key(self.artifact,"spec","spec"))
  self.assertEqual(list(self.P.shared_journal_path(self.artifact,"probe").parent.glob("*.json")),[])

 def test_scoped_post_publish_interruption_recovers_exact_output_without_mutating_source(self):
  from unittest import mock
  initial=self._shared_components()
  r,f,b=self._cycle("component-publish-interruption"); spec=Path(b["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  (spec/"a/prd.md").write_text("# A\n\n## Rule\nours\n")
  l,lf,lb=self._cycle("component-publish-latest"); latest_spec=Path(lb["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(latest_spec,self.artifact,spec_root=latest_spec/"b")
  (latest_spec/"b/prd.md").write_text("# B\nlatest\n")
  winner=self._publish_components(l,lf,lb)
  before=self.P._spec_bytes(spec)
  with patch_split(self.P,"_commit_shared",side_effect=RuntimeError("post-publish interruption")):
   with self.assertRaises(RuntimeError):self._publish_components(r,f,b)
  record=self.P.read_cycle_record(self.artifact,b["cycle_id"])
  manifest=self.P._record_cycle_manifest_path(self.artifact,record); manifest_before=manifest.read_bytes()
  journal_path=next(self.P.shared_journal_path(self.artifact,"probe").parent.glob("*.json"))
  journal=json.loads(journal_path.read_text()); output=self.artifact/journal["target"]
  original=(output/"a/prd.md").read_bytes()
  self.assertEqual(self.P._verified_shared_spec(self.artifact,journal["reference_id"],journal["revision_id"])[0]["b/prd.md"],b"# B\nlatest\n")
  (output/"a/prd.md").write_bytes(b"corruption")
  self.assertTrue(self.P._recover_locked(self.artifact)["unresolved"])
  reference=self.P.find_reference_by_key(self.artifact,"spec","spec")
  self.assertEqual(reference["latest_revision_id"],winner["shared_reference_revision_id"])
  (output/"a/prd.md").write_bytes(original)
  self.assertEqual(self.P._recover_locked(self.artifact)["unresolved"],[])
  self.assertFalse(journal_path.exists())
  retry=self.P.admit_shared(self.artifact,cycle_id=b["cycle_id"],kind="spec",source="spec",key="spec")
  self.assertEqual(retry["shared_reference_revision_id"],journal["revision_id"])
  self.assertEqual(self.P._spec_bytes(spec),before)
  self.assertEqual(manifest.read_bytes(),manifest_before)

 def test_scoped_candidate_merges_latest_other_component_and_rejects_overlap(self):
  initial=self._shared_components()
  a,af,ab=self._cycle("component-a"); ap=Path(ab["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(ap,self.artifact,spec_root=ap/"a")
  (ap/"a/prd.md").write_text("# A\n\n## Rule\nours\n")
  l,lf,lb=self._cycle("component-latest"); lp=Path(lb["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(lp,self.artifact,spec_root=lp/"b"); (lp/"b/prd.md").write_text("# B\nlatest\n")
  self._publish_components(l,lf,lb)
  merged=self._publish_components(a,af,ab)
  output=self.P._verified_shared_spec(self.artifact,merged["shared_reference_id"],merged["shared_reference_revision_id"])[0]
  self.assertEqual(output["b/prd.md"],b"# B\nlatest\n")
  self.assertIn(b"ours\n",output["a/prd.md"])
  x,xf,xb=self._cycle("component-conflict"); xp=Path(xb["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(xp,self.artifact,spec_root=xp/"a")
  (xp/"a/prd.md").write_text("# A\n\n## Rule\nX\n")
  y,yf,yb=self._cycle("component-winner"); yp=Path(yb["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(yp,self.artifact,spec_root=yp/"a"); (yp/"a/prd.md").write_text("# A\n\n## Rule\nY\n")
  self._publish_components(y,yf,yb)
  with self.assertRaises(self.P.ProducerError) as caught:self._publish_components(x,xf,xb)
  self.assertEqual(caught.exception.code,"shared-spec-conflict")

 def test_component_seed_retry_and_scope_extension_preserve_edits_and_deletions(self):
  self._shared_components(); r,f,b=self._cycle("component-retry")
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  (spec/"a/prd.md").write_text("edited\n"); (spec/"a/extra.md").unlink()
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"b")
  self.assertEqual((spec/"a/prd.md").read_text(),"edited\n")
  self.assertFalse((spec/"a/extra.md").exists())
  self.assertEqual((spec/"b/prd.md").read_text(),"# B\nbefore\n")
  receipt=json.loads((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())
  self.assertEqual(receipt["components"],["a","b"])
  self.assertEqual(receipt["component_seeds"],{"a":True,"b":True})
  TX.seed_cycle_spec(spec,self.artifact)
  self.assertIsNone(json.loads((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())["components"])
  self.assertFalse((spec/"a/extra.md").exists())
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertIsNone(json.loads((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())["components"])

 def test_narrow_owner_seed_uses_declared_scope_before_any_transaction(self):
  self.assertIsNone(self.P.spec_scope_components(["spec/<component>/prd.md"]))
  self._shared_components(); r,f,b=self._cycle("component-preseed")
  TX.preseed_owner_cycle(self.artifact,Path(b["cycle_dir"]),route={"nodes":[{"write_scope":["spec/a/**"]}]})
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  self.assertTrue((spec/"a/prd.md").is_file()); self.assertFalse((spec/"b").exists())

 def test_component_seed_interruption_retries_original_base_without_restoring_completed_component(self):
  from unittest.mock import patch
  self._shared_components(); r,f,b=self._cycle("component-interrupted")
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  original=TX.PRODUCER._write_atomic; calls=[]
  def interrupted(path,data):
   if path.is_relative_to(spec/"a"):
    calls.append(str(path))
    if len(calls)==2:raise OSError("seed interrupted")
   return original(path,data)
  with patch_split(TX.PRODUCER,"_write_atomic",interrupted):
   with self.assertRaises(OSError):TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertFalse(json.loads((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())["seed_complete"])
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertTrue((spec/"a/prd.md").exists()); self.assertFalse((spec/"b").exists())

 def test_partial_component_seed_write_retries_exact_base_and_history_after_latest_moves(self):
  from unittest import mock
  initial=self._shared_components(); r,f,b=self._cycle("component-partial-write")
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  base_dir=Path(initial["revision_dir"]); base_before=self.P._spec_bytes(base_dir,revision=True)
  original_open,original_fdopen=os.open,os.fdopen; opened={}; partial=[]
  def track_open(path,*args,**kwargs):
   fd=original_open(path,*args,**kwargs); opened[fd]=Path(path); return fd
  class PartialWriter:
   def __init__(self,handle,path):self.handle,self.path=handle,path
   def __enter__(self):self.handle.__enter__(); return self
   def __exit__(self,*args):return self.handle.__exit__(*args)
   def __getattr__(self,name):return getattr(self.handle,name)
   def write(self,data):
    self.handle.write(data[:4]); self.handle.flush()
    partial.append(self.path.read_bytes())
    raise OSError("after four actual bytes")
  def partial_fdopen(fd,*args,**kwargs):
   handle=original_fdopen(fd,*args,**kwargs); path=opened[fd]
   if path.parent==spec/"a" and path.name.startswith(".prd.md.tmp-"):
    return PartialWriter(handle,path)
   return handle
  with mock.patch.object(os,"open",track_open),mock.patch.object(os,"fdopen",partial_fdopen), \
       mock.patch.object(os,"supports_dir_fd",os.supports_dir_fd|{track_open}
                         if original_open in os.supports_dir_fd else os.supports_dir_fd):
   with self.assertRaises(OSError):TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertEqual(partial,[base_before["a/prd.md"][:4]])
  self.assertFalse((spec/"a/prd.md").exists())
  self.assertEqual(list((spec/"a").glob(".prd.md.tmp-*")),[])
  receipt=json.loads((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())
  self.assertFalse(receipt["seed_complete"]); self.assertEqual(receipt["component_seeds"],{"a":False})
  self.assertEqual(receipt["revision_id"],initial["shared_reference_revision_id"])
  l,lf,lb=self._cycle("component-partial-write-latest"); latest=Path(lb["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(latest,self.artifact,spec_root=latest/"b")
  (latest/"b/prd.md").write_text("# B\nlatest\n")
  self._publish_components(l,lf,lb)
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  receipt=json.loads((spec/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())
  self.assertEqual(receipt["revision_id"],initial["shared_reference_revision_id"])
  self.assertTrue(receipt["seed_complete"]); self.assertEqual(receipt["component_seeds"],{"a":True})
  self.assertEqual((spec/"a/prd.md").read_bytes(),base_before["a/prd.md"])
  self.assertEqual((spec/"a/_internal/versions/v3/prd.md").read_bytes(),base_before["a/_internal/versions/v3/prd.md"])
  self.assertFalse((spec/"b").exists())
  self.assertEqual(self.P._spec_bytes(base_dir,revision=True),base_before)
  (spec/"a/prd.md").write_text("completed seed edit\n"); (spec/"a/extra.md").unlink()
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertEqual((spec/"a/prd.md").read_text(),"completed seed edit\n")
  self.assertFalse((spec/"a/extra.md").exists())

 def test_scoped_admission_refuses_foreign_payload_and_real_component_deletion(self):
  self._shared_components()
  for foreign in (True,False):
   with self.subTest(foreign=foreign):
    r,f,b=self._cycle("component-negative-"+str(foreign)); spec=Path(b["cycle_dir"])/"artifacts/spec"
    TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
    if foreign:
     (spec/"b").mkdir(); (spec/"b/prd.md").write_text("foreign\n")
    else:
     import shutil
     shutil.rmtree(spec/"a")
    with self.assertRaises(self.P.ProducerError) as caught:self._publish_components(r,f,b)
    self.assertEqual(caught.exception.code,"source-manifest-mismatch" if foreign else "component-set-regressed")


 def test_component_retry_recognizes_history_carried_only_by_an_earlier_revision(self):
  from unittest.mock import patch
  self._shared_components(); r,f,b=self._cycle("component-older-history")
  full=Path(b["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(full,self.artifact)
  (full/"a/_internal/versions/v3/prd.md").unlink()
  self._publish_components(r,f,b)
  r,f,b=self._cycle("component-history-interrupted"); spec=Path(b["cycle_dir"])/"artifacts/spec"
  original=TX.PRODUCER._write_atomic
  def interrupted(path,data):
   if path == spec/"a/extra.md":raise OSError("after old history")
   return original(path,data)
  with patch_split(TX.PRODUCER,"_write_atomic",interrupted):
   with self.assertRaises(OSError):TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertEqual((spec/"a/_internal/versions/v3/prd.md").read_text(),"older a\n")
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  self.assertTrue((spec/"a/prd.md").is_file()); self.assertFalse((spec/"b").exists())

 def test_malformed_component_receipt_refuses_without_reseeding(self):
  self._shared_components(); r,f,b=self._cycle("component-bad-receipt")
  spec=Path(b["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
  receipt_path=spec/TX.PRODUCER.SPEC_BASE_RECEIPT; before=json.loads(receipt_path.read_text())
  (spec/"a/extra.md").unlink()
  for states in ("bad",{"a":False}):
   receipt_path.write_text(json.dumps({**before,"component_seeds":states}))
   with self.assertRaises(self.P.ProducerError) as caught:TX.seed_cycle_spec(spec,self.artifact,spec_root=spec/"a")
   self.assertEqual(caught.exception.code,"shared-base-invalid")
   self.assertFalse((spec/"a/extra.md").exists())


 def _second_reference(self, key="cairn-spec"):
  route,route_file,begun=self._cycle("older-reference")
  spec=Path(begun["cycle_dir"])/"artifacts/spec"; spec.mkdir(parents=True)
  (spec/"prd.md").write_text("old reference\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  return self.P.admit_shared(self.artifact,cycle_id=begun["cycle_id"],kind="spec",source="spec",key=key,allow_new_reference=True)

 def test_canonical_key_is_selected_without_retiring_old_reference(self):
  current=self._shared_v1(); old=self._second_reference()
  _r,_f,begun=self._cycle("two-references")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(base,self.artifact)
  receipt=json.loads((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())
  self.assertEqual(receipt["reference_id"],current["shared_reference_id"])
  self.assertEqual(receipt["revision_id"],current["shared_reference_revision_id"])
  self.assertEqual((base/"prd.md").read_text(),"v1\n")
  self.assertTrue(self.P._reference_path(self.artifact,"spec",old["shared_reference_id"]).is_file())
  with self.assertRaises(self.P.ProducerError) as ctx:
   TX.seed_cycle_spec(base,self.artifact,reference_id=old["shared_reference_id"])
  self.assertEqual(ctx.exception.code,"shared-base-reference-mismatch")
  self.assertEqual(json.loads((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text()),receipt)

 def test_explicit_reference_selects_existing_noncanonical_and_missing_ref_refuses_without_writes(self):
  self._shared_v1(); old=self._second_reference()
  _r,_f,begun=self._cycle("explicit-reference")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  with self.assertRaises(self.P.ProducerError) as ctx:
   TX.seed_cycle_spec(base,self.artifact,reference_id="ref_"+"f"*32)
  self.assertEqual(ctx.exception.code,"reference-unknown")
  self.assertFalse(base.exists())
  TX.seed_cycle_spec(base,self.artifact,reference_id=old["shared_reference_id"])
  self.assertEqual((base/"prd.md").read_text(),"old reference\n")
  self.assertEqual(json.loads((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())["reference_id"],old["shared_reference_id"])

 def test_transaction_cli_reference_selects_existing_reference(self):
  self._shared_v1(); old=self._second_reference()
  _r,_f,begun=self._cycle("cli-reference")
  cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"cli-reference-events.jsonl"
  result=self._run(cycle_dir,"pass",events,selector=("--reference",old["shared_reference_id"]))
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  base=cycle_dir/"artifacts/spec"
  self.assertEqual((base/"prd.md").read_text(),"old reference\n")
  self.assertEqual(json.loads((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())["reference_id"],old["shared_reference_id"])

 def test_multiple_noncanonical_references_remain_ambiguous(self):
  self._shared_v1(); self._second_reference("another-spec")
  current=self.P.find_reference_by_key(self.artifact,"spec","spec")
  path=self.P._reference_path(self.artifact,"spec",current["shared_reference_id"])
  row=json.loads(path.read_text()); row["key"]="renamed-spec"; path.write_text(json.dumps(row))
  _r,_f,begun=self._cycle("ambiguous-reference")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  with self.assertRaises(self.P.ProducerError) as ctx: TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual(ctx.exception.code,"shared-reference-ambiguous")
  self.assertFalse(base.exists())

 def test_owner_begin_seeds_before_review_and_transaction_reuses_receipt(self):
  current=self._shared_v1(); self._second_reference()
  begun=self.P.begin(self.artifact,route_file=self.spec_route,capability="autopilot-spec",intensity="strong")
  cycle_dir=Path(begun["cycle_dir"]); base=cycle_dir/"artifacts/spec"
  receipt=(base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes()
  self.assertEqual(json.loads(receipt)["reference_id"],current["shared_reference_id"])
  verdict=base/"_internal/reviews/verdict.json"; verdict.parent.mkdir(parents=True,exist_ok=True); verdict.write_text('{"verdict":"PASS"}\n')
  events=Path(self._tmp.name)/"preseed-events.jsonl"
  result=self._run(cycle_dir,"pass",events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertEqual((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes(),receipt)
  self.assertEqual(verdict.read_text(),'{"verdict":"PASS"}\n')
  again=self.P.begin(self.artifact,route_file=self.spec_route,capability="autopilot-spec",intensity="strong")
  self.assertEqual(again["cycle_id"],begun["cycle_id"])
  self.assertEqual((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes(),receipt)

 def test_failed_owner_preseed_retries_same_open_cycle(self):
  current=self._shared_v1(); self._second_reference()
  path=self.P._reference_path(self.artifact,"spec",current["shared_reference_id"])
  row=json.loads(path.read_text()); row["key"]="renamed-spec"; path.write_text(json.dumps(row))
  with self.assertRaises(self.P.ProducerError) as ctx:
   self.P.begin(self.artifact,route_file=self.spec_route,capability="autopilot-spec",intensity="strong")
  self.assertEqual(ctx.exception.code,"shared-reference-ambiguous")
  opened=self.P.route_cycle_for(self.artifact,json.loads(self.spec_route.read_text()))
  self.assertIsNotNone(opened)
  base=self.P.cycle_dir(self.artifact,opened["campaign_id"],opened["cycle_id"],opened)/"artifacts/spec"
  self.assertFalse((base/TX.PRODUCER.SPEC_BASE_RECEIPT).exists())
  row["key"]="spec"; path.write_text(json.dumps(row))
  result=self.P.begin(self.artifact,route_file=self.spec_route,capability="autopilot-spec",intensity="strong")
  self.assertEqual(result["cycle_id"],opened["cycle_id"])
  self.assertEqual(json.loads((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())["reference_id"],current["shared_reference_id"])

 def _run(self, cycle_dir, code, events, *, selector=()):
  env={**HERMETIC_ENV,"AGENT_ARTIFACT_CYCLE_DIR":str(cycle_dir),"AGENT_ARTIFACT_ROOT":str(self.artifact)}
  cmd=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(self.artifact),"--worktree",str(self.repo),"--route",str(self.spec_route),"--node","prd-transaction","--events",str(events),*selector,"--",sys.executable,"-c",code]
  return subprocess.run(cmd,text=True,capture_output=True,env=env)

 def test_cycle_spec_is_seeded_and_snapshot_comes_from_the_tool(self):
  self._shared_v1()
  _r,_f,begun=self._cycle("spec-edit"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev.jsonl"
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v2\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  seeded=[r for r in rows if r["status"]=="seeded"]
  self.assertEqual(len(seeded),1); self.assertEqual(seeded[0]["files"],3)
  spec=cycle_dir/"artifacts"/"spec"
  self.assertEqual((spec/"prd.md").read_text(),"v2\n")
  self.assertEqual((spec/"_internal"/"versions"/"v1"/"prd.md").read_text(),"v0\n")   # seeded history
  self.assertEqual((spec/"_internal"/"versions"/"v2"/"prd.md").read_text(),"v1\n")   # tool-written snapshot
  self.assertEqual((spec/"pipeline_state.yaml").read_text(),"s\n")                   # whole tree seeded (D-87)
  self.assertTrue(any(r["status"]=="snapshot" and r["version"]==2 for r in rows),rows)

 def test_sd_open_50_worker_research_residue_without_a_prd_still_seeds(self):
  # H1 (cairn v173 owner, 2026-09-07): the route's spec write scope let the
  # research worker write `_internal/research/**` before the transaction, so
  # the any-file predicate skipped the seed -> no pre-image, next_version=1,
  # no snapshot. The predicate is "prd.md absent"; residue is preserved.
  self._shared_v1()
  _r,_f,begun=self._cycle("spec-edit-residue"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev-residue.jsonl"
  spec=cycle_dir/"artifacts"/"spec"; note=spec/"_internal"/"research"/"note.md"; note.parent.mkdir(parents=True); note.write_text("worker wrote this first\n")
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  seeded=[r for r in rows if r["status"]=="seeded"]
  self.assertEqual(len(seeded),1,rows); self.assertEqual((seeded[0]["files"],seeded[0]["preexisting_files"],seeded[0]["kept_existing"]),(3,1,0))
  self.assertFalse(any(r["status"]=="seed-skipped" for r in rows),rows)
  self.assertEqual([r["next_version"] for r in rows if r["status"]=="acquired"],[2])
  self.assertEqual((spec/"_internal"/"versions"/"v2"/"prd.md").read_text(),"v1\n")   # pre-image snapshot from the tool
  self.assertEqual((spec/"prd.md").read_text(),"v2\n")
  self.assertEqual(note.read_text(),"worker wrote this first\n")                      # residue preserved
  # a prd.md already present still skips the seed, and says the prd is there
  _r,_f,begun2=self._cycle("spec-edit-present"); cycle2=Path(begun2["cycle_dir"]); events2=Path(self._tmp.name)/"ev-present.jsonl"
  spec2=cycle2/"artifacts"/"spec"; spec2.mkdir(parents=True); (spec2/"prd.md").write_text("v1\n"); (spec2/"_internal"/"versions"/"v1").mkdir(parents=True); (spec2/"_internal"/"versions"/"v1"/"prd.md").write_text("v0\n")
  result=self._run(cycle2,code,events2)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events2.read_text().splitlines()]
  # review finding 6: a present prd.md is a label, not an early return -- the
  # copy loop still fills what the bucket lacks (here pipeline_state.yaml) and
  # keeps the bucket's own prd.md and history untouched.
  seeded=[r for r in rows if r["status"]=="seeded"]
  self.assertEqual([(r["prd_present"],r["files"],r["kept_existing"],sorted(r["kept_existing_paths"])) for r in seeded],
                   [(True,1,2,["_internal/versions/v1/prd.md","prd.md"])])
  # the v{N} chain spans cycle buckets: the first cycle snapshotted v2, so this one is v3
  self.assertEqual((spec2/"prd.md").read_text(),"v3\n"); self.assertEqual((spec2/"_internal"/"versions"/"v3"/"prd.md").read_text(),"v1\n")

 def test_sd_open_50_partial_seed_is_completed_and_component_scope_judges_its_own_prd(self):
  # review finding 6: the predicate is a label, not an early return -- a bucket
  # that holds only prd.md (crash mid-seed) or another component's prd.md is
  # completed by the copy loop, and `kept_existing` keeps it idempotent.
  self._shared_v1()
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)/"partial"; base.mkdir(); (base/"prd.md").write_text("v1\n")
   first=TX.seed_cycle_spec(base,self.artifact)
   self.assertEqual((first["status"],first["prd_present"],first["kept_existing"],first["kept_existing_paths"]),("seeded",True,1,["prd.md"]))
   self.assertTrue((base/"pipeline_state.yaml").is_file()); self.assertTrue((base/"_internal"/"versions"/"v1"/"prd.md").is_file())
   again=TX.seed_cycle_spec(base,self.artifact)
   self.assertEqual((again["status"],again["reason"],again["kept_existing"]),("seed-skipped","prd-present",3))
   scoped=Path(td)/"scoped"; (scoped/"componentB").mkdir(parents=True); (scoped/"componentB"/"prd.md").write_text("b\n")
   with self.assertRaises(TX.PRODUCER.ProducerError) as ctx:
    TX.seed_cycle_spec(scoped,self.artifact,spec_root=scoped/"componentA")
   self.assertEqual(ctx.exception.code,"shared-base-unproven")
   self.assertEqual((scoped/"componentB"/"prd.md").read_text(),"b\n")

 def test_seed_receipt_precedes_copy_and_stale_retry_never_relabels_base(self):
  from unittest import mock
  first=self._shared_v1()
  _r,_f,begun=self._cycle("interrupted-seed")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  original=TX.PRODUCER._write_atomic
  def interrupt(path,data):
   if path.name=="prd.md": raise OSError("interrupted")
   return original(path,data)
  with patch_split(TX.PRODUCER,"_write_atomic",interrupt):
   with self.assertRaises(OSError): TX.seed_cycle_spec(base,self.artifact)
  receipt=(base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes()
  self.assertEqual(json.loads(receipt)["revision_id"],first["shared_reference_revision_id"])
  # Simulate a later publisher advancing the authoritative pointer.
  ref=TX.PRODUCER._reference_path(self.artifact,"spec",first["shared_reference_id"])
  record=json.loads(ref.read_text()); record["latest_revision_id"]="rrev_"+"f"*32
  ref.write_text(json.dumps(record))
  with self.assertRaises(TX.PRODUCER.ProducerError) as ctx: TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual(ctx.exception.code,"shared-base-invalid")
  self.assertEqual((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes(),receipt)

 def test_completed_seed_preserves_edits_and_deletions_after_latest_moves(self):
  first=self._shared_v1()
  _r,_f,begun=self._cycle("editing-original")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(base,self.artifact)
  receipt=(base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes()
  (base/"prd.md").write_text("my change\n")
  (base/"pipeline_state.yaml").unlink()
  route,route_file,new=self._cycle("new-publisher")
  output=Path(new["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(output,self.artifact)
  (output/"prd.md").write_text("other change\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=new["cycle_id"])
  latest=self.P.admit_shared(self.artifact,cycle_id=new["cycle_id"],kind="spec",source="spec",key="spec")
  result=TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual(result["latest_revision_id"],latest["shared_reference_revision_id"])
  self.assertEqual((base/"prd.md").read_text(),"my change\n")
  self.assertFalse((base/"pipeline_state.yaml").exists())
  self.assertEqual((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_bytes(),receipt)
  self.assertEqual(json.loads(receipt)["revision_id"],first["shared_reference_revision_id"])

 def test_partial_seed_recovery_keeps_original_base_after_real_publish(self):
  from unittest import mock
  first=self._shared_v1()
  _r,_f,begun=self._cycle("partial-original")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  original=TX.PRODUCER._write_atomic
  def interrupt(path,data):
   if path.name=="prd.md": raise OSError("interrupted")
   return original(path,data)
  with patch_split(TX.PRODUCER,"_write_atomic",interrupt):
   with self.assertRaises(OSError): TX.seed_cycle_spec(base,self.artifact)
  route,route_file,new=self._cycle("other-publisher")
  output=Path(new["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(output,self.artifact); (output/"prd.md").write_text("v2\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=new["cycle_id"])
  self.P.admit_shared(self.artifact,cycle_id=new["cycle_id"],kind="spec",source="spec",key="spec")
  TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual((base/"prd.md").read_text(),"v1\n")
  receipt=json.loads((base/TX.PRODUCER.SPEC_BASE_RECEIPT).read_text())
  self.assertEqual(receipt["revision_id"],first["shared_reference_revision_id"])
  self.assertTrue(receipt["seed_complete"])

 def test_new_seed_refuses_corrupt_latest_before_receipt_or_copy(self):
  first=self._shared_v1()
  (Path(first["revision_dir"])/"prd.md").write_text("tampered current")
  _r,_f,begun=self._cycle("corrupt-current")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  with self.assertRaises(self.P.ProducerError) as exc: TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual(exc.exception.code,"shared-revision-integrity")
  self.assertFalse((base/TX.PRODUCER.SPEC_BASE_RECEIPT).exists())
  self.assertFalse((base/"prd.md").exists())

 def test_legacy_seed_missing_file_is_not_silently_resurrected(self):
  self._shared_v1()
  _r,_f,begun=self._cycle("legacy-seed")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  TX.seed_cycle_spec(base,self.artifact)
  receipt_path=base/TX.PRODUCER.SPEC_BASE_RECEIPT
  receipt=json.loads(receipt_path.read_text()); receipt.pop("seed_complete")
  receipt_path.write_text(json.dumps(receipt))
  (base/"pipeline_state.yaml").unlink()
  with self.assertRaises(self.P.ProducerError) as exc: TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual(exc.exception.code,"shared-seed-state-unproven")
  self.assertFalse((base/"pipeline_state.yaml").exists())

 def test_edited_partial_seed_refuses_instead_of_overwriting(self):
  from unittest import mock
  self._shared_v1()
  _r,_f,begun=self._cycle("edited-partial")
  base=Path(begun["cycle_dir"])/"artifacts/spec"
  original=TX.PRODUCER._write_atomic
  def interrupt(path,data):
   if path.name=="prd.md": raise OSError("interrupted")
   return original(path,data)
  with patch_split(TX.PRODUCER,"_write_atomic",interrupt):
   with self.assertRaises(OSError): TX.seed_cycle_spec(base,self.artifact)
  (base/"prd.md").write_text("user edit\n")
  with self.assertRaises(self.P.ProducerError) as exc: TX.seed_cycle_spec(base,self.artifact)
  self.assertEqual(exc.exception.code,"shared-seed-state-unproven")
  self.assertEqual((base/"prd.md").read_text(),"user edit\n")

 def test_malformed_seed_receipt_is_not_replaced(self):
  with tempfile.TemporaryDirectory() as td:
   base=Path(td); receipt=base/TX.PRODUCER.SPEC_BASE_RECEIPT; receipt.parent.mkdir()
   receipt.write_text("null")
   with self.assertRaises(TX.PRODUCER.ProducerError) as ctx: TX.seed_cycle_spec(base,self.artifact)
   self.assertEqual(ctx.exception.code,"shared-base-invalid")
   self.assertEqual(receipt.read_text(),"null")

 def test_unproven_changed_or_deleted_old_file_is_rejected_before_receipt(self):
  self._shared_v1()
  for relative in ("prd.md","retired/prd.md"):
   with tempfile.TemporaryDirectory() as td:
    base=Path(td); path=base/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text("old work")
    with self.assertRaises(TX.PRODUCER.ProducerError) as ctx: TX.seed_cycle_spec(base,self.artifact)
    self.assertEqual(ctx.exception.code,"shared-base-unproven")
    self.assertFalse((base/TX.PRODUCER.SPEC_BASE_RECEIPT).exists())

 def test_seed_unions_version_history_across_revisions(self):
  # Latest revision carries the PRD but no history (cairn's rrev_511a shape);
  # an earlier revision holds _internal/versions/v3. The counter must continue at 4.
  first=self._shared_v1()
  route,route_file,begun=self._cycle("seed-source-2")
  spec=Path(begun["cycle_dir"])/"artifacts"/"spec"; spec.mkdir(parents=True)
  (spec/"prd.md").write_text("v3\n"); (spec/"pipeline_state.yaml").write_text("s\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  # cairn's rrev_511a shape (3 files, history dropped) predates D-87; model it explicitly.
  self.P.admit_shared(self.artifact,cycle_id=begun["cycle_id"],kind="spec",source="spec",key="spec",base_revision=first["shared_reference_revision_id"],drop_components=["_internal"],drop_reason="fixture: pre-D-87 shape")
  _r,_f,begun=self._cycle("spec-edit-3"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev3.jsonl"
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v4\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  seeded=[r for r in rows if r["status"]=="seeded"][0]
  self.assertEqual(seeded["history_versions"],1)
  spec=cycle_dir/"artifacts"/"spec"
  self.assertEqual((spec/"_internal"/"versions"/"v1"/"prd.md").read_text(),"v0\n")
  self.assertEqual((spec/"_internal"/"versions"/"v2"/"prd.md").read_text(),"v3\n")
  self.assertEqual((spec/"prd.md").read_text(),"v4\n")
  self.assertFalse(any(r["status"]=="version-history-absent" for r in rows))


 def test_legacy_only_chain_continues_in_the_first_cutover_cycle(self):
  # The cairn W13 shape: cutover active, no shared revision yet, the whole
  # chain lives in the legacy read-only bucket (v1..v168). The seed is
  # skipped and the counter used to restart at v1.
  legacy=self.artifact/"spec"; (legacy/"_internal"/"versions"/"v168").mkdir(parents=True); (legacy/"prd.md").write_text("v168 body\n")
  _r,_f,begun=self._cycle("first-cutover-cycle"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev5.jsonl"
  spec=cycle_dir/"artifacts"/"spec"; spec.mkdir(parents=True,exist_ok=True); (spec/"prd.md").write_text("v168 body\n")
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  self.assertEqual([r["reason"] for r in rows if r["status"]=="seed-skipped"],["prd-present"])
  self.assertEqual([r["next_version"] for r in rows if r["status"]=="acquired"],[169])
  self.assertFalse(any(r["status"]=="version-history-absent" for r in rows),rows)
  self.assertEqual((spec/"_internal"/"versions"/"v169"/"prd.md").read_text(),"v168 body\n")
  self.assertEqual((spec/"prd.md").read_text(),"v169\n")
  self.assertFalse((spec/"_internal"/"versions"/"v1").exists(),"the chain must not restart at v1")
  self.assertFalse((legacy/"_internal"/"versions"/"v169").exists(),"legacy stays read-only")


 def test_empty_first_cutover_bucket_with_no_shared_revision_continues_the_legacy_chain(self):
  # Exact cairn W13 shape: empty open bucket, no shared revision, legacy holds v1..v168.
  legacy=self.artifact/"spec"; (legacy/"_internal"/"versions"/"v168").mkdir(parents=True); (legacy/"prd.md").write_text("v168 body\n")
  _r,_f,begun=self._cycle("first-cutover-cycle-empty"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev6.jsonl"
  self.assertFalse((cycle_dir/"artifacts"/"spec").exists(),"the producer creates the bucket lazily; the transaction must cope with an absent spec root")
  code="import os; from pathlib import Path; r=Path(os.environ['AGENT_SPEC_ROOT']); r.mkdir(parents=True,exist_ok=True); (r/'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  self.assertEqual([r["reason"] for r in rows if r["status"]=="seed-skipped"],["no-shared-revision"])
  self.assertEqual([(r["next_version"],r["version_source_layout"]) for r in rows if r["status"]=="acquired"],[(169,"legacy")])
  released=[r for r in rows if r["status"]=="released"][0]
  self.assertEqual((released["version"],released["snapshot"]),(169,"not-required-new"))
  self.assertEqual((cycle_dir/"artifacts"/"spec"/"prd.md").read_text(),"v169\n")
  self.assertFalse((cycle_dir/"artifacts"/"spec"/"_internal"/"versions").exists(),"no pre-image, no snapshot; the number alone continues")


 def test_unadmitted_sealed_cycle_still_advances_the_chain(self):
  # Cycle A snapshots v2 but is sealed without admit_shared; cycle B seeds
  # from the shared v1 revision and must not reuse v2.
  self._shared_v1()
  route,route_file,begun=self._cycle("spec-edit-a"); cycle_a=Path(begun["cycle_dir"]); ev_a=Path(self._tmp.name)/"ev-a.jsonl"
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_a,code,ev_a); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertTrue((cycle_a/"artifacts"/"spec"/"_internal"/"versions"/"v2"/"prd.md").is_file())
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  _r,_f,begun=self._cycle("spec-edit-b"); cycle_b=Path(begun["cycle_dir"]); ev_b=Path(self._tmp.name)/"ev-b.jsonl"
  result=self._run(cycle_b,code,ev_b); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in ev_b.read_text().splitlines()]
  acquired=[r for r in rows if r["status"]=="acquired"]
  self.assertEqual([r["next_version"] for r in acquired],[3])
  self.assertEqual(acquired[0]["version_source_layout"],"cycle"); self.assertEqual(Path(acquired[0]["version_source"]),cycle_a/"artifacts"/"spec")
  spec_b=cycle_b/"artifacts"/"spec"
  self.assertEqual((spec_b/"_internal"/"versions"/"v3"/"prd.md").read_text(),"v1\n")
  self.assertFalse((spec_b/"_internal"/"versions"/"v2").exists(),"the seeded copy carries only shared history; v2 lives in cycle A")
  self.assertEqual((spec_b/"prd.md").read_text(),"v3\n")

 def test_refused_route_seeds_nothing(self):
  self._shared_v1()
  _r,_f,begun=self._cycle("spec-edit-4"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev4.jsonl"
  env={**HERMETIC_ENV,"AGENT_ARTIFACT_CYCLE_DIR":str(cycle_dir),"AGENT_ARTIFACT_ROOT":str(self.artifact)}
  cmd=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(self.artifact),"--worktree",str(self.repo),"--route",str(self.spec_route),"--node","no-such-node","--events",str(events),"--",sys.executable,"-c","pass"]
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,65,result.stdout+result.stderr)
  self.assertEqual([p for p in (cycle_dir/"artifacts"/"spec").rglob("*") if p.is_file()],[])
  self.assertFalse(any(json.loads(l)["status"]=="seeded" for l in events.read_text().splitlines()))

 def test_child_writing_legacy_spec_is_a_typed_failure(self):
  self._shared_v1()
  legacy=self.artifact/"spec"; legacy.mkdir(); (legacy/"prd.md").write_text("stale\n")
  _r,_f,begun=self._cycle("spec-edit-2"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev2.jsonl"
  code=f"from pathlib import Path; Path({str(legacy/'prd.md')!r}).write_text('rogue\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,65,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  blocked=[r for r in rows if r["status"]=="blocked" and r["reason"]=="legacy-spec-written"]
  self.assertEqual(len(blocked),1); self.assertEqual(blocked[0]["changed"],["prd.md"])

 def test_routed_transaction_defaults_artifact_worktree_route_and_node(self):
  route=json.loads(self.spec_route.read_text())
  route_file=Path(self.L.admit_runtime_route(self.artifact,route).route_file)
  jobs=Path(self._tmp.name)/"route-jobs.log"; jobs.write_text("")
  begun=self.P.begin(self.artifact,route_file=route_file,capability="autopilot-spec",
                     intensity="strong",jobs=jobs,require_cycle=True)
  node=next(row for row in route["nodes"] if row["id"]=="prd-transaction")
  env={**HERMETIC_ENV,"AGENT_ARTIFACT_ROOT":str(self.artifact),
       "AGENT_ROUTE_FILE":str(route_file),"AGENT_ROUTE_ID":route["route_id"],
       "AGENT_ROUTE_NODE":node["id"],"AGENT_DISPATCH_WORKTREE":str(self.repo),
       "AGENT_DISPATCH_JOBS":str(jobs),**begun["env"]}
  spec=Path(begun["cycle_dir"])/"artifacts/spec"
  spec.mkdir(parents=True,exist_ok=True); (spec/"prd.md").write_text("before\n")
  result=subprocess.run([sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--",
      sys.executable,"-c","from pathlib import Path; import os; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('after\\n')"],
      cwd=self.repo,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertEqual((spec/"prd.md").read_text(),"after\n")


if __name__=="__main__":
 unittest.main()
