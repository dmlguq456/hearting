"""The optional rail changes geometry, not session identity or input authority."""
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]

class SessionLayoutTest(unittest.TestCase):
    def test_geometry_defaults_resize_fallback_and_native_sidebar_are_bounded(self):
        script=r'''
const assert=require('node:assert/strict'),d=require(process.argv[1]);
const configuration={layout:'side',side_width:40,side_min_columns:132};
let g=d.layout({cols:180,rows:40,configuration});
assert.equal(g.side,true);assert.equal(g.nativeCols,138);assert.equal(g.panelCols,40);assert.equal(g.panelColumn,140);assert.equal(g.panelRow,0);
assert.equal(d.layout({cols:131,rows:40,configuration}).side,false);
assert.equal(d.layout({cols:132,rows:40,configuration}).side,true);
assert.equal(d.layout({cols:180,rows:40,configuration,expanded:false}).side,false);
for(const configuration of [null,{}, {layout:'side',side_width:-1,side_min_columns:132}, {layout:'side',side_width:80,side_min_columns:80}])
 assert.equal(d.layout({cols:100,rows:24,configuration}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,layout:'bottom'}}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,side_width:48}}).nativeCols,130);
'''
        result=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
