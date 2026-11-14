"""Real PTY/control loop with synthetic native UI and private dependency assets."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
# `live-*` lines act like Claude itself: the REPL writes its own UI state into its
# session record as it paints (busy, waiting with a dialog, idle), while each hook is
# its own process (a new python that imports what the real hook imports) reporting
# over the socket some time later.
# Each hook lands 1.5 s late (review R5): a state the hooks drew would cross the test's
# 400 ms bound, so the bound holds only while the record itself is read.
HOOK=(
 "import json,os,socket,sys,time\n"
 "time.sleep(1.5)\n"
 "began=time.monotonic()\n"
 "kind,sid,pid,start,log=sys.argv[1:6]\n"
 "utilities=os.environ['LOOP_UTILITIES'];sys.path[:0]=[utilities,os.path.join(utilities,'..','tools')]\n"
 "import session_shell\n"
 "if kind=='prompt':import session_tidy\n"
 "if kind in('blocked','working'):import fleet.interaction\n"
 "req={'op':'event','event':kind,'harness':'claude','sid':sid,'pid':int(pid),'start':start,"
 "'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}\n"
 "c=socket.socket(socket.AF_UNIX);c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET'])\n"
 "c.sendall((json.dumps(req)+'\\n').encode());c.recv(8192);c.close()\n"
 "open(log,'a').write(json.dumps([kind,round((time.monotonic()-began)*1000,1)])+'\\n')\n")
def hook(kind):
    raw=open('/proc/self/stat').read(); start=raw.rsplit(') ',1)[1].split()[19]
    subprocess.Popen([sys.executable,'-c',HOOK,kind,sid,str(os.getpid()),start,os.environ.get('LOOP_HOOK_LOG','/dev/null')],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def record(status,waiting=None,**identity):
    raw=open('/proc/self/stat').read(); start=raw.rsplit(') ',1)[1].split()[19]
    path=Path(os.environ['CLAUDE_CONFIG_DIR'])/'sessions'/(str(os.getpid())+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'pid':os.getpid(),'procStart':start,'sessionId':sid,'status':status,
        **({'waitingFor':waiting} if waiting else {}),**identity,'statusUpdatedAt':int(time.time()*1000)}))
later=[]
def after(seconds,action):later.append((time.monotonic()+seconds,action))
def screen(text):print('\x1b[2J\x1b[H'+text+'\r\n❯ \r\n──────────────────\r\n',end='',flush=True)
def live(line):
    if line.startswith('live-turn-'):
        n=line[10:];record('busy');screen('✻ Working… (esc to interrupt) LIVE_BUSY_'+n);hook('prompt')
        def end():record('idle');screen('LIVE_IDLE_'+n);hook('stop')
        after(0.7,end)
    elif line.startswith('live-perm-'):
        n=line[10:];record('busy');screen('✻ Working… LIVE_BUSY_'+n);hook('prompt')
        def form():record('waiting','permission prompt');print('\x1b[2J\x1b[HDo you want to proceed?\r\n❯ 1. Yes\r\n  2. No\r\nLIVE_FORM_'+n+'\r\n',end='',flush=True);hook('blocked')
        after(0.3,form)
    elif line.startswith('live-yes-'):
        n=line[9:];record('busy');screen('✻ Running the tool… LIVE_APPROVED_'+n)
        def done():hook('working')
        def end():record('idle');screen('LIVE_IDLE_'+n);hook('stop')
        after(1.0,done);after(1.3,end)
    elif line.startswith('live-menu-'):
        # An Enter the native does not take as a prompt, opening a menu dialog (Claude
        # writes `waiting` with `dialog open`): no turn and no answer wait.
        record('waiting','dialog open');screen('LIVE_MENU_'+line[10:])
        after(0.6,lambda:record('idle'))
    elif line.startswith('live-foreign-'):
        # A record written for another session, then for another process: nothing changes.
        n=line[13:];record('busy',sessionId=sid+'-other')
        def second():record('busy',procStart='1');screen('LIVE_FOREIGN_'+n)
        after(0.05,second);after(0.6,lambda:record('idle'))
while True:
    wait=max(0,min(due for due,_ in later)-time.monotonic()) if later else None
    readable,_,_=select.select([0,exit_fd],[],[],wait)
    for item in [item for item in later if item[0]<=time.monotonic()]:later.remove(item);item[1]()
    if not readable:continue
    if exit_fd in readable:break
    data=os.read(0,65536)
    if not data or data==b'\x03':break
    pending+=data.decode()
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        line=line.replace('\x1b[200~','').replace('\x1b[201~','')
        if line.startswith('live-'):live(line);continue
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
        if line=='mouse-on':print('\x1b[?1000h\x1b[?1006h',end='',flush=True)
        if line=='paint-bg':print('\x1b[48;5;232m\x1b[2KPAINTED_BG\x1b[0m\r\n'
                                  '\x1b[48;5;24m\x1b[2KINVERSE_EDGE\x1b[138G\x1b[7;38;5;88;48;5;52mX\x1b[0m\r\n'
                                  '\x1b[48;5;24m\x1b[2KINVERSE_PLAIN\x1b[138G\x1b[7;48;5;52mX\x1b[0m\r\n',end='',flush=True)
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
const sideTerm=['side-columns','route-pages','side-left','state-border','late-route','startup-state','live-state','rule-motion','native-bg','request-panel'].includes(cfg.scenario)?new (require('@xterm/headless').Terminal)({cols:cfg.columns||180,rows:cfg.rows||40,allowProposedApi:true}):null;
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
    {cols:cfg.columns||130,rows:cfg.rows||30,cwd:cfg.project,env:{...process.env,TERM:'xterm-256color',HEARTING_SHELL_DIRECTORY:cfg.directory,
        HEARTING_SHELL_PYTHON:cfg.backend,NODE_PATH:process.env.NODE_PATH,LOOP_NATIVE_EXIT:cfg.exit_fifo,LOOP_UTILITIES:cfg.utilities,LOOP_HOOK_LOG:path.join(cfg.directory,'hooks.log'),NODE_OPTIONS:'--require '+cfg.launcher,
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
 await waitFor(/project\/|(?:\]|REPO +) (?:\x1b\[[0-9;]*m)*project/);
 const startup={};
 if(cfg.scenario==='startup-state'){
   // No key, no status request (which would run the readiness observation): the
   // border's word right after start comes from the native's own session record.
   const word=()=>{for(let y=0;y<sideTerm.rows;y++){const m=(sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'').match(/┌ ([⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●◑]) /);if(m)return m[1]==='●'?'idle':m[1]==='◑'?'waiting':'running';}return '';};
   const began=Date.now();
   while(Date.now()-began<8000&&!startup.word){await new Promise(resolve=>sideTerm.write('',resolve));startup.word=word();if(!startup.word)await new Promise(resolve=>setTimeout(resolve,20));}
   startup.ms=Date.now()-began;
 }
 const liveState={};
 if(cfg.scenario==='live-state'){
   // Spec 11-12 rule 6: how soon the panel's own state follows the native screen.
   // Times are taken where they happen: a native marker when its bytes reach this
   // terminal, the panel's state when the painted border first shows it.
   const word=()=>{for(let y=0;y<sideTerm.rows;y++){const m=(sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'').match(/┌ ([⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●◑]) /);if(m)return m[1]==='●'?'idle':m[1]==='◑'?'waiting':'running';}return '';};
   const now=()=>Number(process.hrtime.bigint())/1e6,sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
   const seen={};child.onData(text=>{for(const m of String(text).matchAll(/LIVE_[A-Z]+_\d+/g))if(!(m[0] in seen))seen[m[0]]=now();});
   const flush=()=>new Promise(resolve=>sideTerm.write('',resolve));
   const until=async want=>{const began=now();while(now()-began<6000){await flush();if(word()===want)return now();await sleep(1);}return null;};
   const shown=async name=>{const began=now();while(!(name in seen)){if(now()-began>6000)throw Error('never shown '+name);await sleep(1);}return seen[name];};
   const send=text=>rpc(cfg.inlet,{batch:Buffer.from(text+'\r').toString('base64')});
   const since=(a,b)=>a===null?null:Math.round((a-b)*10)/10;
   for(const key of ['submit','busy','idle','waiting','approve','approveIdle'])liveState[key]=[];
   liveState.false_running=0;liveState.false_back=[];liveState.false_waiting=0;liveState.foreign_changed=0;
   // One ordinary turn first (in-process events), so every version starts from idle.
   await send('warm');await waitFor(/DONE_warm/);
   if(await until('idle')===null)throw Error('never idle before the turns: '+word());
   for(let n=0;n<10;n++){
     let began=now();await send('live-turn-'+n);
     const running=await until('running');
     liveState.submit.push(since(running,began));liveState.busy.push(since(running,await shown('LIVE_BUSY_'+n)));
     liveState.idle.push(since(await until('idle'),await shown('LIVE_IDLE_'+n)));
     for(const key of Object.keys(seen))delete seen[key];
     await send('live-perm-'+n);await shown('LIVE_BUSY_'+n);
     liveState.waiting.push(since(await until('waiting'),await shown('LIVE_FORM_'+n)));
     began=now();await send('live-yes-'+n);
     liveState.approve.push(since(await until('running'),began));
     liveState.approveIdle.push(since(await until('idle'),await shown('LIVE_IDLE_'+n)));
     // An Enter the native does not take as a prompt: the panel must not claim a turn.
     for(const key of Object.keys(seen))delete seen[key];
     began=now();await send('live-menu-'+n);await shown('LIVE_MENU_'+n);
     let wrong=null,waited=false;while(now()-began<400){await flush();if(word()==='waiting'&&!waited){waited=true;liveState.false_waiting++;}
       if(word()==='running'&&wrong===null){wrong=now();liveState.false_running++;}if(wrong!==null&&word()==='idle'){liveState.false_back.push(Math.round(now()-wrong));break;}await sleep(2);}
     for(const key of Object.keys(seen))delete seen[key];
     await sleep(400);
     // A record of another session or process (review R5): the panel keeps its state.
     await send('live-foreign-'+n);await shown('LIVE_FOREIGN_'+n);began=now();
     let moved=false;while(now()-began<400){await flush();if(word()!=='idle')moved=true;await sleep(2);}
     if(moved)liveState.foreign_changed++;
     for(const key of Object.keys(seen))delete seen[key];
     await sleep(350);
   }
   await sleep(2000);   // the last hooks land 1.5 s late
   liveState.hooks=fs.existsSync(path.join(cfg.directory,'hooks.log'))?fs.readFileSync(path.join(cfg.directory,'hooks.log'),'utf8').trim().split('\n').map(line=>JSON.parse(line)):[];
 }
 if(!['initial','startup-state','live-state'].includes(cfg.scenario)){
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
   // Spec rule 49 (a): the gear stands on the panel's last row inside, the fold button `«` of a left panel before it.
   if(!row(0).startsWith('┌')||row(0).slice(0,40).includes('⚙')||row(0)[39]!=='┐')throw Error('left panel frame moved: '+row(0));
   if(!/«   ⚙ │$/.test(row(38).slice(0,40)))throw Error('left panel last row: '+row(38));
   if(row(1).slice(40,42).trim()||!/^DONE_geometry-left/.test(row(1).slice(42))&&!/DONE_geometry-left/.test(row(0).slice(42)))throw Error('native area not beside the left panel: '+row(0)+'|'+row(1));
   if(/⚙/.test(row(38).slice(42)))throw Error('panel painted twice');
   // #1586 review follow-up 7, spec rule 49 (c): folded, a left panel's strip stands under the native
   // area, which takes the whole width again from the first column (four rows fewer): a press in the
   // native area reaches the native at the same column, a press on the strip opens the panel.
   const pinnedLeft=JSON.parse(fs.readFileSync(path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json'),'utf8'));
   await rpc(pinnedLeft.socket,{...pinnedLeft,op:'panel',expanded:false});
   await rpc(cfg.inlet,{batch:Buffer.from('mouse-on\r').toString('base64')});await waitFor(/DONE_mouse-on/);
   await rpc(cfg.inlet,{batch:Buffer.from('geometry-strip\r').toString('base64')});await waitFor(/GEOMETRY_geometry-strip_180x36/);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b[<0;12;5M\x1b[<0;12;5mMOUSE_AT\r').toString('base64')});
   await waitFor(/NATIVE_ESC_\^\[<0;12;5M\^\[<0;12;5mMOUSE_AT/);
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b[<0;4;38M\x1b[<0;4;38m').toString('base64')});await new Promise(r=>setTimeout(r,150));
   await rpc(cfg.inlet,{batch:Buffer.from('geometry-opened\r').toString('base64')});await waitFor(/GEOMETRY_geometry-opened_138x40/);
 }
 if(cfg.scenario==='side-layout'){
   await rpc(cfg.inlet,{batch:Buffer.from('geometry1\r').toString('base64')});await waitFor(/GEOMETRY_geometry1_138x40/);
   const press=async(x,y)=>{await rpc(cfg.inlet,{batch:Buffer.from(`\x1b[<0;${x};${y}M\x1b[<0;${x};${y}m`).toString('base64')});await new Promise(r=>setTimeout(r,80));};
   const geometry=async(name,size)=>{await rpc(cfg.inlet,{batch:Buffer.from(name+'\r').toString('base64')});await waitFor(new RegExp('GEOMETRY_'+name+'_'+size));};
   const resize=async(c,r)=>{child.resize(c,r);await new Promise(r=>setTimeout(r,80));};
   // Spec rule 49 (d): where the open panel would leave the native under 60 columns it folds by
   // itself into the strip under the native area (the native keeps the whole width, four rows
   // fewer: rule 49 (c)), and it opens again three columns past it.
   await resize(102,24);await geometry('geometry2','60x24');
   await resize(101,24);await geometry('geometry2a','101x20');
   await resize(104,24);await geometry('geometry2b','104x20');
   await resize(105,24);await geometry('geometry2c','63x24');
   await resize(80,24);await geometry('geometry2d','80x20');
   await resize(180,40);await geometry('geometry3','138x40');
   // The fold button `»` (the last row inside, three cells left of the gear) folds by hand; a hand fold stays folded when wider.
   await press(174,39);await geometry('geometry4','180x36');
   await resize(200,40);await geometry('geometry5','200x36');
   // Any cell of the strip (the window's last four rows) opens it.
   await press(198,38);await geometry('geometry6','158x40');
   // Opened by hand it stays open however the window is resized: beside while the native keeps 48
   // columns (here where it would fold by itself), under the native area narrower, and beside
   // again once the width returns.
   await resize(100,40);await geometry('geometry7','58x40');
   await resize(89,40);await geometry('geometry8','89x(?:[1-9]|[1-3]\\d)(?!\\d)');
   await resize(112,40);await geometry('geometry9','70x40');
   // A window of nine cells or fewer leaves the native a cell, and the open panel stands beside
   // again once the window widens.
   child.resize(9,40);await new Promise(r=>setTimeout(r,150));
   child.resize(180,40);await new Promise(r=>setTimeout(r,80));await geometry('geometry10','138x40');
   // #1586 review follow-up 6: folding by hand while the settings editor is open closes the
   // editor first; the strip stands and the keys reach the native.
   const pinnedFold=JSON.parse(fs.readFileSync(path.join(cfg.registry,'claude','session-shell-'+hostPID+'.json'),'utf8'));
   raw='';await press(178,39);await waitFor(/rule motion/);
   await rpc(pinnedFold.socket,{...pinnedFold,op:'panel',expanded:false});await new Promise(r=>setTimeout(r,150));
   await geometry('geometry11','180x36');
   const after=await rpc(pinnedFold.socket,{...pinnedFold,op:'status'});
   if(after.expanded!==false||after.focused!==false)throw Error('the editor outlived the fold '+JSON.stringify(after));
   await press(177,38);await geometry('geometry12','138x40');
   // #1586 review follow-up 3: a width set in the settings editor counts as opening the panel by
   // hand (as a drag does): applying a wider panel that would fold keeps it open, at the next
   // window width too.
   await resize(106,40);await geometry('geometry13','64x40');
   raw='';await press(104,39);await waitFor(/rule motion/);
   await rpc(cfg.inlet,{batch:Buffer.from('\t\t++++++\r').toString('base64')});await new Promise(r=>setTimeout(r,300));
   await geometry('geometry14','58x40');
   await resize(107,40);await geometry('geometry15','59x40');
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
 const displayLag=[],stateLag=[],stateColors=[];
 if(cfg.scenario==='state-border'){
   // The border's own state word, read from the painted top border, and the color
   // of the border's own corner cell painted with it (spec rule 25).
   const border=()=>{for(let y=0;y<sideTerm.rows;y++){const m=(sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'').match(/┌ ([⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●◑]) /);if(m)return m[1]==='●'?'idle':m[1]==='◑'?'waiting':'running';}return '';};
   const corner=()=>{for(let y=0;y<sideTerm.rows;y++){const line=sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y),x=(line?.translateToString()||'').indexOf('┌');if(x>=0){const c=line.getCell(x);return c.getFgColorMode()+':'+c.getFgColor()+(c.isDim()?':dim':'');}}return '';};
   const until=async(word,began)=>{while(Date.now()-began<4000){await new Promise(resolve=>sideTerm.write('',resolve));if(border()===word)return Date.now()-began;await new Promise(resolve=>setTimeout(resolve,20));}throw Error('the border never showed '+word+' (shows '+border()+')');};
   await until('idle',Date.now());stateColors.push(['idle',corner()]);
   // Every projection from here waits on a held backend: only the event itself can repaint.
   fs.writeFileSync(cfg.hold,'1');
   for(const [line,word] of [['turn-open','running'],['turn-close','idle']]){
     const began=Date.now();await rpc(cfg.inlet,{batch:Buffer.from(line+'\r').toString('base64')});
     stateLag.push(await until(word,began));stateColors.push([word,corner()]);
   }
 }
 const lateRoute={};
 if(cfg.scenario==='late-route'){
   const write=(title,state)=>{const value=JSON.parse(fs.readFileSync(cfg.routeCache,'utf8'));value.observed_at=Date.now()/1000;
     value.route_panels['claude\0'+cfg.sid]=[{id:'rt-late',title,state,kind:state,stages:[{name:'inline',st:state==='working'?'active':'done'}]}];
     fs.writeFileSync(cfg.routeCache+'.new',JSON.stringify(value),{mode:0o600});fs.renameSync(cfg.routeCache+'.new',cfg.routeCache);};
   const lines=()=>Array.from({length:sideTerm.rows},(_,y)=>sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'').join('\n');
   // The route row's own mark: a running route's turns on the shared beat; one that
   // claims nothing stands still (the frame follows the session, spec rule 25).
   const mark=()=>(lines().match(/([⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●✕✓◌·]) ROUTE_[A-Z]+/)||[])[1]||'';
   const turns=async()=>{const seen=new Set();for(let n=0;n<6;n++){await new Promise(resolve=>sideTerm.write('',resolve));seen.add(mark());await new Promise(resolve=>setTimeout(resolve,120));}return seen.size>1;};
   const until=async(test,what)=>{for(let n=0;n<800;n++){await new Promise(resolve=>sideTerm.write('',resolve));if(await test())return;await new Promise(resolve=>setTimeout(resolve,25));}throw Error('never '+what+'\n'+lines());};
   const pinned=JSON.parse(fs.readFileSync(file,'utf8'));
   write('ROUTE_OLD','working');await until(()=>/ROUTE_OLD/.test(lines()),'the first route');
   lateRoute.working=await turns();
   // The next projection is computed and then held past every host limit; the
   // newer record lands while it is held, so its own projection queues behind.
   fs.writeFileSync(cfg.hold,'1');write('ROUTE_MID','working');
   await until(()=>fs.existsSync(cfg.held),'a held projection');
   write('ROUTE_NEW','done');
   await until(async()=>(await rpc(pinned.socket,{...pinned,op:'status'})).projection_state==='unavailable','an unavailable projection');
   // Unrefreshed for long, the route row stops claiming its working state.
   await until(async()=>!(await turns()),'a route mark that no longer turns');
   lateRoute.stale=await turns();lateRoute.staleText=/ROUTE_(?:OLD|MID)/.test(lines());
   const gate=fs.openSync(cfg.holdGate,fs.constants.O_RDWR|fs.constants.O_NONBLOCK);fs.writeSync(gate,Buffer.from('1'));fs.closeSync(gate);
   await until(()=>/ROUTE_NEW/.test(lines()),'the newest route after the late reply');
   await until(async()=>(await rpc(pinned.socket,{...pinned,op:'status'})).projection_state==='current','a current projection');
   lateRoute.done=mark();lateRoute.final=lines().match(/ROUTE_[A-Z]+/g);
 }
 if(cfg.scenario==='request-panel'){
   // R27: a request line stands in the route box, a press opens it in place and a second press
   // opens its read page with the sealed body; a typed draft and the native state stay as they were.
   await waitFor(/REQ_TITLE/);
   await new Promise(resolve=>sideTerm.write('',resolve));
   const pinnedRequest=JSON.parse(fs.readFileSync(file,'utf8'));
   const screen=()=>Array.from({length:sideTerm.rows},(_,y)=>sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y)?.translateToString()||'');
   const press=async(needle)=>{
     let row;
     for(let n=0;n<60&&!row;n++){
       await new Promise(resolve=>sideTerm.write('',resolve));
       screen().forEach((line,y)=>{const x=line.indexOf(needle);if(x>=0&&!line.includes('╭─'))row={x:x+2,y:y+1};});
       if(!row)await new Promise(resolve=>setTimeout(resolve,50));
     }
     if(!row)throw Error('request target absent: '+needle+'\n'+screen().join('\n'));
     await rpc(cfg.inlet,{batch:Buffer.from(`\x1b[<0;${row.x};${row.y}M\x1b[<0;${row.x};${row.y}m`).toString('base64')});
   };
   await rpc(cfg.inlet,{batch:Buffer.from('REQ_DRAFT').toString('base64')});
   await new Promise(resolve=>setTimeout(resolve,300));
   const before=await rpc(pinnedRequest.socket,{...pinnedRequest,op:'status'});
   const settled=async()=>{let after;for(let n=0;n<40;n++){after=await rpc(pinnedRequest.socket,{...pinnedRequest,op:'status'});if(after.empty===before.empty)break;await new Promise(resolve=>setTimeout(resolve,50));}
     for(const key of ['empty','focused','state'])if(after[key]!==before[key])throw Error('a request press changed '+key+' '+JSON.stringify([before,after]));};
   const opened={};
   // The panel is painted cell by cell, so its words are read from the terminal screen.
   // The catalog's language is the machine's (English here, Korean on a Korean one): either word stands.
   const shows=async(needle)=>{const any=needle.split('|');for(let n=0;n<120;n++){await new Promise(resolve=>sideTerm.write('',resolve));if(any.some(word=>screen().join('\n').includes(word)))return;await new Promise(resolve=>setTimeout(resolve,50));}
     throw Error('screen never showed '+needle+'\n'+screen().join('\n'));};
   await press('REQ_TITLE');await shows('+ body|+ 본문');await settled();opened.details=screen().join('\n').includes('rq-');
   await press(screen().join('\n').includes('+ body')?'+ body':'+ 본문');await shows('REQ_BODY_NONCE');await settled();opened.page=screen().join('\n');
   // Esc closes the page (no native Esc), then the draft goes to the native as typed.
   raw='';
   await rpc(cfg.inlet,{batch:Buffer.from('\x1b').toString('base64')});
   await shows('REQ_TITLE');await settled();
   await rpc(cfg.inlet,{batch:Buffer.from('\r').toString('base64')});await waitFor(/DONE_REQ_DRAFT/);
   if(/NATIVE_ESC_/.test(raw))throw Error('the page keys reached the native');
   opened.panel=screen().join('\n');
   console.log(JSON.stringify({request_panel:opened,status:before}));return;
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
 if(cfg.scenario==='display')await waitFor(/project\/|(?:\]|REPO +) (?:\x1b\[[0-9;]*m)*project/);
 if(cfg.scenario==='display')for(let index=0;index<4;index++){
   const title='event-title-'+index,before=process.hrtime.bigint();
   const projection=JSON.parse(fs.readFileSync(cfg.routeCache,'utf8'));projection.observed_at=Date.now()/1000;projection.route_status['claude\0'+cfg.sid]='route 1 · '+title;projection.route_sessions['claude\0'+cfg.sid]=Date.now()/1000;
   fs.writeFileSync(cfg.routeCache+'.new',JSON.stringify(projection),{mode:0o600});fs.renameSync(cfg.routeCache+'.new',cfg.routeCache);
   await waitFor(new RegExp(title));displayLag.push(Number(process.hrtime.bigint()-before)/1e6);
 }
 const ruleMotion={};
 if(cfg.scenario==='rule-motion'){
   // Spec E9 rule 47 (d): while the session runs, the head's rule moves on the spinner's beat
   // and only its changed cells go out; the native half of that row is never written again.
   const now=Date.now()/1000;
   fs.writeFileSync(cfg.title,JSON.stringify({title:'RULE_MOTION_TITLE',title_ts:now,ts:now,source:'test'}));
   await rpc(cfg.inlet,{batch:Buffer.from('turn-open\r').toString('base64')});await waitFor(/DONE_turn-open/);
   const opened=Date.now();
   const flush=()=>new Promise(resolve=>sideTerm.write('',resolve));
   // The rule's row: the thin rule (spec rule 52) whose cells are not all the plain dim line.
   // (`at`: that row however it stands, lit or dark.)
   const rule=(at=-1)=>{for(let y=0;y<sideTerm.rows;y++){const row=sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y),line=row?.translateToString()||'';
     const m=line.slice(140).match(/^│ ([─━]{36}) │$/);
     const colors=m&&Array.from({length:36},(_,i)=>{const c=row.getCell(142+i);return c.isFgDefault()?'d':c.getFgColor()+(c.isDim()?'d':'');}).join(',');
     if(m&&(at<0?colors.split(',').some(c=>!c.endsWith('d')):y===at))return {y,native:line.slice(0,138),cells:m[1],colors};}return null;};
   let first=null;const began=Date.now();
   while(Date.now()-began<6000&&!(first=rule())){await flush();await new Promise(r=>setTimeout(r,20));}
   // Correction 8: the light comes in on the paint that shows the turn (it never waits out a rest).
   const litAfter=Date.now()-opened;
   let bytes=0,writes=0;const sub=child.onData(t=>{bytes+=Buffer.byteLength(t);writes++;});
   // The second reading comes 0.95 s in. The light runs out and back over the rule, so a reading
   // at the mirror of the first (2.6 s less twice the first's age into the turn) looks the same:
   // 1.95 s after a first reading 0.3 s in was that mirror. 0.95 s after a first reading under
   // 0.3 s (asserted below) is neither that mirror nor a whole 3.5 s period. The writes are still
   // counted over 1.95 s.
   await new Promise(r=>setTimeout(r,950));await flush();
   // (The same row, lit or resting: the light may be out on its rest by then.)
   const second=first&&rule(first.y);
   await new Promise(r=>setTimeout(r,1000));sub.dispose();await flush();
   // Spec rule 51 (c): the rest (the dark rule for 1 s once the light has left on its first side;
   // the far end's dark beat lasts 0.1 s). Seen dark and still dark 0.25 s later, it is the rest:
   // for the next 0.5 s the host writes nothing for the rule's row, only the spinner's own glyph.
   const dark=()=>{const r=first&&rule(first.y);return !!r&&r.colors.split(',').every(c=>c.endsWith('d'));};
   let restAt=0;const looking=Date.now();
   while(!restAt&&Date.now()-looking<6000){await flush();if(dark()){await new Promise(r=>setTimeout(r,250));await flush();if(dark())restAt=Date.now();}else await new Promise(r=>setTimeout(r,20));}
   const rest=[];const restSub=child.onData(t=>rest.push(t));
   await new Promise(r=>setTimeout(r,500));restSub.dispose();await flush();
   const resting=first&&rule(first.y);
   let back=null;const waiting=Date.now();
   while(Date.now()-waiting<1500&&!(back=rule())){await flush();await new Promise(r=>setTimeout(r,20));}
   Object.assign(ruleMotion,{first,second,bytes,writes,rest,rest_seen:!!restAt,lit_after_ms:litAfter,rest_row:resting&&resting.y,resting:resting&&resting.colors,back:back&&back.colors});
   await rpc(cfg.inlet,{batch:Buffer.from('turn-close\r').toString('base64')});await waitFor(/DONE_turn-close/);
 }
 const nativeBg={};
 if(cfg.scenario==='native-bg'){
   // A native that paints its own background to its last column (OpenCode does): the two cells
   // between it and the panel take that background; a row on the terminal's own keeps none.
   await rpc(cfg.inlet,{batch:Buffer.from('paint-bg\r').toString('base64')});await waitFor(/PAINTED_BG/);
   await new Promise(r=>setTimeout(r,300));await new Promise(resolve=>sideTerm.write('',resolve));
   const rowsOf=()=>Array.from({length:sideTerm.rows},(_,y)=>sideTerm.buffer.active.getLine(sideTerm.buffer.active.viewportY+y));
   const bg=(line,x)=>{const c=line.getCell(x);return c.isBgDefault()?'default':c.getBgColor();};
   const lines=rowsOf(),painted=lines.findIndex(l=>(l?.translateToString()||'').includes('PAINTED_BG'));
   const plain=lines.findIndex((l,y)=>y!==painted&&(l?.translateToString()||'').slice(140,141)==='│'&&bg(l,10)==='default');
   // #1583 review follow-up (다): an inverse edge cell shows its foreground as its background,
   // so the gap takes that color (the terminal's own where the foreground is the default).
   const edge=word=>{const y=lines.findIndex(l=>(l?.translateToString()||'').includes(word));return y<0?null:[138,139].map(x=>bg(lines[y],x));};
   Object.assign(nativeBg,{painted:painted<0?null:[137,138,139,140].map(x=>bg(lines[painted],x)),plain:plain<0?null:[137,138,139].map(x=>bg(lines[plain],x)),
     border:painted<0?null:lines[painted].getCell(140).getChars(),inverse:edge('INVERSE_EDGE'),inverse_plain:edge('INVERSE_PLAIN')});
 }
 const binding=JSON.parse(fs.readFileSync(file,'utf8'));
 const status=await rpc(binding.socket,{...binding,op:'status'});
 console.log(JSON.stringify({native_bg:nativeBg,rule_motion:ruleMotion,ack_ms:ack,status,display_lag_ms:displayLag,state_lag_ms:stateLag,state_colors:stateColors,late_route:lateRoute,startup,live_state:liveState,peer}));
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
        if scenario=='late-route':
            backend=directory/'held-python';hold=directory/'hold';held=directory/'held';hold_gate=directory/'hold-gate';os.mkfifo(hold_gate,0o600)
            backend.write_text('#!'+sys.executable+'\nimport os,sys\nsys.path.insert(0,'+repr(str(ROOT/'utilities'))+')\nimport session_shell as m\n'
                               'original=m.panel_projection\ndef projection(*args,**kwargs):\n'
                               ' value=original(*args,**kwargs)\n'
                               ' if os.path.exists('+repr(str(hold))+'):\n'
                               '  os.remove('+repr(str(hold))+');open('+repr(str(held))+',"w").close()\n'
                               '  fd=os.open('+repr(str(hold_gate))+',os.O_RDWR);os.read(fd,1);os.close(fd)\n'
                               ' return value\n'
                               'm.panel_projection=projection\nraise SystemExit(m._backend())\n')
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
        if scenario=='request-panel':
            # A sent request with a really sealed body, read from the request records by the backend.
            with mock.patch.dict(os.environ,self.env,clear=True):
                import importlib.util as _util
                import peer_request
                spec=_util.spec_from_file_location('peer_message_pty_seed',ROOT/'utilities/peer-message.py')
                peer_message=_util.module_from_spec(spec);spec.loader.exec_module(peer_message)
                me={'harness':'claude','session_id':sid,'name':'e9'}
                other={'harness':'codex','session_id':'00000000-0000-4000-8000-0000000000e7','name':'e7'}
                request_id=peer_request.reserve(me,other,title='REQ_TITLE 한글 제목')
                line=peer_request.render_line(peer_request.load(request_id),'request',revision=1,label='판1',body_lines=2)
                _text,ref=peer_message.prepare_peer_message(
                    'REQ_BODY_NONCE\nsecond line',me,other,defer=True,
                    refs=[peer_request.ref_for(request_id,1,'request')],
                    request={'seal':peer_request.request_seal(request_id,1,'request'),'line':line})
                peer_request.commit_open(request_id,ref,label='판1')
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
        if scenario=='late-route':config.update(hold=str(hold),held=str(held),holdGate=str(hold_gate))
        if peer is not None:
            audit=directory/'audit.py'
            # Counts every subprocess the sender starts and its wall time; nothing else changes.
            # The shared obligation runner is the one start counted apart: a queued message
            # is a retained duty, and the sender starts that runner only when none holds its lock.
            audit.write_text('import json,runpy,sys,time\nout=sys.argv[1];sys.argv=sys.argv[2:]\n'
                'calls=[];runners=[]\n'
                'def hook(e,a):\n'
                ' if e!="subprocess.Popen":return\n'
                ' argv=a[1] if isinstance(a[1],(list,tuple)) else []\n'
                ' (runners if len(argv)>2 and str(argv[2])=="__obligation-runner" else calls).append(str(argv[0]) if argv else "?")\n'
                'sys.addaudithook(hook)\n'
                'began=time.monotonic()\ntry:\n runpy.run_path(sys.argv[0],run_name="__main__")\nexcept SystemExit:\n pass\n'
                'finally:\n open(out,"w").write(json.dumps({"subprocesses":len(calls),"programs":sorted(set(calls)),"runner_starts":len(runners),"ms":round((time.monotonic()-began)*1000)}))\n')
            config.update(peer=peer,audit=str(audit),peerCount=str(directory/'count'),
                          steward=str(ROOT/'utilities/peer-steward.py'),
                          tags=str(Path(self.env['XDG_STATE_HOME'])/'agent-fleet/tag-assignments.json'))
        if scenario in {'side-layout','side-columns','side-left','state-border','late-route','startup-state','live-state','rule-motion','native-bg','request-panel'}:
            config.update(columns=180,rows=40)
        if scenario=='route-pages':config.update(columns=80,rows=24)  # the bottom layout's pages (policy below)
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
                                      **({'position':'left'} if scenario=='side-left' else {})}) if scenario in {'side-layout','side-columns','side-left','state-border','late-route','startup-state','live-state','rule-motion','native-bg','request-panel'}
                          else '{"enabled":true,"layout":"bottom"}' if scenario=='route-pages' else '{"enabled":true}')
        self.addCleanup(policy.write_bytes,previous_policy)
        child=subprocess.Popen([self.node,str(self.driver)],env=env,cwd=self.project,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        birth=Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
        try:
            stdout,stderr=child.communicate(timeout=90 if scenario=='live-state' else 35)
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
                    # Resolution and delivery start no process. The one start allowed is the shared
                    # obligation runner of the retained queued-message duty: it happens only when no
                    # runner holds the lock, which depends on whether an earlier send in this state
                    # root left one alive (the same result at origin/main when this test runs alone).
                    self.assertEqual(rows[label]['subprocesses'],0,rows[label])
                    self.assertLessEqual(rows[label]['runner_starts'],1,rows[label])
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
        # native event arrives, not after a (here held) display projection returns,
        # and the border's color changes in that same paint (spec rule 25).
        result=self.run_loop('state-border')
        print(json.dumps({'event':'session-shell-border-state','lag_ms':result['state_lag_ms']}))
        self.assertEqual(len(result['state_lag_ms']),2)
        self.assertLess(max(result['state_lag_ms']),1000)
        colors=dict((word,color) for word,color in result['state_colors'][:2])
        self.assertEqual([word for word,_ in result['state_colors']],['idle','running','idle'])
        self.assertNotEqual(colors['idle'],colors['running'])
        self.assertEqual(result['state_colors'][2][1],colors['idle'])

    def test_the_head_rule_moves_on_the_spinner_beat_sending_only_its_changed_cells(self):
        # Spec E9 rule 47 (d): a running session's head rule (every cell heavy) carries a moving light on the beat the
        # spinner already keeps; each beat sends only the rule's changed cells beside the spinner's
        # row, so a beat stays small (the whole row each beat would be about 500 bytes), and the
        # native half stays put.
        result=self.run_loop('rule-motion')['rule_motion']
        print(json.dumps({'event':'session-shell-rule-motion','bytes':result['bytes'],'writes':result['writes'],
                          'rest_writes':len(result['rest']),'rest_bytes':[len(c.encode()) for c in result['rest']]}))
        first,second=result['first'],result['second']
        self.assertIsNotNone(first);self.assertIsNotNone(second)
        # Spec E9 rule 52: the moving rule is the thin rule; no heavy cell anywhere on it.
        for value in (first,second):self.assertEqual(value['cells'],'─'*36,value)
        self.assertNotEqual(first['colors'],second['colors'])
        self.assertEqual(first['native'],second['native'])
        self.assertGreaterEqual(result['writes'],15)
        self.assertLess(result['bytes']/result['writes'],400,result)
        # Spec E9 rule 51 (c): through the rest the rule's row stands dark and nothing is written
        # to it; the host's writes there are the spinner's glyph alone (one cell each). Then the
        # light comes back in.
        # Correction 8: the light shows on the first paint of the turn (within 0.3 s of it).
        self.assertLess(result['lit_after_ms'],300,result)
        self.assertTrue(result['rest_seen'],result)
        y=result['rest_row'];self.assertIsNotNone(y,result)
        self.assertTrue(all(c.endswith('d') for c in result['resting'].split(',')),result['resting'])
        self.assertTrue(result['rest'],result)
        for chunk in result['rest']:
            self.assertNotIn('\x1b[%d;'%(y+1),chunk,result['rest'])
            self.assertLess(len(chunk.encode()),96,chunk)
        self.assertIsNotNone(result['back'],result)
        self.assertTrue(any(not c.endswith('d') for c in result['back'].split(',')),result['back'])

    def test_the_gap_beside_the_panel_takes_the_natives_own_background(self):
        # A native that paints its whole area (OpenCode) reaches the panel's frame: the two cells
        # between them take the native's background at that edge, so the panel stands with the
        # same space on both sides; a native on the terminal's own background keeps them plain.
        result=self.run_loop('native-bg')['native_bg']
        self.assertEqual(result['painted'],[232,232,232,'default'],result)
        self.assertEqual(result['border'],'│')
        self.assertEqual(result['plain'],['default','default','default'],result)
        self.assertEqual(result['inverse'],[88,88],result)
        self.assertEqual(result['inverse_plain'],['default','default'],result)

    def test_the_border_names_the_state_right_after_start_without_a_key(self):
        # Spec rule 26: a start event leaves the host's own state unknown; until the
        # next lifecycle event the border shows the native's own session record
        # (here `idle`), read by the projection the start already runs.
        result=self.run_loop('startup-state')['startup']
        print(json.dumps({'event':'session-shell-startup-state','ms':result.get('ms')}))
        self.assertEqual(result.get('word'),'idle')

    def test_the_panel_state_follows_the_natives_own_screen_before_its_hooks(self):
        # Spec 11-12 rule 6: the panel's state is the host's own observation of the
        # native (Claude's REPL writes its UI state into its session record as it
        # paints); each hook (a new process, as the real ones) only confirms it later.
        # An Enter the native does not take as a prompt claims nothing.
        result=self.run_loop('live-state')['live_state']
        import statistics
        summary={key:{'median':statistics.median(values),'max':max(values)} for key,values in result.items()
                 if isinstance(values,list) and values and all(isinstance(v,(int,float)) for v in values)}
        hooks={}
        for kind,ms in result.get('hooks',()):hooks.setdefault(kind,[]).append(ms)
        print(json.dumps({'event':'session-shell-live-state','ms':summary,'false_running':result['false_running'],
                          'hook_ms':{kind:{'median':statistics.median(v),'max':max(v)} for kind,v in hooks.items()}}))
        for key in ('submit','busy','idle','waiting','approve','approveIdle'):
            self.assertEqual(len(result[key]),10,key);self.assertNotIn(None,result[key],key)
            self.assertLess(max(result[key]),400,key)
        self.assertEqual(result['false_running'],0)
        self.assertEqual(result['false_waiting'],0)      # a menu dialog is not an answer wait (R1)
        self.assertEqual(result['foreign_changed'],0)    # another session's or process's record (R5)

    def test_a_late_projection_is_applied_and_never_replaces_a_newer_one(self):
        # A projection held past the host's limit is still painted when it comes,
        # the newer record queued behind it follows, and nothing older returns.
        # While it is held the route row stops claiming its working state.
        result=self.run_loop('late-route')['late_route']
        self.assertTrue(result['working'])
        self.assertFalse(result['stale'])
        self.assertTrue(result['staleText'])
        self.assertEqual(set(result['final']),{'ROUTE_NEW'})
        self.assertEqual(result['done'],'✓')

    def test_a_blocked_answer_keeps_projection_and_native_keyboard_responsive(self):
        self.assertFalse(self.run_loop('answer-display')['status']['focused'])

    def test_native_start_ack_does_not_wait_for_a_blocked_display_backend(self):
        result=self.run_loop('start',blocked_backend=True)
        self.assertLess(result['ack_ms'],250)

    def test_a_request_line_opens_in_place_and_on_its_read_page_and_the_draft_survives(self):
        result=self.run_loop('request-panel')['request_panel']
        self.assertTrue(result['details'])
        self.assertIn('REQ_BODY_NONCE',result['page'])
        self.assertIn('REQUEST',result['page'].upper())
        self.assertIn('REQ_TITLE',result['panel'])

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
