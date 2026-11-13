"""The shell panel's LAB section (draft A): its projection from the shared experiment board.

Fixtures are synthetic projects in a ``TemporaryDirectory`` with their own HOME and without
``AGENT_ARTIFACT_ROOT`` (the catalog test's ``Base``); run records come from the real
``resource_progress`` writer.  No real artifact root, session, or account is read.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for _path in (str(ROOT / "utilities"), str(ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
_spec = importlib.util.spec_from_file_location("experiment_catalog_test_base_for_shell", ROOT / "utilities/experiment_catalog.test.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

import experiment_board as B  # noqa: E402
import experiment_catalog as E  # noqa: E402
from fleet import session_lab  # noqa: E402  (the module the shell backend loads)
from fleet.explorer import lab_table  # noqa: E402

NOW = T.NOW
GROUP = "AMI 가변 화자 개선"
HEALTHY = {"state": "ok", "seconds_since_anchor": 60.0}
STUCK = {"state": "stagnant", "seconds_since_anchor": 9000.0}


def condition(name, run_id):
    return {"display_name": name, "group": GROUP, "refs": {"runs": [{"run_id": run_id}]}}


def texts(parts):
    return "".join(part["text"] for part in parts)


class LabBase(T.Base):
    def setUp(self):
        super().setUp()
        for clear in (session_lab.clear_cache, B.clear_cache):
            clear()
            self.addCleanup(clear)

    def project(self, *, failed=True):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [
            condition("0.3초 이하 짧은 발화 유지", "run-a"), condition("짧은 발화 제외", "run-b"),
            condition("화자 수 고정", "run-c")]})
        self.write_run(root, "run-a", stagnation=HEALTHY, series={"wer": [(10, 21.0), (12, 18.5)]})
        self.write_run(root, "run-b", status="failed" if failed else "running", stagnation=HEALTHY)
        self.write_run(root, "run-c", stagnation=STUCK)
        return root


class ShellLab(LabBase):
    def test_the_projection_is_the_boards_in_fleets_own_words(self):
        root = self.project()
        board, watch = session_lab.read(root.parent, now=NOW)
        # The catalog and the run folder watched whole (its records, however many, redraw with it).
        self.assertEqual(watch, [str(E.catalog_path(root)), str(root / "resource-runs") + os.sep])
        view = session_lab.view(board, NOW)
        group = board["groups"][0]
        self.assertEqual((view["key"], view["count"]), ("lab:" + group["id"], "3"))
        self.assertEqual(view["clock"], B.clock_text(board["generated_at"], NOW))
        # The same cells Fleet's LAB table draws for the same rows: one source, one wording.
        for row, shown in zip(group["rows"], view["rows"]):
            fleet = lab_table.cells(row, group, NOW)
            self.assertEqual(shown["name"]["text"], fleet["name"][0])
            for column in ("latest", "delta", "state"):
                self.assertEqual(shown["cells"][column]["text"], fleet[column][0], column)
            self.assertEqual(shown["cells"]["progress"]["text"], "" if fleet["progress"][0] == "?" else fleet["progress"][0])
            for piece in shown["more"]:
                self.assertIn(texts(piece["parts"]).split(" ")[-1], fleet[piece["col"]][0])
                self.assertNotIn(fleet[piece["col"]][0], ("·", "?"))  # an unknown value is no piece
        # The head counts the failed and the stuck conditions; the board's headline names the failed one.
        self.assertEqual([texts(c) for c in view["counts"]],
                         [lab_table.ui_text("fleet.lab.n_failed", n=1), lab_table.ui_text("fleet.lab.n_stagnant", n=1)])
        self.assertEqual(view["counts"][0][0]["key"], "lvl_r")
        self.assertEqual(view["headline"]["left"][0]["text"], "짧은 발화 제외")
        self.assertEqual(view["goal"][0]["text"], T.group_spec()["goal"])
        self.assertEqual(([p["text"] for p in view["strip"]], view["strip_attention"]), (["!2", "LB"], True))
        # A healthy board: the running count, the best latest value against its baseline, `3 LB`.
        B.clear_cache()
        healthy = self.make_root("ok", "b")
        self.register(healthy, {"groups": [T.group_spec()], "conditions": [condition("짧은 발화 유지", "run-a")]})
        self.write_run(healthy, "run-a", stagnation=HEALTHY, series={"wer": [(10, 21.0), (12, 18.5)]})
        view = session_lab.view(session_lab.read(healthy.parent, now=NOW)[0], NOW)
        self.assertEqual([texts(c) for c in view["counts"]], [lab_table.ui_text("fleet.lab.n_running", n=1)])
        self.assertEqual(texts(view["headline"]["left"]), "짧은 발화 유지 18.50%@12")
        self.assertEqual(texts(view["headline"]["right"]), "fix 19.99%")
        self.assertEqual([p["text"] for p in view["strip"]], ["1", " LB"])

    def test_no_experiments_draw_no_section_and_a_folder_without_a_root_reads_nothing(self):
        blank = self.make_root("blank", "c")
        board, watch = session_lab.read(blank.parent, now=NOW)
        self.assertFalse(board["visible"])
        self.assertIsNone(session_lab.view(board, NOW))
        self.assertIn(str(E.catalog_path(blank)), watch)   # a catalog made later still redraws
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(session_lab.read(plain, now=NOW), (None, []))
        self.assertEqual(session_lab.read(None, now=NOW), (None, []))
        self.assertIsNone(session_lab.view(None, NOW))

    def test_a_renamed_condition_redraws_with_its_new_name(self):
        root = self.project()
        first = session_lab.view(session_lab.read(root.parent, now=NOW)[0], NOW)
        self.assertEqual(first["rows"][1]["name"]["text"], "짧은 발화 제외")
        E.run_write(root, lambda ctx: E.op_rename(ctx, "짧은 발화 제외", "짧은 발화를 뺀 학습"), now=NOW + 1)
        board, watch = session_lab.read(root.parent, now=NOW + 2)    # inside the board's 60 s limit
        second = session_lab.view(board, NOW + 2)
        self.assertEqual(second["rows"][1]["name"]["text"], "짧은 발화를 뺀 학습")
        self.assertEqual(second["headline"]["left"][0]["text"], "짧은 발화를 뺀 학습")
        self.assertIn(str(E.catalog_path(root)), watch)

    def test_a_broken_catalog_or_unnamed_runs_are_unconfirmed_never_no_experiments(self):
        missing = self.make_root()
        self.write_run(missing, "run-x")
        self.write_run(missing, "run-y")
        view = session_lab.view(session_lab.read(missing.parent, now=NOW)[0], NOW)
        self.assertEqual((view["count"], [p["text"] for p in view["strip"]]), ("", ["?2", "LB"]))
        self.assertEqual(texts(view["goal"]), lab_table.ui_text("fleet.lab.catalog_missing"))
        self.assertEqual(texts(view["headline"]["left"]), lab_table.ui_text("fleet.lab.unnamed_runs"))
        self.assertIn("run-x", texts(view["unlinked"]))
        self.assertEqual([texts(c) for c in view["counts"]], [lab_table.ui_text("fleet.lab.n_unnamed", n=2)])
        broken = self.make_root("broken", "b")
        path = E.catalog_path(broken)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        view = session_lab.view(session_lab.read(broken.parent, now=NOW)[0], NOW)
        self.assertEqual([p["text"] for p in view["strip"]], ["?1", "LB"])
        self.assertEqual(texts(view["goal"]), lab_table.ui_text("fleet.lab.unreadable", code=B.read(broken, now=NOW, cache={})[0]["catalog"]["code"]))
        # A read that fails is the same: unconfirmed, never an empty board.
        B.clear_cache()
        with mock.patch.object(E, "status", side_effect=RuntimeError("boom")):
            view = session_lab.view(session_lab.read(missing.parent, now=NOW + 5)[0], NOW + 5)
        self.assertEqual([p["text"] for p in view["strip"]], ["?1", "LB"])

    def project_panel(self, harness, cwd):
        import session_shell as shell
        import human_wait_state
        from fleet import session_view
        observed = lambda *args, **kwargs: ({"cwd": str(cwd)}, [])
        with mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, "")), \
                mock.patch.object(session_view, "observe", observed), \
                mock.patch.object(shell, "_codex_footer_plan", return_value=(None, None)):
            return shell.panel_projection(harness, "00000000-0000-4000-8000-00000000lab1", native_pid=os.getpid(),
                                          native_start="1", columns=40, rows=30)

    def test_the_panel_projection_carries_lab_in_every_harness_and_watches_its_files_last(self):
        root = self.project()
        lab_watch = session_lab.read(root.parent, now=NOW)[1]
        results = {harness: self.project_panel(harness, root.parent) for harness in ("claude", "codex", "opencode")}
        labs = {harness: result["panel"]["lab"] for harness, result in results.items()}
        for harness, result in results.items():
            self.assertEqual(result["watch"][-len(lab_watch):], lab_watch, harness)
            self.assertTrue(labs[harness]["key"].startswith("lab:"), harness)
        strip = lambda lab: [part["text"] for part in lab["strip"]]
        self.assertEqual({harness: strip(lab) for harness, lab in labs.items()},
                         {harness: ["!2", "LB"] for harness in labs})
        drop_clock = lambda lab: json.dumps({**lab, "clock": ""}, sort_keys=True, ensure_ascii=False)
        self.assertEqual(drop_clock(labs["codex"]), drop_clock(labs["claude"]))
        self.assertEqual(drop_clock(labs["opencode"]), drop_clock(labs["claude"]))
        # A project without experiments: no LAB in the projection at all.
        blank = self.make_root("blank", "c")
        self.assertIsNone(self.project_panel("claude", blank.parent)["panel"]["lab"])

    def test_projection_and_render_draw_the_same_section_in_every_harness(self):
        root = self.project()
        view = session_lab.view(session_lab.read(root.parent, now=NOW)[0], NOW)
        script = r"""
