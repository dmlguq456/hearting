#!/usr/bin/env python3
"""Approved-leg readers and closure diagnostics; private fixtures only."""
import os
import sys
if __name__ == '__main__' and not os.environ.get('HEARTING_TEST_ISOLATED'):
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(__file__), '../tools/run-tests.py'),
                            '--direct-run', sys.executable, *sys.orig_argv[1:]])
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import artifact_cycle_titles as titles
import artifact_workflow_group_review as review
import frame_interview as frame
import human_wait_state as waits
import questions
import work_start
from fleet.explorer import live
from fixtures.message_questions import MessageQuestionFixture


def interview():
    return {'route_id': 'rt-fixture', 'understanding': 'One run only.', 'round': 1,
            'brief': {'problem': 'Lost GPU', 'outcome': 'runs: 2', 'affected': 'User',
                      'constraints': 'No new gate', 'open': ''}, 'questions': []}


class LegReadersTest(unittest.TestCase):
    request = '# Original training request\n\nRun E on device zero only.\n'
    wrapped = '## Agreed intent\n\n' + 'proposal context\n' * 400 + (
        '\n## Original work request\n\n' + request + '\n## Frame briefs (read-only input)\n\n- fixture\n')

    def test_cycle_title_reads_original_first_line(self):
        self.assertEqual(titles._route_task_first_line(self.wrapped), 'Original training request')

    def test_fleet_fallback_reads_original_first_line(self):
        record = {'route_id': 'rt-fixture', 'work_request': {'text': self.wrapped}}
        self.assertEqual(live._route_subject('rt-fixture', record, {}, {}, {}, []), 'Original training request')

    def test_review_request_is_unwrapped_before_both_limits(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(review.cycle_titles, '_route_text', return_value=self.wrapped), \
             patch.object(review.producer, 'cycle_dir', side_effect=OSError('no manifest')):
            view = review._view(Path(tmp), 'fixture', {'cycle_id': 'cycle'}, {})
        self.assertEqual(view.request, self.request.strip())
        self.assertIn('Run E on device zero only.', view.request[:review.CONTEXT_REQUEST_CHARS])

    def test_nonhuman_intent_preserves_base_bytes(self):
        for actor in ('automatic', 'supervisor', 'unknown'):
            behalf = actor == 'supervisor'
            expected = ('---\nstatus: ' + ('agreed-on-behalf' if behalf else 'recorded') +
                        f'\nactor_kind: {actor}\ncreated: 2026-11-14\nroute_id: rt-fixture\nround: 1\n'
                        'schema: frame_intent_v1\n---\n\n# Intent\n\n## ' +
                        ("Confirmed understanding (on the person's behalf)" if behalf else 'Recorded understanding') +
                        '\n\nOne run only.\n\n## Problem\n\nLost GPU\n\n## Proposed Outcome\n\nruns: 2\n'
                        '\n## Affected Users / Systems\n\nUser\n\n## Constraints\n\nNo new gate\n\n## Decisions\n\n'
                        f'No question recorded a decision from {actor}; the direction above stands as proposed.\n'
                        '\n## Open Questions\n\nNone recorded.\n\n## Sources\n\n'
                        '- interview: shards/frame/interview.json\n- summary: -\n')
            with self.subTest(actor=actor):
                actual = frame.render_intent(interview(), {'actor_kind': actor, 'understanding_confirmed': True}, now='2026-11-14')
                self.assertEqual(actual.encode(), expected.encode())

    def test_nonhuman_leg_preserves_base_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'shards/frame/intent.md'
            path.parent.mkdir(parents=True)
            for actor in ('automatic', 'supervisor', 'unknown'):
                intent = frame.render_intent(interview(), {'actor_kind': actor}, now='2026-11-14')
                path.write_text(intent)
                route = {'route_id': 'rt-fixture', 'work_request': {'text': self.request}}
                actual = work_start._leg_task_text(route, root, root, [], {}, {'given': []})
                expected = (self.request.rstrip() + '\n\n## Agreed intent\n\n' + intent.rstrip() +
                            '\n\n## Frame briefs (read-only input)\n\n\n## Execution scope\n\ncomplete\n'
                            '\n## Start approvals given\n\n- none\n')
                self.assertEqual(actual.encode(), expected.encode())

    def test_human_leg_and_decisions_still_precede_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'shards/frame/intent.md'
            path.parent.mkdir(parents=True)
            intent = frame.render_intent(interview(), {'actor_kind': 'user'}, now='2026-11-14')
            path.write_text(intent)
            route = {'route_id': 'rt-fixture', 'work_request': {'text': self.request}}
            text = work_start._leg_task_text(route, root, root, [], {}, {'given': []}, actor_kind='user')
            self.assertTrue(text.startswith('## Agreed intent\n'))
            self.assertLess(text.index('## Decisions'), text.index('## Proposed Outcome'))
            self.assertEqual(titles._route_task_first_line(text), 'Original training request')

    def test_fleet_tools_only_entry_imports_shared_reader(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, " + repr(str(ROOT / 'tools')) + "); "
                "from fleet.explorer import live; "
                "print(live._route_subject('rt', {'route_id': 'rt', 'work_request': {'text': " + repr(self.wrapped) +
                "}}, {}, {}, {}, []))")
        result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'Original training request')

    def test_conflicts_ignore_times_urls_and_terminal_punctuation(self):
        self.assertEqual(frame._proposal_conflicts('10:30; https://a; runs: 2.',
                                                  {'10': '45', 'https': '//b', 'runs': '2'}), [])
        self.assertTrue(frame._proposal_conflicts('runs: 2.', {'runs': '1'}))


