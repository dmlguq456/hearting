"""11-27 (the person: "그리고 USAGES는 좀 더 이쁘게 행과열을 맞추고"; of the design's two, "USAGE 공통 열"; told: "폭 44 · 60에서 USAGE 단추가 한 줄
아래로 내려가 한 줄을 더 씁니다. 폭 36에서는 Claude 줄의 모델 값이 빠집니다").

The three harness rows share one set of columns, chosen once: the union of the windows they report (5h, 7d, then the model windows in the
order seen), each the same width in every row: its name left, the value right-aligned in four cells (`?` where the row reports none), and,
where some row has a reset time for it, a time column (`(↻ ?)` where a row has the value and no time). Short of cells the time columns go
in every row together, then the model windows from the last; 5h and 7d stay side by side. The last row is not laid out again beside the
buttons. The expected rows are the design's ``usage/A`` renders (PANEL-MEM-RESOURCES-USAGE-RENDERS-1127).

11-28 (the person, on v4.81.0 with the windows the harnesses really report: "usage에 상태 파악해라 진짜 최악이네"): the columns stand only
where every row reports the same windows. The design drew three rows with different windows (OpenCode no 5h, only Claude a model window)
and filled the gaps with `?`; with real usage nine of fifteen cells were `?`. Where the windows differ each row is fitted on its own, as
v4.80.0 drew it (``RealShapesTest``); the design's columns are checked on rows that report the same windows (``SHARED``).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import re
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
# 11-28: the same windows in every row (5h, 7d, opus; Codex's 7d with no reset time), so the columns stand.
SHARED = {
    "claude": SOURCES["claude"],
    "codex": {**SOURCES["codex"], "model_quotas": [("opus", 40)]},
    "opencode": {"quota_windows": [("5h", 3, NOW + 2 * 3600), ("7d", 8, NOW + 86400)], "model_quotas": [("opus", 20)]},
}
# The design's `usage/A` (the head, the three rows, the buttons' row where the last row does not share it), on ``SHARED``.
DESIGN = {
    36: ["│ USAGE                            │",
         "│ claude   5h   9%  7d  72%        │",
         "│ codex    5h 100%  7d  12%        │",
         "│ opencode 5h   3%  7d   8%  ⚙   ▾ │"],
    44: ["│ USAGE                                    │",
         "│ claude   5h   9%  7d  72%  opus  86%     │",
         "│ codex    5h 100%  7d  12%  opus  40%     │",
         "│ opencode 5h   3%  7d   8%  opus  20%     │",
         "│                                    ⚙   ▾ │"],
    60: ["│ USAGE                                                    │",
         "│ claude   5h   9%(↻ 45m)     7d  72%(↻ 3d 0h)  opus  86%  │",
         "│ codex    5h 100%(↻ 1h 40m)  7d  12%(↻ ?)      opus  40%  │",
         "│ opencode 5h   3%(↻ 2h 0m)   7d   8%(↻ 1d 0h)  opus  20%  │",
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


def usage(sources=SHARED, *, kinds=True):
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

    def test_a_missing_time_reads_a_dim_mark_and_no_value_is_made_up(self):
        drawn, = paint([{"model": usage(), "width": 60}])
        _at, lines, rows = section(drawn)
        marks = [part for row in rows for part in row if part["text"] in ("?", "(↻ ?)")]
        # 11-28: the same windows in every row: no window is drawn as an unknown value; Codex's 7d has a value and no reset time.
        self.assertEqual([part["text"] for part in marks], ["(↻ ?)"])
        self.assertTrue(all(part["dim"] for part in marks))
        self.assertIn("12%(↻ ?)", lines[2])
        # The values keep their own colors (Fleet's levels) and the model window's name its own.
        from fleet import palette
        claude = rows[1]
        self.assertEqual(next(p for p in claude if p["text"] == "86%")["fg"], palette.style_components("lvl_r")["foreground"])
        self.assertEqual(next(p for p in claude if p["text"] == "9%")["fg"], palette.style_components("lvl_g")["foreground"])
        opus = next(p for p in claude if p["text"].startswith("opus"))
        self.assertFalse(opus["dim"])

    def test_short_of_cells_the_times_go_together_then_the_model_windows_from_the_last(self):
        two = {name: {**source, "model_quotas": [*source["model_quotas"], ("sonnet", 40)]} for name, source in SHARED.items()}
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
        # (11-28: so does a backend that names them where the rows report different windows, the design's own rows among them.)
        for kinds, d in zip((False, True), paint([{"model": usage(SOURCES, kinds=kinds), "width": 60} for kinds in (False, True)])):
            _at, lines, _rows = section(d)
            self.assertEqual(lines[1:4], ["│ claude   5h 9%(↻ 45m) │ 7d 72%(↻ 3d 0h) │ opus 86%       │",
                                          "│ codex    5h 100%(↻ 1h 40m) │ 7d 12%                      │",
                                          "│ opencode 7d 8%(↻ 1d 0h)                            ⚙   ▾ │"], kinds)

    def test_a_taller_panel_never_hides_a_usage_value_or_moves_the_buttons(self):
        # Fix 2 (11-28, review of #1911): the buttons stand beside the last row where it fits there as it is, else on a row of their own,
        # whatever the height. Rows from 16 to 64, one at a time: a value USAGE showed once stays, the buttons keep their place.
        # (The review's case: 44 x 45, 46, 47 and 60 x 43, 44, 45 with two servers lost and found `opus 86%`.)
        from tools.fleet.tests import readme_captures, readme_demo, readme_panel
        with mock.patch.dict(os.environ):
            readme_captures.isolate()
            snapshot, hosts = readme_demo.snapshot()
            world = readme_panel.World(snapshot, readme_demo.NOW, hosts)
            panel = readme_panel.projection(world, "claude", readme_panel.API, columns=44, rows=46, side=True)["panel"]
        rows = usage()["usage"]
        jobs, keys = [], []
        for servers, resources in (("0", []), ("2", panel["resources"][:2])):
            for width in (44, 60):
                for height in range(16, 65):
                    jobs.append({"model": {**panel, "usage": rows, "quota": rows[0]["parts"], "resources": resources},
                                 "width": width, "height": height})
                    keys.append((servers, width, height))
        drawn = dict(zip(keys, paint(jobs)))
        for servers in ("0", "2"):
            for width in (44, 60):
                seen, places = set(), set()
                for height in range(16, 65):
                    d = drawn[(servers, width, height)]
                    lines = [text(row) for row in d["rows"]]
                    at = next((i for i, line in enumerate(lines) if line.startswith("│ USAGE")), None)
                    shown = lines[at + 1:-1] if at is not None else []
                    values = {(line.split()[1], label, value) for line in shown if line.split()[1:2] != ["⚙"]
                              for label, value in re.findall(r"(\S+) +(\d+%|\?)", line[10:])}
                    self.assertLessEqual(seen, values, (servers, width, height, "\n".join(shown)))
                    seen |= values
                    last = len(d["rows"]) - 2
                    self.assertEqual(sorted((h["action"], h["left"], h["right"]) for h in d["hits"] if h["row"] == last),
                                     [("fold", width - 4, width - 2), ("settings", width - 8, width - 6)], (servers, width, height))
                    if shown:
                        places.add("beside" if lines[last].startswith("│ opencode") else "own row")
                        if (width, height) in ((44, 45), (44, 46), (44, 47), (60, 43), (60, 44), (60, 45)):
                            self.assertTrue(any("opus  86%" in line for line in shown), (servers, width, height, shown))
                # The person's form at 44 and 60: the buttons on a row of their own, at every height USAGE stands.
                self.assertEqual(places, {"own row"}, (servers, width))


# 11-28 (the person, on v4.81.0: "usage에 상태 파악해라 진짜 최악이네"): the windows the harnesses report today (their kinds and number; the
# values made up): Claude a 7d and a model window, Codex a 7d alone, OpenCode 5h, wk and mo. The rows differ, so each is fitted on its own.
REAL = {
    "claude": {"quota_windows": [("7d", 54, NOW + 3 * 86400 + 3 * 3600)], "model_quotas": [("fable", 36)]},
    "codex": {"quota_windows": [("7d", 62, NOW + 6 * 86400 + 3600)], "model_quotas": []},
    "opencode": {"quota_windows": [("5h", 0, None), ("wk", 0, None), ("mo", 100, None)], "model_quotas": []},
}
# The USAGE rows v4.80.0 (2c4ec6661's panel-view.cjs) drew from the same input: the reset times, `│`, the buttons beside the last row.
BEFORE = {
    44: ["│ USAGE                                    │",
         "│ claude   7d 54%(↻ 3d 3h) │ fable 36%     │",
         "│ codex    7d 62%(↻ 6d 1h)                 │",
         "│ opencode 5h 0% │ wk 0% │ mo 100%   ⚙   ▾ │"],
    50: ["│ USAGE                                          │",
         "│ claude   7d 54%(↻ 3d 3h) │ fable 36%           │",
         "│ codex    7d 62%(↻ 6d 1h)                       │",
         "│ opencode 5h 0% │ wk 0% │ mo 100%         ⚙   ▾ │"],
    60: ["│ USAGE                                                    │",
         "│ claude   7d 54%(↻ 3d 3h) │ fable 36%                     │",
         "│ codex    7d 62%(↻ 6d 1h)                                 │",
         "│ opencode 5h 0% │ wk 0% │ mo 100%                   ⚙   ▾ │"],
    80: ["│ USAGE                                                                        │",
         "│ claude   7d 54%(↻ 3d 3h) │ fable 36%                                         │",
         "│ codex    7d 62%(↻ 6d 1h)                                                     │",
         "│ opencode 5h 0% │ wk 0% │ mo 100%                                       ⚙   ▾ │"],
    # 36: the last row cannot hold OpenCode's three windows beside the buttons. v4.80.0 guessed `wk` and `mo` were model windows and gave
    # 5h up (`opencode wk 0% │ mo 100%`); since #1911 the producer's kinds keep a window of time over the later ones, so `mo` goes.
    36: ["│ USAGE                            │",
         "│ claude   7d 54% │ fable 36%      │",
         "│ codex    7d 62%(↻ 6d 1h)         │",
         "│ opencode 5h 0% │ wk 0%     ⚙   ▾ │"],
}


class RealShapesTest(unittest.TestCase):
    def test_rows_with_different_windows_read_as_v4_80_0_drew_them_and_no_window_is_made_up(self):
        widths = sorted(BEFORE)
        for width, drawn in zip(widths, paint([{"model": usage(REAL), "width": w} for w in widths])):
            _at, lines, _rows = section(drawn)
            self.assertEqual(lines, BEFORE[width], width)
            self.assertNotIn("?", "".join(lines), width)
            # The buttons stand beside the last harness row: no row of their own.
            last = len(drawn["rows"]) - 2
            self.assertEqual(sorted(h["action"] for h in drawn["hits"] if h["row"] == last), ["fold", "settings"], width)
            self.assertTrue(lines[-1].startswith("│ opencode"), (width, lines))

    def test_two_rows_alike_and_one_not_each_fit_on_its_own(self):
        alike = {**SHARED, "claude": {**SHARED["claude"], "model_quotas": []}, "codex": {**SHARED["codex"], "model_quotas": []},
                 "opencode": {"quota_windows": [("7d", 8, NOW + 86400)], "model_quotas": []}}
        d, = paint([{"model": usage(alike), "width": 60}])
        self.assertEqual(section(d)[1][1:], ["│ claude   5h 9%(↻ 45m) │ 7d 72%(↻ 3d 0h)                  │",
                                             "│ codex    5h 100%(↻ 1h 40m) │ 7d 12%                      │",
                                             "│ opencode 7d 8%(↻ 1d 0h)                            ⚙   ▾ │"])

    def test_a_harness_reporting_no_window_reads_one_question_mark(self):
        none = {**SHARED, "opencode": {"quota_windows": [], "model_quotas": []}}
        for width, d in zip((36, 60), paint([{"model": usage(none), "width": w} for w in (36, 60)])):
            lines = section(d)[1]
            self.assertEqual("".join(lines).count("?"), 1, (width, lines))
            self.assertRegex(lines[-1], r"^│ opencode \? +⚙   ▾ │$", width)

    def test_a_backend_that_names_no_kinds_reads_as_v4_80_0_too(self):
        widths = sorted(BEFORE)
        for width, drawn in zip(widths, paint([{"model": usage(REAL, kinds=False), "width": w} for w in widths])):
            lines = section(drawn)[1]
            want = BEFORE[width] if width != 36 else [*BEFORE[36][:3], "│ opencode wk 0% │ mo 100%   ⚙   ▾ │"]
            self.assertEqual(lines, want, width)

if __name__ == "__main__":
    unittest.main()
