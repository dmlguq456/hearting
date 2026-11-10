'use strict';
// Geometry and paint only. The existing question controller owns drafts/effects.
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14};
// OpenCode owns the 42-cell container and its native scrollbar. Keep a cell
// of owned gutter inside the additive slot, without painting that scrollbar.
const nativeSidebarColumns=35;
function nativePanelHit(terminal,view,x,y){
  // Match our entire currently painted additive frame, in cells. This only
  // routes a display click; it supplies no session identity or input proof.
  if(!view?.rows?.length||view.rows.length>terminal.rows)return null;
  const expected=view.rows.map(row=>row.map(part=>part.text).join(''));
  const buffer=terminal.buffer.active,candidates=[];
  for(let top=0;top+expected.length<=terminal.rows;top++){
    if(y<top||y>=top+expected.length)continue;
    const row=buffer.getLine(buffer.viewportY+top);
    for(let left=Math.max(0,terminal.cols-42);left+nativeSidebarColumns<=terminal.cols;left++){
      if(x<left||x>=left+nativeSidebarColumns||row?.getCell(left)?.getChars()!=='┌')continue;
      if(expected.every((text,i)=>buffer.getLine(buffer.viewportY+top+i)?.translateToString(true,left,left+nativeSidebarColumns)===text))
        candidates.push({left,top});
    }
  }
  if(candidates.length!==1)return null;
  const {left,top}=candidates[0];
  return {action:view.hits.find(h=>h.row===y-top&&x-left>=h.left&&x-left<=h.right)?.action};
}
const fields=['layout','side_width','narrow','auxiliary','bottom_rows','side_min_columns'];
const names=['layout','width','narrow','auxiliary','height','threshold'];
function change(value,key,step){
  const options={layout:['side','bottom'],narrow:['bottom','compact'],auxiliary:['summary','detail']};
  if(options[key]){const list=options[key];return {...value,[key]:list[(list.indexOf(value[key])+step+list.length)%list.length]};}
  const bounds={side_width:[32,80,1],bottom_rows:[8,20,1],side_min_columns:[80,1000,10]},[lo,hi,n]=bounds[key];
  return {...value,[key]:Math.max(lo,Math.min(hi,value[key]+step*n))};
}
function render({width,height,side,fill=side,model={},native={},state,sid,questions,sections,configuration,settings,routeDetail=false,routePage=0,focused=false,cwd='',harness='',cellWidth}){
  width=Math.max(12,width);height=Math.max(4,height);
  const labels=model.labels||{},styles=model.styles||{},t=(k,f)=>labels[k]||f;
  const text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const line=(value,key)=>[text(value,key)];
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const measure=cellWidth|| (s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  function clip(parts,limit){let out=[],used=0;for(const part of parts){let s='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;s+=c;used+=w;}if(s)out.push({...part,text:s});if(used>=limit)break;}return {parts:out,used};}
  const inner=width-4,content=[],hits=[],edge=[];
  function add(parts,action){const row=content.length;if(action)hits.push({row:row+1,left:2,right:width-3,action});content.push(parts);}
  function wrap(parts,maximum=3,action,into){
    // `into` collects the rows for a caller that places them after measuring.
    const put=into?row=>into.push(row):row=>add(row,action);
    let current=[],used=0,count=0,space=false;
    for(const part of parts)for(const token of clean(part.text).match(/\s+|\S+/gu)||[]){
      if(/^\s+$/.test(token)){space=used>0;continue;}
      const size=measure(token),gap=space&&used?1:0;
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
  if(settings){
    add(line(t('settings_title','Hearting shell settings'),'loc_repo'));
    const start=Math.max(0,settings.index-Math.max(0,height-7));
    for(let i=start;i<fields.length&&content.length<height-4;i++){
      const key=fields[i],value=settings.value[key],v=typeof value==='string'?t(value,value):value;
      if(harness==='opencode'&&key==='side_min_columns')continue;
      if(harness==='opencode'&&key==='side_width'){add(line((i===settings.index?'› ':'  ')+t('fixed_width','OpenCode · fixed 42 columns'),i===settings.index?'loc_repo':'dim'));continue;}
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
  }else{
    const selected=native.model||model.model_value,effort=native.model?native.effort:model.effort_value;
    const formatted=selected===model.model_value&&(effort||'')===(model.effort_value||'')&&model.model?.some(p=>String(p.text).includes(' · '));
    const m=formatted?model.model:selected?line(selected+' · '+(effort||'?'),'loc_repo'):(model.model||line(t('checking','Checking')+' · ?'));
    add(m);
    if(model.warning)add(line(model.warning,'lvl_r'));
    if(status&&side)add(line(status,color));
    if(routeDetail){
      add(line(t('routes','Route')+' · '+t('back','Back'),'loc_repo'),'route');
      const start=content.length;
      for(const route of routes){
        wrap(line(route.title||t('checking','Checking'),'loc_repo'),2);
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
      add(line(t('work','Current work')+(side?'':' · '+(model.title||t('no_work','No confirmed work yet')))));
      // A side panel's spacer row goes to the session items while a question
      // is shown; the question's own budget below still counts it.
      const reserved=side&&asking?1:0;
      if(side){wrap(line(model.title||(!sid||model.routes_known?t('no_work','No confirmed work yet'):t('checking','Checking')),'loc_repo'),2);if(!reserved)add([]);}
      const routeTitle=first?.title||model.route_label||(model.routes_known?t('no_routes','No current route'):t('checking','Checking'));
      const flow=(first?.stages||[]).map(s=>(s.current?'› ':'')+(s.name||s.id||'')).filter(Boolean).join(' → ');
      const heading=!side&&flow?'⏳ '+t('routes','Route')+' · '+routeTitle+' · '+flow:side&&first?.title===model.title?'⏳ '+t('routes','Route'):
        !first&&model.route_label?model.route_label:'⏳ '+t('routes','Route')+' · '+routeTitle;
      wrap(line(heading,'loc_repo'),side?2:1,'route');
      if(first){
        if(flow&&side)wrap(line(flow),2,'route');
        if(first.stage_more)add(line('+'+first.stage_more+' · Fleet ›'),'fleet');
        if(side&&first.owner_line)wrap(line(first.owner_line),2,'route');
        if(side)wrap(line([first.stage!=='·'?first.stage:'',first.reason||first.state_label||first.state].filter(Boolean).join(' · '),color),2,'route');
        // The bottom panel keeps its rows: the route owner shares the summary row.
        const verdict=first.visibility?.summary||'';
        const owned=!side&&first.owner?[verdict,first.owner].filter(Boolean).join(' · '):verdict;
        if(owned)wrap(line(owned),1,'route');
      }
      if(routes.length>1)add(line('+'+(routes.length-1)+' · '+t('routes','Route')),'route');
      // Bound optional material first. Question editor gets the remaining rows,
      // and keeps its own overflow/Tab/form/draft/revision handling unchanged.
      // At a short bottom editor the answer owns the scarce rows, identically
      // for every harness. Session telemetry takes only the rows left after it.
      const summary=focused&&!side?[]:(model.summary||[]).slice(0,2);
      const remaining=Math.max(2,height-2-content.length-reserved-summary.length);
      const q=questions({budget:Math.max(2,remaining-2),columns:inner-4});
      if(q.length){
        add(line('╭'+'─'.repeat(inner-2)+'╮',asking?'lvl_y':'dim'),'question');
        for(const row of q.slice(0,Math.max(0,height-3-reserved-content.length))){
          const fit=clip(row,inner-4);
          add([text('│ ',asking?'lvl_y':'dim'),...fit.parts,text(' '.repeat(inner-3-fit.used)),text('│',asking?'lvl_y':'dim')],'question');
        }
        add(line('╰'+'─'.repeat(inner-2)+'╯',asking?'lvl_y':'dim'),'question');
      }
      const result=routes.find(r=>r.outcome);
      if(side&&result&&content.length<height-4){
        add([]);add(line(t('results','Results & connected work')));
        add(line(result.outcome,'lvl_g'));
      }
      // Every harness paints the whole statusline here, with no separate view:
      // the summary row first, then folder/title/branch/Git/jobs, then peer
      // news and the last input, each only while rows remain. Absent values
      // are already left out. Ownership is supplied as observed facts.
      let room=Math.max(0,height-2-content.length);
      const kept=summary.slice(0,room);room-=kept.length;
      const session=[],quiet=focused&&!side;
      let placed=0;
      if(!quiet)for(const row of model.session||[]){const rows=[];wrap(row,4,undefined,rows);if(rows.length>room)break;session.push(...rows);room-=rows.length;placed++;}
      // When the question leaves no rows for every session row, title, jobs
      // and branch still stay on screen, in that order: packed into the
      // spare rows, then into the bottom edge.
      if(placed<(model.session||[]).length&&!settings){
        // Partial session rows would spend the spare rows on the folder first.
        room+=session.length;session.length=0;
        const items=model.brief||[];
        // Only rows already painted: a filled side panel's rows or the spacer.
        // A bottom panel does not grow for this; it uses its edge.
        const spare=fill?room:Math.min(room,reserved);
        const slots=Array.from({length:spare},()=>[]);slots.push(edge);
        const size=parts=>measure(parts.map(p=>clean(p.text)).join(''));
        const limit=i=>i<spare?inner:width-6;
        for(const item of items){
          let i=slots.findIndex((slot,n)=>size(slot)+(slot.length?3:0)+size(item)<=limit(n));
          if(i<0)i=slots.findIndex(slot=>!slot.length);
          if(i<0)break;
          if(slots[i].length)slots[i].push(text(' · '));
          slots[i].push(...item);
        }
        for(const slot of slots.slice(0,spare))if(slot.length){session.push(slot);room--;}
      }
      const tail=[];
      if(!quiet)for(const row of sections?.tail||[]){const rows=[];wrap(row,2,undefined,rows);if(rows.length>room)break;tail.push(...rows);room-=rows.length;}
      const bottom=[...tail,...session,...kept];
      if(fill&&bottom.length)while(content.length<height-2-bottom.length)add([]);
      for(const row of bottom)add(row);
    }
  }
  const count=fill?height:Math.min(height,content.length+2),rows=[];
  const settingsLabel='⚙ '+t('settings','Settings'),label=clip(line(' HEARTING ','loc_repo'),width-4),right=clip(line(' '+settingsLabel+' ',color),Math.max(0,width-label.used-2));
  rows.push([text('┌',color),...label.parts.map(p=>({...p,bold:true})),text('─'.repeat(Math.max(0,width-2-label.used-right.used)),color),...right.parts,text('┐',color)]);
  hits.push({row:0,left:width-right.used-1,right:width-2,action:'settings'});
  for(let i=0;i<count-2;i++){
    const fitted=clip(content[i]||[],inner);
    rows.push([text('│',color),text(' '),...fitted.parts,text(' '.repeat(Math.max(0,inner-fitted.used)+1)),text('│',color)]);
  }
  const foot=clip(settings?line(' Esc · '+t('cancel','Cancel')+' '):edge.length?[text(' '),...edge,text(' ')]:[],width-4);
  rows.push([text('└─',color),...foot.parts,text('─'.repeat(Math.max(0,width-3-foot.used))+'┘',color)]);
  return {rows,hits:hits.filter(h=>h.row<count),color,routePages};
}
module.exports={render,defaults,fields,change,nativeSidebarColumns,nativePanelHit};