class ClosedQuestionTest(unittest.TestCase):
    def answer(self, item_id, board):
        err = io.StringIO()
        with patch.object(questions, 'load_board', return_value=board), contextlib.redirect_stderr(err):
            code = questions.main(['answer', item_id, '--text', 'late answer'])
        return code, err.getvalue()

    def test_reopened_source_outweighs_applied_overlay_and_history(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness), MessageQuestionFixture() as f:
                signal, envelope = f.ask(harness, 'session', 'A')
                f.submit(envelope, 'PRIVATE ANSWER')
                wire, _, _ = f.prepare(signal)
                f.consume(signal, wire)
                waits.record_closed_question(signal['id'], 'answered')
                f.ask(harness, 'session', 'B')
                reopened, _ = f.ask(harness, 'session', 'A')
                self.assertEqual((reopened['id'], reopened['lifecycle']), (signal['id'], 'open'))
                self.assertIsNone(waits.closed_question(signal['id']))
                code, text = self.answer(signal['id'], None)
                self.assertEqual(code, 3)
                self.assertIn('unknown · the shared answer-wait observation is not available yet', text)
                self.assertNotIn('is closed', text)

    def test_missing_board_keeps_exit_three_even_with_closed_history(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness), MessageQuestionFixture() as f:
                signal, _ = f.ask(harness)
                waits.close_signal(signal['seat_key'], harness=harness, session_id=signal['session_id'],
                                   sequence=signal['sequence'] + 1, answer='PRIVATE ANSWER')
                code, text = self.answer(signal['id'], None)
                self.assertEqual(code, 3)
                self.assertIn('unknown · the shared answer-wait observation is not available yet', text)
                self.assertIn('answered', text)
                self.assertNotIn('PRIVATE ANSWER', text)

    def test_latest_open_selection_outweighs_older_closed_selection(self):
        item_id = 'S4:fixture'
        overlay = {'old': {'selected_id': item_id, 'state': 'applied', 'updated_at': 1},
                   'new': {'selected_id': item_id, 'state': 'open', 'updated_at': 2}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(waits, 'state_root', return_value=Path(tmp)), \
             patch.object(waits, '_valid_selected_overlay', return_value=True), \
             patch.object(waits, '_read_cache_file', return_value={'selected_responses': overlay}):
            self.assertIsNone(waits.closed_question(item_id))


if __name__ == '__main__':
    unittest.main()
