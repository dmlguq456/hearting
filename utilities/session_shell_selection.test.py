"""A resumed root needs native viewport selection, not retained membership alone."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]
import session_shell as shell
import pane_ownership
from fleet.collectors import codex

# Resolved once: a fixture may narrow PATH while a test runs.
NODE = shutil.which('node') or 'node'


class ResumedSelectionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'selection_answer_fixture', ROOT / 'utilities/session_shell_answer_current.test.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.fx = module.CurrentAnswerTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.old = self.fx.sid
        self.resumed = '00000000-0000-4000-8000-000000000099'
        self.fx.selected = None  # Both loaded roots have this TUI's native origin.
        self.frame_sid = self.resumed
        self.revision = 9
        self.member = (self.fx.native.pid, self.fx.binding['native_start'])
        self.process_row = (self.member[0], 1, self.member[0], 100, self.member[0], self.member[1])
        self.patches = [
            mock.patch.object(shell, 'control', side_effect=self.observe),
            mock.patch.object(codex, 'process_for_thread', side_effect=self.membership),
            mock.patch.object(pane_ownership, '_runtime', return_value=('codex', False)),
            mock.patch.object(pane_ownership, '_registered_worker', return_value=False),
            mock.patch.object(pane_ownership, '_process', return_value=self.process_row),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def observe(self, binding, operation, **_fields):
        self.assertEqual(operation, 'native-selection')
        return dict(ok=True, native_frame=dict(revision=self.revision, epoch=12,
            lines=['› Ask Codex', self.frame_sid + ' · Context 10% used',
                   '← for agents · ? for shortcuts']))

    def membership(self, sid, **kwargs):
        self.assertTrue(kwargs.get('fresh'))
        return self.member if sid in {self.old, self.resumed} else None

    def select_resumed(self):
        self.fx.sid = self.resumed
        self.fx.binding = {**self.fx.binding, 'sid': self.resumed}
        self.assertTrue(shell.publish_binding(self.fx.binding))

    def test_backend_selects_the_current_native_root_without_a_start_hook(self):
        with mock.patch.object(shell.os, 'getppid', return_value=self.fx.binding['host_pid']):
            result = shell._backend_operation(dict(op='selection', binding=self.fx.binding))
        self.assertEqual(result.get('selected_sid'), self.resumed)
        # Observation never rewrites the old attachment or answers a question.
        self.assertEqual(shell.read_binding(shell.binding_path('codex', self.fx.binding['host_pid'])),
                         self.fx.binding)

    def test_resumed_answer_uses_the_existing_queue_and_rechecks_selection_at_effect(self):
        self.select_resumed()
        self.fx.ask('Question in resumed root')
        self.assertTrue(self.fx.answer()['ok'])
        _text, ref, _recipient = self.fx.fx.prepare(self.fx.signal)
        self.frame_sid = self.old
        result = self.fx.deliver(ref)
        self.assertNotIn('thread/queue/add', self.fx.rpcs)
        self.assertIn('current-session-unverified', result.get('reason', ''))
        self.frame_sid = self.resumed
        self.fx.rpcs.clear()
        self.fx.deliver(ref)
        self.assertEqual(self.fx.rpcs.count('thread/queue/add'), 1)

    def test_old_answer_cannot_borrow_the_resumed_root_or_its_display(self):
        self.fx.ask()
        self.assertFalse(self.fx.answer()['ok'])
        self.assertIsNone(self.fx.fx.record(self.fx.signal))

    def test_a_conflicting_status_row_through_the_host_never_answers_the_old_root(self):
        # Both roots in this TUI, no process-native current, the screen shows the
        # resumed root after a UUID-shaped field: the old root's question stays shut.
        screen = ['› Ask Codex', self.old + ' · Context 10% used · ' + self.resumed]
        frame = FooterReaderParityTests.host_frames(self, [screen])[0]
        with mock.patch.object(shell, 'control', return_value=dict(ok=True, native_frame=frame)):
            self.assertIsNone(shell._codex_selected_root(self.fx.binding))
            self.fx.ask('Question in previously retained root')
            self.assertFalse(self.fx.answer()['ok'])
        self.assertEqual(self.fx.rpcs.count('thread/queue/add'), 0)
        self.assertIn(self.resumed, frame['lines'][1])

    def test_selection_needs_stable_frames_and_the_same_native_lifetime(self):
        self.select_resumed()
        self.assertTrue(shell.answer_attachment_current(self.fx.binding))
        for member in (None, (self.fx.native.pid + 1, self.member[1]),
                       (self.fx.native.pid, '0')):
            with self.subTest(member=member), mock.patch.object(codex, 'process_for_thread', return_value=member):
                self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        first = self.observe(self.fx.binding, 'native-selection')
        second = dict(ok=True, native_frame={**first['native_frame'], 'revision': 10})
        with mock.patch.object(shell, 'control', side_effect=[first, second]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        second = dict(ok=True, native_frame={**first['native_frame'], 'epoch': 13})
        with mock.patch.object(shell, 'control', side_effect=[first, second]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        moved = dict(ok=True, native_frame={**first['native_frame'], 'revision': 10,
                                            'lines': ['›', self.old + ' · Context 10% used']})
        with mock.patch.object(shell, 'control', side_effect=[first, moved]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))

    def test_a_tui_without_a_daemon_is_corroborated_by_its_own_native_proof(self):
        # --no-daemon has no daemon membership; the exact native proof of the same
        # thread stands in for it. Membership of another process still refuses.
        self.select_resumed()
        for native, member, expected in ((self.resumed, None, True), (None, None, False),
                                         (self.old, None, False),
                                         (self.resumed, (self.fx.native.pid + 1, self.member[1]), False)):
            self.fx.selected = native
            with self.subTest(native=native, member=member), \
                    mock.patch.object(codex, 'process_for_thread', return_value=member):
                self.assertEqual(shell.answer_attachment_current(self.fx.binding), expected)

    def test_a_repaint_between_the_reads_leaves_the_decision_to_native_proof(self):
        # A working turn repaints the screen between the two reads. The frame is
        # then no observation: only an independent native proof of this same root
        # can pass, exactly as when no frame is available at all.
        self.select_resumed()
        first = self.observe(self.fx.binding, 'native-selection')
        repaint = dict(ok=True, native_frame={**first['native_frame'], 'revision': 10})
        for native, expected in ((None, False), (self.old, False), (self.resumed, True)):
            self.fx.selected = native
            with self.subTest(native=native), \
                    mock.patch.object(shell, 'control', side_effect=[first, repaint]):
                self.assertEqual(shell.answer_attachment_current(self.fx.binding), expected)
        with mock.patch.object(pane_ownership, '_runtime', side_effect=[('codex', False), ('codex', True)]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        with mock.patch.object(codex, 'session_id_of_process', side_effect=[None, None, self.old]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        background = (*self.process_row[:4], self.member[0] + 1, self.member[1])
        with mock.patch.object(pane_ownership, '_process', return_value=background):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        with mock.patch.object(pane_ownership, '_process', side_effect=[self.process_row, background]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))

    def test_unknown_conflicting_or_non_native_footer_never_authorizes_an_answer(self):
        self.select_resumed()
        for lines in (['›', 'custom status', '? for shortcuts'],
                      ['›', self.resumed, 'Hearting projected status'],
                      ['›', self.old + ' · ' + self.resumed, '? for shortcuts'],
                      ['›', self.resumed[:-3] + '…', '? for shortcuts']):
            with self.subTest(lines=lines), mock.patch.object(shell, 'control',
                    return_value=dict(ok=True, native_frame=dict(revision=9, epoch=12, lines=lines))):
                self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        self.fx.selected = self.resumed
        with mock.patch.object(shell, 'control', return_value=dict(ok=True,
                native_frame=dict(revision=9, epoch=12, lines=[], invalid=True))):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        self.fx.selected = self.old  # Independent, current native proof conflicts.
        self.assertFalse(shell.answer_attachment_current(self.fx.binding))

    def test_existing_unique_native_proof_does_not_require_the_new_optional_surface(self):
        self.fx.selected = self.old
        with mock.patch.object(shell, 'control', return_value=dict(ok=False, reason='unsupported-operation')):
            self.assertTrue(shell.answer_attachment_current(self.fx.binding))


class NativeSelectionHostTests(unittest.TestCase):
    def test_actual_control_and_native_output_callbacks_keep_selection_generation(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const begin=source.indexOf('function nativeSelectionFrame()');
const helpers=begin<0?'':source.slice(begin,source.indexOf('function readAction(',begin));
const request=source.slice(source.indexOf('async function request('),source.indexOf('terminal.parser.registerCsiHandler',source.indexOf('async function request(')));
const A='00000000-0000-4000-8000-000000000099',B='00000000-0000-4000-8000-000000000052';
function context(extra=[]){
 const lines=['transcript '+B,'› PRIVATE HUMAN DRAFT',...extra,'',A+' · Context 10% used','← for agents · ? for shortcuts',''];
 const actions=[];let resolve;
 const c={harness:'codex',sid:B,native:{pid:123,start:'456'},screenRevision:9,epoch:12,selection:1,
   sequence:4,token:'owned',ended:false,relay:false,actions,
   terminal:{rows:lines.length,buffer:{active:{viewportY:0,getLine:i=>({translateToString:()=>lines[i]})}}},
   exact:r=>r.sid===c.sid,source:()=>new Promise(r=>{resolve=r;}),lifecycle:r=>{actions.push(r);},
   release:r=>resolve(r)};
 vm.createContext(c);vm.runInContext(helpers+request,c);return c;
}
(async()=>{
 const c=context(),seen=await c.request({op:'native-selection',sid:B});
 a.equal(seen.ok,true);a.equal(seen.native_frame.revision,9);
 a.deepEqual(Array.from(seen.native_frame.lines),['›',A+' · Context 10% used','? for shortcuts']);
 a.equal(JSON.stringify(seen).includes('PRIVATE HUMAN DRAFT'),false);
 for(const body of ['PRIVATE_CONTINUATION_SENTINEL','Where should the new conversation run?']){
  const hidden=await context([body]).request({op:'native-selection',sid:B});
  a.equal(hidden.native_frame.invalid,true);a.equal(hidden.native_frame.lines.length,0);
  a.equal(JSON.stringify(hidden).includes(body),false);
 }
 a.equal((await c.request({op:'native-selection',sid:A})).ok,false);
 for(const changed of ['none','screen','input','selection','unproved']){
  const x=context();x.observeNativeSelection();
  if(changed==='screen')x.screenRevision++;
  if(changed==='input')x.epoch++;
  if(changed==='selection')x.selection++;
  x.release({ok:changed!=='unproved',selected_sid:A,native_revision:9,native_epoch:12});
  await new Promise(setImmediate);
  a.equal(x.actions.length,changed==='none'?1:0,changed);
  if(x.actions.length)a.deepEqual(JSON.parse(JSON.stringify(x.actions[0])),
    {token:'owned',harness:'codex',pid:123,start:'456',sid:A,seq:5,event:'select'});
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_a_screen_painted_during_a_check_is_looked_at_once_it_ends(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const begin=source.indexOf('function nativeSelectionFrame()');
const helpers=source.slice(begin,source.indexOf('function readAction(',begin));
const A='00000000-0000-4000-8000-000000000099',B='00000000-0000-4000-8000-000000000052';
const lines=['› Ask Codex to do anything','',A+' · '+A];
const calls=[],actions=[];
const c={harness:'codex',sid:B,native:{pid:123,start:'456'},screenRevision:9,epoch:12,selection:1,
  sequence:4,token:'owned',ended:false,relay:false,
  terminal:{rows:lines.length,buffer:{active:{viewportY:0,getLine:i=>({translateToString:()=>lines[i]})}}},
  source:()=>new Promise(r=>calls.push(r)),lifecycle:r=>{actions.push(r);}};
vm.createContext(c);vm.runInContext(helpers,c);
(async()=>{
 c.observeNativeSelection();a.equal(calls.length,1);
 c.screenRevision=10;c.observeNativeSelection();a.equal(calls.length,1);  // skipped while checking
 calls[0]({ok:true,selected_sid:A,native_revision:9,native_epoch:12});
 await new Promise(setImmediate);
 a.equal(actions.length,0);a.equal(calls.length,2);                     // the skipped screen, once
 calls[1]({ok:true,selected_sid:A,native_revision:10,native_epoch:12});
 await new Promise(setImmediate);
 a.equal(actions.length,1);a.equal(actions[0].sid,A);a.equal(calls.length,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_clear_keeps_the_model_row_and_a_start_or_switch_reads_it_anew(self):
        # Spec E9 rule 34 (f): `/clear` keeps the process and its model, so the head keeps
        # the model and effort it last observed (context starts empty); a start, a
        # `/resume` or a switch keeps the contract that configured values never stand in
        # for an observed selection.
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const begin=source.indexOf('async function lifecycle(req)');
const body=source.slice(begin,source.indexOf('async function request(req)',begin));
const input=source.slice(source.indexOf('async function refreshReadiness()'),source.indexOf('function observeReport(data)'));
const row={pid:10,start:'s'},noop=()=>{};
const c={processRow:()=>row,rootNative:{pid:10,start:'s'},descends:()=>true,sameTerminal:()=>true,token:'t',harness:'claude',sequence:0,
 nativeFooter:null,paintedCommon:[],nativeContext:undefined,sid:'',nativeSelected:{},displayValues:{},panelModel:{},panelSections:undefined,panelLines:[],
 projectionKey:'',footer:'',footerSegments:[],routeDetail:false,routeScroll:0,selection:0,native:null,state:'unknown',nativeTurn:'',turnMark:0,
 seenTurn:null,turnRecord:'',formSeen:false,eventMark:0,clock:0,empty:false,submitted:-1,lastEnter:-1,block:{leave:noop,update:noop},publishNativeDisplay:noop,
 queueDisplay:noop,currentDelivery:null,releaseHeld:noop,setImmediate:noop,projection:noop,
 epoch:0,nativeEpoch:0,unselected:null,routePage:0,upgradeNote:'',keyTail:'',paste:false,relay:false,screenRevision:0,screenSnapshot:()=>'',queueProjection:noop,
 source:op=>Promise.resolve(op==='readiness'?{ok:true,ready:true}:{})};
vm.createContext(c);vm.runInContext(body+input,c);
const plain=v=>JSON.parse(JSON.stringify(v??null));
let seq=0;
const observed=()=>{
 c.nativeSelected={model:'Opus 5.5',effort:'xhigh'};c.displayValues={model:'Opus 5.5',effort:'xhigh',context:'41%'};
 c.panelModel={...c.panelModel,selected:[{text:'Opus 5.5'},{text:' xhigh'}],model_value:'claude-opus-5-5',effort_value:'xhigh',
  context:[{text:'context 41%'}],session_title:'BEFORE',repo:[{text:'hearting/'}],git:[{text:'(main)'}],quota:[{text:'5h 12%'}]};
};
const start=(sid,how,harness='claude',event='start')=>{c.harness=harness;return c.lifecycle({token:'t',harness,pid:10,start:'s',sid,seq:++seq,event,...(how?{source:how}:{})});};
(async()=>{
 a.equal((await start('A','startup')).ok,true);observed();
 a.equal((await start('B','clear')).ok,true);
 a.deepEqual(plain(c.nativeSelected),{model:'Opus 5.5',effort:'xhigh'});
 a.deepEqual(plain(c.displayValues),{model:'Opus 5.5',effort:'xhigh'},'its context starts empty');
 a.deepEqual(plain(c.panelModel.selected),[{text:'Opus 5.5'},{text:' xhigh'}]);a.equal(c.panelModel.effort_value,'xhigh');
 a.equal(c.panelModel.context,undefined);a.equal(c.panelModel.session_title,undefined);a.equal(c.panelModel.repo[0].text,'hearting/');
 for(const [how,harness,event] of [['resume','claude','start'],['startup','claude','start'],[undefined,'claude','start'],[undefined,'codex','select']]){
  observed();a.equal((await start('S'+seq,how,harness,event)).ok,true);
  a.deepEqual(plain(c.nativeSelected),{},how+' '+event);a.equal(c.panelModel.selected,undefined,how+' '+event);
  a.equal(c.panelModel.repo[0].text,'hearting/',how+' '+event);
 }
 // RV-PR1565 F1: a start or select follows the Enter that chose it (a resume picker), so that
 // frontier is submitted and the screen reading may settle it; a key typed after it is a draft.
 for(const [how,harness,event] of [['resume','claude','start'],[undefined,'codex','select']]){
  c.observeInput(Buffer.from('\x1b[B\r'));a.equal((await start('E'+seq,how,harness,event)).ok,true);
  a.equal(c.submitted,c.lastEnter,how+' '+event);await c.refreshReadiness();a.equal(c.empty,true,how+' '+event);
  c.observeInput(Buffer.from('x'));await c.refreshReadiness();a.equal(c.empty,false,'a key after the '+event+' is a draft');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

class ShownStateOrderTests(unittest.TestCase):
    def test_event_orders_draw_the_natives_own_state(self):
        # Review RV-PR1560 R1-R3, R5: host.cjs's own shownState/noteTurn/readTurnRecord/
        # observeForm text, fed scripted event orders (the review's probe); lifecycle's
        # `state`/`eventMark` changes replayed as lifecycle() applies them.
        script = r"""
