'use strict';
// Geometry and paint only. The existing question controller owns drafts/effects.
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14,
  position:'right',theme:'terminal',shade:'none',route_line:'time'};
// Panel color schemes (spec rule 22: the reviewed set A, the question rose one
// step deeper). `terminal` is the terminal's own background with the shared
// palette; `light` declares a light terminal; `mono` drops hue and keeps weight,
// glyphs and words. Text and glyphs always carry the state, so no scheme makes
// color the only signal. A face is the background the panel stands on: `light`
// for the light theme, else the terminal's background when it is known
// (COLORFGBG or the terminal's own reply to a native's query; nothing is asked).
const FACES={
  dark:{light:234,medium:235,body:253,secondary:248,title:246,frame:244,route:245,messages:242,accent:174,
    working:150,idle:186,blocked:174,dead:210,done:150},
  light:{light:255,medium:254,body:237,secondary:241,title:241,frame:243,route:243,messages:245,accent:131,
    working:22,idle:58,blocked:131,dead:88,done:22},
};
// Fleet's 256-color hues on each face. The light face takes the reviewed deep
// counterparts (running 22, attention 58, failure 88, project 23, model 60)
// instead of the earlier strong substitutes; the dark face changes only the
// failure red, and only on a shade.
const HUES={dark:{217:210},light:{253:237,252:238,150:22,186:58,217:88,116:23,176:60,147:25,230:22,219:132,137:94}};
const LINES=new Set(['frame','route','messages']);
function face(theme,background){return theme==='light'?'light':background==='light'||background==='dark'?background:'';}
function appearance(rows,configuration={},{colors=256,noColor=false,background=''}={}){
  const cfg={...defaults,...configuration},theme=['terminal','light','mono'].includes(cfg.theme)?cfg.theme:'terminal';
  if(noColor)return rows;
  const known=face(theme,background),full=colors>=256;
  // A shade needs a known face: on an unknown background the panel keeps the terminal's own.
  const shade=['light','medium'].includes(cfg.shade)&&full&&known?cfg.shade:'';
  const F=FACES[known||'dark'];
  return rows.map(row=>row.map(part=>{
    let foreground=Number.isInteger(part.foreground)&&part.foreground>=0?part.foreground:null,dim=part.dim===true,bold=part.bold===true;
    const role=part.role||'';
    if(!full){
      // Eight colors: the light theme leaves the pale default white to the terminal.
      if(theme==='mono')foreground=null;
      else if(theme==='light'&&foreground===7)foreground=null;
      return {...part,foreground};
    }
    if(theme==='mono')foreground=null;
    else if(foreground!==null&&(known==='light'||shade))foreground=HUES[known||'dark'][foreground]??foreground;
    // The question's border, `◑` and `›` are rose; mono keeps them by weight.
    // (Box lines never turn bold: weight changes a line's stroke and, in some terminals, its width.)
    if(role==='accent'||role==='accent-line'){if(theme!=='mono')foreground=F.accent;else if(role==='accent')bold=true;dim=false;}
    // A state glyph takes its face's state color, at full strength.
    else if(role.startsWith('state:')&&theme!=='mono'&&(shade||known==='light')&&F[role.slice(6)]){foreground=F[role.slice(6)];dim=false;}
    // The session's own state keeps Fleet's session keys (running g_spin, idle
    // g_work_off, waiting g_blocked) on every face; the state colors above are
    // the route and stage marks' (running 150 / idle 186).
    else if(role==='session'&&theme!=='mono'&&(shade||known==='light'))dim=false;
    // Spec rule 22: on a known face each line takes its role's color with or
    // without a shade (frame 244/243, route 245/243, messages 242/245); an
    // unknown face keeps the terminal's own, mono keeps no hue.
    else if(LINES.has(role)&&known&&theme!=='mono'){foreground=F[role];dim=false;}
    if(shade){
      // On a shade nothing is dimmed on top: secondary text and lines take their own color.
      if(LINES.has(role)){foreground=F[role];dim=false;}
      else if(role==='title'){foreground=F.title;dim=false;}
      else if(foreground===null||dim||role==='secondary'){foreground=foreground===null&&!dim&&role!=='secondary'?F.body:F.secondary;dim=false;}
      return {...part,foreground,dim,bold,background:F[shade]};
    }
    // A declared light terminal: secondary text takes its color instead of a dim default.
    if(theme==='light'&&foreground===null&&dim){foreground=F.secondary;dim=false;}
    return {...part,foreground,dim,bold};
  }));
}
// A combining mark or joined emoji stays with the character it belongs to.
const SEGMENTER=typeof Intl==='object'&&Intl.Segmenter?new Intl.Segmenter(undefined,{granularity:'grapheme'}):null;
const JOINS=/[\p{M}\u200d\u1160-\u11ff\u{1f1e6}-\u{1f1ff}\u{1f3fb}-\u{1f3ff}]/u;
const graphemes=s=>SEGMENTER&&JOINS.test(s)?Array.from(SEGMENTER.segment(s),x=>x.segment):[...s];
// A message's key is its own: when, which way, who and what. The recent list moving
// or losing a neighbour never hands one message's key to another; only an identical
// record (same body) counts its order, from the newest, so an older copy leaving the
// front of the list leaves the kept copies' keys as they were.
function digest(s){let h=0x811c9dc5;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,0x01000193)>>>0;}return h.toString(36);}
function messageKeys(list){
  const seen=new Map();
  return list.map(m=>'message:'+m.at+':'+m.way+':'+digest(String(m.who??'')+'\n'+String(m.text??''))).reverse().map(key=>{
    const n=seen.get(key)||0;seen.set(key,n+1);
    return n?key+'.'+n:key;
  }).reverse();
}
const fields=['layout','position','side_width','narrow','auxiliary','bottom_rows','theme','shade','route_line','side_min_columns'];
const names=['layout','position','width','narrow','auxiliary','height','theme','shade','route_line','threshold'];
const OPTIONS={layout:['side','bottom'],position:['right','left'],narrow:['bottom','compact'],auxiliary:['summary','detail'],
  theme:['terminal','light','mono'],shade:['none','light','medium'],route_line:['time','model']};
