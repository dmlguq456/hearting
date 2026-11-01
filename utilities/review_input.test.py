#!/usr/bin/env python3
"""SD-161 authority and drift fences; no model or installed-state access."""
import argparse
import hashlib
import importlib.util
import json
import contextlib
import io
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import dispatch_contract as DC
import review_input as R


class ReviewInputTest(unittest.TestCase):
    def setUp(self):
        # Refusal assertions exercise the opt-in integrity fence explicitly;
        # gate-off continuation has its own tests that set `off` locally.
        gates = mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'})
        gates.start()
        self.addCleanup(gates.stop)
        import opencode_context_window
        from fixtures.opencode_native_context import launch_context
        native = mock.patch.object(opencode_context_window, "prepare_launch_context", side_effect=launch_context)
        native.start()
        self.addCleanup(native.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs.log'
        self.jobs.touch()
        self.evidence = self.root / 'plan.md'
        self.evidence.write_text('original plan')
        self.node = {'id': 'plan-check', 'kind': 'review-worker', 'unit': 'qa/plan-review', 'depends_on': []}
        self.route = {'route_id': 'rt-review', 'route_hash': 'sha256:route', 'nodes': [self.node], 'effective_intensity': 'standard'}
        self.route_file = self.root / 'route.json'
        self.route_file.write_text(json.dumps(self.route))
        self.meta = {'attempt_id': 'att-review', 'route_id': self.route['route_id'],
                     'route_hash': self.route['route_hash'], 'route_node': self.node['id'],
                     'unit': self.node['unit'], 'route_file': str(self.route_file)}
        # These unit fixtures isolate input fences; real route verification is
        # exercised by the public node/chain entrypoint suite.
        # The registration fence and the preview both read dispatch-node's one
        # `admit_round`; isolate only its route-lineage/row/revision sources.
        self.node_module = R._dispatch_node_module()
        self.rounds = []
        self.revisions = []
        route_module = self.node_module.ROUTE
        for name, fake in (("review_lineage_routes", lambda route, node: [route]),
                           ("review_round_records", lambda *args, **kwargs: list(self.rounds)),
                           ("_dependency_revisions", lambda *args, **kwargs: list(self.revisions))):
            patcher = mock.patch.object(route_module, name, side_effect=fake)
            patcher.start(); self.addCleanup(patcher.stop)

    def seal(self, **kwargs):
        candidate = R.resolve_input(self.route, self.node, self.jobs, self.evidence)
        self.meta[R.KEY] = R.seal_binding(self.jobs, self.meta, candidate, **kwargs)
        return candidate

    def test_plan_check_group_variants_share_the_exact_review_input_resolver(self):
        variant = {**self.node, 'id': 'plan-check-simplicity',
                   'unit': 'qa/simplicity-check', 'completion_gate': 'code-plan-check'}
        self.assertTrue(R.is_plan_review_node(variant))
        self.assertTrue(R.is_review_node(variant))
        self.assertEqual(R.resolve_input(self.route, variant, self.jobs, self.evidence)['sha256'],
                         hashlib.sha256(b'original plan').hexdigest())

    def args(self, **updates):
        return SimpleNamespace(**(dict(self.meta, jobs_path=self.jobs,
                               reviewed_evidence=str(self.evidence), automatic_retry_of=None) | updates))

    def assert_reason(self, reason, fn, *args, **kwargs):
        with self.assertRaises(DC.DispatchContractError) as error:
            fn(*args, **kwargs)
        self.assertEqual(error.exception.reason, reason, str(error.exception))

    def test_producerless_requires_explicit_input_without_mutation(self):
        self.assert_reason('reviewed-evidence-required', R.resolve_input,
                           self.route, self.node, self.jobs)
        self.assertEqual(self.jobs.read_bytes(), b'')
        self.assertFalse((self.root / 'review-inputs').exists())

    def test_producerless_review_reads_the_plan_compose_sealed_from_an_earlier_cycle(self):
        earlier = self.root / 'campaigns' / 'c' / 'cyc' / 'artifacts' / 'plans'
        earlier.mkdir(parents=True)
        (earlier / 'plan.md').write_text('the earlier cycle plan')
        route = dict(self.route, artifact_root=str(self.root))
        node = dict(self.node, inputs=['plan.md', 'checklist.md'],
                    input_sources={'plan.md': {'cycle_id': 'cyc', 'path': 'campaigns/c/cyc/artifacts/plans/plan.md'}})
        candidate = R.resolve_input(route, node, self.jobs)
        self.assertEqual(candidate['path'], str((earlier / 'plan.md').resolve()))
        self.assertEqual(candidate['sha256'], hashlib.sha256(b'the earlier cycle plan').hexdigest())
        self.assertEqual(R.resolve_input(route, node, self.jobs, self.evidence)['path'],
                         str(self.evidence.resolve()))                 # an explicit file still wins
        (earlier / 'plan.md').unlink()
        self.assert_reason('reviewed-evidence-unreadable', R.resolve_input, route, node, self.jobs)

    def test_parallel_review_identity_uses_unit_not_literal_node_name(self):
        node = dict(self.node, id='plan-review-alternative')
        self.assertTrue(R.is_review_node(node))
        self.assert_reason('reviewed-evidence-required', R.resolve_input, self.route, node, self.jobs)
        self.assertIsNone(R.resolve_input(self.route, {'id': 'execute'}, self.jobs))

    def test_evidence_given_for_a_node_that_is_not_a_plan_review_is_ignored_not_refused(self):
        # Codex r4: impl-review (qa/code-review) was refused `reviewed-evidence-node-invalid`.
        code_review = {'id': 'impl-review', 'kind': 'review-worker', 'unit': 'qa/code-review'}
        self.assertIsNone(R.resolve_input(self.route, code_review, self.jobs, self.evidence))
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(R.drop_inapplicable(code_review, str(self.evidence)))
            self.assertEqual(R.drop_inapplicable(self.node, str(self.evidence)), str(self.evidence))
            self.assertIsNone(R.drop_inapplicable(code_review, None))
        self.assertEqual(out.getvalue(), 'reviewed_evidence_ignored=impl-review\n')

    def test_spec_review_accepts_explicit_evidence_and_keeps_no_input_legacy_path(self):
        node = {'id': 'review', 'kind': 'review-worker', 'unit': 'research/plan-review',
                'completion_gate': 'spec-review', 'depends_on': ['research']}
        route = {'route_id': 'rt-spec-review', 'route_hash': 'sha256:spec-route',
                 'nodes': [{'id': 'research'}, node], 'effective_intensity': 'standard'}
        candidate = R.resolve_input(route, node, self.jobs, self.evidence)
        self.assertEqual(candidate['path'], str(self.evidence.resolve()))
        self.assertEqual(candidate['sha256'], hashlib.sha256(self.evidence.read_bytes()).hexdigest())
        self.assertIsNone(R.resolve_input(route, node, self.jobs))
        route_file = self.root / 'spec-route.json'
        route_file.write_text(json.dumps(route))
        args = SimpleNamespace(attempt_id='att-spec-review', route_file=str(route_file),
            route_id=route['route_id'], route_hash=route['route_hash'], route_node='review',
            unit='research/plan-review', jobs_path=self.jobs,
            reviewed_evidence=str(self.evidence), automatic_retry_of=None)
        self.assertEqual(R.prepare_request(args)['sha256'], candidate['sha256'])
        fragment = R.registration_fragment(args)
        metadata = dict(attempt_id=args.attempt_id, route_id=args.route_id,
            route_hash=args.route_hash, route_node=args.route_node, unit=args.unit,
            **DC.parse_registry_metadata(fragment))
        self.assertEqual(R.validate_launch(self.jobs, metadata)['sha256'], candidate['sha256'])
        no_input = SimpleNamespace(attempt_id='att-spec-legacy', route_file=str(route_file),
            route_id=route['route_id'], route_hash=route['route_hash'], route_node='review',
            unit='research/plan-review', jobs_path=self.jobs, reviewed_evidence=None,
            automatic_retry_of=None)
        self.assertIsNone(R.prepare_request(no_input))
        self.assertIsNone(R.validate_launch(self.jobs, {
            'attempt_id': no_input.attempt_id, 'route_id': route['route_id'],
            'route_hash': route['route_hash'], 'route_node': 'review', 'unit': no_input.unit,
        }))

    def _spec_retry(self, *, route_id='rt-spec-retry', binding=None):
        node = {'id': 'review', 'kind': 'review-worker', 'unit': 'research/plan-review',
                'completion_gate': 'spec-review', 'depends_on': ['research']}
        route = {'route_id': route_id, 'route_hash': 'sha256:spec-retry-route',
                 'nodes': [{'id': 'research'}, node], 'effective_intensity': 'standard'}
        metadata = {'attempt_id': 'att-spec-predecessor', 'route_id': route_id,
                    'route_hash': route['route_hash'], 'route_node': 'review'}
        if binding is not None:
            metadata[R.KEY] = binding
        self.jobs.write_text('now\tdone\twt\tbranch\tslug\t' + ','.join(
            f'{key}={value}' for key, value in metadata.items()) + '\n')
        return route, node

    def test_spec_retry_preserves_existing_binding_integrity_errors(self):
        route, node = self._spec_retry(binding='sha256:stale-registry-digest')
        bindings = self.root / 'review-inputs'
        bindings.mkdir()
        (bindings / 'att-spec-predecessor.json').write_text('{corrupt')
        self.assert_reason('reviewed-evidence-unproven', R.resolve_input,
                           route, node, self.jobs, self.evidence, retry_of='att-spec-predecessor')

    def test_spec_retry_does_not_replace_predecessor_whose_original_bytes_changed(self):
        route, node = self._spec_retry()
        original = R.resolve_input(route, node, self.jobs, self.evidence)
        predecessor = {'attempt_id': 'att-spec-predecessor', 'route_id': route['route_id'],
                       'route_hash': route['route_hash'], 'route_node': node['id']}
        binding_digest = R.seal_binding(self.jobs, predecessor, original)
        self._spec_retry(binding=binding_digest)
        self.evidence.write_text('replacement bytes')
        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
            self.assert_reason('reviewed-evidence-changed', R.resolve_input,
                               route, node, self.jobs, self.evidence, retry_of='att-spec-predecessor')

    def test_spec_retry_legacy_absent_binding_allows_no_input_and_explicit_input(self):
        route, node = self._spec_retry()
        self.assertIsNone(R.resolve_input(route, node, self.jobs, retry_of='att-spec-predecessor'))
        candidate = R.resolve_input(route, node, self.jobs, self.evidence,
                                    retry_of='att-spec-predecessor')
        self.assertEqual(candidate['sha256'], hashlib.sha256(self.evidence.read_bytes()).hexdigest())

    def test_spec_retry_missing_claimed_binding_is_unproven(self):
        route, node = self._spec_retry(binding='sha256:registered-binding')
        self.assert_reason('reviewed-evidence-unproven', R.resolve_input,
                           route, node, self.jobs, self.evidence,
                           retry_of='att-spec-predecessor')

    def test_spec_retry_wrong_route_predecessor_is_not_legacy_absence(self):
        route, node = self._spec_retry(route_id='rt-current')
        self._spec_retry(route_id='rt-other')
        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'on'}):
            self.assert_reason('reviewed-evidence-replacement-mismatch', R.resolve_input,
                               route, node, self.jobs, self.evidence, retry_of='att-spec-predecessor')

    def test_spec_retry_wrong_route_bound_predecessor_keeps_gates_off_continuation(self):
        predecessor_route, node = self._spec_retry(route_id='rt-other')
        predecessor = {'attempt_id': 'att-spec-predecessor',
                       'route_id': predecessor_route['route_id'],
                       'route_hash': predecessor_route['route_hash'], 'route_node': node['id']}
        candidate = R.resolve_input(predecessor_route, node, self.jobs, self.evidence)
        binding_digest = R.seal_binding(self.jobs, predecessor, candidate)
        self._spec_retry(route_id='rt-other', binding=binding_digest)
        route = {'route_id': 'rt-current', 'route_hash': predecessor_route['route_hash'],
                 'nodes': [{'id': 'research'}, node], 'effective_intensity': 'standard'}
        replacement = self.root / 'replacement.md'
        replacement.write_text('baseline gates-off continuation')

        with mock.patch.dict(os.environ, {'HEARTING_GATES': 'off'}):
            resolved = R.resolve_input(route, node, self.jobs, replacement,
                                       retry_of='att-spec-predecessor')

        self.assertEqual(resolved['path'], str(replacement.resolve()))
        self.assertEqual(resolved['sha256'], hashlib.sha256(replacement.read_bytes()).hexdigest())

    def test_binding_is_exact_write_once_and_historical_read_survives_edit(self):
        candidate = self.seal()
        self.assertEqual(R.seal_binding(self.jobs, self.meta, candidate), self.meta[R.KEY])
        self.evidence.write_text('corrected plan')
        historical = R.read_binding(self.jobs, self.meta)
        self.assertEqual(historical['sha256'], candidate['sha256'])
        self.assert_reason('reviewed-evidence-changed', R.read_binding,
                           self.jobs, self.meta, verify_current=True)
        corrected = R.resolve_input(self.route, self.node, self.jobs, self.evidence)
        self.assert_reason('reviewed-evidence-binding-conflict', R.seal_binding,
                           self.jobs, self.meta, corrected)

    def test_binding_rejects_forged_identity_digest_and_symlink(self):
        self.seal()
        for key in ('route_hash', 'route_node', R.KEY):
            self.assert_reason('reviewed-evidence-binding-mismatch', R.read_binding,
                               self.jobs, dict(self.meta, **{key: 'forged'}))
        path = self.root / 'review-inputs' / 'att-review.json'
        saved = self.root / 'saved.json'
        path.rename(saved)
        path.symlink_to(saved)
        self.assert_reason('reviewed-evidence-binding-symlink', R.read_binding, self.jobs, self.meta)

    def test_registration_rechecks_preview_bytes_and_prompt_names_input(self):
        args = self.args()
        R.prepare_request(args)
        self.assertIn(str(self.evidence), R.prompt_block(args))
        self.evidence.write_text('changed before registration')
        self.assert_reason('reviewed-evidence-changed', R.registration_fragment, args)
        self.assertFalse((self.root / 'review-inputs').exists())

    def test_registration_records_document_before_claim_and_launch_rechecks(self):
        args = self.args()
        fragment = R.registration_fragment(args)
        metadata = dict(self.meta, **DC.parse_registry_metadata(fragment))
        self.assertEqual(R.validate_launch(self.jobs, metadata)['path'], str(self.evidence))
        self.evidence.write_text('changed before spawn')
        self.assert_reason('reviewed-evidence-changed', R.validate_launch, self.jobs, metadata)
        self.assertEqual(self.jobs.read_bytes(), b'')

    def test_new_review_without_binding_refused_but_legacy_join_metadata_untouched(self):
        self.assert_reason('reviewed-evidence-unproven', R.validate_launch, self.jobs, self.meta)
        self.assertIsNone(R.validate_launch(self.jobs, {'unit': 'dev/backend'}))

    def test_plan_producer_auto_binding_and_explicit_match(self):
        plan = {'id': 'plan'}
        route = dict(self.route, nodes=[plan, self.node])
        self.node['depends_on'] = ['plan', 'plan-replica']
        marker = {'attempt_id': 'att-plan', 'evidence': {
            'path': str(self.evidence), 'sha256': hashlib.sha256(self.evidence.read_bytes()).hexdigest()}}
        marker_path = self.root / 'completion' / self.route['route_id'] / 'plan.json'
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text(json.dumps(marker))
        with mock.patch.object(DC, 'resolve_dispatch_state_root', return_value=self.root), \
                mock.patch.object(DC, 'completion_marker_is_current', return_value=True):
            auto = R.resolve_input(route, self.node, self.jobs)
            self.assertEqual(auto, R.resolve_input(route, self.node, self.jobs, self.evidence))
            self.assertEqual(auto['producer']['attempt_id'], 'att-plan')
            other = self.root / 'other.md'; other.write_text('other')
            self.assert_reason('reviewed-evidence-producer-mismatch', R.resolve_input,
                               route, self.node, self.jobs, other)
        with mock.patch.object(DC, 'resolve_dispatch_state_root', return_value=self.root), \
                mock.patch.object(DC, 'completion_marker_is_current', return_value=False):
            self.assert_reason('reviewed-evidence-producer-unproven', R.resolve_input,
                               route, self.node, self.jobs)

    def test_replacement_copies_original_authority_not_new_plan_marker(self):
        original = self.seal()
        self.jobs.write_text('now\tdone\twt\tbranch\tslug\t' + ','.join(k+'='+v for k,v in self.meta.items())+'\n')
        self.node['depends_on'] = ['plan']
        self.route['nodes'].append({'id': 'plan'})
        self.route_file.write_text(json.dumps(self.route))
        args = self.args(attempt_id='att-replacement', automatic_retry_of='att-review')
        with mock.patch.object(DC, 'completion_marker_is_current', side_effect=AssertionError('must not rebind')):
            R.prepare_request(args)
            digest = DC.parse_registry_metadata(R.registration_fragment(args))[R.KEY]
        meta = dict(self.meta, attempt_id='att-replacement', **{R.KEY: digest})
        replacement = R.read_binding(self.jobs, meta, verify_current=True)
        self.assertEqual(replacement['sha256'], original['sha256'])
        self.assertEqual(replacement['source'], {'attempt_id':'att-review', 'binding_digest': self.meta[R.KEY]})
        self.evidence.write_text('silently substituted')
        self.assert_reason('reviewed-evidence-changed', R.prepare_request,
                           self.args(attempt_id='att-other', automatic_retry_of='att-review'))

    def test_common_registration_and_spawn_fences_prevent_any_child(self):
        current = {'attempt_schema_version': '2', 'dispatch_depth': '2', 'transport': 'headless',
                   'execution_surface': 'registered-headless', 'registered_worker': '1',
                   'fallback_hop': 'same-harness-headless', 'launch_claimed': '0'}
        def row(meta):
            return 'now\topen\t/repo\t/wt\treview\t'+','.join(k+'='+v for k,v in meta.items())
        metadata = dict(current, **self.meta)
        self.assert_reason('reviewed-evidence-unproven', DC.claim_attempt_row,
                           self.jobs, self.meta['attempt_id'], row(metadata))
        self.assertEqual(self.jobs.read_bytes(), b'')
        self.seal()
        metadata.update(self.meta)
        self.jobs.write_text(row(metadata)+'\n')
        self.evidence.write_text('changed after register')
        before = self.jobs.read_bytes()
        spawn = mock.Mock(side_effect=AssertionError('must not spawn'))
        self.assert_reason('reviewed-evidence-changed', DC.spawn_claimed_attempt,
                           self.jobs, self.meta['attempt_id'], parent_binding=None, spawn=spawn)
        spawn.assert_not_called()
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_wrong_input_cannot_borrow_a_recorded_correction_permission(self):
        spec = importlib.util.spec_from_file_location('sd161_dispatch_node', R.ROOT/'utilities/dispatch-node.py')
        node_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(node_module)
        self.route['effective_intensity'] = 'standard'
        self.route['continuation_budget'] = {'review_round_cap': 1}
        self.route_file.write_text(json.dumps(self.route))
        original = self.seal()
        previous = dict(self.meta, note='completed-review-blocking', worker_type='review')
        corrected = self.root/'corrected.md'; corrected.write_text('corrected input B')
        candidate = R.resolve_input(self.route, self.node, self.jobs, corrected)
        revision = {'input_binding_digest': self.meta[R.KEY], 'answers': ['att-review'], 'evidence': candidate}
        module = node_module.ROUTE
        with mock.patch.object(node_module, 'prior_round_attempts', return_value=[(['now','done'], previous)]), \
                mock.patch.object(node_module, '_auto_record_revisions', return_value=()), \
                mock.patch.object(module, 'review_lineage_routes', return_value=[self.route]), \
                mock.patch.object(module, '_review_input_revision_records', return_value=[revision]), \
                mock.patch.object(module, 'publish_review_input_revision', return_value={'input_revision':revision}):
            stale = node_module.admit_round(self.route, self.node, self.jobs, reviewed_evidence=self.evidence)
            fresh = node_module.admit_round(self.route, self.node, self.jobs, reviewed_evidence=corrected)
        self.assertEqual(stale.budget.state, 'exhausted')
        self.assertEqual(fresh.budget.state, 'admit')
        # B is a different content subject with its own first independent
        # budget. The former input-revision closure permission remains for
        # that input family but cannot relabel a new content review.
        self.assertEqual(fresh.budget.round_kind, 'first')
        # The fence reads the same admission as the preview, so the former
        # permission is still not borrowed by the exhausted original input.
        (self.root/'review-input-revisions').mkdir()
        patches = (mock.patch.object(R, '_dispatch_node_module', return_value=node_module),
                   mock.patch.object(node_module, 'prior_round_attempts', return_value=[(['now','done'], previous)]),
                   mock.patch.object(node_module, '_auto_record_revisions', return_value=()),
                   mock.patch.object(module, 'review_lineage_routes', return_value=[self.route]),
                   mock.patch.object(module, 'preview_review_input_revision', return_value={'input_revision':revision}),
                   mock.patch.object(module, '_review_input_revision_records', return_value=[revision]))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            self.assert_reason('reviewed-evidence-revision-not-admitted', R.validate_revision_admission,
                               self.jobs, dict(self.meta, attempt_id='att-new'), original)
            # Between route admission and wrapper registration, B itself changes.
            # The launch fence must not accept the new bytes against B's old record.
            new_meta = dict(self.meta, attempt_id='att-new')
            new_meta[R.KEY] = R.seal_binding(self.jobs, new_meta, candidate)
            corrected.write_text('unapproved bytes C')
            self.assert_reason('reviewed-evidence-changed', R.validate_launch, self.jobs, new_meta)

    def test_parallel_review_cap_applies_without_any_input_revision_directory(self):
        import review_round_cap
        self.node['id']='plan-check-alternative';self.meta['route_node']=self.node['id']
        self.route_file.write_text(json.dumps(self.route))
        candidate=self.seal()
        rounds = self.rounds
        self.assertFalse((self.root/'review-input-revisions').exists())
        for number in (1,2,3,4):
            current=dict(self.meta,attempt_id=f'att-next-{number}')
            if number<=2:
                R.validate_revision_admission(self.jobs,current,candidate)
            else:
                self.assert_reason('reviewed-evidence-revision-not-admitted',
                    R.validate_revision_admission,self.jobs,current,candidate)
            prior = dict(self.meta, attempt_id=f'att-r{number}',
                         worker_type='review', note='completed-review-blocking')
            prior[R.KEY] = R.seal_binding(self.jobs, prior, candidate)
            rounds.append((['now','done','', '', f'slug-{number}'], prior))

    def _sealed_verdict_rows(self, candidate, count):
        for number in range(1, count + 1):
            prior = dict(self.meta, attempt_id=f'att-r{number}', worker_type='review',
                         note='completed-review-blocking')
            prior[R.KEY] = R.seal_binding(self.jobs, prior, candidate)
            self.rounds.append((['now', 'done', '', '', f'slug-{number}'], prior))

    def test_preview_and_fence_share_one_round_admission(self):
        candidate = self.seal()
        preview = SimpleNamespace(**dict(self.meta, action='dry-run', jobs_path=self.jobs, command_attempt_id=None,
                                         reviewed_evidence=str(self.evidence), automatic_retry_of=None,
                                         parent_attempt_id=None, attempt_id='att-next'))
        for count, state in ((0, 'admit'), (1, 'admit'), (2, 'exhausted')):
            self.rounds.clear(); self._sealed_verdict_rows(candidate, count)
            seen = []
            real = R._round_admission
            def spy(*args, **kwargs):
                seen.append(kwargs)
                return real(*args, **kwargs)
            with mock.patch.object(R, '_round_admission', side_effect=spy):
                if state == 'admit':
                    admitted = R.preview_request_admission(preview, self.jobs)
                    R.validate_revision_admission(self.jobs, dict(self.meta, attempt_id='att-next'), candidate)
                    self.assertEqual(admitted.budget.state, 'admit')
                else:
                    self.assert_reason('reviewed-evidence-revision-not-admitted',
                                       R.preview_request_admission, preview, self.jobs)
                    self.assert_reason('reviewed-evidence-revision-not-admitted', R.validate_revision_admission,
                                       self.jobs, dict(self.meta, attempt_id='att-next'), candidate)
            self.assertEqual(len(seen), 2, (count, seen))             # one decision, two callers
            self.assertEqual(seen[0], seen[1], (count, seen))           # over the same facts
            self.assertEqual(seen[0]['reviewed_evidence'], str(self.evidence))

    def test_round_admission_still_refuses_exhausted_without_revision(self):
        candidate = self.seal()
        self._sealed_verdict_rows(candidate, 2)
        preview = SimpleNamespace(**dict(self.meta, action='dry-run', jobs_path=self.jobs, command_attempt_id=None,
                                         reviewed_evidence=str(self.evidence), automatic_retry_of=None,
                                         parent_attempt_id=None, attempt_id='att-next'))
        for call in (lambda: R.preview_request_admission(preview, self.jobs),
                     lambda: R.validate_revision_admission(self.jobs, dict(self.meta, attempt_id='att-next'), candidate)):
            self.assert_reason('reviewed-evidence-revision-not-admitted', call)
        # A sealed subject that no longer matches the observed one is unproven,
        # not a verdict on this attempt; the same digest follows the admission.
        admitted = SimpleNamespace(budget=SimpleNamespace(state='admit'), source_subject_digest='sha256:now')
        sealed = {'subject_digest': 'sha256:sealed'}
        import review_source_subject as subject
        with mock.patch.object(R, '_round_admission', return_value=admitted), \
                mock.patch.object(subject, 'read_binding', return_value=sealed):
            self.assert_reason('reviewed-evidence-revision-unproven', R.validate_revision_admission,
                               self.jobs, dict(self.meta, attempt_id='att-next'), candidate)
            sealed['subject_digest'] = 'sha256:now'
            R.validate_revision_admission(self.jobs, dict(self.meta, attempt_id='att-next'), candidate)
            admitted.budget.state = 'exhausted'
            self.assert_reason('reviewed-evidence-revision-not-admitted', R.validate_revision_admission,
                               self.jobs, dict(self.meta, attempt_id='att-next'), candidate)

    def test_unrecorded_planned_revision_cannot_start(self):
        # The fence may count a planned (unrecorded) revision, but a real start
        # records it first; an unrecorded one is still refused at the start gate.
        agent_home = self.root / 'home'
        marker_dir = agent_home / 'completion' / 'rt-planned'
        marker_dir.mkdir(parents=True)
        (marker_dir / 'plan.json').write_text('{}')
        route = {'route_id': 'rt-planned', 'route_hash': 'sha256:planned', 'dispatch_contract_version': 3,
                 'nodes': [{'id': 'plan', 'kind': 'stage'},
                           {'id': 'plan-check', 'kind': 'review-worker', 'unit': 'qa/plan-review',
                            'depends_on': ['plan']}]}
        route_file = self.root / 'planned-route.json'
        route_file.write_text(json.dumps(route))
        unrecorded = SimpleNamespace(state='revised-unrecorded', reason='completion-evidence-revised-unrecorded',
                                     next_action=None)
        with mock.patch.object(DC, 'dispatch_state_roots', return_value=(agent_home,)), \
                mock.patch.object(DC, '_launch_completion_input'), \
                mock.patch.object(DC, 'completion_marker_is_current', return_value=False), \
                mock.patch.object(DC, 'gate_currency', return_value=unrecorded):
            self.assert_reason('completion-evidence-revised-unrecorded', DC.completion_marker_gate,
                               str(route_file), 'plan-check', 'start', agent_home, self.jobs,
                               planned_revision_nodes=frozenset({'plan'}))

    def test_three_wrapper_mains_seal_input_and_refuse_missing_or_drifted_input(self):
        # Exercise actual parser -> main -> append_job -> common claim wiring.
        # Runtime/root/model probes are fixture stubs, covered by their adapter
        # suites; neither the input helper nor the registration writer is mocked.
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.email','fixture@example.com'],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.name','Fixture'],check=True)
        subprocess.run(['git','-C',str(self.root),'add','plan.md'],check=True)
        subprocess.run(['git','-C',str(self.root),'commit','-qm','fixture'],check=True)
        self.route.update(capability='autopilot-code',capability_mode='dev',cwd=str(self.root),artifact_root=str(self.root))
        self.node.update(dispatch_depth=1,completion_gate='code-plan-check',write_scope=['*'])
        import route_identity
        self.route['route_hash']=route_identity.route_hash(self.route)
        self.route['route_id']=route_identity.route_id_from_hash(self.route['route_hash'])
        self.meta.update(route_id=self.route['route_id'],route_hash=self.route['route_hash'])
        self.route_file.write_text(json.dumps(self.route))
        for harness in ('codex','claude','opencode'):
            with self.subTest(harness=harness),contextlib.ExitStack() as stack:
                self.route_file.write_text(json.dumps(self.route))
                path=R.ROOT/'adapters'/harness/'bin/dispatch-headless.py'
                spec=importlib.util.spec_from_file_location('input_main_'+harness,path)
                wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
                jobs=self.root/(harness+'-jobs.log');jobs.touch()
                evidence=self.root/(harness+'-input.md');evidence.write_text('input v1')
                environment={k:v for k,v in os.environ.items() if not k.startswith(('AGENT_','CODEX_','CLAUDE_'))}
                environment.update(AGENT_HOME=str(R.ROOT),AGENT_DISPATCH_JOBS=str(jobs),
                    AGENT_ARTIFACT_ROOT=str(self.root),AGENT_MODEL_GOVERNOR_ROOT=str(self.root/'.runtime/model-worker-governor'),
                    OPENCODE_CONFIG_CONTENT='{}')
                stack.enter_context(mock.patch.dict(os.environ,environment,clear=True))
                stack.enter_context(mock.patch.object(wrapper,'resolve_artifact_root',return_value=str(self.root)))
                stack.enter_context(mock.patch.object(wrapper,'validate_route_record',side_effect=lambda args:setattr(args,'route_validation',{}) or 0))
                stack.enter_context(mock.patch.object(wrapper,'completion_marker_gate'))
                stack.enter_context(mock.patch.object(wrapper,'headless_attempt_policy',return_value={
                    'fallback_hop':'same-harness-headless','fallback_ordinal':1,'quick':False,
                    'terminal_attempt_limit':None,'replacement_attempt_limit':0,'replacement_notes':frozenset()}))
                stack.enter_context(mock.patch.object(wrapper.shutil,'which',return_value='/bin/true'))
                if harness == 'opencode':
                    # Keep this input-fence test independent of an installed CLI.
                    stack.enter_context(mock.patch.object(wrapper.opencode_runtime,
                        'probe_launch_version',return_value=(1,19,0)))
                stack.enter_context(mock.patch.object(wrapper,'shell_command',return_value='true'))
                for name in ('check_runtime_projection','ensure_runtime_home_projection'):
                    if hasattr(wrapper,name):stack.enter_context(mock.patch.object(wrapper,name,return_value=0 if name=='check_runtime_projection' else None))
                spawn=stack.enter_context(mock.patch.object(wrapper,'spawn_claimed_attempt',side_effect=AssertionError('unexpected model spawn')))
                argv=['dispatch-headless.py','--register','--worktree',str(self.root),'--jobs',str(jobs),
                    '--slug','input-'+harness,'--attempt-id','att-input-'+harness,'--capability','autopilot-code',
                    '--capability-mode','dev','--worker-type','review','--worker-mode','qa/plan-review',
                    '--unit','qa/plan-review','--dispatch-depth','1','--intensity','standard',
                    '--route-file',str(self.route_file),'--route-id',self.route['route_id'],
                    '--route-hash',self.route['route_hash'],'--route-node',self.node['id'],
                    '--model','test','--parent-harness','claude','--parent-transport','headless',
                    '--parent-sandbox','default',
                    {'codex':'--reasoning','claude':'--effort','opencode':'--variant'}[harness],'low']
                def run(arguments):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        code=wrapper.main(arguments)
                    return code,output.getvalue()
                code,output=run(argv)
                self.assertNotEqual(code,0,output);self.assertIn('reviewed-evidence-required',output)
                self.assertEqual(jobs.read_bytes(),b'')
                argv+=['--reviewed-evidence',str(evidence)]
                code,output=run(argv)
                self.assertEqual(code,0,output)
                row=DC.parse_registry_metadata(jobs.read_text().splitlines()[0].split('\t')[5])
                self.assertEqual(R.read_binding(jobs,row)['path'],str(evidence))
                evidence.write_text('input v2 without a new attempt')
                argv[argv.index('--register')]='--start'
                code,output=run(argv)
                self.assertNotEqual(code,0,output)
                self.assertIn('reviewed-evidence-',output)
                self.assertIn('child_spawned=0',output)
                spawn.assert_not_called()

                # The exact autopilot-spec review node accepts optional input on
                # all siblings, while the same registered launch remains valid
                # with no input for its legacy behavior.
                spec_node={'id':'review','kind':'review-worker','unit':'research/plan-review',
                    'completion_gate':'spec-review','depends_on':['research'],'dispatch_depth':1,
                    'commit_expected':False}
                spec_route={**self.route,'capability':'autopilot-spec',
                    'nodes':[{'id':'research'},spec_node]}
                spec_route['route_hash']=route_identity.route_hash(spec_route)
                spec_route['route_id']=route_identity.route_id_from_hash(spec_route['route_hash'])
                self.route_file.write_text(json.dumps(spec_route))
                argv[argv.index('--start')]='--register'
                argv[argv.index('--worker-mode')+1]='research/plan-review'
                argv[argv.index('--unit')+1]='research/plan-review'
                argv[argv.index('--route-id')+1]=spec_route['route_id']
                argv[argv.index('--route-hash')+1]=spec_route['route_hash']
                argv[argv.index('--route-node')+1]='review'
                argv[argv.index('--slug')+1]='spec-'+harness
                argv[argv.index('--attempt-id')+1]='att-spec-'+harness
                argv=argv[:argv.index('--reviewed-evidence')]
                code,output=run(argv)
                self.assertEqual(code,0,output)
                unbound=DC.parse_registry_metadata(jobs.read_text().splitlines()[-1].split('\t')[5])
                self.assertNotIn(R.KEY,unbound)
                argv[argv.index('--attempt-id')+1]='att-spec-evidence-'+harness
                argv+=['--reviewed-evidence',str(evidence)]
                code,output=run(argv)
                self.assertEqual(code,0,output)
                bound=DC.parse_registry_metadata(jobs.read_text().splitlines()[-1].split('\t')[5])
                self.assertEqual(R.read_binding(jobs,bound)['path'],str(evidence))

    def test_all_three_wrapper_parsers_share_single_public_option(self):
        parser = argparse.ArgumentParser()
        R.add_arguments(parser)
        self.assertEqual(parser.parse_args(['--reviewed-evidence', str(self.evidence)]).reviewed_evidence,
                         str(self.evidence))
        for harness in ('codex', 'claude', 'opencode'):
            path = R.ROOT / 'adapters' / harness / 'bin' / 'dispatch-headless.py'
            spec = importlib.util.spec_from_file_location('review_wrapper_'+harness, path)
            wrapper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(wrapper)
            with mock.patch('sys.argv', [str(path), '--help']), mock.patch('sys.stdout'):
                with self.assertRaises(SystemExit) as caught:
                    wrapper.parser().parse_args()
            self.assertEqual(caught.exception.code, 0)

if __name__ == '__main__':
    unittest.main()
