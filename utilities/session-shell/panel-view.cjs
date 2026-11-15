'use strict';
// Geometry and paint only. The existing question controller owns drafts/effects.
// 11-15 (panel polish 4, spec rule 47): how the open panel shows that this session runs, picked by
// the setting `rule motion` (`rule`, the default; `border`; `scanners`; `off`), and the values to tune.
// `rule`: the outer frame stands still and lit at step BORDER_LIT of `FACES.*.orbit` (0 the brightest;
// the still idle frame is the plain gray), and a light passes over the head rule: RULE_PEAK brightest
// cells in its middle and RULE_FADE cells on each side one shade down each, out past one end and back
// in RULE_SCAN spinner frames (100 ms each: 26 = 2.6 s), then the dark rule rests RULE_REST frames
// (10 = 1 s) and the light comes in again on the same side.
const RULE_PEAK=5,RULE_FADE=4,RULE_SCAN=26,RULE_REST=10,BORDER_LIT=1;
// `scanners`: a wide wave flows round the frame clockwise, no edge anywhere: WAVE_COUNT waves share
// the frame (fewer where one would be narrower than WAVE_MIN cells), one place rises and falls once
// every WAVE_LAP frames (56 = 5.6 s), in WAVE_STEPS shades in 24-bit color (the face's four in 256
// colors; fewer on a narrow wave, so two neighbouring cells never differ by more than one), its
// darkest WAVE_FLOOR of the way from the still frame's gray to the brightest (never out).
const WAVE_COUNT=3,WAVE_LAP=56,WAVE_STEPS=10,WAVE_FLOOR=0.3,WAVE_MIN=24;
// `border` (#1639): one bright arc, BORDER_ARC of the frame long, round once every BORDER_LAP frames.
const BORDER_ARC=1/4,BORDER_LAP=60;
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14,
  position:'right',theme:'terminal',shade:'none',route_line:'time',rule_motion:'rule'};
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
    // Spec rule 47: the moving frame's tones (the values at the top). The running frame's shades,
    // brightest first (the session spinner's vanilla, the running frame's color) down to the one
    // beside the neutral frame gray (`border`'s arc, `rule`'s lit frame, `scanners`' wave in 256
    // colors); the wave's two ends in 24-bit color (the still frame's gray, the brightest); the head
    // rule's light, dimmest first; a waiting frame's reds, brightest first (the frame's waiting
    // red), in 256 colors, and its two ends in 24-bit color.
    orbit:[230,187,144,101],waveRGB:[[128,128,128],[255,255,215]],scan:[243,101,144,187,230],
    breath:[217,210,174],breathRGB:[[255,175,175],[215,135,135]],
    // 11-15 (panel polish 1): a question to answer here stands on `fill` (the waiting red's dark
    // end; bright text on it 12:1), its choices on `chip`, its first row in `bright`. Polish 3: while
    // a popup stands, everything outside it reads in `veil` alone.
    fill:52,chip:235,bright:255,veil:239},
  light:{light:255,medium:254,body:237,secondary:241,title:241,frame:243,route:243,messages:245,accent:131,
    working:22,idle:58,blocked:131,dead:88,done:22,steward:[132,175],
    orbit:[22,65,108,151],waveRGB:[[118,118,118],[0,95,0]],scan:[251,151,108,65,22],
    breath:[88,124,131],breathRGB:[[135,0,0],[175,95,95]],
    fill:224,chip:255,bright:232,veil:250},
};
// Fleet's 256-color hues on each face. The light face takes the reviewed deep
// counterparts (running 22, attention 58, failure 88, project 23, model 60)
// instead of the earlier strong substitutes; the dark face changes only the
// failure red, and only on a shade.
const HUES={dark:{217:210},light:{253:237,252:238,150:22,186:58,217:88,116:23,176:60,147:25,230:22,219:132,137:94}};
const LINES=new Set(['frame','route','messages']);
function face(theme,background){return theme==='light'?'light':background==='light'||background==='dark'?background:'';}
// A moving frame cell's color: an arc or lit-frame shade from its face's ramp (0 the brightest); a wave
// at `level` (0..1) between its face's two ends in 24-bit color, else on its ramp (the dimmest first);
// the head rule's light step (1 the dimmest .. 5); a waiting step from its face's 256-color reds, or in
// 24-bit color (when the terminal says it takes it) a point on the cosine between its face's two ends.
const mix=(a,b,f)=>'#'+a.map((c,i)=>Math.round(c+(b[i]-c)*f).toString(16).padStart(2,'0')).join('');
function orbitTone(part,F,colors){
  if(part.tone==='arc')return F.orbit[Math.max(0,Math.min(F.orbit.length-1,part.level))];
  if(part.tone==='scan')return F.scan[Math.max(0,Math.min(F.scan.length-1,part.level-1))];
  if(part.tone==='wave'){const [lo,hi]=F.waveRGB,q=Math.max(0,Math.min(1,part.level));
    return colors>256?mix(lo,hi,WAVE_FLOOR+(1-WAVE_FLOOR)*q):F.orbit[Math.round((1-q)*(F.orbit.length-1))];}
  const steps=part.steps,k=part.step;
  if(colors>256&&steps>1){
    const f=(Math.cos(Math.PI*k/(steps-1))+1)/2,[hi,lo]=F.breathRGB;
    return mix(lo,hi,f);
  }
  return F.breath[Math.max(0,Math.min(F.breath.length-1,k))];
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
    // 11-15 (panel polish 1): the words of a question to answer here, bright on its fill.
    else if(role==='bright'&&theme!=='mono'){foreground=F.bright;dim=false;}
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
    // (On the dark face without a shade that is the glyph's and word's own red, Fleet's g_dead:
    // 11-15 design F 9.)
    else if(role==='route-dead'){if(known&&(shade||known==='light'))foreground=F.dead;dim=false;}
    // The steward mark's two beats on a known face: Fleet's pink, then a receding pink
    // (no dim on top, so a shade keeps the hue); the light face takes the deep pair.
    // The steward's number does not blink (spec rule 35 (a)): it keeps the lit pink.
    else if(role==='steward'&&theme!=='mono'&&known){foreground=F.steward[part.beat===true&&!bold?1:0];dim=false;}
    else if(role==='steward'&&theme==='mono')dim=false;
    // Spec rule 47: the moving frame's cells take their step's tone on their face (an unknown face
    // the dark face's), at full strength; mono never moves (no tone).
    else if(role==='orbit'&&theme!=='mono'&&part.tone){foreground=orbitTone(part,FACES[known||'dark'],colors);dim=false;}
    // Spec rule 22: on a known face each line takes its role's color with or
    // without a shade (frame 244/243, route 245/243, messages 242/245); an
    // unknown face keeps the terminal's own, mono keeps no hue.
    else if(LINES.has(role)&&known&&theme!=='mono'){foreground=F[role];dim=false;}
    // 11-15 (panel polish 1): a part on a question's fill or chip takes that ground on every face (an
    // unknown face the dark one's), over a shade too; mono and eight colors draw a bar instead.
    const ground=part.ground&&theme!=='mono'&&Number.isInteger(F[part.ground])?{background:F[part.ground]}:{};
    if(shade){
      // On a shade nothing is dimmed on top: secondary text and lines take their own color.
      if(LINES.has(role)){foreground=F[role];dim=false;}
      else if(role==='title'){foreground=F.title;dim=false;}
      else if(foreground===null||dim||role==='secondary'){foreground=foreground===null&&!dim&&role!=='secondary'?F.body:F.secondary;dim=false;}
      return {...part,foreground,dim,bold,background:F[shade],...ground};
    }
    // A declared light terminal: secondary text takes its color instead of a dim default.
    if(theme==='light'&&foreground===null&&dim){foreground=F.secondary;dim=false;}
    return {...part,foreground,dim,bold,...ground};
  }));
}
// A combining mark or joined emoji stays with the character it belongs to.
const SEGMENTER=typeof Intl==='object'&&Intl.Segmenter?new Intl.Segmenter(undefined,{granularity:'grapheme'}):null;
const JOINS=/[\p{M}\u200d\u1160-\u11ff\u{1f1e6}-\u{1f1ff}\u{1f3fb}-\u{1f3ff}]/u;
const graphemes=s=>SEGMENTER&&JOINS.test(s)?Array.from(SEGMENTER.segment(s),x=>x.segment):[...s];
// Spec rule 49 (d): the narrow-width judgment is one constant (native-display's NATIVE_MIN), so
// the editor has no `threshold`; `side_min_columns` stays in `defaults` only so a saved policy
// keeps its keys.
const fields=['layout','position','side_width','narrow','auxiliary','bottom_rows','theme','shade','route_line','rule_motion'];
const names=['layout','position','width','narrow','auxiliary','height','theme','shade','route_line','rule_motion'];
const OPTIONS={layout:['side','bottom'],position:['right','left'],narrow:['bottom','compact'],auxiliary:['summary','detail'],
  theme:['terminal','light','mono'],shade:['none','light','medium'],route_line:['time','model'],rule_motion:['rule','border','scanners','off']};
