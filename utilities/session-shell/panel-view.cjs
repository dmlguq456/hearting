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
// 11-16 (the person: "스캐너가 가속도가 붙는 방식이 아니네? 등속으로 오가는거 좀 거슬리네"): the light slows toward the
// ends it turns at and runs fastest in the middle, like a pendulum. RULE_EASE is the one value to tune: 1 puts
// its place on a cosine (stopping at each end, the fastest in the middle), 0 is the old steady speed, between
// them a mix of the two.
const RULE_EASE=1;
// `scanners`: a wide wave flows round the frame clockwise, no edge anywhere: WAVE_COUNT waves share
// the frame (fewer where one would be narrower than WAVE_MIN cells), one place rises and falls once
// every WAVE_LAP frames (56 = 5.6 s), in WAVE_STEPS shades in 24-bit color (the face's four in 256
// colors; fewer on a narrow wave, so two neighbouring cells never differ by more than one), its
// darkest WAVE_FLOOR of the way from the still frame's gray to the brightest (never out).
const WAVE_COUNT=3,WAVE_LAP=56,WAVE_STEPS=10,WAVE_FLOOR=0.3,WAVE_MIN=24;
// `border` (#1639): one bright arc, BORDER_ARC of the frame long, round once every BORDER_LAP frames.
const BORDER_ARC=1/4,BORDER_LAP=60;
// 11-16 (panel polish 1116, the person on v4.40.0: "점멸은 안하는데"): what waits for the person's answer blinks
// on Fleet's own beat (render_cells `_blink_phase`): WAIT_BLINK spinner frames (10 = 1 s, the clock's even
// second) in the lit red of `FACES.*.waitBlink`, as many in its other red.
const WAIT_BLINK=10;
// 11-16 (the person: "최대 5개 정도로 늘리고, 그전에 원래 형식으로 돌려"): MESSAGES shows the newest MESSAGES_MAX
// messages at most, each its head row and its first line (spec rule 31).
const MESSAGES_MAX=5;
// 11-16 (the person: "메모리도 메시지랑 같은 맥락이야"): MEM shows the newest MEM_MAX records at most, each its
// dim head row and its headline on up to two rows (spec rule 46 (b)). 11-18 (the person: "MEM는 최대 최근 3개만 띄워도 되겠다"): three at most.
const MEM_MAX=3;
const defaults={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14,
  position:'right',theme:'terminal',shade:'none',route_line:'time',worker_box:'closed',rule_motion:'rule'};
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
    // rule's light, dimmest first (11-16, the person: "너무 과하게 은은해서 보이지도 않음": no longer the lit
    // frame's own vanilla, whose khaki steps sank beside it, but the gray ramp from the still rule's gray up
    // to a head a step brighter than the body text, in even steps); a waiting frame's reds, brightest first
    // (the frame's waiting red), in 256 colors, and its two ends in 24-bit color.
    orbit:[230,187,144,101],waveRGB:[[128,128,128],[255,255,215]],scan:[243,246,249,252,255],
    breath:[217,210,174],breathRGB:[[255,175,175],[215,135,135]],
    // 11-15 (panel polish 3): while a popup stands, everything outside it reads in `veil` alone.
    veil:239,
    // 11-15 (the person: "팝업이 여전히 구분이 잘 안되는데? 배경 컬러도 다르고 팝업은 폰트가 좀 더 밝아야"):
    // a popup's frame stands on `popup` (a step or two off the terminal's own background; one cell past
    // its side lines too), its line in `popupLine`, its words in `bright`, and a word drawn dim or gray
    // never reads below `popupFloor`. ("질문 선지 선택 효과를 틴트로 해 명확하게"): the choice the
    // question popup has chosen stands on `chosen` across the popup's inside, one ground and no other.
    // 11-17 (the person: "팝업 창 배경색이 못생겼는데 ... 하팅의 테마 컬러 활용해보자"): the popup's own look in
    // Hearting's earth tones (the public page's `--solid`, `--border-2`, `--text`), the whole of it in these
    // few values: `popup` the ground, `popupLine` the frame's line, `popupTitle` the title, `chosen` the chosen
    // row's ground, each a 256-color step, and `rgb` the same four in 24-bit color where the terminal says it
    // takes it (no 256-color step is a dark brown: the ground there falls to its nearest gray, the line and
    // the title keep their warmth). Another design is a change of these eight values (`POPUP-GROUND-MOCK-1117`).
    // The chosen row stays 20 or more apart from the popup's ground (CIE76; the blue it replaced was 34.9 / 24.9), a clay in 24-bit
    // color; on the dark face's 256 colors 52 (the one warm step that is both apart enough and reads the words on it).
    bright:255,popup:235,popupLine:137,popupTitle:255,popupFloor:247,chosen:52,
    rgb:{popup:'#29221e',popupLine:'#786453',popupTitle:'#f1eae3',chosen:'#5a3a1e'},
    // 11-15 (the person: "대기 중인건 점멸을 시키자" / "질문만"): what waits for the person's answer (the
    // question's red letters in QUESTIONS, `◑답 대기 N` on the folded strip) goes between these two reds,
    // the lit one first, on Fleet's one-second beat (WAIT_BLINK). 11-16 ("점멸은 안하는데"): 174 and 131 were
    // an L* gap of 15 apart; these are 34 apart (L* 79 and 44.5), the darker still readable.
    waitBlink:[217,95],
    // 11-16 (the person: "usage에서 하네스 이름도 어둡게 원래 테마 컬러를 넣어주고"): the harness names of the USAGE rows, each in
    // its Fleet hue (`hb_*`: claude orange 173, codex lavender 147, opencode sky 153) one step down (L* 48.6, 59.3, 70.0; the lit hues
    // are L* 63.8, 74.2, 84.4), so a name reads as a label and never louder than its numbers.
    // 11-18 (the harness colors follow the products' own themes): Claude's name was 66, now 130; Codex's was 133, now 104
    // (the step under 147); OpenCode's was 104, now 110 (the step under 153). Each pair is 13 or more apart (CIEDE2000).
    harnessName:{claude:130,codex:104,opencode:110},
    // 11-18 (the person: "패널 스캐너 동작도"): while a session works, the moving frame and the head rule's light take the
    // session's harness color instead of the vanilla ramp: the same steps (the arc's four, the head rule's five, the wave's two
    // ends), each a step of that harness's hue (`orbit` brightest first: a lit tint of the harness color, the color, two steps
    // toward the still frame's gray; `scan` the head rule's, dimmest first, from the still frame's gray to the lit tint, each step 8 or more L* above the one before and the last as bright as the body text; `waveRGB` the same two
    // ends in 24-bit color: the still frame's gray and the lit tint's own RGB). A waiting frame's reds, the unknown harness's
    // vanilla and mono are as before.
    ramp:{claude:{orbit:[216,173,137,102],scan:[243,137,209,216,223],waveRGB:[[128,128,128],[255,175,135]]},
      codex:{orbit:[189,147,104,245],scan:[243,104,177,183,189],waveRGB:[[128,128,128],[215,215,255]]},
      opencode:{orbit:[195,153,110,246],scan:[243,103,74,117,195],waveRGB:[[128,128,128],[215,255,255]]}}},
  light:{light:255,medium:254,body:237,secondary:241,title:241,frame:243,route:243,messages:245,accent:131,
    working:22,idle:58,blocked:131,dead:88,done:22,steward:[132,175],
    orbit:[22,65,108,151],waveRGB:[[118,118,118],[0,95,0]],scan:[244,242,240,238,236],
    breath:[88,124,131],breathRGB:[[135,0,0],[175,95,95]],
    veil:250,bright:232,popup:254,popupLine:137,popupTitle:232,popupFloor:241,chosen:223,waitBlink:[88,167],
    rgb:{popup:'#f1e7dc',popupLine:'#9c8571',popupTitle:'#342a23',chosen:'#fecca4'},
    // (The light face: the deep hues 166, 25, 31 (L* 54.7, 40, 52.3). Codex's name is the step lighter, 68 (L* 56.7); Claude's deep orange
    // and OpenCode's deep sky are already the lightest that read on white (166, 3.3; 31, 4.1), so their names are the head's own hue.)
    harnessName:{claude:166,codex:68,opencode:31},
    // 11-18 (PEERS: this window's number is its deep hue bold, every other window's one step back, "나머지는 컬러도 조금 어둡게"):
    // on white the step back is the lighter one. Claude 173 (2.8 on white, 12.4 from 166 by CIEDE2000), Codex 68 (its name's own,
    // 16.8 from 25), OpenCode 66 (4.0 on white, 14.3 from 31, 23.5 from Codex's 68). Only PEERS reads it; the USAGE names keep `harnessName`.
    peerOther:{claude:173,codex:68,opencode:66},
    // (The light face's ramps, the deep pairs 166, 25, 31 first, toward the light ground: the harness's own steps; `scan` dimmest first,
    // from the still frame's gray up to the deep color.)
    ramp:{claude:{orbit:[166,173,216,223],scan:[243,102,137,173,166],waveRGB:[[118,118,118],[215,95,0]]},
      codex:{orbit:[25,68,146,189],scan:[243,60,61,26,25],waveRGB:[[118,118,118],[0,95,175]]},
      opencode:{orbit:[31,74,110,152],scan:[243,102,67,32,31],waveRGB:[[118,118,118],[0,135,175]]}}},
};
// Fleet's 256-color hues on each face. The light face takes the reviewed deep
// counterparts (running 22, attention 58, failure 88, project 23, model 60)
// instead of the earlier strong substitutes; the dark face changes only the
// failure red, and only on a shade.
// 11-18 (OpenCode's sky 153 -> 31, #0087af, 4.1 on white; Codex's 147 shares Sonnet's, so its deep pair is 25: the person chose the same color.)
// 11-18 (Claude's harness orange 173 -> 166, #d75f00, 3.3 on white: the deep orange nearest its hue (56°; 130 is 64° and is the
// direct zone's and lab's own deep hue here), 9.4 from that 130 by CIEDE2000: see PR text).
// 11-18: the three zone hues (orange, violet, aqua) and the five field hues (code blue, lab amber, design rose, docs sand,
// research sky) take their dark counterparts on the light face: the same hue at a contrast of 4.5 or more on white
// (130 4.7, 55 10, 29 4.5; 26 5.8, 130 4.7, 162 5.0, 94 5.7, 24 7.0), set apart from each other and the state hues
// (Lab distance 20 or more; lab 130 and docs 94 are the closest pair, as amber and sand are in the dark set).
const HUES={dark:{217:210},light:{253:237,252:238,150:22,186:58,217:88,116:23,173:166,176:60,147:25,153:31,230:22,219:132,137:94,
  208:130,99:55,43:29,69:26,214:130,205:162,180:94,38:24}};
