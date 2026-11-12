'use strict';
// Pure helpers for handing the display from one host generation to the next:
// the native VT is written out as the bytes that rebuild it, and read back the
// same way. Nothing here opens a process, a socket or a file.
const crypto=require('node:crypto');
const SCHEMA='hearting-shell-succession-v1';
const MOUSE={x10:9,vt200:1000,drag:1002,any:1003};

function color(c,bg) {
  const v=bg?c.getBgColor():c.getFgColor();
  if(bg?c.isBgRGB():c.isFgRGB())return `${bg?48:38};2;${v>>16};${v>>8&255};${v&255}`;
  if(bg?c.isBgPalette():c.isFgPalette())return `${bg?48:38};5;${v}`;
  return bg?'49':'39';
}
// The bytes the outer terminal receives for one VT row; the seed uses the same text.
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

// The scroll region lives in xterm's own buffer object, not in its public API.
// A missing or non-numeric field means the visible screen cannot be made the
// same, so capture throws and the old host keeps running.
function region(buffer,rows) {
  const top=buffer?.scrollTop,bottom=buffer?.scrollBottom;
  if(!Number.isInteger(top)||!Number.isInteger(bottom)||top<0||bottom<top||bottom>=rows)throw Error('scroll region unavailable');
  return {top,bottom};
}
function rowsOf(buffer,from,count,cols) {
  const lines=[],wrapped=[];
  for(let i=0;i<count;i++) {
    const line=buffer.getLine(from+i);
    lines.push(serialize(line,cols));if(line?.isWrapped)wrapped.push(i);
  }
  return {lines,wrapped};
}
function cursorOf(buffer,cols) {return {cursorX:Math.min(buffer.cursorX,cols-1),cursorY:buffer.cursorY};}

// scrollback=false carries the viewport only. A scrollback row that cannot be
// written out also falls back to that, and says so; a viewport row cannot.
function capture(terminal,{scrollback=true}={}) {
  const cols=terminal.cols,rows=terminal.rows,normal=terminal.buffer.normal,alternate=terminal.buffer.alternate;
  const active=terminal.buffer.active===alternate?'alternate':'normal',buffers=terminal._core?.buffers;
  const normalRegion=region(buffers?.normal,rows),alternateRegion=active==='alternate'?region(buffers?.alt,rows):null;
  let carried=scrollback,reason=scrollback?null:'seed-too-large',kept=normal.baseY,body;
  if(scrollback) {
    try{body=rowsOf(normal,0,normal.length,cols);}
    catch{carried=false;reason='capture-failed';}
  }
  if(!body) {
    kept=0;body=rowsOf(normal,normal.baseY,rows,cols);
  }
  const m=terminal.modes,modes={applicationCursorKeys:m.applicationCursorKeysMode,applicationKeypad:m.applicationKeypadMode,
    bracketedPaste:m.bracketedPasteMode,insert:m.insertMode,mouseTracking:m.mouseTrackingMode,origin:m.originMode,
    reverseWraparound:m.reverseWraparoundMode,sendFocus:m.sendFocusMode,wraparound:m.wraparoundMode};
  const seed={cols,rows,active,modes,scrollRegion:active==='alternate'?alternateRegion:normalRegion,
    normal:{...body,baseY:kept,...cursorOf(normal,cols),region:normalRegion}};
  if(active==='alternate')seed.alternate={...rowsOf(alternate,0,rows,cols),...cursorOf(alternate,cols),region:alternateRegion};
  return {terminal:seed,scrollback:{carried,reason,lines:kept}};
}

// Rebuild the VT from a captured seed. The terminal must be fresh. Resolves
// when the xterm parser has consumed every byte.
function restore(terminal,seed) {
  const {cols,rows,normal,modes}=seed;
  terminal.resize(cols,rows);
  const wrapped=new Set(normal.wrapped);let s='\x1b[?7h\x1b[0m';
  normal.lines.forEach((line,i)=>{s+=line;if(i<normal.lines.length-1&&!wrapped.has(i+1))s+='\r\n';});
  const place=(buffer,relative)=>`\x1b[${buffer.region.top+1};${buffer.region.bottom+1}r`
    +`\x1b[?6${relative?'h':'l'}\x1b[${buffer.cursorY+1-(relative?buffer.region.top:0)};${buffer.cursorX+1}H`;
  if(seed.active==='alternate') {
    s+=place(normal,false);
    const alternate=seed.alternate;
    s+='\x1b[?1049h\x1b[?7l'+alternate.lines.map((line,y)=>`\x1b[${y+1};1H${line}`).join('');
    s+=place(alternate,modes.origin===true);
  }else s+=place(normal,modes.origin===true);
  s+=`\x1b[?1${modes.applicationCursorKeys?'h':'l'}${modes.applicationKeypad?'\x1b=':'\x1b>'}`
    +`\x1b[?2004${modes.bracketedPaste?'h':'l'}\x1b[4${modes.insert?'h':'l'}`
    +'\x1b[?9l\x1b[?1000l\x1b[?1002l\x1b[?1003l'+(MOUSE[modes.mouseTracking]?`\x1b[?${MOUSE[modes.mouseTracking]}h`:'')
    +`\x1b[?45${modes.reverseWraparound?'h':'l'}\x1b[?1004${modes.sendFocus?'h':'l'}\x1b[?7${modes.wraparound?'h':'l'}`;
  return new Promise(resolve=>terminal.write(s,resolve));
}

