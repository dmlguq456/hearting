"""11-16, the review of PR #1662: three small corrections of the same kind and the two looser last-line finders.

1. The folded strip keeps a window at Fleet's red level (the old notice row's `⚠ 5h 92%`), the other windows go first as
   before, and the answer wait and the routes are never pushed out by it.
2. (Fleet side: `tools/fleet/tests/test_direct_route_panel.py`, `utilities/session_tidy_idle.test.py`.)
3. Codex's footer row is found only in the footer layout (exactly the status row and the help row after the composer,
   or the one thread-id status row); OpenCode's bottom bar only with its `ctrl+p commands` row.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const shell=process.argv[1];
const view=require(path.join(shell,'panel-view.cjs')),display=require(path.join(shell,'native-display.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},hb_claude:{foreground:116}};
const t=(text,key='dim',extra={})=>({text,...(styles[key]||{dim:key==='dim'}),...extra});
const red=text=>t(text,'lvl_r',{level:'red'});     // what the backend marks a window at Fleet's red level
// `5h`, `7d` and the model window as the last row showed them; each one red or not.
const quota=(five,week,fable)=>[t('5h '),five?red('92%'):t('12%','lvl_g'),t('(↻ 2h)'),t(' │ '),t('7d '),week?red('95%'):t('66%','lvl_y'),t('(↻ 5d)'),
  t(' │ '),t('fable '),fable?red('97%'):t('3%','lvl_g')];
const model=o=>({styles,colors:256,tag:'[e9]',repo_side:[t('hearting','loc_repo')],git_side:[t('main')],selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],
  context_side:[t('━━━━','lvl_g'),t('────'),t(' 58%')],quota:quota(...(o.red||[false,false,false])),...(o.wait?{wait:'◑답 대기 1'}:{}),
  ...(o.warning?{warning:'압축 임박'}:{}),route_label:o.route||'route 1',route_count:1,route_total:1,routes:[],
  messages:[{way:'←',who:'[e7]',kind:'reply',text:'x',body:'x'},{way:'→',who:'[73]',kind:'reply',text:'x',body:'x'}]});
const strip=(width,o)=>str(view.render({width,height:3,side:false,folded:true,model:model(o),state:'working',harness:'claude',frame:1,sent:()=>[],
  cellWidth:cells,configuration:{}}).rows[0]);
"""


