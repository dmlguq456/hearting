#!/usr/bin/env python3
"""Classify and clean build residue inside a worktree (CONVENTIONS §4.3).

Builds run inside a linked worktree can leave untracked artifacts (for
example dependency-tracing stubs) that pollute `git status`, block guarded
worktree cleanup, and previously required manual deletion.  This helper is
the deterministic defense layer: it identifies untracked files matching an
explicit residue pattern list and, only on request, removes them.

It deliberately does NOT change guarded-cleanup semantics: `--clean` is an
explicit orchestrator action run before `worktree-cleanup.py`, equivalent to
the manual cleanup it replaces, but pattern-fenced and audit-logged.

Fail-closed rules:
- only untracked, non-ignored files (`git ls-files --others --exclude-standard`)
  are ever candidates — tracked and ignored paths are untouchable;
- zero configured patterns means zero candidates; `--clean` then refuses;
- a matched path is removed only while it stays inside the worktree
  (symlinks are unlinked, never followed; `.git` is never touched);
- every removal is appended to `<agent-home>/.dispatch/build-residue.jsonl`.

Patterns come from repeated `--glob` flags plus an optional
`<worktree>/.agent-build-residue` file (one fnmatch glob per line relative to
the worktree root, `#` comments allowed).  fnmatch `*` crosses directory
boundaries, so `sample-app/node_modules/*` matches the whole stub tree.
"""

from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SOURCE_ROOT = Path(__file__).resolve().parents[1]
_agent_home = Path(os.environ.get("AGENT_HOME", SOURCE_ROOT)).expanduser().resolve(strict=False)
AGENT_HOME = _agent_home if (_agent_home / "core/CORE.md").is_file() else SOURCE_ROOT
sys.path.insert(0, str(SOURCE_ROOT / "utilities"))
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402
DISPATCH_STATE_HOME = resolve_dispatch_state_root(AGENT_HOME)
PATTERN_FILE = ".agent-build-residue"
MAX_AUDIT_BYTES = 512 * 1024
KEEP_AUDIT_BYTES = 256 * 1024


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_audit(audit: Path, record: dict[str, object]) -> None:
    lock = Path(f"{audit}.lock")
    audit.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        previous = b""
        if audit.is_file():
            previous = audit.read_bytes()
            if len(previous) > MAX_AUDIT_BYTES:
                previous = previous[-KEEP_AUDIT_BYTES:]
                newline = previous.find(b"\n")
                previous = previous[newline + 1 :] if newline >= 0 else b""
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
        with tempfile.NamedTemporaryFile("wb", dir=str(audit.parent), delete=False) as handle:
            handle.write(previous)
            handle.write(payload)
            temp_name = handle.name
        Path(temp_name).replace(audit)
        fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def load_patterns(worktree: Path, cli_globs: list[str]) -> list[str]:
    patterns = [glob for glob in cli_globs if glob.strip()]
    pattern_file = worktree / PATTERN_FILE
    if pattern_file.is_file():
        for line in pattern_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def untracked_files(worktree: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    return [part.decode("utf-8", "surrogateescape") for part in result.stdout.split(b"\0") if part]


def matches(relpath: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relpath, pattern) for pattern in patterns)


def inside_worktree(worktree: Path, relpath: str) -> bool:
    if relpath == ".git" or relpath.startswith(".git/"):
        return False
    target = worktree / relpath
    # Never follow a symlinked parent out of the worktree; the leaf itself may
    # be a symlink (it is unlinked, not followed).
    parent_real = target.parent.resolve(strict=False)
    worktree_real = worktree.resolve(strict=False)
    return parent_real == worktree_real or worktree_real in parent_real.parents


def prune_empty_dirs(worktree: Path, relpath: str) -> None:
    current = (worktree / relpath).parent
    worktree_real = worktree.resolve(strict=False)
    while current.resolve(strict=False) != worktree_real:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--glob", action="append", default=[], help="residue fnmatch glob relative to the worktree root")
    parser.add_argument("--clean", action="store_true", help="remove matched residue (default is a dry-run check)")
    parser.add_argument(
        "--audit",
        type=Path,
        default=DISPATCH_STATE_HOME / "build-residue.jsonl",
        help="audit journal path (default: <agent-home>/.dispatch/build-residue.jsonl)",
    )
    args = parser.parse_args()

    worktree = args.worktree.expanduser().resolve(strict=False)
    if not (worktree / ".git").exists():
        print("status=failed")
        print(f"worktree={worktree}")
        print("reason=not-a-git-worktree")
        return 2

    patterns = load_patterns(worktree, args.glob)
    if args.clean and not patterns:
        print("status=failed")
        print(f"worktree={worktree}")
        print("reason=no-residue-patterns (fail-closed: refusing an unfenced clean)")
        return 2

    try:
        candidates = untracked_files(worktree)
    except subprocess.CalledProcessError as exc:
        print("status=failed")
        print(f"worktree={worktree}")
        print(f"reason=git-ls-files: {exc.stderr.decode('utf-8', 'replace').strip()}")
        return 1

    matched = [rel for rel in candidates if matches(rel, patterns)]
    removed: list[str] = []
    skipped: list[str] = []
    if args.clean:
        for rel in matched:
            if not inside_worktree(worktree, rel):
                skipped.append(rel)
                continue
            target = worktree / rel
            try:
                target.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                skipped.append(rel)
                continue
            removed.append(rel)
            prune_empty_dirs(worktree, rel)
        append_audit(
            args.audit,
            {
                "at": utcnow(),
                "action": "clean" if removed or skipped else "clean-noop",
                "worktree": str(worktree),
                "patterns": patterns,
                "removed": removed,
                "skipped_unsafe": skipped,
            },
        )

    print(f"status={'cleaned' if args.clean else 'check'}")
    print(f"worktree={worktree}")
    print(f"patterns={len(patterns)}")
    print(f"untracked={len(candidates)}")
    print(f"matched={len(matched)}")
    print(f"removed={len(removed)}")
    print(f"skipped_unsafe={len(skipped)}")
    for rel in matched:
        print(f"residue={rel}")
    if skipped:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
