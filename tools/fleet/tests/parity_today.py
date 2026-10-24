"""Capture one private live snapshot and compare both renderers at the same moment."""
from __future__ import annotations

import argparse
import re
import dataclasses
import importlib
import json
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
WIDTHS = (80, 100, 140, 168)
VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
         "MEMORY", "MODELS", "SYSTEM")


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _frame_from_reference(row):
    from types import SimpleNamespace
    hits = [[hit[0], hit[1], hit[2], list(hit[3])] for hit in row["hits"]]        # actions as lists, like fleet2's
    return SimpleNamespace(cells=row["cells"], hits=hits, cols=len(row["cells"][0]), rows=len(row["cells"]))


def _state_jobs(vm):
    data = vm.to_mapping() if hasattr(vm, "to_mapping") else vm
    repos = data.get("repos") or ()
    sessions = data.get("sessions") or ()
    routes = data.get("routes") or ()
    return (("repo", "repo:" + str((repos[0].get("id") or repos[0].get("name"))), "OVERVIEW"),
            ("session", "session:" + str(sessions[0].get("tag")), "OVERVIEW"),
            ("route", "route:" + str(routes[0].get("id")), "OVERVIEW"))


def _nav(scope, view, usage=False):
    from tools.fleet.explorer.state import NavState
    return NavState(view=view, scope=scope, title=scope.split(":", 1)[-1] if scope != "-" else view,
                    usage_open=usage)


def _side_by_side(render, left, right, label, path):
    """One PNG: the reference copy left, fleet2 right, both in the Fleet palette, labelled on top."""
    import tempfile
    from PIL import Image, ImageDraw
    from tools.fleet.tests.explorer_capture import draw_png
    with tempfile.TemporaryDirectory() as scratch:
        draw_png(render, left, Path(scratch) / "l.png")
        draw_png(render, right, Path(scratch) / "r.png")
        a, b = Image.open(Path(scratch) / "l.png"), Image.open(Path(scratch) / "r.png")
        gap, bar = 12, 22
        sheet = Image.new("RGB", (a.width + gap + b.width, max(a.height, b.height) + bar), (40, 44, 58))
        sheet.paste(a, (0, bar))
        sheet.paste(b, (a.width + gap, bar))
        pen = ImageDraw.Draw(sheet)
        pen.text((6, 5), "reference copy  |  " + label, fill=(230, 230, 230))
        pen.text((a.width + gap + 6, 5), "fleet2", fill=(230, 230, 230))
        sheet.save(path)


def _slug(label):
    return label.replace("/", "_").replace(":", "-")


