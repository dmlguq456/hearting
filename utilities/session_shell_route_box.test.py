"""11-15 (the person's correction of #1648): the panel's route box stands again as v4.36.4 drew it, a
press on it opens the route popup, the requests stand once (in the box), and a popup reads apart from
what it covers (its own ground, bright words).

The golden rows (`fixtures/session-shell-route-box-v4.36.4.json`) were drawn by v4.36.4's
`panel-view.cjs` (8ea2f6408) from the synthetic scenes the file carries; the box drawn now from the same
scenes must match them cell for cell (text, color, weight, ground) and press cell for press cell (the
press now opening a popup). 11-17 (route row 5): the route rows in the golden are drawn again in the new
grammar (the kind and intensity first, the owner's lines two cells in, a worker hanging by `↳`, no worker
box); its top and bottom lines and the request rows stay v4.36.4's. Everything here is synthetic.
11-17 (route zones, two-row session lines): a session's lines are now its who row with `CTX 52% · 2h 10m` beside the model
(no gauge) and, only where the session has a NOW, that NOW under it; the golden's gauge rows and the worker's `execute · Edit`
rows are not drawn any more. The comparison therefore takes the session rows out of both sides (the golden's old ones, the
drawn ones) except the head of each who row (mark and role word, with their colors and press cells), and compares the rest
of the box cell for cell; the session rows' own grammar is `session_shell_route_row_5_1117.test.py`'s.
11-25 (four groups): the box's title reads `ROUTE 2` (the count after the name; the golden's reads `2 ROUTE`, so the comparison reads the drawn title the golden's
way), the box is found by that title, and beside the native area the MESSAGES head and items stand as the SESSION group's last list (the items two cells in).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
GOLDEN = ROOT / "utilities/fixtures/session-shell-route-box-v4.36.4.json"


class RouteBoxTest(unittest.TestCase):
    def node(self, script, *args):
        result = subprocess.run(["node", "-e", script, str(SHELL), str(GOLDEN), *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    HEAD = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const pv=require(path.join(process.argv[1],'panel-view.cjs')),golden=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const txt=r=>r.map(p=>p.text).join('');
// The route box: its top line `│ ╭─ N ROUTE` (`route N` under the native area) to its bottom line `│ ╰`.
// (11-16: the QUESTIONS section is a thin box too, above it: the route box is the one whose top line names the route.)
const boxOf=v=>{const rows=v.rows.map(txt),a=rows.findIndex(s=>/^│ ╭─ (?:ROUTE \d+|route)/.test(s));if(a<0)return null;const b=rows.findIndex((s,i)=>i>a&&/^│ ╰/.test(s));return {a,b};};
const P=t=>({text:t}),D=t=>({text:t,dim:true});
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
"""

    def test_the_route_box_draws_as_v4_36_4_did(self):
        # Item 1: the same scenes give the same box rows: the head (`N ROUTE │ M REQUEST`), the owner's
        # rows (role, harness, model; `CTX` with its gauge and the time after it; the title; kind and stage),
        # the workers' box further in, the request rows, the folded forms, a failed route's line color, the
        # hidden counts on the border, the `route line` choice, every face and theme; and the same press cells.
        out = self.node(self.HEAD + r"""
const styleOf=p=>JSON.stringify([p.foreground??null,!!p.dim,!!p.bold,p.background??null]);
const runs=row=>{const all=[];for(const p of row)for(const c of [...p.text])all.push([c,styleOf(p)]);
 const inner=all.slice(2,-2),out=[];for(const [c,s] of inner){const last=out.at(-1);if(last&&last[1]===s)last[0]+=c;else out.push([c,s]);}return out;};
const strip=m=>{const {lab,sessions,memory_items,messages,wait,answerable,...rest}=m;return {...rest,messages:[]};};
// 11-22: the box's top line ends in `›` at its right end (`╭─ 2 ROUTE ───── ›─╮`, after a hidden count where there is one); the golden's has none. The comparison is made
// with that mark taken out: its cells go back to the line (with a hidden count: the cell the mark took from the line).
// 11-25 (four groups): the count stands after the name (`╭─ ROUTE 2 ──`); the golden's reads `2 ROUTE`, so the name part is read the golden's way too.
const unarrow=row=>{const cs=[];for(const [t,s] of row)for(const c of t.replace(/^ROUTE (\d+)$/,'$1 ROUTE'))cs.push([c,s]);
 const at=cs.findIndex(([c])=>c==='›');if(at<0)return row;
 const line=cs.find(([c])=>c==='─')[1];
 if(cs[at-2]&&cs[at-2][0]==='─'){cs[at-1]=['─',line];cs[at]=['─',line];}
 else{cs.splice(at,1);const rule=cs.findIndex(([c],i)=>c==='─'&&i>3);cs.splice(rule,0,['─',line]);}
 const out=[];for(const [c,s] of cs){const last=out.at(-1);if(last&&last[1]===s)last[0]+=c;else out.push([c,s]);}return out;};
let n=0,same=0,folded=0,soloSame=0,soloFolded=0,noTool=0;
for(const c of golden.cases){
 const scene=c.alone?strip(golden.scenes[c.scene]):golden.scenes[c.scene];
 // 11-18 (review C4): under the native area a route's row reads its owner (as the NOW under it does); the golden's row is the leading
 // worker's, so the comparison there is made without the owner's values (`owner_paint`), which the panel's own tests draw.
 const model=c.side?scene:{...scene,routes:(scene.routes||[]).map(({owner_paint,...route})=>route)};
 // 11-18: the golden draws a worker `↳` under its owner with a stage row of its own, which is the `none` form of the panel setting
 // `worker_box` (the default now boxes the workers under the owner with the stage text on the box's top line); the comparison is made in that form.
 const v=pv.render({frame:0,width:c.width,height:c.height,side:c.side,fill:c.side,model,state:'working',configuration:{...c.cfg,worker_box:'none'},background:c.background});
 const label=JSON.stringify({scene:c.scene,side:c.side,width:c.width,height:c.height,cfg:c.cfg,background:c.background,alone:!!c.alone});
 const b=boxOf(v);assert(b,'no route box '+label+'\n'+v.rows.map(txt).join('\n'));
 const want=c.rows.map(row=>row.map(([t,s])=>[t,JSON.stringify(golden.styles[s])]));
 const hits=v.hits.filter(h=>h.row>=b.a&&h.row<=b.b).map(h=>[h.row-b.a,h.left,h.right,h.action]);
 // 11-16: the request rows, the rule above them and the title/bottom lines that count them are drawn anew (peer
 // requests read as a route's doer: `peer-request-1116`); the route rows are the golden's (drawn again 11-17) cell for
 // cell, and so are their press cells. A scene without requests is compared whole.
 // 11-17 (route zones): the named rules (`─ dispatch ───`, `─ request ───`) are drawn anew too: the comparison is made
 // with those rule rows (they have no press of their own) taken out of the drawn box, the golden having none.
 const drawn=model.requests&&model.requests.length;
 const zoneRule=r=>/^│ ─ (?:direct|dispatch|request) ─+\s*│$/.test(txt(r.map(([t])=>({text:t}))));
 // A who row (mark, role word, harness, model, effort, `CTX`) keeps only its head `● owner` / `↳ ⠋ worker`; the lines that
 // only a session has under it (the golden's gauge and tool rows, a drawn NOW) leave; a stage line stays whole.
 const textOf=r=>r.map(([t])=>t).join('');
 const whoHead=r=>{const t=textOf(r),m=/^(│ +(?:↳ )?\S+ (?:owner|worker|frame-alt|frame)\b)/.exec(t);if(!m)return null;
  const out=[];let n=0;const want=[...m[1]].length;
  for(const [t2,st] of r)for(const ch of t2){if(n>=want)return out;n++;const last=out.at(-1);if(last&&last[1]===st)last[0]+=ch;else out.push([ch,st]);}
  return out;};
 const stageLine=r=>/^│ +(?:staged )?\d+\/\d+: /.test(textOf(r));
 const sessionExtra=r=>stageLine(r)||!whoHead(r)&&/^│ {5,}\S/.test(textOf(r));   // indented four or more under the box's own edge: a gauge, a tool, a NOW (and the stage row, now on the first line)
 const sessions=(rows,hs)=>{const keep=rows.map(r=>!sessionExtra(r)),at=keep.map((k,i)=>k?i:-1).filter(i=>i>=0);
  return {rows:at.map(i=>whoHead(rows[i])||rows[i]),hits:hs.filter(h=>keep[h[0]]).map(h=>[at.indexOf(h[0]),...h.slice(1)])};};
 // 11-18: a route's time stands at the right end of its first line (the kind line), no row of its own: the golden's time-only row
 // leaves with the other session rows (above), and the time on a drawn kind line is blanked to the padding the golden has there.
 const kindLine=/^│ \S (?:[a-z-]+(?: ✓)? → )*[a-z-]+(?:\([^)]*\))?(?: · \S.*?)? +(?:\d+h )?\d+m │$/;
 // 11-18 (review C3): the stage text (` · 3/6: execute`) stands on that first line too, and no stage row apart; the golden has it on
 // a row of its own, which leaves with the session rows above, so on the drawn first line the stage text is blanked to padding as well.
 const stageMid=/^(│ \S (?:[a-z-]+(?: ✓)? → )*[a-z-]+(?:\([^)]*\))?)( · (?:[a-z-]+ )?\d+\/\d+(?:: \S.*?)?)( +(?:\d+h )?\d+m │)$/;
 const blankStage=r=>{const m=stageMid.exec(textOf(r));if(!m)return r;
  const from=[...m[1]].length,to=from+[...m[2]].length,pad=r.find(([t])=>/^ +$/.test(t))?.[1]??r.at(-1)[1],out=[];let n=0;
  for(const [t,st] of r)for(const ch of t){const blank=n>=from&&n<to;n++;const style=blank?pad:st,c=blank?' ':ch,last=out.at(-1);if(last&&last[1]===style)last[0]+=c;else out.push([c,style]);}
  return out;};
 const blankTime=rows=>rows.map(blankStage).map(r=>kindLine.test(textOf(r))?r.map(([t,st],i)=>[i>0&&/^ +(?:\d+h )?\d+m$/.test(t)?' '.repeat(t.length):t,st]):r);
 const unzone=(rows,hs)=>{const keep=rows.map(r=>!zoneRule(r)),at=keep.map((k,i)=>k?i:-1).filter(i=>i>=0);
  return {rows:at.map(i=>rows[i]),hits:hs.filter(h=>keep[h[0]]).map(h=>[at.indexOf(h[0]),...h.slice(1)])};};
 const mine=(rows,hs)=>{const own=new Set(hs.filter(h=>h[3]==='popup:MESSAGES').map(h=>h[0]));
  const keep=rows.map((r,i)=>i>0&&i<rows.length-1&&!own.has(i)&&!txt(r.map(([t])=>({text:t}))).includes('├'));
  const at=keep.map((k,i)=>k?i:-1).filter(i=>i>=0);
  return {rows:at.map(i=>rows[i]),hits:hs.filter(h=>keep[h[0]]).map(h=>[at.indexOf(h[0]),...h.slice(1)])};};
 // 11-18: under the native area the command column (`>_ Edit`) is gone and the title takes its room, so a golden bottom row that
 // has one is not compared cell for cell: the drawn box must carry no command and keep the golden's routes (their titles lead the rows).
 const goldenTool=!c.side&&want.some(r=>textOf(r).includes('>_ '));
 if(goldenTool){
  const rowsText=v.rows.slice(b.a,b.b+1).map(txt).join('\n');
  assert(!rowsText.includes('>_'),label+'\n'+rowsText);noTool++;
 }else if(!drawn){
  const own=unzone(blankTime(v.rows.slice(b.a,b.b+1).map(runs).map((r,i)=>i?r:unarrow(r))),hits),got=sessions(own.rows,own.hits),exp=sessions(want,c.hits);
  // A box with no requests folds its routes one step sooner as well where the zone's rule row now takes one of its rows (the
  // rule stays while a row of its zone shows): then it draws fewer rows than the golden and the golden's rows no longer apply.
  if(got.rows.length<exp.rows.length&&c.side&&c.height<=16)soloFolded++;
  else{
   assert.deepEqual(got.rows,exp.rows,label+'\n'+v.rows.slice(b.a,b.b+1).map(txt).join('\n'));
   assert.deepEqual(got.hits,exp.hits,label);soloSame++;
  }
 }else{
  const own0=unzone(blankTime(v.rows.slice(b.a,b.b+1).map(runs).map((r,i)=>i?r:unarrow(r)).map(r=>r.map(([t,s])=>[t,s]))),hits),own=sessions(own0.rows,own0.hits),oldSide=sessions(want,c.hits),got=mine(own.rows,own.hits),exp=mine(oldSide.rows,oldSide.hits);
  // The requests now take more rows than the one row v4.36.4 gave each, so a short box folds its routes one step
  // sooner: where it still draws the routes in the same form, they are v4.36.4's rows cell for cell (and their
  // press cells); where it folds them, the golden's rows no longer apply (the fold's own tests are below).
  if(got.rows.length===exp.rows.length&&JSON.stringify(got.rows.map(r=>r.length))===JSON.stringify(exp.rows.map(r=>r.length))){
   assert.deepEqual(got.rows,exp.rows,label+'\n'+v.rows.slice(b.a,b.b+1).map(txt).join('\n'));
   assert.deepEqual(got.hits,exp.hits,label);same++;
  }else folded++;
 }
 n++;
}
console.log('cases',n,JSON.stringify({same,folded,soloSame,soloFolded,noTool}));assert(same>=8&&folded<=4&&same+folded+noTool>=17&&n===68,JSON.stringify({same,folded,noTool}));
assert(soloSame>=35&&soloFolded<=3&&n===same+folded+soloSame+soloFolded+noTool,JSON.stringify({soloSame,soloFolded,noTool}));
""")
        self.assertIn("cases 68", out)

    def test_a_press_on_the_route_box_opens_the_route_popup(self):
        # Item 2: any row of a route opens the route popup with that route unfolded, the top line the
        # route popup, a request row the MESSAGES popup (where a MESSAGES request row led), the bottom
        # line nothing; the host opens those popups (nothing in place), and the wheel over the box
        # scrolls it in place while it hides routes.
        self.node(self.HEAD + r"""
const scene=golden.scenes.na;
for(const [side,width,height] of [[true,40,60],[true,54,46],[false,120,20]]){
 const v=pv.render({width,height,side,fill:side,model:scene,state:'working',configuration:{}});
 const b=boxOf(v),rows=v.rows.map(txt),label=side+' '+width+'\n'+rows.join('\n');
 const act=i=>v.hits.filter(h=>h.row===i).map(h=>h.action);
 assert.deepEqual(act(b.a),['popup:ROUTE'],label);assert.deepEqual(act(b.b),[],label);
 for(let i=b.a+1;i<b.b;i++){
  const a=act(i);
  // (11-17: the named zone rules (`─ dispatch ───`) have no press of their own; a request is three rows, all of them the request's, under the `request` rule.)
  const rule=s=>/^│ │ ─ (?:direct|dispatch|request) ─+ │ │$/.test(s),requestAt=rows.findIndex(s=>/^│ │ ─ request ─+ │ │$/.test(s));
  if(rule(rows[i])){assert.equal(a.length,0,i+label);continue;}
  assert.equal(a.length,1,i+label);
  if(requestAt>=0&&i>requestAt)assert.equal(a[0],'popup:MESSAGES',i+label);
  else assert(['popup:ROUTE:rt-1111111111111111','popup:ROUTE:rt-2222222222222222'].includes(a[0]),i+' '+a[0]+label);
 }
 // Each route's rows, from its first to its last (the workers' box with it), open that route.
 const first=id=>v.hits.find(h=>h.action==='popup:ROUTE:'+id).row,last=id=>v.hits.filter(h=>h.action==='popup:ROUTE:'+id).at(-1).row;
 assert(first('rt-1111111111111111')===b.a+2&&last('rt-1111111111111111')<first('rt-2222222222222222'),label);
 assert(v.hits.filter(h=>h.action==='popup:MESSAGES'&&h.row>b.a&&h.row<b.b).length>=2,label);
 assert(rows.slice(b.a+1,b.b).filter(s=>/^│ │ [→◂✓✕] \[/.test(s)).length===2,label);
 // Nothing opens in place any more.
 assert(!v.hits.some(h=>/^(route|request|message|read)[:-]|^route$/.test(h.action)),JSON.stringify(v.hits));
}
// The host: a press is a panel action; it opens the popup (the routes table with that route unfolded,
// the MESSAGES popup), and the wheel over the route box moves its window.
const src=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const section=(start,end)=>{const a=src.indexOf(start),b=src.indexOf(end,a+start.length);assert(a>=0&&b>a,start);return src.slice(a,b);};
const vm=require('node:vm');
const opened=[];let queued=0;
const context={panelView:pv,settings:undefined,epoch:0,empty:true,SECTIONS:['QUESTIONS','ROUTE','LAB','SESSIONS','MESSAGES'],
 openPopup:(name,open='')=>opened.push([name,open]),openSettings(){},foldByHand(){},applyLayout(){},applySettings(){},cancelSettings(){},previewSettings(){},queueDisplay(){queued++;},
 fleetView:undefined,paste:false,relay:false,dragging:false,geometry:()=>({side:true,panelColumn:93,left:false,folded:false}),panelRow:()=>0,height:()=>30,panelColumns:()=>40,
 terminal:{modes:{mouseTrackingMode:'none'}},panelHitsPlacement:'shown',placement:()=>'shown',panelHits:[],routeArea:null,routeScroll:0,cols:133};
vm.createContext(context);
vm.runInContext(section('function panelAction(','let settingsTail=')+section('function panelMouse(','function displayReporter(')
 +'\nthis.action=panelAction;this.mouse=panelMouse;this.get=()=>({routeScroll,routeArea});this.set=(a,s)=>{routeArea=a;routeScroll=s;};this.hits=h=>{panelHits=h;};',context);
context.action('popup:ROUTE:rt-2222222222222222');context.action('popup:ROUTE');context.action('popup:MESSAGES');
assert.deepEqual(opened,[['ROUTE','rt-2222222222222222'],['ROUTE',''],['MESSAGES','']]);
// A press on a box row reaches the same action through the panel's own hit (row 5, column 10 of the panel).
context.hits([{row:5,left:2,right:37,action:'popup:ROUTE:rt-1111111111111111'}]);context.mouse(0,93+1+10,5+1,false);
assert.deepEqual(opened.at(-1),['ROUTE','rt-1111111111111111']);
// The wheel over the box scrolls it (down, then up, never past its ends); outside it, nothing.
context.set({top:4,bottom:12,offset:0,max:2,step:2},0);
context.mouse(65,93+1+10,6,false);assert.equal(context.get().routeScroll,1);
context.mouse(65,93+1+10,6,false);context.mouse(65,93+1+10,6,false);assert.equal(context.get().routeScroll,2);
context.mouse(64,93+1+10,6,false);assert.equal(context.get().routeScroll,1);
context.mouse(65,93+1+10,20,false);assert.equal(context.get().routeScroll,1,'the wheel outside the box does nothing');
context.set({top:4,bottom:12,offset:0,max:0,step:3},0);context.mouse(65,93+1+10,6,false);assert.equal(context.get().routeScroll,0,'a box that shows every route keeps still');
// The panel paints the window the host keeps (its border counts what it hides).
const many={...golden.scenes.na,routes:Array.from({length:6},(_,i)=>({...golden.scenes.na.routes[1],id:'rt-'+String(i).repeat(16),title:'route '+i})),route_count:6,route_total:6};
const at=s=>pv.render({width:40,height:18,side:true,fill:true,model:many,state:'working',configuration:{},routeScroll:s});
const top=at(0),down=at(2);assert(top.routeArea&&top.routeArea.max>0,JSON.stringify(top.routeArea));
assert.notDeepEqual(top.rows.map(txt),down.rows.map(txt));assert(down.rows.map(txt).some(s=>/ ↑2 ›─╮/.test(s)),down.rows.map(txt).join('\n'));
assert.equal(down.routeArea.offset,2);
""")

    def test_the_requests_stand_once_in_the_route_box(self):
        # Item 3: the requests stand in the route box (after the routes, under the `request` rule), and
        # MESSAGES draws the messages alone (its `+N` counts messages); the MESSAGES popup still reads the
        # requests whole (their numbers, reports and body) before the messages.
        self.node(self.HEAD + r"""
const scene=golden.scenes.na;
for(const [side,width,height] of [[true,40,60],[true,32,46],[false,120,20]]){
 const v=pv.render({width,height,side,fill:side,model:scene,state:'working',configuration:{}});
 const rows=v.rows.map(txt),b=boxOf(v),label=side+' '+width+'\n'+rows.join('\n');
 // (11-17: the title counts the routes alone, the requests are the box's `request` zone, named by its rule.)
 assert(rows[b.a].includes(side?'╭─ ROUTE 2':'╭─ route 2')&&!/REQUEST|request \d/.test(rows[b.a]),label);
 // (Short of rows the rules go first: the 32-cell panel here draws none, the others the one `request` rule.)
 const rules=rows.slice(b.a+1,b.b).filter(s=>/^│ │ ─ request ─+ │ │$/.test(s)).length;
 assert(rules<=1&&(width!==40&&width!==120||rules===1),label);
 // The two request rows stand in the box, once each (`◂ [e7] 검토 요청…`, `→ [52] frames…`).
 const requestRow=s=>/^│ │ (◂ \[e7\]|→ \[52\])/.test(s);
 const inBox=rows.slice(b.a+1,b.b).filter(requestRow),outside=[...rows.slice(0,b.a),...rows.slice(b.b+1)].filter(requestRow);
 assert(inBox.length===2&&outside.length===0,label);
 const m=rows.findIndex(s=>/^│ {1,2}(MESSAGES|messages)/.test(s));
 // MESSAGES: the three messages, two rows each (head, body), no `+N` for the requests.
 assert(m>b.b&&/^│ (?: MESSAGES +|messages ─+ )› │$/.test(rows[m]),label);
 const ind=/^│  MESSAGES/.test(rows[m])?2:0;   // 11-25: beside the native area the head stands one cell in and an item two (3 cells after the `│`)
 assert.deepEqual([1,3,5].map(i=>rows[m+i].slice(2+ind,8+ind)),['← [e7]','→ [52]','← [e1]'],label);
}
const body=pv.messagesBody({model:scene,columns:72}).map(txt);
const first=body.findIndex(s=>s.includes('검토 요청: 팝업 바탕')),msg=body.findIndex(s=>s.includes('[e1]'));
assert(first===0&&body.some(s=>s.includes('본문 첫 줄'))&&msg>first,body.join('\n'));
""")

    def test_every_height_and_width_keeps_its_rows_inside_the_panel(self):
        # Item 4: heights 8-60 at widths 24-60, beside the native area and below it, with every section, a
        # route with its worker, a failed one, requests and a question: no row runs past the panel or over
        # another, the route box stays closed and whole (or gone), its hidden routes are counted on its
        # border, QUESTIONS keeps its head and first row, the last row (5h/7d, fold, gear) stands.
        self.node(self.HEAD + r"""
const s=golden.scenes,q={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('route 상자를 이대로 둘까요?')],[P('  '),P('1 네 · 2 아니오')]],one:[[P('◑ '),P('route 상자를 이대로 둘까요?')]],line:[P('route 상자를 이대로 둘까요?')],choices:2,answerable:true};
const model={...s.na,routes:[...s.na.routes,...s.da.routes],route_count:4,route_total:4,wait:'◑답 대기 1',answerable:true};
let seen=0,boxes=0,scrolled=0;
for(const side of [true,false])for(let height=8;height<=60;height+=side?1:4)for(let width=24;width<=60;width+=side?4:12){
 const w=side?width:width+60;
 const v=pv.render({width:w,height,side,fill:side,model,state:'working',configuration:{},brief:()=>q,frame:3});
 const rows=v.rows.map(txt),label=[side,height,w].join(' ')+'\n'+rows.join('\n');seen++;
 assert(rows.length<=height&&rows.every(r=>cells(r)===w),label);
 assert(v.hits.every(h=>h.row>0&&h.row<rows.length-1&&h.left>=0&&h.right<w),label);
 assert(/⚙   ▾ │$/.test(rows.at(-2)),label);
 if(height>=10&&w>=28){assert(rows.some(r=>/QUESTIONS/.test(r)),'the question head stays\n'+label);assert(rows.some(r=>r.includes('route 상자를')),'its first row stays\n'+label);}
 // (11-16: the QUESTIONS section is a thin box too: at most one box of its own, and the route box one of its own.)
 const tops=rows.filter(r=>/^│ ╭/.test(r)).length,bottoms=rows.filter(r=>/^│ ╰/.test(r)).length,routeTops=rows.filter(r=>/^│ ╭─ (?:ROUTE \d+|route)/.test(r)).length;
 assert(tops===bottoms&&routeTops<=1&&tops-routeTops<=1,label);
 const b=boxOf(v);
 if(b){boxes++;
  // Inside the box every row is the box's own (its side lines), and a press there opens a popup of its routes or requests.
  for(let i=b.a+1;i<b.b;i++){assert(/^│ (│ .* │|├─+┤) │$/.test(rows[i]),i+'\n'+label);
   for(const h of v.hits.filter(h=>h.row===i))assert(/^popup:(ROUTE:rt-|MESSAGES$)/.test(h.action),h.action+'\n'+label);}
  const shown=new Set(v.hits.filter(h=>h.row>b.a&&h.row<b.b).map(h=>h.action).filter(a=>a.startsWith('popup:ROUTE:'))).size;
  const hidden=(rows.slice(b.a,b.b+1).join('').match(/[↑↓](\d+) ─/g)||[]).reduce((n,m)=>n+Number(m.slice(1,-2)),0);
  if(hidden)scrolled++;
  // Routes and requests shown plus those the border counts are all of them.
  const requests=rows.slice(b.a+1,b.b).filter(s=>/^│ │ [→◂✓✕] \[/.test(s)).length;
  assert.equal(shown+requests+hidden,6,label);
  if(v.routeArea)assert(v.routeArea.top===b.a&&v.routeArea.bottom===b.b,JSON.stringify(v.routeArea)+label);
 }
}
assert(boxes>100&&scrolled>10&&seen>400,JSON.stringify({seen,boxes,scrolled}));
// The folded strip stays as it is: one row of routes (`4 ROUTE`), no box.
const strip=pv.render({width:90,height:4,folded:true,model,state:'working',configuration:{},brief:()=>q}).rows.map(txt);
assert(!strip.join('').includes('╭')&&strip.some(r=>/4 ROUTE/.test(r)),strip.join('\n'));
""")

    def test_a_popup_stands_on_its_own_ground_with_bright_words(self):
        # Item 5: every cell of a popup's frame (question, table, MESSAGES) stands on its face's `popup`
        # ground, its line in `popupLine`, its plain words `bright`, a word drawn dim or gray never fainter
        # than `popupFloor` (and well clear of the veil's gray); a hue keeps its color at full strength (no
        # dim). The empty cells round it keep the terminal's own. Mono, eight colors and NO_COLOR: no ground,
        # the line bright, as before.
        self.node(self.HEAD + r"""
const F={dark:pv.FACES.dark,light:pv.FACES.light};
// The constants: a ground off the terminal's own, a line and words brighter than the veil, a floor well above it.
const level=n=>8+10*(n-232);
// (11-17: the line is a theme step now, not a gray: it only has to stand off the ground and off the veil; its contrast
// is measured in `session_shell_popup_ground_1117.test.py`.)
assert(F.dark.popup>=235&&F.dark.popup<=238&&level(F.dark.popupFloor)-level(F.dark.veil)>=60&&F.dark.popupLine!==F.dark.popup,JSON.stringify(F.dark));
assert(F.light.popup>=252&&F.light.popup<=254&&level(F.light.veil)-level(F.light.popupFloor)>=60,JSON.stringify(F.light));
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},g_dead:{foreground:217,bold:true},hb_claude:{foreground:116},soft:{foreground:253}};
// Fleet's table cells as the backend sends them: plain, dim, a gray, a hue, a dim hue, a bold dim.
const table=[[{text:'STATE',dim:true},{text:'  ROUTE  ',foreground:-1},{text:'AGE',foreground:244}],
 [{text:'✕',foreground:217,bold:true},{text:' 휠 입력 시험 '},{text:'54m',foreground:242},{text:' code ',dim:true,bold:true}],
 [{text:'⠋',foreground:230},{text:' 팝업 바탕 ',foreground:253},{text:'claude',foreground:116},{text:'cc',foreground:173},{text:' 대기',foreground:150,dim:true}]];
const question=[[{text:'◑ ',role:'accent'},{text:'route 상자를 이대로 둘까요?'}],[{text:'  '},{text:'▸',foreground:116,bold:true},{text:' 1 네',foreground:253,bold:true}],[{text:'    2 아니오',dim:true}],[{text:'↑↓ · Enter 보내기 · Esc 닫기',dim:true}]];
const messages=pv.messagesBody({model:{styles,requests:[{id:'rq-a',glyph:'◂',who:'[e7]',title:'검토 요청',status:'열림',details:[['전달','전달됨','dim']],body:['본문']}],
 messages:[{way:'←',who:'[e1]',harness:'claude',when:'08:10',text:'CI 통과'}]},columns:60});
const gray=f=>Number.isInteger(f)&&f>=232&&f<=255;
for(const [name,body,kind] of [['question',question,'text'],['table',table,'table'],['messages',messages,'text']])
 for(const [cfg,background,face] of [[{},'','dark'],[{},'dark','dark'],[{theme:'light'},'','light'],[{},'light','light']]){
  const r=pv.popup({cols:120,rows:36,kind,title:[{text:'3 ',foreground:217},{text:'QUESTIONS',dim:true}],body,sections:['QUESTIONS','ROUTE','MESSAGES'],current:0,model:{styles,colors:256},configuration:cfg,background});
  const f=F[face],label=name+' '+JSON.stringify(cfg)+' '+background;
  const frame=r.rows.slice(r.frame.top-r.top,r.frame.top-r.top+r.frame.height);
  const lead=r.frame.left>r.left?1:0;
  for(const row of frame){
   const parts=row.slice(lead,row.length-(r.width>r.frame.width+lead?1:0));
   for(const p of parts){
    assert(Number.isInteger(p.background),label+' a cell without ground '+JSON.stringify(p));
    assert(p.dim!==true,label+' a dim cell '+JSON.stringify(p));
    if(gray(p.foreground)){const l=level(p.foreground),floor=level(f.popupFloor);assert(face==='dark'?l>=floor:l<=floor,label+' fainter than the floor '+JSON.stringify(p));}
    assert(p.foreground!==null&&p.foreground!==undefined&&p.foreground!==-1,label+' the terminal\'s own color '+JSON.stringify(p));
   }
   assert(parts.every(p=>p.background===f.popup),label+' '+JSON.stringify(parts.filter(p=>p.background!==f.popup)));
  }
  // The line and the title are bright; the plain words the brightest; hues as they were.
  const top=frame[0];assert(top.filter(p=>/[╭╮─]/.test(p.text)).every(p=>p.foreground===f.popupLine),label);
  assert.equal(top.find(p=>p.text.includes('QUESTIONS')).foreground,f.popupTitle,label);
  assert.equal(top.find(p=>p.text==='3 ').foreground,face==='light'?88:217,label);
  if(name==='table'){const all=frame.flat();assert.equal(all.find(p=>p.text.includes('휠 입력')).foreground,f.bright);
   assert.equal(all.find(p=>p.text==='claude').foreground,face==='light'?23:116);
   // 11-18: Claude's harness orange 173 has its own deep pair (166) on the light face; the teal keeps its own.
   assert.equal(all.find(p=>p.text==='cc').foreground,face==='light'?166:173);const d=all.find(p=>p.text===' 대기');assert(d.dim===false&&d.foreground===(face==='light'?22:150),label);}
  if(name==='question'){const all=frame.flat();assert.equal(all.find(p=>p.text.startsWith('route 상자')).foreground,f.bright);assert.equal(all.find(p=>p.text==='◑ ').foreground,f.accent);}
  // The empty cells round the frame keep the terminal's own (no ground).
  for(const row of [r.rows[0],r.rows.at(-1)])if(r.top<r.frame.top)assert(row.every(p=>p.background===undefined),label);
 }
// Mono, eight colors and NO_COLOR: no ground anywhere, the line bright (never dim).
for(const [m,cfg] of [[{colors:8},{}],[{colors:256,no_color:true},{}],[{colors:256},{theme:'mono'}]]){
 const r=pv.popup({cols:120,rows:36,kind:'table',title:[{text:'ROUTE',dim:true}],body:table,sections:['ROUTE'],current:0,model:{styles,...m},configuration:cfg});
 assert(r.rows.flat().every(p=>p.background===undefined),JSON.stringify(m));
 assert(r.rows.flat().filter(p=>/[╭╮╰╯│─]/.test(p.text)).every(p=>p.dim!==true),JSON.stringify(m));
}
// The panel behind it is unchanged by the popup (the veil is the host's), and its rows carry no popup ground.
const v=pv.render({width:40,height:30,side:true,model:golden.scenes.na,state:'working',configuration:{},background:'dark'});
assert(v.rows.flat().every(p=>p.background!==pv.FACES.dark.popup),'no popup ground in the panel');
""")


if __name__ == "__main__":
    unittest.main()
