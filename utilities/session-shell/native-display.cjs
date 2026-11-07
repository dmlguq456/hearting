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
module.exports={observe,firstLine};