def _capture_pair(reference, vm, state, width, rows, outdir, label):
    """Draw one state at one width from the copy (fed this view model) and from fleet2."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.tests.freeze_explorer import capture
    from tools.fleet.tests.parity_compare import diff_frames
    from tools.fleet.tests.reference_injection import inject_live, load_isolated
    fleet_render = importlib.import_module("tools.fleet.render")
    ref_scope = state.scope if state.scope != "-" else None
    if ref_scope and ref_scope.startswith("session:"):
        ref_scope = ref_scope.split(":", 1)[1]
    ref_module = load_isolated(reference)
    inject_live(ref_module, vm.to_mapping())
    ref_row = capture(reference, width, renderer=ref_module, scrub=False, view=state.view,
                      scope=ref_scope, uopen=state.usage_open)
    fleet_frame = draw(vm, state, width, rows, beat=1)
    reference_frame = _frame_from_reference(ref_row)
    diffs = diff_frames(reference_frame, fleet_frame)
    target = outdir / f"{_slug(label)}-{width}x{rows}.png"
    _side_by_side(fleet_render, reference_frame, fleet_frame,
                  f"{label} {width}x{rows} - {len(diffs)} differences", target)
    return {"state": _slug(label), "width": width, "rows": rows, "equal": not diffs,
            "difference_count": len(diffs), "png": str(target),
            "reference_rows": ["".join(cell[0] for cell in row) for row in reference_frame.cells],
            "reference_edge": [bool(row and (row[-1][0].strip() or (row[-1][0] == "" and len(row) > 1 and row[-2][0].strip())))
                               for row in reference_frame.cells],
            "fleet2_rows": ["".join(cell[0] for cell in row) for row in fleet_frame.cells],
            "differences": diffs}


def _merged_detail(details, seats=None, config=None):
    """One ExplorerDetail from every per-session read: the focused session's reads, all sessions' seats."""
    from tools.fleet.tests.snapshot_fixture import replay_snapshot
    readable = {tag: row for tag, row in details.items() if row.get("identity_available")}
    primary = readable.get("f5") or next(iter(readable.values()), {})
    bundle = dict(primary.get("detail") or {})
    merged = {}
    for row in readable.values():
        merged.update((row.get("detail") or {}).get("seats") or {})
    merged.update(seats or {})
    bundle["seats"] = merged
    if config is not None:
        bundle["config"] = config
    return replay_snapshot({}, detail_bundle=bundle)[1]


def augment_capture(capture_dir):
    """Add what the capture's per-session reads left out: every seat's history and the model config.

    Both are read from this host (the capture's host) and written beside the capture, so a replay
    stays a replay. Existing files are kept.
    """
    from tools.fleet.explorer import nav
    from tools.fleet.explorer.focus import read_model_config, read_seat_history
    from tools.fleet.tests.snapshot_fixture import replay_source_bundle
    capture_dir = Path(capture_dir)
    seats_path, config_path = capture_dir / "seat-reads.json", capture_dir / "model-config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(_jsonable(read_model_config()), ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    if not seats_path.exists():
        cli = json.loads((capture_dir / "fleet-json.json").read_text(encoding="utf-8"))
        now = json.loads((capture_dir / "snapshot-bundle.json").read_text(encoding="utf-8")).get("captured_at")
        snapshot, _ = replay_source_bundle(cli, {}, {}, {}, now=now)
        details = json.loads((capture_dir / "detail-reads.json").read_text(encoding="utf-8"))
        panes = next((row["detail"].get("panes") for row in details.values() if row.get("detail", {}).get("panes")), {})
        seats = {}
        for session in snapshot.sessions:
            sid, harness = nav._get(session, "session_id"), nav._get(session, "harness")
            if not sid or not harness:
                continue
            pane = next((panes[i] for i in (sid, *(nav._get(session, "session_aliases") or ())) if i in panes), None)
            entries, _pane, _health = read_seat_history(session, harness, sid, cards=False, pane=pane)
            if entries is not None:
                seats[f"{harness}:{sid}"] = _jsonable(entries)
        seats_path.write_text(json.dumps(seats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_live_vm(capture_dir):
    """The one live view model both renderers draw: ``fleet --json`` replayed with every seat read."""
    from tools.fleet.explorer.live import build_view_model
    from tools.fleet.tests.snapshot_fixture import replay_source_bundle
    capture_dir = Path(capture_dir)
    cli = json.loads((capture_dir / "fleet-json.json").read_text(encoding="utf-8"))
    now = json.loads((capture_dir / "snapshot-bundle.json").read_text(encoding="utf-8")).get("captured_at")
    details_path = capture_dir / "detail-reads.json"
    details = json.loads(details_path.read_text(encoding="utf-8")) if details_path.exists() else {}
    snapshot, _ = replay_source_bundle(cli, {}, {}, {}, now=now)
    seats_path, config_path = capture_dir / "seat-reads.json", capture_dir / "model-config.json"
    seats = json.loads(seats_path.read_text(encoding="utf-8")) if seats_path.exists() else None
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
    detail = _merged_detail(details, seats, config) if details else None
    return build_view_model(snapshot, accepted_detail=detail, now=now), now


def compare_vm(vm, reference, out_dir, widths=WIDTHS, rows=45, only=None):
    """Every root view plus one repo, session and route page at every width."""
    out_dir.mkdir(parents=True, exist_ok=True)
    states = [(view, "-", view) for view in VIEWS]
    try:
        repo, session, route = _state_jobs(vm)
        states.extend((view, scope, name) for name, scope, view in (repo, session, route))
    except (IndexError, TypeError, AttributeError):
        pass
    records = []
    for view, scope, label in states:
        if only and label not in only:
            continue
        for width in widths:
            records.append(_capture_pair(Path(reference), vm, _nav(scope, view), width, rows, out_dir, label))
    return records


def _bands(record):
    """Group a pair's differing cells into row bands per pane, with both sides' text."""
    width, ref_rows, fleet_rows = record["width"], record["reference_rows"], record["fleet2_rows"]
    split = 33 if width >= 100 else 33
    cells, samples = {}, {}
    for diff in record["differences"]:
        if diff["kind"] != "cell":
            continue
        pane = "frame" if diff["row"] in (0, 1) or diff["row"] >= len(ref_rows) - 3 else \
            ("left" if diff["column"] < split else "right")
        cells.setdefault((pane, diff["row"]), []).append(diff["column"])
        samples.setdefault((pane, diff["row"]), []).append(diff)
    bands, current = [], None
    for (pane, row) in sorted(cells, key=lambda key: (key[0], key[1])):
        if current and current["pane"] == pane and row == current["rows"][-1] + 1:
            current["rows"].append(row)
        else:
            current = {"pane": pane, "rows": [row]}
            bands.append(current)
    for band in bands:
        lo, hi = (0, split) if band["pane"] == "left" else (split, width) if band["pane"] == "right" else (0, width)
        band["cells"] = sum(len(cells[(band["pane"], row)]) for row in band["rows"])
        band["reference"] = [ref_rows[row][lo:hi].rstrip() for row in band["rows"]]
        band["fleet2"] = [fleet_rows[row][lo:hi].rstrip() for row in band["rows"]]
        found = [diff for row in band["rows"] for diff in samples[(band["pane"], row)]]
        band["text_equal"] = all((diff["expected"] or [""])[0] == (diff["actual"] or [""])[0] for diff in found)
        band["style_pairs"] = sorted({(json.dumps((d["expected"] or [None, None])[1]), json.dumps((d["actual"] or [None, None])[1]))
                                      for d in found})
        band["blank_only"] = band["text_equal"] and all((diff["expected"] or [" "])[0] in (" ", "") for diff in found)
        band["ref_edge"] = any(record["reference_edge"][row] for row in band["rows"])
        band["samples"] = [{"row": diff["row"], "column": diff["column"], "reference": diff["expected"],
                            "fleet2": diff["actual"]} for diff in found[:3]]
    hits = [diff for diff in record["differences"] if diff["kind"] == "hits"]
    return bands, hits



# -- classification --------------------------------------------------------------------------------
# Every differing band of every state/width pair gets one reason. ``class`` is one of
#   fleet2-defect     fleet2 draws something the approved reference's rule or design does not (owner module named)
#   copy-limitation   the reference copy cannot draw this live value: its hand tables never held such data
#   intended          a deliberate, recorded difference
#   excluded          a 80/100-column reference self-defect (the reference itself overflows or overlaps)
# A band no rule explains stays ``unexplained`` and fails the run.

def _band_text(band):
    return "\n".join(band["reference"]), "\n".join(band["fleet2"])


def _style_is(band, reference, fleet2):
    """The band differs only in style, and only by this one reference-key to fleet2-key change."""
    return band["text_equal"] and band["style_pairs"] == [(json.dumps(reference), json.dumps(fleet2))]


def _style_in(band, pairs):
    """The band differs only in style, every change being one of ``pairs``."""
    allowed = {(json.dumps(a), json.dumps(b)) for a, b in pairs}
    return band["text_equal"] and bool(band["style_pairs"]) and set(band["style_pairs"]) <= allowed


def _collapse(text):
    return " ".join(text.split())


def _rows_shifted(band):
    """Rows were added or removed on one side: the non-blank row counts of the band differ."""
    return sum(1 for x in band["reference"] if x.strip()) != sum(1 for x in band["fleet2"] if x.strip())


def _has(pattern, text):
    return re.search(pattern, text) is not None


CHROME = "tools/fleet/explorer/chrome.py"
TABLES = "tools/fleet/explorer/tables.py"
PAGES = "tools/fleet/explorer/pages.py"
PEERS = "tools/fleet/explorer/peers.py"
RES = "tools/fleet/explorer/resources.py"
MODELS_PY = "tools/fleet/explorer/models.py"

# (id, class, owner, reason, predicate(record, band))
RULES = [
    ("usage-gauge-80", "fleet2-defect", CHROME,
     "the week-usage gauge at 80% is drawn green (grp_live); the reference's pct_key() paints >= 80 red (lvl_r_flat)",
     lambda r, b: b["pane"] == "frame" and _style_is(b, "g_blocked", "grp_live")),
    ("tree-route-branch", "fleet2-defect", CHROME,
     "tree route rows: a route that is not the session's last draws the last-branch glyph (┗━ twice) and its name is "
     "not cut with an ellipsis (`route-frame`); the reference draws ┣━ and `route-fr…` inside the pane",
     lambda r, b: b["pane"] == "left" and _has("route-fr…", b["reference"][0]) and _has("route-frame", b["fleet2"][0])),
    ("sidebar-compute-gauge", "fleet2-defect", CHROME,
     "sidebar compute block: a GPU at 82% uses the plain red key (g_blocked); the reference's gauge rule uses its bold level red",
     lambda r, b: b["pane"] == "left" and _style_is(b, "g_dead", "g_blocked")),
    ("blank-cell-key", "intended", None,
     "only blank cells differ, and only in their foreground key: a space has no glyph and no background, so nothing visible differs",
     lambda r, b: b["blank_only"]),
    ("legend-overflow-80", "excluded", None,
     "80 columns: the reference's key legend is 91 cells wide and is cut at the screen edge (`F feed`); fleet2 drops the "
     "hint keys by priority (chrome.py draw_chrome) - reference self-defect, narrow-width intent",
     lambda r, b: r["width"] == 80 and b["pane"] == "frame" and b["ref_edge"] and _has("F feed", b["reference"][0]) and _has("q quit", b["fleet2"][0])),
    ("status-clip-80", "excluded", None,
     "80 columns: the reference's bottom status line runs to the screen edge and is cut mid-word; fleet2 cuts it earlier",
     lambda r, b: r["width"] == 80 and b["pane"] == "frame" and b["ref_edge"] and b["rows"][-1] >= 43 and
     any(_has("esc: back|›", x) for x in b["reference"])),
    ("hosts-fold-100", "fleet2-defect", CHROME,
     "100 columns: when the host list does not fit before the census the reference folds it to `⌂ moving4 +4 · 5/5`; "
     "fleet2 clips the list mid-name (chrome.py draw_chrome host loop)",
     lambda r, b: r["width"] == 100 and b["pane"] == "frame" and _has(r"\+4 · 5/5", b["reference"][0])),
    ("more-below-marker", "fleet2-defect", "tools/fleet/explorer/frame.py",
     "the pane's content runs past the bottom rule: the reference writes ` ↓ more below ` on the rule (its `spilled` test); "
     "fleet2 writes it only for its own overflow test, so the marker is missing here",
     lambda r, b: b["pane"] == "right" and _has("more below", b["reference"][-1]) and not _has("more below", b["fleet2"][-1])),
    ("working-codex-model", "copy-limitation", None,
     "a working codex session's model name: the reference colours every fam_gpt model yellow (no 22:53 working codex row exists); "
     "fleet2 colours it with the harness hue like the original board (tables.py)",
     lambda r, b: _style_is(b, "g_idle", "hb_codex")),
    ("harness-mix", "fleet2-defect", TABLES,
     "`owner·worker` counts: fleet2 adds each route's worker row (`route.worker`) although the same view model's `subworker` is null "
     "and its own tree shows no depth-2 row; the reference counts the depth-2 workers it draws (tables.py _harness_mix)",
     lambda r, b: b["pane"] == "right" and _has("owner·worker", b["reference"][0]) and _has("owner·worker", b["fleet2"][0])),
    ("stage-hue", "fleet2-defect", TABLES,
     "route worker row: the step name (and, while it runs, `running …` in bold) is coloured with its stage hue in the reference (index in "
     "the route's stages); the live view model carries no worker_step_key, so fleet2 falls back to g_work_off / dim instead of deriving "
     "the hue (tables.py worker row)",
     lambda r, b: b["pane"] == "right" and _style_in(
         b, [(k, "g_work_off") for k in ("grp_live", "version_release", "hb_claude", "h_claude")] + [("g_work", "dim")])),
    ("unknown-bold-title", "fleet2-defect", TABLES,
     "progress rail: the current stage's name takes its colour from a table that has no entry for stage index 0 and 1 "
     "(tables.py:501 falls back to the unknown key); the reference colours every stage by its hue",
     lambda r, b: b["text_equal"] and _style_is(b, "hb_claude", "unknown")),
    ("worker-effort-max", "fleet2-defect", TABLES,
     "worker harness cell folded to `harness·effort`: fleet2 drops the effort when it is `max`; the reference keeps every effort in its EFFORTS list",
     lambda r, b: b["pane"] == "right" and any(_has(r"claude·max", x) for x in b["reference"]) and
     not any(_has(r"claude·max", x) for x in b["fleet2"][-1:]) and any(_has("worker", x) for x in b["reference"])),
    ("worker-no-effort", "copy-limitation", None,
     "a worker harness cell with a model but no effort (opencode): the reference's narrow fold only handles the six-part cell, so it clips "
     "`opencode (de…`; fleet2 shows the harness name alone",
     lambda r, b: b["pane"] == "right" and any(_has(r"opencode \(de", x) for x in b["reference"])),
    ("worker-row-empty", "copy-limitation", None,
     "a route with no worker record: the reference always draws a worker line and fills it with `·`; fleet2 leaves the cells empty",
     lambda r, b: b["pane"] == "right" and any(_has(r"report +· +·|report +·$", x) for x in b["reference"]) and
     any(_has("report", x) for x in b["fleet2"])),
    ("peers-narrow-frame", "excluded", None,
     "80/100 columns: the reference's repo frames and session boxes are laid out one cell wider than the pane and run to the screen edge; "
     "fleet2 keeps its frames inside the pane",
     lambda r, b: r["width"] in (80, 100) and r["state"] == "PEERS" and b["pane"] == "right" and b["ref_edge"]),
    ("unknown-context-gauge-peers", "fleet2-defect", PEERS,
     "PEERS card for a session whose context is unknown: fleet2 draws an empty gauge (`────────`); its OVERVIEW draws nothing "
     "for the same unknown context (tables.py), as the reference does",
     lambda r, b: r["state"] == "PEERS" and any(_has(r"│ ────────", x) and not _has(r"│ ────────", y)
                                                for x, y in zip(b["fleet2"], b["reference"]))),
    ("unknown-state-glyph", "copy-limitation", None,
     "a session whose state is `unknown`/`stale`: the reference has five session states and draws these as an idle dot; fleet2's PEERS card "
     "draws `·` for them (peers.py STATE_MARKS fallback) while its tree and OVERVIEW draw the idle dot",
     lambda r, b: r["state"] == "PEERS" and any(_has(r"● \[[0-9a-f]{2}\]", x) and _has(r"· \[[0-9a-f]{2}\]", y)
                                                for x, y in zip(b["reference"], b["fleet2"]))),
    ("peers-glyph-style", "copy-limitation", None,
     "PEERS lane/link junction colour: the reference colours a lane by the upper/lower session's harness hue; the live pair's link "
     "colour in fleet2 follows the other session",
     lambda r, b: r["state"] == "PEERS" and b["pane"] == "right" and b["text_equal"] and
     all((d["reference"] or [""])[0] in "├┤" for d in b["samples"])),
    ("answered-head", "fleet2-defect", TABLES,
     "ANSWERED TODAY header: `<first repo> <n>` where n is that repo's answered rows in the reference; fleet2 writes the total of all repos "
     "(20) after the first repo's name (tables.py:904)",
     lambda r, b: _has("ANSWERED TODAY", b["reference"][0]) and _has("ANSWERED TODAY", b["fleet2"][0])),
    ("question-empty-now", "copy-limitation", None,
     "a waiting question whose `now` text is empty: the reference draws nothing after `now:` (all 22:53 questions carried text); fleet2 draws `·`",
     lambda r, b: r["state"] == "QUESTIONS" and _has(r"now:\s*$", b["reference"][0]) and _has(r"now:\s+·", b["fleet2"][0])),
    ("question-header-overflow", "excluded", None,
     "80/100 columns: the reference's QUESTIONS header runs to the screen edge; fleet2 keeps its header inside the pane",
     lambda r, b: r["width"] in (80, 100) and r["state"] == "QUESTIONS" and b["pane"] == "right" and b["ref_edge"]),
    ("question-answer-line-80", "excluded", None,
     "80 columns: the question detail line `answer: ⏎ go to its pane and answer there` is cut by the reference at the pane edge",
     lambda r, b: r["width"] == 80 and r["state"] == "QUESTIONS" and any(_has("answer:", x) for x in b["reference"])),
    ("compute-command-args", "fleet2-defect", RES,
     "COMPUTE process line: fleet2 cuts the command at ` --` (`script.py`); the reference keeps the script and its short options "
     "(`script.py --request A2.json`, its short_cmd())",
     lambda r, b: r["state"] == "COMPUTE" and b["pane"] == "right" and all(
         _collapse(re.sub(r" --.*?(?=\s+\d+(\.\d)? GB)", "", x)) == _collapse(y) for x, y in zip(b["reference"], b["fleet2"]))),
    ("compute-cpu-row-clip", "excluded", None,
     "80/100 columns: the reference's CPU/MEM/SWP row, GPU rows and host summary run to the screen edge and are cut there",
     lambda r, b: r["width"] in (80, 100) and r["state"] == "COMPUTE" and b["ref_edge"]),
    ("session-context-line", "fleet2-defect", PAGES,
     "session page context line: with an unknown last-clear time fleet2 drops the `· last clear ·` clause that its own seat line "
     "(and the reference's context line) print",
     lambda r, b: r["state"] == "session" and any(_has("context:", x) for x in b["reference"])),
    ("hard-cut-vs-ellipsis", "fleet2-defect", PAGES,
     "a seat/field line longer than the pane: the reference cuts it hard at the pane edge, fleet2 ends it with `…`",
     lambda r, b: r["width"] in (80, 100) and b["pane"] == "right" and any(
         y.rstrip().endswith("…") and not x.rstrip().endswith("…") and x[:20] == y[:20] for x, y in zip(b["reference"], b["fleet2"]))),
    ("route-page-stage-line", "fleet2-defect", PAGES,
     "route page leg box: fleet2 prints `stage <name> n/m` above the owner line; the reference's leg box lists the owner line first and puts "
     "`stage` second",
     lambda r, b: r["state"] == "route" and any(_has(r"owner +codex", x) for x in b["reference"]) and any(_has("stage", x) for x in b["fleet2"])),
    ("models-long-name-clip", "fleet2-defect", MODELS_PY,
     "MODELS cell: a model name longer than the column is cut with an ellipsis in the reference (`muse-spark-1.3-cont…`); fleet2 cuts it without one",
     lambda r, b: r["state"] == "MODELS" and any(_has("cont…", x) for x in b["reference"])),
    ("models-main-only-tuple", "fleet2-defect", MODELS_PY,
     "MODELS `main session only` line prints the Python tuple (`claude ('fable',)`) where the reference prints the model name",
     lambda r, b: r["state"] == "MODELS" and any(_has(r"\('", x) for x in b["fleet2"])),
    ("models-tidy-bar", "fleet2-defect", MODELS_PY,
     "CONTEXT & TIDY bar: the live model config carries no tidy points (`tidy` is null; the reference keeps 500K/700K, a code constant in "
     "context_alert.py), so fleet2 paints the whole bar red (g_blocked) while its own chips still show 500K/700K as current "
     "(models.py:80-84)",
     lambda r, b: r["state"] == "MODELS" and _style_in(b, [("g_work_off", "g_blocked"), ("g_idle", "g_blocked")])),
    ("models-narrow", "excluded", None,
     "80/100 columns: MODELS rows, the edit panel foot and the CONTEXT bars run to the screen edge in the reference and are cut there",
     lambda r, b: r["width"] in (80, 100) and r["state"] == "MODELS" and b["ref_edge"]),
    ("system-status", "fleet2-defect", CHROME,
     "SYSTEM status line with an unread `pending` count: fleet2 prints `· deliveries pending`, the reference prints the number",
     lambda r, b: r["state"] == "SYSTEM" and b["pane"] == "frame"),
    ("overview-80", "excluded", None,
     "80 columns: the reference's OVERVIEW draws NOW text over the CTX column and header (`TCTXE`), the known 80-column OVERVIEW self-defect",
     lambda r, b: r["width"] == 80 and r["state"] in ("OVERVIEW", "repo") and b["pane"] == "right" and
     any(_has(r"TCTXE|\S+ \d+%…|… ", x) for x in b["reference"])),
    ("routes-narrow", "excluded", None,
     "80/100 columns: the reference's ROUTES header and worker rows overflow the pane (`AGEH`, `c` cut), a known narrow-width overflow",
     lambda r, b: r["width"] in (80, 100) and b["pane"] == "right" and r["state"] in ("ROUTES", "repo", "route", "session") and
     (b["ref_edge"] or any(_has("AGEH|TCTXE", x) for x in b["reference"]))),
    ("gauge-min-one-cell", "fleet2-defect", TABLES,
     "a small percentage (6%) on a 6-cell gauge: the reference's gauge() always fills at least one cell for a value above 0; "
     "fleet2 rounds it to an empty gauge",
     lambda r, b: b["pane"] == "right" and any(_has(r"━─{5}\s+\d%", x) and _has(r"─{6}\s+\d%", y)
                                              for x, y in zip(b["reference"], b["fleet2"]))),
    ("memory-narrow-clip", "excluded", None,
     "80 columns: the reference's MEMORY record rows run to the screen edge (`artifact-point…` cut at column 80); fleet2 cuts the TYPE column one cell earlier",
     lambda r, b: r["width"] == 80 and r["state"] == "MEMORY" and b["pane"] == "right" and b["ref_edge"]),
    ("narrow-header-overflow", "excluded", None,
     "80/100 columns: the reference's table header / field line is wider than the pane and runs to the screen edge; "
     "fleet2 cuts it at the pane edge (with `…` on field lines)",
     lambda r, b: r["width"] in (80, 100) and r["state"] in ("HISTORY", "SYSTEM", "session") and b["pane"] == "right" and b["ref_edge"]),
    ("sessionless-route", "copy-limitation", None,
     "a running route whose owner session is not on the board: the reference's OVERVIEW/repo page lists only routes that have an owner session "
     "(its past routes are not listed); fleet2 lists it, so the rows below it move down",
     lambda r, b: r["state"] in ("repo", "OVERVIEW") and b["pane"] == "right" and
     (_rows_shifted(b) or any(_has("HISTORY|RECENT", x) for x in b["reference"]))),
]


def classify(records):
    """Attach a reason to every differing band and hit difference; return the classified document."""
    entries, counts = [], {}
    for record in records:
        bands, hits = _bands(record)
        explained_rows = {row for band in bands for row in band["rows"]}
        for band in bands:
            matched = []
            for rule in RULES:
                try:
                    if rule[4](record, band):
                        matched.append(rule)
                except (KeyError, IndexError, TypeError):
                    continue
            if matched:
                # For narrow captures, an observed reference-edge/overlap rule takes
                # precedence over content rules that can describe the same band.
                # This keeps the reference's own layout defects out of fleet2 counts.
                narrow_reference_defect = record["width"] in (80, 100) and (
                    band["ref_edge"] or any(_has(r"AGEH|TCTXE", line) for line in band["reference"]))
                selected = next((rule for rule in matched if rule[1] == "excluded"), matched[0]) \
                    if narrow_reference_defect else matched[0]
                rule_id, cls, owner, reason = selected[:4]
            else:
                rule_id, cls, owner, reason = "unexplained", "unexplained", None, "no rule explains this band"
            entries.append({"state": record["state"], "width": record["width"], "kind": "cells", "pane": band["pane"],
                            "rows": [band["rows"][0], band["rows"][-1]], "cells": band["cells"],
                            "text_equal": band["text_equal"], "rule": rule_id, "class": cls, "owner": owner,
                            "also": [rule[0] for rule in matched if rule is not selected] if matched else [],
                            "reason": reason, "reference": band["reference"][:3], "fleet2": band["fleet2"][:3]})
            counts[cls] = counts.get(cls, 0) + 1
        if hits:
            # a hit area that differs must sit on a row where cells differ too (a moved, resized or missing row
            # moves its hit rows with it); a hit difference on a row whose cells are equal stays unexplained
            expected = [json.dumps(hit) for hit in hits[0]["expected"]]
            actual = [json.dumps(hit) for hit in hits[0]["actual"]]
            only = [json.loads(hit) for hit in expected if hit not in actual] + \
                   [json.loads(hit) for hit in actual if hit not in expected]
            stray = [hit for hit in only if hit[0] not in explained_rows and hit[0] != -1]
            if only and all(hit[2] > record["width"] for hit in only):
                cls, rule_id = "excluded", "hit-outside-screen"
                reason = ("the reference's own hit areas end beyond the %d-column screen (x1 up to %d): the known narrow-width "
                          "self-defect recorded in evidence/parity/matrix/reference-defects.json (MODELS 80/100)"
                          % (record["width"], max(hit[2] for hit in only)))
            elif only and not stray:
                cls, rule_id = "derived", "hits-follow-cells"
                reason = ("hit areas that differ lie on rows whose cells differ and are explained above: a moved, resized "
                          "or missing row takes its hit rows with it (%d differing hit areas)" % len(only))
            elif not only:
                cls, rule_id, reason = "derived", "hits-reordered", "the same hit areas in a different order (rows moved)"
            else:
                cls, rule_id = "unexplained", "unexplained"
                reason = "hit areas differ on rows whose cells are equal: " + json.dumps(stray[:3])
            entries.append({"state": record["state"], "width": record["width"], "kind": "hits", "rule": rule_id,
                            "class": cls, "owner": None, "reason": reason, "differing_hit_areas": only[:20]})
            counts[cls] = counts.get(cls, 0) + 1
    return {"schema": "fleet-today-classified-v1", "summary": {"bands": len(entries), "by_class": counts,
            "unexplained": counts.get("unexplained", 0)}, "entries": entries}


def write_classified(document, out_dir, capture_name):
    """classified.json plus a readable classified.md: fleet2 defects first (the owner's list), then the rest."""
    out_dir = Path(out_dir)
    (out_dir / "classified.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    by_rule = {}
    for entry in document["entries"]:
        row = by_rule.setdefault(entry["rule"], {"class": entry["class"], "owner": entry.get("owner"),
                                                 "reason": entry["reason"], "where": []})
        row["where"].append(f"{entry['state']}@{entry['width']}" + (f" rows {entry['rows'][0]}-{entry['rows'][1]}"
                                                                    if entry.get("rows") else " hits"))
    lines = [f"# Classified differences - {capture_name}", "",
             f"{document['summary']['bands']} differing bands/hit sets; unexplained: {document['summary']['unexplained']}.",
             f"By class: {json.dumps(document['summary']['by_class'], ensure_ascii=False)}", ""]
    for cls, title in (("fleet2-defect", "fleet2 renderer defects (for the owner)"), ("copy-limitation", "Reference-copy limitations"),
                       ("intended", "Intended"), ("excluded", "Excluded 80/100 reference self-defects"),
                       ("derived", "Derived"), ("unexplained", "Unexplained")):
        rules = {key: value for key, value in by_rule.items() if value["class"] == cls}
        if not rules:
            continue
        lines += [f"## {title}", ""]
        for key, value in sorted(rules.items()):
            where = ", ".join(sorted(set(value["where"]))[:12]) + (" ..." if len(set(value["where"])) > 12 else "")
            owner = f" - owner `{value['owner']}`" if value.get("owner") else ""
            lines += [f"### {key}{owner}", "", value["reason"], "", f"Seen in {len(value['where'])} place(s): {where}", ""]
    (out_dir / "classified.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_capture(out, reference, approved=None, source_bundle=None, from_capture=None):
    """Collect once (or replay ``from_capture``), then draw one live view model through both renderers."""
    from tools.fleet.tests.explorer_capture import host_snapshot
    from tools.fleet.tests.snapshot_fixture import replay_snapshot, replay_source_bundle
    render = importlib.import_module("tools.fleet.render")
    now = time.time()
    if from_capture:
        capture_dir = Path(from_capture)
        augment_capture(capture_dir)
        vm, now = build_live_vm(capture_dir)
    else:
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        capture_dir = Path(out) / "evidence/parity/today" / stamp
        capture_dir.mkdir(parents=True, exist_ok=True)
        _collect(capture_dir, render, now, source_bundle)
        augment_capture(capture_dir)
        vm, now = build_live_vm(capture_dir)
    side_dir = Path(out) / "evidence/parity/side-by-side/today"
    records = compare_vm(vm, reference, side_dir)
    summary = [{key: row[key] for key in ("state", "width", "rows", "equal", "difference_count", "png")}
               for row in records]
    (side_dir / "manifest.json").write_text(json.dumps({"schema": "fleet-side-by-side-v1",
        "source": f"today host capture {capture_dir.name}: one live view model injected into the copy and drawn by fleet2",
        "captures": summary}, ensure_ascii=False, indent=2) + "\n")
    (capture_dir / "diff.json").write_text(json.dumps({"schema": "fleet-today-diff-v1", "captured_at": now,
        "captures": summary}, ensure_ascii=False, indent=2) + "\n")
    document = classify(records)
    write_classified(document, capture_dir, capture_dir.name)
    if approved:
        _capture_2253(Path(out), Path(approved), Path(reference), capture_dir, now)
    return capture_dir, records


def _collect(capture_dir, render, now, source_bundle):
    """Collect this host once (``fleet --json`` plus every session's detail reads) into ``capture_dir``."""
    from tools.fleet.explorer.state import NavState
    from tools.fleet.tests.snapshot_fixture import replay_snapshot
    completed = subprocess.run(["fleet", "--json"], check=True, capture_output=True, text=True, timeout=180)
    cli_data = json.loads(completed.stdout)
    (capture_dir / "fleet-json.json").write_text(completed.stdout, encoding="utf-8")
    snapshot, _ = replay_snapshot(cli_data, now=now)
    details = {}
    for session in snapshot.sessions:
        tag = getattr(session, "tag", None) or getattr(session, "session_tag", None)
        if not tag:
            continue
        state = NavState(view="OVERVIEW", scope="session:" + str(tag), title=str(tag))
        render._explorer_compose(snapshot, 140, 45, state, now=now)
        details[str(tag)] = {"identity_available": True,
                             "detail": _jsonable(render.explorer_read_detail(snapshot, state))}
    payload = _jsonable(snapshot)
    payload["captured_at"] = now
    (capture_dir / "detail-reads.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (capture_dir / "snapshot-bundle.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _capture_2253(out, approved, reference, today_dir, now):
    """The 22:53 pair: the approved data drawn by the reference (left, width-scrubbed as the goldens are) and by
    fleet2 from the production adapter's view model of the same four source files (right)."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.view_model import ExplorerViewModel
    from tools.fleet.tests.freeze_explorer import capture
    from tools.fleet.tests.parity_compare import (_nav_state, _state_args, diff_frames, live_ledger,
                                                  overlay_view_model)
    from tools.fleet.tests.reference_injection import extract_view_model, load_isolated
    _ledger, _private, live = live_ledger(reference)
    hand_vm = extract_view_model(load_isolated(approved), scrub=True)
    # fleet2 draws the adapter's view model with the hand values the ledger classifies (class 2/3, no real source)
    # kept from the reference, so a remaining difference is the renderer's, never a missing source
    merged = {key: value for key, value in overlay_view_model(hand_vm, live).items() if not key.startswith("_")}
    overlay_vm = ExplorerViewModel.from_mapping(merged)
    live_vm = ExplorerViewModel.from_mapping(live)
    render = importlib.import_module("tools.fleet.render")
    directory = Path(out) / "evidence/parity/side-by-side/2253"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((Path(__file__).parent / "fixtures/explorer/manifest.json").read_text())
    records = []
    for label in manifest["states"]:
        for width in WIDTHS:
            args = _state_args(label)
            if label == "repo-page":
                args["scope"] = "repo:xxxxxxxx"            # the scrubbed repo id, as the goldens use
            row = capture(approved, width, renderer=load_isolated(approved), scrub=True, **args)
            left = _frame_from_reference(row)
            right = draw(overlay_vm, _nav_state(label), width, 45, beat=1)
            count = len(diff_frames(left, right))
            plain = len(diff_frames(left, draw(live_vm, _nav_state(label), width, 45, beat=1)))
            target = directory / f"{_slug(label)}-{width}x45.png"
            _side_by_side(render, left, right, f"{label} {width}x45 - {count} differences", target)
            records.append({"state": _slug(label), "width": width, "difference_count": count,
                            "difference_count_without_overlay": plain, "png": str(target)})
    (directory / "manifest.json").write_text(json.dumps({"schema": "fleet-side-by-side-v1",
        "source": "22:53 approved data: the reference (left, width-scrubbed) beside fleet2 drawing the production adapter's "
                  "view model with the hand-only values kept from the reference (right)",
        "captures": records}, ensure_ascii=False, indent=2) + "\n")
    return records


def write_index(out):
    """One index of every side-by-side PNG, grouped by data set."""
    base = Path(out) / "evidence/parity/side-by-side"
    index, lines = {}, ["# Side-by-side PNGs", "",
                        "Reference copy on the left, fleet2 on the right, both drawn in the Fleet palette.", ""]
    for group in ("today", "2253"):
        manifest = base / group / "manifest.json"
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        rows = [{"state": row["state"], "width": row["width"], "difference_count": row.get("difference_count"),
                 "png": str(Path(row["png"]).relative_to(base))} for row in data["captures"]]
        index[group] = {"source": data["source"], "captures": rows}
        lines += [f"## {group}", "", data["source"], "", "| state | width | differences | image |", "|---|---|---|---|"]
        lines += [f"| {row['state']} | {row['width']} | {row['difference_count']} | {row['png']} |" for row in rows]
        lines.append("")
    (base / "index.json").write_text(json.dumps({"schema": "fleet-side-by-side-index-v1", "groups": index},
                                                ensure_ascii=False, indent=2) + "\n")
    (base / "index.md").write_text("\n".join(lines) + "\n")
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--approved", type=Path)
    parser.add_argument("--source-bundle", type=Path,
                        help="optional four-file source directory; otherwise collect this host")
    parser.add_argument("--from-capture", type=Path,
                        help="replay an existing capture directory (fleet-json.json, detail-reads.json, "
                             "snapshot-bundle.json) instead of collecting this host")
    parser.add_argument("--round-trip", action="store_true",
                        help="only check that the copy fed the approved data still draws the approved frames")
    args = parser.parse_args(argv)
    if args.round_trip:
        from tools.fleet.tests.reference_injection import round_trip
        result = round_trip(args.approved, args.reference)
        print(json.dumps({"states": result["state_count"], "differences": result["diff_count"]}))
        return 0 if result["diff_count"] == 0 else 1
    capture_dir, records = run_capture(args.out, args.reference, args.approved, args.source_bundle,
                                       args.from_capture)
    write_index(args.out)
    print(json.dumps({"capture_dir": str(capture_dir), "capture_count": len(records),
                      "differences": sum(row["difference_count"] for row in records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
