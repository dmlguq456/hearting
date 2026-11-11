'use strict';
// Geometry and paint only. The existing question controller owns drafts/effects.
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14,
  position:'right',theme:'terminal',shade:'none',route_line:'time'};
// Panel color schemes. `terminal` is the original look: the terminal's own
// background with the shared palette. `light` swaps that palette's pale hues
// for darker ones that read on a light terminal; `mono` drops hue entirely and
// keeps weight, glyphs and words. Text and glyphs always carry the state, so
// no scheme makes color the only signal.
const LIGHT_256={253:236,252:238,150:28,186:130,217:124,116:30,176:91,147:25,230:94,219:162,137:130};
// A shade is the panel's own background. It is drawn with an explicit text
// color, so it reads on either terminal background, and only in 256 colors.
const SHADES={dark:{light:235,medium:237,text:252},light:{light:255,medium:253,text:235}};
function appearance(rows,configuration={},{colors=256,noColor=false}={}){
  const cfg={...defaults,...configuration},theme=['terminal','light','mono'].includes(cfg.theme)?cfg.theme:'terminal';
  const shade=['light','medium'].includes(cfg.shade)&&!noColor&&colors>=256?cfg.shade:'';
  if(theme==='terminal'&&!shade)return rows;
  const base=SHADES[theme==='light'?'light':'dark'];
  return rows.map(row=>row.map(part=>{
    let foreground=Number.isInteger(part.foreground)&&part.foreground>=0?part.foreground:null;
    if(theme==='mono')foreground=null;
    else if(theme==='light'&&foreground!==null)foreground=colors>=256?(LIGHT_256[foreground]??foreground):(foreground===7?null:foreground);
    if(shade&&foreground===null)foreground=base.text;
    const next={...part,foreground};
    if(shade)next.background=base[shade];
    return next;
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
function render({frame=0,width,height,side,fill=side,model={},native={},state,sid,questions,sections,configuration,settings,routeDetail=false,routePage=0,expandedKeys=[],focused=false,cwd='',harness='',cellWidth}){
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
  const asking=!!model.answerable;
  // UI attention is not native agent liveness. Only a confirmed route failure
  // may make this frame red; an unknown source/permission popup cannot.
  const failed=first?.state==='dead',done=first?.state==='done';
  const color=failed?'g_dead':asking?'lvl_y':first?.state==='working'?'g_work':done?'lvl_g':'dim';
  const status=failed?(first.reason||t('unknown','Checking state')):asking?t('answer','Your answer needed'):first?.state==='working'?t('working','Working'):done?t('ready','Result ready'):'';
  const cfg={...defaults,...configuration};
  let routePages=1;
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
      wrap(line(route.title||t('checking','Checking'),'loc_repo'),2);
      // The route's owner and its evidence summary, as the earlier route heading carried them.
      if(route.owner)wrap(line(route.owner),2);
      if(route.visibility?.summary)wrap(line(route.visibility.summary),2);
      for(const stage of route.stages||[])wrap(line((stage.name||stage.id)+' · '+stage.state),2);
      if(route.stage_more)add(line('+'+route.stage_more+' · Fleet'));
      wrap(line(route.reason||route.state_label||route.state),3);
      if(route.outcome)wrap(line(route.outcome,'lvl_g'),2);
      for(const [label,value,tone] of route.visibility?.lines||[]){
        wrap(line(label+': '+value,({good:'lvl_g',bad:'lvl_r',mixed:'lvl_y',info:'loc_repo',dim:'dim'})[tone]||'dim'),20);
      }
      const next=route.visibility?.next;
      if(next&&!(route.visibility?.lines||[]).some(row=>row[1]===next))wrap(line(next),10);
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
    // The whole count is the producer's; the panel keeps only some rows of it.
    const total=Number.isInteger(model.route_total)&&model.route_total>=shown.length?model.route_total:shown.length;
    const listed=shown.slice(0,5),more=shown.length?total-listed.length:0;
    const elapsedWidth=Math.max(0,...listed.map(r=>measure(r.elapsed||'')));
    const columnWidth=(pick)=>Math.max(0,...listed.map(r=>size(pick(r))));
    const owner=r=>{const p=r.paint||{};return [p.harness,p.model_text&&text(' '),p.model_text,p.effort&&text(' '),p.effort].filter(Boolean);};
    const third=r=>r.tool?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:r.step?[text(r.step)]:[];
    function folded(r){
      const p=r.paint||{},lead=[p.glyph||text('·'),text(' ')];
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
      const right=routeLine==='model'?[p.harness,p.model_text&&text(' '),p.model_text,p.ctx&&text(' '),p.ctx].filter(Boolean):
        [p.harness,p.ctx&&text(' '),p.ctx,(elapsedWidth&&text(' '+' '.repeat(elapsedWidth-measure(r.elapsed||''))+(r.elapsed||'')))].filter(Boolean);
      const room=inner-2-size(right)-1,name=cut(line(r.title||'·','loc_repo'),Math.max(4,room));
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
      if(r.flow){const flow=[];inner-=2;try{wrap([text(r.flow)],2,undefined,flow);}finally{inner+=2;}for(const row of flow)out.push([text('  '),...row]);}
      const doing=[r.tool&&side?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:[],r.agents?[text('+'+r.agents+' agent')]:[]].filter(g=>g.length);
      if(doing.length)out.push(cut([text('  '),...joined(doing,' · ')],inner));
      if(r.kind==='dead'&&(r.reason||r.state_label))put(out,[text('  '),text(r.reason||r.state_label,'g_dead')],2);
      if(!out.length)out.push([text('  '),text(t('checking','Checking'))]);
      return out;
    }
    // Spec rule 14: every group is one box of the same thin rounded line; the
    // question box is the person's and sits at the bottom. Strength of color
    // tells the boxes apart: question the answer-needed color, route plain,
    // messages dim, all from the panel's own styles.
    const tone={route:'plain',messages:'dim',question:asking?'lvl_y':'dim'};
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
    const boxTop=(label,key,{action,ellipsis=false}={})=>{
      const room=Math.max(0,inner-2),name=label.length&&room>=4?titled(label,room-3,ellipsis):[];
      const head=name.length?[text('─ ',key),...name,text(' ',key)]:[];
      return {parts:[text('╭',key),...head,text('─'.repeat(Math.max(0,room-size(head))),key),text('╮',key)],action};
    };
    const boxBottom=(key,note=[],action)=>{
      const room=Math.max(0,inner-2),tail=note.length&&size(note)+3<=room?[text(' ',key),...note,text(' ─',key)]:[];
      return {parts:[text('╰',key),text('─'.repeat(Math.max(0,room-size(tail))),key),...tail,text('╯',key)],action};
    };
    const boxRow=(item,key)=>{
      const fit=clip(item.parts||[],Math.max(0,inner-box));
      return {...item,parts:[text('│ ',key),...fit.parts,pad(inner-box-fit.used),text(' │',key)]};
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
        const two=side?[[p.glyph||text('·'),text(' '),...cut(line(r.title||'·','loc_repo'),inner-2)],second(r)].filter(Boolean):null;
        routeRows.push({parts:folded(r),two,action:key});
        if(open.has(key))for(const d of details(r))detailRows.push({parts:d,action:key,after:r.id});
      }
    });
    if(more>0)routeRows.push({parts:line('+'+more+' more'),action:'fleet'});
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
    const routeTitle=shown.length?[text('route '+total,'g_idle')]:label?[text(label,'g_idle')]:null;
    const routeChecking=!shown.length&&label?[{parts:[text(t('checking','Checking'))],action:'route'}]:[];
    // A press in the panel opens a route's question; the editor keeps its budget.
    const core=top.length-spacers.size-optional.size-gitRows.size;
    // Running jobs keep their row beside a question: its editor pages its own content.
    const q=questions({budget:Math.max(2,rows-core-jobRows.size-1-(quota.length?1:0)),columns:inner-box});
    const ask=[];
    if(q.length){
      const key=tone.question;
      ask.push(boxTop(q[0],key,{action:'question',ellipsis:true}));
      for(const row of q.slice(1))ask.push(boxRow({parts:row,action:'question'},key));
      ask.push(boxBottom(key,[],'question'));
    }
    // Short of rows (spec rules 14 and 19, the 11-11 night final layout): spacers;
    // then the route side first — two-row routes fold to one row, opened details
    // close, route rows give way to one row and `+N more`; then messages fold to
    // one row and the last route row goes; the Git row; the route box; running
    // jobs; the messages section only last; 5h/7d.
    let keepGit=true,keepSpacers=true,keepMessages=true,keepDetails=true,routeLimit=routeRows.length,keepRoutes=!!routeTitle,keepOptional=true,keepQuota=quota.length>0;
    let twoRows=side&&routeRows.some(x=>x.two);
    const routeSize=x=>twoRows&&x.two?x.two.length:1;
    const routeBox=()=>keepRoutes?2+(shown.length?routeRows.slice(0,Math.min(routeLimit,routeRows.length)).reduce((n,x,i,list)=>n+(i===list.length-1&&list.length<routeRows.length?1:routeSize(x)),0)
      +(keepDetails?detailRows.filter(d=>routeRows.slice(0,routeLimit).some(x=>x.action==='route:'+d.after)).length:0):routeChecking.length):0;
    const messageSection=()=>keepMessages&&messageRows.length?2+visibleMessages().length:0;
    const need=()=>core+(keepGit?gitRows.size:0)+(keepSpacers?spacers.size:0)+(keepOptional?optional.size:0)+ask.length+routeBox()+messageSection()+(keepQuota?1:0);
    const budget=rows;
    if(need()>budget)keepSpacers=false;
    if(need()>budget)twoRows=false;
    if(need()>budget)keepDetails=false;
    // One route row stays beside `+N more` until the messages have folded too.
    while(need()>budget&&routeLimit>2)routeLimit--;
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
      const key=tone.route;routeBlock=[{...boxTop(routeTitle,key,{action:'route'}),groups:['route']}];
      const kept=routeRows.slice(0,routeLimit);
      if(kept.length<routeRows.length){const hidden=total-kept.slice(0,-1).filter(x=>x.action!=='fleet').length;kept[kept.length-1]={parts:line('+'+hidden+' more'),action:'fleet'};}
      for(const item of shown.length?kept:routeChecking){
        // A press on either row of a two-row route opens it.
        for(const parts of twoRows&&item.two?item.two:[item.parts])routeBlock.push(boxRow({parts,action:item.action},key));
        if(keepDetails)for(const d of detailRows)if(item.action==='route:'+d.after)routeBlock.push(boxRow(d,key));
      }
      routeBlock.push(boxBottom(key));
    }
    // Messages have no box: a titled upper rule and a lower rule, fixed at the
    // bottom right above 5h/7d (spec rule 14, 11-11 night final layout).
    if(keepMessages&&messageRows.length){
      const key=tone.messages,hidden=hiddenMessages();
      messageBlock=[{parts:rule([text('messages',key)]).map(x=>({...x,...(styles[key]||{dim:true})}))}];
      messageBlock.push(...visibleMessages());
      const note=hidden?[text(' +'+hidden+' ','loc_repo'),text('─',key)]:[];
      messageBlock.push({parts:[text('─'.repeat(Math.max(0,inner-size(note))),key),...note]});
    }
    // Each section is placed whole or not at all.
    const tail=ask.length+messageBlock.length+(keepQuota?1:0);
    if(order.length+routeBlock.length+tail>budget)routeBlock=[];
    for(const item of [...order,...routeBlock])add(item.parts,item.action,item.groups);
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
  const live=settings?null:LIVE[state];
  const leftParts=settings?[{...text(' HEARTING ','loc_repo'),bold:true}]:
    live?[text(' ',color),text(live[0]==='spin'?SPIN[Math.abs(Math.trunc(frame))%SPIN.length]:live[0],live[2]),text(' '+live[1]+' ',live[2])]:[];
  const label=clip(leftParts,Math.max(0,width-7)),right=clip(line(' ⚙ ',color),Math.max(0,width-label.used-2));
  rows.push([text('┌',color),...label.parts,text('─'.repeat(Math.max(0,width-2-label.used-right.used)),color),...right.parts,text('┐',color)]);
  // The gear's press reaches past its own cell to the rule beside it and the corner.
  hits.push({row:0,left:Math.max(1,width-right.used-2),right:width-1,action:'settings'});
  for(let i=0;i<count-2;i++){
    const fitted=clip(content[i]||[],inner);
    rows.push([text('│',color),text(' '),...fitted.parts,text(' '.repeat(Math.max(0,inner-fitted.used)+1)),text('│',color)]);
  }
  const foot=clip(settings?line(' Esc · '+t('cancel','Cancel')+' '):edge.length?[text(' '),...edge,text(' ')]:[],width-4);
  rows.push([text('└─',color),...foot.parts,text('─'.repeat(Math.max(0,width-3-foot.used))+'┘',color)]);
  const painted=appearance(rows,cfg,{colors:model.colors??256,noColor:model.no_color===true});
  // The statusline groups this paint actually shows (cut rows count; dropped ones do not).
  const groups=[...new Set(contentGroups.slice(0,Math.max(0,count-2)).flat())];
  return {rows:painted,hits:hits.filter(h=>h.row<count),color,routePages,routeDetail,groups};
}
module.exports={render,defaults,fields,change,appearance,options:OPTIONS,SPIN};
