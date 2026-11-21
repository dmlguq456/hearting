"""11-21 (the person chose design C, "정할 것 먼저"): the status popup opens on a list, what waits for the person first.

For the generated campaign page (its first line is the marker) the popup's first screen is a list: the head, `기다리는 것` (every
bullet, each marked `!`), `하는 중` (three, then `all N ›`), every other heading as a folded line with its count, and
`full document ›`. The chosen line, the opened groups and items are the popup's own state; `full document ›` is the screen the popup
always had (Fleet's lines and the whole document), Esc or Enter going back to the list. Any other document (a hand-written page, none, an
unreadable or cut one) draws the rows it always drew.

Part A is the list logic (`status-list.cjs`) in Node on the backend's real answer for one generated page: the order and the marks, the
choice and what Enter does on each kind of line, a document read again. Part B is the backend's answer (`list` for the generated page
only; the list gives way to nothing and the rows give way to it). Part C runs the real host in a private PTY over a synthetic root:
the keys, a press, the way back, the arrows still moving between sections, a document that changes under an open popup, the time
the first screen takes and that it asks nothing of Fleet. Nothing here reaches a real account, session, artifact root or terminal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
import importlib.util
import json
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SID = '00000000-0000-4000-8000-0000000000e1'
MARKER = '<!-- hearting:campaign-document v1 -->'
PAGE = MARKER + """
# LISTTITLE 캠페인

갱신 2026-11-21 11:02

**한 줄**: LISTONE 한 줄 요약

## 하는 중
- RUN_ONE 일 하나 · #11 · 기록 1
- RUN_TWO 일 둘 · #12 · 기록 2
- RUN_THREE 일 셋 · 기록 1
- RUN_FOUR 일 넷 · 기록 1
- RUN_FIVE 일 다섯 · 기록 1
6시간 넘게 움직임 없는 열린 일 23개는 접어 둠.

## 기다리는 것
- 결정: Q_ONE 첫 질문입니다. 권장은 가 안이고 이 글은 한 줄에 다 들어가지 않을 만큼 길게 이어지다가 Q_ONE_END 에서 끝납니다 그리고 더 이어집니다 마지막 Q_ONE_LAST
- Q_TWO 둘째 질문?
질문판 기준 11:01

## 최근에 끝난 것
- 11-21 DONE_ONE 끝난 일 · #1 · v4.70.2 · 기록 6
- 11-20 DONE_TWO 끝난 일 · 기록 1
앞서 끝난 기록 1,392개는 접어 둠.

