"""The side ROUTE box lays out Fleet's owner and worker readings in one column."""
import os
import sys

if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):
    os.execv(sys.executable, [
        sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"),
        "--direct-run", sys.executable, *sys.orig_argv[1:],
    ])

import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.fleet import session_panel
from tools.fleet.explorer.grid import cell_width
from tools.fleet.palette import style_components

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "utilities/session-shell/panel-view.cjs"
NOW = 10_000.0
TITLE = "Compare panel widths across the sample layouts"
HARNESSES = ("claude", "codex", "opencode")


def worker(harness, model, effort, context, now, minutes, **extra):
    return {"harness": harness, "model": model, "effort": effort, "ctx_pct": context, "now": now,
            "marker": "working", "role": "worker", "stage": "layout", "since": NOW - 60 * minutes, **extra}


def part(text, key="dim"):
    return {"text": text, **style_components(key)}


def paint(row):
    # The same Fleet fields the shell presents as styled parts.
    row["paint"] = {name: part(row[name], row[key]) for name, key in (
        ("glyph", "glyph_key"), ("harness", "harness_key"), ("model_text", "model_key"),
        ("effort", "effort_key"), ("ctx", "ctx_key"),
    ) if row.get(name)}
    for name in ("gauge", "gauge5", "gauge_row"):
        if row.get(name):
            row["paint"][name] = [part(*piece) for piece in row[name]]
    if row.get("now"):
        row["paint"]["now"] = row["now"]
    if "owner_paint" in row:
        paint(row["owner_paint"])
        row["owner_paint"] = row["owner_paint"]["paint"]
    for child in row.get("workers_live", []) + row.get("called", []):
        paint(child)


def model_for(width, workers=None, projection=session_panel, session=None):
    if workers is None:
        workers = [
            worker("codex", "gpt-6.2-sol", "high", 63, "Read labels", 12, called=[
                {"unit": "review", "model": "Haiku", "harness": "claude", "effort": "medium",
                 "marker": "working", "since": NOW - 180}
            ]),
            worker("opencode", "Atlas-Preview-XL", "", 82, "Check margins", 8),
            worker("claude", "Haiku", "medium", 9, "Count rows", 4),
        ]
    row = {
        "id": "rt-sample", "title": TITLE, "state": "working", "shape": "staged", "capability": "autopilot-code",
        "field": "code", "intensity": "quick", "role": "worker", "elapsed_min": 112,
        "owner_doer": {"harness": "claude", "model": "Opus 5.5", "effort": "xhigh", "ctx_pct": 41,
                       "now": "Compare samples"},
        "stages": [{"name": "plan", "st": "done"}, {"name": "layout", "st": "active"}, {"name": "report", "st": "pending"}],
        "workers_live": workers,
    }
    values = {"harness": "codex", "model": "gpt-6.2-sol", "effort": "high", "tag": "[ab]", "cwd": "/fixture/sample-kit",
              "branch": "main", "git": {"repo": 1, "operation": "none", "ahead": 0, "behind": 0}, "ctx_pct": 34, "title": "Panel samples",
              "now": "Compare two placements", "answer_wait": 0, "route_label": "route 1", **(session or {})}
    with patch("time.time", return_value=NOW):
        model = projection.presentation(values, {"route_panels": {"codex\0s": [row]}}, "codex", "s", width)
    for name in ("head", "repo", "repo_side", "git", "git_side", "worktrees", "selected", "context", "context_side"):
        model[name] = [part(*piece) for piece in model[name]]
    model["context_gauges"] = [[part(*piece) for piece in parts] for parts in model.get("context_gauges", [])]
    paint(model["routes"][0])
    return model


def render_panels(scenes, renderer=RENDERER, parts=False):
    script = r"""
const fs = require('node:fs'), view = require(process.argv[1]), parts = process.argv[2] === 'parts';
const scenes = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(scenes.map(({question,...scene}) => view.render({
  side:true, fill:true, state:'working', harness:'codex', frame:2,
  brief:question ? () => question : undefined, ...scene,
}).rows.map(row => parts ? row.map(({text, foreground}) => ({text, foreground})) : row.map(part => part.text).join('')))));
"""
    result = subprocess.run(["node", "-e", script, str(renderer), "parts" if parts else "text"], input=json.dumps(scenes), text=True,
                            capture_output=True, timeout=30, check=True)
    return json.loads(result.stdout)


