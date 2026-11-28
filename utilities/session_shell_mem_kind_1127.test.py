"""11-27 (the person, on the MEM row: "그러면 그 앞의 종류가 중요한데 그거 에따라서 컬러를 부여해주고"; of the design's two, "MEM 가(세 무리)").

The kind word of a MEM record (the panel's row and the MEM popup's head) takes the color of its group, nothing else changes:
* ``work``: a preference carrying a ``unit:``/``field:`` label in its capsule topics (the labels a worker's prompt is given
  records by): the request zone's teal (``zone_request``);
* ``guide``: another type the worker bootstrap's own type fence takes (``mem_base.BOOTSTRAP_TYPE_FENCE``, read and evaluated as
  it is): the dispatch zone's violet (``zone_dispatch``); an unlabelled preference is one;
* every other type (``reference``, ``fact``, ``project``, ``artifact-pointer``, ``lesson`` …) and any type the fence cannot be
  read for: dim, as before.
The evidence is the shown record as stored now (fix 1, 11-28): the read-only query that gives its headline gives its stored type
and topics too, for the record the row shows (the replacement of a superseded one); the type is judged whole, only the word drawn is
cut. Where the read has no such record, the journal row's type with no labels. The memory system's writes and journal are untouched.
The cells, the words, the cuts (``correction``, ``artifact-po…``), the narrow rule that drops the kind, and a colorless terminal
are as they were. The expected rows are the design's ``mem/A`` and ``popup/A`` renders (PANEL-MEM-RESOURCES-USAGE-RENDERS-1127).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT)]

# The design's four record sets (and the colorless one, the first set again), as the backend sends them.
SETS = {
    "1": [("preference", "durable", "global", "2m", "Review one change per pass.", "work"),
          ("decision", "working", "project", "8m", "Keep limits in the gateway.", "guide"),
          ("reference", "durable", "global", "14m", "See the quota notes.", "")],
    "2": [("feedback", "durable", "global", "2m", "Lead with the test result.", "guide"),
          ("convention", "working", "project", "8m", "Use UTC in reports.", "guide"),
          ("correction", "durable", "global", "14m", "Keep the original title.", "guide")],
    "3": [("fact", "durable", "global", "2m", "The fixture has four GPUs.", ""),
          ("project", "working", "project", "8m", "Demo API migration.", ""),
          ("artifact-po…", "durable", "global", "14m", "Open the sample chart.", "")],
    "4": [("preference", "durable", "global", "2m", "Use short chart captions.", "work"),
          ("preference", "working", "project", "8m", "Prefer a short summary.", "guide"),
          ("lesson", "durable", "global", "14m", "Pin the demo clock.", "")],
}
# The design's `mem/A` rows (the same cells as `mem/now`).
ROWS = {
    36: {"1": ["│  ◆ 2m preference Review one cha… │", "│  ◇ 8m decision Keep limits in t… │", "│  ◆ 14m reference See the quota … │"],
         "2": ["│  ◆ 2m feedback Lead with the te… │", "│  ◇ 8m convention Use UTC in rep… │", "│  ◆ 14m correction Keep the orig… │"],
         "3": ["│  ◆ 2m fact The fixture has four… │", "│  ◇ 8m project Demo API migratio… │", "│  ◆ 14m Open the sample chart.    │"],
         "4": ["│  ◆ 2m preference Use short char… │", "│  ◇ 8m preference Prefer a short… │", "│  ◆ 14m lesson Pin the demo cloc… │"]},
    44: {"1": ["│  ◆ 2m preference Review one change per … │", "│  ◇ 8m decision Keep limits in the gatew… │", "│  ◆ 14m reference See the quota notes.    │"],
         "2": ["│  ◆ 2m feedback Lead with the test resul… │", "│  ◇ 8m convention Use UTC in reports.     │", "│  ◆ 14m correction Keep the original tit… │"],
         "3": ["│  ◆ 2m fact The fixture has four GPUs.    │", "│  ◇ 8m project Demo API migration.        │", "│  ◆ 14m artifact-po… Open the sample cha… │"],
         "4": ["│  ◆ 2m preference Use short chart captio… │", "│  ◇ 8m preference Prefer a short summary. │", "│  ◆ 14m lesson Pin the demo clock.        │"]},
    60: {"1": ["│  ◆ 2m preference Review one change per pass.             │", "│  ◇ 8m decision Keep limits in the gateway.               │", "│  ◆ 14m reference See the quota notes.                    │"],
         "2": ["│  ◆ 2m feedback Lead with the test result.                │", "│  ◇ 8m convention Use UTC in reports.                     │", "│  ◆ 14m correction Keep the original title.               │"],
         "3": ["│  ◆ 2m fact The fixture has four GPUs.                    │", "│  ◇ 8m project Demo API migration.                        │", "│  ◆ 14m artifact-po… Open the sample chart.               │"],
         "4": ["│  ◆ 2m preference Use short chart captions.               │", "│  ◇ 8m preference Prefer a short summary.                 │", "│  ◆ 14m lesson Pin the demo clock.                        │"]},
}
# The design's `popup/A` heads (popup frames 32, 40 and 56 cells: the windows 36, 44 and 60).
HEADS = {
    36: {"1": ["◆ global · preference · 2m", "◇ project · decision · 8m", "◆ global · reference · 14m"],
         "3": ["◆ global · fact · 2m", "◇ project · project · 8m", "◆ global · artifact-po… · 14"]},
    60: {"2": ["◆ global · feedback · 2m", "◇ project · convention · 8m", "◆ global · correction · 14m"],
         "4": ["◆ global · preference · 2m", "◇ project · preference · 8m", "◆ global · lesson · 14m"]},
}

NODE = r"""
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const job=JSON.parse(fs.readFileSync(0,'utf8'));
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
// The backend's styles (`palette.style_components`): the zones' 256-color hues, `dim` dim; colorless, none of them.
const styles=job.mono?{dim:{},loc_repo:{},zone_request:{},zone_dispatch:{}}:{dim:{dim:true},loc_repo:{foreground:253},zone_request:{foreground:43},zone_dispatch:{foreground:99}};
const out={};
for(const [name,set] of Object.entries(job.sets)){
  const items=set.map(([kind,tier,scope,age,text,group])=>({kind,tier,scope,age,text,body:text+' Check the example.',...(job.groups?{group}:{})}));
  const model={styles,colors:256,no_color:job.mono===true,memory_items:items};
  for(const width of job.widths){
    const r=view.render({width,height:40,side:true,model,state:'idle',harness:'claude',frame:0,since:0,brief:()=>null,sent:()=>[],cellWidth:cells,configuration:{}});
    const lines=r.rows.map(row=>row.map(p=>p.text).join(''));
    const at=lines.findIndex(s=>/^│ MEM +› │$/.test(s));
    const rows=at<0?[]:r.rows.slice(at+1,at+4);
    const cols=width,inner=view.popupWidth(cols,'table')-4;
    const body=view.memoryBody({model,columns:inner,cellWidth:cells});
    const shown=view.popup({cols,rows:50,band:0,kind:'table',title:[{text:'MEM',dim:true}],body,sections:['MEM'],current:0,model,configuration:{},cellWidth:cells,background:'dark'});
    const heads=body.starts.map(i=>body[i]);
    const popupLines=shown.rows.map(row=>row.map(p=>p.text).join(''));
    out[name+':'+width]={rows:rows.map(row=>row.map(p=>({text:p.text,fg:p.foreground??null,dim:p.dim===true,bold:p.bold===true}))),
      heads:heads.map(row=>row.map(p=>({text:p.text,fg:p.foreground??null,dim:p.dim===true}))),
      popup:popupLines.filter(s=>/[◆◇] (global|project) ·/.test(s)).map(s=>s.replace(/^[^│]*│ /,'').replace(/ *│[^│]*$/,'')),
      hits:r.hits.filter(h=>/^popup:MEM/.test(h.action)).map(h=>h.action)};
  }
}
process.stdout.write(JSON.stringify(out));
"""

KEY = {"work": 43, "guide": 99}


def render(sets, widths=(36, 44, 60), *, mono=False, groups=True):
    job = {"sets": sets, "widths": list(widths), "mono": mono, "groups": groups}
    done = subprocess.run(["node", "-e", NODE, str(SHELL)], input=json.dumps(job), capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise AssertionError(done.stdout + done.stderr)
    return json.loads(done.stdout)


def text(row):
    return "".join(part["text"] for part in row)


class MemKindColorTest(unittest.TestCase):
    def test_the_rows_keep_their_cells_and_the_kind_word_alone_takes_its_groups_color(self):
        drawn = render(SETS)
        for width, sets in ROWS.items():
            for name, want in sets.items():
                got = drawn[f"{name}:{width}"]
                self.assertEqual([text(row) for row in got["rows"]], want, (width, name))
                for (kind, _tier, _scope, _age, headline, group), row in zip(SETS[name], got["rows"]):
                    shown = f" {kind} " in text(row).split(headline[:8])[0]
                    colored = [part for part in row if part["fg"] in (43, 99)]
                    if shown and group:
                        # The kind word alone, its group's hue, not dim and not bold.
                        self.assertEqual([(p["text"], p["fg"], p["dim"], p["bold"]) for p in colored], [(kind, KEY[group], False, False)], (width, name, row))
                    else:
                        self.assertEqual(colored, [], (width, name, row))
                    # The mark, the age and the space after the kind stay dim; the headline keeps the body color.
                    for part in row:
                        if part in colored or not part["text"].strip() or part["text"].startswith("│"):
                            continue
                        if headline.startswith(part["text"].rstrip("…").strip()[:6]):
                            self.assertEqual((part["fg"], part["dim"]), (253, False), (width, name, part))
                        else:
                            self.assertEqual((part["fg"], part["dim"]), (None, True), (width, name, part))
                self.assertEqual(got["hits"], ["popup:MEM", "popup:MEM:0", "popup:MEM:1", "popup:MEM:2"])

    def test_the_popup_head_colors_the_same_word_and_keeps_its_text(self):
        drawn = render(SETS)
        for width, sets in HEADS.items():
            for name, want in sets.items():
                got = drawn[f"{name}:{width}"]
                self.assertEqual(got["popup"], want, (width, name))
                for (kind, _tier, scope, age, _headline, group), head in zip(SETS[name], got["heads"]):
                    self.assertEqual(text(head), " · ".join(["◆" if _tier == "durable" else "◇", scope, kind, age]).replace(" · ", " ", 1))
                    colored = [(p["text"], p["fg"]) for p in head if p["fg"] in (43, 99)]
                    self.assertEqual(colored, [(kind, KEY[group])] if group else [], (width, name, head))
                    # (scope `project` and kind `project`: the scope stays dim.)
                    self.assertTrue(all(p["dim"] for p in head if p["fg"] is None), head)

    def test_a_colorless_terminal_and_an_older_backend_draw_the_same_cells_as_before(self):
        plain = render(SETS, mono=True)
        older = render(SETS, groups=False)
        for width, sets in ROWS.items():
            for name, want in sets.items():
                for drawn in (plain, older):
                    got = drawn[f"{name}:{width}"]
                    self.assertEqual([text(row) for row in got["rows"]], want)
                    self.assertFalse(any(p["fg"] in (43, 99) for row in got["rows"] for p in row))
                    self.assertFalse(any(p["fg"] in (43, 99) for row in got["heads"] for p in row))
                # Colorless: no part takes a hue, a weight or dim.
                self.assertFalse(any(p["fg"] is not None or p["dim"] or p["bold"] for row in plain[f"{name}:{width}"]["rows"] for p in row))


class MemGroupTest(unittest.TestCase):
    def setUp(self):
        from tools.fleet import session_panel
        self.panel = session_panel
        session_panel._bootstrap_type_fence.cache_clear()
        session_panel._bootstrap_type.cache_clear()

    tearDown = setUp

    def test_the_fence_is_the_worker_bootstraps_own(self):
        sys.path[:0] = [str(ROOT / "tools/memory")]
        try:
            import mem_base
        finally:
            sys.path.remove(str(ROOT / "tools/memory"))
        self.assertEqual(self.panel._bootstrap_type_fence(), mem_base.BOOTSTRAP_TYPE_FENCE)

    def test_the_three_groups(self):
        group = self.panel.memory_group
        for kind in ("preference", "Preference", "ui-preference"):
            self.assertEqual(group(kind, ["unit:qa/code-review"]), "work", kind)
            self.assertEqual(group(kind, ["Field:Code"]), "work", kind)
            self.assertEqual(group(kind, []), "guide", kind)
            self.assertEqual(group(kind, None), "guide", kind)
            # Only a label of the two kinds is a work-style label; an empty name, or none after the colon, is none.
            self.assertEqual(group(kind, ["topic:charts", "unit:", "field", "unit", "field: "]), "guide", kind)
        for kind in ("feedback", "decision", "convention", "user-correction", "team-convention"):
            self.assertEqual(group(kind, None), "guide", kind)
        # A labelled record of another candidate type is a candidate, not a work-style preference (the design's table).
        self.assertEqual(group("decision", ["field:code"]), "guide")
        for kind in ("reference", "fact", "project", "artifact-pointer", "lesson", "completion", "handoff", "", None):
            self.assertEqual(group(kind, ["field:code"]), "", kind)
        # The type as stored, whole: a long one the fence takes, spaces the fence refuses.
        self.assertEqual(group("fixture-0123456789-0123456789-SELECT-preference", ["field:code"]), "work")
        self.assertEqual(group(" decision ", None), "")

    def test_a_fence_that_cannot_be_read_leaves_every_kind_as_it_was(self):
        with mock.patch.object(self.panel, "_MEM_BASE", Path("/nonexistent/mem_base.py")):
            self.assertEqual(self.panel.memory_group("preference", ["field:code"]), "")
            self.assertEqual(self.panel.memory_group("decision"), "")

    def test_the_items_take_the_shown_records_stored_values(self):
        long = "fixture-0123456789-0123456789-SELECT-preference"
        rows = [{"type": "preference", "tier": "durable", "scope": "global", "ts": "", "snippet": "A labelled one.",
                 "record": {"type": "preference", "topics": ["field:code"]}},
                # Only the record's own value counts: a title naming a label is no label.
                {"type": "preference", "tier": "durable", "scope": "global", "ts": "", "snippet": "unit:qa/code-review field:code",
                 "record": {"type": "preference", "topics": []}},
                # The stored type wins over the write row's; the word drawn is the stored one.
                {"type": "preference", "tier": "durable", "scope": "global", "ts": "", "snippet": "Same body.",
                 "record": {"type": "decision", "topics": ["field:code"]}}]
        items = self.panel.session_memory_items({"memory_recent": rows})
        self.assertEqual([(i["kind"], i["group"]) for i in items], [("preference", "work"), ("preference", "guide"), ("decision", "guide")])
        # The type judged whole, the word cut (32 cells, then the short word's twelve); spaces are no part of the word.
        rows = [{"type": "fact", "ts": "", "snippet": "Long.", "record": {"type": long, "topics": []}},
                {"type": "fact", "ts": "", "snippet": "Spaced.", "record": {"type": " decision ", "topics": []}},
                # No stored record (the store unreadable, the record gone): the write row's type, no labels.
                {"type": "preference", "ts": "", "snippet": "Not found.", "record": None, "labels": ["field:code"]}]
        items = self.panel.session_memory_items({"memory_recent": rows})
        self.assertEqual([(i["kind"], i["group"]) for i in items], [("fixture-012…", "guide"), ("decision", ""), ("preference", "guide")])

    def test_a_long_kind_word_is_colored_whole_in_the_row_and_the_popup(self):
        drawn = render({"5": [("fixture-012…", "durable", "global", "2m", "Long kinds are judged whole.", "guide")]}, widths=(60,))
        got = drawn["5:60"]
        self.assertEqual([(p["text"], p["fg"]) for row in got["rows"] for p in row if p["fg"] in (43, 99)], [("fixture-012…", 99)])
        self.assertEqual([(p["text"], p["fg"]) for row in got["heads"] for p in row if p["fg"] in (43, 99)], [("fixture-012…", 99)])


MEM = ROOT / "tools/memory/mem.py"
# The keys a write row has (the memory system's own, unchanged by the panel).
JOURNAL_KEYS = {"ts", "action", "id", "tier", "scope", "type", "actor", "sid", "snippet", "cwd"}


class StoredRecordTest(unittest.TestCase):
    """The real memory CLI in a private store; the panel reads the shown record through Fleet's own read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mem-kind-")
        root = Path(self.tmp.name)
        self.store, self.project = root / "store", root / "project"
        self.store.mkdir()
        self.project.mkdir()
        self.env = {**os.environ, "MEM_STORE": str(self.store), "MEM_RECALL_EVENTS": str(root / "recall.jsonl"),
                    "MEM_RECALL_RECEIPTS": str(root / "receipts"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.env.pop("MEM_WRITE_EVENTS", None)
        self.environ = mock.patch.dict(os.environ, {"MEM_STORE": str(self.store)})
        self.environ.start()
        os.environ.pop("MEM_WRITE_EVENTS", None)
        from tools.fleet.collectors import memory
        self.memory = memory
        self.clear()

    def tearDown(self):
        self.clear()
        self.environ.stop()
        self.tmp.cleanup()

    def clear(self):
        self.memory._latest_cache.clear()
        self.memory._project_cache.clear()

    def mem(self, *args):
        done = subprocess.run([sys.executable, str(MEM), *args], cwd=self.project, env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def add(self, *args):
        found = re.search(r"(?:→|strength:) (\S+)", self.mem("add", *args))
        return found.group(1)

    def shown(self):
        from tools.fleet import session_panel, session_work
        self.clear()
        rows, _paths = session_work.recent_memory(str(self.project), session_panel.MEMORY_ITEMS)
        work, _paths = session_work.observe(str(self.project), tuple(row["headline_id"] for row in rows if row.get("headline_id")))
        return session_panel.session_memory_items({"memory_recent": session_work.shown_records(rows, work)})

    def journal(self):
        text = (self.store / "write-events.jsonl").read_text(encoding="utf-8")
        return text, [json.loads(line) for line in text.split("\n") if line.strip()]

    def test_the_same_body_written_as_another_type_reads_as_stored(self):
        self.add("durable", "decision", "Keep the limiter in the gateway.", "--scope", "global", "--topic", "field:code")
        self.add("durable", "preference", "Keep the limiter in the gateway.", "--scope", "global")
        _text, events = self.journal()
        self.assertEqual(events[-1]["type"], "preference")   # the write row names the asked type ...
        items = self.shown()
        self.assertEqual([(i["kind"], i["group"]) for i in items], [("decision", "guide")])   # ... the record stays a decision

    def test_a_labelled_replacement_reads_teal(self):
        old = self.add("durable", "preference", "Write the summary first.", "--scope", "global")
        new = self.add("durable", "preference", "Write the summary first, then the numbers.", "--scope", "global", "--topic", "field:code")
        self.mem("supersede", old, "--by", new)
        items = self.shown()
        self.assertEqual(items[0]["group"], "work", items)
        self.assertTrue(items[0]["text"].startswith("Write the summary first, then"), items)

    def test_an_existing_record_labelled_later_reads_teal_without_a_new_write(self):
        rid = self.add("durable", "preference", "Use short chart captions.", "--scope", "global")
        self.assertEqual([i["group"] for i in self.shown()], ["guide"])
        con = sqlite3.connect(self.store / "memory.db")
        con.execute("UPDATE records SET topics=? WHERE id=?", (json.dumps(["unit:docs/write"]), rid))
        con.commit()
        con.close()
        self.assertEqual([i["group"] for i in self.shown()], ["work"])

    def test_a_label_with_a_line_separator_leaves_the_journal_and_the_log_as_they_are(self):
        rid = self.add("durable", "preference", "Keep a plain body that reads well on its own.", "--scope", "global", "--topic", "field:demo\u2028split")
        text, events = self.journal()
        self.assertNotIn("\u2028", text)
        self.assertEqual(set(events[-1]), JOURNAL_KEYS)
        self.assertIn(rid, self.mem("log", "--limit", "5"))
        self.assertEqual([i["group"] for i in self.shown()], ["work"])

    def test_a_store_that_cannot_be_read_leaves_the_write_rows_type(self):
        self.add("durable", "preference", "Prefer a short summary.", "--scope", "global", "--topic", "field:code")
        self.add("durable", "decision", "Keep limits in the gateway.", "--scope", "global")
        self.add("durable", "fact", "The fixture has four GPUs.", "--scope", "global")
        self.assertEqual([i["group"] for i in self.shown()], ["", "guide", "work"])
        (self.store / "memory.db").rename(self.store / "memory.db.away")
        self.assertEqual([(i["kind"], i["group"]) for i in self.shown()], [("fact", ""), ("decision", "guide"), ("preference", "guide")])


if __name__ == "__main__":
    unittest.main()
