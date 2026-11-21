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
out.marks=v.marks.map(m=>m.key.split(':')[0]);
const head=name=>entries.find(e=>e.key&&e.key.startsWith('g:')&&text(e.rows[0]).includes(name));
const G=head('최근에 끝난 것'),A=entries.find(e=>e.key&&e.key.startsWith('a:'));
// moves: the ends stay, one choice at a time
const walk=[];let k=v.sel;for(let i=0;i<20;i++){const n=list.neighbour(list.layout(entries,{...st,sel:k}),1);walk.push(n.split(':')[0]);if(n===k)break;k=n;}
out.walk=walk;
out.up_at_top=list.neighbour(v,-1)===v.sel;
// Enter on a folded heading opens it, again folds it
st=state();st.sel=G.key;v=list.layout(entries,st);
out.group_press=list.press(st,v,G.key);out.group_open=lines(list.layout(entries,st));
list.press(st,list.layout(entries,st),G.key);out.group_closed=lines(list.layout(entries,st)).filter(l=>l.includes('DONE_ONE')).length;
// all N opens the whole running list
st=state();list.press(st,list.layout(entries,st),A.key);out.all_open=lines(list.layout(entries,st));
// an item whose row was cut opens its whole words in place of the row (the row is not shown twice); one that was not cut has nothing to open
st=state();const item=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:')&&m.text);
const W={width:60,measure:t=>[...t].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)};
const closed=list.layout(entries,st,W),before=lines(closed).length;
out.item_cut_has_text=!!item;
out.item_press=list.press(st,list.layout(entries,st),item.key);
const opened=list.layout(entries,st,W);out.item_more=lines(opened).length-before;out.item_sel=opened.sel===item.key;
const own=lines(opened).slice(opened.at,opened.at+opened.span);
out.item_first=own[0];out.item_span=opened.span;
out.item_rest_indent=own.slice(1).every(l=>l.startsWith('    ')&&!l.startsWith('     '));
out.item_fits=own.every(l=>W.measure(l)<=60);
out.item_whole=own.map((l,i)=>i===0?l.replace(/^> ! /,''):l.trim()).join('').replace(/ /g,'')===entries.find(e=>e.key===item.key).text.replace(/ /g,'');
out.item_ends_cut=own[0].endsWith('…')||own[own.length-1].endsWith('…');
out.item_text_once=lines(opened).filter(l=>l.includes('Q_ONE_END')).length;
out.item_tinted=opened.rows.slice(opened.at,opened.at+opened.span).every(r=>r.some(p=>p.tint));
out.item_hits=list.hits(opened).filter(h=>h.action[1]===item.key).map(h=>h.row);out.item_at=opened.at;
out.item_neighbour_down=list.neighbour(opened,1)!==item.key;
list.press(st,opened,item.key);out.item_closed=lines(list.layout(entries,st,W)).length===before;
// a short item (not cut): Enter and a press change nothing but the choice; pressed again, nothing
st=state();const shortKey=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:')&&!m.text).key;
const noneOpen=lines(list.layout(entries,{...st,sel:shortKey},W));
out.short_press=list.press(st,list.layout(entries,st),shortKey);out.short_same=JSON.stringify(lines(list.layout(entries,st,W)))===JSON.stringify(noneOpen);
out.short_again=list.press(st,list.layout(entries,st),shortKey);out.short_items=st.items.size;
out.wrap=[list.wrap('가나 다라',4,W.measure),list.wrap('',4,W.measure),list.wrap('abcdefgh',3,W.measure)];
// full document
out.full=list.press(st,list.layout(entries,st),'full');
// a document read again: the same words keep the choice (and the open group by its name); a line gone falls to the top
st=state();list.press(st,list.layout(entries,st),G.key);
const doneKey=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:')&&m.group===G.group).key;st.sel=doneKey;
const reread=list.layout(entries,st);out.reread_same=[reread.sel===doneKey,reread.fellBack];
const gone=list.layout(entries.filter(e=>e.key!==doneKey),st);out.reread_gone=[gone.sel===list.layout(entries.filter(e=>e.key!==doneKey),state()).sel,gone.fellBack];
out.reread_open_kept=lines(gone).some(l=>l.includes('›')===false&&l.includes('▾'));
// a screen with nothing to choose
const none=list.layout([{rows:[[{text:'x'}]]}],state());out.none=[none.sel,none.marks.length,none.rows.length];
out.hits=list.hits(list.layout(entries,state())).slice(0,2);
console.log(JSON.stringify(out));
"""


# Review 3 and 4 on the front end: the backend's real answers go through the list module, which keeps what the answer carries.
FRONT = r"""
const fs=require('node:fs'),path=require('node:path');
const list=require(path.join(process.argv[2],'status-list.cjs'));
const cases=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const text=row=>row.map(p=>p.text).join(''),lines=v=>v.rows.map(text);
const measure=t=>[...t].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const out={};
const fresh=()=>({sel:'',fold:new Set(),items:new Set()});
const walk=entries=>{const st=fresh();let v=list.layout(entries,st),k=v.sel;const seen=[k];for(let i=0;i<400;i++){const n=list.neighbour(list.layout(entries,{...st,sel:k}),1);if(n===k)break;seen.push(n);k=n;}return seen;};
const folded=(entries,name)=>list.layout(entries,fresh()).marks.filter(m=>m.key.startsWith('g:')&&text(entries.find(e=>e.key===m.key).rows[0]).includes(name));
// two headings of the same words
{const e=list.entriesOf(cases.dup),seen=walk(e),st=fresh(),heads=folded(e,'Other');
 out.dup_walk=seen;out.dup_heads=heads.map(m=>m.key);out.dup_ids=heads.map(m=>m.group);
 list.press(st,list.layout(e,st),heads[0].key);let l=lines(list.layout(e,st));out.dup_first_open=[l.some(x=>x.includes('  A')),l.some(x=>x.includes('  B'))];
 list.press(st,list.layout(e,st),heads[1].key);l=lines(list.layout(e,st));out.dup_both_open=[l.some(x=>x.includes('  A')),l.some(x=>x.includes('  B'))];
 list.press(st,list.layout(e,st),heads[0].key);l=lines(list.layout(e,st));out.dup_second_only=[l.some(x=>x.includes('  A')),l.some(x=>x.includes('  B'))];
 const down=fresh();down.sel=heads[0].key;out.dup_down=list.neighbour(list.layout(e,down),1)===heads[1].key;
 const up=fresh();up.sel=heads[1].key;out.dup_up=list.neighbour(list.layout(e,up),-1)===heads[0].key;
 const solo=list.entriesOf(cases.dup_solo),one=folded(solo,'Other');out.dup_solo_id=one[0].group;
 const hit=list.hits(list.layout(e,fresh())).filter(h=>h.action[1]===heads[1].key);out.dup_hit=hit.length;}
