"""11-17 (popup ground): the popup's ground, line, title and chosen ground are Hearting's earth tones.

The values stand in `FACES` (`popup`, `popupLine`, `popupTitle`, `chosen` in 256 colors and `rgb` in 24-bit color) and
nothing else: this test names them here, apart from the code, so a change of design is a change of both. It runs the real
popup paint (question, table, MESSAGES) with synthetic values: each face (dark, light, an unknown one = dark as before)
and color count (256, 24-bit) gives the ground the table says; every word keeps its color and reads on the ground and on
the chosen ground (contrast floors, measured from the cells that come out); mono, eight colors and NO_COLOR have no
ground at all; the host writes a 24-bit ground as `48;2;r;g;b`. The terminal's own cells under each harness are in
`session_shell_popup.test.py` (scenario `ground`).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"

SCRIPT = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const pv=require(path.join(process.argv[1],'panel-view.cjs'));
// What each face and color count must paint (named here, not read from FACES): ground, line, title, chosen ground.
const WANT={
 dark:{256:{popup:235,line:137,title:255,chosen:52},24:{popup:'#29221e',line:'#786453',title:'#f1eae3',chosen:'#5a3a1e'}},
 light:{256:{popup:254,line:137,title:232,chosen:223},24:{popup:'#f1e7dc',line:'#9c8571',title:'#342a23',chosen:'#fecca4'}}};
const FACESET=[[{},'','dark'],[{},'dark','dark'],[{theme:'light'},'','light'],[{},'light','light']];
// The cells' colors as sRGB: a 256-color step by the xterm palette, a hex as it is.
const rgb=v=>{if(typeof v==='string'){const m=/^#(..)(..)(..)$/.exec(v);return m.slice(1).map(x=>parseInt(x,16));}
  if(v<16)return [[0,0,0],[128,0,0],[0,128,0],[128,128,0],[0,0,128],[128,0,128],[0,128,128],[192,192,192],[128,128,128],[255,0,0],[0,255,0],[255,255,0],[0,0,255],[255,0,255],[0,255,255],[255,255,255]][v];
  if(v>=232){const g=8+10*(v-232);return [g,g,g];}const n=v-16,s=[0,95,135,175,215,255];return [s[Math.floor(n/36)],s[Math.floor(n/6)%6],s[n%6]];};
const lum=c=>{const [r,g,b]=c.map(x=>{x/=255;return x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4;});return 0.2126*r+0.7152*g+0.0722*b;};
// The colour difference (CIE76, sRGB to Lab under D65): how far one colour stands from another.
const lab=c=>{const f=t=>t>216/24389?Math.cbrt(t):(24389/27*t+16)/116,[r,g,b]=rgb(c).map(x=>{x/=255;return x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4;});
  const X=(0.4124564*r+0.3575761*g+0.1804375*b)/0.95047,Y=0.2126729*r+0.7151522*g+0.072175*b,Z=(0.0193339*r+0.119192*g+0.9503041*b)/1.08883;
  return [116*f(Y)-16,500*(f(X)-f(Y)),200*(f(Y)-f(Z))];};
const delta=(a,b)=>{const x=lab(a),y=lab(b);return Math.hypot(x[0]-y[0],x[1]-y[1],x[2]-y[2]);};
const ratio=(a,b)=>{const x=lum(rgb(a)),y=lum(rgb(b));return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);};
// Every kind of word a popup carries: a plain one, a dim one, a gray, the question's rose, a failure, a caution, a
// success, the three harnesses, a stage, a steward, an orange model name; and the chosen choice's rows.
const styles={dim:{dim:true},soft:{foreground:253},lvl_g:{foreground:150,bold:true},g_dead:{foreground:217,bold:true},lvl_y:{foreground:186},
 hb_claude:{foreground:116},hb_codex:{foreground:176},hb_opencode:{foreground:147},tag_steward:{foreground:219},fam_astra:{foreground:137},stg:{foreground:230}};
const W=(text,key,more={})=>({text,...(styles[key]||{}),...more});
const body=[[W('PLAIN '),W('DIMW ','dim'),W('GRAYW ',null,{foreground:244}),{text:'ROSE ',role:'accent'},W('REDW ','g_dead'),W('YELW ','lvl_y'),W('GRNW ','lvl_g')],
 [W('HCL ','hb_claude'),W('HCX ','hb_codex'),W('HOC ','hb_opencode'),W('STW ','tag_steward'),W('ORG ','fam_astra'),W('STG ','stg')],
 [{text:'▸',foreground:116,bold:true,tint:true},W(' 1 CHOSEN ','soft',{bold:true,tint:true}),W('CDIM ','dim',{tint:true}),{text:'CROSE ',role:'accent',tint:true},W('CRED ','g_dead',{tint:true})],
 [W('after')]];
const kinds=[['question','text'],['table','table'],['messages','text']];
const draw=(colors,cfg,background,kind='text',extra={})=>pv.popup({cols:120,rows:30,kind,title:[{text:'3 ',foreground:217},{text:'QUESTIONS',dim:true}],body,
  sections:['QUESTIONS','ROUTE','MESSAGES'],current:0,model:{styles,colors,...extra},configuration:cfg,background});
const frameCells=r=>{const lead=r.frame.left>r.left?1:0,trail=r.width>r.frame.width+lead?1:0;
  return r.rows.slice(r.frame.top-r.top,r.frame.top-r.top+r.frame.height).map(row=>({all:row,inner:row.slice(lead,row.length-trail),margins:row.filter(p=>p.margin)}));};
const out={checked:0,low:[],ratios:[]};
for(const [name,kind] of kinds)for(const colors of [256,1<<24])for(const [cfg,background,face] of FACESET){
  const w=WANT[face][colors===256?256:24],label=[name,colors,face,JSON.stringify(cfg),background].join(' ');
  const r=draw(colors,cfg,background,kind),rows=frameCells(r);
  // The ground: every cell of every frame row (and the cell each side of it), the chosen row's cells the chosen ground.
  rows.forEach((row,i)=>{
    assert(row.all.length>0&&row.margins.length===2||r.frame.left===0,label);
    for(const p of row.all)assert(p.background===w.popup||p.background===w.chosen,label+' '+JSON.stringify(p));
    for(const p of row.margins)assert.equal(p.background,w.popup,label);
    assert(row.all.every(p=>p.reverse!==true),label);});
  const flat=rows.flatMap(x=>x.inner);
  // The line, the title.
  assert(flat.filter(p=>/^[╭╮╰╯│─ ]+$/.test(p.text)&&/[╭╮╰╯│─]/.test(p.text)).every(p=>p.foreground===w.line),label+' line');
  assert.equal(flat.find(p=>p.text==='QUESTIONS').foreground,w.title,label+' title');
  // The chosen row: its words on the chosen ground, the rest of the inside on the popup's.
  const chosen=flat.filter(p=>p.background===w.chosen);
  assert(chosen.length>=4&&chosen.some(p=>p.text.includes('CHOSEN')),label+' chosen');
  assert(flat.filter(p=>p.text.includes('PLAIN')).every(p=>p.background===w.popup),label+' plain');
  // Every word reads: its contrast against the ground it stands on (measured from the cells that came out).
  for(const p of flat){
    if(!/[A-Za-z]{3}/.test(p.text)||p.foreground===null||p.foreground===undefined)continue;
    const bg=p.background,k=ratio(p.foreground,bg),onChosen=bg===w.chosen;
    out.checked++;out.ratios.push({face,colors,word:p.text.trim().slice(0,8),on:onChosen?'chosen':'ground',ratio:+k.toFixed(2)});
    if(k<(onChosen?2.9:3.3))out.low.push([label,p.text,k]);}
  // The line stands off the ground, the title reads.
  out.line=out.line||{};out.line[face+colors]=+ratio(w.line,w.popup).toFixed(2);assert(ratio(w.line,w.popup)>=2.5,label+' line ratio');
  assert(ratio(w.title,w.popup)>=10,label+' title ratio');
  // The chosen row stands off the ground (the person: "질문 선지 선택 효과를 틴트로 해 명확하게"): 20 or more apart, as the
  // blue it replaced was (34.9 dark, 24.9 light), in 256 colors and in 24-bit.
  out.chosenGap=out.chosenGap||{};out.chosenGap[face+colors]=+delta(w.chosen,w.popup).toFixed(1);assert(delta(w.chosen,w.popup)>=20,label+' chosen against ground '+delta(w.chosen,w.popup));
}
assert.deepEqual(out.low,[],JSON.stringify(out.low));
assert(out.checked>200,out.checked);
// Where the panel shows no hue: no ground, no line color, the chosen cells reversed; the same for every kind.
const none=[[{colors:8},{}],[{colors:256,no_color:true},{}],[{colors:256},{theme:'mono'}],[{colors:1<<24,no_color:true},{}],[{colors:1<<24},{theme:'mono'}]];
for(const [name,kind] of kinds)for(const [model,cfg] of none){
  const r=pv.popup({cols:120,rows:30,kind,title:[{text:'T'}],body,sections:['QUESTIONS'],model:{styles,...model},configuration:cfg});
  const cells=r.rows.flat(),label=JSON.stringify([name,model,cfg]);
  assert(cells.every(p=>p.background===undefined&&p.margin===undefined),label+' a ground');
  assert(cells.filter(p=>p.text.includes('CHOSEN')).every(p=>p.reverse===true),label+' not reversed');
  assert(cells.filter(p=>p.text.includes('PLAIN')).every(p=>p.reverse!==true),label);
  assert.equal(pv.veil(cfg,{colors:model.colors,noColor:model.no_color===true}),null,label);}
// The face is not the unknown's alone: an unknown background paints the dark face's values (as it did).
{const a=draw(1<<24,{},''),b=draw(1<<24,{},'dark');assert.deepEqual(a.rows,b.rows);}
// The shade setting does not reach the popup (it never did): a medium shade paints the same cells.
{for(const shade of ['none','light','medium']){const r=draw(256,{shade},'dark');assert.deepEqual(r.rows,draw(256,{},'dark').rows,shade);}}
// The host writes the ground: a step as `48;5;n`, a hex as `48;2;r;g;b`, nothing as the terminal's own.
{const src=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8'),a=src.indexOf('function foregroundSgr('),b=src.indexOf('const TOKENS=');
 const ctx={};vm.createContext(ctx);vm.runInContext(src.slice(a,b)+'\nthis.attr=cellAttr;',ctx);
 assert.equal(ctx.attr({foreground:174,background:235}),'0;38;5;174;48;5;235');
 assert.equal(ctx.attr({foreground:'#786453',background:'#29221e',bold:true}),'0;38;2;120;100;83;48;2;41;34;30;1');
 assert.equal(ctx.attr({foreground:null}),'0;39;49');assert.equal(ctx.attr({background:'#zzzzzz'}),'0;39;49');}
// The values a design changes: the eight in each face, and no other popup value stands in the code.
for(const face of ['dark','light']){const F=pv.FACES[face],w=WANT[face];
  assert.deepEqual([F.popup,F.popupLine,F.popupTitle,F.chosen],[w[256].popup,w[256].line,w[256].title,w[256].chosen],face);
  assert.deepEqual([F.rgb.popup,F.rgb.popupLine,F.rgb.popupTitle,F.rgb.chosen],[w[24].popup,w[24].line,w[24].title,w[24].chosen],face);}
console.log(JSON.stringify({checked:out.checked,line:out.line,chosenGap:out.chosenGap,worst:out.ratios.sort((x,y)=>x.ratio-y.ratio).slice(0,6)}));
"""


class PopupGroundTest(unittest.TestCase):
    def test_the_popup_ground_each_face_and_color_count_and_every_word_reads_on_it(self):
        result = subprocess.run(["node", "-e", SCRIPT, str(SHELL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreater(summary["checked"], 200, summary)


if __name__ == "__main__":
    unittest.main()
