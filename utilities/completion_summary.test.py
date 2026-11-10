"""Task completion is independent of an audit subject's negative disposition."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'utilities'))
import route_identity
import work_intent
import codex_dispatch_terminal as terminal_reader
import dispatch_supervisor_terminal as supervisor
import dispatch_completion_join as join
import dispatch_terminal_commit as commit


class CompletionMeaningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.artifacts=self.root/'.agent_reports';self.artifacts.mkdir()
        self.report=self.artifacts/'audit.md'
        self.report.write_text('## 검토 결과\n판정: 수정 후 병합\n## 검증\nfocused PASS\n## 남은 것\nP1 수정 필요\n')
        self.jobs=self.root/'jobs.log'
        self.route={'capability':'audit','work_request':{'text':'Review this exact patch'},
                    'artifact_root':str(self.artifacts),'cwd':str(self.repo)}
        self.route['route_hash']=route_identity.route_hash(self.route)
        self.route['route_id']=route_identity.route_id_from_hash(self.route['route_hash'])
        self.routefile=self.artifacts/(self.route['route_id']+'.json')
        self.routefile.write_text(json.dumps(self.route))
        self.aid='att-synthetic-review'

    def seed(self,harness='codex',parent='claude',*,capability=None,blocker='none',artifact=None,worker='owner',verdict='FAIL'):
        if capability:
            self.route['capability']=capability
            self.route['route_hash']=route_identity.route_hash(self.route)
            self.route['route_id']=route_identity.route_id_from_hash(self.route['route_hash'])
            self.routefile.write_text(json.dumps(self.route))
        self.aid='att-synthetic-review-'+harness+'-'+parent
        self.meta={'attempt_schema_version':'2','attempt_id':self.aid,'dispatch_depth':'1',
                   'harness':harness,'owner_harness':harness,'worker_type':worker,
                   'workflow_completion':'runtime-v1','artifact_root':str(self.artifacts),
                   'route_file':str(self.routefile),'route_hash':self.route['route_hash'],
                   'route_id':self.route['route_id'],'parent_sid':'synthetic-'+parent,
                   'parent_completion_delivery':{'claude':'claude-parent-runtime','codex':'codex-native-queue','opencode':'opencode-turn'}[parent]}
        self.text=f'artifact: {artifact or self.report}\nverdict: {verdict}\nblocker: {blocker}'
        if harness=='codex':
            rows=[{'type':'item.completed','item':{'type':'agent_message','text':self.text}}, {'type':'turn.completed'}]
        elif harness=='claude':
            rows=[{'type':'result','subtype':'success','is_error':False,'result':self.text}]
        else:
            rows=[{'type':'text','part':{'text':self.text}},{'type':'step_finish','part':{'reason':'stop'}}]
        self.log=self.root/(harness+'.jsonl');self.log.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        self.meta['log_file']=str(self.log)
        self.write_row('open')

    def write_row(self,status):
        self.jobs.write_text(f'2026-11-10T00:00:00Z\t{status}\t{self.repo}\t{self.repo}\taudit\t'+','.join(k+'='+v for k,v in self.meta.items())+'\n')

    def live(self,harness='codex'):
        raw=supervisor.classify_codex_result(self.text) if harness=='codex' else supervisor.classify_session_result(
            {'subtype':'success','is_error':False,'result':self.text},0,runtime=harness)
        return supervisor.completed_owner_assessment(raw,self.text,jobs=self.jobs,attempt_id=self.aid)

    def test_all_owner_and_parent_harness_pairs_keep_negative_assessment_and_complete_the_audit(self):
        summaries=[]
        for harness in ('claude','codex','opencode'):
            for parent in ('claude','codex','opencode'):
                with self.subTest(owner=harness,parent=parent):
                    self.seed(harness,parent)
                    outcome=self.live(harness)
                    self.assertEqual(outcome.failure_class,'pass')
                    self.assertEqual(outcome.reconcile_reason,'completed-audit-with-findings')
                    parsed=terminal_reader.inspect_terminal_attempt(self.log,worktree=self.repo,
                        artifact_root_metadata=self.artifacts,worker_type='owner',artifact_binding=self.meta)
                    self.assertEqual(parsed['verdict'],'PASS')
                    self.assertEqual(parsed['assessment_verdict'],'FAIL')
                    recovered=supervisor.completed_owner_assessment(supervisor.classify_supervisor_log(self.log,harness),None,jobs=self.jobs,attempt_id=self.aid)
                    self.assertEqual((recovered.note,recovered.failure_class,recovered.reconcile_reason),(outcome.note,outcome.failure_class,outcome.reconcile_reason))
                    carrier,extra=terminal_reader.carrier_terminal_note(self.log,worktree=self.repo,artifact_root_metadata=self.artifacts,worker_type='owner',artifact_binding=self.meta)
                    self.assertEqual(carrier['verdict'],'PASS');self.assertEqual(extra['reconcile_reason'],'completed-audit-with-findings')
                    self.assertIn('수정 후 병합',self.report.read_text())
                    self.meta.update(note=outcome.note,**outcome.evidence());self.write_row('done')
                    self.assertEqual(commit._exact_owner_handoff(self.jobs,self.aid,self.route)['primary'],str(self.report))
                    slot=commit.terminal_slot(self.artifacts,self.route['route_id'],self.aid)
                    request=type('Request',(),{'route_file':self.routefile,'jobs':self.jobs,'owner_attempt_id':self.aid})()
                    with mock.patch.object(commit,'_completion_cost',return_value=None):
                        commit._seal_completion_summary(request,self.route,self.report,slot)
                    summary=work_intent.sealed_summary(self.meta,self.aid)
                    self.assertIn('findings',summary)
                    body=join.completion_followup_text({'children':[{'attempt_id':self.aid,'required_action':'advance-completed'}]},jobs=str(self.jobs),surface=f'/adapter/{parent}/preflight.sh')
                    self.assertIn(summary,body);self.assertNotIn('inspect-done-failure',body)
                    summaries.append(summary)
        self.assertEqual(len(set(summaries)),1)

    def test_subject_findings_in_blocker_are_retained_without_becoming_an_execution_error(self):
        self.seed(blocker='P1 publication misclassification remains')
        outcome=self.live();self.assertEqual(outcome.failure_class,'pass')
        parsed=terminal_reader.inspect_terminal_attempt(self.log,worktree=self.repo,
            artifact_root_metadata=self.artifacts,worker_type='owner',artifact_binding=self.meta)
        self.assertNotIn('assessment_blocker',parsed)
        detail=terminal_reader.inspect_terminal_attempt(self.log,worktree=self.repo,artifact_root_metadata=self.artifacts,worker_type='owner',artifact_binding=self.meta,include_failure_detail=True)
        self.assertEqual(detail['assessment_detail_excerpt'],'P1 publication misclassification remains')
        self.assertEqual(parsed['blocker_reason'],'none')

    def test_real_blockers_missing_artifacts_and_non_audit_or_stage_failures_are_not_promoted(self):
        for args in ({'blocker':'cannot inspect','verdict':'BLOCKED'}, {'artifact':str(self.root/'absent')},
                     {'worker':'review'}, {'worker':'stage'}, {'capability':'autopilot-code'}):
            with self.subTest(args=args):
                self.seed(**args);self.assertNotEqual(self.live().failure_class,'pass')
        self.seed(capability='audit');self.meta['route_hash']='sha256:wrong';self.write_row('open')
        self.assertEqual(self.live().failure_class,'fail')

    def test_runtime_failure_and_already_committed_rows_are_unchanged(self):
        self.seed();self.write_row('done');before=self.jobs.read_bytes()
        self.assertEqual(self.live().failure_class,'fail');self.assertEqual(self.jobs.read_bytes(),before)
        raw=supervisor.classify_codex_result(self.text,process_exit=1)
        self.assertEqual(supervisor.completed_owner_assessment(raw,self.text,jobs=self.jobs,attempt_id=self.aid),raw)

    def test_failure_inspection_is_portable_read_only_and_guarded_to_the_same_attempt(self):
        self.seed()
        command=join.completion_harvest_command(self.aid,'inspect-done-failure',jobs=str(self.jobs),surface='/adapters/codex/bin/preflight.sh')
        self.assertNotIn('/adapters/',command);self.assertIn('observed-status',command)
        import shlex
        before=self.jobs.read_bytes()
        result=subprocess.run(shlex.split(command),cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.jobs.read_bytes(),before)
        self.assertEqual(join.harvest_command_lines(command),[command])
        action=join.classify_supervised_shell_command(base=ROOT,command=command,open_attempt_ids={self.aid},parent_slug='audit',jobs=self.jobs)
        self.assertIsNotNone(action);self.assertFalse(action.mark_done)
        self.assertIsNone(join.classify_supervised_shell_command(base=ROOT,command=command+' --apply',open_attempt_ids={self.aid},parent_slug='audit',jobs=self.jobs))
        self.assertIsNone(join.classify_supervised_shell_command(base=ROOT,command=command,open_attempt_ids={'different'},parent_slug='audit',jobs=self.jobs))


if __name__=='__main__':unittest.main()
