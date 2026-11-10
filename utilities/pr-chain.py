#!/usr/bin/env python3
"""Name the next step of one PR's author -> independent review -> publish chain.

Read-only and stateless: every answer is derived again from the PR's current
GitHub state (head, draft, mergeability, head CI through merge-line's own check
reading) and, when given, the reviewer's report file. Nothing is stored, sent,
merged or commented; the printed `next=` names the one action and its role.

States, in the order they are decided:
  merged, closed, draft, ci-failed, conflict  - the PR itself settles the step
  needs-review       no review report given: the author sends the review request
  awaiting-review    the report has no final verdict yet
  fix-requested      the final verdict asks for changes
  review-stale       the final verdict does not name the current head
  awaiting-ci        final merge verdict on this head, head CI still running
  ready-to-publish   final merge verdict on this head and head CI succeeded
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent

# A verdict is one of three line forms (the line's own `>*#_-` decoration aside):
#   labeled   `최종 판정:` / `판정:` / `Final verdict:` / `Verdict:` (an optional
#             parenthesised date before the colon); "초도 판정", "변경분 판정" are notes
#   first     the report's first non-heading line starts with a verdict phrase
#             ("병합 가능 — 검토 head …", "수정 후 병합 …")
#   title     a heading ending `— 최종 <verdict>`, read only when no other form exists
# The head a verdict names is looked for only in its own block: the verdict line and
# the lines after it up to a blank line or the next verdict, plus a heading directly
# above it ("## 재검토 head …"); a title also takes the paragraph under it.
_DECOR = r"[\s>*#_\-]*"
_LABELED = re.compile(
    rf"^{_DECOR}(?:최종\s*판정|판정|final\s+verdict|verdict)\s*(?:\([^)\n]*\))?\s*[:：]\s*[*_`\s]*(?P<value>.*)$",
    re.IGNORECASE)
_TITLE = re.compile(r"^\s*#{1,6}\s.*[—–-]\s*최종\s+(?P<value>[^\n]+?)\s*$")
_HEADING = re.compile(r"^\s*#{1,6}\s")
# Rejection and conditional wording; checked before any merge word, on word boundaries.
_FIX = re.compile(
    r"수정\s*후\s*병합|수정\s*(?:이\s*)?(?:필요|요구|요청)|병합\s*불가|거부|반려"
    r"|병합\s*(?:가능\s*)?(?:하지|하진|하면)?\s*(?:않|안\s*됨|안\s*된|못|없|아니)"
    r"|\b(?:changes?\s+requested|request(?:ing|ed)?\s+changes|with\s+changes|do\s+not\s+merge"
    r"|don'?t\s+merge|not\s+(?:yet\s+)?(?:mergeable|merge-?ready|ready|approved?)|unmergeable"
    r"|reject(?:ed)?|fix(?:es)?|fail(?:ed|s)?)\b", re.IGNORECASE)
_MERGE = re.compile(
    r"병합\s*가능|머지\s*가능|\b(?:merge-?ready|mergeable|ready\s+to\s+merge|approve[ds]?|pass(?:ed)?|lgtm)\b",
    re.IGNORECASE)
# Any other negation inside the verdict clause keeps a merge word from reading as merge.
_NEGATION = re.compile(r"\b(?:not|never|cannot|no)\b|n't\b|않|불가|못|아니|금지", re.IGNORECASE)
_FIRST = re.compile(rf"^{_DECOR}(?:{_FIX.pattern}|{_MERGE.pattern})", re.IGNORECASE)
# Only the verdict's own first clause is read: "병합 가능. 차단0, 수정 불필요" stays a merge.
_CLAUSE_END = re.compile(r"[.,;(*—。]")
_HEX = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])", re.IGNORECASE)


def classify(value):
    """merge | fix | none for one verdict value."""
    clause = _CLAUSE_END.split(value or "", 1)[0].strip()
    if _FIX.search(clause):
        return "fix"
    if _MERGE.search(clause):
        return "fix" if _NEGATION.search(clause) else "merge"
    return "none"


def _verdicts(text):
    """[(value, block, kind)] in document order; title verdicts only as a fallback."""
    lines = (text or "").splitlines()
    found, titles = [], []
    first = next((i for i, line in enumerate(lines) if line.strip() and not _HEADING.match(line)), None)
    for i, line in enumerate(lines):
        labeled = _LABELED.match(line)
        if labeled:
            found.append((i, labeled.group("value"), "labeled"))
        elif i == first and _FIRST.match(line):
            found.append((i, line.strip(" >*#_-"), "first"))
        else:
            title = _TITLE.match(line)
            if title and classify(title.group("value")) != "none":
                titles.append((i, title.group("value"), "title"))
    rows = found or titles
    starts = {i for i, _, _ in rows}
    out = []
    for i, value, kind in rows:
        block = [lines[i]]
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and _HEADING.match(lines[j]) and j not in starts:
            block.append(lines[j])
        j = i + 1
        if kind == "title":
            while j < len(lines) and not lines[j].strip():
                j += 1
        while j < len(lines) and lines[j].strip() and j not in starts:
            block.append(lines[j])
            j += 1
        out.append((value, "\n".join(block), kind))
    return out


def _names(block, head):
    head = (head or "").lower()
    return len(head) >= 7 and any(head.startswith(run.lower()) for run in _HEX.findall(block))


def review_reading(text, head):
    """{verdict, names_head, line}: verdict merge | fix | none; line is the verdict read.

    The current verdict is the first one whose block names the current head (a review
    added below an older one, or kept above it, both count); without one, the first
    verdict. Different readable verdicts naming the current head are left unread.
    """
    rows = _verdicts(text)
    if not rows:
        return {"verdict": "none", "names_head": False, "line": ""}
    current = [(value, classify(value)) for value, block, _ in rows if _names(block, head)]
    if current:
        readable = {verdict for _, verdict in current if verdict != "none"}
        if len(readable) > 1:
            return {"verdict": "none", "names_head": True, "line": "conflicting verdicts for this head",
                    "conflict": True}
        value, verdict = current[0]
        return {"verdict": verdict, "names_head": True, "line": value.strip()}
    value = rows[0][0]
    return {"verdict": classify(value), "names_head": False, "line": value.strip()}


def review_verdict(text, head):
    """(verdict, names_head): verdict is merge | fix | none."""
    reading = review_reading(text, head)
    return reading["verdict"], reading["names_head"]


def _merge_line():
    spec = importlib.util.spec_from_file_location("merge_line", HERE / "merge-line.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ci_state(snapshot, merge_line=None):
    """success | pending | failure, with merge-line's reading of the head's checks."""
    merge_line = merge_line or _merge_line()
    try:
        return "success" if merge_line.check_state(snapshot) else "pending"
    except merge_line.MergeError:
        return "failure"


def review_verdict(text, head):
    """(verdict, names_head): verdict is merge | fix | none."""
    match = _FINAL.search(text or "")
    if not match:
        return "none", False
    value = _CLAUSE_END.split(match.group("value"), 1)[0].strip().lower()
    verdict = "none"
    # Fix wording is checked first: "수정 후 병합" also contains "병합".
    if any(word in value for word in _FIX):
        verdict = "fix"
    elif any(word in value for word in _MERGE):
        verdict = "merge"
    names_head = bool(head) and len(head) >= 7 and head[:7].lower() in (text or "").lower()
    return verdict, names_head


def decide(snapshot, review_text=None, merge_line=None):
    pr = snapshot.get("number")
    head = snapshot.get("headRefOid") or ""
    ci = ci_state(snapshot, merge_line) if snapshot.get("state") == "OPEN" else "-"
    row = {"pr": pr, "head": head, "ci": ci, "review": "-", "state": "", "role": "-", "next": ""}
    if snapshot.get("state") == "MERGED":
        row.update(state="merged", role="publisher", next="none: merged; release/install follow the publisher's existing flow")
        return row
    if snapshot.get("state") != "OPEN":
        row.update(state="closed", next="none: PR is closed")
        return row
    if snapshot.get("isDraft"):
        row.update(state="draft", role="author", next="mark the PR ready, then request independent review")
        return row
    if ci == "failure":
        row.update(state="ci-failed", role="author", next="fix the head CI failure, push, then request review of the new head")
        return row
    if snapshot.get("mergeable") == "CONFLICTING":
        row.update(state="conflict", role="author", next="rebase onto the latest default branch, push, then request review of the new head")
        return row
    if review_text is None:
        row.update(state="needs-review", role="author",
                   next=f"send the review request for PR #{pr} head {head[:12]} to the reviewer named in the assignment")
        return row
    reading = review_reading(review_text, head)
    verdict, names_head = reading["verdict"], reading["names_head"]
    row["review"] = verdict if verdict == "none" or names_head else f"{verdict}-other-head"
    if verdict == "none" and reading.get("conflict"):
        row.update(state="awaiting-review", role="reviewer",
                   next=(f"the report gives both a merge and a fix verdict for head {head[:12]}; a person "
                         "reads it, and the reviewer keeps one current verdict for that head"))
    elif verdict == "none" and reading["line"]:
        quoted = " ".join(reading["line"].split())[:80]
        row.update(state="awaiting-review", role="reviewer",
                   next=(f"verdict `{quoted}` is not readable as merge or fix; a person reads the report, "
                         f"and the reviewer restates it as `최종 판정: 병합 가능|수정 후 병합 — head {head[:7]}`"))
    elif verdict == "none":
        row.update(state="awaiting-review", role="reviewer",
                   next=f"write the final verdict for this head as `최종 판정: 병합 가능|수정 후 병합 — head {head[:7]}`")
    elif verdict == "fix":
        row.update(state="fix-requested", role="author", next="apply the requested fix, push, then request review of the new head")
    elif not names_head:
        row.update(state="review-stale", role="reviewer",
                   next=f"review the change up to head {head[:12]} and restate the final verdict with that head")
    elif ci == "pending":
        row.update(state="awaiting-ci", role="publisher",
                   next=f"publish through `hearting run merge-line {pr}`; it waits for this head's CI and refuses a failure")
    else:
        row.update(state="ready-to-publish", role="publisher",
                   next=f"publish through `hearting run merge-line {pr}` and the existing release/install flow")
    return row


def format_line(row):
    return (f"pr_chain={row['state']} pr=#{row['pr']} head={(row['head'] or '-')[:12]} "
            f"ci={row['ci']} review={row['review']} role={row['role']} next={row['next']}")


def _gh(args, cwd=None, timeout=30):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if result.returncode:
        raise RuntimeError(" ".join((result.stderr or result.stdout or "gh failed").split()))
    return json.loads(result.stdout)


def snapshot(selector, cwd=None, timeout=30):
    return _gh(["pr", "view", str(selector), "--json",
                "number,state,isDraft,baseRefName,headRefOid,mergeable,statusCheckRollup"],
               cwd=cwd, timeout=timeout)


def github_remote(cwd):
    """True only when the checkout has a GitHub remote; no network."""
    try:
        result = subprocess.run(["git", "-C", str(cwd), "remote", "-v"], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "github.com" in result.stdout


# Capabilities whose finished route is the author's source result; a review, audit or
# research route finished on the same PR branch is not the author asking for review.
SOURCE_CAPABILITIES = ("autopilot-code", "code-execute")


def finish_line(cwd, timeout=20, capability="autopilot-code"):
    """Best-effort `next:` line for a finished source route whose branch has an open PR, or ""."""
    try:
        if capability not in SOURCE_CAPABILITIES or not cwd or not github_remote(cwd):
            return ""
        branch = subprocess.run(["git", "-C", str(cwd), "symbolic-ref", "-q", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if not branch:
            return ""
        row = decide(snapshot(branch, cwd=cwd, timeout=timeout))
    except Exception:
        return ""
    if row["state"] in ("merged", "closed"):
        return ""
    return "next: " + format_line(row)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    nxt = sub.add_parser("next", help="print the PR's current chain state and next action")
    nxt.add_argument("pr", nargs="?", help="PR number, URL or branch (default: current branch)")
    nxt.add_argument("--review", help="the reviewer's report file for this PR")
    nxt.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    review_text = None
    if args.review:
        try:
            review_text = Path(args.review).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"pr_chain=unknown reason=review-unreadable:{exc.strerror or exc}", flush=True)
            return 1
    try:
        snap = snapshot(args.pr) if args.pr else _gh(["pr", "view", "--json",
            "number,state,isDraft,baseRefName,headRefOid,mergeable,statusCheckRollup"])
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"pr_chain=unknown reason={' '.join(str(exc).split()) or type(exc).__name__}", flush=True)
        return 1
    row = decide(snap, review_text)
    print(json.dumps(row, ensure_ascii=False, sort_keys=True) if args.json else format_line(row), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