const APPEARANCE_KEYS=['position','theme','shade'];
// The session's own state, as Fleet's session row judges and draws it
// (render_cells `_LIVE_GLYPH`/`_state_key`): a working session spins.
const SPIN='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
const LIVE={working:['spin','running','g_spin'],idle:['●','idle','g_work_off'],blocked:['◑','waiting','g_blocked']};
function change(value,key,step){
  const options=OPTIONS;
  if(options[key]){const list=options[key];return {...value,[key]:list[(list.indexOf(value[key])+step+list.length)%list.length]};}
  const bounds={side_width:[32,80,1],bottom_rows:[8,20,1],side_min_columns:[80,1000,10]},[lo,hi,n]=bounds[key];
  return {...value,[key]:Math.max(lo,Math.min(hi,value[key]+step*n))};
}
function render({frame=0,width,height,side,fill=side,model={},native={},state,sid,questions,sections,configuration,settings,routeDetail=false,routePage=0,routeScroll=0,attempts=[],expandedKeys=[],focused=false,cwd='',harness='',cellWidth,background=''}){
  width=Math.max(12,width);height=Math.max(4,height);
  const labels=model.labels||{},styles=model.styles||{},t=(k,f)=>labels[k]||f;
  const text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const line=(value,key)=>[text(value,key)];
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const measure=cellWidth|| (s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  function clip(parts,limit){let out=[],used=0;for(const part of parts){let s='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;s+=c;used+=w;}if(s)out.push({...part,text:s});if(used>=limit)break;}return {parts:out,used};}
  let inner=width-4;const content=[],hits=[],edge=[];
  // `groups` names the statusline groups a row paints (head, git, context, wait, jobs, route, quota).
  const contentGroups=[];
  function add(parts,action,groups){const row=content.length;if(action)hits.push({row:row+1,left:2,right:width-3,action});content.push(parts);contentGroups.push(groups||[]);}
  function wrap(parts,maximum=3,action,into){
    // `into` collects the rows for a caller that places them after measuring.
    const put=into?row=>into.push(row):row=>add(row,action);
    // Unbounded rows (a kept message body) carry a token wider than a row on to
    // the next rows by character cells; bounded rows keep their one-row `…`.
    const keep=maximum===Infinity;
    let current=[],used=0,count=0,space=false;
    for(const part of parts)for(const token of clean(part.text).match(/\s+|\S+/gu)||[]){
      if(/^\s+$/.test(token)){space=used>0;continue;}
      const size=measure(token),gap=space&&used?1:0;
      if(keep&&size>inner){
        if(gap&&used+1<inner){current.push({...part,text:' '});used++;}
        else if(gap){put(current);current=[];used=0;}
        space=false;let s='';
        for(const g of graphemes(token)){
          const w=measure(g);
          if(used&&used+w>inner){if(s)current.push({...part,text:s});put(current);current=[];used=0;s='';}
          s+=g;used+=w;
        }
        if(s)current.push({...part,text:s});
        continue;
      }
      if(used&&used+gap+size>inner){put(current);current=[];used=0;space=false;if(++count>=maximum)return;}
      if(space&&used){current.push({...part,text:' '});used++;}space=false;
      if(size>inner){const fit=clip([{...part,text:token}],inner-1);current.push(...fit.parts,{...part,text:'…'});used=inner;}
      else{current.push({...part,text:token});used+=size;}
    }
    if(current.length)put(current);
  }
  const routes=model.routes||[],first=routes[0];
  // Fleet's shared beat (render_cells `_blink_phase`): one second lit, one second
  // dark, from the same clock as the 100 ms spinner frame.
  const beat=Math.floor(Math.trunc(frame)/10)%2===0;
  const spinGlyph={text:SPIN[Math.abs(Math.trunc(frame))%SPIN.length],spin:true};
  // What runs here blinks or spins: the host keeps its beat only while one is
  // painted. A blinking part carries `beat`, a spinning one `spin`; the beat is
  // read from the rows this paint finally shows, so a route the height dropped
  // keeps nothing turning.
  const pulse=(value)=>({...text(value,beat?'g_work':'g_work_off'),beat:true});
  // Values the host could not refresh for a while are shown, not claimed:
  // nothing spins and the frame takes no route state (spec rule 12).
  const stale=model.stale===true;
  const live=r=>!stale&&r?.kind==='working';
  // A route's glyph: a working route spins on the frame like Fleet's route mark.
  const routeGlyph=r=>{const g=r.paint?.glyph||text('·');if(live(r))return {...g,...spinGlyph,role:'state:working'};return {...g,role:'state:'+(r.kind||'idle')};};
  const join=(groups,sep=' · ')=>groups.filter(g=>g&&g.length).flatMap((g,i)=>i?[text(sep),...g]:g);
  // Spec rule 21: an opened route carries what Fleet's ROUTES row unfolds, from
  // the values Fleet already read: kind and owner, each stage with its doer
  // (harness, model, effort, context, tool, sub-agents, time, round), how it
  // was run and checked, the next step, and why it stopped. Values are whole;
  // a long one goes on to the next rows.
  function routeItems(r){
    const items=[],stages=Array.isArray(r.stages)?r.stages:[],workers=Array.isArray(r.workers)?r.workers:[];
    const owner=String(r.owner||'').replace(/^owner · /,'');
    const kind=join([r.kind_text?[text('kind '),text(r.kind_text,'loc_repo')]:[],
      owner?[text('owner '),text(owner),...(r.profile?[text(' '+r.profile)]:[]),...(r.owner_ctx?[text(' '),text(r.owner_ctx,'loc_repo')]:[])]:[]]);
    if(kind.length)items.push(kind);
    // The feed carries one open tool and sub-agent count for the route (its
    // doer), not one per stage: like Fleet's ROUTES row (`└ >_ tool · +n agent`)
    // it stands once on its own row and never on a stage it may not belong to.
    const doing=join([r.tool?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:[],r.agents?[text('+'+r.agents+' agent')]:[]]);
    if(doing.length&&(stages.length||live(r)))items.push(doing);
    const used=new Set();
    stages.forEach((stage,i)=>{
      const mark=stage.mark||'';
      const markPart=mark==='●'?pulse('●'):mark==='✓'?text('✓','lvl_g'):mark==='✕'?text('✕','g_dead'):text('·');
      const name=stage.current?pulse(stage.name||'·'):text(stage.name||'·',mark==='✕'?'g_dead':'loc_repo');
      const mine=workers.filter(w=>w.stage===stage.name);mine.forEach(w=>used.add(w));
      const first=mine[0]||{};
      const doer=[stage.harness||first.harness,stage.model||first.model,stage.effort].filter(Boolean).join(' ');
      const verdict=stage.verdict&&stage.verdict!=='running'?[text(stage.verdict,/^(PASS|done)$/i.test(stage.verdict)?'lvl_g':/^(FAIL|BLOCKED)/i.test(stage.verdict)?'g_dead':'dim')]:[];
      const row=join([[markPart,text(' '),name,...(stage.round>1?[text(' R'+stage.round,stage.round>=3?'g_idle':'dim')]:[])],
        verdict,doer?[text(doer)]:[],first.ctx?[text(first.ctx,'loc_repo')]:[],stage.elapsed?[text(stage.elapsed)]:[],
        stage.when?[text(stage.when)]:[],stage.unit?[text(stage.unit)]:[],stage.gate?[text('gate ✓','lvl_g')]:[]]);
      items.push(row);
      // What the stage's box says beside them: its output, a rounds note, a quoted line.
      for(const [label,value] of [['out',stage.out],['',stage.note],['',stage.says]])if(value)items.push({sub:true,parts:[...(label?[text(label+' ','dim')]:[]),text(value)]});
      // More than one live worker on a stage: each its own row (Fleet's `└ stage · n`).
      mine.slice(1).forEach((w,n)=>items.push({sub:true,parts:[text('└ '+(stage.name||'·')+' · '+(n+2)+' '),text([w.harness,w.model].filter(Boolean).join(' ')),...(w.ctx?[text(' '),text(w.ctx,'loc_repo')]:[])]}));
    });
    for(const w of workers)if(!used.has(w))items.push([text('└ '+w.stage+' '),text([w.harness,w.model].filter(Boolean).join(' ')),...(w.ctx?[text(' '),text(w.ctx,'loc_repo')]:[])]);
    // An older feed row carries only the stage line (`plan ✓ › execute ● › review`).
    if(!stages.length&&r.flow)items.push([text(r.flow)]);
    // The band's head (age, legs, evidence summary) and the note right of it
    // (Fleet's `detail_after`); a narrow panel takes the note to the next rows.
    const how=join([r.age?[text(r.age)]:[],r.legs?[text(r.legs+' leg'+(r.legs===1?'':'s'))]:[],r.visibility?.summary?[text(r.visibility.summary)]:[],r.after?[text(r.after,'dim')]:[]]);
    if(how.length)items.push(how);
    if(r.visibility?.next)items.push([text('next ','dim'),text(r.visibility.next)]);
    // Why it stopped: the worker line Fleet reads, and where it resumes.
    if(!live(r)&&(r.reason||r.state_label))items.push([text(r.reason||r.state_label,r.kind==='dead'?'g_dead':'dim')]);
    if(r.resume)items.push([text('resume ','dim'),text(r.resume,'loc_repo')]);
    if(r.outcome)items.push([text(r.outcome,'lvl_g')]);
    return items;
  }
  // Each item laid out at the current width: the first row indented two cells,
  // its continuation four, nothing cut.
  function itemRows(items,lead='  '){
    const out=[];
    for(const item of items){
      // A stage's own notes sit two cells further in, under the stage they belong to.
      const parts=Array.isArray(item)?item:item.parts,first=item.sub?lead+'  ':lead,rest=first+'  ';
      const rows=[];inner-=rest.length;try{wrap(parts,Infinity,undefined,rows);}finally{inner+=rest.length;}
      rows.forEach((row,i)=>out.push([text(i?rest:first),...row]));
    }
    return out;
  }
  const asking=!!model.answerable;
  // UI attention is not native agent liveness. Only a confirmed route failure
  // may make this frame red; an unknown source/permission popup cannot.
  const failed=!stale&&first?.state==='dead',done=!stale&&first?.state==='done';
  const working=!stale&&first?.state==='working';
  const answer=!stale&&asking;
  const color=failed?'g_dead':answer?'lvl_y':working?'g_work':done?'lvl_g':'dim';
  const status=failed?(first.reason||t('unknown','Checking state')):answer?t('answer','Your answer needed'):working?t('working','Working'):done?t('ready','Result ready'):'';
  const cfg={...defaults,...configuration};
  let routePages=1,routeArea=null,routeOffset=0,routeMax=0,routeStep=1;
  // A message too long to open in place is read here, on the route detail's pages.
  const kept=Array.isArray(model.messages)?model.messages:[],keys=messageKeys(kept);
  const messageKey=m=>keys[kept.indexOf(m)],messageParts=m=>[text(m.way+' '),text(m.who+' ','loc_repo'),text(m.text)];
  const reading=typeof routeDetail==='string'&&routeDetail.startsWith('message:')?kept[keys.indexOf(routeDetail)]:undefined;
  if(typeof routeDetail==='string'&&!reading)routeDetail=false;
  if(settings){
    add(line(t('settings_title','Hearting shell settings'),'loc_repo'));
    const start=Math.max(0,settings.index-Math.max(0,height-7));
    for(let i=start;i<fields.length&&content.length<height-4;i++){
      const key=fields[i],value=settings.value[key],v=typeof value==='string'?t((APPEARANCE_KEYS.includes(key)?key+'_':'')+value,value):value;
      const prefix=`${i===settings.index?'›':' '} ${t(names[i],names[i])}  `,left=2+measure(prefix);
      hits.push({row:content.length+1,left,right:left+1,action:'setting:'+i+':down'});
      hits.push({row:content.length+1,left:left+measure('‹ '+v+' '),right:left+measure('‹ '+v+' ›'),action:'setting:'+i+':up'});
      add(line(prefix+`‹ ${v} ›`,i===settings.index?'loc_repo':'dim'),'setting:'+i+':select');
    }
    const buttons=['apply','cancel','defaults'];let offset=2;
    const buttonLabels=buttons.map(key=>t(key,key));
    for(let i=0;i<buttons.length;i++){
      hits.push({row:content.length+1,left:offset,right:offset+measure(buttonLabels[i])-1,action:buttons[i]});
      offset+=measure(buttonLabels[i]+'  ·  ');
    }
    add(line(buttonLabels.join('  ·  '),'loc_repo'));
    if(settings.error)add(line(t('save_failed','Could not save'),'lvl_r'));
    else add(line('Tab ↑↓ · ←/→ · Enter · Esc'));
  }else if(routeDetail){
    add(line((reading?'messages':t('routes','route'))+' · '+t('back','Back'),'loc_repo'),'route');
    const start=content.length;
    if(reading)wrap(messageParts(reading),Infinity);
    else for(const route of routes){
      // The route's whole detail, as it opens in place, then its evidence lines.
      wrap([routeGlyph(route),text(' '),text(route.title||t('checking','Checking'),'loc_repo')],Infinity);
      for(const row of itemRows(routeItems(route)))add(row);
      if(route.stage_more)add(line('+'+route.stage_more+' · Fleet'));
      for(const [label,value,tone] of route.visibility?.lines||[]){
        wrap(line(label+': '+value,({good:'lvl_g',bad:'lvl_r',mixed:'lvl_y',info:'loc_repo',dim:'dim'})[tone]||'dim'),20);
      }
      // The owner's own output, as Fleet's route page shows it under the stages.
      const raw=(Array.isArray(attempts)?attempts:[]).find(a=>a&&a.route===route.id&&Array.isArray(a.lines)&&a.lines.length);
      if(raw){wrap([text('owner raw','loc_repo'),text(' · attempt-view '+(raw.att||'·'))],Infinity);for(const value of raw.lines)wrap(line(value),Infinity);}
    }
    const detail=content.splice(start),budget=Math.max(1,height-3-start);
    routePages=Math.max(1,Math.ceil(detail.length/budget));
    const page=Math.max(0,Math.min(routePages-1,routePage));
    const counter=` ‹ ${page+1}/${routePages} › `;
    hits.push({row:content.length+1,left:2,right:4,action:'route-prev'});
    hits.push({row:content.length+1,left:5,right:2+measure(counter),action:'route-next'});
    add(line(counter+' · PgUp/PgDn'));
    for(const row of detail.slice(page*budget,(page+1)*budget))add(row);
  }else{
    // Spec C3 (2026-11-11): session, model/context, the answer wait and the task
    // first; then one row per route and per message, each opened in place by a
    // press; 5h/7d last. Groups are divided by `│`; sections by plain rules.
    const painted=v=>Array.isArray(v)?v:[];
    const size=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
    const pad=n=>text(' '.repeat(Math.max(0,n)));
    const cut=(parts,limit)=>{const all=size(parts);if(all<=limit)return parts;const fit=clip(parts,Math.max(0,limit-1));return [...fit.parts,{...(fit.parts.at(-1)||text('')),text:'…'}];};
    const joined=(groups,sep=' │ ')=>groups.filter(g=>g.length).flatMap((g,i)=>i?[text(sep),...g]:g);
    // A narrow panel gives up the lead rule, then whole words from the end of the label:
    // `route N` shows whole or not at all, never as a shorter number.
    const fitted=label=>{
      if(size([text('── '),...label,text(' ')])<=inner)return [text('── '),...label,text(' ')];
      const all=label.map(p=>clean(p.text)).join(''),words=all.split(' ');
      if(words[0]==='route'&&words.length>1)words.splice(0,2,words[0]+' '+words[1]);
      let keep='';
      for(const word of words){const next=keep?keep+' '+word:word;if(measure(next)>inner)break;keep=next;}
      const out=[];let left=keep.length;
      for(const p of label){const t=clean(p.text).slice(0,left);if(t)out.push({...p,text:t});left-=t.length;}
      return keep&&measure(keep)<inner?[...out,text(' ')]:out;
    };
    const rule=(label)=>{const head=label.length?fitted(label):[];return [...head,text('─'.repeat(Math.max(0,inner-size(head))))];};
    const open=new Set(expandedKeys||[]);
    const routeLine=['time','model'].includes(cfg.route_line)?cfg.route_line:'time';
    // The session's own model: the native selection when it is shown, else Fleet's.
    const nativeModel=native.model&&(native.model!==model.model_value||!painted(model.selected).length);
    const selected=nativeModel?[text(native.model,'loc_repo'),...(native.effort?[text(' '+native.effort)]:[])]:
      painted(model.selected).length?model.selected:line(t('checking','Checking'));
    // Spec rule 15: the repo row carries the number and project only; Git takes the row below.
    let head=painted(model.head),git=painted(model.git);
    if(!Array.isArray(model.git)){const at=head.findIndex(p=>/^ \(/.test(String(p.text)));if(at>=0){git=[{...head[at],text:String(head[at].text).slice(1)},...head.slice(at+1)];head=head.slice(0,at);}}
    const jobs=painted(model.jobs),work=painted(model.work),quota=painted(model.quota),mcp=painted(model.mcp);
    // OpenCode's MCP count stays beside the model and context (its native sidebar is hidden).
    const contextParts=mcp.length?joined([painted(model.context),mcp]):painted(model.context);
    const waitParts=model.wait?[text(model.wait,'g_blocked')]:[];
    const title=model.session_title||model.title||'';
    const tidy=typeof model.tidy_notice==='string'?clean(model.tidy_notice).trim():'';
    const top=[],spacers=new Set(),optional=new Set(),gitRows=new Set(),jobRows=new Set(),topGroups=new Map();
    const mark=(from,...names)=>{for(let i=from;i<top.length;i++)topGroups.set(i,[...(topGroups.get(i)||[]),...names]);};
    // The Git row is cut within its own row; short of rows, it is the first to go.
    const putGit=()=>{if(git.length){gitRows.add(top.length);top.push(cut(git,inner));mark(top.length-1,'git');}};
    const put=(rows,parts,max)=>{const out=[];wrap(parts,max,undefined,out);rows.push(...out);};
    if(side){
      if(head.length){put(top,head,2);mark(0,'head');}
      putGit();
      if(head.length||git.length){spacers.add(top.length);top.push([]);}
      top.push(selected);
      if(contextParts.length){top.push(contextParts);mark(top.length-1,'context');}
      if(jobs.length){const from=top.length;put(top,jobs,2);mark(from,'jobs');for(let i=from;i<top.length;i++){optional.add(i);jobRows.add(i);}}
      if(work.length){optional.add(top.length);top.push(cut(work,inner));}
    }else{
      const first=joined([head,selected,contextParts]);
      if(size(first)<=inner){top.push(first);mark(0,'head',...(contextParts.length?['context']:[]));}
      else{put(top,joined([head,selected]),1);mark(0,'head');const from=top.length;put(top,contextParts,1);mark(from,'context');}
      // Running jobs join that row when they fit; otherwise they take a row (below Git) that can give way.
      const joinJobs=jobs.length&&size(top.at(-1))+3+size(jobs)<=inner;
      if(joinJobs){top[top.length-1]=joined([top.at(-1),jobs]);mark(top.length-1,'jobs');}
      putGit();
      if(jobs.length&&!joinJobs){optional.add(top.length);jobRows.add(top.length);top.push(cut(jobs,inner));mark(top.length-1,'jobs');}
      // The repo's open work gives way with the running jobs.
      if(work.length){optional.add(top.length);top.push(cut(work,inner));}
    }
    if(model.warning)top.push(line(model.warning,'lvl_r'));
    if(tidy&&!asking){
      const rows=[];wrap(line('⚠ '+tidy,'lvl_y'),side?4:2,undefined,rows);
      const kept=rows.slice(0,side?3:1);
      if(rows.length>kept.length){const last=kept.length-1,fit=clip(kept[last],inner-1);kept[last]=[...fit.parts,text('…','lvl_y')];}
      top.push(...kept);
    }
    if(side){
      if(waitParts.length||title){spacers.add(top.length);top.push([]);}
      if(waitParts.length){top.push(waitParts);mark(top.length-1,'wait');}
      if(title)put(top,line(title,'loc_repo'),2);
    }else if(waitParts.length||title){top.push(cut(joined([waitParts,title?line(title,'loc_repo'):[]]),inner));if(waitParts.length)mark(top.length-1,'wait');}
    const rows=height-2;
    // Routes: the current ones the statusline counts, one row each.
    const shown=routes.slice(0,Math.max(0,model.route_count??routes.length));
    // The whole count is the producer's. Spec rule 23: every current route gets a
    // row; a count the producer did not send rows for shows only in the box's lower border.
    const total=Number.isInteger(model.route_total)&&model.route_total>=shown.length?model.route_total:shown.length;
    const listed=shown,unsent=shown.length?total-listed.length:0;
    const elapsedWidth=Math.max(0,...listed.map(r=>measure(r.elapsed||'')));
    const columnWidth=(pick)=>Math.max(0,...listed.map(r=>size(pick(r))));
    const owner=r=>{const p=r.paint||{};return [p.harness,p.model_text&&text(' '),p.model_text,p.effort&&text(' '),p.effort].filter(Boolean);};
    const third=r=>r.tool?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:r.step?[text(r.step)]:[];
    function folded(r){
      const p=r.paint||{},lead=[routeGlyph(r),text(' ')];
      if(!side){
        const ow=columnWidth(owner),gw=columnWidth(x=>x.paint?.gauge5||[]),tw3=columnWidth(third);
        // The title gives way first, but keeps half of what the tool or stage column would take.
        const avail=inner-(2+3+ow+3+gw+3+1+elapsedWidth),titleWidth=Math.min(columnWidth(x=>line(x.title||'')),Math.max(6,avail-tw3,Math.floor(avail/2)));
        const name=cut(line(r.title||'·','loc_repo'),titleWidth);
        const parts=[...lead,...name,pad(titleWidth-size(name)),text(' │ '),...owner(r),pad(ow-size(owner(r))),text(' │ '),
          ...(p.gauge5||[]),pad(gw-size(p.gauge5||[])),text(' │ '),...third(r)];
        // The elapsed time keeps the right edge; the tool or stage gives way first.
        const end=r.elapsed?[text(' '),text(r.elapsed)]:[],left=cut(parts,inner-size(end));
        return [...left,pad(inner-size(left)-size(end)),...end];
      }
      // As in second(): the title gives way first, and each fixed value
      // (harness, model, context, time) shows whole or not at all.
      const H=p.harness?[p.harness]:[],M=p.model_text?[p.model_text]:[],C=p.ctx?[p.ctx]:[];
      const E=elapsedWidth?[text(' '.repeat(elapsedWidth-measure(r.elapsed||''))+(r.elapsed||''))]:[];
      const drops=routeLine==='model'?[[H,M,C],[H,M],[H],[]]:[[H,C,E],[H,E],[E],[]];
      const join=values=>values.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);
      const right=drops.map(join).find(values=>!values.length||inner-2-size(values)-1>=4)||[];
      const room=inner-2-size(right)-(right.length?1:0),name=cut(line(r.title||'·','loc_repo'),Math.max(1,room));
      return cut([...lead,...name,pad(inner-2-size(name)-size(right)),...right],inner);
    }
    function details(r){
      const p=r.paint||{},out=[];
      if(side){
        // Inside the box a full gauge may not fit: the bar gives way before its
        // number, so a cut row never hides the percent behind `…`.
        const gauge=p.gauge||[],percent=gauge.length?[gauge.at(-1)]:[];
        const fit=(lead,end=[])=>{for(const g of [gauge,percent,[]]){const row=[...lead,...g];if(size(row)+(end.length?1+size(end):0)<=inner)return row;}return cut(lead,inner-(end.length?1+size(end):0));};
        if(routeLine==='model'){
          const end=r.elapsed?[text(r.elapsed)]:[],left=fit([text('  '),...(p.effort?[p.effort,text('  ')]:[])],end);
          out.push(end.length?[...left,pad(inner-size(left)-size(end)),...end]:left);
        }else{
          const left=fit([text('  '),...(p.model_text?[p.model_text,text(' ')]:[]),...(p.effort?[p.effort,text('  ')]:[])]);
          if(size(left)>2)out.push(left);
        }
      }
      out.push(...itemRows(routeItems(r)));
      if(!out.length)out.push([text('  '),text(t('checking','Checking'))]);
      return out;
    }
    // Spec rule 14: every group is one box of the same thin rounded line; the
    // question box is the person's and sits at the bottom. Strength of color
    // tells the boxes apart: question the answer-needed color, route plain,
    // messages dim, all from the panel's own styles.
    // Spec rule 22: each line keeps its own role, so a shade colors it without a dim on top;
    // the question's border is rose.
    const tone={route:{key:'plain',role:'route'},messages:'dim',question:{key:asking?'lvl_y':'dim',role:'accent-line'}};
    const L=(v,k)=>typeof k==='object'?{...text(v,k.key),role:k.role}:text(v,k);
    const box=4;
    // Box content is laid out at the box's inner width, so nothing is cut by the frame.
    const within=fn=>{inner-=box;try{return fn();}finally{inner+=box;}};
    // A title in the top border: whole words only, so `route N` shows whole or not at all.
    const titled=(label,limit,ellipsis)=>{
      if(size(label)<=limit)return label;
      if(ellipsis)return cut(label,limit);
      const all=label.map(p=>clean(p.text)).join(''),words=all.split(' ');
      if(words[0]==='route'&&words.length>1)words.splice(0,2,words[0]+' '+words[1]);
      let keep='';
      for(const word of words){const next=keep?keep+' '+word:word;if(measure(next)>limit)break;keep=next;}
      const out=[];let left=keep.length;
      for(const p of label){const s=clean(p.text).slice(0,left);if(s)out.push({...p,text:s});left-=s.length;}
      return out;
    };
    // A note on the right of a border (the route box's hidden counts) shows whole or not at all.
    const boxTop=(label,key,{action,ellipsis=false,note=[]}={})=>{
      const room=Math.max(0,inner-2),tail=note.length&&size(note)+3<=room?[L(' ',key),...note,L(' ─',key)]:[];
      const name=label.length&&room-size(tail)>=4?titled(label,room-size(tail)-3,ellipsis):[];
      const head=name.length?[L('─ ',key),...name,L(' ',key)]:[];
      return {parts:[L('╭',key),...head,L('─'.repeat(Math.max(0,room-size(head)-size(tail))),key),...tail,L('╮',key)],action};
    };
    const boxBottom=(key,note=[],action)=>{
      const room=Math.max(0,inner-2),tail=note.length&&size(note)+3<=room?[L(' ',key),...note,L(' ─',key)]:[];
      return {parts:[L('╰',key),L('─'.repeat(Math.max(0,room-size(tail))),key),...tail,L('╯',key)],action};
    };
    const boxRow=(item,key)=>{
      const fit=clip(item.parts||[],Math.max(0,inner-box));
      return {...item,parts:[L('│ ',key),...fit.parts,pad(inner-box-fit.used),L(' │',key)]};
    };
    // Spec rule 19: beside the native area a folded route takes two rows: the
    // glyph and its whole title, then (indented, dim) harness, model, context and
    // elapsed time. Short of cells the `route line` choice keeps its value (time:
    // elapsed, model: model) and the other goes; a value shows whole or not at all.
    function second(r){
      const p=r.paint||{},value=x=>x?[{...x,dim:true}]:[];
      const H=value(p.harness),M=value(p.model_text),C=value(p.ctx),E=r.elapsed?[text(r.elapsed)]:[];
      const build=(h,m,c,e)=>{const left=[h,m,c].filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);return [text('  '),...left,...(e.length?[...(left.length?[text(' · ')]:[]),...e]:[])];};
      const drops=routeLine==='model'?[[H,M,C,E],[H,M,C,[]],[H,M,[],[]],[H,[],[],[]],[[],[],[],[]]]:[[H,M,C,E],[H,[],C,E],[H,[],[],E],[[],[],[],E],[[],[],[],[]]];
      for(const pick of drops){const row=build(...pick);if(size(row)<=inner)return size(row)>2?row:null;}
      return null;
    }
    const routeRows=[],detailRows=[];
    within(()=>{
      for(const r of listed){
        const key='route:'+r.id,p=r.paint||{};
        const two=side?[[routeGlyph(r),text(' '),...cut(line(r.title||'·','loc_repo'),inner-2)],second(r)].filter(Boolean):null;
        routeRows.push({parts:folded(r),two,action:key,kind:r.kind});
        if(open.has(key))for(const d of details(r))detailRows.push({parts:d,action:key,after:r.id});
      }
    });
    const messages=painted(model.messages).slice(-3),messageRows=[];
    for(const m of messages){
      const key=messageKey(m),parts=messageParts(m);
      // Opened: every recorded row. When rows run short it ends in a row that counts the rest.
      if(open.has(key)){const out=[];inner-=2;try{wrap(parts,Infinity,undefined,out);}finally{inner+=2;}out.forEach((row,i)=>messageRows.push({parts:i?[text('  '),...row]:row,action:key,body:key}));}
      else messageRows.push({parts:cut(parts,inner),action:key});
    }
    let messageLimit=Math.max(0,...messageRows.map(x=>x.body?messageRows.filter(y=>y.body===x.body).length:0));
    let messageOne=false;
    const visibleMessages=()=>{
      const out=[];
      for(const row of messageRows){
        if(!row.body){out.push(row);continue;}
        const group=messageRows.filter(y=>y.body===row.body),index=group.indexOf(row);
        // Folded to one message, an opened body keeps one row and its read row.
        const limit=messageOne?Math.min(messageLimit,2):messageLimit;
        if(group.length<=limit&&!(messageOne&&group.length>1))out.push(row);
        else if(index<limit-1)out.push(row);
        else if(index===limit-1)out.push({parts:[text('  '),text('+'+(group.length-index)+' lines ›','loc_repo')],action:'read:'+row.body});
      }
      if(!messageOne)return out;
      // Folded to one message: an opened one stays (its first row and the
      // `+N lines ›` row to its read page), else the first; the lower rule counts the rest.
      const opened=out.find(row=>row.body);
      if(!opened)return out.slice(0,1);
      const read=out.find(row=>row.action==='read:'+opened.body);
      return read?[opened,read]:[opened];
    };
    const hiddenMessages=()=>messageOne?Math.max(0,messages.length-1):0;
    // No rows yet: the producer's own label stands alone; a known empty list draws no box.
    const label=clean(model.route_label||'').trim();
    // An open route count pulses on the shared beat while one of them runs (Fleet's g_work/g_work_off).
    const running=shown.some(live);
    const routeTitle=shown.length?[running?pulse('route '+total):text('route '+total,'g_idle')]:label?[text(label,'g_idle')]:null;
    const routeChecking=!shown.length&&label?[{parts:[text(t('checking','Checking'))],action:'route'}]:[];
    // A press in the panel opens a route's question; the editor keeps its budget.
    const core=top.length-spacers.size-optional.size-gitRows.size;
    // Running jobs keep their row beside a question: its editor pages its own content.
    const q=questions({budget:Math.max(2,rows-core-jobRows.size-1-(quota.length?1:0)),columns:inner-box});
    const ask=[];
    if(q.length){
      const key=tone.question;
      const accent=parts=>parts.flatMap(part=>{
        const s=String(part.text??''),m=/^(\s*)([◑›])(.*)$/su.exec(s);
        if(!m)return [part];
        return [...(m[1]?[{...part,text:m[1]}]:[]),{...part,text:m[2],role:'accent'},...(m[3]?[{...part,text:m[3]}]:[])];
      });
      ask.push(boxTop(accent(q[0]),key,{action:'question',ellipsis:true}));
      for(const row of q.slice(1))ask.push(boxRow({parts:accent(row),action:'question'},key));
      ask.push(boxBottom(key,[],'question'));
    }
    // Short of rows (spec rules 14, 19 and 23): spacers; two-row routes fold to
    // one row; opened details close; messages fold to one row; only then the
    // route box scrolls in place (fewer rows shown, down to one, none dropped);
    // the Git row; the route box; running jobs; the messages section only last; 5h/7d.
    let keepGit=true,keepSpacers=true,keepMessages=true,keepDetails=true,routeLimit=routeRows.length,keepRoutes=!!routeTitle,keepOptional=true,keepQuota=quota.length>0;
    let twoRows=side&&routeRows.some(x=>x.two);
    const routeSize=x=>twoRows&&x.two?x.two.length:1;
    // An opened route's detail that outgrows its rows keeps its first rows and a
    // `+N lines ›` row to the route read page, where every value reads whole.
    let detailCap=Math.max(0,...listed.map(r=>detailRows.filter(d=>d.after===r.id).length));
    const detailsOf=id=>{
      const rows=detailRows.filter(d=>d.after===id);
      if(rows.length<=detailCap)return rows;
      const keep=Math.max(1,detailCap-1);
      return [...rows.slice(0,keep),{parts:[text('  '),text('+'+(rows.length-keep)+' lines ›','loc_repo')],action:'route',after:id}];
    };
    // Whole rows are counted from the top while the box shows every route; once it
    // scrolls, twoRows and details are already gone and every route takes one row.
    const routeBox=()=>keepRoutes?2+(shown.length?routeRows.slice(0,routeLimit).reduce((n,x)=>n+routeSize(x),0)
      +(keepDetails?routeRows.slice(0,routeLimit).reduce((n,x)=>n+detailsOf(x.action.slice(6)).length,0):0):routeChecking.length):0;
    const messageSection=()=>keepMessages&&messageRows.length?2+visibleMessages().length:0;
    const need=()=>core+(keepGit?gitRows.size:0)+(keepSpacers?spacers.size:0)+(keepOptional?optional.size:0)+ask.length+routeBox()+messageSection()+(keepQuota?1:0);
    const budget=rows;
    if(need()>budget)keepSpacers=false;
    if(need()>budget)twoRows=false;
    while(need()>budget&&detailCap>2)detailCap--;
    if(need()>budget)keepDetails=false;
    while(need()>budget&&messageLimit>2)messageLimit--;
    if(need()>budget&&visibleMessages().length>1)messageOne=true;
    while(need()>budget&&routeLimit>1)routeLimit--;
    if(need()>budget)keepGit=false;
    if(need()>budget)keepRoutes=false;
    if(need()>budget)keepOptional=false;
    if(need()>budget)keepMessages=false;
    if(need()>budget)keepQuota=false;
    const order=[];
    top.forEach((row,i)=>{if((keepSpacers||!spacers.has(i))&&(keepOptional||!optional.has(i))&&(keepGit||!gitRows.has(i)))order.push({parts:row,groups:topGroups.get(i)});});
    let routeBlock=[],messageBlock=[];
    if(keepRoutes){
      const key=tone.route;
      // Spec rule 23: rows short, the box shows a window of the routes and scrolls
      // in place (wheel, PgUp/PgDn). A working route comes first, then an answer
      // wait, a stopped one, a finished one; the borders count what is hidden.
      const scrolling=routeLimit<routeRows.length;
      const rank={working:0,blocked:1,idle:2,dead:3,done:4,stale:4};
      const sorted=scrolling?routeRows.map((x,i)=>[x,i]).sort((a,b)=>((rank[a[0].kind]??2)-(rank[b[0].kind]??2))||a[1]-b[1]).map(([x])=>x):routeRows;
      routeOffset=scrolling?Math.max(0,Math.min(routeRows.length-routeLimit,Math.trunc(Number(routeScroll)||0))):0;
      routeStep=routeLimit;routeMax=scrolling?routeRows.length-routeLimit:0;
      const kept=sorted.slice(routeOffset,routeOffset+routeLimit);
      const above=routeOffset,below=routeRows.length-routeOffset-kept.length+unsent;
      const hiddenNote=n=>[text(n,'loc_repo')];
      routeBlock=[{...boxTop(routeTitle,key,{action:'route',note:above?hiddenNote('↑'+above):[]}),groups:['route']}];
      for(const item of shown.length?kept:routeChecking){
        // A press on either row of a two-row route opens it.
        for(const parts of twoRows&&item.two?item.two:[item.parts])routeBlock.push(boxRow({parts,action:item.action},key));
        if(keepDetails&&item.action.startsWith('route:'))for(const d of detailsOf(item.action.slice(6)))routeBlock.push(boxRow(d,key));
      }
      routeBlock.push(boxBottom(key,below?hiddenNote('↓'+below):[]));
    }
    // Messages have no box: a titled upper rule and a lower rule, fixed at the
    // bottom right above 5h/7d (spec rule 14, 11-11 night final layout).
    if(keepMessages&&messageRows.length){
      const key=tone.messages,hidden=hiddenMessages();
      messageBlock=[{parts:rule([{...text('messages',key),role:'title'}]).map(x=>({...x,...(styles[key]||{dim:true}),role:x.role||'messages'}))}];
      messageBlock.push(...visibleMessages());
      const note=hidden?[text(' +'+hidden+' ','loc_repo'),text('─',key)]:[];
      messageBlock.push({parts:[{...text('─'.repeat(Math.max(0,inner-size(note))),key),role:'messages'},...note.map(x=>x.text==='─'?{...x,role:'messages'}:x)]});
    }
    // Each section is placed whole or not at all.
    const tail=ask.length+messageBlock.length+(keepQuota?1:0);
    if(order.length+routeBlock.length+tail>budget)routeBlock=[];
    for(const item of order)add(item.parts,item.action,item.groups);
    // The rows the route box takes, for the host's wheel (panel rows, as hits count them).
    if(routeBlock.length)routeArea={top:content.length+1,bottom:content.length+routeBlock.length,offset:routeOffset,max:routeMax,step:routeStep};
    for(const item of routeBlock)add(item.parts,item.action,item.groups);
    // What comes and goes sits low: the question right above the messages, which keep the bottom.
    if(fill)while(content.length<rows-tail)add([]);
    if(content.length+tail<=budget){
      for(const item of ask)add(item.parts,item.action);
      for(const item of messageBlock)add(item.parts,item.action);
    }else for(const item of ask)add(item.parts,item.action);
    // A reset time that does not fit goes whole, never as a cut `(↻ …`.
    const quotaRow=size(quota)<=inner?quota:quota.filter(part=>!/^\(↻/.test(String(part.text)));
    if(keepQuota&&content.length<rows)add(cut(quotaRow,inner),undefined,['quota']);
  }
  const count=fill?height:Math.min(height,content.length+2),rows=[];
  // Spec rules 16-17: the top border carries this session's state on the left
  // (Fleet's session glyph and color, an English word) and only the gear on the
  // right. `HEARTING` names the settings screen alone. An unknown state is left blank.
  const own=settings?null:LIVE[state];
  const frameText=v=>({...text(v,color),role:'frame'});
  const leftParts=settings?[{...text(' HEARTING ','loc_repo'),bold:true}]:
    own?[frameText(' '),{...text(own[0]==='spin'?spinGlyph.text:own[0],own[2]),...(own[0]==='spin'?{spin:true}:{}),role:'session'},
      {...text(' '+own[1]+' ',own[2]),role:state==='idle'?'secondary':'session'}]:[];
  const label=clip(leftParts,Math.max(0,width-7)),right=clip([frameText(' ⚙ ')],Math.max(0,width-label.used-2));
  rows.push([frameText('┌'),...label.parts,frameText('─'.repeat(Math.max(0,width-2-label.used-right.used))),...right.parts,frameText('┐')]);
  // The gear's press reaches past its own cell to the rule beside it and the corner.
  hits.push({row:0,left:Math.max(1,width-right.used-2),right:width-1,action:'settings'});
  for(let i=0;i<count-2;i++){
    const fitted=clip(content[i]||[],inner);
    rows.push([frameText('│'),text(' '),...fitted.parts,text(' '.repeat(Math.max(0,inner-fitted.used)+1)),frameText('│')]);
  }
  const foot=clip(settings?line(' Esc · '+t('cancel','Cancel')+' '):edge.length?[text(' '),...edge,text(' ')]:[],width-4);
  rows.push([frameText('└─'),...foot.parts,frameText('─'.repeat(Math.max(0,width-3-foot.used))+'┘')]);
  const painted=appearance(rows,cfg,{colors:model.colors??256,noColor:model.no_color===true,background});
  // The rows whose spinner cells turn between paints (the host swaps only those cells).
  const spins=rows.map((row,i)=>row.some(part=>part.spin)?i:-1).filter(i=>i>=0&&i<count);
  const animated=rows.slice(0,count).some(row=>row.some(part=>part.spin||part.beat));
  // The statusline groups this paint actually shows (cut rows count; dropped ones do not).
  const groups=[...new Set(contentGroups.slice(0,Math.max(0,count-2)).flat())];
  if(routeArea&&routeArea.bottom>count-2)routeArea=null;
  return {rows:painted,hits:hits.filter(h=>h.row<count),color,routePages,routeDetail,routeArea,groups,animated:animated&&!settings,spins};
}
module.exports={render,defaults,fields,change,appearance,options:OPTIONS,SPIN,FACES};
