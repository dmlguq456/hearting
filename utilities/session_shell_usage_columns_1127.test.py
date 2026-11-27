"""11-27 (the person: "그리고 USAGES는 좀 더 이쁘게 행과열을 맞추고"; of the design's two, "USAGE 공통 열"; told: "폭 44 · 60에서 USAGE 단추가 한 줄
아래로 내려가 한 줄을 더 씁니다. 폭 36에서는 Claude 줄의 모델 값이 빠집니다").

The three harness rows share one set of columns, chosen once: the union of the windows they report (5h, 7d, then the model windows in the
order seen), each the same width in every row: its name left, the value right-aligned in four cells (`?` where the row reports none), and,
where some row has a reset time for it, a time column (`(↻ ?)` where a row has the value and no time). Short of cells the time columns go
in every row together, then the model windows from the last; 5h and 7d stay side by side. The last row is not laid out again beside the
buttons. The expected rows are the design's ``usage/A`` renders (PANEL-MEM-RESOURCES-USAGE-RENDERS-1127).
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
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT)]
NOW = 1_800_000_000.0

# The design's usage (fictional): Claude 5h, 7d and opus; Codex 5h and a 7d with no reset time; OpenCode a 7d alone.
SOURCES = {
    "claude": {"quota_windows": [("5h", 9, NOW + 45 * 60), ("7d", 72, NOW + 3 * 86400)], "model_quotas": [("opus", 86)]},
    "codex": {"quota_windows": [("5h", 100, NOW + 100 * 60), ("7d", 12, None)], "model_quotas": []},
    "opencode": {"quota_windows": [("7d", 8, NOW + 86400)], "model_quotas": []},
}
# The design's `usage/A` (the head, the three rows, the buttons' row where the last row does not share it).
DESIGN = {
    36: ["│ USAGE                            │",
         "│ claude   5h   9%  7d  72%        │",
         "│ codex    5h 100%  7d  12%        │",
         "│ opencode 5h    ?  7d   8%  ⚙   ▾ │"],
    44: ["│ USAGE                                    │",
         "│ claude   5h   9%  7d  72%  opus  86%     │",
         "│ codex    5h 100%  7d  12%  opus    ?     │",
         "│ opencode 5h    ?  7d   8%  opus    ?     │",
         "│                                    ⚙   ▾ │"],
    60: ["│ USAGE                                                    │",
         "│ claude   5h   9%(↻ 45m)     7d  72%(↻ 3d 0h)  opus  86%  │",
         "│ codex    5h 100%(↻ 1h 40m)  7d  12%(↻ ?)      opus    ?  │",
         "│ opencode 5h    ?            7d   8%(↻ 1d 0h)  opus    ?  │",
         "│                                                    ⚙   ▾ │"],
}
# The design's columns (the left border is column 1): where each window's name starts, where its value ends, where its time starts.
COLUMNS = {36: {"5h": (12, 18, None), "7d": (21, 27, None)},
           44: {"5h": (12, 18, None), "7d": (21, 27, None), "opus": (30, 38, None)},
           60: {"5h": (12, 18, 19), "7d": (31, 37, 38), "opus": (49, 57, None)}}

NODE = r"""
const path=require('node:path'),fs=require('node:fs');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const jobs=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(jobs.map(job=>{
  const r=view.render({width:job.width,height:job.height||40,side:true,model:job.model,state:'idle',harness:job.harness||'claude',frame:0,since:0,
    brief:()=>null,sent:()=>[],cellWidth:cells,configuration:{}});
  return {rows:r.rows.map(row=>row.map(p=>({text:p.text,fg:p.foreground??null,dim:p.dim===true,bold:p.bold===true}))),hits:r.hits};
})));
"""


def usage(sources=SOURCES, *, kinds=True):
    """The USAGE rows as the backend sends them (``usage_rows``: the parts and, 11-28, each window's kind)."""
    from fleet import palette, session_panel
    with mock.patch("time.time", return_value=NOW):
        rows = [{"harness": name, "parts": session_panel.quota_row(sources[name]),
                 "columns": [kind for kind, _parts in session_panel.quota_windows_parts(sources[name])]}
                for name in ("claude", "codex", "opencode")]
    styled = lambda parts: [{"text": text, **palette.style_components(key)} for text, key in parts]  # noqa: E731
    keys = ("dim", "loc_repo", "lvl_g", "lvl_y", "lvl_r")
    return {"styles": {key: palette.style_components(key) for key in keys}, "colors": 256,
            "usage": [{"harness": row["harness"], "parts": styled(row["parts"]), **({"columns": row["columns"]} if kinds else {})}
                      for row in rows],
            "quota": styled(rows[0]["parts"])}


def paint(jobs):
    done = subprocess.run(["node", "-e", NODE, str(SHELL)], input=json.dumps(jobs), capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise AssertionError(done.stdout + done.stderr)
    return json.loads(done.stdout)


def text(row):
    return "".join(part["text"] for part in row)


def section(drawn):
    lines = [text(row) for row in drawn["rows"]]
    at = next(i for i, line in enumerate(lines) if line.startswith("│ USAGE"))
    return at, lines[at:-1], drawn["rows"][at:-1]


class UsageColumnsTest(unittest.TestCase):
    def test_the_three_rows_read_as_the_design_at_36_44_and_60(self):
        for (width, want), drawn in zip(DESIGN.items(), paint([{"model": usage(), "width": w} for w in DESIGN])):
            _at, lines, _rows = section(drawn)
            self.assertEqual(lines, want, width)

    def test_every_window_starts_and_ends_on_the_same_column_in_every_row(self):
        for (width, columns), drawn in zip(COLUMNS.items(), paint([{"model": usage(), "width": w} for w in COLUMNS])):
            _at, lines, _rows = section(drawn)
            for line in lines[1:4]:
                for label, (start, end, time) in columns.items():
                    self.assertEqual(line[start - 1:start - 1 + len(label)], label, (width, line))
                    # The value (a percent, or `?` where the row reports none) ends on the column's last cell.
                    self.assertNotEqual(line[end - 1], " ", (width, label, line))
                    self.assertIn(line[end], " (" if time else " ", (width, label, line))
                    if time:
                        self.assertIn(line[time - 1], "( ", (width, label, line))
                # Nothing of the window shown is past the panel's own width; no value is made 0.
                self.assertEqual(len(line), width)
            self.assertNotIn(" 0%", "\n".join(lines))

    def test_an_unknown_value_reads_a_dim_question_mark_and_a_missing_time_a_dim_one(self):
        drawn, = paint([{"model": usage(), "width": 60}])
        _at, lines, rows = section(drawn)
        marks = [part for row in rows for part in row if part["text"] in ("?", "(↻ ?)")]
        self.assertEqual(sorted(part["text"] for part in marks), ["(↻ ?)", "?", "?", "?"])
        self.assertTrue(all(part["dim"] for part in marks))
        # Codex's 7d has a value and no reset time: `(↻ ?)`; OpenCode's 5h has no value: no time either.
        self.assertIn("12%(↻ ?)", lines[2])
        self.assertNotIn("?(↻", lines[3])
        # The values keep their own colors (Fleet's levels) and the model window's name its own.
        from fleet import palette
        claude = rows[1]
        self.assertEqual(next(p for p in claude if p["text"] == "86%")["fg"], palette.style_components("lvl_r")["foreground"])
        self.assertEqual(next(p for p in claude if p["text"] == "9%")["fg"], palette.style_components("lvl_g")["foreground"])
        opus = next(p for p in claude if p["text"].startswith("opus"))
        self.assertFalse(opus["dim"])
        self.assertTrue(all(p["dim"] for p in rows[2] if p["text"].startswith("opus")), "an unknown model value: its name dim")

    def test_short_of_cells_the_times_go_together_then_the_model_windows_from_the_last(self):
        two = {**SOURCES, "claude": {**SOURCES["claude"], "model_quotas": [("opus", 86), ("sonnet", 40)]}}
        drawn = paint([{"model": usage(two), "width": w} for w in (60, 44, 36, 80)])
        rows = [section(d)[1] for d in drawn]
        at60, at44, at36, at80 = rows
        # 60: both models stand and every time column goes (the times go before any window does).
        self.assertTrue(all("opus" in line and "sonnet" in line and "↻" not in line for line in at60[1:4]), at60)
        # 44: the last model goes first; 36: the other too; 5h and 7d stand side by side to the end.
        self.assertTrue(all("opus" in line and "sonnet" not in line for line in at44[1:4]), at44)
        self.assertTrue(all("5h" in line and "7d" in line and "opus" not in line for line in at36[1:4]), at36)
        # 80: room for all of it, the times too.
        self.assertTrue(all("sonnet" in line for line in at80[1:4]) and "(↻ 45m)" in at80[1], at80)

    def test_the_last_row_keeps_the_columns_and_the_buttons_their_cells(self):
        for width, drawn in zip((36, 44, 60), paint([{"model": usage(), "width": w} for w in (36, 44, 60)])):
            at, lines, _rows = section(drawn)
            last = len(drawn["rows"]) - 2
            # The gear and the fold button: the last row inside, the right end, as before.
            self.assertEqual(sorted(h["action"] for h in drawn["hits"] if h["row"] == last), ["fold", "settings"])
            self.assertTrue(lines[-1].endswith("⚙   ▾ │"), lines[-1])
            # 36: the last harness row shares it; 44 and 60: the buttons have their own row (one row more).
            self.assertEqual(lines[-1].startswith("│ opencode"), width == 36, (width, lines))
            self.assertEqual(len(lines), 4 if width == 36 else 5)
            # No other press on a usage row.
            self.assertFalse([h for h in drawn["hits"] if at < h["row"] < last])


# Review of #1911 (fix 1, 11-28): windows of time the producers name freely (Codex's `4w` and `45s`, Zen Go's `wk` and `mo`) and two model
# windows. The kinds come from the producer; a window of time with a value never gives way to a model window.
REVIEW = {
    "claude": {"quota_windows": [("5h", 9, NOW + 45 * 60), ("7d", 72, NOW + 3 * 86400)], "model_quotas": [("opus", 86), ("sonnet", 40)]},
    "codex": {"quota_windows": [("5h", 24, NOW + 100 * 60), ("7d", 41, None), ("4w", 55, None), ("45s", 50, None)], "model_quotas": []},
    "opencode": {"quota_windows": [("wk", 40, NOW + 86400), ("mo", 73, None)], "model_quotas": []},
}


class UsageFix1Test(unittest.TestCase):
    def test_the_row_reads_as_before_and_the_producer_names_each_windows_kind(self):
        from fleet import session_panel
        with mock.patch("time.time", return_value=NOW):
            row = session_panel.quota_row(REVIEW["claude"])
            windows = session_panel.quota_windows_parts(REVIEW["codex"])
        self.assertEqual("".join(t for t, _k in row), "5h 9%(↻ 45m) │ 7d 72%(↻ 3d 0h) │ opus 86% │ sonnet 40%")
        self.assertEqual([kind for kind, _parts in windows], ["period"] * 4)
        with mock.patch("time.time", return_value=NOW), mock.patch("fleet.collectors.usage_cache.read",
                                                                     lambda name: {"payload": {"rl_windows": [["wk", 40, None], ["mo", 73, None]]}}):
            rows = session_panel.usage_rows("claude", REVIEW["claude"])
        self.assertEqual([r["columns"] for r in rows], [["period", "period", "model", "model"], ["period", "period"], ["period", "period"]])

    def test_no_window_of_time_with_a_value_leaves_the_screen(self):
        drawn = paint([{"model": usage(REVIEW), "width": w} for w in (36, 44, 60)])
        for width, d in zip((36, 44, 60), drawn):
            _at, lines, _rows = section(d)
            for name, source in REVIEW.items():
                line = next(l for l in lines if l.startswith("│ " + name))
                labels = [label for label, _used, _reset in source["quota_windows"]]
                if (width, name) == (36, "codex"):
                    # Four windows of time are more than 32 cells hold on one row (34 cells without a rule between them): the last
                    # goes, 5h and 7d stay. (A decision for the person, in the report.)
                    labels = labels[:3]
                    self.assertNotIn("45s", line)
                for label, used, _reset in source["quota_windows"]:
                    if label in labels:
                        self.assertRegex(line, r"(^|[ │]){} +{}%".format(label, used), (width, line))
            # The model windows give way first: the second at 36 and 44, the first at none of these widths.
            claude = next(l for l in lines if l.startswith("│ claude"))
            self.assertEqual(("opus" in claude, "sonnet" in claude), (True, width == 60), (width, claude))

    def test_a_backend_that_names_no_kinds_draws_each_row_as_before(self):
        d, = paint([{"model": usage(kinds=False), "width": 60}])
        _at, lines, _rows = section(d)
        self.assertEqual(lines[1:4], ["│ claude   5h 9%(↻ 45m) │ 7d 72%(↻ 3d 0h) │ opus 86%       │",
                                      "│ codex    5h 100%(↻ 1h 40m) │ 7d 12%                      │",
                                      "│ opencode 7d 8%(↻ 1d 0h)                            ⚙   ▾ │"])

    def test_the_buttons_row_of_its_own_costs_no_other_part_a_row(self):
        from fleet import palette, session_panel
        from tools.fleet.tests import readme_captures, readme_demo, readme_panel
        with mock.patch.dict(os.environ):
            readme_captures.isolate()
            snapshot, hosts = readme_demo.snapshot()
            world = readme_panel.World(snapshot, readme_demo.NOW, hosts)
            panel = readme_panel.projection(world, "claude", readme_panel.API, columns=44, rows=46, side=True)["panel"]
        st = lambda parts: [{"text": t, **palette.style_components(k)} for t, k in parts]  # noqa: E731
        six = [{"name": n, "self": False, "cpu": 30, "mem": 40,
                "gpus": [{"i": 0, "busy": True, "util": 50, "used": 21, "total": 24, "users": ["4f"]}]} for n in "abcdef"]
        six = [{"host": st(r["host"]), "stats": [st(i) for i in r["stats"]], "gpus": [{k: st(v) for k, v in g.items()} for g in r["gpus"]],
                "brief": [st(i) for i in r["brief"]]} for r in session_panel.resource_rows({"observed_at": 1.0, "hosts": six}, 1.0)]
        ours = usage()["usage"]
        # What USAGE cost before the shared columns: the same rows, the last beside the buttons (no model window to make it longer).
        same = usage({**SOURCES, "claude": {**SOURCES["claude"], "model_quotas": []}})["usage"]
        jobs, keys = [], []
        for servers, resources in (("0", []), ("1", panel["resources"][:1]), ("2", panel["resources"][:2]), ("6", six)):
            for width in (44, 60):
                for height in range(16, 65):
                    for which, rows in (("ours", ours), ("before", same)):
                        jobs.append({"model": {**panel, "usage": rows, "quota": rows[0]["parts"], "resources": resources},
                                     "width": width, "height": height})
                        keys.append((servers, width, height, which))
        drawn = dict(zip(keys, paint(jobs)))
        own_row = set()
        for servers, width, height, which in keys:
            if which != "ours":
                continue
            ours_d, before_d = drawn[(servers, width, height, "ours")], drawn[(servers, width, height, "before")]

            def rest(d):
                lines = [text(row) for row in d["rows"]]
                at = next((i for i, line in enumerate(lines) if line.startswith("│ USAGE")), len(lines) - 1)
                return [line for line in lines[1:at] if line[1:-1].strip(" ─")]
            self.assertGreaterEqual(len(rest(ours_d)), len(rest(before_d)), (servers, width, height))
            # The gear and the fold button: on the panel's last row inside, their cells as before, in either form.
            last = len(ours_d["rows"]) - 2
            self.assertEqual(sorted((h["action"], h["left"], h["right"]) for h in ours_d["hits"] if h["row"] == last),
                             [("fold", width - 4, width - 2), ("settings", width - 8, width - 6)], (servers, width, height))
            line = text(ours_d["rows"][last])
            self.assertTrue(line.endswith("⚙   ▾ │"), line)
            if not line.startswith("│ opencode"):
                own_row.add((width, height))
        # Tall enough, the buttons have their own row (the person's form at 44 and 60); short of rows, they share the last.
        self.assertIn((44, 64), own_row)
        self.assertIn((60, 64), own_row)
        self.assertNotIn((44, 16), own_row)


if __name__ == "__main__":
    unittest.main()