const LINES=new Set(['frame','route','messages']);
function face(theme,background){return theme==='light'?'light':background==='light'||background==='dark'?background:'';}
// A moving frame cell's color: an arc or lit-frame shade from its face's ramp (0 the brightest); a wave
// at `level` (0..1) between its face's two ends in 24-bit color, else on its ramp (the dimmest first);
// the head rule's light step (1 the dimmest .. 5); a waiting step from its face's 256-color reds, or in
// 24-bit color (when the terminal says it takes it) a point on the cosine between its face's two ends.
const mix=(a,b,f)=>'#'+a.map((c,i)=>Math.round(c+(b[i]-c)*f).toString(16).padStart(2,'0')).join('');
function orbitTone(part,F,colors,harness=''){
  // A working session's tones are its harness's (`F.ramp`); a waiting one's reds (`breath`) and an unknown harness's are the face's own.
  const R=part.tone!=='breath'&&F.ramp&&F.ramp[harness]||F;
  if(part.tone==='arc')return R.orbit[Math.max(0,Math.min(R.orbit.length-1,part.level))];
  if(part.tone==='scan')return R.scan[Math.max(0,Math.min(R.scan.length-1,part.level-1))];
  if(part.tone==='wave'){const [lo,hi]=R.waveRGB,q=Math.max(0,Math.min(1,part.level));
    return colors>256?mix(lo,hi,WAVE_FLOOR+(1-WAVE_FLOOR)*q):R.orbit[Math.round((1-q)*(R.orbit.length-1))];}
  const steps=part.steps,k=part.step;
  if(colors>256&&steps>1){
    const f=(Math.cos(Math.PI*k/(steps-1))+1)/2,[hi,lo]=F.breathRGB;
    return mix(lo,hi,f);
  }
  return F.breath[Math.max(0,Math.min(F.breath.length-1,k))];
}
function appearance(rows,configuration={},{colors=256,noColor=false,background='',harness=''}={}){
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
    if(role==='accent'||role==='accent-line'){if(theme!=='mono')foreground=Number.isInteger(part.blinkTone)?F.waitBlink[part.blinkTone]??F.accent:F.accent;else if(role==='accent')bold=true;dim=false;}
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
    else if(role==='harness-name'&&theme!=='mono'){foreground=((part.peer&&F.peerOther)||F.harnessName)[part.harness]??foreground;dim=false;}
    else if(role==='orbit'&&theme!=='mono'&&part.tone){foreground=orbitTone(part,FACES[known||'dark'],colors,harness);dim=false;}
    // Spec rule 22: on a known face each line takes its role's color with or
    // without a shade (frame 244/243, route 245/243, messages 242/245); an
    // unknown face keeps the terminal's own, mono keeps no hue.
    else if(LINES.has(role)&&known&&theme!=='mono'){foreground=F[role];dim=false;}
    if(shade){
      // On a shade nothing is dimmed on top: secondary text and lines take their own color.
      if(LINES.has(role)){foreground=F[role];dim=false;}
      else if(role==='title'){foreground=F.title;dim=false;}
      // A kind word's bracket keeps its field's hue and its weight step (quick dim) on a shade too.
      else if(role==='kind'&&foreground!==null){/* kept as it is */}
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
// Spec rule 49 (d): the narrow-width judgment is one constant (native-display's NATIVE_MIN), so
// the editor has no `threshold`; `side_min_columns` stays in `defaults` only so a saved policy
// keeps its keys.
const fields=['layout','position','side_width','narrow','auxiliary','bottom_rows','theme','shade','route_line','worker_box','rule_motion'];
const names=['layout','position','width','narrow','auxiliary','height','theme','shade','route_line','worker_box','rule_motion'];
const OPTIONS={layout:['side','bottom'],position:['right','left'],narrow:['bottom','compact'],auxiliary:['summary','detail'],
  theme:['terminal','light','mono'],shade:['none','light','medium'],route_line:['time','model'],worker_box:['closed','open','none'],rule_motion:['rule','border','scanners','off']};
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
// 11-16: the waiting blink on Fleet's beat, from the clock (the spinner frame), not from when a state began:
// lit (`FACES.*.waitBlink[0]`) through the even second, the other red through the odd one; and the next
// frame it changes at.
function blinkTone(tick){return mod(Math.floor(Math.trunc(tick)/WAIT_BLINK),2)===0?0:1;}
function blinkNext(tick){return (Math.floor(Math.trunc(tick)/WAIT_BLINK)+1)*WAIT_BLINK;}
// The cells of the parts marked `blink` in rows of parts (row, column, glyph, the part): one per glyph,
// spaces and a glyph a zero-width mark joins left out (the host swaps one cell at a time).
function blinkCells(rows,measure,clean,first=0,last=rows.length){
  const cells=[];
  for(let y=first;y<last;y++){let x=0;
    for(const part of rows[y]||[]){
      for(const c of clean(part.text)){const w=measure(c);
        if(!w){if(cells.at(-1)?.row===y&&cells.at(-1).col+cells.at(-1).w===x)cells.pop();continue;}
        if(part.blink&&c.trim())cells.push({row:y,col:x,ch:c,w,blink:part});
        x+=w;}
    }
  }
  return cells.map(({w,...c})=>c);
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
  const steady=q<RULE_SCAN/2?q/(RULE_SCAN/2):2-q/(RULE_SCAN/2),swing=(1-Math.cos(2*Math.PI*q/RULE_SCAN))/2;
  const y=steady+RULE_EASE*(swing-steady);
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
function render({frame=0,since,width,height,side,fill=side,model={},native={},state,sid,brief,sent,sections,configuration,settings,routeScroll=0,focused=false,cwd='',harness='',cellWidth,background='',folded=false}){
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
  let inner=width-4,routeArea=null;const content=[],hits=[],edge=[];
  // 11-15: what waits for the person's answer blinks between two reds on Fleet's one-second beat (11-16; spec
  // rule 47's tick; not with `rule motion` off, mono, NO_COLOR, fewer than 256 colors or the settings).
  const waitBlinks=!settings&&cfg.rule_motion!=='off'&&model.no_color!==true&&cfg.theme!=='mono'&&(model.colors??256)>=256;
  const blinking=part=>waitBlinks&&part.role==='accent'?{...part,blink:true}:part;
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
  // Spec rule 49 (c), as the person changed it (11-15: "최소로 접었을때 패널이 좀 못생겼어 ... 전체를 한줄로만
  // 만들자"): folded (by itself or by hand, beside or under the native area) the panel is one row under
  // the native area, whatever the window's height and whatever it shows: the session (the state glyph
  // and number, the project, the model and effort, `CTX` with its gauge, 5h, 7d) │ the answer wait
  // (`◑답 대기 N` in its red, only while one waits; compaction imminent and a tidy notice stand in the
  // same place, after it) │ the routes (`4 ROUTE ⠏2 ✕1`, no titles) │ `MESSAGES` with the sessions the
  // recent messages came from or went to (`← [e7] → [73]`: each way and session once, the newest first,
  // `+N` for those that do not fit) … `▴` at the right end. A group with nothing in it does not stand.
  // Short of cells, in this order: the reset times, the branch, the effort, the model windows (Fable's),
  // 7d, 5h, OpenCode's MCP count, the model, the messages' numbers (their count stays), MESSAGES, the
  // project, the windows at Fleet's red level (they outlast the other windows; the last first), the notices, the
  // CTX bar (its percent stays), the routes' counts by state, and narrower than 44 cells `CTX` itself; the glyph and number, the wait, `N ROUTE` and `▴` stay. Nothing wraps. A press on the wait opens the question
  // popup, on the routes the routes popup, on MESSAGES its popup; anywhere else, and `▴`, opens the
  // panel. No frame, gear, beat or moving rule: only the state glyph turns.
  function foldedView(){
    const own=LIVE[state];
    const plain=model.no_color===true||cfg.theme==='mono';
    // 11-15: the answer wait blinks on Fleet's one-second beat (not with `rule motion` off, mono, NO_COLOR
    // or fewer than 256 colors, as the frame's breath).
    const blinking=cfg.rule_motion!=='off'&&!plain&&(model.colors??256)>=256;
    const P=v=>Array.isArray(v)?v.filter(p=>p&&typeof p==='object'):[];
    const size=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
    const cut=(parts,limit)=>size(parts)<=limit?parts:limit<1?[]:[...clip(parts,limit-1).parts,text('…','dim')];
    const bar=[text(' │ ','dim')];
    const joinAll=groups=>groups.filter(g=>g.length).flatMap((g,i)=>i?[...bar,...g]:g);
    // The session.
    const tag=/^\[[^\]?]{1,16}\]$/.test(String(model.tag||''))?clean(String(model.tag).slice(1,-1)):'';
    const glyph=own?[{...text(own[0]==='spin'?spinGlyph.text:own[0],own[0]==='spin'&&['claude','codex','opencode'].includes(harness)?'hb_'+harness:own[1]),...(own[0]==='spin'?{spin:true}:{}),role:'session'}]:[];
    const number=tag?[text('[','dim'),model.steward===true?{...text(tag,'tag_steward'),role:'steward',...(plain?{bold:false,dim:false}:{})}:{...text(tag,own?own[1]:'dim'),role:'session'},text(']','dim')]:[];
    const lead=[...glyph,...(glyph.length&&number.length?[text(' ')]:[]),...number];
    const project=Array.isArray(model.repo_side)?P(model.repo_side):P(model.repo).filter(p=>!/^\[[^\]]*\] ?$/.test(String(p.text)));
    // `CTX` stands for the word `context` (a backend without `context_side` has its word taken off here, as the open panel's).
    const unworded=parts=>{const head=parts[0],rest=String(head?.text??'').replace(/^context /,'');return !head||rest===String(head.text)?parts:rest?[{...head,text:rest},...parts.slice(1)]:parts.slice(1);};
    const selected=selectedParts(),gauge=Array.isArray(model.context_side)?P(model.context_side):unworded(P(model.context));
    // The gauge without its bar: the percent alone.
    const percent=gauge.filter(p=>!/^[━─]+$/.test(clean(p.text))).map((p,i)=>i?p:{...p,text:clean(p.text).replace(/^\s+/,'')}).filter(p=>p.text);
    const windows=[];let current=[];
    for(const part of P(model.quota)){if(String(part.text)===' │ '){if(current.length)windows.push(current);current=[];}else current.push(part);}
    if(current.length)windows.push(current);
    const quotaParts=new Set(windows.flat());
    const kind=g=>{const label=g.map(part=>String(part.text)).join('').trim().split(/\s+/)[0];return label==='7d'?'week':/^\d+[mhd]$/.test(label)?'window':'model';};
    // The checkout after the project, as Fleet's session row writes it (` (branch)`, twelve cells at most).
    const branchName=clean(String(P(model.git_side)[0]?.text??'')).trim().split(/\s+/)[0]||'';
    const branch=branchName&&project.length?[text(' ('+(measure(branchName)>12?clip([text(branchName)],11).parts.map(x=>x.text).join('')+'…':branchName)+')','dim')]:[];
    // The answer wait, then compaction imminent and a tidy notice, in the same place.
    const wait=model.wait?[{...text(clean(model.wait),'g_blocked'),role:'accent',...(model.no_color===true?{bold:true}:{}),...(blinking?{blink:true}:{})}]:[];
    const tidy=typeof model.tidy_notice==='string'?clean(model.tidy_notice).trim():'';
    // 11-16 (the person: "알림이 알림이 아니라 경고 같아서"): a notice of something that ended well (`tidy_quiet`)
    // reads dim, without `⚠`; what leaves the person something to do keeps the caution look.
    const notices=[...(model.warning?[[text('⚠ ','lvl_r'),text(clean(model.warning),'lvl_r')]]:[]),
      ...(tidy&&!model.answerable?[model.tidy_quiet===true?[text(tidy,'dim')]:[text('⚠ ','lvl_y'),text(tidy,'lvl_y')]]:[])];
    // The routes: the count first, the name tag dim, then the counts by state.
    const counted=routes.slice(0,Math.max(0,model.route_count??routes.length));
    const total=Number.isInteger(model.route_total)&&model.route_total>0?model.route_total:counted.length;
    const label=clean(model.route_label||'').trim().replace(/^route (\d+)/,'$1 ROUTE')||(total?total+' ROUTE':'');
    const labelMatch=/^(\d+) ROUTE(.*)$/u.exec(label);
    const routeHead=labelMatch?[text(labelMatch[1],'loc_repo'),text(' '),{...text('ROUTE','dim'),role:'title'}]:label?[{...text(label,'dim'),role:'title'}]:[];
    const tally=labelMatch&&labelMatch[2].trim()?[text(' '+labelMatch[2].trim(),'dim')]:[];
    // The messages: the way and the other session of each, once, the newest first.
    const seen=new Set(),pairs=[];
    for(const m of P(model.messages).slice().reverse()){const who=clean(m.who).trim(),way=clean(m.way).trim();
      if(!who||!way||seen.has(way+who))continue;seen.add(way+who);pairs.push([text(way,way==='←'?'lvl_g':'loc_repo'),text(' '+who,'loc_repo')]);}
    const messagesName=[{...text('MESSAGES','dim'),role:'title'}];
    const messageGroup=(numbers,room)=>{
      if(!pairs.length)return [];
      if(!numbers)return [...messagesName,text(' '+pairs.length,'loc_repo')];
      // As many as fit in `room`, the newest first, `+N` for the rest.
      for(let k=pairs.length;k>=1;k--){const more=pairs.length-k,row=[...messagesName,...pairs.slice(0,k).flatMap(p=>[text(' '),...p]),...(more?[text(' +'+more,'dim')]:[])];
        if(size(row)<=room||k===1)return row;}
      return messagesName;
    };
    const room=Math.max(0,width-2);
    // Each step lets one more thing go; the first that fits is drawn. (11-15, review A5: past the last
    // step the cell before `▴` is taken too, and then the session's number gives way, the glyph staying,
    // so the wait and `N ROUTE` stand whole from 30 cells.)
    // 11-16 (review of #1662, the old notice row's `⚠ 5h 92%`): a window at Fleet's red level (the backend marks its
    // parts `level: red`) outlasts the other windows and goes after the project, the last of the red ones first;
    // then the notices (compaction imminent) go.
    const redOf=new Map();let reds=0;
    windows.forEach((g,i)=>{if(g.some(p=>p.level==='red'))redOf.set(i,++reds);});
    const redSteps=Array.from({length:reds},(_,i)=>'red'+(reds-i));
    const steps=['resets','branch','effort','modelWindows','week','windows','mcp','model','numbers','messages','project',...redSteps,'notices','bar','tally','ctx','tight','number'];
    const build=(gone,noticeRoom=Infinity)=>{
      let q=windows.map((g,i)=>({g,red:redOf.get(i)||0}));
      if(gone.has('resets'))q=q.map(e=>({...e,g:e.g.filter(part=>!/^\s*\(↻/.test(String(part.text)))}));
      const keep=e=>e.red>0&&!gone.has('red'+e.red);
      if(gone.has('modelWindows'))q=q.filter(e=>kind(e.g)!=='model'||keep(e));
      if(gone.has('week'))q=q.filter(e=>kind(e.g)!=='week'||keep(e));
      if(gone.has('windows'))q=q.filter(e=>kind(e.g)!=='window'||keep(e));
      const m=gone.has('model')?[]:gone.has('effort')?selected.slice(0,1):selected;
      const c=gauge.length&&!gone.has('ctx')?[text('CTX ','dim'),...(gone.has('bar')?percent:gauge),...(!gone.has('mcp')&&P(model.mcp).length?[text(' │ ','dim'),...P(model.mcp)]:[])]:[];
      const lead2=gone.has('number')?glyph:lead;
      const head=[...lead2,...(!gone.has('project')&&project.length?[...(lead2.length?[text(' ')]:[]),...project,...(gone.has('branch')?[]:branch)]:[])];
      const session=[head,m,c,...q.map(e=>e.g)];
      const n=gone.has('notices')?[]:notices.map(x=>size(x)<=noticeRoom?x:cut(x,Math.max(3,noticeRoom)));
      const route=routeHead.length?[...routeHead,...(gone.has('tally')?[]:tally)]:[];
      const groups=[...session.map(g=>({parts:g})),...(wait.length?[{parts:wait,action:'question'}]:[]),...n.map(x=>({parts:x})),
        ...(route.length?[{parts:route,action:'popup:ROUTE',route:true}]:[])];
      const before=size(joinAll(groups.map(g=>g.parts)));
      const msg=gone.has('messages')?[]:messageGroup(!gone.has('numbers'),room-before-3);
      if(msg.length)groups.push({parts:msg,action:'popup:MESSAGES'});
      return groups.filter(g=>g.parts.length);
    };
    let groups=null;const gone=new Set();
    const limit=()=>gone.has('tight')?Math.max(0,width-1):room;
    for(let i=0;i<=steps.length&&!groups;i++){
      if(i)gone.add(steps[i-1]);
      let g=build(gone);
      if(size(joinAll(g.map(x=>x.parts)))<=limit()){groups=g;break;}
      // A notice's words are cut before the next value goes, while six cells of them stay.
      if(notices.length&&!gone.has('notices')&&redSteps.every(x=>gone.has(x))){const over=size(joinAll(g.map(x=>x.parts)))-room,longest=Math.max(...notices.map(size));
        if(longest-over>=8){g=build(gone,longest-over);if(size(joinAll(g.map(x=>x.parts)))<=room){groups=g;break;}}}
    }
    if(!groups)groups=build(gone);
    const spans=[],parts=[];let x=0;
    groups.forEach((g,i)=>{if(i){parts.push(...bar);x+=3;}const w=size(g.parts);if(g.action)spans.push({left:x,right:x+w-1,action:g.action});parts.push(...g.parts);x+=w;});
    const body=cut(parts,limit());
    const row=[...body,text(' '.repeat(Math.max(0,width-1-size(body)))),text('▴','dim')];
    const out=[row];while(out.length<height)out.push([text(' '.repeat(width))]);
    const hits=[];
    for(const sp of spans)if(sp.left<limit())hits.push({row:0,left:sp.left,right:Math.min(sp.right,limit()-1),action:sp.action});
    hits.push({row:0,left:0,right:width-1,action:'unfold'});
    const shown=new Set(body);
    const painted=appearance(out,{...cfg,shade:'none'},{colors:model.colors??256,noColor:model.no_color===true,background,harness});
    const spins=out.map((r,i)=>r.some(part=>part.spin)?i:-1).filter(i=>i>=0);
    const groupsPainted=['head',...(groups.some(g=>g.parts.some(p=>p.text==='CTX '))?['context']:[]),...(body.some(part=>quotaParts.has(part))?['quota']:[]),
      ...(wait.length&&body.some(p=>wait.includes(p))?['wait']:[]),...(groups.some(g=>g.route)?['route']:[])];
    const color=own&&own[1]!=='g_work_off'?own[1]:'dim';
    // The wait's cells, swapped by the host between the two reds on Fleet's beat (only those cells).
    const cells=blinkCells(out,measure,clean,0,1);
    const look={...cfg,shade:'none'},paint={colors:model.colors??256,noColor:model.no_color===true,background,harness};
    const motion=cells.length?{cells,at:tick=>appearance([cells.map(c=>({...c.blink,text:c.ch,blinkTone:blinkTone(tick)}))],look,paint)[0],
      next:tick=>blinkNext(tick)}:null;
    return {rows:painted,hits,color,groups:[...new Set(groupsPainted)],animated:spins.length>0||!!motion,spins,motion,folded:true};
  }
  // Spec rule 49 (a), (b), (e): the panel's last row inside carries the fold button at its right end and,
  // three cells left of it, the gear `⚙` (both dim). 11-16 (the person: "접는게 하단 접힘인데, 여전히 화살표가
  // 우측이고, 위치 자체도 직관적이지 않음"): the button points where the panel folds to, `▾` (the strip under the
  // native area) in every placement, and stands where the strip's `▴` opens it again (a right panel's last
  // row's right end: folding and opening are one place). Their presses stay apart (the cell between them is
  // neither's). The row stays however short rows run, so the panel can always fold; whatever shares it
  // (5h/7d) takes the cells left of the buttons.
  const foldMark='▾';
  const BUTTONS=5;
  function footRow(left=[]){
    const at=content.length+1,room=Math.max(0,width-4-BUTTONS-1);
    const all=left.reduce((n,p)=>n+measure(clean(p.text)),0);
    const kept=all<=room?left:room>=4?[...clip(left,room-1).parts,{...(left.at(-1)||text('')),text:'…'}]:[];
    const used=kept.reduce((n,p)=>n+measure(clean(p.text)),0);
    hits.push({row:at,left:width-8,right:width-6,action:'settings'});
    hits.push({row:at,left:width-4,right:width-2,action:'fold'});
    return [...kept,text(' '.repeat(Math.max(0,width-4-BUTTONS-used))),text('⚙','dim'),text('   '),text(foldMark,'dim')];
  }
  // The last row placed; beside the native area it stands on the panel's last row inside.
  // Rows past the panel's own give way to it: the head's last rows first (the first head row
  // stays, and a box below keeps both its borders), then whatever runs past the end.
  function lastRow(left,groups,head=0,floor=1){
    const rows=height-2;
    for(let over=content.length-(rows-1);over>0&&head>floor;over--){
      const at=--head;content.splice(at,1);contentGroups.splice(at,1);
      for(let i=hits.length-1;i>=0;i--){if(hits[i].row===at+1)hits.splice(i,1);else if(hits[i].row>at+1)hits[i].row--;}
      if(routeArea&&routeArea.top>at+1){routeArea={...routeArea,top:routeArea.top-1,bottom:routeArea.bottom-1};}
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
  // 11-17 (route zones, the person: "그래도 컬러는 전부 달라야하지 않을까"): the `●` and the spinner of a route of the `direct` zone
  // wear that zone's color (a lit beat in it, a dark beat dimmed); a `dispatch` route's are the working green they were, a
  // request's arrow the request color (`requestForms`). A failure's, a result's and a wait's mark keep the state's own.
  // 11-18 (the harness spinner, below): a spinner on a line that names its harness (`routeGlyph` with `h`) turns in that harness's
  // color before this, so a `direct` zone's working owner line spins in its harness's color; the zone's color stays on the `●`, on a
  // spinner whose line names no harness, and on the zone's other marks.
  const zonePaint=(r,part)=>{
    const look=r&&r.zone==='direct'?styles.zone_direct:null;
    return look&&(part.spin||part.text==='●'||/^[\u2800-\u28ff]$/.test(String(part.text)))?{...part,...look,dim:part.dim===true||look.dim===true}:part;
  };
  // 11-18 (the person: "스피너 컬러도 하네스에 맞추고"): the spinner of a line that names its harness (`h`, that line's own harness part)
  // turns in that harness's color, on every face (a deep pair on the light face, as the harness name's own); a mark that is not the
  // spinner (`●`, `✕`, `✓`), a route's first line and a line with no known harness keep the state's own color.
  const harnessSpin=(g,h)=>h&&Number.isInteger(h.foreground)?{...g,foreground:h.foreground,bold:false,dim:false}:g;
  const routeGlyph=(r,h)=>{const g=r.paint?.glyph||text('·');if(live(r))return h&&Number.isInteger(h.foreground)?harnessSpin({...g,...spinGlyph},h):zonePaint(r,{...g,...spinGlyph,role:'state:working'});return zonePaint(r,{...g,role:'state:'+(r.kind==='done'&&r.outcome_key==='g_dead'?'dead':r.kind||'idle')});};
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
    // 11-16 (the person: "금액은 대신 opencode에선 옮겨도 되고"): OpenCode's cost, which stood in the bottom line of its own
    // screen, stands after the context (and the MCP count) in that window's panel only.
    const cost=painted(model.cost);
    const contextParts=mcp.length||cost.length?joined([painted(model.context),mcp,cost]):painted(model.context);
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
    // `tail` stands after the value's last row and keeps its cells (the `›` of a row a press opens). The value is cut
    // and wrapped as it is without it: the tail takes room only where the last row has it, else it stands at that
    // row's end as `…›`, so neither the value's rows nor any row around them depend on the tail.
    const tailed=(row,limit,tail)=>{
      if(!tail.length||size(row)+size(tail)<=limit)return [...row,...tail];
      const fit=clip(row,Math.max(0,limit-2));
      return [...fit.parts,{...(fit.parts.at(-1)||text('')),text:'…'},tail.at(-1)];
    };
    const field=(label,parts,max=Infinity,tail=[])=>{
      const lead=labelWidth?text(label.padEnd(labelWidth),'dim'):null,rest=pad(labelWidth),room=inner-labelWidth;
      let rows=wrapAll(parts,room);const from=top.length;
      if(rows.length>max){rows=rows.slice(0,max);rows[max-1]=ellipsis(rows[max-1],room);}
      rows.forEach((row,i)=>{top.push([...(lead?[i?rest:lead]:[]),...(i===rows.length-1?tailed(row,room,tail):row)]);if(i)extra.add(top.length-1);});
      if(rows.length>1)oneRow.set(from,[...(lead?[lead]:[]),...tailed(cut(parts,room),room,tail)]);
    };
    // Spec rules 37 (a), 41: beside the native area the worktree count stands beside the
    // repository name (`REPO   hearting · wt 12`) and the Git row keeps the branch, whether it
    // is up to date and the warnings (`GIT    main ✓`), the labels as TITLE's and NOW's.
    const worktrees=painted(model.worktrees),sideGit=Array.isArray(model.git_side)?painted(model.git_side):git;
    const sideRepo=Array.isArray(model.repo_side)?painted(model.repo_side):repo;
    const labelled=(label,parts)=>labelWidth?[text(label.padEnd(labelWidth),'dim'),...parts]:parts;
    // 11-17 (the person's PEERS sketch): `PEERS  [c7] ⚑  ─ [d4] [3b]`: the supervising session, its flag, two cells, a dash, then the
    // ones it supervises; the same order from every window. Each number is its harness's color (this window's own bold),
    // the flag alone Fleet's steward pink, the dash and the `·` between two groups dim. No state of any session. Short of
    // cells the last numbers go from the back and the count of the ones a group lost stands inside that group (`[3b] ⚑ ─
    // +2`), never after another group's. A group keeps its head (`[x] ⚑ ─`) while that fits, else the group goes whole,
    // and no count is put on the groups left; a window with no relation has no row.
    const peerRow=room=>{
      const peers=model.peers&&typeof model.peers==='object'&&Array.isArray(model.peers.groups)?model.peers:null;
      const groups=(peers?peers.groups:[]).filter(g=>g&&g.steward&&Array.isArray(g.members)&&g.members.length);
      if(!groups.length)return [];
      // 11-18 (the person: "PEERS에서 볼드 여부만 해놓으니까 나의 세션이 뭔지 확 안오네, 나머지는 컬러도 조금 어둡게"): this window's number is its harness
      // color, bold (the brightest); every other session's is its harness's one step back (the USAGE names' own `FACES.*.harnessName`;
      // on the light face `FACES.light.peerOther`, the lighter step, so it never equals this window's deep hue), drawn as that
      // face's fixed color, never the terminal's dim, plain. No color: weight alone tells them apart, as before.
      const number=e=>{const h=/^hb_(claude|codex|opencode)$/.exec(e.key||'');
        return {...text('['+clean(e.tag)+']',e.key||'dim'),...(e.me===true?{bold:true,role:'peer'}:h?{role:'harness-name',harness:h[1],peer:true}:{role:'peer'})};};
      const flag={...text('⚑','tag_steward'),role:'steward',...(model.no_color===true||cfg.theme==='mono'?{bold:true,dim:false}:{})};
      const build=keep=>{
        const out=[];let rest=keep;
        for(const g of groups){
          const take=Math.min(rest,g.members.length),lost=g.members.length-take;rest-=take;
          const piece=[number(g.steward),text(' '),flag,text('  '),text('─','dim'),...g.members.slice(0,take).flatMap(e=>[text(' '),number(e)]),
            ...(lost?[text(' +'+lost,'dim')]:[])];
          const next=[...out,...(out.length?[text(' · ','dim')]:[]),...piece];
          // A group with no number left stands only where its head and its count fit; else it, and those after it, go.
          if(!take&&size(next)>room)break;
          out.splice(0,out.length,...next);
        }
        return out;
      };
      const total=groups.reduce((n,g)=>n+g.members.length,0);
      for(let keep=total;keep>=1;keep--){const row=build(keep);if(size(row)<=room)return row;}
      return cut(build(1),room);
    };
    // 11-15 (the person: "현황은 REPO 옆에 버튼을 만드는게 나을거 같은데", design 2 as recommended): the status
    // button `현황 ›` (`STATUS ›`, Fleet's `fleet.repo.status`) at the right end of the REPO row: the word in
    // the body color, `›` dim as a section head's, no ground, no count; no status document: all of it dim
    // (a press still opens it). Short of cells the word goes first (`›` stays), then `· wt N` whole, then
    // the name is cut with `…`; the name and `›` stay to the last. A press anywhere on the row opens the
    // status popup. No artifact root: no button (`status_doc` absent).
    const statusDoc=model.status_doc&&typeof model.status_doc==='object'&&clean(model.status_doc.word).trim()?model.status_doc:null;
    const topActions=new Map();
    const statusButton=whole=>{const arrow=text('›','dim');if(!whole)return [arrow];
      return [text(clean(statusDoc.word).trim(),statusDoc.has_doc===true?'loc_repo':'dim'),text(' '),arrow];};
    // `name` and `count` with the button at the right end of `room` cells, or null without a button.
    const withButton=(name,count,room)=>{
      if(!statusDoc)return null;
      // In order: the whole button, `›` alone, then `› ` without the count; the word does not come back.
      for(const [lead,whole] of [[[...name,...count],true],[[...name,...count],false],...(count.length?[[name,false]]:[])]){
        const b=statusButton(whole);if(size(lead)+1+size(b)<=room)return [...lead,pad(room-size(lead)-size(b)),...b];
      }
      return [...cut(name,Math.max(1,room-2)),pad(Math.max(1,room-Math.min(size(name),Math.max(1,room-2))-1)),...statusButton(false)];
    };
    // Spec rule 46: the newest saved memory records (this repository's and the global ones, MEM_MAX at
    // most) stand in their own section under the messages, two rows or three each (below). An older backend
    // sends the newest one as painted pieces only (`3m decision headline`): that one record,
    // without its kind (rule 46 (e)); the head carries no MEM row either way.
    const memItems=(()=>{
      if(!side)return [];
      if(Array.isArray(model.memory_items))return model.memory_items.filter(m=>m&&typeof m==='object'&&clean(m.text).trim()).slice(0,MEM_MAX)
        .map(m=>({kind:clean(m.kind).trim(),age:clean(m.age).trim(),text:clean(m.text).trim(),tier:clean(m.tier).trim(),scope:clean(m.scope).trim()}));
      const old=painted(model.memory),body=old.length?clean(old.at(-1).text).trim():'',first=old.length>1?clean(old[0].text).trim():'';
      return body?[{kind:'',age:/^\d+[mhd](?: \d+[mh])?$/.test(first)?first:'',text:body}]:[];
    })();
    const P=v=>Array.isArray(v)?v.filter(p=>p&&typeof p==='object'):[];
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
        const button=withButton(sideRepo,count,room);
        if(button)topActions.set(top.length,'popup:STATUS');
        top.push(labelled('REPO',button||(withCount?[...sideRepo,...count]:cut(sideRepo,room))));mark(0,'head',...(withCount&&!sideGit.length?['git']:[]));
      }
      // Short of cells the first part (the branch, the model name) gives way so what follows
      // it (the status and a warning, the effort) stays whole.
      const firstGives=(parts,room)=>{
        if(size(parts)<=room||parts.length<2)return cut(parts,room);
        const rest=parts.slice(1),left=room-size(rest);
        return left>=6?[...cut(parts.slice(0,1),left),...rest]:cut(parts,room);
      };
      if(sideGit.length){gitRows.add(top.length);top.push(labelled('GIT',firstGives(sideGit,inner-labelWidth)));mark(top.length-1,'git');}
      {const peerParts=peerRow(inner-labelWidth);if(peerParts.length){gitRows.add(top.length);top.push(labelled('PEERS',peerParts));}}
      if(top.length){metaGaps.add(top.length);top.push([]);}
      if(labelWidth){
        // Spec rule 42: `MODEL` and `CTX` as REPO's and GIT's; `CTX` stands for the word
        // `context` (a backend without `context_side` has its word taken off here).
        const word=parts=>{const first=parts[0],rest=String(first?.text??'').replace(/^context /,'');
          return !first||rest===String(first.text)?parts:rest?[{...first,text:rest},...parts.slice(1)]:parts.slice(1);};
        const gauge=Array.isArray(model.context_side)?painted(model.context_side):word(painted(model.context));
        const value=mcp.length||cost.length?joined([gauge,mcp,cost]):gauge;
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
      // 11-17: a press on the NOW (its rows end in `›`, as a section's head does) opens the NOW popup.
      if(now){const from=top.length;field('NOW',line(now,nowKey),Infinity,[text(' '),text('›','dim')]);for(let i=from;i<top.length;i++)topActions.set(i,'popup:NOW');}
      if(tidy&&!asking){
        // 11-16: a notice of something that ended well (`tidy_quiet`) is one or two dim rows without `⚠`; what
        // leaves the person something to do keeps the caution look on up to three rows.
        const quiet=model.tidy_quiet===true,key=quiet?'dim':'lvl_y',most=quiet?2:3;
        const rows=[];wrap(quiet?line(tidy,key):line('⚠ '+tidy,key),most+1,undefined,rows);
        const kept=rows.slice(0,most);
        if(rows.length>kept.length){const last=kept.length-1,fit=clip(kept[last],inner-1);kept[last]=[...fit.parts,text('…',key)];}
        top.push(...kept);
      }
      // One empty row parts the head from the route box below it (a head with no content has its rule).
      if(content){spacers.add(top.length);top.push([]);}
    }else{
      // The bottom layout has few rows: the metadata row (or rows) first, then one
      // content row; the row break parts them (no rule).
      const first=joined([repo,selected,contextParts]);
      // The status button at the first row's right end (the word first gives way, then the row is cut).
      const atEnd=row=>{if(!statusDoc)return row;topActions.set(0,'popup:STATUS');
        for(const whole of [true,false]){const b=statusButton(whole);if(size(row)+1+size(b)<=inner)return [...row,pad(inner-size(row)-size(b)),...b];}
        return [...cut(row,Math.max(1,inner-2)),pad(Math.max(1,inner-Math.min(size(row),Math.max(1,inner-2))-1)),...statusButton(false)];};
      if(size(first)<=inner){top.push(atEnd(first));mark(0,'head',...(contextParts.length?['context']:[]));}
      else{put(top,joined([repo,selected]),1);top[0]=atEnd(top[0]);mark(0,'head');const from=top.length;put(top,contextParts,1);mark(from,'context');}
      // Running jobs join that row when they fit; otherwise they take a row (below Git) that can give way.
      const joinJobs=jobs.length&&size(top.at(-1))+3+size(jobs)<=inner;
      if(joinJobs){top[top.length-1]=joined([top.at(-1),jobs]);mark(top.length-1,'jobs');}
      putGit();
      {const peerParts=peerRow(inner-6);if(peerParts.length){optional.add(top.length);top.push([text('PEERS ','dim'),...peerParts]);}}
      if(jobs.length&&!joinJobs){optional.add(top.length);jobRows.add(top.length);top.push(cut(jobs,inner));mark(top.length-1,'jobs');}
      // The repo's open work gives way with the running jobs.
      if(work.length){optional.add(top.length);top.push(cut(work,inner));}
      if(model.warning)top.push(line(model.warning,'lvl_r'));
      if(tidy&&!asking){
        const quiet=model.tidy_quiet===true,key=quiet?'dim':'lvl_y';
        const rows=[];wrap(quiet?line(tidy,key):line('⚠ '+tidy,key),2,undefined,rows);
        const kept=rows.slice(0,1);
        if(rows.length>kept.length){const fit=clip(kept[0],inner-1);kept[0]=[...fit.parts,text('…',key)];}
        top.push(...kept);
      }
      if(title||now){
        // Title and NOW share what the answer wait leaves: each keeps at least half of it.
        const nowParts=now?[text('NOW ','dim'),text(now,nowKey)]:[],titleParts=title?line(title,'loc_repo'):[];
        // A NOW ends the row in `›`, a press on the row opens the NOW popup. The row is laid out without it.
        const arrow=now?[text(' '),text('›','dim')]:[],avail=inner-(title&&now?3:0),
          titleRoom=title&&now?Math.max(Math.min(size(titleParts),Math.floor(avail/2)),avail-size(nowParts)):avail;
        if(now)topActions.set(top.length,'popup:NOW');
        top.push(tailed(cut(joined([cut(titleParts,Math.max(1,titleRoom)),cut(nowParts,Math.max(1,avail-(title?Math.min(size(titleParts),titleRoom):0)))]),inner),inner,arrow));
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
    const sentSource=typeof sent==='function'?sent()||[]:[];
    const sentAt=()=>sentSource.map(row=>cut(row,inner));
    const sentRows=sentAt();
    const questionCount=waitCount||(summary?String(summary.count):'');
    const asked=!!(summary||sentRows.length||model.wait||guide);
    let briefTwo=true,keepGuide=true,keepSent=true,keepAction=true;
    // 11-15 (the person: "빨간 틴트 안에 다시 검은 틴트는 못생겼는데" / "그냥 질문은 글자 자체를 붉은 색으로"): the
    // question to answer here, now, reads apart from the messages' and memories' rows by its letters
    // alone: its words (the first row, the second where there are no choices) and its choices' row are
    // in the answer wait's red (`accent`, as its count and `◑`), never dimmed, no weight added, on no
    // ground of their own; the choices' row is one line of words (`1 label · 2 label`, the ` · ` and
    // `+N` dim). `↵ answer · Ctrl+]` stands in rose at the right under them. Short of rows the action
    // row goes first, then the choices; the first row stays to the last. Eight colors keep the red;
    // mono and NO_COLOR the weight. A wait answered elsewhere, the answers on their way and the guide
    // line are never red: what is red is what a press answers now.
    const here=!!summary&&summary.answerable===true;
    const red=part=>({...part,...(styles.g_blocked||{}),role:'accent',dim:false,bold:part.bold===true||model.no_color===true});
    // 11-16 (the person: "G1에서 질문 바운더리만 붉게 하고, 내용은 흰색으로 해"): inside the box the red is the border's alone, the
    // question's words, its choices and its `◑` are in the body color (folded back to the plain section they are red as before).
    const body=part=>({...part,...(styles.loc_repo||{}),role:'',dim:false,bold:part.bold===true});
    const ink=part=>boxedQuestions()?body(part):red(part);
    // The choices as one row of words: the first one always shows (short of cells its name is cut with
    // `…`, its number kept, the rest counted `+N`; narrower still the count gives way first).
    const choiceLine=parts=>{
      const lead=parts.length&&!clean(parts[0].text).trim()?[parts[0]]:[];
      const items=choiceItems(parts).map(c=>c.map(part=>/^\d+ $/.test(clean(part.text))?red(part):part.dim===true?part:ink(part)));
      const more=Math.max(0,...parts.map(part=>{const m=/^(?: · )?\+(\d+)$/u.exec(clean(part.text));return m?Number(m[1]):0;}));
      const count=n=>n?[text(' · +'+n,'dim')]:[];
      // The `…` of a cut name stands for its words: in their color, not the number's rose.
      const cutItem=(item,room)=>cut(item,room).map((part,i,all)=>i===all.length-1&&part.text==='…'&&item[1]?{...item[1],text:'…'}:part);
      for(let k=items.length;k>=1;k--){
        const row=[...lead,...items.slice(0,k).flatMap((c,i)=>[...(i?[text(' · ','dim')]:[]),...c]),...count(items.length-k+more)];
        if(size(row)<=inner)return row;
      }
      if(items.length)for(const rest of [count(items.length-1+more),[]]){
        const room=inner-size(lead)-size(rest);
        if(room>=2||!rest.length)return [...lead,...cutItem(items[0],Math.max(1,room)),...rest];
      }
      return [...lead,...count(more)];
    };
    // `↵ answer · Ctrl+]` at the right; narrow, its first word alone.
    const actionRow=()=>{
      const words=t('answer_action','↵ answer · Ctrl+]'),first=words.split(' · ')[0];
      const shown=measure(words)<=inner?words:first,a=[{...text(shown,'g_blocked'),role:'accent'}];
      return [pad(inner-size(a)),...a];
    };
    // 11-15 (the person: "대기 중인건 점멸을 시키자" / "질문만"): the red of what waits for the answer blinks:
    // the question answered here (its rows' red letters), and for one answered elsewhere its `◑` and the
    // guide line's count (in the wait's red). The sent answers' lines never blink.
    // (A row the backend already cut to a wider panel ends in its own `…` part: that is taken off before the row is cut again.)
    const recut=row=>size(row)>inner&&row.length>1&&clean(row.at(-1).text)==='…'?row.slice(0,-1):row;
    const summaryRows=()=>{
      const list=P(briefTwo?summary.rows:summary.one).map(row=>accent(recut(P(row))));
      if(!here)return list.map(row=>cut(row,inner).map(blinking));
      const out=list.map((row,i)=>i===1&&summary.choices>0?choiceLine(row):cut(row.map(part=>!clean(part.text).trim()?part:part.role==='accent'&&!boxedQuestions()?part:ink(part)),inner));
      if(keepAction&&briefTwo)out.push(actionRow());
      // 11-16 (the person: the box's border blinks, not the letters inside it): the letters of a question answered here
      // stand still while the box carries the blink; folded back to the plain section they blink as before.
      return boxedQuestions()?out:out.map(row=>row.map(blinking));
    };
    const guideRow=()=>{const m=/^(\d+)(.*)$/su.exec(guide);
      return cut(m?[blinking(red(text(m[1],'g_blocked'))),text(m[2],'dim')]:[text(guide,'dim')],inner);};
    const questionRows=()=>[...(keepSent?sentAt():[]),
      ...(summary?summaryRows():[]),
      ...(guide&&keepGuide?[guideRow()]:[])];
    // ROUTE (restored 11-15 from v4.36.4, the user's correction: "라우트도 비슷한 방식으로 팝업으로 뜨게"
    // asked for a popup on a press, not for the box to go): the route box as the person shaped it
    // (spec rules 23, 24, 25, 34, 36, 38, 43, 48, 57), drawn by the same code. A press on any of its
    // rows opens the route popup with that route unfolded, on its top border the route popup, on a
    // request row the MESSAGES popup; nothing opens in place any more.
    const routeLine=['time','model'].includes(cfg.route_line)?cfg.route_line:'time';
    // Routes: the current ones the statusline counts, one row each, then the recent results
    // the producer keeps after them (a finished route with its outcome, at most two): the
    // notice line under Claude's input no longer carries them (2026-11-14).
    const countedRoutes=routes.slice(0,Math.max(0,model.route_count??routes.length));
    const shown=countedRoutes.length?[...countedRoutes,...routes.slice(countedRoutes.length).filter(r=>r&&r.state==='done'&&r.outcome)]:countedRoutes;
    // The whole count is the producer's. Spec rule 23: every current route gets a
    // row; a count the producer did not send rows for shows only in the box's lower border.
    const total=Number.isInteger(model.route_total)&&model.route_total>=countedRoutes.length?model.route_total:countedRoutes.length;
    const listed=shown,unsent=countedRoutes.length?total-countedRoutes.length:0;
    // Requests this session sent (→) or received (◂): lines in the route box after the routes
    // (R27); each opens in place by its own key and reads whole on its own page.
    const requestList=Array.isArray(model.requests)?model.requests.filter(q=>q&&typeof q.id==='string'):[];
    // Under the route (`side:false`) a unit a worker called is a row of its own under its caller's, in the same columns: it is a
    // route-shaped row (its title two cells in, its kind, its time) that no count of routes holds. Its cells count in the columns' widths.
    const workersAll=r=>Array.isArray(r.workers_live)?r.workers_live.filter(w=>w&&typeof w==='object').slice(0,8):[];
    const walkCalled=(items,depth)=>(Array.isArray(items)?items:[]).filter(c=>c&&typeof c==='object').slice(0,8).flatMap(c=>[[c,depth],...walkCalled(c.called,depth+1)]);
    const flatCalled=r=>[...walkCalled(r.called,0),...workersAll(r).flatMap(w=>walkCalled(w.called,0))];
    const calledBottom=new Map(listed.map(r=>[r,side?[]:flatCalled(r).filter(([c])=>clean(c.unit_word||'').trim()).map(([c,depth])=>({
      title:c.title||'',kind:c.kind,paint:c.paint||{},elapsed:c.elapsed,unit_route:true,unit_word:c.unit_word,kind_word:c.kind_word,
      kind_field:c.kind_field,indent:2+2*depth,shape:''}))]));
    const calledAll=[...calledBottom.values()].flat();
    const indentOf=r=>r.indent||0;
    const titleCols=x=>[...(indentOf(x)?[text(' '.repeat(indentOf(x)))]:[]),...titleOf(x,'')];
    // The columns of a set of rows: the width of one cell of each, and of the time. The routes' columns are the routes' alone;
    // the called rows' are counted only in the set bottomLayout tries them in.
    const columnsOf=set=>({set,elapsed:Math.max(0,...set.map(r=>measure(r.elapsed||''))),width:pick=>Math.max(0,...set.map(r=>size(pick(r))))});
    const routeCols=columnsOf(listed);
    // Under the route (`side:false`) a unit route's worker is its unit: the unit's short name leads the doer column, dim as a role word.
    // The column is as wide as the routes' harness, model and effort. A unit's name is counted only where every route of the box is a
    // unit route (so no other route's row changes); beside any other route the name stands in the column's cells if they hold it
    // (bottomLayout, doerCell), never in the title's.
    const owner=(r,named=true)=>{const p=leadPaint(r),u=named&&r.unit_route===true?clean(r.unit_word||'').trim():'';
      return [...(u?[text(u,'dim'),text(' ')]:[]),p.harness,p.model_text&&text(' '),p.model_text,p.effort&&text(' '),p.effort].filter(Boolean);};
    // A route whose record names its kind: its stage stands in the kind column, never as
    // the older `execute ✕ 2/3` step text (that stays for a record that names no kind).
    const shaped=r=>{const shape=String(r.shape||'');return shape==='one-shot-owner'||!!shape&&shape!=='unknown'&&(!/^staged/.test(shape)||Array.isArray(r.stages)&&r.stages.length>0);};
    const third=r=>r.step&&!shaped(r)?[text(r.step)]:[];
    // 11-16 (the person: "staged stage 펼쳤을떄 stage 이름은 좀 눈에 띄게 ... fleet 처럼"): a stage's name in the expanded rows
    // (a live worker's stage row, the kind row's stage) wears the color Fleet gives that stage of its route (the backend's
    // `key`/`stage_key`: Fleet's `route_stage_key`, one table for both screens); mono, eight colors and NO_COLOR read as before.
    const stageInk=key=>key&&model.no_color!==true&&cfg.theme!=='mono'&&(model.colors??256)>=256?key:'loc_repo';
    // 11-17 (route row 5): a stage's mark. The one thing that blinks in a route with a stage line is the current stage's
    // name (Fleet's beat: lit in its color, dark in the off color), so the `●` after it is gone; `✓` and `✕` stay.
    const markOf=stage=>stage.mark==='✓'?text('✓','lvl_g'):stage.mark==='✕'?text('✕','g_dead'):null;
    const stageName=(stage,key)=>stage.mark==='●'?{...text(clean(stage.name||'·'),beat?key:'g_work_off'),beat:true}:text(clean(stage.name||'·'),key);
    const stageCell=(stage,key)=>[stageName(stage,key),...(markOf(stage)?[text(' '),markOf(stage)]:[])];
    // The kind word leads a route, the one bold word of its first line (the body color, never dim, in mono and NO_COLOR too);
    // its intensity follows in dim brackets. Nothing else goes in the brackets.
    // 11-18 (route kind color; the person: "code, lab 같은 라우트 유형에 컬러를 넣어줘" / "강도도 컬러를 넣어주자"): the kind word wears
    // its field's hue (the backend's `kind_field`: one of five; an older route that records none, its own kind's name when that is
    // a field's), and its bracket the same hue stepped by weight (quick dim, standard plain, strong and beyond bold). Where no
    // hue is shown (mono, NO_COLOR, under 256 colors) or the route has no field, both read as they did.
    const hued=model.no_color!==true&&cfg.theme!=='mono'&&(model.colors??256)>=256;
    const FIELDS=['code','lab','design','docs','research'],WEIGHT={quick:'q',light:'q',standard:'s',strong:'b',thorough:'b',adversarial:'b'};
    const kindField=(r,name)=>{
      const word=clean(name).trim(),own=word===clean(r.kind_word||'').trim();
      const field=own?clean(r.kind_field||'').trim():['code','lab','design','research'].includes(word)&&!clean(r.field||r.kind_field||'').trim()?word:'';
      return hued&&FIELDS.includes(field)&&styles['kind_'+field]?field:'';
    };
    const kindText=(word,field='')=>field?{...text(clean(word),'kind_'+field),bold:true,dim:false}:{text:clean(word),foreground:null,bold:true,dim:false};
    const bracket=(word,field='')=>field?{...text('('+word+')','kind'+(WEIGHT[String(word).trim().toLowerCase()]||'s')+'_'+field),role:'kind'}:text('('+word+')','dim');
    const framed=r=>String(r.shape||'')==='framed';
    // A direct route's word stands for its intensity when the record names none (the backend sends it in the screen language).
    const intensityRaw=r=>{
      const word=clean(r.intensity||'').trim();
      if(word||framed(r))return word;
      const shape=clean(r.shape||'').trim(),stages=Array.isArray(r.stages)?r.stages:[];
      return shape&&!/^(staged|one-shot-owner|unknown)/.test(shape)&&!stages.length?shape:'';
    };
    // 11-17 (route zones): in the `direct` zone the zone's own name says it, so `(direct)` (the word the record or its shape
    // gives) is left out of the line; a real intensity (`quick`) stays.
    // A unit route's intensity is not a choice (it is always `quick`), so its first line has no bracket.
    const intensityOf=r=>{
      if(r.unit_route===true)return '';
      const word=intensityRaw(r);
      return r.zone==='direct'&&(/^direct$/i.test(word)||word===clean(r.shape||'').trim())?'':word;
    };
    const kindHead=r=>{const word=clean(r.kind_word||'').trim(),inten=intensityOf(r);
      const field=kindField(r,word);
      return word?[kindText(word,field),...(inten?[bracket(inten,field)]:[])]:[];};
    // Spec rule 34 (d): the bottom layout's route row names its kind too: `code(standard) · staged 3/7: execute`, a route with
    // no stage line reads `lab(quick)`; short of cells `3/7: execute`.
    function kindCell(r){
      const cell=shapeCell(r),head=kindHead(r);
      if(!head.length)return cell&&{...cell,kind:cell.full};
      if(!cell)return {full:[],short:[],kind:head};
      return {...cell,kind:[...head,text(' · ','dim'),...cell.full]};
    }
    // 11-17 (route row 5): every route with a stage list reads its stage line, whatever its shape: the shape's own word
    // (`staged`, `map-reduce`, `transactional-owner`; none when the record names none), the position, the stage's name.
    // A frame route has no stage line (its legs hang under it), and a route its owner does alone (one stage) has none.
    const stageWord=shape=>/^staged/.test(shape)?'staged':/^(|unknown|framed|one-shot-owner)$/.test(shape)?'':shape;
    const stageLined=r=>{const shape=String(r.shape||''),n=Array.isArray(r.stages)?r.stages.length:0;
      return !framed(r)&&(/^staged/.test(shape)?n>0:n>1);};
    function shapeCell(r){
      const shape=String(r.shape||''),stages=Array.isArray(r.stages)?r.stages:[];
      const current=stages.findIndex(stage=>stage.current||stage.mark==='✕'),next=stages.findIndex(stage=>stage.mark!=='✓');
      if(stageLined(r)){
        const at=current>=0?current:next>=0?next:stages.length-1,position=(at+1)+'/'+stages.length,word=stageWord(shape);
        const stage=stageCell(stages[at],'loc_repo');
        return {full:[text((word?word+' ':'')+position+': ','dim'),...stage],short:[text(position+': ','dim'),...stage]};
      }
      return r.step&&!shape?{full:[text(r.step)],short:[text(r.step)]}:null;
    }
    // Which of the kind column's forms the bottom rows keep, the same for every row: the kind with its stage word first, then
    // the stage word alone; the title keeps ten cells (or its whole width) through both. Short of that the rows keep the
    // earlier columns (title, owner, context, then the stage). 11-18: the command column (`>_ Bash`) is gone, as it is
    // beside the native area: a route reads its harness, model and context number, and its NOW on the row under it.
    const planBottom=(ow,gw,C)=>{
      const kinds=C.set.map(kindCell);
      const widths=Object.fromEntries(['kind','full','short'].map(level=>[level,Math.max(0,...kinds.map(k=>k?size(k[level]):0))]));
      const longest=C.width(titleCols),avail=inner-(2+3+ow+3+gw+1+C.elapsed);
      const order=widths.kind>widths.full?['kind','full','short']:['full','short'];
      for(const level of order){
        if(!widths[level])break;
        const room=avail-(3+widths[level]);
        if(room>=Math.min(longest,10))return {level,width:widths[level],title:Math.min(longest,room),kinds,top:level===order[0]};
      }
      return {level:''};
    };
    // The bottom rows' columns are one set for the box. The doer column is as wide as the routes' harness, model and effort (a
    // unit's name counted only where every route of the box is a unit route). Where a unit route is drawn among other routes its
    // name stands in front of its doer cell when the column holds it (see doerCell), and the column is widened to the whole named
    // cell only where that changes nothing else in the box: the same stage form, the same title width (so beside a stage cell with
    // room to spare), and only from the richest stage form the box can have (the first of the order), so a width that widens it
    // widens it at every wider width; widened or not, every row's columns stand at the same cells and no row lends another its cells.
    // 11-19 (review of #1742): a unit a worker called never takes from its caller's row. The columns and the stage form are the
    // routes' alone (`routeCols`: the box as it stands with no called unit); the called rows are drawn into them (the title cut
    // with `…` and empty where no cells are left, the worker cell folded in the narrow worker line's order). Only where the
    // columns grown by the called rows leave every route row reading the same, letter for letter (as it reads in the routes'
    // own), the box takes those wider columns, as it takes the widened named column above.
    let bottomSet;
    const layoutOf=C=>{
      const gw=C.width(x=>leadPaint(x).gauge5||[]),named=C.width(x=>doerCell(x,Infinity)),
        base=listed.every(y=>y.unit_route===true)?named:C.width(x=>owner(x,false)),plan=planBottom(base,gw,C);
      if(named>base&&plan.level&&plan.top){const wide=planBottom(named,gw,C);if(wide.level===plan.level&&wide.title===plan.title)return {ow:named,gw,plan:wide,C};}
      return {ow:base,gw,plan,C};
    };
    const bottomLayout=()=>{
      if(bottomSet)return bottomSet;
      const own=layoutOf(routeCols);
      if(!calledAll.length)return bottomSet=own;
      const grown=layoutOf(columnsOf([...listed,...calledAll])),letters=parts=>clean(parts.map(p=>p.text).join('')).replace(/\s+/g,' ').trim();
      return bottomSet=grown.plan.level===own.plan.level&&listed.every(r=>letters(folded(r,own))===letters(folded(r,grown)))
        &&calledAll.every(c=>{folded(c,grown);return !clipped;})?grown:own;
    };
    // A unit route's doer cell in a column `width` wide: its name, then as much of the effort, harness and model as the column holds,
    // in the order the narrow worker line gives them up (effort, then harness, then model; the name stays last). A column that
    // holds not even the name gets the cell with no name.
    const doerCell=(r,width)=>{
      const unit=r.unit_route===true?clean(r.unit_word||'').trim():'',p=leadPaint(r);
      if(!unit)return owner(r);
      const H=p.harness?[p.harness]:[],M=p.model_text?[p.model_text]:[],F=p.effort?[p.effort]:[],keep=M.length?M:H;
      for(const parts of [[H,M,F],[H,M],[[],keep,[]],[[],[],[]]]){
        const rest=parts.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x),row=[text(unit,'dim'),...(rest.length?[text(' '),...rest]:[])];
        if(size(row)<=width)return row;
      }
      return owner(r,false);
    };
    let clipped=false;   // the last bottom row folded had its end cut away by the width
    function folded(r,laid){
      // 11-18: beside the native area the one-row form is the leader's; under it the row reads the owner, whose NOW stands under it.
      const p=side?r.paint||{}:leadPaint(r),ind=indentOf(r),lead=[...(ind?[text(' '.repeat(ind))]:[]),routeGlyph(r,p.harness),text(' ')];
      if(!side){
        const lay=laid||bottomLayout(),{ow,gw,plan}=lay,C=lay.C,calledRow=ind>0;
        // A called row is drawn into columns it did not make: its title is cut to what is left of the title's; its worker cell is the
        // unit's name with what the column holds, else the name cut (the name is what the cell is for).
        const calledCell=(c,width)=>{const cell=doerCell(c,width),unit=clean(c.unit_word||'').trim();
          return cell.length&&clean(cell[0].text)===unit?cell:fit([text(unit,'dim')],width);};
        const fit=(parts,room)=>calledRow&&room<1?[]:cut(parts,room),doer=calledRow?calledCell(r,ow):doerCell(r,ow);
        // The time of a called row is cut to the width the routes' times leave.
        const stamp=r.elapsed?calledRow&&measure(r.elapsed)>C.elapsed?fit([text(r.elapsed)],C.elapsed):[text(r.elapsed)]:[];
        // The title column is as wide as the routes' make it; a called row's two cells in are never wider than it (the bars stay).
        const avail=inner-(2+3+ow+3+gw+3+1+C.elapsed),titleWidth=plan.level?plan.title:Math.min(C.width(titleCols),Math.max(6,avail-C.width(third),Math.floor(avail/2)));
        const shift=calledRow?Math.min(ind,titleWidth):ind,row=calledRow?[text(' '.repeat(shift)),routeGlyph(r,p.harness),text(' ')]:lead;
        if(plan.level){
          const at=C.set.indexOf(r),kind=at>=0?plan.kinds[at]:kindCell(r),cell=kind?kind[plan.level]:[],name=fit(titleOf(r),plan.title-shift);
          const parts=[...row,...name,pad(plan.title-shift-size(name)),text(' │ '),...doer,pad(ow-size(doer)),text(' │ '),
            ...(p.gauge5||[]),pad(gw-size(p.gauge5||[])),text(' │ '),...cell];
          const end=stamp.length?[text(' '),...stamp]:[],left=cut(parts,inner-size(end));clipped=size(parts)>inner-size(end);
          return [...left,pad(inner-size(left)-size(end)),...end];
        }
        // The title gives way first, but keeps half of what the stage column would take.
        const name=fit(titleOf(r),titleWidth-shift);
        const base=[...row,...name,pad(titleWidth-shift-size(name)),text(' │ '),...doer,pad(ow-size(doer)),text(' │ '),
          ...(p.gauge5||[]),pad(gw-size(p.gauge5||[]))];
        // The elapsed time keeps the right edge; the stage gives way first, whole (spec rule 30 (d)).
        const end=stamp.length?[text(' '),...stamp]:[],whole=[...base,text(' │ '),...third(r)];
        const left=cut(third(r).length&&size(whole)<=inner-size(end)?whole:base,inner-size(end));clipped=size(base)>inner-size(end);
        return [...left,pad(inner-size(left)-size(end)),...end];
      }
      // As in leadRow(): the title gives way first, and each fixed value
      // (harness, model, context, time) shows whole or not at all.
      const H=p.harness?[p.harness]:[],M=p.model_text?[p.model_text]:[],C=p.ctx?[p.ctx]:[];
      const E=routeCols.elapsed?[text(' '.repeat(routeCols.elapsed-measure(r.elapsed||''))+(r.elapsed||''))]:[];
      const drops=routeLine==='model'?[[H,M,C],[H,M],[H],[]]:[[H,C,E],[H,E],[E],[]];
      const join=values=>values.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);
      const right=drops.map(join).find(values=>!values.length||inner-2-size(values)-1>=4)||[];
      const room=inner-2-ind-size(right)-(right.length?1:0),name=cut(titleOf(r),Math.max(1,room));
      return cut([...lead,...name,pad(inner-2-ind-size(name)-size(right)),...right],inner);
    }
    // Spec rule 14: the route box is the one thin rounded box (11-15: the questions section has
    // none, its count and `◑` keep the question's rose). Strength of color tells the sections
    // apart: route plain, messages dim, all from the panel's own styles.
    // Spec rule 22: each line keeps its own role, so a shade colors it without a dim on top.
    // Spec rule 25: a route's state stays inside the route box. A failed current route
    // turns the box line to the failure color; a running one pulses `route N` (below).
    const failedRoute=!stale&&shown.some(r=>r.kind==='dead');
    const tone={route:failedRoute?{key:'g_dead',role:'route-dead'}:{key:'plain',role:'route'},messages:'dim'};
    const L=(v,k)=>typeof k==='object'?{...text(v,k.key),role:k.role,...(k.blink?{blink:true}:{})}:text(v,k);
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
    const boxTop=(label,key,{action,ellipsis=false,note=[],alt=[]}={})=>{
      const room=Math.max(0,inner-2),tail=note.length&&size(note)+3<=room?[L(' ',key),...note,L(' ─',key)]:[];
      // A shorter form of the title (the route box's `REQUEST`, then its route count alone, never a count without its
      // name) stands where the whole one does not fit.
      const fits=form=>form&&form.length&&size(form)<=room-size(tail)-3;
      if(!fits(label)){const form=alt.find(fits);if(form)label=form;else if(alt.length)label=alt.filter(x=>x&&x.length).at(-1)||label;}
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
    // leads the row). The context rows start with a dim `CTX` in the role column, the gauge
    // under the harness, and (spec rule 57) the route's time follows the percent. Spec rule 48: the
    // owner's live workers stand in one rounded box under it (below). An older backend's row
    // keeps its one doer and the rows as they were.
    const treeOf=r=>side&&Array.isArray(r.workers_live);
    const leadPaint=r=>r.owner_paint&&typeof r.owner_paint==='object'?r.owner_paint:(r.paint||{});
    // A frame route has no owner yet: its two legs hang straight under the route (11-17, route row 5).
    // A unit route (one worker called with no owner) has no `owner` or `worker` role: the unit's short name stands in the
    // role word's place (same dim color, same column), and goes whole when the row has no room for it.
    const unitRoute=r=>r.unit_route===true;
    const unitName=r=>unitRoute(r)?clean(r.unit_word||'').trim():'';
    const leadRole=r=>unitRoute(r)?unitName(r):framed(r)&&treeOf(r)?'':r.owner_paint&&typeof r.owner_paint==='object'?'owner':r.role;
    const workersOf=r=>treeOf(r)?r.workers_live.filter(w=>w&&typeof w==='object').slice(0,8):[];
    // The one-row form's percent and time columns, shared by owners and their workers (rule 48 (c)).
    const allWorkers=listed.flatMap(r=>workersOf(r));
    const ctxWidth=Math.max(0,...listed.map(r=>leadPaint(r).ctx?size([leadPaint(r).ctx]):0),...allWorkers.map(w=>w.paint?.ctx?size([w.paint.ctx]):0));
    const timeWidth=Math.max(0,...allWorkers.map(w=>measure(w.elapsed||'')));   // (an owner has no time of its own: the route's stands on its first line)
    const ROLE=new Set(['owner','frame','frame-alt','worker']);
    let unitLabel='';      // the unit's short name on the route being drawn (a unit route's role word)
    const roled=role=>ROLE.has(role)||(!!unitLabel&&role===unitLabel);
    // A frame leg is `frame`, its alternative `frame-alt`; any other stage worker is `worker`.
    const workerRole=w=>unitLabel?unitLabel:w.role==='frame'?(/alt/.test(String(w.stage||''))?'frame-alt':'frame'):'worker';
    // The role column's width is the route's own (11-17: a frame route's `frame-alt` does not widen another route's rows).
    let roleWidth=0;
    const setRoles=r=>{unitLabel=unitName(r);roleWidth=side?Math.max(0,roled(leadRole(r))?measure(leadRole(r)):0,...workersOf(r).map(w=>unitRoute(r)&&!unitLabel?0:measure(workerRole(w)))):0;};
    const roleCell=role=>roleWidth?[text((roled(role)?role:'').padEnd(roleWidth),'dim'),text(' ')]:[];
    const rolePart=r=>roleCell(leadRole(r));
    const spaced=values=>values.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);
    // A unit a worker called (`called`: the backend's list, in the order they were called, each with the ones it called in turn) stands
    // on one row under its caller's row, two cells in (a called unit's own calls two more): its mark, the unit's short name in the
    // role word's dim, its harness, model and effort, and its time at the right edge. Short of cells the effort goes first, then the
    // harness, then the model; the name and the time stay. Only what runs now is listed. Short of rows these rows go first (`keepCalled`).
    let keepCalled=true;
    const calledOf=x=>x&&Array.isArray(x.called)?x.called.filter(c=>c&&typeof c==='object').slice(0,8):[];
    function calledRow(c,gap){
      const p=c.paint||{},name=clean(c.unit_word||'').trim();
      if(!name)return null;
      const head=[text(' '.repeat(gap)),routeGlyph({paint:p,kind:c.kind},p.harness),text(' '),text(name,'dim')];
      const H=p.harness?[p.harness]:[],M=p.model_text?[p.model_text]:[],F=p.effort?[p.effort]:[],keep=M.length?M:H;
      const E=c.elapsed?[text(clean(c.elapsed),'dim')]:[];
      for(const parts of [[H,M,F],[H,M],[[],keep,[]],[[],[],[]]]){
        const values=spaced(parts),body=[...head,...(values.length?[text(' '),...values]:[])];
        if(E.length?size(body)+1+size(E)<=inner:size(body)<=inner)return E.length?[...body,pad(inner-size(body)-size(E)),...E]:body;
      }
      return cut(head,inner);
    }
    const calledRows=(items,gap,depth=0)=>calledOf({called:items}).flatMap(c=>{const row=calledRow(c,gap+2*depth);return row?[row,...(depth<3?calledRows(c.called,gap,depth+1):[])]:[];});
    // The owner's own mark: it spins while it does the work itself, stands as a dim `●` while its workers run, and keeps the
    // route's state mark when nothing runs.
    const ownerGlyph=(r,supervising)=>live(r)?(supervising?text('●','dim'):{...routeGlyph(r,leadPaint(r).harness)}):routeGlyph(r);
    const withBare=(lead,bare)=>{lead.bare=bare;return lead;};   // (`bare`: the same lead without its role word, for a unit's name that does not fit)
    const ownerLead=(r,supervising)=>{const mark=ownerGlyph(r,supervising);return withBare([text('  '),mark,text(' '),...rolePart(r)],[text('  '),mark,text(' ')]);};
    // 11-17 (the person: "라우트 내부의 CTX는 하네스, 모델 옆에 붙이고 게이지는 없애자 ... 대신 NOW를 아래줄에"): a session's
    // first line is its mark, role, harness, model, effort and, beside them, `CTX 40% · 42m` with no gauge. Short of cells the
    // time goes first, then the word `CTX` (the number stays), then the effort, then the harness; the role and the model stay.
    // 11-18 (the person: "CTX는 게이지를 포함해서 아래줄로 내리자 ... NOW를 두줄까지는 허용치를 늘려주고 (owner, worker 둘다)"): a session
    // is three blocks. Its first row is its mark, role, harness, model and effort; under it, two cells in, `CTX ━━━━━───── 41%` (the
    // gauge and its number, then ` · 12m` only when the time is that session's own: a worker's, never the owner's, which is the
    // route's time and stands at the end of the route's first line); under that its NOW on at most two rows (the second ending in
    // `…` where it goes on). A session with no NOW has no NOW rows. Short of cells the first row loses its effort, then its
    // harness; the CTX row loses its gauge's width first (down to the number alone), then its time, then the word `CTX`.
    const whoRow=(r,supervising)=>whoOf(ownerLead(r,supervising),leadPaint(r),leadRole(r));
    function whoOf(lead,p,role,room=inner){
      const value=x=>x?[x]:[];
      const H=value(p.harness),M=value(p.model_text),F=value(p.effort),C=value(p.ctx),keep=M.length?M:H;
      if(!roled(role)&&!H.length&&!M.length&&!F.length)return null;   // (a session whose values are not known: its CTX row leads with its mark instead)
      // A unit's name (in the role word's place) is the last word to stay: the effort goes first, then the harness, then the model, and
      // the name stands alone. Only where the name itself has no room does it go, whole (never cut in the middle), and the row is drawn
      // as a row with no name is.
      const named=!!unitLabel&&!!lead.bare,leads=named?[lead,lead.bare]:[lead];
      for(const head of leads)for(const parts of [[H,M,F],[H,M],[[],keep,[]],...(named&&head===lead?[[[],[],[]]]:[])]){
        const row=[...head,...spaced(parts)];
        if(size(row)<=room)return row;
        // The name alone needs no space after it.
        if(named&&head===lead&&!parts.some(x=>x.length)&&size(row)-1<=room)return row.slice(0,-1);
      }
      return cut(leads.at(-1),room);
    }
    // The CTX row at `gap` cells in: the gauge (the backend's `gauge_row`, at the panel's width), else the five-cell one, else the
    // number alone; the session's own time after it when it has one.
    const ctxRowOf=(p,gap,time,lead=null)=>{
      const C=p.ctx?[p.ctx]:[],at=time?[text(clean(time),'dim')]:[],head=lead||[text(' '.repeat(gap))];
      if(!C.length&&!at.length)return null;
      if(!C.length)return lead?[...head,...at]:null;   // (a time alone is no row: a worker's ends its first row)
      const gauges=[p.gauge_row,p.gauge,p.gauge5].filter((g,i,all)=>Array.isArray(g)&&g.length&&all.indexOf(g)===i).sort((x,y)=>size(y)-size(x));
      // The gauge gives up its width first (down to the number alone), then the time, then the word `CTX`.
      const tries=[];
      for(const withTime of at.length?[true,false]:[false])for(const g of [...gauges,C])tries.push([g,true,withTime]);
      tries.push([C,false,false]);
      for(const [g,word,withTime] of tries){
        const row=[...head,...(word?[text('CTX ','dim')]:[]),...g,...(withTime?[text(' · ','dim'),...at]:[])];
        if(size(row)<=inner)return row;
      }
      return cut([...head,...C],inner);
    };
    // The one-row form used short of rows is the older fold (who, then percent and time at the right edge in shared columns).
    const leadRow=(r,supervising)=>foldRow(ownerLead(r,supervising),leadPaint(r),leadRole(r),[]);
    // The session's NOW at `gap` cells in, on at most `most` rows (the last ending in `…` where the value goes on; a word wider than a
    // row goes on by cells, so no row is wider than the box), none when the session has none (no empty row). Bright while the session
    // works or waits, as Fleet's NOW cell.
    const nowRows=(p,kind,gap,most=2)=>{
      const value=clean(p?.now||'').replace(/\s+/g,' ').trim();
      if(!value)return [];
      const room=Math.max(1,inner-gap),rows=wrapAll([text(value,kind==='working'||kind==='blocked'?'loc_repo':'dim')],room);
      const shown=rows.slice(0,most);
      if(rows.length>most)shown[most-1]=ellipsis(shown[most-1],room);
      return shown.map(row=>[text(' '.repeat(gap)),...row]);
    };
    const nowRow=(p,kind,gap)=>nowRows(p,kind,gap,1)[0]||null;
    // One session's blocks, as the route box draws them: `who` (or, with its CTX row folded in, `one`), `ctx`, and `now` in its two
    // forms (`now2`, `now1`). The steps below (short of rows) drop the second NOW row, then the CTX row (its number goes into `one`),
    // then the first NOW row.
    const sessionOf=({who,one,p,kind,gap,time,lead,called=[]})=>({who,one,called,ctx:ctxRowOf(p,gap,time,who?null:lead),now2:nowRows(p,kind,gap,2),now1:nowRows(p,kind,gap,1)});
    const sessionRowsOf=u=>[...(keepCtxLine?(u.who?[u.who,...(u.ctx?[u.ctx]:[])]:[u.ctx||u.one]):[u.one||u.who]),...(keepNow?(nowTwo?u.now2:u.now1):[]),...(keepCalled&&u.called?u.called:[])];
    // The one-row form (an owner's, and since spec rule 48 (c) its workers' too, two cells in):
    // who, then the percent and the time at the right in columns shared by every route and worker.
    function foldRow(lead0,p,role0,E){
      const value=x=>x?[x]:[];
      const M=value(p.model_text),H=value(p.harness),K=M.length?M:H,F=value(p.effort),C=value(p.ctx);
      let lead=lead0,role=role0;
      const build=(h,m,f,withContext,e,alone)=>{
        const c=withContext&&ctxWidth>0;
        const left=spaced([h,m,f]);
        const right=[...(c?[pad(ctxWidth-size(C)),...C]:[]),...(e&&timeWidth?[...(c?[text(' ')]:[]),pad(timeWidth-size(E)),...E]:[])];
        const body=[...(alone?lead.slice(0,-1):lead),...left];   // (a name alone needs no space after it)
        if(!roled(role)&&!left.length&&!(c&&size(C))&&!(e&&E.length))return {row:body,empty:true};
        return {row:right.length?[...body,pad(inner-size(body)-size(right)),...right]:body,width:size(body)+(right.length?1+size(right):0)};
      };
      const drops=routeLine==='model'?[[H,M,F,1,1],[H,M,[],1,1],[[],K,[],1,1],[[],K,[],1,0],[[],K,[],0,0]]:
        [[H,M,F,1,1],[H,M,[],1,1],[[],K,[],1,1],[[],K,[],0,1],[[],K,[],0,0]];
      // A unit's name (in the role word's place) goes whole, never cut, where no form of the row has room for it.
      // The name stays after the model has gone (the unit's name alone), as in `whoOf`.
      for(const [head,word] of unitLabel&&lead0.bare?[[lead0,role0],[lead0.bare,'']]:[[lead0,role0]]){
        lead=head;role=word;
        for(const pick of head===lead0&&unitLabel&&lead0.bare?[...drops,[[],[],[],0,0,1]]:drops){
          if(!E.length&&pick[4]&&!pick[3])continue;   // (no time of its own: a row that gave its percent up for a time column would show neither)
          const got=build(...pick);
          if(got.empty)return null;
          if(got.width>inner)continue;
          return got.row;
        }
      }
      return null;
    }
    // 11-17 (route row 5; 11-18: CTX and NOW under the first row): a worker hangs under its owner by a `↳`, with no box: its who (its
    // own mark, role, harness, model, effort), under it its CTX row (the gauge and number, then ` · 12m` when the worker has a time
    // of its own) and under that its NOW on up to two rows (none: no row). Its stage name and open tool are not drawn (the owner's
    // stage line has the stage). A frame leg (the route has no owner yet) hangs straight under the route the same way. Short of
    // cells the who loses its effort, then its harness; short of rows (rule 48 (c)) the worker takes the owner's one-row form.
    // In a box (`boxed`, below) the mark leads the row and the CTX and NOW rows stand two cells in.
    function workerBlock(w,base,boxed=false){
      const p=w.paint||{},role=workerRole(w),glyph=routeGlyph({paint:p,kind:w.kind},p.harness),E=w.elapsed?[text(w.elapsed)]:[];
      const gap=boxed?2:base+4;
      const bare=boxed?[glyph,text(' ')]:[text(' '.repeat(base)),text('↳ ','dim'),glyph,text(' ')];
      const lead=withBare([...bare,...roleCell(role)],bare);
      // A model name longer than the line itself is cut with `…` rather than left out.
      let who=whoOf(lead,p,role)||cut(lead,inner);
      if(!unitLabel&&size(who)<=size(lead)&&(p.model_text||p.harness))who=cut([...lead,p.model_text||p.harness],inner);
      // No CTX value, so no CTX row to carry its time: the time ends its first row, at the right edge (the effort, then the harness, give
      // way to it; where even the model does not leave it room the row keeps its values and has no time).
      const stamp=!p.ctx&&w.elapsed?clean(w.elapsed):'';
      if(stamp){
        const room=inner-1-measure(stamp),tight=whoOf(lead,p,role,room);
        if(tight&&size(tight)<=room&&size(tight)>size(lead))who=[...tight,pad(inner-size(tight)-measure(stamp)),text(stamp,'dim')];
      }
      return sessionOf({who,one:foldRow(lead,p,role,E)||cut(lead,inner),p,kind:w.kind,gap,time:w.elapsed,called:calledRows(w.called,gap)});
    }
    // Spec rule 24 (11-12), 36 (b), 11-17: the stage line under the owner: `staged 3/6: execute`, the current stage's name
    // the one thing that blinks (a failed one keeps `✕`, a done one `✓`); with no stage running, the next stage and what it
    // waits for. A route with no stage list (one-shot, direct, frame) has none: its first line says what it is. Short of
    // cells the values go from the back, whole: `owner <model>`, then the word `staged` (`3/6: execute`).
    // 11-18 (review C2): the stage a worker runs is its own (`w.stage`, a stage's name); the route's current stage is the one for a
    // worker that names none the route knows. Parallel stages each have their workers' box, in the stages' order.
    const stagesOf=r=>Array.isArray(r.stages)?r.stages:[];
    const currentAt=r=>{const stages=stagesOf(r),current=stages.findIndex(stage=>stage.current||stage.mark==='✕'),next=stages.findIndex(stage=>stage.mark!=='✓');
      return current>=0?current:next>=0?next:stages.length-1;};
    const stageGroups=(r,list)=>{
      const stages=stagesOf(r),groups=new Map();
      list.forEach((w,i)=>{const named=stages.findIndex(stage=>stage&&w.stage&&stage.name===w.stage),at=named>=0?named:currentAt(r);
        if(!groups.has(at))groups.set(at,[]);groups.get(at).push(i);});
      return [...groups.entries()].sort((a,b)=>a[0]-b[0]).map(([at,items])=>({at,items}));
    };
    function piecesOf(r){
      const current=stagesOf(r).findIndex(stage=>stage.current||stage.mark==='✕'),pieces=[];
      if(current>=0){if(r.doer_worker&&r.owner_model&&!treeOf(r))pieces.push([text('owner '+r.owner_model,'dim')]);}
      else if(r.visibility?.next)pieces.push([text(r.visibility.next,'dim')]);
      return pieces;
    }
    function placeRow(r,workers=0,limit=inner-4,given=null){
      const shape=String(r.shape||''),stages=stagesOf(r);
      if(!stageLined(r))return null;
      const at=given!==null&&stages[given]?given:currentAt(r),position=(at+1)+'/'+stages.length;
      const stage=stageCell(stages[at],stageInk(stages[at].key)),pieces=piecesOf(r);
      // Spec rule 43 (g): the workers gone for rows, the stage line ends with their count (it stays while the values before it go).
      for(const end of workers?[[text(' · ','dim'),text('worker '+workers,'dim')],[]]:[[]])for(const word of [...new Set([(stageWord(shape)?stageWord(shape)+' ':'')+position,position])])for(let n=pieces.length;n>=0;n--){
        const row=[text(word,'dim'),text(': ','dim'),...stage,...pieces.slice(0,n).flatMap(piece=>[text(' · ','dim'),...piece]),...end];
        if(size(row)<=limit)return row;
      }
      return cut([text(position,'dim'),text(': ','dim'),...stage],limit);
    }
    // The first line: a state mark, then the chain of kinds the route continues (`frame ✓ → code(standard)`: what is behind
    // dim with `✓`, this one bold, what is left dim; a frame route reads `frame → ??` until its direction is chosen).
    function chainParts(r,{past=true,inten=true}={}){
      const kind=clean(r.kind_word||'').trim(),word=inten?intensityOf(r):'';
      const own=name=>{const field=kindField(r,name);return [kindText(name,field),...(word?[bracket(word,field)]:[])];};
      const legs=(Array.isArray(r.chain)?r.chain:[]).filter(l=>l&&l.name);
      if(!legs.length)return kind?own(kind):[];
      const out=[];
      legs.forEach((l,i)=>{
        if(i<legs.length-1&&!past)return;
        if(out.length)out.push(text(' → ','dim'));
        out.push(...(i<legs.length-1?[text(clean(l.name)+(l.state==='done'?' ✓':''),'dim')]:own(clean(l.name))));
      });
      if(framed(r))out.push(text(' → ??','dim'));
      return out;
    }
    // A frame route whose legs are not running waits for the person's direction (its mark `◑`, not blinking).
    const directionWait=r=>framed(r)&&!live(r)&&['idle','blocked'].includes(r.kind);
    // 11-18 (the person, on the first line: "route 첫 줄 오른쪽 끝에 둔다"): the route's elapsed time stands at the right end of this
    // line, whatever the width, and the kind word with it. Short of cells the middle text (`· waiting for direction`) goes first
    // (cut with `…`, then whole), then the earlier legs of the chain, then the intensity. A route with no time leaves the end empty.
    // 11-18 (review C3; the person's picture `● code(standard) · staged 3/6          1h52m`): a route with a stage line carries its stage
    // there, whether its workers stand in a box or none run. Where no box holds the stage text the first line takes the stage's name
    // too (`· staged 3/6: execute`; `name` below). Short of cells this middle text goes first (what the stage waits for, then the name,
    // then the whole), as the waiting-for-direction text does.
    function stageForms(r,name){
      if(!stageLined(r))return [];
      const stages=stagesOf(r),at=currentAt(r),word=(stageWord(String(r.shape||''))?stageWord(String(r.shape||''))+' ':'')+(at+1)+'/'+stages.length;
      const forms=[];
      if(name){const stage=stageCell(stages[at],stageInk(stages[at].key)),pieces=piecesOf(r);
        for(let n=pieces.length;n>=0;n--)forms.push({named:true,parts:[text(word,'dim'),text(': ','dim'),...stage,...pieces.slice(0,n).flatMap(piece=>[text(' · ','dim'),...piece])]});}
      forms.push({named:false,parts:[text(word,'dim')]});
      return forms.map(form=>({named:form.named,parts:[text(' · ','dim'),...form.parts]}));
    }
    function headRow(r){
      const chains=[{},{past:false},{past:false,inten:false}].map(o=>chainParts(r,o));
      if(!chains[0].length)return null;
      const wait=directionWait(r)?[text(' · ','dim'),text(t('direction_wait','waiting for direction'),'dim')]:[];
      const width=inner,room=width-2;     // the box's inner width, read now (the row is drawn after the box's width is given back)
      const time=clean(r.elapsed||'').trim(),end=time?measure(time)+1:0;
      // `blink`: the first line's dot pulses; `stage.blinks`: the stage's name blinks where it stands on this line, and the dot then stands still.
      return (blink,stage=null)=>{
        const finish=(body,named=false)=>{
          const dot=blink&&!(named&&stage&&stage.blinks);
          const mark=wait.length?{...text('◑','g_blocked'),role:'state:blocked'}:live(r)?zonePaint(r,dot?pulse('●'):{...text('●','g_work'),role:'state:working'}):routeGlyph(r);
          const row=[mark,text(' '),...body],out=time?[...row,pad(Math.max(1,width-size(row)-measure(time))),text(time,'dim')]:row;
          out.named=named;       // (whether the stage's name stands on this line: the caller draws the stage line apart when it does not)
          return out;
        };
        for(const chain of chains){
          const left=room-end-size(chain);
          if(left<0)continue;
          if(wait.length&&size(wait)<=left)return finish([...chain,...wait]);
          if(wait.length&&left>=5)return finish([...chain,...cut(wait,left)]);
          const form=stage?stageForms(r,stage.name).find(f=>size(f.parts)<=left):null;
          if(form)return finish([...chain,...form.parts],form.named);
          return finish(chain);
        }
        return finish(cut(chains.at(-1),Math.max(1,room-end)));
      };
    }
    // 11-18 (the person: "worker 부를떄 stage ~ 부분을 블록의 상단에 그려서 그냥 worker는 박스에 넣어도 되겠는데?" / "박스 여부는 내가 골라가면서 볼 수
    // 있게 그냥 옵션에 좀 넣어줘"): the setting `worker box` (`closed`, the default; `open`; `none`). A route that has workers and a stage
    // line puts its workers of that stage in one box under its owner, the stage text (`staged 3/6: execute`) on the box's top line, and
    // the stage row apart is not drawn. `closed` has all four borders, `open` only the top line and the left side, `none` the stage row
    // and the `↳` indent as before. The lines are dim; the right side of a closed box stands in one column whatever the widths of the
    // text (the row is padded in cells). Where the box's text would be under BOX_TEXT_MIN cells wide the closed box gives way to the open
    // one, and short of rows (below) the closed box's bottom line goes first. A route with no workers draws no box (its stage text stays
    // on its stage row); a frame route's legs hang under it as before.
    const BOX_TEXT_MIN=24;
    const boxSetting=OPTIONS.worker_box.includes(cfg.worker_box)?cfg.worker_box:'closed';
    const narrower=(by,fn)=>{inner-=by;try{return fn();}finally{inner+=by;}};
    function boxedWorkers(r,count){
      if(!side||!count||boxSetting==='none'||!treeOf(r))return null;
      const line=value=>({...text(value,'dim'),role:'messages'}),W=inner,list=workersOf(r),groups=stageGroups(r,list);
      const make=closed=>{
        // One box for each stage that has workers, its top line that stage's own position and name (`staged 2/4: eval-asr`).
        const by=closed?6:4,boxes=groups.map(g=>({blocks:narrower(by,()=>g.items.map(i=>workerBlock(list[i],0,true))),title:placeRow(r,0,Math.max(1,W-6),g.at)}));
        if(!boxes.length||boxes.some(box=>!box.title))return null;
        return {lines:()=>boxes.flatMap(({blocks,title})=>{
          const rows=[[text('  '),line('┌ '),...title,text(' '),line('─'.repeat(Math.max(0,W-4-size(title)-1-(closed?1:0)))+(closed?'┐':''))]];
          for(const parts of blocks.flatMap(b=>sessionRowsOf(b).filter(Boolean))){
            const fit=clip(parts,W-by);
            rows.push(closed?[text('  '),line('│ '),...fit.parts,pad(W-by-fit.used),line(' │')]:[text('  '),line('│ '),...fit.parts]);
          }
          if(closed)rows.push([text('  '),line('└'+'─'.repeat(Math.max(0,W-4))+'┘')]);
          return rows;
        })};
      };
      const wide=boxSetting==='closed'&&W-6>=BOX_TEXT_MIN?make(true):null,open=make(false);
      return open?{closed:wide,open}:null;
    }
    const routeRows=[];
    within(()=>{
      for(const r of listed){
        const key='popup:ROUTE:'+r.id,title=titleOf(r),glyph=[routeGlyph(r),text(' ')];
        // Spec rule 36 (c): the title takes at most two rows, the second ending in `…`.
        let names=side?wrapAll(title,inner-2):[];
        if(names.length>2)names=[names[0],ellipsis(names[1],inner-2)];
        setRoles(r);
        const tree=treeOf(r),open=framed(r)&&tree,base=open?2:4,workerList=tree?workersOf(r):[],workers=workerList.map(w=>workerBlock(w,base));
        // The `none` form: each stage's own stage row, its workers under it.
        const groups=side&&!open&&placeRow(r)?stageGroups(r,workerList).map(g=>({items:g.items,place:placeRow(r,0,inner-4,g.at)})):[];
        const supervising=workers.length>0;
        const place=side?placeRow(r):null,stages=Array.isArray(r.stages)?r.stages:[];
        const boxed=!open&&place?boxedWorkers(r,workers.length):null;
        const running=stages.find(stage=>stage.current||stage.mark==='✕');
        routeRows.push({zone:r.zone==='direct'?'direct':'dispatch',parts:folded(r),two:side,bnow:side?null:nowRow(leadPaint(r),r.kind,2),first:side?headRow(r):null,
          // The owner's blocks (11-18); its time is the route's own, which stands on the first line, so it has none here.
          session:side&&!open?sessionOf({who:whoRow(r,supervising),one:leadRow(r,supervising),p:leadPaint(r),kind:r.kind,gap:4,time:'',lead:ownerLead(r,supervising),called:calledRows(r.called,4)}):null,
          // A route with no session values and no kind word (no first line to end in its time) keeps its time alone on a row, at the right edge.
          time:side&&!open&&r.elapsed&&!whoRow(r,supervising)&&!headRow(r)?[pad(inner-measure(r.elapsed)),text(r.elapsed)]:null,
          names,name:[cut(title,inner-2)],glyph,place,action:key,kind:r.kind,
          // The stage name blinks while the stage line stands and a stage runs; else the first line's dot does.
          stageBlinks:!!place&&live(r)&&running&&running.mark==='●',
          calledRows:side?null:(calledBottom.get(r)||[]).map(c=>folded(c)),calledCount:flatCalled(r).length,
          ...(tree?{tree,workers,groups,boxed,counted:workers.length?placeRow(r,workers.length):null}:{})});
      }
      for(const q of requestList){
        // A request row opens the MESSAGES popup, where the requests read whole (as their MESSAGES rows did).
        // 11-16: beside the native area its rows read as a route's (`requestForms`, the one drawing of them).
        const key='popup:MESSAGES',f=requestForms(q,{text,measure,width:inner,arrow:'zone_request'});
        routeRows.push({zone:'request',parts:f.one,two:side,head:f.head,titleRow:f.title,statusRow:f.status,titled:f.titled,
          action:key,kind:q.kind||'request-out',request:true});
      }
    });
    // 11-17 (route zones): the rows stand in their zones' order, `direct`, `dispatch`, `request`, each zone's own order kept.
    const ZONES=['direct','dispatch','request'],zoneIndex=x=>ZONES.indexOf(x.zone);
    routeRows.sort((a,b)=>zoneIndex(a)-zoneIndex(b));
    // No rows yet: the producer's own label stands alone; a known empty list draws no box.
    // Spec rule 45 (a): beside the native area the count leads its upper-case word (`2 ROUTE`),
    // the producer's label too (`route 3 ⠏2` → `3 ROUTE ⠏2`); the bottom layout keeps `route N`.
    const upper=value=>side?value.replace(/^route (\d+)/,'$1 ROUTE').replace(/^route\b/,'ROUTE'):value;
    const label=upper(clean(model.route_label||'').trim());
    // An open route count pulses on the shared beat while one of them runs (Fleet's g_work/g_work_off).
    const running=shown.some(live);
    // 11-17 (route zones): the box's title is the route count alone (the requests are a zone of the box, not a tab);
    // a box with requests and no route reads `ROUTE`.
    const routeHead=shown.length?[running?pulse(upper('route '+total)):text(upper('route '+total),'g_idle')]:label?[text(label,'g_idle')]:requestList.length?[text(upper('route'),'g_idle')]:[];
    const routeTitle=routeHead.length?routeHead:null,routeTitleAlt=[];
    const routeChecking=!routeRows.length&&label?[{parts:[text(t('checking','Checking'))],action:'popup:ROUTE'}]:[];
    // The route box's own steps short of rows (below): its title's second rows (`keepMulti`), the
    // context rows (and the workers' box) fold into the first rows, the workers go, the kind rows go,
    // every route takes one row, then the box shows fewer routes and scrolls in place.
    let routeLimit=routeRows.length,keepPlace=true;
    // Short of rows (below), in this order: the box's closing border, the NOW's second row, the CTX row (its number goes into the first row), the NOW's first row.
    let keepBoxEnd=true,nowTwo=true,keepCtxLine=true,keepNow=true;
    // Spec rule 48 (c): the workers stay (in their box while the owners keep their context row,
    // else one row each, as their owners) or go (the kind row counts them).
    let keepWorkers=true;
    const anyWorkers=side&&routeRows.some(x=>x.tree&&x.workers.length);
    const anyCalled=routeRows.some(x=>x.calledCount>0);
    const twoFull=side&&routeRows.some(x=>x.two);
    let twoRows=twoFull;
    // Spec rule 38: who / context and time / what (two rows, then one) / which kind.
    const routeForm=x=>{
      // Under the native area (11-18) the route's NOW stands on the row under its one row (the first to go short of rows).
      if(!(twoRows&&x.two))return [x.parts,...(keepNow&&x.bnow?[x.bnow]:[]),...(keepCalled&&x.calledRows?x.calledRows:[])];
      // A request: who and age / title / status; the kind rows gone, the title and the status share a row.
      if(x.request)return [x.head,...(keepPlace&&x.statusRow?[x.titleRow,x.statusRow]:[x.titled])];
      return routeRowsOf(x);
    };
    // 11-17 (route row 5): the kind and its intensity / the title / the owner (its `CTX` beside its model) / its NOW / the
    // stage line / the workers under it by `↳`, each with its own NOW under it. Short of rows (rule 48 (c)) the NOW rows go
    // and the owner's and the workers' first rows take the one-row form, the stage line goes (counting the workers), then
    // the workers go, then the route takes one row.
    function routeRowsOf(x){
      const workers=x.tree&&keepWorkers?x.workers:[],names=keepMulti?x.names:x.name,out=[],indent=text('  ');
      const place=!keepWorkers&&x.counted?x.counted:x.place,showPlace=keepPlace&&place;
      // 11-18: the stage text stands on the top of the workers' box, so the stage row is not drawn apart.
      const boxed=keepWorkers&&keepPlace&&x.boxed&&x.workers.length?(keepBoxEnd&&x.boxed.closed?x.boxed.closed:x.boxed.open):null;
      // 11-18 (review C3): the stage text stands on the first line unless a stage row stands apart (the `none` form, or the workers'
      // count form); beside a box it is the position alone (the box's top line has the name), with no workers the name too.
      // (A route with no first line, no kind word, has no place for it: its stage row stands as it did.)
      // 11-18 (round 5): a route with no workers (so no box) whose stage name did not fit on its first line gets the stage row back under the
      // owner (`staged 3/6: execute`), the one the box's top line carries where workers stand; with the name on the first line there is none.
      const some=!!(x.tree&&x.workers.length);
      let apart=showPlace&&!boxed&&(some||!x.first);
      if(x.first){
        const headStage=x.place&&!apart?{name:!some,blinks:x.stageBlinks}:null;
        let head=x.first(!((boxed||apart)&&x.stageBlinks),headStage);
        if(headStage&&headStage.name&&showPlace&&!head.named){apart=true;head=x.first(!x.stageBlinks,headStage);}
        out.push(head);out.push(...names.map(row=>[indent,...row]));
      }
      else out.push([...x.glyph,...names[0]],...names.slice(1).map(row=>[indent,...row]));
      // A route whose owner is not known keeps its time and `CTX` row under the title all the same.
      const owner=x.session?sessionRowsOf(x.session).filter(Boolean):[];
      out.push(...owner);
      if(keepNow&&x.time)out.push(x.time);
      const standing=[text(owner.length?'    ':'  ')];
      if(boxed)out.push(...boxed.lines());
      else if(apart&&some&&keepWorkers&&x.groups.length>1)for(const g of x.groups){
        out.push([...standing,...g.place]);
        for(const i of g.items)out.push(...sessionRowsOf(workers[i]).filter(Boolean));
      }else{
        if(apart)out.push([...standing,...place]);
        for(const w of workers)out.push(...sessionRowsOf(w).filter(Boolean));
      }
      return out;
    }
    const routeSize=x=>routeForm(x).length;
    // Rows short, the box shows a window of the routes in the order below and scrolls in place. It takes
    // the rows of its tallest window, so wheeling it moves nothing else (11-15: v4.36.4 measured the
    // first routes in their own order, and a taller window took a head row's place).
    const rank={working:0,'request-in':0,blocked:1,'request-rejected':1,idle:2,'request-out':2,dead:3,done:4,'request-done':4,stale:4};
    // (Short of rows the window keeps the zones in their order, and within a zone the most important first.)
    const routeOrder=()=>routeLimit<routeRows.length?routeRows.map((x,i)=>[x,i]).sort((a,b)=>(zoneIndex(a[0])-zoneIndex(b[0]))||((rank[a[0].kind]??2)-(rank[b[0].kind]??2))||a[1]-b[1]).map(([x])=>x):routeRows;
    // (A window's rows count the named rule of each zone it shows a row of: `routeRule` below.)
    const routeWindows=()=>{const list=routeOrder(),n=Math.max(1,list.length-routeLimit+1);
      return Math.max(...Array.from({length:n},(_,at)=>{const view=list.slice(at,at+routeLimit);
        return view.reduce((a,x)=>a+routeSize(x),0)+(routeRule()?new Set(view.map(x=>x.zone)).size:0);}));};
    // 11-17 (route zones, the person: "나로 하고 direct / 그 아래는 dispatch 라고 쓰고, 다시 그 아래에 request"): a named rule
    // (`─ direct ───`) opens each zone that has rows, in the zone's own color; a zone with no row has no rule. Short of rows the
    // content rows give way first (the steps below); a zone's rule stays as long as one row of that zone shows, in a box
    // scrolled to fewer routes too. Only when even one route with its rule does not fit, and the box would go whole, the
    // rules go (the last step before the box itself: a route without its name rather than no route).
    let keepRouteRule=true;
    const routeRule=()=>keepRouteRule&&routeRows.length>0;
    const routeBox=()=>kept.ROUTE?2+(routeRows.length?routeWindows():routeChecking.length):0;
    const zoneRow=(zone,key)=>{
      const room=Math.max(0,inner-box),head=[{...text('─ ','dim'),role:'messages'},{...text(zone,'zone_'+zone),role:'zone'},text(' ')];
      return boxRow({parts:[...head,{...text('─'.repeat(Math.max(0,room-size(head))),'dim'),role:'messages'}]},key);
    };
    const routeBlockOf=()=>{
      const key=tone.route;
      // Spec rule 23: rows short, the box shows a window of the routes and scrolls
      // in place (the wheel over it). A working route comes first, then an answer
      // wait, a stopped one, a finished one; the borders count what is hidden.
      // A request waiting for this session's answer and a rejected or cancelled one stand with
      // the working and stopped routes; a sent one with the idle; a closed one with the finished
      // (`rank` above).
      const scrolling=routeLimit<routeRows.length,sorted=routeOrder();
      const routeOffset=scrolling?Math.max(0,Math.min(routeRows.length-routeLimit,Math.trunc(Number(routeScroll)||0))):0;
      const routeStep=routeLimit,routeMax=scrolling?routeRows.length-routeLimit:0;
      const inView=sorted.slice(routeOffset,routeOffset+routeLimit);
      const above=routeOffset,below=routeRows.length-routeOffset-inView.length+unsent;
      const hiddenNote=n=>[text(n,'loc_repo')];
      const block=[{...boxTop(routeTitle,key,{action:'popup:ROUTE',note:above?hiddenNote('↑'+above):[],alt:routeTitleAlt}),groups:['route']}];
      let before=null;
      (routeRows.length?inView:routeChecking).forEach(item=>{
        if(routeRule()&&item.zone&&(!before||before.zone!==item.zone))block.push(zoneRow(item.zone,key));
        before=item;
        // A press on any row of a route opens the route popup with it unfolded.
        for(const parts of item.two||item.bnow||item.calledRows&&item.calledRows.length?routeForm(item):[item.parts])block.push(boxRow({parts,action:item.action},key));
      });
      block.push(boxBottom(key,below?hiddenNote('↓'+below):[]));
      return {block,area:{offset:routeOffset,max:routeMax,step:routeStep}};
    };
    // MESSAGES (spec rules 31, 45 (b), 58; 11-16, restored from v4.36.4, the person: "메시지 형식은 퇴행했네?
    // ... 원래 형식으로 돌려. 3줄로 줄이라는게 아니라 원래는 3'건'으로 줄이자는 말이야"): a message takes two rows,
    // its head (the direction arrow in its color, the other session's number, then dim harness, time, kind
    // and a sent one's delivery state) and, two cells in, its first meaningful text in the body color on one
    // row (`…` where it goes on). MESSAGES_MAX show at most however many rows are left; `+N` on the section
    // head counts the rest. Short of rows (below) every message folds to the one-row form (arrow, number,
    // text), then fewer show from the oldest. The requests this session sent or received stand in the route
    // box (above), once; the popup reads both whole.
    const messages=painted(model.messages);
    let messageOne=false;
    const arrowOf=m=>text(m.way,m.way==='←'?'lvl_g':'loc_repo');
    const bodyOf=m=>String(m.body||m.text||'');
    const messageLine=m=>cut([arrowOf(m),text(' '),text(String(m.who||''),'loc_repo'),text(' '),text(bodyOf(m),'loc_repo')],inner);
    const messageTop=m=>{
      const arrow=arrowOf(m),who=text(String(m.who||''),'loc_repo');
      const delivery=Array.isArray(m.delivery)&&m.delivery[0]?[text(' · ','dim'),text(String(m.delivery[0]),m.delivery[1]||'dim')]:[];
      const pieces=[m.harness?[text(' '+m.harness,'dim')]:[],m.when?[text(' · '+m.when,'dim')]:[],m.kind?[text(' · '+m.kind,'dim')]:[]];
      // Short of cells kind, harness and time go first; the delivery state stays.
      for(const drop of [[],[2],[2,0],[2,0,1]]){
        const row=[arrow,text(' '),who,...pieces.flatMap((x,i)=>drop.includes(i)?[]:x),...delivery];
        if(size(row)<=inner)return row;
      }
      // 11-17: `awaiting delivery · why` (a sent message the other window has not taken in): the first word stays whole, its
      // why goes on as far as the row reaches and ends in `…`.
      const word=Array.isArray(m.delivery)&&typeof m.delivery[2]==='string'&&m.delivery[2]&&String(m.delivery[0]).startsWith(m.delivery[2])?m.delivery[2]:'';
      if(word){
        const lead=[arrow,text(' '),who,text(' · ','dim')],room=inner-size(lead)-measure(word);
        if(room>=0)return [...lead,text(word,m.delivery[1]||'dim'),...cut([text(String(m.delivery[0]).slice(word.length),m.delivery[1]||'dim')],room)];
        return [...cut([arrow,text(' '),who],Math.max(1,inner-3-measure(word))),text(' · ','dim'),text(word,m.delivery[1]||'dim')];
      }
      return cut([arrow,text(' '),who,...delivery],inner);
    };
    const messageBody=m=>{
      const body=wrapAll([text(bodyOf(m),'loc_repo')],inner-2),first=body[0]||[];
      return [text('  '),...(body.length>1?cut([...first,text(' '),...body[1].slice(0,1)],inner-2):first)];
    };
    const messageTotal=messages.length;
    const messagesHead=n=>{const hidden=messageTotal-Math.min(n,messageTotal);return sectionHead([],side?'MESSAGES':'messages',hidden>0?[text('+'+hidden,'loc_repo')]:[]);};
    // The newest messages (in their order, the newest last).
    const messageRows=n=>n>0?messages.slice(-n).flatMap(m=>messageOne?[messageLine(m)]:[messageTop(m),messageBody(m)]):[];
    // MEM (spec rule 46; 11-16, v4.36.4's two rows restored, the person: "메모리도 메시지랑 같은 맥락이야"): the
    // newest saved memory records of this repository and the global ones together, newest first,
    // MEM_MAX at most, each a dim head row and, two cells in, its headline in the body color on up to
    // two rows (the second ending in `…` where it goes on). The head keeps what the person asked for on
    // 11-15 ("전역인지 여기 레포인지, 영구적인지, 일시적인이"): `◆` durable or `◇` working (`·` for a record that
    // names no tier), its scope and its kind as the memory system names them, and its age
    // (`◆ project · decision · 10m`); short of cells the kind leaves, then the scope (the mark and the
    // age stay). Short of rows (below) every headline takes one row, then fewer records show. No popup:
    // its head has no `›`.
    let memBody=2;
    const memTop=m=>{
      const mark=m.tier==='durable'?'◆':m.tier==='working'?'◇':'·';
      for(const words of [[m.scope,m.kind],[m.scope],[]]){
        const row=[text([mark,...words.filter(Boolean),m.age].filter(Boolean).join(' · ').replace(/^(.) · /,'$1 '),'dim')];
        if(size(row)<=inner)return row;
      }
      return cut([text([mark,m.age].filter(Boolean).join(' '),'dim')],inner);
    };
    const memRows=m=>{
      let body=wrapAll([text(m.text,'loc_repo')],inner-2);
      if(body.length>memBody)body=[...body.slice(0,memBody-1),ellipsis(body[memBody-1],inner-2)];
      return [memTop(m),...body.map(row=>[text('  '),...row])];
    };
    // The sections in order, each with the most rows it takes and its rows at a given count.
    const SECTIONS=[
      {name:'QUESTIONS',on:asked,max:3,head:()=>sectionHead(questionCount?[{...text(questionCount,'g_blocked'),role:'accent'}]:[],summary?.label||'QUESTIONS'),
        rows:()=>questionRows(),action:'question',groups:model.wait?['wait']:[]},
      {name:'ROUTE',on:!!routeTitle,box:true},
      {name:'MESSAGES',on:messageTotal>0,max:MESSAGES_MAX,head:()=>messagesHead(take.MESSAGES),rows:n=>messageRows(n),action:'popup:MESSAGES'},
      {name:'MEM',on:memItems.length>0,max:MEM_MAX,head:()=>sectionHead([],'MEM',[],[[]],false),rows:n=>memItems.slice(0,n).flatMap(memRows)}];
    const take={},kept={};
    for(const s of SECTIONS){take[s.name]=s.max;kept[s.name]=s.on;}
    // 11-16 (the person: "질문은 이전처럼 그냥 붉은색 블록으로 감싸는게 모양은 제일 나아" / "붉은 바운더리 말이야"): the QUESTIONS
    // section is one thin rounded box again (v4.33.1's, as the route box's line), its name `N QUESTIONS ›` in
    // the top border; the line is the rose where this window has a question to answer, dim otherwise. The rows
    // inside are the section's own, laid out at the box's inner width. Short of rows the two border rows go
    // after the content rows (`keepQuestionBox`), the section standing as it did before.
    // (A section with a wait count and no row to show stays the one head row: an empty box is no box.)
    const boxedQuestions=()=>keepQuestionBox&&kept.QUESTIONS&&((keepSent&&sentSource.length>0)||!!summary||(keepGuide&&!!guide));
    const questionRowsIn=()=>boxedQuestions()?within(questionRows):questionRows();
    const sectionRows=s=>s.name==='QUESTIONS'?questionRowsIn():s.rows(take[s.name]);
    const sectionSize=s=>!kept[s.name]?0:s.box?routeBox():(s.name==='QUESTIONS'&&boxedQuestions()?2:1)+sectionRows(s).length;
    const core=top.length-spacers.size-metaGaps.size-rules.size-optional.size-gitRows.size-extra.size;
    let keepGit=true,keepSpacers=true,keepMetaGap=true,keepRules=true,keepOptional=true,keepMulti=true,keepQuota=quota.length>0,keepFootRule=true,keepQuestionBox=true;
    // 11-16 (the person: "그 라우트는 다른 하네스도 쓰니까 usage에 3가지를 전부 띄우는게 좋겠어" / "구분줄을 USAGE로 해서 넣자"): the
    // panel's bottom is the USAGE section: its name row (the thin rule above the last row, now named), then
    // one row for each harness (Claude, Codex, OpenCode, Fleet's header order): the harness's name dim, then the
    // windows it reports as the last row showed them. The last of them shares the last row with the gear and
    // the fold button when it fits there, else stands above a row of the buttons alone. Short of rows the
    // other two harnesses' rows go (the last first), then the name row; this window's own harness stays
    // longest (spec rule 59). A backend that sends no `usage` keeps the one row of 5h/7d it had.
    const usageData=Array.isArray(model.usage)?model.usage.filter(u=>u&&typeof u==='object'&&['claude','codex','opencode'].includes(u.harness)):[];
    const usageOn=usageData.length>0;
    if(usageOn)keepQuota=true;
    const ownUsage=usageData.findIndex(u=>u.harness===harness);
    const keepLine=usageData.map(()=>true);
    let keepUsageHead=true;
    const quotaRoom=inner-BUTTONS-1;
    const usageName=u=>clean(u.harness);
    const usageWide=Math.max(0,...usageData.map(u=>measure(usageName(u))));
    // One harness's row within `room` cells: the windows from the first, each whole, going in the order the last row
    // gave them up (the reset times, 5h, the model windows from the last, 7d); a harness with none reported reads
    // `?`. `values` is false where only the name was left.
    const usageFit=(u,room)=>{
      const parts=painted(u.parts).filter(p=>p&&typeof p==='object'),groups=[];let current=[],sep=null;
      for(const part of parts){if(String(part.text)===' │ '){sep=sep||part;if(current.length)groups.push(current);current=[];}else current.push(part);}
      if(current.length)groups.push(current);
      const name=usageName(u),mark=text(name,'dim'),lead=pad=>[{...mark,role:'harness-name',harness:u.harness},text(' '.repeat(Math.max(1,pad)))];
      const tries=[];
      if(!groups.length)tries.push([text('?','dim')]);
      else{
        const kind=g=>{const label=g.map(part=>String(part.text)).join('').trim().split(/\s+/)[0];return label==='7d'?'week':/^\d+[mhd]$/.test(label)?'window':'model';};
        const join=gs=>gs.flatMap((g,i)=>i&&sep?[sep,...g]:g);
        let left=groups.map(g=>g.filter(part=>!/^\(↻/.test(String(part.text))));
        tries.push(join(groups),join(left));
        left=left.filter(g=>kind(g)!=='window');tries.push(join(left));
        while(left.some(g=>kind(g)==='model')){const at=left.map(kind).lastIndexOf('model');left=left.filter((_,i)=>i!==at);tries.push(join(left));}
      }
      for(const body of tries)for(const pad of inner>=30?[usageWide+1-measure(name),1]:[1]){
        if(groups.length&&!body.length)continue;   // (nothing of its windows left is the name alone, below)
        const row=[...lead(pad),...body];
        if(size(row)<=room)return {row,values:true};
      }
      return {row:cut([{...mark,role:'harness-name',harness:u.harness}],room),values:false};
    };
    const usageLines=()=>keepQuota&&usageOn?usageData.filter((_u,i)=>keepLine[i]):[];
    // 11-18 (the person: "패널 제일 하단에 RESOURCES도 띄우자. USAGE 위에 그리고 어떤 세션이 잡고 있는지만 추가로 표시하고"; then, on v4.54.0,
    // "RESOURCE는 세로로 차지하는 공간이 너무 많은데, 이걸 게이지는 없애자 그리고 가로로 나열이 가능하면 가로로 나열을 좀 하자"): RESOURCES stands
    // right above USAGE. One row for each host: its name and `CPU 34%  MEM 61%`. Under it every GPU of the host in its number's order, side by
    // side (`GPU0  93% 21/24G [3b]  GPU1  40% 10/48G`, `GPU2   0%  0/48G` under them: the grid below), onto the next row where they do not fit; one GPU's group is never split
    // between rows. The use is its number, in the level's color. 11-19 (the person: "빈 GPU도 일관성있게 표시해줘. 그냥 0% 0/48GB
    // 이런식으로 해놔. 컬러만 좀 어둡게 하면 되잖아. 0%에"): a GPU nothing runs on has the same group with the sample's own values and no session
    // number, all of it `dim` (the producer paints it so; the host row no longer counts them). The values are the sample the shared producer
    // already took. Short of cells on a host row the values go in this order: MEM, then CPU, then the name is cut with `…` (12 cells at least);
    // short of cells for one GPU's group the memory goes first, then the use, and the session's number stays (who holds the GPU is what the
    // section is for). Short of rows it gives way before everything else: the GPU rows (from the last), the host rows (from the last; the name
    // row goes with the last of them), so USAGE and the sections above it stand as they did without it.
    // 11-19 (the person, on v4.59.6's panel: "가로로 길어지면 RESOURCE에 게이지도 넣어주면 좋고"): where the width allows, a GPU's group has a
    // memory gauge (Fleet's VRAM gauge: `GPU0  93% ━━━━━━━━─ 21/24G [3b]`) right before its memory number. The rows come first (the person,
    // 11-18: the section took too many), so the gauge is only what the width has left over: it stands only when the rows are as many as
    // without it and it has 4 cells at least, in the widest width (up to 10) the section has room for, the same for every group. The
    // groups may then stand fewer to a row where that keeps the rows as many (a host of two GPUs on a width for three). The backend draws
    // the gauge at each width (`gauge4` … `gauge10`, the fill and the color are its); the panel only picks one. Not in the forms without
    // the memory, and not on the host rows.
    const resourceData=Array.isArray(model.resources)?model.resources.filter(h=>h&&typeof h==='object'&&Array.isArray(h.host)).slice(0,8):[];
    let resHosts=resourceData.length,resGpus=Infinity;
    // 11-19 (the person: "그리고 GPU 표시를 행과 열 간격을 맞춰."): one grid for the whole section, from every host's values. The host rows: the name
    // column as wide as the longest name, each stat (`CPU`, `MEM`) a column with its value right-aligned to the widest. The GPU groups: all the same
    // width, the label left-aligned, the use and the memory right-aligned, the session numbers left-aligned after them (blank where a GPU has none, so
    // the next group keeps its place; the last group of a row is not padded behind), two cells between groups; every row of every host takes as many
    // groups as the widest form lets in one row, so a group and its values stand on the same cells row over row. The form of the groups (the memory,
    // then the use goes) and the stats' columns (MEM, then CPU) are chosen for the section, not per host; a name that must be cut is the step after.
    const plainOf=parts=>parts.map(p=>String(p.text)).join('');
    const blank=n=>n>0?[text(' '.repeat(n))]:[];
    const leftIn=(parts,w)=>[...parts,...blank(w-size(parts))];
    const rightIn=(parts,w)=>[...blank(w-size(parts)),...parts];
    const trimEnd=parts=>{const out=[...parts];while(out.length&&/^ *$/.test(String(out.at(-1).text)))out.pop();return out;};
    const grid=(()=>{
      const hosts=resourceData.map(h=>({name:painted(h.host),stats:painted(h.stats).filter(x=>Array.isArray(x)&&x.length).map(painted)}));
      const nameW=Math.max(0,...hosts.map(h=>size(h.name)));
      const cols=[];
      for(const h of hosts)for(const st of h.stats){
        const key=plainOf([st[0]]).trim();let c=cols.find(x=>x.key===key);
        if(!c){c={key,labelW:0,valueW:0};cols.push(c);}
        c.labelW=Math.max(c.labelW,size([st[0]]));c.valueW=Math.max(c.valueW,size(st.slice(1)));
      }
      // The stats kept: the most whose aligned row fits; none left, the names alone (each cut to the row where it is longer).
      let kept=cols.length;
      while(kept>0&&nameW+cols.slice(0,kept).reduce((n,c)=>n+2+c.labelW+c.valueW,0)>inner)kept--;
      const cells=resourceData.flatMap(h=>painted(h.gpus)).map(g=>({label:painted(g.label),pct:painted(g.pct),mem:painted(g.mem),users:painted(g.users)}));
      const width=k=>Math.max(0,...cells.map(c=>size(c[k])));
      const w={label:width('label'),pct:width('pct'),mem:width('mem'),users:width('users')};
      // One form for every group (0 whole; 1 without the memory; 2 without the use too; 3 the label alone): the first that lets the widest stand alone.
      const parts=f=>[w.label&&'label',f<=1&&w.pct&&'pct',f===0&&w.mem&&'mem',f<=2&&w.users&&'users'].filter(Boolean);
      const full=f=>parts(f).reduce((n,k)=>n+(n?1:0)+w[k],0);
      let form=0;while(form<3&&2+full(form)>inner)form++;
      const perRow=Math.max(1,Math.floor(inner/(full(form)+2)));
      // The gauge: only in the whole form (the memory is there), and only if every GPU has its widths drawn. Of the groups-to-a-row `k` (at most the
      // section's now) that keep the rows as many, the one that gives the gauge the most; none with 4 cells, no gauge (the rows as they were).
      let gauge=0,across=perRow;
      const drawn=cells.length&&w.mem&&resourceData.every(h=>painted(h.gpus).every(g=>painted(g.gauge4).length));
      if(form===0&&drawn){
        const counts=resourceData.map(h=>painted(h.gpus).length),rowsAt=k=>counts.reduce((n,c)=>n+Math.ceil(c/k),0),rows0=rowsAt(perRow);
        for(let k=perRow;k>=1;k--){
          if(rowsAt(k)!==rows0)break;
          const room=Math.min(10,Math.floor(inner/k)-2-full(0)-1);
          if(room>=4&&room>gauge){gauge=room;across=k;}
        }
      }
      if(gauge){w.gauge=gauge;}
      const order=gauge?['label','pct','gauge','mem','users'].filter(k=>w[k]):parts(form);
      return {hosts,nameW,cols,kept,w,form,gauge,parts:order,perRow:across};
    })();
    const resourceHostRow=h=>{
      const at=resourceData.indexOf(h),{name,stats}=grid.hosts[at];
      const cells=grid.cols.slice(0,grid.kept).map(c=>{
        const st=stats.find(x=>plainOf([x[0]]).trim()===c.key);
        return st?[...leftIn([st[0]],c.labelW),...rightIn(st.slice(1),c.valueW)]:null;
      });
      let last=cells.length;while(last>0&&!cells[last-1])last--;
      if(!last)return cut(name,inner);   // (no stat to give, or none kept: the name alone, cut to the row)
      return [...leftIn(name,grid.nameW),...cells.slice(0,last).flatMap((x,i)=>[text('  '),...(x||blank(grid.cols[i].labelW+grid.cols[i].valueW))])];
    };
    // One GPU's group: the same columns for every GPU (see above); `end` leaves a group last on its row without its trailing blanks.
    const resourceGpuGroup=(g,end)=>{
      const c={label:painted(g.label),pct:painted(g.pct),mem:painted(g.mem),users:painted(g.users),gauge:grid.gauge?painted(g['gauge'+grid.gauge]):[]},{w}=grid;
      const out=[];
      for(const k of grid.parts){
        if(out.length)out.push(text(' '));
        out.push(...(k==='label'||k==='users'||k==='gauge'?leftIn(c[k],w[k]):rightIn(c[k],w[k])));
      }
      return end?trimEnd(out):out;
    };
    // The host's GPU rows: `perRow` groups each (the section's), two cells in, two cells between.
    const resourceGpuRows=h=>{
      const gpus=painted(h.gpus);
      const rows=[];
      for(let i=0;i<gpus.length;i+=grid.perRow){
        const row=gpus.slice(i,i+grid.perRow).flatMap((g,j,all)=>[text('  '),...resourceGpuGroup(g,j===all.length-1)]);
        rows.push(row);
      }
      return rows.map(r=>cut(r,inner));
    };
    const resourceLines=()=>{
      const out=[];let room=resGpus;
      resourceData.slice(0,resHosts).forEach(h=>{
        out.push(resourceHostRow(h));
        for(const line of resourceGpuRows(h)){if(room<=0)break;out.push(line);room--;}
      });
      return out;
    };
    const resourceHead=()=>sectionHead([],'RESOURCES',[],[[]],false);
    const resourceCount=()=>side&&resHosts>0?1+resourceLines().length:0;
    const resourceGpuTotal=()=>resourceData.reduce((n,h)=>n+resourceGpuRows(h).length,0);
    // The last row beside the buttons: that harness's row when its values fit there.
    const usageShared=u=>u&&usageFit(u,quotaRoom).values;
    const footBase=()=>{
      if(usageOn){const list=usageLines();return list.length?(keepUsageHead?1:0)+list.length-1+(usageShared(list.at(-1))?0:1):0;}
      return side&&keepFootRule&&(kept.MESSAGES||kept.MEM)?1:0;
    };
    const footRule=()=>footBase()+resourceCount();
    // 11-16 (the person: "MEM과 가장 하단을 구분하는 줄이 필요"): beside the native area a thin rule (a section
    // head's rule, its color) parts the bottom sections (MESSAGES, MEM) from the last row (5h/7d, the gear and
    // the fold button); short of rows it goes before any content row.
    const need=()=>core+(keepMulti?extra.size:0)+(keepGit?gitRows.size:0)+(keepSpacers?spacers.size:0)+(keepMetaGap?metaGaps.size:0)
      +(keepRules?rules.size:0)+(keepOptional?optional.size:0)+SECTIONS.reduce((n,s)=>n+sectionSize(s),0)+footRule()+1;
    const budget=rows;
    // Short of rows (spec rule 34 (b), 11-15): the empty rows, the rule above the last row (11-16) and a
    // value's further rows first (a route title's second row too); then MEM and the question step down
    // (MEM to two rows, the question's action row; MEM to one row, the question's second row and the guide
    // line); then the structural rows; then every message takes its one-row form (11-16, v4.36.4's places
    // and order: a body keeps its one row, then the one-row form, then fewer messages). Then the route
    // box's own steps (v4.36.4, spec rules 38, 48 (c)): every route's context row folds into its
    // first row and, in the same step, its workers take one row each; the workers go (the kind row counts
    // them); the kind rows go; every route takes one row; then fewer messages show, down to one; the box
    // shows fewer routes and scrolls in place, down to one. Then Git, then the route box (v4.36.4's place
    // for it: before the running jobs), then the running jobs; then MEM gives its row, then its name, then
    // MESSAGES the same; QUESTIONS keeps its head and first row to the last; 5h/7d last of all. A step that
    // freed more than it had to gives its rows back, the most important first.
    const gave=[];
    // `family`: steps given in a priority order and given back in it (review follow-up 1): once one of them cannot be given back the
    // ones less important than it stay given, so a taller panel never shows less of the more important one.
    const give=(can,apply,undo,family=null)=>{while(need()>budget&&can()){apply();gave.push({undo,family});}};
    const lowest=['MEM','MESSAGES'];
    // RESOURCES gives way first (it is the newest and the least needed of the bottom's sections): the GPU rows, then the host rows.
    resGpus=resourceGpuTotal();
    give(()=>side&&resHosts>0&&resGpus>0,()=>{resGpus--;},()=>{resGpus++;});
    give(()=>side&&resHosts>0,()=>{resHosts--;},()=>{resHosts++;});
    give(()=>keepSpacers,()=>{keepSpacers=false;},()=>{keepSpacers=true;});
    if(!usageOn)give(()=>!!footBase(),()=>{keepFootRule=false;},()=>{keepFootRule=true;});
    give(()=>keepMulti,()=>{keepMulti=false;},()=>{keepMulti=true;});
    for(const down of [2,1])for(const name of ['MEM','QUESTIONS']){
      // (MEM, 11-16: its headlines take one row before fewer records show.)
      if(name==='MEM'&&down===2)give(()=>kept.MEM&&memBody>1,()=>{memBody=1;},()=>{memBody=2;});
      if(name==='QUESTIONS'){if(down===2)give(()=>here&&keepAction,()=>{keepAction=false;},()=>{keepAction=true;});
        if(down===1){give(()=>!!summary&&briefTwo,()=>{briefTwo=false;},()=>{briefTwo=true;});give(()=>keepGuide&&!!guide,()=>{keepGuide=false;},()=>{keepGuide=true;});}continue;}
      give(()=>kept[name]&&take[name]>down,()=>{take[name]--;},()=>{take[name]++;});
    }
    give(()=>keepMetaGap,()=>{keepMetaGap=false;},()=>{keepMetaGap=true;});
    give(()=>keepRules,()=>{keepRules=false;},()=>{keepRules=true;});
    // (11-16: the QUESTIONS box's two border rows go after its content rows above, before the structural rows below.)
    give(()=>kept.QUESTIONS&&keepQuestionBox,()=>{keepQuestionBox=false;},()=>{keepQuestionBox=true;});
    give(()=>kept.MESSAGES&&!messageOne,()=>{messageOne=true;},()=>{messageOne=false;});
    // The box comes back with its context rows only while the workers stand.
    // The unit rows a worker's calls add are the first of the route's rows to go.
    give(()=>anyCalled&&keepCalled,()=>{keepCalled=false;},()=>{keepCalled=true;},'route');
    give(()=>keepBoxEnd,()=>{keepBoxEnd=false;},()=>{keepBoxEnd=true;},'route');
    give(()=>nowTwo,()=>{nowTwo=false;},()=>{nowTwo=true;},'route');
    give(()=>keepCtxLine,()=>{keepCtxLine=false;},()=>{keepCtxLine=true;},'route');
    // The box comes back with its NOW rows only while the workers stand.
    give(()=>keepNow,()=>{keepNow=false;},()=>{if(keepWorkers||!anyWorkers)keepNow=true;},'route');
    give(()=>anyWorkers&&keepWorkers&&!keepNow,()=>{keepWorkers=false;},()=>{keepWorkers=true;},'route');
    give(()=>keepPlace,()=>{keepPlace=false;},()=>{keepPlace=true;},'route');
    give(()=>twoRows,()=>{twoRows=false;},()=>{twoRows=true;},'route');
    give(()=>kept.MESSAGES&&take.MESSAGES>1,()=>{take.MESSAGES--;},()=>{take.MESSAGES++;});
    give(()=>routeLimit>1,()=>{routeLimit--;},()=>{routeLimit++;});
    give(()=>kept.ROUTE&&routeRule(),()=>{keepRouteRule=false;},()=>{keepRouteRule=true;});
    give(()=>keepGit,()=>{keepGit=false;},()=>{keepGit=true;});
    give(()=>kept.ROUTE,()=>{kept.ROUTE=false;},()=>{kept.ROUTE=true;});
    give(()=>keepOptional,()=>{keepOptional=false;},()=>{keepOptional=true;});
    for(const name of lowest){
      give(()=>kept[name]&&take[name]>0,()=>{take[name]--;},()=>{take[name]++;});
      give(()=>kept[name],()=>{kept[name]=false;},()=>{kept[name]=true;});
      // 11-16: USAGE gives way after MEM does: the harnesses that are not this window's (the last first), then its name row.
      if(name==='MEM'&&usageOn){
        for(let i=usageData.length-1;i>=0;i--)if(i!==ownUsage)give(()=>keepLine[i],()=>{keepLine[i]=false;},()=>{keepLine[i]=true;});
        give(()=>keepUsageHead,()=>{keepUsageHead=false;},()=>{keepUsageHead=true;});
      }
    }
    give(()=>keepSent&&sentRows.length>0,()=>{keepSent=false;},()=>{keepSent=true;});
    // (11-16: this window's own usage row beside the buttons costs no row, so it stays while no other usage row is left to give.)
    give(()=>keepQuota&&(!usageOn||footRule()>0),()=>{keepQuota=false;},()=>{keepQuota=true;});
    const choices=()=>JSON.stringify([keepSpacers,keepMulti,keepMetaGap,keepRules,keepGit,keepOptional,keepQuota,briefTwo,keepGuide,keepSent,take,kept,keepAction,
      keepNow,keepWorkers,keepPlace,twoRows,routeLimit,messageOne,keepFootRule,memBody,keepQuestionBox,keepUsageHead,keepLine,keepRouteRule,keepBoxEnd,nowTwo,keepCtxLine,resHosts,resGpus,keepCalled]);
    const stuck=new Set();
    for(const {undo,family} of gave.reverse()){
      if(family&&stuck.has(family))continue;
      const was=choices();undo();
      if(need()>budget){if(family)stuck.add(family);const v=JSON.parse(was);[keepSpacers,keepMulti,keepMetaGap,keepRules,keepGit,keepOptional,keepQuota,briefTwo,keepGuide,keepSent]=v;
        Object.assign(take,v[10]);Object.assign(kept,v[11]);[keepAction,keepNow,keepWorkers,keepPlace,twoRows,routeLimit,messageOne,keepFootRule,memBody,keepQuestionBox,keepUsageHead]=v.slice(12,23);keepLine.splice(0,keepLine.length,...v[23]);keepRouteRule=v[24];[keepBoxEnd,nowTwo,keepCtxLine,resHosts,resGpus,keepCalled]=v.slice(25,31);}
    }
    const order=[];
    top.forEach((row,i)=>{if((keepSpacers||!spacers.has(i))&&(keepMetaGap||!metaGaps.has(i))&&(keepRules||!rules.has(i))&&(keepOptional||!optional.has(i))&&(keepGit||!gitRows.has(i))&&(keepMulti||!extra.has(i)))
      order.push({parts:!keepMulti&&oneRow.has(i)?oneRow.get(i):row,groups:topGroups.get(i),action:topActions.get(i)});});
    for(const item of order)add(item.parts,item.action,item.groups);
    // Each section stands whole (its head and the rows it kept), one after another from the top; the
    // rows left over stand between the sections above and MESSAGES·MEM (spec rule 14: those two keep to
    // the panel's bottom, right above the last row), or under the last section when there are none.
    const lower=[];
    for(const s of SECTIONS){
      if(!kept[s.name])continue;
      // QUESTIONS never gives its place to the structural rows above it: those go first (`lastRow`).
      if(s.name!=='QUESTIONS'&&content.length+lower.length+sectionSize(s)+(['MESSAGES','MEM'].includes(s.name)?footRule():0)>budget-1)break;
      if(s.box){
        // The rows the route box takes, for the host's wheel (panel rows, as hits count them).
        const {block,area}=routeBlockOf();
        routeArea={top:content.length+1,bottom:content.length+block.length,...area};
        for(const item of block)add(item.parts,item.action,item.groups);
        continue;
      }
      if(s.name==='QUESTIONS'&&boxedQuestions()){
        const key={key:here?'lvl_y':'dim',role:here?'accent-line':'messages',blink:here&&waitBlinks};
        const label=[...(questionCount?[{...text(questionCount,'g_blocked'),role:'accent'},text(' ')]:[]),{...text(summary?.label||'QUESTIONS','dim'),role:'title'},text(' '),text('›','dim')];
        add(boxTop(label,key,{action:'question'}).parts,'question',s.groups);
        for(const row of questionRowsIn())add(boxRow({parts:row},key).parts,'question');
        add(boxBottom(key).parts,'question');
        continue;
      }
      const into=['MESSAGES','MEM'].includes(s.name)?(parts,action,groups)=>lower.push({parts,action,groups}):add;
      into(s.head(),s.action,s.groups);
      sectionRows(s).forEach(row=>into(row,s.action));
    }
    // 11-16: the USAGE rows stand last in the lower block (the last harness's row, where it fits, on the last row below).
    if(resourceCount()){lower.push({parts:resourceHead()});for(const line of resourceLines())lower.push({parts:line});}
    let lastUsage=[];
    if(usageOn&&usageLines().length){
      const list=usageLines(),own=u=>u.harness===harness?['quota']:[];
      if(keepUsageHead)lower.push({parts:sectionHead([],'USAGE',[],[[]],false)});
      list.slice(0,-1).forEach(u=>lower.push({parts:usageFit(u,inner).row,groups:own(u)}));
      const end=list.at(-1);
      if(usageShared(end))lastUsage=usageFit(end,quotaRoom).row;else lower.push({parts:usageFit(end,inner).row,groups:own(end)});
    }
    const ruled=!usageOn&&lower.length&&footBase()?1:0;
    if(fill&&lower.length)while(content.length+lower.length+ruled<height-2-1)add([]);
    for(const row of lower)add(row.parts,row.action,row.groups);
    if(ruled)add([{...text('─'.repeat(inner),'dim'),role:'messages'}]);
    // A reset time that does not fit goes whole, never as a cut `(↻ …`. Spec rule 49 (b): 5h/7d
    // share the last row with the fold button and the gear, which stay however short rows run.
    // Correction 7 (11-13): the model windows (Fable's) stand after 7d; short of cells the row
    // gives way in this order: the reset times, then 5h (the windows shorter than 7d), then the
    // model values from the last, then 7d. Each value goes whole.
    const quotaRow=usageOn?lastUsage:(()=>{
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
    const lastEnd=usageOn?usageLines().at(-1):null;
    lastRow(keepQuota?quotaRow:[],usageOn?(lastEnd&&lastUsage.length&&lastEnd.harness===harness?['quota']:[]):keepQuota&&quota.length?['quota']:[],order.length,kept.QUESTIONS?0:1);
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
  const glyphPart=own?[{...text(own[0]==='spin'?spinGlyph.text:own[0],own[0]==='spin'&&harnessName?'hb_'+harnessName:own[1]),...(own[0]==='spin'?{spin:true}:{}),role:'session'}]:[];
  const tagPart=tag?[text('[','dim'),steward?stewardTag(tag):{...text(tag,own?own[1]:'dim'),role:'session'},text(']','dim')]:[];
  const harnessPart=harnessName?[text(harnessName,'hb_'+harnessName)]:[];
  const flagPart=steward?[stewardPart('⚑')]:[];
  // Short of cells the steward mark goes first, then the harness, then the number;
  // the glyph stays to the end.
  // 11-18: a flag at the end of the head is followed by two cells before the line: `⚑` (U+2691) is drawn two cells wide in some
  // terminals and would touch the line; the width still counts it as one cell and the second cell is counted as a cell.
  const headParts=pieces=>{const shown=pieces.filter(x=>x.length);return shown.length?[frameText(' '),...shown.flatMap((x,i)=>i?[text(' '),...x]:x),frameText(shown.at(-1)===flagPart?'  ':' ')]:[];};
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
  const painted=appearance(rows,cfg,{colors:borderColors,noColor:model.no_color===true,background,harness});
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
  // 11-15: the waiting blink's cells (the QUESTIONS rows' red letters) ride the same motion, on Fleet's beat (11-16).
  const framed=cells.length;
  if(waitBlinks)cells.push(...blinkCells(rows,measure,clean,1,count-1));
  const blinked=cells.length>framed;
  const motion=cells.length?{cells,
    at:tick=>appearance([cells.map(c=>c.blink?{...c.blink,text:c.ch,blinkTone:blinkTone(tick)}:c.rule!==undefined?ruleLook(c.rule,tick):borderCell(place(c.row,c.col),c.ch,tick))],cfg,{colors:borderColors,noColor:model.no_color===true,background,harness})[0],
    next:tick=>Math.min(blinked?blinkNext(tick):Infinity,framed?frameNext(tick):Infinity)}:null;
  function frameNext(tick){const local=borderTick(tick),shift=Math.trunc(tick)-local;
      if(ruleMoving){const r=ruleTick(tick);return Math.trunc(tick)+scanNext(r,inner)-r;}
      if(moving==='scanners')return Math.trunc(tick)+1;
      if(moving==='border'){const k=Math.floor(local*around/BORDER_LAP);let n=Math.ceil((k+1)*BORDER_LAP/around);if(n<=local)n=local+1;return n+shift;}
      const now=breathStep(local,breathSteps);for(let t=local+1;t<=local+BORDER_BREATHE;t++)if(breathStep(t,breathSteps)!==now)return t+shift;return local+BORDER_BREATHE+shift;}
  const animated=!!motion||rows.slice(0,count).some(row=>row.some(part=>part.spin||part.beat));
  // The statusline groups this paint actually shows (cut rows count; dropped ones do not).
  const groups=[...new Set(contentGroups.slice(0,Math.max(0,count-2)).flat())];
  if(routeArea&&routeArea.bottom>count-2)routeArea=null;
  return {rows:painted,hits:hits.filter(h=>h.row<count),color,routeArea,groups,animated:animated&&!settings,blinks:blinks&&!settings,spins,motion};
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
// cells the window less 4; narrower than 28 the whole window. A table popup stands up to 116 cells, the routes popup up to 196, the question popup up to 132. Height: the content's, at most the
// rows above the strip less 2 (the content then pages inside). Display only: the keys go to the
// popup's owner (the host) while it stands; nothing here reads input.
// 11-18: the routes popup alone stands up to 196 cells (`route`: 148, then "더 늘려"; the read for it takes `columns` up to 200), the question
// popup up to 132 (`question`: "가로 길이를 좀 늘려줘라. 가독성이 안좋다"; 104 was too little for the person), the other table popups at most 116.
// `text` (76) is no popup's now; a popup asked for with no kind keeps it.
const POPUP_TEXT=76,POPUP_TABLE=116,POPUP_ROUTE=196,POPUP_QUESTION=132;
// A foreground's gray level (0 black .. 255 white), or null for a hue or the terminal's own color.
function grayLevel(fg){
  if(Number.isInteger(fg)){
    if(fg>=232&&fg<=255)return 8+10*(fg-232);
    if(fg>=16&&fg<=231){const n=fg-16,r=Math.floor(n/36),g=Math.floor(n/6)%6,b=n%6;return r===g&&g===b?(r?55+40*r:0):null;}
    return ({0:0,7:192,8:128,15:255})[fg]??null;
  }
  const m=/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(fg??''));
  if(!m)return null;
  const [r,g,b]=m.slice(1).map(x=>parseInt(x,16));
  return Math.max(r,g,b)-Math.min(r,g,b)<=8?Math.round((r+g+b)/3):null;
}
// 11-15: a popup's own look where the panel shows hue (the face's `popup`, `popupLine`, `popupTitle`, `bright` and
// `popupFloor`; an unknown face the dark one's, as the veil): every cell of its frame on the ground (no
// part keeps a ground or a reversed cell of its own: inside a popup there are two grounds, the popup's
// and the chosen choice's), the line and the title bright, a word in the terminal's own color the
// brightest, a dim or gray word no fainter than the floor. A hue (a state, a harness, the question's
// rose) keeps its color and reads at its full strength (a dim one is no longer dimmed).
// 11-17: the popup's ground, line, title and chosen ground are the face's 256-color step, or in 24-bit color
// (where the terminal says it takes it) its `rgb` value.
const popupTone=(F,key,colors)=>colors>256&&F.rgb&&F.rgb[key]!==undefined?F.rgb[key]:F[key];
function popupLook(part,F,light,colors=256){
  const floor=grayLevel(F.popupFloor),level=grayLevel(part.foreground),faint=v=>light?v>floor:v<floor;
  const ground={background:popupTone(F,'popup',colors),reverse:false};
  if(part.popupLine)return {...part,foreground:popupTone(F,'popupLine',colors),dim:false,...ground};
  const own=part.foreground===null||part.foreground===undefined||part.foreground===-1;
  if(!own&&level===null)return {...part,dim:false,...ground};
  if(part.popupTitle)return {...part,foreground:popupTone(F,'popupTitle',colors),dim:false,...ground};
  if(own)return {...part,foreground:part.dim?F.popupFloor:F.bright,dim:false,...ground};
  return part.dim||faint(level)?{...part,foreground:F.popupFloor,dim:false,...ground}:{...part,...ground};
}
function popupWidth(cols,kind='text'){
  if(cols<28)return Math.max(4,cols);
  if(cols<80)return cols-4;
  return kind==='route'?Math.min(POPUP_ROUTE,cols-4):kind==='question'?Math.min(POPUP_QUESTION,cols-4):kind==='table'?Math.min(POPUP_TABLE,cols-4):POPUP_TEXT;
}
// The rows the content may take inside the frame. A low window gives up the empty rows round the
// frame first (under 8 rows), then its bottom line (3 rows or fewer), so the content keeps rows.
function popupHeight(space){return space>=8?space-2:space;}
function popupRoom(rows,band=0){const H=popupHeight(Math.max(1,rows-band));return Math.max(0,H>3?H-2:H-1);}
function popup({cols,rows,band=0,kind='text',title=[],body=[],sections=[],labels=sections,current=0,model={},configuration,cellWidth,background=''}){
  const measure=cellWidth||(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0));
  const styles=model.styles||{},text=(value,key='dim')=>({text:String(value??''),...(styles[key]||{dim:key==='dim'})});
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const size=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
  // (11-15: a part that carries its cells as Fleet measured them, `cells`, takes them whole: the row is laid
  // out by the measure Fleet drew it with.)
  function clip(parts,limit){let out=[],used=0;for(const part of parts){
    if(Number.isInteger(part.cells)&&used+part.cells<=limit){const {cells,...rest}=part;out.push({...rest,text:clean(part.text)});used+=cells;continue;}
    let t='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;t+=c;used+=w;}if(t){const {cells,...rest}=part;out.push({...rest,text:t});}if(used>=limit)break;}return {parts:out,used};}
  const cut=(parts,limit)=>size(parts)<=limit?parts:limit<1?[]:[...clip(parts,limit-1).parts,{...text('…','dim')}];
  const space=Math.max(1,rows-band),W=Math.min(cols,popupWidth(cols,kind)),inner=Math.max(0,W-4);
  const maxH=popupHeight(space),shut=maxH>3,list=body.slice(0,Math.max(0,shut?maxH-2:maxH-1)),H=Math.min(maxH,list.length+(shut?2:1));
  const x=Math.floor((cols-W)/2),y=Math.max(0,Math.round((space-H)/2));
  // 11-15 (panel polish 3): where nothing behind it is veiled (mono, eight colors, NO_COLOR) the
  // frame's line is bright instead of the messages' gray.
  const bright=veil(configuration,{colors:model.colors??256,noColor:model.no_color===true,background})===null;
  const line=v=>bright?{text:v}:{text:v,popupLine:true};
  // The top line: `╭─ title ──… ✕ ─╮`; a title too long ends in `…`, the `✕` stays.
  const room=Math.max(0,W-10),name=room>=4?cut(title.map(p=>({...p,text:clean(p.text),popupTitle:true})),room):[];
  const head=name.length?[line('╭─ '),...name,line(' ')]:[line('╭─')];
  const close=W>=8?[text(' ','dim'),text('✕','dim'),line(' ─╮')]:[line('╮')];
  const top=[...head,line('─'.repeat(Math.max(0,W-size(head)-size(close)))),...close];
  // The bottom line: the sections, the current one bright.
  // (11-15: a section's name on the line is its label, `현황` for STATUS; its press opens it by its key.)
  const named=i=>String(labels[i]??sections[i]);
  const names=sections.flatMap((s,i)=>[...(i?[text(' · ','dim')]:[]),{...text(named(i),i===current?'loc_repo':'dim'),role:i===current?'':'title',bold:i===current}]);
  const listed=names.length&&size(names)+6<=W?[line('╰─ '),...names,line(' ')]:[line('╰─')];
  const bottom=[...listed,line('─'.repeat(Math.max(0,W-size(listed)-1))),line('╯')];
  const frame=[top];
  // 11-15 ("질문 선지 선택 효과를 틴트로"): a row the question editor marks chosen (`tint`) stands on the
  // chosen ground across the popup's inside, its content's cells exactly (the cell each side between
  // it and the side line keeps the popup's ground); mono, eight colors and NO_COLOR reverse those cells.
  for(const row of list){const fit=clip(Array.isArray(row)?row:[],inner),chosen=Array.isArray(row)&&row.some(p=>p&&p.tint===true);
    const cells=[...fit.parts,text(' '.repeat(Math.max(0,inner-fit.used)))].map(p=>chosen?{...p,chosenRow:true}:p);
    frame.push([line('│ '),...cells,line(' │')]);}
  if(shut)frame.push(bottom);
  // One empty cell round the frame where the window has it.
  const left=x>0?1:0,right=x+W<cols?1:0,above=y>0?1:0,below=y+H<space?1:0;
  const blank=n=>({text:' '.repeat(n)});
  const out=[];
  if(above)out.push([blank(W+left+right)]);
  for(const row of frame)out.push([...(left?[blank(1)]:[]),...row,...(right?[blank(1)]:[])]);
  if(below)out.push([blank(W+left+right)]);
  const cfg={...defaults,...configuration,shade:'none'};
  let painted=appearance(out,cfg,{colors:model.colors??256,noColor:model.no_color===true,background});
  // Where the panel shows hue the frame stands on its own ground (`popupLook`), and (11-15: "바운더리와
  // 틴트의 간격이 ... 좌우가 더 좁거든? 스페이스바 한칸 정도 여백을 더") so does the one empty cell each side of it
  // on the frame's rows, where the window has it; the empty rows above and below keep the terminal's
  // own (they part it from what it covers). The chosen row's cells take the chosen ground.
  if(!bright){
    const known=face(cfg.theme,background),F=FACES[known||'dark'],light=known==='light',colors=model.colors??256;
    painted=painted.map((row,i)=>{
      const inFrame=i>=above&&i<above+frame.length;if(!inFrame)return row;
      return row.map((part,k)=>(left&&k===0)||(right&&k===row.length-1)?{...part,background:popupTone(F,'popup',colors),margin:true}:
        part.chosenRow?{...popupLook(part,F,light,colors),background:popupTone(F,'chosen',colors)}:popupLook(part,F,light,colors));
    });
  }else painted=painted.map(row=>row.map(part=>part.chosenRow?{...part,reverse:true}:part));
  painted=painted.map(row=>row.map(({popupLine,popupTitle,chosenRow,tint,...part})=>part));
  // Presses, in window cells: `✕` closes, a section's name opens its popup.
  const hits=[];
  if(close.length>1)hits.push({row:y,left:x+W-4,right:x+W-3,action:'popup-close'});
  let at=x+3;for(let i=0;i<sections.length&&shut&&listed.length>2;i++){const n=measure(clean(named(i)));hits.push({row:y+H-1,left:at,right:at+n-1,action:'popup:'+sections[i]});at+=n+3;}
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
// 11-16 (the person: "블록 안에서 구분선을 하나 긋고, 양식을 맞추자. [6c] claude opus 5.5 xhigh 이런식으로"): one peer request's
// rows, drawn here alone for the route box (beside the native area and under it) and the MESSAGES popup. Its head reads
// as a route's doer row: the mark (→ sent, ◂ received, ✓/✕ closed), the other session's Fleet number, then its harness,
// model and effort in the colors a route's doer takes (the backend's `paint`), and the age at the right edge, where a
// route keeps its time. Two cells in, the title where a route's title stands, then the status, dim, where a route's kind
// row stands. `titled` is the title and the status on one row (the kind rows gone), `one` the one-row form. Short of
// cells the age goes first, then the effort, then the model; then the title is cut with `…` (past that the harness goes,
// then the title); the other session and the status stay.
function requestForms(q,{text,measure,width,arrow='loc_repo'}){
  const clean=s=>String(s??'').replace(/[\x00-\x1f\x7f-\x9f]/g,' ');
  const size=parts=>parts.reduce((n,p)=>n+measure(clean(p.text)),0);
  const clip=(parts,limit)=>{const out=[];let used=0;for(const part of parts){let s='';for(const c of clean(part.text)){const w=measure(c);if(used+w>limit)break;s+=c;used+=w;}if(s)out.push({...part,text:s});if(used>=limit)break;}return out;};
  const cut=(parts,limit)=>size(parts)<=limit?parts:limit<1?[]:[...clip(parts,limit-1),text('…','dim')];
  const pad=n=>text(' '.repeat(Math.max(0,n)));
  const paint=q.paint&&typeof q.paint==='object'?q.paint:{};
  const piece=p=>p&&typeof p==='object'&&clean(p.text).trim()?[{...p,text:clean(p.text)}]:[];
  const mark=text(String(q.glyph||q.way||'→'),q.glyph==='✕'?'lvl_r':q.glyph==='✓'?'lvl_g':arrow);
  // 11-18 (the person: "세션 id 외에도 repo 위치까지 request는 표시를 해주는게 좋겠네"): the other session's repository name, dim, after its number
  // (`[c2] other-repo`); the backend sends none for a session of this window's own repository or one whose it does not know. It is part
  // of who the request is with, so it stays after the harness, model, effort and age have gone, and the title goes before it does.
  const repo=clean(q.repo||'').trim()?[text(clean(q.repo).trim(),'dim')]:[];
  const who=q.who?[text(String(q.who),'dim'),...(repo.length?[text(' '),...repo]:[])]:[];
  const H=piece(paint.harness),M=piece(paint.model_text),F=piece(paint.effort);
  const title=[text(String(q.title||q.id),'loc_repo')];
  const status=q.status?[text(String(q.status),q.kind==='request-rejected'?'lvl_r':'dim')]:[];
  const age=q.age?[text(String(q.age),'dim')]:[];
  const joint=[text(' · ','dim')],indent=[text('  ')];
  const spaced=list=>list.filter(x=>x.length).flatMap((x,i)=>i?[text(' '),...x]:x);
  // A row with the age at the right edge, one cell at least between them; null where it does not fit.
  const ended=(left,end)=>size(left)+1+size(end)<=width?[...left,pad(width-size(left)-size(end)),...end]:null;
  const drops=[[H,M,F,age],[H,M,F,[]],[H,M,[],[]],[H,[],[],[]]];
  const lead=w=>[mark,...(w.length?[text(' '),...w]:[])];
  let head=null;
  for(const [h,m,f,e] of [...drops,[[],[],[],[]]]){
    const left=lead(spaced([who,h,m,f])),row=e.length?ended(left,e):size(left)<=width?left:null;
    if(row){head=row;break;}
  }
  head=head||cut(lead(who),width);
  const titleRow=[...indent,...cut(title,width-2)];
  const statusRow=status.length?[...indent,...cut(status,width-2)]:null;
  const room=width-2-size(joint)-size(status);
  const titled=!status.length?titleRow:room>=3?[...indent,...cut(title,room),...joint,...status]:[...indent,...cut(status,width-2)];
  // (`→ [c2] other-repo · title`: with no harness or model between them, the repository and the title read as the sketch does.)
  const body=(w,t,bare=false)=>[...lead(w),...(t.length?[bare&&repo.length?joint[0]:text(' '),...t]:[]),...(status.length?[...joint,...status]:[])];
  const one=(()=>{
    for(const [h,m,f,e] of drops){
      const left=body(spaced([who,h,m,f]),title,!h.length&&!m.length&&!f.length),row=e.length?ended(left,e):size(left)<=width?left:null;
      if(row)return row;
    }
    for(const h of [H,[]]){const w=spaced([who,h]),left=width-size(body(w,[]))-(!h.length&&repo.length?size(joint):1);if(left>=3)return body(w,cut(title,left),!h.length);}
    if(size(body(who,[]))<=width)return body(who,[]);
    // Too narrow for the repository name beside the number and the status: it goes last of all, and the title with the number stays.
    if(repo.length&&q.who){const tag=[text(String(q.who),'dim')],left=width-size(body(tag,[]))-1;if(left>=0)return left>=3?body(tag,cut(title,left)):body(tag,[]);}
    // 11-17: a status that does not fit even with no title (`awaiting delivery · why`) keeps its first word whole and ends in `…`.
    if(status.length&&typeof q.status_head==='string'&&q.status_head){
      const room=width-size(lead(who))-size(joint);
      if(room>=measure(q.status_head))return [...lead(who),...joint,...cut(status,room)];
    }
    const left=width-size(body([],[]))-1;
    return left>=3?body([],cut(title,left)):cut([mark,text(' '),...title],width);
  })();
  return {head,title:titleRow,status:statusRow,titled,one};
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
    // (11-16: its head is the route box's one-row request form, at the popup's width: the same values, one drawing.)
    out.push(requestForms(q,{text,measure,width:columns}).one);
    for(const [label,value,tone] of Array.isArray(q.details)?q.details:[])out.push(...wrapText((label?label+': ':'')+String(value||''),({lvl_r:'lvl_r',lvl_g:'lvl_g',dim:'dim'})[tone]||'loc_repo',columns));
    // (11-15: a body's markdown tables come drawn by Fleet as aligned rows, two cells in; its other lines wrap.)
    for(const value of Array.isArray(q.body_parts)?q.body_parts:Array.isArray(q.body)?q.body:[]){
      if(Array.isArray(value))out.push([text('  '),...value.filter(part=>part&&typeof part==='object').map(part=>({...part,text:clean(part.text)}))]);
      else out.push(...wrapText(value,'loc_repo',columns));
    }
    if(q.body_more)out.push([text('  …')]);
  }
  for(const m of (Array.isArray(model.messages)?model.messages:[]).slice().reverse()){
    if(!m||typeof m!=='object')continue;
    if(out.length)out.push([]);
    const delivery=Array.isArray(m.delivery)&&m.delivery[0]?[text(' · ','dim'),text(String(m.delivery[0]),m.delivery[1]||'dim')]:[];
    const head=[text(m.way,m.way==='←'?'lvl_g':'loc_repo'),text(' '),text(String(m.who||''),'loc_repo'),...(m.harness?[text(' '+m.harness,'dim')]:[]),
      ...(m.when?[text(' · '+m.when,'dim')]:[]),...(m.kind?[text(' · '+m.kind,'dim')]:[])];
    // 11-17: where `awaiting delivery · why` does not fit the head's row, it goes on whole on the row under it (the popup shows the why whole).
    if(delivery.length&&head.concat(delivery).reduce((n,p)=>n+measure(clean(p.text)),0)>columns){
      out.push(head);out.push(...wrapText(String(m.delivery[0]),m.delivery[1]||'dim',columns));
    }else out.push([...head,...delivery]);
    out.push(...wrapText(m.text||m.body||'','loc_repo',columns));
  }
  return out.length?out:[[text('·')]];
}
// 11-18: the ROUTE popup's first screen, before its own reading comes: the panel's route box (this same `render`'s
// one drawing, from the routes the panel already holds) laid at the popup's width, its box sides and the panel's
// own sides taken off. The requests and messages are left out of the model (the popup's table has no such rows);
// the rows are the box's between its top and bottom lines. A press on a route's row is `['route', id]`, as the
// popup's table gives it; the box's title press and any other are not the popup's. `null` where the box did not
// draw (no routes) or its rows are not the shape this strips (the popup then keeps its own spinner).
function routesBody({model={},columns=72,cellWidth,configuration,background=''}={}){
  columns=Math.max(1,Math.min(200,Math.trunc(Number(columns))||72));
  const v=render({frame:0,width:columns+8,height:200,side:true,fill:true,model:{...model,requests:[],messages:[]},configuration,cellWidth,background,state:'idle'});
  const area=v&&v.routeArea;if(!area)return null;
  const top=area.top+1,source=v.rows.slice(top,area.bottom);
  if(!source.length)return null;
  const rows=[];
  for(const row of source){
    if(!Array.isArray(row)||row.length<7)return null;
    // The panel's own sides (`│`, ` `) and the box's (`│ `, ` │`) stand in these pieces; any other shape is not stripped.
    if(row.slice(0,3).map(part=>part.text).join('')!=='│ │ '||row.slice(-3).map(part=>part.text).join('')!==' │ │')return null;
    const inner=row.slice(3,-3).map(({beat,spin,blink,...part})=>part);
    while(inner.length&&/^ *$/.test(inner[inner.length-1].text))inner.pop();
    rows.push(inner);
  }
  const hits=[],routes=[];
  for(const h of v.hits){
    if(h.row<top||h.row>=area.bottom||typeof h.action!=='string')continue;
    const m=/^popup:ROUTE:(.+)$/.exec(h.action);if(!m)continue;
    hits.push({row:h.row-top,left:0,right:columns-1,action:['route',m[1]]});
    if(!routes.includes(m[1]))routes.push(m[1]);
  }
  return {rows,hits,routes};
}
module.exports={render,keyRows,defaults,fields,change,appearance,options:OPTIONS,SPIN,FACES,breathStep,popup,popupWidth,popupRoom,messagesBody,veil,
  RULE:{PEAK:RULE_PEAK,FADE:RULE_FADE,SCAN:RULE_SCAN,REST:RULE_REST,EASE:RULE_EASE},scanAt,WAVE:{COUNT:WAVE_COUNT,LAP:WAVE_LAP,STEPS:WAVE_STEPS,FLOOR:WAVE_FLOOR,MIN:WAVE_MIN},
  BORDER:{LIT:BORDER_LIT,ARC:BORDER_ARC,LAP:BORDER_LAP,SHADES:BORDER_SHADES,BREATHE:BORDER_BREATHE,BREATHE_STEPS:BORDER_BREATHE_STEPS,BREATHE_STEPS_256:BORDER_BREATHE_STEPS_256}};
module.exports.routesBody=routesBody;
