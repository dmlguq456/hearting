#!/usr/bin/env python3
"""`hearting run pane open [--title TEXT] -- <command...>`: one command pane beside the caller.

Prints one JSON line, `{surface, pane, placement, notes, view_command}`. With no
terminal surface nothing opens: `surface` is `none`, `view_command` is the line to
run by hand, and the exit status is 0. A surface that could not open the pane or
start the command exits 1. Nothing is retried; `notes` says what was observed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal_surface import open_pane  # noqa: E402


def parser():
    value = argparse.ArgumentParser(prog="hearting run pane")
    actions = value.add_subparsers(dest="action", required=True)
    opening = actions.add_parser("open", help="open one command pane beside the caller")
    opening.add_argument("--title", help="pane name; defaults to the command's name")
    opening.add_argument("command", nargs=argparse.REMAINDER)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser().error("a command is required after --")
    result = open_pane(command, title=args.title or Path(command[0]).name, cwd=os.getcwd())
    print(json.dumps(result.json(), ensure_ascii=False, separators=(",", ":")))
    opened = result.pane is not None and "pane-launch-failed" not in result.notes
    return 0 if result.surface == "none" or opened else 1


if __name__ == "__main__":
    raise SystemExit(main())
