"""Conceal only the managed complete footer; retain the native observation."""
import subprocess,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class NativeFooterTest(unittest.TestCase):
 def test_exact_footer_late_resize_and_narrow_frames_keep_the_original_vt(self):
  script=r"""const a=require('node:assert/strict'),d=require(process.argv[1]);
const sid='00000000-0000-4000-8000-000000000052';
const footer='  '+sid+' · Context 10% used';
const make=(width,footer)=>['body',footer.padEnd(width),'  ? for shortcuts'.padEnd(width),''];
const late=make(100,'');a.deepEqual([...d.hiddenFooterRows('codex',late,sid)],[]);
for(const width of [100,80,274,100]) {
 const rows=make(width,footer),original=JSON.stringify(rows);
 a.deepEqual([...d.hiddenFooterRows('codex',rows,sid)],[1]);
 a.equal(d.observe('codex',rows,sid).percent,10);
 a.equal(JSON.stringify(rows),original);
 a.deepEqual([...d.hiddenFooterRows('claude',rows,sid)],[]);
}
for(const value of [footer.slice(0,46),footer+' · model',footer.replace(' · ',' | '),footer.replace('10%','101%'),footer.replace('Context','context')])
 a.deepEqual([...d.hiddenFooterRows('codex',make(100,value),sid)],[]);
a.deepEqual([...d.hiddenFooterRows('codex',[footer,'a normal body line','',''],sid)],[]);
a.deepEqual([...d.hiddenFooterRows('codex',make(100,footer),'different')],[]);
a.equal(d.blockHeight({harness:'claude',count:0,limit:10}),0);
a.equal(d.blockHeight({harness:'claude',count:1,limit:10}),1);
a.equal(d.blockHeight({harness:'claude',count:1,detail:4,limit:10}),5);
a.equal(d.blockHeight({harness:'claude',count:0,detail:2,limit:10}),2);
a.equal(d.blockHeight({harness:'codex',count:0,limit:10,loaded:false}),1);
"""
  p=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],text=True,capture_output=True)
  self.assertEqual(p.returncode,0,p.stderr)
if __name__=='__main__':unittest.main()
