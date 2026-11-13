"""The optional rail changes geometry, not session identity or input authority."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
// Spec rule 49 (d): the old `side_min_columns` threshold no longer decides; an open panel stands
// beside wherever it physically fits (48 native columns, 8 rows) and folds by the one judgment below.
assert.equal(d.layout({cols:131,rows:40,configuration}).side,true);
assert.equal(d.layout({cols:89,rows:40,configuration}).side,false);assert.equal(d.layout({cols:90,rows:40,configuration}).side,true);
assert.equal(d.layout({cols:180,rows:7,configuration}).side,false);
// Folded beside the native area: the eight-cell strip, one blank cell apart, the native takes the rest.
g=d.layout({cols:180,rows:40,configuration,expanded:false});
assert.deepEqual([g.side,g.folded,g.panelCols,g.nativeCols,g.panelColumn,g.nativeColumn,g.gap],[true,true,8,171,172,0,1]);assert.equal(d.STRIP,8);
g=d.layout({cols:180,rows:40,configuration:{...configuration,position:'left'},expanded:false});
assert.deepEqual([g.left,g.panelColumn,g.nativeColumn,g.nativeCols],[true,0,9,171]);
for(const configuration of [null,{}, {layout:'side',side_width:-1,side_min_columns:132}])
 assert.equal(d.layout({cols:100,rows:24,configuration}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,layout:'bottom'}}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,layout:'bottom'},expanded:false}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,side_width:48}}).nativeCols,130);
// The one judgment: 80 native columns beside the open panel, a hand's choice winning at its width.
assert.equal(d.NATIVE_MIN,80);
const f=(cols,choice=null,extra={})=>d.fold({cols,choice,configuration,...extra});
assert.equal(f(122).folded,false);assert.equal(f(121).folded,true);
assert.equal(f(121,null,{pinned:true}).folded,false);
assert.deepEqual(f(200,{folded:true}),{folded:true,choice:{folded:true}});
assert.deepEqual(f(100,{folded:false,cols:100}),{folded:false,choice:{folded:false,cols:100}});
assert.deepEqual(f(101,{folded:false,cols:100}),{folded:true,choice:null});
assert.equal(d.fold({cols:60,configuration:{...configuration,layout:'bottom'}}).folded,false);
'''
        result=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
