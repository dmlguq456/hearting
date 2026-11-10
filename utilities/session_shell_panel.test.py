"""The approved work panel preserves facts, editor receipts and private settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import session_shell as shell


class PanelTest(unittest.TestCase):
    def test_native_sidebar_disconnect_clears_stale_rows_and_notifies_fallback(self):
        for mode in ("error-close", "close-only"):
            with self.subTest(mode=mode):
                result = subprocess.run(["node", str(ROOT / "utilities/fixtures/session-shell-sidebar-disconnect.cjs"), str(ROOT), mode],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mouse_opened_editor_owns_keys_during_peer_delivery(self):
        result = subprocess.run(["node", str(ROOT / "utilities/fixtures/session-shell-delivery-focus.cjs"), str(ROOT)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_route_evidence_pages_remain_readable_without_question_effects(self):
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const lines=['route','why','owner','test','review','CI','gate','result','next'].map(label=>[label,label+' EVIDENCE '.repeat(10),'dim']);
const model={routes:[{title:'Actual route',state:'working',stages:[{name:'inline',state:'active'}],visibility:{summary:'direct',lines}}]};
for(const [width,height] of [[37,21],[80,14]]){
 const parts=[];let pages=1;
 for(let page=0;page<pages;page++){
  const v=view.render({width,height,side:width<80,routeDetail:true,routePage:page,model,questions:()=>{throw Error('detail cannot submit');}});
  pages=v.routePages;parts.push(v.rows.flat().map(p=>p.text).join(''));
  assert(v.rows.length<=height);assert(v.hits.some(h=>h.action==='route-next'));
 }
 assert(pages>1);for(const [label] of lines)assert(parts.join('').includes(label+': '+label));
}
'''
        self.node(script, "panel-view.cjs")

    def test_frame_is_panel_only_neutral_and_keeps_model_route_question_at_80(self):
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
for(const width of [37,40,80]){
 const model={model:[{text:'Native selected · high'}],title:'Preserve the approved task',answerable:true,
  routes:[{title:'Exact route',state:'working',stage:'verify',stages:[{name:'implement'},{name:'verify'}],reason:'Verify result'}]};
 const v=view.render({width,height:width===80?12:30,side:width<80,model,sid:'exact',configuration:{},
  questions:()=>[[{text:'Where should this result go?'}],[{text:'1 · Current project'}],[{text:'Ctrl+] answer'}]]});
 const text=v.rows.map(r=>r.map(s=>s.text).join(''));
 assert(text.some(s=>s.includes('Native selected')));assert(text.some(s=>s.includes('Exact route')));
 assert(text.some(s=>s.includes('Where should')));assert(text.some(s=>s.includes('implement → verify')));
 assert(!text.some(s=>s.includes('Results & connected work')));
 assert(text[0].startsWith('┌'));assert(text.at(-1).endsWith('┘'));
 assert(v.rows.every(r=>r.every(s=>s.background===undefined&&s.reverse!==true)));
 assert.equal(v.color,'lvl_y');assert(v.hits.some(h=>h.action==='settings'));
 assert(text.every(s=>s.length<=width));
}
const direct=view.render({width:40,height:24,side:true,model:{routes:[{title:'One step',state:'idle',stages:[{name:'inline'}]}]},questions:()=>[],configuration:{}});
const text=direct.rows.flat().map(s=>s.text).join('');assert(!text.includes('→'));assert.equal(direct.color,'dim');
const unknown=view.render({width:80,height:12,model:{},state:'blocked',questions:()=>[],configuration:{}});
assert.equal(unknown.color,'dim'); // Native permission state is not route failure.
'''
        self.node(script, "panel-view.cjs")

    def test_session_rows_are_always_painted_after_the_question_with_no_details_view(self):
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const session=[[{text:'📁 project · [23] ● Title'}],[{text:'⎇ topic ↑2 ↓1 🚧 3 dirty · 21:52'}],[{text:'>_ running: pytest'}]];
const model={model:[{text:'fixture · high'}],summary:[[{text:'context 42% · 5h 23% · 7d 60%'}]],session,
 routes:[{title:'Route',state:'working',stages:[{name:'one'}]}]};
const tail=[[{text:'✉ peer news'}],[{text:'› last input'}]];
const paint=(width,height,side,q)=>view.render({width,height,side,model,sid:'exact',configuration:{},sections:{tail},
 questions:o=>q.slice(0,o.budget)}).rows.map(r=>r.map(s=>s.text).join(''));
const question=[[{text:'Q head'}],[{text:'1 · yes'}],[{text:'2 · no'}],[{text:'Ctrl+] answer'}]];
for(const [width,height,side] of [[40,30,true],[80,14,false]]){
 const idle=paint(width,height,side,[]);
 for(const want of ['📁 project','⎇ topic','running: pytest','context 42%','peer news','last input'])
  assert(idle.some(s=>s.includes(want)),width+' missing '+want);
 assert(!idle.join('\n').includes('Session details'));assert(/^└─+┘$/.test(idle.at(-1)));
 // The summary stays at the bottom; session rows sit directly above it.
 assert(idle.at(-2).includes('context 42%'));assert(idle.at(-3).includes('running'));
 const asked=paint(width,height,side,question);
 for(const row of question)assert(asked.some(s=>s.includes(row[0].text)),'question row lost at '+width);
 assert(asked.some(s=>s.includes('context 42%')));
}
// A short bottom panel gives the question exactly the rows it had without
// session rows; those take only what is left.
for(const height of [8,10,12,14]){
 const short=paint(80,height,false,question);
 const bare=view.render({width:80,height,side:false,model:{...model,session:[]},sid:'exact',configuration:{},
  questions:o=>question.slice(0,o.budget)}).rows.map(r=>r.map(s=>s.text).join(''));
 const asked=rows=>rows.filter(s=>question.some(q=>s.includes(q[0].text)));
 assert.deepEqual(asked(short),asked(bare));
 assert(short.length<=height);
}
const r=view.render({width:40,height:30,side:true,model,sid:'exact',configuration:{},questions:()=>[]});
assert(!r.hits.some(h=>h.action==='details'));
'''
        self.node(script, "panel-view.cjs")

    def test_session_status_leaves_out_absent_values_and_painted_fields(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.session_panel import session_status
        text = lambda rows: "\n".join("".join(t for t, _k in row) for row in rows)
        values = {"harness": "claude", "tag": "[23]", "state": "idle", "title": "Title", "branch": "topic",
                  "git": {"repo": 1, "operation": "none", "ahead": 2, "behind": 0, "extra_worktrees": 0,
                          "observation": "fresh", "observed_label": "21:52"},
                  "model": "fixture", "effort": "high", "ctx_pct": 42, "quota_windows": [("5h", 23, None)],
                  "route_label": "⏳ 1 route", "answer_wait": 1, "jobs": "pytest"}
        painted = text(session_status(values, 36))
        for want in ("Title", "⎇ topic", "↑2", "fresh", "21:52", "running: pytest"):
            self.assertIn(want, painted)
        for already in ("fixture", "context", "5h", "route", "◑"):
            self.assertNotIn(already, painted)
        self.assertNotIn("?", text(session_status({"harness": "claude", "state": "idle"}, 36)))
        self.assertEqual(session_status({"harness": "claude", "state": "idle"}, 36), [])
        # Sessions are told apart by number and title; the panel paints no Codex thread ID.
        sid = "00000000-0000-4000-8000-000000000052"
        codex = text(session_status({**values, "harness": "codex", "native_sid": sid}, 36))
        self.assertIn("Title", codex)
        self.assertNotIn(sid, codex)
        self.assertNotIn(sid[:8], codex)

    def test_title_and_jobs_stay_on_screen_while_a_question_takes_the_session_rows(self):
        # Review [52] missing-field probe: a question at 80x14, its focused
        # editor, and OpenCode's 35-column sidebar used to drop every session
        # row, leaving the title and running jobs nowhere on screen.
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.session_panel import presentation
        values = {"harness": "claude", "tag": "[cb]", "state": "idle", "title": "REVIEW1463_TITLE",
                  "branch": "values-fixture*", "cwd": "/x/project", "model": "fixture", "effort": "high",
                  "ctx_pct": 15, "jobs": "REVIEW1463_JOB",
                  "git": {"repo": 1, "operation": "none", "ahead": 2, "behind": 1, "extra_worktrees": 3,
                          "observation": "fresh", "observed_label": "21:56:43"}}
        plain = lambda rows: [[{"text": text} for text, _key in row] for row in rows]
        models = {}
        for columns in (80, 35):
            panel = presentation(values, {}, "claude", "sid", columns)
            models[columns] = {**{key: plain(panel[key]) for key in ("summary", "session", "brief")},
                               "model": plain([panel["model"]])[0]}
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]),models=JSON.parse(process.argv[2]);
const question=['◑ 질문 1 · 1/1 · 답변 필요','REVIEW1463: 결과를 어디에 둘까요?','1 현재 프로젝트 (추천)','2 다른 폴더','Ctrl+] · 답변 필요','more','more 2'].map(text=>[{text}]);
const routes=[{title:'REVIEW1463_ROUTE',state:'blocked',stages:[{name:'one-shot',current:true}],visibility:{summary:'검증 ?'}}];
const paint=(width,height,side,focused,model,n=question.length)=>view.render({width,height,side,fill:false,focused,sid:'s',configuration:{},
 model:{...model,answerable:true,routes},sections:{tail:[[{text:'› last input'}]]},questions:o=>question.slice(0,Math.min(n,o.budget))})
 .rows.map(r=>r.map(p=>p.text).join(''));
const asked=rows=>rows.filter(s=>question.some(q=>s.includes(q[0].text)));
// A five-row question leaves the focused editor a spare row it never painted.
for(const [width,height,side,focused,n] of [[80,14,false,false],[80,14,false,true],[80,14,false,true,5],[35,21,true,false],[35,26,true,false],[80,10,false,false]]){
 const model=models[width===35?35:80],rows=paint(width,height,side,focused,model,n);
 for(const want of ['REVIEW1463_TITLE','REVIEW1463_JOB'])
  assert(rows.some(s=>s.includes(want)),`${width}x${height} focused=${focused} lost ${want}\n`+rows.join('\n'));
 if(width===80)assert(rows.some(s=>s.includes('values-fixture*')),'branch lost at 80');
 // The question keeps its rows and the panel does not grow for these values.
 const bare=paint(width,height,side,focused,{...model,session:[],brief:[]},n);
 assert.deepEqual(asked(rows),asked(bare));
 if(!rows.some(s=>s.includes('📁 project')))assert.equal(rows.length,bare.length,`${width}x${height} focused=${focused} grew`);
}
// Running jobs join the branch row at 80, saving the row a separate job row took.
const idle=view.render({width:80,height:14,side:false,sid:'s',configuration:{},model:{...models[80],routes:[{...routes[0],state:'working'}]},
 questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
assert(idle.some(s=>s.includes('⎇ values-fixture*')&&s.includes('running: REVIEW1463_JOB')),idle.join('\n'));
assert(/^└─+┘$/.test(idle.at(-1)));
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"), json.dumps(models)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_model_is_observed_without_session_question_binding(self):
        script = r'''
const assert=require('node:assert/strict'),d=require(process.argv[1]);
const id='00000000-0000-4000-8000-000000000052';
let v=d.observe('codex',['  '+id+' · work · main · Context 42% used · gpt-6 high · 5h: 90% left · Weekly 80% left',' ? for shortcuts'],'');
assert.equal(v.model,'gpt-6');assert.equal(v.effort,'high');assert.equal(v.percent,42);
assert.equal(d.observe('codex',['Someone said model: made-up high'],'exact'),null);
v=d.observe('codex',['  main · Context 0% used · gpt-6 high','  ? for shortcuts'],'');
assert.equal(v.model,'gpt-6');assert.equal(v.effort,'high');assert.equal(v.sid,undefined);
assert.equal(d.observe('codex',['  main · gpt-6 high','normal conversation output'],''),null);
v=d.observe('claude',['📁 repo · ⎇ main · context ━━━━────── 42% · Opus high · 5h 10% · 7d 20%'],'');
assert.equal(v.model,'Opus');assert.equal(v.effort,'high');
'''
        self.node(script, "native-display.cjs")

    def node(self, script, name):
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell" / name)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mouse_packets_do_not_rewrite_typing_or_pasted_mouse_text(self):
        script = r'''
const assert=require('node:assert/strict'),decode=require(process.argv[1]);let output='',events=[],paste=false;
const d=decode({bytes:b=>{output+=b.toString();if(output.endsWith('\x1b[200~'))paste=true;},
 mouse:(...event)=>events.push(event.slice(0,4)),suppressed:()=>paste});
d.feed(Buffer.from('draft'));assert.equal(output,'draft');
d.feed(Buffer.from('\x1b'));d.feed(Buffer.from('[<0;'));d.feed(Buffer.from('95;1Mtail'));
assert.equal(output,'drafttail');assert.deepEqual(events,[[0,95,1,false]]);
d.feed(Buffer.from('\x1b[200~'));d.feed(Buffer.from('literal\x1b[<0;95;1M\x1b[201~'));
assert.equal(events.length,1);assert(output.includes('literal\x1b[<0;95;1M'));
paste=false;d.feed(Buffer.from('\x1b'));d.flush();assert(output.endsWith('\x1b'));
'''
        self.node(script, "panel-mouse.cjs")

    def test_settings_controls_render_and_keep_the_native_sidebar_width_readonly(self):
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
for(const harness of ['claude','codex','opencode']){
 const settings={index:1,value:{...v.defaults}};
 const r=v.render({width:40,height:24,side:true,harness,settings,questions:()=>[]});
 assert(r.rows.flat().map(s=>s.text).join('').includes('Hearting shell settings'));
 assert(r.hits.some(h=>h.action==='apply'));assert(r.hits.some(h=>h.action==='cancel'));assert(r.hits.some(h=>h.action==='defaults'));
 assert.equal(r.hits.some(h=>h.action==='setting:1:up'),harness!=='opencode');
}
'''
        self.node(script, "panel-view.cjs")

    def test_editing_budget_keeps_the_draft_visible_in_native_sidebar_and_80_columns(self):
        script = r'''
const assert=require('node:assert/strict'),path=require('node:path'),v=require(process.argv[1]);
const c=require(path.join(path.dirname(process.argv[1]),'block-input.cjs'))({changed(){},answer(){throw Error('no submit');},action(){}});
c.update([{id:'exact',revision:'one',answerable:true,questions:[{id:'q',question:'Where should this result go?',choices:['Here','Another folder'],display:{head:[{text:'Question · answer needed'}],body:[[{text:'Where should this result go?'}]],options:[[{text:'1 Here · 2 Another folder'}]],source:[[{text:'this session'}]]}}]}]);
c.enter();c.input(Buffer.from('tDraft answer'));
const model={answerable:true,title:'Confirmed task',routes:[{title:'Confirmed task',state:'working',stage:'review',stages:[{name:'prepare'},{name:'review'}],reason:'Review this result together'}]};
for(const [width,height,side] of [[37,21,true],[80,12,false]]){
 const r=v.render({width,height,side,focused:true,model,configuration:{},questions:o=>c.detail(o)});
 const text=r.rows.map(row=>row.map(p=>p.text).join('')).join('\n');
 assert(text.includes('Draft answer'),text);assert(text.includes('Tab next'),text);
}
'''
        self.node(script, "panel-view.cjs")

    def test_settings_apply_preserves_enablement_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory}
            path = shell.policy_path(env)
            path.parent.mkdir()
            original = b'{ "enabled": true, "layout": "bottom" }\n'
            path.write_bytes(original)
            path.chmod(0o600)
            desired = {**shell.DISPLAY_DEFAULTS, "side_width": 48}
            self.assertEqual(shell.save_display_policy(desired, env), desired)
            self.assertIs(json.loads(path.read_text())["enabled"], True)
            self.assertEqual(next(path.parent.glob("*.backup-*")).read_bytes(), original)
            self.assertEqual(shell.display_policy(env)["side_width"], 48)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                shell.save_display_policy({**desired, "model": "not-shell-config"}, env)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
