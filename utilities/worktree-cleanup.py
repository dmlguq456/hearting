#!/usr/bin/env python3
"""Fail-closed cleanup for merged, pushed, inactive linked worktrees."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (
    DispatchContractError,
    dispatch_state_root,
    parse_registry_metadata,
    resolve_agent_home as _resolve_agent_home,
    resolve_dispatch_state_root,
    validate_attempt_metadata,
)

OPEN_STATES = {"open", "running"}
MAX_AUDIT_BYTES = 1024 * 1024
KEEP_AUDIT_BYTES = 512 * 1024


@dataclass
class WorktreeEntry:
    path: Path
    head: str = ""
    branch: str = ""
    locked: bool = False


@dataclass
class Verdict:
    path: Path
    primary: Path
    integration_ref: str
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)
    head: str = ""
    branch: str = ""
    upstream: str = "none"
    stale_rows: int = 0
    active_pids: list[int] = field(default_factory=list)
    process_scan: str = "unknown"
    repository_mode: str = "remote"


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(argv)}: {detail}")
    return result


def normalize(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def resolve_agent_home() -> Path:
    return _resolve_agent_home()


def worktree_entries(repo: Path) -> list[WorktreeEntry]:
    output = run(["git", "-C", str(repo), "worktree", "list", "--porcelain"]).stdout
    entries: list[WorktreeEntry] = []
    current: WorktreeEntry | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                entries.append(current)
            current = WorktreeEntry(normalize(line[len("worktree ") :]))
        elif current is not None and line.startswith("HEAD "):
            current.head = line[len("HEAD ") :]
        elif current is not None and line.startswith("branch "):
            current.branch = line[len("branch ") :]
        elif current is not None and line.startswith("locked"):
            current.locked = True
    if current is not None:
        entries.append(current)
    return entries


def common_git_dir(repo: Path) -> Path:
    return git_path(repo, "--git-common-dir")


def git_path(repo: Path, field: str) -> Path:
    value = run(["git", "-C", str(repo), "rev-parse", field]).stdout.strip()
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return normalize(path)


def default_integration_ref(primary: Path) -> str:
    symbolic = run(
        ["git", "-C", str(primary), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if symbolic.returncode == 0 and symbolic.stdout.strip().startswith("origin/"):
        candidate = symbolic.stdout.strip().split("/", 1)[1]
        if run(
            ["git", "-C", str(primary), "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            check=False,
        ).returncode == 0:
            return candidate
    for candidate in ("main", "master", "develop", "trunk"):
        if run(
            ["git", "-C", str(primary), "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            check=False,
        ).returncode == 0:
            return candidate
    raise RuntimeError("no local integration branch; pass --integration-ref")


def git_operation(worktree: Path) -> str:
    gitdir = git_path(worktree, "--git-dir")
    if (gitdir / "MERGE_HEAD").exists():
        return "merge"
    if (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists():
        return "rebase"
    if (gitdir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return "none"


def parse_registry(jobs: Path, target: Path) -> tuple[int, list[int]]:
    stale = 0
    active: list[int] = []
    if not jobs.is_file():
        return stale, active
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6 or fields[1] not in OPEN_STATES:
            continue
        if normalize(fields[3]) != target:
            continue
        match = re.search(r"(?:^|,)pid=([0-9]+)(?:,|$)", fields[5])
        start = re.search(r"(?:^|,)pid_start=([0-9]+)(?:,|$)", fields[5])
        if match and pid_is_live(int(match.group(1)), start.group(1) if start else None):
            active.append(int(match.group(1)))
        else:
            stale += 1
    return stale, sorted(set(active))


def pid_is_live(pid: int, pid_start: str | None = None) -> bool:
    """(pid, start-time) identity when the registry recorded `pid_start`: a live
    pid whose /proc starttime differs is a host PID reuse, not this attempt.
    Bare-pid probing survives only for legacy rows that never captured it."""
    if pid_start is not None:
        return read_proc_start(pid) == pid_start
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_proc_start(pid: int) -> str | None:
    """/proc/<pid>/stat field 22 (starttime) as str, else None. comm (field 2) is
    parenthesized and may contain spaces/parens, so split after the last ')';
    zombies are terminal and count as gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    try:
        rest = data[data.rindex(")") + 1:].split()
        if rest[0] == "Z":
            return None
        return rest[19]
    except (ValueError, IndexError):
        return None


def process_cwds(target: Path) -> tuple[str, list[int]]:
    proc = Path("/proc")
    matches: list[int] = []
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                cwd = normalize(os.readlink(entry / "cwd"))
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if cwd == target or target in cwd.parents:
                matches.append(pid)
        return "procfs", sorted(set(matches))

    lsof = shutil.which("lsof")
    if lsof:
        result = run([lsof, "-t", "+D", str(target)], check=False)
        if result.returncode not in (0, 1):
            return "lsof-error", []
        for value in result.stdout.splitlines():
            if value.isdigit() and int(value) != os.getpid():
                matches.append(int(value))
        return "lsof", sorted(set(matches))
    return "unavailable", []


