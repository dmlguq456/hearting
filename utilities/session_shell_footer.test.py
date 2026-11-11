"""Paint concealment needs a default config, exact SID, and all common fields."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
import codex_tui_config

class NativeFooterTest(unittest.TestCase):
 def test_managed_footer_late_resize_and_uncertain_frames_keep_the_original_vt(self):
  with tempfile.TemporaryDirectory(prefix='shell-footer-') as tmp:
   path=Path(tmp)/'config.toml'
   path.write_text(codex_tui_config.merge_status_line_text(''));path.chmod(0o600)
   config=codex_tui_config.managed_status_line_observation(tmp)
   self.assertIsNotNone(config)
   script=r"""const a=require('node:assert/strict'),fs=require('node:fs'),d=require(process.argv[1]);
const config=JSON.parse(process.argv[2]),sid='00000000-0000-4000-8000-000000000052';
const fields=[[sid],[sid,'Title'],['branch'],['Context 10% used'],['fixture high'],['5h 77% left'],['Weekly 40% left']];
const status='📁 project · [52] ● Title · '+sid+' · ⎇ branch · context ━───────── 10% · fixture high · 5h 23% · 7d 60%';
const plan={sid,config,fields,lines:[status]};
const footer='  '+[sid,'Title','branch','Context 10% used','fixture high','5h 77% left','Weekly 40% left'].join(' · ');
const make=(width,text)=>['body',text.slice(0,width).padEnd(width),'  ← for agents · ? for shortcuts'.padEnd(width),''];
const hidden=(rows,p=plan,painted=[status],selected=sid)=>[...d.hiddenFooterRows('codex',rows,selected,p,painted)];
a.deepEqual(hidden(make(410,'')),[]);
for(const width of [410,274,220,410]) {
 const rows=make(width,footer),original=JSON.stringify(rows);
 a.deepEqual(hidden(rows),[1]);a.equal(d.observe('codex',rows,sid).percent,10);
 a.equal(JSON.stringify(rows),original);
 a.deepEqual([...d.hiddenFooterRows('claude',rows,sid,plan,[status])],[]);
}
a.deepEqual(hidden(make(410,footer),{...plan,lines:['  '+status]},['  '+status]),[1]);
a.deepEqual(hidden(make(410,footer),{...plan,lines:['  '+status]},[status]),[]);
a.deepEqual(hidden(make(410,footer),plan,['HEARTING','Current work',status,'Session details']),[1]);
a.deepEqual(hidden(make(410,footer),{...plan,lines:[status,'more status']},[status,'a question','more status']),[]);
a.deepEqual(hidden(make(100,footer)),[]);
for(const value of [footer+' · extra',footer.replace('Title','different title'),footer.replace('10%','101%'),footer.replace('fixture high','fixture low'),footer.replace('Context','context')])
 a.deepEqual(hidden(make(410,value)),[]);
a.deepEqual(hidden([footer,'a normal body line','','']),[]);
a.deepEqual(hidden(make(410,footer),plan,[status],'different'),[]);
a.deepEqual(hidden(make(410,footer),{...plan,sid:'old-selection'}),[]);
a.deepEqual(hidden(make(410,footer),plan,[]),[]);
a.deepEqual(hidden(make(410,footer),{...plan,lines:[status,'more status']},[status]),[]);
a.deepEqual(hidden(make(410,footer),null),[]);
// The approved work panel keeps the full ID in same-session details rather
// than exposing it merely to prove duplicate paint. Native binding is intact.
const compact=['fixture · high','context 10% · 5h 23% · 7d 60%'];
const panel={...plan,presentation:'session-panel',lines:compact,detail_lines:[status]};
const inPanel=(painted=compact,details=[status],selected=sid)=>[...d.hiddenFooterRows('codex',make(410,footer),selected,panel,painted,details)];
a.deepEqual(inPanel(),[1]);
a.deepEqual(inPanel([compact[0],'Current task',compact[1]]),[1]);
a.deepEqual(inPanel(compact,[]),[]);
a.deepEqual(inPanel(compact,[status.replace(sid,'other-session')]),[]);
a.deepEqual(inPanel([compact[0]]),[]);
a.deepEqual(inPanel(compact,[status],'other-session'),[]);
// A user's two-item layout is preserved after the automatic CLI override is gone.
a.deepEqual(hidden(make(100,'  '+sid+' · Context 10% used'),null),[]);
// The native title's SID fallback carries no additional title information.
a.deepEqual(hidden(make(410,footer.replace('Title',sid))),[1]);
const noLimits=footer.split(' · ').slice(0,5).join(' · ');
a.deepEqual(hidden(make(410,noLimits),{...plan,fields:fields.slice(0,5).concat([null,null])}),[1]);
a.deepEqual(hidden(make(410,footer),{...plan,fields:fields.slice(0,5).concat([null,null])}),[]);
// Changing the preference without waiting for the backend invalidates paint permission.
fs.appendFileSync(config.path,'\n# changed\n');a.deepEqual(hidden(make(410,footer)),[]);
a.equal(d.blockHeight({harness:'claude',count:0,limit:10}),0);
a.equal(d.blockHeight({harness:'claude',count:1,limit:10}),1);
a.equal(d.blockHeight({harness:'claude',count:1,detail:4,limit:10}),5);
a.equal(d.blockHeight({harness:'claude',count:0,detail:2,limit:10}),2);
a.equal(d.blockHeight({harness:'codex',count:0,limit:10,loaded:false}),1);
"""
   run=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs'),json.dumps(config)],text=True,capture_output=True)
   self.assertEqual(run.returncode,0,run.stderr)

 def test_shell_owns_the_codex_thread_id_field_without_painting_it(self):
  with tempfile.TemporaryDirectory(prefix='shell-footer-id-') as tmp:
   for name,text in (('only','[tui]\nstatus_line = ["thread-id"]\n'),('full',codex_tui_config.merge_status_line_text(''))):
    (Path(tmp)/name).mkdir();path=Path(tmp)/name/'config.toml';path.write_text(text);path.chmod(0o600)
   only=codex_tui_config.managed_status_line_observation(Path(tmp)/'only')
   full=codex_tui_config.managed_status_line_observation(Path(tmp)/'full')
   script=r"""const a=require('node:assert/strict'),d=require(process.argv[1]);
