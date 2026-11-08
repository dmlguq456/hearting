'use strict';
// Only the visible native footer/sidebar. Never a readiness or identity claim.
function observe(harness, lines, sid) {
  const valid=n=>Number.isFinite(n)&&n>=0&&n<=100;
  if(harness==='claude') {
    const at=lines.findLastIndex(line=>line.includes('📁 ')&&line.includes('⎇ '));
    if(at>=0) {
      const status=lines.slice(at,at+3).map(s=>s.trim()).join(' · ');
      const route=status.match(/⏳ route \d+(?: · (?!◑|⏵)[^·]+)*/)?.[0]?.trim();
      const wait=status.match(/◑ (\d+)\b/);
      if(route||wait)return {native_route:route,native_wait:wait?Number(wait[1]):undefined,sid};
    }
  }
  if(harness==='codex')for(const line of lines.slice(-4)) {
    const m=line.match(/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\s*·.*?Context\s+(\d+(?:\.\d+)?)% used\s*$/i);
    if(m&&(!sid||m[1]===sid)&&valid(Number(m[2])))return {percent:Number(m[2]),sid:m[1]};
  }
  if(harness==='opencode') {
    const width=Math.max(0,...lines.map(s=>s.length));
    for(let i=0;i+2<lines.length;i++) {
      const at=lines[i].search(/Context\s*$/);
      if(at<Math.max(20,width/2))continue;
      const tokens=lines[i+1].slice(at).trim(),m=lines[i+2].slice(at).trim().match(/^(\d+(?:\.\d+)?)% used$/);
      if(/^\d[\d,]* tokens$/.test(tokens)&&m&&valid(Number(m[1])))return {percent:Number(m[1]),sid};
    }
  }
  return null;
}
function firstLine(value) {return [{text:'context '+value.percent+'%',dim:true}];}
function hiddenFooterRows(harness,lines,sid) {
  const found=[];
  if(harness!=='codex')return new Set();
  const exact=/^ *([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}) · Context (?:100|[1-9]?[0-9])% used *$/;
  for(let y=Math.max(0,lines.length-4);y+1<lines.length;y++) {
    const match=lines[y].match(exact);
    if(match&&(!sid||match[1]===sid)&&/(?:^|\s)\? for shortcuts(?:\s|$)/.test(lines[y+1]))found.push(y);
  }
  return new Set(found.length===1?found:[]);
}
function blockHeight({harness,count,detail=0,limit,loaded=true}) {
  const summary=count||(!loaded&&harness!=='claude'?1:0);
  return Math.min(limit,Math.max(summary,detail+(detail&&summary?1:0)));
}
function publisher(directory, identity) {
  // One bounded observation in the existing private PTY directory. Neither
  // attachment identity nor its delivery/readiness record is changed by this.
  const fs=require('node:fs/promises'),path=require('node:path');
  const target=path.join(directory,'native-display.json');
  const temporary=target+'.'+process.pid+'.tmp';
  let pending,last,inflight,draining,writing=false;
  async function flush() {
    if(writing)return;
    writing=true;
    try {
      while(pending!==undefined) {
        const raw=pending;pending=undefined;inflight=raw;
        try {await fs.writeFile(temporary,raw,{mode:0o600});await fs.rename(temporary,target);last=raw;}
        catch {try{await fs.unlink(temporary);}catch{}}
        finally {inflight=undefined;}
      }
    } finally {writing=false;}
  }
  return values=>{
    const selected=identity();
    if(!selected.sid||!selected.native_pid)return;
    const fields={};
    for(const key of ['model','effort'])if(values[key]===null||typeof values[key]==='string')
      fields[key]=values[key]===null?null:values[key].slice(0,128).replace(/[\x00-\x1f\x7f]/g,'');
    if(values.context_percent===null||Number.isFinite(values.context_percent)&&values.context_percent>=0&&values.context_percent<=100)
      fields.context_percent=values.context_percent;
    const raw=JSON.stringify({schema:'hearting-native-display-v1',
      harness:selected.harness,sid:selected.sid,pid:selected.native_pid,start:selected.native_start,
      host_pid:selected.host_pid,host_start:selected.host_start,incarnation:selected.incarnation,
      values:fields});
    const newest=pending!==undefined?pending:(writing?inflight:last);
    if(raw===newest||Buffer.byteLength(raw)>4096)return draining;
    pending=raw;if(!writing)draining=flush();return draining;
  };
}
function titleSequence(title) {
  const text=[...String(title).replace(/[\x00-\x1f\x7f-\x9f]/g,'')].slice(0,1024).join('');
  return '\x1b]2;'+text+'\x07';
}
module.exports={observe,firstLine,hiddenFooterRows,blockHeight,titleSequence,publisher};
