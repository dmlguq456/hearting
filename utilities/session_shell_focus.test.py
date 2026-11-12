"""Terminal focus reports are observations, not edits to a native composer."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FocusFrontierTest(unittest.TestCase):
    def test_focus_reports_preserve_draft_frontier_and_recheck_observation(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8');
function declaration(name){
  const begin=text.search(new RegExp('^(?:async )?function '+name+'\\(','m'));
  const end=text.indexOf('\n}',begin);
  a.ok(begin>=0&&end>begin,'standalone function '+name+' is present');
  return text.slice(begin,end+2);
}
const observation=declaration('refreshReadiness')+'\n'+declaration('observeInput');
function make(){
  const s={Buffer,epoch:0,nativeEpoch:0,lastEnter:-1,submitted:-1,empty:true,keyTail:'',paste:false,
    relay:false,currentDelivery:null,sid:'owned-sid',sequence:0,selection:0,screenRevision:0,state:'idle',
    screenSnapshot:()=>[],queueProjection:()=>{},source:async()=>({ok:true,ready:true})};
  vm.createContext(s);vm.runInContext(observation,s);return s;
}
(async()=>{
  for(const report of ['\x1b[I','\x1b[O'])for(const split of [0,1,2,3]){
    const s=make();s.observeInput(Buffer.from(report.slice(0,split)));
    if(split>0&&split<3){await s.refreshReadiness();a.equal(s.empty,false);}
    s.observeInput(Buffer.from(report.slice(split)));
    a.equal(s.nativeEpoch,0,'focus report is not an edit');a.equal(s.epoch,3);
    a.equal(s.keyTail,'');await s.refreshReadiness();a.equal(s.empty,true);
  }
  for(const key of ['draft','\t','\x1b[C','\x1b[1;2A']){
    const s=make();s.observeInput(Buffer.from(key));await s.refreshReadiness();
    a.equal(s.empty,false,'edits and suggestion acceptance still withhold input');
  }
  const mixed=make();mixed.observeInput(Buffer.from('\x1b[Ihello\r\x1b[O'));
  a.equal(mixed.lastEnter,6);a.equal(mixed.nativeEpoch,6);mixed.submitted=mixed.lastEnter;
  await mixed.refreshReadiness();a.equal(mixed.empty,true);
  mixed.observeInput(Buffer.from('draft\x1b[I'));await mixed.refreshReadiness();a.equal(mixed.empty,false);
  const pasted=make(),bytes=Buffer.from('\x1b[200~\x1b[I\x1b[O\x1b[201~');
  pasted.observeInput(bytes);a.equal(pasted.nativeEpoch,bytes.length);await pasted.refreshReadiness();a.equal(pasted.empty,false);
  const race=make();let resolve;race.source=()=>new Promise(r=>resolve=r);
  const pending=race.refreshReadiness();race.observeInput(Buffer.from('\x1b[I'));resolve({ok:true,ready:true});await pending;
  a.equal(race.empty,false,'focus still invalidates an in-flight observation');
  race.source=async()=>({ok:true,ready:true});await race.refreshReadiness();a.equal(race.empty,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "utilities/session-shell/host.cjs")],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wheel_reports_keep_input_settled_but_clicks_and_drafts_do_not(self):
        # PG-50: Herdr sends a Claude pane wheel down/up/down on every recent read.
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8');
function declaration(name){
  const begin=text.search(new RegExp('^(?:async )?function '+name+'\\(','m'));
  const end=text.indexOf('\n}',begin);
  a.ok(begin>=0&&end>begin,'standalone function '+name+' is present');
  return text.slice(begin,end+2);
}
const code=['refreshReadiness','observeInput','observeReport','nativePointer'].map(declaration).join('\n');
function make(){
  const s={Buffer,epoch:0,nativeEpoch:0,lastEnter:-1,submitted:-1,empty:true,keyTail:'',paste:false,
    relay:false,currentDelivery:null,sid:'owned-sid',sequence:0,selection:0,screenRevision:0,state:'idle',heldBytes:0,
    left:0,written:[],held:[],screenSnapshot:()=>[],queueProjection:()=>{},queueDisplay:()=>{},
    source:async()=>({ok:true,ready:true})};
  s.block={leave:()=>s.left++};s.child={write:d=>s.written.push(d)};s.inputOwner={hold:d=>s.held.push(d)};
  vm.createContext(s);vm.runInContext(code,s);return s;
}
const packet=(c,release=false)=>Buffer.from(`\x1b[<${c};60;20${release?'m':'M'}`,'latin1');
(async()=>{
  const s=make();s.observeInput(Buffer.from('turn one\r'));s.submitted=s.lastEnter;
  for(const c of [65,65,65,64,64,64,65,65,65,68,81])s.nativePointer(c,false,packet(c));
  a.equal(s.nativeEpoch,9,'wheel reports are not edits');a.equal(s.left,0,'wheel keeps block focus');
  a.equal(s.written.length,11,'native still receives the scroll');a.ok(s.epoch>9);
  await s.refreshReadiness();a.equal(s.empty,true);
  const held=make();held.currentDelivery='ref';held.nativePointer(65,false,packet(65));
  a.equal(held.held.length,1);a.equal(held.nativeEpoch,0);a.equal(held.heldBytes,packet(65).length);
  const click=make();click.nativePointer(0,false,packet(0));click.nativePointer(0,true,packet(0,true));
  a.equal(click.left,1);a.ok(click.nativeEpoch>0);await click.refreshReadiness();
  a.equal(click.empty,false,'a click still withholds input');
  const draft=make();draft.observeInput(Buffer.from('draft'));draft.nativePointer(65,false,packet(65));
  await draft.refreshReadiness();a.equal(draft.empty,false,'a typed draft stays protected after a scroll');
  const pasted=make();pasted.observeInput(Buffer.from('\x1b[200~'));pasted.nativePointer(65,false,packet(65));
  a.equal(pasted.nativeEpoch,6+packet(65).length,'pasted wheel text counts');
  const race=make();let resolve;race.source=()=>new Promise(r=>resolve=r);
  const pending=race.refreshReadiness();race.nativePointer(65,false,packet(65));resolve({ok:true,ready:true});await pending;
  a.equal(race.empty,false,'a scroll still invalidates an in-flight observation');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "utilities/session-shell/host.cjs")],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_late_ready_answer_never_overwrites_a_newer_lifecycle_event(self):
        # An idle observation started before a newer blocked event is older than it,
        # even when the native screen has not been redrawn yet.
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8');
const code=text.slice(text.indexOf('async function lifecycle(req)'),text.indexOf('async function request(req)'))
  +text.slice(text.indexOf('async function refreshReadiness()'),text.indexOf('function observeInput('));
(async()=>{
  for(const harness of ['claude','codex','opencode'])for(const between of [true,false]){
    let resolve;const s={Promise,harness,token:'t',rootNative:{pid:10,start:'1'},sequence:1,sid:'A',state:'idle',
      empty:true,submitted:0,lastEnter:0,currentDelivery:null,epoch:5,selection:1,screenRevision:10,nativeEpoch:0,
      relay:false,paste:false,keyTail:'',clock:0,eventMark:0,formSeen:false,painted:[],source:()=>new Promise(r=>resolve=r),screenSnapshot:()=>[],
      processRow:pid=>({pid,start:'1'}),descends:()=>true,sameTerminal:()=>true,projection(){},
      queueProjection(){},status:()=>({})};
    s.queueDisplay=()=>s.painted.push(s.state);
    vm.createContext(s);vm.runInContext(code,s);
    const pending=s.refreshReadiness();
    if(between)a.equal((await s.lifecycle({event:'blocked',seq:2,sid:'A',pid:11,start:'1',token:'t',harness})).ok,true);
    resolve({ok:true,ready:true});await pending;
    const shown=s.painted[s.painted.length-1];
    if(between){a.equal(s.state,'blocked',harness);a.equal(s.empty,false,harness);a.equal(shown,'blocked',harness);}
    else{a.equal(s.state,'idle',harness);a.equal(s.empty,true,harness);}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "utilities/session-shell/host.cjs")],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
