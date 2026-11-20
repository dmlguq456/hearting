"""11-16 (panel polish 1116): the person's seven corrections on v4.39.0/v4.40.0, panel-view's and the backend's side.

1. MESSAGES draws v4.36.4's two rows per message again (spec rule 31: the head, then the first line two cells
   in), five at most (`+N` counts the rest; three at most since 11-20); short of rows a message keeps its one body row, then every
   message takes the one-row form, then fewer show.
2. A sent answer's line stays ten minutes, its session having it or not (no timer: it leaves at the next update
   or paint past that).
3. What waits for an answer blinks on Fleet's one-second beat, from the clock, between two reds an L* gap of 25
   or more apart; nothing blinks with `rule motion` off, mono, NO_COLOR or eight colors, or with nothing waiting.
4. The head rule's light: its head as bright as the body text, its tail four even steps down (both faces).
5. A tidy result that ended well reads as a dim line without `⚠` (its inner words out of the panel's line, the
   notice itself unchanged); what leaves the person something to do keeps the caution look; the release lines
   are quiet too.
6. Beside the native area a thin rule parts MEM (the bottom sections) from the last row; it goes before content.
7. The fold button is `▾` at the last row's right end, the gear left of it, their presses moved with them.

(3's beat in a real PTY: session_shell_wait_blink_pty.test.py.)
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const shell=process.argv[1];
const view=require(path.join(shell,'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},g_spin:{foreground:230},g_work:{foreground:150,bold:true},g_work_off:{foreground:150,dim:true},g_idle:{foreground:186},
  g_dead:{foreground:217,bold:true},hb_claude:{foreground:116},hb_codex:{foreground:176},hb_opencode:{foreground:147}};
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const MSG=[['←','[e7]','codex','09:12','report','스캐너 값 측정 끝났습니다',null],['→','[73]','claude','09:20','request','MEM 아래 구분선 그림 하나 부탁',['delivered','dim']],
  ['←','[73]','claude','09:31','reply','그림 두 장 보냈습니다. 하나는 50칸, 하나는 40칸, 둘 다 합성 값으로 그렸습니다',null],
  ['→','[e7]','codex','09:40','message','접기 단추 자리 세 안 비교 부탁합니다',['sent','dim']],['←','[12]','opencode','09:52','report','시험 통과, CI 기다리는 중',null],
  ['←','[e7]','codex','10:03','reply','세 안 중 권장안이 가장 낫습니다',null],['→','[12]','opencode','10:05','message','고맙습니다',['not delivered','lvl_y']]]
  .map(([way,who,harness,when,kind,text,delivery])=>({way,who,harness,when,kind,text,body:text,delivery}));
function model(o={}){
  const route=o.route!==false;
  return {styles,colors:o.colors??256,no_color:o.no_color===true,tag:'[e9]',repo_side:[t('hearting','loc_repo')],worktrees:[t('wt 12')],
    git_side:[t('panel-polish-1116'),t(' ✓','lvl_g')],selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],
    quota:[t('5h ','dim'),t('23%','lvl_g'),t(' │ '),t('7d ','dim'),t('61%','lvl_y')],session_title:'패널 손질',now:'두 줄 꼴',
    ...(o.question!==false?{wait:'◑답 대기 1'}:{}),route_label:route?'route 1 ⠏1':'',route_count:route?1:0,route_total:route?1:0,
    routes:route?[{id:'rt-1',title:'패널 손질 일곱',kind:'working',state:'working',shape:'one-shot-owner',kind_word:'code',elapsed:'42m',tool:'Edit',target:'panel-view.cjs',role:'owner',
      paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','hb_claude'),effort:t('xhigh','dim'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' '),t('52%','lvl_g')]}}]:[],
    messages:o.messages===0?[]:MSG.slice(-(o.messages??7)),
    memory_items:o.mem===false?[]:[{kind:'decision',age:'10m',text:'접기 단추는 펼침 단추와 같은 자리',tier:'durable',scope:'project'},{kind:'lesson',age:'2h',text:'점멸은 1초 박자로',tier:'working',scope:'global'}],
    tidy_notice:o.tidy||'',tidy_quiet:o.quiet===true};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',
  rows:[[t('◑ ','g_blocked'),t('MESSAGES 몇 건까지 보일까요?','loc_repo')],[t('1 ','dim'),t('5건 (추천)','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('3건','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('MESSAGES 몇 건까지 보일까요?','loc_repo')]],line:'MESSAGES 몇 건까지 보일까요?'});
const draw=(o={})=>view.render({width:o.width??50,height:o.height??46,side:o.side??true,model:model(o),state:o.state||'working',harness:'claude',
  frame:o.frame??1000,since:o.since??990,brief:o.question===false?undefined:brief,sent:()=>[],cellWidth:cells,
  configuration:{side_width:50,rule_motion:o.motion||'rule',theme:o.theme||'terminal',position:o.position||'right'},folded:!!o.folded});
const lines=v=>v.rows.map(str);
// The rows between the MESSAGES head and the MEM head (inside the frame).
function section(v){const ls=lines(v).map(l=>l.slice(2,-2)),a=ls.findIndex(l=>l.startsWith('MESSAGES')),b=ls.findIndex(l=>l.startsWith('MEM'));
  return a<0?null:{head:ls[a],rows:ls.slice(a+1,b<0?ls.length-2:b),at:a};}
"""