// an item of 4000, 4001 and more characters: the end of it is in what opens
for(const name of ['long4000','long4001','long12000','scripts']){
 const e=list.entriesOf(cases[name]),W={width:60,measure},st=fresh();
 const item=list.layout(e,st,W).marks.find(m=>m.key.startsWith('i:')&&m.text&&e.find(x=>x.key===m.key).text.endsWith('끝'));
 const whole=e.find(x=>x.key===item.key).text;
 st.sel=item.key;list.press(st,list.layout(e,st),item.key);const v=list.layout(e,st,W);
 const own=lines(v).slice(v.at,v.at+v.span);
 out[name]={has_text:!!item,len:[...whole].length,rows:v.span,fits:own.every(l=>measure(l)<=60),ends:own[own.length-1].trim().slice(-1),
   lone:/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(own.join('')),
   down:list.neighbour(v,1)!==item.key};}
// a heading of 1100 characters: chosen, opened, its items reachable
{const e=list.entriesOf(cases.long_heading),seen=walk(e),st=fresh();
 const head=list.layout(e,st).marks.filter(m=>m.key.startsWith('g:')).pop();
 out.lh_reach=seen.includes(head.key);out.lh_keylen=Math.max(...e.filter(x=>x.key).map(x=>x.key.length));
 list.press(st,list.layout(e,st),head.key);const l=lines(list.layout(e,st));out.lh_open=l.some(x=>x.includes('HIDDEN_ITEM'));
 out.lh_item_reach=walk(e).length;
 const v=list.layout(e,st);out.lh_item_choose=v.marks.some(m=>m.key.startsWith('i:')&&m.group===head.group);}
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

    def answer(self, body, name='campaign.md', width=96, age=600, part='list'):
        folder = self.root / ('c%d' % time.monotonic_ns()) / 'campaigns' / '2026-11-10_alpha'
        folder.mkdir(parents=True)
        page = folder / name
        page.write_text(body, encoding='utf-8')
        stamp = time.time() - age
        os.utime(page, (stamp, stamp))
        view = {'word': 'STATUS', 'has_doc': True, 'campaign': 'alpha', 'age': '', 'title': 'STATUS alpha', 'path': str(page),
                'slot': '', 'watch': []}
        with mock.patch.object(self.status, 'held', lambda cwd, claim, now=None: view):
            return self.m._status_popup({'harness': 'claude', 'sid': SID}, {'cwd': str(self.root), 'routes': [], 'status': {}, 'part': part},
                                        width, 256, True)

    def test_the_backend_carries_the_list_for_the_generated_page_only_and_the_document_read_whole_is_its_own_answer(self):
        generated = self.answer(PAGE)
        self.assertTrue(generated['ok'])
        self.assertTrue(generated['list'])
        self.assertEqual(generated['rows'], [])                         # the first screen is the list alone: no rows share its answer
        whole = self.answer(PAGE, part='doc')
        self.assertTrue(whole['ok'])
        self.assertNotIn('list', whole)                                  # the document read whole: the answer it always was
        self.assertTrue(whole['rows'])
        for body, name in (('# 현황\n\n## 다음\n- 하나\n', '현황.md'), (PAGE.split('\n', 1)[1], 'campaign.md')):
            hand, plain = self.answer(body, name), self.answer(body, name, part='doc')
            self.assertTrue(hand['ok'])
            self.assertNotIn('list', hand, name)
            self.assertEqual({k: v for k, v in hand.items() if k != 'part'}, {k: v for k, v in plain.items() if k != 'part'}, name)   # one answer, as before

    def test_the_document_read_whole_is_what_it_was_whatever_the_list_weighs(self):
        """Review 1: the list and the document no longer share one answer's size, so the document's rows are the ones it always had."""
        for label, item in (('740', 'x' * 740), ('200', 'x' * 200)):
            body = MARKER + '\n# T\n\n## 기다리는 것\n' + ''.join('- %03d %s\n' % (n, item) for n in range(60)) + '\n## END\nTAIL_SENTINEL\n'
            whole = self.answer(body, part='doc')
            self.assertNotIn('list', whole, label)
            texts = [''.join(run[0] for run in row) for row in whole['rows']]
            self.assertEqual(texts[-1], 'TAIL_SENTINEL', label)             # to the last line, nothing cut
            self.assertNotIn('…', texts[-1], label)
            self.assertEqual(len(texts) >= 60 + 4, True, label)
            listed = self.answer(body)
            self.assertTrue(listed.get('list'), label)
            self.assertEqual(listed['rows'], [], label)
        # A page whose document answer was cut for its size was cut on the rows alone: the same page's list asks nothing of that.
        big = MARKER + '\n# T\n\n## 기다리는 것\n- 하나\n\n## 나중에\n' + ''.join('- 글 %05d %s\n' % (n, 'y' * 70) for n in range(2000))
        whole = self.answer(big, part='doc')
        self.assertLessEqual(len(json.dumps(whole, ensure_ascii=False).encode()), self.m.POPUP_ANSWER_BYTES)
        self.assertNotIn('list', whole)

    def test_no_count_stops_the_list_and_a_list_that_does_not_fit_is_left_out_with_the_document_as_it_was(self):
        # Every item of every group, however many (61 and 300 are on the screen, with the keys to choose them).
        for count in (61, 300):
            many = PAGE.replace('## 기다리는 것\n', '## 기다리는 것\n' + ''.join('- 질문 %04d?\n' % n for n in range(count)))
            running = count if count <= 61 else 61           # (the answer's own size is the limit: two lists of 300 are more than it may weigh)
            many = many.replace('## 하는 중\n', '## 하는 중\n' + ''.join('- 하는 %04d\n' % n for n in range(running)))
            answer = self.answer(many, width=96)
            self.assertTrue(answer.get('list'), count)
            self.assertLessEqual(len(json.dumps(answer, ensure_ascii=False).encode()), self.m.POPUP_ANSWER_BYTES)
            keys = [entry['key'] for entry in answer['list'] if entry.get('key')]
            self.assertEqual(len(keys), len(set(keys)), count)
            seen = ' '.join(''.join(run[0] for row in entry['rows'] for run in row) for entry in answer['list'])
            self.assertIn('질문 %04d?' % (count - 1), seen)
            self.assertIn('하는 %04d' % (running - 1), seen)
            self.assertEqual(sum(1 for key in keys if key.startswith('i:')), count + 2 + running + 5 + 2 + 1, count)
        # A list that alone is more than an answer may weigh is left out: the popup then draws the document as it did.
        huge = PAGE.replace('- LATER_ONE 나중 일', ''.join('- 일 %04d ' % n + '가나다라마바사 ' * 8 + '\n' for n in range(900)))
        for group in ('## 하는 중', '## 기다리는 것', '## 최근에 끝난 것'):
            huge = huge.replace(group, group + '\n' + ''.join('- 또 %s %04d ' % (group[3:5], n) + '가나다라마바사 ' * 8 + '\n' for n in range(300)))
        big = self.answer(huge, width=96)
        self.assertTrue(big['ok'])
        self.assertNotIn('list', big)
        self.assertLessEqual(len(json.dumps(big, ensure_ascii=False).encode()), self.m.POPUP_ANSWER_BYTES)
        plain = self.answer(huge, width=96, part='doc')
        self.assertEqual({k: v for k, v in big.items() if k != 'part'}, {k: v for k, v in plain.items() if k != 'part'})   # the answer the popup always had

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
        self.assertEqual(first[1], 'updated 11-21 11:02')               # the age is the title band's, said once
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
        self.assertEqual(out['first_sel'].split(':')[0], 'i')
        # Everything that can be chosen, in order, and the ends stay.
        self.assertEqual(out['marks'], ['i', 'i', 'i', 'i', 'i', 'a', 'g', 'g', 'full'])
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
        # A cut item opens in place of its row: the whole words, folded in the width, the `> !` on the first row and the words' own column
        # on the rest, the row not shown a second time; opened again it is one row again. An item that was not cut has nothing to open.
        self.assertTrue(out['item_cut_has_text'])
        self.assertEqual(out['item_press'], 'item')
        self.assertGreaterEqual(out['item_more'], 2)
        self.assertEqual(out['item_span'], out['item_more'] + 1)
        self.assertTrue(out['item_first'].startswith('> ! 결정: Q_ONE'), out['item_first'])
        self.assertTrue(out['item_rest_indent'] and out['item_fits'] and out['item_whole'] and out['item_tinted'], out)
        self.assertFalse(out['item_ends_cut'], out)                      # nothing is cut any more
        self.assertEqual(out['item_text_once'], 1, out)                  # the same words are not on the screen twice
        self.assertEqual(out['item_hits'], list(range(out['item_at'], out['item_at'] + out['item_span'])))   # a press on any of its rows is on it
        self.assertTrue(out['item_neighbour_down'])
        self.assertEqual(out['short_press'], 'choose')                  # the choice moves to the pressed row and that is all
        self.assertTrue(out['short_same'])
        self.assertEqual((out['short_again'], out['short_items']), ('', 0))
        self.assertEqual(out['wrap'], [['가나', '다라'], [''], ['abc', 'def', 'gh']])
        self.assertTrue(out['item_sel'] and out['item_closed'])
        self.assertEqual(out['full'], 'full')
        # A document read again: the same words keep the choice, a line gone falls to the top.
        self.assertEqual(out['reread_same'], [True, False])
        self.assertEqual(out['reread_gone'], [True, True])
        self.assertTrue(out['reread_open_kept'])
        self.assertEqual(out['none'], ['', 0, 1])
        self.assertEqual(out['hits'][0]['action'][0], 'status')

    def test_the_front_end_keeps_what_the_answer_carries_and_tells_two_equal_headings_apart(self):
        """Review 3 and 4: the same heading twice, a 1,100-character heading, items of 4,000 and more characters, any script."""
        cases = {
            'dup': self.answer(PAGE + '\n## Other\n- A\n\n## Other\n- B\n'),
            'dup_solo': self.answer(PAGE + '\n## Other\n- B\n'),
            'long_heading': self.answer(PAGE + '\n## ' + 'h' * 1100 + '\n- HIDDEN_ITEM\n'),
            'scripts': self.answer(PAGE.replace('- Q_TWO 둘째 질문?', '- ' + '😀가é' * 700 + '끝')),
        }
        for size in (4000, 4001, 12000):
            cases['long%d' % size] = self.answer(PAGE.replace('- Q_TWO 둘째 질문?', '- ' + '가' * (size - 1) + '끝'))
        self.assertTrue(all(answer.get('list') for answer in cases.values()))
        path = self.root / 'front.json'
        path.write_text(json.dumps(cases, ensure_ascii=False))
        unit = self.root / 'front.cjs'
        unit.write_text(FRONT)
        done = subprocess.run([self.node, str(unit), str(ROOT / 'utilities/session-shell'), str(path)], capture_output=True, text=True,
                              timeout=60, env={'PATH': os.environ.get('PATH', '/usr/bin')})
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        # Two headings of the same words: two lines that can be chosen, ↓ and ↑ go through both, a press opens the one pressed.
        self.assertEqual(len(out['dup_heads']), 2)
        other = out['dup_heads']
        self.assertEqual(len(set(other)), 2)
        self.assertEqual(len(set(out['dup_walk'])), len(out['dup_walk']))           # the walk never stands still before the end
        self.assertEqual(out['dup_walk'][-1], 'full')
        self.assertTrue(set(other) <= set(out['dup_walk']))
        self.assertTrue(out['dup_down'] and out['dup_up'])
        self.assertEqual(out['dup_first_open'], [True, False])
        self.assertEqual(out['dup_both_open'], [True, True])
        self.assertEqual(out['dup_second_only'], [False, True])
        self.assertEqual(out['dup_hit'], 1)
        self.assertNotEqual(out['dup_ids'][0], out['dup_ids'][1])
        # Taking the first one out leaves the other with the id of the first (the order is what tells equal words apart).
        self.assertEqual(out['dup_solo_id'], out['dup_ids'][0])
        # An item of 4,000, 4,001 and 12,000 characters, and one of every script: what opens ends with its last character.
        for name, last in (('long4000', '끝'), ('long4001', '끝'), ('long12000', '끝'), ('scripts', '끝')):
            self.assertTrue(out[name]['has_text'], name)
            self.assertEqual(out[name]['ends'], last, name)
            self.assertTrue(out[name]['fits'] and not out[name]['lone'] and out[name]['down'], name)
        self.assertEqual((out['long4000']['len'], out['long4001']['len']), (4000, 4001))
        self.assertGreater(out['long4001']['rows'], 50)
        # A heading of 1,100 characters is chosen with the keys, opens, and its items can be chosen.
        self.assertTrue(out['lh_reach'] and out['lh_open'] and out['lh_item_choose'])
        self.assertLessEqual(out['lh_keylen'], 40)


