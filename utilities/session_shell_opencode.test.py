"""The real TUI module observes native route renders without a polling timer."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PROBE=r"""
const fs=require('node:fs'),net=require('node:net'),vm=require('node:vm');
const actual=fs.readFileSync(process.argv[1],'utf8'),events=[],slots=[],timers=[];
let route={type:'session',sessionID:'ses_first'},listener;
process.env.HEARTING_SHELL_SOCKET=process.argv[2];process.env.HEARTING_SHELL_TOKEN='private-fixture';
const context=vm.createContext({process,Buffer,Promise,Number,Set,JSON,
 setTimeout,clearTimeout,setInterval:(fn,ms)=>{timers.push([fn,ms]);return 1;},clearInterval:()=>{}});
const module=new vm.SourceTextModule(actual,{context});
const socket=()=>{const callbacks={};return {on(event,callback){callbacks[event]=callback;if(event==='connect')queueMicrotask(callback);return this;},
  write(raw){events.push(JSON.parse(raw));callbacks.data?.('{}\n');},destroy(){}};};
const netModule=new vm.SyntheticModule(['default'],function(){this.setExport('default',{connect:socket});},{context});
const fsModule=new vm.SyntheticModule(['readFileSync'],function(){this.setExport('readFileSync',fs.readFileSync);},{context});
// This probe covers lifecycle selection; native slot rendering has separate
// private OpenCode captures and never changes this event sequence.
const panelModule=new vm.SyntheticModule(['sessionPanel'],function(){this.setExport('sessionPanel',()=>({observe(){},dispose(){}}));},{context});
(async()=>{
 await module.link(name=>name==='node:net'?netModule:name==='node:fs'?fsModule:name==='./panel.tsx'?panelModule:Promise.reject(Error('unexpected import '+name)));
 await module.evaluate();
 const dispose=module.namespace.default.setup({ui:{router:{current:()=>route},slot:options=>{slots.push(options);return ()=>{};}},
   data:{session:{status:()=> 'idle'},listen:fn=>{listener=fn;return ()=>{};}}});
 const drain=()=>new Promise(resolve=>setImmediate(resolve));
 if(process.argv[3]==='rapid'){
   route={type:'session',sessionID:'ses_third'};for(const slot of slots)slot.render({});
   listener({details:{type:'fixture-native-update',data:{}}});await drain();
 }else{
   for(const slot of slots)slot.render({});await drain();
   route={type:'session',sessionID:'ses_second'};for(const slot of slots)slot.render({});await drain();
   route={type:'home'};for(const slot of slots)slot.render({});await drain();
 }
 dispose();await drain();
 console.log(JSON.stringify({events,timers:timers.map(item=>item[1]),slots:slots.length}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


class OpenCodeShellSelectionTest(unittest.TestCase):
    def probe(self,mode='normal'):
        node=shutil.which('node')
        if not node:self.skipTest('node unavailable')
        with tempfile.TemporaryDirectory(prefix='shell-oc-event-') as root:
            result=subprocess.run([node,'--experimental-vm-modules','-e',PROBE,
                str(ROOT/'adapters/opencode/tui/hearting-session-shell.js'),str(Path(root)/'private.sock'),mode],
                capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_native_route_mounts_change_the_selected_session_without_timer_or_bus_traffic(self):
        data=self.probe()
        self.assertEqual(data['timers'],[])
        selections=[row['sid'] for row in data['events'] if row['event'] in ('start','select')]
        self.assertEqual(selections,['ses_first','ses_second'])
        self.assertTrue(any(row['event']=='deselect' and row['sid']=='ses_second' for row in data['events']))

    def test_a_queued_old_selection_cannot_be_reported_after_the_native_route_moves(self):
        data=self.probe('rapid')
        selections=[row['sid'] for row in data['events'] if row['event'] in ('start','select')]
        self.assertEqual(selections,['ses_third'])


if __name__=='__main__':unittest.main()
