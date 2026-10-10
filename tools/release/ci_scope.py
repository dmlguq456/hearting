#!/usr/bin/env python3
"""Select expensive Checks jobs; uncertain changes retain the full suite."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_plan", ROOT / "tools/release/plan.py")
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)
DOCUMENTS = {
    "AGENTS.md", "CLAUDE.md", "INSTALL_LAYOUT.md", "LICENSE", "MANUAL.md",
    "README.md", "README.ko.md", "RELEASE_POLICY.md",
}


def documentation_path(path):
    return path in DOCUMENTS or (path.startswith("docs/") and path.endswith(".md"))


def published_tag(context):
    repository = context.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository unavailable")
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/releases/latest", "--jq", ".tag_name"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    tag = result.stdout.strip()
    PLAN.parse_version(tag, stable_only=True)
    return tag


def gh_api(path):
    result = subprocess.run(
        ["gh", "api", path], check=True, capture_output=True, text=True, timeout=15,
    )
    return json.loads(result.stdout)


def validated_pr_tree(repo, head, context, api):
    """Return the tree SHA that a successful same-repository PR Checks run vouched for.

    The marker artifact name is only a search key. What is trusted is the run
    the API attributes it to, together with the tree computed from this
    checkout. Anything short of a clean match returns None (or raises), and
    the caller then runs the full suite.
    """
    repository = context.get("GITHUB_REPOSITORY", "")
    if not context.get("GH_TOKEN") or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return None
    tree = PLAN.git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        return None
    repository_id = context.get("GITHUB_REPOSITORY_ID", "")
    listing = api(f"repos/{repository}/actions/artifacts?name=validated-tree-{tree}&per_page=100")
    for artifact in listing["artifacts"]:
        if artifact["name"] != f"validated-tree-{tree}" or artifact["expired"] is not False:
            continue
        run = api(f"repos/{repository}/actions/runs/{int(artifact['workflow_run']['id'])}")
        base, fork = run["repository"], run["head_repository"]
        if (base["full_name"].lower() == repository.lower()
                and (not repository_id or str(base["id"]) == repository_id)
                and fork["id"] == base["id"]
                and run["path"] == ".github/workflows/checks.yml"
                and run["event"] == "pull_request"
                and run["status"] == "completed"
                and run["conclusion"] == "success"):
            return tree
    return None


def select(repo, event, context, *, released_tag=None, api=gh_api):
    kind = context.get("GITHUB_EVENT_NAME")
    head = context.get("GITHUB_SHA") or "HEAD"
    try:
        if kind == "push" and context.get("GITHUB_REF") == "refs/heads/main":
            # Last-push-only diffs could hide code from a failed/cancelled CI.
            # A released baseline has already passed the full release checks.
            # A newly pushed manual tag may still be awaiting validation.
            # Use the published release, not simply the newest local tag.
            base = released_tag or published_tag(context)
            PLAN.parse_version(base, stable_only=True)
            subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head],
                           check=True, capture_output=True)
        elif kind == "pull_request":
            base = event["pull_request"]["base"]["sha"]
            if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
                raise ValueError("invalid PR base")
            base = PLAN.git(repo, "merge-base", base, head).strip()
        else:
            # Tags, manual runs and reusable release validation stay full.
            return True, "explicit-full-validation"
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            check=True, capture_output=True, text=True,
        )
        paths = [path for path in result.stdout.split("\0") if path]
        if paths and all(documentation_path(path) for path in paths):
            return False, "documentation-only"
        if kind in ("push", "pull_request"):
            # Only a change that would otherwise run everything asks whether
            # a PR already ran this exact tree; any doubt keeps the full run.
            # Rebasing queued heads can change commit identity without changing
            # the integration's bytes. Use the same proven tree on PR and main.
            try:
                tree = validated_pr_tree(repo, head, context, api)
            except Exception:
                tree = None
            if tree:
                return False, f"validated-pr-tree:{tree}"
        return True, "runtime-test-or-unclassified-change"
    except (PLAN.PlanError, subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        return True, "comparison-unavailable"


def main():
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        full, reason = select(ROOT, event, os.environ)
    except (OSError, KeyError, ValueError):
        full, reason = True, "event-unavailable"
    print(f"full_tests={str(full).lower()}")
    print(f"reason={reason}")


if __name__ == "__main__":
    main()
