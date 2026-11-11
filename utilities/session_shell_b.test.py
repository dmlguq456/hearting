"""B uses canonical display rows and keeps local edits separate from native effects."""
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
const lit=b=>b.detail({budget:20,columns:60}).filter(row=>row[0]?.text==='›').map(row=>row.slice(2).map(p=>p.text).join(''));
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


if __name__=='__main__':unittest.main()