def evaluate(
    target: Path,
    jobs: Path,
    integration_ref: str | None,
    repository_mode: str = "remote",
) -> Verdict:
    entries = worktree_entries(target)
    if not entries:
        raise RuntimeError("git reported no registered worktrees")
    primary = entries[0].path
    chosen_ref = integration_ref or default_integration_ref(primary)
    verdict = Verdict(
        path=target,
        primary=primary,
        integration_ref=chosen_ref,
        repository_mode=repository_mode,
    )

    by_path = {entry.path: entry for entry in entries}
    entry = by_path.get(target)
    if entry is None:
        verdict.reasons.append("not-registered-worktree")
        return verdict
    verdict.head = entry.head
    branch_prefix = "refs/heads/"
    verdict.branch = (
        entry.branch[len(branch_prefix) :]
        if entry.branch.startswith(branch_prefix)
        else entry.branch
    ) or "detached"

    if target == primary:
        verdict.reasons.append("primary-worktree")
    worktree_gitdir = git_path(target, "--git-dir")
    if entry.locked or (worktree_gitdir / "locked").exists():
        verdict.reasons.append("locked")
    if not target.is_dir():
        verdict.reasons.append("worktree-path-missing")
        return verdict

    dirty = run(
        ["git", "-C", str(target), "status", "--porcelain=v1", "--untracked-files=all"]
    ).stdout
    if dirty.strip():
        verdict.reasons.append("dirty")

    operation = git_operation(target)
    if operation != "none":
        verdict.reasons.append(f"git-operation-{operation}")

    ref_check = run(
        ["git", "-C", str(primary), "rev-parse", "--verify", f"{chosen_ref}^{{commit}}"],
        check=False,
    )
    if ref_check.returncode != 0:
        verdict.reasons.append("integration-ref-missing")
    else:
        merged = run(
            ["git", "-C", str(primary), "merge-base", "--is-ancestor", entry.head, chosen_ref],
            check=False,
        )
        if merged.returncode != 0:
            verdict.reasons.append("unmerged")

        upstream_result = run(
            [
                "git",
                "-C",
                str(primary),
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                f"{chosen_ref}@{{upstream}}",
            ],
            check=False,
        )
        if repository_mode == "local-only":
            verdict.upstream = "local-only"
        elif upstream_result.returncode != 0 or not upstream_result.stdout.strip():
            verdict.reasons.append("no-upstream-configured")
        else:
            verdict.upstream = upstream_result.stdout.strip()
            counts = run(
                [
                    "git",
                    "-C",
                    str(primary),
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{verdict.upstream}...{chosen_ref}",
                ]
            ).stdout.split()
            if len(counts) != 2 or counts != ["0", "0"]:
                verdict.reasons.append("integration-not-pushed")

    verdict.stale_rows, registered_pids = parse_registry(jobs, target)
    scan_kind, cwd_pids = process_cwds(target)
    verdict.process_scan = scan_kind
    verdict.active_pids = sorted(set(registered_pids + cwd_pids))
    if verdict.active_pids:
        verdict.reasons.append("active-process")
    if scan_kind in {"unavailable", "lsof-error"}:
        verdict.reasons.append("process-scan-unavailable")

    verdict.eligible = not verdict.reasons
    return verdict


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reconcile_registry(jobs: Path, target: Path) -> int:
    if not jobs.is_file():
        return 0
    lock = Path(f"{jobs}.lock")
    changed = 0
    with file_lock(lock):
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        rewritten: list[str] = []
        for line in lines:
            fields = line.rstrip("\n").split("\t")
            current_attempt = False
            if len(fields) == 6:
                try:
                    validate_attempt_metadata(parse_registry_metadata(fields[5]))
                    current_attempt = True
                except DispatchContractError:
                    current_attempt = False
            if (
                len(fields) == 6
                and fields[1] in OPEN_STATES
                and current_attempt
                and normalize(fields[3]) == target
            ):
                fields[1] = "done"
                if not re.search(r"(?:^|,)note=cleanup-merged(?:,|$)", fields[5]):
                    fields[5] += ",note=cleanup-merged"
                line = "\t".join(fields) + "\n"
                changed += 1
            rewritten.append(line)
        if changed:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(jobs.parent), delete=False
            ) as handle:
                handle.writelines(rewritten)
                temp_name = handle.name
            Path(temp_name).replace(jobs)
    return changed