const only=JSON.parse(process.argv[2]),full=JSON.parse(process.argv[3]),sid='00000000-0000-4000-8000-000000000052';
const make=(width,text)=>['body',text.slice(0,width).padEnd(width),'  ← for agents · ? for shortcuts'.padEnd(width),''];
const mask=(rows,plan,painted=[],details=[],selected=sid)=>{const m=d.footerConcealment('codex',rows,selected,plan,painted,details);return {rows:[...m.rows],cells:[...m.cells]};};
// An ID-only footer row is blanked although the panel paints no thread ID.
const idPlan={sid,config:only,common:false,fields:[[sid]],lines:[]};
a.deepEqual(mask(make(80,'  '+sid),idPlan),{rows:[1],cells:[]});
a.deepEqual(mask(make(133,'  '+sid),idPlan),{rows:[1],cells:[]});
// Exact current SID, the help row below it and an unchanged config stay required.
a.deepEqual(mask(make(80,'  '+sid),idPlan,[],[],'00000000-0000-4000-8000-000000000053'),{rows:[],cells:[]});
a.deepEqual(mask(['body','  '+sid,'not help',''],idPlan),{rows:[],cells:[]});
a.deepEqual(mask(make(80,'  '+sid),{...idPlan,config:{...only,stamp:['0']}}),{rows:[],cells:[]});
a.deepEqual(mask(make(80,'  '+sid),{...idPlan,config:{...only,items:['context-used','thread-id']}}),{rows:[],cells:[]});
a.deepEqual(mask(make(80,'  '+sid.slice(0,20)),idPlan),{rows:[],cells:[]});
a.deepEqual(mask(make(80,'  '+sid),null),{rows:[],cells:[]});
a.equal(d.hiddenFooterRows('claude',make(80,'  '+sid),sid,idPlan).size,0);
// With other fields the row stays, and only the leading ID cells are blanked,
// unless the panel painted every other value (the existing whole-row rule).
const footer='  '+[sid,'Title','branch','Context 10% used','fixture high'].join(' · ');
const partial={sid,config:full,common:false,fields:[[sid],null,null,null,null,null,null],lines:[]};
a.deepEqual(mask(make(133,footer),partial),{rows:[],cells:[[1,2+sid.length+3]]});
// An untitled thread's title repeats the ID, and a working thread's ID carries a spinner.
a.deepEqual(mask(make(133,'  '+sid+' · '+sid+' · Context 0%…'),partial),{rows:[],cells:[[1,2+2*(sid.length+3)]]});
a.deepEqual(mask(make(133,'  '+sid+' ⠏ · '+sid+' ⠏ · Contex…'),partial),{rows:[],cells:[[1,2+2*(sid.length+5)]]});
a.deepEqual(mask(make(80,'  '+sid+' · '+sid),idPlan),{rows:[1],cells:[]});
a.deepEqual(mask(make(80,'  '+sid+' ⠏'),idPlan),{rows:[1],cells:[]});
// Measured: with a typed draft Codex drops the help row; the composer anchors the footer.
const draft=['› OWNED_HUMAN_DRAFT','','  '+sid+' · Context 10% used','            ⚠ 1 warning · f2 to view'];
a.deepEqual(mask(draft,partial),{rows:[],cells:[[2,2+sid.length+3]]});
a.deepEqual(mask(['transcript','','  '+sid+' · Context 10% used','            ⚠ 1 warning · f2 to view'],partial),{rows:[],cells:[]});
// Measured at 80 columns: the default footer is clipped right after the ID.
a.deepEqual(mask(make(80,'  '+sid+' ·…'),partial),{rows:[1],cells:[]});
a.deepEqual(mask(make(80,'  '+sid+' · '+sid+' ·…'),partial),{rows:[1],cells:[]});
a.deepEqual(mask(make(80,'  '+sid+' · Fixture ti…'),partial),{rows:[],cells:[[1,2+sid.length+3]]});
a.deepEqual(mask(make(133,'  '+sid+' · 00000000-0000-4000-8000-000000000053 · Context 0%'),partial),{rows:[],cells:[[1,2+sid.length+3]]});
const compact=['fixture · high','context 10%'],status='📁 project · Title · ⎇ branch';
const painted={sid,config:full,common:true,presentation:'session-panel',fields:[[sid],[sid,'Title'],['branch'],['Context 10% used'],['fixture high'],null,null],lines:compact,detail_lines:[status]};
a.deepEqual(mask(make(133,footer),painted,compact,[status]),{rows:[1],cells:[]});
a.deepEqual(mask(make(133,footer),painted,[compact[0]],[status]),{rows:[],cells:[[1,2+sid.length+3]]});
a.deepEqual(mask(make(133,footer.replace('Title','Other')),painted,compact,[status]),{rows:[],cells:[[1,2+sid.length+3]]});
"""
   run=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs'),json.dumps(only),json.dumps(full)],text=True,capture_output=True)
   self.assertEqual(run.returncode,0,run.stderr)

if __name__=='__main__':unittest.main()
