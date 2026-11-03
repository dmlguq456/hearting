#!/usr/bin/env python3
"""Dev-only: re-freeze just the transitions.json frames an intended screen change moved.

    python3 tools/fleet/tests/refreeze_transitions.py [--check]      # from the repository root

Replays every case exactly like test_explorer_transitions (same decoder, reducer and draw).
Each step whose current frame differs from its stored frame gets a new frame (new rows go to
the end of the shared row pool) and points at it; input chunks, navigation states and every
unchanged frame stay as they are. PEERS steps are skipped (the test normalizes their lanes).
``--check`` only lists the steps that would move. transitions.json is one line, so two screen
PRs always conflict on it: take either side and run this again on the merged renderer.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.input import ExplorerInput
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.tests import test_explorer_transitions as T

T._KOREAN.start()           # the same screen language the test replays the frames in

FIXTURE_DIR = ROOT / "tools" / "fleet" / "tests" / "fixtures" / "explorer"
check_only = "--check" in sys.argv


def runs(row):
    out = []
    for glyph, colour, tint in row:
        if out and out[-1][:3] == [glyph, colour, tint]:
            out[-1][3] += 1
        else:
            out.append([glyph, colour, tint, 1])
    return out


vm = load_fixture(FIXTURE_DIR / "view_model.json")
path = FIXTURE_DIR / "transitions.json"
fixture = json.loads(path.read_text())
frames, rows, cases = fixture["frames"], fixture["rows"], fixture["cases"]
row_index = {json.dumps(row, ensure_ascii=False): i for i, row in enumerate(rows)}
frame_index = {json.dumps(frame, ensure_ascii=False, sort_keys=True): i for i, frame in enumerate(frames)}
changed = []
for case in cases:
    reader, state = ExplorerInput(), NavState()
    by_state = {T._reference_projection(step["st"]): step for step in case["trace"]}
    order = tuple("session:" + str(item.get("tag")) for item in vm.sessions)
    for chunk in case["chunks"]:
        for event in reader.feed(chunk.encode("latin1")):
            if event.get("type") == "key" and event.get("key") == "q":
                continue
            frame = draw(vm, state, case["cols"], 45, beat=1)
            tabs = tuple(hit.action[1] for hit in frame.hits if hit.action[:1] == ("tab",))
            state, _effects = reduce(state, {**event, "tabs": tabs, "order": order}, frame)
            step = by_state.get(T._nav_projection(state))
            if step is None:
                raise SystemExit(f"{case['name']}: unexpected state {T._nav_projection(state)}")
            if state.view == "PEERS":
                continue                     # untouched here; the test normalizes its lanes
            cells, hits = T._frame_json(draw(vm, state, case["cols"], 45, beat=1))
            stored = frames[step["frame"]]
            expected = T._expand([rows[i] for i in stored["cells"]])
            if expected == cells and stored["hits"] == hits:
                continue
            refs = []
            for row in cells:
                record = runs(row)
                key = json.dumps(record, ensure_ascii=False)
                if key not in row_index:
                    row_index[key] = len(rows)
                    rows.append(record)
                refs.append(row_index[key])
            new_frame = {"cells": refs, "hits": hits}
            key = json.dumps(new_frame, ensure_ascii=False, sort_keys=True)
            if key not in frame_index:
                frame_index[key] = len(frames)
                frames.append(new_frame)
            changed.append((case["name"], case["cols"], T._nav_projection(state)[0],
                            step["frame"], frame_index[key]))
            step["frame"] = frame_index[key]
for item in changed:
    print("moved", *item)
print(f"steps moved: {len(changed)}  frames: {len(frames)}  rows: {len(rows)}")
if not check_only and changed:
    path.write_text(json.dumps(fixture, separators=(",", ":"), ensure_ascii=False))