def append_audit(audit: Path, record: dict[str, object]) -> None:
    lock = Path(f"{audit}.lock")
    with file_lock(lock):
        audit.parent.mkdir(parents=True, exist_ok=True)
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


def emit(verdict: Verdict, status: str, reconciled: int = 0) -> None:
    print(f"status={status}")
    print(f"worktree={verdict.path}")
    print(f"primary_worktree={verdict.primary}")
    print(f"integration_ref={verdict.integration_ref}")
    print(f"integration_upstream={verdict.upstream}")
    print(f"repository_mode={verdict.repository_mode}")
    print(f"head={verdict.head or '-'}")
    print(f"branch={verdict.branch or '-'}")
    print(f"process_scan={verdict.process_scan}")
    print(
        "active_pids="
        + (",".join(str(pid) for pid in verdict.active_pids) if verdict.active_pids else "-")
    )
    print(f"stale_registry_rows={verdict.stale_rows}")
    print(f"registry_reconciled={reconciled}")
    print("branch_deleted=0")
    print("artifact_harvest_required=0")
    print("reasons=" + (",".join(verdict.reasons) if verdict.reasons else "-"))


def registered_job_worktrees(jobs: Path, repo: Path) -> list[Path]:
    if not jobs.is_file():
        return []
    expected_common = common_git_dir(repo)
    found: list[Path] = []
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        try:
            metadata = parse_registry_metadata(fields[5])
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            continue
        if not metadata.get("attempt_id"):
            continue
        candidate = normalize(fields[3])
        if candidate in found or not candidate.is_dir():
            continue
        result = run(
            ["git", "-C", str(candidate), "rev-parse", "--git-common-dir"],
            check=False,
        )
        if result.returncode == 0:
            candidate_common = Path(result.stdout.strip())
            if not candidate_common.is_absolute():
                candidate_common = candidate / candidate_common
            candidate_common = normalize(candidate_common)
        else:
            candidate_common = Path()
        if result.returncode == 0 and candidate_common == expected_common:
            found.append(candidate)
    return found


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    action = p.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="evaluate only (default)")
    action.add_argument("--apply", action="store_true", help="remove eligible worktrees")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--worktree")
    target.add_argument(
        "--all-eligible",
        action="store_true",
        help="evaluate worktrees referenced by this repo's jobs registry",
    )
    p.add_argument("--repo", default=os.getcwd(), help="repo used with --all-eligible")
    p.add_argument("--integration-ref")
    p.add_argument(
        "--repository-mode",
        choices=("remote", "local-only"),
        default="remote",
        help="explicitly skip only the push-sync gate for a local-only repository",
    )
    p.add_argument("--jobs")
    p.add_argument("--audit")
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv[1:])
    apply = bool(args.apply)
    agent_home = resolve_agent_home()
    jobs = normalize(
        resolve_dispatch_state_root(agent_home, explicit_jobs=args.jobs) / "jobs.log"
    )
    audit = normalize(args.audit or dispatch_state_root(jobs) / "worktree-cleanup.jsonl")

    if args.all_eligible:
        targets = registered_job_worktrees(jobs, normalize(args.repo))
        if not targets:
            print("status=no-candidates")
            print("candidate_source=job-registry")
            return 0
    else:
        targets = [normalize(args.worktree)]

    blocked = 0
    for index, target in enumerate(targets):
        if index:
            print()
        try:
            verdict = evaluate(
                target, jobs, args.integration_ref, args.repository_mode
            )
        except (OSError, RuntimeError) as e:
            print("status=blocked")
            print(f"worktree={target}")
            print("reasons=evidence-error")
            print(f"detail={e}")
            blocked += 1
            continue

        if not verdict.eligible:
            emit(verdict, "blocked")
            blocked += 1
            continue
        if not apply:
            emit(verdict, "eligible")
            continue

        result = run(
            ["git", "-C", str(verdict.primary), "worktree", "remove", str(target)],
            check=False,
        )
        if result.returncode != 0:
            verdict.reasons.append("remove-failed")
            emit(verdict, "blocked")
            if result.stderr.strip():
                print(f"detail={result.stderr.strip()}")
            blocked += 1
            continue
        run(["git", "-C", str(verdict.primary), "worktree", "prune"])
        reconciled = reconcile_registry(jobs, target)
        record = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": "removed",
            "worktree": str(target),
            "primary_worktree": str(verdict.primary),
            "integration_ref": verdict.integration_ref,
            "integration_upstream": verdict.upstream,
            "repository_mode": verdict.repository_mode,
            "head": verdict.head,
            "branch": verdict.branch,
            "branch_deleted": False,
            "registry_reconciled": reconciled,
            "artifact_harvest_required": False,
        }
        append_audit(audit, record)
        emit(verdict, "removed", reconciled)
        print(f"audit_log={audit}")

    return 3 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
