#!/usr/bin/env python3
"""Merge regressions: revision previews use writer proof, keep durable state intact."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sd161_marker_fixture', ROOT/'utilities/dispatch_completion_marker.test.py')
FIX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FIX)
R, N = FIX.ROUTE, FIX.DISPATCH_NODE

class RevisionPreviewTest(FIX.CompletionMarkerTest):
    def snapshot(self):
        caches = {self.artifact/'.runtime/routes/.route-children-index.json',
                  self.artifact/'.runtime/artifact-producer/v1/cycles/.cycle-routes-index.json'}
        # Cache publication may change only these parent-directory mtimes.
        # Every durable child, lock and marker still has its own snapshot row.
        cache_dirs = {p.parent for p in caches}
        return {str(p.relative_to(self.base)): (p.is_dir(),
                None if p.is_dir() and not p.is_symlink() and p in cache_dirs else p.stat().st_mtime_ns,
                None if p.is_dir() else p.read_bytes())
                for p in self.base.rglob('*') if p not in caches or p.is_symlink() or not p.is_file()}

    def input_case(self, rounds=1, *, returned_content=False):
        route,node,output,owner = self.sd161_input_fixture()
        evidence = output/'preview-plan.md'
        evidence.write_text('input B\n' if returned_content else 'input A\n')
        for i in range(1,rounds+1):
            attempt=f'att-preview-r{i}'
            self.review_blocking_row(attempt,i,directory=output)
            self.sd161_bind_row(route,attempt,evidence)
        if returned_content:
            # A return to B must retain the B verdicts, unlike a first B input.
            evidence.write_text('input A\n')
            attempt='att-preview-intermediate'
            self.review_blocking_row(attempt,rounds+1,directory=output)
            self.sd161_bind_row(route,attempt,evidence)
        evidence.write_text('input B\n')
        return route,node,evidence,owner,attempt

    def test_preview_producer_cycle_scope_refuses_like_writer(self):
        import artifact_producer as P
        route,node=self._a_sd154_10_fixture('preview-cycle.json')
        with mock.patch.dict(os.environ,self.base_env(),clear=True):
            P.begin(self.artifact,route_file=self.base/'preview-cycle.json',
                    capability=route['capability'],intensity=route['effective_intensity'],require_cycle=True)
            with self.assertRaisesRegex(ValueError,'artifact-outside-bound-cycle'):
                R.publish_revision_locked(route,'plan',self.base/'plan.md',basis='review-findings',
                       answers=('att-plancheck-1',),author_attempt_id='att-owner-preview',jobs=self.jobs)
            before=self.snapshot()
            N.admit_round(route,node,self.jobs,owner_attempt_id='att-owner-preview',record_auto_revisions=False)
            self.assertEqual(before,self.snapshot())

    def test_preview_self_input_cap_plus_one_and_writer_agree_without_mutation(self):
        route,node,evidence,owner,attempt=self.input_case(rounds=2)
        Path(f'{self.jobs}.lock').unlink(missing_ok=True)
        with mock.patch.dict(os.environ,self.base_env(),clear=True):
            before=self.snapshot()
            preview=R.preview_review_input_revision(route,node['id'],evidence,
                answers=(attempt,),author_attempt_id=owner,jobs=self.jobs)
            admission=N.admit_round(route,node,self.jobs,owner_attempt_id=owner,
                reviewed_evidence=str(evidence),record_auto_revisions=False)
            self.assertEqual(admission.auto_revisions,())
            self.assertEqual(admission.budget.state,'admit')
            # The legacy raw-row cap+1 contract still holds. Content-aware
            # admission also proves the changed B input starts its own budget,
            # reviewed as a closure check of the lineage's blocking verdict.
            rows=N.prior_round_attempts(self.jobs,route['route_id'],node['id'],route=route)
            legacy=R.REVIEW_ROUND_CAP.round_budget(route,node,
                [(cols[1],meta) for cols,meta in rows],
                revisions=(preview['input_revision'],))
            self.assertEqual(legacy.round_kind,'closure-check')
            self.assertEqual(legacy.verdict_rounds,2)
            self.assertEqual(legacy.state,'admit')
            self.assertEqual(admission.budget.round_kind,'closure-check')
            self.assertEqual(admission.budget.verdict_rounds,0)
            self.assertEqual(admission.reviewed_input['path'],str(evidence))
            self.assertEqual(before,self.snapshot())
            written=R.publish_review_input_revision(route,node['id'],evidence,
                answers=(attempt,),author_attempt_id=owner,jobs=self.jobs)
            for result in (preview,written):result['input_revision'].pop('recorded_at')
            self.assertEqual(preview,written)
            started=N.admit_round(route,node,self.jobs,owner_attempt_id=owner,
                reviewed_evidence=str(evidence),record_auto_revisions=True)
            self.assertEqual(admission.budget,started.budget)

    def test_preview_self_input_third_verdict_cannot_authorize_fourth(self):
        route,node,evidence,owner,attempt=self.input_case(rounds=3,returned_content=True)
        with mock.patch.dict(os.environ,self.base_env(),clear=True):
            before=self.snapshot()
            admission=N.admit_round(route,node,self.jobs,owner_attempt_id=owner,
                reviewed_evidence=str(evidence),record_auto_revisions=False)
            self.assertEqual(admission.budget.state,'exhausted')
            self.assertEqual(before,self.snapshot())

    def test_preview_self_input_refusals_share_writer_without_mutation(self):
        route,node,evidence,owner,attempt=self.input_case()
        outside=self.base/'outside.md';outside.write_text('outside input\n')
        opts=dict(answers=(attempt,),author_attempt_id=owner,jobs=self.jobs)
        with mock.patch.dict(os.environ,{**self.base_env()},clear=True):
            for changes,path in (({'author_attempt_id':'att-wrong'},evidence),
                                 ({'answers':('att-wrong',)},evidence),({},outside)):
                errors=[]
                before=self.snapshot()
                for action in (R.preview_review_input_revision,R.publish_review_input_revision):
                    with self.assertRaises(Exception) as raised:
                        action(route,node['id'],path,**(opts|changes))
                    errors.append((type(raised.exception),str(raised.exception)))
                self.assertEqual(errors[0],errors[1])
                self.assertEqual(before,self.snapshot())

    def test_preview_self_input_terminal_and_history_refusals_match_writer(self):
        route,node,evidence,owner,attempt=self.input_case()
        opts=dict(answers=(attempt,),author_attempt_id=owner,jobs=self.jobs)
        log=self.logs/f'{attempt}.claude.jsonl';original=log.read_bytes()
        with mock.patch.dict(os.environ,self.base_env(),clear=True):
            log.write_text('truncated terminal envelope\n')
            before=self.snapshot()
            for action in (R.preview_review_input_revision,R.publish_review_input_revision):
                with self.assertRaisesRegex(ValueError,'verdict-unproven'):
                    action(route,node['id'],evidence,**opts)
            self.assertEqual(before,self.snapshot())
            log.write_bytes(original)
            history=self.jobs.parent/'review-input-revisions'/route['route_id']/node['id']
            history.mkdir(parents=True)
            (history/'000001.json').write_text('{"partial":')
            before=self.snapshot()
            for action in (R.preview_review_input_revision,R.publish_review_input_revision):
                with self.assertRaisesRegex(ValueError,'history-invalid'):
                    action(route,node['id'],evidence,**opts)
            self.assertEqual(before,self.snapshot())

    def assert_changed_evidence_broken_link(self, *, forge=False, revised=False):
        route,node=self._a_sd154_10_fixture('preview-broken-link.json')
        evidence=self.base/'plan.md'
        opts=dict(basis='review-findings',answers=('att-plancheck-1',),
                  author_attempt_id='att-owner-preview',jobs=self.jobs)
        with mock.patch.dict(os.environ,self.base_env(),clear=True):
            if revised:
                R.publish_revision_locked(route,'plan',evidence,**opts)
                evidence.write_text('plan v3 changed again\n')
            directory=R.completion_dir(route['route_id'],jobs=self.jobs)
            link=directory/'plan.att-plan-1.attempt.json'
            if forge:
                value=FIX.json.loads(link.read_text())
                value['completion_marker_history']=str(directory/'plan.999.json')
                link.write_text(FIX.json.dumps(value))
            else:
                link.unlink()
            before=self.snapshot()
            plan=next(n for n in route['nodes'] if n['id']=='plan')
            currency=FIX.D.gate_currency(route,plan,directory/'plan.json')
            self.assertEqual(currency.reason,'revision-predecessor-link-invalid')
            self.assertEqual(before,self.snapshot())
            markers_before={p.name:p.read_bytes() for p in directory.glob('*.json')}
            with self.assertRaisesRegex(ValueError,'revision-predecessor-link-invalid'):
                R.publish_revision_locked(route,'plan',evidence,**opts)
            self.assertEqual(markers_before,{p.name:p.read_bytes() for p in directory.glob('*.json')})
            # The real writer may create its ordinary serialization lock;
            # only preview is required to create no lock at all.
            before=self.snapshot()
            N.admit_round(route,node,self.jobs,
                owner_attempt_id='att-owner-preview',record_auto_revisions=False)
            gate_reasons=[]
            for action in ('dry-run','start'):
                with self.assertRaises(FIX.D.DispatchContractError) as raised:
                    FIX.D.completion_marker_gate(str(self.base/'preview-broken-link.json'),
                        node['id'],action,self.agent_home,self.jobs)
                gate_reasons.append(raised.exception.reason)
            self.assertEqual(gate_reasons,['completion-marker-integrity-broken']*2)
            self.assertEqual(before,self.snapshot())

    def test_preview_changed_evidence_missing_original_link_refuses_both_actions(self):
        self.assert_changed_evidence_broken_link()

    def test_preview_changed_evidence_forged_original_link_refuses_both_actions(self):
        self.assert_changed_evidence_broken_link(forge=True)

    def test_preview_changed_revision_checks_original_historical_link(self):
        self.assert_changed_evidence_broken_link(forge=True,revised=True)

if __name__=='__main__':
    # Only the added regressions; inherited tests remain in their own suite.
    names=[name for name in RevisionPreviewTest.__dict__ if name.startswith('test_preview_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(RevisionPreviewTest(n) for n in names))
    sys.exit(not result.wasSuccessful())
