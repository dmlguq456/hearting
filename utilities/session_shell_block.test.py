"""Explicit block responses keep questions answer's exact-owner and receipt boundary."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
import session_shell as shell
sys.path.insert(0,str(ROOT/'tools'))
import human_wait_state as hws
import questions


class BlockAnswersTest(unittest.TestCase):
    def test_a_changed_owner_cannot_receive_the_same_visible_question_answer(self):
        binding=dict(harness='codex',sid='original-session')
        before=dict(id='exact-question',owner=dict(harness='codex',session_id='original-session'))
        after=dict(id='exact-question',owner=dict(harness='claude',session_id='another-session'))
        with mock.patch.object(hws,'cached_view',return_value={'items':[before]}), \
             mock.patch.object(shell,'answer_attachment_current',return_value=True), \
             mock.patch.object(questions,'load_board',return_value={'items':[after]}), \
             mock.patch.object(hws,'submit_selected_response') as submit:
            result=shell._block_answer(binding,{'question_id':'exact-question','revision':'0'*64,'answers':{'q':['yes']}})
        self.assertFalse(result['ok']);self.assertIn('owner changed',result['reason']);submit.assert_not_called()

    def test_choice_and_text_use_the_existing_cli_implementation_and_return_its_receipt(self):
        binding=dict(harness='codex',sid='original-session')
        item=dict(id='exact-question',owner=dict(harness='codex',session_id='original-session'))
        envelope=dict(state='open',support={'supported':True},kind='question',question_id='q',
                      detail={'question':'Question','choices':['Yes','No']})
        def consume(ref,response,**kwargs):
            self.assertEqual(response,{'answers':{'q':['typed answer']}})
            return dict(state='submitted',action_id=kwargs['action_id'])
        with mock.patch.object(hws,'cached_view',return_value={'items':[item]}), \
             mock.patch.object(shell,'answer_attachment_current',return_value=True), \
             mock.patch.object(questions,'load_board',return_value={'items':[item]}), \
             mock.patch.object(hws,'read_selected_request',return_value=envelope), \
             mock.patch.object(hws,'submit_selected_response',side_effect=consume) as submit:
            result=shell._block_answer(binding,{'question_id':'exact-question','revision':questions.request_revision(envelope),'answers':{'q':['typed answer']}})
        self.assertTrue(result['ok']);self.assertEqual(result['state'],'submitted');submit.assert_called_once()

    def test_focus_editor_preserves_exact_ids_and_keeps_normal_keys_with_the_native_caller(self):
        code=r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]);
let sent=[],actions=[];
const block=create({changed(){},answer:async row=>{sent.push(row);return {state:'submitted'};},action:key=>actions.push(key)});
assert.equal(block.focused,false);
block.update([{id:'original',revision:'old-revision',answerable:true,questions:[{id:'q',question:'Include?',choices:['Yes','No']}]}]);
block.enter();block.input(Buffer.from('2\r'));
setImmediate(()=>{
 assert.deepEqual(sent,[{question_id:'original',revision:'old-revision',answers:{q:['No']}}]);
 block.update([{id:'replacement',answerable:true,questions:[{id:'new-q',question:'New?',choices:[]}]}]);
 block.input(Buffer.from('t한글\r'));
 setImmediate(()=>{
  assert.equal(sent[1].question_id,'replacement');assert.deepEqual(sent[1].answers,{'new-q':['한글']});
  block.input(Buffer.from('\x1d'));assert.equal(block.focused,false);
  console.log('PASS');
 });
});'''
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)


    def test_a_long_question_names_the_keys_that_read_its_body(self):
        # While ↑↓ choose, the body pages with PgUp/PgDn; the hint says so. Keys are unchanged.
        code=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const block=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
const many=Array.from({length:9},(_,i)=>'CHOICE_'+i);
block.update([{id:'c',answerable:true,questions:[{id:'q',question:'Which?',choices:many}]}]);
block.enter();
let text=JSON.stringify(block.detail({budget:6,columns:30}));
assert(text.includes('more · PgUp/PgDn to read')&&!text.includes('focus and ↑↓'),text);
// A question without choices still reads with ↑↓.
block.update([{id:'t',answerable:true,questions:[{id:'q',question:'Q',choices:[]}]}]);
text=JSON.stringify(block.detail({budget:3,columns:30}));
assert(text.includes(' +1 ↑↓')&&!text.includes('PgUp'),text);
console.log('PASS');"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)

    def test_page_keys_read_a_long_question_from_its_first_row_and_keep_the_choice(self):
        # PR #1532 R2: PgUp/PgDn read the body to its first and last rows; the page stays
        # where reading left it, the highlighted choice stays, and ↑↓/numbers follow the choice.
        code=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
let sent=[];
const c=create({changed(){},answer:async v=>{sent.push(v);return {state:'submitted'};},action(){}});
const body=Array.from({length:12},(_,i)=>[{text:'BODY_LINE_'+i}]);
c.update([{id:'q',revision:'r1',answerable:true,request_state:'open',questions:[{id:'a',question:'LONG',choices:['A','B'],
 display:{head:[{text:'Q_HEAD'}],body,options:[[{text:'1 '},{text:'A'}],[{text:'2 '},{text:'B'}]]}}]}]);c.enter();
const draw=()=>c.detail({budget:7,columns:40}).map(r=>r.map(x=>x.text).join(''));
const has=(rows,s)=>rows.some(x=>x.includes(s));
assert(has(draw(),'› A'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));
let rows=draw();assert(has(rows,'BODY_LINE_0'),rows.join('\n'));
assert(has(draw(),'BODY_LINE_0'),'the page stays on a later paint');
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[6~'));
rows=draw();assert(has(rows,'› A')&&has(rows,'2 B'),rows.join('\n'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));
// ↓ moves the choice and brings it back onto the page; a number does the same.
c.input(Buffer.from('\x1b[B'));rows=draw();assert(has(rows,'› B'),rows.join('\n'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));c.input(Buffer.from('1'));rows=draw();assert(has(rows,'› A'),rows.join('\n'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));c.input(Buffer.from('\r'));
setImmediate(()=>{assert.deepEqual(sent,[{question_id:'q',revision:'r1',answers:{a:['A']}}]);console.log('PASS');});"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)

    def test_split_controls_drafts_and_replaced_questions_never_retarget_native_input(self):
        code=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
(async()=>{
 let sent=[],left,exit;
 const block=create({changed(){if(exit&&!block.focused)exit();},answer:async value=>{sent.push(value);return {ok:true,state:'submitted'};},action(){}});
 const a={id:'a',revision:'one',answerable:true,questions:[{id:'q',question:'First?',choices:[]}]};
 block.update([a]);block.enter();block.input(Buffer.from('tdraft'));
 block.update([a,{id:'b',revision:'one',answerable:true,questions:[{id:'q2',question:'Other?',choices:[]}]}]);
 assert.ok(JSON.stringify(block.detail()).includes('draft'));
 block.input(Buffer.from('\r'));await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(sent[0],{question_id:'a',revision:'one',answers:{q:['draft']}});
 // A new open request, rather than the submitted request, owns this draft.
 const fresh={...a,id:'fresh'};block.update([fresh]);
 block.input(Buffer.from('tpreserved'));
 block.update([{...fresh,revision:'two'}]);block.input(Buffer.from('\r'));
 await new Promise(resolve=>setImmediate(resolve));assert.equal(sent.length,1);
 assert.ok(JSON.stringify(block.detail()).includes('preserved'));
 left=block.input(Buffer.from('\x1dnative\r'));assert.deepEqual(left,Buffer.from('native\r'));
 block.enter();assert.equal(block.input(Buffer.from('\x1b')).length,0);
 assert.equal(block.input(Buffer.from('[A')).length,0);assert.equal(block.focused,true);
 block.input(Buffer.from('\x1b[200~paste\x1dtext\x1b[201~'));assert.equal(block.focused,true);
 const exited=new Promise(resolve=>exit=resolve);block.input(Buffer.from('\x1b'));await exited;
 assert.equal(block.focused,false);console.log('PASS');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)


    def test_an_arrow_choice_is_kept_like_a_number_choice_when_its_question_changes(self):
        # Review RV-PR1528 §1: ↓ then a new ID or revision must not send the new question's first choice.
        code=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const item=(id,revision,decision=false)=>({id,revision,request_state:'open',answerable:true,answer_kind:decision?'decision':'message',
 questions:[{id:'q',question:id+' question',choices:['FIRST','SECOND'],recommended:0}]});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 for(const harness of ['claude','codex','opencode'])for(const mode of ['replace','revision','number','text','stable','insert']){
  const sent=[],block=create({changed(){},answer:async v=>{sent.push(v);return {state:'received'};},action(){}});
  const owner={harness,session_id:'s'},a={...item('old','r1'),owner},b={...item(mode==='revision'?'old':'new',mode==='revision'?'r2':'r1'),owner};
  block.update([a]);block.enter();block.input(Buffer.from(mode==='number'?'2':mode==='text'?'tDRAFT':'\x1b[B'));
  block.update(mode==='stable'?[a]:mode==='insert'?[b,a]:[b]);
  block.input(Buffer.from('\r'));await tick();block.input(Buffer.from('\r'));await tick();
  const label=harness+' '+mode+' '+JSON.stringify(sent);
  if(['stable','insert'].includes(mode))assert.deepEqual(sent,[{question_id:'old',revision:'r1',answers:{q:['SECOND']}}],label);
  else{assert.equal(sent.length,0,label);assert.ok(JSON.stringify(block.detail()).includes('question changed'),label);}
 }
 // A decision still needs its explicit choice; one Enter sends it once.
 const sent=[],block=create({changed(){},answer:async v=>{sent.push(v);return {state:'received'};},action(){}});
 block.update([item('d','r1',true)]);block.enter();block.input(Buffer.from('\r'));await tick();assert.equal(sent.length,0);
 block.input(Buffer.from('\x1bOB'));block.input(Buffer.from('\r'));await tick();block.input(Buffer.from('\r'));await tick();
 assert.deepEqual(sent,[{question_id:'d',revision:'r1',decision:'approve'}]);
 // Unfocused arrows and Enter return to the native caller untouched.
 const idle=create({changed(){},answer:async()=>{throw Error('unfocused');},action(){}});idle.update([item('old','r1')]);
 assert.deepEqual(idle.input(Buffer.from('\x1b[B\r')),Buffer.from('\x1b[B\r'));
 console.log('PASS');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)


if __name__=='__main__':unittest.main()
