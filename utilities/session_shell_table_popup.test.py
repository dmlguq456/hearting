"""11-15 (popup design B): the routes popup is Fleet's own table.

The backend's `popup` reading draws Fleet's `draw_routes` and `_route_page` into a grid of the popup's
width and sends its painted cells; nothing here is a table of the shell's own. Given the same synthetic
view model, every popup row (its words, glyphs, colors and number forms, and the columns a narrow width
leaves out) is Fleet's. (The LAB and sessions popups, and another session's question opened from the
latter, went with their panel sections, 11-15.) Synthetic values only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import contextlib
import io
from itertools import product
import json
import subprocess
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]

import session_shell as shell  # noqa: E402

OWN_SID, OTHER_SID = '00000000-0000-4000-8000-0000000000e9', '00000000-0000-4000-8000-000000000012'
TAGS = {('claude', OWN_SID): 'e9', ('claude', OTHER_SID): '12'}


def stages(*pairs):
    return [{'name': name, 'st': state} for name, state in pairs]


VISIBILITY = {'lines': [
    ['route', 'solo · default · autopilot-code quick', 'info'], ['owner', 'no owner attempt in the dispatch registry', 'dim'],
    ['stages', '·', 'dim'], ['review', 'no independent reviewer stage in this route', 'dim'],
    ['CI', 'no CI result in the route outcome', 'dim'], ['gate', 'unproven', 'mixed'],
    ['test', 'synthetic check one', 'good'], ['test', 'synthetic check two', 'good'],
    ['unverified', 'a check the route could not run', 'mixed'], ['blocked', 'a reason that keeps going until the end', 'bad'],
    ['path', 'route /syn/art/route.json', 'dim'], ['path', 'evidence one-shot /syn/art/evidence.md', 'dim'],
    ['path', 'report /syn/art/report.md', 'dim'], ['next', 'wait · one-shot', 'info']]}
ROUTES = [
    {'id': 'rt-aaaaaaaaaaaaaaa1', 'session_tag': 'e9', 'repo': 'hearting', 'title': '질문 팝업 구현 — 창 가운데 팝업, 패널 안 요약, 답 직후 Esc·Enter 막기',
     'state': 'working', 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'xhigh', 'owner_gauge': [38, 0.2], 'age': '23m',
     'capability': 'autopilot-code', 'mode': 'dev', 'intensity': 'standard',
     'stages': stages(('plan', 'done'), ('execute', 'active'), ('review', 'pending')),
     'doer': {'harness': 'claude', 'model': 'Sonnet 5.5', 'effort': 'high', 'context': 22, 'window': 0.2, 'tool': 'Edit', 'target': 'panel-view.cjs'},
     'worker': ['worker', [['claude Sonnet 5.5 high', 'soft']], [['running', 'g_spin']]], 'worker_stage': 'execute', 'activity': 'running'},
    {'id': 'rt-aaaaaaaaaaaaaaa2', 'session_tag': 'e9', 'repo': 'hearting', 'title': '휠 입력 시험', 'state': 'dead', 'harness': 'opencode',
     'model': 'glm-5', 'effort': 'high', 'owner_gauge': [12, 0.128], 'age': '54m', 'capability': 'autopilot-code', 'mode': 'debug',
     'intensity': 'quick', 'stages': stages(('one-shot', 'failed')), 'visibility': VISIBILITY, 'resume': '같은 start에 --prompt-file',
     'worker': ['owner', [['BLOCKED: 사설 PTY를 열지 못함 — runtime_dir에 node-pty 빌드가 없어 휠 시험을 돌리지 못했다', 'lvl_r']],
                [['dead', 'g_dead']]], 'worker_stage': 'one-shot'},
    {'id': 'rt-bbbbbbbbbbbbbbb3', 'session_tag': '12', 'repo': 'speech-lab', 'title': '다른 세션의 route', 'state': 'working',
     'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'high', 'age': '5m', 'stages': stages(('one-shot', 'active'))},
]
SESSIONS = [
    {'tag': 'e9', 'repo_id': 'hearting', 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'xhigh', 'context': 46, 'window': 200000,
     'state': 'working', 'title': '질문·route 팝업 구현', 'now': '세 창 폭에서 그리는 중', 'age': '23m'},
    {'tag': '73', 'repo_id': 'hearting', 'harness': 'codex', 'model': 'gpt-5.5', 'effort': 'high', 'context': 33, 'window': 400000,
     'state': 'working', 'title': 'v4.32.7 설치 확인', 'now': 'CI 기다리는 중', 'age': '12m'},
    {'tag': 'e7', 'repo_id': 'hearting', 'harness': 'claude', 'model': 'Sonnet 5.5', 'effort': 'high', 'context': 71, 'window': 200000,
     'state': 'idle', 'title': '#1640 검토', 'now': '', 'age': '1h'},
    {'tag': '12', 'repo_id': 'speech-lab', 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'high', 'context': 58, 'window': 200000,
     'state': 'blocked', 'answer_wait_marker': '◑', 'title': '분리 모델 학습 비교', 'now': '학습률 정할 답 대기', 'age': '41m'},
]
REPOS = [{'id': 'hearting', 'name': 'hearting'}, {'id': 'speech-lab', 'name': 'speech-lab'}]


def view_model():
    from fleet.explorer.view_model import ExplorerViewModel
    return ExplorerViewModel.from_mapping({'routes': ROUTES, 'sessions': SESSIONS, 'repos': REPOS, 'system': {}})


class TablePopupTest(unittest.TestCase):
    binding = {'harness': 'claude', 'sid': OWN_SID, 'native_pid': 2, 'native_start': '1'}

    def setUp(self):
        shell._HELD_VIEW.clear()
        self.addCleanup(shell._HELD_VIEW.clear)

    def popup(self, model=None, **request):
        # The terminal's 256 colors, as the projection reads them (TERM).
        model = view_model() if model is None else model
        with mock.patch.object(shell, '_popup_view_model', return_value=model), \
                mock.patch('fleet.session_tags.assigned_tag', lambda harness, sid: TAGS.get((harness, sid))), \
                mock.patch.dict(os.environ, {'TERM': 'xterm-256color'}):
            return shell._panel_popup(self.binding, request)

    @staticmethod
    def fleet(draw, columns, rows=168):
        from fleet.explorer.grid import CellGrid
        grid = CellGrid(columns, rows)
        draw(grid)
        return shell.popup_rows(grid)

    @staticmethod
    def text(rows):
        return [''.join(run[0] for run in row) for row in rows]

    def test_the_route_popup_is_fleets_table_at_the_popup_width(self):
        from fleet.explorer import pages
        mine = tuple(route for route in view_model().routes if route['session_tag'] == 'e9')
        for columns in (52, 82, 112):
            for opened in ('', 'rt-aaaaaaaaaaaaaaa2'):
                got = self.popup(kind='routes', columns=columns, open=opened)
                want = self.fleet(lambda grid: pages._draw_route_subset(grid, view_model(), mine, 0, 0, grid.rows - 4,
                                                                        opened=opened, no_repo_group=True), columns)
                self.assertEqual((got['rows'], got['styles'], got['hits']), want, (columns, opened))
                # Only this session's routes; the order Fleet's.
                self.assertEqual(got['routes'], [route['id'] for route in mine])
                self.assertNotIn('다른 세션의 route', '\n'.join(self.text(got['rows'])))
        # 11-15: the LAB, sessions and other-session question popups went with their panel sections.
        for kind in ('lab', 'sessions', 'questions'):
            self.assertEqual(self.popup(kind=kind, columns=82, open='12', tag='12'), {'ok': False, 'reason': 'invalid-popup'}, kind)
        # Narrow, Fleet's own columns leave as Fleet leaves them (PROGRESS before the title).
        narrow = self.text(self.popup(kind='routes', columns=52)['rows'])[0]
        wide = self.text(self.popup(kind='routes', columns=112)['rows'])[0]
        self.assertNotIn('PROGRESS', narrow)
        for word in ('ID', 'MODEL', 'CONTEXT', 'ROUTE', 'PROGRESS', 'AGE'):
            self.assertIn(word, wide)
        # The unfolded failed route's reason reads whole (Fleet's band wraps it), at every width.
        for columns in (52, 82, 112):
            whole = ''.join(self.text(self.popup(kind='routes', columns=columns, open='rt-aaaaaaaaaaaaaaa2')['rows'])).replace(' ', '')
            # (Where the row is narrow the state stands right after its first word, the rest on the rows under it.)
            self.assertIn('BLOCKED:', whole, columns)
            self.assertIn('사설PTY를열지못함—runtime_dir에node-pty빌드가없어휠시험을돌리지못했다', whole, columns)

    def test_a_held_backend_draws_the_open_route_and_its_page_from_one_collection_only_when_asked_to_reuse_it(self):
        calls = []

        def collect(harness, sid, kind):
            calls.append(sid)
            return view_model()
        opened = 'rt-aaaaaaaaaaaaaaa2'
        with mock.patch.object(shell, '_popup_view_model', collect), \
                mock.patch('fleet.session_tags.assigned_tag', lambda harness, sid: TAGS.get((harness, sid))), \
                mock.patch.dict(os.environ, {'TERM': 'xterm-256color'}):
            # No `reuse`: every read collects (the one-shot way and every other caller).
            shell._panel_popup(self.binding, {'kind': 'routes', 'columns': 82})
            shell._panel_popup(self.binding, {'kind': 'routes', 'columns': 82})
            self.assertEqual(len(calls), 2)
            # `reuse` after a collection: the unfolded route and its page come from it, with the time it was read.
            table = shell._panel_popup(self.binding, {'kind': 'routes', 'columns': 82, 'open': opened, 'reuse': True})
            page = shell._panel_popup(self.binding, {'kind': 'routes', 'columns': 82, 'open': opened, 'page': True,
                                                     'rows': 20, 'reuse': True})
            self.assertEqual(len(calls), 2)
            self.assertTrue(page['page'])
            self.assertIsInstance(table['read_at'], int)
            self.assertEqual(table['read_at'], page['read_at'])
            self.assertGreater(table['read_at'], 0)
            # Another session is not this collection.
            other = {'harness': 'claude', 'sid': 'another-session', 'native_pid': 3, 'native_start': '1'}
            shell._panel_popup(other, {'kind': 'routes', 'columns': 82, 'reuse': True})
            self.assertEqual(len(calls), 3)
            # A read without `reuse` collects again even when the slot is full.
            shell._panel_popup(other, {'kind': 'routes', 'columns': 82})
            self.assertEqual(len(calls), 4)

    def test_the_route_page_is_fleets_page_with_a_fitted_frame(self):
        from dataclasses import replace
        from fleet.explorer import pages
        mine = tuple(route for route in view_model().routes if route['session_tag'] == 'e9')
        got = self.popup(kind='routes', columns=82, open='rt-aaaaaaaaaaaaaaa2', page=True, rows=20)
        self.assertTrue(got['page'])
        want_rows, want_styles, _hits = self.fleet(lambda grid: pages._route_page(
            grid, replace(view_model(), routes=mine), 'rt-aaaaaaaaaaaaaaa2', 0, 81, grid.rows,
            pages._SubsetState(None, 'route:rt-aaaaaaaaaaaaaaa2', 'rt-aaaaaaaaaaaaaaa2', True), whole=True),
            82, rows=shell.POPUP_ROWS + 8)
        while want_rows and not want_rows[0]:
            want_rows.pop(0)
        self.assertEqual((got['rows'], got['styles']), (want_rows, want_styles))
        # A route not this session's has no page: the table stands.
        self.assertFalse(self.popup(kind='routes', columns=82, open='rt-bbbbbbbbbbbbbbb3', page=True)['page'])

    def test_the_route_page_has_every_line_at_any_window_height_to_read_on(self):
        # PR #1648 review B: a low window folded the page's artifact paths and `next` into `+N more`, which
        # the popup could not unfold. The page is as long as its lines (the popup reads it on with PgUp/PgDn
        # and the wheel, as it reads any table), so every line stands, whatever the window's height.
        for window in range(16, 61):
            for columns in (52, 82, 112):
                got = self.popup(kind='routes', columns=columns, open='rt-aaaaaaaaaaaaaaa2', page=True, rows=window - 6)
                self.assertTrue(got['page'])
                whole = ''.join(self.text(got['rows'])).replace(' ', '')
                for label, text, _tone in VISIBILITY['lines']:
                    self.assertIn((label + ':' + text).replace(' ', ''), whole, (window, columns, label))
                self.assertNotRegex(whole, r'\+\d+more', (window, columns))

    def test_a_long_route_page_is_whole_at_every_requested_height(self):
        from dataclasses import replace

        route = dict(ROUTES[0], shape='staged+resource')
        ident = route['id']
        stages = [{'n': 's%d' % i, 'st': 'active', 'when': 'now', 'out': 'out.md', 'unit': 'dev/code'}
                  for i in range(12)]
        node = {'n': 'leg', 'kind': 'leg', 'st': 'active', 'title': [('code dev', 'head')], 'detail': stages}
        model = replace(view_model(), routes=(route,), nodes={ident: (node,)})
        views = [self.popup(model, kind='routes', columns=124, open=ident, page=True, rows=height)
                 for height in (24, 40, 54)]
        self.assertGreater(len(views[0]['rows']), 33)
        for view in views:
            self.assertTrue(self.text(view['rows'])[-1].startswith('╚═'))
            self.assertEqual(view['rows'], views[0]['rows'])

    def test_short_routes_fit_their_stage_rows(self):
        from dataclasses import replace

        route = ROUTES[0]
        ident = route['id']
        forms = (('unit', 'one-shot-owner', 1), ('solo', 'one-shot-owner', 1),
                 ('staged', 'staged', 3), ('team', 'staged+resource', 6))
        for (form, shape, count), columns in product(forms, (92, 124, 188)):
            with self.subTest(form=form, columns=columns):
                stages = [{'n': 's%d' % i, 'st': 'active', 'when': 'now', 'out': 'out.md', 'unit': 'dev/code'}
                          for i in range(count)]
                node = {'n': 'leg', 'kind': 'leg', 'st': 'active', 'title': [('code dev', 'head')], 'detail': stages}
                row = dict(route, shape=shape, **({'capability': 'unit', 'field': 'code'} if form == 'unit' else {}))
                model = replace(view_model(), routes=(row,), nodes={ident: (node,)})
                views = [self.popup(model, kind='routes', columns=columns, open=ident, page=True, rows=height)
                         for height in (24, 40, 54)]
                self.assertEqual(views[0]['rows'], views[1]['rows'])
                self.assertEqual(views[1]['rows'], views[2]['rows'])
                rows = self.text(views[0]['rows'])
                for index in range(count):
                    label = '┌─ s%d ' % index
                    top = next(i for i, text in enumerate(rows) if label in text)
                    left = rows[top].index(label)
                    self.assertEqual([rows[top + offset][left:left + 1] for offset in range(1, 5)],
                                     ['│', '│', '│', '└'])
                self.assertIn('└', rows[-2])
                self.assertTrue(rows[-1].startswith('╚═'))

    def test_fitted_boxes_with_different_heights_keep_straight_stage_links(self):
        from dataclasses import replace

        route = ROUTES[0]
        ident = route['id']
        stages = [
            {'n': 'plan', 'st': 'done', 'graph_known': True, 'level': 0},
            {'n': 'execute', 'st': 'active', 'graph_known': True, 'level': 1, 'depends_on': ['plan'],
             'who': ('claude', 'fixture'), 'gate': 'ready', 'says': 'working'},
            {'n': 'check', 'st': 'todo', 'graph_known': True, 'level': 2, 'depends_on': ['execute']},
        ]
        node = {'n': 'leg', 'kind': 'leg', 'st': 'active', 'detail': stages}
        model = replace(view_model(), routes=(route,), nodes={ident: (node,)})
        view = self.popup(model, kind='routes', columns=124, open=ident, page=True)
        frame = [row for row in self.text(view['rows']) if row.startswith('║')]
        self.assertEqual([row.count('▶') for row in frame if '▶' in row], [2])

    def test_fitted_frame_keeps_a_loop_return_on_the_panel_rail(self):
        from dataclasses import replace
        from fleet.palette import style_components

        route = dict(ROUTES[0], shape='staged')
        ident = route['id']
        stages = [{'n': name, 'st': 'done'} for name in ('plan', 'execute', 'check')]
        node = {'n': 'leg', 'kind': 'leg', 'st': 'active', 'detail': stages,
                'loops': [(2, 0, '↺ 1 · check → plan')]}
        model = replace(view_model(), routes=(route,), nodes={ident: (node,)})
        view = self.popup(model, kind='routes', columns=124, open=ident, page=True)
        rows = self.text(view['rows'])
        first = next(i for i, row in enumerate(rows) if row.startswith('║ ┌▶'))
        last = next(i for i, row in enumerate(rows) if row.startswith('║ └─'))
        expected = [('┌', style_components(('y', 0, 1)))]
        expected += [('│', expected[0][1])] * (last - first - 1)
        expected += [('└', expected[0][1])]
        rail = []
        for row in view['rows'][first:last + 1]:
            cells = [(char, view['styles'][style]) for text, style in row for char in text]
            rail.append(cells[2])
        self.assertEqual(rail, expected)

    def test_route_raw_strip_requires_drawable_rows_and_fits_short_text(self):
        from dataclasses import replace

        route = ROUTES[0]
        ident = route['id']
        node = {'kind': 'leg', 'st': 'active', 'n': 'one-shot',
                'detail': [{'n': 'one-shot', 'st': 'active'}]}
        model = replace(view_model(), routes=(route,), nodes={ident: (node,)})
        for lines in ((), ('', '   '), ('first', 'second', 'third')):
            with self.subTest(lines=lines):
                view = self.popup(replace(model, attempts={'route': ident, 'lines': lines}), kind='routes',
                                  columns=124, open=ident, page=True, rows=40)
                rows = self.text(view['rows'])
                if len(lines) == 3:
                    start = next(i for i, text in enumerate(rows) if 'owner raw' in text)
                    self.assertIn('first', rows[start + 1])
                    self.assertIn('second', rows[start + 2])
                    self.assertIn('third', rows[start + 3])
                    self.assertIn('last 3 rows', rows[start + 4])
                    self.assertEqual(start + 5, len(rows) - 1)
                else:
                    self.assertNotIn('owner raw', '\n'.join(rows))
        # The page is longer than a low window: the host's table reads it on and says where it stands.
        low = self.popup(kind='routes', columns=82, open='rt-aaaaaaaaaaaaaaa2', page=True, rows=10)
        self.assertGreater(len(low['rows']), 13)

    def test_another_sessions_question_is_never_answered_from_this_window(self):
        # 11-15: the sessions popup and its way into another session's question are gone; an answer this
        # window sends is its own session's only, whatever owner a request names.
        import human_wait_state
        import questions
        item = {'id': 'S4:other', 'source': 'S4', 'owner': {'harness': 'claude', 'session_id': OTHER_SID}}
        sent = []

        def answer(args):
            sent.append(args)
            print(json.dumps({'id': args.id, 'state': 'submitted'}))
            return 0
        request = {'question_id': 'S4:other', 'revision': 'a' * 64, 'answers': {'lr': ['3e-4']},
                   'owner': {'harness': 'claude', 'session_id': OTHER_SID}}
        with mock.patch.object(human_wait_state, 'cached_view', lambda request=False: {'items': [item]}), \
                mock.patch.object(questions, '_find', lambda board, wanted: (item, None) if wanted == 'S4:other' else (None, 'no')), \
                mock.patch.object(questions, '_answer', answer):
            self.assertEqual(shell._block_answer(self.binding, request)['reason'], 'question-changed')
            self.assertEqual(shell._block_answer(self.binding, {k: v for k, v in request.items() if k != 'owner'})['reason'], 'question-changed')
            # This window's own question still goes, with its own session as the owner.
            mine = {**item, 'owner': {'harness': 'claude', 'session_id': OWN_SID}}
            with mock.patch.object(questions, '_find', lambda board, wanted: (mine, None)):
                self.assertTrue(shell._block_answer(self.binding, request)['ok'])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].expected_owner, {'harness': 'claude', 'session_id': OWN_SID})

    def test_a_long_table_is_cut_to_what_fits_with_one_line_for_the_rest(self):
        # 300 routes (116 cells wide, the answer past the host's 64 KB read as one table): the rows that fit
        # stand, the last line says how many routes are left (the entrance's `외 N개`), and the count is what
        # the kept rows do not show. A table that fits has no such line.
        from fleet.explorer.view_model import ExplorerViewModel
        routes = [dict(ROUTES[0], id='rt-%016d' % n, title='합성 route %d — ' % n + '긴 제목 ' * 8, session_tag='e9') for n in range(300)]
        sessions = [dict(SESSIONS[1], tag='s%03d' % n, title='합성 세션 %d ' % n + '긴 제목 ' * 8) for n in range(160)]
        model = ExplorerViewModel.from_mapping({'routes': routes, 'sessions': sessions, 'repos': REPOS, 'system': {}})
        with mock.patch.object(shell, '_popup_view_model', lambda harness, sid, kind: model), \
                mock.patch('fleet.session_tags.assigned_tag', lambda harness, sid: TAGS.get((harness, sid))), \
                mock.patch.dict(os.environ, {'TERM': 'xterm-256color', 'FLEET_NOW_LANG': 'ko'}):
            for kind, total, shown_of in (('routes', 300, lambda got: len(got['routes'])),):
                for columns in (56, 116, 200):
                    got = shell._panel_popup(self.binding, {'kind': kind, 'columns': columns, 'open': ''})
                    self.assertTrue(got['ok'], (kind, columns))
                    weight = len(json.dumps({'id': 1, **got}, ensure_ascii=False).encode())
                    self.assertLessEqual(weight, 64 * 1024, (kind, columns))
                    shown = shown_of(got)
                    self.assertTrue(0 < shown < total, (kind, columns, shown))
                    last = ''.join(run[0] for run in got['rows'][-1])
                    self.assertEqual(last, '외 %d개' % (total - shown), (kind, columns))
                    self.assertLessEqual(len(got['rows']), shell.POPUP_ROWS + 1)
                    self.assertTrue(all(hit['row'] < len(got['rows']) - 1 for hit in got['hits']))
                    # The last line is dim, as the table's own quiet words.
                    self.assertEqual(got['styles'][got['rows'][-1][0][1]].get('dim'), True)
        # A table that fits is Fleet's own rows, whole, with no such line.
        small = self.popup(kind='routes', columns=112)
        self.assertNotIn('외 ', self.text(small['rows'])[-1])


if __name__ == '__main__':
    unittest.main()
