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

# The first final-verdict line of a review report is its current verdict; later
# lines are kept history. A bare initial note ("초도 판정", "first pass") is not final.
_FINAL = re.compile(
    r"^[\s>*#_-]*(?:최종\s*판정|final\s+verdict|verdict)\s*[:：]\s*[*_`\s]*(?P<value>[^\n]*)",
    re.IGNORECASE | re.MULTILINE)
_MERGE = ("병합 가능", "merge-ready", "mergeable", "ready to merge", "approve", "pass")
_FIX = ("수정 후 병합", "수정 필요", "changes requested", "request changes", "fix", "fail")
# Only the verdict's own first clause is read: "병합 가능. 차단0, 수정 불필요" stays a merge.
_CLAUSE_END = re.compile(r"[.,;(*—。]")


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
    verdict, names_head = review_verdict(review_text, head)
    row["review"] = verdict if verdict == "none" or names_head else f"{verdict}-other-head"
    if verdict == "none":
        row.update(state="awaiting-review", role="reviewer", next="write the final verdict for this head")
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


def finish_line(cwd, timeout=20):
    """Best-effort `next:` line for a finished source route whose branch has an open PR, or ""."""
    try:
        if not cwd or not github_remote(cwd):
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
