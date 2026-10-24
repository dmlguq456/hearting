#!/usr/bin/env python3
"""Bind release authority to a trusted Checks result or local validation.

Read only GitHub's event file and runner context; never fetch artifacts, poll
another workflow, or execute candidate code to decide whether it is trusted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping


class Rejected(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Rejected(reason)


def _sha(value: object) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
             "release commit must be a full SHA")
    return str(value)


def trusted_checks(api, repository: str, repository_id: str, head: str) -> bool:
    """Reuse only a successful Checks run for this exact main commit."""
    try:
        result = api(
            "actions/workflows/checks.yml/runs"
            f"?head_sha={head}&branch=main&event=push&per_page=20"
        )
        runs = result.get("workflow_runs") if isinstance(result, dict) else None
        if not isinstance(runs, list):
            return False
        for run in runs:
            if not isinstance(run, dict):
                continue
            repo = run.get("repository")
            head_repo = run.get("head_repository")
            commit = run.get("head_commit")
            if (run.get("name") == "Checks"
                    and run.get("path") == ".github/workflows/checks.yml"
                    and run.get("event") == "push"
                    and run.get("head_branch") == "main"
                    and run.get("status") == "completed"
                    and run.get("conclusion") == "success"
                    and run.get("head_sha") == head
                    and isinstance(commit, dict) and commit.get("id") == head
                    and isinstance(repo, dict) and repo.get("full_name") == repository
                    and isinstance(head_repo, dict)
                    and head_repo.get("full_name") == repository
                    and str(repo.get("id")) == repository_id
                    and str(head_repo.get("id")) == repository_id):
                return True
    except Exception:
        # An unavailable Actions API must not make a valid request unavailable.
        return False
    return False


def admit(event: dict, context: Mapping[str, str], *, api=None) -> dict[str, str]:
    repository = context.get("GITHUB_REPOSITORY")
    repository_id = context.get("GITHUB_REPOSITORY_ID")
    _require(bool(repository) and bool(repository_id), "missing repository identity")

    def trusted(value: object) -> bool:
        return (isinstance(value, dict) and value.get("full_name") == repository
                and str(value.get("id")) == repository_id)

    _require(trusted(event.get("repository")), "foreign event repository")
    kind = context.get("GITHUB_EVENT_NAME")
    _require(kind != "workflow_run", "workflow_run cannot request a release")

    # These paths validate within Release through the same reusable Checks.
    # A manual feature branch is not automatic release authority.
    ref = context.get("GITHUB_REF", "")
    version_tag = ref.startswith("refs/tags/v")
    _require((kind == "push" and version_tag)
             or (kind == "workflow_dispatch"
                 and (ref == "refs/heads/main" or version_tag)),
             "release requires a version tag or manual main invocation")
    head = _sha(context.get("GITHUB_SHA"))
    if kind == "push":
        _require(event.get("deleted") is False and event.get("ref") == ref,
                 "tag push identity mismatch")
        # `after` may identify an annotated tag object. The runner's commit
        # SHA is the validation input; Release also checks the peeled tag.
    reuse = (kind == "workflow_dispatch" and ref == "refs/heads/main"
             and api is not None and trusted_checks(api, repository, repository_id, head))
    return {"head": head, "validation_required": "false" if reuse else "true"}


def main() -> int:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        repository = os.environ["GITHUB_REPOSITORY"]
        def api(endpoint):
            result = subprocess.run(
                ["gh", "api", f"repos/{repository}/{endpoint}"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Actions API failed")
            return json.loads(result.stdout)
        result = admit(
            event, os.environ, api=api,
        )
    except (Rejected, KeyError, OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"Release denied: {exc}", file=sys.stderr)
        return 1
    for name, value in result.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
