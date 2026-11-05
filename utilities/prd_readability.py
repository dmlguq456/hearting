#!/usr/bin/env python3
"""Warn-only PRD readability check. Pure text in, report out; never writes files.

`spec-transaction.py` loads this module after a transaction changed `prd.md` and
records the report as one `readability` event. The check only reports; it has no
authority over the write or its exit code.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

SCHEMA = "prd-readability/1"
LIMITS = {"summary_lines": 40, "summary_item_chars": 160, "summary_chars": 2500, "body_line_chars": 600}
MAX_LISTED = 50
EXCERPT_CHARS = 80
BEGIN = "<!-- BLUEPRINT-SUMMARY:BEGIN -->"
END = "<!-- BLUEPRINT-SUMMARY:END -->"
AUTOJUNK_MIDDLE = 5000

CIRCLED_RANGES = (
    (0x2160, 0x217F), (0x2460, 0x24FF), (0x2776, 0x2793), (0x3200, 0x321E),
    (0x3251, 0x325F), (0x3260, 0x327E), (0x3280, 0x32BF), (0x1F100, 0x1F10C),
    (0x1F110, 0x1F169),
)
CIRCLED = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in CIRCLED_RANGES) + "]")

SECTION_REF = re.compile(r"§\s?\d")
VERSION_TAG = re.compile(r"[⟨〈][^⟩〉\n]{0,40}?\bv\d+[^⟩〉\n]{0,40}[⟩〉]")
COMMIT_HASH = re.compile(
    r"(?<![0-9A-Za-z_./#-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![0-9A-Za-z_/-])")
DIGEST_LABEL = re.compile(r"(?i)(sha1|sha256|sha512|md5|blake2b?|digest|checksum)(\s*[:=]\s*|\s+)[`'\"(]?$")
CHECKSUM_HEADER = re.compile(r"(?i)sha|checksum|digest|hash|체크섬|해시|다이제스트")
COMMIT_HEADER = re.compile(r"(?i)commit|커밋|revision")
ID_RULES = (
    ("route-id", re.compile(r"\brt-[0-9a-f]{16}\b")),
    ("revision-id", re.compile(r"\brrev_[0-9a-f]{32}\b")),
    ("cycle-id", re.compile(r"\bcyc_[0-9a-f]{32}\b")),
    ("attempt-id", re.compile(r"\batt-[0-9a-f]{32}\b")),
    ("session-id", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("pane-id", re.compile(r"\bw\d+:p\d+\b")),
)
# Body-only prose rules (prd-authoring "History outside the body" and "Sentences"). They read the
# line with inline code spans blanked, so enum values and identifiers in backticks never match.
# A PRD revision is a bare integer `vN`; a name glued before it ("BC-ResNet v2", "LibriMix v1")
# or a dotted release ("v2.152.0") is not one, so only revision narration is matched.
_REVISION = r"(?<![\w.\-])(?<![A-Za-z0-9][ \-])v\d+(?![.\d])"
_REVISION_WORD = r"(?:바꿈|바꾼|바꿨|바뀜|바뀐|개정|변경|도입|추가|더해|더했|더한|삭제|제거|폐기|대체|신설|정정|교체|이관)"
PROSE_RULES = (
    ("version-mention", re.compile(
        _REVISION + r"(?:\s*(?:부터|까지|당시|이후|이전)"
        r"|(?:에서|으로|로|에)\S{0,2}\s+(?:\S+\s+){0,2}?" + _REVISION_WORD
        + r"|\s+" + _REVISION_WORD + r")"
        r"|(?i:\b(?:since|until|as of|introduced in|changed in|revised in|removed in|added in|replaced in"
        r"|superseded in)\s+v\d+\b(?![.\d]))")),
    ("supersede-narrative", re.compile(
        r"(?i:\bsuperseded by\b|\bformerly\b|\bpreviously\b)"
        r"|(?:대체|폐기)(?:했다|했고|했으며|됐다|되었다|되었고|됨|하였다)"
        r"|이전에는[^.。]{0,80}?(?:였으나|었으나|했으나|였지만|었지만|했지만)"
        r"|현재\s*정본은|아래는\s*이력")),
    ("user-quote", re.compile(
        r"사용자\s*원문|사용자\s*확정\s*\d+\s*건|트리거\s*=|사용자\s*[:：]\s*[\"“「‘']"
        r"|(?i:\buser(?:'s)?\s+(?:original\s+)?words\b|\bthe\s+user\s+said\b|\btrigger\s*=)")),
)
CODE_SPAN = re.compile(r"`[^`\n]*`")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
BLOCK_RULES = ("summary-missing", "summary-position", "summary-lines", "summary-chars")


def fenced_mask(lines: list[str]) -> list[bool]:
    """True for lines inside a fenced code block, fence lines included (CommonMark)."""
    mask = [False] * len(lines)
    fence = None  # (char, length) of the open fence
    for i, line in enumerate(lines):
        if fence is None:
            m = FENCE_OPEN.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                mask[i] = True
        else:
            mask[i] = True
            m = FENCE_OPEN.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
    return mask


def summary_bounds(lines: list[str]) -> tuple[int, int] | None:
    """(BEGIN index, END index), 0-based; None when the marker pair is not usable."""
    mask = fenced_mask(lines)
    begin = next((i for i, l in enumerate(lines) if not mask[i] and l.strip() == BEGIN), None)
    if begin is None:
        return None
    end = next((i for i in range(begin + 1, len(lines)) if not mask[i] and lines[i].strip() == END), None)
    return None if end is None else (begin, end)


def summary_items(block_lines: list[str]) -> list[tuple[int, str]]:
    """(start line offset, item text) per list item or paragraph; markers stripped,
    continuation lines joined with one space, fenced lines and headings left out."""
    mask = fenced_mask(block_lines)
    items: list[list] = []
    current = None
    for i, line in enumerate(block_lines):
        stripped = line.strip()
        if mask[i] or not stripped:
            current = None
        elif stripped.startswith("#"):
            current = None
        elif LIST_MARK.match(line):
            current = [i, LIST_MARK.sub("", line, count=1).strip()]
            items.append(current)
        elif current is not None:
            current[1] = f"{current[1]} {stripped}"
        else:
            current = [i, stripped]
            items.append(current)
    return [(i, text) for i, text in items]


def new_line_numbers(lines: list[str], base_lines: list[str] | None) -> set[int]:
    """1-based numbers of lines that are new or changed relative to `base_lines`."""
    if base_lines is None:
        return set(range(1, len(lines) + 1))
    prefix = 0
    limit = min(len(lines), len(base_lines))
    while prefix < limit and lines[prefix] == base_lines[prefix]:
        prefix += 1
    suffix = 0
    limit -= prefix  # the tail may not overlap the head already matched
    while suffix < limit and lines[len(lines) - 1 - suffix] == base_lines[len(base_lines) - 1 - suffix]:
        suffix += 1
    mid = lines[prefix:len(lines) - suffix]
    base_mid = base_lines[prefix:len(base_lines) - suffix]
    if not mid:
        return set()
    if not base_mid:
        return set(range(prefix + 1, prefix + len(mid) + 1))
    junk = len(mid) > AUTOJUNK_MIDDLE and len(base_mid) > AUTOJUNK_MIDDLE
    matcher = difflib.SequenceMatcher(None, base_mid, mid, autojunk=junk)
    new: set[int] = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            new.update(range(prefix + j1 + 1, prefix + j2 + 1))
    return new


def _excerpt(line: str, start: int = 0) -> str:
    seg = line[max(0, start - 20):].strip()
    return seg if len(seg) <= EXCERPT_CHARS else seg[:EXCERPT_CHARS - 1] + "…"


def _clip(text: str) -> str:
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1] + "…"


def _tables(lines: list[str], mask: list[bool]) -> set[int]:
    """Indexes of lines in a table whose header row has a checksum-like column."""
    exempt: set[int] = set()
    i = 0
    while i < len(lines):
        if mask[i] or not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        j = i
        while j < len(lines) and not mask[j] and lines[j].lstrip().startswith("|"):
            j += 1
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        if any(CHECKSUM_HEADER.search(c) and not COMMIT_HEADER.search(c) for c in cells):
            exempt.update(range(i, j))
        i = j
    return exempt


def _block_findings(lines: list[str]) -> tuple[list[tuple[str, int, str]], tuple[int, int] | None]:
    """Block-level findings as (rule, 1-based line, excerpt) and the marker bounds."""
    bounds = summary_bounds(lines)
    if bounds is None:
        return [("summary-missing", 1, "no BLUEPRINT-SUMMARY marker pair")], None
    begin, end = bounds
    found: list[tuple[str, int, str]] = []
    mask = fenced_mask(lines)
    h1 = next((i for i, l in enumerate(lines) if not mask[i] and l.startswith("# ")), None)
    if h1 is None:
        first = next((i for i, l in enumerate(lines) if l.strip()), None)
    else:
        first = next((i for i in range(h1 + 1, len(lines)) if lines[i].strip()), None)
    if first != begin:
        found.append(("summary-position", begin + 1, "BEGIN marker is not the first content after the H1 title"))
    block = lines[begin + 1:end]
    if len(block) > LIMITS["summary_lines"]:
        found.append(("summary-lines", begin + 1, f"{len(block)} lines > {LIMITS['summary_lines']}"))
    chars = sum(len(l) for l in block)
    if chars > LIMITS["summary_chars"]:
        found.append(("summary-chars", begin + 1, f"{chars} chars > {LIMITS['summary_chars']}"))
    return found, bounds


def _line_findings(lines: list[str], bounds: tuple[int, int] | None) -> list[tuple[str, int, str, str]]:
    """Per-line findings as (rule, 1-based line, area, excerpt): one per (line, rule)."""
    mask = fenced_mask(lines)
    exempt_table = _tables(lines, mask)
    h1 = next((i for i, l in enumerate(lines) if not mask[i] and l.startswith("# ")), None)
    begin, end = bounds if bounds else (-1, -1)
    heading = next((i for i in range(begin + 1, end) if not mask[i] and lines[i].startswith("#")), None) if bounds else None
    found: list[tuple[str, int, str, str]] = []
    for i, line in enumerate(lines):
        if mask[i] or i in (begin, end):
            continue
        area = "summary" if bounds and begin < i < end else "body"
        hits: list[tuple[str, re.Match]] = []
        matches = list(VERSION_TAG.finditer(line))
        if i == heading:
            matches = matches[1:]
        if matches:
            hits.append(("version-tag", matches[0]))
        if i != h1:
            if area == "summary":
                m = SECTION_REF.search(line)
                if m:
                    hits.append(("section-ref", m))
            if i not in exempt_table:
                for m in COMMIT_HASH.finditer(line):
                    if not (DIGEST_LABEL.search(line[:m.start()]) or line.startswith(("…", "..."), m.end())):
                        hits.append(("commit-hash", m))
                        break
            for rule, pattern in ID_RULES:
                m = pattern.search(line)
                if m:
                    hits.append((rule, m))
            m = CIRCLED.search(line)
            if m:
                hits.append(("circled-char", m))
            if area == "body":
                hits.extend(_prose_hits(line))
        for rule, m in hits:
            found.append((rule, i + 1, area, _excerpt(line, m.start())))
        if area == "body" and i != h1 and not line.lstrip().startswith("|") and len(line) > LIMITS["body_line_chars"]:
            found.append(("long-line", i + 1, area, _clip(f"{len(line)} chars > {LIMITS['body_line_chars']}: {line}")))
    return found


def _prose_hits(line: str) -> list[tuple[str, re.Match]]:
    """Body prose rules, one hit per rule; code spans and bracketed version tags are blanked first."""
    blank = lambda m: " " * len(m.group(0))
    text = VERSION_TAG.sub(blank, CODE_SPAN.sub(blank, line))
    hits = []
    for rule, pattern in PROSE_RULES:
        if rule == "version-mention" and line.lstrip().startswith("|"):
            continue  # table rows name models and datasets with versions
        m = pattern.search(text)
        if m:
            hits.append((rule, m))
    return hits


def _summary_info(lines: list[str], bounds: tuple[int, int] | None) -> dict:
    if bounds is None:
        return {"present": False}
    begin, end = bounds
    block = lines[begin + 1:end]
    items = summary_items(block)
    return {"present": True, "lines": len(block), "chars": sum(len(l) for l in block),
            "items": len(items), "max_item_chars": max((len(t) for _i, t in items), default=0)}


def check_text(text: str, base: str | None = None) -> dict:
    lines = text.splitlines()
    base_lines = None if base is None else base.splitlines()
    block_found, bounds = _block_findings(lines)
    new_lines = new_line_numbers(lines, base_lines)
    base_rules = None
    if base_lines is not None:
        base_rules = {rule for rule, _n, _x in _block_findings(base_lines)[0]}
    violations = []
    for rule, number, excerpt in block_found:
        age = "new" if base_rules is None or rule not in base_rules else "existing"
        violations.append({"line": number, "area": "summary", "rule": rule, "age": age, "excerpt": excerpt})
    for rule, number, area, excerpt in _line_findings(lines, bounds):
        violations.append({"line": number, "area": area, "rule": rule,
                           "age": "new" if number in new_lines else "existing", "excerpt": excerpt})
    if bounds:
        begin, end = bounds
        block = lines[begin + 1:end]
        for offset, item in summary_items(block):
            if len(item) > LIMITS["summary_item_chars"]:
                number = begin + 2 + offset
                violations.append({"line": number, "area": "summary", "rule": "summary-item-length",
                                   "age": "new" if number in new_lines else "existing",
                                   "excerpt": _clip(f"{len(item)} chars > {LIMITS['summary_item_chars']}: {item}")})
    violations.sort(key=lambda v: (v["age"] != "new", v["line"], v["rule"]))
    by_rule: dict[str, int] = {}
    by_area: dict[str, int] = {}
    for v in violations:
        by_rule[v["rule"]] = by_rule.get(v["rule"], 0) + 1
        by_area[v["area"]] = by_area.get(v["area"], 0) + 1
    total = len(violations)
    new = sum(1 for v in violations if v["age"] == "new")
    listed = violations[:MAX_LISTED]
    return {
        "schema": SCHEMA,
        "limits": dict(LIMITS),
        "base": "none" if base is None else "preimage",
        "summary": _summary_info(lines, bounds),
        "counts": {"total": total, "new": new, "existing": total - new,
                   "by_rule": dict(sorted(by_rule.items())), "by_area": dict(sorted(by_area.items()))},
        "violations": listed,
        "listed": len(listed),
        "truncated": total - len(listed),
    }


def check_bytes(post: bytes, pre: bytes | None) -> dict:
    decode = lambda raw: raw.decode("utf-8", errors="replace")
    return check_text(decode(post), None if pre is None else decode(pre))


def stderr_line(report: dict) -> str | None:
    counts = report["counts"]
    if not counts["total"]:
        return None
    ranked = sorted(counts["by_rule"].items(), key=lambda kv: (-kv[1], kv[0]))
    parts = ", ".join(f"{rule} {n}" for rule, n in ranked[:5]) + (", …" if len(ranked) > 5 else "")
    noun = "warning" if counts["total"] == 1 else "warnings"
    return (f"prd-readability: {counts['total']} {noun} (new {counts['new']}, existing {counts['existing']}): "
            f"{parts} — see the readability event")


def _human(path: str, report: dict) -> str:
    s, c = report["summary"], report["counts"]
    out = [f"file: {path}"]
    if s["present"]:
        lim = report["limits"]
        out.append(f"summary: lines={s['lines']}/{lim['summary_lines']} chars={s['chars']}/{lim['summary_chars']} "
                   f"items={s['items']} max_item={s['max_item_chars']}/{lim['summary_item_chars']}")
    else:
        out.append("summary: missing")
    out.append(f"total={c['total']} new={c['new']} existing={c['existing']} base={report['base']}")
    for rule, n in sorted(c["by_rule"].items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"  {rule}: {n}")
    for v in report["violations"]:
        out.append(f"{v['line']}: [{v['age']}] {v['area']}/{v['rule']}: {v['excerpt']}")
    if report["truncated"]:
        out.append(f"... {report['truncated']} more not listed")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prd_readability.py", allow_abbrev=False,
                                     description="Warn-only PRD readability report; exit 0 whenever the check completes.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", allow_abbrev=False, help="report readability findings for one prd.md")
    check.add_argument("prd", type=Path)
    check.add_argument("--base", type=Path, default=None, help="previous prd.md; lines it already has count as existing")
    check.add_argument("--json", action="store_true", help="print the receipt payload as JSON")
    args = parser.parse_args(argv)
    try:
        post = args.prd.read_bytes()
        pre = None if args.base is None else args.base.read_bytes()
    except OSError as exc:
        parser.error(str(exc))
    report = check_bytes(post, pre)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else _human(str(args.prd), report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
