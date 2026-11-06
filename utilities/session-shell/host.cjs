'use strict';
// Optional VT/PTY attachment. Messages and their receipts remain in peer-message.
const fs = require('node:fs');
const net = require('node:net');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
const args = process.argv.slice(2), harness = args.shift(), command = args;
let activated = false, child, backend, server, projectionTimer, frameTimer, ended = false;
function startupResult(value) {
  const fd = Number(process.env.HEARTING_SHELL_START_FD);
  fs.writeSync(fd, value + '\n'); fs.closeSync(fd);
}
function bypass() { startupResult('BYPASS'); process.exit(0); }
let pty, Terminal, Unicode11Addon;
try {
  if (!['claude','codex','opencode'].includes(harness) || !command.length || !process.stdin.isTTY || !process.stdout.isTTY) throw Error();
  pty = require('node-pty'); ({Terminal} = require('@xterm/headless'));
  ({Unicode11Addon} = require('@xterm/addon-unicode11'));
} catch { bypass(); }
const dir = process.env.HEARTING_SHELL_DIRECTORY;
try {
  const st = fs.lstatSync(dir);
  if (!st.isDirectory() || st.uid !== process.getuid() || st.mode & 0o077) throw Error();
} catch { bypass(); }
const socketPath = path.join(dir, 'control.sock');
let cols = process.stdout.columns, rows = process.stdout.rows;
if (cols < 20 || rows < 8) bypass();
const terminal = new Terminal({cols, rows:rows-1, allowProposedApi:true, scrollback:1000, logLevel:'off'});
terminal.loadAddon(new Unicode11Addon()); terminal.unicode.activeVersion = '11';
const incarnation = crypto.randomBytes(24).toString('hex'), token = crypto.randomBytes(32).toString('hex');
function processRow(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 1) return null;
  try {
    const st = fs.statSync(`/proc/${pid}`), text = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    const fields = text.slice(text.lastIndexOf(') ')+2).trim().split(/\s+/);
    if (st.uid !== process.getuid() || ['Z','X'].includes(fields[0])) return null;
    return {pid, start:fields[19], parent:Number(fields[1])};
  } catch { return null; }
}
function descends(pid, ancestor) {
  const seen = new Set();
  for (let i=0; i<32; i++) {
    if (pid === ancestor) return true;
    if (seen.has(pid)) return false;
    seen.add(pid); const row = processRow(pid); if (!row) return false; pid = row.parent;
  }
  return false;
}
const host = processRow(process.pid);
if (!host) bypass();
function sameTerminal(pid, root) {
  try {
    const candidate = fs.statSync(`/proc/${pid}/fd/0`), original = fs.statSync(`/proc/${root}/fd/0`);
    return candidate.isCharacterDevice() && original.isCharacterDevice() && candidate.rdev === original.rdev;
  } catch { return false; }
}
let native, rootNative, sid = '', sequence = 0, selection = 0, state = 'unknown', epoch = 0, empty = false;
let lastEnter = -1, submitted = -1, currentDelivery, held = [], heldBytes = 0;
let expanded = false, relay = false, paste = false, keyTail = '', cursorVisible = true;
let last = [], footer = '', footerSegments = [], footerLast = '\0', panelRows = [], panelLines = [], projectionKey = '';
let sourceError;
const deliveries = new Map(), calls = new Map(), watchers = new Map();
let callID = 0, projecting = false, projectionPending = false;
function binding() {
  return {schema:'hearting-session-shell-v1',harness,sid,incarnation,host_pid:host.pid,host_start:host.start,
    native_pid:native?.pid,native_start:native?.start,socket:socketPath};
}
function identity() {
  const b = binding(); delete b.schema; delete b.socket; return b;
}
function live() { const row = native && processRow(native.pid); return !!row && row.start === native.start; }
function status() { return {...identity(),epoch,state,empty:empty && !keyTail && !paste,alive:live(),expanded,held_bytes:heldBytes,projection_state:sourceError?"unavailable":"current"}; }
function source(op, extra={}) {
  if (!backend || backend.exitCode !== null || !sid) return Promise.resolve({ok:false,reason:'source-unavailable'});
  return new Promise(resolve => {
    const id=++callID;
    const timer=setTimeout(()=>{calls.delete(id);resolve({ok:false,reason:'source-unavailable'});},1500);
    calls.set(id,{resolve,timer});
    backend.stdin.write(JSON.stringify({id,op,binding:binding(),...extra})+'\n');
  });
}
function emit(value) { process.stdout.write(value); }
function color(c,bg) {
  const v=bg?c.getBgColor():c.getFgColor();
  if(bg?c.isBgRGB():c.isFgRGB())return `${bg?48:38};2;${v>>16};${v>>8&255};${v&255}`;
  if(bg?c.isBgPalette():c.isFgPalette())return `${bg?48:38};5;${v}`;
  return bg?'49':'39';
}
function serialize(line,width) {
  let s='',previous='';
  for(let x=0;x<width;x++) {
    const c=line?.getCell(x); if(!c||!c.getWidth())continue;
    const a=[0,color(c,false),color(c,true)];
    for(const [method,sgr] of [['isBold',1],['isDim',2],['isItalic',3],['isUnderline',4],['isBlink',5],['isInverse',7],['isInvisible',8],['isStrikethrough',9],['isOverline',53]])if(c[method]())a.push(sgr);
    const attr=a.join(';');if(attr!==previous)s+=`\x1b[${attr}m`;
    s+=c.getChars()||' ';previous=attr;
  }
  return s+'\x1b[0m';
}
function height() { return expanded?Math.min(10,Math.max(3,rows-8)):1; }
function schedule() { if(!frameTimer)frameTimer=setTimeout(render,8); }
function render() {
  frameTimer=undefined;if(ended||!activated||relay)return;
  try {
    let text='\x1b[?2026h\x1b[?25l\x1b[?7l';
    for(let y=0;y<terminal.rows;y++) {
      const line=serialize(terminal.buffer.active.getLine(terminal.buffer.active.viewportY+y),terminal.cols);
      if(last[y]!==line){text+=`\x1b[${y+1};1H${line}`;last[y]=line;}
    }
    if(expanded)for(let y=0;y<panelRows.length;y++)text+=`\x1b[${rows-height()+y+1};1H${panelRows[y]}`;
    else if(footerLast!==displayFooter){text+=`\x1b[${rows};1H\x1b[0m\x1b[2K${displayFooter}`;footerLast=displayFooter;}
    const b=terminal.buffer.active,m=terminal.modes;
    text+=`\x1b[${Math.min(b.cursorY+1,terminal.rows)};${Math.min(b.cursorX+1,terminal.cols)}H`;
    text+=`\x1b[?2004${m.bracketedPasteMode?'h':'l'}\x1b[?1${m.applicationCursorKeysMode?'h':'l'}`;
    text+='\x1b[?1000l\x1b[?1002l\x1b[?1003l';
    if(m.mouseTrackingMode!=='none')text+=`\x1b[?${({x10:9,vt200:1000,drag:1002,any:1003})[m.mouseTrackingMode]}h\x1b[?1006h`;
    text+=`\x1b[?1004${m.sendFocusMode?'h':'l'}\x1b[?7h${cursorVisible?'\x1b[?25h':''}\x1b[?2026l`;
    emit(text);
  } catch { rawRelay(); }
}
function displayLines(lines,height,done) {
  const vt=new Terminal({cols,rows:height,scrollback:0,allowProposedApi:true,logLevel:'off'});
  vt.loadAddon(new Unicode11Addon());vt.unicode.activeVersion='11';
  const safe=s=>String(s).replace(/[\x00-\x1f\x7f]/g,' ');
  const paint=line=>Array.isArray(line)?line.map(part=>{
    const attrs=[0];
    if(Number.isInteger(part.foreground)&&part.foreground>=0&&part.foreground<=255)attrs.push(38,5,part.foreground);
    if(part.bold===true)attrs.push(1);if(part.dim===true)attrs.push(2);if(part.reverse===true)attrs.push(7);
    return `\x1b[${attrs.join(';')}m${safe(part.text)}`;
  }).join(''):safe(line);
  vt.write('\x1b[?7l'+lines.slice(0,height).map((s,i)=>`\x1b[${i+1};1H${paint(s)}`).join(''),()=>{
    done(Array.from({length:height},(_,y)=>serialize(vt.buffer.active.getLine(y),cols)));vt.dispose();schedule();
  });
}
function rebuildDisplay() {
  const extra=currentDelivery?` · 전달 중${heldBytes?' · 입력 보존 '+heldBytes+'B':''}`:'';
  const line=footerSegments.length?[...footerSegments,{text:extra}]:[(footer||`하팅 · ${harness} · 세션 확인 중`)+extra];
  displayLines([footerSegments.length?line:line[0]],1,lines=>{footerLast='\0';displayFooter=lines[0];});
  if(expanded)displayLines(panelLines.length?panelLines:[`하팅 · ${harness}  Ctrl+] 접기`],height(),lines=>{panelRows=lines;});
}
// Footer is serialized once by the scratch VT; child parser state stays untouched.
let displayFooter='';
function applyLayout() {
  if(relay)return;
  child.resize(cols,rows-height());terminal.resize(cols,rows-height());last=[];footerLast='\0';
  emit('\x1b[0m\x1b[2J');rebuildDisplay();schedule();queueProjection();
}
function queueProjection() {
  if(ended||projectionTimer)return;
  projectionTimer=setTimeout(()=>{projectionTimer=undefined;projection();},25);
}
function observeFiles(paths) {
  const wanted=new Map();let changed=false;
  for(const target of (Array.isArray(paths)?paths:[]).slice(0,64)){
    if(typeof target!=='string'||!path.isAbsolute(target)||target.length>4096)continue;
    let directory=path.dirname(target);
    while(!fs.existsSync(directory)&&path.dirname(directory)!==directory)directory=path.dirname(directory);
    if(!wanted.has(directory))wanted.set(directory,[]);wanted.get(directory).push(target);
  }
  for(const [directory,row] of watchers)if(!wanted.has(directory)){row.watch.close();watchers.delete(directory);changed=true;}
  for(const [directory,targets] of wanted){
    const existing=watchers.get(directory);
    if(existing){if(JSON.stringify(existing.targets)!==JSON.stringify(targets))changed=true;existing.targets=targets;continue;}
    try{
      const row={targets};
      row.watch=fs.watch(directory,(_event,name)=>{
        const changed=name?path.join(directory,String(name)):directory;
        if(!name||row.targets.some(target=>target===changed||target.startsWith(changed+path.sep)))queueProjection();
      });
      row.watch.on('error',()=>{row.watch.close();watchers.delete(directory);sourceError={reason:'file-observation-unavailable'};});
      watchers.set(directory,row);
      changed=true;
    }catch{sourceError={reason:'file-observation-unavailable'};}
  }
  return changed;
}
async function projection() {
  if(!sid||ended)return;if(projecting){projectionPending=true;return;}
  projecting=true;projectionPending=false;const selected=selection;
  try{
    const data=await source('projection',{columns:cols,state,expanded});
    if(selected!==selection)return;if(!data.ok){sourceError=data;return;}sourceError=undefined;
    // Close the read-before-watch gap once when the input set changes.
    if(observeFiles(data.watch))projectionPending=true;
    const key=JSON.stringify([data.footer,data.segments,data.lines,cols,expanded,currentDelivery,heldBytes]);
    if(key===projectionKey)return;projectionKey=key;footer=data.footer;footerSegments=data.segments||[];panelLines=data.lines;rebuildDisplay();
  }finally{projecting=false;if(projectionPending)queueProjection();}
}
function rawRelay() {
  if(relay||ended)return;relay=true;state='unknown';empty=false;
  emit('\x1b[0m\x1b[?25h\x1b[?1049l');child.resize(cols,rows);
}
function releaseHeld() {
  const saved=held;held=[];heldBytes=0;process.stdin.resume();
  for(const data of saved)userInput(data);
}
function userInput(data) {
  if(!data.length)return;
  if(data.length===1&&data[0]===0x1d&&!paste&&!currentDelivery){expanded=!expanded;applyLayout();return;}
  const previous=epoch;epoch+=data.length;empty=false;
  if(currentDelivery){held.push(Buffer.from(data));heldBytes+=data.length;if(heldBytes>=65536)process.stdin.pause();rebuildDisplay();return;}
  for(let offset=0;offset<data.length;offset++) {
    const c=String.fromCharCode(data[offset]);
    if(keyTail){keyTail+=c;if(keyTail==='\x1b[200~'){paste=true;keyTail='';}else if(keyTail==='\x1b[201~'){paste=false;keyTail='';}
      else if(keyTail.length>2&&/[\x40-\x7e]/.test(c)||keyTail.length>64)keyTail='';}
    else if(c==='\x1b')keyTail=c;
    else if(!paste&&(c==='\r'||c==='\n'))lastEnter=previous+offset+1;
  }
  child.write(data);
}
function exact(req) { const me=identity();return Object.keys(me).every(k=>req[k]===me[k])&&live(); }
async function lifecycle(req) {
  const reporter=processRow(req.pid);
  const root=rootNative&&processRow(rootNative.pid);
  if(req.token!==token||req.harness!==harness||!reporter||reporter.start!==req.start
     ||!root||root.start!==rootNative.start||!descends(req.pid,root.pid)||!sameTerminal(req.pid,root.pid)
     ||typeof req.sid!=='string'||!/^[A-Za-z0-9._-]{1,256}$/.test(req.sid))return {ok:false,reason:'event-identity'};
  if(!Number.isSafeInteger(req.seq)||req.seq<=sequence)return {ok:false,reason:'stale-event'};
  if(req.event==='start'||req.event==='select') {
    sequence=req.seq;selection++;native=reporter;sid=req.sid;state='unknown';empty=false;submitted=-1;
    if(currentDelivery){currentDelivery=undefined;releaseHeld();}
    const selected=selection;
    // Display collection must never delay the native start hook or input.
    setImmediate(()=>{if(selected===selection)source('register').then(()=>projection());});
    return {ok:true};
  }
  if(req.sid!==sid)return {ok:false,reason:'selection-changed'};
  sequence=req.seq;
  if(req.event==='deselect'){
    const old=binding();selection++;sid='';state='unknown';empty=false;footer='';footerSegments=[];panelLines=[];
    for(const row of watchers.values())row.watch.close();watchers.clear();
    backend.stdin.write(JSON.stringify({id:++callID,op:'unregister',binding:old})+'\n');rebuildDisplay();return {ok:true};
  }
  if(req.event==='prompt'||req.event==='submit') {
    submitted=lastEnter;state='working';empty=false;
    if(currentDelivery){deliveries.set(currentDelivery,'submitted');currentDelivery=undefined;releaseHeld();rebuildDisplay();}
  } else if(req.event==='stop') { state='idle';empty=submitted>=0&&epoch===submitted; }
  else {state='blocked';empty=false;}
  projection();return {ok:true,...status()};
}
async function request(req) {
  if(req.op==='event')return lifecycle(req);
  if(!exact(req))return {ok:false,reason:'attachment-changed',not_started:true};
  if(req.op==='status')return {ok:true,...status()};
  if(req.op==='panel'){expanded=req.expanded===true;applyLayout();return {ok:true,...status()};}
  if(req.op!=='inject'||harness==='codex')return {ok:false,reason:'unsupported-operation',not_started:true};
  if(typeof req.ref!=='string'||req.ref.length>128)return {ok:false,reason:'peer-ref-unavailable',not_started:true};
  if(deliveries.has(req.ref))return {ok:true,duplicate:true,state:deliveries.get(req.ref)};
  const safe=()=>!relay&&exact(req)&&req.epoch===epoch&&state==='idle'&&empty&&!paste&&!keyTail&&!currentDelivery&&terminal.modes.bracketedPasteMode;
  if(!safe())return {ok:false,reason:'not-idle-empty',not_started:true};
  const selected=selection,data=await source('payload',{ref:req.ref,claim:req.claim});
  if(!data.ok||selected!==selection||!safe())return {ok:false,reason:data.reason||'input-changed',not_started:true};
  currentDelivery=req.ref;state='submitting';empty=false;epoch++;lastEnter=epoch;
  if(deliveries.size>=128)deliveries.delete(deliveries.keys().next().value);
  deliveries.set(req.ref,'transport-accepted');
  // Native TUIs distinguish a paste event from a later Enter. Keyboard bytes
  // remain ordered behind this submission until the native prompt callback.
  child.write('\x1b[200~'+data.text+'\x1b[201~');
  setTimeout(()=>{if(!ended&&selection===selected&&currentDelivery===req.ref)child.write('\r');},50);
  rebuildDisplay();return {ok:true,state:'transport-accepted'};
}
terminal.parser.registerCsiHandler({prefix:'?',final:'h'},p=>{if(p.includes(25))cursorVisible=true;return false;});
terminal.parser.registerCsiHandler({prefix:'?',final:'l'},p=>{if(p.includes(25))cursorVisible=false;return false;});
for(const [code,rgb] of [[10,'dddd/dddd/dddd'],[11,'1111/1111/1111']])terminal.parser.registerOscHandler(code,text=>{
  if(text==='?'){child.write(`\x1b]${code};rgb:${rgb}\x1b\\`);return true;}return false;
});
server=net.createServer(conn=>{
  let raw=Buffer.alloc(0),consumed=false;conn.setTimeout(2000,()=>conn.destroy());
  conn.on('error',()=>{});
  conn.on('data',chunk=>{
    if(consumed)return;raw=Buffer.concat([raw,chunk]);if(raw.length>65536)return conn.destroy();
    const newline=raw.indexOf(10);if(newline<0)return;consumed=true;conn.pause();
    setImmediate(async()=>{
      let result;try{result=await request(JSON.parse(raw.subarray(0,newline).toString()));}
      catch{result={ok:false,reason:'control-unavailable',not_started:false};}
      conn.end(JSON.stringify({...result,...identity()})+'\n');
    });
  });
});
server.on('error',()=>{if(!activated)bypass();else{state='unknown';empty=false;}});
server.listen(socketPath,()=>{
  fs.chmodSync(socketPath,0o600);
  try {
    child=pty.spawn(command[0],command.slice(1),{name:process.env.TERM||'xterm-256color',cols,rows:rows-1,cwd:process.cwd(),
      env:{...process.env,HEARTING_SHELL_SOCKET:socketPath,HEARTING_SHELL_TOKEN:token}});
    native=processRow(child.pid);rootNative=native;
  } catch {server.close();fs.rmSync(socketPath,{force:true});bypass();return;}
  // The parent may only fall back before this boundary. A failed display keeps
  // this same native child; no second CLI is ever started.
  startupResult('STARTED');activated=true;process.stdin.setRawMode(true);process.stdin.resume();
  backend=cp.spawn(process.env.HEARTING_SHELL_PYTHON,[path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});
  backend.on('error',()=>{});backend.stdin.on('error',()=>{});
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    try {const data=JSON.parse(line),call=calls.get(data.id);if(call){calls.delete(data.id);clearTimeout(call.timer);call.resolve(data);}}catch{}
  });
  emit('\x1b[?1049h\x1b[2J');
  terminal.onData(data=>child.write(data));terminal.onWriteParsed(schedule);
  child.onData(data=>{if(relay)emit(data);else terminal.write(data);});
  process.stdin.on('data',userInput);
  process.stdout.on('resize',()=>{cols=process.stdout.columns;rows=process.stdout.rows;if(relay)child.resize(cols,rows);else applyLayout();});
  child.onExit(async event=>{
    ended=true;clearTimeout(frameTimer);clearTimeout(projectionTimer);
    for(const row of watchers.values())row.watch.close();watchers.clear();
    server.close();await source('unregister');backend?.stdin.end();
    process.stdin.setRawMode(false);emit('\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l');
    fs.rmSync(socketPath,{force:true});process.exit(event.exitCode);
  });
  rebuildDisplay();schedule();
});
