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
// Folded, in every placement: the strip under the native area, which keeps the whole width (spec rule 49 (c)).
g=d.layout({cols:180,rows:40,configuration,expanded:false,panelRows:d.foldRows(40)});
assert.deepEqual([g.side,g.folded,g.panelCols,g.nativeCols,g.nativeRows,g.panelColumn,g.nativeColumn,g.gap,g.panelRow],[false,true,180,180,36,0,0,0,null]);
g=d.layout({cols:180,rows:40,configuration:{...configuration,position:'left'},expanded:false,panelRows:d.foldRows(40)});
assert.deepEqual([g.left,g.panelColumn,g.nativeColumn,g.nativeCols],[false,0,0,180]);
for(const configuration of [null,{}, {layout:'side',side_width:-1,side_min_columns:132}])
 assert.equal(d.layout({cols:100,rows:24,configuration}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,layout:'bottom'}}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,layout:'bottom'},expanded:false}).side,false);
assert.equal(d.layout({cols:180,rows:40,configuration:{...configuration,side_width:48}}).nativeCols,130);
// The one judgment: 60 native columns beside the open panel, a hand's choice winning at every width.
assert.equal(d.NATIVE_MIN,60);
const f=(cols,choice=null,extra={})=>d.fold({cols,choice,configuration,...extra});
assert.equal(f(102).folded,false);assert.equal(f(101).folded,true);
assert.equal(f(101,null,{pinned:true}).folded,false);
assert.deepEqual(f(200,{folded:true}),{folded:true,choice:{folded:true}});
assert.deepEqual(f(100,{folded:false}),{folded:false,choice:{folded:false}});
assert.deepEqual(f(60,{folded:false}),{folded:false,choice:{folded:false}});
assert.equal(d.fold({cols:60,configuration:{...configuration,layout:'bottom'}}).folded,false);
'''
        result=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