def route_rows(panel):
    start = next(i for i, line in enumerate(panel) if "╭─ 1 ROUTE" in line)
    end = next(i for i in range(start + 1, len(panel)) if panel[i].startswith("│ ╰"))
    return panel[start:end + 1]


class RouteContextTest(unittest.TestCase):
    def test_design_columns_and_elapsed_at_the_existing_breakpoints(self):
        widths = (36, 44, 49, 50)
        panels = render_panels([{"width": w, "height": 52, "model": model_for(w)} for w in widths])
        for width, panel in zip(widths, panels):
            with self.subTest(width=width):
                rows = route_rows(panel)
                self.assertFalse(any(re.search(r"\bCTX\b", row) for row in rows), rows)
                doers = [row for row in rows if re.search(r"\b(owner|worker)\b", row)]
                column = width - (4 if width == 36 else 6)
                self.assertEqual([row.index("%") + 1 for row in doers], [column] * 4)
                self.assertEqual([re.search(r"\d+%", row).group() for row in doers], ["41%", "63%", "82%", "9%"])
                gauges = [re.search(r"([━─]+) +\d+%", row) for row in doers]
                self.assertEqual([g[1] if g else "" for g in gauges],
                                 ["━━───", "━━━──", "━━━━─", "─────"] if width == 50 else [""] * 4)
                for now, stamp in (("Read labels", "12m"), ("Check margins", "8m"), ("Count rows", "4m")):
                    row = next(line for line in rows if now in line)
                    self.assertIn(stamp, row)
                    self.assertEqual(row.index(stamp) + len(stamp), column)
                    self.assertTrue(all(stamp not in line for line in doers))
                self.assertRegex(next(line for line in rows if "review" in line), r"\breview\b .*3m +│")
                self.assertTrue(all(cell_width(row) == width for row in panel))

    def test_workers_with_missing_context_or_now_and_wide_characters(self):
        extras = [
            worker("claude", "Short", "high", None, "Keep notes", 5),
            worker("codex", "매우긴시험모델이름" * 5, "xhigh", 100, "Check the model against all named samples " * 4, 6),
            worker("opencode", "Atlas", "high", 17, "", 7),
        ]
        base = [
            worker("codex", "gpt-6.2-sol", "high", 63, "Read labels", 12),
            worker("opencode", "Atlas-Preview-XL", "", 82, "Check margins", 8),
            worker("claude", "Haiku", "medium", 9, "Count rows", 4),
        ]
        scenes = [{"width": w, "height": 64, "model": model_for(w, (base + extras)[:n])}
                  for w in (36, 50) for n in (1, 3, 6)]
        for scene, panel in zip(scenes, render_panels(scenes)):
            width = scene["width"]
            count = len(scene["model"]["routes"][0]["workers_live"])
            with self.subTest(width=width, workers=count):
                rows = route_rows(panel)
                doers = [row for row in rows if re.search(r"\bworker\b", row)]
                self.assertEqual(len(doers), count)
                self.assertTrue(all(cell_width(row) == width for row in panel))
                self.assertFalse(any(re.search(r"\breview\b", row) for row in rows))
                if count == 6:
                    self.assertRegex(doers[3], r"Short(?: high)? +5m +│")
                    self.assertNotIn("%", doers[3])
                    self.assertIn("…", doers[4])
                    percent_columns = {cell_width(row[:row.index("%") + 1]) for row in doers if "%" in row}
                    self.assertEqual(percent_columns, {width - (4 if width == 36 else 6)})
                    self.assertIn("100%", doers[4])
                    self.assertRegex(doers[5], r"7m (?:[━─]+ +)?17% +│")
                    index = rows.index(doers[4])
                    self.assertNotIn("6m", rows[index + 1])
                    self.assertRegex(rows[index + 2], r"… +6m +│")



def time_workers(now):
    return [worker("codex", "gpt-6.2-sol", "high", 63, now, 12), worker("opencode", "Atlas-Preview-XL", "", 82, "Check margins", 8)]


