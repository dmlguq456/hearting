"""Regenerate the current-screen oracle from synthetic data, without a live Fleet."""
import argparse
import json
import os
from pathlib import Path
from unittest import mock

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture


FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
REGENERATE = "Intentional screen change? Regenerate: python3 -m tools.fleet.tests.freeze_explorer_oracle"
CASES = {
    "OVERVIEW": NavState(),
    **{view: NavState(view=view, title=view) for view in
       ("ROUTES", "QUESTIONS", "HISTORY", "PEERS", "COMPUTE", "MEMORY", "MODELS", "SYSTEM")},
    "repo-page": NavState(scope="repo:xxxxxxxx"),
    "session-page-f5": NavState(scope="session:f5"),
    "route-page-f5": NavState(scope="route:f5"),
    "ROUTES-open-f5": NavState(view="ROUTES", title="ROUTES", folds=(("ropen:ROUTES", "f5"),)),
    # The bb wait unfolded in place (``question.open``); the state keeps its historical name.
    "QUESTIONS-fold-bb": NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qopen:QUESTIONS", "bb"),)),
    "PEERS-pick-f5": NavState(view="PEERS", title="PEERS", folds=(("pfocus:PEERS", "f5"),)),
    "HISTORY-open-0c": NavState(view="HISTORY", title="HISTORY", folds=(("hopen:HISTORY", "0c"),)),
    "HISTORY-open-0c-card": NavState(view="HISTORY", title="HISTORY", folds=(("hopen:HISTORY", "0c"),)),
    "HISTORY-open-0c-picked-generation": NavState(
        view="HISTORY", title="HISTORY", folds=(("hopen:HISTORY", "0c"), ("ui:gen:f5", "0"))),
    "OVERVIEW-open-09": NavState(opened="09", folds=(("sopen:OVERVIEW", "09"),)),
}


def capture(vm, state, cols):
    # Freeze presentation inputs only. The production renderer, styles, cell geometry
    # and ordered actions run unchanged; no collectors or live runtime are started.
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}), \
            mock.patch("time.time", return_value=1_800_000_000):
        frame = draw(vm, state, cols, 45, beat=1)
    return {"cells": [[list(cell) for cell in row] for row in frame.cells],
            "hits": [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in frame.hits]}


def compact(capture):
    rows = []
    for row in capture["cells"]:
        runs = []
        for cell in row:
            if runs and runs[-1][:3] == cell:
                runs[-1][3] += 1
            else:
                runs.append([*cell, 1])
        rows.append(runs)
    return {"cells": rows, "hits": capture["hits"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check without rewriting expectations")
    args = parser.parse_args()
    vm = load_fixture(FIXTURES / "view_model.json")
    mismatches = []
    for cols in (140, 168):
        value = {"schema": 1, "source": "view_model.json", "rows": 45,
                 "cols": cols, "beat": 1, "language": "English",
                 "states": {name: compact(capture(vm, state, cols)) for name, state in CASES.items()}}
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = FIXTURES / f"oracle-{cols}.json"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != data:
                mismatches.append(path.name)
        else:
            path.write_text(data, encoding="utf-8")
    if mismatches:
        print(f"Oracle mismatch: {', '.join(mismatches)}. {REGENERATE}")
        return 1
    print(f"Current-screen oracle: {len(CASES) * 2} frames, "
          f"{'unchanged' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