# -- Part D: the host's own reading functions, run unchanged on a stand-in backend --------------------------------------------
#
# The functions that read the status popup (`askTable`, `askDoc`, `askFleet`, `statusFull`, `statusBack`, `tableRows`) are cut out of the
# real `host.cjs` and run in a Node context whose backend answers are held by the test and let go in the order it chooses: a late answer
# (the popup went back to the list, was closed, changed section or width, or the page was read again) is exactly what is being asked.

HOST_FUNCTIONS = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const root=process.argv[2];
const src=fs.readFileSync(path.join(root,'host.cjs'),'utf8');
const cut=(from,to)=>{const a=src.indexOf(from),b=src.indexOf(to,a);if(a<0||b<0)throw new Error('marker '+from);return src.slice(a,b);};
const reading=cut('let fleetAsking=false,','// The NOW popup\'s answer:');
const keys=cut('// 11-21: Enter or a press on a line of the status popup','// Enter in the NOW popup:');
const draw=cut('function tableRows(inner){','// The NOW popup\'s rows:');
const statusList=require(path.join(root,'status-list.cjs'));
const out={};
const make=()=>{
  const calls=[],held=[];
  const ctx={process,Promise,setImmediate,Date,Math,Array,Number,JSON,String,Set,Buffer,console,statusList,calls,held,
    table:null,tableAsking:false,tableAgain:false,selection:1,statusGeneration:2,rows:30,expanded:true,statusPopup:null,popupKeys:{},
    POPUP_KIND:{STATUS:'status'},panelView:{popupRoom:()=>20},statusRoutes:()=>[],statusHeld:()=>({}),height:()=>0,queueDisplay:()=>{},
    actionLines:[],panelModel:{labels:{}},label:(n,f)=>f,cellWidth:t=>[...t].length,
    tableParts:r=>statusList.rowsOf(r),statusEntries:r=>statusList.entriesOf(r),
    readAction:asked=>new Promise(resolve=>{calls.push(asked.part);held.push({part:asked.part,resolve});})};
  vm.createContext(ctx);vm.runInContext(reading,ctx);vm.runInContext(keys,ctx);vm.runInContext(draw,ctx);
  return ctx;
};
const settle=()=>new Promise(r=>setTimeout(r,15));
const text=rows=>rows.map(row=>row.map(p=>p.text).join(''));
const LIST=(extra={})=>({ok:true,kind:'status',title:'STATUS x',styles:[{}],rows:[],part:'list',has_doc:true,
  list:[{rows:[[['  ',0,2],['full document ›',0,15]]],key:'full'}],...extra});
