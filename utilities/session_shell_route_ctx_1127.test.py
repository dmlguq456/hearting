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
    for name in ("head", "repo", "repo_side", "git", "git_side", "worktrees", "selected", "context", "context_side", "context_gauge5"):
        model[name] = [part(*piece) for piece in model.get(name, [])]
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



def session_rows(panel):
    # The SESSION group's rows: from the rule over MODEL to the first box (QUESTIONS or ROUTE).
    start = next(i for i, line in enumerate(panel) if re.fullmatch(r"│ ─+ │", line))
    end = next(i for i in range(start + 1, len(panel)) if "╭─" in panel[i])
    return panel[start + 1:end]


class SessionContextTest(unittest.TestCase):
    """11-27 (the person: "패널에서 MODEL + CTX를 그냥 한줄로 고치자. CTX 단어도 필요 없을듯. 라우팅이랑 일관성을 맞추자는것")."""

    def test_the_session_model_row_ends_in_the_route_rows_reading(self):
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
                # The same rule as the route's doers: the five-cell gauge from 50 cells, the percent alone narrower.
                doers = [row for row in route_rows(panel) if re.search(r"\b(owner|worker)\b", row)]
                gauges = [re.search(r"([━─]+) +\d+%", row) for row in [model, *doers]]
                if width >= 50:
                    self.assertEqual([g[1] if g else "" for g in gauges], ["━━───", "━━───", "━━━──", "━━━━─", "─────"])
                else:
                    self.assertEqual(gauges, [None] * 5, [model, *doers])
                self.assertTrue(all(cell_width(row) == width for row in panel))

    def test_the_session_percent_takes_the_route_rows_level_color(self):
        scenes = [{"width": w, "height": 52, "model": model_for(w, session={"ctx_pct": pct})}
                  for w in (44, 50) for pct in (9, 63, 82)]
        for scene, panel in zip(scenes, render_panels(scenes, parts=True)):
            pct = scene["model"]["context_side"][-1]["text"]
            with self.subTest(width=scene["width"], percent=pct):
                text = ["".join(part["text"] for part in row) for row in panel]
                at = next(i for i, line in enumerate(text) if line.startswith("│  MODEL  "))
                own = next(part for part in panel[at] if part["text"] == pct)
                doer = next(part for row in panel[at + 1:] for part in row if part["text"] == pct)
                self.assertEqual(own["foreground"], doer["foreground"], (own, doer))
                self.assertIsNotNone(own["foreground"])

    def test_short_of_cells_the_effort_goes_first_and_no_reading_leaves_the_place_empty(self):
        long = {"model": "Atlas-Preview-XL-Extended", "effort": "xhigh", "ctx_pct": 100}
        scenes = [{"width": w, "height": 40, "model": model_for(w, session=session)}
                  for w in (36, 50) for session in (long, {"ctx_pct": None})]
        for scene, panel in zip(scenes, render_panels(scenes)):
            width, model = scene["width"], session_rows(panel)[0]
            with self.subTest(width=width, values=scene["model"]["context_side"]):
                if scene["model"]["context_side"]:
                    # The effort goes before a cell of the model name does; the percent stays at the right edge.
                    self.assertNotIn("xhigh", model)
                    self.assertEqual(cell_width(model[:model.index("100%") + 4]), width - 2, model)
                    self.assertRegex(model, r"^│  MODEL  Atlas-Preview-XL-E… +100% │$" if width == 36
                                     else r"^│  MODEL  Atlas-Preview-XL-Extended +━━━━━ 100% │$")
                else:
                    # No reading: nothing stands in its place and no row of its own.
                    self.assertRegex(model, r"^│  MODEL  gpt-6\.2-sol high +│$")
                    self.assertTrue(session_rows(panel)[1].startswith("│  TITLE  "))


if __name__ == "__main__":
    unittest.main()
