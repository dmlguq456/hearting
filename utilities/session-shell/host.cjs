'use strict';
// Optional VT/PTY attachment. Messages and their receipts remain in peer-message.
const fs = require('node:fs');
const net = require('node:net');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
const args = process.argv.slice(2), harness = args.shift(), command = args;
let activated = false, child, inputOwner, backend, server, projectionTimer, frameTimer, ended = false;
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
const incarnation = crypto.randomBytes(24).toString('hex'), token = process.env.HEARTING_SHELL_TOKEN;
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
let nativeEpoch = 0, lastEnter = -1, submitted = -1, currentDelivery, releasing, heldBytes = 0;
let expanded = true, relay = false, paste = false, keyTail = '', cursorVisible = true;
let screenRevision = 0;
let last = [], footer = '', footerSegments = [], footerLast = '\0', panelRows = [], panelLines = [], projectionKey = '';
let sourceError, fleetView,blockCursor;
const deliveries = new Map(), calls = new Map(), watchers = new Map(), displayVTs = new Set();
let callID = 0, projecting = false, projectionPending = false;
let actionWorker;
const nativeDisplay=require('./native-display.cjs');
let nativeContext;
function binding() {
  return {schema:'hearting-session-shell-v1',harness,sid,incarnation,host_pid:host.pid,host_start:host.start,
    native_pid:native?.pid,native_start:native?.start,socket:socketPath,
    broker_pid:Number(process.env.HEARTING_SHELL_BROKER_PID),broker_start:process.env.HEARTING_SHELL_BROKER_START};
}
function identity() {
  const b = binding(); delete b.schema; delete b.socket; return b;
}
function live() { const row = native && processRow(native.pid); return !!row && row.start === native.start; }
function status() { return {...identity(),epoch,state,empty:empty && !keyTail && !paste && !block.focused && !fleetView,alive:live(),expanded,focused:block.focused,held_bytes:heldBytes,projection_state:relay||sourceError?"unavailable":"current"}; }
function source(op, extra={}) {
  if(op==='action')return readAction(extra);
  if (!backend || backend.exitCode !== null || !sid) return Promise.resolve({ok:false,reason:'source-unavailable'});
  return new Promise(resolve => {
    const id=++callID;
    const timer=setTimeout(()=>{calls.delete(id);resolve({ok:false,reason:'source-unavailable'});},1500);
    calls.set(id,{resolve,timer});
    const frame=JSON.stringify({id,op,binding:binding(),...extra})+'\n';
    if(Buffer.byteLength(frame)>65536){calls.delete(id);clearTimeout(timer);resolve({ok:false,reason:'source-request-too-large'});return;}
    backend.stdin.write(frame);
  });
}
function readAction(extra) {
  // A person-requested ledger/repo read may be slow. It must not queue ahead
  // of event-driven values or readiness on the ordinary projection worker.
  if(actionWorker)return Promise.resolve({ok:false,reason:'action-pending'});
  return new Promise(resolve=>{
    let raw='',settled=false;
    const worker=cp.spawn(process.env.HEARTING_SHELL_PYTHON,
      [path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});
    actionWorker=worker;
    const finish=value=>{if(!settled){settled=true;clearTimeout(timer);resolve(value);}};
    const timer=setTimeout(()=>finish({ok:false,reason:'action-unavailable'}),12000);
    worker.on('error',()=>finish({ok:false,reason:'action-unavailable'}));
    worker.stdin.on('error',()=>finish({ok:false,reason:'action-unavailable'}));
    worker.on('close',()=>{if(actionWorker===worker)actionWorker=undefined;finish({ok:false,reason:'action-unavailable'});});
    worker.stdout.setEncoding('utf8');
    worker.stdout.on('data',bytes=>{
      if(settled)return;
      raw+=bytes;if(Buffer.byteLength(raw)>65536){finish({ok:false,reason:'action-unavailable'});return;}
      if(raw.includes('\n'))try{finish(JSON.parse(raw.split('\n')[0]));}catch{finish({ok:false,reason:'action-unavailable'});}
    });
    worker.stdin.end(JSON.stringify({op:'action',binding:binding(),...extra})+'\n');
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
function maxHeight(){return Math.min(10,Math.max(4,Math.floor(rows/3)),Math.max(1,rows-4));}
function height(){
  if(fleetView)return 0;
  if(!expanded)return harness==='claude'?0:1;
  const detail=block.focused?(actionLines.length?actionLines:block.detail()):[];
  return nativeDisplay.blockHeight({harness,count:Math.max(panelLines.length,currentDelivery?1:0),
    detail:detail.length,limit:maxHeight(),loaded:!!projectionKey});
}
function schedule() { if(!frameTimer)frameTimer=setTimeout(render,8); }
function render() {
  frameTimer=undefined;if(ended||!activated||relay)return;
  try {
    const nativeLines=Array.from({length:terminal.rows},(_,i)=>
      terminal.buffer.active.getLine(terminal.buffer.active.viewportY+i)?.translateToString(true)||'');
    const observed=nativeDisplay.observe(harness,nativeLines,sid);
    const concealed=fleetView?new Set():nativeDisplay.hiddenFooterRows(harness,nativeLines,sid);
    if((observed||harness==='claude')&&JSON.stringify(observed)!==JSON.stringify(nativeContext)){
      nativeContext=observed;if(harness!=='codex')queueProjection();}
    let text='\x1b[?2026h\x1b[?25l\x1b[?7l';
    const view=fleetView?.terminal||terminal;
    for(let y=0;y<view.rows;y++) {
      const line=concealed.has(y)?'\x1b[0m'+' '.repeat(view.cols):serialize(view.buffer.active.getLine(view.buffer.active.viewportY+y),view.cols);
      if(last[y]!==line){text+=`\x1b[${y+1};1H${line}`;last[y]=line;}
    }
    if(!fleetView&&expanded)for(let y=0;y<panelRows.length;y++){
      const row=rows-height()+y;
      const cached=y===0&&!panelLines.length&&typeof nativeContext?.percent==='number'&&harness!=='claude'?
        '\x1b[0m\x1b[2K'+nativeDisplay.firstLine(nativeContext)[0].text:panelRows[y];
      if(last[row]!==cached){text+=`\x1b[${row+1};1H${cached}`;last[row]=cached;}
    }
    else if(!fleetView&&height()>0&&footerLast!==displayFooter){text+=`\x1b[${rows};1H\x1b[0m\x1b[2K${displayFooter}`;footerLast=displayFooter;}
    const b=view.buffer.active,m=view.modes;
    const cursor=block.focused&&!fleetView?blockCursor:null;
    text+=`\x1b[${cursor?.row||Math.min(b.cursorY+1,view.rows)};${cursor?.col||Math.min(b.cursorX+1,view.cols)}H`;
    text+=`\x1b[?2004${m.bracketedPasteMode?'h':'l'}\x1b[?1${m.applicationCursorKeysMode?'h':'l'}`;
    text+='\x1b[?1000l\x1b[?1002l\x1b[?1003l';
    if(m.mouseTrackingMode!=='none')text+=`\x1b[?${({x10:9,vt200:1000,drag:1002,any:1003})[m.mouseTrackingMode]}h\x1b[?1006h`;
    text+=`\x1b[?1004${m.sendFocusMode?'h':'l'}\x1b[?7h${(block.focused&&!fleetView?!!blockCursor:cursorVisible)?'\x1b[?25h':''}\x1b[?2026l`;
    emit(text);
  } catch { rawRelay(); }
}
function lineLength(line){
  if(!line)return 0;
  for(let x=cols-1;x>=0;x--){const cell=line.getCell(x);if(cell?.getChars())return x+cell.getWidth();}
  return 0;
}
function displayLines(lines,height,done) {
  if(displayVTs.size>=4){rawRelay();return;}
  const vt=new Terminal({cols,rows:height,scrollback:0,allowProposedApi:true,logLevel:'off'});displayVTs.add(vt);
  vt.loadAddon(new Unicode11Addon());vt.unicode.activeVersion='11';
  const safe=s=>String(s).replace(/[\x00-\x1f\x7f]/g,' ');
  const paint=line=>Array.isArray(line)?line.map(part=>{
    const attrs=[0];
    if(Number.isInteger(part.foreground)&&part.foreground>=0&&part.foreground<=255)attrs.push(38,5,part.foreground);
    if(part.bold===true)attrs.push(1);if(part.dim===true)attrs.push(2);if(part.reverse===true)attrs.push(7);
    return `\x1b[${attrs.join(';')}m${safe(part.text)}`;
  }).join(''):safe(line);
  try{vt.write('\x1b[?7l'+lines.slice(0,height).map((s,i)=>`\x1b[${i+1};1H${paint(s)}`).join(''),()=>{
    try{if(!relay&&!ended)done(Array.from({length:height},(_,y)=>serialize(vt.buffer.active.getLine(y),cols)),Array.from({length:height},(_,y)=>block.focused?lineLength(vt.buffer.active.getLine(y)):0));}
    catch{rawRelay();}
    finally{displayVTs.delete(vt);vt.dispose();}
    schedule();
  });}catch{displayVTs.delete(vt);vt.dispose();rawRelay();}
}
let rebuilding=false,rebuildPending=false,displayQueued=false,layoutQueued=false;
function queueDisplay(){
  if(displayQueued)return;displayQueued=true;
  setImmediate(()=>{displayQueued=false;try{if(!fleetView&&terminal.rows!==rows-height())applyLayout();else rebuildDisplay();}catch{rawRelay();}});
}
function queueLayout(){
  if(layoutQueued)return;layoutQueued=true;
  setImmediate(()=>{layoutQueued=false;try{applyLayout();}catch{rawRelay();}});
}
const block=require('./block-input.cjs')({changed:queueDisplay,
  answer:async fields=>{const result=await source('answer',fields);queueProjection();return result;},
  action:key=>blockAction(key)});
let actionLines=[];
function rebuildDisplay() {
  if(relay||ended||fleetView)return;
  if(!height()){panelRows=[];displayFooter='';blockCursor=null;schedule();return;}
  if(rebuilding){rebuildPending=true;return;}rebuilding=true;rebuildPending=false;
  const extra=currentDelivery?` · 전달 중${heldBytes?' · 입력 보존 '+heldBytes+'B':''}`:'';
  let lines,edit=-1;
  if(expanded){
    const base=panelLines.length?panelLines:(!projectionKey&&harness!=='claude'?
      [[{text:`📁 ${path.basename(process.cwd())} · ${harness} · CTX ?`,dim:true}]]:[]);
    const detail=block.focused?(actionLines.length?actionLines:block.detail()):[];
    if(detail.length){
      const prefix=base.slice(0,Math.max(0,height()-detail.length)),visible=detail.slice(-Math.max(1,height()-prefix.length));
      const at=visible.findIndex(line=>Array.isArray(line)&&line.some(part=>String(part.text).startsWith('› ')));
      edit=at<0?-1:prefix.length+at;lines=[...prefix,...visible];
    }else lines=base;
    if(extra)lines=[...lines.slice(0,height()-1),[{text:extra,dim:true}]];
  }else lines=[footerSegments.length?[...footerSegments,{text:extra}]:footer||`${harness} · CTX ?`];
  displayLines(lines,expanded?height():1,(painted,lengths)=>{
    blockCursor=edit>=0?{row:rows-height()+edit+1,col:Math.min(cols,lengths[edit]+1)}:null;
    panelRows=painted;displayFooter=painted[0];footerLast='\0';rebuilding=false;
    if(rebuildPending)rebuildDisplay();
  });
}
async function blockAction(key) {
  actionLines=[];
  if(key==='f'){openFleet();return;}
  actionLines=[[{text:'⠏',dim:true}]];queueDisplay();
  const selected=selection;
  const result=await source('action',{action:key});
  if(selected!==selection||!block.focused)return;
  actionLines=(result.lines||['?']).slice(0,height()-1).map(text=>[{text,dim:true}]);
  rebuildDisplay();
}
function openFleet() {
  if(fleetView||relay||currentDelivery)return;
  try{
    const vt=new Terminal({cols,rows,allowProposedApi:true,scrollback:1000,logLevel:'off'});
    vt.loadAddon(new Unicode11Addon());vt.unicode.activeVersion='11';
    const exe=path.join(__dirname,'..','..','tools','fleet','fleet.py');
    const session=pty.spawn(process.env.HEARTING_SHELL_PYTHON,[exe,'--explorer'],
      {cols,rows,cwd:process.cwd(),env:{...process.env}});
    const view={terminal:vt,child:session,closing:false,inputTail:'',paste:false};
    fleetView=view;block.leave();last=[];emit('\x1b[2J');
    vt.onData(data=>session.write(data));vt.onWriteParsed(schedule);
    session.onData(data=>{try{vt.write(data);}catch{session.write('q');rawRelay();}});
    session.onExit(()=>{
      vt.dispose();if(fleetView!==view)return;
      fleetView=undefined;block.leave();applyLayout();
    });
    schedule();
  }catch{actionLines=[[{text:'Fleet unavailable',dim:true}]];rebuildDisplay();}
}
// Footer is serialized once by the scratch VT; child parser state stays untouched.
let displayFooter='';
function applyLayout() {
  if(relay)return;
  if(fleetView){fleetView.child.resize(cols,rows);fleetView.terminal.resize(cols,rows);last=[];schedule();return;}
  child.resize(cols,rows-height());terminal.resize(cols,rows-height());last=[];footerLast='\0';
  emit('\x1b[0m\x1b[2J');rebuildDisplay();schedule();queueProjection();
}
function queueProjection() {
  if(ended||relay||projectionTimer)return;
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
  if(!sid||ended||relay)return;if(projecting){projectionPending=true;return;}
  projecting=true;projectionPending=false;const selected=selection;
  try{
    const data=await source('projection',{columns:cols,rows,state,expanded,native_context:nativeContext?.percent,native_badges:nativeContext});
    if(relay||selected!==selection)return;if(!data.ok){sourceError=data;return;}sourceError=undefined;
    // Close the read-before-watch gap once when the input set changes.
    if(observeFiles(data.watch))projectionPending=true;
    const key=JSON.stringify([harness==='claude'?null:[data.footer,data.segments],data.lines,data.questions,data.help,cols,expanded,currentDelivery,heldBytes]);
    if(key===projectionKey)return;const oldHeight=height();projectionKey=key;footer=data.footer;footerSegments=data.segments||[];panelLines=data.lines;block.update(data.questions,data.help);if(height()!==oldHeight)applyLayout();else rebuildDisplay();
  }finally{projecting=false;if(projectionPending)queueProjection();}
}
function rawRelay() {
  if(relay||ended)return;
  if(!activated){bypass();return;}relay=true;state='unknown';empty=false;
  if(fleetView){try{fleetView.child.write('q');}catch{}fleetView=undefined;}
  clearTimeout(frameTimer);clearTimeout(projectionTimer);frameTimer=projectionTimer=undefined;
  try{terminal.dispose();}catch{}
  for(const vt of displayVTs)try{vt.dispose();}catch{}
  displayVTs.clear();
  for(const row of watchers.values())row.watch.close();watchers.clear();
  try{emit('\x1b[0m\x1b[?25h\x1b[2J\x1b[H');}catch{}
  try{child?.resize(cols,rows);}catch{}
}
// A display exception never owns the lifetime of the native terminal.
function displayFailure(){if(relay)process.exit(1);rawRelay();}
process.on('uncaughtException',displayFailure);
process.on('unhandledRejection',displayFailure);
process.stdout.on('error',()=>process.exit(1));
process.stdin.on('error',()=>process.exit(1));

async function releaseHeld(ref,cancel=false) {
  if(currentDelivery!==ref||releasing===ref)return;releasing=ref;
  // No new direct writes may overtake keys the owner is still draining.
  process.stdin.pause();
  await inputOwner.release(ref,cancel);
  if(currentDelivery===ref){currentDelivery=undefined;heldBytes=0;if(state==='submitting')state='unknown';}
  releasing=undefined;process.stdin.resume();rebuildDisplay();
}
function screenSnapshot() {
  const lines=[],buffer=terminal.buffer.active;
  for(let y=0;y<terminal.rows;y++){
    const line=buffer.getLine(buffer.viewportY+y),runs=[];
    for(let x=0;x<terminal.cols;x++){
      const cell=line?.getCell(x);if(!cell||!cell.getWidth())continue;
      const dim=!!cell.isDim(),text=cell.getChars()||' ',previous=runs[runs.length-1];
      if(previous&&previous[1]===dim)previous[0]+=text;else runs.push([text,dim]);
    }
    lines.push(runs);
  }
  return lines;
}
async function refreshReadiness() {
  // A previous empty frame cannot acknowledge bytes still waiting inside the
  // native input loop. Only untouched startup or an exact submitted frontier
  // can be corroborated by the screen/native-idle observation.
  const inputSettled=nativeEpoch===0||(submitted>=0&&submitted===nativeEpoch);
  if(relay||paste||keyTail||currentDelivery||!sid||!inputSettled){empty=false;return;}
  const before=epoch,selected=selection,revision=screenRevision;
  const result=await source('readiness',{screen:screenSnapshot(),state});
  if(before!==epoch||selected!==selection||revision!==screenRevision||currentDelivery){empty=false;return;}
  empty=result.ok===true&&result.ready===true;
  if(empty&&state!=='idle'){state='idle';queueProjection();}
}
function observeInput(data) {
  // All keys invalidate an in-flight observation, but only native-bound bytes
  // can leave an unsubmitted native draft. Block/Fleet keys have no such bytes.
  const previous=nativeEpoch;epoch+=data.length;nativeEpoch+=data.length;empty=false;
  for(let offset=0;offset<data.length;offset++) {
    const c=String.fromCharCode(data[offset]);
    if(keyTail){keyTail+=c;if(keyTail==='\x1b[200~'){paste=true;keyTail='';}else if(keyTail==='\x1b[201~'){paste=false;keyTail='';}
      else if(keyTail.length>2&&/[\x40-\x7e]/.test(c)||keyTail.length>64)keyTail='';}
    else if(c==='\x1b')keyTail=c;
    else if(!paste&&(c==='\r'||c==='\n'))lastEnter=previous+offset+1;
  }
}
function userInput(data) {
  if(!data.length)return;
  if(currentDelivery){
    inputOwner.hold(data);observeInput(data);heldBytes+=data.length;
    if(heldBytes>=65536)process.stdin.pause();if(!relay)rebuildDisplay();return;
  }
  while(data.length){
    if(relay){child.write(data);return;}
    if(fleetView){
      const view=fleetView;
      if(view.closing){observeInput(data);child.write(data);return;}
      let exit=-1;
      for(let i=0;i<data.length;i++){
        view.inputTail=(view.inputTail+String.fromCharCode(data[i])).slice(-6);
        if(view.inputTail.endsWith('\x1b[200~'))view.paste=true;
        else if(view.inputTail.endsWith('\x1b[201~'))view.paste=false;
        else if(data[i]===0x1d&&!view.paste){exit=i;break;}
      }
      if(exit<0){view.child.write(data);epoch+=data.length;return;}
      if(exit)view.child.write(data.subarray(0,exit));epoch+=exit+1;
      view.closing=true;
      // Escape belongs to Fleet's own menus/CSI parser. Only our explicit
      // focus key exits the modal. Subsequent bytes immediately use the original
      // native PTY, so a display-host failure cannot lose another held draft.
      view.child.write('\x1b\x1bq');
      const rest=data.subarray(exit+1);if(rest.length){observeInput(rest);child.write(rest);}return;
    }
    if(block.focused){
      actionLines=[];const rest=block.input(data);epoch+=data.length-rest.length;
      data=rest;queueDisplay();continue;
    }
    const offset=data.indexOf(0x1d);
    if(offset<0){observeInput(data);child.write(data);return;}
    const prefix=data.subarray(0,offset);
    if(prefix.length){observeInput(prefix);child.write(prefix);}
    if(!paste&&!keyTail){epoch++;expanded=true;actionLines=[];block.enter();queueLayout();}
    else{const literal=data.subarray(offset,offset+1);observeInput(literal);child.write(literal);}
    data=data.subarray(offset+1);
  }
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
    if(nativeContext?.sid!==req.sid)nativeContext=undefined;
    sequence=req.seq;selection++;native=reporter;sid=req.sid;state='unknown';empty=false;submitted=-1;block.leave();block.update([]);
    if(currentDelivery)releaseHeld(currentDelivery,true);
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
    if(currentDelivery){deliveries.set(currentDelivery,'submitted');releaseHeld(currentDelivery,true);rebuildDisplay();}
  } else if(req.event==='stop'||req.event==='idle') { state='idle';empty=false; }
  else if(req.event==='working') { state='working';empty=false; }
  else {state='blocked';empty=false;}
  projection();return {ok:true,...status()};
}
async function request(req) {
  if(req.op==='event')return lifecycle(req);
  if(!exact(req))return {ok:false,reason:'attachment-changed',not_started:true};
  if(req.op==='status'){await refreshReadiness();return {ok:true,...status()};}
  if(req.op==='panel'&&relay)return {ok:false,reason:'display-unavailable'};
  if(req.op==='panel'){expanded=req.expanded===true;if(!expanded)block.leave();applyLayout();return {ok:true,...status()};}
  if(req.op!=='inject'||harness==='codex')return {ok:false,reason:'unsupported-operation',not_started:true};
  if(typeof req.ref!=='string'||req.ref.length>128)return {ok:false,reason:'peer-ref-unavailable',not_started:true};
  if(deliveries.has(req.ref))return {ok:true,duplicate:true,state:deliveries.get(req.ref)};
  const safe=()=>!relay&&exact(req)&&req.epoch===epoch&&state==='idle'&&empty&&!paste&&!keyTail&&!block.focused&&!fleetView&&!currentDelivery&&terminal.modes.bracketedPasteMode;
  if(!safe())return {ok:false,reason:'not-idle-empty',not_started:true};
  const selected=selection,revision=screenRevision;
  const data=await source('payload',{ref:req.ref,claim:req.claim,screen:screenSnapshot(),state});
  if(!data.ok||selected!==selection||revision!==screenRevision||!safe())return {ok:false,reason:data.reason||'input-changed',not_started:true};
  currentDelivery=req.ref;state='submitting';empty=false;epoch++;nativeEpoch++;lastEnter=nativeEpoch;
  if(deliveries.size>=128)deliveries.delete(deliveries.keys().next().value);
  deliveries.set(req.ref,'transport-accepted');
  // The retained owner is the sole writer for this admitted paste/50ms Enter
  // and shared held-byte buffer. A dead display cannot lose or replay them.
  await inputOwner.begin(req.ref,data.text);
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
    const broker=processRow(Number(process.env.HEARTING_SHELL_BROKER_PID));
    if(!broker||broker.pid!==host.parent||broker.start!==process.env.HEARTING_SHELL_BROKER_START)throw Error('PTY owner changed');
    child=require('./adopted-pty.cjs')(pty,Number(process.env.HEARTING_SHELL_PTY_FD),
      Number(process.env.HEARTING_SHELL_NATIVE_PID),Number(process.env.HEARTING_SHELL_PTY_EXIT_FD));
    inputOwner=require('./input-owner.cjs')(Number(process.env.HEARTING_SHELL_INPUT_FD),
      Number(process.env.HEARTING_SHELL_HELD_FD),ref=>{
        if(deliveries.get(ref)!=='submitted')deliveries.set(ref,'transport-submitted');
        releaseHeld(ref);
      });
    native=processRow(child.pid);rootNative=native;
  } catch {server.close();fs.rmSync(socketPath,{force:true});bypass();return;}
  // The parent may only fall back before this boundary. A failed display keeps
  // this same native child; no second CLI is ever started.
  startupResult('STARTED');activated=true;process.stdin.resume();
  backend=cp.spawn(process.env.HEARTING_SHELL_PYTHON,[path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});
  backend.on('error',rawRelay);backend.on('exit',rawRelay);backend.stdin.on('error',()=>{});
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    try {const data=JSON.parse(line),call=calls.get(data.id);if(call){calls.delete(data.id);clearTimeout(call.timer);call.resolve(data);}}catch{}
  });
  emit('\x1b[?1049h\x1b[2J');
  terminal.onData(data=>child.write(data));terminal.onWriteParsed(()=>{screenRevision++;schedule();});
  terminal.onTitleChange(title=>{
    try{if(!ended&&!relay)emit(nativeDisplay.titleSequence(title));}catch{rawRelay();}
  });
  child.onData(data=>{if(relay)emit(data);else try{terminal.write(data);}catch{rawRelay();emit(data);}});
  process.stdin.on('data',userInput);
  process.stdout.on('resize',()=>{cols=process.stdout.columns;rows=process.stdout.rows;try{if(relay)child.resize(cols,rows);else applyLayout();}catch{rawRelay();}});
  child.onExit(async event=>{
    ended=true;clearTimeout(frameTimer);clearTimeout(projectionTimer);
    for(const row of watchers.values())row.watch.close();watchers.clear();
    server.close();await source('unregister');backend?.stdin.end();
    emit('\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l');
    fs.rmSync(socketPath,{force:true});process.exit(event.exitCode);
  });
  applyLayout();
});
