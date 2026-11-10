#!/usr/bin/env python3
"""Name the next step of one PR's author -> independent review -> publish chain.

Read-only and stateless: every answer is derived again from the PR's current
GitHub state (head, draft, mergeability, head CI through merge-line's own check
reading) and, when given, the reviewer's report file. Nothing is stored, sent,
merged or commented; the printed `next=` names the one action and its role.

States, in the order they are decided:
  merged, closed, draft, ci-failed, conflict  - the PR itself settles the step
  needs-review       no review report given: the author sends the review request
  awaiting-review    the report's first line is not a closed verdict: a person reads the report
  review-stale       the first-line closed verdict names another head
  fix-requested      closed-form fix verdict on this head
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

# A verdict is read only from the report's first line: the first line that is not
# blank and not a heading (`#...`). That line, with its `**`/`__` emphasis, surrounding
# spaces and one trailing period removed, must be exactly one of these closed forms:
#   최종 판정: 병합 가능 — head <7-40 hex>        Final verdict: mergeable — head <hex>
#   최종 판정: 수정 후 병합 — head <hex>          Final verdict: changes requested — head <hex>
# Any other wording on that line makes it not a verdict, and the rest of the document
# (code examples, quotes, history) is never read, so a person reads the report.
# core/OPERATIONS.md §5.11 names the same rule; keep the two in step.
VERDICT_FORMS = (
    ("merge", "최종 판정: 병합 가능"),
    ("merge", "Final verdict: mergeable"),
    ("fix", "최종 판정: 수정 후 병합"),
    ("fix", "Final verdict: changes requested"),
)


def _form_pattern(label):
    words = [re.escape(word) for word in label.replace(":", " : ").split()]
    return r"\s*".join(words).replace(r"\s*:\s*", r"\s*[:：]\s*")


_CLOSED = re.compile(
    r"^(?P<label>" + "|".join(f"(?:{_form_pattern(label)})" for _, label in VERDICT_FORMS)
    + r")\s+—\s+head\s+`?(?P<head>[0-9a-f]{7,40})`?$", re.IGNORECASE)
_KINDS = {re.sub(r"\s+", "", label).replace("：", ":").lower(): kind for kind, label in VERDICT_FORMS}


def _normalize(line):
    line = line.replace("**", "").replace("__", "").strip()
    return line[:-1].rstrip() if line.endswith(".") else line


def closed_verdict(line):
    """(merge | fix, head hex) when the whole line is one closed form, else None."""
    match = _CLOSED.match(_normalize(line or ""))
    if not match:
        return None
    label = re.sub(r"\s+", "", match.group("label")).replace("：", ":").lower()
    return _KINDS[label], match.group("head").lower()


def first_line(text):
    """The report's first line that is neither blank nor a heading, or ""."""
    for line in (text or "").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return line
    return ""


def review_reading(text, head):
    """{verdict, names_head, line}: verdict merge | fix | none, read from the first line only.

    A closed form whose head is a prefix of the current head is the current verdict; a
    closed form on another head gives names_head False (review-stale).
    """
    head = (head or "").lower()
    line = first_line(text)
    verdict = closed_verdict(line)
    if not verdict:
        return {"verdict": "none", "names_head": False, "line": line.strip()}
    kind, hexa = verdict
    return {"verdict": kind, "names_head": len(head) >= 7 and head.startswith(hexa), "line": line.strip()}


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
    if verdict == "none" and reading["line"]:
        quoted = " ".join(reading["line"].split())[:80]
        row.update(state="awaiting-review", role="reviewer",
                   next=(f"`{quoted}` is not a closed verdict line for head {head[:7]}; a person reads the "
                         "report and takes the next step (a first line exactly "
                         f"`최종 판정: 병합 가능|수정 후 병합 — head {head[:7]}` continues automatically)"))
    elif verdict == "none":
        row.update(state="awaiting-review", role="reviewer",
                   next=(f"no closed verdict line for head {head[:7]}; a person reads the report (a first line exactly "
                         f"`최종 판정: 병합 가능|수정 후 병합 — head {head[:7]}` continues automatically)"))
    elif not names_head:
        row.update(state="review-stale", role="reviewer",
                   next=f"review the change up to head {head[:12]} and restate the final verdict with that head")
    elif verdict == "fix":
        row.update(state="fix-requested", role="author", next="apply the requested fix, push, then request review of the new head")
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