class WorkerTimeTest(unittest.TestCase):
    """11-27, reviews of #1885: a worker's time ends its last NOW row. The value's rows are the ones it has with no time
    (`wrapAll` at the row's width, never wrapped again); where the time does not fit their last one and another row is
    allowed, it stands alone at the right end of a row of its own; only where no row is left is the last row cut."""

    @staticmethod
    def now_rows(panel, now):
        # The first worker's NOW rows: the rows under its who row up to the next worker's.
        rows = route_rows(panel) if any("╭─ 1 ROUTE" in line for line in panel) else []
        at = next((i for i, line in enumerate(rows) if "gpt-6.2-sol" in line), None)
        if at is None:
            return []    # (the worker's rows have gone at this height)
        out = []
        for line in rows[at + 1:]:
            if re.search(r"\bworker\b|[└╰]", line):
                break
            out.append(re.sub(r"^[│ ]+|[│ ]+$", "", line))
        return out

    def panels(self, cases, height=64):
        # Each case drawn twice: the worker with its time, and the same worker with none.
        scenes = []
        for width, now, stamp in cases:
            for given in (stamp, ""):
                model = model_for(width, time_workers(now))
                model["routes"][0]["workers_live"][0]["elapsed"] = given
                scenes.append({"width": width, "height": height, "model": model})
        drawn = render_panels(scenes)
        return [(case, drawn[2 * i], drawn[2 * i + 1]) for i, case in enumerate(cases)]

    def test_the_time_fits_one_row_or_takes_a_row_of_its_own(self):
        cases = [(44, "Read labels", "12m"), (44, "Check every sample margin", "12m"), (50, "Check every sample margin", "12m")]
        (_, one, _), (_, two, _), (_, wide, _) = self.panels(cases)
        self.assertRegex(" / ".join(self.now_rows(one, None)), r"^Read labels +12m$")
        # The value and the time do not fit one row of the closed box at 44: the value keeps its row, the time takes the next.
        self.assertEqual(self.now_rows(two, None), ["Check every sample margin", "12m"])
        self.assertRegex(" / ".join(self.now_rows(wide, None)), r"^Check every sample margin +12m$")

    def test_the_value_keeps_the_rows_it_has_with_no_time(self):
        # The second and third reviews' inputs: a word as wide as the row (path, URL, Hangul, a wide character on the edge),
        # what `wrapAll` keeps together (`Atlas 2.5`, `· review`, `Atlas-Preview-XXLarge 2.5`), a long time, and a first row
        # the full width holds. With the time the value's rows are the no-time rows to the letter: nothing cut, nothing split.
        room = {36: 22, 44: 28, 50: 34}     # the first worker's NOW cells (36: the open box; 44, 50: the closed one)
        cases = []
        for width, n in room.items():
            cases += [(width, "/lab/" + "abcdefghijklmnopqrstuvwxyz0123456789"[:n - 5], "12m"),
                      (width, "https://ex.test/" + "abcdefghijklmnopqrstuvwxyz"[:n - 16], "12m"),
                      (width, "가나다라마바사아자차카타파하거너더러머버서어"[:n // 2], "12m"),
                      (width, "a" + "가나다라마바사아자차카타파하거너더러"[:(n - 1) // 2], "12m"),
                      (width, "Compareabcdefghijk Atlas 2.5", "12m"),
                      (width, "Compareabcdefghijkl · review", "12m"),
                      (width, "Check every sample margin", "1h 52m"),
                      (width, "Check every sample margin", "12d 3h"),
                      (width, "Go /lab/abcdefghijklmnopq up", "12m"),
                      (width, "Atlas-Preview-XXLarge 2.5 OK", "12m"),
                      (width, "Check abcdefghijklmnopqr log", "1h 52m")]
        own_rows, past = 0, set()
        for (width, now, stamp), timed, plain in self.panels(cases):
            rows, bare = self.now_rows(timed, None), self.now_rows(plain, None)
            with self.subTest(width=width, now=now, time=stamp):
                self.assertLessEqual(len(rows), 2, rows)
                self.assertEqual(sum(row.count(stamp) for row in rows), 1, rows)
                self.assertTrue(all(cell_width(line) == width for line in timed))
                if "…" in bare[-1] or len(bare) == 2 and cell_width(bare[-1]) + 1 + cell_width(stamp) > room[width]:
                    # (the value fills its two rows, as these do in 36's 22 cells: no row is left for the time, so the
                    # earlier rows stay the same and the last is cut before the time)
                    past.add(width)
                    self.assertEqual(rows[:-1], bare[:-1])
                    self.assertRegex(rows[-1], r"…  *" + re.escape(stamp) + "$")
                    continue
                self.assertFalse(any("…" in row for row in rows), rows)
                if rows[-1] == stamp:
                    own_rows += 1
                    value = rows[:-1]
                else:
                    self.assertTrue(rows[-1].endswith(" " + stamp), rows)
                    value = rows[:-1] + [rows[-1][:-len(stamp)].rstrip()]
                self.assertEqual(value, bare)
        self.assertGreater(own_rows, 0)
        self.assertEqual(past, {36})    # (at 44 and 50 every one of them stands whole)

    def test_a_value_past_its_rows_is_cut_on_the_last_one_before_the_time(self):
        now = "Check every sample margin and every label in the sample panel and its rows"
        for (width, _, stamp), timed, plain in self.panels([(44, now, "12m"), (50, now, "12m")]):
            rows, bare = self.now_rows(timed, None), self.now_rows(plain, None)
            with self.subTest(width=width):
                self.assertEqual(len(rows), 2, rows)
                self.assertEqual(rows[0], bare[0])
                self.assertRegex(rows[1], r"…  *12m$")
                self.assertTrue(bare[1].endswith("…"), bare)

    def test_the_time_row_folds_with_the_value(self):
        # Short of rows the time's own row goes with the NOW's second row: the time never stands without its value.
        scenes = [{"width": 44, "height": h, "model": model_for(44, time_workers("Check every sample margin"))} for h in range(16, 41)]
        seen = set()
        for scene, panel in zip(scenes, render_panels(scenes)):
            rows = self.now_rows(panel, None)
            with self.subTest(height=scene["height"]):
                if not rows:
                    continue
                self.assertTrue(rows[0].startswith("Check"), rows)
                if len(rows) == 2:
                    seen.add("own row")
                    self.assertEqual(rows, ["Check every sample margin", "12m"])
                else:
                    seen.add("one row")
                    self.assertRegex(rows[0], r" 12m$")
        self.assertEqual(seen, {"own row", "one row"})

    def test_short_of_rows_the_one_row_is_cut_before_the_time(self):
        heights, kinds = range(16, 65), set()
        scenes = [{"width": w, "height": h, "model": model_for(w, time_workers("Check every sample margin and its labels"))}
                  for w in (36, 44) for h in heights]
        for scene, panel in zip(scenes, render_panels(scenes)):
            rows = self.now_rows(panel, None)
            with self.subTest(width=scene["width"], height=scene["height"]):
                if not rows or not rows[0].startswith("Check"):
                    continue    # (the NOW rows have gone at this height)
                self.assertRegex(rows[-1], r"(^| )12m$", rows)
                if len(rows) == 1 and "…" in rows[0]:
                    kinds.add("cut")
                    self.assertRegex(rows[0], r"^Check every \S.*… +12m$")
                else:
                    kinds.add("whole" if "labels" in " ".join(rows) else "two-cut")
                    self.assertLessEqual(len(rows), 2, rows)
                self.assertTrue(all(cell_width(line) == scene["width"] for line in panel))
        self.assertIn("cut", kinds)
        self.assertTrue(kinds & {"whole", "two-cut"}, kinds)

def session_rows(panel):
    # The SESSION group's rows: from the rule over MODEL to the first box (QUESTIONS or ROUTE).
    start = next(i for i, line in enumerate(panel) if re.fullmatch(r"│ ─+ │", line))
    end = next(i for i in range(start + 1, len(panel)) if "╭─" in panel[i])
    return panel[start + 1:end]


class SessionContextTest(unittest.TestCase):
    """11-27 (the person: "패널에서 MODEL + CTX를 그냥 한줄로 고치자. CTX 단어도 필요 없을듯. 라우팅이랑 일관성을 맞추자는것",
    then "게이지는 놔두고"): the session's MODEL row ends in its gauge and percent; the route rows keep their own rule."""

    def test_the_session_model_row_keeps_its_gauge_at_the_right_edge(self):
        widths = (36, 44, 49, 50)
        panels = render_panels([{"width": w, "height": 52, "model": model_for(w)} for w in widths])
        for width, panel in zip(widths, panels):
            with self.subTest(width=width):
                rows = session_rows(panel)
                model = rows[0]
                # The percent ends the MODEL row at its right edge (the cell before the panel's border), and TITLE follows it.
                self.assertTrue(model.startswith("│  MODEL  gpt-6.2-sol"), rows)
                self.assertEqual(cell_width(model[:model.index("34%") + 3]), width - 2, model)
                self.assertTrue(rows[1].startswith("│  TITLE  "), rows)
                self.assertFalse(any(re.search(r"\bCTX\b|context", line) for line in rows), rows)
                # The gauge stands at every width: whole (ten cells) beside the model and effort from 44 cells; at 36 the
                # effort goes first and the gauge shortens beside the whole model name.
                gauge = re.search(r" ([━─]+) 34% │$", model)
                self.assertIsNotNone(gauge, model)
                self.assertEqual(gauge[1], "━━━─────" if width == 36 else "━━━───────", model)
                self.assertEqual("high" in model, width >= 44, model)
                # The route's doers keep their rule: the five-cell gauge from 50 cells, the percent alone narrower.
                doers = [row for row in route_rows(panel) if re.search(r"\b(owner|worker)\b", row)]
                gauges = [re.search(r"([━─]+) +\d+%", row) for row in doers]
                self.assertEqual([g[1] if g else "" for g in gauges],
                                 ["━━───", "━━━──", "━━━━─", "─────"] if width >= 50 else [""] * 4, doers)
                self.assertTrue(all(cell_width(row) == width for row in panel))

    def test_the_session_reading_takes_the_route_rows_level_color(self):
        scenes = [{"width": w, "height": 52, "model": model_for(w, session={"ctx_pct": pct})}
                  for w in (44, 50) for pct in (9, 63, 82)]
        for scene, panel in zip(scenes, render_panels(scenes, parts=True)):
            pct = scene["model"]["context_side"][-1]["text"]
            with self.subTest(width=scene["width"], percent=pct):
                text = ["".join(part["text"] for part in row) for row in panel]
                at = next(i for i, line in enumerate(text) if line.startswith("│  MODEL  "))
                own = next(part for part in panel[at] if part["text"] == pct)
                bar = next(part for part in panel[at] if re.fullmatch(r"━+", part["text"]))
                doer = next(part for row in panel[at + 1:] for part in row if part["text"] == pct)
                self.assertEqual(own["foreground"], doer["foreground"], (own, doer))
                self.assertEqual(bar["foreground"], own["foreground"], (bar, own))
                self.assertIsNotNone(own["foreground"])

    def test_short_of_cells_the_gauge_shortens_before_the_percent_stands_alone(self):
        long = {"model": "Atlas-Preview-XL-Extended", "effort": "xhigh", "ctx_pct": 100}
        cases = [(50, long, r"^│  MODEL  Atlas-Preview-XL-Extended ━━━━━━━ 100% │$"),
                 (36, long, r"^│  MODEL  Atlas-Previe… ━━━━━ 100% │$"),
                 # Narrower than the side panel's 32 cells: the model is cut beside the five-cell gauge while six cells of it
                 # stand, and only then does the percent stand alone.
                 (24, {}, r"^│  gpt-6\.2-… ━━─── 34% │$"),
                 (18, {}, r"^│  gpt-6\.2-… 34% │$"),
                 (36, {"ctx_pct": None}, r"^│  MODEL  gpt-6\.2-sol high +│$"),
                 (50, {"ctx_pct": None}, r"^│  MODEL  gpt-6\.2-sol high +│$")]
        scenes = [{"width": w, "height": 40, "model": model_for(w, session=session)} for w, session, _ in cases]
        for (width, session, want), panel in zip(cases, render_panels(scenes)):
            rows = session_rows(panel)
            with self.subTest(width=width, values=session):
                self.assertRegex(rows[0], want)
                # No reading: nothing stands in its place and no row of its own.
                self.assertFalse(any("CTX" in line or "context" in line for line in rows), rows)


if __name__ == "__main__":
    unittest.main()
