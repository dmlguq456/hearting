'use strict';
const fs=require('node:fs');
// Only the visible native footer/sidebar. Never a readiness or identity claim.
function observe(harness, lines, sid) {
  const valid=n=>Number.isFinite(n)&&n>=0&&n<=100;
  if(harness==='claude') {
    const at=lines.findLastIndex(line=>line.includes('📁 ')&&line.includes('⎇ '));
    if(at>=0) {
      // The panel paints Claude's whole status area; this row owns no field.
      // Its exact callback values are still display evidence for the header.
      const status=lines.slice(at,at+3).map(s=>s.trim()).join(' · ');
      const selected=status.match(/context\s+[━─█░]+\s+(\d+(?:\.\d+)?)%\s*·\s*([^·]+?)\s*·\s*(?:5h|7d)\b/);
      const context=status.match(/context\s+[━─█░]+\s+(\d+(?:\.\d+)?)%/);
      const choice=selected?.[2].trim().match(/^(.+?)(?:\s+(low|medium|high|xhigh|max|ultra))?$/);
      const model=selected&&!/…|\.\.\.|^(?:\d+[hd]|weekly)\b/i.test(choice[1])?{model:choice[1],effort:choice[2]||''}:{};
      if(context&&valid(+context[1]))return {sid,percent:+context[1],...model};
    }
  }
  if(harness==='codex')for(const line of lines.slice(-4)) {
    const m=line.match(/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\s*·.*?Context\s+(\d+(?:\.\d+)?)% used(?:\s*·|\s*$)/i);
    if(m&&(!sid||m[1]===sid)&&valid(Number(m[2]))){
      const chosen=line.match(/Context\s+[\d.]+% used\s*·\s*([\w./:-]+)\s+(low|medium|high|xhigh|max|ultra)(?=\s*·|\s*$)/);
      return {percent:Number(m[2]),sid:m[1],...(chosen?{model:chosen[1],effort:chosen[2]}:{})};
    }
  }
  if(harness==='codex'){
    // Before a thread exists, native omits the ID/title fields from its managed
    // footer. The selected model is still present beside the native help row.
    for(let y=Math.max(0,lines.length-4);y+1<lines.length;y++){
      if(!/(?:^|\s)\? for shortcuts(?:\s|$)/.test(lines[y+1])||!/^ {2}\S/.test(lines[y]))continue;
      const seen=lines[y].match(/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/i)?.[0];
      if(seen&&sid&&seen!==sid)continue;
      const selected=lines[y].match(/(?:^ {2}| · )([\w./:-]+)\s+(low|medium|high|xhigh|max|ultra)(?=\s*·|\s*$)/);
      if(selected){const context=lines[y].match(/Context (\d+(?:\.\d+)?)% used/);return {model:selected[1],effort:selected[2],...(seen?{sid:seen}:{}),...(context&&valid(+context[1])?{percent:+context[1]}:{})};}
    }
    for(const line of lines.slice(-4)){
      const footer=line.match(/^ {2}([\w./:-]+)\s+(low|medium|high|xhigh|max|ultra)\s+·\s+(?:\/|~)/);
      if(footer)return {model:footer[1],effort:footer[2]};
    }
    const rows=lines.slice(0,12),at=rows.findIndex(s=>/OpenAI Codex\s*\(v[\d.]+\)/.test(s));
    if(!sid&&at>=0)for(const line of rows.slice(at+1,at+5)){
      const chosen=line.match(/^\s*[│ ]\s*model:\s+([\w./:-]+)(?:\s+(low|medium|high|xhigh|max|ultra))?(?:\s+\/model|\s*│?\s*$)/);
      if(chosen)return {model:chosen[1],effort:chosen[2]||''};
    }
  }
  if(harness==='claude'&&!sid){
    const at=lines.slice(0,10).findIndex(line=>/Claude Code v\d/.test(line));
    if(at>=0)for(const line of lines.slice(at+1,at+3)){
      const choice=line.match(/\s{2}([A-Za-z0-9][^·│]{0,96}?)\s+·\s+(?:API Usage Billing|Claude (?:Max|Pro)|Team|Enterprise)/);
      if(choice){const effort=lines.slice(-8).map(s=>s.match(/● (low|medium|high|max) · \/effort/)).find(Boolean);return {model:choice[1].trim(),effort:effort?.[1]||''};}
    }
  }
  if(harness==='opencode') {
    for(const line of lines.slice(-3)){
      const percent=line.match(/\s+\d[\d,.]*[KkMm]?\s+\((\d+(?:\.\d+)?)%\)\s+ctrl\+p commands\b/);
      if(percent&&valid(Number(percent[1])))return {percent:Number(percent[1]),sid,owned:['context']};
    }
    const width=Math.max(0,...lines.map(s=>s.length));
    for(let i=0;i+2<lines.length;i++) {
      const at=lines[i].search(/Context\s*$/);
      if(at<Math.max(20,width/2))continue;
      const tokens=lines[i+1].slice(at).trim(),m=lines[i+2].slice(at).trim().match(/^(\d+(?:\.\d+)?)% used$/);
      if(/^\d[\d,]* tokens$/.test(tokens)&&m&&valid(Number(m[1])))return {percent:Number(m[1]),sid,owned:['context']};
    }
  }
  return null;
}
// Rows whose text equals the statusline output Claude's callback recorded for
// this exact session. Only Claude's statusline area counts: the rows directly
// under the lowest input box (a rule, a `❯` prompt row, a rule). A screen
// without that box, such as the Ctrl+O transcript, conceals nothing, and only a
// complete contiguous match counts; the previous record covers the paint in flight.
function claudeStatusRows(lines,sid,record) {
  if(!sid||!record||record.session_id!==sid)return new Set();
  const rule=s=>/^─{8,}$/.test(String(s).trim());
  let bottom=-1;
  for(let y=lines.length-2;y>=1&&bottom<0;y--)if(rule(lines[y])){
    let top=y-1;while(top>=0&&!rule(lines[top]))top--;
    if(top>=0&&top<y-1&&/^❯/.test(String(lines[top+1])))bottom=y;
    else return new Set();
  }
  if(bottom<0)return new Set();
  // Cell spacing after wide glyphs and emoji presentation selectors are
  // terminal rendering, not text; every visible character must still match.
  const norm=s=>String(s).replace(/[\s\uFE0F]+/g,'');
  const same=(row,want)=>{
    const a=norm(row),b=norm(want);if(!a||!b)return false;if(a===b)return true;
    // Claude clips a line wider than its row with an ellipsis.
    return a.length>=12&&/…$/.test(a)&&b.startsWith(a.slice(0,-1).trimEnd());
  };
  const y=bottom+1;
  for(const want of [record.lines,record.previous]){
    if(!Array.isArray(want)||!want.length||want.length>6||!want.every(s=>typeof s==='string'&&norm(s)))continue;
    if(y+want.length<=lines.length&&want.every((line,i)=>same(lines[y+i],line)))return new Set(want.map((_l,i)=>y+i));
  }
  return new Set();
}
function firstLine(value) {return [{text:'context '+value.percent+'%',dim:true}];}
function hiddenFooterRows(harness,lines,sid,plan,painted=[],details=[]) {
  const found=[];
  if(harness!=='codex'||!sid||plan?.sid!==sid||!Array.isArray(plan.fields)
     ||!Array.isArray(plan.lines)||!plan.lines.length)return new Set();
  if(plan.presentation==='session-panel'){
    if(!Array.isArray(plan.detail_lines)||!plan.detail_lines.length
       ||JSON.stringify(plan.detail_lines)!==JSON.stringify(details)
       ||!plan.lines.every(line=>painted.some(row=>row.trim()===line.trim())))return new Set();
  }else if(!painted.some((_row,offset)=>plan.lines.every((line,i)=>line.trimEnd()===painted[offset+i]?.trimEnd())))return new Set();
  try {
    const s=fs.lstatSync(plan.config.path,{bigint:true});
    const generation=[s.dev,s.ino,s.size,s.mtimeNs,s.ctimeNs].map(String);
    if(!s.isFile()||s.uid!==BigInt(process.getuid())||s.mode&0o022n
       ||JSON.stringify(generation)!==JSON.stringify(plan.config.stamp))return new Set();
  }catch{return new Set();}
  const expected=plan.fields.filter(field=>Array.isArray(field)&&field.length);
  const exact=/^ +([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}) · .+$/;
  for(let y=Math.max(0,lines.length-4);y+1<lines.length;y++) {
    const match=lines[y].match(exact);
    const fields=lines[y].trim().split(' · ').map(text=>text.replace(/\s+/g,' '));
    if(match&&match[1]===sid&&fields.length===expected.length
       &&fields.every((value,i)=>expected[i].includes(value))
       &&/(?:^|\s)\? for shortcuts(?:\s|$)/.test(lines[y+1]))found.push(y);
  }
  return new Set(found.length===1?found:[]);
}
function layout({cols,rows,expanded=true,configuration={}}) {
  configuration=configuration&&typeof configuration==='object'?configuration:{};
  const width=configuration.side_width,minimum=configuration.side_min_columns;
  const side=expanded&&configuration.layout==='side'
    &&Number.isInteger(width)&&width>=32&&width<=80
    &&Number.isInteger(minimum)&&minimum>=80&&minimum<=1000
    &&cols>=minimum&&cols-width-2>=48&&rows>=8;
  return {side,panelCols:side?width:cols,nativeCols:side?cols-width-2:cols,
    panelColumn:side?cols-width:0,panelRow:side?0:null};
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
module.exports={layout,observe,firstLine,hiddenFooterRows,claudeStatusRows,blockHeight,titleSequence,publisher};