class FixPr1662Test(unittest.TestCase):
    def node(self, body, *args):
        result = subprocess.run(["node", "-e", HEAD + body, str(SHELL), *args], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    # 1 ---------------------------------------------------------------------------------------------------------
    def test_a_red_window_stays_on_the_folded_strip_in_a_narrow_window(self):
        self.node(r"""
// Width 80 (the review's case): the red 5h stays, with and without an answer wait; the calm windows still go first.
assert(/5h 92%/.test(strip(80,{red:[true,false,false]})),strip(80,{red:[true,false,false]}));
assert(/5h 92%/.test(strip(80,{red:[true,false,false],wait:true})),strip(80,{red:[true,false,false],wait:true}));
assert(!/5h|7d|fable/.test(strip(80,{red:[false,false,false]})),strip(80,{}));          // nothing red: as before
assert(!/7d 66%|fable 3%/.test(strip(80,{red:[true,false,false]})));                    // a calm window does not ride with it
// The model window (`fable 97%`) is a window too.
assert(/fable 97%/.test(strip(80,{red:[false,false,true],wait:true})),strip(80,{red:[false,false,true],wait:true}));
// Over widths 60..160 the answer wait and `N ROUTE` are always whole; a lone red window (the 5h) is there from 70.
for(let w=60;w<=160;w+=2){
  const s=strip(w,{red:[true,false,false],wait:true,route:'route 3 ✕1'});
  assert(/◑답 대기 1/.test(s)&&/3 ROUTE ✕1/.test(s),w+' '+s);
  if(w>=70)assert(/5h 92%/.test(s),w+' '+s);
  assert(cells(s)<=w,w+' '+cells(s));
}
// Several red windows go the last first (the model window, 7d, then the 5h); the wait and the routes stay.
const all=w=>strip(w,{red:[true,true,true],wait:true,route:'route 3 ✕1'});
assert(/5h 92%.*7d 95%.*fable 97%/.test(all(100)),all(100));
assert(/5h 92%.*7d 95%/.test(all(80))&&!/fable/.test(all(80)),all(80));
assert(/5h 92%/.test(all(70))&&!/7d|fable/.test(all(70)),all(70));
assert(!/5h|7d|fable/.test(all(60))&&/◑답 대기 1/.test(all(60))&&/3 ROUTE ✕1/.test(all(60)),all(60));
// A compaction notice outranks it (the old row's order): it is the red window that goes first.
const both=strip(70,{red:[true,false,false],warning:true,wait:true});
assert(/압축 임박|압축/.test(both),both);
""")

    def test_the_projection_marks_only_red_readings(self):
        import human_wait_state
        import session_shell as shell
        import session_tidy
        shell.registry_root()
        values = {"cwd": str(ROOT), "ctx_pct": 20, "quota_windows": [("5h", 92, None), ("7d", 66, None)],
                  "model_quotas": [("fable", 97)]}
        with mock.patch.object(shell, "_observe_session", return_value=(values, [])), \
                mock.patch.object(session_tidy, "prompt_display", return_value={}), \
                mock.patch.object(human_wait_state, "cached_view", return_value={"items": [], "by_session": {}}), \
                mock.patch.object(human_wait_state, "cached_statusline", return_value=((0, 0), "")):
            panel = shell.panel_projection("claude", "00000000-0000-4000-8000-000000001662", native_pid=os.getpid(),
                                           native_start=None, columns=40, rows=40)["panel"]
        marked = {part["text"]: part.get("level") for part in panel["quota"]}
        self.assertEqual((marked["92%"], marked["66%"], marked["97%"]), ("red", None, "red"), marked)
        self.assertEqual([p["text"] for p in panel["quota"] if p.get("level") == "red"], ["92%", "97%"])
        # The folded strip drawn from this very projection keeps the red 5h in a 50-cell window.
        script = r"""
const view=require(process.argv[1]),panel=JSON.parse(process.argv[2]);
const v=view.render({width:50,height:3,side:false,folded:true,model:panel,configuration:{},questions:()=>[]});
process.stdout.write(v.rows[0].map(s=>s.text).join(''));
"""
        shown = subprocess.run(["node", "-e", script, str(SHELL / "panel-view.cjs"), json.dumps(panel, ensure_ascii=False)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("5h 92%", shown.stdout)
        self.assertNotIn("7d 66%", shown.stdout)

    # 3 ---------------------------------------------------------------------------------------------------------
    def test_codex_covers_only_the_footer_layout(self):
        self.node(r"""
const uuid='019e1a2b-1111-2222-3333-444444444444',rows=l=>display.footerRows('codex',l);
const help='  ? for shortcuts',status='  gpt-5 xhigh · ~/proj',idRow=`  ${uuid} · gpt-5 xhigh · Context 5% used`;
// What stays covered: the status row above the help row, the composer's blank row between, a draft of several lines,
// the copy help, the id status row without a help row (a draft typed, Codex 0.163), a very low window.
assert.deepEqual(rows(['','› hi','',idRow,help]),[3]);
assert.deepEqual(rows(['› hi',status,help]),[1]);
assert.deepEqual(rows(['› first line','  second line','',idRow,help]),[3]);
assert.deepEqual(rows(['› draft',idRow]),[1]);
assert.deepEqual(rows(['› first line','  second line','',idRow]),[3]);
assert.deepEqual(rows(['› hi','',status,'  ctrl+c copy · enter copy & follow · esc clear']),[2]);
assert.deepEqual(rows(['› hi',idRow,help]),[1]);
// The review's loose shapes: a sign-led row above the help row is not the footer.
for(const sign of ['  ↳ queued follow-up','  ⏎ to send pasted text','  ■ stream error · retrying later','  • Working (3s · esc to interrupt)'])
  assert.deepEqual(rows(['› draft',sign,help]),[],sign);
// A draft's second line that begins with an id (no status row of that shape): not the footer either way.
assert.deepEqual(rows(['› first',`  ${uuid} is the thread I mean`,help]),[]);
assert.deepEqual(rows(['› first',`  ${uuid} is the thread I mean`]),[]);
assert.deepEqual(rows(['› first',`  ${uuid} is the thread I mean`,'  gpt-5 · Context 5% used']),[]);
// More or fewer rows than the layout after the composer's `›`: not found, so not covered.
assert.deepEqual(rows(['› draft','  ↳ queued',status,help]),[]);
assert.deepEqual(rows(['› draft',status]),[]);
assert.deepEqual(rows(['› draft','  just text']),[]);
assert.deepEqual(rows([status,help]),[]);                       // no composer on the screen
assert.deepEqual(display.footerRows('claude',['› hi',status,help]),[]);
""")

    def test_opencode_counts_a_path_row_only_with_its_command_bar(self):
        self.node(r"""
const rows=l=>display.footerRows('opencode',l);
// The bottom bar with its `ctrl+p commands`, and the folder row right above it, as before.
assert.deepEqual(rows(['  ┃ prompt text','','  12.3K (4%) $0.10 ctrl+p commands']),[2]);
assert.deepEqual(rows(['┃ prompt','~/proj/hearting:main      1 MCP  /status','  esc interrupt   ctrl+p commands']),[1,2]);
// The review's loose shapes, without the bar: a borderless `/models` input, a slash list, a conversation line that opens with a path.
assert.deepEqual(rows(['','  some answer','/models']),[]);
assert.deepEqual(rows(['/models   Switch model','/status   Show status','/help     Help']),[]);
assert.deepEqual(rows(['  text','~/proj/hearting/utilities/x.py  was changed','  more words']),[]);
assert.deepEqual(rows(['/tmp/build.log','/tmp/build.err','done']),[]);
// With the bar, only the row right above it can be the folder row; a path-led row further up is not it.
assert.deepEqual(rows(['/models   Switch model','  ┃ prompt','  ctrl+p commands']),[2]);
assert.deepEqual(rows(['~/a  b','~/proj  1 MCP','  ctrl+p commands']),[1,2]);
assert.deepEqual(rows(['a','b','c']),[]);
""")


if __name__ == "__main__":
    unittest.main()