const HAND=tag=>({ok:true,kind:'status',title:'STATUS x',styles:[{}],rows:[[['HAND '+tag,0,6]]],part:'list',has_doc:true});
const DOC=tag=>({ok:true,kind:'status',title:'STATUS x',styles:[{}],rows:[[['DOC '+tag,0,5]]],part:'doc',has_doc:true});
const FLEET=tag=>({ok:true,kind:'status',styles:[{}],rows:[[['FLEET '+tag,0,7]]],part:'fleet'});
// The next held request of a part is answered (in the order asked unless an index is given).
const answer=(ctx,part,value,at=0)=>{const i=ctx.held.findIndex((h,k)=>h.part===part&&!h.done&&at--<=0);if(i<0)throw new Error('no held '+part);ctx.held[i].done=true;ctx.held[i].resolve(value);};
const pending=(ctx,part)=>ctx.held.filter(h=>h.part===part&&!h.done).length;
const fresh=async()=>{
  const ctx=make();
  ctx.table={kind:'STATUS',width:104,loaded:false,screen:'',listing:null,doc:null,head:undefined,sel:'',fold:new Set(),items:new Set(),scroll:0,
    open:'',listScroll:0,reveal:true};
  ctx.askTable();answer(ctx,'list',LIST());await settle();return ctx;
};
const snap=ctx=>({calls:[...ctx.calls],screen:ctx.table?ctx.table.screen:null,doc:ctx.table&&ctx.table.doc?text(ctx.table.doc):null,
  head:ctx.table&&ctx.table.head?text(ctx.table.head):null,listing:!!(ctx.table&&ctx.table.listing)});
