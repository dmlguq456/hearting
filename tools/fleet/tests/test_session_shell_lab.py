"""The host's file watch as the shell panel's LAB section (draft A) needed it: folders watched whole, made
again, a busy folder's pace, failing watches. (11-15: the LAB section left the panel, the person: "실험 현황은
다소 섣부른 추가같은데. 지금은 일단 빼"; its projection tests went with it, see the PR. The watch itself stays.)

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
        B.clear_cache()
        self.addCleanup(B.clear_cache)

    def project(self, *, failed=True):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [
            condition("0.3초 이하 짧은 발화 유지", "run-a"), condition("짧은 발화 제외", "run-b"),
            condition("화자 수 고정", "run-c")]})
        self.write_run(root, "run-a", stagnation=HEALTHY, series={"wer": [(10, 21.0), (12, 18.5)]})
        self.write_run(root, "run-b", status="failed" if failed else "running", stagnation=HEALTHY)
        self.write_run(root, "run-c", stagnation=STUCK)
        return root


HOST = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const body=source.slice(source.indexOf('function queueProjection('),source.indexOf('async function projection()'));
const input=JSON.parse(process.argv[2]);
const host=observed=>{const c={fs,path,setTimeout,clearTimeout,watchers:new Map(),turnRecord:'',readTurnRecord(){},popupWatch:[],popupDirty:false,statusTargets:new Set(),statusDirty:false,lastWatch:[],statusDoc:()=>null,backlogDoc:()=>null,observeStatus(){},
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
 sourceError:undefined,openColumns:()=>40,geometry:()=>({side:true}),panelView:{popupWidth:()=>76},rows:30,cols:80,state:'idle',expanded:true,nativeContext:undefined,
 nativeMcp:undefined,block:{references:()=>[],update(){}},actionWorker:undefined,currentDelivery:undefined,
 upgradeNoteNow:()=>'',displayValues:{},harness:'codex',turnRecord:'',readTurnRecord(){},noteTurn(){},
 projectionKey:'',heldBytes:0,shownState:()=>'idle',bootstrapModel:false,nativeSelected:{},publishNativeDisplay(){},
 height:()=>30,applyLayout(){},nativeFooter:null,footer:'',footerSegments:[],panelLines:[],panelSections:undefined,
 panelModel:{},popupWatch:[],popupDirty:false,statusTargets:new Set(),statusDirty:false,lastWatch:[],statusDoc:()=>null,backlogDoc:()=>null,observeStatus(){},popupKeys:{},sessionsKey:null,sessionsSummary:null,table:null,
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


# Review RV-PR1612 R4: the watch on a question, route or message FILE keeps failing. That path is not
# the run folder's: it keeps its own pace (a change is drawn within its 25 ms batch, as before this PR).
# Three files, three names, three folders; every watch made on their folders fails at once.
HOST_FAILING_FILES = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const host=fs.readFileSync(process.argv[1],'utf8');
const body=host.slice(host.indexOf('function queueProjection('),host.indexOf('\nfunction claudePanelPainted('));
const files=JSON.parse(process.argv[2]).files;
const read=file=>{try{return fs.readFileSync(file,'utf8');}catch{return '';}};
const wait=ms=>new Promise(done=>setTimeout(done,ms));
const folders=new Set(files.map(file=>path.dirname(file)));
let made=0;
const nativeWatch=fs.watch.bind(fs);
fs.watch=(...args)=>{
 const w=nativeWatch(...args);
 if(folders.has(String(args[0]))){made++;setImmediate(()=>w.emit('error',Object.assign(new Error('injected watch failure'),{code:'ENOSPC'})));}
 return w;
};
let drawn=[];
const c={fs,path,setTimeout,clearTimeout,watchers:new Map(),sid:'s',ended:false,relay:false,succeeding:false,
 projectionTimer:undefined,projecting:false,projectionPending:false,selection:0,clock:0,nativeSelectionRevision:0,
 sourceError:undefined,openColumns:()=>40,geometry:()=>({side:true}),panelView:{popupWidth:()=>76},rows:30,cols:80,state:'idle',expanded:true,nativeContext:undefined,
 nativeMcp:undefined,block:{references:()=>[],update(){}},actionWorker:undefined,currentDelivery:undefined,
 upgradeNoteNow:()=>'',displayValues:{},harness:'codex',turnRecord:'',readTurnRecord(){},noteTurn(){},
 projectionKey:'',heldBytes:0,shownState:()=>'idle',bootstrapModel:false,nativeSelected:{},publishNativeDisplay(){},
 height:()=>30,applyLayout(){},nativeFooter:null,footer:'',footerSegments:[],panelLines:[],panelSections:undefined,
 panelModel:{},popupWatch:[],popupDirty:false,statusTargets:new Set(),statusDirty:false,lastWatch:[],statusDoc:()=>null,backlogDoc:()=>null,observeStatus(){},popupKeys:{},sessionsKey:null,sessionsSummary:null,table:null,
 source:async()=>{const lines=files.map(read);await wait(20);return {ok:true,watch:files,lines};},
 rebuildDisplay(){drawn.push({at:Date.now(),lines:c.panelLines.slice()});}};
vm.createContext(c);vm.runInContext(body,c);
(async()=>{
 c.projection();await wait(1100);
 assert(made>=1,'the files\' folders are watched');
 for(let i=0;i<files.length;i++){
  await wait(1100); // the watch has failed and been made again for a while by now
  drawn=[];const at=Date.now();fs.writeFileSync(files[i],'changed '+i);
  await wait(400);
  const shown=drawn.find(row=>row.lines[i]==='changed '+i);
  assert(shown,'the change to '+path.basename(files[i])+' is drawn within 400 ms');
  assert(shown.at-at<300,path.basename(files[i])+' drawn after '+(shown.at-at)+' ms');
 }
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
        runs = root / "resource-runs"
        # The catalog, then the run folder as one folder target: no record listed one by one.
        watch = [str(E.catalog_path(root)), str(runs) + os.sep]
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

    def test_a_files_watch_that_keeps_failing_keeps_its_own_pace(self):
        """Review RV-PR1612 R4: the file targets' watch failing again and again was slowed to once a second."""
        files = []
        for folder, name in (("questions", "q.json"), ("routes", "route-1.json"), ("messages", "m-7.json")):
            (self.base / folder).mkdir()
            files.append(str(self.base / folder / name))
        self.host(HOST_FAILING_FILES, {"files": files})


if __name__ == "__main__":
    unittest.main()
