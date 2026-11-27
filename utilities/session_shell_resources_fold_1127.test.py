"""11-27 (the person: "그리고 RESOURCES는 기본값을 접은것으로 두고 그때는 서버별로 1줄로만 뜨는 형식으로 좀 고쳐보자"; of the design's two, "RESOURCES 가(세션 먼저)";
"펼치기는 RESOURCES 머리 줄을 누르는 것으로 짓습니다").

RESOURCES stands folded: one row a host, its name then, as many as fit from the first, ``GPU <busy>/<all>``, the sessions holding them (each once;
``[?]`` for a holder not known), ``max N%`` (the highest use observed; ``max ?`` with none), ``CPU``, ``MEM``. A press on its head opens the rows
as they stood (the same cells as before) and folds them again; the window keeps which while it is up (no setting, no file), a new panel stands
folded. The heights count the folded rows; the other sections lose no row for it. The expected rows are the design's ``res/A`` and
``expanded/A`` renders (PANEL-MEM-RESOURCES-USAGE-RENDERS-1127).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT)]


def gpu(i, busy, util, used, users=()):
    return {"i": i, "busy": busy, "util": util, "used": used, "total": 24, "users": list(users)}


# The design's six hosts (fictional): two GPUs held by two sessions, a GPU a process holds for no known session, a host with no GPU,
# two idle GPUs, a session on two GPUs, and a GPU whose use was not observed.
HOSTS = [
    {"name": "atlas", "self": True, "cpu": 34, "mem": 61,
     "gpus": [gpu(0, True, 92, 21, ["4f"]), gpu(1, True, 40, 21, ["b2"]), gpu(2, False, 0, 0), gpu(3, False, 0, 0)]},
    {"name": "birch", "self": False, "cpu": 0, "mem": 82, "gpus": [gpu(0, True, 0, 21), gpu(1, False, 0, 0)]},
    {"name": "cove", "self": False, "cpu": 11, "mem": 26, "gpus": []},
    {"name": "dune", "self": False, "cpu": 56, "mem": 41, "gpus": [gpu(0, False, 0, 0), gpu(1, False, 0, 0)]},
    {"name": "ember", "self": False, "cpu": 100, "mem": 77,
     "gpus": [gpu(0, True, 100, 21, ["9c", "e8"]), gpu(1, True, 80, 21, ["9c"])]},
    {"name": "fjord", "self": False, "cpu": 19, "mem": None, "gpus": [gpu(0, True, None, 21, ["e8"])]},
]

FOLDED = {
 36: {
  1: [
   "atlas GPU 2/4 [4f][b2] max 92%"
  ],
  3: [
   "atlas GPU 2/4 [4f][b2] max 92%",
   "birch GPU 1/2 [?] max 0% CPU 0%",
   "cove GPU 0/0 CPU 11% MEM 26%"
  ],
  6: [
   "atlas GPU 2/4 [4f][b2] max 92%",
   "birch GPU 1/2 [?] max 0% CPU 0%",
   "cove GPU 0/0 CPU 11% MEM 26%",
   "dune GPU 0/2 max 0% CPU 56%",
   "ember GPU 2/2 [9c][e8] max 100%",
   "fjord GPU 1/1 [e8] max ? CPU 19%"
  ]
 },
 44: {
  1: [
   "atlas GPU 2/4 [4f][b2] max 92% CPU 34%"
  ],
  3: [
   "atlas GPU 2/4 [4f][b2] max 92% CPU 34%",
   "birch GPU 1/2 [?] max 0% CPU 0% MEM 82%",
   "cove GPU 0/0 CPU 11% MEM 26%"
  ],
  6: [
   "atlas GPU 2/4 [4f][b2] max 92% CPU 34%",
   "birch GPU 1/2 [?] max 0% CPU 0% MEM 82%",
   "cove GPU 0/0 CPU 11% MEM 26%",
   "dune GPU 0/2 max 0% CPU 56% MEM 41%",
   "ember GPU 2/2 [9c][e8] max 100% CPU 100%",
   "fjord GPU 1/1 [e8] max ? CPU 19%"
  ]
 },
 60: {
  1: [
   "atlas GPU 2/4 [4f][b2] max 92% CPU 34% MEM 61%"
  ],
  3: [
   "atlas GPU 2/4 [4f][b2] max 92% CPU 34% MEM 61%",
   "birch GPU 1/2 [?] max 0% CPU 0% MEM 82%",
   "cove GPU 0/0 CPU 11% MEM 26%"
  ],
  6: [
   "atlas GPU 2/4 [4f][b2] max 92% CPU 34% MEM 61%",
   "birch GPU 1/2 [?] max 0% CPU 0% MEM 82%",
   "cove GPU 0/0 CPU 11% MEM 26%",
   "dune GPU 0/2 max 0% CPU 56% MEM 41%",
   "ember GPU 2/2 [9c][e8] max 100% CPU 100% MEM 77%",
   "fjord GPU 1/1 [e8] max ? CPU 19%"
  ]
 }
}
OPEN = {
 36: {
  1: [
   " atlas  CPU 34%  MEM 61%",
   "   GPU0 92% ━━━━━━━━─ 21/24G [4f]",
   "   GPU1 40% ━━━━━━━━─ 21/24G [b2]",
   "   GPU2  0% ─────────  0/24G",
   "   GPU3  0% ─────────  0/24G"
  ],
  3: [
   " atlas  CPU 34%  MEM 61%",
   "   GPU0 92% ━━━━━━━━─ 21/24G [4f]",
   "   GPU1 40% ━━━━━━━━─ 21/24G [b2]",
   "   GPU2  0% ─────────  0/24G",
   "   GPU3  0% ─────────  0/24G",
   " birch  CPU  0%  MEM 82%",
   "   GPU0  0% ━━━━━━━━─ 21/24G",
   "   GPU1  0% ─────────  0/24G",
   " cove   CPU 11%  MEM 26%"
  ],
  6: [
   " atlas  CPU  34%  MEM 61%",
   "   GPU0  92% 21/24G [4f]",
   "   GPU1  40% 21/24G [b2]",
   "   GPU2   0%  0/24G",
   "   GPU3   0%  0/24G",
   " birch  CPU   0%  MEM 82%",
   "   GPU0   0% 21/24G",
   "   GPU1   0%  0/24G",
   " cove   CPU  11%  MEM 26%",
   " dune   CPU  56%  MEM 41%",
   "   GPU0   0%  0/24G",
   "   GPU1   0%  0/24G",
   " ember  CPU 100%  MEM 77%",
   "   GPU0 100% 21/24G [9c] [e8]",
   "   GPU1  80% 21/24G [9c]",
   " fjord  CPU  19%",
   "   GPU0      21/24G [e8]"
  ]
 },
 44: {
  1: [
   " atlas  CPU 34%  MEM 61%",
   "   GPU0 92% ━━━━━━━━━─ 21/24G [4f]",
   "   GPU1 40% ━━━━━━━━━─ 21/24G [b2]",
   "   GPU2  0% ──────────  0/24G",
   "   GPU3  0% ──────────  0/24G"
  ],
  3: [
   " atlas  CPU 34%  MEM 61%",
   "   GPU0 92% ━━━━━━━━━─ 21/24G [4f]",
   "   GPU1 40% ━━━━━━━━━─ 21/24G [b2]",
   "   GPU2  0% ──────────  0/24G",
   "   GPU3  0% ──────────  0/24G",
   " birch  CPU  0%  MEM 82%",
   "   GPU0  0% ━━━━━━━━━─ 21/24G",
   "   GPU1  0% ──────────  0/24G",
   " cove   CPU 11%  MEM 26%"
  ],
  6: [
   " atlas  CPU  34%  MEM 61%",
   "   GPU0  92% ━━━━━━━━━─ 21/24G [4f]",
   "   GPU1  40% ━━━━━━━━━─ 21/24G [b2]",
   "   GPU2   0% ──────────  0/24G",
   "   GPU3   0% ──────────  0/24G",
   " birch  CPU   0%  MEM 82%",
   "   GPU0   0% ━━━━━━━━━─ 21/24G",
   "   GPU1   0% ──────────  0/24G",
   " cove   CPU  11%  MEM 26%",
   " dune   CPU  56%  MEM 41%",
   "   GPU0   0% ──────────  0/24G",
   "   GPU1   0% ──────────  0/24G",
   " ember  CPU 100%  MEM 77%",
   "   GPU0 100% ━━━━━━━━━─ 21/24G [9c] [e8]",
   "   GPU1  80% ━━━━━━━━━─ 21/24G [9c]",
   " fjord  CPU  19%",
   "   GPU0      ━━━━━━━━━─ 21/24G [e8]"
  ]
 },
 60: {
  1: [
   " atlas  CPU 34%  MEM 61%",
   "   GPU0 92% ━━━━─ 21/24G [4f]  GPU1 40% ━━━━─ 21/24G [b2]",
   "   GPU2  0% ─────  0/24G       GPU3  0% ─────  0/24G"
  ],
  3: [
   " atlas  CPU 34%  MEM 61%",
   "   GPU0 92% ━━━━─ 21/24G [4f]  GPU1 40% ━━━━─ 21/24G [b2]",
   "   GPU2  0% ─────  0/24G       GPU3  0% ─────  0/24G",
   " birch  CPU  0%  MEM 82%",
   "   GPU0  0% ━━━━─ 21/24G       GPU1  0% ─────  0/24G",
   " cove   CPU 11%  MEM 26%"
  ],
  6: [
   " atlas  CPU  34%  MEM 61%",
   "   GPU0  92% 21/24G [4f]       GPU1  40% 21/24G [b2]",
   "   GPU2   0%  0/24G            GPU3   0%  0/24G",
   " birch  CPU   0%  MEM 82%",
   "   GPU0   0% 21/24G            GPU1   0%  0/24G",
   " cove   CPU  11%  MEM 26%",
   " dune   CPU  56%  MEM 41%",
   "   GPU0   0%  0/24G            GPU1   0%  0/24G",
   " ember  CPU 100%  MEM 77%",
   "   GPU0 100% 21/24G [9c] [e8]  GPU1  80% 21/24G [9c]",
   " fjord  CPU  19%",
   "   GPU0      21/24G [e8]"
  ]
 }
}

NODE = r"""
const path=require('node:path'),fs=require('node:fs');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const jobs=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(jobs.map(job=>{
  const r=view.render({width:job.width,height:job.height,side:true,model:job.model,state:'idle',harness:'claude',frame:0,since:0,brief:()=>null,sent:()=>[],
    cellWidth:cells,configuration:{},...('open' in job?{resourcesOpen:job.open}:{})});
  return {lines:r.rows.map(row=>row.map(p=>p.text).join('')),hits:r.hits};
})));
"""


def styled(parts):
    from fleet import palette
    return [{"text": text, **palette.style_components(key)} for text, key in parts]


def model(hosts, *, brief=True):
    from fleet import palette, session_panel
    rows = session_panel.resource_rows({"observed_at": 1000.0, "hosts": hosts}, 1000.0)
    resources = [{"host": styled(r["host"]), "stats": [styled(item) for item in r["stats"]],
                  "gpus": [{name: styled(parts) for name, parts in g.items()} for g in r["gpus"]],
                  **({"brief": [styled(item) for item in r["brief"]]} if brief else {})} for r in rows]
    usage = [{"harness": "claude", "parts": styled([("5h ", "dim"), ("9%", "lvl_g"), (" │ ", "dim"), ("7d ", "dim"), ("72%", "lvl_y")])},
             {"harness": "codex", "parts": styled([("5h ", "dim"), ("24%", "lvl_g")])}]
    return {"styles": {key: palette.style_components(key) for key in ("dim", "loc_repo", "lvl_g", "lvl_y", "lvl_r")}, "colors": 256,
            "resources": resources, "usage": usage}


def paint(jobs):
    done = subprocess.run(["node", "-e", NODE, str(SHELL)], input=json.dumps(jobs), capture_output=True, text=True, timeout=180)
    if done.returncode:
        raise AssertionError(done.stdout + done.stderr)
    return json.loads(done.stdout)


def block(lines):
    """The RESOURCES head's index and its rows (up to USAGE), each without the frame and the trailing blanks."""
    at = next((i for i, line in enumerate(lines) if line.startswith("│ RESOURCES")), None)
    if at is None:
        return None, []
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("│ USAGE")), len(lines) - 1)
    return at, [line[1:-1].rstrip() for line in lines[at + 1:end]]


