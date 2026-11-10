'use strict';
// Optional VT/PTY attachment. Messages and their receipts remain in peer-message.
const fs = require('node:fs');
const net = require('node:net');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
const args = process.argv.slice(2), harness = args.shift(), command = args;
let activated = false, child, inputOwner, mouseInput, backend, server, projectionTimer, frameTimer, ended = false;
function startupResult(value) {
  const fd = Number(process.env.HEARTING_SHELL_START_FD);
  fs.writeSync(fd, value + '\n'); fs.closeSync(fd);
}
function bypass() { startupResult('BYPASS'); process.exit(0); }
let pty, Terminal, Unicode11Addon;
try {
  if (!['claude','codex','opencode'].includes(harness) || !command.length || !process.stdin.isTTY || !process.stdout.isTTY) throw Error();
  // The running host already knows its ABI; no auxiliary Node probe is needed.
  const key=crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'package-lock.json')))
    .update('\0').update(process.versions.modules).digest('hex').slice(0,24);
  const cache=process.env.XDG_CACHE_HOME||path.join(require('node:os').homedir(),'.cache');
  const assets=path.resolve(cache,'hearting','session-shell',key);
  const ready=JSON.parse(fs.readFileSync(path.join(assets,'ready.json'),'utf8'));
  if(!ready||ready.key!==key||Object.keys(ready).length!==1||
     !fs.statSync(path.join(assets,'node_modules','node-pty')).isDirectory())throw Error('assets unavailable');
  const assetRequire=require('node:module').createRequire(path.join(assets,'package.json'));
  pty=assetRequire('node-pty');({Terminal}=assetRequire('@xterm/headless'));
  ({Unicode11Addon}=assetRequire('@xterm/addon-unicode11'));
} catch { bypass(); }
const dir = process.env.HEARTING_SHELL_DIRECTORY;
try {
  const st = fs.lstatSync(dir);
  if (!st.isDirectory() || st.uid !== process.getuid() || st.mode & 0o077) throw Error();
} catch { bypass(); }
const socketPath = path.join(dir, 'control.sock');
let cols = process.stdout.columns, rows = process.stdout.rows;
if (cols < 20 || rows < 8) bypass();
const nativeDisplay=require('./native-display.cjs');
const panelView=require('./panel-view.cjs');
let displayConfiguration={...panelView.defaults};
try{displayConfiguration=JSON.parse(process.env.HEARTING_SHELL_LAYOUT||'{}');}catch{}
const initialGeometry=nativeDisplay.layout({cols,rows,configuration:displayConfiguration});
const terminal = new Terminal({cols:initialGeometry.nativeCols, rows:initialGeometry.side?rows:rows-1, allowProposedApi:true, scrollback:1000, logLevel:'off'});
terminal.loadAddon(new Unicode11Addon()); terminal.unicode.activeVersion = '11';
let widthProvider;
new Unicode11Addon().activate({unicode:{register:provider=>{widthProvider=provider;}}});
const cellWidth=text=>[...text].reduce((width,char)=>width+widthProvider.wcwidth(char.codePointAt(0)),0);
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
let nativeContext;
let displayValues={};
let nativeSelected={};
let modelReadKey='',modelReading=false,bootstrapModel=false;
let nativeFooter=null,paintedCommon=[],panelSections;
let panelModel={},panelHits=[],settings,routeDetail=false,routePage=0,routePageCount=1,dragging=false;
let displaySequence=0,nativeMcp;
function binding() {
  // An unselected seat (OpenCode home after /new) keeps its pane as '-': no
  // session lookup can match it, so nothing is ever delivered to an old session.
  return {schema:'hearting-session-shell-v1',harness,sid:sid||'-',incarnation,host_pid:host.pid,host_start:host.start,
    native_pid:native?.pid,native_start:native?.start,socket:socketPath,
    broker_pid:Number(process.env.HEARTING_SHELL_BROKER_PID),broker_start:process.env.HEARTING_SHELL_BROKER_START};
}
function identity() {
  const b = binding(); delete b.schema; delete b.socket; return b;
}
const publishDisplay=nativeDisplay.publisher(dir,identity);
function publishNativeDisplay() {
  publishDisplay({...displayValues,...(typeof nativeContext?.percent==='number'
    ?{context_percent:nativeContext.percent}:{})});
}
function live() { const row = native && processRow(native.pid); return !!row && row.start === native.start; }
function status() { return {...identity(),epoch,state,empty:empty && !keyTail && !paste && (!block.focused||block.waitingForReceipt) && !settings && !fleetView,alive:live(),expanded,focused:block.focused||!!settings,held_bytes:heldBytes,projection_state:relay||sourceError?"unavailable":"current"}; }
function source(op, extra={}) {
  if(op==='action'||op==='answer')return readAction(extra,op);
  if (!backend || backend.exitCode !== null || !sid&&!['display-settings','display-bootstrap','display-model','register','unregister'].includes(op)) return Promise.resolve({ok:false,reason:'source-unavailable'});
  return new Promise(resolve => {
    const id=++callID;
    // Startup display replies are finite, asynchronous reads. A busy backend
    // must not discard its eventual language/model result before first input.
    // They never delay native start or keys; backend exit settles all callers.
    const timer=['display-bootstrap','display-model'].includes(op)?null:
      setTimeout(()=>{calls.delete(id);resolve({ok:false,reason:'source-unavailable'});},1500);
    calls.set(id,{resolve,timer});
    const frame=JSON.stringify({id,op,binding:binding(),...extra})+'\n';
    if(Buffer.byteLength(frame)>65536){calls.delete(id);clearTimeout(timer);resolve({ok:false,reason:'source-request-too-large'});return;}
    backend.stdin.write(frame);
  });
}
function nativeSelectionFrame() {
  // Original native VT only: neither concealed paint nor the Hearting panel.
  const lines=Array.from({length:terminal.rows},(_,i)=>
    terminal.buffer.active.getLine(terminal.buffer.active.viewportY+i)?.translateToString(true)||'');
  while(lines.length&&!lines[lines.length-1].trim())lines.pop();
  const prompt=lines.findLastIndex(line=>line.trimStart().startsWith('›'));
  const tail=prompt<0?[]:lines.slice(prompt+1).map(line=>line.trim()).filter(Boolean);
  const frame={revision:screenRevision,epoch,lines:[]};
  // Refuse interstitial body without returning it. Keep the strict parser's
  // location-menu veto even when a menu happens to occupy only two rows.
  const flat=tail.join('').replace(/\s/g,'').toLowerCase();
  const menu=['whereshouldthenewconversationrun','whereshouldtheforkedconversationrun',
    'usecurrentgitworktree','keepusingthecurrentworkingdirectory','createnewgitworktree'];
  if(tail.length>2||flat.length>16384||menu.some(word=>flat.includes(word)))return {...frame,invalid:true};
  // Codex 0.163 draws a configured status line with no help row under an idle
  // composer. Accept exactly the row pane_ownership._codex_status_only accepts
  // (whole thread-id first, then Context or the repeated unnamed-thread title;
  // a warning or key hint may follow) and pass on only that identity evidence.
  const uuid='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  const statusOnly=row=>{
    const fields=row.split('·').map(value=>value.trim());
    if(fields.length<2||!new RegExp('^'+uuid+'$').test(fields[0]))return '';
    const context=new RegExp('^'+uuid+' · (?:[^·]* · )*Context (\\d{1,3})% used(?: ·|$)').exec(row);
    if(context)return fields[0]+' · Context '+context[1]+'% used';
    const repeated=fields[1].split(/\s+/).filter(Boolean)[0]||'';
    return repeated===fields[0]||repeated.endsWith('…')&&repeated.length>8
      &&fields[0].startsWith(repeated.slice(0,-1))?fields[0]+' · '+repeated:'';
  };
  const only=tail.length===1?statusOnly(tail[0]):'';
  if(only)return {...frame,lines:['›',only]};
  const hint=tail[tail.length-1]||'';
  const compact=/^(?:(?:ctrl|shift|alt)\+|[⌃⇧⌥⌘^])*(?:[←→↑↓]|f\d{1,2}|enter|space|tab|esc|pgup|pgdn|home|end|[a-z0-9])(?: (?:(?:ctrl|shift|alt)\+|[⌃⇧⌥⌘^])*(?:[←→↑↓]|f\d{1,2}|enter|space|tab|esc|pgup|pgdn|home|end|[a-z0-9]))? for agents(?![0-9A-Za-z])/.exec(hint);
  const help=hint.toLowerCase().replace(/\s/g,'').includes('?forshortcuts')?'? for shortcuts':compact?.[0];
  if(!help)return frame;
  if(tail.length!==2)return {...frame,invalid:true};
  // Only identity-shaped tokens leave the native viewport. Titles, telemetry
  // and even footer-shaped draft text never travel in this read-only response.
  const fields=tail[0].split('·'),safe=value=>/^(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8}-[0-9a-f-]{0,27}…)$/.test(value.trim())?value.trim():'?';
  const status=fields.length===1?safe(fields[0]):safe(fields[0])+' · '+safe(fields[fields.length-1]);
  return {...frame,lines:['›',status,help]};
}
let checkingNativeSelection=false,nativeSelectionRevision=-1;
function observeNativeSelection() {
  if(harness!=='codex'||!sid||ended||relay||checkingNativeSelection||nativeSelectionRevision===screenRevision)return;
  const frame=nativeSelectionFrame();
  // This is only an observation hint. Python owns the strict footer judgment,
  // native root ownership and repeated, birth-bound snapshot verification.
  const fields=(frame.lines.slice(1).find(line=>line.trim())||'').split('·');
  const ids=[fields[0],fields[fields.length-1]].map(value=>value.trim())
    .filter(value=>/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value));
  if(!ids.some(id=>id!==sid))return;
  checkingNativeSelection=true;nativeSelectionRevision=frame.revision;
  const selected=selection;
  source('selection').then(data=>{
    if(!data.ok||relay||ended||selected!==selection||frame.revision!==screenRevision||frame.epoch!==epoch
       ||data.native_revision!==screenRevision||data.native_epoch!==epoch||data.selected_sid===sid)return;
    return lifecycle({token,harness,pid:native.pid,start:native.start,
      sid:data.selected_sid,seq:sequence+1,event:'select'});
  }).finally(()=>{checkingNativeSelection=false;});
}
function readAction(extra,operation='action') {
  // A person-requested read or answer may be slow. It must not queue ahead
  // of event-driven values or readiness on the ordinary projection worker.
  if(actionWorker)return Promise.resolve({ok:false,reason:'action-pending'});
  return new Promise(resolve=>{
    let raw='',settled=false;
    const worker=cp.spawn(process.env.HEARTING_SHELL_PYTHON,
      [path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});
    actionWorker=worker;queueProjection();
    const finish=value=>{if(!settled){settled=true;clearTimeout(timer);resolve(value);}};
    const timer=setTimeout(()=>finish({ok:false,reason:'action-unavailable'}),12000);
    worker.on('error',()=>finish({ok:false,reason:'action-unavailable'}));
    worker.stdin.on('error',()=>finish({ok:false,reason:'action-unavailable'}));
    worker.on('close',()=>{if(actionWorker===worker){actionWorker=undefined;queueProjection();}finish({ok:false,reason:'action-unavailable'});});
    worker.stdout.setEncoding('utf8');
    worker.stdout.on('data',bytes=>{
      if(settled)return;
      raw+=bytes;if(Buffer.byteLength(raw)>65536){finish({ok:false,reason:'action-unavailable'});return;}
      if(raw.includes('\n'))try{finish(JSON.parse(raw.split('\n')[0]));}catch{finish({ok:false,reason:'action-unavailable'});}
    });
    worker.stdin.end(JSON.stringify({op:operation,binding:binding(),...extra})+'\n');
  });
}
function emit(value) { process.stdout.write(value); }
function color(c,bg) {
  const v=bg?c.getBgColor():c.getFgColor();
  if(bg?c.isBgRGB():c.isFgRGB())return `${bg?48:38};2;${v>>16};${v>>8&255};${v&255}`;
  if(bg?c.isBgPalette():c.isFgPalette())return `${bg?48:38};5;${v}`;
  return bg?'49':'39';
}
function serialize(line,width,from=0) {
  let s='',previous='';
  for(let x=from;x<width;x++) {
    const c=line?.getCell(x); if(!c||!c.getWidth())continue;
    const a=[0,color(c,false),color(c,true)];
    for(const [method,sgr] of [['isBold',1],['isDim',2],['isItalic',3],['isUnderline',4],['isBlink',5],['isInverse',7],['isInvisible',8],['isStrikethrough',9],['isOverline',53]])if(c[method]())a.push(sgr);
    const attr=a.join(';');if(attr!==previous)s+=`\x1b[${attr}m`;
    s+=c.getChars()||' ';previous=attr;
  }
  return s+'\x1b[0m';
}
function geometry(){return nativeDisplay.layout({cols,rows,expanded,configuration:displayConfiguration});}
function panelColumns(){return geometry().panelCols;}
function nativeColumns(){return geometry().nativeCols;}
function nativeRows(){return geometry().side?rows:rows-height();}
function panelRow(){return geometry().side?0:rows-height();}
function maxHeight(){return geometry().side?rows:Math.min(block.focused?Math.max(12,displayConfiguration.bottom_rows||14):displayConfiguration.narrow==='compact'&&!settings?8:displayConfiguration.bottom_rows||14,Math.max(4,rows-6));}
function panelContent(){
  const view=panelView.render({width:panelColumns(),height:maxHeight(),side:geometry().side,
    model:panelModel,native:displayValues,state,sid,sections:panelSections,configuration:displayConfiguration,
    settings,routeDetail,routePage,focused:block.focused,cwd:process.cwd(),harness,
    cellWidth,
    questions:options=>actionLines.length&&block.focused?actionLines:block.detail(options)});
  panelHits=view.hits;routePageCount=view.routePages;return view.rows;
}
function height(){
  if(fleetView)return 0;
  if(!expanded)return harness==='claude'?0:1;
  if(geometry().side)return rows;
  const content=panelContent();
  return Math.min(maxHeight(),Math.max(content.length,currentDelivery?1:0));

}
function schedule() { if(!frameTimer)frameTimer=setTimeout(render,8); }
function displayModelKey(lines){return lines.slice(-4).join('\n');}
function observeSelectedModel(lines){
  if(harness!=='codex'||modelReading||!native||nativeSelected.model)return;
  const key=displayModelKey(lines);
  if(key===modelReadKey||!/\? for shortcuts/.test(key)||!/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/.test(key))return;
  modelReadKey=key;modelReading=true;const selected=selection,start=native.start;
  source('display-model',{lines}).then(data=>{
    const now=Array.from({length:terminal.rows},(_,i)=>terminal.buffer.active.getLine(terminal.buffer.active.viewportY+i)?.translateToString(true)||'');
    if(data.ok&&!relay&&live()&&native.start===start&&selection===selected&&displayModelKey(now)===key){
      nativeSelected={...data.display};bootstrapModel=true;displayValues={...displayValues,...nativeSelected};publishNativeDisplay();queueDisplay();
    }
  }).finally(()=>{modelReading=false;});
}
function render() {
  frameTimer=undefined;if(ended||!activated||relay)return;
  try {
    observeNativeSelection();
    const nativeLines=Array.from({length:terminal.rows},(_,i)=>
      terminal.buffer.active.getLine(terminal.buffer.active.viewportY+i)?.translateToString(true)||'');
    const observed=nativeDisplay.observe(harness,nativeLines,sid);
    observeSelectedModel(nativeLines);
    const selectedChanged=!!observed?.model&&(observed.model!==displayValues.model||(observed.effort||'')!==(displayValues.effort||''));
    const footerMask=fleetView?{rows:new Set(),cells:new Map()}:nativeDisplay.footerConcealment(harness,nativeLines,sid,nativeFooter,paintedCommon,(panelModel.status||[]).map(row=>row.map(p=>p.text).join('')));
    const concealed=footerMask.rows;
    let notice=-1;
    if(harness==='claude'){
      // The first concealed row (the top of the status area) carries the notice;
      // any further concealed row stays blank.
      if(claudePanelPainted())for(const y of nativeDisplay.claudeStatusRows(nativeLines,sid,claudeStatusRecord())){concealed.add(y);if(notice<0||y<notice)notice=y;}
    }
    if((observed||harness==='claude')&&JSON.stringify(observed)!==JSON.stringify(nativeContext)){
      nativeContext=observed;
      if(observed?.model){bootstrapModel=false;nativeSelected={model:observed.model,effort:observed.effort||''};displayValues={...displayValues,...nativeSelected};}
      if(typeof observed?.percent==='number')displayValues.context_percent=observed.percent;
      publishNativeDisplay();queueDisplay();if(harness!=='codex')queueProjection();}
    // Paint the observed selected model with the same native frame, rather
    // than first emitting a known model beside an obsolete Checking row.
    if(selectedChanged)return;
    let text='\x1b[?2026h\x1b[?25l\x1b[?7l';
    const view=fleetView?.terminal||terminal;
    for(let y=0;y<view.rows;y++) {
      // The rest of a Codex footer row whose ID cells are concealed keeps the
      // row's own indent instead of starting after a blank gap.
      const masked=fleetView?0:footerMask.cells.get(y)||0,indent=masked?Math.min(masked,nativeLines[y].match(/^ */)[0].length):0;
      let line=y===notice?noticeRow(view.cols):concealed.has(y)?'\x1b[0m'+' '.repeat(view.cols):masked?
        '\x1b[0m'+' '.repeat(indent)+serialize(view.buffer.active.getLine(view.buffer.active.viewportY+y),view.cols,masked)+' '.repeat(masked-indent):
        serialize(view.buffer.active.getLine(view.buffer.active.viewportY+y),view.cols);
      if(!fleetView&&geometry().side){
        const panel=y<panelRows.length?panelRows[y]:' '.repeat(panelColumns());
        line=geometry().left?panel+`\x1b[${panelColumns()+1}G\x1b[0m  `+line:line+`\x1b[${view.cols+1}G\x1b[0m  `+panel;
      }
      if(last[y]!==line){text+=`\x1b[${y+1};1H${line}`;last[y]=line;}
    }
    if(!fleetView&&expanded&&!geometry().side)for(let y=0;y<panelRows.length;y++){
      const row=rows-height()+y;
      const cached=y===0&&!panelLines.length&&typeof nativeContext?.percent==='number'&&harness!=='claude'?
        '\x1b[0m\x1b[2K'+nativeDisplay.firstLine(nativeContext)[0].text:panelRows[y];
      if(last[row]!==cached){text+=`\x1b[${row+1};1H${cached}`;last[row]=cached;}
    }
    else if(!fleetView&&!geometry().side&&height()>0&&footerLast!==displayFooter){text+=`\x1b[${rows};1H\x1b[0m\x1b[2K${displayFooter}`;footerLast=displayFooter;}
    const b=view.buffer.active,m=view.modes;
    const cursor=block.focused&&!fleetView?blockCursor:null;
    text+=`\x1b[${cursor?.row||Math.min(b.cursorY+1,view.rows)};${cursor?.col||Math.min(b.cursorX+1,view.cols)+(fleetView?0:geometry().nativeColumn)}H`;
    text+=`\x1b[?2004${m.bracketedPasteMode?'h':'l'}\x1b[?1${m.applicationCursorKeysMode?'h':'l'}`;
    text+='\x1b[?1000l\x1b[?1002l\x1b[?1003l';
    if(expanded&&!fleetView&&height()>0)text+='\x1b[?1002h\x1b[?1006h';
    else if(m.mouseTrackingMode!=='none')text+=`\x1b[?${({x10:9,vt200:1000,drag:1002,any:1003})[m.mouseTrackingMode]}h\x1b[?1006h`;
    text+=`\x1b[?1004${m.sendFocusMode?'h':'l'}\x1b[?7h${(block.focused&&!fleetView?!!blockCursor:cursorVisible)?'\x1b[?25h':''}\x1b[?2026l`;
    emit(text);
  } catch { rawRelay(); }
}
function lineLength(line){
  if(!line)return 0;
  for(let x=panelColumns()-1;x>=0;x--){const cell=line.getCell(x);if(cell?.getChars())return x+cell.getWidth();}
  return 0;
}
function displayLines(lines,height,done) {
  if(displayVTs.size>=4){rawRelay();return;}
  const width=panelColumns();
  const vt=new Terminal({cols:width,rows:height,scrollback:0,allowProposedApi:true,logLevel:'off'});displayVTs.add(vt);
  vt.loadAddon(new Unicode11Addon());vt.unicode.activeVersion='11';
  const safe=s=>String(s).replace(/[\x00-\x1f\x7f]/g,' ');
  const paint=line=>Array.isArray(line)?line.map(part=>{
    const attrs=[0];
    if(Number.isInteger(part.foreground)&&part.foreground>=0&&part.foreground<=255)attrs.push(38,5,part.foreground);
    if(Number.isInteger(part.background)&&part.background>=0&&part.background<=255)attrs.push(48,5,part.background);
    if(part.bold===true)attrs.push(1);if(part.dim===true)attrs.push(2);if(part.reverse===true)attrs.push(7);
    return `\x1b[${attrs.join(';')}m${safe(part.text)}`;
  }).join(''):safe(line);
  try{vt.write('\x1b[?7l'+lines.slice(0,height).map((s,i)=>`\x1b[${i+1};1H${paint(s)}`).join(''),()=>{
    try{if(!relay&&!ended)done(Array.from({length:height},(_,y)=>serialize(vt.buffer.active.getLine(y),width)),Array.from({length:height},(_,y)=>block.focused?lineLength(vt.buffer.active.getLine(y)):0),Array.from({length:height},(_,y)=>vt.buffer.active.getLine(y)?.translateToString(true)||''));}
    catch{rawRelay();}
    finally{displayVTs.delete(vt);vt.dispose();}
    schedule();
  });}catch{displayVTs.delete(vt);vt.dispose();rawRelay();}
}
let rebuilding=false,rebuildPending=false,displayQueued=false,layoutQueued=false;
function queueDisplay(){
  if(displayQueued)return;displayQueued=true;
  setImmediate(()=>{displayQueued=false;try{if(!fleetView&&(terminal.rows!==nativeRows()||terminal.cols!==nativeColumns()))applyLayout();else rebuildDisplay();}catch{rawRelay();}});
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
  if(!height()){panelRows=[];paintedCommon=[];displayFooter='';blockCursor=null;schedule();return;}
  if(rebuilding){rebuildPending=true;return;}rebuilding=true;rebuildPending=false;
  const extra=currentDelivery?` · 전달 중${heldBytes?' · 입력 보존 '+heldBytes+'B':''}`:'';
  let lines,edit=-1;
  if(expanded){
    const base=panelContent();
    const detail=[];
    if(detail.length){
      const prefix=base.slice(0,Math.max(0,height()-detail.length)),visible=detail.slice(-Math.max(1,height()-prefix.length));
      const at=visible.findIndex(line=>Array.isArray(line)&&line.some(part=>String(part.text).startsWith('› ')));
      edit=at<0?-1:prefix.length+at;lines=[...prefix,...visible];
    }else lines=base;
    if(block.focused&&!settings)edit=lines.findIndex(line=>line.some(part=>String(part.text).startsWith('› ')&&String(part.text).length>2));
    if(extra)lines=[...lines.slice(0,height()-1),[{text:extra,dim:true}]];
  }else lines=footerSegments.length?panelView.appearance([[...footerSegments,{text:extra}]],{...displayConfiguration,shade:'none'},
    {colors:panelModel.colors??256,noColor:panelModel.no_color===true}):[footer||`${harness} · CTX ?`];
  const editParts=edit>=0?lines[edit]:[];
  const editAt=editParts.findIndex(part=>String(part.text).startsWith('› ')&&String(part.text).length>2);
  const editColumn=editAt>=0?cellWidth(editParts.slice(0,editAt+1).map(part=>part.text).join(''))+1:0;
  displayLines(lines,expanded?height():1,(painted,lengths,plain)=>{
    blockCursor=edit>=0?{row:panelRow()+edit+1,col:geometry().panelColumn+Math.min(panelColumns()-2,editColumn)}:null;
    panelRows=painted;
    paintedCommon=plain.map(row=>row.startsWith('│ ')&&row.endsWith(' │')?'  '+row.slice(2,-2).trimEnd():row);
    displayFooter=painted[0];footerLast='\0';rebuilding=false;
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
  child.resize(nativeColumns(),nativeRows());terminal.resize(nativeColumns(),nativeRows());last=[];footerLast='\0';
  emit('\x1b[0m\x1b[2J');rebuildDisplay();schedule();queueProjection();
}
function queueProjection() {
  if(ended||relay||projectionTimer)return;
  nativeSelectionRevision=-1; // Existing lifecycle/file events may settle a prior observation.
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
    const data=await source('projection',{columns:panelColumns(),rows,state,expanded,native_context:nativeContext?.percent,native_badges:nativeMcp?{...(nativeContext||{}),mcp:nativeMcp}:nativeContext,retained_questions:block.references(),answer_busy:!!actionWorker||!!currentDelivery});
    if(relay||selected!==selection)return;if(!data.ok){sourceError=data;return;}sourceError=undefined;
    // Close the read-before-watch gap once when the input set changes.
    if(observeFiles(data.watch))projectionPending=true;
    const key=JSON.stringify([harness==='claude'?null:[data.footer,data.segments],data.lines,data.block,data.questions,data.help,data.native_footer,data.panel,cols,expanded,currentDelivery,heldBytes]);
    const freshDisplay=Object.fromEntries(Object.entries(data.native_display||{}).filter(([,value])=>value!=null));
    if(bootstrapModel&&freshDisplay.model){nativeSelected={};bootstrapModel=false;}
    displayValues={...displayValues,...freshDisplay,...nativeSelected};publishNativeDisplay();
    if(key===projectionKey)return;const oldHeight=height();projectionKey=key;nativeFooter=data.native_footer||null;footer=data.footer;footerSegments=data.segments||[];panelLines=data.lines;panelSections=data.block;panelModel={...data.panel,answerable:data.questions?.some(item=>item.answerable===true&&(item.request_state||'open')==='open')};block.update(data.questions,data.help,data.block);if(height()!==oldHeight)applyLayout();else rebuildDisplay();
  }finally{projecting=false;if(projectionPending)queueProjection();}
}
// Claude's statusline row may be blanked only while this visible panel paints
// the same session's model, compact status and session rows (folder, title,
// branch, Git, jobs). Any other state, including a panel too short for all of
// them, shows it.
function claudePanelPainted(){
  if(harness!=='claude'||relay||ended||fleetView||!activated||!sid||!expanded||settings||!height())return false;
  const rows=[...(panelModel.summary||[]),...(panelModel.session||[])].map(row=>row.map(part=>String(part.text)).join('').trim()).filter(Boolean);
  return rows.length>0&&rows.every(text=>paintedCommon.some(row=>row.includes(text)));
}
// The blanked Claude row carries the panel's one-line notice (empty by default),
// at the native statusline's own two-cell indent.
function noticeRow(width){
  let used=2,text='\x1b[0m  ';
  // Claude's own status row: the panel's theme, never its shade.
  const [notice=[]]=panelView.appearance([panelModel.notice||[]],{...displayConfiguration,shade:'none'},
    {colors:panelModel.colors??256,noColor:panelModel.no_color===true});
  for(const part of notice){
    const attrs=[0];
    if(Number.isInteger(part.foreground)&&part.foreground>=0&&part.foreground<=255)attrs.push(38,5,part.foreground);
    if(part.bold===true)attrs.push(1);if(part.dim===true)attrs.push(2);
    let chunk='';
    for(const c of String(part.text).replace(/[\x00-\x1f\x7f]/g,' ')){const w=cellWidth(c);if(used+w>width-1)break;chunk+=c;used+=w;}
    text+=`\x1b[${attrs.join(';')}m${chunk}`;if(used>=width-1)break;
  }
  return text+'\x1b[0m'+' '.repeat(Math.max(0,width-used));
}
let claudeStatus={key:'',value:null};
function claudeStatusRecord(){
  try{
    const s=fs.lstatSync(path.join(dir,'claude-status.json'));
    if(!s.isFile()||s.uid!==process.getuid()||s.size>65536)return null;
    const key=[s.ino,s.size,s.mtimeMs].join(':');
    if(key!==claudeStatus.key)claudeStatus={key,value:JSON.parse(fs.readFileSync(path.join(dir,'claude-status.json'),'utf8'))};
    return claudeStatus.value;
  }catch{return null;}
}
function rawRelay() {
  if(relay||ended)return;
  if(!activated){bypass();return;}relay=true;state='unknown';empty=false;
  if(fleetView){try{fleetView.child.write('q');}catch{}fleetView=undefined;}
  clearTimeout(frameTimer);clearTimeout(projectionTimer);frameTimer=projectionTimer=undefined;
  mouseInput?.flush();
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
  if(currentDelivery===ref){currentDelivery=undefined;heldBytes=0;if(state==='submitting')state='unknown';queueProjection();}
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
  epoch+=data.length;empty=false;
  for(let offset=0;offset<data.length;offset++) {
    nativeEpoch++;
    const c=String.fromCharCode(data[offset]);
    if(keyTail){keyTail+=c;
      // FocusIn/FocusOut report terminal focus, not a composer edit. Keep the
      // observation epoch above, including split reports; pasted bytes count.
      if(!paste&&(keyTail==='\x1b[I'||keyTail==='\x1b[O')){nativeEpoch-=keyTail.length;keyTail='';}
      else if(keyTail==='\x1b[200~'){paste=true;keyTail='';}else if(keyTail==='\x1b[201~'){paste=false;keyTail='';}
      else if(keyTail.length>2&&/[\x40-\x7e]/.test(c)||keyTail.length>64)keyTail='';}
    else if(c==='\x1b')keyTail=c;
    else if(!paste&&(c==='\r'||c==='\n'))lastEnter=nativeEpoch;
  }
}
function openSettings(){
  settings={value:{...panelView.defaults,...displayConfiguration},before:{...displayConfiguration},index:0};
  block.leave();epoch++;queueLayout();
}
function previewSettings(){displayConfiguration={...settings.value};queueLayout();}
async function applySettings(){
  const current=settings;if(!current||current.saving)return;current.saving=true;
  const result=await source('display-settings',{settings:current.value});
  if(settings!==current)return;
  if(result.ok){displayConfiguration=result.settings;settings=undefined;}
  else{current.saving=false;current.error=true;}
  queueLayout();
}
function cancelSettings(){if(settings&&!settings.saving){displayConfiguration=settings.before;settings=undefined;queueLayout();}}
function panelAction(action){
  epoch++;empty=false;
  if(settings?.saving)return;
  if(action==='settings'){openSettings();return;}
  if(action==='fleet'){openFleet();return;}
  if(action==='apply'){applySettings();return;}
  if(action==='cancel'){cancelSettings();return;}
  if(action==='defaults'&&settings){settings.value={...panelView.defaults};previewSettings();return;}
  if(action==='route'){routeDetail=!routeDetail;routePage=0;block.leave();}
  if((action==='route-prev'||action==='route-next')&&routeDetail){routePage=Math.max(0,Math.min(routePageCount-1,routePage+(action==='route-prev'?-1:1)));}
  if(action==='question'){routeDetail=false;block.enter();}
  if(action.startsWith('setting:')&&settings){
    const [,field,operation]=action.split(':'),index=Number(field);if(index<0||index>=panelView.fields.length)return;
    settings.index=index;if(operation!=='select')settings.value=panelView.change(settings.value,panelView.fields[index],operation==='down'?-1:1);previewSettings();return;
  }
  queueDisplay();
}
let settingsTail='',settingsEscapeTimer,settingsPaste=false;
function settingsInput(data){
  clearTimeout(settingsEscapeTimer);
  if(settings?.saving)return;
  settingsTail+=data.toString('utf8');
  while(settingsTail&&settings){
    if(settingsPaste){const end=settingsTail.indexOf('\x1b[201~');if(end<0){settingsTail=settingsTail.slice(-5);return;}settingsTail=settingsTail.slice(end+6);settingsPaste=false;continue;}
    if(settingsTail.startsWith('\x1b[200~')){settingsTail=settingsTail.slice(6);settingsPaste=true;continue;}
    if('\x1b[200~'.startsWith(settingsTail)&&settingsTail.length>2)return;
    if(/^\x1b\[?$/.test(settingsTail)){
      settingsEscapeTimer=setTimeout(()=>{if(settingsTail==='\x1b'){settingsTail='';cancelSettings();}},25);return;
    }
    const key=/^\x1b\[[ABCDZ]/.exec(settingsTail)?.[0]||settingsTail[0];settingsTail=settingsTail.slice(key.length);
    if(key==='\x1b'||key==='\x1d'){cancelSettings();break;}
    if(key==='\r'||key==='\n'){applySettings();continue;}
    if(key==='\t'||key==='\x1b[B')settings.index=(settings.index+1)%panelView.fields.length;
    else if(key==='\x1b[A'||key==='\x1b[Z')settings.index=(settings.index+panelView.fields.length-1)%panelView.fields.length;
    else if(['\x1b[D','\x1b[C','-','+'].includes(key)){
      settings.value=panelView.change(settings.value,panelView.fields[settings.index],['\x1b[D','-'].includes(key)?-1:1);previewSettings();
    }else if(key==='d'){settings.value={...panelView.defaults};previewSettings();}
  }
  settingsTail='';queueDisplay();
}
function panelMouse(code,x,y,release){
  if(fleetView||paste||relay)return false;
  const g=geometry(),local=x-1-g.panelColumn,row=y-1-panelRow();
  if(dragging){
    if(!release){displayConfiguration={...displayConfiguration,side_width:Math.max(32,Math.min(80,g.left?x:cols-x+1))};if(settings)settings.value={...displayConfiguration};queueLayout();}
    else{dragging=false;source('display-settings',{settings:{...panelView.defaults,...displayConfiguration}}).then(r=>{if(!r.ok)openSettings();});}
    return true;
  }
  // The boundary beside the native area resizes: the left edge of a right
  // panel, the right edge of a left one.
  if(g.side&&local===(g.left?panelColumns()-1:0)&&(code&3)===0&&!release){dragging=true;return true;}
  if(row<0||row>=height()||local<0||local>=panelColumns())return terminal.modes.mouseTrackingMode==='none';
  if(release||code>=32||(code&3)!==0)return true;
  const hit=panelHits.find(h=>h.row===row&&local>=h.left&&local<=h.right);
  if(hit){
    if(hit.action==='settings-buttons')panelAction(local<panelColumns()/3?'apply':local<panelColumns()*2/3?'cancel':'defaults');
    else panelAction(hit.action);
  }
  return true;
}
function displayReporter(req){
  const row=processRow(req.pid),root=rootNative&&processRow(rootNative.pid);
  return harness==='opencode'&&req.token===token&&row&&row.start===req.start&&root&&root.start===rootNative.start
    &&row.pid===native?.pid&&row.start===native?.start&&descends(req.pid,root.pid)&&sameTerminal(req.pid,root.pid);
}
mouseInput=require('./panel-mouse.cjs')({
  suppressed:()=>paste||block.pasting||settingsPaste||relay||!!fleetView,
  bytes:inputBytes,
  mouse:(code,x,y,release,raw)=>{
    if(panelMouse(code,x,y,release))return;
    if(terminal.modes.mouseTrackingMode==='none')return;
    // A left panel moves the native area right; native sees its own columns.
    const offset=geometry().nativeColumn;
    if(offset){if(x<=offset)return;raw=Buffer.from(`\x1b[<${code};${x-offset};${y}${release?'m':'M'}`,'latin1');}
    if(!release&&(code&3)===0){block.leave();queueDisplay();}
    if(currentDelivery){inputOwner.hold(raw);observeInput(raw);heldBytes+=raw.length;return;}
    observeInput(raw);child.write(raw);
  }
});
function userInput(data){if(data.length){epoch+=data.length;empty=false;mouseInput.feed(data);}}
function inputBytes(data) {
  if(!data.length)return;
  while(data.length){
    // Mouse-opened shell editors own their visible keys even while a peer's
    // paste is admitted. Only native-directed bytes enter the retained queue.
    if(settings&&!relay){settingsInput(data);epoch+=data.length;return;}
    if(routeDetail&&!relay&&!paste){const pageKey=/^\x1b\[([56])~/.exec(data.toString('latin1'));if(pageKey){panelAction(pageKey[1]==='5'?'route-prev':'route-next');data=data.subarray(pageKey[0].length);continue;}}
    if(!relay&&routeDetail&&(data[0]===27&&data.length===1||data[0]===29)){routeDetail=false;queueDisplay();data=data.subarray(1);continue;}
    if(currentDelivery&&(!block.focused||relay)){
      inputOwner.hold(data);observeInput(data);heldBytes+=data.length;
      if(heldBytes>=65536)process.stdin.pause();if(!relay)rebuildDisplay();return;
    }
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
    // Escape has already reached native. Ctrl+] cannot continue an Escape/CSI
    // prefix; keeping that lone byte pending would disable focus after a modal.
    if(keyTail==='\x1b'&&!paste)keyTail='';
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
    nativeFooter=null;paintedCommon=[];
    if(nativeContext?.sid!==req.sid)nativeContext=undefined;
    // Preserve a current TUI-selected model across its first SID binding only.
    // Context and session data are never carried from the previous selection.
    if(sid&&harness!=='opencode')nativeSelected={};
    if(typeof req.native_model==='string'&&req.native_model.length>0&&req.native_model.length<=128&&!/[\x00-\x1f\x7f]/.test(req.native_model)){
      nativeSelected={model:req.native_model,...(nativeSelected.model===req.native_model?{effort:nativeSelected.effort}:{})};
    }
    displayValues={...nativeSelected};
    panelModel={labels:panelModel.labels,styles:panelModel.styles};panelSections=undefined;panelLines=[];
    projectionKey='';footer='';footerSegments=[];routeDetail=false;
    sequence=req.seq;selection++;native=reporter;sid=req.sid;state='unknown';empty=false;submitted=-1;block.leave();block.update([]);
    publishNativeDisplay();queueDisplay();
    if(currentDelivery)releaseHeld(currentDelivery,true);
    const selected=selection;
    // Display collection must never delay the native start hook or input.
    setImmediate(()=>{if(selected===selection)source('register').then(()=>projection());});
    return {ok:true};
  }
  if(req.sid!==sid)return {ok:false,reason:'selection-changed'};
  sequence=req.seq;
  if(req.event==='deselect'){
    selection++;sid='';state=harness==='opencode'?'idle':'unknown';empty=false;footer='';footerSegments=[];panelLines=[];panelSections=undefined;panelModel={};displayValues={};nativeContext=undefined;routeDetail=false;block.update([]);
    for(const row of watchers.values())row.watch.close();watchers.clear();
    source('register');rebuildDisplay();return {ok:true};
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
  if(req.op==='display-observation'){
    if(!displayReporter(req))return {ok:false};
    if(!Number.isSafeInteger(req.seq)||req.seq<=displaySequence)return {ok:false};
    displaySequence=req.seq;
    // Native selected model is display-only and may precede a session ID.
    if(typeof req.model==='string'&&req.model.length<=128&&!/[\x00-\x1f\x7f]/.test(req.model))nativeSelected.model=req.model;
    if(typeof req.effort==='string'&&req.effort.length<=64&&!/[\x00-\x1f\x7f]/.test(req.effort))nativeSelected.effort=req.effort;
    // OpenCode's MCP server counts, shown only because its native sidebar is hidden here.
    const counts=req.mcp;
    if(counts&&typeof counts==='object'&&['total','connected','failed'].every(key=>Number.isInteger(counts[key])&&counts[key]>=0&&counts[key]<=999)
       &&JSON.stringify(nativeMcp)!==JSON.stringify(counts)){nativeMcp={total:counts.total,connected:counts.connected,failed:counts.failed};queueProjection();}
    displayValues={...displayValues,...nativeSelected};
    publishNativeDisplay();queueDisplay();return {ok:true};
  }
  if(req.op==='event')return lifecycle(req);
  if(!exact(req))return {ok:false,reason:'attachment-changed',not_started:true};
  if(req.op==='native-selection')return {ok:!relay,native_frame:nativeSelectionFrame()};
  if(req.op==='status'){await refreshReadiness();return {ok:true,...status()};}
  // The native cells under the panel and frame: the seat's own screen for draft/form judgments.
  if(req.op==='screen')return relay?{ok:false,reason:'display-unavailable'}:{ok:true,screen:screenSnapshot(),...status()};
  if(req.op==='panel'&&relay)return {ok:false,reason:'display-unavailable'};
  if(req.op==='panel'){expanded=req.expanded===true;if(!expanded)block.leave();applyLayout();return {ok:true,...status()};}
  if(req.op!=='inject'||harness==='codex')return {ok:false,reason:'unsupported-operation',not_started:true};
  if(typeof req.ref!=='string'||req.ref.length>128)return {ok:false,reason:'peer-ref-unavailable',not_started:true};
  if(deliveries.has(req.ref))return {ok:true,duplicate:true,state:deliveries.get(req.ref)};
  const safe=()=>!relay&&exact(req)&&req.epoch===epoch&&state==='idle'&&empty&&!paste&&!keyTail&&(!block.focused||block.waitingForReceipt)&&!settings&&!fleetView&&!currentDelivery&&terminal.modes.bracketedPasteMode;
  if(!safe())return {ok:false,reason:'not-idle-empty',not_started:true};
  const selected=selection,revision=screenRevision;
  const data=await source('payload',{ref:req.ref,claim:req.claim,screen:screenSnapshot(),state});
  if(!data.ok||selected!==selection||revision!==screenRevision||!safe())return {ok:false,reason:data.reason||'input-changed',not_started:true};
  currentDelivery=req.ref;state='submitting';empty=false;epoch++;nativeEpoch++;lastEnter=nativeEpoch;queueProjection();
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
      let result;try{
        const req=JSON.parse(raw.subarray(0,newline).toString());
        result=await request(req);}
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
  // OpenCode starts on its home route with no session: publish the seat now.
  if(harness==='opencode'&&!sid){state='idle';source('register');}
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    try {const data=JSON.parse(line),call=calls.get(data.id);if(call){calls.delete(data.id);clearTimeout(call.timer);call.resolve(data);}}catch{}
  });
  source('display-bootstrap').then(data=>{if(data.ok&&!projectionKey){panelModel=data.panel;queueDisplay();}});
  emit('\x1b[?1049h\x1b[2J');
  terminal.onData(data=>child.write(data));terminal.onWriteParsed(()=>{screenRevision++;schedule();});
  terminal.onTitleChange(title=>{
    try{if(!ended&&!relay)emit(nativeDisplay.titleSequence(title));}catch{rawRelay();}
  });
  child.onData(data=>{if(relay)emit(data);else try{terminal.write(data);}catch{rawRelay();emit(data);}});
  process.stdin.on('data',userInput);
  process.stdout.on('resize',()=>{
    cols=process.stdout.columns;rows=process.stdout.rows;
    try{if(relay)child.resize(cols,rows);else applyLayout();}catch{rawRelay();}
  });
  child.onExit(async event=>{
    ended=true;clearTimeout(frameTimer);clearTimeout(projectionTimer);
    for(const row of watchers.values())row.watch.close();watchers.clear();
    server.close();await source('unregister');backend?.stdin.end();
    emit('\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l');
    fs.rmSync(socketPath,{force:true});process.exit(event.exitCode);
  });
  applyLayout();
});
