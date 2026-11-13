#!/usr/bin/env python3
"""Review regressions for approved frame preparation and input delivery."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'utilities' / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

F = load('frame_revision_start_fixture', 'framed_start.test.py')
S = load('frame_revision_interview_fixture', 'work_start.test.py')

class RepositoryBoundary(F.FrameFrictionContinuationTest):
    def outside_repository(self):
        outside = self.root.parent / 'outside-approved-parent'; outside.mkdir()
        target = outside / 'new-project'; target.mkdir()
        subprocess.run(['git', 'init', '--quiet', str(target)], check=True)
        subprocess.run(['git', '-C', str(target), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit',
                        '--allow-empty', '-qm', 'Outside fixture'], check=True)
        return outside, target

    def swap_parent(self, outside):
        self.parent.rename(self.parent.with_name('original-parent'))
        self.parent.symlink_to(outside, target_is_directory=True)

    def assert_outside_untouched(self, target, receipt):
        worktrees = subprocess.check_output(['git', '-C', str(target), 'worktree',
                                             'list', '--porcelain'], text=True)
        self.assertEqual(worktrees.count('worktree '), 1)
        self.assertFalse(receipt.get('owner_started'), receipt)
        self.assertEqual(self.leg_calls, [])

    def test_parent_swap_after_preparation_cannot_allocate_or_start_outside(self):
        self.new_repository_frame()
        outside, target = self.outside_repository()
        original = F.W._prepare_frame_repository
        def swap(*args):
            result = original(*args)
            self.swap_parent(outside)
            return result
        with mock.patch.object(F.W, '_prepare_frame_repository', side_effect=swap):
            receipt = self.settle()
        self.assert_outside_untouched(target, receipt)
        self.assertEqual(self.leg_routes(), [])

    def test_parent_swap_during_compile_cannot_publish_or_start_outside(self):
        self.new_repository_frame()
        outside, target = self.outside_repository()
        module = F.W._route_module()
        original = module.compose_route
        def swap(**kwargs):
            result = original(**kwargs)
            if kwargs.get('selection_only') and not self.parent.is_symlink():
                self.swap_parent(outside)
            return result
        with mock.patch.object(module, 'compose_route', side_effect=swap):
            receipt = self.settle()
        self.assert_outside_untouched(target, receipt)
        self.assertEqual(self.leg_routes(), [])

    def checkpoint_swap(self, checkpoint):
        self.new_repository_frame()
        outside, target = self.outside_repository()
        def swap(name):
            if name == checkpoint:
                self.swap_parent(outside)
        with mock.patch.object(F.W, '_checkpoint', side_effect=swap):
            receipt = self.settle()
        self.assert_outside_untouched(target, receipt)

    def test_parent_swap_after_snapshot_stops_before_publication(self):
        self.checkpoint_swap('after-compiled-snapshot')
        self.assertEqual(self.leg_routes(), [])

    def test_parent_swap_after_publication_stops_before_start(self):
        self.checkpoint_swap('after-route-bind')

    def test_resumed_snapshot_reopens_the_same_physical_repository(self):
        self.new_repository_frame()
        outside, target = self.outside_repository()
        def interrupt(name):
            if name == 'after-compiled-snapshot':
                raise RuntimeError('fixture interrupted after snapshot')
        with mock.patch.object(F.W, '_checkpoint', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'fixture interrupted'):
                self.settle()
        self.assertIn('snapshot', self.record()['first_leg'])
        self.swap_parent(outside)
        receipt = self.settle()
        self.assert_outside_untouched(target, receipt)
        self.assertEqual(self.leg_routes(), [])

    def test_swap_during_launch_preparation_stops_before_the_launcher(self):
        self.new_repository_frame()
        outside, target = self.outside_repository()
        original = F.W.prepare_task_request
        def swap(*args, **kwargs):
            result = original(*args, **kwargs)
            self.swap_parent(outside)
            return result
        with mock.patch.object(F.W, 'prepare_task_request', side_effect=swap):
            receipt = self.settle()
        self.assert_outside_untouched(target, receipt)

    def test_registered_parent_binding_survives_recheck_after_approval(self):
        self.new_repository_frame()
        self.interview = F.W._interview_repository_preparation(self.route, self.jobs, self.interview, {})
        self.parent.rename(self.parent.with_name('original-parent'))
        self.parent.mkdir()
        result = self.settle()
        self.assertFalse(self.destination.exists())
        self.assertFalse(result.get('owner_started'))

    def test_parent_symlink_swap_before_preparation_cannot_initialize_outside(self):
        self.new_repository_frame()
        outside = self.root.parent / 'outside'; outside.mkdir()
        original = F.W._prepare_frame_repository
        def swap(*args):
            self.parent.rename(self.parent.with_name('original-parent'))
            self.parent.symlink_to(outside, target_is_directory=True)
            return original(*args)
        with mock.patch.object(F.W, '_prepare_frame_repository', side_effect=swap):
            result = self.settle()
        self.assertFalse((outside / 'new-project/.git').exists())
        self.assertFalse(result.get('owner_started'))
        self.assertEqual(self.leg_routes(), [])

    def test_parent_directory_swap_before_preparation_cannot_initialize_replacement(self):
        self.new_repository_frame()
        original = F.W._prepare_frame_repository
        def swap(*args):
            self.parent.rename(self.parent.with_name('original-parent'))
            self.parent.mkdir()
            return original(*args)
        with mock.patch.object(F.W, '_prepare_frame_repository', side_effect=swap):
            result = self.settle()
        self.assertFalse(self.destination.exists())
        self.assertFalse(result.get('owner_started'))

    def test_parent_swap_between_mkdir_and_init_never_redirects_git(self):
        self.new_repository_frame()
        outside = self.root.parent / 'outside'; outside.mkdir()
        (outside / 'new-project').mkdir()
        original = F.W._bind
        def swap(record_path, **facts):
            result = original(record_path, **facts)
            if 'repository_created' in facts:
                self.parent.rename(self.parent.with_name('original-parent'))
                self.parent.symlink_to(outside, target_is_directory=True)
            return result
        with mock.patch.object(F.W, '_bind', side_effect=swap):
            result = self.settle()
        self.assertFalse((outside / 'new-project/.git').exists())
        self.assertFalse(result.get('owner_started'))

class NativeRevision(S.FrameInterviewStepTest):
    def test_native_candidate_cannot_inherit_revised_brief_with_identical_question_text(self):
        question = self.scope_question()
        self.step(interview=self.question_file)
        shown = [{'question': '제가 이해한 내용: ' + question['understanding'],
                  'options': ['예 (권장)', '아니오(고쳐 말하기)'], 'answer': '예'},
                 {'question': question['questions'][0]['question'],
                  'options': ['Both (recommended)', 'Approval only'], 'answer': 'Approval only'}]
        candidate = S.W.native_answer_candidate(self.route, self.jobs, shown)
        self.question_file.write_text(json.dumps({**question, 'brief': {
            **question['brief'], 'outcome': 'A revised deliverable with identical visible choices.'}}))
        self.step(interview=self.question_file)
        self.assertIsNone(S.W.record_native_answer(self.route, self.jobs, shown,
                                                  expected_interview_digest=candidate[2]))
        self.assertEqual(self.step()['state'], 'needs-question')

    def stale_reply(self, partial):
        question = self.scope_question()
        first = self.step(interview=self.question_file)
        shown = [{'question': '제가 이해한 내용: ' + question['understanding'],
                  'options': ['예 (권장)', '아니오(고쳐 말하기)'], 'answer': '예'}]
        if not partial:
            shown.append({'question': question['questions'][0]['question'],
                          'options': ['Both (recommended)', 'Approval only'], 'answer': 'Approval only'})
        real = S.W._native_progress
        def interleave(*args):
            progress = real(*args)
            self.assertIsNotNone(progress)
            changed = {**question, 'understanding': 'A newly revised project scope.',
                       'brief': {**question['brief'], 'outcome': 'A different deliverable.'}}
            self.question_file.write_text(json.dumps(changed))
            self.step(interview=self.question_file)
            return progress
        with mock.patch.object(S.W, '_native_progress', side_effect=interleave):
            saved = S.W.record_native_answer(self.route, self.jobs, shown)
        directory = Path(first['interview_file']).parent
        self.assertIsNone(saved)
        self.assertFalse((directory / 'answers.native.json').exists())
        self.assertFalse((directory / 'answers.native.partial.json').exists())
        self.assertEqual(self.step()['state'], 'needs-question')
        self.assertEqual(self.resolution()['status'], 'blocked')
        self.assertEqual(self.resolution()['epoch'], 1)

    def test_complete_native_reply_cannot_approve_replacement(self):
        self.stale_reply(False)

    def test_partial_native_reply_cannot_occupy_replacement(self):
        self.stale_reply(True)

class CorrectionDelivery(F.StartBase):
    def test_large_retained_history_delivers_recent_conditions_with_coverage(self):
        import dispatch_owner_input as inputs
        import work_intent
        from fleet import route as fleet_route
        for index in range(17):
            receipt = inputs.frame_input(self.route, f'Condition {index}: ' + 'x'*65000, f'condition-{index}')
        path = Path(self.route['artifact_root']) / '.runtime/framed-decision' / (self.route['route_id']+'.corrections.json')
        self.assertGreater(path.stat().st_size, 1024*1024)
        self.assertEqual(len(json.loads(path.read_text())['requests']), 17)
        loaded = work_intent.frame_corrections(self.route)
        self.assertTrue(loaded)
        self.assertEqual(loaded[0]['id'], 'condition-16')
        text = work_intent.frame_corrections_text(self.route)
        self.assertIn('truncated', text.lower())
        self.assertIn(str(path), text)
        self.assertLess(len(text.encode()), 16384)
        template = F.W.frame_interview_step(self.route, self.path, self.jobs)
        self.assertIn('Condition 16:', template['interview_template']['brief']['constraints'])
        view = fleet_route.with_frame_corrections({'nodes':[{'id':'frame'}]}, self.route)
        says = view['nodes'][0]['says']
        self.assertIn('Condition 16:', says)
        self.assertIn('truncated', says.lower())
        self.assertTrue(receipt['delivery_coverage']['truncated'])
        self.assertEqual(receipt['delivery_coverage']['total'], 17)
        self.settle()
        leg = json.loads(self.leg_routes()[0].read_text())
        self.assertIn('Condition 16:', leg['work_request']['text'])
        self.assertIn('truncated', leg['work_request']['text'].lower())

if __name__ == '__main__':
    # Imported fixture classes are helpers, not additional copies of their suites.
    suite = unittest.TestSuite()
    for cls, names in [(RepositoryBoundary, [n for n in RepositoryBoundary.__dict__ if n.startswith('test_')]),
                       (NativeRevision, [n for n in NativeRevision.__dict__ if n.startswith('test_')]),
                       (CorrectionDelivery, [n for n in CorrectionDelivery.__dict__ if n.startswith('test_')])]:
        suite.addTests(cls(n) for n in names)
    if len(sys.argv) > 1:
        suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:], sys.modules[__name__])
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
