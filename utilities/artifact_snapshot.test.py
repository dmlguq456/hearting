#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json, subprocess, sys, tempfile, unittest
import os
from pathlib import Path
from unittest import mock
from split_module_fixture import patch_split

ROOT=Path(__file__).resolve().parents[1]
HELPER=ROOT/"utilities/artifact-snapshot.py"
CLEAN_ENV={k:v for k,v in os.environ.items() if not k.startswith((
 "AGENT_DISPATCH_","AGENT_ROUTE_","AGENT_REVIEW_","AGENT_ARTIFACT_"))}


class ArtifactSnapshotTest(unittest.TestCase):
 def route(self, root: Path, *, route_id="rt-one", capability="autopilot-refine", intensity="standard") -> Path:
  path=root/f"{route_id}.json"
  path.write_text(json.dumps({"route_id":route_id,"route_hash":f"sha256:{route_id}","capability":capability,"effective_intensity":intensity,"nodes":[{"id":"transaction","write_scope":["target-artifact"]}]}))
  return path

 def run_helper(self, artifact: Path, target: Path, route: Path, route_id: str, node: str="transaction"):
  return subprocess.run([sys.executable,str(HELPER),"prepare","--artifact-root",str(artifact),"--target",str(target),"--route",str(route),"--route-id",route_id,"--node",node],text=True,capture_output=True,env=CLEAN_ENV)

 def draft_route(self, root: Path, *, route_id="rt-draft", intensity="standard") -> Path:
  path=root/f"{route_id}.json"
  nodes=[
   {"id":"frame","write_scope":["shards/frame/**"]},
   {"id":"frame-alternative","write_scope":["shards/frame-alternative/**"]},
   {"id":"strategy","write_scope":["analysis/**","strategy/**","assets/source/**"]},
   {"id":"review","write_scope":["reviews/strategy/**"]},
   {"id":"draft-production","write_scope":["draft/**"]},
   {"id":"finalize","write_scope":["final/**","pipeline_summary.md"]},
  ]
  path.write_text(json.dumps({"route_id":route_id,"route_hash":f"sha256:{route_id}","capability":"autopilot-draft","effective_intensity":intensity,"nodes":nodes}))
  return path

 def test_same_route_reuses_one_version_and_preserves_relative_paths(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/cycle/doc.md"; appendix=artifact/"documents/cycle/parts/appendix.md"
   appendix.parent.mkdir(parents=True); doc.write_text("doc-before\n"); appendix.write_text("appendix-before\n")
   route=self.route(root)
   first=self.run_helper(artifact,doc,route,"rt-one"); second=self.run_helper(artifact,appendix,route,"rt-one"); repeat=self.run_helper(artifact,doc,route,"rt-one")
   self.assertEqual((first.returncode,second.returncode,repeat.returncode),(0,0,0),first.stderr+second.stderr+repeat.stderr)
   version=artifact/"documents/cycle/_internal/versions/v1"
   self.assertEqual((version/"doc.md").read_text(),"doc-before\n")
   self.assertEqual((version/"parts/appendix.md").read_text(),"appendix-before\n")
   self.assertEqual(len(list((version.parent).glob("v*"))),1)
   self.assertEqual(json.loads(repeat.stdout)["snapshot"],"matched")

 def test_next_route_allocates_next_version(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"research/topic/report.md"; doc.parent.mkdir(parents=True); doc.write_text("v0\n")
   one=self.route(root,route_id="rt-one"); self.assertEqual(self.run_helper(artifact,doc,one,"rt-one").returncode,0)
   doc.write_text("v1\n"); two=self.route(root,route_id="rt-two"); self.assertEqual(self.run_helper(artifact,doc,two,"rt-two").returncode,0)
   self.assertEqual((doc.parent/"_internal/versions/v1/report.md").read_text(),"v0\n")
   self.assertEqual((doc.parent/"_internal/versions/v2/report.md").read_text(),"v1\n")

 def test_direct_refine_and_new_target_do_not_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/cycle/doc.md"; doc.parent.mkdir(parents=True); doc.write_text("before\n")
   direct=self.route(root,intensity="direct"); result=self.run_helper(artifact,doc,direct,"rt-one")
   self.assertEqual(result.returncode,0,result.stderr); self.assertIn("minor-direct-edit",result.stdout); self.assertFalse((doc.parent/"_internal").exists())
   major=self.route(root,route_id="rt-two"); new=doc.parent/"new.md"; result=self.run_helper(artifact,new,major,"rt-two")
   self.assertEqual(result.returncode,0,result.stderr); self.assertIn("new-target",result.stdout); self.assertFalse((doc.parent/"_internal").exists())

 def test_draft_refinement_snapshots_existing_file(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; draft=artifact/"documents/cycle/draft/manuscript.md"; draft.parent.mkdir(parents=True); draft.write_text("draft\n")
   route=self.route(root,capability="autopilot-draft",intensity="direct"); result=self.run_helper(artifact,draft,route,"rt-one")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((artifact/"documents/cycle/_internal/versions/v1/draft/manuscript.md").read_text(),"draft\n")

 def test_unowned_container_and_mismatched_preimage_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; legacy=artifact/"rebuttal/rebuttal.md"; legacy.parent.mkdir(parents=True); legacy.write_text("legacy\n")
   route=self.route(root); result=self.run_helper(artifact,legacy,route,"rt-one")
   self.assertEqual(result.returncode,65); self.assertIn("target-container-unowned",result.stderr)
   doc=artifact/"documents/cycle/doc.md"; doc.parent.mkdir(parents=True); doc.write_text("before\n")
   self.assertEqual(self.run_helper(artifact,doc,route,"rt-one").returncode,0)
   snapshot=doc.parent/"_internal/versions/v1/doc.md"; snapshot.write_text("corrupt\n")
   result=self.run_helper(artifact,doc,route,"rt-one")
   self.assertEqual(result.returncode,65); self.assertIn("snapshot-preimage-mismatch",result.stderr)

 def test_existing_legacy_sibling_layout_is_preserved(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/legacy/doc.md"; doc.parent.mkdir(parents=True); doc.write_text("current\n"); (doc.parent/"doc_v1.md").write_text("old\n")
   route=self.route(root); result=self.run_helper(artifact,doc,route,"rt-one")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((doc.parent/"doc_v2.md").read_text(),"current\n")
   self.assertFalse((doc.parent/"_internal").exists())

 def test_draft_support_scopes_skip_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; route=self.draft_route(root)
   cases=[("frame","shards/frame/direction-brief.md"),
          ("frame-alternative","shards/frame-alternative/direction-brief.md"),
          ("strategy","strategy/plan.md"),
          ("strategy","analysis/source-map.md"),
          ("review","reviews/strategy/verdict.md"),
          ("draft-production","draft/cheatsheet.md"),
          ("finalize","final/final.md"),
          ("finalize","pipeline_summary.md")]
   for node,rel in cases:
    target=artifact/rel; target.parent.mkdir(parents=True,exist_ok=True); target.write_text("support\n")
    result=self.run_helper(artifact,target,route,"rt-draft",node=node)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertIn("support-artifact",result.stdout)
   self.assertEqual([path for path in artifact.rglob("_internal")],[])

 def test_cycle_layout_support_artifact_skips_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"
   target=artifact/"campaigns/2026-09-14_c/cycles/cyc_x/artifacts/shards/frame/direction-brief.md"
   target.parent.mkdir(parents=True); target.write_text("support\n")
   route=self.draft_route(root)
   result=self.run_helper(artifact,target,route,"rt-draft",node="frame")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertIn("support-artifact",result.stdout)

 def test_cycle_layout_document_still_snapshots(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"
   target=artifact/"campaigns/c/cycles/y/artifacts/documents/name/draft/manuscript.md"
   target.parent.mkdir(parents=True); target.write_text("doc-before\n")
   route=self.draft_route(root)
   result=self.run_helper(artifact,target,route,"rt-draft",node="draft-production")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((target.parents[1]/"_internal/versions/v1/draft/manuscript.md").read_text(),"doc-before\n")

 def test_malformed_and_undeclared_support_paths_still_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; route=self.draft_route(root)
   malformed=artifact/"documents/loose.md"; malformed.parent.mkdir(parents=True); malformed.write_text("x\n")
   result=self.run_helper(artifact,malformed,route,"rt-draft",node="strategy")
   self.assertEqual(result.returncode,65); self.assertIn("target-container-unowned",result.stderr)
   undeclared=artifact/"shards/other/x.md"; undeclared.parent.mkdir(parents=True); undeclared.write_text("x\n")
   result=self.run_helper(artifact,undeclared,route,"rt-draft",node="strategy")
   self.assertEqual(result.returncode,65); self.assertIn("target-container-unowned",result.stderr)

 def test_shared_and_outside_root_still_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; route=self.draft_route(root)
   shared=artifact/"shared/spec/ref/revisions/1/prd.md"; shared.parent.mkdir(parents=True); shared.write_text("x\n")
   result=self.run_helper(artifact,shared,route,"rt-draft",node="finalize")
   self.assertEqual(result.returncode,65); self.assertIn("target-shared-immutable",result.stderr)
   outside=root/"outside.md"; outside.write_text("x\n")
   result=self.run_helper(artifact,outside,route,"rt-draft",node="finalize")
   self.assertEqual(result.returncode,65); self.assertIn("target-outside-artifact-root",result.stderr)
   # A route working in a repository accepts that repository's documents, never a path outside both.
   repository=root/"repo"; repository.mkdir(); subprocess.run(["git","init","-q",str(repository)],check=True)
   refine=self.route(root,route_id="rt-repo")
   refine.write_text(json.dumps({**json.loads(refine.read_text()),"cwd":str(repository)}))
   for path in (outside,repository/".git"/"config"):
    result=self.run_helper(artifact,path,refine,"rt-repo")
    self.assertEqual(result.returncode,65,result.stdout); self.assertIn("target-outside-artifact-root",result.stderr)

 def test_only_a_verified_git_repository_counts_as_the_route_repository(self):
  # A cwd whose `git rev-parse` fails must not be taken as the repository root.
  import importlib.util
  if str(ROOT/"utilities") not in sys.path:
   sys.path.insert(0,str(ROOT/"utilities"))
  import artifact_producer
  spec=importlib.util.spec_from_file_location("artifact_snapshot_helper",HELPER)
  helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); plain=root/"plain"; plain.mkdir(); (plain/"notes.md").write_text("x\n")
   repository=root/"repo"; repository.mkdir(); subprocess.run(["git","init","-q",str(repository)],check=True)
   (repository/"README.md").write_text("r\n")
   cycle=root/".agent_reports"/"cycle"/"artifacts"; cycle.mkdir(parents=True)
   with patch_split(artifact_producer,"route_cycle_for",return_value={"cycle_id":"cyc"}), \
        mock.patch.object(artifact_producer,"_cycle_artifacts_dir",return_value=cycle):
    self.assertIsNone(helper.worktree_target(root/".agent_reports",{"cwd":str(plain)},plain/"notes.md"))
    self.assertIsNone(helper.worktree_target(root/".agent_reports",{"cwd":str(repository)},repository/".git"/"HEAD"))
    self.assertEqual(helper.worktree_target(root/".agent_reports",{"cwd":str(repository)},repository/"README.md"),
                     (cycle,Path("README.md")))


class ProjectDocumentSnapshotTest(unittest.TestCase):
 def setUp(self):
  import importlib.util
  self.temp=tempfile.TemporaryDirectory(prefix="project-document-"); self.addCleanup(self.temp.cleanup)
  self.base=Path(self.temp.name); self.project=self.base/"project"; self.project.mkdir()
  self.artifact=self.project/".agent_reports"; self.output=self.artifact/"campaigns/fixture/cycle/artifacts"
  self.output.mkdir(parents=True)
  spec=importlib.util.spec_from_file_location("project_document_snapshot",HELPER)
  self.helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.helper)
  self.route={"route_id":"rt-project-doc","route_hash":"sha256:fixture","source_commit":"unversioned",
              "capability":"autopilot-draft","effective_intensity":"direct","cwd":str(self.project),
              "artifact_root":str(self.artifact),"nodes":[{"id":"inline","write_scope":["target-artifact","draft/**"]}]}
  self.route_path=self.base/"route.json"; self.route_path.write_text(json.dumps(self.route))

 def prepare(self,target):
  import io,contextlib,artifact_producer
  out=io.StringIO()
  with patch_split(artifact_producer,"route_cycle_for",return_value={"cycle_id":"cyc-fixture"}), \
       mock.patch.object(artifact_producer,"_cycle_artifacts_dir",return_value=self.output), \
       mock.patch.dict(os.environ,{"GIT_CEILING_DIRECTORIES":str(self.base),"AGENT_ROUTE_FILE":"","AGENT_ROUTE_ID":""}), \
       contextlib.redirect_stdout(out):
   try:
    from types import SimpleNamespace
    code=self.helper.prepare(SimpleNamespace(artifact_root=str(self.artifact),target=str(target),
                           route=str(self.route_path),route_id=self.route["route_id"],node="inline"))
   except self.helper.SnapshotError as exc:
    return 65,{"reason":str(exc)}
  return code,json.loads(out.getvalue())

 def test_new_non_git_document_starts_at_the_project_path_and_its_revision_stays_in_records(self):
  target=self.project/"report.md"
  code,receipt=self.prepare(target)
  self.assertEqual(code,0,receipt)
  self.assertEqual(receipt["reason"],"new-target")
  self.assertFalse(target.exists())
  target.write_text("first project draft\n")
  code,receipt=self.prepare(target)
  self.assertEqual(code,0,receipt)
  snapshot=self.output/"_internal/versions/v1/report.md"
  self.assertEqual(snapshot.read_text(),"first project draft\n")
  self.assertEqual(receipt["target"],str(target.resolve()))
  stored=json.loads((snapshot.parent/".snapshot-receipt.json").read_text())
  self.assertEqual(stored["targets"]["report.md"]["path"],str(target.resolve()))
  self.assertEqual(stored["targets"]["report.md"]["preimage_sha256"],receipt["preimage_sha256"])
  target.write_text("integrated project report\n")
  self.assertEqual(target.read_text(),"integrated project report\n")
  self.assertFalse((self.project/"_internal").exists())
  self.assertFalse((self.project/".git").exists())
  self.assertFalse(list(self.base.glob("*-wt")))

 def test_non_git_snapshot_does_not_expand_past_the_bound_project(self):
  outside=self.base/"outside.md";outside.write_text("outside\n")
  code,receipt=self.prepare(outside)
  self.assertEqual(code,65)
  self.assertIn("target-outside-artifact-root",receipt["reason"])
  self.assertEqual(outside.read_text(),"outside\n")
  self.assertFalse((self.output/"_internal").exists())


import importlib.util, os
_SPEC=importlib.util.spec_from_file_location("owner_preview_fixture_for_snapshot",Path(__file__).with_name("owner_preview_approval.test.py"))
PRODUCER_FIXTURE=importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(PRODUCER_FIXTURE)


class OwnerPreviewSnapshotTest(PRODUCER_FIXTURE.OwnerRefineBase):
    """A standard refine route's snapshot and write guard hold the target until the preview is released.

    Real compose/admission and real `workflow-supervisor.py gate/release`; the helper runs as the
    owner would run it (no node of its own, the route's own entry-bound `transaction`).
    """

    def prepare(self, target, *, node="transaction"):
        env={**os.environ,"AGENT_ARTIFACT_ROOT":str(self.root),"AGENT_DISPATCH_JOBS":str(self.jobs)}
        env.pop("AGENT_DISPATCH_REGISTERED_WORKER",None)
        return subprocess.run([sys.executable,str(HELPER),"prepare","--artifact-root",str(self.root),"--target",str(target),
                               "--route",str(self.path),"--route-id",self.route["route_id"],"--node",node],
                              text=True,capture_output=True,env=env)

    def target(self):
        doc=Path(self.cycle["cycle_dir"])/"artifacts"/"documents"/"topic"/"doc.md"
        doc.parent.mkdir(parents=True,exist_ok=True); doc.write_text("original\n")
        return doc

    def test_the_target_is_not_snapshotted_before_the_person_releases_the_preview(self):
        self.build("claude",close=False)
        doc=self.target()
        refused=self.prepare(doc)
        self.assertEqual(refused.returncode,65,refused.stdout+refused.stderr)
        self.assertIn("human-gate-not-raised",refused.stderr)
        self.assertNotIn("preview-approval-required",refused.stderr)
        self.assertFalse((doc.parent/"_internal").exists())
        self.raise_gate()
        unreleased=self.prepare(doc)
        self.assertEqual(unreleased.returncode,65)
        self.assertIn("human-gate-unreleased",unreleased.stderr)
        self.assertEqual(self.release("proceed",worker=True).returncode,64)   # an owner cannot release it
        self.assertEqual(self.prepare(doc).returncode,65)
        self.assertEqual(self.release("proceed").returncode,0)
        done=self.prepare(doc)
        self.assertEqual(done.returncode,0,done.stderr)
        self.assertEqual((doc.parent/"_internal"/"versions"/"v1"/"doc.md").read_text(),"original\n")

    def test_a_document_in_the_route_repository_is_snapshotted_into_the_cycle_after_release(self):
        # Claude/Codex r4: refine's target was the repository README, refused as outside the artifact root.
        self.build("claude",close=False)
        readme=Path(self.route["cwd"]).resolve()/"README.md"
        before=readme.read_bytes()
        self.raise_gate()
        self.assertEqual(self.prepare(readme).returncode,65)          # the preview fence still holds it
        self.assertEqual(self.release("proceed").returncode,0)
        done=self.prepare(readme)
        self.assertEqual(done.returncode,0,done.stderr)
        snapshot=Path(self.cycle["cycle_dir"])/"artifacts"/"_internal"/"versions"/"v1"/"README.md"
        self.assertEqual(json.loads(done.stdout)["path"],str(snapshot.resolve()))
        self.assertEqual(snapshot.read_bytes(),before)
        self.assertEqual(readme.read_bytes(),before)
        self.assertFalse(list(readme.parent.glob("README_v*.md")))   # nothing is written into the repository
        self.assertEqual(self.prepare(readme).returncode,0)            # the same route reuses its version

    def test_a_revise_stop_or_changed_preview_holds_the_target_again(self):
        for decision in ("revise","stop"):
            with self.subTest(decision=decision):
                fixture=OwnerPreviewSnapshotTest("test_a_revise_stop_or_changed_preview_holds_the_target_again")
                fixture.setUp()
                try:
                    fixture.build("claude",close=False); doc=fixture.target(); fixture.raise_gate()
                    self.assertEqual(fixture.release(decision).returncode,0)
                    refused=fixture.prepare(doc)
                    self.assertEqual(refused.returncode,65,refused.stdout+refused.stderr)
                    self.assertIn("human-gate-",refused.stderr)
                    self.assertNotIn("preview-approval-required",refused.stderr)
                finally:
                    fixture.doCleanups()

    def test_the_general_write_guard_never_carries_the_owner_operation_fence(self):
        # The producer's write check is not where the owner's apply is held: a refine owner's support, draft,
        # report or preview writes are not the apply, and the target mutation is fenced at its snapshot.
        import artifact_producer as producer
        self.build("claude",close=False)
        doc=self.target()
        artifacts=Path(self.cycle["cycle_dir"])/"artifacts"
        owner_env={"AGENT_ROUTE_FILE":str(self.path),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_ARTIFACT_ROOT":str(self.root)}
        with mock.patch.dict(os.environ,owner_env):
            os.environ.pop("AGENT_ROUTE_NODE",None)
            for support in (artifacts/"documents"/"topic"/"draft.md",artifacts/"research"/"topic"/"notes.md",
                            artifacts/"documents"/"topic"/"_internal"/"scratch.md",self.preview,self.report,doc):
                with self.subTest(write=str(support.relative_to(artifacts))):
                    checked=producer.check_write(self.root,support)
                    self.assertNotEqual(checked.get("layout"),"inline-gate",checked)
                    self.assertNotEqual(checked.get("reason"),"preview-approval-required",checked)
            outside=Path(tempfile.gettempdir())/"outside-owner-write.md"
            self.assertNotEqual(producer.check_write(self.root,outside).get("layout"),"inline-gate")

    def test_a_support_or_draft_write_and_the_preview_precede_the_release(self):
        import artifact_producer as producer
        self.build("claude",close=False)
        artifacts=Path(self.cycle["cycle_dir"])/"artifacts"
        owner_env={"AGENT_ROUTE_FILE":str(self.path),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_ARTIFACT_ROOT":str(self.root)}
        with mock.patch.dict(os.environ,owner_env):
            os.environ.pop("AGENT_ROUTE_NODE",None)
            for rel in ("documents/topic/draft.md","research/topic/notes.md"):
                with self.subTest(support=rel):
                    checked=producer.check_write(self.root,artifacts/rel)
                    self.assertEqual(checked["verdict"],"allow",checked)
            self.assertEqual(producer.check_write(self.root,self.preview)["verdict"],"allow")

    def test_a_declared_support_path_is_not_held_by_the_snapshot_fence(self):
        # The fence guards the bound target only; a path the transaction declares as support
        # (`changelog/**`) is classified first and skipped, before any preview exists.
        self.build("claude",close=False)
        support=Path(self.cycle["cycle_dir"])/"artifacts"/"changelog"/"topic"/"CHANGELOG.md"
        support.parent.mkdir(parents=True,exist_ok=True); support.write_text("log\n")
        result=self.prepare(support)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"],"support-artifact")
        self.assertNotIn("human-gate",result.stdout+result.stderr)

    def test_the_target_snapshot_follows_a_persons_proceed(self):
        self.build("claude",close=False)
        doc=self.target()
        self.assertEqual(self.prepare(doc).returncode,65)
        self.raise_gate()
        self.assertEqual(self.release("proceed").returncode,0)
        done=self.prepare(doc)
        self.assertEqual(done.returncode,0,done.stderr)

    def test_a_preview_binding_whose_owner_operation_does_not_mutate_the_target_snapshots_as_before(self):
        self.build("claude",close=False)
        doc=self.target()
        route=json.loads(self.path.read_text())
        for node in route["nodes"]:
            if node.get("id")=="transaction":
                node["write_scope"]=[s for s in node.get("write_scope",[]) if s!="target-artifact"]
        other=Path(self.cycle["cycle_dir"]).parent/"nonmutating-route.json"
        other.write_text(json.dumps(route))
        for node in ("transaction",""):
            with self.subTest(node=node):
                env={**CLEAN_ENV,"AGENT_ARTIFACT_ROOT":str(self.root),"AGENT_DISPATCH_JOBS":str(self.jobs)}
                done=subprocess.run([sys.executable,str(HELPER),"prepare","--artifact-root",str(self.root),"--target",str(doc),
                                     "--route",str(other),"--route-id",route["route_id"],"--node",node],
                                    text=True,capture_output=True,env={**CLEAN_ENV,**env})
                self.assertEqual(done.returncode,0,done.stderr)
                self.assertIn("node-does-not-mutate-target",done.stdout)
        self.assertFalse((doc.parent/"_internal").exists())

    def test_a_route_without_the_preview_binding_snapshots_as_before(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/cycle/doc.md"
            doc.parent.mkdir(parents=True); doc.write_text("before\n")
            route=ArtifactSnapshotTest().route(root)
            done=ArtifactSnapshotTest().run_helper(artifact,doc,route,"rt-one")
            self.assertEqual(done.returncode,0,done.stderr)

    def test_routed_prepare_defaults_route_context_and_preserves_explicit_target(self):
        self.build("claude",close=False)
        doc=self.target()
        self.raise_gate()
        self.assertEqual(self.release("proceed").returncode,0)
        env={**CLEAN_ENV,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.root),
            "AGENT_ROUTE_FILE":str(self.path),"AGENT_ROUTE_ID":self.route["route_id"],
            "AGENT_ROUTE_NODE":"transaction","AGENT_DISPATCH_WORKTREE":self.route["cwd"],
            "AGENT_DISPATCH_JOBS":str(self.jobs),**self.cycle["env"]}
        done=subprocess.run([sys.executable,str(HELPER),"prepare","--target",str(doc)],
            cwd=self.route["cwd"],env=env,text=True,capture_output=True)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertEqual(doc.read_text(),"original\n")
        self.assertEqual(json.loads(done.stdout)["status"],"snapshot")


if __name__=="__main__": unittest.main()
