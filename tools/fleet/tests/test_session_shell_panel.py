"""Fleet opens only the exact native session captured by its selected request."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.fleet import render
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.types import FocusKey


class FleetShellPanelTest(unittest.TestCase):
    def test_control_bracket_requests_the_selected_session_panel(self):
        state=NavState(scope='session:52')
        after,effects=reduce(state,{'type':'key','key':'\x1d'})
        self.assertEqual(after,state)
        self.assertEqual(effects, (('shell.panel','52'),))

    def test_panel_dispatch_uses_the_existing_native_focus_key_and_blocks_replay(self):
        from tools.fleet.explorer import focus
        native=mock.Mock(return_value={'ok':True})
        target=(FocusKey('codex','native-first'),('codex:native-first',),())
        dispatch=getattr(render,'_explorer_open_shell_panel',lambda *_:None)
        with mock.patch.object(render,'_EXPLORER_STATE',NavState(scope='session:52')), \
                mock.patch.object(render,'_explorer_requests',return_value={'session':target}), \
                mock.patch.object(focus,'utility',return_value=SimpleNamespace(open_panel=native)), \
                mock.patch.object(render,'_EXPLORER_REPLAY_ACTIVE',False):
            dispatch(('shell.panel','52'),object(),object())
            native.assert_called_once_with('codex','native-first')
            native.reset_mock()
            with mock.patch.object(render,'_EXPLORER_REPLAY_ACTIVE',True):
                dispatch(('shell.panel','52'),object(),object())
            native.assert_not_called()


NOW = 1_000_000.0


def _request(state='open', side='out', *, updated=NOW - 60, reports=(), cancel=None, revisions=None):
    return {'record': {
        'id': 'rq-aaaaaa', 'title': '#1516 검토', 'state': state, 'updated': updated, 'cancel': cancel,
        'requester': {'harness': 'claude', 'session_id': 'a', 'name': 'e9'},
        'recipient': {'harness': 'codex', 'session_id': 'b', 'name': 'e7'},
        'revisions': revisions or [{'n': 1, 'label': 'head 1', 'state': 'current', 'transfer_ref': 'r1'}],
        'reports': list(reports)}, 'side': side, 'rows': {}, 'gone': False, 'body': 'BODY\nsecond'}


class RequestRowsTest(unittest.TestCase):
    """The panel's request lines (R27): two columns, never mixed, and a bounded afterglow."""

    def setUp(self):
        patch = mock.patch('fleet.config.ui_language', return_value='ko')
        try:
            patch.start()
        except ModuleNotFoundError:
            patch = mock.patch('tools.fleet.config.ui_language', return_value='ko')
            patch.start()
        self.addCleanup(patch.stop)

    def rows(self, views):
        from tools.fleet.session_panel import request_rows
        return request_rows(views, NOW, lambda harness, sid: {'a': 'e9', 'b': 'e7'}.get(sid))

    def test_delivery_and_work_are_two_columns_and_no_progress_word_is_invented(self):
        view = _request()
        for delivered, word in (('pending', '보냄'), ('queued', '전달됨')):
            view['rows'] = {'r1': {'state': delivered}}
            row, = self.rows([view])
            self.assertEqual((row['glyph'], row['status'], row['kind']), ('→', word, 'request-out'))
        # Once seen, the line says the work; delivery stays in its own detail row.
        view['rows'] = {'r1': {'state': 'received'}}
        row, = self.rows([view])
        self.assertEqual(row['status'], '열림')
        self.assertIn(['DELIVERY', '보임', 'dim'], row['details'])
        for line in self.rows([view]) + self.rows([_request('partial')]):
            self.assertNotIn('진행', str(line))

    def test_received_work_is_shown_while_open_and_leaves_when_closed(self):
        row, = self.rows([_request('open', 'in')])
        self.assertEqual((row['glyph'], row['kind'], row['who']), ('◂', 'request-in', '[e9]'))
        self.assertEqual(self.rows([_request('final', 'in', reports=[{'n': 1, 'kind': 'final', 'at': NOW - 5}])]), [])

    def test_a_closed_sent_request_stays_for_the_existing_afterglow_and_until_its_answer_is_seen(self):
        final = {'n': 1, 'kind': 'final', 'at': NOW - 60, 'transfer_ref': 't1', 'revision': 1, 'result': 'ok'}
        view = _request('final', reports=[final])
        view['rows'] = {'t1': {'state': 'received'}}
        row, = self.rows([view])
        self.assertEqual((row['glyph'], row['status'], row['kind']), ('✓', '최종 답', 'request-done'))
        old = dict(final, at=NOW - 16 * 60)
        view = _request('final', reports=[old])
        view['rows'] = {'t1': {'state': 'received'}}
        self.assertEqual(self.rows([view]), [])
        view['rows'] = {'t1': {'state': 'queued'}}  # the answer was not seen: it stays
        row, = self.rows([view])
        self.assertEqual(row['glyph'], '✓')

    def test_cancelled_shows_its_own_word_and_the_revision_seen_state_stays_separate(self):
        view = _request('cancelled', cancel={'at': NOW - 30, 'reason': '필요 없음', 'notice_ref': 'n1'},
                        revisions=[{'n': 1, 'label': 'head 1', 'state': 'cancelled', 'transfer_ref': 'r1'}])
        view['rows'] = {'r1': {'state': 'received'}, 'n1': {'state': 'queued'}}
        row, = self.rows([view])
        self.assertEqual((row['glyph'], row['status'], row['kind']), ('✕', '취소됨', 'request-rejected'))
        self.assertIn(['DELIVERY', '보임', 'dim'], row['details'])

    def test_a_confirmed_ended_peer_changes_only_the_delivery_word(self):
        view = _request(); view['gone'] = True; view['rows'] = {'r1': {'state': 'queued'}}
        row, = self.rows([view])
        self.assertEqual(row['state'], 'open')
        self.assertIn(['DELIVERY', '상대 끝남', 'dim'], row['details'])

    def test_late_reports_name_their_superseded_revision_and_overflow_is_bounded(self):
        late = {'n': 1, 'kind': 'final', 'at': NOW - 20, 'revision': 1, 'late': True, 'result': 'old verdict',
                'transfer_ref': 't1'}
        view = _request('open', revisions=[{'n': 1, 'label': 'a', 'state': 'superseded', 'transfer_ref': 'r1'},
                                           {'n': 2, 'label': 'b', 'state': 'current', 'transfer_ref': 'r2'}],
                        reports=[late])
        view['body'] = '\n'.join('line %d' % i for i in range(500))
        row, = self.rows([view])
        self.assertTrue(any('1(대체됨)' in value for _label, value, _tone in row['details']))
        self.assertEqual(len(row['body']), 200)
        self.assertTrue(row['body_more'])

    def test_order_puts_received_work_and_stopped_requests_first(self):
        rows = self.rows([_request('open', 'out'), _request('open', 'in'),
                          _request('final', 'out', reports=[{'n': 1, 'kind': 'final', 'at': NOW - 5}]),
                          _request('rejected', 'out', reports=[{'n': 1, 'kind': 'reject', 'at': NOW - 5,
                                                                'result': '범위 밖'}])])
        self.assertEqual([row['kind'] for row in rows],
                         ['request-in', 'request-rejected', 'request-out', 'request-done'])


if __name__=='__main__':unittest.main()
