"""Native screen observations are a display cache, never session/input authority."""
from pathlib import Path
import subprocess,unittest
ROOT=Path(__file__).resolve().parents[1]
class NativeDisplayTest(unittest.TestCase):
 def test_first_cached_value_does_not_wait_for_the_projection(self):
  source=ROOT/'utilities/session-shell/native-display.cjs'
  script="""const assert=require('node:assert/strict'),d=require(process.argv[1]);
const sid='00000000-0000-4000-8000-000000000052';
const n=d.observe('codex',['body','',sid+' · Context 0% used','? shortcuts'],sid);
assert.equal(n.percent,0);assert.equal(d.firstLine(n)[0].text,'context 0%');
assert.equal(d.observe('codex',['Context 99% used','body','',''],sid),null);
assert.equal(d.observe('codex',['','',sid+' · Context 0% used',''], 'other'),null);
"""
  p=subprocess.run(['node','-e',script,str(source)],text=True,capture_output=True)
  self.assertEqual(p.returncode,0,p.stderr)
 def test_opencode_uses_the_visible_sidebar_percentage(self):
  source=ROOT/'utilities/session-shell/native-display.cjs'
  script="""const assert=require('node:assert/strict'),d=require(process.argv[1]);
const lines=['assistant mentions 99% used',' '.repeat(80)+'Context',' '.repeat(80)+'20,010 tokens',' '.repeat(80)+'10% used'];
assert.equal(d.observe('opencode',lines,'exact').percent,10);
assert.equal(d.observe('opencode',['Context','20,010 tokens','10% used'],'exact'),null);
assert.equal(d.observe('opencode',[' '.repeat(80)+'Context',' '.repeat(80)+'20,010 tokens',' '.repeat(80)+'101% used'],'exact'),null);
"""
  p=subprocess.run(['node','-e',script,str(source)],text=True,capture_output=True)
  self.assertEqual(p.returncode,0,p.stderr)
if __name__=='__main__':unittest.main()
