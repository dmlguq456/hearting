"""Real PTY/control loop with synthetic native UI and private dependency assets."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]

FAKE_NATIVE=r"""
import json,os,select,socket,subprocess,sys,time,tty
from pathlib import Path
sid=sys.argv[1]
def event(kind,identity=sid):
    raw=open('/proc/self/stat').read(); start=raw.rsplit(') ',1)[1].split()[19]
    request={'op':'event','event':kind,'harness':'claude','sid':identity,
        'native_model':'NATIVE_SELECTED_MODEL',
        'pid':os.getpid(),'start':start,'token':os.environ['HEARTING_SHELL_TOKEN'],
        'seq':time.monotonic_ns()//1000}
    registry=Path(os.environ['CLAUDE_CONFIG_DIR'])/'sessions'/str(os.getpid())
    registry=registry.with_suffix('.json');registry.parent.mkdir(parents=True,exist_ok=True)
    registry.write_text(json.dumps({'pid':os.getpid(),'procStart':start,'sessionId':identity,
        'status':'busy' if kind=='prompt' else 'idle'}))
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])
        channel.sendall((json.dumps(request)+'\n').encode());channel.recv(8192)
if '--headless-event' in sys.argv:
    event('start',sid+'-child');raise SystemExit()
tty.setraw(0)
exit_fd=os.open(os.environ['LOOP_NATIVE_EXIT'],os.O_RDWR|os.O_NONBLOCK)
began=time.monotonic();event('start');delay=(time.monotonic()-began)*1000
print('\x1b]2;OWN_NATIVE_TITLE\x07\x1b[?2004hACK_MS='+str(delay)+'\r\nREADY\r\n❯ \r\n──────────────────\r\n',end='',flush=True)
pending=''
while True:
    readable,_,_=select.select([0,exit_fd],[],[])
    if exit_fd in readable:break
    data=os.read(0,65536)
    if not data or data==b'\x03':break
    pending+=data.decode()
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        line=line.replace('\x1b[200~','').replace('\x1b[201~','')
        callback_missing='PEER_NO_CALLBACK' in line
        if not callback_missing and line!='turn-close':event('prompt')
        if line=='spawn-child':
            subprocess.run([sys.executable,__file__,sid,'--headless-event'],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        if not callback_missing and line!='turn-open':event('stop')
        print('\x1b[2J\x1b[HDONE_'+line+'\r\n❯ '+pending+'\r\n──────────────────\r\n',end='',flush=True)
        if line.startswith('geometry'):print('GEOMETRY_'+line+'_'+str(os.get_terminal_size(0).columns)+'x'+str(os.get_terminal_size(0).lines)+'\r\n',end='',flush=True)
        if '\x1b' in line:print('NATIVE_ESC_'+line.replace('\x1b','^')+'\r\n',end='',flush=True)
        if line=='side-unicode':print('\x1b[3;1H🙂',end='',flush=True)
        if line=='form':print('Do you want to proceed?\r\n1. Yes\r\n2. No\r\n',end='',flush=True)
"""

HOST_LAUNCHER=r"""
const fs=require('node:fs'),net=require('node:net');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
if(process.argv.includes(cfg.host)){
 const report=error=>fs.appendFileSync(cfg.diagnostic,String(error?.stack||error)+'\n');
 process.on('uncaughtException',report);process.on('unhandledRejection',report);
 const cp=require('node:child_process'),spawn=cp.spawn;
 cp.spawn=function(file,args,options){
  if(args?.some(arg=>String(arg).endsWith('/session_shell.py'))){const child=spawn(file,args,{...options,stdio:['pipe','pipe','pipe']});child.stderr.on('data',data=>fs.appendFileSync(cfg.diagnostic,data));return child;}
  return spawn(file,args,options);
 };
 process.env.HEARTING_SHELL_PYTHON=cfg.backend;
 if(cfg.fakeFleet){const pty=require('node-pty'),start=pty.spawn;
  pty.spawn=function(file,args,options){return args?.includes('--explorer')?start(cfg.python,[cfg.fakeFleet],options):start(file,args,options);};
 }
 fs.writeFileSync(cfg.hostPidFile,String(process.pid));
 const inlet=net.createServer(socket=>{let raw='';socket.on('data',data=>{
  raw+=data;if(!raw.includes('\n'))return;
  const input=JSON.parse(raw.split('\n')[0]);
  process.stdin.emit('data',Buffer.from(input.batch,'base64'));socket.end('{}\n');
 });});
 inlet.listen(cfg.inlet);
}
"""

DRIVER=r"""
const fs=require('node:fs'),net=require('node:net'),path=require('node:path');
const pty=require('node-pty'),cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let raw='',alive=true,ended,child,barrier;
const sideTerm=['side-columns','route-pages','side-left','state-border'].includes(cfg.scenario)?new (require('@xterm/headless').Terminal)({cols:cfg.columns||180,rows:cfg.rows||40,allowProposedApi:true}):null;
const listeners=[];
const exit=new Promise(resolve=>ended=resolve);
function waitFor(pattern){return new Promise((resolve,reject)=>{
 if(pattern.test(raw))return resolve();
 const item={pattern,resolve:()=>{clearTimeout(timer);resolve();}};
 const timer=setTimeout(()=>{const at=listeners.indexOf(item);if(at>=0)listeners.splice(at,1);reject(Error('native output unavailable '+pattern));},12000);
 listeners.push(item);
});}
function rpc(endpoint,data){return new Promise((resolve,reject)=>{
 const socket=net.connect(endpoint),timer=setTimeout(()=>{socket.destroy();reject(Error('control timeout'));},6000);let raw='';
 socket.on('error',error=>{clearTimeout(timer);reject(error);});
 socket.on('connect',()=>socket.write(JSON.stringify(data)+'\n'));
 socket.on('data',part=>{raw+=part;if(raw.includes('\n')){clearTimeout(timer);socket.end();resolve(JSON.parse(raw.split('\n')[0]));}});
});}
function existing(file){return new Promise((resolve,reject)=>{
 if(fs.existsSync(file))return resolve();
 const watch=fs.watch(path.dirname(file),()=>{if(fs.existsSync(file)){clearTimeout(timer);watch.close();resolve();}});
 const timer=setTimeout(()=>{watch.close();reject(Error('binding unavailable'));},12000);
 if(fs.existsSync(file)){clearTimeout(timer);watch.close();resolve();}
});}
(async()=>{try{
 if(cfg.barrier)barrier=fs.openSync(cfg.barrier,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);
 child=pty.spawn(cfg.python,[cfg.entry,'claude',cfg.python,cfg.fake,cfg.sid],
    {cols:cfg.columns||100,rows:cfg.rows||30,cwd:cfg.project,env:{...process.env,TERM:'xterm-256color',HEARTING_SHELL_DIRECTORY:cfg.directory,
        HEARTING_SHELL_PYTHON:cfg.backend,NODE_PATH:process.env.NODE_PATH,LOOP_NATIVE_EXIT:cfg.exit_fifo,NODE_OPTIONS:'--require '+cfg.launcher,
        ...(cfg.scenario==='herdr'||cfg.peer?{HERDR_ENV:'1',HERDR_PANE_ID:'private:p1',HERDR_BIN_PATH:'/private/herdr',HERDR_SOCKET_PATH:path.join(process.env.XDG_CONFIG_HOME||path.join(process.env.HOME,'.config'),'herdr','herdr.sock')}:{})}});
 child.onData(text=>{if(sideTerm)sideTerm.write(text);raw+=text;if(raw.length>1024*1024)raw=raw.slice(-512*1024);
    for(let n=listeners.length-1;n>=0;n--)if(listeners[n].pattern.test(raw))listeners.splice(n,1)[0].resolve();});
 child.onExit(()=>{alive=false;ended();});
 await waitFor(/READY/);
 if(cfg.scenario==='herdr'){
   await waitFor(/\x1b\]2;OWN_NATIVE_TITLE\x07/);
   if(!fs.readFileSync('/proc/'+child.pid+'/environ').includes(Buffer.from('HERDR_AGENT=claude\0')))throw Error('foreground initial exec environment has no native hint');
 }
 const ack=Number(raw.match(/ACK_MS=([0-9.]+)/)[1]);
 if(cfg.scenario==='startup-model')await waitFor(/NATIVE_SELECTED_MODEL/);
 if(barrier!==undefined){fs.writeSync(barrier,Buffer.from('1'));fs.closeSync(barrier);barrier=undefined;}
 await existing(cfg.hostPidFile);const hostPID=Number(fs.readFileSync(cfg.hostPidFile,'utf8'));
 const file=path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json');
 await existing(file);
 // A published identity is not yet a completed display/readiness observation.
 await waitFor(/\] (?:\x1b\[[0-9;]*m)*project/);
 if(cfg.scenario!=='initial'){
   await rpc(cfg.inlet,{batch:Buffer.from('warm\r').toString('base64')});await waitFor(/DONE_warm/);
 }
 if(cfg.scenario==='mixed'){
   await rpc(cfg.inlet,{batch:Buffer.from('second\rdraft').toString('base64')});await waitFor(/DONE_second/);
 }else if(cfg.scenario==='child'){
   await rpc(cfg.inlet,{batch:Buffer.from('spawn-child\r').toString('base64')});await waitFor(/DONE_spawn-child/);
 }else if(cfg.scenario==='form'){
   await rpc(cfg.inlet,{batch:Buffer.from('form\r').toString('base64')});await waitFor(/Do you want to proceed/);
 }else if(cfg.scenario==='unpainted-draft'){
   await rpc(cfg.inlet,{batch:Buffer.from('unpainted draft').toString('base64')});
 }else if(cfg.scenario==='missing-callback'||cfg.scenario==='missing-callback-enter'){
   const cp=require('node:child_process');
   const body="import json,sys;sys.path.insert(0,"+JSON.stringify(cfg.utilities)+");import session_shell as s;s.registry_root();p=s._peer_message();text,ref=p.prepare_peer_message('PEER_NO_CALLBACK '+sys.argv[2],{'harness':'codex','session_id':'synthetic-sender'},{'harness':'claude','session_id':sys.argv[1]},defer=True);print(json.dumps(s.deliver_pending(ref,'claude',sys.argv[1])))";
   const sent=JSON.parse(cp.execFileSync(cfg.python,['-c',body,cfg.sid,cfg.scenario],{env:{...process.env},encoding:'utf8'}));
   if(!sent.ok)throw Error('synthetic admission failed '+JSON.stringify(sent));
   await rpc(cfg.inlet,{batch:Buffer.from(cfg.scenario==='missing-callback-enter'?'held human\r':'held human').toString('base64')});
   if(cfg.scenario==='missing-callback-enter')await waitFor(/DONE_held human/);
   else await waitFor(/PEER_NO_CALLBACK/);
 }
 if(cfg.scenario==='block-focus'||cfg.scenario==='block-focus-batch'){
   const file=path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json');
   const pinned=JSON.parse(fs.readFileSync(file,'utf8'));
   await rpc(cfg.inlet,{batch:Buffer.from('\x1d').toString('base64')});
   if(!(await rpc(pinned.socket,{...pinned,op:'status'})).focused)throw Error('block focus missing');
   await rpc(cfg.inlet,{batch:Buffer.from('123\t').toString('base64')});
   await rpc(cfg.inlet,{batch:Buffer.from('\x1d').toString('base64')});
   const restored=await rpc(pinned.socket,{...pinned,op:'status'});
   if(restored.focused)throw Error('block focus retained');
   if(!restored.empty)throw Error('block-only keys poisoned the native submitted frontier');
   await rpc(cfg.inlet,{batch:Buffer.from('r1w2f3\r').toString('base64')});await waitFor(/DONE_r1w2f3/);
   if(/DONE_123/.test(raw))throw Error('focused keys leaked into native');
   if(cfg.scenario==='block-focus-batch'){await rpc(cfg.inlet,{batch:Buffer.from('\x1d123\t\x1dBATCH_PRESERVED\r').toString('base64')});await waitFor(/DONE_BATCH_PRESERVED/);}
 }
 if(cfg.scenario==='native-escape-focus'){
   const file=path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json');
   const pinned=JSON.parse(fs.readFileSync(file,'utf8'));
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b').toString('base64')});
   await rpc(cfg.inlet,{batch:Buffer.from('\x1d').toString('base64')});
   if(!(await rpc(pinned.socket,{...pinned,op:'status'})).focused)throw Error('native Escape blocked the following block focus');
   await rpc(cfg.inlet,{batch:Buffer.from('\x1d').toString('base64')});
 }
 if(cfg.scenario==='side-columns'){
   await rpc(cfg.inlet,{batch:Buffer.from('side-unicode\r').toString('base64')});await waitFor(/🙂/);
   await new Promise(r=>setTimeout(r,100));await new Promise(r=>sideTerm.write('',r));
   const line=sideTerm.buffer.active.getLine(2);
   if(line.getCell(140).getChars()!=='│'||[138,139].some(x=>line.getCell(x).getChars().trim()))throw Error('side panel moved with native Unicode width');
 }
 if(cfg.scenario==='side-left'){
   // The same panel at the left: native keeps its column count and moves right.
   await rpc(cfg.inlet,{batch:Buffer.from('geometry-left\r').toString('base64')});await waitFor(/GEOMETRY_geometry-left_138x40/);
   await new Promise(r=>setTimeout(r,100));await new Promise(r=>sideTerm.write('',r));
   const row=y=>sideTerm.buffer.active.getLine(y).translateToString(true);
   if(!row(0).startsWith('┌')||!row(0).slice(0,40).includes('⚙')||row(0)[39]!=='┐')throw Error('left panel frame moved: '+row(0));
   if(row(1).slice(40,42).trim()||!/^DONE_geometry-left/.test(row(1).slice(42))&&!/DONE_geometry-left/.test(row(0).slice(42)))throw Error('native area not beside the left panel: '+row(0)+'|'+row(1));
   if(/⚙/.test(row(0).slice(42)))throw Error('panel painted twice');
 }
 if(cfg.scenario==='side-layout'){
   await rpc(cfg.inlet,{batch:Buffer.from('geometry1\r').toString('base64')});await waitFor(/GEOMETRY_geometry1_138x40/);
   child.resize(80,24);await new Promise(r=>setTimeout(r,80));
   await rpc(cfg.inlet,{batch:Buffer.from('geometry2\r').toString('base64')});await waitFor(/GEOMETRY_geometry2_80x(?:1[2-9]|2[0-4])/);
   child.resize(180,40);await new Promise(r=>setTimeout(r,80));
   await rpc(cfg.inlet,{batch:Buffer.from('geometry3\r').toString('base64')});await waitFor(/GEOMETRY_geometry3_138x40/);
 }
 if(cfg.scenario==='fleet-modal'){
   await rpc(cfg.inlet,{batch:Buffer.from('\x1df').toString('base64')});await waitFor(/OWN_FLEET_READY/);
   // The open explorer holds the outer keys: the screen answer says the native cells are not their target.
   const pinned=JSON.parse(fs.readFileSync(path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json'),'utf8'));
   const during=await rpc(pinned.socket,{...pinned,op:'screen'});
   if(!during.ok||during.focused!==true)throw Error('Fleet explorer key owner missing from screen '+JSON.stringify({ok:during.ok,focused:during.focused}));
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b').toString('base64')});
   await rpc(cfg.inlet,{batch:Buffer.from('[A\x1b[200~PASTE\x1dTAIL\x1b[201~').toString('base64')});
   await rpc(cfg.inlet,{batch:Buffer.from('\x1dAFTER_FLEET\r').toString('base64')});
   await waitFor(/DONE_AFTER_FLEET/);
   const got=fs.readFileSync(cfg.fleetRead);
   if(!got.includes(Buffer.from('\x1b[A\x1b[200~PASTE\x1dTAIL\x1b[201~')))throw Error('Fleet CSI/paste leaked or lost');
   if(got.includes(Buffer.from('AFTER_FLEET')))throw Error('native suffix remained in Fleet');
 }
 if(cfg.scenario==='action-display'||cfg.scenario==='answer-display'){
   await rpc(cfg.inlet,{batch:Buffer.from(cfg.scenario==='answer-display'?'\x1d1\r':'\x1dw').toString('base64')});
   await existing(cfg.actionReady);
   const value=JSON.parse(fs.readFileSync(cfg.routeCache,'utf8'));value.observed_at=Date.now()/1000;value.route_status['claude\0'+cfg.sid]='route 1 · DURING_ACTION';value.route_sessions['claude\0'+cfg.sid]=Date.now()/1000;
   fs.writeFileSync(cfg.routeCache+'.new',JSON.stringify(value),{mode:0o600});fs.renameSync(cfg.routeCache+'.new',cfg.routeCache);
   await waitFor(/DURING_ACTION/);
   const gate=fs.openSync(cfg.actionGate,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);fs.writeSync(gate,Buffer.from('1'));fs.closeSync(gate);
   await waitFor(cfg.scenario==='answer-display'?/ANSWER_DONE/:/ACTION_DONE/);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1dAFTER_ACTION\r').toString('base64')});await waitFor(/DONE_AFTER_ACTION/);
 }
 const peer=[];
 if(cfg.peer){
   // peer-steward prompt from an unrelated process, addressed only by Hearting records.
   const cp=require('node:child_process');
   const tags=JSON.parse(fs.readFileSync(cfg.tags,'utf8')).assignments.filter(row=>row.session_id===cfg.sid);
   for(const [label,target,extra] of cfg.peer.targets){
     const address=target==='TAG'?'['+tags[0].tag+']':target;
     const body='PEER_HELLO_'+label+'_'+cfg.peer.round;
     const out=cp.execFileSync(cfg.python,[cfg.audit,cfg.peerCount+'-'+label,cfg.steward,'prompt',address,body],
       {env:{...process.env,PATH:cfg.peer.path,...(extra||{})},encoding:'utf8'});
     const count=JSON.parse(fs.readFileSync(cfg.peerCount+'-'+label,'utf8'));
     const row={label,address,receipt:out.trim().split('\n').pop(),...count};
     if(/prompted=queued/.test(row.receipt)&&/via=session-shell/.test(row.receipt)&&cfg.peer.expectInput){
       await waitFor(new RegExp('DONE_'+body));row.typed=true;
     }
     peer.push(row);
   }
   const log=cfg.peer.herdrLog&&fs.existsSync(cfg.peer.herdrLog)?fs.readFileSync(cfg.peer.herdrLog,'utf8'):'';
   peer.push({herdr_calls:log.split('\n').filter(Boolean).length});
 }
 const displayLag=[],stateLag=[];
 if(cfg.scenario==='state-border'){
   // The border's own state word, read from the painted top border.
   const border=()=>{for(let y=0;y<sideTerm.rows;y++){const m=(sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'').match(/┌ \S+ (running|idle|waiting) /);if(m)return m[1];}return '';};
   const until=async(word,began)=>{while(Date.now()-began<4000){await new Promise(resolve=>sideTerm.write('',resolve));if(border()===word)return Date.now()-began;await new Promise(resolve=>setTimeout(resolve,20));}throw Error('the border never showed '+word+' (shows '+border()+')');};
   await until('idle',Date.now());
   // Every projection from here waits on a held backend: only the event itself can repaint.
   fs.writeFileSync(cfg.hold,'1');
   for(const [line,word] of [['turn-open','running'],['turn-close','idle']]){
     const began=Date.now();await rpc(cfg.inlet,{batch:Buffer.from(line+'\r').toString('base64')});
     stateLag.push(await until(word,began));
   }
 }
 if(cfg.scenario==='route-pages'){
   const value=JSON.parse(fs.readFileSync(cfg.routeCache,'utf8'));
   value.observed_at=Date.now()/1000;value.route_panels['claude\0'+cfg.sid]=[{
     id:'rt-detail',title:'ROUTE_PAGE',state:'working',worker_stage:'inline',stages:[{name:'inline',st:'active'}],
     harness:'codex',model:'gpt-fixture',effort:'high',ctx_pct:40,agents:2,
     visibility:{codes:{schema:'route_visibility_codes_v1',shape:'direct',next:{kind:'closed'},verification:{gate:'unproven'},quoted:Array.from({length:8},(_,i)=>['test','EVIDENCE_'+i+' '+'bounded detail '.repeat(5)])}}}];
   fs.writeFileSync(cfg.routeCache+'.new',JSON.stringify(value),{mode:0o600});fs.renameSync(cfg.routeCache+'.new',cfg.routeCache);
   await waitFor(/ROUTE_PAGE/);
   await new Promise(resolve=>sideTerm.write('',resolve));
   // A press on a route row opens it in place and closes it again. Display only:
   // like any outer input it invalidates the in-flight observation, then `empty`
   // and focus settle back to what they were (PG-50 class); no native draft.
   const pinnedRoute=JSON.parse(fs.readFileSync(file,'utf8'));
   const before=await rpc(pinnedRoute.socket,{...pinnedRoute,op:'status'});
   let row;
   for(let y=0;y<sideTerm.rows;y++){
     const line=sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'';
     const x=line.indexOf('ROUTE_PAGE');if(x>=0&&!line.includes('╭─ route'))row={x:x+2,y:y+1};
   }
   if(!row)throw Error('route row absent');
   for(const marker of [/\+2 agent/,/ROUTE_PAGE/]){
     await rpc(cfg.inlet,{batch:Buffer.from(`\x1b[<0;${row.x};${row.y}M\x1b[<0;${row.x};${row.y}m`).toString('base64')});
     await waitFor(marker);
     let after;
     for(let n=0;n<40;n++){after=await rpc(pinnedRoute.socket,{...pinnedRoute,op:'status'});if(after.empty===before.empty)break;await new Promise(resolve=>setTimeout(resolve,50));}
     for(const key of ['empty','focused','state'])if(after[key]!==before[key])throw Error('a route row press changed '+key+' '+JSON.stringify([before,after]));
   }
   let point;
   for(let n=0;n<60&&!point;n++){
   await new Promise(resolve=>setTimeout(resolve,50));await new Promise(resolve=>sideTerm.write('',resolve));
   for(let y=0;y<sideTerm.rows;y++){
     const line=sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y);
     // The route section's heading opens the evidence pages; its rows open in place.
     if(!line?.translateToString().includes('╭─ route 1'))continue;
     let s='';for(let x=0;x<sideTerm.cols;x++){const c=line.getCell(x);if(!c||!c.getWidth())continue;s+=c.getChars()||' ';if(s.endsWith('╭─ r'))point={x:x+1,y:y+1};}
   }
   }
   if(!point)throw Error('route detail target absent\n'+Array.from({length:sideTerm.rows},(_,y)=>sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()).join('\n'));
   await rpc(cfg.inlet,{batch:Buffer.from(`\x1b[<0;${point.x};${point.y}M\x1b[<0;${point.x};${point.y}m`).toString('base64')});
   await waitFor(/PgUp\/PgDn/);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b[6~').toString('base64')});await waitFor(/‹ 2\//);
   // A collapsed panel shows no page, so its page keys and Esc reach the native.
   await rpc(pinnedRoute.socket,{...pinnedRoute,op:'panel',expanded:false});
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b[6~HIDDEN_PGDN\r').toString('base64')});await waitFor(/NATIVE_ESC_\^\[6~HIDDEN_PGDN/);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b').toString('base64')});
   await rpc(cfg.inlet,{batch:Buffer.from('HIDDEN_ESC\r').toString('base64')});await waitFor(/NATIVE_ESC_\^HIDDEN_ESC/);
   // Shown again, the same page takes them again.
   raw='';
   await rpc(pinnedRoute.socket,{...pinnedRoute,op:'panel',expanded:true});await waitFor(/PgUp\/PgDn/);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b[5~').toString('base64')});await waitFor(/‹ 1\//);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1dAFTER_DETAIL\r').toString('base64')});await waitFor(/DONE_AFTER_DETAIL/);
   if(/NATIVE_ESC_[^\r]*\^\[5~/.test(raw))throw Error('a shown page let PgUp reach the native');
 }
 // Registry publication precedes the first complete display observation.
 if(cfg.scenario==='display')await waitFor(/\] (?:\x1b\[[0-9;]*m)*project/);
 if(cfg.scenario==='display')for(let index=0;index<4;index++){
   const title='event-title-'+index,before=process.hrtime.bigint();
   const projection=JSON.parse(fs.readFileSync(cfg.routeCache,'utf8'));projection.observed_at=Date.now()/1000;projection.route_status['claude\0'+cfg.sid]='route 1 · '+title;projection.route_sessions['claude\0'+cfg.sid]=Date.now()/1000;
   fs.writeFileSync(cfg.routeCache+'.new',JSON.stringify(projection),{mode:0o600});fs.renameSync(cfg.routeCache+'.new',cfg.routeCache);
   await waitFor(new RegExp(title));displayLag.push(Number(process.hrtime.bigint()-before)/1e6);
 }
 const binding=JSON.parse(fs.readFileSync(file,'utf8'));
 const status=await rpc(binding.socket,{...binding,op:'status'});
 console.log(JSON.stringify({ack_ms:ack,status,display_lag_ms:displayLag,state_lag_ms:stateLag,peer}));
 }catch(error){console.error(error.stack);process.exitCode=1;}
 finally{
   if(fs.existsSync(cfg.diagnostic)){console.error(fs.readFileSync(cfg.diagnostic,'utf8'));process.exitCode=1;}
   if(barrier!==undefined){fs.writeSync(barrier,Buffer.from('1'));fs.closeSync(barrier);}
   if(cfg.hold){try{fs.unlinkSync(cfg.hold);}catch{}const gate=fs.openSync(cfg.holdGate,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);fs.writeSync(gate,Buffer.from('1111'));fs.closeSync(gate);}
   if(cfg.actionGate){const gate=fs.openSync(cfg.actionGate,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);fs.writeSync(gate,Buffer.from('1'));fs.closeSync(gate);}
   if(child&&alive){
     const exitFD=fs.openSync(cfg.exit_fifo,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);fs.writeSync(exitFD,Buffer.from('q'));fs.closeSync(exitFD);
     await new Promise((resolve,reject)=>{
     const timeout=setTimeout(()=>reject(Error('owned native child did not exit')),5000);
     exit.then(()=>{clearTimeout(timeout);resolve();});
   });}
 }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


class SessionShellPTYTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node=shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary=tempfile.TemporaryDirectory(prefix='hearting-shell-loop-',dir='/var/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root=Path(cls.temporary.name)
        spec=importlib.util.spec_from_file_location('shell_loop_isolation',ROOT/'tools/run-tests.py')
        runner=importlib.util.module_from_spec(spec);sys.modules[spec.name]=runner;spec.loader.exec_module(runner)
        cls.env=runner.build_isolated_env(cls.root,repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR']=str(cls.root/'native-claude')
        policy=Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME'])/'.config')/'hearting/session-shell.json'
        policy.parent.mkdir(parents=True,exist_ok=True);policy.write_text('{"enabled":true}');policy.chmod(0o600)
        cls.env['npm_config_cache']=str(cls.root/'npm-cache')
        cls.env['npm_config_devdir']=str(cls.root/'node-gyp')
        cls.env['npm_config_userconfig']=str(cls.root/'npmrc');(cls.root/'npmrc').write_text('')
        sys.path[:0]=[str(ROOT/'tools/install'),str(ROOT/'utilities')]
        import session_shell_install,session_shell
        with mock.patch.dict(os.environ,cls.env,clear=True):
            assets=session_shell_install.prepare_assets()
            cls.registry=Path(session_shell.registry_root())
        cls.env['NODE_PATH']=str(assets/'node_modules')
        (cls.registry/'claude').mkdir(parents=True,exist_ok=True)
        cls.fake=cls.root/'native.py';cls.fake.write_text(FAKE_NATIVE)
        cls.launcher=cls.root/'host.cjs';cls.launcher.write_text(HOST_LAUNCHER)
        cls.driver=cls.root/'driver.cjs';cls.driver.write_text(DRIVER)
        cls.project=cls.root/'project';cls.project.mkdir()

    def run_loop(self,scenario,*,blocked_backend=False,peer=None,case=None):
        directory=self.root/('case-'+(case or scenario));directory.mkdir(mode=0o700)
        exit_fifo=directory/'native-exit';os.mkfifo(exit_fifo,0o600)
        sid='00000000-0000-4000-8000-000000000052'
        import time
        cache=Path(self.env['XDG_STATE_HOME'])/'hearting/human-waits'
        cache.mkdir(mode=0o700,parents=True,exist_ok=True);cache.chmod(0o700)
        key='claude\0'+sid;now=time.time()
        (cache/'projection.json').write_text(json.dumps({'schema_version':3,'observed_at':now,'items':[],
            'by_session':{},'total':0,'older':0,'complete':True,'route_panels':{key:[]},'route_status':{key:''},'route_sessions':{key:now}}))
        (cache/'projection.json').chmod(0o600)
        backend=sys.executable;barrier=None
        if blocked_backend:
            barrier=directory/'barrier';os.mkfifo(barrier,0o600)
            backend=str(directory/'python')
            Path(backend).write_text('#!'+sys.executable+'\nimport os,runpy,sys\nf=os.open('+repr(str(barrier))+',os.O_RDONLY)\nos.read(f,1);os.close(f)\nsys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")\n')
            Path(backend).chmod(0o700)
        if scenario in {'action-display','answer-display'}:
            backend=directory/'action-python';gate=directory/'action-gate';ready=directory/'action-ready';os.mkfifo(gate,0o600)
            backend.write_text('#!'+sys.executable+'\nimport os,sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(ROOT/'utilities'))+')\nimport session_shell as m\ndef blocked(binding,request):\n Path('+repr(str(ready))+').touch()\n fd=os.open('+repr(str(gate))+',os.O_RDWR);os.read(fd,1);os.close(fd)\n return {"ok":True,"lines":["ACTION_DONE"]}\nm._block_action=blocked\nraise SystemExit(m._backend())\n')
            if scenario=='answer-display':
                setup=('original=m.panel_projection\ndef projection(*args,**kwargs):\n'
                       ' value=original(*args,**kwargs)\n'
                       ' value["questions"]=[{"id":"owned-question","revision":"0"*64,"answerable":True,"questions":[{"id":"q","question":"Owned question","choices":["Yes","No"]}]}]\n'
                       ' return value\nm.panel_projection=projection\nm._block_answer=blocked\n')
                backend.write_text(backend.read_text().replace('m._block_action=blocked\n',setup)
                                   .replace('{"ok":True,"lines":["ACTION_DONE"]}',
                                            '{"ok":False,"reason":"ANSWER_DONE"}'))
            backend.chmod(0o700)
        if scenario=='state-border':
            backend=directory/'held-python';hold=directory/'hold';hold_gate=directory/'hold-gate';os.mkfifo(hold_gate,0o600)
            backend.write_text('#!'+sys.executable+'\nimport os,sys\nsys.path.insert(0,'+repr(str(ROOT/'utilities'))+')\nimport session_shell as m\n'
                               'original=m.panel_projection\ndef projection(*args,**kwargs):\n'
                               ' if os.path.exists('+repr(str(hold))+'):\n'
                               '  fd=os.open('+repr(str(hold_gate))+',os.O_RDWR);os.read(fd,1);os.close(fd)\n'
                               ' return original(*args,**kwargs)\n'
                               'm.panel_projection=projection\nraise SystemExit(m._backend())\n')
            backend.chmod(0o700)
        config={'directory':str(directory),'host':str(ROOT/'utilities/session-shell/host.cjs'),
                'startFile':str(directory/'started'),'inlet':str(directory/'input.sock'),
                'entry':str(ROOT/'utilities/session-shell/entry.py'),'hostPidFile':str(directory/'host-pid'),
                'diagnostic':str(directory/'display-errors'),
                'launcher':str(self.launcher),'python':sys.executable,'fake':str(self.fake),'sid':sid,
                'project':str(self.project),'backend':str(backend),'barrier':str(barrier) if barrier else None,
                'registry':str(self.registry),'scenario':scenario,'utilities':str(ROOT/'utilities'),
                'exit_fifo':str(exit_fifo),'routeCache':str(cache/'projection.json')}
        if scenario in {'action-display','answer-display'}:config.update(actionGate=str(gate),actionReady=str(ready))
        if scenario=='state-border':config.update(hold=str(hold),holdGate=str(hold_gate))
        if peer is not None:
            audit=directory/'audit.py'
            # Counts every subprocess the sender starts and its wall time; nothing else changes.
            audit.write_text('import json,runpy,sys,time\nout=sys.argv[1];sys.argv=sys.argv[2:]\n'
                'calls=[]\nsys.addaudithook(lambda e,a:calls.append(str(a[1][0]) if a[1] else "?") if e=="subprocess.Popen" else None)\n'
                'began=time.monotonic()\ntry:\n runpy.run_path(sys.argv[0],run_name="__main__")\nexcept SystemExit:\n pass\n'
                'finally:\n open(out,"w").write(json.dumps({"subprocesses":len(calls),"programs":sorted(set(calls)),"ms":round((time.monotonic()-began)*1000)}))\n')
            config.update(peer=peer,audit=str(audit),peerCount=str(directory/'count'),
                          steward=str(ROOT/'utilities/peer-steward.py'),
                          tags=str(Path(self.env['XDG_STATE_HOME'])/'agent-fleet/tag-assignments.json'))
        if scenario in {'side-layout','side-columns','side-left','state-border'}:
            config.update(columns=180,rows=40)
        if scenario=='route-pages':config.update(columns=80,rows=24)
        if scenario=='fleet-modal':
            fleet=directory/'fleet.py';read=directory/'fleet-read'
            fleet.write_text("import os,tty\nfrom pathlib import Path\ntty.setraw(0)\nprint('OWN_FLEET_READY\\r\\n',end='',flush=True)\nwhile True:\n data=os.read(0,65536)\n with open("+repr(str(read))+",'ab') as f:f.write(data)\n if b'q' in data:break\n")
            config.update(fakeFleet=str(fleet),fleetRead=str(read))
        from fleet import titles
        with mock.patch.dict(os.environ,self.env,clear=True):
            title=Path(titles.sidecar_path(sid,'claude'))
        title.parent.mkdir(parents=True,exist_ok=True);config['title']=str(title)
        configfile=directory/'config.json';configfile.write_text(json.dumps(config))
        env=dict(self.env,LOOP_CONFIG=str(configfile))
        policy=Path(self.env.get('XDG_CONFIG_HOME') or Path(self.env['HOME'])/'.config')/'hearting/session-shell.json'
        previous_policy=policy.read_bytes()
        policy.write_text(json.dumps({'enabled':True,'layout':'side','side_width':40,'side_min_columns':132,
                                      **({'position':'left'} if scenario=='side-left' else {})}) if scenario in {'side-layout','side-columns','side-left','state-border'} else '{"enabled":true}')
        self.addCleanup(policy.write_bytes,previous_policy)
        child=subprocess.Popen([self.node,str(self.driver)],env=env,cwd=self.project,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        birth=Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
        try:
            stdout,stderr=child.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            fd=os.pidfd_open(child.pid)
            try:
                current=Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
                if child.pid>1 and current==birth:signal.pidfd_send_signal(fd,signal.SIGTERM)
            finally:os.close(fd)
            child.communicate(timeout=5)
            raise
        self.assertEqual(child.returncode,0,stderr)
        return json.loads(stdout.splitlines()[-1])

    def peer_path(self,directory,*,herdr):
        # The sender's PATH: no herdr at all, or a private herdr that only records its argv.
        bin_dir=self.root/('peer-bin-'+directory);bin_dir.mkdir(exist_ok=True)
        log=self.root/('peer-herdr-'+directory+'.log')
        if herdr:
            fake=bin_dir/'herdr'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> '+repr(str(log))+'\nexit 1\n');fake.chmod(0o700)
        dirs=[str(bin_dir)]+[d for d in self.env['PATH'].split(os.pathsep)
                              if d and not os.access(os.path.join(d,'herdr'),os.X_OK)]
        return os.pathsep.join(dirs),str(log)

    def peer_rows(self,result):
        rows=[row for row in result['peer'] if 'label' in row]
        return {row['label']:row for row in rows},result['peer'][-1]['herdr_calls']

    def test_peer_prompt_reaches_an_idle_shell_by_session_number_and_window_without_herdr(self):
        for herdr in (False,True):
            with self.subTest(herdr=herdr):
                path,log=self.peer_path('idle-'+str(herdr),herdr=herdr)
                sid='00000000-0000-4000-8000-000000000052'
                result=self.run_loop('warm',case='peer-idle-'+str(herdr),peer={
                    'path':path,'herdrLog':log,'expectInput':True,'round':'idle'+str(herdr),
                    'targets':[['sid','claude:'+sid],['tag','TAG'],['window','private:p1'],
                               ['other_server','private:p1',{'AGENT_HERDR_SESSION':'other-server'}],
                               # Same server name, another Herdr configuration: never this window.
                               ['other_namespace','private:p1',
                                {'HERDR_CONFIG_PATH':str(self.root/'other-herdr'/'config.toml')}]]})
                rows,herdr_calls=self.peer_rows(result)
                for label in ('sid','tag','window'):
                    self.assertIn('prompted=queued',rows[label]['receipt'])
                    self.assertIn('via=session-shell',rows[label]['receipt'])
                    self.assertTrue(rows[label].get('typed'),rows[label])
                    self.assertEqual(rows[label]['subprocesses'],0,rows[label])
                for label in ('other_server','other_namespace'):
                    self.assertIn('prompted=unverified reason=target-pane-unresolved',rows[label]['receipt'])
                self.assertEqual(herdr_calls,0)

    def test_peer_prompt_leaves_a_form_or_draft_untouched_and_pending(self):
        for scenario in ('form','unpainted-draft'):
            with self.subTest(scenario=scenario):
                path,log=self.peer_path(scenario,herdr=True)
                result=self.run_loop(scenario,case='peer-'+scenario,peer={
                    'path':path,'herdrLog':log,'expectInput':False,'round':scenario,
                    'targets':[['held','claude:00000000-0000-4000-8000-000000000052']]})
                rows,herdr_calls=self.peer_rows(result)
                self.assertIn('prompted=queued',rows['held']['receipt'])
                self.assertIn('reason=not-idle-empty',rows['held']['receipt'])
                self.assertEqual(herdr_calls,0)

    def test_native_escape_closes_a_modal_without_disabling_the_next_block_focus(self):
        self.assertFalse(self.run_loop('native-escape-focus')['status']['focused'])

    def test_side_column_is_fixed_when_native_and_outer_terminal_disagree_about_emoji_width(self):
        self.run_loop('side-columns')

    def test_side_layout_resizes_the_same_native_pty_and_falls_back_when_narrow(self):
        self.run_loop('side-layout')

    def test_a_left_panel_keeps_the_native_columns_and_moves_native_right(self):
        self.run_loop('side-left')

    def test_foreground_exec_hint_and_native_title_survive_the_real_pty_loop(self):
        self.assertTrue(self.run_loop('herdr')['status']['ok'])

    def test_block_focus_keeps_digits_tabs_and_action_keys_out_of_native_only_while_focused(self):
        result=self.run_loop('block-focus')
        self.assertFalse(result['status']['focused'])

    def test_focus_changes_in_one_input_packet_preserve_following_native_bytes(self):
        result=self.run_loop('block-focus-batch')
        self.assertFalse(result['status']['focused'])

    def test_fleet_modal_preserves_csi_paste_and_following_native_input(self):
        result=self.run_loop('fleet-modal')
        self.assertFalse(result['status']['focused'])

    def test_a_blocked_read_action_does_not_delay_event_driven_values(self):
        result=self.run_loop('action-display')
        self.assertFalse(result['status']['focused'])

    def test_the_border_state_follows_the_event_while_the_projection_is_held(self):
        # The border's running/idle is the host's own state: it is painted when the
        # native event arrives, not after a (here held) display projection returns.
        result=self.run_loop('state-border')
        print(json.dumps({'event':'session-shell-border-state','lag_ms':result['state_lag_ms']}))
        self.assertEqual(len(result['state_lag_ms']),2)
        self.assertLess(max(result['state_lag_ms']),1000)

    def test_a_blocked_answer_keeps_projection_and_native_keyboard_responsive(self):
        self.assertFalse(self.run_loop('answer-display')['status']['focused'])

    def test_native_start_ack_does_not_wait_for_a_blocked_display_backend(self):
        result=self.run_loop('start',blocked_backend=True)
        self.assertLess(result['ack_ms'],250)

    def test_route_detail_pages_keep_navigation_out_of_native_input(self):
        result=self.run_loop('route-pages')
        self.assertTrue(result['status']['alive'])

    def test_native_model_paints_before_the_optional_backend_is_released(self):
        result=self.run_loop('startup-model',blocked_backend=True)
        self.assertLess(result['ack_ms'],250)

    def test_enter_and_following_draft_in_one_chunk_stays_nonempty(self):
        result=self.run_loop('mixed')
        self.assertEqual(result['status']['state'],'idle')
        self.assertFalse(result['status']['empty'])

    def test_headless_descendant_cannot_replace_the_native_session_identity(self):
        result=self.run_loop('child')
        self.assertTrue(result['status']['ok'])
        self.assertEqual(result['status']['sid'],'00000000-0000-4000-8000-000000000052')

    def test_record_changes_update_the_line_without_waiting_for_a_collection_tick(self):
        result=self.run_loop('display')
        print(json.dumps({'event':'session-shell-record-update','lag_ms':result['display_lag_ms']}))
        self.assertEqual(len(result['display_lag_ms']),4)
        self.assertLess(max(result['display_lag_ms']),500)

    def test_new_idle_session_has_a_verified_empty_composer_before_its_first_prompt(self):
        result=self.run_loop('initial')
        self.assertEqual(result['status']['state'],'idle')
        self.assertTrue(result['status']['empty'])

    def test_a_form_appearing_after_stop_is_not_an_empty_ready_composer(self):
        result=self.run_loop('form')
        self.assertFalse(result['status']['empty'])

    def test_a_draft_waiting_for_native_paint_is_not_proved_empty_by_the_old_screen(self):
        result=self.run_loop('unpainted-draft')
        self.assertFalse(result['status']['empty'])

    def test_a_held_enter_still_advances_the_existing_input_frontier(self):
        result=self.run_loop('missing-callback-enter')
        self.assertTrue(result['status']['empty'],result['status'])

    def test_missing_native_callback_cannot_leave_human_keyboard_bytes_held(self):
        result=self.run_loop('missing-callback')
        self.assertEqual(result['status']['held_bytes'],0)


if __name__=='__main__':unittest.main()
