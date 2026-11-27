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


def usage(sources=SOURCES):
    from fleet import palette, session_panel
    with mock.patch("time.time", return_value=NOW):
        rows = [{"harness": name, "parts": session_panel.quota_row(sources[name])} for name in ("claude", "codex", "opencode")]
    styled = lambda parts: [{"text": text, **palette.style_components(key)} for text, key in parts]  # noqa: E731
    keys = ("dim", "loc_repo", "lvl_g", "lvl_y", "lvl_r")
    return {"styles": {key: palette.style_components(key) for key in keys}, "colors": 256,
            "usage": [{"harness": row["harness"], "parts": styled(row["parts"])} for row in rows],
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


if __name__ == "__main__":
    unittest.main()
