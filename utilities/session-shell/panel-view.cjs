'use strict';
// Geometry and paint only. The existing question controller owns drafts/effects.
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14,
  position:'right',theme:'terminal',shade:'none',route_line:'time',rule_motion:'on'};
// Panel color schemes (spec rule 22: the reviewed set A, the question rose one
// step deeper). `terminal` is the terminal's own background with the shared
// palette; `light` declares a light terminal; `mono` drops hue and keeps weight,
// glyphs and words. Text and glyphs always carry the state, so no scheme makes
// color the only signal. A face is the background the panel stands on: `light`
// for the light theme, else the terminal's background when it is known
// (COLORFGBG or the terminal's own reply to a native's query; nothing is asked).
const FACES={
  dark:{light:234,medium:235,body:253,secondary:248,title:246,frame:244,route:245,messages:242,accent:174,
    working:150,idle:186,blocked:174,dead:210,done:150,steward:[219,132],
    // Spec rule 47: the moving rule's tones. The running light's five steps, dimmest first (the
    // session spinner's vanilla at the top, just above the dark rule at the bottom); a waiting
    // rule's steps of red, brightest first (the frame's waiting red) in 256 colors, and its two
    // ends in 24-bit color.
    scan:[243,101,144,187,230],wait:[217,210,174,167,131,95],waitRGB:[[255,175,175],[90,56,56]]},
  light:{light:255,medium:254,body:237,secondary:241,title:241,frame:243,route:243,messages:245,accent:131,
    working:22,idle:58,blocked:131,dead:88,done:22,steward:[132,175],
    scan:[251,151,108,65,22],wait:[88,124,131,167,174,181],waitRGB:[[135,0,0],[225,190,190]]},
};
// Fleet's 256-color hues on each face. The light face takes the reviewed deep
// counterparts (running 22, attention 58, failure 88, project 23, model 60)
// instead of the earlier strong substitutes; the dark face changes only the
// failure red, and only on a shade.
const HUES={dark:{217:210},light:{253:237,252:238,150:22,186:58,217:88,116:23,176:60,147:25,230:22,219:132,137:94}};
const LINES=new Set(['frame','route','messages']);
function face(theme,background){return theme==='light'?'light':background==='light'||background==='dark'?background:'';}
// A moving rule cell's color: a light step from its face's ramp; a waiting step from its face's
// 256-color reds, or in 24-bit color (when the terminal says it takes it) a point on the cosine
// between its face's two ends.
function ruleTone(part,F,colors){
  if(part.tone==='scan')return F.scan[Math.max(0,Math.min(F.scan.length-1,part.level-1))];
  const steps=part.steps,k=part.step;
  if(colors>256&&steps>1){
    const f=(Math.cos(Math.PI*k/(steps-1))+1)/2,[hi,lo]=F.waitRGB;
    return '#'+hi.map((c,i)=>Math.round(lo[i]+(c-lo[i])*f).toString(16).padStart(2,'0')).join('');
  }
  return F.wait[Math.max(0,Math.min(F.wait.length-1,k))];
}
function appearance(rows,configuration={},{colors=256,noColor=false,background=''}={}){
  const cfg={...defaults,...configuration},theme=['terminal','light','mono'].includes(cfg.theme)?cfg.theme:'terminal';
  if(noColor)return rows;
  const known=face(theme,background),full=colors>=256;
  // A shade needs a known face: on an unknown background the panel keeps the terminal's own.
  const shade=['light','medium'].includes(cfg.shade)&&full&&known?cfg.shade:'';
  const F=FACES[known||'dark'];
  return rows.map(row=>row.map(part=>{
    let foreground=Number.isInteger(part.foreground)&&part.foreground>=0||/^#[0-9a-f]{6}$/i.test(String(part.foreground))?part.foreground:null,dim=part.dim===true,bold=part.bold===true;
    // Spec rule 25: a frame or route box in a state color reads as the plain line where no hue is shown.
    const role=theme==='mono'&&part.role==='frame-state'?'frame':theme==='mono'&&part.role==='route-dead'?'route':part.role||'';
    // (Box lines never turn bold: weight changes a line's stroke and, in some terminals, its width.)
    if(role==='route-dead')bold=false;
    // Spec rule 50 (d): Git's `↑N`/`↓N` keep Fleet's green and red only where the panel shows
    // hue; in mono and eight colors they read as the rest of the Git words, dim.
    if(role==='git-fresh'&&(theme==='mono'||!full)){foreground=null;bold=false;dim=true;}
    if(!full){
      // Eight colors: the light theme leaves the pale default white to the terminal.
      if(theme==='mono')foreground=null;
      else if(theme==='light'&&foreground===7)foreground=null;
      return {...part,foreground,bold,...(role==='git-fresh'?{dim}:{})};
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
    // Spec rule 25: a running or waiting session's frame keeps its state word's color
    // (Fleet's session key, the light face's deep counterpart) at full strength.
    else if(role==='frame-state')dim=false;
    // A failed route's box line: its face's failure color, at full strength.
    else if(role==='route-dead'){if(known)foreground=F.dead;dim=false;}
    // The steward mark's two beats on a known face: Fleet's pink, then a receding pink
    // (no dim on top, so a shade keeps the hue); the light face takes the deep pair.
    // The steward's number does not blink (spec rule 35 (a)): it keeps the lit pink.
    else if(role==='steward'&&theme!=='mono'&&known){foreground=F.steward[part.beat===true&&!bold?1:0];dim=false;}
    else if(role==='steward'&&theme==='mono')dim=false;
    // Spec rule 47: the moving rule's cells take their step's tone on their face (an unknown face
    // the dark face's), at full strength; mono never moves (no tone).
    else if(role==='rule'&&theme!=='mono'&&part.tone){foreground=ruleTone(part,FACES[known||'dark'],colors);dim=false;}
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
// Spec rule 49 (d): the narrow-width judgment is one constant (native-display's NATIVE_MIN), so
// the editor has no `threshold`; `side_min_columns` stays in `defaults` only so a saved policy
// keeps its keys.
const fields=['layout','position','side_width','narrow','auxiliary','bottom_rows','theme','shade','route_line','rule_motion'];
const names=['layout','position','width','narrow','auxiliary','height','theme','shade','route_line','rule_motion'];
const OPTIONS={layout:['side','bottom'],position:['right','left'],narrow:['bottom','compact'],auxiliary:['summary','detail'],
  theme:['terminal','light','mono'],shade:['none','light','medium'],route_line:['time','model'],rule_motion:['on','off']};
const APPEARANCE_KEYS=['position','theme','shade'];
// The session's own state, as Fleet's session row judges and draws it
// (render_cells `_LIVE_GLYPH`/`_state_key`): a working session spins.
const SPIN='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
// Spec rule 26: an answer being typed in for the person reads as running. Spec rule 29:
// the state is told by glyph and color alone (no `running`/`idle`/`waiting` word); the
// three glyphs differ in shape, so mono and NO_COLOR still tell them apart.
const LIVE={working:['spin','g_spin'],submitting:['spin','g_spin'],idle:['●','g_work_off'],blocked:['◑','g_blocked']};
// Spec rule 47 (the user's final values, 11-13), as rules 51 and 52 retune it: every value of the
// moving rule in one place. The rule is the thin `─` in every state (no heavy cell anywhere on it).
// Running, a light passes over it: RULE_PEAK cells at its middle are brightest and RULE_FADE cells
// on each side step down one shade each (RULE_PEAK + 2 RULE_FADE cells, five shades); its middle
// runs at one speed from fully outside one end to fully outside the other and back in RULE_SCAN
// spinner frames (100 ms each: 26 = 2.6 s). Having left on the side it came in from, the dark rule
// rests RULE_REST frames (10 = 1 s, the light's own turn beat included) and the light comes in
// again on that side; at the far end it turns after one dark beat. Waiting, the whole red rule
// breathes once every RULE_BREATHE frames (36 = 3.6 s) through RULE_WAIT_STEPS shades on a cosine
// (24-bit color; the 256-color palette has six reds for it), each shade an equal time.
const RULE_PEAK=5,RULE_FADE=4,RULE_SCAN=26,RULE_REST=10,RULE_BREATHE=36,RULE_WAIT_STEPS=8,RULE_WAIT_STEPS_256=6;
const RULE_CYCLE=RULE_SCAN+RULE_REST-1;
const mod=(t,n)=>((Math.trunc(t)%n)+n)%n;
// Where the light's middle stands at a frame on an `n`-cell rule, or null while the dark rule rests.
function scanAt(tick,n){
  const plateau=(RULE_PEAK-1)/2,lo=-(RULE_FADE+plateau)-1,hi=n+RULE_FADE+plateau,q=mod(tick,RULE_CYCLE);
  if(q>=RULE_SCAN)return null;
  // (The middle stands on a whole cell, so the brightest run is always RULE_PEAK cells.)
  const y=q<RULE_SCAN/2?q/(RULE_SCAN/2):2-q/(RULE_SCAN/2);
  return Math.round(lo+y*(hi-lo));
}
// The light's shade at cell `i` of an `n`-cell rule (5 brightest, 0 none).
function scanLevel(tick,i,n){
  const plateau=(RULE_PEAK-1)/2,c=scanAt(tick,n);
  return c===null?0:Math.max(0,RULE_FADE+1-Math.max(0,Math.abs(i-c)-plateau));
}
// The next frame after `tick` at which an `n`-cell running rule looks different: the next frame
// while the light is inside, the light's return at the end of the rest (nothing to wake for or
// write before it).
function scanNext(tick,n){
  const t=Math.trunc(tick),lit=f=>{const c=scanAt(f,n),plateau=(RULE_PEAK-1)/2,reach=RULE_FADE+plateau;return c!==null&&c+reach>=0&&c-reach<n;};
  if(lit(t))return t+1;
  for(let f=t+1;f<=t+RULE_CYCLE;f++)if(lit(f))return f;
  return t+1;
}
// A breath's shade at a frame: 0 brightest .. steps-1 darkest, down and back up, each shade for an
// equal stretch of the breath (2 (steps-1) stretches).
function breathStep(tick,steps){
  const slots=2*(steps-1),s=Math.floor(mod(tick,RULE_BREATHE)*slots/RULE_BREATHE);
  return s<steps?s:slots-s;
}
// One cell `i` of an `n`-cell rule at spinner frame `tick`, always the thin `─` (spec rule 52):
// running (`scan`), the light's shade (0: the dark rule); waiting (`wait`), red at the breath's
// shade; `still` moves nothing (rule motion off, mono, NO_COLOR, eight colors): a running rule
// stays the plain one, a waiting one stays red. Anything else is the plain rule.
function ruleCell(kind,tick,i,n,{still=false,steps=RULE_WAIT_STEPS}={}){
  if(kind==='wait')return still?['─','wait',0]:['─','wait',breathStep(tick,steps)];
  if(kind==='scan'&&!still){const level=scanLevel(tick,i,n);return ['─',level?'scan':'base',level];}
  return ['─','line',0];
}
function change(value,key,step){
  const options=OPTIONS;
  if(options[key]){const list=options[key];return {...value,[key]:list[(list.indexOf(value[key])+step+list.length)%list.length]};}
  const bounds={side_width:[32,80,1],bottom_rows:[8,20,1],side_min_columns:[80,1000,10]},[lo,hi,n]=bounds[key];
  return {...value,[key]:Math.max(lo,Math.min(hi,value[key]+step*n))};
}
function render({frame=0,width,height,side,fill=side,model={},native={},state,sid,questions,sections,configuration,settings,routeDetail=false,routePage=0,routeScroll=0,attempts=[],expandedKeys=[],focused=false,cwd='',harness='',cellWidth,background='',folded=false,segments=[]}){
  // Spec rule 49: a folded panel is the eight-cell strip beside the native area or one row under it.
  folded=folded===true&&!settings;
  width=Math.max(folded&&side?5:12,width);height=Math.max(folded?side?3:1:4,height);
  const cfg={...defaults,...configuration};
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
  // Spec rule 34 (a): a row breaks only between tokens. A token is a word (text with no
  // space in it, across parts too), a model name with its version (`Sonnet 5.5`,
  // `(Opus 5.5·xhigh)`), or a lone `·`/`›` with the word after it (`· failed`, `› test`).
  // A joined token wider than a row comes apart at its own spaces first.
  function units(parts){
    const out=[];let space=false;
    for(const part of parts)for(const token of clean(part.text).match(/\s+|\S+/gu)||[]){
      if(/^\s+$/.test(token)){space=true;continue;}
      const x={part,text:token,gap:space},last=out.at(-1),prev=last&&last.at(-1);space=false;
      if(prev&&(!x.gap||/^[·›]$/u.test(prev.text)||/^\d+(?:\.\d+)+/u.test(token)&&/[A-Za-z]$/u.test(prev.text)))last.push(x);
      else out.push([x]);
    }
    const sized=[];
    for(const unit of out){
      const size=unit.reduce((n,x,i)=>n+(i&&x.gap?1:0)+measure(x.text),0);
      if(unit.length>1&&size>inner)for(const x of unit)sized.push({tokens:[x],size:measure(x.text),gap:x.gap});
      else sized.push({tokens:unit,size,gap:unit[0].gap});
    }
    return sized;
  }
  function wrap(parts,maximum=3,action,into){
    // `into` collects the rows for a caller that places them after measuring.
    const put=into?row=>into.push(row):row=>add(row,action);
    // Unbounded rows (a kept message body) carry a token wider than a row on to
    // the next rows by character cells; bounded rows keep their one-row `…`.
    const keep=maximum===Infinity;
    let current=[],used=0,count=0;
    for(const {tokens,size,gap:before} of units(parts)){
      const gap=before&&used?1:0,first=tokens[0];
      if(keep&&size>inner){
        if(gap&&used+1<inner){current.push({...first.part,text:' '});used++;}
        else if(gap){put(current);current=[];used=0;}
        let s='';
        for(const g of graphemes(first.text)){
          const w=measure(g);
          if(used&&used+w>inner){if(s)current.push({...first.part,text:s});put(current);current=[];used=0;s='';}
          s+=g;used+=w;
        }
        if(s)current.push({...first.part,text:s});
        continue;
      }
      if(used&&used+gap+size>inner){put(current);current=[];used=0;if(++count>=maximum)return;}
      else if(gap){current.push({...first.part,text:' '});used++;}
      if(size>inner){const fit=clip(tokens.map(x=>({...x.part,text:x.text})),inner-1);current.push(...fit.parts,{...first.part,text:'…'});used=inner;}
      else tokens.forEach((x,i)=>{if(i&&x.gap){current.push({...x.part,text:' '});used++;}current.push({...x.part,text:x.text});used+=measure(x.text);});
    }
    if(current.length)put(current);
  }
  const routes=model.routes||[];
  // Fleet's shared beat (render_cells `_blink_phase`): one second lit, one second
  // dark, from the same clock as the 100 ms spinner frame.
  const beat=Math.floor(Math.trunc(frame)/10)%2===0;
  const spinGlyph={text:SPIN[Math.abs(Math.trunc(frame))%SPIN.length],spin:true};
  // What runs here blinks or spins: the host keeps its beat only while one is
  // painted. A blinking part carries `beat`, a spinning one `spin`; the beat is
  // read from the rows this paint finally shows, so a route the height dropped
  // keeps nothing turning.
  const pulse=(value)=>({...text(value,beat?'g_work':'g_work_off'),beat:true});
  if(folded)return foldedView();
  // Spec rule 49 (c): folded beside the native area the panel is a strip of eight cells (two
  // borders, a blank cell inside each, four content cells), its border in this session's state
  // color (the open panel's frame source). Inside, from the top: the state glyph (spinning
  // while the session runs, `◑` waiting, `●` idle) with the session number (`⠸ e9`), an empty
  // row, the route count (`2 RT`, from ten `9+RT`), the answer waits (`◑ 1`, from ten `◑ 9+`),
  // and the unfold mark on the last row, where the open panel's fold button stands. An absent
  // value draws no row; short of rows the empty row goes first, then the route count, the
  // waits; the glyph row and the unfold mark stay. No steward mark, gear or moving rule: only the state glyph turns. A
  // press anywhere on it opens the panel. Spec rule 49 (e): folded under the native area,
  // one row: the session's status line and `▴` at its right end.
  function foldedView(){
    const own=LIVE[state],color=own&&own[1]!=='g_work_off'?own[1]:'dim';
    const frameText=v=>({...text(v,color),role:color==='dim'?'frame':'frame-state'});
    const plain=model.no_color===true||cfg.theme==='mono';
    const out=[],hits=[];
    if(side){
      // The strip's width is native-display's STRIP (8): one blank cell inside each border and
      // four content cells between them.
      const content=Math.max(1,width-4);
      const tag=/^\[[^\]?]{1,16}\]$/.test(String(model.tag||''))?clean(String(model.tag).slice(1,-1)):'';
      const cell=parts=>{const fit=clip(parts,content);return [text(' '),...fit.parts,text(' '.repeat(Math.max(0,content-fit.used)+1))];};
      // The glyph keeps its cell while the state is unknown, so the number does not move.
      const glyph=own?{...text(own[0]==='spin'?spinGlyph.text:own[0],own[1]),...(own[0]==='spin'?{spin:true}:{}),role:'session'}:text(' ');
      const number=tag?[text(' '),model.steward===true?{...text(tag,'tag_steward'),role:'steward',...(plain?{bold:false,dim:false}:{})}:{...text(tag,own?own[1]:'dim'),role:'session'}]:[];
      const total=Number.isInteger(model.route_total)&&model.route_total>0?model.route_total:Number.isInteger(model.route_count)&&model.route_count>0?model.route_count:0;
      const waiting=typeof model.wait==='string'&&model.wait.trim()?/(\d+)\s*$/.exec(model.wait):null;
      const waits=waiting?Number(waiting[1]):typeof model.wait==='string'&&model.wait.trim()?-1:0;
      const items=[{rank:5,parts:[glyph,...number]}];
      if(total||waits){
        items.push({rank:0,parts:[]});
        if(total)items.push({rank:3,parts:total>=10?[text('9+','loc_repo'),text('RT','dim')]:[text(String(total),'loc_repo'),text(' RT','dim')]});
        // An answer wait whose count is unknown shows its glyph alone (spec rule 12).
        if(waits)items.push({rank:4,parts:[text('◑','g_blocked'),...(waits<0?[]:[text(' '+(waits>=10?'9+':String(waits)),'g_blocked')])]});
      }
      const room=Math.max(0,height-3);
      while(items.length>room){let low=0;items.forEach((item,i)=>{if(item.rank<items[low].rank)low=i;});items.splice(low,1);}
      const rule='─'.repeat(Math.max(0,width-2));
      out.push([frameText('┌'+rule+'┐')]);
      for(let i=0;i<height-2;i++){
        const last=i===height-3,parts=last?[text(' '),text(cfg.position==='left'?'»':'«','dim')]:items[i]?.parts||[];
        out.push([frameText('│'),...cell(parts),frameText('│')]);
      }
      out.push([frameText('└'+rule+'┘')]);
      for(let i=0;i<out.length;i++)hits.push({row:i,left:0,right:width-1,action:'unfold'});
    }else{
      const line=(Array.isArray(segments)?segments:[]).filter(part=>part&&typeof part==='object');
      const fit=clip(line,Math.max(0,width-2));
      const body=fit.used<line.reduce((n,p)=>n+measure(clean(p.text)),0)?[...clip(line,Math.max(0,width-3)).parts,text('…','dim')]:fit.parts;
      const used=body.reduce((n,p)=>n+measure(clean(p.text)),0);
      out.push([...body,text(' '.repeat(Math.max(0,width-1-used))),text('▴','dim')]);
      hits.push({row:0,left:0,right:width-1,action:'unfold'});
    }
    const painted=appearance(out,side?cfg:{...cfg,shade:'none'},{colors:model.colors??256,noColor:model.no_color===true,background});
    const spins=out.map((row,i)=>row.some(part=>part.spin)?i:-1).filter(i=>i>=0);
    return {rows:painted,hits,color,routePages:1,routeDetail,routeArea:null,groups:[],animated:spins.length>0,spins,motion:null,folded:true};
  }
  // Spec rule 49 (a), (b), (e): the panel's last row inside carries the gear `⚙` (dim) at its
  // right end and, three cells left of it, the fold button: `»` beside the native area (`«`
  // for a left panel), `▾` in the bottom layout. Their presses stay apart (the cell between
  // them is neither's). The row stays however short rows run, so the panel can always fold;
  // whatever shares it (5h/7d) takes the cells left of the buttons.
  const foldMark=cfg.layout==='side'?cfg.position==='left'?'«':'»':'▾';
  const BUTTONS=5;
  function footRow(left=[]){
    const at=content.length+1,room=Math.max(0,width-4-BUTTONS-1);
    const all=left.reduce((n,p)=>n+measure(clean(p.text)),0);
    const kept=all<=room?left:room>=4?[...clip(left,room-1).parts,{...(left.at(-1)||text('')),text:'…'}]:[];
    const used=kept.reduce((n,p)=>n+measure(clean(p.text)),0);
    hits.push({row:at,left:width-8,right:width-6,action:'fold'});
    hits.push({row:at,left:width-4,right:width-2,action:'settings'});
    return [...kept,text(' '.repeat(Math.max(0,width-4-BUTTONS-used))),text(foldMark,'dim'),text('   '),text('⚙','dim')];
  }
  // The last row placed; beside the native area it stands on the panel's last row inside.
  // Rows past the panel's own give way to it: the head's last rows first (the first head row
  // stays, and a box below keeps both its borders), then whatever runs past the end.
  function lastRow(left,groups,head=0){
    const rows=height-2;
    for(let over=content.length-(rows-1);over>0&&head>1;over--){
      const at=--head;content.splice(at,1);contentGroups.splice(at,1);
      for(let i=hits.length-1;i>=0;i--){if(hits[i].row===at+1)hits.splice(i,1);else if(hits[i].row>at+1)hits[i].row--;}
      if(routeArea&&routeArea.top>at+1){routeArea={...routeArea,top:routeArea.top-1,bottom:routeArea.bottom-1};}
    }
    if(content.length>rows-1){content.length=rows-1;contentGroups.length=rows-1;for(let i=hits.length-1;i>=0;i--)if(hits[i].row>rows-1)hits.splice(i,1);}
    if(fill)while(content.length<rows-1)add([]);
    add(footRow(left),undefined,groups);
  }
  // Spec rule 47: the thin rule under the metadata follows this session's own state, the
  // frame's source (not a route's): running moves a light along it, an answer or confirmation
  // wait breathes it red, anything else is the plain rule; it stays the thin rule throughout
  // (spec rule 52). Rule motion off, mono and NO_COLOR move nothing (a wait stays a still red
  // rule). A moving rule's parts carry `motion`: the host repaints that one row, its changed
  // cells only, at the frames it changes.
  const ruleKind=settings?'':state==='blocked'?'wait':LIVE[state]?.[0]==='spin'?'scan':'';
  const ruleColors=model.colors??256;
  const ruleStill=cfg.rule_motion==='off'||model.no_color===true||cfg.theme==='mono'||ruleColors<256;
  const ruleSteps=ruleColors>256?RULE_WAIT_STEPS:RULE_WAIT_STEPS_256;
  const ruleWidth=inner;
  function ruleParts(tick){
    const flag=ruleStill||!ruleKind?{}:{motion:true},out=[];
    let run='',key='',cell=null;
    const flush=()=>{if(!run)return;const [,tone,level]=cell;
      out.push(tone==='line'||tone==='base'?{...text(run,'dim'),role:'messages',...flag}:
        tone==='wait'?{...text(run,'g_blocked'),role:'rule',...(ruleStill?{}:{tone,step:level,steps:ruleSteps}),...flag}:
        {...text(run,'g_spin'),role:'rule',tone,level,...flag});};
    for(let i=0;i<ruleWidth;i++){const c=ruleCell(ruleKind,tick,i,ruleWidth,{still:ruleStill,steps:ruleSteps}),k=c.join(':');if(k!==key){flush();run='';key=k;cell=c;}run+=c[0];}
    flush();
    return out;
  }
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
    // With it, what the tool acts on and for how long, or that none is open; then the
    // call it just finished (Fleet's unfolded `>_ tool · target  3m` and `after …` rows).
    const doing=join([r.tool?[text('>_ ','lvl_g'),text(r.tool,'loc_repo'),...(r.target?[text(' · '+r.target)]:[]),...(r.tool_age?[text('  '+r.tool_age,'dim')]:[])]:
      r.idle_tool?[text('>_ ','dim'),text('no tool','dim')]:[],r.agents?[text('+'+r.agents+' agent')]:[]]);
    if(doing.length&&(stages.length||live(r)))items.push(doing);
    if(r.after_tool)items.push({one:true,parts:[text('after '+r.after_tool,'dim')]});
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
      const rows=[];
      // The finished call (`one`) keeps one row, cut with `…`, as Fleet draws it.
      if(item.one){const room=inner-first.length,all=parts.reduce((n,p)=>n+measure(clean(p.text)),0);
        rows.push(all<=room?parts:[...clip(parts,Math.max(0,room-1)).parts,text('…','dim')]);}
      else{inner-=rest.length;try{wrap(parts,Infinity,undefined,rows);}finally{inner+=rest.length;}}
      rows.forEach((row,i)=>out.push([text(i?rest:first),...row]));
    }
    return out;
  }
  const asking=!!model.answerable;
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
    // Spec rule 45 (c): beside the native area the page's word is upper case, as its section's head.
    const word=reading?'messages':t('routes','route');
    add(line((side?word.toUpperCase():word)+' · '+t('back','Back'),'loc_repo'),'route');
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
    // A newer release waiting for this page to close says so in the page's head, one row under the
    // page counter (the panel's notice slot is not drawn while the page is open).
    const waiting=typeof model.upgrade_waiting==='string'?clean(model.upgrade_waiting).trim():'';
    const detail=content.splice(start),budget=Math.max(1,height-4-start-(waiting?1:0));
    routePages=Math.max(1,Math.ceil(detail.length/budget));
    const page=Math.max(0,Math.min(routePages-1,routePage));
    const counter=` ‹ ${page+1}/${routePages} › `;
    hits.push({row:content.length+1,left:2,right:4,action:'route-prev'});
    hits.push({row:content.length+1,left:5,right:2+measure(counter),action:'route-next'});
    add(line(counter+' · PgUp/PgDn'));
    if(waiting)add(line(waiting));
    for(const row of detail.slice(page*budget,(page+1)*budget))add(row);
    lastRow([],[]);
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
    // `route N` (beside the native area `N ROUTE`, spec rule 45 (a)) shows whole or not at
    // all, never as a shorter number.
    const counted=words=>{if(words.length>1&&(words[0]==='route'||/^\d+$/.test(words[0])&&words[1]==='ROUTE'))words.splice(0,2,words[0]+' '+words[1]);return words;};
    const fitted=label=>{
      if(size([text('── '),...label,text(' ')])<=inner)return [text('── '),...label,text(' ')];
      const all=label.map(p=>clean(p.text)).join(''),words=counted(all.split(' '));
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
    // The native's own model name takes the color the backend gave it (its family's, else the
    // harness's, as route rows); until that arrives for a new name, the harness's color.
    const nativePart=model.native_model_part&&model.native_model_part.text===native.model?{...model.native_model_part,text:native.model}:
      text(native.model,['claude','codex','opencode'].includes(harness)?'hb_'+harness:'loc_repo');
    const selected=nativeModel?[nativePart,...(native.effort?[text(' '+native.effort)]:[])]:
      painted(model.selected).length?model.selected:line(t('checking','Checking'));
    // Spec rule 15: the repo row carries the project only; Git takes the row below. Spec rule
    // 29: the session number moves to the top border, so the row keeps `hearting/`.
    let head=painted(model.head),git=painted(model.git);
    if(!Array.isArray(model.git)){const at=head.findIndex(p=>/^ \(/.test(String(p.text)));if(at>=0){git=[{...head[at],text:String(head[at].text).slice(1)},...head.slice(at+1)];head=head.slice(0,at);}}
    const repo=Array.isArray(model.repo)?painted(model.repo):head.filter(p=>!/^\[[^\]]*\] ?$/.test(String(p.text)));
    const jobs=painted(model.jobs),work=painted(model.work),quota=painted(model.quota),mcp=painted(model.mcp);
    // OpenCode's MCP count stays beside the model and context (its native sidebar is hidden).
    const contextParts=mcp.length?joined([painted(model.context),mcp]):painted(model.context);
    const waitParts=model.wait?[text(model.wait,'g_blocked')]:[];
    // Spec rule 29 (b): the session's own content, Fleet's TITLE and NOW (rule 28's value;
    // bright while the session works or waits, as Fleet's NOW cell).
    const title=clean(model.session_title||'').trim(),now=clean(model.now||'').trim();
    const nowKey=state==='working'||state==='submitting'||state==='blocked'?'loc_repo':'dim';
    const tidy=typeof model.tidy_notice==='string'?clean(model.tidy_notice).trim():'';
    const top=[],spacers=new Set(),metaGaps=new Set(),rules=new Set(),optional=new Set(),gitRows=new Set(),jobRows=new Set(),topGroups=new Map();
    // A value that runs over several rows (spec rule 29 (c)): its rows after the first go
    // first when rows run short, and the first row then ends in `…`.
    const extra=new Set(),oneRow=new Map();
    const mark=(from,...names)=>{for(let i=from;i<top.length;i++)topGroups.set(i,[...(topGroups.get(i)||[]),...names]);};
    // The Git row is cut within its own row; short of rows, it is the first to go.
    const putGit=(parts=git)=>{if(parts.length){gitRows.add(top.length);top.push(cut(parts,inner));mark(top.length-1,'git');}};
    const put=(rows,parts,max)=>{const out=[];wrap(parts,max,undefined,out);rows.push(...out);};
    // Every row a value takes at `width` cells, broken at word boundaries (a word wider
    // than a row goes on by character cells).
    const wrapAll=(parts,width)=>{const out=[],was=inner;inner=Math.max(1,width);try{wrap(parts,Infinity,undefined,out);}finally{inner=was;}return out.length?out:[[]];};
    // Spec rule 30 (b), (d): a content row's label (dim, Fleet's word) stands in one
    // column and every value starts at the same cell; its next rows start there too.
    // Narrower than 30 cells inside, the labels go before any value is cut.
    const labelWidth=inner>=30?7:0;
    // A row that ends a value cut short: its last cell becomes `…` (spec rule 36 (c), 37 (b)).
    const ellipsis=(row,limit)=>{const all=size(row);if(all+1<=limit)return [...row,{...(row.at(-1)||text('')),text:'…'}];
      const fit=clip(row,Math.max(0,limit-1));return [...fit.parts,{...(fit.parts.at(-1)||text('')),text:'…'}];};
    const field=(label,parts,max=Infinity)=>{
      const lead=labelWidth?text(label.padEnd(labelWidth),'dim'):null,rest=pad(labelWidth);
      let rows=wrapAll(parts,inner-labelWidth);const from=top.length;
      if(rows.length>max){rows=rows.slice(0,max);rows[max-1]=ellipsis(rows[max-1],inner-labelWidth);}
      rows.forEach((row,i)=>{top.push([...(lead?[i?rest:lead]:[]),...row]);if(i)extra.add(top.length-1);});
      if(rows.length>1)oneRow.set(from,[...(lead?[lead]:[]),...cut(parts,inner-labelWidth)]);
    };
    // Spec rules 37 (a), 41: beside the native area the worktree count stands beside the
    // repository name (`REPO   hearting · wt 12`) and the Git row keeps the branch, whether it
    // is up to date and the warnings (`GIT    main ✓`), the labels as TITLE's and NOW's.
    const worktrees=painted(model.worktrees),sideGit=Array.isArray(model.git_side)?painted(model.git_side):git;
    const sideRepo=Array.isArray(model.repo_side)?painted(model.repo_side):repo;
    const labelled=(label,parts)=>labelWidth?[text(label.padEnd(labelWidth),'dim'),...parts]:parts;
    // Spec rule 46: this repository's newest saved memory records (two at most) stand in their
    // own section under the messages, each `◆ kind · age` over its headline. An older backend
    // sends the newest one as painted pieces only (`3m decision headline`): that one record,
    // without its kind (rule 46 (e)); the head carries no MEM row either way.
    const memItems=(()=>{
      if(!side)return [];
      if(Array.isArray(model.memory_items))return model.memory_items.filter(m=>m&&typeof m==='object'&&clean(m.text).trim()).slice(0,2)
        .map(m=>({kind:clean(m.kind).trim(),age:clean(m.age).trim(),text:clean(m.text).trim()}));
      const old=painted(model.memory),body=old.length?clean(old.at(-1).text).trim():'',first=old.length>1?clean(old[0].text).trim():'';
      return body?[{kind:'',age:/^\d+[mhd](?: \d+[mh])?$/.test(first)?first:'',text:body}]:[];
    })();
    if(side){
      // Spec rule 29 (b), 30 (a): the metadata first, one block (the repository, Git,
      // model and effort, context, jobs, the repo's open work), then one thin rule, then
      // this session's content (title, NOW, the answer wait and a tidy notice). Spec rule
      // 35 (b): inside the metadata one empty row parts [repository, Git] from [model and
      // effort, context]; without Git it parts the repository from the model.
      if(sideRepo.length){
        // The name keeps its color; the count (dim) shows whole or not at all.
        const room=inner-labelWidth,count=worktrees.length?[text(' · ','dim'),...worktrees]:[];
        const withCount=count.length>0&&size(sideRepo)+size(count)<=room;
        top.push(labelled('REPO',withCount?[...sideRepo,...count]:cut(sideRepo,room)));mark(0,'head',...(withCount&&!sideGit.length?['git']:[]));
      }
      // Short of cells the first part (the branch, the model name) gives way so what follows
      // it (the status and a warning, the effort) stays whole.
      const firstGives=(parts,room)=>{
        if(size(parts)<=room||parts.length<2)return cut(parts,room);
        const rest=parts.slice(1),left=room-size(rest);
        return left>=6?[...cut(parts.slice(0,1),left),...rest]:cut(parts,room);
      };
      if(sideGit.length){gitRows.add(top.length);top.push(labelled('GIT',firstGives(sideGit,inner-labelWidth)));mark(top.length-1,'git');}
      if(top.length){metaGaps.add(top.length);top.push([]);}
      if(labelWidth){
        // Spec rule 42: `MODEL` and `CTX` as REPO's and GIT's; `CTX` stands for the word
        // `context` (a backend without `context_side` has its word taken off here).
        const word=parts=>{const first=parts[0],rest=String(first?.text??'').replace(/^context /,'');
          return !first||rest===String(first.text)?parts:rest?[{...first,text:rest},...parts.slice(1)]:parts.slice(1);};
        const gauge=Array.isArray(model.context_side)?painted(model.context_side):word(painted(model.context));
        const value=mcp.length?joined([gauge,mcp]):gauge;
        top.push(labelled('MODEL',firstGives(selected,inner-labelWidth)));
        if(value.length){top.push(labelled('CTX',cut(value,inner-labelWidth)));mark(top.length-1,'context');}
      }else{
        // Narrower than 30 cells inside, the rows as they were, without labels.
        top.push(selected);
        if(contextParts.length){top.push(contextParts);mark(top.length-1,'context');}
      }
      if(jobs.length){const from=top.length;put(top,jobs,2);mark(from,'jobs');for(let i=from;i<top.length;i++){optional.add(i);jobRows.add(i);}}
      if(work.length){optional.add(top.length);top.push(cut(work,inner));}
      if(model.warning)top.push(line(model.warning,'lvl_r'));
      const content=title||now||waitParts.length||(tidy&&!asking);
      // Spec rule 34 (c): the rule is not an empty row; it goes only once TITLE and NOW are down to one row each.
      // Spec rule 47: it follows this session's state (above).
      if(content){rules.add(top.length);top.push(ruleParts(frame));}
      // Spec rule 44: the title's value bold, in this session's harness color (Fleet's `hb_*`;
      // the label stays dim). No color keeps the weight, as the steward's `⚑` (rule 35 (a)).
      const own='hb_'+(['claude','codex','opencode'].includes(harness)?harness:'other');
      if(title)field('TITLE',[{...text(title,own),bold:true}]);
      if(now)field('NOW',line(now,nowKey));
      if(waitParts.length){top.push(waitParts);mark(top.length-1,'wait');}
      if(tidy&&!asking){
        const rows=[];wrap(line('⚠ '+tidy,'lvl_y'),4,undefined,rows);
        const kept=rows.slice(0,3);
        if(rows.length>kept.length){const last=kept.length-1,fit=clip(kept[last],inner-1);kept[last]=[...fit.parts,text('…','lvl_y')];}
        top.push(...kept);
      }
      // One empty row parts the head from the route box below it.
      if(top.length){spacers.add(top.length);top.push([]);}
    }else{
      // The bottom layout has few rows: the metadata row (or rows) first, then one
      // content row; the row break parts them (no rule).
      const first=joined([repo,selected,contextParts]);
      if(size(first)<=inner){top.push(first);mark(0,'head',...(contextParts.length?['context']:[]));}
      else{put(top,joined([repo,selected]),1);mark(0,'head');const from=top.length;put(top,contextParts,1);mark(from,'context');}
      // Running jobs join that row when they fit; otherwise they take a row (below Git) that can give way.
      const joinJobs=jobs.length&&size(top.at(-1))+3+size(jobs)<=inner;
      if(joinJobs){top[top.length-1]=joined([top.at(-1),jobs]);mark(top.length-1,'jobs');}
      putGit();
      if(jobs.length&&!joinJobs){optional.add(top.length);jobRows.add(top.length);top.push(cut(jobs,inner));mark(top.length-1,'jobs');}
      // The repo's open work gives way with the running jobs.
      if(work.length){optional.add(top.length);top.push(cut(work,inner));}
      if(model.warning)top.push(line(model.warning,'lvl_r'));
      if(tidy&&!asking){
        const rows=[];wrap(line('⚠ '+tidy,'lvl_y'),2,undefined,rows);
        const kept=rows.slice(0,1);
        if(rows.length>kept.length){const fit=clip(kept[0],inner-1);kept[0]=[...fit.parts,text('…','lvl_y')];}
        top.push(...kept);
      }
      if(waitParts.length||title||now){
        // Title and NOW share what the answer wait leaves: each keeps at least half of it.
        const nowParts=now?[text('NOW ','dim'),text(now,nowKey)]:[],titleParts=title?line(title,'loc_repo'):[];
        const avail=inner-size(waitParts)-(waitParts.length?3:0)-(title&&now?3:0);
        const titleRoom=title&&now?Math.max(Math.min(size(titleParts),Math.floor(avail/2)),avail-size(nowParts)):avail;
        top.push(cut(joined([waitParts,cut(titleParts,Math.max(1,titleRoom)),cut(nowParts,Math.max(1,avail-(title?Math.min(size(titleParts),titleRoom):0)))]),inner));
        if(waitParts.length)mark(top.length-1,'wait');
      }
    }
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
    // A route whose record names its kind: its stage stands in the kind column, never as
    // the older `execute ✕ 2/3` step text (that stays for a record that names no kind).
    const shaped=r=>{const shape=String(r.shape||'');return shape==='one-shot-owner'||!!shape&&shape!=='unknown'&&(!/^staged/.test(shape)||Array.isArray(r.stages)&&r.stages.length>0);};
    const third=r=>r.tool?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:r.step&&!shaped(r)?[text(r.step)]:[];
    const markOf=stage=>stage.mark==='●'?pulse('●'):stage.mark==='✓'?text('✓','lvl_g'):stage.mark==='✕'?text('✕','g_dead'):text('·','dim');
    // Spec rule 34 (d): the bottom layout's route row names its kind too, in the words and
    // values of the side layout's third row and Fleet's ROUTES (`one-shot`, `staged 3/7
    // execute ●`, a failed one `staged 2/3 execute ✕`); short of cells `3/7 execute ●`.
    // Spec rule 36 (b): the route's kind word leads it (`code · staged 3/7 execute ●`) and goes first.
    function kindCell(r){
      const cell=shapeCell(r),word=clean(r.kind_word||'').trim();
      return cell&&{...cell,kind:word?[text(word,'dim'),text(' · ','dim'),...cell.full]:cell.full};
    }
    function shapeCell(r){
      const shape=String(r.shape||''),stages=Array.isArray(r.stages)?r.stages:[];
      const current=stages.findIndex(stage=>stage.current||stage.mark==='✕'),next=stages.findIndex(stage=>stage.mark!=='✓');
      if(shape==='one-shot-owner'){const word=[text('one-shot','dim')];return {full:word,short:word};}
      if(/^staged/.test(shape)&&stages.length){
        const at=current>=0?current:next>=0?next:stages.length-1,position=(at+1)+'/'+stages.length;
        const stage=[text(stages[at].name||'·','loc_repo'),...(current>=0?[text(' '),markOf(stages[at])]:[])];
        return {full:[text('staged '+position+' ','dim'),...stage],short:[text(position+' ','dim'),...stage]};
      }
      if(shape&&shape!=='unknown'){
        const stage=current>=0?[text(stages[current].name||'·','loc_repo'),text(' '),markOf(stages[current])]:[];
        return {full:[text(shape,'dim'),...(stage.length?[text(' '),...stage]:[])],short:stage.length?stage:[text(shape,'dim')]};
      }
      return r.step?{full:[text(r.step)],short:[text(r.step)]}:null;
    }
    // Which of the kind and tool columns the bottom rows keep, the same for every row: the
    // kind word goes first, then the tool, then the `staged` word; the title keeps ten cells (or its
    // whole width) through both. Short of that the rows keep the earlier columns (title,
    // owner, context, then the tool or the stage).
    let bottomPlan;
    const planBottom=(ow,gw)=>{
      const kinds=listed.map(kindCell),tools=r=>r.tool?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:[];
      const widths=Object.fromEntries(['kind','full','short'].map(level=>[level,Math.max(0,...kinds.map(k=>k?size(k[level]):0))]));
      const tw=columnWidth(tools),longest=columnWidth(x=>line(x.title||'')),avail=inner-(2+3+ow+3+gw+1+elapsedWidth);
      const levels=widths.kind>widths.full?[['kind',true],['full',true],['full',false],['short',false]]:[['full',true],['full',false],['short',false]];
      for(const [level,tool] of levels){
        if(!widths[level])break;
        const room=avail-(3+widths[level])-(tool&&tw?3+tw:0);
        if(room>=Math.min(longest,10))return {level,tool:tool&&tw>0,width:widths[level],title:Math.min(longest,room),kinds,tools};
      }
      return {level:''};
    };
    function folded(r){
      const p=r.paint||{},lead=[routeGlyph(r),text(' ')];
      if(!side){
        const ow=columnWidth(owner),gw=columnWidth(x=>x.paint?.gauge5||[]),tw3=columnWidth(third);
        const plan=bottomPlan||(bottomPlan=planBottom(ow,gw));
        if(plan.level){
          const kind=plan.kinds[listed.indexOf(r)],cell=kind?kind[plan.level]:[],name=cut(line(r.title||'·','loc_repo'),plan.title);
          const parts=[...lead,...name,pad(plan.title-size(name)),text(' │ '),...owner(r),pad(ow-size(owner(r))),text(' │ '),
            ...(p.gauge5||[]),pad(gw-size(p.gauge5||[])),text(' │ '),...cell,...(plan.tool?[pad(plan.width-size(cell)),text(' │ '),...plan.tools(r)]:[])];
          const end=r.elapsed?[text(' '),text(r.elapsed)]:[],left=cut(parts,inner-size(end));
          return [...left,pad(inner-size(left)-size(end)),...end];
        }
        // The title gives way first, but keeps half of what the tool or stage column would take.
        const avail=inner-(2+3+ow+3+gw+3+1+elapsedWidth),titleWidth=Math.min(columnWidth(x=>line(x.title||'')),Math.max(6,avail-tw3,Math.floor(avail/2)));
        const name=cut(line(r.title||'·','loc_repo'),titleWidth);
        const base=[...lead,...name,pad(titleWidth-size(name)),text(' │ '),...owner(r),pad(ow-size(owner(r))),text(' │ '),
          ...(p.gauge5||[]),pad(gw-size(p.gauge5||[]))];
        // The elapsed time keeps the right edge; the tool or stage gives way first, whole (spec rule 30 (d)).
        const end=r.elapsed?[text(' '),text(r.elapsed)]:[],whole=[...base,text(' │ '),...third(r)];
        const left=cut(third(r).length&&size(whole)<=inner-size(end)?whole:base,inner-size(end));
        return [...left,pad(inner-size(left)-size(end)),...end];
      }
      // As in leadRow(): the title gives way first, and each fixed value
      // (harness, model, context, time) shows whole or not at all.
      const H=p.harness?[p.harness]:[],M=p.model_text?[p.model_text]:[],C=p.ctx?[p.ctx]:[];
      const E=elapsedWidth?[text(' '.repeat(elapsedWidth-measure(r.elapsed||''))+(r.elapsed||''))]:[];
      const drops=routeLine==='model'?[[H,M,C],[H,M],[H],[]]:[[H,C,E],[H,E],[E],[]];
      const join=values=>values.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);
      const right=drops.map(join).find(values=>!values.length||inner-2-size(values)-1>=4)||[];
      const room=inner-2-size(right)-(right.length?1:0),name=cut(line(r.title||'·','loc_repo'),Math.max(1,room));
      return cut([...lead,...name,pad(inner-2-size(name)-size(right)),...right],inner);
    }
    // Spec rule 32: an opened route reads like a table. Its rows hang two cells in under
    // the route behind a thin dim guide: OWNER and KIND as label rows, one row per stage
    // in columns (mark, name, verdict, doer model, context, time right-aligned), the
    // running stage's tool and further workers under it, then INFO, NEXT, WHY, RESUME and
    // OUT. A label stands in one dim column and its value starts in one column, its next
    // rows too; a stage column that does not fit goes whole (context, then model, then
    // verdict), and no row is folded. Every value the route carries keeps a place (INFO
    // holds what has no column). Rows carry their item so a cut never splits one.
    function details(r){
      const out=[];let item=0;
      const guide=[text('  '),{...text('┆','dim'),role:'messages'},text(' ')],W=Math.max(8,inner-4);
      const labels=W>=24,L=labels?7:0;
      const push=(parts,sub=false)=>out.push({parts:[...guide,...(sub?[text('  ')]:[]),...parts],item});
      // Spec rule 34 (a): `forms` runs from the whole value to its shortest (effort, then
      // the harness and its brackets, then a kind's brackets go); the first that fits one
      // row is shown, else the shortest breaks between tokens.
      const field=(label,forms,sub=false)=>{
        forms=(Array.isArray(forms[0])?forms:[forms]).filter(parts=>parts.length);
        if(!forms.length)return;
        const width=W-(sub?2:0)-L,parts=forms.find(parts=>size(parts)<=width)||forms.at(-1),rows=wrapAll(parts,width);
        rows.forEach((row,i)=>push([...(L?[text((i?'':label).padEnd(L),'dim')]:[]),...row],sub));item++;
      };
      // `claude (Opus 5.5·xhigh)` → `claude (Opus 5.5)` → `Opus 5.5`, wherever it stands in a value.
      const modelForms=value=>{
        const whole=String(value||''),plain=whole.replace(/\(([^()·]+)·[^()]*\)/gu,'($1)'),bare=plain.replace(/(^|\s)(?:claude|codex|opencode) \(([^()]+)\)/gu,'$1$2');
        return [...new Set([whole,plain,bare])];
      };
      const stages=Array.isArray(r.stages)?r.stages:[],workers=Array.isArray(r.workers)?r.workers:[];
      const owner=String(r.owner||'').replace(/^owner · /,'');
      field('OWNER',modelForms(owner).map(o=>join([owner?[text(o,'loc_repo')]:[],r.owner_ctx?[text(r.owner_ctx,'loc_repo')]:[],r.profile?[text(r.profile,'dim')]:[]])));
      // Who works now, whole (the folded first row may have let its effort or model go):
      // a stage worker, or a doer with no owner row.
      const pd=r.paint||{};
      const doer=(...values)=>join([values.filter(Boolean).flatMap((x,i)=>i?[text(' '),x]:[x]),pd.ctx?[pd.ctx]:[]]);
      if(r.doer_worker||!owner)field('DOER',[doer(pd.harness,pd.model_text,pd.effort),doer(pd.harness,pd.model_text),doer(pd.model_text||pd.harness)]);
      if(r.kind_text)field('KIND',[[text(r.kind_text,'loc_repo')],[text(String(r.kind_text).replace(/\([^()]*\)$/u,'')||r.kind_text,'loc_repo')]]);
      const nameOf=stage=>(stage.name||'·')+(stage.round>1?' R'+stage.round:'');
      const mineOf=stage=>workers.filter(w=>w.stage===stage.name);
      const verdictOf=stage=>stage.verdict&&stage.verdict!=='running'?stage.verdict:'';
      const modelOf=stage=>stage.model||mineOf(stage)[0]?.model||'';
      const ctxOf=stage=>mineOf(stage)[0]?.ctx||'';
      // Spec rule 34 (e): a stage not reached yet, with nothing of its own to show, joins
      // the others on one dim row (`· impl-review › test › report`), broken at `›`; only a
      // stage with values (done, running, failed) takes a row of the table.
      const pending=stage=>!stage.current&&!['●','✓','✕'].includes(stage.mark||'')&&/^(|pending)$/i.test(verdictOf(stage))
        &&!stage.out&&!stage.note&&!stage.says&&!mineOf(stage).length;
      const table=stages.filter(stage=>!pending(stage));
      const widest=(pick,cap)=>Math.min(cap,Math.max(0,...table.map(stage=>measure(pick(stage)))));
      const nameW=widest(nameOf,16),vW=widest(verdictOf,9),mW=widest(modelOf,14),cW=widest(ctxOf,6),eW=widest(x=>x.elapsed||'',8);
      const width=(v,m,c)=>2+nameW+(v&&vW?1+vW:0)+(m&&mW?1+mW:0)+(c&&cW?1+cW:0)+(eW?1+eW:0);
      const [V,M,C]=[[1,1,1],[1,1,0],[1,0,0],[0,0,0]].find(([v,m,c])=>width(v,m,c)<=W)||[0,0,0];
      const used=new Set();
      const sub=parts=>push(parts,true);
      const doing=()=>{
        // The open call is whole (its next rows start under it); the finished one keeps one row.
        // Spec rule 34 (a): as the folded third row, the path gives way to its file name,
        // then the tool stands alone, before the call is broken over rows.
        if(r.tool){
          const tool=[text('>_ ','lvl_g'),text(r.tool,'loc_repo')],base=String(r.target||'').replace(/\/+$/u,'').split('/').pop();
          const age=r.tool_age?[text('  '+r.tool_age,'dim')]:[],agents=r.agents?[text(' · +'+r.agents+' agent')]:[];
          const forms=[[...tool,...(r.target?[text(' · '+r.target)]:[]),...age,...agents],[...tool,...(base?[text(' · '+base)]:[]),...age,...agents],
            [...tool,...(base?[text(' · '+base)]:[]),...agents],[...tool,...agents],tool];
          wrapAll(forms.find(parts=>size(parts)<=W-2)||tool,W-2).forEach(row=>sub(row));
        }
        else if(r.idle_tool)sub([text('>_ ','dim'),text('no tool','dim'),...(r.agents?[text(' · +'+r.agents+' agent')]:[])]);
        else if(r.agents)sub([text('+'+r.agents+' agent')]);
        if(r.after_tool)sub(cut([text('after '+r.after_tool,'dim')],W-2));
      };
      let doingShown=false;
      const extras=[];
      let waiting=[];
      const later=()=>{
        if(!waiting.length)return;
        const names=waiting.flatMap((stage,i)=>i?[text(' › ','dim'),text(nameOf(stage),'dim')]:[text(nameOf(stage),'dim')]);
        wrapAll(names,W-2).forEach((row,i)=>push([...(i?[text('  ')]:[text('·','dim'),text(' ')]),...row]));item++;
        waiting=[];
      };
      stages.forEach(stage=>{
        const more=[stage.when,stage.unit,stage.gate?'gate ✓':''].filter(Boolean);
        if(more.length)extras.push((stage.name||'·')+' '+more.join(' '));
        if(pending(stage)){waiting.push(stage);return;}
        later();
        const mark=stage.mark||'',current=!!stage.current,done=mark==='✓',failed=mark==='✕';
        const markPart=mark==='●'?pulse('●'):done?text('✓','lvl_g'):failed?text('✕','g_dead'):text('·','dim');
        const tone=current?'loc_repo':failed?'g_dead':'dim';
        const name=cut([text(nameOf(stage),tone)],nameW),nameCell=current?[{...pulse(clean(name.map(x=>x.text).join(''))),beat:true}]:name;
        const verdict=verdictOf(stage),key=/^(PASS|done)$/i.test(verdict)?'lvl_g':/^(FAIL|BLOCKED)/i.test(verdict)?'g_dead':'dim';
        const cells=[markPart,text(' '),...nameCell,pad(nameW-size(name)),
          ...(V&&vW?[text(' '),text(verdict,key),pad(vW-measure(verdict))]:[]),
          ...(M&&mW?[text(' '),...cut([text(modelOf(stage),current?'loc_repo':'dim')],mW),pad(mW-Math.min(mW,measure(modelOf(stage))))]:[]),
          ...(C&&cW?[text(' '),pad(cW-measure(ctxOf(stage))),text(ctxOf(stage),current?'loc_repo':'dim')]:[])];
        const end=eW&&stage.elapsed?[text(stage.elapsed,current?'loc_repo':'dim')]:[];
        push([...cells,pad(W-size(cells)-size(end)),...end]);item++;
        const mine=mineOf(stage);mine.forEach(w=>used.add(w));
        if(current&&!doingShown){doing();doingShown=true;item++;}
        // More than one live worker on a stage: each its own row (Fleet's `└ stage · n`).
        mine.slice(1).forEach((w,n)=>{sub([text('└ '+(n+2)+' ','dim'),text([w.harness,w.model].filter(Boolean).join(' ')),...(w.ctx?[text(' '),text(w.ctx,'loc_repo')]:[])]);});
        if(mine.length>1)item++;
        if(stage.out)field('OUT',[text(stage.out)],true);
        for(const note of [stage.note,stage.says])if(note)field('NOTE',[text(note)],true);
      });
      later();
      if(!doingShown&&(r.tool||r.idle_tool||r.agents||r.after_tool)){doing();item++;}
      for(const w of workers)if(!used.has(w)){sub([text('└ '+w.stage+' ','dim'),text([w.harness,w.model].filter(Boolean).join(' ')),...(w.ctx?[text(' '),text(w.ctx,'loc_repo')]:[])]);item++;}
      // An older feed row carries only the stage line (`plan ✓ › execute ● › review`).
      if(!stages.length&&r.flow){push([text(r.flow)]);item++;}
      field('INFO',join([r.age?[text(r.age)]:[],r.legs?[text(r.legs+' leg'+(r.legs===1?'':'s'))]:[],r.visibility?.summary?[text(r.visibility.summary)]:[],
        r.after?[text(r.after,'dim')]:[],...extras.map(x=>[text(x,'dim')])]));
      if(r.visibility?.next)field('NEXT',[text(r.visibility.next)]);
      if(!live(r)&&(r.reason||r.state_label))field('WHY',modelForms(r.reason||r.state_label).map(value=>[text(value,r.kind==='dead'?'g_dead':'loc_repo')]));
      if(r.resume)field('RESUME',[text(r.resume,'loc_repo')]);
      if(r.outcome)field('OUT',[text(r.outcome,'lvl_g')]);
      if(r.stage_more){push([text('+'+r.stage_more+' · Fleet','dim')]);item++;}
      if(!out.length)push([text(t('checking','Checking'))]);
      return out;
    }
    // Spec rule 14: every group is one box of the same thin rounded line; the
    // question box is the person's and sits at the bottom. Strength of color
    // tells the boxes apart: question the answer-needed color, route plain,
    // messages dim, all from the panel's own styles.
    // Spec rule 22: each line keeps its own role, so a shade colors it without a dim on top;
    // the question's border is rose.
    // Spec rule 25: a route's state stays inside the route box. A failed current route
    // turns the box line to the failure color; a running one pulses `route N` (below).
    const failedRoute=!stale&&shown.some(r=>r.kind==='dead');
    const tone={route:failedRoute?{key:'g_dead',role:'route-dead'}:{key:'plain',role:'route'},messages:'dim',question:{key:asking?'lvl_y':'dim',role:'accent-line'}};
    const L=(v,k)=>typeof k==='object'?{...text(v,k.key),role:k.role}:text(v,k);
    const box=4;
    // Box content is laid out at the box's inner width, so nothing is cut by the frame.
    const within=fn=>{inner-=box;try{return fn();}finally{inner+=box;}};
    // A title in the top border: whole words only, so `route N` (`N ROUTE`) shows whole or not at all.
    const titled=(label,limit,ellipsis)=>{
      if(size(label)<=limit)return label;
      if(ellipsis)return cut(label,limit);
      const all=label.map(p=>clean(p.text)).join(''),words=counted(all.split(' '));
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
    // Spec rules 24, 36 and 38: beside the native area a folded route reads who / context
    // and time / what / which kind. The first row is its glyph, the doer's recorded role
    // (`owner`, `frame`, `worker`: a dim label in one column, none when unrecorded), then
    // harness, model, effort and what a one-step route's doer is on now. The next, two cells
    // in, the context gauge and percent with the elapsed time at the right edge, each in its
    // own column (spec rule 30 (b)); an unknown context leaves its cells empty, and with
    // neither the row is left out. Then the title, two cells in, on at most two rows (the
    // second ending in `…`), and the kind row. Short of cells effort goes first, then the
    // tool, then the harness; the role and the model stay, and a value shows whole or not at
    // all. No value at all: the glyph and title share the first row. Short of rows the
    // context row folds into the first row (its percent and time at the right, no gauge:
    // short of cells there the `route line` choice keeps its value, time: elapsed, model:
    // context), then the kind row goes, then the route takes one row.
    // Spec rule 43: a backend that sends `workers_live` names the owner and each stage worker
    // running now apart. The owner leads the route (its values in `owner_paint` while a worker
    // leads the row). The route's time stands at the end of the title, and the context rows
    // start with a dim `CTX` in the role column, the gauge under the harness. Spec rule 48: the
    // owner's live workers stand in one rounded box under it (below). An older backend's row
    // keeps its one doer and the rows as they were.
    const treeOf=r=>side&&Array.isArray(r.workers_live);
    const leadPaint=r=>r.owner_paint&&typeof r.owner_paint==='object'?r.owner_paint:(r.paint||{});
    const leadRole=r=>r.owner_paint&&typeof r.owner_paint==='object'?'owner':r.role;
    const workersOf=r=>treeOf(r)?r.workers_live.filter(w=>w&&typeof w==='object').slice(0,8):[];
    // The one-row form's percent and time columns, shared by owners and their workers (rule 48 (c)).
    const allWorkers=listed.flatMap(r=>workersOf(r));
    const ctxWidth=Math.max(0,...listed.map(r=>leadPaint(r).ctx?size([leadPaint(r).ctx]):0),...allWorkers.map(w=>w.paint?.ctx?size([w.paint.ctx]):0));
    const timeWidth=Math.max(elapsedWidth,...allWorkers.map(w=>measure(w.elapsed||'')));
    const ROLE=new Set(['owner','frame','worker']);
    const roleWidth=side?Math.max(0,...listed.map(r=>ROLE.has(leadRole(r))?measure(leadRole(r)):0),
      ...listed.flatMap(r=>workersOf(r).map(w=>ROLE.has(w.role)?measure(w.role):0))):0;
    const roleCell=role=>roleWidth?[text((ROLE.has(role)?role:'').padEnd(roleWidth),'dim'),text(' ')]:[];
    const rolePart=r=>roleCell(leadRole(r));
    // (A one-shot route's kind row carries its tool; any other one-step route shows it here.)
    const oneStepTool=r=>r.tool&&!r.flow&&!(Array.isArray(r.stages)&&r.stages.length>1)&&String(r.shape||'')!=='one-shot-owner'?[text('>_ ','lvl_g'),text(r.tool,'loc_repo')]:[];
    const spaced=values=>values.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);
    // (A worker's open tool is on its stage row, so the owner's row names none.)
    const leadTool=r=>r.owner_paint?[]:oneStepTool(r);
    const whoRow=r=>whoOf([routeGlyph(r),text(' '),...rolePart(r)],leadPaint(r),leadRole(r),leadTool(r));
    function whoOf(lead,p,role,T,room=inner){
      const value=x=>x?[x]:[];
      const H=value(p.harness),M=value(p.model_text),F=value(p.effort),keep=M.length?M:H;
      if(!ROLE.has(role)&&!H.length&&!M.length&&!F.length&&!T.length)return null;
      // Effort goes first, then the tool, then the harness; the role and the model stay.
      for(const [h,m,f,t] of [[H,M,F,T],[H,M,[],T],[H,M,[],[]],[[],keep,[],[]]]){
        const left=spaced([h,m,f]),row=[...lead,...left,...(t.length?[...(left.length?[text(' · ')]:[]),...t]:[])];
        if(size(row)<=room)return row;
      }
      return cut(lead,room);
    }
    const leadRow=r=>foldRow([routeGlyph(r),text(' '),...rolePart(r)],leadPaint(r),leadRole(r),r.elapsed?[text(r.elapsed)]:[],leadTool(r));
    // The one-row form (an owner's, and since spec rule 48 (c) its workers' too, two cells in):
    // who, then the percent and the time at the right in columns shared by every route and worker.
    function foldRow(lead,p,role,E,tool=[]){
      const value=x=>x?[x]:[];
      const M=value(p.model_text),H=value(p.harness),K=M.length?M:H,F=value(p.effort),C=value(p.ctx);
      const build=(h,m,f,withContext,e,t=[])=>{
        const c=withContext&&ctxWidth>0;
        const left=spaced([h,m,f]);
        const more=t.length?[...(left.length?[text(' · ')]:[]),...t]:[];
        const right=[...(c?[pad(ctxWidth-size(C)),...C]:[]),...(e&&timeWidth?[...(c?[text(' ')]:[]),pad(timeWidth-size(E)),...E]:[])];
        const body=[...lead,...left,...more];
        if(!ROLE.has(role)&&!left.length&&!more.length&&!(c&&size(C))&&!(e&&E.length))return {row:body,empty:true};
        return {row:right.length?[...body,pad(inner-size(body)-size(right)),...right]:body,width:size(body)+(right.length?1+size(right):0)};
      };
      const drops=routeLine==='model'?[[H,M,F,1,1],[H,M,[],1,1],[[],K,[],1,1],[[],K,[],1,0],[[],K,[],0,0]]:
        [[H,M,F,1,1],[H,M,[],1,1],[[],K,[],1,1],[[],K,[],0,1],[[],K,[],0,0]];
      // A one-step route (no stage flow) adds its open tool's name, Fleet's `>_ Bash`,
      // only where the row still has room for it; it never pushes a value out.
      for(const pick of drops){
        const got=build(...pick);
        if(got.empty)return null;
        if(got.width>inner)continue;
        const more=tool.length?build(...pick,tool):null;
        return more&&!more.empty&&more.width<=inner?more.row:got.row;
      }
      return null;
    }
    // The context row's columns: the gauge, the percent right-aligned under the others, and
    // the elapsed time at the right edge (the head's context gauge and level colors, at the
    // panel's width; an exact token count with no window stands in the percent column).
    const gaugeOfPaint=p=>{const g=painted(p?.gauge_row||p?.gauge5);
      return g.length>=4?{bar:g.slice(0,2),pct:g.slice(3)}:{bar:[],pct:g.length?g:p?.ctx?[p.ctx]:[]};};
    const gaugeOf=r=>gaugeOfPaint(leadPaint(r));
    const gauges=listed.flatMap(r=>[gaugeOf(r),...workersOf(r).map(w=>gaugeOfPaint(w.paint))]);
    const barWidth=Math.max(0,...gauges.map(g=>size(g.bar))),pctWidth=Math.max(0,...gauges.map(g=>size(g.pct)));
    function ctxRow(r){
      const {bar,pct}=gaugeOf(r),E=r.elapsed?[text(r.elapsed)]:[];
      if(!pct.length&&!E.length)return null;
      const cell=[...(bar.length?bar:[pad(barWidth)]),...(barWidth?[text(' ')]:[]),pad(pctWidth-size(pct)),...pct];
      const row=[text('  '),...cell];
      return E.length?[...row,pad(inner-size(row)-size(E)),...E]:row;
    }
    // Spec rule 43 (d): `CTX` in the role column (one space after it with no role column); an
    // unknown context draws no row.
    const ctxLabel=()=>roleWidth?[text('CTX'.padEnd(roleWidth),'dim'),text(' ')]:[text('CTX','dim'),text(' ')];
    const gaugeCells=p=>{const {bar,pct}=gaugeOfPaint(p);
      return pct.length?[...ctxLabel(),...(bar.length?bar:[pad(barWidth)]),...(barWidth?[text(' ')]:[]),pad(pctWidth-size(pct)),...pct]:null;};
    // Spec rule 43 (c): the time at the right end of the title's last row; the title folds and
    // ends in `…` within the cells left beside it.
    function timedTitle(title,E){
      const room=Math.max(1,inner-2-(E.length?size(E)+1:0));
      let rows=wrapAll(title,room);if(rows.length>2)rows=[rows[0],ellipsis(rows[1],room)];
      const end=row=>E.length?[...row,pad(inner-2-size(row)-size(E)),...E]:row;
      return {rows:rows.map((row,i)=>i===rows.length-1?end(row):row),one:[end(cut(title,room))]};
    }
    // Spec rule 48 (a-1), (b), (d): a worker's rows read as the owner's: who (its own mark,
    // role, harness, model, effort) stands in the top line of the workers' box (`╭─ … ─╮`, a
    // later worker's in the line between, `├─ … ─┤`); inside the box, four cells narrower,
    // `CTX` under the role word with the gauge under the harness, then the stage, the open tool
    // and its target (the path, the file name, the tool, then none) with the worker's time at
    // the right. Short of cells the who loses its effort, then its harness, and `─` fills.
    // Short of rows (rule 48 (c)) the worker takes the owner's one-row form, two cells in.
    const boxLine=s=>({...text(s,'dim'),role:'messages'});
    function workerBlock(w){
      const p=w.paint||{},glyph=routeGlyph({paint:p,kind:w.kind}),E=w.elapsed?[text(w.elapsed)]:[];
      const lead=[glyph,text(' '),...roleCell(w.role)],body=[],room=inner-4;
      // (At least one `─` after the who while its model fits so; the narrowest panel gives that up first.)
      let who=whoOf(lead,p,w.role,[],inner-6)||cut(lead,inner-6);
      if(size(who)<=size(lead))who=whoOf(lead,p,w.role,[],inner-5)||cut(lead,inner-5);
      // A model name longer than the line itself is cut with `…` rather than left out.
      if(size(who)<=size(lead)&&(p.model_text||p.harness))who=cut([...lead,p.model_text||p.harness],inner-5);
      // Short of cells (a four-cell percent at the narrowest panel) the bar goes and `CTX` and the
      // percent stay, as the owner's row gives way (#1583 review follow-up (가)).
      const g=gaugeCells(p),fit=g&&3+size(g)>room?[...ctxLabel(),...gaugeOfPaint(p).pct]:g;
      if(fit)body.push([text('   '),...fit]);
      const stage=w.stage?[text(w.stage,'loc_repo')]:[],tool=w.tool?[text(w.tool,'loc_repo')]:[];
      const base=String(w.target||'').replace(/\/+$/,'').split('/').pop(),what=room-3-(E.length?size(E)+1:0);
      for(const t of [[...tool,...(w.target?[text(' '+w.target,'loc_repo')]:[])],[...tool,...(base?[text(' '+base,'loc_repo')]:[])],tool,[]]){
        const row=[...stage,...(stage.length&&t.length?[text(' · ','dim')]:[]),...t];
        if(t.length&&size(row)>what)continue;
        const kept=cut(row,Math.max(0,what));
        if(row.length||E.length)body.push([text('   '),...kept,...(E.length?[pad(room-3-size(kept)-size(E)),...E]:[])]);
        break;
      }
      const lead1=[text('  '),glyph,text(' '),...roleCell(w.role)];
      return {who,body,one:foldRow(lead1,p,w.role,E)||cut(lead1,inner)};
    }
    function workerBox(blocks){
      const out=[];
      blocks.forEach((b,i)=>{
        const [l,r]=i?['├','┤']:['╭','╮'];
        out.push([boxLine(l+'─ '),...b.who,boxLine(' '+'─'.repeat(Math.max(0,inner-5-size(b.who)))+r)]);
        for(const row of b.body)out.push([boxLine('│'),text(' '),...row,pad(inner-4-size(row)),text(' '),boxLine('│')]);
      });
      out.push([boxLine('╰'+'─'.repeat(Math.max(0,inner-2))+'╯')]);
      return out;
    }
    // Spec rule 24 (11-12), 36 (b): the third row says which kind of route it is and where it
    // stands: Fleet's kind word (`code`, `lab`, `frame`, dim), then the route's shape
    // (`one-shot`: the owner does the work itself, and what it is on now; `staged N/M`: a
    // worker per stage, and the current stage with its mark, then `owner <model>` when a stage
    // worker leads the first row; with no stage running, the next stage and what it waits
    // for). A route with neither kind nor shape draws no third row. Short of cells the values
    // go from the back, whole: the tool's target, the tool, `owner <model>`; kind and shape stay.
    function placeRow(r,workers=0){
      const shape=String(r.shape||''),stages=Array.isArray(r.stages)?r.stages:[];
      const current=stages.findIndex(stage=>stage.current||stage.mark==='✕'),next=stages.findIndex(stage=>stage.mark!=='✓');
      const kind=clean(r.kind_word||'').trim();
      let word,pieces=[];
      if(shape==='one-shot-owner'){
        word='one-shot';
        if(r.tool){
          const base=String(r.target||'').replace(/\/+$/,'').split('/').pop();
          const age=r.tool_age?[text(' '+r.tool_age,'dim')]:[],tool=[text(r.tool,'loc_repo')];
          pieces.push([[...tool,...(r.target?[text(' '+r.target,'loc_repo')]:[]),...age],[...tool,...(base?[text(' '+base,'loc_repo')]:[]),...age],
            [...tool,...(base?[text(' '+base,'loc_repo')]:[])],tool]);
        }else if(r.after_tool)pieces.push([[text('after '+r.after_tool,'dim')]]);
      }else if(/^staged/.test(shape)&&stages.length){
        const at=current>=0?current:next>=0?next:stages.length-1;
        word='staged '+(at+1)+'/'+stages.length;
        if(current>=0){
          pieces.push([[text(stages[current].name||'·','loc_repo'),text(' '),markOf(stages[current])]]);
          if(r.doer_worker&&r.owner_model&&!treeOf(r))pieces.push([[text('owner '+r.owner_model,'dim')]]);
        }else{
          pieces.push([[text(stages[at].name||'·','loc_repo')]]);
          if(r.visibility?.next)pieces.push([[text(r.visibility.next,'dim')]]);
        }
      }else if(shape&&shape!=='unknown'){
        word=shape;
        if(current>=0)pieces.push([[text(stages[current].name||'·','loc_repo'),text(' '),markOf(stages[current])]]);
      }else if(!kind)return null;
      const lead=[text('  '),...(kind?[text(kind,'dim')]:[]),...(kind&&word?[text(' · ','dim')]:[]),...(word?[text(word,'dim')]:[])];
      // Spec rule 43 (g): the workers gone for rows, the kind row ends with their count (it
      // stays while the values before it go).
      for(const end of workers?[[text(' · ','dim'),text('worker '+workers,'dim')],[]]:[[]])for(let n=pieces.length;n>=0;n--){
        const kept=pieces.slice(0,n);
        // The last kept piece may take a shorter whole form (path → file name → tool).
        for(const last of n?kept[n-1]:[[]]){
          const row=[...lead,...kept.slice(0,Math.max(0,n-1)).flatMap(choices=>[text(' · ','dim'),...choices[0]]),...(n?[text(' · ','dim'),...last]:[]),...end];
          if(size(row)<=inner)return row;
        }
      }
      return size(lead)<=inner?lead:null;
    }
    const routeRows=[],detailRows=[];
    within(()=>{
      for(const r of listed){
        const key='route:'+r.id,title=line(r.title||'·','loc_repo'),glyph=[routeGlyph(r),text(' ')];
        // Spec rule 36 (c): the title takes at most two rows, the second ending in `…`.
        let names=side?wrapAll(title,inner-2):[];
        if(names.length>2)names=[names[0],ellipsis(names[1],inner-2)];
        const tree=treeOf(r),workers=tree?workersOf(r).map(workerBlock):[];
        routeRows.push({parts:folded(r),two:side,who:side?whoRow(r):null,lead:side?leadRow(r):null,ctx:side?ctxRow(r):null,
          names,name:[cut(title,inner-2)],glyph,place:side?placeRow(r):null,action:key,kind:r.kind,
          ...(tree?{tree,workers,box:workers.length?workerBox(workers):[],timed:timedTitle(title,r.elapsed?[text(r.elapsed)]:[]),gauge:gaugeCells(leadPaint(r)),
            counted:workers.length?placeRow(r,workers.length):null}:{})});
        if(open.has(key))for(const d of details(r))detailRows.push({...d,action:key,after:r.id});
      }
    });
    // Spec rule 31: a message takes two rows: a head (the direction arrow in its color,
    // the other session's number, then dim harness, time, kind and a sent one's delivery
    // state) and, two cells in, its first meaningful text in the body color. More body
    // rows and more messages (from three) take rows that are left; short of rows a body
    // keeps one row, then a message folds to the one-row form (arrow, number, bright
    // text), then fewer messages show (the lower rule counts the rest).
    const messages=painted(model.messages);
    let messageCount=Math.min(3,messages.length),messageBody=1,messageOne=false,messageLimit=Infinity;
    const messageHead=m=>{
      const arrow=text(m.way,m.way==='←'?'lvl_g':'loc_repo'),who=text(String(m.who||''),'loc_repo');
      const delivery=Array.isArray(m.delivery)&&m.delivery[0]?[text(' · ','dim'),text(String(m.delivery[0]),m.delivery[1]||'dim')]:[];
      const pieces=[m.harness?[text(' '+m.harness,'dim')]:[],m.when?[text(' · '+m.when,'dim')]:[],m.kind?[text(' · '+m.kind,'dim')]:[]];
      // Short of cells kind, harness and time go first; the delivery state stays.
      for(const drop of [[],[2],[2,0],[2,0,1]]){
        const row=[arrow,text(' '),who,...pieces.flatMap((x,i)=>drop.includes(i)?[]:x),...delivery];
        if(size(row)<=inner)return row;
      }
      return cut([arrow,text(' '),who,...delivery],inner);
    };
    const bodyOf=m=>String(m.body||m.text||'');
    const messageRowsOf=()=>{
      const rows=[];
      for(const m of messages.slice(messages.length-messageCount)){
        const key=messageKey(m);
        if(open.has(key)){
          // Opened: the head and every retained row; short of rows its last row counts the rest.
          const body=wrapAll([text(String(m.text||''),'loc_repo')],inner-2).map(row=>[text('  '),...row]);
          const cap=Math.max(1,messageLimit-1);
          rows.push({parts:messageHead(m),action:key,body:key});
          if(body.length<=cap)body.forEach(row=>rows.push({parts:row,action:key,body:key}));
          else{body.slice(0,cap-1).forEach(row=>rows.push({parts:row,action:key,body:key}));
            rows.push({parts:[text('  '),text('+'+(body.length-cap+1)+' lines ›','loc_repo')],action:'read:'+key,body:key});}
        }else if(messageOne)rows.push({parts:cut([text(m.way,m.way==='←'?'lvl_g':'loc_repo'),text(' '),text(String(m.who||''),'loc_repo'),text(' '),text(bodyOf(m),'loc_repo')],inner),action:key});
        else{
          rows.push({parts:messageHead(m),action:key});
          const body=wrapAll([text(bodyOf(m),'loc_repo')],inner-2);
          const kept=body.slice(0,messageBody);
          if(body.length>kept.length)kept[kept.length-1]=cut([...kept[kept.length-1],text(' '),...body[kept.length].slice(0,1)],inner-2);
          kept.forEach(row=>rows.push({parts:[text('  '),...row],action:key}));
        }
      }
      return rows;
    };
    const visibleMessages=messageRowsOf;
    const hiddenMessages=()=>Math.max(0,messages.length-messageCount);
    // Spec rule 46 (b): a memory record takes two rows like a message: `◆ kind · age` (dim) and,
    // two cells in, its headline in the body color on at most two rows (the second ending in
    // `…`). Short of rows (rule 46 (d)): two records, one, its headline on one row, none.
    let memCount=Math.min(2,memItems.length),memBody=2;
    const memRowsOf=()=>{
      const rows=[];
      for(const m of memItems.slice(0,memCount)){
        const head=[m.kind,m.age].filter(Boolean);
        rows.push({parts:cut([text('◆','dim'),...(head.length?[text(' '+head.join(' · '),'dim')]:[])],inner)});
        let body=wrapAll([text(m.text,'loc_repo')],inner-2);
        if(body.length>memBody)body=[...body.slice(0,memBody-1),ellipsis(body[memBody-1],inner-2)];
        for(const row of body)rows.push({parts:[text('  '),...row]});
      }
      return rows;
    };
    // No rows yet: the producer's own label stands alone; a known empty list draws no box.
    // Spec rule 45 (a): beside the native area the count leads its upper-case word (`2 ROUTE`),
    // the producer's label too (`route 3 ⠏2` → `3 ROUTE ⠏2`); the bottom layout keeps `route N`.
    const upper=value=>side?value.replace(/^route (\d+)/,'$1 ROUTE').replace(/^route\b/,'ROUTE'):value;
    const label=upper(clean(model.route_label||'').trim());
    // An open route count pulses on the shared beat while one of them runs (Fleet's g_work/g_work_off).
    const running=shown.some(live);
    const routeTitle=shown.length?[running?pulse(upper('route '+total)):text(upper('route '+total),'g_idle')]:label?[text(label,'g_idle')]:null;
    const routeChecking=!shown.length&&label?[{parts:[text(t('checking','Checking'))],action:'route'}]:[];
    // A press in the panel opens a route's question; the editor keeps its budget.
    const core=top.length-spacers.size-metaGaps.size-rules.size-optional.size-gitRows.size-extra.size;
    // Running jobs keep their row beside a question: its editor pages its own content.
    const q=questions({budget:Math.max(2,rows-core-jobRows.size-1-1),columns:inner-box});
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
    // Short of rows (spec rule 34 (b), which orders rules 14, 19, 23, 24, 29 (c) and 31):
    // empty rows; the messages' extra rows (past three, and a body's rows after its first:
    // they only ever take rows the routes leave in their full form); opened details
    // shorten, then close (an opened message shortens with them); a value on several rows
    // keeps one (ending in `…`; a route title, spec rule 36 (c), too); spec rule 35 (d): the
    // empty row under the route box, then the one inside
    // the metadata (structural rows, not spare ones); then the rule under the metadata; the
    // messages fold to one row each (three of them); spec rule 38: the routes' context row
    // folds into their first row; the routes' third row, then their second (one-row
    // form); fewer messages (three down to one); only then the route box scrolls in place
    // (fewer rows shown, down to one, none dropped); the Git row; the route box; running
    // jobs; the messages section only last; 5h/7d. Every route in the box takes the same form.
    // Spec rule 46 (d): the MEM section under the messages gives way before them, right after
    // the opened details: two records, one, its headline on one row, none.
    let keepGit=true,keepSpacers=true,keepRouteGap=true,keepMetaGap=true,keepRules=true,keepMessages=true,keepDetails=true,routeLimit=routeRows.length,keepRoutes=!!routeTitle,keepOptional=true,keepQuota=quota.length>0,keepMulti=true,keepPlace=true,keepCtx=true,keepMem=memItems.length>0;
    // Spec rule 48 (c): the workers stay (in their box while the owners keep their context row,
    // else one row each, as their owners) or go (the kind row counts them).
    let keepWorkers=true;
    const anyWorkers=side&&routeRows.some(x=>x.tree&&x.workers.length);
    const twoFull=side&&routeRows.some(x=>x.two);
    let twoRows=twoFull;
    // Spec rule 38: who / context and time / what (two rows, then one) / which kind.
    const routeForm=x=>{
      if(!(twoRows&&x.two))return [x.parts];
      if(x.tree)return treeForm(x);
      const head=keepCtx?x.who:x.lead,names=keepMulti?x.names:x.name,out=[];
      if(head){out.push(head);if(keepCtx&&x.ctx)out.push(x.ctx);out.push(...names.map(row=>[text('  '),...row]));}
      else{out.push([...x.glyph,...names[0]],...names.slice(1).map(row=>[text('  '),...row]));if(keepCtx&&x.ctx)out.push(x.ctx);}
      if(keepPlace&&x.place)out.push(x.place);
      return out;
    };
    // Spec rule 48: the owner's rows as any route's, its workers' box under them; short of rows
    // the owner and its workers fold to one row together (rule 48 (c)), never one without the other.
    function treeForm(x){
      const workers=keepWorkers?x.workers:[],bar=[text('  ')],out=[];
      const head=keepCtx?x.who:x.lead,names=keepCtx?(keepMulti?x.timed.rows:x.timed.one):(keepMulti?x.names:x.name);
      if(head){out.push(head);if(keepCtx&&x.gauge)out.push([...bar,...x.gauge]);out.push(...names.map(row=>[...bar,...row]));}
      else{out.push([...x.glyph,...names[0]],...names.slice(1).map(row=>[...bar,...row]));if(keepCtx&&x.gauge)out.push([...bar,...x.gauge]);}
      const place=!keepWorkers&&x.counted?x.counted:x.place;
      if(keepPlace&&place)out.push(place);
      if(workers.length)out.push(...(keepCtx?x.box:workers.map(w=>w.one)));
      return out;
    }
    const routeSize=x=>routeForm(x).length;
    // An opened route's detail that outgrows its rows keeps its first whole items and a
    // `+N lines ›` row to the route read page, where every value reads whole.
    const detailFull=Math.max(0,...listed.map(r=>detailRows.filter(d=>d.after===r.id).length));
    let detailCap=detailFull;
    const detailsOf=id=>{
      const rows=detailRows.filter(d=>d.after===id);
      if(rows.length<=detailCap)return rows;
      let keep=0;
      for(let i=0;i<rows.length&&i<detailCap-1;i++)if(i+1===rows.length||rows[i+1].item!==rows[i].item)keep=i+1;
      keep=Math.max(1,keep||detailCap-1);
      return [...rows.slice(0,keep),{parts:[text('  '),{...text('┆','dim'),role:'messages'},text(' '),text('+'+(rows.length-keep)+' lines ›','loc_repo')],action:'route',after:id}];
    };
    // One empty row parts an opened route's detail from the next route.
    const gapAfter=(x,i,list)=>keepDetails&&i<list.length-1&&detailsOf(x.action.slice(6)).length>0;
    const routeBox=()=>keepRoutes?2+(shown.length?routeRows.slice(0,routeLimit).reduce((n,x,i,list)=>n+routeSize(x)
      +(keepDetails?detailsOf(x.action.slice(6)).length:0)+(gapAfter(x,i,list)?1:0),0):routeChecking.length):0;
    // The lower section: the messages' titled rule and rows, then MEM's, then one closing rule.
    const memShown=()=>keepMem&&memItems.length>0;
    const messageSection=()=>{const m=keepMessages&&messages.length>0,k=memShown();return (m?1+visibleMessages().length:0)+(k?1+memRowsOf().length:0)+(m||k?1:0);};
    // Spec rule 35 (c): beside the native area one empty row parts the route box from what
    // follows it (the question box, else the messages rule); the messages' extra rows leave it.
    const routeGap=()=>side&&keepRouteGap&&keepRoutes&&(ask.length>0||keepMessages&&messages.length>0||memShown())?1:0;
    const need=()=>core+(keepMulti?extra.size:0)+(keepGit?gitRows.size:0)+(keepSpacers?spacers.size:0)+(keepMetaGap?metaGaps.size:0)+(keepRules?rules.size:0)+(keepOptional?optional.size:0)+ask.length+routeBox()+routeGap()+messageSection()+1;
    const budget=rows;
    const baseCount=messageCount;
    // Each step is taken only while rows are short; a later step that frees more rows than
    // it had to gives them back to the steps before it, the most important first, so no row
    // is left empty that an earlier value could fill.
    const gave=[];
    const give=(can,apply,undo)=>{while(need()>budget&&can()){apply();gave.push(undo);}};
    give(()=>keepSpacers,()=>{keepSpacers=false;},()=>{keepSpacers=true;});
    give(()=>detailCap>2,()=>{detailCap--;},()=>{detailCap++;});
    give(()=>keepDetails,()=>{keepDetails=false;},()=>{keepDetails=true;});
    give(()=>keepMem&&memCount>1,()=>{memCount--;},()=>{memCount++;});
    give(()=>keepMem&&memBody>1,()=>{memBody--;},()=>{memBody++;});
    give(()=>keepMem,()=>{keepMem=false;},()=>{keepMem=true;});
    if(need()>budget&&messageLimit===Infinity){
      const opened=Math.max(2,...visibleMessages().map(x=>x.body?visibleMessages().filter(y=>y.body===x.body).length:0));
      give(()=>messageLimit===Infinity,()=>{messageLimit=opened;},()=>{messageLimit=Infinity;});
    }
    give(()=>messageLimit>2&&messageLimit!==Infinity,()=>{messageLimit--;},()=>{messageLimit++;});
    give(()=>keepMulti,()=>{keepMulti=false;},()=>{keepMulti=true;});
    give(()=>keepRouteGap,()=>{keepRouteGap=false;},()=>{keepRouteGap=true;});
    give(()=>keepMetaGap,()=>{keepMetaGap=false;},()=>{keepMetaGap=true;});
    give(()=>keepRules,()=>{keepRules=false;},()=>{keepRules=true;});
    give(()=>messages.length>0&&!messageOne,()=>{messageOne=true;},()=>{messageOne=false;});
    // Spec rule 48 (c): every route's context row folds into its first row and, in the same step,
    // its workers take one row each; then the workers go (the kind row counts them). The box comes
    // back only with the context rows, and only while the workers stand.
    give(()=>keepCtx,()=>{keepCtx=false;},()=>{if(keepWorkers||!anyWorkers)keepCtx=true;});
    give(()=>anyWorkers&&keepWorkers&&!keepCtx,()=>{keepWorkers=false;},()=>{keepWorkers=true;});
    give(()=>keepPlace,()=>{keepPlace=false;},()=>{keepPlace=true;});
    give(()=>twoRows,()=>{twoRows=false;},()=>{twoRows=true;});
    give(()=>messageCount>1,()=>{messageCount--;},()=>{messageCount++;});
    give(()=>routeLimit>1,()=>{routeLimit--;},()=>{routeLimit++;});
    give(()=>keepGit,()=>{keepGit=false;},()=>{keepGit=true;});
    give(()=>keepRoutes,()=>{keepRoutes=false;},()=>{keepRoutes=true;});
    give(()=>keepOptional,()=>{keepOptional=false;},()=>{keepOptional=true;});
    give(()=>keepMessages,()=>{keepMessages=false;},()=>{keepMessages=true;});
    give(()=>keepQuota,()=>{keepQuota=false;},()=>{keepQuota=true;});
    const kept=()=>[keepSpacers,detailCap,keepDetails,memCount,memBody,keepMem,messageLimit,keepMulti,keepRouteGap,keepMetaGap,keepRules,messageOne,keepWorkers,keepCtx,keepPlace,twoRows,messageCount,routeLimit,keepGit,keepRoutes,keepOptional,keepMessages,keepQuota];
    for(const undo of gave.reverse()){
      const was=kept();undo();
      if(need()>budget)[keepSpacers,detailCap,keepDetails,memCount,memBody,keepMem,messageLimit,keepMulti,keepRouteGap,keepMetaGap,keepRules,messageOne,keepWorkers,keepCtx,keepPlace,twoRows,messageCount,routeLimit,keepGit,keepRoutes,keepOptional,keepMessages,keepQuota]=was;
    }
    // Rows to spare beside the native area once everything above the messages' extra rows
    // stands whole (the routes in their three-row form, details, values on all their rows,
    // both structural empty rows): bodies take up to three rows and more messages show,
    // never past the rows the panel has.
    const memWhole=keepMem===memItems.length>0&&memCount===Math.min(2,memItems.length)&&memBody===2;
    const whole=detailCap===detailFull&&keepDetails&&keepMulti&&memWhole&&keepRouteGap&&keepMetaGap&&keepRules&&keepWorkers&&keepCtx&&keepPlace&&twoRows===twoFull&&routeLimit===routeRows.length&&keepRoutes===!!routeTitle&&keepGit&&keepOptional;
    if(side&&keepMessages&&whole&&!messageOne&&messageLimit===Infinity&&messageCount===baseCount){
      const grow=()=>{
        let changed=false;
        if(messageBody<3){messageBody++;if(need()>budget)messageBody--;else changed=true;}
        if(messageCount<messages.length){messageCount++;if(need()>budget)messageCount--;else changed=true;}
        return changed;
      };
      while(grow());
    }
    const order=[];
    top.forEach((row,i)=>{if((keepSpacers||!spacers.has(i))&&(keepMetaGap||!metaGaps.has(i))&&(keepRules||!rules.has(i))&&(keepOptional||!optional.has(i))&&(keepGit||!gitRows.has(i))&&(keepMulti||!extra.has(i)))
      order.push({parts:!keepMulti&&oneRow.has(i)?oneRow.get(i):row,groups:topGroups.get(i)});});
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
      (shown.length?kept:routeChecking).forEach((item,i,list)=>{
        // A press on any row of a folded route opens it.
        for(const parts of item.two?routeForm(item):[item.parts])routeBlock.push(boxRow({parts,action:item.action},key));
        if(keepDetails&&item.action.startsWith('route:')){
          for(const d of detailsOf(item.action.slice(6)))routeBlock.push(boxRow(d,key));
          if(gapAfter(item,i,list))routeBlock.push(boxRow({parts:[],action:item.action},key));
        }
      });
      routeBlock.push(boxBottom(key,below?hiddenNote('↓'+below):[]));
    }
    // Messages have no box: a titled upper rule and a lower rule, fixed at the
    // bottom right above 5h/7d (spec rule 14, 11-11 night final layout). Spec rule 45 (b): beside
    // the native area the title is `MESSAGES`. Spec rule 46 (a): MEM's records follow under their
    // own titled rule (`── MEM ──`) in place of the messages' lower rule, and the one closing
    // rule ends both; the count of messages not shown (`+N`) then stays with the messages, at
    // the end of their title rule.
    const keepMessageRows=keepMessages&&messages.length>0;
    if(keepMessageRows||memShown()){
      const key=tone.messages,hidden=keepMessageRows?hiddenMessages():0;
      const note=hidden?[text(' +'+hidden+' ','loc_repo'),text('─',key)]:[];
      const noted=parts=>parts.map(x=>x.text==='─'?{...x,role:'messages'}:x);
      const titleRule=(word,tail=[])=>{const head=fitted([{...text(word,key),role:'title'}]);
        return [...[...head,text('─'.repeat(Math.max(0,inner-size(head)-size(tail))))].map(x=>({...x,...(styles[key]||{dim:true}),role:x.role||'messages'})),...noted(tail)];};
      if(keepMessageRows){
        messageBlock.push({parts:titleRule(side?'MESSAGES':'messages',memShown()?note:[])});
        messageBlock.push(...visibleMessages());
      }
      if(memShown()){messageBlock.push({parts:titleRule('MEM')});messageBlock.push(...memRowsOf());}
      const closing=memShown()?[]:note;
      messageBlock.push({parts:[{...text('─'.repeat(Math.max(0,inner-size(closing))),key),role:'messages'},...noted(closing)]});
    }
    // Each section is placed whole or not at all; the empty row under the route box goes first.
    const tail=ask.length+messageBlock.length+1;
    let gap=routeBlock.length?routeGap():0;
    if(order.length+routeBlock.length+gap+tail>budget)gap=0;
    if(order.length+routeBlock.length+tail>budget)routeBlock=[];
    for(const item of order)add(item.parts,item.action,item.groups);
    // The rows the route box takes, for the host's wheel (panel rows, as hits count them).
    if(routeBlock.length)routeArea={top:content.length+1,bottom:content.length+routeBlock.length,offset:routeOffset,max:routeMax,step:routeStep};
    for(const item of routeBlock)add(item.parts,item.action,item.groups);
    if(gap&&routeBlock.length)add([]);
    // What comes and goes sits low: the question right above the messages, which keep the bottom.
    if(fill)while(content.length<rows-tail)add([]);
    if(content.length+tail<=budget){
      for(const item of ask)add(item.parts,item.action);
      for(const item of messageBlock)add(item.parts,item.action);
    }else for(const item of ask)add(item.parts,item.action);
    // A reset time that does not fit goes whole, never as a cut `(↻ …`. Spec rule 49 (b): 5h/7d
    // share the last row with the fold button and the gear, which stay however short rows run.
    const quotaRoom=inner-BUTTONS-1;
    const quotaRow=size(quota)<=quotaRoom?quota:quota.filter(part=>!/^\(↻/.test(String(part.text)));
    lastRow(keepQuota?quotaRow:[],keepQuota&&quota.length?['quota']:[],order.length);
  }
  const count=fill?height:Math.min(height,content.length+2),rows=[];
  // Spec rules 16-17, 29 (a): the top border carries Fleet's session-row head on the
  // left: the state glyph, the session number (`[e9]`), the harness name in its Fleet
  // hue and, for a steward, Fleet's `⚑` (spec rule 49 (a): no gear there). The state
  // has no word: glyph, number and frame share its color. `HEARTING` names the
  // settings screen alone. An unknown state has no glyph; an unknown number is left out.
  const own=settings?null:LIVE[state];
  // Spec rule 25: the outer frame takes the color of that state: running and waiting
  // their Fleet session key, idle and an unknown state the neutral frame gray (the
  // terminal's dim on an unknown face). Route failures and results stay on their own
  // rows' marks; the question box keeps its rose.
  const color=own&&own[1]!=='g_work_off'?own[1]:'dim';
  const frameText=v=>({...text(v,color),role:color==='dim'?'frame':'frame-state'});
  // Spec rule 29, 35 (a): a steward's `⚑` alone blinks in Fleet's steward pink
  // (`tag_steward` ↔ `tag_steward_dim`) on the panel's one beat, idle or not; without
  // color, bold ↔ plain. Its number keeps the steward color without blinking, as on
  // Fleet's session row (`tag_steward`); without color, plain weight.
  const cellsOf=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
  const steward=!settings&&model.steward===true;
  const plain=model.no_color===true||cfg.theme==='mono';
  const stewardPart=v=>({...text(v,beat?'tag_steward':'tag_steward_dim'),beat:true,role:'steward',...(plain?{bold:beat,dim:false}:{})});
  const stewardTag=v=>({...text(v,'tag_steward'),role:'steward',...(plain?{bold:false,dim:false}:{})});
  const tag=/^\[[^\]?]{1,16}\]$/.test(String(model.tag||''))?String(model.tag).slice(1,-1):'';
  const harnessName=['claude','codex','opencode'].includes(harness)?harness:'';
  const glyphPart=own?[{...text(own[0]==='spin'?spinGlyph.text:own[0],own[1]),...(own[0]==='spin'?{spin:true}:{}),role:'session'}]:[];
  const tagPart=tag?[text('[','dim'),steward?stewardTag(tag):{...text(tag,own?own[1]:'dim'),role:'session'},text(']','dim')]:[];
  const harnessPart=harnessName?[text(harnessName,'hb_'+harnessName)]:[];
  const flagPart=steward?[stewardPart('⚑')]:[];
  // Short of cells the steward mark goes first, then the harness, then the number;
  // the glyph stays to the end.
  const headParts=pieces=>{const shown=pieces.filter(x=>x.length);return shown.length?[frameText(' '),...shown.flatMap((x,i)=>i?[text(' '),...x]:x),frameText(' ')]:[];};
  // Spec rule 49 (a): right of the head the top border is the line alone; the gear stands
  // inside, at the right end of the last row.
  const room=Math.max(0,width-3);
  const leftParts=settings?[{...text(' HEARTING ','loc_repo'),bold:true}]:
    [[glyphPart,tagPart,harnessPart,flagPart],[glyphPart,tagPart,harnessPart],[glyphPart,tagPart],[glyphPart]].map(headParts).find(x=>cellsOf(x)<=room)||[];
  const label=clip(leftParts,room);
  rows.push([frameText('┌'),...label.parts,frameText('─'.repeat(Math.max(0,width-2-label.used))),frameText('┐')]);
  const frameRow=parts=>{const fitted=clip(parts,inner);return [frameText('│'),text(' '),...fitted.parts,text(' '.repeat(Math.max(0,inner-fitted.used)+1)),frameText('│')];};
  for(let i=0;i<count-2;i++)rows.push(frameRow(content[i]||[]));
  const foot=clip(settings?line(' Esc · '+t('cancel','Cancel')+' '):edge.length?[text(' '),...edge,text(' ')]:[],width-4);
  rows.push([frameText('└─'),...foot.parts,frameText('─'.repeat(Math.max(0,width-3-foot.used))+'┘')]);
  const painted=appearance(rows,cfg,{colors:model.colors??256,noColor:model.no_color===true,background});
  // The rows whose spinner cells turn between paints (the host swaps only those cells).
  const spins=rows.map((row,i)=>row.some(part=>part.spin)?i:-1).filter(i=>i>=0&&i<count);
  const animated=rows.slice(0,count).some(row=>row.some(part=>part.spin||part.beat||part.motion));
  // Only a blinking part needs the one-second repaint (a spinner and the rule turn on their own).
  const blinks=rows.slice(0,count).some(row=>row.some(part=>part.beat));
  // Spec rule 47 (d): the moving rule's row, that row painted at any spinner frame, and the next
  // frame it changes at (the light: every frame while it is inside, then the end of its rest,
  // spec rule 51 (c); a breath: its next step): the host repaints this one row then, on the
  // beat it already keeps, its changed cells only.
  const motionAt=settings?-1:rows.slice(0,count).findIndex(row=>row.some(part=>part.motion));
  const motion=motionAt>=0?{row:motionAt,at:tick=>appearance([frameRow(ruleParts(tick))],cfg,{colors:model.colors??256,noColor:model.no_color===true,background})[0],
    next:tick=>{if(ruleKind!=='wait')return scanNext(tick,ruleWidth);const now=breathStep(tick,ruleSteps);for(let t=Math.trunc(tick)+1;t<=Math.trunc(tick)+RULE_BREATHE;t++)if(breathStep(t,ruleSteps)!==now)return t;return Math.trunc(tick)+RULE_BREATHE;}}:null;
  // The statusline groups this paint actually shows (cut rows count; dropped ones do not).
  const groups=[...new Set(contentGroups.slice(0,Math.max(0,count-2)).flat())];
  if(routeArea&&routeArea.bottom>count-2)routeArea=null;
  return {rows:painted,hits:hits.filter(h=>h.row<count),color,routePages,routeDetail,routeArea,groups,animated:animated&&!settings,blinks:blinks&&!settings,spins,motion};
}
module.exports={render,defaults,fields,change,appearance,options:OPTIONS,SPIN,FACES,ruleCell,scanLevel,breathStep,
  RULE:{PEAK:RULE_PEAK,FADE:RULE_FADE,SCAN:RULE_SCAN,REST:RULE_REST,BREATHE:RULE_BREATHE,WAIT_STEPS:RULE_WAIT_STEPS,WAIT_STEPS_256:RULE_WAIT_STEPS_256}};
