#!/usr/bin/env python3
"""Tier A/B write-target parser for the Bash channel of artifact-guard.sh (C-2b).

Given a shell command string and a cwd, returns the set of literal write
targets it can decide (Tier A: `>`/`>>`/`tee`/`cp`/`mv`/`install`/`ln`/
`mkdir -p`/`touch`/`rm`, with one level of `sh -c "<literal>"` recursion) and
the set of segments it cannot decide (Tier B: `$`/backtick/`$(`/glob-meta
targets, interpreter-mediated writes, or a parse failure).

Fail-safe by construction: anything not confidently Tier A becomes Tier B,
never a Tier A block. Never silently loses information — an unparseable
command is Tier B, not dropped.

Import-reuses hooks/material-route-guard.py's `_shell_segments()` for shell
tokenization (do not duplicate the parser — see C-23 drift precedent).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "material_route_guard", Path(__file__).resolve().parent / "material-route-guard.py"
)
_mrg = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mrg)
_shell_segments = _mrg._shell_segments  # noqa: SLF001 (deliberate reuse, not duplication)

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "awk"}
_GLOB_META = re.compile(r"[*?\[]")
_DOLLAR = re.compile(r"\$|`")

_REDIRECT_GLUED_RE = re.compile(r"^(?:\d+)?(?:>>|>|&>)(.*)$")

_TIER_A_VERBS = {"cp", "mv", "install", "ln", "mkdir", "touch", "rm", "tee"}


def _is_undecidable_target(token: str) -> bool:
    return bool(_DOLLAR.search(token) or _GLOB_META.search(token))


def _resolve(cwd: Path, raw: str) -> str:
    p = Path(raw)
    return str((p if p.is_absolute() else cwd / p).resolve(strict=False))


def parse(command: str, cwd: Path) -> dict:
    decidable: list[str] = []
    undecidable: list[dict] = []

    def walk(cmd: str, base: Path, depth: int) -> None:
        if depth > 1:
            undecidable.append({"reason": "recursion-depth-exceeded", "segment": cmd[:200]})
            return
        try:
            segments = list(_shell_segments(cmd))
        except Exception:
            undecidable.append({"reason": "parse-error", "segment": cmd[:200]})
            return
        current_cwd = base
        for segment in segments:
            if not segment:
                continue
            head = Path(segment[0]).name

            if head == "cd" and len(segment) == 2 and not segment[1].startswith("-"):
                current_cwd = Path(_resolve(current_cwd, segment[1]))
                continue

            if head in _SHELLS:
                try:
                    dash_c = segment.index("-c")
                except ValueError:
                    dash_c = -1
                if dash_c >= 0 and dash_c + 1 < len(segment):
                    walk(segment[dash_c + 1], current_cwd, depth + 1)
                    continue
                undecidable.append(
                    {"reason": "shell-invocation-without-literal-c", "segment": " ".join(segment)[:200]}
                )
                continue

            interp_base = re.sub(r"\d+(\.\d+)?$", "", head)
            if interp_base in _INTERPRETERS or head in {"sed"} and "-i" in segment[1:]:
                undecidable.append({"reason": "interpreter-mediated-write", "segment": " ".join(segment)[:200]})
                continue

            # Redirects: shlex (no ">" in punctuation_chars) leaves the
            # operator glued to its target when there is no surrounding
            # whitespace (">/dev/null", "2>&1"), and as its own token when
            # there is ("> /dev/null"). Handle both.
            redirected = False
            idx = 0
            while idx < len(segment):
                token = segment[idx]
                glued = _REDIRECT_GLUED_RE.match(token)
                if not glued:
                    idx += 1
                    continue
                redirected = True
                target = glued.group(1)
                consumed = 1
                if target == "" and idx + 1 < len(segment):
                    target = segment[idx + 1]
                    consumed = 2
                if target and not target.startswith("&"):
                    if _is_undecidable_target(target):
                        undecidable.append({"reason": "undecidable-redirect-target", "segment": " ".join(segment)[:200]})
                    else:
                        decidable.append(_resolve(current_cwd, target))
                idx += consumed
            if redirected:
                continue

            if head not in _TIER_A_VERBS:
                continue

            args = [a for a in segment[1:] if not a.startswith("-")]
            if not args:
                continue
            if any(_is_undecidable_target(a) for a in args):
                undecidable.append({"reason": "undecidable-verb-argument", "segment": " ".join(segment)[:200]})
                continue

            if head == "tee":
                for a in args:
                    decidable.append(_resolve(current_cwd, a))
            elif head in {"cp", "mv", "install", "ln"}:
                # Last literal argument is the destination.
                decidable.append(_resolve(current_cwd, args[-1]))
            elif head in {"mkdir", "touch", "rm"}:
                for a in args:
                    decidable.append(_resolve(current_cwd, a))

    walk(command, cwd, 0)
    return {"decidable": sorted(set(decidable)), "undecidable": undecidable}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--command", required=True)
    ap.add_argument("--cwd", required=True)
    args = ap.parse_args(argv)
    result = parse(args.command, Path(args.cwd))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
