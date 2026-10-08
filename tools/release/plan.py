#!/usr/bin/env python3
"""Deterministically decide whether HEAD needs a SemVer release."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


STABLE_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
BREAKING_SUBJECT_RE = re.compile(r"^[A-Za-z]+(?:\([^)]*\))?!:")
FEATURE_SUBJECT_RE = re.compile(r"^feat(?:\([^)]*\))?:", re.IGNORECASE)
BREAKING_BODY_RE = re.compile(r"^BREAKING(?: |-)CHANGE:\s*\S", re.MULTILINE)

NON_RELEASE_EXACT = {
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    "INSTALL_LAYOUT.md",
    "LICENSE",
    "MANUAL.md",
    "README.md",
    "README.ko.md",
    "RELEASE_POLICY.md",
}
NON_RELEASE_PREFIXES = (
    ".agent_reports/",
    ".claude_reports/",
    ".github/",
    "docs/",
    "research/",
)
TEST_PARTS = {"fixture", "fixtures", "test", "tests"}
TEST_NAME_RE = re.compile(
    r"(?:^test_|_test\.|\.test\.|-test\.)(?:py|sh|js|jsx|ts|tsx)$",
    re.IGNORECASE,
)
SEVERITY = {"none": 0, "patch": 1, "minor": 2, "major": 3}


class PlanError(RuntimeError):
    pass


class GitHubError(PlanError):
    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.status = status


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PlanError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def parse_version(value: str, *, stable_only: bool = False) -> tuple[int, int, int]:
    match = (STABLE_RE if stable_only else VERSION_RE).fullmatch(value)
    if not match:
        kind = "stable SemVer" if stable_only else "SemVer"
        raise PlanError(f"release tag must be {kind} with a v prefix: {value}")
    if not stable_only:
        prerelease = match.group(4)
        if prerelease:
            for identifier in prerelease.split("."):
                if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                    raise PlanError(f"numeric prerelease identifiers cannot have leading zeroes: {value}")
    return tuple(int(match.group(i)) for i in range(1, 4))


def is_release_relevant(path: str) -> bool:
    normalized = path.strip("/")
    if not normalized or normalized in NON_RELEASE_EXACT:
        return False
    if any(normalized.startswith(prefix) for prefix in NON_RELEASE_PREFIXES):
        return False
    parts = set(Path(normalized).parts)
    if parts & TEST_PARTS or TEST_NAME_RE.search(Path(normalized).name):
        return False
    return True


def latest_stable_tag(repo: Path, head: str) -> tuple[str, tuple[int, int, int]]:
    tags = git(repo, "tag", "--merged", head, "--list", "v*").splitlines()
    parsed = []
    for tag in tags:
        try:
            parsed.append((parse_version(tag, stable_only=True), tag))
        except PlanError:
            continue
    if not parsed:
        raise PlanError("no stable vMAJOR.MINOR.PATCH tag is reachable from HEAD")
    version, tag = max(parsed)
    return tag, version


def changed_paths(repo: Path, older: str, newer: str) -> list[str]:
    return sorted(
        set(
            line
            for line in git(repo, "diff", "--name-only", f"{older}..{newer}").splitlines()
            if line
        )
    )


def commit_paths(repo: Path, commit: str) -> list[str]:
    return [
        line
        for line in git(
            repo, "diff-tree", "-m", "--no-commit-id", "--name-only", "-r", commit
        ).splitlines()
        if line
    ]


def bump(version: tuple[int, int, int], level: str) -> str:
    major, minor, patch = version
    if level == "major":
        return f"v{major + 1}.0.0"
    if level == "minor":
        return f"v{major}.{minor + 1}.0"
    if level == "patch":
        return f"v{major}.{minor}.{patch + 1}"
    raise PlanError(f"cannot bump level: {level}")


def plan(repo: Path, head: str, base_tag: str | None = None) -> dict:
    head_sha = git(repo, "rev-parse", "--verify", f"{head}^{{commit}}").strip()
    if base_tag:
        base_version = parse_version(base_tag, stable_only=True)
        git(repo, "rev-parse", "--verify", f"{base_tag}^{{commit}}")
    else:
        base_tag, base_version = latest_stable_tag(repo, head_sha)
    if subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base_tag, head_sha]
    ).returncode:
        raise PlanError(f"base tag is not an ancestor of HEAD: {base_tag}")

    paths = changed_paths(repo, base_tag, head_sha)
    relevant = [path for path in paths if is_release_relevant(path)]
    if not relevant:
        return {
            "release": False,
            "base_tag": base_tag,
            "head": head_sha,
            "bump": "none",
            "version": "",
            "reason": "no-release-relevant-changes",
            "paths": [],
        }

    level = "patch"
    reason = "release-relevant-change"
    commits = git(repo, "rev-list", "--reverse", f"{base_tag}..{head_sha}").splitlines()
    for commit in commits:
        if not any(is_release_relevant(path) for path in commit_paths(repo, commit)):
            continue
        message = git(repo, "show", "-s", "--format=%s%n%b", commit)
        subject = message.splitlines()[0] if message.splitlines() else ""
        candidate = "patch"
        if BREAKING_SUBJECT_RE.search(subject) or BREAKING_BODY_RE.search(message):
            candidate = "major"
        elif FEATURE_SUBJECT_RE.search(subject):
            candidate = "minor"
        if SEVERITY[candidate] > SEVERITY[level]:
            level = candidate
            reason = f"{candidate}:{commit[:12]}:{subject[:80]}"

    return {
        "release": True,
        "base_tag": base_tag,
        "head": head_sha,
        "bump": level,
        "version": bump(base_version, level),
        "reason": reason,
        "paths": relevant,
    }


def github_api(repository: str, endpoint: str, *, method: str = "GET", **fields):
    command = ["gh", "api", "--method", method, f"repos/{repository}/{endpoint}"]
    for key, value in fields.items():
        command.extend(("-f", f"{key}={value}"))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        match = re.search(r"HTTP (\d{3})", result.stderr)
        status = int(match[1]) if match else None
        if method == "GET" and status == 404:
            return None
        raise GitHubError(result.stderr.strip() or "GitHub API failed", status)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise GitHubError("GitHub API returned invalid JSON") from exc


def tag_commit(api, version: str) -> str | None:
    reference = api(f"git/ref/tags/{version}")
    if reference is None:
        return None
    obj = reference["object"]
    for _ in range(8):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api(f"git/tags/{obj['sha']}")["object"]
    raise PlanError(f"release tag does not resolve to a commit: {version}")


def stable_tags(repo: Path, *selection: str) -> list[str]:
    tags = git(repo, "tag", *selection, "--list", "v*").splitlines()
    return sorted((tag for tag in tags if STABLE_RE.fullmatch(tag)),
                  key=lambda tag: parse_version(tag, stable_only=True), reverse=True)


def prepare(repo: Path, head: str, *, api, version: str | None = None) -> dict:
    """Refresh and reserve before assets embed the version; never move a tag."""
    head = git(repo, "rev-parse", "--verify", f"{head}^{{commit}}").strip()
    if version:
        parse_version(version)
        if tag_commit(api, version) != head:
            raise PlanError(f"selected tag does not match the tested commit: {version}")
        release = api(f"releases/tags/{version}")
        return dict(release=release is None, base_tag="", head=head, bump="manual",
                    version=version, reason="validated-tag" if release is None else "already-published",
                    mode="tag")

    for _ in range(8):
        # Checkout and earlier plans may predate another publisher's tag.
        git(repo, "fetch", "--tags", "origin")
        tags = stable_tags(repo)
        resume_tag = None
        for tag in stable_tags(repo, "--contains", head):
            release = api(f"releases/tags/{tag}")
            if release is not None and not release.get("draft") and not release.get("prerelease"):
                return dict(release=False, base_tag=tag, head=head, bump="none",
                            version="", reason="already-published", mode="auto")
            if resume_tag is None and git(repo, "rev-parse", f"{tag}^{{commit}}").strip() == head:
                resume_tag = tag
        if resume_tag is not None:
            # An orphan reservation cannot hide a published containing commit
            # with a lower version. Check all publications before resuming.
            return dict(release=True, base_tag=resume_tag, head=head, bump="none",
                        version=resume_tag, reason="resume-existing-tag", mode="auto")

        value = dict(plan(repo, head), mode="auto")
        if not value["release"]:
            return value
        if tags:
            floor = parse_version(tags[0], stable_only=True)
            if parse_version(value["version"], stable_only=True) <= floor:
                value["version"] = bump(floor, value["bump"])
        version = value["version"]
        existing = tag_commit(api, version)
        if existing == head:
            release = api(f"releases/tags/{version}")
            value["release"] = release is None
            value["reason"] = "resume-existing-tag" if release is None else "already-published"
            return value
        if existing is not None:
            # Re-read publication state and the version floor after a collision.
            continue
        obj = api("git/tags", method="POST", tag=version,
                  message=f"Hearting {version}", object=head, type="commit")
        try:
            api("git/refs", method="POST", ref=f"refs/tags/{version}", sha=obj["sha"])
        except GitHubError as exc:
            # Only an observed tag collision is retryable. Auth/network errors
            # remain failures, and no existing ref or asset is overwritten.
            if exc.status != 422 or tag_commit(api, version) is None:
                raise
            continue
        return value
    raise PlanError("release tags kept changing during version reservation")


def emit(value: dict, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    for key in ("release", "base_tag", "head", "bump", "version", "reason"):
        item = value[key]
        if isinstance(item, bool):
            item = str(item).lower()
        print(f"{key}={item}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--repo", default=".")
    plan_parser.add_argument("--head", default="HEAD")
    plan_parser.add_argument("--base-tag")
    plan_parser.add_argument("--format", choices=("json", "github"), default="json")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--repo", default=".")
    prepare_parser.add_argument("--head", default="HEAD")
    prepare_parser.add_argument("--format", choices=("json", "github"), default="json")
    validate_parser = sub.add_parser("validate-version")
    validate_parser.add_argument("version")
    args = parser.parse_args()
    try:
        if args.command == "validate-version":
            parse_version(args.version)
            print(args.version)
        elif args.command == "prepare":
            repository = os.environ["GITHUB_REPOSITORY"]
            version = os.environ.get("REF_NAME") if os.environ.get("REF_TYPE") == "tag" else None
            value = prepare(Path(args.repo).resolve(), args.head,
                            api=lambda endpoint, **fields: github_api(repository, endpoint, **fields),
                            version=version)
            emit(value, args.format)
            if args.format == "github":
                print(f"mode={value['mode']}")
        else:
            emit(plan(Path(args.repo).resolve(), args.head, args.base_tag), args.format)
        return 0
    except PlanError as exc:
        print(f"release-plan: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
