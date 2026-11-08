"""Explicit block responses keep questions answer's exact-owner and receipt boundary."""
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


if __name__=='__main__':unittest.main()