// A digest of what a person could see or scroll to: every row's cells and
// attributes, the wrap flags, cursor, modes and scroll region of the active buffer.
function grid(terminal) {
  const hash=crypto.createHash('sha256'),cols=terminal.cols,normal=terminal.buffer.normal,alternate=terminal.buffer.alternate;
  const active=terminal.buffer.active===alternate?'alternate':'normal',buffers=terminal._core?.buffers;
  const update=(buffer,count)=>{for(let i=0;i<count;i++){const line=buffer.getLine(i);hash.update(serialize(line,cols));hash.update(line?.isWrapped?'\x01':'\x00');}};
  hash.update(JSON.stringify([cols,terminal.rows,normal.length,normal.baseY,active]));
  update(normal,normal.length);
  if(active==='alternate')update(alternate,alternate.length);
  const buffer=terminal.buffer.active,m=terminal.modes;
  hash.update(JSON.stringify([buffer.cursorX,buffer.cursorY,m.applicationCursorKeysMode,m.applicationKeypadMode,m.bracketedPasteMode,
    m.insertMode,m.mouseTrackingMode,m.originMode,m.reverseWraparoundMode,m.sendFocusMode,m.wraparoundMode,
    buffers?.normal?.scrollTop,buffers?.normal?.scrollBottom,active==='alternate'?[buffers?.alt?.scrollTop,buffers?.alt?.scrollBottom]:null]));
  return {digest:hash.digest('hex'),scrollback:normal.baseY,active,cursor:{x:buffer.cursorX,y:buffer.cursorY}};
}

const INTEGER=value=>Number.isSafeInteger(value);
const IDENTITY=value=>value&&INTEGER(value.pid)&&value.pid>1&&typeof value.start==='string'&&value.start.length>0;
const TEXT=list=>Array.isArray(list)&&list.every(item=>typeof item==='string');
// Why a seed cannot be taken, or null. A missing field or another schema makes
// the new host refuse; the old one then resumes.
function validate(seed) {
  if(!seed||typeof seed!=='object')return 'not-an-object';
  if(seed.schema!==SCHEMA)return 'schema';
  if(!['claude','codex','opencode'].includes(seed.harness)||typeof seed.sid!=='string')return 'identity';
  if(!IDENTITY(seed.native)||!IDENTITY(seed.rootNative))return 'native';
  for(const key of ['sequence','selection','epoch','nativeEpoch','submitted','lastEnter'])if(!INTEGER(seed[key]))return key;
  if(typeof seed.state!=='string')return 'state';
  if(typeof seed.cursorVisible!=='boolean'||typeof seed.expanded!=='boolean')return 'flags';
  if(!seed.displayConfiguration||typeof seed.displayConfiguration!=='object'||!Array.isArray(seed.panelOpen))return 'display';
  if(!Array.isArray(seed.deliveries)||seed.deliveries.length>128||!seed.deliveries.every(row=>Array.isArray(row)&&typeof row[0]==='string'
     &&['submitted','transport-submitted'].includes(row[1])))return 'deliveries';
  const t=seed.terminal;
  if(!t||!INTEGER(t.cols)||!INTEGER(t.rows)||t.cols<1||t.rows<1||!['normal','alternate'].includes(t.active)
     ||!t.modes||typeof t.modes!=='object'||!t.scrollRegion||!INTEGER(t.scrollRegion.top)||!INTEGER(t.scrollRegion.bottom))return 'terminal';
  const body=buffer=>buffer&&TEXT(buffer.lines)&&Array.isArray(buffer.wrapped)&&INTEGER(buffer.cursorX)&&INTEGER(buffer.cursorY)
    &&buffer.region&&INTEGER(buffer.region.top)&&INTEGER(buffer.region.bottom);
  if(!body(t.normal)||t.normal.lines.length<t.rows||(t.active==='alternate'&&(!body(t.alternate)||t.alternate.lines.length!==t.rows)))return 'buffers';
  if(!seed.scrollback||typeof seed.scrollback.carried!=='boolean'||!INTEGER(seed.scrollback.lines))return 'scrollback';
  const o=seed.outer;
  if(!o||!INTEGER(o.cols)||!INTEGER(o.rows)||!Array.isArray(o.last)||!Array.isArray(o.nativeLast)||typeof o.footerLast!=='string')return 'outer';
  return null;
}

module.exports={SCHEMA,color,serialize,capture,restore,grid,validate};
