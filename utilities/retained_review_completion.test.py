#!/usr/bin/env python3
"""Actual completion/registry/reader seams, using isolated historical fixtures."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as D
import dispatch_replacement as replacement
import retained_review_completion as retained
from codex_dispatch_terminal import inspect_terminal_attempt

spec = importlib.util.spec_from_file_location("retained_marker_fixture", ROOT / "utilities/dispatch_completion_marker.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
R = fixture.ROUTE
PARENT = "11111111-2222-3333-4444-555555555555"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RetainedReviewTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CompletionMarkerTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.env = self.f.base_env()
        for key in list(self.env):
            if key.startswith(("CLAUDE_", "OPENCODE_", "AGENT_DISPATCH_")):
                self.env.pop(key)
        self.env.update(AGENT_HOME=str(ROOT), AGENT_DISPATCH_JOBS=str(self.f.jobs),
                        CODEX_THREAD_ID=PARENT, AGENT_SESSION_ROLE="main")
        self.patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.route = self.f.compile_route(capability="autopilot-lab", capability_mode="eval", signals=[])
        self.path = self.f.write_route(self.route)
        self.node = next(n for n in self.route["nodes"] if n["id"] == "independent-verify-alternative")
        self.root = self.f.artifact
        self.evidence = self.root / "reviews-alternative/eval-verdict.json"
        self.evidence.parent.mkdir()
        self.saved = self.root / "reviews/eval-alternative/gap-1/prior-eval-verdict.json"
        self.saved.parent.mkdir(parents=True)
        self.manifest = self.root / "report/report_manifest.json"
        self.manifest.parent.mkdir()
        self.bundle_files = []
        for name,text in (("REPORT.md","# fixed report"),("index.html","<p>fixed report</p>"),("media/figure.txt","fixed figure")):
            path=self.manifest.parent/name
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
            self.bundle_files.append({"path":name,"absolute_path":str(path),"sha256":sha(path.read_bytes()),"bytes":path.stat().st_size})
        self.manifest.write_text(json.dumps({"files":self.bundle_files,"parent_inputs":[],"governing_inputs":[]}))
        self.report = self.saved.parent / "REPORT.md"
        self.report.write_text("# BLOCKED: correction belongs to the other peer\n")
        self.review = self.root / "conflict-review.md"
        self.review.write_text("Reviewed both original subjects and the unchanged report inputs.\n")
        owner = {"attempt_schema_version":"2", "dispatch_depth":"1", "transport":"headless",
                 "execution_surface":"registered-headless", "registered_worker":"1",
                 "fallback_hop":"same-harness-headless", "attempt_id":"att-retained-owner",
                 "worker_type":"owner", "parent_sid":PARENT, "parent_harness":"codex",
                 "owner_route_id":self.route["route_id"], "owner_route_hash":self.route["route_hash"],
                 "owner_route_file":str(self.path)}
        self.f.jobs.write_text("\t".join(["2026-10-25T00:00:00Z", "open", str(self.f.repo), str(self.f.repo), "owner",
                                         ",".join(k+"="+v for k,v in owner.items())])+"\n")
        self.directory = self.f.jobs.parent / "completion" / self.route["route_id"]

    def seed(self, harness="codex", *, scientific=False):
        prior = {"verdict":"PASS", "io":{"verdict":"PASS"}, "route_node":self.node["id"],
                 "guard_session_id":"att-retained-prior", "reviewer_attempt_id":"att-retained-prior",
                 "blocking_findings":[], "report_manifest_sha256":sha(self.manifest.read_bytes()),
                 "reviewed_report_manifest":{"path":str(self.manifest),"sha256":sha(self.manifest.read_bytes())},
                 "input_hashes":[{"path":str(self.manifest),"sha256":sha(self.manifest.read_bytes()),"bytes":self.manifest.stat().st_size},
                     *[{"path":str(self.manifest.parent/e["path"]),"sha256":e["sha256"],"bytes":e["bytes"]} for e in self.bundle_files]]}
        self.evidence.write_text(json.dumps(prior))
        self.saved.write_bytes(self.evidence.read_bytes())
        self.register("att-retained-prior", harness, "PASS", self.evidence)
        # Historical publication predates fixed-report subject binding. Do not
        # manufacture a binding on either closed historical attempt.
        with mock.patch("review_source_subject.marker_subject",return_value=None):
            R.complete_node(self.route, self.node, self.node["id"], self.evidence, self.f.jobs, "att-retained-prior")
        blocked = {"verdict":"BLOCKED", "io":{"verdict":"BLOCKED"}, "route_node":self.node["id"],
                   "reviewer_attempt":"att-retained-rejected", "guard_session_id":"att-retained-rejected",
                   "report_manifest_sha256":prior["report_manifest_sha256"],
                   "source_changed":False,"prd_changed":False,"other_peer_files_read":False,
                   "new_inference_or_resource":False,"delta_correctness_defects":[],
                   "completed_counts":{"raw_records":0,"raw_shards":0,"scientific_levels":0},
                   "failed_counts":{"observed_scientific_mismatches":0},
                   "prior_red_status":{"closed":0,"open":0,"regressed":0},
                   "prior_verdict":{"verdict":"PASS","blocking_findings_count":0,
                       "preserved":[{"source":str(self.evidence),"preserved":str(self.saved),
                                     "sha256":sha(self.saved.read_bytes()),"bytes":self.saved.stat().st_size}]}}
        if scientific:
            blocked["completed_counts"]["scientific_levels"] = 1
        self.evidence.write_text(json.dumps(blocked))
        self.register("att-retained-rejected", harness, "BLOCKED", self.report)
        # Seed the historical writer defect, not a new permitted production path.
        with mock.patch("codex_dispatch_terminal.artifact_declared_verdict", return_value="unstated"), \
             mock.patch("review_source_subject.marker_subject",return_value=None):
            R.complete_node(self.route, self.node, self.node["id"], self.evidence, self.f.jobs, "att-retained-rejected")
        D.reconcile_attempt_terminal(self.f.jobs, "att-retained-rejected", "dead-worker-blocked",
              evidence={"failure_class":"blocked","classifier_source":"completion-join-late-terminal-v1"})
        self.original = {p:p.read_bytes() for p in [self.evidence,self.saved,self.manifest,
                self.directory/f"{self.node['id']}.1.json",self.directory/f"{self.node['id']}.2.json",
                self.directory/f"{self.node['id']}.att-retained-prior.attempt.json",
                self.directory/f"{self.node['id']}.att-retained-rejected.attempt.json"]}

    def register(self, aid, harness, verdict, artifact):
        log = self.root / (aid+".jsonl")
        text = f"artifact: {artifact}\nverdict: {verdict}\nblocker: {'none' if verdict=='PASS' else 'assignment/node/scope mismatch'}"
        log.write_text("\n".join(json.dumps(e) for e in [
            {"type":"item.completed","item":{"type":"agent_message","text":text}},
            {"type":"turn.completed"}])+"\n")
        args = SimpleNamespace(attempt_id=aid,jobs_path=self.f.jobs,worktree=self.f.repo,
                replacement_input_argv=["--route-node",self.node["id"]],route_id=self.route["route_id"],route_node=self.node["id"])
        sealed = replacement.seal_launch_input(args,harness,"Own alternative correction input "+aid)
        pid = self.live.pid if getattr(self,"live",None) is not None and aid=="att-retained-rejected" else 99999999
        start = D.process_start_ticks(pid) if pid != 99999999 else "1"
        extra = (f"worker_type=review,harness={harness},artifact_root={self.root},route_file={self.path},"
                 f"parent_attempt_id=att-retained-owner,parent_sid={PARENT},log_file={log},"
                 f"pid={pid},pid_start={start},pgid={pid},"+
                 f"pid_ns={D.process_namespace_identity(os.getpid())},pid_observer_ns={D.process_namespace_identity(os.getpid())}"+sealed)
        self.f.write_row("open",aid,aid,extra,node_id=self.node["id"])

    def preview(self):
        return retained.preview(self.f.jobs,"att-retained-rejected")

    def apply(self):
        rows=retained._rows(self.f.jobs)
        return D.resolve_terminal_conflict(self.f.jobs,"att-retained-rejected",
                    expected_row_sha256=sha(rows["att-retained-rejected"][2].encode()),review=self.review)

    def test_writer_refuses_explicit_nonpass_before_publishing(self):
        for verdict in ("BLOCKED","FAIL"):
            with self.subTest(verdict=verdict):
                aid="att-retained-"+verdict.lower()
                self.evidence.write_text(json.dumps({"verdict":verdict,"io":{"verdict":verdict}}))
                self.register(aid,"codex",verdict,self.evidence)
                before=self.f.jobs.read_bytes()
                with self.assertRaisesRegex(ValueError,"completion-evidence-verdict-not-pass"):
                    R.complete_node(self.route,self.node,self.node["id"],self.evidence,self.f.jobs,aid)
                self.assertEqual(self.f.jobs.read_bytes(),before)
                self.assertFalse((self.directory/f"{self.node['id']}.json").exists())

    def test_join_does_not_settle_a_pass_envelope_with_blocked_json(self):
        import dispatch_completion_join as join
        self.evidence.write_text(json.dumps({"verdict":"BLOCKED"}))
        self.register("att-retained-json","codex","PASS",self.evidence)
        before=self.f.jobs.read_bytes()
        self.assertFalse(join.settle_open_pass(self.f.jobs,"att-retained-json"))
        self.assertEqual(before,self.f.jobs.read_bytes())

    def test_public_command_preserves_codex_binding(self):
        self.exercise_harness("codex")

    def test_public_command_preserves_claude_binding(self):
        self.exercise_harness("claude")

    def test_public_command_preserves_opencode_binding(self):
        self.exercise_harness("opencode")

    def exercise_harness(self, harness):
        self.seed(harness)
        before=self.f.jobs.read_bytes()
        self.assertTrue(self.preview()["can_reuse_prior_review"])
        self.assertTrue(self.preview()["authority_ready"])
        self.assertEqual(before,self.f.jobs.read_bytes())
        args=[sys.executable,"-B",str(ROOT/"utilities/dispatch-registry.py"),"resolve-terminal-conflict",
              "--jobs",str(self.f.jobs),"--attempt","att-retained-rejected",
              "--expected-row-sha256",sha(retained._rows(self.f.jobs)["att-retained-rejected"][2].encode()),
              "--review-evidence",str(self.review),"--apply"]
        proc=subprocess.run(args,env=self.env,text=True,capture_output=True,timeout=60)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertIn('"outcome": "failed"',proc.stdout)
        self.assert_reads()
        self.assertEqual(self.preview()["recovery_state"],"applied")
        self.apply()
        self.assertEqual(len(list(self.directory.glob(self.node["id"]+".[0-9]*.json"))),3)

    def assert_reads(self):
        for p,raw in self.original.items():self.assertEqual(p.read_bytes(),raw,str(p))
        marker=json.loads((self.directory/f"{self.node['id']}.json").read_bytes())
        self.assertEqual(marker["attempt_id"],"att-retained-prior")
        self.assertEqual(marker["sequence"],3)
        self.assertEqual(marker["round_census"],json.loads(self.original[self.directory/f"{self.node['id']}.2.json"])["round_census"])
        self.assertEqual(marker["evidence"]["path"],str(self.saved))
        self.assertEqual(D.gate_currency(self.route,self.node,self.directory/f"{self.node['id']}.json",jobs=self.f.jobs).state,"current")
        self.assertIsNone(R._registered_marker_fence(self.route,self.node,marker,self.f.jobs.read_text().splitlines()))
        self.assertEqual(D.completion_attempt_readiness(self.route,self.node,marker,self.f.jobs).state,"ready")
        meta=retained._rows(self.f.jobs)["att-retained-rejected"][1]
        self.assertFalse(D.verdict_pass(meta))
        self.assertEqual(D.committed_outcome("done",meta),"failed")
        self.assertFalse(D.terminal_conflict_pending(meta))
        # Previous installed readers recognize only review_sha256. Their
        # original v1 pending predicate remains true: no false PASS delivery.
        self.assertTrue(any(not e.get("review_sha256") for e in D.terminal_conflicts(meta).values()))
        observed = R._marker_identity_row(self.route,self.node,self.node["id"],self.node["completion_gate"],
                                          jobs=self.f.jobs,exact_terminal=True)
        self.assertTrue(observed["passed"],observed)
        import dispatch_completion_join as join
        for aid,verdict in (("att-retained-prior","PASS"),("att-retained-rejected","BLOCKED")):
            current = join.current_delivery_state(self.f.jobs,aid,parent_attempt_id="att-retained-owner",advance=False)
            self.assertEqual(current.verdict,verdict,current)
        self.assertEqual(join.required_action_for_attempt("done",meta),"inspect-done-failure")

    def test_changed_input_or_scientific_block_does_not_release_conflict(self):
        self.seed()
        original=self.manifest.read_bytes()
        self.manifest.write_text('{"fixed":"different"}')
        self.assertFalse(self.preview()["can_reuse_prior_review"])
        before=self.f.jobs.read_bytes()
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(before,self.f.jobs.read_bytes())
        self.manifest.write_bytes(original)
        figure=self.manifest.parent/"media/figure.txt"
        figure.write_text("changed figure with unchanged index")
        self.assertFalse(self.preview()["can_reuse_prior_review"])
        # Rehashing a changed BLOCKED claim does not grant a historical marker.
        data=json.loads(self.evidence.read_text());data["completed_counts"]["scientific_levels"]=1
        self.evidence.write_text(json.dumps(data))
        self.assertFalse(self.preview()["can_reuse_prior_review"])

    def test_scientific_blocked_with_valid_hashes_does_not_reuse_a_pass(self):
        self.seed(scientific=True)
        observed=self.preview()
        self.assertFalse(observed["can_reuse_prior_review"])
        self.assertEqual(observed["recovery_reason"],"retained-review-blocked-not-preserved-judgment")
        before=self.f.jobs.read_bytes()
        with self.assertRaisesRegex(ValueError,"blocked-not-preserved-judgment"):self.apply()
        self.assertEqual(before,self.f.jobs.read_bytes())

    def test_live_group_is_not_released_by_a_stored_terminal_marker(self):
        self.live=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],stdin=subprocess.PIPE,start_new_session=True)
        self.addCleanup(lambda:self.live.communicate(timeout=5) if self.live.poll() is None else None)
        self.seed()
        observed=self.preview()
        self.assertFalse(observed["can_reuse_prior_review"])
        self.assertEqual(observed["recovery_reason"],"retained-review-attempt-not-settled")
        before=self.f.jobs.read_bytes()
        with self.assertRaisesRegex(ValueError,"attempt-not-settled"):self.apply()
        self.assertEqual(before,self.f.jobs.read_bytes())
        self.assertIsNone(self.live.poll())

    def test_changed_sealed_route_is_refused_even_with_execution_gates_off(self):
        self.seed()
        changed=json.loads(self.path.read_text());changed["slug"]="different sealed assignment"
        self.path.write_text(json.dumps(changed))
        observed=self.preview()
        self.assertFalse(observed["can_reuse_prior_review"])
        self.assertIn("route-lineage-unverified",observed["recovery_reason"])
        before=self.f.jobs.read_bytes()
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(before,self.f.jobs.read_bytes())

    def test_wrong_actor_live_attempt_and_row_race_are_refused(self):
        self.seed()
        before=self.f.jobs.read_bytes()
        with mock.patch.dict(os.environ,{"CODEX_THREAD_ID":"other-parent"}):
            self.assertFalse(self.preview()["authority_ready"])
            with self.assertRaises(ValueError):self.apply()
        self.assertEqual(before,self.f.jobs.read_bytes())
        with self.assertRaisesRegex(ValueError,"retained-review-row-changed"):
            D.resolve_terminal_conflict(self.f.jobs,"att-retained-rejected",expected_row_sha256="0"*64,review=self.review)
        self.assertEqual(before,self.f.jobs.read_bytes())
        self.register("att-retained-later","codex","PASS",self.saved)
        self.assertFalse(self.preview()["can_reuse_prior_review"])

    def test_partial_publication_resumes_without_a_new_history_or_round(self):
        self.seed()
        route_module=D._route_module()
        original=route_module.atomic_write
        def interrupted(path,value):
            if Path(path)==self.directory/f"{self.node['id']}.json":raise OSError("interrupted canonical replace")
            return original(path,value)
        with mock.patch.object(route_module,"atomic_write",side_effect=interrupted):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual(D.gate_currency(self.route,self.node,self.directory/f"{self.node['id']}.json",jobs=self.f.jobs).reason,"completion-evidence-verdict-not-pass")
        self.assertEqual(self.preview()["recovery_state"],"publication-pending")
        self.apply()
        self.assert_reads()
        self.assertEqual(len(list(self.directory.glob(self.node["id"]+".[0-9]*.json"))),3)

    def test_uncommitted_history_is_reused_after_registry_replace_interruption(self):
        self.seed()
        with mock.patch.object(D,"_atomic_registry_replace",side_effect=OSError("registry write interrupted")):
            with self.assertRaises(OSError):self.apply()
        self.assertTrue(D.terminal_conflict_pending(retained._rows(self.f.jobs)["att-retained-rejected"][1]))
        self.apply()
        self.assert_reads()
        self.assertEqual(len(list(self.directory.glob(self.node["id"]+".[0-9]*.json"))),3)

    def test_recovery_cannot_overwrite_a_newer_canonical_marker(self):
        self.seed();self.apply()
        canonical=self.directory/f"{self.node['id']}.json"
        recorded=json.loads(canonical.read_text())
        newer=copy.deepcopy(recorded);newer.update(sequence=4,state="superseded-by-upstream-revision")
        canonical.write_text(json.dumps(newer));(self.directory/f"{self.node['id']}.4.json").write_text(json.dumps(newer))
        before=self.f.jobs.read_bytes()
        with self.assertRaisesRegex(ValueError,"newer-completion-marker"):self.apply()
        self.assertEqual(self.f.jobs.read_bytes(),before)
        # A later producer edit does not destroy authentication of the old
        # recovery edge; current reuse still refuses the changed subject.
        self.manifest.write_text("changed by a later producer revision")
        retained.verify_marker(self.route,self.node,recorded,self.directory/f"{self.node['id']}.3.json",historical=True)

    def test_later_review_memo_edit_does_not_invalidate_a_committed_judgment(self):
        self.seed();self.apply()
        self.review.write_text("Later operator note; original reviewed hash remains recorded.\n")
        marker_path=self.directory/f"{self.node['id']}.json"
        currency=D.gate_currency(self.route,self.node,marker_path,jobs=self.f.jobs)
        self.assertEqual(currency.state,"current",currency)
        import dispatch_completion_join as join
        current=join.current_delivery_state(self.f.jobs,"att-retained-prior",parent_attempt_id="att-retained-owner",advance=False)
        self.assertEqual(join.delivery_classification(current),"success")
        self.review.unlink()
        self.assertEqual(D.gate_currency(self.route,self.node,marker_path,jobs=self.f.jobs).state,"current")

    def test_forged_recovery_marker_and_history_do_not_pass_readers(self):
        self.seed();self.apply()
        canonical=self.directory/f"{self.node['id']}.json"
        history=self.directory/f"{self.node['id']}.3.json"
        marker=json.loads(canonical.read_text());marker["round_census"]["verdict_rounds"]=1
        canonical.write_text(json.dumps(marker));history.write_text(json.dumps(marker))
        self.assertNotEqual(D.gate_currency(self.route,self.node,canonical,jobs=self.f.jobs).state,"current")


if __name__=="__main__":unittest.main()