const whole=async ctx=>{ctx.statusFull(ctx.table);answer(ctx,'doc',DOC('a'));await settle();answer(ctx,'fleet',FLEET('a'));await settle();};
(async()=>{
  // 1. The first screen asks the list alone: no document answer, no Fleet answer; and the list is kept at once for the next opening.
  let c=await fresh();out.first=snap(c);out.first_kept=!!(c.statusPopup&&c.statusPopup.listing&&c.statusPopup.doc===null);
  out.first_rows=text(c.tableRows(100)).length>0;
  // 2. `full document ›`: the document's own answer, then Fleet's; entering again, and back, ask nothing.
  c.statusFull(c.table);out.full_asked=snap(c);out.full_wait=text(c.tableRows(100));
  answer(c,'doc',DOC('a'));await settle();out.full_doc=snap(c);answer(c,'fleet',FLEET('a'));await settle();out.full_fleet=snap(c);
  out.full_rows=text(c.tableRows(100));
  c.statusBack(c.table);c.statusFull(c.table);await settle();out.full_again=snap(c);
  out.full_again_rows=text(c.tableRows(100));
  // 3. A document answer that comes late.
  c=await fresh();c.statusFull(c.table);c.statusBack(c.table);answer(c,'doc',DOC('late'));await settle();         // back to the list meanwhile
  out.late_back={...snap(c),kept:text(c.table.doc),fresh:c.docFresh(c.table)};
  c.statusFull(c.table);await settle();out.late_back_again=snap(c);                                               // entering again uses it
  c=await fresh();c.statusFull(c.table);const gone=c.table;c.table=null;answer(c,'doc',DOC('closed'));await settle();   // the popup closed
  out.late_closed={calls:[...c.calls],doc:gone.doc,cache:c.statusPopup&&c.statusPopup.doc};
  c=await fresh();c.statusFull(c.table);c.table.kind='NOW';answer(c,'doc',DOC('moved'));await settle();           // another section stands
  out.late_section={calls:[...c.calls],doc:c.table.doc};
  c=await fresh();c.statusFull(c.table);c.table.width=90;answer(c,'doc',DOC('width'));await settle();             // another width
  out.late_width={calls:[...c.calls],doc:c.table.doc};
  c=await fresh();c.statusFull(c.table);c.selection=2;answer(c,'doc',DOC('session'));await settle();               // another session
  out.late_session={calls:[...c.calls],doc:c.table.doc};
  // 4. The page read again while the document read whole is on screen: it and Fleet's lines are asked again; the old ones stay until then.
  c=await fresh();await whole(c);c.statusGeneration=3;c.askTable();await settle();out.reread_asked=snap(c);
  out.reread_old=text(c.tableRows(100));
  answer(c,'list',LIST());await settle();out.reread_list=snap(c);out.reread_during=text(c.tableRows(100));
  answer(c,'doc',DOC('b'));await settle();out.reread_doc=snap(c);answer(c,'fleet',FLEET('b'));await settle();out.reread_done=snap(c);
  out.reread_rows=text(c.tableRows(100));out.reread_head_fresh=c.headFresh(c.table);
  //    a Fleet request already going: the old answer is dropped, the new one asked after it
  c=await fresh();c.statusFull(c.table);answer(c,'doc',DOC('a'));await settle();                                    // Fleet's asked, held
  c.statusGeneration=3;c.askTable();await settle();answer(c,'list',LIST());await settle();                         // the page read again
  answer(c,'doc',DOC('b'),0);await settle();                                                                      // (the held doc is the new one)
  out.busy_calls=[...c.calls];answer(c,'fleet',FLEET('old'));await settle();out.busy_after_old=snap(c);
  answer(c,'fleet',FLEET('new'));await settle();out.busy_done=snap(c);
  //    the old generation's answers are dropped, in either order
  c=await fresh();c.statusFull(c.table);c.statusGeneration=3;c.askTable();await settle();
  answer(c,'doc',DOC('old-gen'));await settle();out.old_gen_dropped=c.table.doc;answer(c,'list',LIST());await settle();
  out.old_gen_then=[...c.calls];answer(c,'doc',DOC('new-gen'));await settle();out.old_gen_final=text(c.table.doc);
  c=await fresh();c.statusFull(c.table);c.statusGeneration=3;c.askTable();await settle();answer(c,'list',LIST());await settle();
  answer(c,'doc',DOC('old-gen'),0);await settle();out.order2=[[...c.calls],c.table.doc&&text(c.table.doc)];
  // 5. The page read again while the list is on screen asks nothing of the document or Fleet; what was kept of them is let go.
  c=await fresh();await whole(c);c.statusBack(c.table);c.calls.length=0;c.statusGeneration=3;c.askTable();await settle();answer(c,'list',LIST());await settle();
  out.list_reread={calls:[...c.calls],doc:c.table.doc,screen:c.table.screen,headFresh:c.headFresh(c.table)};
  // 6. While it is read again: back to the list, the popup closed, another section: nothing more is asked.
  c=await fresh();await whole(c);c.calls.length=0;c.statusGeneration=3;c.askTable();c.statusBack(c.table);answer(c,'list',LIST());await settle();
  out.reread_back=[...c.calls];
  c=await fresh();await whole(c);c.calls.length=0;c.statusGeneration=3;c.askTable();c.table=null;answer(c,'list',LIST());await settle();
  out.reread_close=[...c.calls];
  c=await fresh();await whole(c);c.calls.length=0;c.statusGeneration=3;c.askTable();c.table={kind:'MEM',width:104};answer(c,'list',LIST());await settle();
  out.reread_section=[...c.calls];
  // 7. A page that is not the generated one (or whose list did not fit): the rows come with the answer, Fleet's lines after, as before.
  c=make();c.table={kind:'STATUS',width:104,loaded:false,screen:'',listing:null,doc:null,head:undefined,sel:'',fold:new Set(),items:new Set(),scroll:0,open:''};
  c.askTable();answer(c,'list',HAND('x'));await settle();out.hand=snap(c);out.hand_rows=text(c.tableRows(100));answer(c,'fleet',FLEET('h'));await settle();
  out.hand_done=snap(c);out.hand_rows2=text(c.tableRows(100));
  // 8. A document that cannot be read whole when `full document ›` asks: said, and asked again at the next entry.
  c=await fresh();c.statusFull(c.table);answer(c,'doc',{ok:false,reason:'doc-unreadable'});await settle();out.unread=text(c.tableRows(100));
  c.statusBack(c.table);c.statusFull(c.table);await settle();out.unread_again=[...c.calls];
  console.log(JSON.stringify(out));
})().catch(e=>{console.error(e.stack);process.exit(1);});
"""


class HostFunctionTest(unittest.TestCase):
    """Part D: the host's own reading functions for the status popup, with the backend held by the test."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('needs node')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsl3-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        script = Path(cls.temporary.name) / 'host-functions.cjs'
        script.write_text(HOST_FUNCTIONS)
        done = subprocess.run([cls.node, str(script), str(ROOT / 'utilities/session-shell')], capture_output=True, text=True, timeout=120,
                              env={'PATH': os.environ.get('PATH', '/usr/bin')})
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.out = json.loads(done.stdout)

    def test_the_first_screen_asks_the_list_alone(self):
        o = self.out
        self.assertEqual(o['first']['calls'], ['list'])                      # no document answer, no Fleet answer
        self.assertEqual((o['first']['screen'], o['first']['listing'], o['first']['doc'], o['first']['head']), ('list', True, None, None))
        self.assertTrue(o['first_kept'] and o['first_rows'])                 # kept for the next opening at once

    def test_full_document_asks_its_own_answer_then_fleet_and_is_kept(self):
        o = self.out
        self.assertEqual(o['full_asked']['calls'], ['list', 'doc'])
        self.assertEqual(o['full_wait'], ['⠏'])                              # until it comes
        self.assertEqual(o['full_doc']['doc'], ['DOC a'])
        self.assertEqual(o['full_doc']['calls'], ['list', 'doc', 'fleet'])   # Fleet follows the document, as it always did
        self.assertEqual(o['full_fleet']['head'], ['FLEET a'])
        self.assertEqual(o['full_rows'], ['FLEET a', 'DOC a'])
        self.assertEqual(o['full_again']['calls'], ['list', 'doc', 'fleet'])  # back and in again: nothing is asked
        self.assertEqual(o['full_again_rows'], ['FLEET a', 'DOC a'])

    def test_a_document_answer_that_comes_late(self):
        o = self.out
        # Back at the list meanwhile: kept (nothing is asked for Fleet while the list stands), and used when it is entered again.
        self.assertEqual((o['late_back']['screen'], o['late_back']['kept'], o['late_back']['fresh']), ('list', ['DOC late'], True))
        self.assertEqual(o['late_back']['calls'], ['list', 'doc'])
        self.assertEqual(o['late_back_again']['calls'], ['list', 'doc', 'fleet'])   # (Fleet's lines are what entering asks for)
        # The popup closed, another section standing, another width, another session: the answer is not this popup's and is kept by none.
        self.assertEqual((o['late_closed']['calls'], o['late_closed']['doc'], o['late_closed']['cache']), (['list', 'doc'], None, None))
        self.assertEqual((o['late_section']['calls'], o['late_section']['doc']), (['list', 'doc'], None))
        self.assertEqual((o['late_width']['calls'], o['late_width']['doc']), (['list', 'doc'], None))
        self.assertEqual((o['late_session']['calls'], o['late_session']['doc']), (['list', 'doc'], None))

    def test_a_page_read_again_on_the_document_read_whole_asks_its_document_and_fleet_again(self):
        o = self.out
        self.assertEqual(o['reread_asked']['calls'], ['list', 'doc', 'fleet', 'list'])
        self.assertEqual(o['reread_old'], ['FLEET a', 'DOC a'])                    # what is there stays until the new comes
        self.assertEqual(o['reread_list']['calls'], ['list', 'doc', 'fleet', 'list', 'doc'])
        self.assertEqual(o['reread_during'], ['FLEET a', 'DOC a'])
        self.assertEqual(o['reread_doc']['calls'][-1], 'fleet')                  # review 5: the old Fleet line + the new document is no screen
        self.assertEqual(o['reread_done']['doc'], ['DOC b'])
        self.assertEqual(o['reread_rows'], ['FLEET b', 'DOC b'])
        self.assertTrue(o['reread_head_fresh'])
        # A Fleet request already going when the page is read again: its answer is let go and Fleet is asked once more after it.
        self.assertEqual(o['busy_calls'], ['list', 'doc', 'fleet', 'list', 'doc'])
        self.assertEqual(o['busy_after_old']['head'], None)                       # the old generation's lines are not shown
        self.assertEqual(o['busy_after_old']['calls'][-1], 'fleet')
        self.assertEqual(o['busy_done']['head'], ['FLEET new'])
        # An old generation's document answer is dropped, whichever comes first.
        self.assertEqual(o['old_gen_dropped'], None)
        self.assertEqual(o['old_gen_then'], ['list', 'doc', 'list', 'doc'])
        self.assertEqual(o['old_gen_final'], ['DOC new-gen'])
        self.assertEqual(o['order2'][1], None)
        self.assertEqual(o['order2'][0].count('doc'), 2)

    def test_a_page_read_again_on_the_list_asks_nothing_of_the_document_or_fleet(self):
        o = self.out
        self.assertEqual(o['list_reread']['calls'], ['list'])
        self.assertEqual((o['list_reread']['doc'], o['list_reread']['screen'], o['list_reread']['headFresh']), (None, 'list', False))

    def test_going_back_closing_or_changing_section_while_it_is_read_again_asks_nothing_more(self):
        o = self.out
        self.assertEqual(o['reread_back'], ['list'])
        self.assertEqual(o['reread_close'], ['list'])
        self.assertEqual(o['reread_section'], ['list'])

    def test_a_page_without_a_list_is_read_as_it_always_was(self):
        o = self.out
        self.assertEqual(o['hand']['calls'], ['list', 'fleet'])                   # the rows come with the answer, Fleet's lines after
        self.assertEqual((o['hand']['screen'], o['hand']['listing']), ('', False))
        self.assertEqual(o['hand_rows'], ['HAND x'])
        self.assertEqual(o['hand_rows2'], ['FLEET h', 'HAND x'])

    def test_a_document_that_cannot_be_read_whole_is_said_and_asked_again_at_the_next_entry(self):
        o = self.out
        self.assertEqual(o['unread'], ['could not read'])
        self.assertEqual(o['unread_again'], ['list', 'doc', 'doc'])