## 나중에 할 것
- LATER_ONE 나중 일
"""


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'utilities' / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# -- Part A and B: the list logic and the backend's answer ---------------------------------------------------------------

UNIT = r"""
const fs=require('node:fs'),path=require('node:path');
const list=require(path.join(process.argv[2],'status-list.cjs'));
const answer=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const entries=list.entriesOf(answer),text=row=>row.map(p=>p.text).join('');
const state=()=>({sel:'',fold:new Set(),items:new Set()});
const lines=v=>v.rows.map(text);
const out={};
let st=state(),v=list.layout(entries,st);
out.first=lines(v);
out.first_sel=v.sel;out.first_fell=v.fellBack;
out.tinted=v.rows.map((r,i)=>r.some(p=>p.tint)?i:-1).filter(i=>i>=0);
out.marks=v.marks.map(m=>m.key.split('\x1f')[0]);
// moves: the ends stay, one choice at a time
const walk=[];let k=v.sel;for(let i=0;i<20;i++){const n=list.neighbour(list.layout(entries,{...st,sel:k}),1);walk.push(n.split('\x1f')[0]);if(n===k)break;k=n;}
out.walk=walk;
out.up_at_top=list.neighbour(v,-1)===v.sel;
// Enter on a folded heading opens it, again folds it
st=state();st.sel='g:최근에 끝난 것';v=list.layout(entries,st);
out.group_press=list.press(st,v,'g:최근에 끝난 것');out.group_open=lines(list.layout(entries,st));
list.press(st,list.layout(entries,st),'g:최근에 끝난 것');out.group_closed=lines(list.layout(entries,st)).filter(l=>l.includes('DONE_ONE')).length;
// all N opens the whole running list
st=state();list.press(st,list.layout(entries,st),'a:하는 중');out.all_open=lines(list.layout(entries,st));
// an item opens its whole text under it
st=state();const item=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:기다리는 것'));
const W={width:60,measure:t=>[...t].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)};
const before=lines(list.layout(entries,st,W)).length;
out.item_press=list.press(st,list.layout(entries,st),item.key);
const opened=list.layout(entries,st,W);out.item_more=lines(opened).length-before;out.item_sel=opened.sel===item.key;
const under=lines(opened).slice(opened.at+1,opened.at+1+out.item_more);
out.item_under_fits=under.every(l=>W.measure(l)<=60);out.item_under_indent=under.every(l=>l.startsWith('    '));
out.item_whole=under.map(l=>l.trim()).join('').replace(/ /g,'')===entries.find(e=>e.key===item.key).text.replace(/ /g,'');
list.press(st,opened,item.key);out.item_closed=lines(list.layout(entries,st,W)).length===before;
out.wrap=[list.wrap('가나 다라',4,W.measure),list.wrap('',4,W.measure),list.wrap('abcdefgh',3,W.measure)];
// full document
out.full=list.press(st,list.layout(entries,st),'full');
// a document read again: the same words keep the choice (and the open group by its name); a line gone falls to the top
st=state();list.press(st,list.layout(entries,st),'g:최근에 끝난 것');
const doneKey=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:최근에 끝난 것')).key;st.sel=doneKey;
const reread=list.layout(entries,st);out.reread_same=[reread.sel===doneKey,reread.fellBack];
const gone=list.layout(entries.filter(e=>e.key!==doneKey),st);out.reread_gone=[gone.sel===list.layout(entries.filter(e=>e.key!==doneKey),state()).sel,gone.fellBack];
out.reread_open_kept=lines(gone).some(l=>l.includes('›')===false&&l.includes('▾'));
// a screen with nothing to choose
const none=list.layout([{rows:[[{text:'x'}]]}],state());out.none=[none.sel,none.marks.length,none.rows.length];
out.hits=list.hits(list.layout(entries,state())).slice(0,2);
console.log(JSON.stringify(out));
"""


class ListLogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('needs node')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsl-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]
        import session_shell
        from fleet import session_status, session_tags
        cls.m, cls.status, cls.tags = session_shell, session_status, session_tags

    def answer(self, body, name='campaign.md', width=96, age=600):
        folder = self.root / ('c%d' % time.monotonic_ns()) / 'campaigns' / '2026-11-10_alpha'
        folder.mkdir(parents=True)
        page = folder / name
        page.write_text(body, encoding='utf-8')
        stamp = time.time() - age
        os.utime(page, (stamp, stamp))
        view = {'word': 'STATUS', 'has_doc': True, 'campaign': 'alpha', 'age': '', 'title': 'STATUS alpha', 'path': str(page),
                'slot': '', 'watch': []}
        with mock.patch.object(self.status, 'held', lambda cwd, claim, now=None: view):
            return self.m._status_popup({'harness': 'claude', 'sid': SID}, {'cwd': str(self.root), 'routes': [], 'status': {}, 'part': 'doc'},
                                        width, 256, True)

    def test_the_backend_carries_the_list_for_the_generated_page_only(self):
        generated = self.answer(PAGE)
        self.assertTrue(generated['ok'])
        self.assertTrue(generated['list'])
        self.assertTrue(generated['rows'])                              # the whole document stays: `full document ›` reads it
        for body, name in (('# 현황\n\n## 다음\n- 하나\n', '현황.md'), (PAGE.split('\n', 1)[1], 'campaign.md')):
            hand = self.answer(body, name)
            self.assertTrue(hand['ok'])
            self.assertNotIn('list', hand, name)

    def test_the_list_gives_way_to_nothing_and_the_rows_give_way_to_it(self):
        # A page with a long list in it: the list is kept whole (to the cap), the rows of the document give way to it.
        many = PAGE.replace('- LATER_ONE 나중 일', ''.join('- 일 %04d ' % n + '가나다라마바사 ' * 6 + '\n' for n in range(70)) + '\n- LATER_ONE 나중 일')
        many = many.replace('## 하는 중', '## 하는 중\n' + ''.join('- 하는 %04d ' % n + '가나다라마바사 ' * 6 + '\n' for n in range(70)))
        answer = self.answer(many, width=96)
        self.assertTrue(answer['ok'])
        self.assertLessEqual(len(json.dumps(answer, ensure_ascii=False).encode()), self.m.POPUP_ANSWER_BYTES)
        self.assertTrue(answer.get('list'), 'a list that fits alone is kept')
        keys = [entry['key'] for entry in answer['list'] if entry.get('key')]
        self.assertIn('full', keys)
        self.assertTrue(any(key.startswith('i:하는 중') for key in keys))
        self.assertLessEqual(len([key for key in keys if key.startswith('i:나중에')]), self.status.LIST_ITEMS)
        # A list that alone is more than an answer may weigh is left out: the popup then draws the document as it did.
        huge = PAGE.replace('- LATER_ONE 나중 일', ''.join('- 일 %04d ' % n + '가나다라마바사 ' * 8 + '\n' for n in range(900)))
        for group in ('## 하는 중', '## 기다리는 것', '## 최근에 끝난 것'):
            huge = huge.replace(group, group + '\n' + ''.join('- 또 %s %04d ' % (group[3:5], n) + '가나다라마바사 ' * 8 + '\n' for n in range(self.status.LIST_ITEMS)))
        big = self.answer(huge, width=96)
        self.assertTrue(big['ok'])
        self.assertNotIn('list', big)
        self.assertLessEqual(len(json.dumps(big, ensure_ascii=False).encode()), self.m.POPUP_ANSWER_BYTES)

    def test_the_list_logic_on_the_real_answer(self):
        answer = self.answer(PAGE)
        path = self.root / 'answer.json'
        path.write_text(json.dumps(answer, ensure_ascii=False))
        unit = self.root / 'unit.cjs'
        unit.write_text(UNIT)
        done = subprocess.run([self.node, str(unit), str(ROOT / 'utilities/session-shell'), str(path)], capture_output=True, text=True,
                              timeout=60, env={'PATH': os.environ.get('PATH', '/usr/bin')})
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        first = out['first']
        # The first screen, in the designed order, the first line chosen (a mark `>` and the chosen ground, not the colour alone).
        self.assertTrue(first[0].startswith('LISTTITLE'), first)
        self.assertTrue(first[1].startswith('updated 11-21 11:02 · '))
        self.assertIn('LISTONE', first[2])
        at = first.index('기다리는 것 2')
        self.assertTrue(first[at + 1].startswith('> ! 결정: Q_ONE'), first[at:at + 3])
        self.assertTrue(first[at + 1].endswith('…'))                     # cut to the width; the whole text is under it when opened
        self.assertEqual(first[at + 2], '  ! Q_TWO 둘째 질문?')
        self.assertEqual(first[at + 3], '    질문판 기준 11:01')
        run = first.index('하는 중 5')
        self.assertEqual(first[run + 1:run + 4], ['  RUN_ONE 일 하나 · #11 · 기록 1', '  RUN_TWO 일 둘 · #12 · 기록 2', '  RUN_THREE 일 셋 · 기록 1'])
        self.assertEqual(first[run + 4], '  all 5 ›')
        self.assertFalse([line for line in first if 'RUN_FOUR' in line or 'DONE_ONE' in line or 'LATER_ONE' in line])
        self.assertEqual([line for line in first if line.lstrip().startswith('›')], ['  › 최근에 끝난 것 2', '  › 나중에 할 것 1'])
        self.assertEqual(first[-1], '  full document ›')
        self.assertEqual(out['tinted'], [at + 1])
        self.assertEqual(out['first_sel'].split('\x1f')[0], 'i:기다리는 것')
        # Everything that can be chosen, in order, and the ends stay.
        self.assertEqual(out['marks'], ['i:기다리는 것', 'i:기다리는 것', 'i:하는 중', 'i:하는 중', 'i:하는 중', 'a:하는 중', 'g:최근에 끝난 것', 'g:나중에 할 것', 'full'])
        self.assertEqual(out['walk'][-1], 'full')
        self.assertEqual(len(out['walk']), 9)
        self.assertTrue(out['up_at_top'])
        # Enter: a heading opens (and folds), `all N ›` opens the whole running list, an item its whole text, `full document ›` the document.
        self.assertEqual(out['group_press'], 'fold')
        self.assertIn('  ▾ 최근에 끝난 것 2'.replace('  ▾', '> ▾'), out['group_open'])
        self.assertTrue(any('DONE_ONE' in line for line in out['group_open']))
        self.assertIn('    앞서 끝난 기록 1,392개는 접어 둠.', out['group_open'])
        self.assertEqual(out['group_closed'], 0)
        self.assertTrue(any('RUN_FIVE' in line for line in out['all_open']))
        self.assertIn('> all 5 ▾', out['all_open'])
        self.assertEqual(out['item_press'], 'item')
        self.assertGreaterEqual(out['item_more'], 2)
        self.assertTrue(out['item_under_fits'] and out['item_under_indent'] and out['item_whole'], out)     # whole, in the width, under the words
        self.assertEqual(out['wrap'], [['가나', '다라'], [''], ['abc', 'def', 'gh']])
        self.assertTrue(out['item_sel'] and out['item_closed'])
        self.assertEqual(out['full'], 'full')
        # A document read again: the same words keep the choice, a line gone falls to the top.
        self.assertEqual(out['reread_same'], [True, False])
        self.assertEqual(out['reread_gone'], [True, True])
        self.assertTrue(out['reread_open_kept'])
        self.assertEqual(out['none'], ['', 0, 1])
        self.assertEqual(out['hits'][0]['action'][0], 'status')


# -- Part C: the real host, a private PTY -----------------------------------------------------------------------------------

fast = None


SCENARIO = r"""
const checks={};
const rowsOf=s=>{const b=box(s);return b?b.rows.slice(1,-1).map(l=>l.replace(/^│\s?/,'').replace(/\s*│.*$/,'')):[];};
const chosen=s=>rowsOf(s).filter(l=>l.startsWith('> '));
const shows=word=>s=>rowsOf(s).some(l=>l.includes(word));
const frame=s=>box(s)?box(s).rows:[];
(async()=>{try{
  await until(hasButton,'the button');await sleep(500);
  const v0=count('vm'),r0=count('read-start');
  // The first screen: the document only, under a second, and nothing asked of Fleet.
  const began=Date.now();await opened();
  let r=await until(shows('full document ›'),'the list screen');
  checks.first_ms=Date.now()-began;checks.first_vm=count('vm')-v0;
  checks.first_rows=rowsOf(r.s);checks.first_chosen=chosen(r.s);checks.title=frame(r.s)[0];checks.keys=frame(r.s).slice(-3,-1).join(' ');
  checks.whole_doc_on_first=shows('갱신 2026')(r.s);
  // ↓ moves the choice, Tab too, ↑ back.
  await key('\x1b[B');checks.down=chosen(await screen());
  await key('\t');checks.tab=chosen(await screen());
  await key('\x1b[A');checks.up=chosen(await screen());
  await key('\x1b[Z');checks.shift_tab=chosen(await screen());
  // Enter opens the whole text of the item under it; again closes it.
  await key('\r');r=await until(shows('Q_ONE_LAST'),'the item opened');checks.item_open=rowsOf(r.s).filter(l=>l.includes('Q_ONE')||l.includes('Q_ONE_LAST')).length;
  checks.item_end_cut=rowsOf(r.s)[rowsOf(r.s).findIndex(l=>l.startsWith('> '))].endsWith('…');
  await key('\r');await until(s=>!shows('Q_ONE_LAST')(s),'the item closed');
  // Down to `all 5 ›`: Enter shows the whole running list.
  for(let i=0;i<5;i++)await key('\x1b[B',120);
  checks.at_all=chosen(await screen());
  await key('\r');r=await until(shows('RUN_FIVE'),'all of the running list');checks.all_open=chosen(r.s);
  await key('\r');await until(s=>!shows('RUN_FIVE')(s),'the running list folded again');
  // A folded heading: Enter opens it in place (▾), Enter again folds it.
  await key('\x1b[B',150);checks.at_group=chosen(await screen());
  await key('\r');r=await until(shows('DONE_ONE'),'the folded group opened');checks.group_open=chosen(r.s);checks.group_notes=shows('앞서 끝난 기록 1,392개는 접어 둠.')(r.s);
  await key('\r');await until(s=>!shows('DONE_ONE')(s),'the group folded');
  // ←→ are still the sections: → leaves the status popup, ← comes back to a fresh list.
  await key('\x1b[C',350);r=await screen();checks.right_title=frame(r)[0]||'';
  checks.right_not_list=!shows('full document ›')(r);
  await key('\x1b[D',350);r=await until(shows('full document ›'),'back at the status popup by ←');checks.left_chosen=chosen(r.s);
  // A press on a folded heading opens it (the wheel and PgUp/PgDn are the popup's own).
  const spot=await at('나중에 할 것');await press(spot.x,spot.y,300);r=await until(shows('LATER_ONE'),'the press opened the group');checks.press_open=shows('▾ 나중에 할 것')(r.s);
  const again=await at('▾ 나중에 할 것');await press(again.x,again.y,300);await until(s=>!shows('LATER_ONE')(s),'the press folded the group');   // (the popup is centred: it moved as it grew)
  // `full document ›`: the screen the popup always had, Fleet's lines asked for only now; Esc (then Enter) goes back, Esc again closes.
  const vBefore=count('vm');
  await key('\x1b[B',150);await key('\x1b[B',150);await key('\x1b[B',150);checks.at_full=chosen(await screen());
  await key('\r',300);r=await until(shows('갱신 2026-11-21 11:02'),'the document read whole');
  checks.full_has_table_word=shows('LISTONE')(r.s);checks.full_list_gone=!shows('full document ›')(r.s);
  checks.full_keys=frame(r.s).slice(-3,-1).join(' ');
  r=await until(shows('FLEET_STATUS_LINE'),'Fleet\'s line in the full document',15000);checks.fleet_in_full=count('vm')-vBefore;
  await key('\x1b',400);r=await screen();checks.esc_back=shows('full document ›')(r)&&!!box(r);checks.esc_back_chosen=chosen(r);
  await key('\r',300);await until(shows('갱신 2026-11-21 11:02'),'the document again');
  await key('\r',300);r=await until(shows('full document ›'),'Enter went back');checks.enter_back=!!box(r.s);
  await key('\x1b',400);await until(s=>!box(s),'Esc closed it');checks.esc_closed=true;
  // A document that changes under an open popup: the chosen line stays on its words; a group stays open by its name; a line gone falls to the top.
  await opened();await until(shows('full document ›'),'the list again');
  await key('\x1b[B',150);checks.before_edit=chosen(await screen());           // Q_TWO
  await key('\x1b[B',150);await key('\x1b[B',150);await key('\x1b[B',150);await key('\x1b[B',150);await key('\x1b[B',150); // → the folded `최근에 끝난 것`
  await key('\r',200);await until(shows('DONE_ONE'),'the group opened before the change');
  await key('\x1b[B',150);checks.on_done=chosen(await screen());
  edit('- 결정: Q_ONE','- 결정: Q_ONE_EDITED');await sleep(2800);
  r=await until(shows('Q_ONE_EDITED'),'the changed document in the list',15000);
  checks.after_edit=chosen(r.s);checks.group_still_open=shows('DONE_ONE')(r.s);
  edit('- 11-21 DONE_ONE','- 11-21 DONE_ONE_REWORDED');await sleep(2800);
  r=await until(shows('DONE_ONE_REWORDED'),'the reworded line',15000);
  checks.reworded=chosen(r.s);checks.reworded_top=shows('LISTTITLE')(r.s);
  await close();
  fs.writeFileSync(cfg.result,JSON.stringify({checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""

HAND = r"""
const checks={};
const rowsOf=s=>{const b=box(s);return b?b.rows.slice(1,-1).map(l=>l.replace(/^│\s?/,'').replace(/\s*│.*$/,'')):[];};
(async()=>{try{
  await until(hasButton,'the button');await sleep(500);
  await opened();
  let r=await until(popupHas('HANDDOC_ONE'),'the hand-written page');
  r=await until(popupHas('FLEET_STATUS_LINE'),'Fleet\'s lines above it',15000);
  checks.rows=rowsOf(r.s);checks.no_list=!popupHas('full document ›')(r.s);
  await key('\x1b[B');await key('\r');checks.after_keys=rowsOf(await screen());
  await key('\x1b',400);await until(s=>!box(s),'Esc closed it at once');checks.esc_closed=true;
  fs.writeFileSync(cfg.result,JSON.stringify({checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""


class PtyTest(unittest.TestCase):
    """Part C: the real host over a synthetic root."""

    @classmethod
    def setUpClass(cls):
        global fast
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        fast = load('shell_status_list_fast', 'session_shell_status_popup_fast.test.py')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsl2-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_status_list_isolation', ROOT / 'tools/run-tests.py')
        runner = importlib.util.module_from_spec(spec); sys.modules[spec.name] = runner; spec.loader.exec_module(runner)
        cls.env = runner.build_isolated_env(cls.root, repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR'] = str(cls.root / 'native-claude')
        cls.env['FLEET_NOW_LANG'] = 'en'
        policy = Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME']) / '.config') / 'hearting/session-shell.json'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({'enabled': True, 'layout': 'side', 'side_width': 40, 'side_min_columns': 132}))
        policy.chmod(0o600)
        cls.env['npm_config_cache'] = str(cls.root / 'npm-cache')
        cls.env['npm_config_devdir'] = str(cls.root / 'node-gyp')
        cls.env['npm_config_userconfig'] = str(cls.root / 'npmrc'); (cls.root / 'npmrc').write_text('')
        sys.path[:0] = [str(ROOT / 'tools/install'), str(ROOT / 'utilities')]
        import session_shell_install
        with mock.patch.dict(os.environ, cls.env, clear=True):
            assets = session_shell_install.prepare_assets()
        cls.env['NODE_PATH'] = str(assets / 'node_modules')
        cache = Path(cls.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        cls.lock = os.open(cache / '.projection.lock', os.O_CREAT | os.O_RDWR, 0o600)
        cls.addClassCleanup(os.close, cls.lock)
        fcntl.flock(cls.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(fast.FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(fast.LAUNCHER)
        prelude = fast.DRIVER.split('const checks={};')[0]
        cls.drivers = {}
        for name, body in (('list', SCENARIO), ('hand', HAND)):
            path = cls.root / ('driver-%s.cjs' % name); path.write_text(prelude + body); cls.drivers[name] = path

    def run_case(self, scenario):
        case = self.root / scenario; case.mkdir(mode=0o700)
        control = case / 'control'; control.mkdir()
        project, doc = fast.make_root(case / 'tree', campaigns=4)
        if scenario == 'list':
            page = doc.parent / 'campaign.md'
            page.write_text(PAGE, encoding='utf-8')
            doc = page
        else:
            doc.write_text('# HANDDOC_ONE 현황\n\n**한 줄**: 손으로 쓴 문서\n\n## 다음\n- HANDDOC_TWO\n\n| 가 | 나 |\n|--|--|\n| 1 | 2 |\n', encoding='utf-8')
        key = 'claude\0' + SID
        target = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits/projection.json'
        value = json.loads(target.read_text()) if target.exists() else {
            'schema_version': 3, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
            'route_panels': {}, 'route_status': {}, 'route_sessions': {}}
        value.update(observed_at=time.time())
        value['route_panels'][key] = [fast.route_row('rt-b000000000000001', '현황 팝업 구현')]
        value['route_status'][key] = 'route 1 ⠏1'; value['route_sessions'][key] = time.time() + 3600
        target.write_text(json.dumps(value)); target.chmod(0o600)
        vm = case / 'vm.json'
        vm.write_text(json.dumps({'repos': [{'id': 'proj', 'name': 'proj', 'status': {
            'routes': 1, 'questions': 0, 'campaigns': [{'campaign': 'camp-000', 'line': 'FLEET_STATUS_LINE'}], 'next': '다음 일'}}],
            'system': {}, 'routes': [], 'sessions': [{'tag': 'e1', 'repo_id': 'proj', 'harness': 'claude', 'state': 'idle', 'title': '이 창'}]},
            ensure_ascii=False))
        backend = case / 'backend'
        backend.write_text(fast.BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'),
                                               sid=SID, vm=str(vm), control=str(control)))
        backend.chmod(0o700)
        config = {'harness': 'claude', 'sid': SID, 'key': key, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'cache': str(target),
                  'project': str(project), 'result': str(case / 'result.json'), 'control': str(control),
                  'doc': str(doc), 'scenario': scenario, 'unreadable': ''}
        (case / 'config.json').write_text(json.dumps(config))
        env = dict(self.env, LOOP_CONFIG=str(case / 'config.json'))
        child = subprocess.Popen([self.node, str(self.drivers[scenario])], env=env, cwd=project, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM); child.communicate(timeout=10); raise
        diagnostic = (case / 'display-errors').read_text() if (case / 'display-errors').exists() else ''
        self.assertEqual(child.returncode, 0, err + diagnostic)
        return json.loads((case / 'result.json').read_text())['checks']

    def test_the_popup_opens_on_the_list_and_every_key_does_what_the_screen_says(self):
        c = self.run_case('list')
        print(json.dumps({'event': 'status-list-pty', 'first_ms': c['first_ms'], 'first_vm': c['first_vm']}), flush=True)
        # The first screen: the document only, quickly, nothing asked of Fleet; the list and not the whole document.
        self.assertLess(c['first_ms'], 1500, c)
        self.assertEqual(c['first_vm'], 0, c)
        self.assertFalse(c['whole_doc_on_first'], c)
        rows = c['first_rows']
        self.assertTrue(rows[0].startswith('LISTTITLE'), rows)
        self.assertTrue(any(row.startswith('기다리는 것 2') for row in rows), rows)
        self.assertEqual(len(c['first_chosen']), 1, c)
        self.assertTrue(c['first_chosen'][0].startswith('> ! 결정: Q_ONE'), c)
        self.assertIn('STATUS', c['title'])
        self.assertIn('Enter open', c['keys'])
        self.assertIn('←→ section', c['keys'])                          # the popups' own keys row
        # The choice moves with the arrows, Tab and Shift+Tab.
        self.assertTrue(c['down'][0].startswith('> ! Q_TWO'), c)
        self.assertTrue(c['tab'][0].startswith('> RUN_ONE'), c)
        self.assertTrue(c['up'][0].startswith('> ! Q_TWO'), c)
        self.assertTrue(c['shift_tab'][0].startswith('> ! 결정: Q_ONE'), c)
        # An item opens its whole text under it, and closes.
        self.assertTrue(c['item_end_cut'], c)
        self.assertGreaterEqual(c['item_open'], 2, c)
        # `all N ›`, a folded heading: Enter opens in place, again folds.
        self.assertIn('all 5 ›', c['at_all'][0])
        self.assertIn('all 5 ▾', c['all_open'][0])
        self.assertIn('› 최근에 끝난 것 2', c['at_group'][0])
        self.assertIn('▾ 최근에 끝난 것 2', c['group_open'][0])
        self.assertTrue(c['group_notes'], c)
        # ←→ are still the sections.
        self.assertNotIn('STATUS', c['right_title'].split('─')[1] if '─' in c['right_title'] else c['right_title'])
        self.assertTrue(c['right_not_list'], c)
        self.assertTrue(c['left_chosen'] and c['left_chosen'][0].startswith('> ! 결정: Q_ONE'), c)        # a fresh list
        self.assertTrue(c['press_open'], c)
        # `full document ›` is the screen the popup always had; Fleet is asked for only then; Esc and Enter go back, Esc again closes.
        self.assertIn('full document ›', c['at_full'][0])
        self.assertTrue(c['full_has_table_word'] and c['full_list_gone'], c)
        self.assertIn('back to the list', c['full_keys'])
        self.assertEqual(c['fleet_in_full'], 1, c)
        self.assertTrue(c['esc_back'], c)
        self.assertIn('full document ›', c['esc_back_chosen'][0])                          # on the line it left
        self.assertTrue(c['enter_back'] and c['esc_closed'], c)
        # A document that changes under the popup.
        self.assertTrue(c['before_edit'][0].startswith('> ! Q_TWO'), c)
        self.assertIn('DONE_ONE', c['on_done'][0])
        self.assertIn('DONE_ONE', c['after_edit'][0], c)                                    # the same words keep the choice
        self.assertTrue(c['group_still_open'], c)                                           # the group stays open by its name
        self.assertTrue(c['reworded'][0].startswith('> ! 결정: Q_ONE_EDITED'), c)             # a line gone: the top
        self.assertTrue(c['reworded_top'], c)

    def test_a_hand_written_page_is_the_popup_it_was(self):
        c = self.run_case('hand')
        self.assertTrue(c['no_list'], c)
        self.assertTrue(any('HANDDOC_ONE' in row for row in c['rows']), c)
        self.assertTrue(any('HANDDOC_TWO' in row for row in c['rows']), c)
        self.assertTrue(any('가' in row and '나' in row and '|' not in row for row in c['rows']), c)   # the table aligned, as before
        self.assertEqual(c['after_keys'][:3], c['rows'][:3], c)                                       # ↓ and Enter do nothing there
        self.assertTrue(c['esc_closed'], c)


if __name__ == '__main__':
    unittest.main()
