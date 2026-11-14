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

    def test_focus_opens_on_the_first_answerable_question_and_names_the_page_keys_before_it(self):
        # 11-14: Ctrl+] opens on a question the person can answer now, even when the one shown
        # is a receipt; before focus a long question with choices names PgUp/PgDn, the keys
        # that read it once focused (↑↓ choose there).
        code=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
const block=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
const receipt={id:'sent',revision:'r0',answerable:false,request_state:'submitted',questions:[{id:'q',question:'SENT_BEFORE',choices:['A']}]};
const open={id:'open',revision:'r1',answerable:true,questions:[{id:'q',question:'ANSWER_ME',choices:['A','B']}]};
block.update([receipt,open]);
let text=JSON.stringify(block.detail({budget:8,columns:40}));
assert(text.includes('SENT_BEFORE'),text);
block.enter();
text=JSON.stringify(block.detail({budget:8,columns:40}));
assert(text.includes('ANSWER_ME')&&text.includes('▸'),text);
block.leave();
const many=Array.from({length:9},(_,i)=>'CHOICE_'+i);
block.update([{id:'c',revision:'r2',answerable:true,questions:[{id:'q',question:'Which?',choices:many}]}]);
text=JSON.stringify(block.detail({budget:6,columns:40}));
assert(text.includes('more · focus and PgUp/PgDn to read')&&!text.includes('focus and ↑↓'),text);
console.log('PASS');"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)

    def test_the_panel_board_holds_only_what_the_person_can_answer_here(self):
        # 11-14 (the supervisor's panel): eight blocked owners, an unanswerable question and one
        # the person can answer, published the normal way. Blocked owners stay out of the
        # session's index and every count; the block carries the one answerable question,
        # first; the other is one guidance line, never counted.
        import tempfile
        sid='panel-session'
        owner=dict(harness='claude',session_id=sid)
        owners=[dict(id='S3:att-%d'%n,source='S3',kind='decision',since=100+n,stale=False,owner=owner)
                for n in range(8)]
        old=dict(id='S4:old',source='S4',kind='message',since=200,stale=False,owner=owner)
        new=dict(id='S4:new',source='S4',kind='message',since=300,stale=False,owner=owner)
        def describe(item,**kw):
            if item['id'].startswith('S3:'):
                return dict(id=item['id'],answerable=False,reason='owner-correction-native-parent-context-required',
                            questions=[dict(id='q',question='owner waits',choices=[])])
            if item['id']=='S4:old':
                return dict(id='S4:old',answerable=False,request_state='unknown',
                            reason='message-question-no-longer-current',questions=[dict(id='message-response',question='OLD',choices=[])])
            return dict(id='S4:new',answerable=True,revision='r',questions=[dict(id='message-response',question='NEW',choices=['A'])])
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ,{'XDG_STATE_HOME':tmp}), \
             mock.patch.object(hws,'_request_producer'), \
             mock.patch.object(hws,'_message_source_state',return_value='open'), \
             mock.patch.object(questions,'describe',side_effect=describe):
            import time
            self.assertTrue(hws.publish_cache({'items':[*owners,old,new],'observed_at':time.time(),'complete':True}))
            view=hws.cached_view(request=False)
            self.assertEqual(view['by_session'],{('claude',sid):['S4:old','S4:new']})
            self.assertEqual(view['total'],2)
            elsewhere=[]
            board=shell.session_questions('claude',sid,view=view,elsewhere=elsewhere)
        self.assertEqual([row['id'] for row in board],['S4:new'])
        self.assertEqual([row['id'] for row in elsewhere],['S4:old'])

    def test_the_count_holds_every_wait_a_person_answers_and_ctrl_names_only_the_panel(self):
        # 11-14 (the supervisor's correction): the panel counts every wait of this session a
        # person must answer, wherever it is answered; the block holds what the panel answers and
        # the rest is one line saying where. `Ctrl+]` stands on the notice line only when the
        # panel answers one. Blocked owners and finished waits count nowhere.
        import tempfile, time
        import session_tidy
        from fleet.session_line import answer_wait_word
        from fleet.config import ui_text
        from fleet import herdr_projection
        native_reason={'claude':'claude-native-response-surface-unavailable',
                       'codex':'codex-native-request-unsupported','opencode':'opencode-native-request-unsupported'}
        def describe(item,**kw):
            ident=item['id']
            row=dict(id=ident,source=item.get('source'),owner=item.get('owner'),state='current',
                     questions=[dict(id='q',question=ident,choices=[])])
            if ident.startswith('S3:'):
                return dict(row,answerable=False,reason='owner-correction-native-parent-context-required')
            if ident.startswith('S1:'):
                return dict(row,answerable=False,reason=native_reason[item['owner']['harness']])
            if ident=='S4:marker':
                return dict(row,answerable=False,reason='marker-has-no-bound-native-response-surface')
            if ident=='S4:closed':
                return dict(row,answerable=False,request_state='unknown',reason='message-question-no-longer-current')
            return dict(row,answerable=True,revision='r',questions=[dict(id='message-response',question='NEW',choices=['A'])])
        def projection(harness,sid,items):
            with tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.dict(os.environ,{'XDG_STATE_HOME':tmp}), \
                 mock.patch.object(hws,'_request_producer'), \
                 mock.patch.object(hws,'_message_source_state',return_value='open'), \
                 mock.patch.object(questions,'describe',side_effect=describe), \
                 mock.patch.object(session_tidy,'prompt_display',return_value={'human_state':'none'}), \
                 mock.patch.object(herdr_projection,'project_panel') as pane:
                self.assertTrue(hws.publish_cache({'items':items,'observed_at':time.time(),'complete':True}))
                total=hws.cached_view(request=False)['total']
                result=shell.panel_projection(harness,sid,columns=40,pane_binding=dict(harness=harness,sid=sid))
                # The pane's own `Q<n>/◑A<n>` reads the same count: every wait, the panel's answerable ones.
                _binding,values,shown=pane.call_args.args
                return total,result,herdr_projection._panel_counts(harness,sid,values,shown)
        text=lambda parts:''.join(part['text'] for part in parts or ())
        wait=lambda n:answer_wait_word()+' '+str(n)
        below=ui_text('fleet.session_block.compact_answer_below')
        guide=lambda n:ui_text('fleet.session_block.questions')+' '+str(n)+' · '+below
        for harness in ('claude','codex','opencode'):
            sid='sid-'+harness
            owner=dict(harness=harness,session_id=sid)
            item=lambda ident,source,kind:dict(id=ident,source=source,kind=kind,since=100,stale=False,owner=owner)
            owners=[item('S3:att-%d'%n,'S3','decision') for n in range(8)]
            question=item('S4:new','S4','message')
            native=item('S1:%s:%s:1'%(harness,sid),'S1','approval')
            cases=(
                # (waits, shared total, count, block ids, guidance line, notice line)
                ('eight blocked owners and one question',[*owners,question],1,1,['S4:new'],'',wait(1)+' │ Ctrl+]'),
                ('a native request and one question',[native,question],2,2,['S4:new'],guide(1),wait(2)+' │ Ctrl+]'),
                ('a marker question alone',[item('S4:marker','S4','marker')],1,1,[],guide(1),wait(1)+' │ '+below),
                # A late publication may still list a question its session's next one closed:
                # finished, it waits for no one here (the next publication drops it).
                ('a blocked owner and a closed question',[owners[0],item('S4:closed','S4','message')],1,0,[],'',''),
            )
            for name,items,shared,count,block,line,notice in cases:
                with self.subTest(harness=harness,case=name):
                    total,result,pane=projection(harness,sid,items)
                    panel=result['panel']
                    self.assertEqual(total,shared)
                    self.assertEqual(pane,(count,len(block)))
                    self.assertEqual(panel['wait'],wait(count) if count else '')
                    self.assertEqual([row['id'] for row in result['questions']],block)
                    self.assertEqual(panel['wait_guide'],line)
                    if harness=='claude':
                        self.assertEqual(text(panel['notice']),notice)
                    else:
                        self.assertNotIn('notice',panel)

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
assert(has(draw(),'▸ 1 A'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));
let rows=draw();assert(has(rows,'BODY_LINE_0'),rows.join('\n'));
assert(has(draw(),'BODY_LINE_0'),'the page stays on a later paint');
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[6~'));
rows=draw();assert(has(rows,'▸ 1 A')&&has(rows,'2 B'),rows.join('\n'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));
// ↓ moves the choice and brings it back onto the page; a number does the same.
c.input(Buffer.from('\x1b[B'));rows=draw();assert(has(rows,'▸ 2 B'),rows.join('\n'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));c.input(Buffer.from('1'));rows=draw();assert(has(rows,'▸ 1 A'),rows.join('\n'));
for(let i=0;i<5;i++)c.input(Buffer.from('\x1b[5~'));c.input(Buffer.from('\r'));
setImmediate(()=>{assert.deepEqual(sent,[{question_id:'q',revision:'r1',answers:{a:['A']}}]);console.log('PASS');});"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs')],
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PASS',result.stdout)

    def test_split_page_keys_page_the_route_box_like_whole_ones(self):
        # PR #1549 P2: the panel-mouse decoder lets a held `ESC [` go after 8ms, so
        # PgUp/PgDn can reach the block parser in two reads. With no question it pages
        # the route box all the same; with one the question's body reads on.
        code=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]),decoder=require(process.argv[2]);
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 let pages=[],native=[];
 const block=create({changed(){},answer:async()=>({state:'submitted'}),action(){},page:dir=>pages.push(dir)});
 const mouse=decoder({suppressed:()=>false,mouse(){},bytes:data=>{const rest=block.focused?block.input(data):data;if(rest.length)native.push(rest.toString('latin1'));}});
 block.update([]);block.enter();
 mouse.feed(Buffer.from('\x1b[6~'));assert.deepEqual(pages,[1]);
 for(const gap of [0,12,40]){
  pages=[];mouse.feed(Buffer.from('\x1b['));await wait(gap);mouse.feed(Buffer.from('6~'));await wait(20);
  assert.deepEqual(pages,[1],'split PgDn after '+gap+'ms');
  pages=[];mouse.feed(Buffer.from('\x1b'));await wait(gap?gap/2:0);mouse.feed(Buffer.from('[5~'));await wait(20);
  assert.deepEqual(pages,[-1],'split PgUp after '+gap+'ms');
 }
 assert.deepEqual(native,[]);assert.ok(block.focused);
 // A question owns the same split keys: its body pages, the route box stays.
 pages=[];
 const body=Array.from({length:12},(_,i)=>[{text:'BODY_LINE_'+i}]);
 block.update([{id:'q',revision:'r1',answerable:true,request_state:'open',questions:[{id:'a',question:'LONG',choices:['A','B'],
  display:{head:[{text:'Q_HEAD'}],body,options:[[{text:'1 '},{text:'A'}],[{text:'2 '},{text:'B'}]]}}]}]);
 const draw=()=>block.detail({budget:7,columns:40}).map(r=>r.map(x=>x.text).join('')).join('\n');
 const first=draw();
 mouse.feed(Buffer.from('\x1b['));await wait(40);mouse.feed(Buffer.from('6~'));await wait(20);
 assert.deepEqual(pages,[]);assert.notEqual(draw(),first);
 // Unfocused, the whole key goes to the native unchanged.
 block.leave();mouse.feed(Buffer.from('\x1b[6~'));assert.deepEqual(native,['\x1b[6~']);assert.deepEqual(pages,[]);
 console.log('PASS');
})().catch(error=>{console.error(error);process.exit(1);});"""
        result=subprocess.run(['node','-e',code,str(ROOT/'utilities/session-shell/block-input.cjs'),
                               str(ROOT/'utilities/session-shell/panel-mouse.cjs')],text=True,capture_output=True,timeout=10)
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
