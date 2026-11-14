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
function bypass() { try{startupResult('BYPASS');}catch{} process.exit(0); }
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
const succession=require('./succession.cjs');
const {serialize}=succession;
// A broker that offers hand-over names its channel; without it this host never asks for or takes one.
const SUCCESSION=process.env.HEARTING_SHELL_SUCCESSION==='1'&&Number(process.env.HEARTING_SHELL_SUCCESSION_FD)>=3;
const SUCCESSOR=SUCCESSION&&process.env.HEARTING_SHELL_SUCCESSOR==='1';
const GENERATION=Number(process.env.HEARTING_SHELL_GENERATION)||0;
const panelView=require('./panel-view.cjs');
let displayConfiguration={...panelView.defaults};
try{displayConfiguration=JSON.parse(process.env.HEARTING_SHELL_LAYOUT||'{}');}catch{}
// Spec rule 49 (d): a window too narrow for the open panel starts with it folded.
const initialFold=nativeDisplay.fold({cols,configuration:displayConfiguration});
const initialGeometry=nativeDisplay.layout({cols,rows,expanded:!initialFold.folded,configuration:displayConfiguration});
const terminal = new Terminal({cols:initialGeometry.nativeCols, rows:initialGeometry.nativeRows, allowProposedApi:true, scrollback:1000, logLevel:'off'});
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
// What the panel shows as this session's state is the host's own observation (spec
// rules 25-26, 11-12 rule 6); hook events confirm and correct it. Display only: `state`
// (the lifecycle the hooks report), readiness and every input gate keep their meaning.
// - Claude's REPL writes its own UI state into its session record as it paints it
//   (`busy` while it shows progress, `waiting` with a dialog or question open, `idle`);
//   the host reads that record the moment its watched file changes, so the panel turns
//   with the native screen instead of after a hook's process start.
// - Codex appends each turn's start and end to its rollout (read with the projection);
//   a newer reading than the last hook event wins. After an approval the question
//   screen leaving (the readiness judgment's own form test) means the turn goes on.
// - OpenCode's in-process plugin reports its native events directly.
// Nothing here polls: readings come from file events and native output the host
// already receives.
let nativeTurn = '', turnMark = 0, eventMark = 0, seenTurn = null, clock = 0, turnRecord = '';
const TURN={busy:'working',idle:'idle'};
// A record's `waiting` is an answer wait only for these reasons (Claude's own `waitingFor`).
// A menu, settings or side dialog (`dialog open`, also `/btw` beside a running turn) is not
// one, as Fleet's row reads it, and says nothing about the turn: the previous reading
// stands (after an answer wait, or with none, the hooks say). An unlisted reason is the hooks'.
const ANSWER_WAITS=new Set(['permission prompt','input needed','worker request','sandbox request','goal proposal']);
const recordTurn=value=>value.status!=='waiting'?TURN[value.status]||'':ANSWER_WAITS.has(value.waitingFor)?'blocked'
  :value.waitingFor==='dialog open'&&nativeTurn!=='blocked'?nativeTurn:'';
