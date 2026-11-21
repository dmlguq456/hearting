#!/usr/bin/env python3
"""Dev-only: print the MODELS profile table and the box under it as text, the way the product draws it.

    python3 tools/fleet/tests/models_follow_picture.py [--answer FILE] [--width 140] [--select deep/codex]
        [--today 2026-11-22] [--open followed,held] [--fold ui:fol=keep]

``FILE`` holds the answer of ``model_follow.fleet_view()`` as JSON (a list with one item per harness).
Without it the table is drawn as a quiet day: no follow answer at all. The table itself is a made-up
one (``base_models``): this reads no session, no user table, no catalog and no artifact folder, and
leaves no file behind. The drawing is the explorer's own (``frame.draw`` through ``focus.follow_into``,
the same step the live read takes); nothing is drawn by hand.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

LEFT = 34          # the pane starts after the navigation column (models.X)

_CLAUDE = {"top": ["opus", "max"], "deep": ["opus", "xhigh"], "balanced-deep": ["opus", "medium"],
           "balanced": ["sonnet", "high"], "light": ["sonnet", "medium"],
           "mini": ["claude-haiku-5-5", "medium"]}
_CODEX = {"top": ["gpt-6-astra", "xhigh"], "deep": ["gpt-6-astra", "high"],
          "balanced-deep": ["gpt-6.1-sol", "xhigh"], "balanced": ["gpt-6.1-terra", "xhigh"],
          "light": ["gpt-6-luna", "xhigh"], "mini": ["gpt-6-luna", "high"]}
_OPENCODE = {"top": ["opencode-go/glm-5.3-flash", "max"], "deep": ["opencode-go/glm-5.3-flash", "max"],
             "balanced-deep": ["opencode-go/glm-5.3-flash", "max"],
             "balanced": ["opencode-go/deepseek-v4.1-flash", "max"],
             "light": ["opencode-go/muse-spark", "max"], "mini": ["opencode-go/deepseek-v4.1-flash", "xhigh"]}
_SHOWN = {"opus": "Opus", "sonnet": "Sonnet", "claude-haiku-5-5": "Haiku 5.5", "fable": "Fable",
          "gpt-6-astra": "gpt-6-astra", "gpt-6.1-sol": "gpt-6.1-sol", "gpt-6.1-terra": "gpt-6.1-terra",
          "gpt-6-luna": "gpt-6-luna", "opencode-go/glm-5.3-flash": "glm-5.3-flash",
          "opencode-go/deepseek-v4.1-flash": "deepseek-v4.1-flash",
          "opencode-go/muse-spark": "muse-spark"}


def base_models():
    """A made-up models view in the shape ``focus.read_model_config`` returns (no ``follow`` key)."""
    return {
        "profiles": ["top", "deep", "balanced-deep", "balanced", "light", "mini"],
        "profile_map": {"claude": dict(_CLAUDE), "codex": dict(_CODEX), "opencode": dict(_OPENCODE)},
        "model_choices": {"claude": ["opus", "sonnet", "claude-haiku-5-5"],
                          "codex": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"],
                          "opencode": ["opencode-go/glm-5.3-flash", "opencode-go/deepseek-v4.1-flash",
                                       "opencode-go/muse-spark"]},
        "main_only": {"claude": ["fable"]}, "efforts": ["low", "medium", "high", "xhigh", "max"],
        "pools": {profile: [[["claude", "codex"]], "last resort codex"] if profile != "top" else None
                  for profile in ("top", "deep", "balanced-deep", "balanced", "light", "mini")},
        "effort_keys": {"low": "dim", "medium": "eff_medium", "high": "soft", "xhigh": "eff_xhigh",
                        "max": "dead"},
        "shown": dict(_SHOWN), "context": {}, "context_source": {}, "catalog_sources": {},
        "tidy": None, "token_units": {}, "allocation": {}, "steward": {}}


def fit_table(models, answer):
    """Make the made-up table agree with the answer: a table cell takes the model and effort of the
    answer's ``profile/<name>`` cell, and a model the table did not hold becomes a choice."""
    for item in answer if isinstance(answer, list) else ():
        harness = item.get("harness")
        for cell in item.get("cells", ()):
            profile = str(cell.get("target", "")).partition("profile/")[2]
            if profile in models["profile_map"].get(harness, {}) and models["profile_map"][harness][profile]:
                models["profile_map"][harness][profile] = [cell["model"], cell["effort"]]
    for harness, cells in models["profile_map"].items():          # the choices are the models the table holds
        models["model_choices"][harness] = list(dict.fromkeys(
            cell[0] for cell in cells.values() if cell))
    return models


def epoch_of(day):
    """Local noon of ``YYYY-MM-DD`` as the epoch seconds the explorer's clock carries."""
    year, month, date = (int(part) for part in day.split("-"))
    return datetime.datetime(year, month, date, 12, 0).timestamp()


def section_text(models, cols=140, selected="deep/codex", today=None, folds=None, rows=70):
    """The MODEL PROFILE rows through the bottom border of the box, as text (the navigation cut off)."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.state import NavState
    from tools.fleet.explorer.view_model import ExplorerViewModel
    chosen = {"ui:sel": selected} if selected else {}
    chosen.update(folds or {})
    state = NavState(view="MODELS", title="MODELS", folds=tuple(sorted(chosen.items())))
    vm = ExplorerViewModel.from_mapping({"models": models, "clock": epoch_of(today) if today else None})
    text = draw(vm, state, cols, rows, beat=1).text_rows()
    start = next(index for index, row in enumerate(text) if row[LEFT:].startswith("MODEL PROFILE"))
    out = []
    for row in text[start:]:
        out.append(("│ " + row[LEFT:]).rstrip())
        if "└─" in row[LEFT:LEFT + 4] and "┘" in row:
            break
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--answer", help="JSON file: the answer of model_follow.fleet_view()")
    parser.add_argument("--sample", help="a made-up answer of tools/fleet/tests/models_follow_samples.py instead")
    parser.add_argument("--unlisted", action="append", default=[],
                        help="HARNESS:MODEL the harness's list does not hold (a cell with nothing to follow)")
    parser.add_argument("--width", type=int, action="append", help="screen width(s); default 140")
    parser.add_argument("--select", default="deep/codex", help="the chosen cell, <profile>/<harness>; '' for none")
    parser.add_argument("--today", help="the day the screen counts three days from, YYYY-MM-DD")
    parser.add_argument("--open", default="", help="comma list of summary lines to show opened")
    parser.add_argument("--fold", action="append", default=[], help="a view fold, KEY=VALUE (e.g. ui:fol=keep)")
    args = parser.parse_args(argv)
    models = base_models()
    answer = None
    if args.answer:
        answer = json.loads(Path(args.answer).read_text(encoding="utf-8"))
    elif args.sample:
        from tools.fleet.tests import models_follow_samples
        answer = models_follow_samples.samples()[args.sample]
        args.today = args.today or models_follow_samples.TODAY
    if answer is not None:
        from tools.fleet.explorer import focus
        focus.follow_into(fit_table(models, answer), answer, [tuple(item.split(":", 1)) for item in args.unlisted])
    folds = dict(item.partition("=")[::2] for item in args.fold)
    for name in filter(None, args.open.split(",")):
        folds["ui:fopen:" + name] = "open"
    for width in args.width or [140]:
        print(f"## width {width} · chosen {args.select or '-'}")
        print("\n".join(section_text(models, width, args.select, args.today, folds)))
        print()


if __name__ == "__main__":
    main()
