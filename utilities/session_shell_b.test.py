"""B uses canonical display rows and keeps local edits separate from native effects."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'utilities'),str(ROOT/'tools')]
import session_shell
from fleet.session_block import capacity, question_presentations


class BBottomTests(unittest.TestCase):
    def node(self, code):
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_question_choices_show_before_focus_and_decision_needs_an_explicit_choice(self):
        self.node(r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});
b.update([{id:'id',revision:'r',answerable:true,answer_kind:'decision',questions:[{id:'q',question:'Approve export?'}]}]);
assert.equal(b.focused,false);const visible=JSON.stringify(b.detail());
assert.ok(visible.includes('Approve export?')&&visible.includes('approve')&&visible.includes('reject'));
b.enter();b.input(Buffer.from('\r'));await new Promise(setImmediate);assert.equal(sent.length,0);
b.input(Buffer.from('2\r'));await new Promise(setImmediate);assert.deepEqual(sent,[{question_id:'id',revision:'r',decision:'reject'}]);
b.input(Buffer.from('\r'));await new Promise(setImmediate);assert.equal(sent.length,1);
})();''')

    def test_arrows_move_the_highlighted_choice_and_enter_sends_it_once(self):
        # Spec rule 18 (2026-11-11 night): ↑↓ and Enter as in Claude's own question;
        # the arrow-picked value is the number-picked value, sent once.
        self.node(r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
const item=(id,extra={})=>({id,revision:'r',answerable:true,questions:[{id:'q',question:'Which?',choices:['one','two','three'],...extra}]});
const lit=b=>b.detail({budget:20,columns:60}).filter(row=>row[0]?.text==='▸').map(row=>row.slice(2).map(p=>p.text).join('').replace(/^\d+ /,''));
for(const [keys,want] of [['\x1b[B\r','two'],['\x1bOB\x1bOB\x1b[A\r','two'],['2\r','two'],['\r','one'],['\x1b[A\x1b[A\r','one'],['\x1b[B\x1b[B\x1b[B\x1b[B\r','three']]){
 let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});
 b.update([item('id')]);b.enter();
 if(keys==='\r')assert.deepEqual(lit(b),['1 one'.slice(2)],'the first choice starts highlighted');
 b.input(Buffer.from(keys));await new Promise(setImmediate);
 assert.deepEqual(sent,[{question_id:'id',revision:'r',answers:{q:[want]}}],JSON.stringify(keys));
 b.input(Buffer.from('\r'));await new Promise(setImmediate);assert.equal(sent.length,1,'sent once');
}
// The recommended choice is where the highlight starts; the hint names the keys.
{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update([item('rec',{recommended:2})]);b.enter();
 assert.deepEqual(lit(b),['three']);b.input(Buffer.from('\x1b[A'));assert.deepEqual(lit(b),['two']);
 assert.ok(b.detail({budget:20,columns:80}).some(row=>row.map(p=>p.text).join('').includes('↑↓ choose · Enter send · t text')));}
// Without focus the arrows and Enter are the conversation's: every byte goes back.
{let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});b.update([item('id')]);
 const rest=b.input(Buffer.from('\x1b[B\r'));assert.equal(rest.toString(),'\x1b[B\r');await new Promise(setImmediate);assert.equal(sent.length,0);}
// `t` still types an answer, and arrows do not move a choice while typing.
{let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});b.update([item('id')]);b.enter();
 b.input(Buffer.from('tmine\x1b[B\r'));await new Promise(setImmediate);assert.deepEqual(sent,[{question_id:'id',revision:'r',answers:{q:['mine']}}]);}
// A decision highlights nothing until ↑↓ or a number picks one; then Enter sends it.
{let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});
 b.update([{id:'d',revision:'r',answerable:true,answer_kind:'decision',questions:[{id:'q',question:'Approve?'}]}]);b.enter();
 b.input(Buffer.from('\r'));await new Promise(setImmediate);assert.equal(sent.length,0);
 b.input(Buffer.from('\x1b[B\x1b[B\r'));await new Promise(setImmediate);assert.deepEqual(sent,[{question_id:'d',revision:'r',decision:'reject'}]);}
})().catch(e=>{console.error(e);process.exit(1);});''')

    def test_the_chosen_choice_stands_out_by_its_mark_weight_and_brightness(self):
        # Spec E9 rule 53: while ↑↓ choose, the chosen choice's row leads with `▸` (this session's
        # harness color, bold) and its words are bold in the bright body color; every other
        # choice keeps the mark's column blank and reads dim grey. A choice wrapped over several
        # rows has its words in the same form on each (the mark on its first row only, so it
        # never reads as two choices). Without color (NO_COLOR, mono) the mark and the
        # weight alone tell them apart. Unfocused, the mark's column stays blank (no shift).
        # The panel keeps the mark's harness color (it is not the question's rose accent).
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const view=require(require('node:path').join(require('node:path').dirname(process.argv[1]),'panel-view.cjs'));
const color={dim:{foreground:-1,dim:true},loc_repo:{foreground:253},g_blocked:{foreground:217},lvl_g:{foreground:150},lvl_r:{foreground:210},harness:{foreground:173}};
const plain={dim:{foreground:null,dim:false},loc_repo:{foreground:null},g_blocked:{foreground:null},lvl_g:{foreground:null},lvl_r:{foreground:null},harness:{foreground:null}};
const S=(t,st,styles)=>({text:t,...styles[st]});
const long='a choice whose words run past one row';
const display=styles=>({head:[S('◑ ','g_blocked',styles),S('questions 1','dim',styles),S(' · 1/1 · ','dim',styles),S('answer needed','g_blocked',styles)],
 body:[[S('Which?','loc_repo',styles)]],source:[],
 options:[[S('1 ','dim',styles),S('one','loc_repo',styles)],[S('2 ','dim',styles),S('a choice whose','loc_repo',styles)],[S('  ','dim',styles),S('words run past','loc_repo',styles)],[S('  ','dim',styles),S('one row','loc_repo',styles)],[S('3 ','dim',styles),S('three','loc_repo',styles)]]});
const make=styles=>{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
 b.update([{id:'id',revision:'r',answerable:true,questions:[{id:'q',question:'Which?',choices:['one',long,'three'],display:display(styles)}]}],'',{styles,labels:{}});return b;};
const options=b=>b.detail({budget:20,columns:40}).filter(row=>row.some(p=>/^(one|a choice whose|words run past|one row|three)$/.test(p.text)));
const words=row=>row.filter(p=>/[a-z]/.test(p.text));
// Unfocused: the mark's column is blank and the words keep their own paint.
{const rows=options(make(color));assert.equal(rows.length,5);
 for(const row of rows){assert.equal(row[0].text,'  ');assert(!row.some(p=>p.text==='▸'));assert(words(row).every(p=>p.foreground===253&&!p.dim&&!p.bold),JSON.stringify(row));}}
for(const [styles,colored] of [[color,true],[plain,false]]){
 const b=make(styles);b.enter();b.input(Buffer.from('\x1b[B'));
 const rows=options(b);assert.equal(rows.length,5);
 rows.forEach((row,i)=>{
  const chosen=i>=1&&i<=3;
  if(chosen){
   // (a) `▸` in the harness color, bold, on the choice's first row; the words bold in the bright
   // body color. (d) The same words on every row of a wrapped choice.
   if(i===1){assert.equal(row[0].text,'▸',JSON.stringify(row));assert.equal(row[0].bold,true);assert.equal(row[1].text,' ');
    if(colored)assert.equal(row[0].foreground,173);}
   else{assert.equal(row[0].text,'  ',JSON.stringify(row));assert(!row.some(p=>p.text==='▸'));}
   assert(words(row).every(p=>p.bold===true&&!p.dim&&(colored?p.foreground===253:true)),JSON.stringify(row));
  }else{
   // (b) The mark's place blank, the words dim grey and not bold. (c) Without color: not bold.
   assert.equal(row[0].text,'  ',JSON.stringify(row));assert(!row.some(p=>p.text==='▸'));
   assert(words(row).every(p=>!p.bold&&(colored?p.dim===true&&p.foreground!==253:true)),JSON.stringify(row));
  }
 });
 // The chosen choice moves with the arrows, the form with it.
 b.input(Buffer.from('\x1b[B'));const moved=options(b);assert.equal(moved[4][0].text,'▸');assert.equal(moved[1][0].text,'  ');
}
// The panel: `▸` keeps the harness color on the terminal's own background and on a known face; it is not rose.
{const b=make(color);b.enter();b.input(Buffer.from('\x1b[B'));const q=o=>b.detail(o);
 for(const [cfg,background] of [[{},''],[{},'dark'],[{shade:'medium'},'dark']]){
  const r=view.render({width:40,height:30,side:true,model:{styles:color,colors:256},configuration:cfg,background,questions:q});
  const mark=r.rows.flat().find(p=>p.text==='▸');assert(mark,JSON.stringify(cfg));
  assert.notEqual(mark.foreground,view.FACES.dark.accent,JSON.stringify(cfg));assert.equal(mark.foreground,173,JSON.stringify(cfg));assert.equal(mark.bold,true);}
 const mono=view.render({width:40,height:30,side:true,model:{styles:color,colors:256},configuration:{theme:'mono'},questions:q}).rows.flat();
 assert.equal(mono.find(p=>p.text==='▸').bold,true);
}
""")

    def test_a_wait_not_answerable_here_names_where_to_answer_and_never_its_machine_reason(self):
        # Spec E9 rule 54: the editor's head word and its notice on Enter are the backend's
        # phrase for the reason (the five `answer_*` words); the body row is the backend's whole
        # phrase; the machine reason never shows. A local send state still wins while it lasts.
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
const labels={answer_below:'BELOW',answer_there:'THERE',answer_parent:'PARENT',answer_sending:'SENDING',answer_closed:'CLOSED',submitted:'SUBMITTED'};
const item=(state,reason,extra={})=>({id:'w-'+state,revision:'r',answerable:false,reason,...(state?{answer_state:state}:{}),...extra,
 questions:[{id:'q',question:'Which?',choices:['a','b'],display:{head:[{text:'◑ '},{text:'questions 1'},{text:' · 1/1 · '},{text:'x'}],body:[[{text:'Which?'}]],source:[],options:[[{text:'WHOLE '+state}]]}}]});
