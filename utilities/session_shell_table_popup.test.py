"""11-15 (popup design B, C, D): the route, LAB and sessions popups are Fleet's own tables.

The backend's `popup` reading draws Fleet's `draw_routes`, `_route_page`, `draw_overview(session_table=True)`
and `lab_table.draw_lab` into a grid of the popup's width and sends its painted cells; nothing here is a
table of the shell's own. Given the same synthetic view model, every popup row (its words, glyphs, colors
and number forms, and the columns a narrow width leaves out) is Fleet's. Another session's question,
opened from the sessions popup, is answered with that session as its exact owner. Synthetic values only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import contextlib
import io
import json
import subprocess
import time
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


ROUTES = [
    {'id': 'rt-aaaaaaaaaaaaaaa1', 'session_tag': 'e9', 'repo': 'hearting', 'title': '질문 팝업 구현 — 창 가운데 팝업, 패널 안 요약, 답 직후 Esc·Enter 막기',
     'state': 'working', 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'xhigh', 'owner_gauge': [38, 0.2], 'age': '23m',
     'capability': 'autopilot-code', 'mode': 'dev', 'intensity': 'standard',
     'stages': stages(('plan', 'done'), ('execute', 'active'), ('review', 'pending')),
     'doer': {'harness': 'claude', 'model': 'Sonnet 5.5', 'effort': 'high', 'context': 22, 'window': 0.2, 'tool': 'Edit', 'target': 'panel-view.cjs'},
     'worker': ['worker', [['claude Sonnet 5.5 high', 'soft']], [['running', 'g_spin']]], 'worker_stage': 'execute', 'activity': 'running'},
    {'id': 'rt-aaaaaaaaaaaaaaa2', 'session_tag': 'e9', 'repo': 'hearting', 'title': '휠 입력 시험', 'state': 'dead', 'harness': 'opencode',
     'model': 'glm-5', 'effort': 'high', 'owner_gauge': [12, 0.128], 'age': '54m', 'capability': 'autopilot-code', 'mode': 'debug',
     'intensity': 'quick', 'stages': stages(('one-shot', 'failed')), 'resume': '같은 start에 --prompt-file',
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


def board(now):
    import experiment_board
    iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(t)))  # noqa: E731
    def cond(cid, name, run, reached, latest, delta, anomalies=()):
        return {'id': cid, 'display_name': name, 'run_status': run, 'anomalies': list(anomalies),
                'current_run': {'run_id': 'r-' + cid, 'started_at': iso(now - 7200)} if run else None,
                'progress': {'reached': reached, 'total': 60, 'unit': 'epoch'} if reached is not None else None,
                'latest': {'value': latest, 'epoch': reached, 'key': 'si_sdr'} if latest is not None else None,
                'best': {'value': latest, 'epoch': reached, 'key': 'si_sdr'} if latest is not None else None,
                'comparison': {'state': 'ok', 'baseline': {'label': '기준', 'value': 12.4, 'epoch': 40}, 'delta': delta, 'delta_unit': 'dB',
                               'relative_percent': round(100 * delta / 12.4, 1), 'better': delta > 0} if delta is not None else {'state': 'no-data'},
                'since_last_progress_seconds': 240, 'observed_at': iso(now - 60), 'observed_age_seconds': 60}
    status = {'schema': 'experiment-status/v1', 'generated_at': iso(now - 60),
              'catalog': {'status': 'ok', 'path': 'experiments/catalog.yaml', 'revision': 7, 'updated_at': iso(now - 3600)},
              'groups': [{'id': 'sep', 'display_name': '분리 모델 비교', 'goal': 'SI-SDR 13 dB 넘기기',
                          'metric': {'key': 'si_sdr', 'unit': 'dB', 'direction': 'max'}, 'target': {'kind': 'condition', 'condition': 'c0'},
                          'conditions': [cond('c0', '기준 모델', None, 40, 12.4, None),
                                         cond('c1', '큰 학습률', 'running', 36, 12.9, 0.5),
                                         cond('c2', '작은 배치', 'running', None, None, None),
                                         cond('c3', '긴 문맥', 'running', 9, 10.2, -2.2, ('status-failed',))]}],
              'unlinked_runs': {'run_ids': [], 'truncated': 0}, 'warnings': []}
    return experiment_board.board(status)


class TablePopupTest(unittest.TestCase):
    binding = {'harness': 'claude', 'sid': OWN_SID, 'native_pid': 2, 'native_start': '1'}

    def popup(self, **request):
        # The terminal's 256 colors, as the projection reads them (TERM).
        with mock.patch.object(shell, '_popup_view_model', lambda harness, sid, kind: view_model()), \
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

    def test_the_route_and_sessions_popups_are_fleets_tables_at_the_popup_width(self):
        from fleet.explorer import pages, tables
        from types import SimpleNamespace
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
            got = self.popup(kind='sessions', columns=columns, open='12')
            want = self.fleet(lambda grid: tables.draw_overview(grid, view_model(), SimpleNamespace(opened='12', scope='-'),
                                                                x=0, y=0, session_table=True), columns)
            self.assertEqual((got['rows'], got['styles'], got['hits']), want, columns)
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

    def test_the_route_page_is_fleets_page_at_the_popup_height(self):
        from dataclasses import replace
        from fleet.explorer import pages
        mine = tuple(route for route in view_model().routes if route['session_tag'] == 'e9')
        got = self.popup(kind='routes', columns=82, open='rt-aaaaaaaaaaaaaaa2', page=True, rows=20)
        self.assertTrue(got['page'])
        want_rows, want_styles, _hits = self.fleet(lambda grid: pages._route_page(
            grid, replace(view_model(), routes=mine), 'rt-aaaaaaaaaaaaaaa2', 0, 81, grid.rows,
            pages._SubsetState(None, 'route:rt-aaaaaaaaaaaaaaa2', 'rt-aaaaaaaaaaaaaaa2', True)), 82, rows=23)
        while want_rows and not want_rows[0]:
            want_rows.pop(0)
        self.assertEqual((got['rows'], got['styles']), (want_rows, want_styles))
        self.assertLessEqual(len(got['rows']), 23)
        # A route not this session's has no page: the table stands.
        self.assertFalse(self.popup(kind='routes', columns=82, open='rt-bbbbbbbbbbbbbbb3', page=True)['page'])

    def test_the_lab_popup_is_fleets_lab_table(self):
        from fleet import session_lab
        from fleet.explorer import lab_table
        now = time.time()
        lab = board(now)
        with mock.patch.object(session_lab, 'read', lambda cwd, now=None: (lab, ['/tmp/catalog.yaml'])), \
                mock.patch('fleet.session_view.observe', lambda *args: ({'cwd': '/tmp/project'}, [])):
            for columns in (52, 82, 112):
                got = self.popup(kind='lab', columns=columns)
                want = self.fleet(lambda grid: lab_table.draw_lab(grid, lab, 0, columns, 0), columns)
                # (The table reads Fleet's minute clock: the same minute draws the same rows.)
                self.assertEqual((got['rows'], got['styles']), want[:2], columns)
                self.assertEqual(got['watch'], ['/tmp/catalog.yaml'])
            words = '\n'.join(self.text(self.popup(kind='lab', columns=112)['rows']))
        # The words are Fleet's LAB words (the running state `진행 중`, the baseline said once and its particle).
        with mock.patch.dict(os.environ, {'FLEET_NOW_LANG': 'ko'}):
            from fleet.config import ui_text
            self.assertEqual(ui_text('fleet.lab.state.running'), '진행 중')
            self.assertEqual(ui_text('fleet.lab.base_condition', name='기준 모델'), '기준 모델과 비교')
            self.assertEqual(ui_text('fleet.lab.base_condition', name='작은 배치'), '작은 배치와 비교')
        self.assertIn('긴 문맥', words)

    def test_the_sessions_section_is_fleets_session_cells_the_persons_waits_first(self):
        from fleet.explorer.tables import session_marker
        from fleet.palette import style_components
        with mock.patch('fleet.session_tags.assigned_tag', lambda harness, sid: TAGS.get((harness, sid))):
            summary = shell.session_summary(view_model(), 'claude', OWN_SID)
        self.assertEqual(summary['count'], 4)
        self.assertEqual([''.join(p['text'] for p in row['tag']) for row in summary['rows']], ['[12]', '[e9]', '[73]', '[e7]'])
        self.assertEqual([row['own'] for row in summary['rows']], [False, True, False, False])
        self.assertEqual([c[0]['text'] for c in summary['counts']], ['◑1', '⠏2', '●1'])
        # Each glyph in Fleet's own key and color.
        for row, session in zip(summary['rows'], [SESSIONS[3], SESSIONS[0], SESSIONS[1], SESSIONS[2]]):
            glyph, key = session_marker(session)
            self.assertEqual(row['glyph'], [{'text': glyph, **style_components(key)}])

    def test_another_sessions_question_is_answered_with_that_session_as_its_owner(self):
        import human_wait_state
        import questions
        item = {'id': 'S4:other', 'source': 'S4', 'owner': {'harness': 'claude', 'session_id': OTHER_SID}}
        sent = []

        def answer(args):
            sent.append(args)
            print(json.dumps({'id': args.id, 'state': 'submitted'}))
            return 0
        binding = {**self.binding, 'harness': 'codex', 'sid': '00000000-0000-4000-8000-0000000000c2'}
        request = {'question_id': 'S4:other', 'revision': 'a' * 64, 'answers': {'lr': ['3e-4']},
                   'owner': {'harness': 'claude', 'session_id': OTHER_SID}}
        with mock.patch.object(human_wait_state, 'cached_view', lambda request=False: {'items': [item]}), \
                mock.patch.object(questions, '_find', lambda board, wanted: (item, None) if wanted == 'S4:other' else (None, 'no')), \
                mock.patch.object(questions, '_answer', answer), \
                mock.patch.object(shell, 'answer_attachment_current', lambda value: False):
            result = shell._block_answer(binding, request)
            # That session is the exact owner the questions API binds; this window's own Codex
            # attachment speaks only for its own session's questions.
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].expected_owner, {'harness': 'claude', 'session_id': OTHER_SID})
            self.assertFalse(hasattr(sent[0], 'answer_attachment'))
            # The same request named for another owner, or malformed, never answers.
            for owner in ({'harness': 'claude', 'session_id': 'someone-else'}, {'harness': 'claude'}, 'x',
                          {'harness': 'other', 'session_id': OTHER_SID}):
                self.assertEqual(shell._block_answer(binding, {**request, 'owner': owner})['reason'], 'question-changed', owner)
            # Without an owner, this window's own question (its Codex attachment checked as before).
            self.assertEqual(shell._block_answer(binding, {k: v for k, v in request.items() if k != 'owner'})['reason'],
                             'current-session-unverified')
        self.assertEqual(len(sent), 1)

    def test_another_sessions_waits_read_with_where_they_are_answered(self):
        import human_wait_state
        view = {'items': [{'id': 'S4:other', 'owner': {'harness': 'claude', 'session_id': OTHER_SID}}]}
        own = {'id': 'S4:open', 'revision': 'a' * 64, 'answerable': True, 'questions': [{'id': 'lr', 'question': '학습률은?', 'choices': ['1e-4', '3e-4']}]}
        native = {'id': 'S1:native', 'revision': 'b' * 64, 'answerable': False, 'reason': 'native-window', 'source': 'S1',
                  'questions': [{'id': 'q', 'question': '이 도구를 허용할까요?'}]}

        def board(harness, sid, view=None, elsewhere=None, **_kw):
            self.assertEqual((harness, sid), ('claude', OTHER_SID))
            elsewhere.append(native)
            return [dict(own)]
        with mock.patch.object(human_wait_state, 'cached_view', lambda request=False: view), \
                mock.patch.object(shell, 'session_questions', board), \
                mock.patch('fleet.session_tags.assigned_tag', lambda harness, sid: TAGS.get((harness, sid))):
            got = shell._panel_popup(self.binding, {'kind': 'questions', 'columns': 72, 'tag': '12'})
            self.assertEqual(got['owner'], {'harness': 'claude', 'session_id': OTHER_SID})
            self.assertEqual([item['id'] for item in got['questions']], ['S4:open', 'S1:native'])
            # A wait with no owner binding to answer it here says where it is answered.
            self.assertEqual(got['questions'][1]['answer_state'], 'answer_there')
            self.assertFalse(got['questions'][1]['answerable'])
            self.assertTrue(all(question.get('display') for item in got['questions'] for question in item['questions']))
            # This window's own number, or a number no session owns, reads nothing.
            self.assertEqual(shell._panel_popup(self.binding, {'kind': 'questions', 'columns': 72, 'tag': 'e9'})['questions'], [])
            self.assertEqual(shell._panel_popup(self.binding, {'kind': 'questions', 'columns': 72, 'tag': '99'})['questions'], [])
            self.assertFalse(shell._panel_popup(self.binding, {'kind': 'questions', 'columns': 72, 'tag': 'a b'})['ok'])
        self.assertFalse(shell._panel_popup(self.binding, {'kind': 'nothing', 'columns': 72})['ok'])
        self.assertFalse(shell._panel_popup(self.binding, {'kind': 'routes', 'columns': 5})['ok'])

    def test_the_editor_answers_another_sessions_question_apart_from_its_own(self):
        # The editor (block-input) keeps this session's questions while another session's stand, sends the
        # other's answer with that session as owner, once, and brings its own back when left.
        script = r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
const sent=[];
const b=create({changed(){},answer:async f=>{sent.push(f);return {state:'submitted'};},action(){}});
const own=[{id:'S4:own',revision:'o'.repeat(64),answerable:true,questions:[{id:'q',question:'내 질문?',choices:['a','b']}]}];
b.update(own,'',{labels:{},styles:{}});
b.enterForeign({tag:'12',owner:{harness:'claude',session_id:'other'},items:[{id:'S4:other',revision:'c'.repeat(64),answerable:true,questions:[{id:'lr',question:'학습률?',choices:['1e-4','3e-4']}]}]});
assert.equal(b.foreign.tag,'12');assert(b.focused);
// The panel's summary stays this session's own question.
assert.equal(b.summary().rows[0][1].text,'내 질문?');
// An update of this session's own list while the other stands changes nothing shown.
b.update([...own,{id:'S4:own2',revision:'p'.repeat(64),answerable:true,questions:[{id:'q2',question:'둘째?'}]}],'',{});
assert(b.detail({budget:8,columns:60}).some(row=>row.some(p=>String(p.text).includes('학습률?'))));
b.input(Buffer.from('2'));b.input(Buffer.from('\r'));await new Promise(r=>setTimeout(r,20));
assert.deepEqual(sent,[{question_id:'S4:other',revision:'c'.repeat(64),answers:{lr:['3e-4']},owner:{harness:'claude',session_id:'other'}}]);
// Sent: the popup closed, the other session's list left, this session's own (with the update) back.
assert.equal(b.focused,false);assert.equal(b.foreign,null);
assert.equal(b.summary().count,2);assert.deepEqual(b.references().map(r=>r.id),['S4:own','S4:own2']);
// Its own question answers without an owner, as always.
b.enter();b.input(Buffer.from('\r'));await new Promise(r=>setTimeout(r,20));
assert.deepEqual(sent[1],{question_id:'S4:own',revision:'o'.repeat(64),answers:{q:['a']}});
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        out = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/block-input.cjs')],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == '__main__':
    unittest.main()