# -- Part E: the other popups go through the functions this change touched, and come out as they did ------------------------------

OTHER_POPUPS = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const root=process.argv[2];
const src=fs.readFileSync(path.join(root,'host.cjs'),'utf8');
const cut=(from,to)=>{const a=src.indexOf(from),b=src.indexOf(to,a);if(a<0||b<0)throw new Error('marker '+from);return src.slice(a,b);};
const one=name=>{const m=src.match(new RegExp('function '+name+'\\(result\\)\\{[^\\n]*\\n'));if(!m)throw new Error(name);return m[0];};
const statusList=require(path.join(root,'status-list.cjs'));
const log=[];
const ctx={process,Promise,Date,Math,Array,Number,JSON,String,Set,statusList,log,table:null,expanded:false,panelModel:{labels:{}},
  label:(n,f)=>f,cellWidth:t=>[...t].length,actionLines:[],queueDisplay:()=>log.push('display'),askTable:()=>log.push('askTable'),
  moveSel:(d)=>log.push('moveSel '+d),enterNow:()=>log.push('enterNow'),statusListing:()=>false,panelView:{}};
vm.createContext(ctx);
vm.runInContext(cut('function tableGuide(kind){','// Key words packed')+cut('function moveRow(delta){','// Enter in the NOW popup:')
  +cut('function nowRows(t){','function popupNow(){')+one('tableParts')+cut('function tableRows(inner){','// The NOW popup\'s rows:'),ctx);
const out={};
const run=(t,f)=>{ctx.table=t;log.length=0;const r=f();return {log:[...log],r};};
// The key words of each popup.
for(const expanded of [false,true]){ctx.expanded=expanded;
  out['guide_'+expanded]={route:run({kind:'ROUTE',page:false},()=>ctx.tableGuide('ROUTE')).r,route_page:run({kind:'ROUTE',page:true},()=>ctx.tableGuide('ROUTE')).r,
    now:run({kind:'NOW',page:false},()=>ctx.tableGuide('NOW')).r,now_page:run({kind:'NOW',page:true},()=>ctx.tableGuide('NOW')).r,
    messages:run({kind:'MESSAGES'},()=>ctx.tableGuide('MESSAGES')).r,mem:run({kind:'MEM'},()=>ctx.tableGuide('MEM')).r,
    hand:run({kind:'STATUS',listing:null,screen:''},()=>ctx.tableGuide('STATUS')).r,
    list:run({kind:'STATUS',listing:[{}],screen:'list'},()=>ctx.tableGuide('STATUS')).r,full:run({kind:'STATUS',listing:[{}],screen:'full'},()=>ctx.tableGuide('STATUS')).r};}
