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
const observation=text.slice(text.indexOf('async function refreshReadiness()'),text.indexOf('function userInput(data)'));
function make(){
  const s={Buffer,epoch:0,nativeEpoch:0,lastEnter:-1,submitted:-1,empty:true,keyTail:'',paste:false,
    relay:false,currentDelivery:null,sid:'owned-sid',selection:0,screenRevision:0,state:'idle',
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


if __name__ == "__main__":
    unittest.main()