def overlapping(hits):
    return [(a, b) for i, a in enumerate(hits) for b in hits[i + 1:]
            if a["row"] == b["row"] and a["left"] <= b["right"] and b["left"] <= a["right"]]


class ResourcesFoldTest(unittest.TestCase):
    def test_the_folded_fields_come_from_the_sample_and_make_nothing_up(self):
        from fleet import session_panel
        rows = session_panel.resource_rows({"observed_at": 1000.0, "hosts": HOSTS}, 1000.0)
        joined = [" ".join(["".join(t for t, _k in r["host"]), *("".join(t for t, _k in f) for f in r["brief"])]) for r in rows]
        self.assertEqual(joined, FOLDED[60][6])
        keys = {(t, k) for r in rows for f in r["brief"] for t, k in f}
        # The words dim, the values in the body color; an unobserved use is `max ?`, dim, never a 0.
        self.assertLessEqual({k for _t, k in keys}, {"dim", "loc_repo"})
        self.assertIn(("max ?", "dim"), keys)
        self.assertEqual([("GPU ", "dim"), ("0/0", "loc_repo")], rows[2]["brief"][0])
        self.assertFalse(any(t.startswith("max") for f in rows[2]["brief"] for t, _k in f), "a host with no GPU has no use")
        # The rows the open section draws are unchanged.
        self.assertEqual(set(rows[0]), {"host", "gpus", "stats", "brief"})

    def test_the_folded_section_is_one_row_a_host_at_36_44_and_60(self):
        jobs, keys = [], []
        for width in (36, 44, 60):
            for count in (1, 3, 6):
                jobs.append({"model": model(HOSTS[:count]), "width": width, "height": 94})
                keys.append((width, count))
        for (width, count), drawn in zip(keys, paint(jobs)):
            at, rows = block(drawn["lines"])
            self.assertEqual([row[1:] for row in rows], FOLDED[width][count], (width, count))
            # The head reads as before: its name alone, no mark added.
            self.assertEqual(drawn["lines"][at][1:-1].strip(), "RESOURCES")

    def test_the_fields_go_in_from_the_first_and_none_stands_after_one_that_did_not_fit(self):
        # Sessions first: where the sessions do not fit, neither `max` nor CPU stands after them (they would fit alone).
        crowded = {"name": "atlas-long", "self": True, "cpu": 3, "mem": 5,
                   "gpus": [gpu(i, True, 50, 21, [tag]) for i, tag in enumerate(("4f", "b2", "9c", "e8"))]}
        drawn = paint([{"model": model([crowded]), "width": w, "height": 60} for w in (36, 44, 60)])
        self.assertEqual([[row[1:] for row in block(d["lines"])[1]] for d in drawn],
                         [["atlas-long GPU 4/4"], ["atlas-long GPU 4/4 [4f][b2][9c][e8]"],
                          ["atlas-long GPU 4/4 [4f][b2][9c][e8] max 50% CPU 3%"]])

    def test_a_press_on_the_head_opens_the_rows_as_they_stood_and_folds_them_again(self):
        text = (SHELL / "host.cjs").read_text(encoding="utf-8")
        code = (text[text.index("function panelMouse("):text.index("function displayReporter(")]
                + text[text.index("function panelAction("):text.index("let settingsTail=''")])
        for width in (36, 44, 60):
            for count in (1, 3, 6):
                folded, opened, older = paint([{"model": model(HOSTS[:count]), "width": width, "height": 94},
                                               {"model": model(HOSTS[:count]), "width": width, "height": 94, "open": True},
                                               {"model": model(HOSTS[:count], brief=False), "width": width, "height": 94}])
                at, _rows = block(folded["lines"])
                presses = [h for h in folded["hits"] if h["action"] == "resources"]
                # (a hit's row counts the panel's rows from its top border: the head's own line)
                self.assertEqual([(h["row"], h["left"], h["right"]) for h in presses], [(at, 2, width - 3)])
                self.assertEqual(overlapping(folded["hits"]) + overlapping(opened["hits"]), [])
                self.assertEqual([h for h in opened["hits"] if h["action"] == "resources"][0]["row"], block(opened["lines"])[0])
                # Open: the same cells as the section had (the design's `expanded/A`, the rows of a backend without the summary).
                self.assertEqual(block(opened["lines"])[1], OPEN[width][count], (width, count))
                self.assertEqual(opened["lines"], older["lines"])
                # The host: a press on the head row turns the window's own state over, and draws again; nothing else opens.
                script = r"""
const vm=require('node:vm'),fs=require('node:fs');
const [code,row]=[fs.readFileSync(0,'utf8'),Number(process.argv[1])];
const g={side:true,folded:false,left:false,panelColumn:90,panelCols:40},place='p';
const c={fleetView:null,paste:false,relay:false,dragging:false,settings:null,epoch:0,empty:true,SECTIONS:['MEM'],resourcesOpen:false,shown:0,opened:[],
  geometry:()=>g,panelColumns:()=>40,panelRow:()=>0,height:()=>100,terminal:{modes:{mouseTrackingMode:'none'}},
  layoutQueued:false,panelHitsPlacement:place,placement:()=>place,queueLayout(){},openPopup:(n)=>c.opened.push(n),
  panelHits:[{row,left:2,right:37,action:'resources'},{row:row+1,left:2,right:37,action:'popup:MEM'}],routeArea:null};
c.queueDisplay=()=>{c.shown++;};
vm.createContext(c);vm.runInContext(code,c);
const states=[];
for(let i=0;i<3;i++){c.panelMouse(0,95,row+1,false);states.push(c.resourcesOpen);}
process.stdout.write(JSON.stringify({states,shown:c.shown,opened:c.opened}));
"""
                done = subprocess.run(["node", "-e", script, str(at)], input=code, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout), {"states": [True, False, True], "shown": 3, "opened": []})

    def test_a_new_panel_stands_folded_and_the_state_is_kept_nowhere(self):
        host = (SHELL / "host.cjs").read_text(encoding="utf-8")
        self.assertIn("let resourcesOpen=false;", host)
        self.assertIn("folded:!expanded,resourcesOpen,", host)
        # Not a setting and not carried to a successor host: a new panel stands folded.
        for name in ("succession.cjs", "panel-view.cjs"):
            source = (SHELL / name).read_text(encoding="utf-8")
            self.assertNotIn("resources_open", source)
        self.assertNotIn("resourcesOpen", (SHELL / "succession.cjs").read_text(encoding="utf-8"))
        seed = host[host.index("function seedValue(") if "function seedValue(" in host else 0:]
        self.assertNotIn("resourcesOpen:", seed.split("\n\n")[0])
        drawn, = paint([{"model": model(HOSTS[:3]), "width": 40, "height": 60}])
        self.assertEqual(len(block(drawn["lines"])[1]), 3)

    def test_the_other_sections_keep_their_rows_at_every_height(self):
        from unittest import mock
        with mock.patch.dict(os.environ):
            from tools.fleet.tests import readme_captures, readme_demo, readme_panel
            readme_captures.isolate()
            snapshot, hosts = readme_demo.snapshot()
            world = readme_panel.World(snapshot, readme_demo.NOW, hosts)
            payload = readme_panel.projection(world, "claude", readme_panel.API, columns=40, rows=46, side=True)
        panel = payload["panel"]
        self.assertTrue(panel["resources"] and all("brief" in h for h in panel["resources"]))
        older = {**panel, "resources": [{k: v for k, v in h.items() if k != "brief"} for h in panel["resources"]]}
        heights = list(range(16, 65))
        jobs = [{"model": m, "width": 40, "height": h} for h in heights for m in (panel, older)]
        drawn = paint(jobs)
        for i, height in enumerate(heights):
            new, old = drawn[2 * i], drawn[2 * i + 1]
            self.assertEqual(overlapping(new["hits"]), [], height)

            def rest(lines):
                at, rows = block(lines)
                skip = set(range(at, at + 1 + len(rows))) if at is not None else set()
                return [line for j, line in enumerate(lines) if j not in skip and line[1:-1].strip(" ─")]
            mine, theirs = rest(new["lines"]), rest(old["lines"])
            self.assertGreaterEqual(len(mine), len(theirs), (height, "\n".join(new["lines"]), "\n".join(old["lines"])))
            heads = lambda lines: [line[1:-1].split()[0] for line in lines if line.startswith("│ ") and line[2:3].isupper()]
            self.assertLessEqual(set(heads(theirs)), set(heads(mine)), height)
            self.assertLessEqual(len(block(new["lines"])[1]), max(1, len(block(old["lines"])[1])), height)


if __name__ == "__main__":
    unittest.main()