// ↑↓ and Tab, Enter and Esc.
const routes=['a','b','c'];
out.move_now=run({kind:'NOW',page:false,scroll:0},()=>{ctx.moveRow(1);ctx.moveRow(-1);}).log;
out.move_now_page=run({kind:'NOW',page:true,scroll:3},()=>{ctx.moveRow(1);return ctx.table.scroll;});
out.move_route=run({kind:'ROUTE',page:false,routes,open:'a',scroll:0,follow:false},()=>{ctx.moveRow(1);const first=ctx.table.open;ctx.moveRow(1);ctx.moveRow(1);ctx.moveRow(-1);return [first,ctx.table.open,ctx.table.follow];});
out.move_route_none=run({kind:'ROUTE',page:false,routes:[],open:'',scroll:2},()=>{ctx.moveRow(-1);return ctx.table.scroll;});
out.move_messages=run({kind:'MESSAGES',scroll:2},()=>{ctx.moveRow(1);ctx.moveRow(1);ctx.moveRow(-1);return ctx.table.scroll;});
out.move_hand=run({kind:'STATUS',listing:null,screen:'',scroll:0},()=>{ctx.moveRow(1);ctx.moveRow(1);return ctx.table.scroll;});
out.enter_now=run({kind:'NOW'},()=>ctx.enterRow()).log;
out.enter_route=run({kind:'ROUTE',open:'a',page:false,scroll:5,follow:false},()=>{ctx.enterRow();const p=[ctx.table.page,ctx.table.scroll,ctx.table.pageTop];ctx.enterRow();return [p,ctx.table.page,ctx.table.follow];});
out.enter_route_none=run({kind:'ROUTE',open:'',page:false},()=>ctx.enterRow()).log;
out.enter_hand=run({kind:'STATUS',listing:null,screen:''},()=>ctx.enterRow()).log;
out.enter_mem=run({kind:'MEM'},()=>ctx.enterRow()).log;
out.back={};for(const [name,t] of Object.entries({route:{kind:'ROUTE'},route_page:{kind:'ROUTE',page:true},now:{kind:'NOW'},now_page:{kind:'NOW',page:true},messages:{kind:'MESSAGES'},mem:{kind:'MEM'},
  hand:{kind:'STATUS',listing:null,screen:''},list:{kind:'STATUS',listing:[{}],screen:'list'}}))out.back[name]=run(t,()=>ctx.popupBack()).r;
out.back.none=run(null,()=>ctx.popupBack()).r;
out.back.full=run({kind:'STATUS',listing:[{}],screen:'full',scroll:4,listScroll:2},()=>{const r=ctx.popupBack();return [r,ctx.table.screen,ctx.table.scroll];}).r;
// The rows each draws: a routes popup's are its table's, the NOW list's the same with the row stood on tinted, a page's as read.
const rowsOfTable=[[{text:'r1'}],[{text:'r2',dim:true}]];
out.rows_route=run({kind:'ROUTE',loaded:true,rows:rowsOfTable},()=>ctx.tableRows(80)).r;
out.rows_hand=run({kind:'STATUS',loaded:true,listing:null,rows:rowsOfTable,hits:[1]},()=>ctx.tableRows(80)).r;
out.rows_unloaded=run({kind:'ROUTE',loaded:false,rows:rowsOfTable,error:true},()=>ctx.tableRows(80)).r;
const list={rows:[[{text:'n1'}],[{text:'n2'}],[{text:'n3'}]],meta:[{key:'k1'},{key:'k2'},{key:'k3'}]};
out.rows_now=run({kind:'NOW',loaded:true,page:false,list,sel:'k2'},()=>ctx.tableRows(80)).r;
out.rows_now_page=run({kind:'NOW',loaded:true,page:true,read:{rows:[[{text:'p1'}]]},list,sel:'k2'},()=>ctx.tableRows(80)).r;
// The conversion of a backend's answer into painted parts, as it was (the odd cases a backend could send too).
const before=result=>{const styles=Array.isArray(result.styles)?result.styles:[];
  return (Array.isArray(result.rows)?result.rows:[]).map(row=>(Array.isArray(row)?row:[]).map(run=>({text:String(run?.[0]??''),...(styles[run?.[1]]||{}),
    ...(Number.isInteger(run?.[2])&&run[2]>=0&&run[2]<=400?{cells:run[2]}:{})})));};
const answers=[{rows:[[['a',0,1],['한글',1,4]],[],[['x',5],['y',0,401],['z',0,-1],['w',0,1.5],['n'],[null],null,'s']],styles:[{fg:1},{bold:true}]},
  {rows:'no',styles:null},{styles:[]},{rows:[null,'x',[['q',0,400]],[[ 'v',0,0]]],styles:[{dim:true}]},{rows:[[[1,0,2],[undefined,0]]],styles:[{}]}];