const flat=b=>b.detail({budget:12,columns:40}).map(r=>r.map(p=>p.text).join('')).join('\n');
for(const [state,reason,word] of [['answer_below','marker-has-no-bound-native-response-surface','BELOW'],['answer_parent','owner-correction-lineage-unverified','PARENT'],
  ['answer_sending','action-pending','SENDING'],['answer_closed','codex-native-request-not-current','CLOSED'],['answer_there','no exact owner session binding','THERE'],[null,'unknown-reason','BELOW']]){
 let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});
 b.update([item(state,reason)],'',{labels});
 let text=flat(b);
 assert(text.split('\n')[0].endsWith(word),text);assert(!text.includes(reason),text);
 if(state)assert(text.includes('WHOLE '+state),text);
 assert(!/unavailable/.test(text),text);
 b.enter();b.input(Buffer.from('\r'));await new Promise(setImmediate);assert.equal(sent.length,0);
 text=flat(b);assert(!text.includes(reason),text);assert(text.split('\n').some(l=>l.trim()===word),text);
}
// A local send state wins over the backend's phrase while it lasts.
{const b=create({changed(){},answer:async()=>new Promise(()=>{}),action(){}});
 b.update([{id:'o',revision:'r',answerable:true,questions:[{id:'q',question:'Which?',choices:['a']}]}],'',{labels});b.enter();b.input(Buffer.from('\r'));
 await new Promise(setImmediate);b.update([{id:'o',revision:'r',answerable:false,reason:'action-pending',answer_state:'answer_sending',questions:[{id:'q',question:'Which?',choices:['a']}]}],'',{labels});
 assert(flat(b).split('\n')[0].includes('SUBMITTED'),flat(b));}
})().catch(e=>{console.error(e);process.exit(1);});""")

    def test_an_answered_question_leaves_the_panel_once_focus_returns(self):
        # Correction 3 (11-13): after a panel answer the question stayed in the panel for good
        # (`✓ consumed`): the editor held it, so the backend kept returning it as a retained
        # reference. While focused the receipt stays readable; leaving focus, or a settled one
        # arriving while unfocused, lets it go, and it is no longer asked for.
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
const open={id:'S4:q',revision:'r',answerable:true,questions:[{id:'q',question:'Which?',choices:['a','b']}]};
const done={...open,answerable:false,request_state:'applied'};
const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
b.update([open]);b.enter();b.input(Buffer.from('\r'));await new Promise(setImmediate);
b.update([done]);
assert(JSON.stringify(b.detail()).includes('applied'),'the receipt reads while focused');
assert.deepEqual(b.references(),[{id:'S4:q',revision:'r'}]);
b.leave();
assert.deepEqual(b.references(),[]);assert.deepEqual(b.detail(),[]);
// The backend no longer asks for it; were it still sent while unfocused, it stays out.
b.update([done]);assert.deepEqual(b.references(),[]);assert.deepEqual(b.detail(),[]);
// An open question beside it stays, and so does one only sent (not settled).
const other={...open,id:'S4:o'},sent={...open,id:'S4:s',answerable:false,request_state:'submitted'};
b.update([done,other,sent]);assert.deepEqual(b.references().map(x=>x.id),['S4:o','S4:s']);
})().catch(e=>{console.error(e);process.exit(1);});""")

    def test_decision_enter_cannot_submit_before_an_explicit_selection(self):
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'submitted'};},action(){}});
b.update([{id:'decision',revision:'r',answerable:true,answer_kind:'decision',questions:[{id:'q',question:'Approve?'}]}]);
b.enter();b.input(Buffer.from('\r'));await new Promise(setImmediate);assert.equal(sent.length,0);
})();""")

    def test_question_roundtrip_preserves_choices_and_free_text_paste_for_a_complete_form(self):
        self.node(r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'received'};},action(){}});
const item={id:'form',revision:'revision',answerable:true,questions:[{id:'choice',question:'Which?',choices:['first','second']},{id:'text',question:'Why?'}]};
b.update([item]);b.enter();b.input(Buffer.from('2\t'));
b.input(Buffer.from('\x1b[200~한글 j/k\nline2\x1b[201~'));
b.input(Buffer.from('\t\t'));assert.ok(JSON.stringify(b.detail()).includes('second'));
b.input(Buffer.from('\t'));assert.ok(JSON.stringify(b.detail()).includes('한글 j/k'));
b.input(Buffer.from('\r'));await new Promise(setImmediate);
assert.deepEqual(sent,[{question_id:'form',revision:'revision',answers:{choice:['second'],text:['한글 j/k\nline2']}}]);
b.update([{...item,request_state:'applied',answerable:false}]);assert.ok(JSON.stringify(b.detail()).includes('applied'));
const rest=b.input(Buffer.from('\x1dnative\r'));assert.equal(rest.toString(),'native\r');
})();''')

    def test_whole_form_collects_all_latest_text_drafts_after_tab_or_reediting(self):
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
for(const edited of [false,true]){
 const sent=[],b=create({changed(){},answer:async x=>{sent.push(x);return {state:'received'};},action(){}});
 b.update([{id:'exact',revision:'r',answerable:true,questions:[{id:'first',question:'First?'},{id:'second',question:'Second?'}]}]);b.enter();
 if(edited){b.input(Buffer.from('first\r'));await new Promise(setImmediate);b.input(Buffer.from('t-EDIT'));}
 else b.input(Buffer.from('first'));
 b.input(Buffer.from('\tsecond\r'));await new Promise(setImmediate);
 assert.deepEqual(sent,[{question_id:'exact',revision:'r',answers:{first:[edited?'first-EDIT':'first'],second:['second']}}]);
 b.input(Buffer.from('\x1b[Z'));assert.ok(JSON.stringify(b.detail()).includes(edited?'first-EDIT':'first'));
}
const sent=[],b=create({changed(){},answer:async x=>{sent.push(x);return {state:'received'};},action(){}});
b.update([{id:'cleared',revision:'r',answerable:true,questions:[{id:'first',question:'First?'},{id:'second',question:'Second?'}]}]);b.enter();
b.input(Buffer.from('first\r'));await new Promise(setImmediate);b.input(Buffer.from('t\x7f\x7f\x7f\x7f\x7f\tsecond\r'));await new Promise(setImmediate);
assert.equal(sent.length,0,'cleared latest text must not fall back to an older saved answer');
})();""")

    def test_secret_answers_stay_masked_after_selection_submission_and_consumption(self):
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
const secret='FAKE_SECRET_B52';
for(const picked of [false,true]){
 let sent=[];const b=create({changed(){},answer:async x=>{sent.push(x);return {state:'received'};},action(){}});
 const item={id:'secret',revision:'r',answerable:true,questions:[{id:'q',question:'Synthetic secret?',secret:true,...(picked?{choices:[{label:'Stored credential',value:secret}]}:{})}]};
 b.update([item]);b.enter();b.input(Buffer.from(picked?'1':secret));assert.ok(!JSON.stringify(b.detail()).includes(secret));
 b.input(Buffer.from('\r'));await new Promise(setImmediate);b.leave();assert.ok(!JSON.stringify(b.detail()).includes(secret));
 assert.deepEqual(sent[0].answers,{q:[secret]});
 for(const state of ['received','applied','expired','cancelled']){b.update([{...item,answerable:false,request_state:state}]);assert.ok(!JSON.stringify(b.detail()).includes(secret));}
}
})();""")

    def test_single_active_action_keeps_another_questions_choice_without_claiming_consumption(self):
        self.node(r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
let release,sent=[];const b=create({changed(){},answer:x=>{sent.push(x);return new Promise(r=>release=r);},action(){}});
const item=id=>({id,revision:'r',answerable:true,questions:[{id,question:id,choices:['one','two']}]});
b.update([item('A'),item('B')]);b.enter();b.input(Buffer.from('1\r\t2\r'));
assert.equal(sent.length,1);assert.ok(JSON.stringify(b.detail()).includes('pending'));
release({state:'received'});await new Promise(setImmediate);
b.input(Buffer.from('\r'));assert.equal(sent.length,2);assert.deepEqual(sent[1].answers,{B:['two']});
release({state:'received'});await new Promise(setImmediate);
b.input(Buffer.from('\t\t'));assert.ok(JSON.stringify(b.detail()).includes('received'));
assert.ok(!JSON.stringify(b.detail()).includes('consumed'));
})();''')

    def test_long_question_scrolls_and_late_refresh_keeps_the_exact_draft(self):
        self.node(r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
const a={id:'a',revision:'r',answerable:true,questions:[{id:'q',question:'First?',display:{head:[{text:'Head'}],body:Array.from({length:12},(_,i)=>[{text:'row-'+i}]),options:[[{text:'choice'}]]}}]};
b.update([a]);b.enter();b.input(Buffer.from('tdraft'));
let first=JSON.stringify(b.detail({budget:5,columns:80}));assert.ok(first.includes('row-0')&&first.includes('draft'));
b.input(Buffer.from('\x1b[B\x1b[B'));assert.ok(!JSON.stringify(b.detail({budget:5})).includes('row-0'));
b.update([a,{id:'late',revision:'r',answerable:true,questions:[{id:'q',question:'Late?'}]}]);assert.ok(JSON.stringify(b.detail({budget:5})).includes('draft'));
b.update([{...a,revision:'changed'}]);assert.ok(JSON.stringify(b.detail({budget:5})).includes('changed'));
''')

    def test_narrow_focused_guidance_keeps_the_full_return_key_visible(self):
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
b.update([{id:'exact',revision:'r',answerable:true,questions:[{id:'q',question:'Choose?',choices:['one','two']}]}]);b.enter();
for(const selected of [false,true]){
 if(selected)b.input(Buffer.from('1'));
 const rows=b.detail({budget:20,columns:40}).map(row=>row.map(part=>part.text).join(''));
 const hints=rows.filter(row=>row.includes('Esc/')||row.includes('choose')||row.includes('Enter send'));
 assert.ok(hints.every(row=>[...row].reduce((n,c)=>n+(c.charCodeAt(0)<128?1:2),0)<=36),'focus guidance must fit the panel');
 assert.ok(hints.join(' ').includes('Esc/Ctrl+] back'));
 assert.ok(hints.join(' ').includes('↑↓ choose · Enter send'));
}
""")

    def test_display_prepares_readable_choices_without_a_checkmark_for_recommendation(self):
        item={'answerable':True,'age':'2m','questions':[{'id':'q','question':'한글 질문 '*25,'choices':['one','two'],'recommended':1}]}
        question_presentations([item],80)
        view=item['questions'][0]['display']
        self.assertGreater(len(view['body']),1)
        options=''.join(t for row in view['options'] for t,_ in row)
        self.assertIn('2 two',options);self.assertNotIn('✓',options)
        self.assertEqual(capacity(24),10)
        self.assertGreaterEqual(24-capacity(24),6)

    def test_board_shares_exact_session_filter_and_retained_receipts_never_offer_input(self):
        import questions
        owned={'id':'S4:'+'a'*64,'owner':{'harness':'codex','session_id':'exact'}}
        foreign={'id':'S4:'+'b'*64,'owner':{'harness':'codex','session_id':'other'}}
        stale={'id':'S4:'+'c'*64,'owner':owned['owner'],'stale':True}
        view={'by_session':{('codex','exact'):[x['id'] for x in (owned,foreign,stale)]},'items':[owned,foreign,stale]}
        def describe(item,**kw):return {**item,'revision':'r','answerable':True,'request_state':'applied'}
        with mock.patch.object(questions,'describe',side_effect=describe):
            rows=session_shell.session_questions('codex','exact',view=view)
            self.assertEqual([x['id'] for x in rows],[owned['id']])
            rows=session_shell.session_questions('codex','exact',view={'items':[]},retained=[{'id':owned['id'],'revision':'r'}])
            self.assertEqual(rows[0]['request_state'],'applied');self.assertFalse(rows[0]['answerable'])

    def test_optional_panel_metadata_reuses_canonical_board_and_restricts_a_busy_slot(self):
        import session_tidy,human_wait_state
        from fleet import herdr_projection
        item={'id':'owned','revision':'r','answerable':True,'questions':[{'id':'q','question':'Owned?'}]}
        with mock.patch.object(session_shell,'registry_root'),mock.patch.object(session_shell,'session_questions',return_value=[item]), \
             mock.patch.object(human_wait_state,'cached_statusline',return_value=([0,0],'')),mock.patch.object(human_wait_state,'cached_view',return_value={}), \
             mock.patch.object(session_tidy,'resolve_seat',return_value='owned-seat'),mock.patch.object(session_tidy,'prompt_display',return_value={}), \
             mock.patch.object(session_tidy,'session_seat',return_value='owned-seat'),mock.patch.object(session_tidy,'_prompt_seq_path',return_value=Path('/private-fixture/prompt')), \
             mock.patch('fleet.session_work.observe',return_value=([],[])),mock.patch.object(herdr_projection,'project_panel',create=True) as publish:
            result=session_shell.panel_projection('claude','exact',pane_binding={'sid':'exact'},answer_busy=True)
            self.assertTrue(result['questions'][0]['answerable'])
            self.assertFalse(publish.call_args.args[2][0]['answerable'])
            self.assertEqual(publish.call_args.args[0],{'sid':'exact'})
            publish.side_effect=OSError('optional unavailable')
            self.assertIn('lines',session_shell.panel_projection('claude','exact',pane_binding={'sid':'exact'}))


    def test_the_route_page_reads_its_owner_output_with_fleets_reader(self):
        # Fleet's `attempts`: the same read_owner_raw, once per opened page, for the
        # routes the page shows, each through its current owner; bounded so the read
        # reply stays inside its frame. A row without an owner is not guessed.
        from fleet.explorer import focus
        import human_wait_state
        shown=[{'id':'rt-0123456789abcdef','owner_attempt':'att-new'},{'id':'rt-fedcba9876543210','owner_attempt':'att-b'},
               {'id':'not-a-route','owner_attempt':'att-c'},{'id':'rt-1111111111111111'}]
        view={'route_panels':{'claude\0exact':shown,'claude\0other':[{'id':'rt-2222222222222222','owner_attempt':'att-x'}]}}
        calls=[]
        def raw(route,attempt,rows,live,reader):
            calls.append((route,attempt,rows,live))
            return {'att':attempt,'route':route,'lines':['x'*500]+['line %d'%n for n in range(60)]} if route.endswith('cdef') else None
        with mock.patch.object(human_wait_state,'cached_view',return_value=view), \
             mock.patch.object(focus,'read_attempts',return_value=['ROWS']) as attempts, \
             mock.patch.object(focus,'read_owner_raw',side_effect=raw):
            result=session_shell._block_action({'sid':'exact','harness':'claude'},{'action':'raw'})
        attempts.assert_called_once_with(None,'exact')
        self.assertEqual([c[:2] for c in calls],[('rt-0123456789abcdef','att-new'),('rt-fedcba9876543210','att-b')])
        self.assertTrue(all(c[2]==['ROWS'] and c[3]=={} for c in calls))
        self.assertEqual(result['ok'],True);self.assertEqual(len(result['attempts']),1)
        row=result['attempts'][0];self.assertEqual((row['route'],row['att']),('rt-0123456789abcdef','att-new'))
        self.assertEqual(row['lines'][-1],'line 59');self.assertEqual(len(row['lines']),30)
        self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),65536)
        with mock.patch.object(human_wait_state,'cached_view',return_value=None), \
             mock.patch.object(focus,'read_attempts') as attempts:
            self.assertEqual(session_shell._block_action({'sid':'exact','harness':'claude'},{'action':'raw'}),{'ok':True,'attempts':[]})
        attempts.assert_not_called()

    def test_the_route_page_reads_the_current_owner_through_the_real_registry(self):
        # Review [52]: an earlier owner of the same route, and finished routes recorded
        # after the current one, must not replace the current route's owner output.
        import tempfile
        from fleet.explorer import focus, output
        import human_wait_state
        with tempfile.TemporaryDirectory() as tmp:
            jobs=Path(tmp)/'jobs.log';logs=Path(tmp)/'logs';logs.mkdir()
            rid='rt-0123456789abcdef';sid='sid-rv1537-current'
            def make(aid,who,route,body,status='open',owner=True):
                log=logs/('fixture.'+aid+'.claude.jsonl')
                log.write_text(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':body}]}})+'\n')
                meta={'attempt_schema_version':'2','dispatch_depth':'1','transport':'headless','execution_surface':'registered-headless',
                      'registered_worker':'1','attempt_id':aid,'parent_sid':who,'worker_type':'owner' if owner else 'stage','harness':'claude',
                      'route_id':route,'owner_route_id':route,'log_file':str(log),'launch_outcome':'never-launched','note':'fixture'}
                return '2026-11-12T00:00:00Z\t'+status+'\t/synthetic\t/synthetic\tfixture\t'+','.join(k+'='+v for k,v in meta.items())+'\n'
            old=make('att-rv1537-old',sid,rid,'OLD_OWNER_OUTPUT','done');new=make('att-rv1537-new',sid,rid,'CURRENT_OWNER_OUTPUT')
            alien=make('att-rv1537-alien','sid-other','rt-fedcba9876543210','OTHER_SESSION_OUTPUT')
            stage=make('att-rv1537-stage',sid,rid,'STAGE_OUTPUT',owner=False)
            history=''.join(make('att-rv1537-history-'+str(i),sid,'rt-'+format(i+16,'016x'),'HISTORY_'+str(i),'done') for i in range(4))
            view={'route_panels':{'claude\0'+sid:[{'id':rid,'owner_attempt':'att-rv1537-new'}]}}
            def read(text):
                jobs.write_text(text);output.attempt_view()._REGISTRY_READERS.clear()
                with mock.patch.dict(os.environ,{'AGENT_DISPATCH_JOBS':str(jobs)}), \
                     mock.patch.object(human_wait_state,'cached_view',return_value=view):
                    return session_shell._block_action({'sid':sid,'harness':'claude'},{'action':'raw'})['attempts']
            for label,text in (('single',new+alien+stage),('old-before-new',old+new+alien+stage),('four-later-routes',new+history)):
                with self.subTest(label):
                    got=read(text)
                    self.assertEqual([(a['route'],a['att']) for a in got],[(rid,'att-rv1537-new')])
                    body=' '.join(got[0]['lines'])
                    self.assertIn('CURRENT_OWNER_OUTPUT',body)
                    for other in ('OLD_OWNER','OTHER_SESSION','STAGE_OUTPUT','HISTORY_'):
                        self.assertNotIn(other,body)
            # An owner of another session is not read even when a row names it.
            view={'route_panels':{'claude\0'+sid:[{'id':'rt-fedcba9876543210','owner_attempt':'att-rv1537-alien'}]}}
            self.assertEqual(read(new+alien),[])

if __name__=='__main__':unittest.main()