def lightness(index):
    """CIE L* of a 256-color index (the xterm cube and gray ramp)."""
    if index >= 232:
        rgb = [8 + 10 * (index - 232)] * 3
    else:
        n = index - 16
        rgb = [0 if v == 0 else 55 + 40 * v for v in (n // 36, n // 6 % 6, n % 6)]
    lin = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y


class PanelPolish1116Test(unittest.TestCase):
    def node(self, body, *args, script=HEAD):
        result = subprocess.run(["node", "-e", script + body, str(SHELL), *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    # 1 ---------------------------------------------------------------------------------------------------------
    def test_messages_take_two_rows_each_three_at_most(self):
        out = self.node(r"""
const v=draw(),m=section(v);
assert(m,lines(v).join('\n'));
assert.match(m.head,/^MESSAGES \+4 ─+ ›$/,m.head);
// Three messages (11-20: three at most, the person: "MESSAGE도 최대 3개 정도로 다시 줄이고"), each its head and its first line two cells in, the newest last.
assert.equal(m.rows.length,6,m.rows.join('\n'));
const heads=m.rows.filter((_,i)=>i%2===0),bodies=m.rows.filter((_,i)=>i%2===1);
assert.deepEqual(heads.map(h=>h.trim()),['← [12] opencode · 09:52 · report','← [e7] codex · 10:03 · reply','→ [12] opencode · 10:05 · not delivered'],heads.join('\n'));
assert(bodies.every(b=>/^  \S/.test(b)),bodies.join('\n'));
assert.match(bodies[0].trimEnd(),/^  시험 통과, CI 기다리는 중$/,bodies[0]);
// The looks: the arrow in its direction's color, the number in the body color, the rest of the head dim,
// a sent one's state in its own color; the body in the body color. A press on any row opens the popup.
const row=v.rows.find(r=>str(r).includes('← [12] opencode'));
const look=s=>row.find(p=>p.text.includes(s));
assert.equal(look('←').foreground,150);assert.equal(look('[12]').foreground,253);assert.equal(look(' opencode').dim,true);assert.equal(look(' · 09:52').dim,true);
const body=v.rows.find(r=>str(r).includes('시험 통과'));assert.equal(body.find(p=>p.text.includes('시험')).foreground,253);
const late=v.rows.find(r=>str(r).includes('not delivered'));assert.equal(late.find(p=>p.text==='not delivered').foreground,186);
const top=v.rows.findIndex(r=>str(r).includes('MESSAGES +4'));
for(let y=top;y<=top+6;y++)assert(v.hits.some(h=>h.row===y&&h.action==='popup:MESSAGES'),'row '+y);
console.log(JSON.stringify({rows:m.rows.length}));
""")
        self.assertIn('"rows":6', out)

    def test_short_of_rows_a_message_folds_to_one_row_before_fewer_show(self):
        # Every height from the person's 46 rows down: the rule over the last row goes first; then (after the
        # head's structural rows) every message takes the one-row form while all three stay (11-20: three at most, and MESSAGES
        # with MEM is the first section to go down); fewer show only after that, down to the newest one; the newest stay.
        out = self.node(r"""
const seen=[];
for(let h=46;h>=18;h--){
  const v=draw({height:h}),m=section(v),ls=lines(v),rule=/^│ ─+ │$/.test(ls[ls.length-3]);
  if(!m){seen.push({h,count:0,form:'',rule});continue;}
  const two=m.rows.length>=2&&/^  \S/.test(m.rows[1]),count=two?m.rows.length/2:m.rows.length;
  seen.push({h,count,form:two?'two':'one',rule,newest:m.rows.at(two?-2:-1)});
}
let sawOne=false,sawFewer=false,ruleGone=false;
for(const s of seen){
  if(!s.rule)ruleGone=true;
  if(s.form==='one')sawOne=true;
  if(s.form==='one'||s.count<3)assert(ruleGone,JSON.stringify(s));
  if(s.count&&s.count<3){sawFewer=true;assert.equal(s.form,'one',JSON.stringify(s));}
  if(s.count)assert.match(s.newest,/\[12\]/,JSON.stringify(s));
}
assert(sawOne&&sawFewer,JSON.stringify(seen));
assert.deepEqual([seen[0].count,seen[0].form,seen[0].rule],[3,'two',true]);
// The section stands at one row (the newest message) where rows are the shortest of these heights.
assert.deepEqual([seen.at(-1).count,seen.at(-1).form],[1,'one'],JSON.stringify(seen.at(-1)));
console.log(JSON.stringify(seen.map(s=>[s.h,s.count,s.form,s.rule?1:0])));
""")
        self.assertTrue(out.strip())

    def test_three_messages_and_none_hidden_read_whole(self):
        self.node(r"""
const v=draw({messages:3}),m=section(v);
assert.match(m.head,/^MESSAGES ─+ ›$/,m.head);
assert.equal(m.rows.length,6,m.rows.join('\n'));
""")

    # 2 ---------------------------------------------------------------------------------------------------------
    def test_a_sent_answer_line_stays_ten_minutes(self):
        self.node(r"""
const create=require(path.join(shell,'block-input.cjs'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const sentLines=b=>b.sent().map(r=>r.map(x=>x.text).join(''));
(async()=>{
  const real=Date.now;let now=real.call(Date);Date.now=()=>now;
  const a={id:'a',revision:'r',answerable:true,questions:[{id:'q',question:'FIRST_Q',choices:['Alpha','Beta']}]};
  const n={id:'n',revision:'r',answerable:true,questions:[{id:'q',question:'NEXT_Q',choices:['One']}]};
  const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update([a,n]);b.enter();b.input(Buffer.from('\x1b[B\r'));await tick();
  assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);
  // Its session has it within seconds: the line stays (it went here before), and the panel keeps asking for it.
  now+=5000;b.update([{...a,answerable:false,request_state:'received'},n]);
  assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);assert.deepEqual(b.references().map(x=>x.id),['a','n']);
  now+=60000;b.update([{...a,answerable:false,request_state:'applied'},n]);
  assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);
  // A next question comes: the line keeps its place first, the next question after it (the existing order).
  b.update([{...a,answerable:false,request_state:'applied'},n,{id:'m',revision:'r',answerable:true,questions:[{id:'q',question:'THIRD_Q',choices:['X']}]}]);
  assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);assert.match(JSON.stringify(b.summary()),/NEXT_Q/);
  // Nine minutes after it was sent it still stands; past ten it goes at the next paint and the next update.
  now+=(9*60-65)*1000;assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);
  now+=61000;assert.deepEqual(sentLines(b),[]);
  b.update([{...a,answerable:false,request_state:'applied'},n]);assert.deepEqual(b.references().map(x=>x.id),['n']);
  // A delivery that failed still brings the question back with its word.
  const x=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  x.update([a]);x.enter();x.input(Buffer.from('\r'));await tick();
  x.update([{...a,answerable:false,request_state:'unknown',reason:'message-answer-delivery-unconfirmed',answer_state:'answer_below'}]);
  assert.deepEqual(sentLines(x),[]);
  Date.now=real;console.log('PASS');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
""")

    def test_a_sent_answer_line_leaves_when_the_clock_goes_back(self):
        # A sent moment ahead of now is no age at all: the line leaves rather than standing for the gap.
        self.node(r"""
const create=require(path.join(shell,'block-input.cjs'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const sentLines=b=>b.sent().map(r=>r.map(x=>x.text).join(''));
(async()=>{
  const real=Date.now;let now=real.call(Date);Date.now=()=>now;
  const a={id:'a',revision:'r',answerable:true,questions:[{id:'q',question:'FIRST_Q',choices:['Alpha','Beta']}]};
  const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update([a]);b.enter();b.input(Buffer.from('\x1b[B\r'));await tick();
  assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);
  b.update([{...a,answerable:false,request_state:'received'}]);
  assert.deepEqual(sentLines(b),['✓ answer sent · Beta']);
  // The clock goes an hour back: the sent moment is ahead of now, so the line has no age to keep it.
  now-=60*60*1000;
  assert.deepEqual(sentLines(b),[]);
  b.update([{...a,answerable:false,request_state:'applied'}]);
  assert.deepEqual(sentLines(b),[]);
  // Forward again past the hold: it stays gone (the entry kept no line to bring back).
  now+=70*60*1000;assert.deepEqual(sentLines(b),[]);
  Date.now=real;console.log('PASS');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
""")

    # 3 ---------------------------------------------------------------------------------------------------------
    def test_the_wait_blinks_on_fleets_beat_between_two_clearly_different_reds(self):
        out = self.node(r"""
const blinkCells=v=>v.motion?v.motion.cells.filter(c=>c.blink):[];
const tones=(v,f)=>{const all=v.motion.cells,at=v.motion.at(f);return [...new Set(all.map((c,i)=>c.blink?at[i].foreground:null).filter(x=>x!==null))];};
for(const [state,since] of [['idle',0],['working',995],['blocked',3]]){
  const v=draw({state,since,frame:1000});
  assert(blinkCells(v).length>=10,state);
  // The clock's second: lit (the first red) frames 1000-1009, the other 1010-1019, whenever the state began.
  for(let f=1000;f<1010;f++)assert.deepEqual(tones(v,f),[view.FACES.dark.waitBlink[0]],state+' '+f);
  for(let f=1010;f<1020;f++)assert.deepEqual(tones(v,f),[view.FACES.dark.waitBlink[1]],state+' '+f);
  if(state==='idle')assert.equal(v.motion.next(1003),1010);
}
// The folded strip's wait the same.
const f=draw({folded:true,width:120,height:1,state:'idle',frame:2000,since:7});
const fc=f.motion.cells;assert(fc.length>=5);
assert.deepEqual([...new Set(f.motion.at(2004).map(p=>p.foreground))],[view.FACES.dark.waitBlink[0]]);
assert.deepEqual([...new Set(f.motion.at(2014).map(p=>p.foreground))],[view.FACES.dark.waitBlink[1]]);
assert.equal(f.motion.next(2004),2010);
// Nothing blinks with `rule motion` off, mono, NO_COLOR or eight colors; with nothing waiting an idle panel has no motion.
for(const o of [{motion:'off'},{theme:'mono'},{no_color:true},{colors:8}])assert.equal(blinkCells(draw({state:'idle',...o})).length,0,JSON.stringify(o));
assert.equal(draw({state:'idle',question:false}).motion,null);
assert.equal(draw({state:'idle',question:false,folded:true,width:120,height:1}).motion,null);
console.log(JSON.stringify({dark:view.FACES.dark.waitBlink,light:view.FACES.light.waitBlink}));
""")
        pairs = json.loads(out.strip().splitlines()[-1])
        dark = [lightness(v) for v in pairs['dark']]
        light = [lightness(v) for v in pairs['light']]
        self.assertGreaterEqual(abs(dark[0] - dark[1]), 25, pairs)
        self.assertGreaterEqual(min(dark), 40, pairs)   # readable on a dark ground
        self.assertGreater(dark[0], dark[1], pairs)     # lit first
        self.assertGreaterEqual(abs(light[0] - light[1]), 25, pairs)
        self.assertLessEqual(max(light), 60, pairs)     # readable on a light ground
        self.assertLess(light[0], light[1], pairs)      # lit (stronger on a light ground) first

    # 4 ---------------------------------------------------------------------------------------------------------
    def test_the_rule_light_is_body_bright_and_steps_down_evenly(self):
        faces = json.loads(self.node("console.log(JSON.stringify(view.FACES));"))
        for name, sign in (('dark', 1), ('light', -1)):
            with self.subTest(face=name):
                face = faces[name]
                steps = [lightness(v) for v in face['scan']]   # dimmest first: level 1 .. 5
                self.assertEqual(len(steps), 5)
                # The head at least as strong as the body text on its ground.
                self.assertGreaterEqual(sign * (steps[-1] - lightness(face['body'])), 0, (face['scan'], face['body']))
                # Four even steps down from the head, each an L* gap of 7 or more.
                gaps = [sign * (b - a) for a, b in zip(steps, steps[1:])]
                self.assertTrue(all(g >= 7 for g in gaps), [round(g, 1) for g in gaps])
                # The dimmest step stands within one ramp step of the still rule's own gray.
                self.assertLessEqual(abs(steps[0] - lightness(face['messages'])), 9, (face['scan'], face['messages']))

    # 5 ---------------------------------------------------------------------------------------------------------
    def test_a_quiet_notice_is_a_dim_line_and_a_caution_keeps_its_look(self):
        self.node(r"""
const quiet='기억 정리 · 추가 2 · 갱신 0 · 강화 1 · 건너뜀 0 · 일부만 읽음 — 앞부분은 다음 정리에서 계속';
const loud='[정리] 대화는 압축했지만 이어서해를 넣지 못했습니다(입력 대기 아님). 이어서 하려면 이어서해라고 입력하세요.';
const parts=v=>v.rows.flat();
for(const [side,width,height] of [[true,50,46],[false,120,14]]){
  const q=draw({question:false,tidy:quiet,quiet:true,side,width,height}),qs=lines(q).join('\n');
  assert(!qs.includes('⚠'),qs);assert(qs.includes('기억 정리'),qs);
  const qp=parts(q).filter(p=>/기억|추가|갱신|앞부분/.test(p.text));assert(qp.length&&qp.every(p=>p.dim===true&&p.foreground!==186),JSON.stringify(qp));
  if(side)assert(lines(q).filter(l=>/기억 정리|앞부분/.test(l)).length<=2,qs);
  const l=draw({question:false,tidy:loud,side,width,height}),ls=lines(l).join('\n');
  assert(ls.includes('⚠ [정리]'),ls);assert(parts(l).some(p=>p.text.startsWith('⚠')&&p.foreground===186),ls);
}
// The folded strip by the same rule.
const fq=draw({folded:true,width:160,height:1,question:false,tidy:'기억 정리 · 추가 2',quiet:true}),fl=draw({folded:true,width:160,height:1,question:false,tidy:'[정리] 압축 실패'});
assert(!lines(fq)[0].includes('⚠')&&lines(fq)[0].includes('기억 정리 · 추가 2'),lines(fq)[0]);
assert(fq.rows[0].filter(p=>p.text.includes('기억')).every(p=>p.dim===true),JSON.stringify(fq.rows[0]));
assert(lines(fl)[0].includes('⚠ [정리] 압축 실패'),lines(fl)[0]);
""")

    def test_the_notice_writer_marks_a_result_that_ended_well(self):
        import session_tidy as st
        import session_tidy_runner as runner
        with tempfile.TemporaryDirectory(prefix='pp16-') as directory, \
                mock.patch.dict(os.environ, {'XDG_STATE_HOME': directory, 'FLEET_NOW_LANG': 'ko'}):
            cwd = Path(directory) / 'proj'; cwd.mkdir()
            seat = st.resolve_seat('claude', str(cwd))
            st.write_notice(seat, '[정리] 묶음 b-1: 추가 2 · 갱신 0 — 되돌리기: mem tidy-undo b-1', author_harness='claude',
                            author_sid='sid-A', notice_id='tidy:x', level='done')
            text, paths, level = st.waiting_notice_display(seat, 'claude', 'sid-A', with_level=True)
            self.assertEqual((level, bool(paths)), ('done', True))
            self.assertIn('mem tidy-undo b-1', text, 'the line the session receives is unchanged')
            self.assertEqual(st.waiting_notice_display(seat, 'claude', 'sid-A'), (text, paths))
            # The same notice written again as a failure is no longer quiet.
            st.write_notice(seat, '[정리] 기억 정리가 중간에 멈췄습니다', author_harness='claude', author_sid='sid-A', notice_id='tidy:x')
            self.assertEqual(st.waiting_notice_display(seat, 'claude', 'sid-A', with_level=True)[2], '')
        # The runner's notice for a finished batch passes the level; its failures pass none.
        with mock.patch.object(runner.st, 'write_notice') as wrote, mock.patch.object(runner, '_card_seat', return_value='seat'):
            runner.notify({'harness': 'claude', 'sid': 's', 'id': 'i'}, 'ok', level='done')
            runner.notify({'harness': 'claude', 'sid': 's', 'id': 'i'}, runner.failure_line('boom'))
        self.assertEqual([c.kwargs.get('level') for c in wrote.call_args_list], ['done', ''])

    def test_the_panel_line_of_a_quiet_result_leaves_the_machines_words_out(self):
        import session_shell as shell
        with mock.patch.dict(os.environ, {'FLEET_NOW_LANG': 'ko'}):
            cases = {
                '[정리] 묶음 b-20261116-ab12: 추가 2 · 갱신 0 · 강화 1 · 건너뜀 0 · 일부만 읽음(byte 2,880,000–3,145,728 / 전체 3,145,728)'
                ' — 앞부분은 다음 정리에서 계속 — 되돌리기: mem tidy-undo b-20261116-ab12':
                    '기억 정리 · 추가 2 · 갱신 0 · 강화 1 · 건너뜀 0 · 일부만 읽음 — 앞부분은 다음 정리에서 계속',
                '[tidy] batch tidy-7f3a: added 2 · updated 0 — undo: mem tidy-undo tidy-7f3a': '기억 정리 · added 2 · updated 0',
                '[정리] 읽은 범위에는 정리할 대화가 없었습니다 · 일부만 읽음(열린 질문 앞까지, rowid 10–20 / 전체 90)':
                    '기억 정리 · 읽은 범위에는 정리할 대화가 없었습니다 · 일부만 읽음(열린 질문 앞까지)',
                '[정리] 새로 정리할 대화가 없습니다.': '기억 정리 · 새로 정리할 대화가 없습니다.',
            }
            for given, want in cases.items():
                self.assertEqual(shell.quiet_notice_line(given), want)

    def test_the_projection_carries_the_quiet_mark(self):
        import session_shell as shell
        import session_tidy
        import human_wait_state
        from fleet.config import ui_text
        sid = '00000000-0000-4000-8000-000000001116'
        view = {'route_panels': {}, 'items': [], 'by_session': {}}
        done = '[정리] 묶음 b-1: 추가 2 · 갱신 0 — 되돌리기: mem tidy-undo b-1'
        loud = '[정리] 대화는 압축했지만 이어서해를 넣지 못했습니다(x). 이어서 하려면 이어서해라고 입력하세요.'

        def project(notice, level, upgrade=''):
            with mock.patch.object(session_tidy, 'waiting_notice_display', return_value=(notice, [], level)), \
                    mock.patch.object(human_wait_state, 'cached_view', return_value=view), \
                    mock.patch.object(human_wait_state, 'cached_statusline', return_value=((0, 0), '')):
                return shell.panel_projection('claude', sid, columns=50, rows=46, upgrade_note=upgrade)['panel']
        panel = project(done, 'done')
        self.assertEqual((panel['tidy_quiet'], panel['tidy_notice']), (True, shell.quiet_notice_line(done)))
        self.assertNotIn('tidy-undo', panel['tidy_notice'])
        panel = project(loud, '')
        self.assertEqual((panel['tidy_quiet'], panel['tidy_notice']), (False, loud))
        for note in ('pending', 'scrollback'):
            panel = project('', '', note)
            self.assertEqual((panel['tidy_quiet'], panel['tidy_notice']), (True, ui_text('session_shell.upgrade_' + note)))

    # 6 ---------------------------------------------------------------------------------------------------------
    def test_a_thin_rule_parts_the_bottom_sections_from_the_last_row(self):
        self.node(r"""
for(const o of [{},{messages:0},{mem:false}]){
  const v=draw(o),ls=lines(v),n=ls.length;
  assert.match(ls[n-2],/5h 23% │ 7d 61%/,ls.join('\n'));
  assert.match(ls[n-3],/^│ ─{46} │$/,ls.join('\n'));
  // The section heads' rule: its look (dim, the messages role).
  const rule=v.rows[n-3].find(p=>/^─+$/.test(p.text));assert(rule&&rule.dim===true,JSON.stringify(v.rows[n-3]));
  assert.match(ls[n-4],o.mem===false?/[^─]/:/^│   점멸은 1초 박자로/,ls[n-4]);
}
// Without MESSAGES and MEM nothing stands to part; under the native area there is no MEM and no rule.
const none=lines(draw({messages:0,mem:false}));assert(!/^│ ─{46} │$/.test(none.at(-3)),none.join('\n'));
const bottom=lines(draw({side:false,width:120,height:14}));assert(!bottom.some(l=>/^│ ─{116} │$/.test(l)),bottom.join('\n'));
""")

    # 7 ---------------------------------------------------------------------------------------------------------
    def test_the_fold_button_points_down_at_the_last_rows_right_end(self):
        self.node(r"""
for(const o of [{position:'right'},{position:'left'},{side:false,width:120,height:14}]){
  const v=draw(o),w=o.width??50,ls=lines(v),foot=ls[ls.length-2];
  assert(foot.endsWith('⚙   ▾ │'),JSON.stringify(o)+'\n'+foot);
  assert(!/[»«]/.test(ls.join('')),JSON.stringify(o));
  const fold=v.hits.find(h=>h.action==='fold'),gear=v.hits.find(h=>h.action==='settings');
  assert.deepEqual([fold.left,fold.right],[w-4,w-2],JSON.stringify(o));
  assert.deepEqual([gear.left,gear.right],[w-8,w-6],JSON.stringify(o));
  // `▾` stands inside the fold button's press, `⚙` inside the gear's, and the cell between is neither's.
  assert.equal([...foot][w-3],'▾');assert.equal([...foot][w-7],'⚙');
  assert(!v.hits.some(h=>h.row===fold.row&&h.left<=w-5&&h.right>=w-5&&['fold','settings'].includes(h.action)));
}
// Folded, `▴` keeps the strip's right end: a right panel folds and opens at its right end.
const f=draw({folded:true,width:120,height:1});assert.equal([...lines(f)[0]].at(-1),'▴');
""")


if __name__ == "__main__":
    unittest.main()
