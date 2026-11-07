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
function titleSequence(title) {
  const text=[...String(title).replace(/[\x00-\x1f\x7f-\x9f]/g,'')].slice(0,1024).join('');
  return '\x1b]2;'+text+'\x07';
}
module.exports={observe,firstLine,hiddenFooterRows,blockHeight,titleSequence};