const APPEARANCE_KEYS=['position','theme','shade'];
// The session's own state, as Fleet's session row judges and draws it
// (render_cells `_LIVE_GLYPH`/`_state_key`): a working session spins.
const SPIN='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
// Spec rule 26: an answer being typed in for the person reads as running. Spec rule 29:
// the state is told by glyph and color alone (no `running`/`idle`/`waiting` word); the
// three glyphs differ in shape, so mono and NO_COLOR still tell them apart.
const LIVE={working:['spin','g_spin'],submitting:['spin','g_spin'],idle:['●','g_work_off'],blocked:['◑','g_blocked']};
// Spec rule 47 (11-14: the outer frame; 11-15 panel polish 4: the setting picks the frame's or the
// rule's motion, the values at the top), every value of the moving frame in one place. Waiting,
// whatever the setting (but `off`), the whole frame breathes red once every BORDER_BREATHE frames
// (48 = 4.8 s) through BORDER_BREATHE_STEPS shades on a cosine (24-bit color; BORDER_BREATHE_STEPS_256
// in 256 colors), each shade an equal time; the head rule stays the plain thin `─` then (spec rule 52).
const BORDER_SHADES=4,BORDER_BREATHE=48,BORDER_BREATHE_STEPS=6,BORDER_BREATHE_STEPS_256=3;
const mod=(t,n)=>((Math.trunc(t)%n)+n)%n;
// A breath's shade at a frame: 0 brightest .. steps-1 darkest, down and back up, each shade for an
// equal stretch of the breath (2 (steps-1) stretches).
function breathStep(tick,steps){
  const slots=2*(steps-1),s=Math.floor(mod(tick,BORDER_BREATHE)*slots/BORDER_BREATHE);
  return s<steps?s:slots-s;
}
// `border`: where the arc's head stands at a frame on a frame of `cells` cells, from `start`, and its
// shade at a cell `behind` cells back from its head (0 the head .. BORDER_SHADES-1), or -1 past its tail.
function orbitHead(tick,cells,start){return mod(start+Math.floor(Math.trunc(tick)*cells/BORDER_LAP),cells);}
function orbitShade(behind,length){return behind<length?Math.min(BORDER_SHADES-1,Math.floor(behind*BORDER_SHADES/length)):-1;}
// `scanners`: how many waves a frame of `cells` cells carries, the shades a wave of that width keeps
// (one shade at most between neighbouring cells: the brightness climbs at most 1.3π/width a cell), and
// a cell's level (0..1, in those shades) at a frame: (½ + ½ cos 2π(place/cells · waves − frame/WAVE_LAP))^1.3.
function waves(cells){return Math.max(1,Math.min(WAVE_COUNT,Math.floor(cells/WAVE_MIN)));}
function waveSteps(cells,most){return Math.max(1,Math.min(most,Math.floor(cells/waves(cells)/(1.3*Math.PI))));}
function waveLevel(at,tick,cells,steps){
  const u=.5+.5*Math.cos(2*Math.PI*(at/cells*waves(cells)-Math.trunc(tick)/WAVE_LAP));
  return Math.round(Math.pow(u,1.3)*steps)/steps;
}
// `rule`: the light over an `n`-cell head rule: where its middle stands at a frame (null while the
// dark rule rests), its shade at cell `i` (5 the brightest, 0 none), and the next frame it looks
// different at (the next frame while the light is inside; its return at the end of the rest).
const RULE_CYCLE=RULE_SCAN+RULE_REST-1;
function scanAt(tick,n){
  const plateau=(RULE_PEAK-1)/2,lo=-(RULE_FADE+plateau)-1,hi=n+RULE_FADE+plateau,q=mod(tick,RULE_CYCLE);
  if(q>=RULE_SCAN)return null;
  // (The middle stands on a whole cell, so the brightest run is always RULE_PEAK cells.)
  const y=q<RULE_SCAN/2?q/(RULE_SCAN/2):2-q/(RULE_SCAN/2);
  return Math.round(lo+y*(hi-lo));
}
function scanLevel(tick,i,n){
  const plateau=(RULE_PEAK-1)/2,c=scanAt(tick,n);
  return c===null?0:Math.max(0,RULE_FADE+1-Math.max(0,Math.abs(i-c)-plateau));
}
function scanNext(tick,n){
  const t=Math.trunc(tick),lit=f=>{const c=scanAt(f,n),reach=RULE_FADE+(RULE_PEAK-1)/2;return c!==null&&c+reach>=0&&c-reach<n;};
  if(lit(t))return t+1;
  for(let f=t+1;f<=t+RULE_CYCLE;f++)if(lit(f))return f;
  return t+1;
}
function change(value,key,step){
  const options=OPTIONS;
  if(options[key]){const list=options[key];return {...value,[key]:list[(list.indexOf(value[key])+step+list.length)%list.length]};}
  const bounds={side_width:[32,80,1],bottom_rows:[8,20,1],side_min_columns:[80,1000,10]},[lo,hi,n]=bounds[key];
  return {...value,[key]:Math.max(lo,Math.min(hi,value[key]+step*n))};
}
function render({frame=0,since,width,height,side,fill=side,model={},native={},state,sid,brief,sent,sections,configuration,settings,focused=false,cwd='',harness='',cellWidth,background='',folded=false}){
  // Spec rule 49 (c): a folded panel is the strip under the native area, in every placement.
  folded=folded===true&&!settings;
  width=Math.max(folded?1:12,width);height=Math.max(folded?1:4,height);
  const cfg={...defaults,...configuration};
  const labels=model.labels||{},styles=model.styles||{},t=(k,f)=>labels[k]||f;
  const text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const line=(value,key)=>[text(value,key)];
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const measure=cellWidth|| (s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  function clip(parts,limit){let out=[],used=0;for(const part of parts){let s='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;s+=c;used+=w;}if(s)out.push({...part,text:s});if(used>=limit)break;}return {parts:out,used};}
  // A question's choices row as one list of parts per choice (`1 `, then its words), its ` · `
  // joints and a `+N` count left out.
  function choiceItems(parts){
    const items=[];
    for(const part of parts){const s=clean(part.text);
      if(/^(?: · )?\+\d+$/u.test(s)||s===' · ')continue;
      if(/^\d+ $/.test(s))items.push([part]);else if(items.length)items.at(-1).push(part);}
    return items;
  }
  let inner=width-4;const content=[],hits=[],edge=[];
  // 11-15 (popup design A): the question the person would answer next, as its summary (the
  // shell's editor reads it whole in the popup): `count` boxes, `rows` (its first row and its
  // choices on one row, or its first two rows), `one` (its first row), `line` (its whole first
  // line, for the folded strip), `choices` (how many).
  const summary=(()=>{if(typeof brief!=='function')return null;try{const b=brief();return b&&b.count>0?b:null;}catch{return null;}})();
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
  // The session's own model: the native selection when it is shown, else Fleet's.
  function selectedParts(){
    const list=v=>Array.isArray(v)?v:[];
    const nativeModel=native.model&&(native.model!==model.model_value||!list(model.selected).length);
    // The native's own model name takes the color the backend gave it (its family's, else the
    // harness's, as route rows); until that arrives for a new name, the harness's color.
    const nativePart=model.native_model_part&&model.native_model_part.text===native.model?{...model.native_model_part,text:native.model}:
      text(native.model,['claude','codex','opencode'].includes(harness)?'hb_'+harness:'loc_repo');
    return nativeModel?[nativePart,...(native.effort?[text(' '+native.effort)]:[])]:
      list(model.selected).length?model.selected:line(t('checking','Checking'));
  }
  if(folded)return foldedView();
  // Spec rule 49 (c): folded (by itself or by hand, beside or under the native area) the panel is
  // one strip under the native area: a thin rule in the messages' line color with the unfold mark
  // `▴` at its right end, then three rows, each the most pressing first and giving way whole item
  // by whole item from its end:
  //   1. the session: the state glyph and number, the project, model and effort, `CTX` and its
  //      gauge, 5h, 7d (short of cells the reset times go first, then the effort, the project);
  //   2. what to do now: the answer wait with its question's first row, where a wait answered
  //      elsewhere is answered, compaction imminent, a tidy notice;
  //   3. the routes: `N ROUTE ⠏2 ✕1`, the failed routes' titles (else the running ones'),
  //      `MESSAGES ←N` (received since the person's last input) and LAB's attention token.
  // Short of rows the routes row goes first, then the session row: what to do stays to the last;
  // a row with nothing on it gives its place to the next, and rows stand from the top. Labels are
  // dark gray upper case. No frame, gear, beat or moving rule: only the state glyph turns. A press
  // anywhere on it opens the panel.
  function foldedView(){
    const own=LIVE[state],color=own&&own[1]!=='g_work_off'?own[1]:'dim';
    const plain=model.no_color===true||cfg.theme==='mono';
    const P=v=>Array.isArray(v)?v.filter(p=>p&&typeof p==='object'):[];
    const size=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
    const room=Math.max(0,width-1);
    const cut=(parts,limit)=>size(parts)<=limit?parts:limit<1?[]:[...clip(parts,limit-1).parts,text('…','dim')];
    // A row's items, most pressing first: an item is `head` (whole or not at all) and `tail` (cut
    // with `…` where cells run short, while it keeps four cells; a `titled` item without its tail
    // goes whole); the first item that does not fit ends the row.
    const pack=(items,room=Math.max(0,width-1))=>{
      const out=[];let used=0;
      for(const item of items){
        const gap=out.length&&!item.choices?[text(' │ ','dim')]:[],head=[...gap,...item.head],tail=item.tail||[];
        if(used+size(head)+size(tail)<=room){out.push(...head,...tail);used+=size(head)+size(tail);continue;}
        // The question's line keeps its cells (it never gives them to its choices).
        // The choices after the question's first line: whole, else the first one cut with `…` and
        // the rest counted (`+N`), never nothing.
        if(item.choices){
          const first=choiceItems(item.head)[0],left=Math.max(0,(item.total||0)-1);
          for(const rest of [left?[text(' +'+left,'dim')]:[],[]]){
            const space=room-used-size(item.lead)-size(rest);
            if(first&&space>=2){out.push(...item.lead,...cut(first,space),...rest);break;}
          }
          continue;
        }
        const left=room-used-size(head);
        if(tail.length&&left>=5)out.push(...head,...cut(tail,left));
        else if(left>=0&&!item.titled)out.push(...head);
        else if(!out.length)out.push(...cut([...item.head,...tail],room));
        break;
      }
      return out;
    };
    // 1. The session row.
    const tag=/^\[[^\]?]{1,16}\]$/.test(String(model.tag||''))?clean(String(model.tag).slice(1,-1)):'';
    const glyph=own?[{...text(own[0]==='spin'?spinGlyph.text:own[0],own[1]),...(own[0]==='spin'?{spin:true}:{}),role:'session'}]:[];
    const number=tag?[text('[','dim'),model.steward===true?{...text(tag,'tag_steward'),role:'steward',...(plain?{bold:false,dim:false}:{})}:{...text(tag,own?own[1]:'dim'),role:'session'},text(']','dim')]:[];
    const lead=[...glyph,...(glyph.length&&number.length?[text(' ')]:[]),...number];
    const project=Array.isArray(model.repo_side)?P(model.repo_side):P(model.repo).filter(p=>!/^\[[^\]]*\] ?$/.test(String(p.text)));
    // `CTX` stands for the word `context` (a backend without `context_side` has its word taken off here, as the open panel's).
    const unworded=parts=>{const head=parts[0],rest=String(head?.text??'').replace(/^context /,'');return !head||rest===String(head.text)?parts:rest?[{...head,text:rest},...parts.slice(1)]:parts.slice(1);};
    const selected=selectedParts(),gauge=Array.isArray(model.context_side)?P(model.context_side):unworded(P(model.context));
    const ctxLabel=text('CTX ','dim'),ctx=gauge.length?[ctxLabel,...gauge,...(P(model.mcp).length?[text(' │ ','dim'),...P(model.mcp)]:[])]:[];
    const windows=[];let current=[];
    for(const part of P(model.quota)){if(String(part.text)===' │ '){if(current.length)windows.push(current);current=[];}else current.push(part);}
    if(current.length)windows.push(current);
    const quotaParts=new Set(windows.flat());
    const kind=g=>{const label=g.map(part=>String(part.text)).join('').trim().split(/\s+/)[0];return label==='7d'?'week':/^\d+[mhd]$/.test(label)?'window':'model';};
    const bare=gs=>gs.map(g=>g.filter(part=>!/^\(↻/.test(String(part.text))));
    const joinAll=groups=>groups.filter(g=>g.length).flatMap((g,i)=>i?[text(' │ ','dim'),...g]:g);
    // The checkout after the project, as Fleet's session row writes it (` (branch)`, twelve cells at most).
    const branchName=clean(String(P(model.git_side)[0]?.text??'')).trim().split(/\s+/)[0]||'';
    const branch=branchName&&project.length?[text(' ('+(measure(branchName)>12?clip([text(branchName)],11).parts.map(x=>x.text).join('')+'…':branchName)+')','dim')]:[];
    const sessionRow=()=>{
      // Each step lets one more value go: the branch, the reset times, the effort, the project, 5h
      // (the windows shorter than 7d), the model windows from the last, 7d, the model, the context.
      const steps=[];let q=windows,m=selected,named=true,c=ctx,b=branch;
      const add=()=>steps.push([[...lead,...(named&&project.length?[...(lead.length?[text(' ')]:[]),...project,...b]:[])],m,c,...q]);
      add();b=[];add();q=bare(q);add();m=selected.slice(0,1);add();named=false;add();
      q=q.filter(g=>kind(g)!=='window');add();
      while(q.some(g=>kind(g)==='model')){const at=q.map(kind).lastIndexOf('model');q=q.filter((_,i)=>i!==at);add();}
      q=[];add();m=[];add();c=[];add();
      for(const groups of steps){const row=joinAll(groups);if(size(row)<=room)return row;}
      return cut(lead,room);
    };
    // 2. What to do now.
    const first=model.wait&&summary?P(summary.line).map(p=>({...p,text:clean(p.text)})).filter(p=>p.text.trim()):[];
    // 11-15 (popup design E): where cells are left, the choices follow on the same row, whole or as
    // their count alone (`· +4`).
    const choiceRow=summary&&Array.isArray(summary.rows)&&summary.choices>0?P(summary.rows[1]).filter((p,i)=>i>0||String(p.text).trim()):[];
    const tidy=typeof model.tidy_notice==='string'?clean(model.tidy_notice).trim():'';
    // A wait answered elsewhere (#1633): where, dim, after the wait (never part of its count).
    const guide=typeof model.wait_guide==='string'?clean(model.wait_guide).trim():'';
    const todo=[
      ...(model.wait?[{head:[text(clean(model.wait),'g_blocked')],tail:first.length?[text(' · ','dim'),...first]:[]}]:[]),
      ...(first.length&&choiceRow.length?[{choices:true,lead:[text(' · ','dim')],total:summary.choices,head:[text(' · ','dim'),...choiceRow]}]:[]),
      ...(guide?[{titled:true,head:[],tail:[text(guide,'dim')]}]:[]),
      ...(model.warning?[{head:[text('⚠ ','lvl_r')],tail:[text(clean(model.warning),'lvl_r')]}]:[]),
      ...(tidy&&!model.answerable?[{head:[text('⚠ ','lvl_y')],tail:[text(tidy,'lvl_y')]}]:[])];
    // 3. The routes, LAB, the sessions, the new messages (11-15, popup design E): one group each
    // (`4 ROUTE ⠏2 ✕1 · ✕ title │ 4 LAB failed 1 │ 5 SESSIONS ◑2 │ MESSAGES ←2`), a press on a group
    // opens its popup. The route count and the groups after it are whole or leave from the last; the
    // failed routes' titles (else the running ones') take the cells between, cut with `…`.
    const counted=routes.slice(0,Math.max(0,model.route_count??routes.length));
    const total=Number.isInteger(model.route_total)&&model.route_total>0?model.route_total:counted.length;
    const label=clean(model.route_label||'').trim().replace(/^route (\d+)/,'$1 ROUTE')||(total?total+' ROUTE':'');
    const labelMatch=/^(\d+) ROUTE(.*)$/u.exec(label);
    // The count, then the name tag in dark gray as every other one, then the counts by state.
    const labelParts=labelMatch?[text(labelMatch[1],'loc_repo'),text(' '),{...text('ROUTE','dim'),role:'title'},...(labelMatch[2]?[text(labelMatch[2],'dim')]:[])]:label?[{...text(label,'dim'),role:'title'}]:[];
    const failed=counted.filter(r=>r&&r.kind==='dead'),running=counted.filter(r=>r&&r.kind==='working');
    const titles=(failed.length?failed:running).filter(r=>clean(r.title).trim()).map(r=>[
      {...(r.paint?.glyph||text(r.kind==='dead'?'✕':'·',r.kind==='dead'?'g_dead':'dim')),role:'state:'+r.kind},text(' '),...line(clean(r.title).trim(),'loc_repo')]);
    const unread=Number.isInteger(model.messages_new)&&model.messages_new>0?model.messages_new:0;
    const labCounts=model.lab&&Array.isArray(model.lab.counts)?model.lab.counts.map(P).filter(c=>c.length):[];
    const folks=model.sessions&&typeof model.sessions==='object'?model.sessions:null;
    const folkCounts=folks&&Array.isArray(folks.counts)?folks.counts.map(P).filter(c=>c.length):[];
    const groupsAfter=[
      // LAB stands only while it needs a look (failed, stuck, unconfirmed, unnamed runs).
      ...(labCounts.length&&model.lab.strip_attention===true?[{action:'popup:LAB',parts:[...(model.lab.count?[text(clean(model.lab.count),'loc_repo'),text(' ')]:[]),{...text('LAB','dim'),role:'title'},text(' '),...labCounts[0]]}]:[]),
      ...(folks&&folkCounts.length?[{action:'popup:SESSIONS',parts:[...(Number.isInteger(folks.count)?[text(String(folks.count),'loc_repo'),text(' ')]:[]),{...text('SESSIONS','dim'),role:'title'},text(' '),...folkCounts[0]]}]:[]),
      ...(unread?[{action:'popup:MESSAGES',parts:[{...text('MESSAGES','dim'),role:'title'},text(' ←'+unread,'loc_repo')]}]:[])];
    const bar=()=>text(' │ ','dim');
    const routeRow=(()=>{
      const out=[];let used=size(labelParts);
      if(used>room)return {parts:cut(labelParts,room),spans:[]};
      const fixed=[];
      for(const g of groupsAfter){const w=(used||fixed.length?3:0)+size(g.parts);if(used+w>room)break;used+=w;fixed.push(g);}
      const spans=[];let x=0;
      // A group is one press target however many pieces it has (the count and the titles after it).
      const put=(parts,action)=>{const w=size(parts),last=spans.at(-1);
        if(action&&last&&last.action===action&&last.right===x-1)last.right=x+w-1;else if(action)spans.push({left:x,right:x+w-1,action});
        out.push(...parts);x+=w;};
      put(labelParts,'popup:ROUTE');
      // The titles: the first cut to what is left (four cells at least), each further one whole.
      let left=room-used;
      titles.forEach((t,i)=>{
        const sep=labelParts.length||i?[text(' · ','dim')]:[],w=size(sep)+size(t);
        if(w<=left){put([...sep,...t],'popup:ROUTE');left-=w;}
        else if(!i&&left-size(sep)>=5){const c=cut(t,left-size(sep));put([...sep,...c],'popup:ROUTE');left=0;}
      });
      for(const g of fixed){if(out.length)put([bar()]);put(g.parts,g.action);}
      return {parts:out,spans};
    })();
    // 11-15 (panel polish 1): while the row of what to do carries a question to answer here it stands
    // on that question's fill, the strip's whole width (mono, eight colors and NO_COLOR: the rose bar).
    const here=first.length>0&&summary.answerable===true;
    const grounded=(model.colors??256)>=256&&model.no_color!==true&&cfg.theme!=='mono';
    const todoRow=()=>!todo.length?[]:here&&!grounded?[{...text('▌','g_blocked'),role:'accent'},text(' '),...pack(todo,Math.max(0,width-3))]:pack(todo);
    const rowsOf={session:sessionRow(),todo:todoRow(),routes:routeRow.parts};
    // What stays, by rank: what to do, the session, the routes; a row with nothing on it gives its place.
    const strip=height>=2,count=strip?height-1:1;
    const chosen=new Set(['todo','session','routes'].filter(name=>rowsOf[name].length).slice(0,count));
    const order=['session','todo','routes'].filter(name=>chosen.has(name));
    const out=[],hits=[],groups=[],named=[];
    const pad=parts=>[...parts,text(' '.repeat(Math.max(0,width-size(parts))))];
    const withMark=parts=>{const body=cut(parts,Math.max(0,width-2));return [...body,text(' '.repeat(Math.max(0,width-1-size(body)))),text('▴','dim')];};
    if(strip)out.push(width>=3?[{...text('─'.repeat(width-2),'dim'),role:'messages'},text(' '),text('▴','dim')]:withMark([]));
    for(const name of order){
      const parts=rowsOf[name];
      named[out.length]=name;
      const row=strip?pad(parts):withMark(parts);
      out.push(name==='todo'&&here&&grounded?row.map(part=>({...part,ground:'fill'})):row);
      if(name==='session')groups.push('head',...(parts.includes(ctxLabel)?['context']:[]),...(parts.some(part=>quotaParts.has(part))?['quota']:[]));
      if(name==='todo'&&model.wait)groups.push('wait');
      if(name==='routes'&&label)groups.push('route');
    }
    if(!strip&&!out.length)out.push(withMark([]));
    while(out.length<height)out.push(pad([]));
    // 11-15 (popup design E): the row of what to do opens the question popup while it carries the
    // question; the rule with `▴` and every other row open the panel.
    // The routes row's groups open their popups; a press between them opens the panel.
    for(let i=0;i<out.length;i++){
      if(named[i]==='routes')for(const span of routeRow.spans)hits.push({row:i,...span});
      hits.push({row:i,left:0,right:width-1,action:named[i]==='todo'&&first.length?'question':'unfold'});
    }
    const painted=appearance(out,{...cfg,shade:'none'},{colors:model.colors??256,noColor:model.no_color===true,background});
    const spins=out.map((row,i)=>row.some(part=>part.spin)?i:-1).filter(i=>i>=0);
    return {rows:painted,hits,color,groups:[...new Set(groups)],animated:spins.length>0,spins,motion:null,folded:true};
  }
  // Spec rule 49 (a), (b), (e): the panel's last row inside carries the gear `⚙` (dim) at its
  // right end and, three cells left of it, the fold button: `»` beside the native area (`«`
  // for a left panel), `▾` under it (the bottom layout, or a panel opened by hand where it
  // cannot stand beside). Their presses stay apart (the cell between them is neither's). The
  // row stays however short rows run, so the panel can always fold; whatever shares it
  // (5h/7d) takes the cells left of the buttons.
  const foldMark=side?cfg.position==='left'?'«':'»':'▾';
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
  function lastRow(left,groups,head=0,floor=1){
    const rows=height-2;
    for(let over=content.length-(rows-1);over>0&&head>floor;over--){
      const at=--head;content.splice(at,1);contentGroups.splice(at,1);
      for(let i=hits.length-1;i>=0;i--){if(hits[i].row===at+1)hits.splice(i,1);else if(hits[i].row>at+1)hits[i].row--;}
    }
    if(content.length>rows-1){content.length=rows-1;contentGroups.length=rows-1;for(let i=hits.length-1;i>=0;i--)if(hits[i].row>rows-1)hits.splice(i,1);}
    if(fill)while(content.length<rows-1)add([]);
    add(footRow(left),undefined,groups);
  }
  // Spec rule 52: the thin rule under the metadata is the plain thin `─` in every state; the
  // session's moving state is the outer frame's (below).
  // Spec rule 47 (11-15 panel polish 4): this session's own state (not a route's) moves the frame or
  // the head rule, by the setting `rule motion` (`on`, saved before 11-15, reads as `rule`). Running:
  // `rule` keeps the frame still and lit and passes a light over the head rule, `border` sends one arc
  // round the frame, `scanners` a wave. An answer or confirmation wait breathes the whole frame red
  // under every setting but `off`. Anything else, and `off`, mono, NO_COLOR, eight colors or the
  // settings screen: the still frame in the state's color and the plain rule. The host repaints only
  // the changed cells, at the frames they change.
  const borderColors=model.colors??256;
  const motionSetting=OPTIONS.rule_motion.includes(cfg.rule_motion)?cfg.rule_motion:'rule';
  const borderStill=motionSetting==='off'||model.no_color===true||cfg.theme==='mono'||borderColors<256;
  const running=!settings&&!borderStill&&LIVE[state]?.[0]==='spin';
  const moving=settings||borderStill?'':state==='blocked'?'breathe':running&&motionSetting!=='rule'?motionSetting:'';
  const ruleMoving=running&&motionSetting==='rule';
  const breathSteps=borderColors>256?BORDER_BREATHE_STEPS:BORDER_BREATHE_STEPS_256;
  // Correction 8 (11-13): `since` is the frame this state began (the host's). The motion is already
  // out on that very paint (the arc's head just right of the head's words, the rule's light coming
  // in) and the breath starts at its brightest. Without it the beat runs on the clock alone.
  const anchored=Number.isFinite(since),borderTick=t=>anchored?Math.trunc(t)-Math.trunc(since):Math.trunc(t);
  const ruleTick=t=>borderTick(t)+(anchored?1:0);
  // Spec rule 52: the thin rule under the metadata; under `rule` while the session runs, its cells
  // carry the light (`rule` marks them, so the host gets them as the moving cells).
  const ruleLook=(i,tick)=>{const level=scanLevel(ruleTick(tick),i,inner);
    return level?{...text('─','g_spin'),role:'orbit',tone:'scan',level,rule:true}:{...text('─','dim'),role:'messages',rule:true};};
  const ruleParts=()=>ruleMoving?Array.from({length:inner},(_,i)=>ruleLook(i,frame)):[{...text('─'.repeat(inner),'dim'),role:'messages',rule:true}];
  // Values the host could not refresh for a while are shown, not claimed:
  // nothing spins and the frame takes no route state (spec rule 12).
  const stale=model.stale===true;
  const live=r=>!stale&&r?.kind==='working';
  // A route's glyph: a working route spins on the frame like Fleet's route mark.
  // A finished result that did not pass (its `outcome_key` is the failure's) takes the failure's mark and hue.
  const routeGlyph=r=>{const g=r.paint?.glyph||text('·');if(live(r))return {...g,...spinGlyph,role:'state:working'};return {...g,role:'state:'+(r.kind==='done'&&r.outcome_key==='g_dead'?'dead':r.kind||'idle')};};
  // A finished route's row leads its title with the result word (`FAIL · title`), so a cut title keeps it.
  const titleOf=(r,fallback='·')=>[...(r.kind==='done'&&r.outcome?[text(r.outcome,r.outcome_key||'lvl_g'),text(' · ','dim')]:[]),...line(r.title||fallback,'loc_repo')];
  const join=(groups,sep=' · ')=>groups.filter(g=>g&&g.length).flatMap((g,i)=>i?[text(sep),...g]:g);
  const asking=!!model.answerable;
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
    const selected=selectedParts();
    // Spec rule 15: the repo row carries the project only; Git takes the row below. Spec rule
    // 29: the session number moves to the top border, so the row keeps `hearting/`.
    let head=painted(model.head),git=painted(model.git);
    if(!Array.isArray(model.git)){const at=head.findIndex(p=>/^ \(/.test(String(p.text)));if(at>=0){git=[{...head[at],text:String(head[at].text).slice(1)},...head.slice(at+1)];head=head.slice(0,at);}}
    const repo=Array.isArray(model.repo)?painted(model.repo):head.filter(p=>!/^\[[^\]]*\] ?$/.test(String(p.text)));
    const jobs=painted(model.jobs),work=painted(model.work),quota=painted(model.quota),mcp=painted(model.mcp);
    // OpenCode's MCP count stays beside the model and context (its native sidebar is hidden).
    const contextParts=mcp.length?joined([painted(model.context),mcp]):painted(model.context);
    // A wait answered elsewhere (11-14): one dim line of where, never part of the count.
    const guide=typeof model.wait_guide==='string'?clean(model.wait_guide).trim():'';
    // 11-15 (popup design E): the answer wait's count leads the QUESTIONS section's head (below).
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
    // Spec rule 46: the newest saved memory records (this repository's and the global ones, two at
    // most) stand in their own section under the messages, one row each (below). An older backend
    // sends the newest one as painted pieces only (`3m decision headline`): that one record,
    // without its kind (rule 46 (e)); the head carries no MEM row either way.
    const memItems=(()=>{
      if(!side)return [];
      if(Array.isArray(model.memory_items))return model.memory_items.filter(m=>m&&typeof m==='object'&&clean(m.text).trim()).slice(0,2)
        .map(m=>({kind:clean(m.kind).trim(),age:clean(m.age).trim(),text:clean(m.text).trim(),tier:clean(m.tier).trim(),scope:clean(m.scope).trim()}));
      const old=painted(model.memory),body=old.length?clean(old.at(-1).text).trim():'',first=old.length>1?clean(old[0].text).trim():'';
      return body?[{kind:'',age:/^\d+[mhd](?: \d+[mh])?$/.test(first)?first:'',text:body}]:[];
    })();
    // The LAB section (spec rule 59, 11-15): this project's experiment board, worded by the backend
    // with Fleet's LAB-table cells (its head and rows below). Beside the native area only, as MEM.
    const lab=side&&model.lab&&typeof model.lab==='object'&&!Array.isArray(model.lab)?model.lab:null;
    const P=v=>Array.isArray(v)?v.filter(p=>p&&typeof p==='object'):[];
    // A cut name ends in `…` right after its last letter (no space before it).
    const labCut=(parts,limit)=>{const out=cut(parts,limit);if(size(out)<size(parts)&&out.length){const last=out.at(-1);
      if(String(last.text).endsWith('…')){const body=String(last.text).slice(0,-1).replace(/\s+$/u,'');
        if(body)return [...out.slice(0,-1),{...last,text:body+'…'}];
        const before=out.slice(0,-1);if(before.length){const prev=before.at(-1);return [...before.slice(0,-1),{...prev,text:String(prev.text).replace(/\s+$/u,'')},last];}}}
      return out;};
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
      const content=title||now||(tidy&&!asking);
      // Spec rule 34 (c): the rule parts the head's block from what follows it; it is not an empty row
      // but it stands without a title too (11-15 small fixes: what the default `rule motion` moves
      // must not wait on a title), where something stands above it.
      // Spec rule 47: it follows this session's state (above).
      if(content||top.length){rules.add(top.length);top.push(ruleParts());}
      // Spec rule 44: the title's value bold, in this session's harness color (Fleet's `hb_*`;
      // the label stays dim). No color keeps the weight, as the steward's `⚑` (rule 35 (a)).
      const own='hb_'+(['claude','codex','opencode'].includes(harness)?harness:'other');
      if(title)field('TITLE',[{...text(title,own),bold:true}]);
      if(now)field('NOW',line(now,nowKey));
      if(tidy&&!asking){
        const rows=[];wrap(line('⚠ '+tidy,'lvl_y'),4,undefined,rows);
        const kept=rows.slice(0,3);
        if(rows.length>kept.length){const last=kept.length-1,fit=clip(kept[last],inner-1);kept[last]=[...fit.parts,text('…','lvl_y')];}
        top.push(...kept);
      }
      // One empty row parts the head from the route box below it (a head with no content has its rule).
      if(content){spacers.add(top.length);top.push([]);}
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
      if(title||now){
        // Title and NOW share what the answer wait leaves: each keeps at least half of it.
        const nowParts=now?[text('NOW ','dim'),text(now,nowKey)]:[],titleParts=title?line(title,'loc_repo'):[];
        const avail=inner-(title&&now?3:0);
        const titleRoom=title&&now?Math.max(Math.min(size(titleParts),Math.floor(avail/2)),avail-size(nowParts)):avail;
        top.push(cut(joined([cut(titleParts,Math.max(1,titleRoom)),cut(nowParts,Math.max(1,avail-(title?Math.min(size(titleParts),titleRoom):0)))]),inner));
      }
    }
    const rows=height-2;
    // 11-15 (popup design E): under the head, one section per kind of work, each its head row and
    // its few rows. The head has one form for every section: the count (where it has one), the name
    // tag in dark gray upper case, the counts by state, a thin rule, and `›` where a press opens the
    // section's popup (Fleet's own table, whole). A press on the head or on any row of a section
    // opens it there; nothing opens in place. The rows are the table's own columns, picked.
    const sectionHead=(count,name,after=[],rights=[[]],arrow=true)=>{
      const lead=[...count,...(count.length?[text(' ')]:[]),{...text(name,'dim'),role:'title'}];
      const close=arrow?[text(' '),text('›','dim')]:[];
      for(const a of [after,[]])for(const r of rights){
        const head=[...lead,...(a.length?[text(' '),...a]:[])],tail=[...(r.length?[text(' '),...r]:[]),...close];
        const rest=inner-size(head)-size(tail);
        if(rest>=2)return [...head,text(' '),{...text('─'.repeat(rest-1),'dim'),role:'messages'},...tail];
        if(rest>=0)return [...head,pad(rest),...tail];
      }
      return cut(lead,inner);
    };
    // QUESTIONS (11-15, popup design A): the answer wait's count leads the name (`3 QUESTIONS`, the
    // `◑답 대기 N` row merged into it), then the answers on their way (one dim line each), the next
    // question's summary (its first row and its choices on one row, or its first two rows) and, for
    // a wait answered elsewhere, where (#1633). Ctrl+] or a press opens the question popup.
    const accent=parts=>parts.flatMap(part=>{
      const s=String(part.text??''),m=/^(\s*)([◑›])(.*)$/su.exec(s);
      if(!m)return [part];
      return [...(m[1]?[{...part,text:m[1]}]:[]),{...part,text:m[2],role:'accent'},...(m[3]?[{...part,text:m[3]}]:[])];
    });
    const waitCount=(()=>{const m=/(\S+)\s*$/u.exec(clean(model.wait||'').trim());return model.wait&&m?m[1]:'';})();
    const sentRows=(typeof sent==='function'?sent()||[]:[]).map(row=>cut(row,inner));
    const questionCount=waitCount||(summary?String(summary.count):'');
    const asked=!!(summary||sentRows.length||model.wait||guide);
    let briefTwo=true,keepGuide=true,keepSent=true,keepAction=true;
    // 11-15 (panel polish 1): the question to answer here, now, reads as one button: its rows stand
    // on the rose fill across the panel's inside width (its first row bright, its choices numbered
    // chips) over `↵ answer · Ctrl+]` in rose at the right. Short of rows the action row goes first,
    // then the choices; the first row stays to the last. Mono, eight colors and NO_COLOR lead each
    // row with a rose bar `▌` instead. A wait answered elsewhere and the answers on their way are
    // never painted: what is painted is what a press answers now.
    const here=!!summary&&summary.answerable===true;
    const grounded=(model.colors??256)>=256&&model.no_color!==true&&cfg.theme!=='mono';
    const bar=()=>({...text('▌','g_blocked'),role:'accent'});
    const button=row=>{
      if(!grounded)return [bar(),text(' '),...cut(row,Math.max(0,inner-2))];
      const fit=cut(row,inner);return [...fit,pad(inner-size(fit))].map(part=>({...part,ground:part.ground||'fill'}));
    };
    // The choices' row as chips (` 1 label ` on the chip ground, one fill cell apart, the first under
    // the question's words); short of cells the last chips leave for their count (`+N`).
    const chips=parts=>{
      const items=choiceItems(parts).map(c=>c.map((part,i)=>i?(part.dim===true?part:{...part,role:'bright'}):{...part,role:'accent'}));
      const more=Math.max(0,...parts.map(part=>{const m=/^(?: · )?\+(\d+)$/u.exec(clean(part.text));return m?Number(m[1]):0;}));
      const chip=c=>[text(' '),...c,text(' ')].map(part=>({...part,ground:'chip'}));
      const count=n=>n?[text(' +'+n,'dim')]:[];
      for(let k=items.length;k>=1;k--){
        const row=[text(' '),...items.slice(0,k).flatMap((c,i)=>[...(i?[text(' ')]:[]),...chip(c)]),...count(items.length-k+more)];
        if(size(row)<=inner)return row;
      }
      // Not even the first chip fits beside the count: it shows with its name cut (`…`), its number
      // kept, and the rest are counted; narrower still, the count gives way before the choice.
      if(items.length)for(const rest of [count(items.length-1+more),[]]){
        const room=inner-1-2-size(rest);
        if(room>=2||!rest.length)return [text(' '),...chip(cut(items[0],Math.max(1,room))),...rest];
      }
      return [text(' '),...count(more)];
    };
    // `↵ answer · Ctrl+]` at the right; narrow, its first word alone.
    const actionRow=()=>{
      const room=grounded?inner:Math.max(0,inner-2),words=t('answer_action','↵ answer · Ctrl+]'),first=words.split(' · ')[0];
      const shown=measure(words)<=room?words:first,a=[{...text(shown,'g_blocked'),role:'accent'}];
      return [pad(room-size(a)),...a];
    };
    const summaryRows=()=>{
      const list=P(briefTwo?summary.rows:summary.one).map(row=>accent(P(row)));
      if(!here)return list.map(row=>cut(row,inner));
      const out=list.map((row,i)=>!i?row.map(part=>part.role==='accent'?part:{...part,role:'bright'}):i===1&&summary.choices>0&&grounded?chips(row):row);
      if(keepAction&&briefTwo)out.push(actionRow());
      return out.map(button);
    };
    const questionRows=()=>[...(keepSent?sentRows:[]),
      ...(summary?summaryRows():[]),
      ...(guide&&keepGuide?[cut([text(guide,'dim')],inner)]:[])];
    // ROUTE: the current routes the statusline counts and the recent results the producer keeps
    // after them, the failed first, then the running, the waiting, the finished (`✕` → `⠏` → `●`
    // → `✓`); each its state, ROUTE, PROGRESS and AGE (Fleet's ROUTES columns, picked).
    const countedRoutes=routes.slice(0,Math.max(0,model.route_count??routes.length));
    const shown=countedRoutes.length?[...countedRoutes,...routes.slice(countedRoutes.length).filter(r=>r&&r.state==='done'&&r.outcome)]:countedRoutes;
    const total=Number.isInteger(model.route_total)&&model.route_total>=countedRoutes.length?model.route_total:countedRoutes.length;
    const failedOf=r=>r.kind==='dead'||r.kind==='done'&&r.outcome_key==='g_dead';
    const RANK={dead:0,working:1,blocked:2,idle:2,stale:3,done:4};
    const rankOf=r=>failedOf(r)?0:RANK[r.kind]??2;
    const ranked=shown.map((r,i)=>[r,i]).sort((a,b)=>rankOf(a[0])-rankOf(b[0])||a[1]-b[1]).map(([r])=>r);
    const markOf=stage=>stage.mark==='●'?pulse('●'):stage.mark==='✓'?text('✓','lvl_g'):stage.mark==='✕'?text('✕','g_dead'):text('·','dim');
    // PROGRESS: the route's kind and where it stands (`one-shot ✕`, `3/7 execute ●`), as Fleet's row.
    function shapeCell(r){
      const shape=String(r.shape||''),stages=Array.isArray(r.stages)?r.stages:[];
      const current=stages.findIndex(stage=>stage.current||stage.mark==='✕'),next=stages.findIndex(stage=>stage.mark!=='✓');
      if(shape==='one-shot-owner'){
        const mark=current>=0?[text(' '),markOf(stages[current])]:r.kind==='dead'?[text(' '),text('✕','g_dead')]:[];
        return [text('one-shot','dim'),...mark];
      }
      if(/^staged/.test(shape)&&stages.length){
        const at=current>=0?current:next>=0?next:stages.length-1;
        return [text((at+1)+'/'+stages.length+' ','dim'),text(stages[at].name||'·','loc_repo'),...(current>=0?[text(' '),markOf(stages[at])]:[])];
      }
      if(shape&&shape!=='unknown'){
        return current>=0?[text(stages[current].name||'·','loc_repo'),text(' '),markOf(stages[current])]:[text(shape,'dim')];
      }
      return r.step?[text(r.step)]:[];
    }
    const routeLabel=clean(model.route_label||'').trim().replace(/^⏳ /,'');
    const labelMatch=/^route (\d+)(.*)$/u.exec(routeLabel);
    const routeCount=labelMatch?labelMatch[1]:shown.length?String(total):'';
    const routeTally=labelMatch?labelMatch[2].trim():'';
    const running=shown.some(live);
    const countPart=v=>running?pulse(v):text(v,'loc_repo');
    // Spec rule 45 (a): beside the native area the count leads (`4 ROUTE ⠏2 ✕1`); under it `route 4 ⠏2 ✕1`.
    const routeHead=()=>side?sectionHead(routeCount?[countPart(routeCount)]:[],'ROUTE',routeTally?[text(routeTally,'dim')]:[]):
      sectionHead([],'route',[...(routeCount?[countPart(routeCount)]:[]),...(routeTally?[text(' '+routeTally,'dim')]:[])]);
    const routeShown=!!(shown.length||routeLabel);
    // The `route line` setting (spec rule 57) picks the row's last value: AGE (time), or the doer's model.
    const routeLine=['time','model'].includes(cfg.route_line)?cfg.route_line:'time';
    const lastOf=r=>routeLine==='model'?(r.paint?.model_text?[{...r.paint.model_text}]:[]):r.elapsed?[text(r.elapsed,'dim')]:[];
    const progressWidth=Math.max(0,...ranked.map(r=>size(shapeCell(r)))),ageWidth=Math.max(0,...ranked.map(r=>size(lastOf(r))));
    const routeRow=r=>{
      const lead=[routeGlyph(r),text(' ')],name=titleOf(r),progress=shapeCell(r),age=lastOf(r);
      // Short of cells PROGRESS goes first, then AGE; the title keeps ten cells (or its whole width).
      for(const [p,a] of [[progressWidth,ageWidth],[0,ageWidth],[0,0]]){
        const right=[...(p?[...progress,pad(p-size(progress))]:[]),...(p&&a?[text('  ')]:[]),...(a?[pad(a-size(age)),...age]:[])];
        const room=inner-size(lead)-(right.length?size(right)+1:0);
        if(room>=Math.min(10,size(name))||!right.length){const n=cut(name,Math.max(1,room));return [...lead,...n,pad(room-size(n)),...(right.length?[text(' '),...right]:[])];}
      }
      return cut([...lead,...name],inner);
    };
    const routeRows=()=>ranked.length?ranked.map(routeRow):routeLabel?[line(t('checking','Checking'))]:[];
    // LAB (spec rule 59): this project's experiment board, worded by the backend with Fleet's LAB-table
    // cells: `N LAB ──── failed 1 waiting 1 ›`, then the conditions in the board's order of what to
    // look at (failed, stuck, unconfirmed, …, the best), each CONDITION, STATE, PROGRESS and DIFF.
    // A value is whole or its column leaves; only a condition name ends in `…`. Beside the native
    // area only. A failure here never colors the frame or the glyph.
    const labRowsOf=()=>lab&&Array.isArray(lab.rows)?lab.rows.filter(r=>r&&typeof r==='object'):[];
    const labHead=()=>{
      const counts=(Array.isArray(lab.counts)?lab.counts:[]).map(P).filter(c=>c.length);
      const spaced=list=>list.flatMap((c,i)=>i?[text(' '),...c]:c);
      // Short of cells the later counts go first.
      return sectionHead(lab.count?[text(clean(lab.count),'loc_repo')]:[],'LAB',[],counts.map((_,i)=>spaced(counts.slice(0,counts.length-i))).concat([[]]));
    };
    const LAB_GAP=2,LAB_NAME=6;
    const labLines=n=>{
      const list=labRowsOf().slice(0,n),notes=(Array.isArray(lab.notes)?lab.notes:[]).map(P).filter(x=>x.length).map(x=>cut(x,inner));
      // Without a condition the goal (or why there is none) stands; a note (a catalog not read, another
      // group to look at) takes a row the conditions leave.
      if(!list.length)return [...(P(lab.goal).length?[cut(P(lab.goal),inner)]:[]),...notes].slice(0,n);
      const cellOf=(r,c)=>r.cells&&r.cells[c]&&typeof r.cells[c]==='object'?r.cells[c]:text('');
      const width=c=>Math.max(0,...list.map(r=>measure(clean(cellOf(r,c).text))));
      const room=f=>inner-f.reduce((n,c)=>n+width(c)+LAB_GAP,0);
      const cols=[['state','progress','delta'],['state','progress'],['state'],[]].find(f=>room(f)>=LAB_NAME)||[];
      const nameWidth=Math.max(1,room(cols));
      return list.map(r=>{
        const name=labCut(r.name&&typeof r.name==='object'?[r.name]:[text('')],nameWidth),out=[...name,pad(nameWidth-size(name))];
        // STATE reads from the left; PROGRESS and DIFF are numbers, right-aligned.
        for(const c of cols){const v=[cellOf(r,c)];out.push(...(c==='state'?[pad(LAB_GAP),...v,pad(width(c)-size(v))]:[pad(LAB_GAP+width(c)-size(v)),...v]));}
        return out;
      }).concat(notes).slice(0,n);
    };
    // SESSIONS (popup design D, new): the sessions on this machine from Fleet's session table, those
    // a person must answer first, then the running, then the idle, this window's own left out; each
    // its state glyph, ID, TITLE and AGE. Beside the native area only, as LAB and MEM.
    const sessions=side&&model.sessions&&typeof model.sessions==='object'&&Array.isArray(model.sessions.rows)?model.sessions:null;
    const sessionList=sessions?sessions.rows.filter(s=>s&&typeof s==='object'&&!s.own):[];
    const sessionHead=()=>{
      const counts=(Array.isArray(sessions.counts)?sessions.counts:[]).map(P).filter(c=>c.length);
      return sectionHead(Number.isInteger(sessions.count)?[text(String(sessions.count),'loc_repo')]:[],'SESSIONS',
        counts.flatMap((c,i)=>i?[text(' '),...c]:c));
    };
    const sessionRow=s=>{
      const glyph=P(s.glyph).map(p=>SPIN.includes(String(p.text))?{...p,...spinGlyph}:p);
      const lead=[...glyph,text(' '),...P(s.tag),text(' ')],age=P(s.age),title=P(s.title);
      const room=inner-size(lead)-(age.length?size(age)+1:0),n=cut(title,Math.max(1,room));
      return room>=4?[...lead,...n,pad(room-size(n)),...(age.length?[text(' '),...age]:[])]:cut([...lead,...title],inner);
    };
    // MESSAGES (spec rules 31, 45 (b), 58): the requests this session sent (→) or received (◂), then
    // the recent messages, one row each (`← [e7] text`), three at most; `+N` counts the rest. The
    // popup reads them whole.
    const messages=painted(model.messages);
    const requestList=Array.isArray(model.requests)?model.requests.filter(q=>q&&typeof q.id==='string'):[];
    const requestLine=q=>{
      const mark=text(String(q.glyph||q.way||'→'),q.glyph==='✕'?'lvl_r':q.glyph==='✓'?'lvl_g':'loc_repo');
      const who=q.who?[text(' '+q.who,'dim')]:[],title=text(' '+String(q.title||q.id),'loc_repo');
      const status=q.status?[text(' · ','dim'),text(String(q.status),q.kind==='request-rejected'?'lvl_r':'dim')]:[];
      const stay=[mark,...who,...status];
      return size([mark,...who,title,...status])<=inner?[mark,...who,title,...status]:
        size(stay)+4<=inner?[mark,...who,...cut([title],inner-size(stay)),...status]:cut([mark,title],inner);
    };
    const messageLine=m=>cut([text(m.way,m.way==='←'?'lvl_g':'loc_repo'),text(' '),text(String(m.who||''),'loc_repo'),text(' '),text(String(m.body||m.text||''),'loc_repo')],inner);
    const messageTotal=requestList.length+messages.length;
    const messageHead=n=>{const hidden=messageTotal-Math.min(n,messageTotal);return sectionHead([],side?'MESSAGES':'messages',hidden>0?[text('+'+hidden,'loc_repo')]:[]);};
    // The requests first, then the newest messages (in their order, the newest last).
    const messageRows=n=>{const req=requestList.slice(0,n).map(requestLine),left=n-req.length;return [...req,...(left>0?messages.slice(-left).map(messageLine):[])];};
    // MEM (spec rule 46; 11-15 panel polish 5): the newest saved memory records of this repository and
    // the global ones together, newest first, two at most, one row each: `◆` durable or `◇` working
    // (`·` for a record that names no tier), then, dim, its scope and its kind as the memory system
    // names them (`◆ project decision headline   10m`, `◇ global lesson …   2h`), the age at the right.
    // Short of cells the headline is cut first, then the kind word leaves, then the scope word (the
    // mark and the age stay). No popup: its head has no `›`.
    const memRow=m=>{
      const age=m.age?[text(m.age,'dim')]:[],tail=age.length?size(age)+1:0;
      const mark=text((m.tier==='durable'?'◆':m.tier==='working'?'◇':'·')+' ','dim'),words=[m.scope,m.kind].filter(Boolean);
      for(let k=words.length;k>=0;k--){
        const lead=[mark,...words.slice(0,k).map(w=>text(w+' ','dim'))],room=inner-size(lead)-tail;
        if(room>=4||!k){const n=cut([text(m.text,'loc_repo')],Math.max(1,room));
          return room>=4?[...lead,...n,pad(room-size(n)),...(age.length?[text(' '),...age]:[])]:cut([...lead,text(m.text,'loc_repo')],inner);}
      }
      return [];
    };
    // The sections in order, each with the most rows it takes and its rows at a given count.
    const SECTIONS=[
      {name:'QUESTIONS',on:asked,max:3,head:()=>sectionHead(questionCount?[{...text(questionCount,'g_blocked'),role:'accent'}]:[],summary?.label||'QUESTIONS'),
        rows:()=>questionRows(),action:'question',groups:model.wait?['wait']:[]},
      {name:'ROUTE',on:routeShown,max:3,head:routeHead,rows:n=>routeRows().slice(0,n),action:'popup:ROUTE',groups:['route']},
      {name:'LAB',on:!!lab,max:3,head:()=>labHead(),rows:n=>labLines(n),action:'popup:LAB'},
      {name:'SESSIONS',on:!!sessions&&(sessionList.length>0||sessions.count>0),max:3,head:()=>sessionHead(),rows:n=>sessionList.slice(0,n).map(sessionRow),action:'popup:SESSIONS'},
      {name:'MESSAGES',on:messageTotal>0,max:3,head:()=>messageHead(take.MESSAGES),rows:n=>messageRows(n),action:'popup:MESSAGES'},
      {name:'MEM',on:memItems.length>0,max:2,head:()=>sectionHead([],'MEM',[],[[]],false),rows:n=>memItems.slice(0,n).map(memRow)}];
    const take={},kept={};
    for(const s of SECTIONS){take[s.name]=s.max;kept[s.name]=s.on;}
    const sectionRows=s=>s.name==='QUESTIONS'?questionRows():s.rows(take[s.name]);
    const sectionSize=s=>kept[s.name]?1+sectionRows(s).length:0;
    const core=top.length-spacers.size-metaGaps.size-rules.size-optional.size-gitRows.size-extra.size;
    let keepGit=true,keepSpacers=true,keepMetaGap=true,keepRules=true,keepOptional=true,keepMulti=true,keepQuota=quota.length>0;
    const need=()=>core+(keepMulti?extra.size:0)+(keepGit?gitRows.size:0)+(keepSpacers?spacers.size:0)+(keepMetaGap?metaGaps.size:0)
      +(keepRules?rules.size:0)+(keepOptional?optional.size:0)+SECTIONS.reduce((n,s)=>n+sectionSize(s),0)+1;
    const budget=rows;
    // Short of rows (spec rule 34 (b), 11-15): the empty rows and a value's further rows first; then
    // every section down to two rows and then to one, the lowest first; then the structural rows,
    // Git and the running jobs; then MEM gives its row, then its name, then MESSAGES, SESSIONS, LAB
    // and ROUTE the same; QUESTIONS keeps its head and first row to the last; 5h/7d last of all. A
    // step that freed more than it had to gives its rows back, the most important first.
    const gave=[];
    const give=(can,apply,undo)=>{while(need()>budget&&can()){apply();gave.push(undo);}};
    const lowest=['MEM','MESSAGES','SESSIONS','LAB','ROUTE'];
    give(()=>keepSpacers,()=>{keepSpacers=false;},()=>{keepSpacers=true;});
    give(()=>keepMulti,()=>{keepMulti=false;},()=>{keepMulti=true;});
    for(const down of [2,1])for(const name of [...lowest,'QUESTIONS']){
      if(name==='QUESTIONS'){if(down===2)give(()=>here&&keepAction,()=>{keepAction=false;},()=>{keepAction=true;});
        if(down===1){give(()=>!!summary&&briefTwo,()=>{briefTwo=false;},()=>{briefTwo=true;});give(()=>keepGuide&&!!guide,()=>{keepGuide=false;},()=>{keepGuide=true;});}continue;}
      give(()=>kept[name]&&take[name]>down,()=>{take[name]--;},()=>{take[name]++;});
    }
    give(()=>keepMetaGap,()=>{keepMetaGap=false;},()=>{keepMetaGap=true;});
    give(()=>keepRules,()=>{keepRules=false;},()=>{keepRules=true;});
    give(()=>keepGit,()=>{keepGit=false;},()=>{keepGit=true;});
    give(()=>keepOptional,()=>{keepOptional=false;},()=>{keepOptional=true;});
    for(const name of lowest){
      give(()=>kept[name]&&take[name]>0,()=>{take[name]--;},()=>{take[name]++;});
      give(()=>kept[name],()=>{kept[name]=false;},()=>{kept[name]=true;});
    }
    give(()=>keepSent&&sentRows.length>0,()=>{keepSent=false;},()=>{keepSent=true;});
    give(()=>keepQuota,()=>{keepQuota=false;},()=>{keepQuota=true;});
    const choices=()=>JSON.stringify([keepSpacers,keepMulti,keepMetaGap,keepRules,keepGit,keepOptional,keepQuota,briefTwo,keepGuide,keepSent,take,kept,keepAction]);
    for(const undo of gave.reverse()){
      const was=choices();undo();
      if(need()>budget){const v=JSON.parse(was);[keepSpacers,keepMulti,keepMetaGap,keepRules,keepGit,keepOptional,keepQuota,briefTwo,keepGuide,keepSent]=v;
        Object.assign(take,v[10]);Object.assign(kept,v[11]);keepAction=v[12];}
    }
    const order=[];
    top.forEach((row,i)=>{if((keepSpacers||!spacers.has(i))&&(keepMetaGap||!metaGaps.has(i))&&(keepRules||!rules.has(i))&&(keepOptional||!optional.has(i))&&(keepGit||!gitRows.has(i))&&(keepMulti||!extra.has(i)))
      order.push({parts:!keepMulti&&oneRow.has(i)?oneRow.get(i):row,groups:topGroups.get(i)});});
    for(const item of order)add(item.parts,item.action,item.groups);
    // Each section stands whole (its head and the rows it kept), one after another from the top; the
    // rows left over stand between the sections above and MESSAGES·MEM (spec rule 14: those two keep to
    // the panel's bottom, right above the last row), or under the last section when there are none.
    const lower=[];
    for(const s of SECTIONS){
      if(!kept[s.name])continue;
      // QUESTIONS never gives its place to the structural rows above it: those go first (`lastRow`).
      if(s.name!=='QUESTIONS'&&content.length+lower.length+sectionSize(s)>budget-1)break;
      const into=['MESSAGES','MEM'].includes(s.name)?(parts,action,groups)=>lower.push({parts,action,groups}):add;
      into(s.head(),s.action,s.groups);
      // A route's or a session's own row opens the popup with it unfolded.
      sectionRows(s).forEach((row,i)=>into(row,s.name==='ROUTE'&&ranked[i]?'popup:ROUTE:'+ranked[i].id:
        s.name==='SESSIONS'&&sessionList[i]?'popup:SESSIONS:'+clean(P(sessionList[i].tag).map(p=>p.text).join('')).replace(/^\[|\]$/g,''):s.action));
    }
    if(fill&&lower.length)while(content.length+lower.length<height-2-1)add([]);
    for(const row of lower)add(row.parts,row.action,row.groups);
    // A reset time that does not fit goes whole, never as a cut `(↻ …`. Spec rule 49 (b): 5h/7d
    // share the last row with the fold button and the gear, which stay however short rows run.
    // Correction 7 (11-13): the model windows (Fable's) stand after 7d; short of cells the row
    // gives way in this order: the reset times, then 5h (the windows shorter than 7d), then the
    // model values from the last, then 7d. Each value goes whole.
    const quotaRoom=inner-BUTTONS-1;
    const quotaRow=(()=>{
      const groups=[];let cur=[],sep=null;
      for(const part of quota){if(String(part.text)===' │ '){sep=sep||part;if(cur.length)groups.push(cur);cur=[];}else cur.push(part);}
      if(cur.length)groups.push(cur);
      const kind=g=>{const label=g.map(part=>String(part.text)).join('').trim().split(/\s+/)[0];return label==='7d'?'week':/^\d+[mhd]$/.test(label)?'window':'model';};
      const join=gs=>gs.flatMap((g,i)=>i&&sep?[sep,...g]:g);
      let kept=groups.map(g=>g.filter(part=>!/^\(↻/.test(String(part.text))));
      const tries=[groups,kept];
      kept=kept.filter(g=>kind(g)!=='window');tries.push(kept);
      while(kept.some(g=>kind(g)==='model')){const at=kept.map(kind).lastIndexOf('model');kept=kept.filter((_,i)=>i!==at);tries.push(kept);}
      tries.push([]);
      return join(tries.find(gs=>size(join(gs))<=quotaRoom)||[]);
    })();
    lastRow(keepQuota?quotaRow:[],keepQuota&&quota.length?['quota']:[],order.length,kept.QUESTIONS?0:1);
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
  // Under `rule` the running frame's line stands lit and still (BORDER_LIT of the face's ramp).
  const frameText=v=>ruleMoving&&/^[─│┌┐└┘]+$/.test(v)?{...text(v,'g_spin'),role:'orbit',tone:'arc',level:BORDER_LIT}:
    {...text(v,color),role:color==='dim'?'frame':'frame-state'};
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
  const foot=clip(settings?line(' Esc · '+t('cancel','Cancel')+' '):edge.length?[text(' '),...edge,text(' ')]:[],width-4);
  // Spec rule 47: the frame's line cells, each at its place round the frame, clockwise from the
  // top-left corner. The head's words on the top border and the foot's on the bottom one are no
  // line: the scanners pass behind them (they keep their own colors).
  const W=width,H=count,around=2*W+2*(H-2),arc=Math.max(1,Math.round(around*BORDER_ARC)),start=label.used+1;
  const waveShades=waveSteps(around,borderColors>256?WAVE_STEPS:FACES.dark.orbit.length-1);
  const place=(y,x)=>y===0?x:x===W-1&&y<H-1?W-1+y:y===H-1?2*W+H-3-x:2*W+2*H-4-y;
  // A moving frame cell at a frame: on the arc, its shade (0 the head), past its tail the neutral frame
  // gray; on the wave, its level; waiting, the breath's red.
  function borderCell(at,ch,tick){
    const local=borderTick(tick);
    if(moving==='border'){
      const k=orbitShade(mod(orbitHead(local,around,start)-at,around),arc);
      return k<0?{...text(ch,'dim'),role:'frame'}:{...text(ch,'g_spin'),role:'orbit',tone:'arc',level:k};
    }
    if(moving==='scanners')return {...text(ch,'g_spin'),role:'orbit',tone:'wave',level:waveLevel(at,local,around,waveShades)};
    return {...text(ch,'g_blocked'),role:'orbit',tone:'breath',step:breathStep(local,breathSteps),steps:breathSteps};
  }
  const border=(y,x,ch)=>moving?borderCell(place(y,x),ch,frame):frameText(ch);
  const borderRun=(y,from,to)=>Array.from({length:Math.max(0,to-from+1)},(_,i)=>borderCell(place(y,from+i),'─',frame));
  // Neighbouring cells of one look stand as one part.
  const joined=parts=>parts.reduce((out,part)=>{const last=out.at(-1),{text:a,...x}=part;
    if(last){const {text:b,...y}=last;if(JSON.stringify(x)===JSON.stringify(y)){out[out.length-1]={...last,text:b+a};return out;}}
    out.push(part);return out;},[]);
  rows.push(moving?joined([border(0,0,'┌'),...label.parts,...borderRun(0,label.used+1,W-2),border(0,W-1,'┐')]):
    [frameText('┌'),...label.parts,frameText('─'.repeat(Math.max(0,width-2-label.used))),frameText('┐')]);
  const frameRow=(parts,y)=>{const fitted=clip(parts,inner);return [border(y,0,'│'),text(' '),...fitted.parts,text(' '.repeat(Math.max(0,inner-fitted.used)+1)),border(y,W-1,'│')];};
  for(let i=0;i<count-2;i++)rows.push(frameRow(content[i]||[],i+1));
  rows.push(moving?joined([border(H-1,0,'└'),border(H-1,1,'─'),...foot.parts,...borderRun(H-1,2+foot.used,W-2),border(H-1,W-1,'┘')]):
    [frameText('└─'),...foot.parts,frameText('─'.repeat(Math.max(0,width-3-foot.used))+'┘')]);
  const painted=appearance(rows,cfg,{colors:borderColors,noColor:model.no_color===true,background});
  // The rows whose spinner cells turn between paints (the host swaps only those cells).
  const spins=rows.map((row,i)=>row.some(part=>part.spin)?i:-1).filter(i=>i>=0&&i<count);
  // Only a blinking part needs the one-second repaint (a spinner and the frame turn on their own).
  const blinks=rows.slice(0,count).some(row=>row.some(part=>part.beat));
  // Spec rule 47 (d): the moving frame's line cells (row, column and glyph, in their order round
  // the frame), those cells painted at any spinner frame, and the next frame they change at (the
  // scanners: the next frame they move on, every frame on a frame of 60 cells or more; a breath:
  // its next step). The host writes their changed cells only, on the beat it already keeps.
  const cells=[];
  if(moving){
    for(let x=0;x<W;x++)if(x===0||x>label.used&&x<W-1||x===W-1)cells.push({row:0,col:x,ch:x===0?'┌':x===W-1?'┐':'─'});
    for(let y=1;y<H-1;y++)cells.push({row:y,col:W-1,ch:'│'});
    for(let x=W-1;x>=0;x--)if(x<=1||x>1+foot.used&&x<W-1||x===W-1)cells.push({row:H-1,col:x,ch:x===0?'└':x===W-1?'┘':'─'});
    for(let y=H-2;y>0;y--)cells.push({row:y,col:0,ch:'│'});
  }else if(ruleMoving){
    // `rule`: the head rule's cells (where the rule stands; a panel short of rows that drops it moves nothing).
    const y=rows.findIndex((r,i)=>i>0&&i<count-1&&r.some(part=>part.rule));
    if(y>0)for(let x=0;x<inner;x++)cells.push({row:y,col:2+x,ch:'─',rule:x});
  }
  const motion=cells.length?{cells,
    at:tick=>appearance([cells.map(c=>c.rule!==undefined?ruleLook(c.rule,tick):borderCell(place(c.row,c.col),c.ch,tick))],cfg,{colors:borderColors,noColor:model.no_color===true,background})[0],
    next:tick=>{const local=borderTick(tick),shift=Math.trunc(tick)-local;
      if(ruleMoving){const r=ruleTick(tick);return Math.trunc(tick)+scanNext(r,inner)-r;}
      if(moving==='scanners')return Math.trunc(tick)+1;
      if(moving==='border'){const k=Math.floor(local*around/BORDER_LAP);let n=Math.ceil((k+1)*BORDER_LAP/around);if(n<=local)n=local+1;return n+shift;}
      const now=breathStep(local,breathSteps);for(let t=local+1;t<=local+BORDER_BREATHE;t++)if(breathStep(t,breathSteps)!==now)return t+shift;return local+BORDER_BREATHE+shift;}}:null;
  const animated=!!motion||rows.slice(0,count).some(row=>row.some(part=>part.spin||part.beat));
  // The statusline groups this paint actually shows (cut rows count; dropped ones do not).
  const groups=[...new Set(contentGroups.slice(0,Math.max(0,count-2)).flat())];
  return {rows:painted,hits:hits.filter(h=>h.row<count),color,groups,animated:animated&&!settings,blinks:blinks&&!settings,spins,motion};
}
// 11-15 (small fixes): key words packed into rows of `inner` cells, broken only between their pieces
// (` · ` stands between them); a piece wider than a row breaks at its spaces, a word wider than a
// row ends in `…`. Short of `rows`, the pieces before the last go first, from the end backward, so
// the closing key (the last piece) stays. Every popup's key line is this one rule.
function keyRows(pieces,inner,{rows=Infinity,cellWidth}={}){
  const measure=cellWidth||(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  const room=Math.max(1,inner);
  const word=w=>{if(measure(w)<=room)return w;let t='',u=0;for(const c of w){const n=measure(c);if(u+n>room-1)break;t+=c;u+=n;}return t+'…';};
  const wrap=piece=>{const out=[];let row='';for(const w of String(piece).split(' ').filter(Boolean).map(word)){const next=row?row+' '+w:w;if(row&&measure(next)>room){out.push(row);row=w;}else row=next;}if(row)out.push(row);return out;};
  const pack=list=>{
    const out=[];let row='';
    for(const piece of list){
      if(measure(piece)>room){if(row)out.push(row);const lines=wrap(piece);out.push(...lines.slice(0,-1));row=lines.at(-1)||'';continue;}
      const next=row?row+' · '+piece:piece;
      if(row&&measure(next)>room){out.push(row);row=piece;}else row=next;
    }
    if(row)out.push(row);
    return out;
  }
  const list=pieces.map(x=>String(x).trim()).filter(Boolean);
  let out=pack(list);
  while(out.length>rows&&list.length>1){list.splice(list.length-2,1);out=pack(list);}
  return rows>0?out.slice(-rows):[];
}
// 11-15 (popup design, 공통): one frame for every popup (the questions now; the routes, LAB and
// sessions use it as it is). It stands in the middle of the window, the native area and the panel
// together; with the folded strip under the native area, in the middle of the rows above it (the
// strip is never covered). A thin grey rounded line, one empty cell away from what it covers (a
// native that paints its own background, or a wide character on the edge, ends there). The top
// line carries the title and `✕`, the bottom line the sections (the current one bright), and the
// row above it the keys (the content's last row). Width: text wraps at 76 cells; narrower than 80
// cells the window less 4; narrower than 28 the whole window. Height: the content's, at most the
// rows above the strip less 2 (the content then pages inside). Display only: the keys go to the
// popup's owner (the host) while it stands; nothing here reads input.
const POPUP_TEXT=76,POPUP_TABLE=116;
function popupWidth(cols,kind='text'){
  if(cols<28)return Math.max(4,cols);
  if(cols<80)return cols-4;
  return kind==='table'?Math.min(POPUP_TABLE,cols-4):POPUP_TEXT;
}
// The rows the content may take inside the frame. A low window gives up the empty rows round the
// frame first (under 8 rows), then its bottom line (3 rows or fewer), so the content keeps rows.
function popupHeight(space){return space>=8?space-2:space;}
function popupRoom(rows,band=0){const H=popupHeight(Math.max(1,rows-band));return Math.max(0,H>3?H-2:H-1);}
function popup({cols,rows,band=0,kind='text',title=[],body=[],sections=[],current=0,model={},configuration,cellWidth,background=''}){
  const measure=cellWidth||(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  const styles=model.styles||{},text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const size=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
  function clip(parts,limit){let out=[],used=0;for(const part of parts){let t='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;t+=c;used+=w;}if(t)out.push({...part,text:t});if(used>=limit)break;}return {parts:out,used};}
  const cut=(parts,limit)=>size(parts)<=limit?parts:limit<1?[]:[...clip(parts,limit-1).parts,{...text('…','dim')}];
  const space=Math.max(1,rows-band),W=Math.min(cols,popupWidth(cols,kind)),inner=Math.max(0,W-4);
  const maxH=popupHeight(space),shut=maxH>3,list=body.slice(0,Math.max(0,shut?maxH-2:maxH-1)),H=Math.min(maxH,list.length+(shut?2:1));
  const x=Math.floor((cols-W)/2),y=Math.max(0,Math.round((space-H)/2));
  // 11-15 (panel polish 3): where nothing behind it is veiled (mono, eight colors, NO_COLOR) the
  // frame's line is bright instead of the messages' gray.
  const bright=veil(configuration,{colors:model.colors??256,noColor:model.no_color===true,background})===null;
  const line=v=>bright?{text:v}:{...text(v,'dim'),role:'messages'};
  // The top line: `╭─ title ──… ✕ ─╮`; a title too long ends in `…`, the `✕` stays.
  const room=Math.max(0,W-10),name=room>=4?cut(title.map(p=>({...p,text:clean(p.text)})),room):[];
  const head=name.length?[line('╭─ '),...name,line(' ')]:[line('╭─')];
  const close=W>=8?[text(' ','dim'),text('✕','dim'),line(' ─╮')]:[line('╮')];
  const top=[...head,line('─'.repeat(Math.max(0,W-size(head)-size(close)))),...close];
  // The bottom line: the sections, the current one bright.
  const names=sections.flatMap((s,i)=>[...(i?[text(' · ','dim')]:[]),{...text(s,i===current?'loc_repo':'dim'),role:i===current?'':'title',bold:i===current}]);
  const listed=names.length&&size(names)+6<=W?[line('╰─ '),...names,line(' ')]:[line('╰─')];
  const bottom=[...listed,line('─'.repeat(Math.max(0,W-size(listed)-1))),line('╯')];
  const frame=[top];
  for(const row of list){const fit=clip(Array.isArray(row)?row:[],inner);frame.push([line('│ '),...fit.parts,text(' '.repeat(Math.max(0,inner-fit.used))),line(' │')]);}
  if(shut)frame.push(bottom);
  // One empty cell round the frame where the window has it.
  const left=x>0?1:0,right=x+W<cols?1:0,above=y>0?1:0,below=y+H<space?1:0;
  const blank=n=>({text:' '.repeat(n)});
  const out=[];
  if(above)out.push([blank(W+left+right)]);
  for(const row of frame)out.push([...(left?[blank(1)]:[]),...row,...(right?[blank(1)]:[])]);
  if(below)out.push([blank(W+left+right)]);
  const cfg={...defaults,...configuration,shade:'none'};
  const painted=appearance(out,cfg,{colors:model.colors??256,noColor:model.no_color===true,background});
  // Presses, in window cells: `✕` closes, a section's name opens its popup.
  const hits=[];
  if(close.length>1)hits.push({row:y,left:x+W-4,right:x+W-3,action:'popup-close'});
  let at=x+3;for(let i=0;i<sections.length&&shut&&listed.length>2;i++){const n=measure(sections[i]);hits.push({row:y+H-1,left:at,right:at+n-1,action:'popup:'+sections[i]});at+=n+3;}
  return {rows:painted,left:x-left,top:y-above,width:W+left+right,height:out.length,frame:{left:x,top:y,width:W,height:H},inner,body:list.length,hits};
}
// 11-15 (panel polish 3): while a popup stands, the one gray everything behind it reads in (the
// native's rows, the panel, the strip; no other color, no background): its face's `veil` (an
// unknown face the dark one's). Null where the panel shows no hue (mono, eight colors, NO_COLOR):
// nothing is veiled there and the popup's frame is bright instead.
function veil(configuration={},{colors=256,noColor=false,background=''}={}){
  const theme={...defaults,...configuration}.theme;
  return noColor||!(colors>=256)||theme==='mono'?null:FACES[face(theme,background)||'dark'].veil;
}
// 11-15 (popup design E): the MESSAGES popup's rows: the requests this session sent or received
// (their numbers, reports and body) and the recent messages, the newest first, each its head (the
// direction, the other session, then dim harness, time, kind and a sent one's delivery state) and
// its whole text under it, two cells in. What the panel's rows cut reads whole here.
function messagesBody({model={},columns=72,cellWidth}={}){
  const measure=cellWidth||(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  const styles=model.styles||{},text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const wrapText=(value,key,width,lead='  ')=>{
    const out=[];const room=Math.max(4,width-measure(lead));
    for(const paragraph of clean(value).split(/\s*\n\s*/)){
      let row='',used=0;
      for(const word of paragraph.split(/(\s+)/).filter(Boolean)){
        const w=measure(word);
        if(/^\s+$/.test(word)){if(used&&used<room){row+=' ';used++;}continue;}
        if(used&&used+w>room){out.push([text(lead),text(row.trimEnd(),key)]);row='';used=0;}
        if(w>room){for(const g of graphemes(word)){const gw=measure(g);if(used+gw>room){out.push([text(lead),text(row,key)]);row='';used=0;}row+=g;used+=gw;}continue;}
        row+=word;used+=w;
      }
      if(row.trim())out.push([text(lead),text(row.trimEnd(),key)]);
    }
    return out;
  };
  const out=[];
  for(const q of (Array.isArray(model.requests)?model.requests:[]).filter(q=>q&&typeof q.id==='string')){
    if(out.length)out.push([]);
    out.push([text(String(q.glyph||q.way||'→'),q.glyph==='✕'?'lvl_r':q.glyph==='✓'?'lvl_g':'loc_repo'),text(' '),text(String(q.who||''),'dim'),text(' '),
      text(String(q.title||q.id),'loc_repo'),...(q.status?[text(' · '+q.status,'dim')]:[]),...(q.age?[text(' '+q.age,'dim')]:[])]);
    for(const [label,value,tone] of Array.isArray(q.details)?q.details:[])out.push(...wrapText((label?label+': ':'')+String(value||''),({lvl_r:'lvl_r',lvl_g:'lvl_g',dim:'dim'})[tone]||'loc_repo',columns));
    for(const value of Array.isArray(q.body)?q.body:[])out.push(...wrapText(value,'loc_repo',columns));
    if(q.body_more)out.push([text('  …')]);
  }
  for(const m of (Array.isArray(model.messages)?model.messages:[]).slice().reverse()){
    if(!m||typeof m!=='object')continue;
    if(out.length)out.push([]);
    const delivery=Array.isArray(m.delivery)&&m.delivery[0]?[text(' · ','dim'),text(String(m.delivery[0]),m.delivery[1]||'dim')]:[];
    out.push([text(m.way,m.way==='←'?'lvl_g':'loc_repo'),text(' '),text(String(m.who||''),'loc_repo'),...(m.harness?[text(' '+m.harness,'dim')]:[]),
      ...(m.when?[text(' · '+m.when,'dim')]:[]),...(m.kind?[text(' · '+m.kind,'dim')]:[]),...delivery]);
    out.push(...wrapText(m.text||m.body||'','loc_repo',columns));
  }
  return out.length?out:[[text('·')]];
}
module.exports={render,keyRows,defaults,fields,change,appearance,options:OPTIONS,SPIN,FACES,breathStep,popup,popupWidth,popupRoom,messagesBody,veil,
  RULE:{PEAK:RULE_PEAK,FADE:RULE_FADE,SCAN:RULE_SCAN,REST:RULE_REST},WAVE:{COUNT:WAVE_COUNT,LAP:WAVE_LAP,STEPS:WAVE_STEPS,FLOOR:WAVE_FLOOR,MIN:WAVE_MIN},
  BORDER:{LIT:BORDER_LIT,ARC:BORDER_ARC,LAP:BORDER_LAP,SHADES:BORDER_SHADES,BREATHE:BORDER_BREATHE,BREATHE_STEPS:BORDER_BREATHE_STEPS,BREATHE_STEPS_256:BORDER_BREATHE_STEPS_256}};
