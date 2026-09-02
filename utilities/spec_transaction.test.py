#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"utilities"))
import artifact_lifecycle as L  # noqa: E402
import artifact_producer as P  # noqa: E402
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
R=load("route",ROOT/"utilities/capability-route.py")
TX=load("spec_transaction",ROOT/"utilities/spec-transaction.py")
REPO_ID,ROOT_ID="repo_"+"a"*32,"root_"+"b"*32

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
   code="import os,time; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n'); time.sleep(float(os.environ.get('HOLD','0')))"
   base=self.command(root,artifact,route,code,events=events)
   first=subprocess.Popen(base,env={**os.environ,"HOLD":".4"},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   deadline=time.time()+2
   while time.time()<deadline and (not events.exists() or '"status": "acquired"' not in events.read_text()): time.sleep(.02)
   second=subprocess.Popen(base,env={**os.environ,"HOLD":"0"},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   out1,err1=first.communicate(timeout=4); out2,err2=second.communicate(timeout=4)
   self.assertEqual(first.returncode,0,out1+err1); self.assertEqual(second.returncode,0,out2+err2)
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
    result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    self.assertFalse((spec/"_internal/versions").exists())

 def test_empty_version_directory_cannot_bypass_snapshot_file(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; root=Path(os.environ['AGENT_SPEC_ROOT']); (root/'_internal/versions'/('v'+os.environ['AGENT_SPEC_NEXT_VERSION'])).mkdir(parents=True,exist_ok=True); (root/'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"before\n")

 def test_mismatched_manual_snapshot_fails_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; root=Path(os.environ['AGENT_SPEC_ROOT']); snap=root/'_internal/versions'/('v'+os.environ['AGENT_SPEC_NEXT_VERSION']); snap.mkdir(parents=True,exist_ok=True); (snap/'prd.md').write_text('wrong\\n'); (root/'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True)
   self.assertEqual(result.returncode,65,result.stdout+result.stderr)
   self.assertIn("version-snapshot-mismatch",result.stdout)

 def test_failed_command_still_snapshots_changed_preimage(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os,sys; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('partial\\n'); sys.exit(7)"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True)
   self.assertEqual(result.returncode,7,result.stdout+result.stderr)
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"before\n")

 def test_spec_touch_required(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; artifact.mkdir(); subprocess.run(["git","init","-q",str(root)],check=True)
   gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
   route=R.compile_route("autopilot-code","dev","direct",root,artifact,predicates=["atomic-outcome","known-scope","no-shared-contract","no-resource-run","no-artifact-handoff","no-independent-verifier","focused-verification"],inline_reason="atomic-direct",tracking="tracked",tracked_gate_evidence=gate)
   path=root/"route.json"; path.write_text(json.dumps(route)); result=subprocess.run([sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(artifact),"--worktree",str(root),"--route",str(path),"--node","inline","--",sys.executable,"-c","pass"],text=True,capture_output=True)
   self.assertEqual(result.returncode,65); self.assertIn("spec-touch-not-declared",result.stdout)

 # -- W7C cutover: the v{N} chain is canonical, not per-cycle -------------

 def cutover_fixture(self, root: Path):
  """A cutover-active root whose legacy chain already reached v168.

  `resolve_output_dir` then hands the transaction the open cycle's empty
  `artifacts/spec` bucket, which is exactly the state that used to restart
  the chain at v1 on every cycle.
  """
  artifact=root/".agent_reports"; artifact.mkdir(parents=True)
  subprocess.run(["git","init","-q",str(root)],check=True)
  subprocess.run(["git","-C",str(root),"config","user.email","fixture@example.com"],check=True)
  subprocess.run(["git","-C",str(root),"config","user.name","Fixture"],check=True)
  (root/"README").write_text("x\n"); subprocess.run(["git","-C",str(root),"add","README"],check=True); subprocess.run(["git","-C",str(root),"commit","-qm","init"],check=True)
  P.activate(artifact,repository_id=REPO_ID,artifact_root_id=ROOT_ID)
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  route=R.compile_route("autopilot-spec","update","strong",root,artifact,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(root))
  binding=L.admit_runtime_route(artifact,route)
  begun=P.begin(artifact,route_file=Path(binding.route_file),capability="autopilot-spec",intensity="strong")
  (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
  return artifact,Path(begun["cycle_dir"]),Path(binding.route_file)

 def test_next_version_continues_the_canonical_chain_across_layouts(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; cycle_spec=artifact/"campaigns/c/cycles/y/artifacts/spec"
   cycle_spec.mkdir(parents=True)
   (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),169,"empty cycle bucket must not restart at v1")
   rev=artifact/"shared/spec/ref_x/revisions/rrev_x"
   (rev/"_internal/versions/v200").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),201,"an immutable shared revision also carries history")
   (cycle_spec/"_internal/versions/v300").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),301)
   # a sealed cycle bucket that was never admitted to shared/ still counts, so
   # the next cycle cannot reuse v300
   other=artifact/"campaigns/c/cycles/z/artifacts/spec"; other.mkdir(parents=True)
   self.assertEqual(TX.next_version(other,artifact),301)

 def test_next_version_keeps_a_component_on_its_own_chain(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; cycle_spec=artifact/"campaigns/c/cycles/y/artifacts/spec"
   (cycle_spec/"comp").mkdir(parents=True)
   (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
   (artifact/"spec/comp/_internal/versions/v5").mkdir(parents=True)
   (artifact/"shared/spec/ref_x/revisions/rrev_x/comp/_internal/versions/v7").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec/"comp",artifact,"comp"),8)
   self.assertEqual(TX.next_version(cycle_spec,artifact),169)

 def test_cutover_transaction_snapshots_v169_into_the_open_cycle(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,cycle_dir,route=self.cutover_fixture(root)
   spec=cycle_dir/"artifacts/spec"; spec.mkdir(parents=True,exist_ok=True)
   (spec/"prd.md").write_text("v168 body\n")
   env={**os.environ,"AGENT_ARTIFACT_CYCLE_DIR":str(cycle_dir)}
   code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=env)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertIn('"next_version": 169',result.stdout)
   self.assertEqual((spec/"_internal/versions/v169/prd.md").read_text(),"v168 body\n")
   self.assertEqual((spec/"prd.md").read_text(),"v169\n")
   self.assertFalse((spec/"_internal/versions/v1").exists(),"the chain must not restart at v1")
   self.assertFalse((artifact/"spec/_internal/versions/v169").exists(),"legacy stays read-only")

 def test_component_spec_root_owns_its_version_sequence(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,component,route=self.fixture(root,component="component"); (component/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code,spec_root=component),text=True,capture_output=True)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual((component/"_internal/versions/v1/prd.md").read_text(),"before\n")
   self.assertFalse((artifact/"spec/_internal/versions/v1").exists())

if __name__=="__main__": unittest.main()