const fs=require('node:fs'),a=require('node:assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
const block=src.slice(src.indexOf("let nativeTurn = ''"),src.indexOf('let nativeEpoch = 0'));
function world(h){
  return new Function('uid',`
  let harness=${JSON.stringify(h)},state='unknown',sid='s1',relay=false,ended=false,selection=0,screenRevision=0;
  function queueDisplay(){}function schedule(){}function screenSnapshot(){return 'snap'+screenRevision;}
  let reply=null;async function source(op,x){return reply(op,x);}
  const native={pid:1,start:'7'};let current={};
  const fs={lstatSync:()=>({isFile:()=>true,uid,size:64}),readFileSync:()=>JSON.stringify(current)};
  ${block}
  turnRecord='/record';
  function hook(ev){const was=state;
    if(ev==='select'){state='unknown';nativeTurn='';turnMark=0;seenTurn=null;formSeen=false;eventMark=++clock;return;}
    if(ev==='prompt')state='working';else if(ev==='stop'||ev==='idle')state='idle';else if(ev==='working')state='working';else state='blocked';
    eventMark=++clock;if(state==='blocked'&&was!=='blocked')formSeen=false;}
  const record=fields=>{current={pid:1,procStart:'7',sessionId:'s1',...fields};readTurnRecord();};
  return {hook,record,reading:turn=>noteTurn(turn,++clock),shown:()=>shownState(),observeForm,setReply:f=>{reply=f;},
    bump:()=>{screenRevision++;},get state(){return state;}};`)(process.getuid());
}
const tick=()=>new Promise(r=>setTimeout(r,5));
(async()=>{
 // Claude: the record wins over late hooks (C1, C2); an unmapped status leaves the hooks (C4).
 {const w=world('claude');w.hook('select');w.record({status:'idle'});w.hook('prompt');w.record({status:'busy'});w.record({status:'idle'});
  w.record({status:'busy'});w.hook('stop');a.equal(w.shown(),'working','C1');}
 {const w=world('claude');w.hook('select');w.record({status:'idle'});w.record({status:'busy'});w.record({status:'idle'});w.hook('prompt');
  a.equal(w.shown(),'idle','C2');a.equal(w.state,'working');}
 {const w=world('claude');w.hook('select');w.record({status:'busy'});w.hook('stop');w.record({status:'shell'});a.equal(w.shown(),'idle','C4');}
 // R1: a menu or settings dialog is not an answer wait; the person's own reasons are; an unlisted one is the hooks'.
 for(const [why,want] of [['dialog open','idle'],['permission prompt','blocked'],['input needed','blocked'],['worker request','blocked'],
   ['sandbox request','blocked'],['goal proposal','blocked'],['a reason not listed','idle'],[undefined,'idle']]){
  const w=world('claude');w.hook('select');w.record({status:'idle'});w.hook('stop');w.record({status:'waiting',waitingFor:why});
  a.equal(w.shown(),want,'C3 '+why);a.equal(w.state,'idle');
 }
 // A dialog says nothing about the turn: the previous reading stands (a running turn beside
 // `/btw`; an idle one under a late `prompt` hook); after an answer wait the hooks say.
 {const w=world('claude');w.hook('select');w.record({status:'busy'});w.hook('prompt');w.record({status:'waiting',waitingFor:'dialog open'});
  a.equal(w.shown(),'working','dialog beside a running turn');}
 {const w=world('claude');w.hook('select');w.record({status:'busy'});w.record({status:'idle'});w.hook('prompt');
  w.record({status:'waiting',waitingFor:'dialog open'});a.equal(w.shown(),'idle','dialog after the turn, its prompt hook late');}
 {const w=world('claude');w.hook('select');w.record({status:'waiting',waitingFor:'permission prompt'});a.equal(w.shown(),'blocked');
  w.hook('working');w.record({status:'waiting',waitingFor:'dialog open'});a.equal(w.shown(),'working','after an answer wait the hooks say');}
 // R5: a record written for another session or another process changes nothing.
 for(const other of [{sessionId:'another'},{procStart:'8'},{pid:2}]){
  const w=world('claude');w.hook('select');w.record({status:'idle'});w.hook('stop');w.record({status:'busy',...other});
  a.equal(w.shown(),'idle',JSON.stringify(other));
 }
 // R3: right after a resume a rollout ending in `task_started` is not a running turn (X1);
 // its idle is taken; a turn seen starting after that reads as running.
 {const w=world('codex');w.hook('select');w.reading('working');a.equal(w.shown(),'unknown','X1');a.equal(w.state,'unknown');}
 {const w=world('codex');w.hook('select');w.reading('idle');a.equal(w.shown(),'idle');w.reading('working');a.equal(w.shown(),'working');}
 {const w=world('claude');w.hook('select');w.record({status:'busy'});a.equal(w.shown(),'working','a live Claude record is not held back');}
 // R2: one screen the judgment misses does not hold the panel at running (X3);
 // a question seen again turns it back to waiting while the hooks wait.
 {const w=world('codex');w.hook('select');w.reading('idle');w.hook('prompt');w.reading('working');w.hook('blocked');
  const seq=['form-open',null,'form-open','form-open'];let i=0;w.setReply(async()=>({ok:true,reason:seq[Math.min(i++,seq.length-1)]}));
  const states=[];for(let k=0;k<4;k++){w.bump();w.observeForm();await tick();states.push(w.shown());}
  a.deepEqual(states,['blocked','working','blocked','blocked'],'X3');a.equal(w.state,'blocked');}
 // An approval: the question gone, the turn runs on, and the next hook takes over.
 {const w=world('codex');w.hook('select');w.reading('idle');w.hook('prompt');w.reading('working');w.hook('blocked');
  const seq=['form-open',null,null];let i=0;w.setReply(async()=>({ok:true,reason:seq[Math.min(i++,seq.length-1)]}));
  const states=[];for(let k=0;k<3;k++){w.bump();w.observeForm();await tick();states.push(w.shown());}
  a.deepEqual(states,['blocked','working','working'],'approved');w.hook('working');a.equal(w.shown(),'working');w.hook('stop');a.equal(w.shown(),'idle');}
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


class FooterReaderParityTests(unittest.TestCase):
    """The host frame and the Python footer reader name the same current thread."""
    FIXTURES = ROOT / 'utilities/fixtures/codex-0.163-shared-footer'
    REAL = {'side-133x30-native91.txt': '01a1c650-6f32-7880-bb70-815b30d8bd06',
            'bottom-80x24.txt': '01a1c655-bacb-75a2-b8d1-2997f18b9fcd',
            # A UUID-shaped git branch drawn before the real thread-id: a conflict.
            'branch-first-133x30.txt': None, 'branch-first-80x24.txt': None}
    T = '01a1c520-2193-71f0-a79c-b1fb86bd1a21'
    OTHER = '99a1c520-2193-71f0-a79c-b1fb86bd1a21'

    def host_frames(self, screens):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8'),screens=JSON.parse(fs.readFileSync(0,'utf8'));
const begin=source.indexOf('function nativeSelectionFrame()');
const fn=source.slice(begin,source.indexOf('let checkingNativeSelection=',begin));
const c={screenRevision:3,epoch:1};vm.createContext(c);vm.runInContext(fn+'\nthis.frame=nativeSelectionFrame;',c);
console.log(JSON.stringify(screens.map(lines=>{
 c.terminal={rows:lines.length,
   buffer:{active:{viewportY:0,getLine:i=>({translateToString:()=>lines[i]})}}};
 return c.frame();
})));
"""
        result = subprocess.run([NODE, '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                input=__import__('json').dumps(screens), text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return __import__('json').loads(result.stdout)

    @staticmethod
    def current(state_sid):
        state, sid = state_sid
        return sid if state == 'current' else None

    def test_both_readers_agree_on_real_and_edge_screens(self):
        real = {name: self.FIXTURES.joinpath(name).read_text().splitlines() for name in self.REAL}
        prompt = ['', '› Ask Codex to do anything', '']
        rows = [
            # Status line alone (Codex 0.163, no help row).
            f'  {self.T} · {self.T} · master · Context 0% used · gpt default',
            f'  {self.T} · proj · Context 12% used',
            f'  {self.T} · {self.T[:-3]}…',
            f'  {self.T} · {self.T[:8]}…  ⚠ 1 warning · f2 to view',
            # Not a status line: no identity.
            f'  text mentioning {self.T}', f'  {self.T} · master',
            f'  {self.T} · {self.OTHER[:20]}…', f'  title · {self.T} · Context 1% used',
            # Conflicting ids on an accepted row: every id reaches the Python reader.
            f'  {self.T} · Context 0% used · {self.OTHER}', f'  {self.T} · {self.T} · {self.OTHER}',
            f'  {self.T} · Context 10% used · {self.OTHER[:20]}…',
        ]
        layouts = {name: lines for name, lines in real.items()}
        layouts.update({f'row-{i}': prompt + [row] for i, row in enumerate(rows)})
        layouts.update({
            'help-first': prompt + [f'  {self.T} · Context 10% used', '  ← for agents · ? for shortcuts'],
            'help-last': prompt + [f'  proj · {self.T}', '  ? for shortcuts'],
            'help-compact': prompt + [f'  {self.T} · {self.T}', '  ← for agents'],
            'help-conflict': prompt + [f'  {self.T} · x · {self.OTHER}', '  ? for shortcuts'],
            'menu': prompt + ['  Where should the new conversation run?'],
            'no-prompt': ['  ' + self.T + ' · ' + self.T],
        })
        frames = dict(zip(layouts, self.host_frames(list(layouts.values()))))
        for name, lines in layouts.items():
            with self.subTest(screen=name):
                frame = frames[name]
                direct = self.current(pane_ownership._codex_current_footer(lines))
                via_host = (None if frame.get('invalid')
                            else self.current(pane_ownership._codex_current_footer(frame['lines'])))
                self.assertEqual(via_host, direct)
                if name in self.REAL:
                    self.assertEqual(direct, self.REAL[name])
                if name in {'row-8', 'row-9', 'row-10'}:
                    self.assertIsNone(direct)
                # Only identity evidence leaves the native viewport.
                for line in frame['lines']:
                    self.assertNotIn('master', line)
                    self.assertNotIn('warning', line)

    def test_every_composed_status_row_reads_the_same_through_the_host(self):
        # Rows are composed, not listed: every ordered choice of 2-4 fields from
        # the materials, with or without a trailing warning, key hint or help row.
        # A new material is covered in every position by adding it here.
        import itertools
        materials = (self.T, self.OTHER, self.T[:20] + '…', self.OTHER[:20] + '…',
                     'Context 10% used', 'master', 'Fix login flow')
        endings = ('', '  ⚠ 1 warning', ' · f2 to view', '  ⚠ 1 warning · f2 to view')
        prompt = ['', '› Ask Codex to do anything', '']
        screens = []
        for size in (2, 3, 4):
            for fields in itertools.product(materials, repeat=size):
                for ending in endings:
                    row = '  ' + ' · '.join(fields) + ending
                    screens.append(prompt + [row])
                    screens.append(prompt + [row, '  ← for agents · ? for shortcuts'])
        frames = self.host_frames(screens)
        mismatched, accepted = [], 0
        for lines, frame in zip(screens, frames):
            direct = pane_ownership._codex_current_footer(lines)
            via_host = (('invalid', '') if frame.get('invalid')
                        else pane_ownership._codex_current_footer(frame['lines']))
            if self.current(via_host) != self.current(direct):
                mismatched.append((lines[-1], direct, via_host))
            accepted += bool(frame['lines'])
            for line in frame['lines']:
                self.assertFalse({'master', 'Fix', 'login', 'warning', 'f2'} & set(line.split()), line)
        self.assertEqual(len(screens), 2 * 4 * (7 ** 2 + 7 ** 3 + 7 ** 4))
        self.assertGreater(accepted, len(screens) // 4)
        self.assertEqual(mismatched, [])


if __name__ == '__main__':
    unittest.main()
