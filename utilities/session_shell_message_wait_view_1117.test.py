"""11-17: a sent message that has not gone in reads ``awaiting delivery`` and why in the panel's MESSAGES and request lines.

A sent row's delivery value carries ``[whole, key, word]``: the word stays whole, its why ends in ``…`` when the row is
short of cells, the MESSAGES popup shows it whole; a request's status does the same with ``status_head``. Everything
here is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"

FIXTURE = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]+'/panel-view.cjs');
const P=t=>({text:t}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const names=['plan','plan-check','execute','impl-review','test','report'];
const six=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true,key:'stg2_on'}:{name,mark:''});
const paint=g=>({glyph:P(g),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh'),ctx:P('38%'),gauge5:[P('━━─── 38%')]});
const R={
 staged:{id:'b',title:'속도 3단계',kind:'working',state:'working',elapsed:'1h',shape:'staged',stages:six(2),kind_word:'code',role:'owner',paint:paint('⠋')},
 solo:{id:'a',title:'셸 고치기',kind:'working',state:'working',elapsed:'41m',tool:'Edit',target:'utilities/session-shell/host.cjs',tool_age:'3m',shape:'one-shot-owner',kind_word:'lab',role:'owner',stages:[{name:'one-shot',current:true,mark:'●'}],paint:paint('⠋')},
 direct:{id:'d',title:'작은 수정',kind:'working',state:'working',elapsed:'3m',shape:'직접',kind_word:'code',stages:[],paint:paint('⠏')},
 failed:{id:'e',title:'PG-50',kind:'dead',state:'dead',elapsed:'54m',shape:'staged',kind_word:'code',stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'✕'},{name:'test',mark:''}],paint:paint('✕')},
};
const STYLES={dim:{dim:true},loc_repo:{foreground:253},stg2_on:{foreground:150,bold:true},g_dead:{foreground:196,bold:true},lvl_g:{foreground:114},lvl_r:{foreground:196,bold:true},g_spin:{foreground:150}};
const base={repo:[P('hearting/')],messages:[],styles:STYLES};
const render=(routes,width,height,{side=true,model={},configuration={},harness='claude'}={})=>view.render({width,height,side,fill:side,harness,
 model:{...base,routes,route_count:routes.length,route_total:routes.length,...model},state:'working',configuration,questions:()=>[]});
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const draw=(routes,width,height,o)=>plain(render(routes,width,height,o));
const kindRows=rows=>rows.filter(s=>/│ │   (code|lab|frame)\(|│ │   \d\/\d|│ │   (staged|one-shot)/.test(s)).map(s=>s.replace(/^│ │ +/,'').replace(/ +│ │$/,''));
'''


class MessageWaitViewTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_waiting_sent_row_keeps_its_word_and_the_why_ends_in_an_ellipsis(self):
        self.node(r"""
const why='전달 대기 · 입력창에 글 있음 · 보내거나 지우면 받음';
const message=(delivery,extra={})=>({way:'→',who:'[6c]',harness:'claude',when:'3m',kind:'steer',body:'검토 요청',text:'검토 요청',at:1,delivery,...extra});
const sent=message(['보냄','dim']),waiting=message([why,'dim','전달 대기']),bare=message(['전달 대기','dim','전달 대기']);
const top=(m,width,side=true)=>{const v=render([],width,30,{side,model:{messages:[m],route_count:0,route_total:0}});return plain(v).filter(s=>/→ \[6c\]/.test(s)).map(s=>s.replace(/^│ │? ?/,'').replace(/ *│$/,''));};
const rowCount=(m,width,side)=>plain(render([],width,30,{side,model:{messages:[m]}})).length;
// 40 cells beside: the word whole, the why as far as it goes and then `…`; no row is added.
for(const width of [40,44,50]){
 const line=top(waiting,width)[0];assert(line.includes('전달 대기'),line);
 const inner=width-4;assert(cells(line.trimEnd())<=inner,width+' '+line);
}
const narrow=top(waiting,40)[0];assert(/전달 대기 · .*…$/.test(narrow.trimEnd()),narrow);
assert.equal(rowCount(waiting,40,true),rowCount(sent,40,true));
// A word with no why reads whole; a sent one reads as before.
assert(/ · 전달 대기 *$/.test(top(bare,40)[0])&&!top(bare,40)[0].includes('…'));assert(/보냄/.test(top(sent,40)[0]));
// Room enough: the whole why, no ellipsis, the harness and time still in front.
const wide=top(waiting,120,false)[0];assert(wide.includes(why)&&!wide.includes('…'),wide);
// The bottom layout (one wide row for the message head) reads the same word.
for(const width of [60,80,100]){const line=top(waiting,width,false)[0];assert(line.includes('전달 대기'),width+line);}
""")

    def test_the_messages_popup_shows_the_whole_why(self):
        self.node(r"""
const why='전달 대기 · 입력창에 글 있음 · 보내거나 지우면 받음';
const m={way:'→',who:'[6c]',harness:'claude',when:'3m',kind:'steer',body:'검토 요청',text:'검토 요청',at:1,delivery:[why,'dim','전달 대기']};
for(const columns of [72,60,44,30]){
 const rows=view.messagesBody({model:{messages:[m],styles:{}},columns}).map(r=>r.map(p=>p.text).join(''));
 const shown=rows.join('\n').replace(/\n\s*/g,' ');
 assert(shown.includes(why)||rows.join(' ').replace(/\s+/g,' ').includes(why),columns+'\n'+rows.join('\n'));
 for(const s of rows)assert(cells(s)<=columns,columns+' '+s);
}
// a sent one reads as before, on the head row
const rows=view.messagesBody({model:{messages:[{...m,delivery:['보냄','dim']}],styles:{}},columns:72}).map(r=>r.map(p=>p.text).join(''));
assert(/→ \[6c\] claude · 3m · steer · 보냄$/.test(rows[0]),rows[0]);
""")

    def test_a_request_line_keeps_its_status_word_in_every_form(self):
        self.node(r"""
const why='전달 대기 · 입력창에 글 있음 · 보내거나 지우면 받음';
const request={id:'rq-1',way:'→',glyph:'→',who:'[6c]',title:'검토 요청 — 셸 패널 시안',status:why,status_head:'전달 대기',age:'3m',kind:'request-out',state:'open',side:'out',delivery:'waiting',details:[],body:[]};
const sentRequest={...request,status:'보냄',status_head:undefined};
const forms=(q,width)=>{const v=render([],width,40,{model:{requests:[q],messages:[]}});return plain(v).filter(s=>/→ \[6c\]|검토|전달|보냄|입력창/.test(s));};
for(const width of [40,50,60]){const rows=forms(request,width);assert(rows.some(s=>s.includes('전달 대기')),width+'\n'+rows.join('\n'));
 for(const s of rows)assert(cells(s)<=width,s);}
assert(forms(sentRequest,40).some(s=>s.includes('보냄')));
// the popup's head row and the one-row form at 36 and 72 cells
const mine=view.messagesBody({model:{requests:[request],messages:[],styles:{}},columns:72}).map(r=>r.map(p=>p.text).join(''));
assert(mine.join('\n').includes('전달 대기'),mine.join('\n'));
// the one-row form (the popup's head row, a short box) where even the title cannot stay: the word stays whole, the why ends in `…`
for(const columns of [36,30,24]){
 const rows=view.messagesBody({model:{requests:[request],messages:[],styles:{}},columns}).map(r=>r.map(p=>p.text).join(''));
 assert(rows[0].includes('전달 대기')&&cells(rows[0])<=columns,columns+' '+rows[0]);
}
""")


if __name__ == "__main__":
    unittest.main()
