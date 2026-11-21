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
const picking=require('./selection.cjs');
const {serialize}=succession;
// A broker that offers hand-over names its channel; without it this host never asks for or takes one.
const SUCCESSION=process.env.HEARTING_SHELL_SUCCESSION==='1'&&Number(process.env.HEARTING_SHELL_SUCCESSION_FD)>=3;
const SUCCESSOR=SUCCESSION&&process.env.HEARTING_SHELL_SUCCESSOR==='1';
const GENERATION=Number(process.env.HEARTING_SHELL_GENERATION)||0;
const panelView=require('./panel-view.cjs');
const statusList=require('./status-list.cjs');
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
// The mouse selection in the native area (see `nativePick`) and the last press that began one.
let pick = null, pressed = null;
// The screen revision the native had painted when its last native-bound byte arrived, and when
// (monotonic ms) it arrived. A successor counts from its own start: it knows no earlier key's time.
let keyRevision = -1, keyAt = performance.now();
// The input frontier after the last byte that may have put a character into the composer (text, a
// paste, an Enter, a key whose meaning the native decides). Bytes after it are keys and reports that
// cannot (QUIET_KEY): once they are quiet, an empty composer on screen needs no repaint after them.
// Up, Down, Right and End are not among them: they can bring a past prompt or a suggestion into the
// composer, so the native must paint once after them (a slow one repaints its old, empty screen first).
let typed = 0;
// Why the last reading kept the window from taking a message (`status().hold`): what this host
// knows, for the sender's record (`peer-message` pending row `held`). Empty while it takes one.
let lastHold = 'unsettled';
const HOLD_OF={'form-open':'form','native-trust-wait':'form','draft':'draft','draft-unknown':'draft'};
const QUIET_KEY=/^(?:\x1b|\x1b\[[\d;]*[DEGHPQRSZ]|\x1b\[(?:[1-3567]|1\d|2[0-689]|3[0-4])(?:;\d+)*~|\x1bO[DHP-S]|\x1b\[<[\d;]*[Mm]|\x1b\[[?>=][\d;:$ ]*[@-~]|\x1b\[[\d;]*[nt]|\x1b[\]P_][^\x07\x1b]*(?:\x07|\x1b\\))$/;
// How long the keys must have been quiet before a screen read after them may stand for them: right
// after a person's Enter the composer is empty before that turn's prompt callback comes, and a slow
// native repaints its old screen before the keys' own. Only compared at a reading that comes anyway
// (a courier pass's status, an idle transition); nothing waits for it.
const KEY_QUIET_MS = 2000;
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
let panelModel={},panelHits=[],settings,dragging=false;
// The route box's place in its routes (spec rule 23: rows short, it scrolls in place under the wheel)
// and the rows it takes on the panel, as the last paint drew them.
let routeScroll=0,routeArea=null;
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
function status() {
  const ready=empty && !keyTail && !paste && (!block.focused||block.waitingForReceipt) && !table && !settings && !fleetView;
  // `busy` a turn runs, `form` a native or shell menu/question is open, `draft` text stands in the
  // composer, `paste` one is under way, `unsettled` keys are not settled yet, `unobserved` the
  // native's rest is not confirmed.
  const hold=ready&&state==='idle'?'':paste?'paste':state==='blocked'?'form':state!=='idle'?'busy'
    :table||settings||fleetView||block.focused&&!block.waitingForReceipt?'form':keyTail?'unsettled':lastHold||'unsettled';
  return {...identity(),epoch,state,empty:ready,hold,alive:live(),expanded,focused:block.focused||!!table||!!settings||!!fleetView,held_bytes:heldBytes,projection_state:relay||sourceError?"unavailable":"current"};
}
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
    // A read that outlives its 12 seconds ends then (a person's answer is left to finish: it holds the one slot).
    const timer=setTimeout(()=>{finish({ok:false,reason:'action-unavailable'});if(operation!=='answer')try{worker.kill('SIGKILL');}catch{}},12000);
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
// The popup's own reader (NOW's list, and the routes popup's route and page). A key in NOW's folded list asks for
// the list again with the row stood on open, and a backend started for each key costs 0.2 s (its modules load
// again); a routes popup's key would otherwise collect Fleet again (4-9 s). So one backend is held while that
// popup stands and asked a line at a time (the backend already answers a line at a time). It ends with the popup
// (closed, another section, the window ended) and when it fails; a read it cannot answer in time, or at all,
// is made the one-shot way as before, and the popup then reads that way for as long as it stands.
let heldWorker=null,heldNext=0;
// keep: the kind of popup being opened; a backend held for that kind stays.
function heldStop(keep){
  if(keep&&heldWorker&&heldWorker.kind===keep)return;
  const worker=heldWorker;heldWorker=null;if(!worker)return;
  // A routes read cut at its time says so: the one-shot read would collect the same again and wait as long once more.
  // NOW's (a list in milliseconds) goes the one-shot way.
  if(worker.pending){clearTimeout(worker.pending.timer);worker.pending.resolve(worker.cut&&worker.kind!=='NOW'?{ok:false,reason:'timeout'}:null);worker.pending=null;}
  try{worker.stdin.end();}catch{}
  try{worker.kill('SIGKILL');}catch{}
}
function heldRead(kind,extra,wait){
  return new Promise(resolve=>{
    if(heldWorker&&heldWorker.kind!==kind)heldStop();
    let worker=heldWorker;
    if(!worker){
      try{worker=cp.spawn(process.env.HEARTING_SHELL_PYTHON,[path.join(__dirname,'..','session_shell.py'),'backend'],{stdio:['pipe','pipe','ignore']});}
      catch{resolve(null);return;}
      worker.pending=null;worker.raw='';worker.warm=false;worker.kind=kind;heldWorker=worker;
      const fail=()=>{if(heldWorker===worker)heldStop();else if(worker.pending){clearTimeout(worker.pending.timer);worker.pending.resolve(null);worker.pending=null;}};
      worker.on('error',fail);worker.stdin.on('error',fail);worker.on('close',fail);
      worker.stdout.setEncoding('utf8');
      worker.stdout.on('data',bytes=>{
        worker.raw+=bytes;
        for(let at=worker.raw.indexOf('\n');at>=0;at=worker.raw.indexOf('\n')){
          const line=worker.raw.slice(0,at);worker.raw=worker.raw.slice(at+1);
          const pending=worker.pending;if(!pending)continue;
          let value;try{value=JSON.parse(line);}catch{fail();return;}
          if(value?.id!==pending.id)continue;
          worker.pending=null;worker.warm=true;clearTimeout(pending.timer);pending.resolve(value);
        }
        if(Buffer.byteLength(worker.raw)>65536)fail();
      });
    }
    const id=++heldNext;
    // The first answer waits for the backend to start; the ones after it come in milliseconds.
    const timer=setTimeout(()=>{if(heldWorker===worker){worker.cut=true;heldStop();}},!worker.warm?12000:(wait||4000));
    worker.pending={id,timer,resolve};
    try{worker.stdin.write(JSON.stringify({id,op:'popup',binding:binding(),...extra})+'\n');}catch{if(heldWorker===worker)heldStop();}
  });
}
process.on('exit',heldStop);
function emit(value) { process.stdout.write(value); }
function geometry(){return nativeDisplay.layout({cols,rows,expanded,configuration:displayConfiguration});}
// Spec rule 49: the panel opened or folded through the same layout path a window resize takes
// (the native is told its new width by the PTY resize in applyLayout). An open settings editor or
// the panel's edge being dragged keeps an open panel open. A question is answered in the popup
// (11-15), which stands over the window whether the panel is open or folded.
function pinned(){return expanded&&(!!settings||dragging);}
function refold(){
  const next=nativeDisplay.fold({cols,choice:foldChoice,configuration:displayConfiguration,pinned:pinned(),expanded});
  foldChoice=next.choice;
  if(expanded===!next.folded)return;
  expanded=!next.folded;
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
function maxHeight(){if(!expanded)return nativeDisplay.foldRows(rows);return geometry().side?rows:Math.min(displayConfiguration.narrow==='compact'&&!settings?8:displayConfiguration.bottom_rows||14,Math.max(4,rows-6));}
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
  const model=stale?{...panelModel,stale}:panelModel;
  return panelView.render({width:panelColumns(),height:maxHeight(),side:geometry().side,
    model,native:displayValues,state:shown,sid,harness,frame:spinFrame(),since:motionSince(shown),sections:panelSections,configuration:displayConfiguration,
    settings,routeScroll,focused:block.focused,cwd:process.cwd(),cellWidth,background:terminalBackground,folded:!expanded,
    brief:()=>block.summary(),sent:()=>block.sent()});
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
// A part's background as SGR: a 256-color index, or `#rrggbb` (24-bit: the popup's ground where the terminal takes it).
function backgroundSgr(value){
  if(Number.isInteger(value)&&value>=0&&value<=255)return '48;5;'+value;
  const m=/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(value??''));
  return m?'48;2;'+m.slice(1).map(x=>parseInt(x,16)).join(';'):'49';
}
function cellAttr(part){
  const a=[0,foregroundSgr(part.foreground)||'39',backgroundSgr(part.background)];
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
    if(!stale(list[k])||covered(screen,column+list[k].col-1)){k++;continue;}
    let run=`\x1b[${screen+1};${column+list[k].col}H`,current=null;
    for(let x=list[k].col;k<list.length&&list[k].col===x&&stale(list[k])&&!covered(screen,column+x-1);k++){
      const c=list[k];x+=Math.max(1,cellWidth(c.ch));if(current!==now[c.i]){run+=`\x1b[${now[c.i]}m`;current=now[c.i];}run+=c.ch;
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
function beating(){return panelAnimated&&activated&&!ended&&!relay&&!succeeding&&!fleetView&&!settings&&panelRows.length>0&&veilTone()===null;}
// The beat wakes at the next moment something painted changes: a spinner's next frame, the
// moving frame's next change (the scanners' next step; a breath's next shade), a blink's phase
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
    if(spin.includes(token)&&!covered(y,column+col-1))out+=`\x1b[${y+1};${column+col}H${sgr}${glyph}`;
    col+=cellWidth(token);
  }
  return out&&out+'\x1b[0m';
}
// Only the changed cells of the panel rows (the spinner glyphs, the moving frame's cells), with
// the cursor kept where the native left it. A native frame already on its way paints them itself.
// A row whose other cells were not written as they stand now goes whole (the panel part).
function spinPaint(){
  if(frameTimer||!paintedSpins.size&&!paintedMotion||succeeding||veiledFrame!==null||veilTone()!==null)return;
  let text='';const side=geometry().side,wrote=new Set();
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
      if(!quiet){motionWrote(y,y,column);wrote.add(y);}
      last[y]=line;written[y]=panelKey[y];
    }else{
      const row=rows-height()+y;if(expanded&&y===0&&!panelLines.length&&typeof nativeContext?.percent==='number'&&harness!=='claude')continue;
      if(last[row]===panel)continue;
      const quiet=last[row]!==undefined&&written[row]===panelKey[y];
      text+=quiet?cellSpan(y,row,1):`\x1b[${row+1};1H`+panel;
      if(!quiet){motionWrote(y,row,1);wrote.add(row);}
      last[row]=panel;written[row]=panelKey[y];
    }
  }
  text+=popupOver(wrote,false);
  if(text)emit('\x1b[?2026h\x1b7'+text+'\x1b8\x1b[?2026l');
}
// ---- the popup (11-15): one layer over the scene ----
// The question popup stands while the question editor has the keys (`block.focused`): Ctrl+], a
// press on the panel's QUESTIONS rows or on the folded strip's row of what to do opens it; a new
// question never does. It is laid over the rows the scene writes (the native's own copy and the
// panel) in the same synchronized frame: a row written whole again takes the popup's cells again
// after it, and the cells that turn on their own under it (a spinner, the moving frame) are left
// alone. Closed or moved, the rows it covered are written again from the native's copy as it is
// now. The PTY keeps its size; the frame and its rows are panel-view's `popup`.
let popupLayer=null,popupRect=null,overlayLast=[],overlayBase=null;
// 11-15 (popup design B, E): the other popups: Fleet's own route table, drawn by the backend at the
// popup's width (`popup`, read in its own worker as a person's
// read: it never takes the answer's one action slot), and the messages read whole. `table` is the
// one standing; the question popup stands while the editor has the keys. Both share the frame, the
// keys (←→ the sections, ↑↓ Tab the rows, Enter, PgUp/PgDn, Esc · Ctrl+] · ✕ · a press outside)
// and the rule that nothing opens by itself.
// (11-15: the panel's LAB and SESSIONS sections are gone, the person's word "실험 현황은 다소 섣부른
// 추가같은데. 지금은 일단 빼" and the answer that "세션 현황" meant this session's own progress.)
let table=null,tableAsking=false,tableAgain=false;
// 11-18: the routes popup's first screen. The last table it read for this session (the one: at its width, with its
// route unfolded, as the panel's route values stood), shown at once when the popup is opened again; else the panel's
// own route box laid at the popup's width (`panelView.routesBody`); a reading follows either way and replaces it.
let routePopup=null;
// 11-15 (design 2): the status popup first, as its button stands at the panel's head (the REPO row).
// 11-20 (the person: "메모리 ... 메시지 처럼 팝업이 가능하게"): MEM opens a popup of the same frame and keys as MESSAGES', its rows drawn from the
// records the panel already holds (`panelView.memoryBody`): no reading, no new kind of popup.
const SECTIONS=['STATUS','BACKLOG','NOW','QUESTIONS','ROUTE','MESSAGES','MEM'];
const POPUP_KIND={ROUTE:'routes',STATUS:'status',BACKLOG:'backlog',NOW:'now'};
// 11-21: the status popup and the backlog popup are one list screen (`status-list.cjs`): the same state, keys and drawing; the backlog has no Fleet lines.
const listed=kind=>kind==='STATUS'||kind==='BACKLOG';
// 11-18: the width kind of a table popup: the routes popup stands wider (`panelView.popupWidth`), the others as a table; the question popup is `question`.
const tableKind=name=>name==='ROUTE'?'route':'table';
// 11-15 (design 2): the panel's status button value (Fleet's `session_status.read`: the status document of
// this session's campaign, else the one Fleet's project line picks; null where the project has no
// artifact root, and no button then), read in its own worker so a slow first read holds neither the panel
// nor a key: at the first projection, when this session's route list changes, and at an event of the file
// watch on the paths it names (no timer, no poller); between them the value read last stands. The popup's
// rows are kept the same way (`statusPopup`: read again only at such an event, or at another width).
let statusValue=null,statusAsking=false,statusAgain=false,statusAgainDirty=false,statusKey='',statusDirty=false,statusGeneration=0,
  statusTargets=new Set(),statusPopup=null,lastWatch=[];
// 11-21: the two short lists the panel shows under REPO (the first items and the count of the status document's and of `backlog.md`), read with the
// button's value in that same worker and only when the document changed (`sign`: sent back with the next ask, so an unchanged document is
// neither read nor parsed again). The panel draws from these; the drawing reads no file.
let panelLists={status:null,backlog:null};
const STATUS_ROUTE=/^[A-Za-z0-9_.-]{1,80}$/;
function statusRoutes(panel=panelModel){return [...new Set((Array.isArray(panel.routes)?panel.routes:[]).map(r=>r&&r.id).filter(id=>typeof id==='string'&&STATUS_ROUTE.test(id)))].slice(0,8);}
// The button's value as the popup's read takes it (so the popup does not read again what the button read).
function statusHeld(){const v=statusValue;
  return v&&typeof v==='object'?{word:String(v.word||''),title:String(v.title||''),has_doc:v.has_doc===true,path:String(v.path||''),slot:String(v.slot||'')}:null;}
// 11-20: the panel draws the button alone (the word, and whether a document stands: dim when none); the document's lists are the popup's.
// The name of the folder this window's status document is in: the backlog puts the group of that name first.
function ownFolder(){const p=statusValue&&typeof statusValue.path==='string'?statusValue.path:'';return p?path.basename(path.dirname(p)):'';}
function listItems(v){return (Array.isArray(v?.items)?v.items:[]).filter(i=>i&&typeof i.text==='string').slice(0,3).map(i=>({mark:i.mark==='!'?'!':'',text:i.text,key:typeof i.key==='string'?i.key:''}));}
function listTotal(v,items){return Math.max(items.length,Number.isInteger(v?.total)&&v.total>=0?v.total:0);}
function statusDoc(){
  if(!(statusValue&&typeof statusValue.word==='string'))return null;
  const items=listItems(panelLists.status);
  return {word:statusValue.word,has_doc:statusValue.has_doc===true,items,total:listTotal(panelLists.status,items)};}
// The backlog part stands wherever the status one does (an artifact root): its word dim when the document is not there or cannot be read.
function backlogDoc(){
  if(!(statusValue&&typeof statusValue.word==='string'))return null;
  const items=listItems(panelLists.backlog);
  return {word:'BACKLOG',has_doc:panelLists.backlog?.has_doc===true,items,total:listTotal(panelLists.backlog,items)};}
function askStatus(dirty=false,routes=statusRoutes()){
  if(statusAsking){statusAgain=true;statusAgainDirty=statusAgainDirty||dirty;return;}
  statusAsking=true;statusAgain=false;statusAgainDirty=false;
  const selected=selection;
  const have={};for(const name of ['status','backlog'])if(typeof panelLists[name]?.sign==='string')have[name]=panelLists[name].sign;
  readAction({cwd:process.cwd(),routes,have},'status',true).then(result=>{
    if(selected!==selection)return;
    // A read that failed or ran out its time is asked again at the next projection event (the key it stood for is
    // forgotten), never by a timer; one asks at a time, so a failure that goes on stacks no worker.
    if(!result?.ok){statusKey='';return;}
    const value=result.status&&typeof result.status==='object'?result.status:null;
    const changed=JSON.stringify(value)!==JSON.stringify(statusValue);
    statusValue=value;
    // (a list the answer says is the same document stays as it is; no artifact root: no lists)
    if(!value)panelLists={status:null,backlog:null};
    else for(const name of ['status','backlog']){const got=result.lists&&typeof result.lists==='object'?result.lists[name]:null;if(got&&typeof got==='object'&&got.same!==true)panelLists[name]=got;}
    statusTargets=new Set((Array.isArray(value?.watch)?value.watch:[]).filter(t=>typeof t==='string'&&path.isAbsolute(t)).slice(0,10));
    if(changed||dirty)statusGeneration++;
    if(observeFiles([...lastWatch,...statusTargets]))queueProjection();
    panelModel={...panelModel,status_doc:statusDoc(),backlog_doc:backlogDoc()};
    if(listed(table?.kind)&&(changed||dirty))askTable();
    queueDisplay();
  }).finally(()=>{statusAsking=false;if(statusAgain)askStatus(statusAgainDirty);});
}
// At a projection: the value is read again when this session's route list changed or a watch event came on its paths.
function observeStatus(panel){
  const key=JSON.stringify([process.cwd(),statusRoutes(panel)]);
  if(key!==statusKey||statusDirty){const dirty=statusDirty;statusKey=key;statusDirty=false;askStatus(dirty,statusRoutes(panel));}
}
function popupShown(){return (block.focused||!!table)&&activated&&!relay&&!ended&&!fleetView&&!settings&&!succeeding;}
// The sections with something in them, in the panel's order (the one standing always).
function sections(){
  const m=panelModel,list=v=>Array.isArray(v)&&v.length>0;
  const has={STATUS:!!statusDoc(),BACKLOG:!!backlogDoc(),NOW:!!m.now,QUESTIONS:!!block.summary()||!!m.wait||!!m.wait_guide,ROUTE:list(m.routes)||!!m.route_label,
    MESSAGES:list(m.messages)||list(m.requests),MEM:list(m.memory_items)};
  const current=table?table.kind:'QUESTIONS';
  return SECTIONS.filter(name=>has[name]||name===current);
}
function label(name,fallback){return panelModel.labels?.[name]||fallback;}
// The popup's own words: its keys, for this kind (the panel fold key says which way it goes).
function tableGuide(kind){
  const own=kind==='ROUTE'?label(table?.page?'popup_route_page':'popup_route','↑↓ route · Enter open')
    :kind==='NOW'?label(table?.page?'popup_now_page':'popup_now',table?.page?'Enter back to the list':'↑↓ · Enter read that part')
    // 11-21: the status popup with a list screen names its keys; one without (a hand-written page, none) keeps the keys it had.
    // (the document read whole has the keys every popup has: its Esc goes back to the list, and Enter does too)
    :listed(kind)&&table?.listing&&table.screen!=='full'?label('popup_status','↑↓ · Enter open'):'';
  const fold=label(expanded?'popup_fold':'popup_unfold',expanded?'p fold panel':'p open panel');
  return [own,label('popup_keys','←→ section · PgUp/PgDn').replace('{panel}',fold)].filter(Boolean);
}
// Key words packed into rows of `inner` cells, broken only between their ` · ` pieces (the one
// rule of every popup's key line, `panelView.keyRows`); short of `rows` the closing key stays.
function wrapGuide(lines,inner,rows){
  return panelView.keyRows(lines.flatMap(line=>String(line).split(' · ')),inner,{rows,cellWidth});
}
// A table popup's title: the section's own head (`4 ROUTE ⠏2 ✕1`).
function tableTitle(kind){
  const dim=v=>({text:v,dim:true}),m=panelModel;
  if(kind==='ROUTE'){const r=/^route (\d+)(.*)$/.exec(String(m.route_label||'').trim()),head=r?[{text:r[1]},dim(' ROUTE'+r[2])]:[dim('ROUTE')];
    // A first screen that is not this reading's says so at the end of the title, which stands whatever the scroll.
    return table?.kind==='ROUTE'&&table.loaded&&table.stale?[...head,dim(' · '+staleWords(table.stale))]:head;}
  // `STATUS <campaign> · <age>` (the word as the button's, dim; `STATUS` alone without a document).
  if(listed(kind)){const word=kind,whole=String(table?.title||(kind==='STATUS'?statusValue?.title:'')||word);
    return whole.startsWith(word)&&whole.length>word.length?[dim(word),{text:whole.slice(word.length)}]:[dim(whole)];}
  // `NOW · <counts>` for the list; `NOW · <time> <kind>` for the part being read.
  if(kind==='NOW'){const v=table?.page?table.read:table?.list,more=String(v?.title||'');return more?[dim('NOW · '),{text:more}]:[dim('NOW')];}
  if(kind==='MEM')return [dim('MEM')];
  return [dim('MESSAGES')];
}
// `as read at 14:05 · reading again` (the words are the panel's; no time known: what follows the ` · `).
function staleWords(stale){
  const failed=stale.failed===true,words=label(failed?'popup_shown_failed':'popup_shown_at',failed?'as read at {time} · could not read again':'as read at {time} · reading again');
  const at=Number(stale.at);
  if(!(at>0)){const cut=words.indexOf(' · ');return cut>=0?words.slice(cut+3):words.replace('{time}','').trim();}
  const d=new Date(at*1000),two=n=>String(n).padStart(2,'0'),today=new Date();
  // A reading of another day says the day (`11-17 18:30`); today's is the time alone.
  const day=d.toDateString()===today.toDateString()?'':two(d.getMonth()+1)+'-'+two(d.getDate())+' ';
  return words.replace('{time}',day+two(d.getHours())+':'+two(d.getMinutes()));
}
function tableRows(inner){
  const t=table;
  if(t.kind==='MESSAGES')return [...panelView.messagesBody({model:panelModel,columns:inner,cellWidth}),...actionLines];
  if(t.kind==='MEM')return panelView.memoryBody({model:panelModel,columns:inner,cellWidth});
  if(t.kind==='NOW')return nowRows(t);
  if(!t.loaded)return [[{text:t.error?label('popup_unread','could not read'):'⠏',dim:true}]];
  if(listed(t.kind))t.hits=[];
  if(statusListing(t)){
    // 11-21: the list screen, laid out by the popup's own state. A line chosen that the document no longer has (read again,
    // or its group closed) falls to the first one, at the top.
    const view=statusList.layout(t.listing,t,{width:inner,measure:cellWidth});
    if(view.fellBack&&t.sel!==view.sel){t.sel=view.sel;t.scroll=0;}
    t.listView=view;t.hits=statusList.hits(view);
    return view.rows;
  }
  // The document read whole of a page that has a list screen: its own answer (`part: 'doc'`), Fleet's lines above it.
  if(listed(t.kind)&&t.listing){
    if(!Array.isArray(t.doc))return [[{text:t.docError?label('popup_unread','could not read'):'⠏',dim:true}]];
    return [...(t.head||[]),...t.doc];
  }
  return t.rows;
}
// The status popup is on its list screen (a generated campaign page; the document read whole is `full document ›`).
function statusListing(t){return !!t&&listed(t.kind)&&!!t.listing&&t.screen==='list';}
// The NOW popup's rows: the list with the row stood on shown as a chosen row (the frame's `tint`, reversed
// where there is no color), or the part being read. Until the first answer, the NOW the panel already holds
// stands on its own row, so the popup is never empty.
function nowRows(t){
  const v=t.page?t.read:t.list;
  if(!v){
    const now=String(panelModel.now||'').trim();
    if(t.error)return [[{text:label('popup_unread','could not read'),dim:true}]];
    return now&&!t.page?[[{text:'    '+now,...(panelModel.styles?.loc_repo||{})}]]:[[{text:'⠏',dim:true}]];
  }
  if(t.page)return v.rows;
  const at=v.meta.findIndex(m=>m.key===t.sel);
  return at<0?v.rows:v.rows.map((row,i)=>i===at?row.map(part=>({...part,tint:true})):row);
}
function popupNow(){
  if(!popupShown())return null;
  const band=expanded?0:height(),kind=table?tableKind(table.kind):'question',width=panelView.popupWidth(cols,kind),inner=Math.max(0,width-4),room=panelView.popupRoom(rows,band);
  const names=sections(),current=Math.max(0,names.indexOf(table?table.kind:'QUESTIONS'));
  let title,body,view;
  if(table){
    // The keys, broken between their ` · ` pieces where the popup is narrow (never cut), and where the
    // table is longer than its rows, the reading's place first (`3/20 · …`).
    const all=tableRows(inner);
    let place,guide,space;
    const lay=()=>{
      place=all.length>Math.max(1,room-tableGuide(table.kind).length)?`${Math.min(all.length,table.scroll+1)}/${all.length}`:'';
      guide=wrapGuide([place,...tableGuide(table.kind)].filter(Boolean),inner,Math.max(1,room-1)).map(text=>[{text,dim:true}]);
      space=Math.max(1,room-guide.length);
    };
    const clamp=()=>{table.scroll=Math.max(0,Math.min(table.scroll,Math.max(0,all.length-space)));};
    lay();
    // The row stood on comes into view (the popup opens on the bottom one; a list moves with the keys) before the
    // place is said, so the place is the one the popup stands at (a second pass where the place's width moved a line).
    if(table.kind==='NOW'&&!table.page&&table.reveal&&table.list){
      const at=table.list.meta.findIndex(m=>m.key===table.sel);
      if(at>=0){for(let pass=0;pass<2;pass++){if(at<table.scroll)table.scroll=at;else if(at>=table.scroll+space)table.scroll=at-space+1;clamp();lay();}table.reveal=false;}
    }
    // The list screen's chosen row comes into view (the head above it too while it is within the first screen).
    if(statusListing(table)&&table.reveal&&table.listView){
      // (an item opened in place has several rows: all of them come into view when they fit, else its first stands at the top)
      const at=table.listView.at,span=table.listView.span||1;
      for(let pass=0;pass<2;pass++){if(at<table.scroll)table.scroll=at<space?0:at;else if(at+span>table.scroll+space)table.scroll=Math.min(at,at+span-space);clamp();lay();}
      table.reveal=false;
    }
    clamp();lay();clamp();
    table.space=space;
    title=tableTitle(table.kind);
    body=[...all.slice(table.scroll,table.scroll+space),...guide];
    view=panelView.popup({cols,rows,band,kind,title,body,sections:names,current,model:panelModel,configuration:displayConfiguration,cellWidth,background:terminalBackground});
    // The table's own press targets, moved to where its rows stand on the screen.
    const first=view.frame.top+1,left=view.frame.left+2;
    view.tableHits=(table.hits||[]).filter(h=>h.row>=table.scroll&&h.row<table.scroll+space)
      .map(h=>({row:first+h.row-table.scroll,left:left+h.left,right:left+Math.min(h.right,inner-1),action:h.action}));
  }else{
    const label=[{text:block.summary()?.label||'QUESTIONS',dim:true}];
    title=label;
    // A peer or work reading asked for from the popup (w) stands in it until the next key.
    if(actionLines.length)body=actionLines.slice(0,room);
    else if(block.question){const all=block.detail({budget:room+1,columns:inner});title=all[0]||label;body=all.slice(1);}
    else body=block.detail({budget:room,columns:inner});
    view=panelView.popup({cols,rows,band,kind,title,body,sections:names,current,model:panelModel,
      configuration:displayConfiguration,cellWidth,background:terminalBackground});
  }
  // OpenCode paints its own background: every popup row starts from the terminal's own look. The cell
  // each side of the frame on its own ground (`margin`) is first written plain and then painted: a wide
  // character it cuts in half leaves its other half blank in the terminal's own look, not the popup's.
  // (11-15: no control character reaches the terminal from a popup row: C0, DEL and C1 alike.)
  view.strings=view.rows.map(row=>'\x1b[0m'+row.map(part=>(part.margin?'\x1b[0m \x1b[D':'')+`\x1b[${cellAttr(part)}m${String(part.text).replace(/[\x00-\x1f\x7f-\x9f]/g,' ')}`).join('')+'\x1b[0m');
  if(table)return view;
  // The caret stands after the answer being typed (its row starts `› `), as it did in the panel.
  const edit=part=>String(part.text).startsWith('› ')&&String(part.text).length>2;
  const at=body.slice(0,view.body).findIndex(row=>Array.isArray(row)&&row.some(edit));
  if(at>=0){const parts=body[at],upto=parts.slice(0,parts.findIndex(edit)+1).map(part=>part.text).join('');
    view.cursor={row:view.frame.top+at+2,col:view.frame.left+3+Math.min(inner-1,cellWidth(upto))};}
  return view;
}
// The table's rows as painted parts: the backend sends runs `[text, style index]` and the styles; a run
// may carry its cells as Fleet measured them (`[text, style, cells]`, the status popup), which the popup
// lays the row out by.
function tableParts(result){return statusList.rowsOf(result);}
// 11-21: the status popup's list screen as entries of painted parts (`status-list.cjs` lays them out), or null.
function statusEntries(result){return statusList.entriesOf(result);}
// One reading at a time for the table standing; a reason to read again while one is under way is
// taken up when it ends (the content as it is then). The status popup's Fleet part is a reading of its
// own: the document lets the table's place go when it ends, so a popup opened again while Fleet is out
// stands at once; Fleet's lines are read one at a time and fill the popup standing when they arrive
// (its width and generation as asked), and are not kept when either changed.
let fleetAsking=false,fleetAgain=false,fleetWidth=0,fleetGeneration=0;
// A collection the held backend made is drawn again by a key for as long as it is fresher than this many seconds; an older
// one is drawn at once with the words that it is not this reading's, and read again behind them.
const HELD_READ_AGE=60;
function askTable(){
  const t=table;if(!t||!POPUP_KIND[t.kind])return;
  if(tableAsking){tableAgain=true;return;}
  tableAsking=true;tableAgain=false;
  const selected=selection,width=t.width,columns=Math.max(20,Math.min(200,width-4));
  const now=t.kind==='NOW';
  // The NOW list folds to the lines the popup has: its rows less the key line and the place (3).
  const room=now?Math.max(3,Math.min(200,panelView.popupRoom(rows,expanded?0:height())-3)):Math.max(6,Math.min(200,panelView.popupRoom(rows,expanded?0:height())-2));
  const status=listed(t.kind);
  const routeTable=t.kind==='ROUTE',pageAsked=!!t.page;
  // The routes popup's held backend draws a route unfolded or its page from the collection it already made while the
  // panel's route rows are the ones that collection was read for (`reuse`); else it collects (a first read, a changed key).
  const collect=!!t.collect;t.collect=false;
  const reuse=routeTable&&!collect&&t.readKey!==undefined&&t.readKey===(popupKeys.ROUTE??null);
  const ask=extra=>{
    const asked={kind:POPUP_KIND[t.kind],columns,open:now?t.sel||'':t.open||'',page:!!t.page,rows:room,
      ...(status?{cwd:process.cwd(),routes:statusRoutes(),status:statusHeld(),own:ownFolder(),...extra}:{}),...(reuse?{reuse:true}:{})};
    if(!(now||routeTable)||t.noWorker)return readAction(asked,'popup',true);
    // The popup's own backend answers. A read it did not answer is made the one-shot way (and the popup reads
    // that way from then on); a popup that is gone by then starts nothing.
    return heldRead(t.kind,asked,now?undefined:(reuse?4000:12000)).then(result=>{
      if(result)return result;
      t.noWorker=true;
      return table===t?readAction(asked,'popup',true):{ok:false,reason:'action-unavailable'};
    });
  };
  const stale=()=>table!==t||selected!==selection;
  const generation=statusGeneration;
  let loaded=false;
  const openAsked=t.open||'',keyAsked=popupKeys.ROUTE,atAsked=Date.now()/1000;
  // A first screen that said `could not read again` is being read again now.
  if(t.kind==='ROUTE'&&t.stale?.failed)t.stale={...t.stale,failed:false};
  ask(status?{part:'list'}:{}).then(result=>{
    if(stale())return;
    // Read at a width the popup no longer has: its rows are not this popup's; the read made for the new width stands.
    if(t.width!==width){tableAgain=true;return;}
    if(now){
      // A part that could not be asked for goes back to the list; a list that could not be read says so.
      if(!result?.ok){t.queued=[];if(t.page){t.page=false;t.scroll=t.listScroll||0;t.reveal=true;}else t.error=!t.list;queueDisplay();return;}
      nowAnswer(t,result);queueDisplay();
      // The keys that waited for this list: the first moves over it (and reads again if the list is folded).
      setImmediate(()=>nowDrain(t));
      return;
    }
    // The routes popup's table (not a route's page): what the person was reading stays in place when the rows are replaced.
    const route=t.kind==='ROUTE'&&result?.ok&&result.page!==true;
    if(!result?.ok){
      t.error=!t.loaded;
      // A first screen that stands because the reading did not come (or was cut at 12 s) says so; its rows stay.
      if(t.kind==='ROUTE'&&t.stale)t.stale={...t.stale,failed:true};
      // A table read before that could not be read again says so, as a first screen does (#1730 review).
      else if(t.kind==='ROUTE'&&t.loaded&&t.readAt)t.stale={at:t.readAt,failed:true};
      queueDisplay();return;
    }
    const anchor=route?routeAnchor(t):null;
    t.hits=Array.isArray(result.hits)?result.hits:[];t.loaded=true;t.error=false;loaded=true;
    if(t.kind==='ROUTE'){
      t.stale=null;t.readKey=keyAsked??null;t.readAt=Number(result.read_at)||atAsked;
      // A held collection (`reuse`) that this answer says was made over a minute ago is drawn with the words (its time is when it was
      // collected) and collected again behind them. Judged by the answer's own `read_at`: a read made afresh (no held backend, the
      // 12 s cut, a backend that died) is new, and is not read a second time.
      if(reuse&&Date.now()/1000-t.readAt>=HELD_READ_AGE){t.stale={at:t.readAt,failed:false};t.collect=true;tableAgain=true;}
    }
    if(status){
      // The first screen is `part: 'list'`: for a generated campaign page the page's list screen (what was chosen and opened is kept by
      // the lines' own ids; a line the page no longer has falls to the top) and none of its rows, the document read whole being the
      // other answer (`askDoc`); for any other document the rows as they always came, with Fleet's lines after.
      t.title=typeof result.title==='string'?result.title:'';
      t.listing=statusEntries(result);
      if(t.listing){
        t.rows=[];
        if(!t.screen)t.screen='list';
        // What the document read whole had was of an older page or width: it goes while the list is on screen (nothing draws it), and
        // while the whole document is on screen it stays until the new one comes (`askDoc`, below).
        if(t.screen==='list'&&(t.docGeneration!==generation||t.docWidth!==width))t.doc=null;
        t.docError=false;
        keepStatus(t,generation);
      }else{
        t.screen='';t.doc=tableParts(result);t.rows=[...(t.head||[]),...t.doc];
      }
    }else{
      t.rows=tableParts(result);
      // The pressed or tabbed route comes into view (when no scroll of the person's came after); else the route at the
      // top of what was read stays at the top.
      if(route&&!t.follow&&anchor){
        const rows=t.hits.filter(h=>h.action[0]==='route'&&h.action[1]===anchor.id).map(h=>h.row);
        if(rows.length)t.scroll=Math.max(0,Math.min(...rows)+anchor.offset);
      }else followOpen(t);
    }
    if(Array.isArray(result.routes))t.routes=result.routes;
    if(t.kind==='ROUTE'&&t.page&&pageAsked&&result.page!==true)t.page=false;
    // A page asked for while the table was being read opens at its top, not where the table's own scroll followed the row to.
    if(t.kind==='ROUTE'&&result.page===true&&t.pageTop){t.scroll=0;t.pageTop=false;}
    // Kept for the next opening: a table, or a route's page (the key says which; one place, the last read).
    if(route||t.kind==='ROUTE'&&pageAsked&&result.page===true)routePopup={sid,width,open:openAsked,key:keyAsked,page:result.page===true,rows:t.rows,hits:t.hits,routes:t.routes,at:t.readAt};
    queueDisplay();
  }).finally(()=>{
    tableAsking=false;
    if(tableAgain)askTable();
    // Fleet's lines stand above the document read whole: on a list screen neither are asked for until `full document ›` is opened
    // (and a page read again while that is on screen asks for its document and Fleet's lines again, as the popup always did).
    else if(t.kind==='STATUS'&&loaded&&!t.listing)askFleet();
    else if(status&&loaded&&t.screen==='full')askDoc();
  });
}
// What the status popup has read, kept for the next opening (the same width and page): its first screen at once, and what the document
// read whole and Fleet's lines already brought.
function keepStatus(t,generation){
  if(t.kind!=='STATUS')return;
  statusPopup={rows:t.rows,head:t.head,doc:t.doc,title:t.title,listing:t.listing,docGeneration:t.docGeneration,docWidth:t.docWidth,
    fleetGeneration:t.fleetGeneration,fleetWidth:t.fleetWidth,width:t.width,generation};
}
// The document read whole of a page with a list screen: the document's own answer (`part: 'doc'`, the rows and budget it always had),
// asked for when `full document ›` is opened and again when the page is read again while that is on screen; Fleet's lines follow it.
// One at a time like Fleet's; an answer for another width or page, a gone popup or another session is not kept.
let docAsking=false,docAgain=false,docWidth=0,docGeneration=0;
function askDoc(){
  const t=table;if(!t||!listed(t.kind)||!t.loaded||!t.listing)return;
  const width=t.width,generation=statusGeneration,selected=selection,kind=t.kind;
  if(docAsking){
    if(docWidth!==width||docGeneration!==generation)docAgain=true;
    return;
  }
  docAsking=true;docAgain=false;docWidth=width;docGeneration=generation;t.docError=false;
  const columns=Math.max(20,Math.min(200,width-4));
  const room=Math.max(6,Math.min(200,panelView.popupRoom(rows,expanded?0:height())-2));
  readAction({kind:POPUP_KIND[kind],columns,open:'',page:false,rows:room,cwd:process.cwd(),
    routes:statusRoutes(),status:statusHeld(),own:ownFolder(),part:'doc'},'popup',true).then(result=>{
    const cur=table;
    if(selected!==selection||!cur||cur.kind!==kind||!cur.listing)return;
    if(cur.width!==width||statusGeneration!==generation)return;
    if(!result?.ok){cur.docError=true;queueDisplay();return;}
    cur.doc=tableParts(result);cur.docGeneration=generation;cur.docWidth=width;cur.docError=false;
    keepStatus(cur,generation);
    queueDisplay();
  }).finally(()=>{
    docAsking=false;
    if(docAgain){docAgain=false;askDoc();return;}
    const cur=table;
    if(cur&&listed(cur.kind)&&cur.listing&&cur.screen==='full'&&docFresh(cur)&&!headFresh(cur))askFleet();
  });
}
function docFresh(t){return Array.isArray(t.doc)&&t.docGeneration===statusGeneration&&t.docWidth===t.width;}
function headFresh(t){return t.kind!=='STATUS'||t.head!==undefined&&t.fleetGeneration===statusGeneration&&t.fleetWidth===t.width;}
function askFleet(){
  const t=table;if(!t||t.kind!=='STATUS'||!t.loaded||!(t.listing||Array.isArray(t.doc)))return;   // (the backlog has no Fleet lines)
  const width=t.width,generation=statusGeneration;
  if(fleetAsking){
    // The one under way answers this popup too when it was asked at its width and generation; else once more after it.
    if(fleetWidth!==width||fleetGeneration!==generation)fleetAgain=true;
    return;
  }
  fleetAsking=true;fleetAgain=false;fleetWidth=width;fleetGeneration=generation;
  const columns=Math.max(20,Math.min(200,width-4));
  const room=Math.max(6,Math.min(200,panelView.popupRoom(rows,expanded?0:height())-2));
  readAction({kind:POPUP_KIND.STATUS,columns,open:t.open||'',page:false,rows:room,cwd:process.cwd(),
    routes:statusRoutes(),status:statusHeld(),part:'fleet'},'popup',true).then(more=>{
    const cur=table;
    // A failed or late Fleet part leaves the document as it stands; it is not kept, so the next opening reads again.
    if(!more?.ok||!cur||cur.kind!=='STATUS'||!cur.loaded||!(cur.listing||Array.isArray(cur.doc)))return;
    // Rows laid out at another width, or read before the value changed, are not this popup's.
    if(cur.width!==width||statusGeneration!==generation)return;
    const head=tableParts(more),before=(cur.head||[]).length;
    cur.head=head;cur.fleetGeneration=generation;cur.fleetWidth=width;
    if(!cur.listing)cur.rows=[...head,...cur.doc];
    // What the person was reading stays where it is when lines come in above it.
    if(cur.scroll>0&&!statusListing(cur))cur.scroll=Math.max(0,cur.scroll+head.length-before);
    keepStatus(cur,generation);
    queueDisplay();
  }).finally(()=>{fleetAsking=false;if(fleetAgain){fleetAgain=false;askFleet();}});
}
// The NOW popup's answer: the list (with its note per row) or the part being read. The row stood on stays
// where it is while another reading is still owed (the key was moved meanwhile); a first one starts on the
// bottom row (`start`, the current NOW).
function nowAnswer(t,result){
  const rowsOf=tableParts(result),title=typeof result.title==='string'?result.title:'';
  t.error=false;t.loaded=true;
  if(result.page===true){t.read={rows:rowsOf,title,width:t.width};return;}
  const meta=Array.isArray(result.meta)?result.meta.map(m=>({key:String(m?.key??''),kind:String(m?.kind??''),select:m?.select===true,
    read:m?.read===true,go:typeof m?.go==='string'?m.go:''})):[];
  t.list={rows:rowsOf,meta,title,folded:result.folded===true,width:t.width};
  if(!t.sel||(!meta.some(m=>m.key===t.sel)&&!tableAgain))t.sel=String(result.start||t.sel||'');
  t.reveal=true;
}
// ↑↓ (a row at a time) and PgUp/PgDn (a page of rows): the row stood on moves to the next one that has a press
// to give; a list folded to its room is read again with the new row open (its prompt unfolds).
function nowDrain(t){
  while(table===t&&!tableAsking&&!tableAgain&&t.queued?.length){t.queued.shift()();}
}
function moveSel(delta,byRow=false){
  const t=table;if(!t||t.page||!t.list)return;
  // A folded list that is being read again is not the one this key moves over (its row stood on is open there, the
  // list in hand has it folded): the key waits for the answer and moves over that, so a held key never steps over
  // the middle of a folded prompt. (`t.queued` is taken up, in order, as the answers arrive.)
  if(t.list.folded&&(tableAsking||tableAgain)){if((t.queued=t.queued||[]).length<200)t.queued.push(()=>moveSel(delta,byRow));return;}
  const meta=t.list.meta,at=meta.findIndex(m=>m.key===t.sel),dir=delta<0?-1:1;
  const find=(from,way)=>{for(let i=from;i>=0&&i<meta.length;i+=way)if(meta[i].select)return i;return -1;};
  let to;
  if(byRow){
    const target=Math.max(0,Math.min(meta.length-1,(at<0?(dir>0?-1:meta.length):at)+delta));
    to=find(target,dir);if(to<0)to=find(target,-dir);
  }else to=find(at<0?(dir>0?0:meta.length-1):at+dir,dir);
  if(to<0||to===at)return;
  t.sel=meta[to].key;t.reveal=true;
  if(t.list.folded)askTable();else queueDisplay();
}
// The route the top of the table's window stands in (the last route whose first row is at or above the scroll) and how
// many rows below that route's first row the window starts; read before the rows are replaced.
function routeAnchor(t){
  const first=new Map();
  for(const h of t.hits||[]){
    if(!Array.isArray(h.action)||h.action[0]!=='route')continue;
    if(!first.has(h.action[1])||h.row<first.get(h.action[1]))first.set(h.action[1],h.row);
  }
  let best=null;
  for(const [id,row] of first)if(row<=t.scroll&&(!best||row>best.row))best={id,row};
  return best?{id:best.id,offset:t.scroll-best.row}:null;
}
// ↑↓ Tab move the unfolded row; the reading's place follows it (the row at the top when it is out of view),
// once the table is read again with the row unfolded.
function followOpen(t){
  if(!t.follow||!t.open||!t.space)return;t.follow=false;
  const mine=h=>h.action[0]==='route'&&h.action[1]===t.open;
  const rows=(t.hits||[]).filter(mine).map(h=>h.row);if(!rows.length)return;
  const row=Math.min(...rows),space=t.space;
  if(row<t.scroll||row>=t.scroll+space)t.scroll=row;
}
// Opening a popup (a press on its section, Ctrl+], ←→ inside one). The keys are its own from now on.
// A route pressed on the panel (or entered from NOW) opens its page (`page`); a section name, Ctrl+] and ←→ open the table.
function openPopup(name,open='',page=false){
  epoch++;empty=false;actionLines=[];resetTableInput();
  heldStop(name);
  if(name==='QUESTIONS'){table=null;block.enter();queueDisplay();return;}
  const was=table,pageFirst=name==='ROUTE'&&page===true&&!!open;
  // The routes popup opens with the pressed route unfolded, else the most pressing one.
  if(name==='ROUTE'&&!open)open=String((rankedRoutes()[0]||{}).id||'');
  // A routes first screen (`stale`) is not carried over: the reopened popup picks its first screen again, so the
  // rows that are not a reading's own keep saying so. A route's page is not carried either (its rows are not a table's).
  const carry=was?.kind===name&&!(name==='ROUTE'&&(was.stale||was.page||pageFirst))&&!(listed(name)&&was.listing);
  table={kind:name,rows:carry?was.rows:[],hits:[],scroll:0,open,follow:!!open&&!pageFirst,page:pageFirst,pageTop:pageFirst,routes:[],loaded:carry&&was.loaded,width:panelView.popupWidth(cols,tableKind(name)),
    ...(name==='NOW'?{sel:'',list:null,read:null,reveal:true,listScroll:0,loaded:false}:{}),
    // 11-21: the status popup's list screen state: the line chosen, the groups and items opened (by their own words).
    // (a press on one of the panel's items opens it standing on that item: `open` is the item's key, taken if the list has it)
    ...(listed(name)?{listing:null,screen:'',sel:open,fold:new Set(),items:new Set(),reveal:true,listScroll:0}:{})};
  // The status popup's rows as read last, while no watch event came and the width is the same.
  const kept=name==='STATUS'&&statusPopup&&statusPopup.width===table.width&&statusPopup.generation===statusGeneration;
  if(kept)Object.assign(table,{rows:statusPopup.rows,head:statusPopup.head,doc:statusPopup.doc,title:statusPopup.title,loaded:true,
    listing:statusPopup.listing||null,screen:statusPopup.listing?'list':'',docGeneration:statusPopup.docGeneration,docWidth:statusPopup.docWidth,
    fleetGeneration:statusPopup.fleetGeneration,fleetWidth:statusPopup.fleetWidth});
  if(name==='ROUTE'&&!table.loaded)Object.assign(table,routeFirst(table)||{});
  if(block.focused)block.leave();
  if(name==='MESSAGES')blockAction('r');
  else if(name==='MEM'){
    // The popup opens on the record pressed (`popup:MEM:<n>`): its head is the first row on screen.
    const at=Number(open),starts=panelView.memoryBody({model:panelModel,columns:Math.max(1,table.width-4),cellWidth}).starts||[];
    if(Number.isInteger(at)&&at>0&&at<starts.length)table.scroll=starts[at];
  }
  else if(!kept)askTable();
  queueDisplay();
}
// What a routes popup shows before its own reading comes: what was read last for this session (same width, same
// route, table or page as it opens, the panel's route values unchanged since), else for a route's page the stage boxes
// the panel's values draw (`panelView.routePageFirst`), else the panel's route box at the popup's width; nothing
// where there is neither (the popup's spinner). Its rows are not this reading's, so it carries `stale`.
function routeFirst(t){
  const r=routePopup;
  if(r&&r.sid===sid&&r.width===t.width&&r.open===t.open&&r.key===popupKeys.ROUTE&&!!r.page===!!t.page)return {rows:r.rows,hits:r.hits,routes:r.routes,loaded:true,stale:{at:r.at,failed:false}};
  const args={model:panelModel,columns:Math.max(20,Math.min(200,t.width-4)),cellWidth,configuration:displayConfiguration,background:terminalBackground};
  // A route with no stage line (the owner alone, a unit's) has no stage boxes: its first screen stays the panel's box.
  const b=(t.page&&panelView.routePageFirst({...args,id:t.open}))||panelView.routesBody(args);
  return b?{rows:b.rows,hits:b.hits,routes:b.routes,loaded:true,stale:{at:Number(panelModel.route_read_at)||0,failed:false}}:null;
}
// The panel's route order for its popup (failed, running, waiting, finished): the first unfolded route.
function rankedRoutes(){
  const routes=Array.isArray(panelModel.routes)?panelModel.routes.filter(r=>r&&r.id):[];
  const rank=r=>r.kind==='dead'||r.kind==='done'&&r.outcome_key==='g_dead'?0:({working:1,blocked:2,idle:2,stale:3,done:4})[r.kind]??2;
  return routes.map((r,i)=>[r,i]).sort((a,b)=>rank(a[0])-rank(b[0])||a[1]-b[1]).map(([r])=>r);
}
function closePopup(){
  resetTableInput();
  table=null;heldStop();
  if(block.focused)block.leave();
  actionLines=[];queueDisplay();
}
// ←→: the next or previous section with something in it.
function moveSection(delta){
  const names=sections(),at=Math.max(0,names.indexOf(table?table.kind:'QUESTIONS'));
  openPopup(names[(at+delta+names.length)%names.length]);
}
// ↑↓ Tab: the routes popup unfolds the next route (Fleet's `▶`); the others read on a row at a time.
function moveRow(delta){
  const t=table;if(!t)return;
  // 11-21: on the status popup's list screen the keys move the chosen line (the document read whole scrolls as before).
  if(statusListing(t)){
    const to=statusList.neighbour(statusList.layout(t.listing,t),delta);
    if(to&&to!==t.sel){t.sel=to;t.reveal=true;queueDisplay();}
    return;
  }
  if(t.kind==='NOW'&&!t.page){moveSel(delta);return;}
  if(t.kind==='ROUTE'&&!t.page&&t.routes.length){
    const at=t.routes.indexOf(t.open),next=at<0?(delta>0?0:t.routes.length-1):Math.max(0,Math.min(t.routes.length-1,at+delta));
    if(t.routes[next]!==t.open){t.open=t.routes[next];t.follow=true;askTable();}return;
  }
  t.scroll=Math.max(0,t.scroll+delta);queueDisplay();
}
// Enter: the unfolded route's page (Fleet's route page) and back to the table.
function enterRow(){
  const t=table;if(!t)return;
  if(t.kind==='NOW'){enterNow(t);return;}
  if(listed(t.kind)&&t.listing){if(t.screen==='full')statusBack(t);else statusActivate(t,statusList.layout(t.listing,t).sel);return;}
  // (A popup opened on a route's page has not followed the unfolded row yet: back at the table it does.)
  if(t.kind==='ROUTE'&&t.open){t.page=!t.page;t.scroll=0;t.pageTop=t.page;if(!t.page)t.follow=true;askTable();}
}
// 11-21: Enter or a press on a line of the status popup's list screen. A heading or `all N ›` opens or closes its group, an item
// its whole text, and `full document ›` the document read whole (Fleet's lines are asked for then, not before); Esc or Enter there
// goes back to the list, on the line it left.
function statusActivate(t,key){
  const did=statusList.press(t,statusList.layout(t.listing,t),key);
  if(did==='full')statusFull(t);
  else if(did){t.reveal=true;queueDisplay();}
}
// The document read whole: its own answer, unless the one the popup has is of this page and width (asked for when the popup was here
// before, and kept), then Fleet's lines, unless those are the page's and width's too.
function statusFull(t){
  t.listScroll=t.scroll;t.screen='full';t.scroll=0;
  if(!docFresh(t))askDoc();
  else if(!headFresh(t))askFleet();
  queueDisplay();
}
function statusBack(t){t.screen='list';t.scroll=t.listScroll||0;t.reveal=true;queueDisplay();}
// Esc on the document read whole goes back to the list; true when that is all it did (else the popup closes).
function popupBack(){
  const t=table;
  if(!t||!listed(t.kind)||!t.listing||t.screen!=='full')return false;
  statusBack(t);return true;
}
// Enter in the NOW popup: a prompt or NOW row reads its part of the conversation here (Enter again is back at
// the list, on that row); a route, question or message row opens that section's popup. No key goes to the window.
function enterNow(t,queued=false){
  // Keys that wait for a list read again come first.
  if(!queued&&t.queued?.length){if(t.queued.length<200)t.queued.push(()=>enterNow(t,true));return;}
  if(t.page){
    t.page=false;t.scroll=t.listScroll||0;t.reveal=true;
    if(!t.list||t.list.width!==t.width)askTable();
    queueDisplay();return;
  }
  const at=t.list?t.list.meta.findIndex(m=>m.key===t.sel):-1,m=at>=0?t.list.meta[at]:null;
  if(!m)return;
  if(m.go){
    const [where,...rest]=m.go.split(':');
    if(where==='routes')openPopup('ROUTE',rest.join(':'),true);else if(where==='questions')openPopup('QUESTIONS');else if(where==='messages')openPopup('MESSAGES');
    return;
  }
  if(m.read){t.listScroll=t.scroll;t.page=true;t.read=null;t.scroll=0;askTable();queueDisplay();}
}
function covered(y,x){const r=popupRect;return !!r&&y>=r.top&&y<r.top+r.height&&x>=r.left&&x<r.left+r.width;}
// 11-15 (panel polish 3): while a popup stands, everything outside it (the native's rows, the panel,
// the strip) reads in one gray, no other color and no background, so the popup stands out; nothing
// in the panel turns meanwhile (the moving frame is a still gray line). The rows are the ones the
// scene writes anyway, each SGR in them replaced by the gray, so output under it keeps coming veiled
// at the frames it would have come at. Lifting or laying the veil writes every row whole once. Mono,
// eight colors and NO_COLOR veil nothing (the popup's frame is bright there instead).
let veiledFrame=null;
function veilTone(){return popupLayer&&!fleetView&&!relay?panelView.veil(displayConfiguration,{colors:panelModel.colors??256,noColor:panelModel.no_color===true,background:terminalBackground}):null;}
// A serialized row in the veil: every SGR is the gray (a concealed cell stays concealed).
function veilRow(row,tone){
  return String(row).replace(/\x1b\[([0-9;:]*)m/g,(_,list)=>{
    const p=list.split(';');let hidden=false;
    for(let i=0;i<p.length;i++){if(p[i]==='38'||p[i]==='48')i+=p[i+1]==='2'?4:2;else if(p[i]==='8')hidden=true;}
    return `\x1b[0;38;5;${tone}${hidden?';8':''}m`;
  });
}
// Before a frame: a popup that closed or moved gives its rows back to the scene (written whole again).
function popupBefore(){
  if(overlayBase!==last){overlayBase=last;overlayLast=[];}
  const l=popupLayer,rect=l?{top:l.top,left:l.left,width:l.width,height:l.height}:null;
  if(popupRect&&JSON.stringify(popupRect)!==JSON.stringify(rect))
    for(let y=popupRect.top;y<popupRect.top+popupRect.height;y++){last[y]=undefined;overlayLast[y]=undefined;}
  popupRect=rect;
}
// After the rows: the popup's rows that changed, and those over a row just written whole.
function popupOver(wrote,changed=true){
  const l=popupLayer;if(!l||!popupRect||l.top!==popupRect.top||l.left!==popupRect.left||l.height!==popupRect.height)return '';
  if(overlayBase!==last){overlayBase=last;overlayLast=[];}
  let out='';
  for(let i=0;i<l.strings.length;i++){
    const y=l.top+i;if(y>=rows)break;
    if(wrote.has(y)||changed&&overlayLast[y]!==l.strings[i]){out+=`\x1b[${y+1};${l.left+1}H`+l.strings[i];overlayLast[y]=l.strings[i];}
  }
  return out;
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
    let notice=-1,ownLine=-1;
    // Codex's footer row and OpenCode's bottom bar (rule 60): the row itself becomes the shell's line.
    if(!fleetView&&activated&&!relay&&harness!=='claude'){const own=nativeDisplay.footerRows(harness,nativeLines);for(const y of own)concealed.add(y);ownLine=own.at(-1)??-1;}
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
    // Laying or lifting the veil writes every row whole (the cells the beat would swap included).
    const tone=veilTone();
    if(tone!==veiledFrame){veiledFrame=tone;last=[];written=[];motionShown.clear();}
    popupBefore();const wrote=new Set();
    let text='\x1b[?2026h\x1b[?25l\x1b[?7l';
    const view=fleetView?.terminal||terminal;
    for(let y=0;y<view.rows;y++) {
      // The rest of a Codex footer row whose ID cells are concealed keeps the
      // row's own indent instead of starting after a blank gap.
      const masked=fleetView?0:footerMask.cells.get(y)||0,indent=masked?Math.min(masked,nativeLines[y].match(/^ */)[0].length):0;
      let line=y===notice||y===ownLine?footerLine(view.cols):concealed.has(y)?'\x1b[0m'+' '.repeat(view.cols):masked?
        '\x1b[0m'+' '.repeat(indent)+serialize(view.buffer.active.getLine(view.buffer.active.viewportY+y),view.cols,masked)+' '.repeat(masked-indent):
        serialize(view.buffer.active.getLine(view.buffer.active.viewportY+y),view.cols,0,pickMark(y));
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
        motion=shown&&!tone&&!!paintedMotion?.rows.has(y);
        if(motion&&same&&last[y]!==undefined&&last[y]!==line&&written[y]===panelKey[y]){text+=cellSpan(y,y,column);last[y]=line;}
        else if(motion&&last[y]!==line)motionWrote(y,y,column);
        written[y]=shown&&!tone?panelKey[y]:undefined;
      }
      if(tone)line=veilRow(line,tone);
      if(last[y]!==line){text+=`\x1b[${y+1};1H${line}`;last[y]=line;wrote.add(y);}
    }
    if(!fleetView&&!geometry().side&&(expanded||height()>0))for(let y=0;y<panelRows.length;y++){
      const row=rows-height()+y,first=expanded&&y===0&&!panelLines.length&&typeof nativeContext?.percent==='number'&&harness!=='claude';
      const own=first?'\x1b[0m\x1b[2K'+nativeDisplay.firstLine(nativeContext)[0].text:spun(y,panelRows[y]),cached=tone?veilRow(own,tone):own;
      if(last[row]!==cached){
        // A row of the moving frame whose other cells are unchanged: only its changed cells.
        const quiet=!first&&!tone&&!!paintedMotion?.rows.has(y)&&last[row]!==undefined&&written[row]===panelKey[y];
        text+=quiet?cellSpan(y,row,1):`\x1b[${row+1};1H${cached}`;
        if(!quiet&&!first&&!tone)motionWrote(y,row,1);
        if(!quiet)wrote.add(row);
        last[row]=cached;
      }
      written[row]=first||tone?undefined:panelKey[y];
    }
    else if(!fleetView&&!geometry().side&&height()>0&&footerLast!==displayFooter){text+=`\x1b[${rows};1H\x1b[0m\x1b[2K${displayFooter}`;footerLast=displayFooter;wrote.add(rows-1);}
    if(!fleetView)text+=popupOver(wrote);
    const b=view.buffer.active,m=view.modes;
    const owning=!!popupLayer&&!fleetView,cursor=owning?blockCursor:null;
    text+=`\x1b[${cursor?.row||Math.min(b.cursorY+1,view.rows)};${cursor?.col||Math.min(b.cursorX+1,view.cols)+(fleetView?0:geometry().nativeColumn)}H`;
    text+=`\x1b[?2004${m.bracketedPasteMode?'h':'l'}\x1b[?1${m.applicationCursorKeysMode?'h':'l'}`;
    text+='\x1b[?1000l\x1b[?1002l\x1b[?1003l';
    if(!fleetView&&height()>0)text+='\x1b[?1002h\x1b[?1006h';
    else if(m.mouseTrackingMode!=='none')text+=`\x1b[?${({x10:9,vt200:1000,drag:1002,any:1003})[m.mouseTrackingMode]}h\x1b[?1006h`;
    text+=`\x1b[?1004${m.sendFocusMode?'h':'l'}\x1b[?7h${(owning?!!blockCursor:cursorVisible)?'\x1b[?25h':''}\x1b[?2026l`;
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
const block=require('./block-input.cjs')({changed:queueDisplay,cellWidth,
  answer:async fields=>{const result=await source('answer',fields);queueProjection();return result;},
  action:key=>blockAction(key),
  // ←→ in the question popup move to the next section's popup, as in every other popup.
  section:delta=>setImmediate(()=>moveSection(delta))});
let actionLines=[];
function rebuildDisplay() {
  // The popup layer first: Fleet's screen or the plain relay closes it with the rest.
  popupLayer=relay||ended?null:popupNow();blockCursor=popupLayer?.cursor||null;
  if(relay||ended||fleetView)return;
  schedule();
  if(!height()){panelRows=[];panelKey=[];paintedCommon=[];paintedGroups=new Set();paintedSpins=new Set();paintedMotion=null;motionShown.clear();panelBlinks=false;panelAnimated=false;displayFooter='';schedule();return;}
  if(rebuilding){rebuildPending=true;return;}rebuilding=true;rebuildPending=false;
  const extra=deliveryNote();
  let lines,groups=[],spins=[],animated=false,motion=null,blinks=false;
  if(expanded){
    lines=panelContent();groups=viewGroups;spins=viewSpins;motion=viewMotion;blinks=viewBlinks;animated=viewAnimated;
    if(extra)lines=[...lines.slice(0,height()-1),[{text:extra,dim:true}]];
  }else{
    // Spec rule 49 (c): folded, the strip under the native area.
    // (11-15: the answer wait's blink is the strip's moving cells.)
    lines=panelContent();groups=viewGroups;spins=viewSpins;motion=viewMotion;animated=viewAnimated;
    if(extra)lines=[...lines.slice(0,height()-1),[{text:extra,dim:true}]];
  }
  displayLines(lines,height(),(painted,lengths,plain)=>{
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
  // `p` folds or opens the panel (11-15: the keyboard's way, named in every popup's keys); the popup
  // closes so the panel is seen.
  if(key==='p'){closePopup();foldByHand(expanded);applyLayout();return;}
  // `r` is the messages' popup.
  if(key==='r'&&table?.kind!=='MESSAGES'){openPopup('MESSAGES');return;}
  actionLines=[[{text:'⠏',dim:true}]];queueDisplay();
  const selected=selection,standing=table;
  const result=await source('action',{action:key});
  if(selected!==selection||!block.focused&&!(standing&&table===standing))return;
  actionLines=(result.lines||['?']).slice(0,panelView.popupRoom(rows,expanded?0:height())).map(text=>[{text,dim:true}]);
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
    fleetView=view;table=null;heldStop();block.leave();last=[];emit('\x1b[2J');
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
  pick=null;child.resize(nativeColumns(),nativeRows());terminal.resize(nativeColumns(),nativeRows());last=[];footerLast='\0';
  // A table popup is Fleet's table at the popup's width: read again at the new one. Rows laid out at the
  // old width (Fleet's lines above the status document) are not drawn at the new one: the document stands
  // alone until the new read.
  if(table&&POPUP_KIND[table.kind]&&table.width!==panelView.popupWidth(cols,tableKind(table.kind))){table.width=panelView.popupWidth(cols,tableKind(table.kind));
    if(listed(table.kind)&&table.loaded&&Array.isArray(table.doc)){table.head=[];if(!table.listing)table.rows=[...table.doc];}
    askTable();}
  emit('\x1b[0m\x1b[2J');rebuildDisplay();schedule();queueProjection();
}
// A redraw waits 25 ms to gather the events that come with it. A change seen only in a folder
// watched whole (a folder of records may change many times a second) redraws at most once a second:
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
// replaced, added or removed in it redraws, however many files it holds.
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
          if(row.targets.some(target=>statusTargets.has(target)))statusDirty=true;
          row.watch.close();watchers.delete(directory);queueProjection(folder(row.targets));return;
        }
        const hits=name?row.targets.filter(target=>target===changed||target.startsWith(changed+path.sep)
          ||target.endsWith(path.sep)&&changed.startsWith(target)):row.targets;
        // A change on a path the status value names: the value (and a standing status popup) is read again.
        if(hits.some(target=>statusTargets.has(target)))statusDirty=true;
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
// 11-15 (popup design, "while it stands"): a standing routes popup is read again when this session's
// route rows change, never on a timer.
let popupKeys={};
function refreshPopups(panel){
  const keys={NOW:JSON.stringify(panel.now||''),ROUTE:JSON.stringify([(Array.isArray(panel.routes)?panel.routes:[]).map(r=>[r.id,r.title,r.state,r.kind,r.stage,r.outcome,r.elapsed]),panel.route_label])};
  const changed=name=>popupKeys[name]!==undefined&&popupKeys[name]!==keys[name];
  const route=table&&table.kind==='ROUTE'&&changed('ROUTE');
  // A standing NOW list reads again when this session's NOW changed (the read it holds is still the one shown).
  const now=table&&table.kind==='NOW'&&!table.page&&table.list&&changed('NOW');
  // The key is in place before the reading it starts, so what it reads is kept under the key it stands for.
  popupKeys=keys;
  if(route||now)askTable();
}
async function projection() {
  if(!sid||ended||relay||succeeding)return;if(projecting){projectionPending=true;return;}
  projecting=true;projectionPending=false;const selected=selection;
  try{
    const asked=++clock;
    const data=await source('projection',{side:geometry().side,columns:openColumns(),popup_columns:panelView.popupWidth(cols,'question'),table_columns:panelView.popupWidth(cols,'table')-4,rows,state,expanded,native_context:nativeContext?.percent,native_badges:nativeMcp?{...(nativeContext||{}),mcp:nativeMcp}:nativeContext,retained_questions:block.references(),answer_busy:!!actionWorker||!!currentDelivery,upgrade_note:upgradeNoteNow(),
      ...(typeof displayValues.model==='string'&&displayValues.model?{native_model:displayValues.model}:{})});
    if(relay||selected!==selection)return;if(!data.ok){sourceError=data;return;}sourceError=undefined;
    turnRecord=harness==='claude'&&typeof data.turn_record==='string'&&path.isAbsolute(data.turn_record)?data.turn_record:'';
    // Claude's record is read here as well, by the host itself: one reading rule (a dialog
    // keeps the previous reading), and the file as it is now.
    if(turnRecord)readTurnRecord();
    else noteTurn(['idle','working','blocked'].includes(data.native_turn)?data.native_turn:'',asked);
    // Close the read-before-watch gap once when the input set changes.
    lastWatch=Array.isArray(data.watch)?data.watch.slice(0,64):[];
    if(observeFiles([...lastWatch,...statusTargets]))projectionPending=true;
    refreshPopups(data.panel||{});
    observeStatus(data.panel||{});
    const key=JSON.stringify([harness==='claude'?null:[data.footer,data.segments],data.lines,data.block,data.questions,data.help,data.native_footer,data.panel,cols,expanded,currentDelivery,heldBytes,state,shownState(),upgradeNoteNow()]);
    const freshDisplay=Object.fromEntries(Object.entries(data.native_display||{}).filter(([,value])=>value!=null));
    if(bootstrapModel&&freshDisplay.model){nativeSelected={};bootstrapModel=false;}
    displayValues={...displayValues,...freshDisplay,...nativeSelected};publishNativeDisplay();
    if(key===projectionKey)return;const oldHeight=height();projectionKey=key;nativeFooter=data.native_footer||null;footer=data.footer;footerSegments=data.segments||[];panelLines=data.lines;panelSections=data.block;panelModel={...data.panel,status_doc:statusDoc(),backlog_doc:backlogDoc(),answerable:data.questions?.some(item=>item.answerable===true&&(item.request_state||'open')==='open')};block.update(data.questions,data.help,data.block);if(height()!==oldHeight)applyLayout();else rebuildDisplay();
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
// 11-16 (the person: "맨 아래줄이 비면이 아니고 항상 비우라고 그냥"): the window's last line is always the shell's one line
// (below), in every harness; what a harness drew there is not shown. The line is found on the native screen
// (Claude: the rows its recorded status text fills; Codex: its footer row; OpenCode: its bottom bar) and
// written over in the painted copy only; the native screen keeps what it drew, and the shell reads from it.
function claudePanelPainted(){
  return harness==='claude'&&!relay&&!ended&&!fleetView&&activated&&!!sid&&!settings&&height()>0;
}
// The release this window runs, as the installed folder's name says (`v4.40.0`); none for a source checkout.
const releaseVersion=(()=>{const m=/[\\/]releases[\\/](v\d+(?:\.\d+)*[^\\/]*)[\\/]/.exec(myRelease||'');return m?m[1]:'';})();
// The line: two cells in, the working folder (home as `~`, cut from the front with `…` when it is wider than what
// the right text leaves), `hearting v<version>` at the right end, both dim; a newer installed release waiting (this
// window an old one) puts its words in the right text's place.
function footerLine(width){
  const room=Math.max(0,width-3),note=String(panelModel.footer_notice||'').replace(/[\x00-\x1f\x7f]/g,' ');
  const right=note||('hearting'+(releaseVersion?' '+releaseVersion:''));
  let rightText='',used=0;for(const c of right){const w=cellWidth(c);if(used+w>room)break;rightText+=c;used+=w;}
  let path='';
  const left=room-used-(used?2:0);
  if(left>=2){
    const chars=[...String(panelModel.footer_path||'').replace(/[\x00-\x1f\x7f]/g,' ')];
    let w=chars.reduce((n,c)=>n+cellWidth(c),0);
    if(w>left){let cut=[];w=1;for(let i=chars.length-1;i>=0;i--){const cw=cellWidth(chars[i]);if(w+cw>left)break;cut.unshift(chars[i]);w+=cw;}path='…'+cut.join('');}
    else path=chars.join('');
  }
  const pathWidth=[...path].reduce((n,c)=>n+cellWidth(c),0);
  return '\x1b[0m  \x1b[2m'+path+' '.repeat(Math.max(0,room-pathWidth-used))+rightText+'\x1b[0m'+' '.repeat(Math.max(0,width-2-Math.max(room,pathWidth+used)));
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
  table=null;heldStop();
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
  // native input loop. Untouched startup or an exact submitted frontier can be
  // corroborated by the screen/native-idle observation; so can keys the native
  // took without a turn (a local command's menu, an Esc, an erased draft: no
  // prompt callback follows them) once it has painted after them and the keys
  // have been quiet for KEY_QUIET_MS. A reading of that newer screen as idle
  // with an empty composer settles them; a draft or form it shows keeps them
  // unsettled, and a native that has not painted since its last key, or a
  // reading within the quiet time (an Enter whose turn has not called back
  // yet, a slow native's old screen), proves nothing (11-16: a window stayed
  // undeliverable for hours after `/model`). Keys since the composer was last
  // seen empty that cannot have typed into it (`typed` has not moved: a lone
  // Esc, cursor keys, a click, the terminal's own replies) owe no repaint: they
  // change nothing on screen, so waiting for one waited for the next prompt
  // (11-16, the same window after an Esc). OpenCode's keys wait for the next
  // submitted turn: its focus can leave the composer while the composer still
  // reads empty (a dialog over the transcript, its terminal pane).
  const inputSettled=nativeEpoch===0||(submitted>=0&&submitted===nativeEpoch);
  const untyped=typed<=Math.max(submitted,0);
  const painted=!inputSettled&&(screenRevision>keyRevision||untyped)&&harness!=='opencode'&&performance.now()-keyAt>=KEY_QUIET_MS;
  const loneEscape=keyTail==='\x1b'&&!paste;
  if(relay||paste||(keyTail&&!(loneEscape&&painted))||currentDelivery||!sid||(!inputSettled&&!painted)){
    empty=false;lastHold=currentDelivery?'busy':!sid||relay?'unobserved':'unsettled';return '';}
  // A lifecycle event that arrives meanwhile is newer than this observation.
  const before=epoch,selected=selection,revision=screenRevision,observed=sequence,frontier=nativeEpoch;
  const result=await source('readiness',{screen:screenSnapshot(),state});
  if(before!==epoch||selected!==selection||revision!==screenRevision||observed!==sequence||currentDelivery){empty=false;return 'moved';}
  empty=result.ok===true&&result.ready===true;
  lastHold=empty?'':HOLD_OF[result.reason]||'unobserved';
  if(empty&&!inputSettled){submitted=frontier;lastEnter=Math.max(lastEnter,frontier);if(loneEscape)keyTail='';}
  if(empty&&state!=='idle'){state='idle';queueDisplay();queueProjection();}
  return result.ok===true?'':'unavailable';
}
function observeInput(data) {
  // All keys invalidate an in-flight observation, but only native-bound bytes
  // can leave an unsubmitted native draft. Block/Fleet keys have no such bytes.
  epoch+=data.length;empty=false;const before=nativeEpoch;
  for(let offset=0;offset<data.length;offset++) {
    nativeEpoch++;
    const c=String.fromCharCode(data[offset]);
    // A string the terminal sends (an OSC or DCS reply) ends at BEL or ST. An
    // Escape that starts no ST, or another control byte, ends it unfinished (as
    // typed) and is read as the key it is: a paste's start, an Enter.
    if(/^\x1b[\]P_]/.test(keyTail)){
      if(keyTail.endsWith('\x1b')&&c!=='\\'){typed=nativeEpoch;keyTail='\x1b';}
      else if(c<' '&&c!=='\x07'&&c!=='\x1b'){typed=nativeEpoch;keyTail='';}
    }
    if(keyTail){
      // An Escape after a lone Escape: that one was its own key.
      if(keyTail==='\x1b'&&c==='\x1b'&&!paste)continue;
      keyTail+=c;
      const string=/^\x1b[\]P_]/.test(keyTail);
      // FocusIn/FocusOut report terminal focus, not a composer edit. Keep the
      // observation epoch above, including split reports; pasted bytes count.
      if(!paste&&(keyTail==='\x1b[I'||keyTail==='\x1b[O')){nativeEpoch-=keyTail.length;keyTail='';}
      else if(keyTail==='\x1b[200~'){paste=true;keyTail='';}else if(keyTail==='\x1b[201~'){paste=false;keyTail='';typed=nativeEpoch;}
      else if(string?c==='\x07'||keyTail.endsWith('\x1b\\'):keyTail.length>2&&/[\x40-\x7e]/.test(c)){if(!QUIET_KEY.test(keyTail))typed=nativeEpoch;keyTail='';}
      else if(keyTail.length>(string?512:64)){typed=nativeEpoch;keyTail='';}
    }
    else if(c==='\x1b')keyTail=c;
    else {typed=nativeEpoch;if(!paste&&(c==='\r'||c==='\n'))lastEnter=nativeEpoch;}
    if(paste)typed=nativeEpoch;
  }
  // A focus report alone is no key: it neither restarts the quiet time nor owes a repaint.
  if(nativeEpoch!==before||keyTail){keyRevision=screenRevision;keyAt=performance.now();}
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
  epoch++;empty=false;
  if(settings?.saving)return;
  if(action==='settings'){openSettings();return;}
  // Spec rule 49 (b), (c): the fold button folds the panel; a press on the folded strip opens it.
  if(action==='fold'||action==='unfold'){foldByHand(action==='fold');applyLayout();return;}
  if(action==='apply'){applySettings();return;}
  if(action==='cancel'){cancelSettings();return;}
  if(action==='defaults'&&settings){settings.value={...panelView.defaults};previewSettings();return;}
  // 11-15 (popup design E): a press on a section (its head or a row, beside or in the folded strip)
  // opens its popup over the window; nothing opens in place.
  if(action==='question'){openPopup('QUESTIONS');return;}
  if(action.startsWith('popup:')&&!settings){
    const [,name,...rest]=action.split(':');
    if(SECTIONS.includes(name)){openPopup(name,rest.join(':'),name==='ROUTE');return;}
  }
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
  // The wheel over the route box scrolls it in place (spec rule 23); elsewhere in the panel it does
  // nothing (the other sections read whole in their popups).
  if((code&0xc3)===64||(code&0xc3)===65){if(!release&&!settings&&routeArea&&row>=routeArea.top&&row<=routeArea.bottom)scrollRoutes((code&1)?1:-1);return true;}
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
    if(popupMouse(code,x,y,release))return;
    if(nativePick(code,x,y,release))return;
    if(panelMouse(code,x,y,release))return;
    if(terminal.modes.mouseTrackingMode==='none')return;
    // A left panel moves the native area right; native sees its own columns.
    const offset=geometry().nativeColumn;
    if(offset){if(x<=offset)return;raw=Buffer.from(`\x1b[<${code};${x-offset};${y}${release?'m':'M'}`,'latin1');}
    nativePointer(code,release,raw);
  }
});
// 11-15: while the popup stands it owns the pointer: a press outside it (on the native or the
// panel) closes it and goes nowhere else, `✕` closes it, the wheel over it reads its question on;
// nothing reaches a native that reads the mouse (Codex, OpenCode).
// The release of the press that closed it is its own too.
let popupPressed=false;
function popupMouse(code,x,y,release){
  if(release&&popupPressed){popupPressed=false;return true;}
  // The drag that follows the press which closed it is its own too, up to the release.
  if(popupPressed&&code>=32&&(code&0xc0)!==64)return true;
  if(!release&&code<32)popupPressed=false;
  const l=popupLayer;if(!l||!popupShown()||relay||fleetView)return false;
  const col=x-1,row=y-1,f=l.frame,inside=row>=f.top&&row<f.top+f.height&&col>=f.left&&col<f.left+f.width;
  if((code&0xc3)===64||(code&0xc3)===65){if(!release&&inside){const d=(code&1)?1:-1;if(table){table.scroll=Math.max(0,table.scroll+d);if(table.kind==='ROUTE')table.follow=false;queueDisplay();}else block.wheel(d);}return true;}
  if(release||code>=32||(code&3)!==0)return true;
  popupPressed=true;
  const hit=inside&&l.hits.find(h=>h.row===row&&col>=h.left&&col<=h.right);
  if(!inside||hit&&hit.action==='popup-close'){epoch++;closePopup();return true;}
  // A section's name on the bottom line opens its popup.
  if(hit&&hit.action.startsWith('popup:')){openPopup(hit.action.slice(6));return true;}
  // A table's row: a route unfolds (pressed again, its page).
  const target=table&&(l.tableHits||[]).find(h=>h.row===row&&col>=h.left&&col<=h.right);
  if(target){
    const [verb,second]=target.action;
    if(table.kind==='ROUTE'&&verb==='route'&&second){if(table.open===second)enterRow();else{table.open=second;table.page=false;table.follow=true;askTable();}}
    else if(verb==='status'&&second&&statusListing(table))statusActivate(table,second);
  }
  return true;
}
// 11-20: a native that does not read the mouse (`none`) leaves a drag over its area to the shell, which
// is what keeps the panel's presses reported to it: the drag selects text, drawn flipped while it goes,
// and the release copies it to the outer terminal's clipboard (OSC 52) from the shell's own copy of the
// native screen. Two presses on a cell select its word, three its line. A press that does not drag
// does what it did before (nothing). A native that reads the mouse gets every report, as before.
function nativeCell(x,y,clamp){
  const cx=x-1-geometry().nativeColumn,cy=y-1;
  if(clamp)return {x:Math.max(0,Math.min(terminal.cols-1,cx)),y:Math.max(0,Math.min(terminal.rows-1,cy))};
  return cx>=0&&cx<terminal.cols&&cy>=0&&cy<terminal.rows?{x:cx,y:cy}:null;
}
function clearPick(){if(pick){pick=null;queueDisplay();}}
function pickMark(y){return pick?.dragged&&!fleetView?picking.rowSpan(terminal,pick,y):null;}
function finishPick(cell){
  if(cell)pick.b=cell;
  pick.live=false;
  const single=pick.mode==='cell'&&pick.a.x===pick.b.x&&pick.a.y===pick.b.y;
  const sequence=pick.dragged&&!single?picking.copySequence(picking.textOf(terminal,pick)):'';
  if(!sequence)pick=null;else emit(sequence);
  queueDisplay();
}
function nativePick(code,x,y,release){
  if(fleetView||paste||relay||settings)return false;
  if(terminal.modes.mouseTrackingMode!=='none'){clearPick();return false;}
  if((code&0xc0)===64)return false;
  if(pick?.live){
    const cell=nativeCell(x,y,true);
    // A word or a line chosen by presses stays as chosen: the release copies it, motion changes nothing.
    if(pick.mode!=='cell'&&(release||code>=32)){if(release)finishPick(null);return true;}
    if(release){finishPick(cell);return true;}
    if(code>=32){if(cell.x!==pick.b.x||cell.y!==pick.b.y){pick.b=cell;pick.dragged=true;queueDisplay();}return true;}
    // A press while one is held: its release was lost, so that selection ends here and this one begins.
    finishPick(null);
  }
  if(release||code>=32||(code&3)!==0)return false;
  const cell=nativeCell(x,y,false);
  if(!cell)return false;
  const now=Date.now(),count=picking.clicks(pressed,cell.x,cell.y,now);
  pressed={...cell,time:now,count};
  const span=count===2?picking.wordAt(terminal,cell.x,cell.y):count===3?picking.lineAt(terminal,cell.y):null;
  pick={a:span?span.a:cell,b:span?span.b:cell,mode:span?(count===2?'word':'line'):'cell',live:true,dragged:!!span};
  if(span)queueDisplay();
  return true;
}
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
// The next input of any kind takes the selection's flipped cells back (a press that starts another
// makes its own); a key typed while one is being dragged ends it.
function userInput(data){if(data.length){epoch+=data.length;empty=false;if(pick&&(!pick.live||data[0]!==27||data[1]!==91||data[2]!==60))clearPick();mouseInput.feed(data);}}
function inputBytes(data) {
  if(!data.length)return;
  while(data.length){
    // Mouse-opened shell editors own their visible keys even while a peer's
    // paste is admitted. Only native-directed bytes enter the retained queue.
    if(settings&&!relay){settingsInput(data);epoch+=data.length;return;}
    // 11-15: while a popup stands it takes every key; nothing typed there reaches the native. A lone Esc
    // closes it; Ctrl+] closes it and the rest goes on. A popup not drawn (the host being handed over) owns none.
    if(table&&popupShown()){
      const rest=tableInput(data);epoch+=data.length-rest.length;
      data=rest;queueDisplay();continue;
    }
    if(block.focused&&popupShown()){
      actionLines=[];const rest=block.input(data);epoch+=data.length-rest.length;
      data=rest;queueDisplay();continue;
    }
    // Rule 5 (11-14): a sent answer returns focus to the native input. The lone Esc a person
    // presses right after it, to leave the popup as before, is not the native's interrupt; nor is
    // a lone Enter (11-15), which would submit what stands in the native input.
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
    const offset=data.indexOf(0x1d);
    if(offset<0){observeInput(data);child.write(data);return;}
    const prefix=data.subarray(0,offset);
    if(prefix.length){observeInput(prefix);child.write(prefix);}
    // Escape has already reached native. Ctrl+] cannot continue an Escape/CSI
    // prefix; keeping that lone byte pending would disable focus after a modal.
    if(keyTail==='\x1b'&&!paste)keyTail='';
    // 11-15: Ctrl+] always opens a popup over the window as it stands (open or folded panel, the
    // native at its width): the question popup while a question waits to be answered here, else the
    // first section with something in it. Every popup names its keys: ←→ the sections, `p` the
    // panel's fold (the keyboard's way to open a folded one), `f` Fleet. Ctrl+] closes it again.
    if(!paste&&!keyTail){
      const names=sections().filter(name=>name!=='QUESTIONS'&&name!=='NOW');
      openPopup(block.question||!names.length?'QUESTIONS':names[0]);
    }
    else{const literal=data.subarray(offset,offset+1);observeInput(literal);child.write(literal);}
    data=data.subarray(offset+1);
  }
}
// The table popups' keys (11-15): a lone Esc or Ctrl+] closes; ←→ the sections; ↑↓ Tab Shift+Tab
// the rows; Enter; PgUp/PgDn a page; `p` the panel's fold; `r` the messages; `w` the open work; `f`
// Fleet. Every other
// key, a paste and an Alt+letter are the popup's and do nothing. Returns what follows a closing key.
let tableTail='',tableEscape,tablePaste=false;
const PASTE_ESCAPE_MS=250;
// The popup's input state (a paste under way, a key's front, the Esc being waited on) is the popup's: it
// ends with it, closed or opened again, as the question editor's `leave()` ends its parser.
function resetTableInput(){clearTimeout(tableEscape);tableEscape=undefined;tableTail='';tablePaste=false;}
function tableInput(data){
  clearTimeout(tableEscape);
  let text=tableTail+data.toString('latin1');tableTail='';
  while(text.length){
    if(!table)return Buffer.from(text,'latin1');
    if(tablePaste){
      // A paste is the popup's and does nothing; its end mark may arrive split anywhere (`ESC` | `[201~` …
      // `ESC[20` | `1~`), so the front of it is kept for the next read. A Ctrl+] inside it still closes (a
      // paste that never ends must not hold the popup); the rest of that read is the paste's, dropped.
      const end=text.indexOf('\x1b[201~'),stop=text.indexOf('\x1d');
      if(stop>=0&&(end<0||stop<end)){closePopup();return Buffer.alloc(0);}
      if(end>=0){text=text.slice(end+6);tablePaste=false;continue;}
      let keep=0;for(let k=5;k>0&&!keep;k--)if(text.endsWith('\x1b[201~'.slice(0,k)))keep=k;
      if(keep){tableTail=text.slice(-keep);
        // A lone Esc left at the end of a paste is the person's Esc if no rest of the end mark follows it.
        if(keep===1)tableEscape=setTimeout(()=>{if(tableTail==='\x1b'){tableTail='';if(!popupBack()){closePopup();block.escaped();}}},PASTE_ESCAPE_MS);}
      text='';break;
    }
    if(text.startsWith('\x1b[200~')){tablePaste=true;text=text.slice(6);continue;}
    if(text[0]==='\x1d'){closePopup();return Buffer.from(text.slice(1),'latin1');}
    if(text[0]==='\x1b'){
      if(text.length===1){tableTail=text;tableEscape=setTimeout(()=>{if(tableTail==='\x1b'){tableTail='';if(!popupBack()){closePopup();block.escaped();}}},25);return Buffer.alloc(0);}
      // A key is `ESC [ … final` or `ESC O x` (an application-cursor arrow or keypad key: any byte after
      // the `O`, as the question popup reads it); a key split across reads waits for its rest.
      const csi=/^\x1b(?:\[[0-9;]*[\x40-\x7e]|O[\s\S])/.exec(text);
      if(!csi){
        if(/^\x1b(?:\[[0-9;]*|O)$/.test(text)){tableTail=text;return Buffer.alloc(0);}
        // ESC then anything but `[` or `O` (an arrow or function key) is no key of the popup's, as in the
        // question popup: Alt+letter, Shift/Alt+Enter, Esc then a control byte are dropped and it stands
        // (an Enter reaching the native would submit its text, a ^C interrupt its turn). Only a second Esc
        // closes it, and is dropped too. (A UTF-8 letter's further bytes are no key either.)
        // (11-21: a first Esc on the status popup's document read whole is the way back to its list; the second closes.)
        if(text[1]==='\x1b'){if(popupBack()){text=text.slice(1);continue;}closePopup();block.escaped();return Buffer.from(text.slice(2),'latin1');}
        text=text.slice(2);continue;
      }
      const key=csi[0].replace('\x1bO','\x1b[');text=text.slice(csi[0].length);
      if(key==='\x1b[A')moveRow(-1);else if(key==='\x1b[B')moveRow(1);
      else if(key==='\x1b[C')moveSection(1);else if(key==='\x1b[D')moveSection(-1);
      else if(key==='\x1b[Z')moveRow(-1);
      else if((key==='\x1b[5~'||key==='\x1b[6~')&&table.kind==='NOW'&&!table.page&&table.list)moveSel((key==='\x1b[5~'?-1:1)*Math.max(1,(table.space||2)-1),true);
      else if(key==='\x1b[5~'||key==='\x1b[6~'){table.scroll=Math.max(0,table.scroll+(key==='\x1b[5~'?-1:1)*Math.max(1,(table.space||2)-1));if(table.kind==='ROUTE')table.follow=false;}
      continue;
    }
    const c=text[0];text=text.slice(1);
    if(c==='\t')moveRow(1);
    else if(c==='\r'||c==='\n')enterRow();
    else if(c==='p'||c==='f'||c==='r')blockAction(c);
    // `w` (this repository's open work) reads in the question popup, as it always did.
    else if(c==='w'){openPopup('QUESTIONS');blockAction('w');}
  }
  return Buffer.alloc(0);
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
    panelModel={labels:panelModel.labels,styles:panelModel.styles,repo:panelModel.repo,git:panelModel.git,quota:panelModel.quota,usage:panelModel.usage,
      head:Array.isArray(panelModel.head)?panelModel.head.filter(part=>!/^\[/.test(String(part?.text))):undefined,
      ...(cleared?{selected:panelModel.selected,model_value:panelModel.model_value,effort_value:panelModel.effort_value}:{})};panelSections=undefined;panelLines=[];
    projectionKey='';footer='';footerSegments=[];
    // A popup reads one session's values: another selection closes it; the same session selected again
    // with no key between (an OpenCode plugin reload) keeps it, read again for the session as it is now. The popup's held
    // backend ends with it either way (its collection is not this session's).
    const again=unselected&&unselected.sid===req.sid&&unselected.nativeEpoch===nativeEpoch;
    heldStop();table=again?unselected.table:null;routeScroll=again?unselected.routeScroll:0;unselected=null;if(table)setImmediate(askTable);
    if(routePopup&&routePopup.sid!==req.sid)routePopup=null;
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
    unselected={sid:req.sid,nativeEpoch,table,routeScroll};
    selection++;sid='';state=harness==='opencode'?'idle':'unknown';nativeTurn='';turnMark=0;seenTurn=null;turnRecord='';formSeen=false;eventMark=++clock;empty=false;footer='';footerSegments=[];panelLines=[];panelSections=undefined;panelModel={};displayValues={};nativeContext=undefined;table=null;heldStop();block.update([]);
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
  currentDelivery=req.ref;state='submitting';eventMark=++clock;empty=false;epoch++;nativeEpoch++;lastEnter=typed=nativeEpoch;queueDisplay();queueProjection();
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
// 11-20: a clipboard write the native asks for goes on to the outer terminal (the shell holds the
// native's output, which would otherwise end it here); a read of the clipboard is never passed on.
terminal.parser.registerOscHandler(52,data=>{
  const sequence=picking.nativeCopy(data);
  if(sequence&&!relay&&!ended)emit(sequence);
  return true;
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
  terminal.onData(data=>child.write(data));terminal.onWriteParsed(()=>{screenRevision++;if(pick&&!pick.live)pick=null;schedule();});
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
    ended=true;heldStop();clearTimeout(frameTimer);clearTimeout(projectionTimer);clearTimeout(beatTimer);
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
// A question waiting on the panel is not such a moment: the new host draws it again and it is answered the same way.
function successionSafe(skipEmpty=false) {
  const quiet=!relay&&activated&&!ended&&running&&!succeeding&&state==='idle'&&!paste&&!keyTail&&!block.focused&&!settings&&!fleetView
    &&!currentDelivery&&!releasing&&heldBytes===0&&!table&&!actionWorker&&!dragging&&!settingsPaste
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
    sequence,selection,state,epoch,nativeEpoch,submitted,lastEnter,typed,cursorVisible,expanded,foldChoice,displayConfiguration,displaySequence,
    deliveries:[...deliveries].filter(([,value])=>value==='submitted'||value==='transport-submitted').slice(-128),
    displayValues,nativeSelected,nativeContext:nativeContext??null,nativeMcp:nativeMcp??null,
    // The host's own turn observation (what the panel shows as the state), so the successor shows the same
    // state at its first paint and goes on reading the native's turn record; its marks keep their order.
    turn:{nativeTurn,turnMark,eventMark,clock,seenTurn,turnRecord,formSeen},
    panel:{model:{...panelModel,...(panelModel.tidy_upgrade?{tidy_notice:''}:{})},lines:panelLines,sections:panelSections??null,footer,segments:footerSegments,nativeFooter},
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
    // An older host's seed has no `typed`: every byte it took may have typed.
    submitted=seed.submitted;lastEnter=seed.lastEnter;typed=Number.isSafeInteger(seed.typed)?seed.typed:nativeEpoch;cursorVisible=seed.cursorVisible;expanded=seed.expanded;empty=false;
    // An earlier release's open-by-hand choice also carried its width (`{folded:false,cols}`).
    const choice=seed.foldChoice;foldChoice=choice&&typeof choice==='object'&&typeof choice.folded==='boolean'?{folded:choice.folded}:null;
    displayConfiguration=seed.displayConfiguration;displaySequence=Number.isSafeInteger(seed.displaySequence)?seed.displaySequence:0;
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
