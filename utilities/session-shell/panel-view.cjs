'use strict';
// Geometry and paint only. The existing question controller owns drafts/effects.
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14};
const fields=['layout','side_width','narrow','auxiliary','bottom_rows','side_min_columns'];
const names=['layout','width','narrow','auxiliary','height','threshold'];
function change(value,key,step){
  const options={layout:['side','bottom'],narrow:['bottom','compact'],auxiliary:['summary','detail']};
  if(options[key]){const list=options[key];return {...value,[key]:list[(list.indexOf(value[key])+step+list.length)%list.length]};}
  const bounds={side_width:[32,80,1],bottom_rows:[8,20,1],side_min_columns:[80,1000,10]},[lo,hi,n]=bounds[key];
  return {...value,[key]:Math.max(lo,Math.min(hi,value[key]+step*n))};
}
function render({width,height,side,fill=side,model={},native={},state,sid,questions,sections,configuration,settings,routeDetail=false,details=false,focused=false,cwd='',harness='',cellWidth}){
  width=Math.max(12,width);height=Math.max(4,height);
  const labels=model.labels||{},styles=model.styles||{},t=(k,f)=>labels[k]||f;
  const text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const line=(value,key)=>[text(value,key)];
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const measure=cellWidth|| (s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  function clip(parts,limit){let out=[],used=0;for(const part of parts){let s='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;s+=c;used+=w;}if(s)out.push({...part,text:s});if(used>=limit)break;}return {parts:out,used};}
  const inner=width-4,content=[],hits=[];
  function add(parts,action){const row=content.length;if(action)hits.push({row:row+1,left:2,right:width-3,action});content.push(parts);}
  function wrap(parts,maximum=3,action){let current=[],used=0,count=0;for(const part of parts){for(const c of clean(part.text)){const n=measure(c);if(used+n>inner){add(current,action);current=[];used=0;if(++count>=maximum)return;}current.push({...part,text:c});used+=n;}}if(current.length)add(current,action);}
  const routes=model.routes||[],first=routes[0];
  const asking=!!model.answerable;
  // UI attention is not native agent liveness. Only a confirmed route failure
  // may make this frame red; an unknown source/permission popup cannot.
  const failed=first?.state==='dead',done=first?.state==='done';
  const color=failed?'g_dead':asking?'lvl_y':first?.state==='working'?'g_work':done?'lvl_g':'dim';
  const status=failed?(first.reason||t('unknown','Checking state')):asking?t('answer','Your answer needed'):first?.state==='working'?t('working','Working'):done?t('ready','Result ready'):'';
  const cfg={...defaults,...configuration};
  if(settings){
    add(line(t('settings_title','Hearting shell settings'),'loc_repo'));
    const start=Math.max(0,settings.index-Math.max(0,height-7));
    for(let i=start;i<fields.length&&content.length<height-4;i++){
      const key=fields[i],value=settings.value[key],v=typeof value==='string'?t(value,value):value;
      if(harness==='opencode'&&['side_width','side_min_columns'].includes(key)){
        add(line('  '+t('fixed_width','OpenCode sidebar: fixed 42 columns')));continue;
      }
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
    const m=native.model&&(native.model!==model.model_value||native.effort&&native.effort!==model.effort_value)?line(native.model+(native.effort?' · '+native.effort:''),'loc_repo'):(model.model||line(t('checking','Checking')));
    add(m);
    if(model.warning)add(line(model.warning,'lvl_r'));
    if(status&&side)add(line(status,color));
    if(details){
      add(line(t('details','Session details'),'loc_repo'),'details');
      for(const row of model.status||[])wrap(row,4);
      for(const row of sections?.tail||[])wrap(row,3);
      add(line('Esc · '+t('back','Back')),'details');
    }else if(routeDetail){
      add(line(t('routes','Route')+' · '+t('back','Back'),'loc_repo'),'route');
      for(const route of routes){
        wrap(line(route.title||t('checking','Checking'),'loc_repo'),2);
        for(const stage of route.stages||[])wrap(line((stage.name||stage.id)+' · '+stage.state),2);
        if(route.stage_more)add(line('+'+route.stage_more+' · Fleet ›'),'fleet');
        wrap(line(route.reason||route.state),3);
        if(route.outcome)wrap(line(route.outcome,'lvl_g'),2);
      }
    }else{
      if(side){add(line(t('work','Current work')));wrap(line(model.title||(!sid||model.routes_known?t('no_work','No confirmed work yet'):t('checking','Checking')),'loc_repo'),2);add([]);}
      const routeTitle=first?.title||model.route_label||(model.routes_known?t('no_routes','No current route'):t('checking','Checking'));
      const heading=side&&first?.title===model.title?'⏳ '+t('routes','Route'):
        !first&&model.route_label?model.route_label:'⏳ '+t('routes','Route')+' · '+routeTitle;
      wrap(line(heading,'loc_repo'),side?2:1,'route');
      if(first){
        const flow=(first.stages||[]).map(s=>s.name||s.id).filter(Boolean).join(' → ');
        if(flow&&(side||!focused))wrap(line(flow),side?2:1,'route');
        if(first.stage_more)add(line('+'+first.stage_more+' · Fleet ›'),'fleet');
        if(side||!focused)wrap(line([first.stage!=='·'?first.stage:'',first.reason||first.state].filter(Boolean).join(' · '),color),side?2:1,'route');
      }
      if(routes.length>1)add(line('+'+(routes.length-1)+' · '+t('routes','Route')),'route');
      // Bound optional material first. Question editor gets the remaining rows,
      // and keeps its own overflow/Tab/form/draft/revision handling unchanged.
      const remaining=Math.max(2,height-2-content.length);
      const q=questions({budget:Math.max(2,remaining-2),columns:inner-2});
      if(q.length){
        add(line('╭'+'─'.repeat(inner-2)+'╮',asking?'lvl_y':'dim'),'question');
        for(const row of q.slice(0,Math.max(0,height-3-content.length))){
          const fit=clip(row,inner-2);
          add([text('│',asking?'lvl_y':'dim'),...fit.parts,text(' '.repeat(inner-2-fit.used)),text('│',asking?'lvl_y':'dim')],'question');
        }
        add(line('╰'+'─'.repeat(inner-2)+'╯',asking?'lvl_y':'dim'),'question');
      }
      const result=routes.find(r=>r.outcome);
      if(side&&result&&content.length<height-4){
        add([]);add(line(t('results','Results & connected work')),'details');
        add(line(result.outcome,'lvl_g'),'details');
      }
      const statusRows=model.status||[];
      // Existing native fields remain secondary, but when they all fit they
      // are actually painted (required before concealing a managed footer).
      if(harness!=='claude'&&statusRows.length&&statusRows.length<=height-2-content.length){
        for(const row of statusRows)add(row,'details');
      }else if(cfg.auxiliary==='detail')for(const row of statusRows){if(content.length>=height-3)break;wrap(row,1,'details');}
      if(content.length<height-2&&typeof native.context_percent==='number'){
        const pct=native.context_percent;
        add(line(`context ${pct}%`),'details');
      }
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
  const foot=clip(line(' '+(settings?'Esc · '+t('cancel','Cancel'):t('details','Session details')+' ›')+' '),width-4);
  rows.push([text('└─',color),...foot.parts,text('─'.repeat(Math.max(0,width-3-foot.used))+'┘',color)]);
  if(!settings)hits.push({row:count-1,left:1,right:width-2,action:'details'});
  return {rows,hits:hits.filter(h=>h.row<count),color};
}
module.exports={render,defaults,fields,change};