const assert=require('node:assert/strict'),v=require(process.argv[1]),lab=JSON.parse(process.argv[2]);
const draw=(harness,width,open,folded=false)=>v.render({width:folded?8:width,height:60,side:true,fill:true,folded,harness,state:'idle',configuration:{},
 model:{lab,repo:[{text:'proj'}]},expandedKeys:open?[lab.key]:[],questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join('')).filter(s=>!s.startsWith('┌'));
for(const width of [40,32])for(const open of [false,true])for(const folded of [false,true]){
 const claude=draw('claude',width,open,folded);
 for(const h of ['codex','opencode'])assert.deepEqual(draw(h,width,open,folded),claude,h);
 if(folded)assert(claude.includes('│ !2LB │'),claude.join('\n'));
 else{assert(claude.some(s=>/^│ 3 LAB /.test(s)),claude.join('\n'));
  // The failed condition is the one to look at, by its catalog name (cut with `…` only when narrow).
  assert(claude.some(s=>/짧은 발화( 제외|…)/.test(s)),claude.join('\n'));}
}
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"),
                                 json.dumps(view, ensure_ascii=False)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# The host's own `queueProjection` and `observeFiles` (real `fs.watch`, its real batches: 25 ms for a
# watched file, at most one redraw a second for a watched folder); `projection` counts the redraws
# they schedule and, given the paths, observes them again as the host's projection does.
HOST = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const body=source.slice(source.indexOf('function queueProjection('),source.indexOf('async function projection()'));
const input=JSON.parse(process.argv[2]);
const host=observed=>{const c={fs,path,setTimeout,clearTimeout,watchers:new Map(),turnRecord:'',readTurnRecord(){},
 ended:false,relay:false,projectionTimer:undefined,succeeding:false,nativeSelectionRevision:0,sourceError:undefined,
 redraws:0,projection(){c.redraws++;if(c.drawn)c.drawn();if(observed)c.observeFiles(observed);}};
 vm.createContext(c);vm.runInContext(body,c);return c;};
const shape=c=>JSON.stringify([...c.watchers].map(([directory,row])=>[directory,row.targets]));
const close=c=>{for(const row of c.watchers.values())row.watch.close();c.watchers.clear();};
const wait=ms=>new Promise(done=>setTimeout(done,ms));
const settle=()=>wait(1100);   // past the second a folder's change may wait for
const run=main=>main().catch(error=>{console.error(error);process.exit(1);});  // open watches would keep it alive
"""

HOST_WATCH = HOST + r"""
run(async()=>{
 const c=host();
 c.observeFiles(input.existing.concat(input.lab));await settle();c.redraws=0;
 const counted=async(what,act,want)=>{act();await settle();assert.equal(c.redraws,want,what);c.redraws=0;};
 const last=input.last,tmp=path.join(path.dirname(last),'.'+path.basename(last)+'.tmp');
 await counted('the last record written in place',()=>fs.writeFileSync(last,'{"run_id":"x","n":1}'),1);
 await counted('the last record replaced through a temporary file',()=>{fs.writeFileSync(tmp,'{"run_id":"x","n":2}');fs.renameSync(tmp,last);},1);
 await counted('a new record',()=>fs.writeFileSync(input.fresh,'{}'),1);
 await counted('a record removed',()=>fs.unlinkSync(input.fresh),1);
 await counted('many records at once ride one redraw',()=>{for(let n=0;n<20;n++)fs.writeFileSync(input.fresh+n,'{}');},1);
 await counted('a folder the LAB does not watch',()=>{for(const file of input.unrelated)fs.writeFileSync(file,'{}');},0);
 close(c);
 // Seventy paths above it: the host's first 64 are those paths, as without the LAB.
 const many=host(),alone=host();
 many.observeFiles(input.seventy.concat(input.lab));alone.observeFiles(input.seventy.slice(0,64));
 assert.equal(shape(many),shape(alone));close(many);close(alone);
 // Fewer above it: every path above stays, and the LAB's targets follow them.
 const few=host(),above=host();
 few.observeFiles(input.seventy.slice(0,60).concat(input.lab));above.observeFiles(input.seventy.slice(0,60));
 const kept=JSON.parse(shape(few)),before=JSON.parse(shape(above));
 for(const [directory,targets] of before)assert.deepEqual(kept.find(([d])=>d===directory)[1].slice(0,targets.length),targets,directory);
 assert(kept.some(([,targets])=>targets.includes(input.lab[1])),shape(few));
 close(few);close(above);
});
"""

# Review RV-PR1612 R2: a folder made again at the same path before the redraw is another folder; the
# old one's watch sees nothing more, so what is written after it must still redraw.
HOST_REMADE = HOST + r"""
run(async()=>{
 const runs=input.runs,watched=[input.catalog,runs+path.sep],c=host(watched);
 c.observeFiles(watched);await settle();c.redraws=0;
 const counted=async(c,what,act,want,pause=settle)=>{act();await pause();assert.equal(c.redraws,want,what);c.redraws=0;};
 const record=path.join(runs,'run-00.json'),tmp=path.join(runs,'.run-00.json.tmp');
 const then=async(what,act)=>{
  act();await settle();c.redraws=0;
  await counted(c,what+', then a record written in place',()=>fs.writeFileSync(record,'{"n":1}'),1);
  await counted(c,what+', then the record replaced',()=>{fs.writeFileSync(tmp,'{"n":2}');fs.renameSync(tmp,record);},1);
 };
 await then('deleted and made again at once',()=>{fs.rmSync(runs,{recursive:true});fs.mkdirSync(runs);});
 await then('renamed away and back at once',()=>{fs.renameSync(runs,runs+'.away');fs.renameSync(runs+'.away',runs);});
 await then('renamed away and another folder made in its place',()=>{fs.renameSync(runs,runs+'.old');fs.mkdirSync(runs);});
 await then('its parent deleted and made again with it',()=>{fs.rmSync(input.root,{recursive:true});fs.mkdirSync(runs,{recursive:true});});
 close(c);
 // A watched file keeps its 25 ms batch: written 1, replaced 1, a file beside it 0.
 const quick=()=>wait(250),q=input.question,f=host([q]);
 f.observeFiles([q]);await quick();f.redraws=0;
 await counted(f,'a watched file written',()=>fs.writeFileSync(q,'{"n":1}'),1,quick);
 await counted(f,'a watched file replaced',()=>{fs.writeFileSync(q+'.tmp','{}');fs.renameSync(q+'.tmp',q);},1,quick);
 await counted(f,'a file beside it',()=>fs.writeFileSync(path.join(path.dirname(q),'other.json'),'{}'),0,quick);
 // Its folder deleted and made again at once: the file written after it still redraws.
 fs.rmSync(path.dirname(q),{recursive:true});fs.mkdirSync(path.dirname(q));await quick();f.redraws=0;
 await counted(f,'a watched file written in its folder made again',()=>fs.writeFileSync(q,'{"n":2}'),1,quick);
 close(f);
 // A file whose folder is missing: the folder made 1, then the file written 1.
 const later=input.later,g=host([later]);
 g.observeFiles([later]);await quick();g.redraws=0;
 await counted(g,'the missing folder made',()=>fs.mkdirSync(path.dirname(later),{recursive:true}),1,quick);
 await counted(g,'then the file written',()=>fs.writeFileSync(later,'{}'),1,quick);
 close(g);
});
"""

# The supervisor's cap (RV-PR1612 R2): run records changing many times a second redraw at most once
# a second, the last change always drawn; a question file keeps its 25 ms batch meanwhile.
HOST_BUSY = HOST + r"""
run(async()=>{
 const runs=input.runs,q=input.question,watched=[q,runs+path.sep],c=host(watched);
 const records=Array.from({length:8},(_,n)=>path.join(runs,'run-'+(32+n)+'.json'));
 const read=file=>{try{return fs.readFileSync(file,'utf8');}catch{return '';}};
 const drawn=[];c.drawn=()=>drawn.push({at:Date.now(),records:records.map(read),question:read(q)});
 c.observeFiles(watched);await settle();drawn.length=0;
 // One change after a quiet second is drawn at once.
 let at=Date.now();fs.writeFileSync(records[0],'first');await wait(400);
 assert.equal(drawn.length,1,'one change, one redraw');
 assert(drawn[0].at-at<300,'one change drawn after '+(drawn[0].at-at)+' ms');
 assert.equal(drawn[0].records[0],'first');
 await settle();drawn.length=0;
 // Eight records each written about five times a second, in place or replaced, for `seconds`.
 let n=0;
 const busy=async(seconds,during)=>{
  const start=Date.now();
  while(Date.now()-start<seconds*1000){
   const file=records[n%8],text=String(n++);
   if(n%2)fs.writeFileSync(file,text);else{fs.writeFileSync(file+'.tmp',text);fs.renameSync(file+'.tmp',file);}
   if(during)during(Date.now()-start);
   await wait(25);
  }
 };
 await busy(3);
 const final=records.map(read);await settle();
 assert(drawn.length>=3&&drawn.length<=5,'redraws in three busy seconds: '+drawn.length);
 for(let i=1;i<drawn.length;i++)assert(drawn[i].at-drawn[i-1].at>=950,'redraws '+(drawn[i].at-drawn[i-1].at)+' ms apart');
 assert.deepEqual(drawn.at(-1).records,final,'the last change is drawn');
 drawn.length=0;
 // A question written while the records are busy is drawn in its 25 ms batch.
 let asked=0;
 await busy(2,elapsed=>{if(!asked&&elapsed>=700){asked=Date.now();fs.writeFileSync(q,'asked');}});
 await settle();
 const shown=drawn.find(row=>row.question==='asked');
 assert(shown,'the question is drawn');
 assert(shown.at-asked<300,'the question drawn after '+(shown.at-asked)+' ms');
 assert.deepEqual(drawn.at(-1).records,records.map(read),'the last change is drawn');
 close(c);
});
"""

# Review RV-PR1612 R3: a watch that fails again and again. The host's real `projection()` runs with
# `queueProjection` and `observeFiles`, so the redraw a watch made again asks for is the host's own;
# `source` reads the synthetic files as the backend would. While `failing` holds, every watch made on
# the run folder fails (the review's injection: an error emitted on the real `fs.watch` object).
HOST_FAILING = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const host=fs.readFileSync(process.argv[1],'utf8');
const body=host.slice(host.indexOf('function queueProjection('),host.indexOf('\nfunction claudePanelPainted('));
const input=JSON.parse(process.argv[2]),runs=input.runs,q=input.question,record=path.join(runs,'run-00.json');
const read=file=>{try{return fs.readFileSync(file,'utf8');}catch{return '';}};
const wait=ms=>new Promise(done=>setTimeout(done,ms));
const fail=w=>w.emit('error',Object.assign(new Error('injected watch failure'),{code:'ENOSPC'}));
let failing=false,current,made=[],asked=[],drawn=[];
const nativeWatch=fs.watch.bind(fs);
fs.watch=(...args)=>{
 const w=nativeWatch(...args);
 if(String(args[0])===runs){current=w;made.push(Date.now());if(failing)setImmediate(()=>fail(w));}
 return w;
};
const c={fs,path,setTimeout,clearTimeout,watchers:new Map(),sid:'s',ended:false,relay:false,succeeding:false,
 projectionTimer:undefined,projecting:false,projectionPending:false,selection:0,clock:0,nativeSelectionRevision:0,
 sourceError:undefined,openColumns:()=>40,rows:30,cols:80,state:'idle',expanded:true,nativeContext:undefined,
 nativeMcp:undefined,block:{references:()=>[],update(){}},actionWorker:undefined,currentDelivery:undefined,
 upgradeNoteNow:()=>'',displayValues:{},harness:'codex',turnRecord:'',readTurnRecord(){},noteTurn(){},
 projectionKey:'',heldBytes:0,shownState:()=>'idle',bootstrapModel:false,nativeSelected:{},publishNativeDisplay(){},
 height:()=>30,applyLayout(){},nativeFooter:null,footer:'',footerSegments:[],panelLines:[],panelSections:undefined,
 panelModel:{},
 // The backend's read takes about as long as the real one (20 ms) and asks for the same watch.
 source:async()=>{const at=Date.now(),lines=[read(q),read(record)];asked.push(at);await wait(20);
  return {ok:true,watch:[q,runs+path.sep],lines};},
 rebuildDisplay(){drawn.push({at:Date.now(),question:c.panelLines[0],record:c.panelLines[1]});}};
vm.createContext(c);vm.runInContext(body,c);
const settle=()=>wait(1100);
const reset=()=>{made.length=0;asked.length=0;drawn.length=0;};
const apart=(times,what)=>{for(let i=1;i<times.length;i++)
 assert(times[i]-times[i-1]>=950,what+' '+(times[i]-times[i-1])+' ms apart: '+times.map(t=>t-times[0]).join(','));};
(async()=>{
 c.projection();await settle();await settle();
 assert(current&&made.length>=1,'the run folder is watched');
 // Every watch on the run folder fails from now on, the one in place first: three seconds.
 failing=true;reset();fail(current);
 let start=Date.now();
 await wait(1200);fs.writeFileSync(record,'during');
 await wait(1800);
 const remade=made.length,projected=asked.length;
 assert(remade>=2,'a failed watch is made again: '+remade);
 assert(remade<=4,'watches made again in three failing seconds: '+remade);
 assert(projected<=4,'projections in three failing seconds: '+projected);
 apart(asked,'projections');
 await settle();
 assert.equal(drawn.at(-1)?.record,'during','the last change is drawn while the watch fails');
 // A question written while the run folder's watch keeps failing is drawn in its 25 ms batch.
 reset();await wait(500);
 const at=Date.now();fs.writeFileSync(q,'asked');
 await wait(300);
 const shown=drawn.find(row=>row.question==='asked');
 assert(shown,'the question is drawn within 300 ms');
 assert(shown.at-at<300,'the question drawn after '+(shown.at-at)+' ms');
 await wait(1700);
 assert(made.length<=4,'watches made again in the question\'s 2.5 s: '+made.length);
 // The failure stops: the next watch made again stays, and the next write redraws once.
 failing=false;await settle();await settle();await settle();
 assert(c.watchers.get(runs)&&!c.watchers.get(runs).failed,'the run folder is watched again');
 reset();fs.writeFileSync(record,'after');await settle();
 assert.equal(made.length,0,'no watch made again once it holds');
 assert.equal(asked.length,1,'one write, one projection: '+asked.length);
 assert.equal(drawn.length,1,'one write, one redraw: '+drawn.length);
 assert.equal(drawn[0].record,'after');
 c.ended=true;clearTimeout(c.projectionTimer);
 for(const row of c.watchers.values())row.watch.close();c.watchers.clear();
})().catch(error=>{console.error(error);process.exit(1);});
"""


class ShellLabWatch(LabBase):
    """Review RV-PR1612 R1: a record past the first few is watched like the first one. R2: a folder
    made again is watched again, and a busy one redraws at most once a second."""

    def test_the_run_folder_is_watched_whole_so_any_record_in_it_redraws(self):
        root = self.make_root()
        names = ["run-%02d" % n for n in range(40)]
        self.register(root, {"groups": [T.group_spec()],
                             "conditions": [condition("조건 %02d" % n, name) for n, name in enumerate(names)]})
        for name in names:
            self.write_run(root, name, stagnation=HEALTHY)
        board, watch = session_lab.read(root.parent, now=NOW)
        runs = root / "resource-runs"
        self.assertEqual(board["groups"][0]["count"], 40)
        # The catalog, then the run folder as one folder target: no record listed one by one.
        self.assertEqual(watch, [str(E.catalog_path(root)), str(runs) + os.sep])
        other = self.base / "other"
        (other / "resource-runs").mkdir(parents=True)
        existing_root = self.base / "existing"
        seventy = []
        for n in range(70):
            folder = existing_root / ("d%d" % (n % 7))
            folder.mkdir(parents=True, exist_ok=True)
            seventy.append(str(folder / ("f%02d.json" % n)))
        script_input = {"lab": watch, "existing": seventy[:10], "seventy": seventy,
                        "last": str(runs / "run-39.json"), "fresh": str(runs / "run-40.json"),
                        "unrelated": [str(other / "resource-runs" / "run-39.json"),
                                      str(E.catalog_path(root).parent / "notes.json")]}
        self.host(HOST_WATCH, script_input)

    def host(self, script, script_input):
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/host.cjs"),
                                 json.dumps(script_input)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_run_folder_made_again_at_the_same_path_is_watched_again(self):
        """Review RV-PR1612 R2: deleted and made again before the redraw, it kept the old folder's watch."""
        root = self.project()
        question = self.base / "questions" / "q.json"
        question.parent.mkdir()
        self.host(HOST_REMADE, {"root": str(root), "runs": str(root / "resource-runs"),
                                "catalog": str(E.catalog_path(root)), "question": str(question),
                                "later": str(self.base / "missing" / "sub" / "q.json")})

    def test_a_busy_run_folder_redraws_at_most_once_a_second_and_draws_its_last_change(self):
        root = self.project()
        question = self.base / "questions" / "q.json"
        question.parent.mkdir()
        self.host(HOST_BUSY, {"runs": str(root / "resource-runs"), "question": str(question)})

    def test_a_watch_that_keeps_failing_is_made_again_at_most_once_a_second(self):
        """Review RV-PR1612 R3: each watch made again for its failure asked a 25 ms redraw (67 in 3.2 s)."""
        root = self.project()
        question = self.base / "questions" / "q.json"
        question.parent.mkdir()
        self.host(HOST_FAILING, {"runs": str(root / "resource-runs"), "question": str(question)})

    def test_more_record_folders_than_the_cap_are_unconfirmed_never_left_out_silently(self):
        root = self.project()
        board = B.read(root, now=NOW, cache={})[0]
        cap = session_lab.FOLDER_CAP
        folders = [str(self.base / ("records-%d" % n)) for n in range(cap + 2)]
        listed = [str(E.catalog_path(root)), str(root / "resource-runs")] + [
            os.path.join(folder, "run-%d.json" % n) for n, folder in enumerate(folders)]
        with mock.patch.object(B, "read", return_value=(board, listed)):
            seen, watch = session_lab.read(root.parent, now=NOW)
        self.assertEqual(watch, [listed[0]] + [folder + os.sep for folder in ([listed[1]] + folders)[:cap]])
        view = session_lab.view(seen, NOW)
        self.assertIn(lab_table.ui_text("fleet.lab.unwatched", n=3), [texts(note) for note in view["notes"]])
        self.assertEqual(view["notes"][-1][0]["key"], "lvl_y")
        self.assertEqual([p["text"] for p in view["strip"]], ["!2", "LB"])   # a failure still leads the strip
        # Without a failure the strip says unconfirmed; the values stay.
        healthy = self.make_root("ok", "b")
        self.register(healthy, {"groups": [T.group_spec()], "conditions": [condition("짧은 발화 유지", "run-a")]})
        self.write_run(healthy, "run-a", stagnation=HEALTHY)
        board = B.read(healthy, now=NOW, cache={})[0]
        with mock.patch.object(B, "read", return_value=(board, listed)):
            view = session_lab.view(session_lab.read(healthy.parent, now=NOW)[0], NOW)
        self.assertEqual([p["text"] for p in view["strip"]], ["?3", "LB"])
        self.assertEqual(view["rows"][0]["name"]["text"], "짧은 발화 유지")


class ShellLabFailure(LabBase):
    """Review RV-PR1612 R2: LAB's own failure is LAB's unconfirmed state, never no LAB nor no panel."""

    def unconfirmed(self, view, code):
        self.assertEqual([p["text"] for p in view["strip"]], ["?1", "LB"])
        self.assertEqual(texts(view["goal"]), lab_table.ui_text("fleet.lab.unreadable", code=code))

    def test_a_read_that_fails_is_unconfirmed_and_keeps_its_watch(self):
        root = self.project()
        board, watch = session_lab.read(root.parent, now=NOW)
        later = NOW + session_lab.INTERVAL + 1          # the session folder's root is looked up again
        with mock.patch.object(session_lab.project_status, "artifact_root", side_effect=PermissionError("denied")):
            failed, kept = session_lab.read(root.parent, now=later)
        self.unconfirmed(session_lab.view(failed, later), "PermissionError")
        self.assertEqual(kept, watch)
        with mock.patch.object(B, "read", side_effect=RuntimeError("boom")):
            failed, kept = session_lab.read(root.parent, now=later + 1)
        self.unconfirmed(session_lab.view(failed, later + 1), "RuntimeError")
        self.assertEqual(kept, watch)
        # The next read that works draws the board again.
        self.assertEqual(session_lab.view(session_lab.read(root.parent, now=later + 2)[0], later + 2)["count"], "3")
        # A first read that fails: the root's catalog and run folder are still watched.
        session_lab.clear_cache()
        with mock.patch.object(B, "read", side_effect=RuntimeError("boom")):
            failed, kept = session_lab.read(root.parent, now=later + 3)
        self.unconfirmed(session_lab.view(failed, later + 3), "RuntimeError")
        self.assertEqual(kept, watch)
        # No experiments stays no section: no catalog and no runs, or no artifact folder at all.
        blank = self.make_root("blank", "c")
        self.assertIsNone(session_lab.view(session_lab.read(blank.parent, now=NOW)[0], NOW))
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(session_lab.read(plain, now=NOW), (None, []))
        # Wording that fails is unconfirmed too, and never raises.
        self.unconfirmed(session_lab.unconfirmed(NOW, "RuntimeError"), "RuntimeError")

    def panel(self, harness, cwd, ledger):
        import session_shell as shell
        import human_wait_state
        from fleet import session_view
        sid = "00000000-0000-4000-8000-00000000lab2"
        question = {"id": "owned", "revision": "r", "answerable": True,
                    "questions": [{"id": "q", "question": "LAB 옆 질문?"}]}
        routes = {"route_panels": {harness + "\0" + sid: [{"id": "rt-lab", "title": "ROUTE", "state": "working",
                                                          "marker": "working"}]}}
        observed = lambda *args, **kwargs: ({"cwd": str(cwd)}, [])
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(ledger)}):
            shard = shell._peer_message()._ledger_path(sid)
            shard.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            shard.write_text(json.dumps({"ts": stamp, "from": {"session_id": sid},
                                         "to": {"harness": "codex", "session_id": "x", "name": "n"}, "kind": "note",
                                         "summary": "SENT_LAB", "message_id": "m1", "delivery": {"status": "unknown"}}) + "\n")
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(ledger)}), \
                mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, "")), \
                mock.patch.object(human_wait_state, "cached_view", return_value=routes), \
                mock.patch.object(shell, "session_questions", return_value=[question]), \
                mock.patch.object(session_view, "observe", observed), \
                mock.patch.object(shell, "_codex_footer_plan", return_value=(None, None)):
            return shell.panel_projection(harness, sid, native_pid=os.getpid(), native_start="1", columns=40, rows=30)

    def test_a_lab_failure_stays_in_lab_and_the_rest_of_the_projection_draws_on(self):
        root = self.project()
        ledger = self.base / "ledger"
        for harness in ("claude", "codex", "opencode"):
            session_lab.clear_cache()
            healthy = self.panel(harness, root.parent, ledger)
            lab_watch = session_lab.read(root.parent)[1]
            self.assertEqual(healthy["watch"][-len(lab_watch):], lab_watch, harness)
            rest = healthy["watch"][:-len(lab_watch)]
            self.assertEqual([q["id"] for q in healthy["questions"]], ["owned"], harness)
            self.assertEqual([r["id"] for r in healthy["panel"]["routes"]], ["rt-lab"], harness)
            self.assertEqual([m["text"] for m in healthy["panel"]["messages"]], ["SENT_LAB"], harness)
            injections = (
                ("PermissionError", (mock.patch.object(session_lab, "INTERVAL", 0),
                                     mock.patch.object(session_lab.project_status, "artifact_root",
                                                       side_effect=PermissionError("denied")))),
                ("RuntimeError", (mock.patch.object(B, "read", side_effect=RuntimeError("boom")),)),
                ("RuntimeError", (mock.patch.object(session_lab, "view", side_effect=RuntimeError("boom")),)))
            for code, patches in injections:
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    result = self.panel(harness, root.parent, ledger)
                what = harness + " " + code
                self.unconfirmed(result["panel"]["lab"], code)
                self.assertEqual(result["questions"], healthy["questions"], what)
                route = lambda rows: [(r["id"], r["title"], r["state"]) for r in rows]   # the spinner moves on
                self.assertEqual(route(result["panel"]["routes"]), route(healthy["panel"]["routes"]), what)
                self.assertEqual([m["text"] for m in result["panel"]["messages"]], ["SENT_LAB"], what)
                self.assertEqual(result["watch"], rest + lab_watch, what)
            # No experiments: still no section.
            blank = self.make_root("blank-" + harness, "c")
            self.assertIsNone(self.panel(harness, blank.parent, ledger)["panel"]["lab"], harness)


if __name__ == "__main__":
    unittest.main()