out.parts_same=answers.map(a=>JSON.stringify(ctx.tableParts(a))===JSON.stringify(before(a)));
console.log(JSON.stringify(out));
"""


class OtherPopupsTest(unittest.TestCase):
    """Part E: the ROUTE, NOW, MESSAGES and MEM popups through the host functions the status popup's change touched (keys, guide, rows)."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('needs node')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsl4-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        script = Path(cls.temporary.name) / 'other-popups.cjs'
        script.write_text(OTHER_POPUPS)
        done = subprocess.run([cls.node, str(script), str(ROOT / 'utilities/session-shell')], capture_output=True, text=True, timeout=120,
                              env={'PATH': os.environ.get('PATH', '/usr/bin')})
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.out = json.loads(done.stdout)

    def test_the_key_words_of_every_other_popup_are_the_ones_it_had(self):
        for expanded, fold in (('false', 'p open panel'), ('true', 'p fold panel')):
            g = self.out['guide_' + expanded]
            keys = '←→ section · PgUp/PgDn'.replace('{panel}', fold)
            self.assertEqual(g['route'], ['↑↓ route · Enter open', keys])
            self.assertEqual(g['route_page'], ['↑↓ route · Enter open', keys])        # (the page's own words come from the panel's labels)
            self.assertEqual(g['now'], ['↑↓ · Enter read that part', keys])
            self.assertEqual(g['now_page'], ['Enter back to the list', keys])
            self.assertEqual(g['messages'], [keys])
            self.assertEqual(g['mem'], [keys])
            self.assertEqual(g['hand'], [keys])                                         # a hand-written page: the keys it had
            self.assertEqual(g['list'], ['↑↓ · Enter open', keys])
            self.assertEqual(g['full'], [keys])                                         # the document read whole: the popups' keys, Esc said once

    def test_the_arrows_tab_and_enter_do_what_they_did_in_the_other_popups(self):
        o = self.out
        self.assertEqual(o['move_now'], ['moveSel 1', 'moveSel -1'])
        self.assertEqual(o['move_now_page']['r'], 4)
        self.assertEqual(o['move_route']['r'], ['b', 'b', True])                      # the next route unfolds (b, c, the end stays, back to b), the read follows
        self.assertEqual(o['move_route_none']['r'], 1)
        self.assertEqual(o['move_messages']['r'], 3)
        self.assertEqual(o['move_hand']['r'], 2)                                      # a hand-written page scrolls by a line
        self.assertEqual(o['enter_now'], ['enterNow'])
        self.assertEqual(o['enter_route']['r'], [[True, 0, True], False, True])         # the page, and back to the table following the row
        self.assertEqual(o['enter_route_none'], [])
        self.assertEqual(o['enter_hand'], [])
        self.assertEqual(o['enter_mem'], [])

    def test_esc_closes_every_popup_but_the_status_documents_whole_page_which_goes_back_to_its_list(self):
        back = self.out['back']
        for name in ('route', 'route_page', 'now', 'now_page', 'messages', 'mem', 'hand', 'list', 'none'):
            self.assertIs(back[name], False, name)
        self.assertEqual(back['full'], [True, 'list', 2])

    def test_the_rows_of_the_other_popups_are_drawn_as_they_were(self):
        o = self.out
        table = [[{'text': 'r1'}], [{'text': 'r2', 'dim': True}]]
        self.assertEqual(o['rows_route'], table)
        self.assertEqual(o['rows_hand'], table)
        self.assertEqual(o['rows_unloaded'], [[{'text': 'could not read', 'dim': True}]])
        self.assertEqual(o['rows_now'], [[{'text': 'n1'}], [{'text': 'n2', 'tint': True}], [{'text': 'n3'}]])
        self.assertEqual(o['rows_now_page'], [[{'text': 'p1'}]])
        self.assertTrue(all(o['parts_same']), o['parts_same'])                          # `tableParts`, moved into the list module, converts as before


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
  // Enter opens the whole text of the cut item in place of its row (folded, the first row marked, nothing twice); again makes it one row.
  const rowsBefore=rowsOf(r.s).length;
  await key('\r');r=await until(shows('Q_ONE_LAST'),'the item opened');
  checks.item_first=chosen(r.s)[0];checks.item_last_count=rowsOf(r.s).filter(l=>l.includes('Q_ONE_LAST')).length;
  checks.item_head_count=rowsOf(r.s).filter(l=>l.includes('결정: Q_ONE')).length;checks.item_cut_left=rowsOf(r.s).some(l=>l.endsWith('…'));
  checks.item_more=rowsOf(r.s).length-rowsBefore;
  checks.item_rest=rowsOf(r.s)[rowsOf(r.s).findIndex(l=>l.startsWith('> '))+1];
  await key('\r');await until(s=>!shows('Q_ONE_LAST')(s),'the item closed');checks.item_closed_rows=rowsOf(await screen()).length-rowsBefore;
  // Enter on an item that was not cut changes nothing.
  await key('\x1b[B');const shortSel=rowsOf(await screen());await key('\r',300);checks.short_same=JSON.stringify(rowsOf(await screen()))===JSON.stringify(shortSel);
  await key('\x1b[A');
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
  checks.full_guide_rows=frame(r.s).filter(l=>/section|PgUp|Ctrl\+\]/.test(l)).length;checks.full_esc_words=(frame(r.s).join(' ').match(/Esc/g)||[]).length;
  checks.full_whole=shows('LISTONE')(r.s);
  await key('\x1b',400);r=await screen();checks.esc_back=shows('full document ›')(r)&&!!box(r);checks.esc_back_chosen=chosen(r);
  // (again: what the popup has of the document and of Fleet is shown at once and asked for no more)
  const vReuse=count('vm');await key('\r',300);r=await until(shows('갱신 2026-11-21 11:02'),'the document again');
  checks.reuse_fleet_line=shows('FLEET_STATUS_LINE')(r.s);await sleep(600);checks.reuse_vm=count('vm')-vReuse;
  await key('\r',300);r=await until(shows('full document ›'),'Enter went back');checks.enter_back=!!box(r.s);
  await key('\x1b',400);await until(s=>!box(s),'Esc closed it');checks.esc_closed=true;
  // A document that changes under an open popup: the chosen line stays on its words; a group stays open by its name; a line gone falls to the top.
  await opened();await until(shows('full document ›'),'the list again');const vList=count('vm');
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
  checks.vm_while_listing=count('vm')-vList;                                          // a document read again on the list asks nothing of Fleet
  // On the document read whole, a page read again asks for Fleet's lines again (and the document), as the popup always did.
  for(let i=0;i<14;i++)await key('\x1b[B',60);
  await key('\r',300);r=await until(shows('갱신 2026-11-21 11:02'),'the document read whole, before the change');
  await until(shows('FLEET_STATUS_LINE'),'Fleet\'s line again',15000);const vFull=count('vm');
  edit('- 결정: Q_ONE_EDITED','- 결정: Q_ONE_AGAIN');
  r=await until(shows('Q_ONE_AGAIN'),'the changed document read whole',15000);
  for(let i=0;i<60&&count('vm')===vFull;i++)await sleep(100);
  checks.refleet_on_full=count('vm')-vFull;checks.refleet_line=shows('FLEET_STATUS_LINE')((await screen()));checks.full_still=!shows('full document ›')(r.s);
  await key('\x1b',400);r=await until(shows('full document ›'),'back at the list after the change');checks.after_change_list=shows('Q_ONE_AGAIN')(r.s);
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
        self.assertTrue(c['item_first'].startswith('> ! 결정: Q_ONE'), c)                  # the row is the words, folded: marked on its first row
        self.assertEqual((c['item_last_count'], c['item_head_count'], c['item_cut_left']), (1, 1, False), c)   # nothing twice, nothing cut
        self.assertGreaterEqual(c['item_more'], 1, c)
        self.assertTrue(c['item_rest'].startswith('    ') and not c['item_rest'].startswith('     '), c)  # the words' own column
        self.assertEqual(c['item_closed_rows'], 0, c)                                     # again: one row, as before
        self.assertTrue(c['short_same'], c)                                              # an item that was not cut has nothing to open
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
        self.assertEqual((c['full_guide_rows'], c['full_esc_words']), (1, 1), c)          # one row of keys, which say Esc once
        self.assertNotIn('back to the list', c['full_keys'])
        self.assertTrue(c['full_whole'], c)
        self.assertTrue(c['reuse_fleet_line'], c)                                         # kept: shown at once
        self.assertEqual(c['reuse_vm'], 0, c)                                             # and not asked for again
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
        self.assertEqual(c['vm_while_listing'], 0, c)
        self.assertEqual(c['refleet_on_full'], 1, c)                                      # the full document read again asks Fleet's lines again
        self.assertTrue(c['refleet_line'] and c['full_still'] and c['after_change_list'], c)

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
