#!/usr/bin/env python3
"""Validate the SD caller declaration ledger without modifying it."""
from __future__ import annotations
import argparse, ast, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = {48,49,50,58,61,64,65,66,67,69,70,72,78,79,83,91,92,93,97,100,103,106,110,112}
KINDS = {"procedure-step", "producer-symbol", "gate-fixture"}

def parse_rows(path):
    lines = [x.strip().split("\t") for x in path.read_text().splitlines() if x.strip() and not x.startswith("#")]
    if not lines or lines[0] != ["sd", "caller_kind", "anchor", "status"]:
        raise ValueError("header")
    rows = [dict(zip(lines[0], row)) for row in lines[1:]]
    seen = set()
    for row in rows:
        m = re.fullmatch(r"SD-(\d+)", row["sd"])
        if not m or int(m.group(1)) <= 0: raise ValueError(f"invalid sd {row['sd']}")
        if row["sd"] in seen: raise ValueError(f"duplicate {row['sd']}")
        seen.add(row["sd"])
        if row["caller_kind"] not in KINDS or row["status"] not in {"wired", "baseline"}: raise ValueError(f"invalid row {row}")
        if row["status"] == "baseline" and row["anchor"] != "-": raise ValueError(f"baseline anchor {row['sd']}")
    return rows

SYMBOL_KINDS = {"producer-symbol", "gate-fixture"}


def symbol_is_defined(path, text, symbol):
    """True only when `symbol` is really *defined* in `text`.

    A mention of the name -- a call, a string, an import -- never counts.
    Python is checked through the AST; shell scripts through a function
    definition line. Any other suffix is unsupported and fails closed.
    """
    if path.suffix == ".py":
        try: tree = ast.parse(text)
        except SyntaxError: return False
        wanted = symbol.split(".")[-1]
        return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and node.name == wanted for node in ast.walk(tree))
    if path.suffix == ".sh":
        return re.search(r"^\s*(?:function\s+)?" + re.escape(symbol) + r"\s*\(\)\s*\{", text, re.M) is not None
    return False


def resolve_anchor(root, row):
    """Resolve one ledger anchor. No fallback: a bare SD mention never passes."""
    if row["status"] == "baseline": return True
    anchor, kind = row["anchor"], row["caller_kind"]
    if kind == "procedure-step":
        if "#" not in anchor: return False
        file, heading = anchor.split("#", 1)
        try: text = (root / file).read_text()
        except OSError: return False
        marker = re.search(r"^#{2,6} .*" + re.escape(heading) + r"\s*$", text, re.M)
        if not marker: return False
        next_heading = re.search(r"^#{2,6} ", text[marker.end():], re.M)
        section = text[marker.end(): marker.end() + next_heading.start() if next_heading else None]
        # heading and in-section SD are two separate conditions, both required.
        return bool(marker) and row["sd"] in section
    if kind in SYMBOL_KINDS:
        if "::" not in anchor: return False
        file, symbol = anchor.split("::", 1)
        path = root / file
        try: text = path.read_text()
        except OSError: return False
        return symbol_is_defined(path, text, symbol) and row["sd"] in text
    return False

def previous_baseline_count(root, path):
    try:
        result = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{path.relative_to(root)}"], capture_output=True, text=True, env={})
        if result.returncode != 0: return None
        old = result.stdout
        return sum(1 for row in old.splitlines() if row.startswith("SD-") and row.endswith("\tbaseline"))
    except Exception: return None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); ap.add_argument("--prd", type=Path)
    args = ap.parse_args(); path = ROOT / "tools/sd-procedure-hooks.tsv"
    try:
        rows = parse_rows(path); nums = {int(r["sd"][3:]) for r in rows}; baseline = {int(r["sd"][3:]) for r in rows if r["status"] == "baseline"}
        if not baseline <= BASELINE: raise ValueError("baseline outside frozen set")
        if previous_baseline_count(ROOT, path) is not None and len(baseline) > previous_baseline_count(ROOT, path): raise ValueError("baseline increased")
        for row in rows:
            if not resolve_anchor(ROOT, row): raise ValueError(f"anchor {row['sd']}")
        prd_sds = 0
        if args.prd:
            prd_sds = len(set(re.findall(r"^#{2,6} .*SD-(\d+)", args.prd.read_text(), re.M)))
            missing = {int(x) for x in re.findall(r"^#{2,6} .*SD-(\d+)", args.prd.read_text(), re.M)} - nums
            if missing: raise ValueError("PRD SDs missing: " + ",".join(map(str, sorted(missing))))
        print(f"check=ok\nrows={len(rows)}\nwired={len(rows)-len(baseline)}\nbaseline={len(baseline)}" + (f"\nprd_sds={prd_sds}" if args.prd else "")); return 0
    except (OSError, ValueError) as exc:
        print(f"error=invalid detail={exc}", file=sys.stderr); return 1
if __name__ == "__main__": raise SystemExit(main())