function shownState(){
  if(state==='submitting')return state;
  if(harness==='claude'&&nativeTurn)return nativeTurn;
  if(seenTurn&&seenTurn.mark>eventMark)return seenTurn.value;
  // Before any hook (a start or resume) a rollout's last turn start may be a turn cut off
  // mid-way: only its idle is taken; a turn seen starting after that reads as `seenTurn`.
  return state==='unknown'&&nativeTurn&&nativeTurn!=='working'?nativeTurn:state;
}
// One reading of the native's own turn record, ordered by when it was asked for.
function noteTurn(turn,mark){
  if(mark<turnMark)return;
  const before=shownState();
  if(turn!==nativeTurn&&turn&&turnMark&&mark>eventMark)seenTurn={value:turn,mark};
  nativeTurn=turn;turnMark=mark;
  if(shownState()!==before)queueDisplay();
}
function readTurnRecord(){
  if(harness!=='claude'||!turnRecord||!native||!sid||relay||ended)return;
  const mark=++clock;let value;
  try{
    const st=fs.lstatSync(turnRecord);if(!st.isFile()||st.uid!==process.getuid()||st.size>65536)return;
    value=JSON.parse(fs.readFileSync(turnRecord,'utf8'));
  }catch{return;}
  // The same identity the pane reader requires: this exact process and session.
  if(!value||value.pid!==native.pid||String(value.procStart??'')!==String(native.start)||value.sessionId!==sid)return;
  noteTurn(recordTurn(value),mark);
}
// Codex after an approval: the question screen shown, then gone, while no newer event came.
// It is asked for as long as the hooks say the turn waits (a screen the judgment missed
// once is asked about again), and a question seen again turns the panel back to waiting.
let formSeen=false,formRevision=-1,formAsking=false;
function observeForm(){
  if(harness!=='codex'||!sid||relay||ended||formAsking||formRevision===screenRevision||state!=='blocked')return;
  formAsking=true;formRevision=screenRevision;const mark=++clock,events=eventMark,selected=selection;
  source('readiness',{screen:screenSnapshot(),state}).then(result=>{
    if(selected!==selection||events!==eventMark||!result?.ok)return;
    if(result.reason==='form-open'){formSeen=true;if(seenTurn){seenTurn=null;queueDisplay();}return;}
    if(formSeen&&shownState()==='blocked'){formSeen=false;seenTurn={value:'working',mark};queueDisplay();}
  }).finally(()=>{formAsking=false;if(formRevision!==screenRevision)schedule();});
}
// `unselected`: the input frontier of the session the native just deselected. OpenCode reloads its
// plugins when an install rewrites its config, which reports deselect then select of the same session;
// that is no new input, so the frontier and an open read page survive it (keys typed in between end it).
let nativeEpoch = 0, lastEnter = -1, submitted = -1, unselected = null, currentDelivery, releasing, heldBytes = 0;
let expanded = !initialFold.folded, relay = false, paste = false, keyTail = '', cursorVisible = true;
let screenRevision = 0;
let last = [], nativeLast = [], nativeGap = [], footer = '', footerSegments = [], footerLast = '\0', panelRows = [], panelLines = [], projectionKey = '';
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
// Spec rule 23: the route box's own scroll (routes hidden above), and the rows it took in the last paint.
let routeScroll=0,routeArea=null;
// Route and message rows opened in place by a press. Display only: a press
// here is not typing, so it moves neither the input epoch nor `empty`.
const panelOpen=new Set();
// Spec rule 49 (d), (f): the person's last fold choice while this window is up (`{folded:true}`
// folded or `{folded:false}` opened by hand, at every width); without one `expanded` follows the width.
let foldChoice=null;
let displaySequence=0,nativeMcp;
// Hand-over between host generations (plan §3.0): `succeeding` is the frozen window of the old
// display, `upgrade` a newer installed release waiting for a safe moment.
let succeeding=false,switched=!SUCCESSOR,running=!SUCCESSOR,upgrade=null,upgradeNote='',checkingRelease=false,checkedKey='',recheckRelease=false,releaseWake;
let successionChannel,socketIno=-1,seedFile='',layoutDeferred=false;
const bound=[],releaseWatchers=[],staleServers=[],nextPath=socketPath+'.next';
const myRelease=(()=>{try{return fs.realpathSync(__filename);}catch{return null;}})();
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
function status() { return {...identity(),epoch,state,empty:empty && !keyTail && !paste && (!block.focused||block.waitingForReceipt) && !settings && !fleetView,alive:live(),expanded,focused:block.focused||!!settings||!!fleetView,held_bytes:heldBytes,projection_state:relay||sourceError?"unavailable":"current"}; }
// A projection reply past the 1.5 s limit is still awaited and painted: one
// projection is in flight at a time, so no older reply can follow a newer one.
// Values left unanswered or failed for STALE_MS stop claiming a route state
// (spec rule 12); the next good reply repaints them.
const STALE_MS=10000;let staleTimer,stale=false;
function projectionMissed(){
  sourceError={ok:false,reason:'source-unavailable'};
  if(!staleTimer&&!stale)staleTimer=setTimeout(()=>{staleTimer=undefined;stale=true;queueDisplay();},STALE_MS);
}
function projectionAnswered(data){
  if(!data.ok){projectionMissed();return;}
  clearTimeout(staleTimer);staleTimer=undefined;if(stale){stale=false;projectionKey='';}
}
function source(op, extra={}) {
  if(op==='action'||op==='answer')return readAction(extra,op);
  if (!backend || backend.exitCode !== null || !sid&&!['display-settings','display-bootstrap','display-model','register','unregister'].includes(op)) return Promise.resolve({ok:false,reason:'source-unavailable'});
  return new Promise(resolve => {
    const id=++callID;
    // Startup display replies are finite, asynchronous reads. A busy backend
    // must not discard its eventual language/model result before first input.
    // They never delay native start or keys; backend exit settles all callers.
    const timer=['display-bootstrap','display-model'].includes(op)?null:
      setTimeout(()=>{if(op==='projection'){projectionMissed();return;}calls.delete(id);resolve({ok:false,reason:'source-unavailable'});},1500);
    calls.set(id,{resolve:op==='projection'?data=>{projectionAnswered(data);resolve(data);}:resolve,timer});
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
  // a warning or key hint may follow).
  const uuid='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  const statusOnly=row=>{
    const fields=row.split('·').map(value=>value.trim());
    if(fields.length<2||!new RegExp('^'+uuid+'$').test(fields[0]))return false;
    if(new RegExp('^'+uuid+' · (?:[^·]* · )*Context \\p{Nd}{1,3}% used(?: ·|$)','u').test(row))return true;
    const repeated=fields[1].split(/\s+/).filter(Boolean)[0]||'';
    return repeated===fields[0]||repeated.endsWith('…')&&repeated.length>8
      &&fields[0].startsWith(repeated.slice(0,-1));
  };
  // Pass on the accepted row's layout with every identity-shaped word (whole or
  // clipped thread-id) in place and nothing dropped; any other word becomes "?".
  // Whether ids repeat or conflict is decided once, by the Python reader.
  const evidence=row=>row.replace(/(Context[ \u00a0]\p{Nd}{1,3}%[ \u00a0]used)|[^\s·]+/gu,(word,context)=>
    context||/^(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f][0-9a-f-]{0,35}…)$/.test(word)?word:'?')
    .replace(/\?(?:[^\S·]+\?)+/g,'?');
  if(tail.length===1&&statusOnly(tail[0]))return {...frame,lines:['›',evidence(tail[0])]};
  const hint=tail[tail.length-1]||'';
  const compact=/^(?:(?:ctrl|shift|alt)\+|[⌃⇧⌥⌘^])*(?:[←→↑↓]|f\d{1,2}|enter|space|tab|esc|pgup|pgdn|home|end|[a-z0-9])(?: (?:(?:ctrl|shift|alt)\+|[⌃⇧⌥⌘^])*(?:[←→↑↓]|f\d{1,2}|enter|space|tab|esc|pgup|pgdn|home|end|[a-z0-9]))? for agents(?![0-9A-Za-z])/.exec(hint);
  const help=hint.toLowerCase().replace(/\s/g,'').includes('?forshortcuts')?'? for shortcuts':compact?.[0];
  if(!help)return frame;
  if(tail.length!==2)return {...frame,invalid:true};
  // Only identity-shaped words leave the native viewport. Titles, telemetry
  // and even footer-shaped draft text never travel in this read-only response.
  return {...frame,lines:['›',evidence(tail[0]),help]};
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
  }).finally(()=>{
    checkingNativeSelection=false;
    // A screen painted while this check ran was skipped; look at it once now.
    if(nativeSelectionRevision!==screenRevision)observeNativeSelection();
  });
}
function readAction(extra,operation='action',own=false) {
  // A person-requested read or answer may be slow. It must not queue ahead
  // of event-driven values or readiness on the ordinary projection worker.
  // An `own` display read (the route page's raw output) never takes the one
  // action slot, so it cannot hold an answer back.
  if(actionWorker&&!own)return Promise.resolve({ok:false,reason:'action-pending'});
  return new Promise(resolve=>{
    let raw='',settled=false;
    const worker=cp.spawn(process.env.HEARTING_SHELL_PYTHON,
      [path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});
    if(!own){actionWorker=worker;queueProjection();}
    const finish=value=>{if(!settled){settled=true;clearTimeout(timer);resolve(value);}};
    const timer=setTimeout(()=>finish({ok:false,reason:'action-unavailable'}),12000);
    worker.on('error',()=>finish({ok:false,reason:'action-unavailable'}));
    worker.stdin.on('error',()=>finish({ok:false,reason:'action-unavailable'}));
    worker.on('close',()=>{if(actionWorker===worker){actionWorker=undefined;queueProjection();checkRelease();}finish({ok:false,reason:'action-unavailable'});});
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
function geometry(){return nativeDisplay.layout({cols,rows,expanded,configuration:displayConfiguration});}
// Spec rule 49: the panel opened or folded through the same layout path a window resize takes
// (the native is told its new width by the PTY resize in applyLayout). An open settings editor,
// a question being answered or the panel's edge being dragged keeps an open panel open.
function pinned(){return expanded&&(!!settings||block.focused||dragging);}
function refold(){
  const next=nativeDisplay.fold({cols,choice:foldChoice,configuration:displayConfiguration,pinned:pinned(),expanded});
  foldChoice=next.choice;
  if(expanded===!next.folded)return;
  expanded=!next.folded;
  if(!expanded)block.leave();
}
// #1586 review follow-up 3: when what kept the panel open ends, the judgment is the same for all
// three: a width the person set (a drag, or the settings editor applying a new width) counts as
// opening it by hand; otherwise (a question answered, the editor closed) the width is judged once
// again, now.
let wasPinned=false;
function pinLapsed(){const now=pinned(),lapsed=wasPinned&&!now;wasPinned=now;return lapsed;}
// #1586 review follow-up 6: folding by hand closes an open settings editor first (it never
// stands squeezed into the strip with the keys going to it).
function foldByHand(folded){
  if(folded&&settings&&!settings.saving){displayConfiguration=settings.before;settings=undefined;}
  foldChoice={folded};
}
function panelColumns(){return geometry().panelCols;}
// The open panel's width: the projection is laid out for it while the panel is folded, so
// opening paints current values at once.
function openColumns(){return nativeDisplay.layout({cols,rows,expanded:true,configuration:displayConfiguration}).panelCols;}
function nativeColumns(){return geometry().nativeCols;}
function nativeRows(){return nativeDisplay.layout({cols,rows,expanded,configuration:displayConfiguration,panelRows:height()}).nativeRows;}
function panelRow(){return geometry().side?0:rows-height();}
// Spec rule 49 (c): folded, the strip's rows follow the window's rows alone (native-display's foldRows).
function maxHeight(){if(!expanded)return nativeDisplay.foldRows(rows);return geometry().side?rows:Math.min(block.focused?Math.max(12,displayConfiguration.bottom_rows||14):displayConfiguration.narrow==='compact'&&!settings?8:displayConfiguration.bottom_rows||14,Math.max(4,rows-6));}
// The opened route page's owner output (Fleet's `attempts`), read once per opening.
let routeRaw;
function readRouteRaw(){
  const selected=selection,who=sid;routeRaw=undefined;
  readAction({action:'raw'},'action',true).then(r=>{if(selected===selection&&who===sid&&routeDetail===true&&r?.ok&&Array.isArray(r.attempts)){routeRaw={sid:who,attempts:r.attempts};queueDisplay();}});
}
// What is being delivered: it takes the panel's last row, open or folded.
function deliveryNote(){return currentDelivery?` · 전달 중${heldBytes?' · 입력 보존 '+heldBytes+'B':''}`:'';}
// Correction 8 (11-13): the frame the shown state's frame motion began (running sends the arc
// round, a wait breathes), so the motion starts with the state on the same paint.
let motionKind='',motionBegan=0;
function motionSince(shown){
  const kind=shown==='blocked'?'breathe':shown==='working'||shown==='submitting'?'orbit':'';
  if(kind!==motionKind){motionKind=kind;motionBegan=spinFrame();}
  return motionBegan;
}
function panelViewNow(){
  const shown=shownState();
  const view=panelView.render({width:panelColumns(),height:maxHeight(),side:geometry().side,
    model:stale?{...panelModel,stale}:panelModel,native:displayValues,state:shown,sid,harness,frame:spinFrame(),since:motionSince(shown),sections:panelSections,configuration:displayConfiguration,
    settings,routeDetail,routePage,attempts:routeRaw?.sid===sid?routeRaw.attempts:[],expandedKeys:[...panelOpen],focused:block.focused,cwd:process.cwd(),
    cellWidth,background:terminalBackground,folded:!expanded,
    routeScroll,questions:options=>actionLines.length&&block.focused?actionLines:block.detail(options),sent:()=>block.sent()});
  // A message no longer kept closes its page here too, so its page keys go back to the native.
  if(typeof routeDetail==='string'&&view.routeDetail===false){routeDetail=false;routePage=0;}
  routePageCount=view.routePages;return view;
}
// One beat for everything that runs (Fleet's 100 ms spinner frame and its
// one-second blink, render_cells `_blink_phase`): the session's own spinner,
// working route marks, the running stage and the open route count. The beat
// runs only while the painted panel shows one of them; an idle panel paints
// nothing between native frames. A spinner turn swaps only its own cells in
// the cached rows; the blink, a color change, repaints the panel once a second.
function spinFrame(){return Math.floor(Date.now()/100);}
let viewGroups=[],paintedGroups=new Set(),viewSpins=[],paintedSpins=new Set(),viewAnimated=false,panelAnimated=false;
// Spec rule 47 (d): the moving frame turns on the same beat as the spinner, beside the native area
// and under it alike. Its line cells (the view's: row, column and glyph) are written as the
// scratch VT would write them (`serialize`'s per-cell attributes; each is one cell wide): a whole
// row written for any other reason carries them at the current spinner frame, and the beat sends
// only the cells that changed, a run at a time. The panel rows are kept with those cells at one
// fixed look (`panelKey`), so a rebuild whose rows only differ there rewrites nothing.
let viewMotion=null,paintedMotion=null,viewBlinks=false,panelBlinks=false,panelKey=[],written=[];
// What the terminal shows at each moving cell (`row,column` on the screen).
const motionShown=new Map();
// A part's foreground as SGR: a 256-color index, or `#rrggbb` (24-bit, where the terminal takes it:
// the moving frame's breath), as the scratch VT writes them back.
function foregroundSgr(value){
  if(Number.isInteger(value)&&value>=0&&value<=255)return '38;5;'+value;
  const m=/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(value??''));
  return m?'38;2;'+m.slice(1).map(x=>parseInt(x,16)).join(';'):null;
}
function cellAttr(part){
  const a=[0,foregroundSgr(part.foreground)||'39',
    Number.isInteger(part.background)&&part.background>=0&&part.background<=255?'48;5;'+part.background:'49'];
  if(part.bold===true)a.push(1);if(part.dim===true)a.push(2);if(part.reverse===true)a.push(7);
  return a.join(';');
}
const TOKENS=/\x1b\[[0-9;]*m|[\s\S]/gu;
// The glyph at each cell of a serialized row.
function rowGlyphs(row){
  const out=[];let col=0;
  for(const token of String(row).match(TOKENS)||[]){if(token.startsWith('\x1b['))continue;const w=cellWidth(token);if(w>0){out[col]=token;col+=w;}}
  return out;
}
// The view's moving cells that stand painted where it says (a row the host replaced, such as the
// delivery note's, keeps none), by row in column order, with that motion's paint and next change.
function motionPlan(motion,painted,drop){
  if(!motion||!Array.isArray(motion.cells))return null;
  const rows=new Map(),glyphs=new Map();
  motion.cells.forEach((c,i)=>{
    if(c.row===drop||typeof painted[c.row]!=='string')return;
    if(!glyphs.has(c.row))glyphs.set(c.row,rowGlyphs(painted[c.row]));
    if(glyphs.get(c.row)[c.col]!==c.ch)return;
    if(!rows.has(c.row))rows.set(c.row,[]);rows.get(c.row).push({row:c.row,col:c.col,ch:c.ch,i});
  });
  for(const list of rows.values())list.sort((a,b)=>a.col-b.col);
  return rows.size?{rows,at:motion.at,next:motion.next,frame:null,now:[],spliced:new Map()}:null;
}
// The moving cells' looks at the current spinner frame (once a frame).
function motionNow(){
  const m=paintedMotion,frame=spinFrame();
  if(m.frame!==frame){m.frame=frame;m.now=m.at(frame).map(cellAttr);m.spliced=new Map();}
  return m.now;
}
// A serialized row with its moving cells in the given looks; every other cell keeps its own.
function motionSplice(row,list,look){
  const at=new Map(list.map(c=>[c.col,c]));
  let out='',own='',pending=false,current=null,col=0;
  for(const token of row.match(TOKENS)||[]){
    if(token.startsWith('\x1b[')){own=token;pending=true;continue;}
    const c=at.get(col);
    if(c&&token===c.ch){const attr=look(c);if(current!==attr){out+=`\x1b[${attr}m`;current=attr;}pending=false;}
    else if(pending||current!==null){out+=own||'\x1b[0m';pending=false;current=null;}
    out+=token;col+=cellWidth(token);
  }
  return pending||current!==null?out+(pending?own:'\x1b[0m'):out;
}
function motionRow(y,row){
  const m=paintedMotion,list=m.rows.get(y),now=motionNow(),kept=m.spliced.get(y);
  if(kept&&kept[0]===row)return kept[1];
  const out=motionSplice(row,list,c=>now[c.i]);m.spliced.set(y,[row,out]);return out;
}
// A whole row just written: its moving cells show their current looks.
function motionWrote(y,screen,column){
  const list=paintedMotion?.rows.get(y);if(!list)return;
  const now=motionNow();for(const c of list)motionShown.set(screen+','+(column+c.col),now[c.i]);
}
// The moving cells of a panel row that differ from what the terminal shows, a run at a time.
function motionSpan(y,screen,column){
  const list=paintedMotion?.rows.get(y);if(!list)return '';
  const now=motionNow(),stale=c=>motionShown.get(screen+','+(column+c.col))!==now[c.i];
  let out='';
  for(let k=0;k<list.length;){
    if(!stale(list[k])){k++;continue;}
    let run=`\x1b[${screen+1};${column+list[k].col}H`,current=null;
    for(let x=list[k].col;k<list.length&&list[k].col===x&&stale(list[k]);k++,x++){
      const c=list[k];if(current!==now[c.i]){run+=`\x1b[${now[c.i]}m`;current=now[c.i];}run+=c.ch;
      motionShown.set(screen+','+(column+c.col),now[c.i]);
    }
    out+=run+'\x1b[0m';
  }
  return out;
}
// A row whose other cells stand as last written: its spinner glyphs and its changed moving cells.
function cellSpan(y,screen,column){return (paintedSpins.has(y)?spinSpan(screen,panelRows[y],column):'')+motionSpan(y,screen,column);}
// The gap beside the panel: the terminal's own background, or the native's own at its edge cell.
// Two cells beside the open panel (the folded strip stands under the native area).
function gapText(){return '\x1b[0m'+' '.repeat(geometry().gap);}
// An inverse edge cell shows its foreground as its background (#1583 review follow-up (다)): the
// gap takes that color, the terminal's own where that foreground is the default.
function gapOf(line,x){
  const cell=line?.getCell(x);if(!cell)return gapText();
  const inverse=!!cell.isInverse();
  if(inverse?cell.isFgDefault():cell.isBgDefault())return gapText();
  const sgr=inverse?succession.color(cell,false).replace(/^38;/,'48;'):succession.color(cell,true);
  return `\x1b[0;${sgr}m${' '.repeat(geometry().gap)}\x1b[0m`;
}
function spun(y,row){
  if(typeof row!=='string')return row;
  let out=row;
  if(paintedSpins.has(y)){const spin=panelView.SPIN;out=out.replace(new RegExp('['+spin+']','g'),spin[spinFrame()%spin.length]);}
  return paintedMotion?.rows.has(y)?motionRow(y,out):out;
}
let beatTimer,beatPhase=-1;
function beating(){return panelAnimated&&activated&&!ended&&!relay&&!succeeding&&!fleetView&&!settings&&panelRows.length>0;}
// The beat wakes at the next moment something painted changes: a spinner's next frame, the
// moving frame's next change (the arc's next step; a breath's next shade), a blink's phase
// change once a second (spec rule 47 (d)). A blink repaints the panel; the rest swap their own
// cells. Nothing changing, nothing wakes.
function armBeat(){
  if(beatTimer||!beating())return;
  const now=Date.now();let wait=Infinity;
  if(panelBlinks)wait=1000-now%1000;
  if(paintedSpins.size)wait=Math.min(wait,100-now%100);
  if(paintedMotion)wait=Math.min(wait,Math.max(1,paintedMotion.next(spinFrame())*100-now));
  if(wait!==Infinity)beatTimer=setTimeout(beatTick,wait);
}
function beatTick(){
  beatTimer=undefined;if(!beating())return;
  const phase=Math.floor(Date.now()/1000)%2;
  if(phase!==beatPhase){beatPhase=phase;if(panelBlinks){rebuildDisplay();armBeat();return;}}
  spinPaint();
  armBeat();
}
// Only the spinner glyphs of a row whose other cells stand as last painted: each written at its
// own cell with the attributes it stands in (the row as the scratch VT wrote it; Unicode 11 widths).
function spinSpan(y,row,column){
  const spin=panelView.SPIN,glyph=spin[spinFrame()%spin.length];
  let out='',sgr='',col=0;
  for(const token of String(row).match(/\x1b\[[0-9;]*m|[\s\S]/gu)||[]){
    if(token.startsWith('\x1b[')){sgr=token;continue;}
    if(spin.includes(token))out+=`\x1b[${y+1};${column+col}H${sgr}${glyph}`;
    col+=cellWidth(token);
  }
  return out&&out+'\x1b[0m';
}
// Only the changed cells of the panel rows (the spinner glyphs, the moving frame's cells), with
// the cursor kept where the native left it. A native frame already on its way paints them itself.
// A row whose other cells were not written as they stand now goes whole (the panel part).
function spinPaint(){
  if(frameTimer||!paintedSpins.size&&!paintedMotion||succeeding)return;
  let text='';const side=geometry().side;
  for(const y of paintedMotion?new Set([...paintedSpins,...paintedMotion.rows.keys()]):paintedSpins){
    if(y>=panelRows.length)continue;
    const panel=spun(y,panelRows[y]);
    if(side){
      if(nativeLast[y]===undefined)continue;
      const gap=nativeGap[y]||gapText();
      const line=geometry().left?panel+`\x1b[${panelColumns()+1}G`+gap+nativeLast[y]:nativeLast[y]+`\x1b[${terminal.cols+1}G`+gap+panel;
      if(last[y]===line)continue;
      const column=geometry().left?1:terminal.cols+1+geometry().gap,quiet=last[y]!==undefined&&written[y]===panelKey[y];
      text+=quiet?cellSpan(y,y,column):`\x1b[${y+1};${column}H\x1b[0m`+panel;
      if(!quiet)motionWrote(y,y,column);
      last[y]=line;written[y]=panelKey[y];
    }else{
      const row=rows-height()+y;if(y===0&&!panelLines.length&&typeof nativeContext?.percent==='number'&&harness!=='claude')continue;
      if(last[row]===panel)continue;
      const quiet=last[row]!==undefined&&written[row]===panelKey[y];
      text+=quiet?cellSpan(y,row,1):`\x1b[${row+1};1H`+panel;
      if(!quiet)motionWrote(y,row,1);
      last[row]=panel;written[row]=panelKey[y];
    }
  }
  if(text)emit('\x1b[?2026h\x1b7'+text+'\x1b8\x1b[?2026l');
}
// The terminal background, only where it is already known (COLORFGBG). Nothing
// is asked: the host answers a native's own OSC 10/11 query itself, so an
// unknown background keeps the terminal's own and paints no shade.
const terminalBackground=(()=>{const value=String(process.env.COLORFGBG||''),bg=Number(value.split(';').pop());
  return value.includes(';')&&Number.isInteger(bg)?([7,15].includes(bg)?'light':bg>=0&&bg<16?'dark':''):'';})();
// #1586 review follow-up 5: the press targets carry the placement they were drawn for; a press
// while the panel is being drawn for another placement (folding or opening) is dropped.
let panelHitsPlacement='';
function placement(){const g=geometry();return [expanded,g.side,g.folded,g.left,g.panelCols,cols,rows,!!settings].join(':');}
function panelContent(){const view=panelViewNow();panelHits=view.hits;panelHitsPlacement=placement();routeArea=view.routeArea||null;if(routeArea)routeScroll=routeArea.offset;viewGroups=view.groups||[];viewSpins=view.spins||[];viewMotion=view.motion||null;viewBlinks=!!view.blinks;viewAnimated=!!view.animated;return view.rows;}
// A read page (a kept message or the route evidence) owns PgUp/PgDn and Esc
// only while it is on screen. Collapsed, under Fleet or the settings editor,
// relayed, or no longer kept, it leaves them to the native like any other key;
// key ownership is not left to the next paint.
function readPageShown(){
  if(!routeDetail||relay||ended||!activated||fleetView||settings||!expanded)return false;
  return panelViewNow().hits.some(hit=>hit.action==='route-prev')&&!!routeDetail;
}
function height(){
  if(fleetView)return 0;
  if(geometry().side)return rows;
  if(!expanded)return maxHeight();
  const content=panelContent();
  return Math.min(maxHeight(),Math.max(content.length,currentDelivery?1:0));

}
function schedule() { if(!frameTimer&&!succeeding)frameTimer=setTimeout(render,8); }
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
  frameTimer=undefined;if(ended||!activated||relay||succeeding)return;
  try {
    observeNativeSelection();observeForm();
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
      let motion=false;
      if(!fleetView&&geometry().side){
        // The two cells between the native and the panel take the native's own background at
        // that edge (OpenCode paints its whole area): the native's area then reaches the panel's
        // frame and the panel stands with the same space on both sides; a native on the
        // terminal's own background keeps the bytes it had.
        const plain=y!==notice&&!concealed.has(y)&&!masked;
        const gap=plain?gapOf(view.buffer.active.getLine(view.buffer.active.viewportY+y),geometry().left?0:view.cols-1):gapText();
        const same=nativeLast[y]===line&&nativeGap[y]===gap;nativeLast[y]=line;nativeGap[y]=gap;
        const shown=y<panelRows.length,panel=shown?spun(y,panelRows[y]):' '.repeat(panelColumns());
        line=geometry().left?panel+`\x1b[${panelColumns()+1}G`+gap+line:line+`\x1b[${view.cols+1}G`+gap+panel;
        // A row of the moving frame whose native part and other panel cells are unchanged: only
        // its changed cells (and its spinner glyphs).
        const column=geometry().left?1:view.cols+1+geometry().gap;
        motion=shown&&!!paintedMotion?.rows.has(y);
        if(motion&&same&&last[y]!==undefined&&last[y]!==line&&written[y]===panelKey[y]){text+=cellSpan(y,y,column);last[y]=line;}
        else if(motion&&last[y]!==line)motionWrote(y,y,column);
        written[y]=shown?panelKey[y]:undefined;
      }
      if(last[y]!==line){text+=`\x1b[${y+1};1H${line}`;last[y]=line;}
    }
    if(!fleetView&&!geometry().side&&(expanded||height()>0))for(let y=0;y<panelRows.length;y++){
      const row=rows-height()+y,first=expanded&&y===0&&!panelLines.length&&typeof nativeContext?.percent==='number'&&harness!=='claude';
      const cached=first?'\x1b[0m\x1b[2K'+nativeDisplay.firstLine(nativeContext)[0].text:spun(y,panelRows[y]);
      if(last[row]!==cached){
        // A row of the moving frame whose other cells are unchanged: only its changed cells.
        const quiet=!first&&!!paintedMotion?.rows.has(y)&&last[row]!==undefined&&written[row]===panelKey[y];
        text+=quiet?cellSpan(y,row,1):`\x1b[${row+1};1H${cached}`;
        if(!quiet&&!first)motionWrote(y,row,1);
        last[row]=cached;
      }
      written[row]=first?undefined:panelKey[y];
    }
    else if(!fleetView&&!geometry().side&&height()>0&&footerLast!==displayFooter){text+=`\x1b[${rows};1H\x1b[0m\x1b[2K${displayFooter}`;footerLast=displayFooter;}
    const b=view.buffer.active,m=view.modes;
    const cursor=block.focused&&!fleetView?blockCursor:null;
    text+=`\x1b[${cursor?.row||Math.min(b.cursorY+1,view.rows)};${cursor?.col||Math.min(b.cursorX+1,view.cols)+(fleetView?0:geometry().nativeColumn)}H`;
    text+=`\x1b[?2004${m.bracketedPasteMode?'h':'l'}\x1b[?1${m.applicationCursorKeysMode?'h':'l'}`;
    text+='\x1b[?1000l\x1b[?1002l\x1b[?1003l';
    if(!fleetView&&height()>0)text+='\x1b[?1002h\x1b[?1006h';
    else if(m.mouseTrackingMode!=='none')text+=`\x1b[?${({x10:9,vt200:1000,drag:1002,any:1003})[m.mouseTrackingMode]}h\x1b[?1006h`;
    text+=`\x1b[?1004${m.sendFocusMode?'h':'l'}\x1b[?7h${(block.focused&&!fleetView?!!blockCursor:cursorVisible)?'\x1b[?25h':''}\x1b[?2026l`;
    emit(text);armBeat();checkRelease();
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
    const fg=foregroundSgr(part.foreground);if(fg)attrs.push(fg);
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
  setImmediate(()=>{displayQueued=false;try{if(pinLapsed())refold();if(!fleetView&&(terminal.rows!==nativeRows()||terminal.cols!==nativeColumns()))applyLayout();else rebuildDisplay();}catch{rawRelay();}});
}
function queueLayout(){
  if(layoutQueued)return;layoutQueued=true;
  setImmediate(()=>{layoutQueued=false;try{applyLayout();}catch{rawRelay();}});
}
const block=require('./block-input.cjs')({changed:queueDisplay,
  answer:async fields=>{const result=await source('answer',fields);queueProjection();return result;},
  action:key=>blockAction(key),
  // Focused on the panel with no question to read, PgUp/PgDn scroll the route
  // box; with a question they stay the question's (its body reads on).
  page:dir=>{if(!paste&&routeArea)scrollRoutes(dir*Math.max(1,routeArea.step||1));}});
let actionLines=[];
function rebuildDisplay() {
  if(relay||ended||fleetView)return;
  if(!height()){panelRows=[];panelKey=[];paintedCommon=[];paintedGroups=new Set();paintedSpins=new Set();paintedMotion=null;motionShown.clear();panelBlinks=false;panelAnimated=false;displayFooter='';blockCursor=null;schedule();return;}
  if(rebuilding){rebuildPending=true;return;}rebuilding=true;rebuildPending=false;
  const extra=deliveryNote();
  let lines,edit=-1,groups=[],spins=[],animated=false,motion=null,blinks=false;
  if(expanded){
    const base=panelContent();groups=viewGroups;spins=viewSpins;motion=viewMotion;blinks=viewBlinks;animated=viewAnimated;
    const detail=[];
    if(detail.length){
      const prefix=base.slice(0,Math.max(0,height()-detail.length)),visible=detail.slice(-Math.max(1,height()-prefix.length));
      const at=visible.findIndex(line=>Array.isArray(line)&&line.some(part=>String(part.text).startsWith('› ')));
      edit=at<0?-1:prefix.length+at;lines=[...prefix,...visible];
    }else lines=base;
    if(block.focused&&!settings)edit=lines.findIndex(line=>line.some(part=>String(part.text).startsWith('› ')&&String(part.text).length>2));
    if(extra)lines=[...lines.slice(0,height()-1),[{text:extra,dim:true}]];
  }else{
    // Spec rule 49 (c): folded, the strip under the native area.
    lines=panelContent();groups=viewGroups;spins=viewSpins;animated=viewAnimated;
    if(extra)lines=[...lines.slice(0,height()-1),[{text:extra,dim:true}]];
  }
  const editParts=edit>=0?lines[edit]:[];
  const editAt=editParts.findIndex(part=>String(part.text).startsWith('› ')&&String(part.text).length>2);
  const editColumn=editAt>=0?cellWidth(editParts.slice(0,editAt+1).map(part=>part.text).join(''))+1:0;
  displayLines(lines,height(),(painted,lengths,plain)=>{
    blockCursor=edit>=0?{row:panelRow()+edit+1,col:geometry().panelColumn+Math.min(panelColumns()-2,editColumn)}:null;
    panelRows=painted;paintedGroups=new Set(groups);paintedSpins=new Set(spins);panelAnimated=animated;
    // The moving frame's cells as painted (the delivery note's row, which replaces the last one,
    // keeps none); what the terminal shows at them stays known (`motionShown`).
    paintedMotion=motionPlan(motion,painted,extra?lines.length-1:-1);
    if(!paintedMotion)motionShown.clear();
    panelKey=painted.map((row,y)=>paintedMotion?.rows.has(y)?motionSplice(row,paintedMotion.rows.get(y),()=>'0'):row);
    panelBlinks=blinks;
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
  if(succeeding){layoutDeferred=true;return;}
  if(fleetView){fleetView.child.resize(cols,rows);fleetView.terminal.resize(cols,rows);last=[];schedule();return;}
  refold();wasPinned=pinned();
  child.resize(nativeColumns(),nativeRows());terminal.resize(nativeColumns(),nativeRows());last=[];footerLast='\0';
  emit('\x1b[0m\x1b[2J');rebuildDisplay();schedule();queueProjection();
}
// A redraw waits 25 ms to gather the events that come with it. A change seen only in a folder
// watched whole (LAB's run records may change many times a second) redraws at most once a second:
// it waits for the second since the last redraw began, and the last change always lands. Any other
// request keeps the 25 ms wait, and that redraw carries the folder's changes as well.
function queueProjection(folder=false) {
  if(ended||relay||succeeding)return;
  const now=Date.now(),delay=folder?Math.min(FOLDER_PAUSE,Math.max(25,projectionStarted+FOLDER_PAUSE-now)):25;
  if(projectionTimer){if(projectionDue<=now+delay)return;clearTimeout(projectionTimer);}
  nativeSelectionRevision=-1; // Existing lifecycle/file events may settle a prior observation.
  projectionDue=now+delay;
  projectionTimer=setTimeout(()=>{projectionTimer=undefined;projectionStarted=Date.now();projection();},delay);
}
const FOLDER_PAUSE=1000;let projectionDue=0,projectionStarted=0;
// A folder is its device, inode and birth time: one made again at the same path is another
// folder even when it is given the old inode number again.
function folderIdentity(directory){try{const s=fs.statSync(directory);return [s.dev,s.ino,s.birthtimeMs].join(':');}catch{return '';}}
// A target that ends in the path separator is a folder watched whole: any entry written,
// replaced, added or removed in it redraws, however many files it holds (LAB's run records).
// A watched folder removed, renamed away or made again leaves its watch on the old folder: the
// watch is dropped at that event, or at the next projection's observation, and made again.
// A folder's watch that fails stays as failed until the next projection's observation makes it
// again. Made again for its failure alone (the same folder, the same targets), it asks no 25 ms
// redraw: its re-read takes a folder's once-a-second pace, so a watch failing again and again is
// made again, and redrawn for, at most once a second, the last change still drawn. A watch on a
// file target keeps its own way: dropped at the failure, made again as a new watch at the next
// projection.
function observeFiles(paths) {
  const wanted=new Map();let changed=false;
  for(const target of (Array.isArray(paths)?paths:[]).slice(0,64)){
    if(typeof target!=='string'||!path.isAbsolute(target)||target.length>4096)continue;
    let directory=target.endsWith(path.sep)?target.slice(0,-1)||path.sep:path.dirname(target);
    while(!fs.existsSync(directory)&&path.dirname(directory)!==directory)directory=path.dirname(directory);
    if(!wanted.has(directory))wanted.set(directory,[]);wanted.get(directory).push(target);
  }
  for(const [directory,row] of watchers)if(!wanted.has(directory)){row.watch.close();watchers.delete(directory);changed=true;}
  for(const [directory,targets] of wanted){
    const existing=watchers.get(directory),identity=folderIdentity(directory);
    const retried=!!existing?.failed&&existing.identity===identity&&JSON.stringify(existing.targets)===JSON.stringify(targets);
    if(existing&&(existing.failed||existing.identity!==identity)){existing.watch.close();watchers.delete(directory);}
    else if(existing){if(JSON.stringify(existing.targets)!==JSON.stringify(targets))changed=true;existing.targets=targets;continue;}
    try{
      const row={targets,identity};
      // Only folders watched whole among the targets: their changes take the once-a-second pace.
      const folder=hits=>hits.length>0&&hits.every(target=>target.endsWith(path.sep));
      row.watch=fs.watch(directory,(event,name)=>{
        if(watchers.get(directory)!==row)return;
        const changed=name?path.join(directory,String(name)):directory;
        // The watched folder's own removal or move comes under its own name.
        if(event==='rename'&&(name===path.basename(directory)||folderIdentity(directory)!==row.identity)){
          row.watch.close();watchers.delete(directory);queueProjection(folder(row.targets));return;
        }
        const hits=name?row.targets.filter(target=>target===changed||target.startsWith(changed+path.sep)
          ||target.endsWith(path.sep)&&changed.startsWith(target)):row.targets;
        if(hits.length){
          // The native's own turn record is read at once; the rest waits for the projection.
          if(turnRecord&&(changed===turnRecord||!name))readTurnRecord();
          queueProjection(!!name&&folder(hits));
        }
      });
      // Only a folder watched whole stays as failed (its slow, once-a-second remake). A file target's
      // watch is dropped and made again at the projection as before this PR.
      row.watch.on('error',()=>{row.watch.close();
        if(row.targets.some(target=>target.endsWith(path.sep)))row.failed=true;
        else if(watchers.get(directory)===row)watchers.delete(directory);
        sourceError={reason:'file-observation-unavailable'};queueProjection(true);});
      watchers.set(directory,row);
      if(retried)queueProjection(true);else changed=true;
    }catch{sourceError={reason:'file-observation-unavailable'};}
  }
  return changed;
}
async function projection() {
  if(!sid||ended||relay||succeeding)return;if(projecting){projectionPending=true;return;}
  projecting=true;projectionPending=false;const selected=selection;
  try{
    const asked=++clock;
    const data=await source('projection',{columns:openColumns(),rows,state,expanded,native_context:nativeContext?.percent,native_badges:nativeMcp?{...(nativeContext||{}),mcp:nativeMcp}:nativeContext,retained_questions:block.references(),answer_busy:!!actionWorker||!!currentDelivery,upgrade_note:upgradeNoteNow(),
      ...(typeof displayValues.model==='string'&&displayValues.model?{native_model:displayValues.model}:{})});
    if(relay||selected!==selection)return;if(!data.ok){sourceError=data;return;}sourceError=undefined;
    turnRecord=harness==='claude'&&typeof data.turn_record==='string'&&path.isAbsolute(data.turn_record)?data.turn_record:'';
    // Claude's record is read here as well, by the host itself: one reading rule (a dialog
    // keeps the previous reading), and the file as it is now.
    if(turnRecord)readTurnRecord();
    else noteTurn(['idle','working','blocked'].includes(data.native_turn)?data.native_turn:'',asked);
    // Close the read-before-watch gap once when the input set changes.
    if(observeFiles(data.watch))projectionPending=true;
    const key=JSON.stringify([harness==='claude'?null:[data.footer,data.segments],data.lines,data.block,data.questions,data.help,data.native_footer,data.panel,cols,expanded,currentDelivery,heldBytes,state,shownState(),upgradeNoteNow()]);
    const freshDisplay=Object.fromEntries(Object.entries(data.native_display||{}).filter(([,value])=>value!=null));
    if(bootstrapModel&&freshDisplay.model){nativeSelected={};bootstrapModel=false;}
    displayValues={...displayValues,...freshDisplay,...nativeSelected};publishNativeDisplay();
    if(key===projectionKey)return;const oldHeight=height();projectionKey=key;nativeFooter=data.native_footer||null;footer=data.footer;footerSegments=data.segments||[];panelLines=data.lines;panelSections=data.block;panelModel={...data.panel,answerable:data.questions?.some(item=>item.answerable===true&&(item.request_state||'open')==='open')};block.update(data.questions,data.help,data.block);if(height()!==oldHeight)applyLayout();else rebuildDisplay();
  }finally{projecting=false;if(projectionPending)queueProjection();}
}
// Claude's statusline row may be blanked only while this visible panel paints
// the same session's groups that row carries: number and project, Git, context,
// the answer wait, the route count, 5h/7d and running jobs. A group the panel
// paints counts even when its row is cut to the panel's width; only a group
// the panel does not paint at all (short of rows, a read page, settings) keeps
// the native rows, so no value leaves the screen. Spec rule 49 (c): the folded
// strip takes that row's place while it paints its session row (the strip carries
// no Git or job row), so a value never shows twice; the row keeps the notice.
function claudePanelPainted(){
  if(harness!=='claude'||relay||ended||fleetView||!activated||!sid||settings||!height())return false;
  if(!expanded)return paintedGroups.has('head');
  const has=parts=>(Array.isArray(parts)?parts:[]).some(part=>String(part.text).trim());
  const wanted=[['head',has(panelModel.head)],['git',has(panelModel.git)],['context',has(panelModel.context)],['quota',has(panelModel.quota)],
    ['jobs',has(panelModel.jobs)],['wait',!!panelModel.wait],['route',!!panelModel.route_count]].filter(([,shown])=>shown).map(([name])=>name);
  return wanted.length>0&&wanted.every(name=>paintedGroups.has(name));
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
  // A display lost while frozen for hand-over reads again: the relay needs the PTY and the keyboard.
  // Ending this side of the hand-over channel first tells the owner (`eof`), so it drops the successor
  // before that one is told to read; a `retire` already sent still arrives and ends this host.
  if(succeeding){succeeding=false;try{successionChannel?.end();}catch{}try{child.resume();process.stdin.resume();}catch{}}
  if(fleetView){try{fleetView.child.write('q');}catch{}fleetView=undefined;}
  clearTimeout(frameTimer);clearTimeout(projectionTimer);clearTimeout(beatTimer);frameTimer=projectionTimer=beatTimer=undefined;
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
  if(currentDelivery===ref){currentDelivery=undefined;heldBytes=0;if(state==='submitting'){state='unknown';eventMark=++clock;}queueProjection();}
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
// Returns why the reading gave no answer for the screen it was asked about ('moved': the screen,
// input or lifecycle changed meanwhile; 'unavailable': the backend did not answer), or '' when it did.
async function refreshReadiness() {
  // A previous empty frame cannot acknowledge bytes still waiting inside the
  // native input loop. Only untouched startup or an exact submitted frontier
  // can be corroborated by the screen/native-idle observation.
  const inputSettled=nativeEpoch===0||(submitted>=0&&submitted===nativeEpoch);
  if(relay||paste||keyTail||currentDelivery||!sid||!inputSettled){empty=false;return '';}
  // A lifecycle event that arrives meanwhile is newer than this observation.
  const before=epoch,selected=selection,revision=screenRevision,observed=sequence;
  const result=await source('readiness',{screen:screenSnapshot(),state});
  if(before!==epoch||selected!==selection||revision!==screenRevision||observed!==sequence||currentDelivery){empty=false;return 'moved';}
  empty=result.ok===true&&result.ready===true;
  if(empty&&state!=='idle'){state='idle';queueDisplay();queueProjection();}
  return result.ok===true?'':'unavailable';
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
function observeReport(data){epoch+=data.length;empty=false;}
function openSettings(){
  settings={value:{...panelView.defaults,...displayConfiguration},before:{...displayConfiguration},index:0};
  block.leave();epoch++;queueLayout();
}
function previewSettings(){displayConfiguration={...settings.value};queueLayout();}
async function applySettings(){
  const current=settings;if(!current||current.saving)return;current.saving=true;
  const result=await source('display-settings',{settings:current.value});
  if(settings!==current)return;
  if(result.ok){if(result.settings?.side_width!==current.before.side_width)foldByHand(false);displayConfiguration=result.settings;settings=undefined;}
  else{current.saving=false;current.error=true;}
  queueLayout();
}
function cancelSettings(){if(settings&&!settings.saving){displayConfiguration=settings.before;settings=undefined;queueLayout();}}
function panelAction(action){
  // A route, a message, a request, or the LAB section (`lab:<group>`, draft A) opens in place
  // and closes again.
  if(/^(?:route|message|request|lab):/.test(action)&&!settings){
    if(panelOpen.has(action))panelOpen.delete(action);else{if(panelOpen.size>=16)panelOpen.clear();panelOpen.add(action);}
    queueLayout();return;
  }
  epoch++;empty=false;
  if(settings?.saving)return;
  if(action==='settings'){openSettings();return;}
  // Spec rule 49 (b), (c): the fold button folds the panel; a press on the folded strip opens it.
  if(action==='fold'||action==='unfold'){foldByHand(action==='fold');applyLayout();return;}
  if(action==='apply'){applySettings();return;}
  if(action==='cancel'){cancelSettings();return;}
  if(action==='defaults'&&settings){settings.value={...panelView.defaults};previewSettings();return;}
  if(action==='route'){routeDetail=!routeDetail;routePage=0;block.leave();if(routeDetail)readRouteRaw();}
  // A message longer than its rows reads on, from its start, on the detail pages.
  if(action.startsWith('read:message:')||action.startsWith('read:request:')){routeDetail=action.slice(5);routePage=0;block.leave();}
  if((action==='route-prev'||action==='route-next')&&routeDetail){routePage=Math.max(0,Math.min(routePageCount-1,routePage+(action==='route-prev'?-1:1)));}
  if(action==='question'){routeDetail=false;block.enter();}
  if(action.startsWith('setting:')&&settings){
    const [,field,operation]=action.split(':'),index=Number(field);if(index<0||index>=panelView.fields.length)return;
    settings.index=index;if(operation!=='select')settings.value=panelView.change(settings.value,panelView.fields[index],operation==='down'?-1:1);previewSettings();return;
  }
  queueDisplay();
}
// Scroll the route box by `delta` routes; a box that shows every route keeps still.
function scrollRoutes(delta){
  if(!routeArea||!routeArea.max)return false;
  const next=Math.max(0,Math.min(routeArea.max,routeScroll+delta));
  if(next!==routeScroll){routeScroll=next;queueDisplay();}
  return true;
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
    // A width set by hand is the person's choice to keep the panel open.
    else{dragging=false;foldByHand(false);schedule();source('display-settings',{settings:{...panelView.defaults,...displayConfiguration}}).then(r=>{if(!r.ok)openSettings();});}
    return true;
  }
  // The boundary beside the native area resizes: the left edge of a right
  // panel, the right edge of a left one.
  if(g.side&&!g.folded&&local===(g.left?panelColumns()-1:0)&&(code&3)===0&&!release){dragging=true;return true;}
  if(row<0||row>=height()||local<0||local>=panelColumns())return terminal.modes.mouseTrackingMode==='none';
  // The wheel over the route box scrolls it in place (spec rule 23); elsewhere in the panel it does nothing.
  if((code&0xc3)===64||(code&0xc3)===65){if(!release&&!routeDetail&&!settings&&routeArea&&row>=routeArea.top&&row<=routeArea.bottom)scrollRoutes((code&1)?1:-1);return true;}
  if(release||code>=32||(code&3)!==0)return true;
  // The targets drawn for this very placement are what the person pressed, a queued layout or not.
  if(panelHitsPlacement!==placement())return true;
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
    nativePointer(code,release,raw);
  }
});
function nativePointer(code,release,raw){
  // A wheel report scrolls; it neither edits the composer nor clicks away
  // the question block. Like a focus report it invalidates the in-flight
  // observation but leaves no native draft, so a scroll (or the scroll nudge
  // Herdr sends a Claude pane on a recent read) keeps the input settled.
  const wheel=(code&0xc0)===64;
  if(!release&&!wheel&&(code&3)===0){block.leave();queueDisplay();}
  const observe=wheel&&!keyTail&&!paste?observeReport:observeInput;
  if(currentDelivery){inputOwner.hold(raw);observe(raw);heldBytes+=raw.length;return;}
  observe(raw);child.write(raw);
}
function userInput(data){if(data.length){epoch+=data.length;empty=false;mouseInput.feed(data);}}
function inputBytes(data) {
  if(!data.length)return;
  while(data.length){
    // Mouse-opened shell editors own their visible keys even while a peer's
    // paste is admitted. Only native-directed bytes enter the retained queue.
    if(settings&&!relay){settingsInput(data);epoch+=data.length;return;}
    const page=!!routeDetail&&readPageShown();
    if(page&&!paste){const pageKey=/^\x1b\[([56])~/.exec(data.toString('latin1'));if(pageKey){panelAction(pageKey[1]==='5'?'route-prev':'route-next');data=data.subarray(pageKey[0].length);continue;}}
    if(page&&(data[0]===27&&data.length===1||data[0]===29)){routeDetail=false;queueDisplay();data=data.subarray(1);continue;}
    // Rule 5 (11-14): a sent answer returns focus to the native input. The lone Esc a person
    // presses right after it, to leave the block as before, is not the native's interrupt.
    if(!block.focused&&!relay&&block.answeredEscape?.(data)){data=data.subarray(1);queueDisplay();continue;}
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
    if(!paste&&!keyTail){epoch++;foldByHand(false);expanded=true;actionLines=[];routeDetail=false;routePage=0;block.enter();queueLayout();}
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
    // Spec E9 rule 34 (f): `/clear` keeps the process and its model, so the model and
    // effort row stays as last observed (its context starts empty); a start or a
    // switch (`/resume`) reads them anew.
    const cleared=req.event==='start'&&req.source==='clear'&&!!sid;
    nativeFooter=null;paintedCommon=[];
    if(nativeContext?.sid!==req.sid)nativeContext=undefined;
    // Preserve a current TUI-selected model across its first SID binding only.
    // Context and session data are never carried from the previous selection.
    if(sid&&harness!=='opencode'&&!cleared)nativeSelected={};
    if(typeof req.native_model==='string'&&req.native_model.length>0&&req.native_model.length<=128&&!/[\x00-\x1f\x7f]/.test(req.native_model)){
      nativeSelected={model:req.native_model,...(nativeSelected.model===req.native_model?{effort:nativeSelected.effort}:{})};
    }
    displayValues={...nativeSelected};
    // Spec E9 rule 33 (c): this seat's own values (its folder, Git, the account usage)
    // stay drawn across a start, resume, /clear or switch; session values clear.
    panelModel={labels:panelModel.labels,styles:panelModel.styles,repo:panelModel.repo,git:panelModel.git,quota:panelModel.quota,
      head:Array.isArray(panelModel.head)?panelModel.head.filter(part=>!/^\[/.test(String(part?.text))):undefined,
      ...(cleared?{selected:panelModel.selected,model_value:panelModel.model_value,effort_value:panelModel.effort_value}:{})};panelSections=undefined;panelLines=[];
    projectionKey='';footer='';footerSegments=[];
    const again=unselected&&unselected.sid===req.sid&&unselected.nativeEpoch===nativeEpoch;
    // The same session selected again with no key between keeps its open read page as well.
    routeDetail=again?unselected.routeDetail:false;routePage=again?unselected.routePage:0;routeScroll=again?unselected.routeScroll:0;unselected=null;
    sequence=req.seq;selection++;native=reporter;sid=req.sid;state='unknown';nativeTurn='';turnMark=0;seenTurn=null;turnRecord='';formSeen=false;eventMark=++clock;empty=false;block.leave();block.update([]);
    publishNativeDisplay();queueDisplay();
    if(currentDelivery)releaseHeld(currentDelivery,true);
    // A start or selection follows the keys that chose it (a resume picker's Enter, /clear):
    // bytes up to that Enter were submitted, so the screen reading can settle this frontier.
    // Without this, a seat reopened through a picker refused every delivery until a prompt.
    submitted=lastEnter;
    const selected=selection;
    // Display collection must never delay the native start hook or input.
    setImmediate(()=>{if(selected===selection)source('register').then(()=>projection());});
    return {ok:true};
  }
  if(req.sid!==sid)return {ok:false,reason:'selection-changed'};
  sequence=req.seq;const was=state;
  if(req.event==='deselect'){
    unselected={sid:req.sid,nativeEpoch,routeDetail,routePage,routeScroll};
    selection++;sid='';state=harness==='opencode'?'idle':'unknown';nativeTurn='';turnMark=0;seenTurn=null;turnRecord='';formSeen=false;eventMark=++clock;empty=false;footer='';footerSegments=[];panelLines=[];panelSections=undefined;panelModel={};displayValues={};nativeContext=undefined;routeDetail=false;routeScroll=0;panelOpen.clear();block.update([]);
    for(const row of watchers.values())row.watch.close();watchers.clear();
    source('register');rebuildDisplay();return {ok:true};
  }
  if(req.event==='prompt'||req.event==='submit') {
    submitted=lastEnter;state='working';empty=false;
    if(upgradeNote==='scrollback'){upgradeNote='';queueProjection();}
    if(currentDelivery){deliveries.set(currentDelivery,'submitted');releaseHeld(currentDelivery,true);rebuildDisplay();}
  } else if(req.event==='stop'||req.event==='idle') { state='idle';empty=false;setImmediate(()=>{detectRelease();checkRelease();}); }
  else if(req.event==='working') { state='working';empty=false; }
  else {state='blocked';empty=false;}
  // A hook event is newer than every reading asked for before it.
  eventMark=++clock;if(state==='blocked'&&was!=='blocked')formSeen=false;
  // The border's state is the host's own: paint it now, not after the projection.
  if(state!==was)queueDisplay();
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
  if(req.op==='status'){await refreshReadiness();if(empty)checkRelease();return {ok:true,...status()};}
  // The native cells under the panel and frame: the seat's own screen for draft/form judgments.
  if(req.op==='screen')return relay?{ok:false,reason:'display-unavailable'}:{ok:true,screen:screenSnapshot(),...status()};
  if(req.op==='panel'&&relay)return {ok:false,reason:'display-unavailable'};
  if(req.op==='panel'){foldByHand(req.expanded!==true);applyLayout();return {ok:true,...status()};}
  if(req.op!=='inject'||harness==='codex')return {ok:false,reason:'unsupported-operation',not_started:true};
  if(!running||succeeding)return {ok:false,reason:'not-idle-empty',not_started:true};
  if(typeof req.ref!=='string'||req.ref.length>128)return {ok:false,reason:'peer-ref-unavailable',not_started:true};
  if(deliveries.has(req.ref))return {ok:true,duplicate:true,state:deliveries.get(req.ref)};
  const safe=()=>!relay&&!succeeding&&exact(req)&&req.epoch===epoch&&state==='idle'&&empty&&!paste&&!keyTail&&(!block.focused||block.waitingForReceipt)&&!settings&&!fleetView&&!currentDelivery&&terminal.modes.bracketedPasteMode;
  if(!safe())return {ok:false,reason:'not-idle-empty',not_started:true};
  const selected=selection,revision=screenRevision;
  const data=await source('payload',{ref:req.ref,claim:req.claim,screen:screenSnapshot(),state});
  if(!data.ok||selected!==selection||revision!==screenRevision||!safe())return {ok:false,reason:data.reason||'input-changed',not_started:true};
  currentDelivery=req.ref;state='submitting';eventMark=++clock;empty=false;epoch++;nativeEpoch++;lastEnter=nativeEpoch;queueDisplay();queueProjection();
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
// A connection's one request line, answered by this host's own state.
function serve(conn,line) {
  setImmediate(async()=>{
    let result;try{
      const req=JSON.parse(line.toString());
      result=await request(req);}
    catch{result={ok:false,reason:'control-unavailable',not_started:false};}
    conn.end(JSON.stringify({...result,...identity()})+'\n');
  });
}
function onConnection(conn) {
  let raw=Buffer.alloc(0),consumed=false;conn.setTimeout(2000,()=>conn.destroy());
  conn.on('error',()=>{});
  conn.on('data',chunk=>{
    if(consumed)return;raw=Buffer.concat([raw,chunk]);if(raw.length>65536)return conn.destroy();
    const newline=raw.indexOf(10);if(newline<0)return;consumed=true;conn.pause();
    // While the display is frozen for hand-over no request is answered: it waits here for the
    // successor (retire) or for this host again (resume).
    if(succeeding){bound.push({conn,line:raw.subarray(0,newline+1)});return;}
    serve(conn,raw.subarray(0,newline));
  });
}
function serverFailure() {if(!activated)bypass();else{state='unknown';empty=false;}}
function listenControl(target,done) {
  const listener=net.createServer(onConnection);
  listener.on('error',serverFailure);
  listener.listen(target,()=>{fs.chmodSync(target,0o600);done(listener);});
  return listener;
}
terminal.parser.registerCsiHandler({prefix:'?',final:'h'},p=>{if(p.includes(25))cursorVisible=true;return false;});
terminal.parser.registerCsiHandler({prefix:'?',final:'l'},p=>{if(p.includes(25))cursorVisible=false;return false;});
for(const [code,rgb] of [[10,'dddd/dddd/dddd'],[11,'1111/1111/1111']])terminal.parser.registerOscHandler(code,text=>{
  if(text==='?'){child.write(`\x1b]${code};rgb:${rgb}\x1b\\`);return true;}return false;
});

// ---- the native, the input owner and the backend: the same for a first host and a successor ----
function adoptNative(paused) {
  const broker=processRow(Number(process.env.HEARTING_SHELL_BROKER_PID));
  if(!broker||broker.pid!==host.parent||broker.start!==process.env.HEARTING_SHELL_BROKER_START)throw Error('PTY owner changed');
  child=require('./adopted-pty.cjs')(pty,Number(process.env.HEARTING_SHELL_PTY_FD),
    Number(process.env.HEARTING_SHELL_NATIVE_PID),Number(process.env.HEARTING_SHELL_PTY_EXIT_FD),paused);
  native=processRow(child.pid);rootNative=native;
}
function createInputOwner() {
  inputOwner=require('./input-owner.cjs')(Number(process.env.HEARTING_SHELL_INPUT_FD),
    Number(process.env.HEARTING_SHELL_HELD_FD),ref=>{
      if(deliveries.get(ref)!=='submitted')deliveries.set(ref,'transport-submitted');
      releaseHeld(ref);
    });
}
function startBackend() {
  backend=cp.spawn(process.env.HEARTING_SHELL_PYTHON,[path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});
  backend.on('error',rawRelay);backend.on('exit',rawRelay);backend.stdin.on('error',()=>{});
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    try {const data=JSON.parse(line),call=calls.get(data.id);if(call){calls.delete(data.id);clearTimeout(call.timer);call.resolve(data);}}catch{}
  });
}
function wireDisplay() {
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
    // A successor that has not taken over owns nothing outside itself.
    if(!switched)process.exit(0);
    ended=true;clearTimeout(frameTimer);clearTimeout(projectionTimer);clearTimeout(beatTimer);
    for(const row of watchers.values())row.watch.close();watchers.clear();
    for(const watch of releaseWatchers)try{watch.close();}catch{}
    server.close();await source('unregister');backend?.stdin.end();
    emit('\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l');
    fs.rmSync(socketPath,{force:true});fs.rmSync(nextPath,{force:true});if(seedFile)fs.rmSync(seedFile,{force:true});process.exit(event.exitCode);
  });
}

// ---- release detection (H1) and the safe moment (H2) ----
function installedRelease() {try{return fs.realpathSync(process.env.HEARTING_SHELL_HOST_PATH);}catch{return null;}}
function upgradeNoteNow() {return upgradeNote||(upgrade?'pending':'');}
// A newer installed release is noticed when its link moves (an event, not a poll) and when
// the session goes idle. A link that cannot be watched leaves only the idle check.
function detectRelease() {
  if(!SUCCESSION||!activated||ended||relay||succeeding||!running)return;
  const target=installedRelease();
  if(!target||target===myRelease){
    if(upgrade&&!upgrade.requested){upgrade=null;queueProjection();}
    return;
  }
  if(upgrade&&upgrade.target===target)return;
  upgrade={target,requested:false,blocked:''};queueProjection();checkRelease();
}
function watchRelease() {
  const hostPath=process.env.HEARTING_SHELL_HOST_PATH;
  if(!SUCCESSION||!hostPath||!path.isAbsolute(hostPath))return;
  let probe=path.sep;const links=[];
  for(const part of hostPath.split(path.sep).filter(Boolean)){
    const next=path.join(probe,part);
    try{if(fs.lstatSync(next).isSymbolicLink())links.push([probe,part]);}catch{break;}
    probe=next;
  }
  for(const [directory,name] of links.slice(0,4)){
    try{
      const watch=fs.watch(directory,(_event,changed)=>{if(!changed||String(changed)===name)detectRelease();});
      watch.on('error',()=>{try{watch.close();}catch{}});releaseWatchers.push(watch);
    }catch{}
  }
}
// The old host's conditions for starting the hand-over. `empty` is not assumed: it is proven
// by one readiness read of this exact screen. Anything a person or a delivery is in the middle of keeps this false.
function successionSafe(skipEmpty=false) {
  const quiet=!relay&&activated&&!ended&&running&&!succeeding&&state==='idle'&&!paste&&!keyTail&&!block.focused&&!settings&&!fleetView
    &&!currentDelivery&&!releasing&&heldBytes===0&&!routeDetail&&!actionWorker&&!panelModel.answerable&&!dragging&&!settingsPaste
    &&displayVTs.size===0&&!rebuilding&&!checkingNativeSelection&&!modelReading&&!mouseInput.pending()&&terminal.modes.bracketedPasteMode;
  return quiet&&(skipEmpty||empty);
}
// Every reason to look again is kept until a look has seen it: one that comes while a look is under
// way is taken up when that look ends (it may have read a screen that changed since), and the second's
// pause after an attempt that ended unsafe (or a reading the backend could not give) ends in one look of
// its own, since an idle window has no other event to look again on. Neither polls the link.
function checkRelease() {
  if(checkingRelease){recheckRelease=true;return;}
  if(!upgrade||upgrade.requested||upgrade.blocked||succeeding||!successionChannel)return;
  const pause=(upgrade.retryAfter||0)-Date.now();
  if(pause>0){if(!releaseWake)releaseWake=setTimeout(()=>{releaseWake=undefined;checkRelease();},pause);return;}
  if(!successionSafe(true))return;
  checkingRelease=true;
  (async()=>{
    const key=[screenRevision,epoch,selection,state].join(':');
    if(!successionSafe()&&checkedKey!==key){
      checkedKey=key;
      // A reading with no answer for this screen proves nothing either way: a screen that moved is read
      // on the change that moved it, a backend that did not answer is asked again after the pause.
      const unread=await refreshReadiness();
      if(unread){checkedKey='';if(unread==='unavailable'&&upgrade)upgrade.retryAfter=Date.now()+1000;}
    }
    if(upgrade&&!upgrade.requested&&successionSafe()){upgrade.requested=true;succSend({op:'request',release:myRelease});}
  })().catch(()=>{}).finally(()=>{
    checkingRelease=false;
    if(recheckRelease||upgrade&&!upgrade.requested&&Date.now()<(upgrade.retryAfter||0)){recheckRelease=false;checkRelease();}
  });
}

// ---- the succession channel: the old host answers freeze/resume/retire, the new one commit/switch/run ----
function succSend(value) {try{successionChannel.write(JSON.stringify(value)+'\n');}catch{}}
function startSuccessionChannel() {
  if(!SUCCESSION||successionChannel)return;
  successionChannel=new net.Socket({fd:Number(process.env.HEARTING_SHELL_SUCCESSION_FD),readable:true,writable:true});
  successionChannel.on('error',()=>{});
  let queue=Promise.resolve();
  readline.createInterface({input:successionChannel}).on('line',line=>{
    let message;try{message=JSON.parse(line);}catch{return;}
    // One message at a time: a resume that arrives during a freeze waits for it to finish.
    queue=queue.then(()=>onSuccession(message)).catch(()=>{});
  });
}
async function onSuccession(message) {
  if(message.op==='declined'){
    if(upgrade){
      upgrade.requested=false;
      // `busy`: the owner is still ending an earlier attempt, so this one is asked again after the pause.
      if(message.reason==='busy')upgrade.retryAfter=Date.now()+1000;
      else if(message.reason!=='not-running')upgrade.blocked=String(message.reason||'declined');
      checkRelease();
    }
    return;
  }
  if(message.op==='freeze')return succSend(await freeze());
  if(message.op==='resume')return resumeHost(message.failed===true);
  if(message.op==='retire')return retireHost();
  if(!SUCCESSOR)return;
  if(message.op==='commit')return succSend(await commitSuccessor(message.seed));
  if(message.op==='switch')return succSend(await switchSuccessor());
  if(message.op==='run')return runSuccessor();
}

// H3/H4: stop reading both sides, let the VT parse what it already has, give up the binding and write the screen out.
function buildSeed() {
  // The seed is capped at 8 MiB; HEARTING_SHELL_SEED_LIMIT can only lower it (a test hook).
  const limit=Math.min(8*1024*1024,Number(process.env.HEARTING_SHELL_SEED_LIMIT)||8*1024*1024);
  const captured=succession.capture(terminal,{scrollback:true});
  const seed={schema:succession.SCHEMA,harness,sid,native:{pid:native.pid,start:native.start},rootNative:{pid:rootNative.pid,start:rootNative.start},
    sequence,selection,state,epoch,nativeEpoch,submitted,lastEnter,cursorVisible,expanded,foldChoice,displayConfiguration,displaySequence,
    panelOpen:[...panelOpen],deliveries:[...deliveries].filter(([,value])=>value==='submitted'||value==='transport-submitted').slice(-128),
    displayValues,nativeSelected,nativeContext:nativeContext??null,nativeMcp:nativeMcp??null,
    // The host's own turn observation (what the panel shows as the state), so the successor shows the same
    // state at its first paint and goes on reading the native's turn record; its marks keep their order.
    turn:{nativeTurn,turnMark,eventMark,clock,seenTurn,turnRecord,formSeen},
    panel:{model:{...panelModel,...(panelModel.tidy_upgrade?{tidy_notice:''}:{}),upgrade_waiting:''},lines:panelLines,sections:panelSections??null,footer,segments:footerSegments,nativeFooter},
    terminal:captured.terminal,scrollback:captured.scrollback,
    outer:{cols,rows,last:Array.from(last,value=>value??null),nativeLast:Array.from(nativeLast,value=>value??null),footerLast}};
  let json=JSON.stringify(seed);
  if(Buffer.byteLength(json)>limit&&seed.scrollback.carried){
    // Too large: carry the viewport only and say so, never cut the scrollback silently.
    seed.terminal=succession.capture(terminal,{scrollback:false}).terminal;seed.scrollback={carried:false,reason:'seed-too-large',lines:0};
    json=JSON.stringify(seed);
  }
  return json;
}
// The host's own display work (a panel rebuild, a scratch VT, a model or selection read) ends by
// itself within milliseconds: a freeze that meets it waits for it (inside the owner's freeze limit)
// instead of ending the attempt as unsafe, which would spend a successor on no person's activity.
function displayBusy(){return rebuilding||displayVTs.size>0||checkingNativeSelection||modelReading;}
async function freeze() {
  for(let waited=0;displayBusy()&&waited<200;waited++)await new Promise(resolve=>setTimeout(resolve,5));
  if(!successionSafe())return {ok:false,reason:'unsafe'};
  succeeding=true;
  try{
    process.stdin.pause();child.pause();
    clearTimeout(frameTimer);clearTimeout(projectionTimer);clearTimeout(beatTimer);frameTimer=projectionTimer=beatTimer=undefined;
    await new Promise(resolve=>terminal.write('',resolve));
    await new Promise(resolve=>process.stdout.write('',resolve));
    if(currentDelivery||releasing||heldBytes||paste||keyTail||settings||fleetView||mouseInput.pending()){await resumeHost(false);return {ok:false,reason:'unsafe'};}
    await source('unregister');
    const json=buildSeed();
    seedFile=path.join(dir,`succession-${GENERATION+1}.json`);
    fs.writeFileSync(seedFile+'.tmp',json,{mode:0o600});fs.renameSync(seedFile+'.tmp',seedFile);
    return {ok:true,seed:seedFile};
  }catch(error){
    await resumeHost();
    if(upgrade)upgrade.blocked='capture';
    return {ok:false,reason:'capture'};
  }
}
// H7: the successor did not take over. Take the socket path back if it is no longer this host's,
// publish the binding again and paint the whole screen.
async function resumeHost(failed=true) {
  // A host that only asked (nothing frozen) learns the attempt is over. A counted failure may retry at once,
  // another end (not safe after all) waits a second so a persistent condition cannot spin.
  if(upgrade)upgrade.retryAfter=failed?0:Date.now()+1000;
  // A host that only asked also paints the whole screen once: whatever a dying successor wrote is overwritten.
  if(!succeeding){if(upgrade)upgrade.requested=false;last=[];footerLast='\0';if(!relay&&!ended)schedule();checkRelease();return;}
  let listenFailed=false;
  try{
    let ino=-1;try{ino=fs.statSync(socketPath).ino;}catch{}
    if(ino!==socketIno){
      fs.rmSync(nextPath,{force:true});
      staleServers.push(server);
      // A listen that fails settles too: a rollback never leaves the host frozen with the queue blocked.
      await new Promise(resolve=>{
        server=listenControl(nextPath,()=>{try{fs.renameSync(nextPath,socketPath);socketIno=fs.statSync(socketPath).ino;}catch{listenFailed=true;}resolve();});
        server.once('error',()=>{listenFailed=true;resolve();});
      });
    }
    if(seedFile){fs.rmSync(seedFile,{force:true});fs.rmSync(seedFile+'.tmp',{force:true});}
  }catch{}
  // No control socket to serve: the display keeps nothing frozen and falls back to the plain relay.
  if(listenFailed){for(const entry of bound.splice(0))entry.conn.destroy();rawRelay();return;}
  succeeding=false;
  if(upgrade)upgrade.requested=false;
  await source('register');
  child.resume();process.stdin.resume();
  last=[];footerLast='\0';
  if(layoutDeferred){layoutDeferred=false;applyLayout();}
  schedule();queueProjection();
  for(const entry of bound.splice(0))if(!entry.conn.destroyed)serve(entry.conn,entry.line.subarray(0,entry.line.length-1));
}
// H9: hand each waiting connection to the successor's socket (same path) and leave without
// closing this host's listener: closing it would unlink the path the successor now owns.
function forward(entry) {
  return new Promise(resolve=>{
    if(entry.conn.destroyed)return resolve();
    const up=net.connect(socketPath);let finished=false;
    const finish=()=>{if(!finished){finished=true;resolve();}};
    up.setTimeout(1500,()=>{up.destroy();entry.conn.destroy();finish();});
    up.on('connect',()=>up.write(entry.line));
    up.on('data',data=>entry.conn.write(data));
    up.on('end',()=>{entry.conn.end();finish();});
    up.on('error',()=>{entry.conn.destroy();finish();});
  });
}
async function retireHost() {
  // The owner retires only a host it froze; one that fell to the relay meanwhile leaves as well.
  if(!succeeding&&!relay)return;
  await new Promise(resolve=>setImmediate(resolve));
  await Promise.all(bound.splice(0).map(forward));
  try{backend?.stdin.end();}catch{}
  if(seedFile){try{fs.rmSync(seedFile,{force:true});}catch{}}
  process.exit(0);
}

// H8: the successor. It prepares (assets, Terminal), commits (reads the seed, rebuilds the screen
// and state, listens on a side path, warms its backend), switches (takes the control path) and
// runs (reads the PTY and the keyboard) only on the owner's word.
async function commitSuccessor(file) {
  try{
    const info=fs.lstatSync(file);
    if(typeof file!=='string'||path.dirname(file)!==dir||!info.isFile()||info.uid!==process.getuid()||info.mode&0o077||info.size>9*1024*1024)throw Error('seed file');
    const seed=JSON.parse(fs.readFileSync(file,'utf8'));
    const bad=succession.validate(seed);if(bad)throw Error('seed '+bad);
    if(seed.harness!==harness)throw Error('seed harness');
    seedFile=file;
    adoptNative(true);
    const root=processRow(seed.rootNative.pid),row=processRow(seed.native.pid);
    if(!root||root.start!==seed.rootNative.start||child.pid!==root.pid||!row||row.start!==seed.native.start)throw Error('native changed');
    rootNative=root;native=row;
    sid=seed.sid;sequence=seed.sequence;selection=seed.selection;state=seed.state;epoch=seed.epoch;nativeEpoch=seed.nativeEpoch;
    submitted=seed.submitted;lastEnter=seed.lastEnter;cursorVisible=seed.cursorVisible;expanded=seed.expanded;empty=false;
    // An earlier release's open-by-hand choice also carried its width (`{folded:false,cols}`).
    const choice=seed.foldChoice;foldChoice=choice&&typeof choice==='object'&&typeof choice.folded==='boolean'?{folded:choice.folded}:null;
    displayConfiguration=seed.displayConfiguration;displaySequence=Number.isSafeInteger(seed.displaySequence)?seed.displaySequence:0;
    for(const key of seed.panelOpen.slice(0,16))if(typeof key==='string')panelOpen.add(key);
    for(const [ref,value] of seed.deliveries)deliveries.set(ref,value);
    const object=value=>value&&typeof value==='object'&&!Array.isArray(value)?value:{};
    displayValues=object(seed.displayValues);nativeSelected=object(seed.nativeSelected);
    nativeContext=seed.nativeContext&&typeof seed.nativeContext==='object'?seed.nativeContext:undefined;
    nativeMcp=seed.nativeMcp&&typeof seed.nativeMcp==='object'?seed.nativeMcp:undefined;
    {
      // Absent or malformed (an older seed): the first projection reads the native's record again.
      const turn=object(seed.turn),count=value=>Number.isSafeInteger(value)&&value>=0?value:0,word=value=>['idle','working','blocked'].includes(value)?value:'';
      nativeTurn=word(turn.nativeTurn);clock=count(turn.clock);turnMark=Math.min(count(turn.turnMark),clock);eventMark=Math.min(count(turn.eventMark),clock);
      seenTurn=word(turn.seenTurn?.value)?{value:turn.seenTurn.value,mark:Math.min(count(turn.seenTurn.mark),clock)}:null;
      turnRecord=typeof turn.turnRecord==='string'&&path.isAbsolute(turn.turnRecord)?turn.turnRecord:'';formSeen=turn.formSeen===true;
    }
    const panel=object(seed.panel);
    panelModel=object(panel.model);panelLines=Array.isArray(panel.lines)?panel.lines:[];panelSections=panel.sections??undefined;
    footer=typeof panel.footer==='string'?panel.footer:'';footerSegments=Array.isArray(panel.segments)?panel.segments:[];
    nativeFooter=panel.nativeFooter&&typeof panel.nativeFooter==='object'?panel.nativeFooter:null;
    await succession.restore(terminal,seed.terminal);
    last=seed.outer.last.map(value=>value===null?undefined:value);nativeLast=seed.outer.nativeLast.map(value=>value===null?undefined:value);
    footerLast=seed.outer.footerLast;seedOuter={cols:seed.outer.cols,rows:seed.outer.rows};
    if(seed.scrollback.carried===false)upgradeNote='scrollback';
    fs.rmSync(nextPath,{force:true});
    await new Promise(resolve=>{server=listenControl(nextPath,()=>resolve());});
    startBackend();
    // The panel rows are rebuilt from the seeded values before the first paint, so that paint is a difference.
    rebuildDisplay();
    for(let waited=0;(rebuilding||rebuildPending)&&waited<400;waited++)await new Promise(resolve=>setTimeout(resolve,5));
    return {ok:true};
  }catch(error){return {ok:false,reason:String(error&&error.message||error).slice(0,80)};}
}
let seedOuter={cols:0,rows:0};
async function switchSuccessor() {
  try{
    fs.renameSync(nextPath,socketPath);socketIno=fs.statSync(socketPath).ino;
    switched=true;activated=true;
    try{fs.rmSync(seedFile,{force:true});}catch{}
    // Stay paused (no PTY, no keys) until the owner says run: a successor lost now loses nothing.
    process.stdin.pause();
    wireDisplay();
    // Normally nothing differs and nothing is cleared: the old host left the screen painted.
    cols=process.stdout.columns;rows=process.stdout.rows;
    if(cols!==seedOuter.cols||rows!==seedOuter.rows||terminal.cols!==nativeColumns()||terminal.rows!==nativeRows())applyLayout();
    await source('register');
    clearTimeout(frameTimer);frameTimer=undefined;render();
    queueProjection();
    return {ok:true,settled:true};
  }catch(error){return {ok:false,reason:String(error&&error.message||error).slice(0,80)};}
}
function runSuccessor() {
  if(running||!switched)return;
  running=true;
  child.resume();createInputOwner();process.stdin.resume();
  // From now on this host is the window's display, so it notices the next install as the first
  // host does: by the watch on the link's directory. The check below sees one made before the watch.
  watchRelease();
  refreshReadiness().finally(()=>{detectRelease();checkRelease();});
}

// ---- start ----
server=net.createServer(onConnection);
server.on('error',serverFailure);
if(SUCCESSOR){
  // Nothing is read, listened or spawned until the owner commits: a failure here is a quiet exit.
  startupResult('PREPARED');
  startSuccessionChannel();
}else server.listen(socketPath,()=>{
  fs.chmodSync(socketPath,0o600);
  try {
    socketIno=fs.statSync(socketPath).ino;
    adoptNative(false);createInputOwner();
  } catch {server.close();fs.rmSync(socketPath,{force:true});bypass();return;}
  // The parent may only fall back before this boundary. A failed display keeps
  // this same native child; no second CLI is ever started.
  startupResult('STARTED');activated=true;process.stdin.resume();
  startBackend();
  // OpenCode starts on its home route with no session: publish the seat now.
  if(harness==='opencode'&&!sid){state='idle';source('register');}
  source('display-bootstrap').then(data=>{if(data.ok&&!projectionKey){panelModel=data.panel;queueDisplay();}});
  emit('\x1b[?1049h\x1b[2J');
  wireDisplay();
  applyLayout();
  startSuccessionChannel();watchRelease();detectRelease();
});
