#!/usr/bin/env python3
"""Generate exact-worktree dispatch evidence for a prospective owner.

This is the checked pre-owner surface.  In particular, a Codex parent is
probed with its prospective standard-owner network and registry contract; a
caller must not reproduce that context by hand.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
NESTED_SPEC = importlib.util.spec_from_file_location(
    "hearting_nested_dispatch_eligibility",
    ROOT / "utilities" / "nested-dispatch-eligibility.py",
)
assert NESTED_SPEC and NESTED_SPEC.loader
NESTED = importlib.util.module_from_spec(NESTED_SPEC)
NESTED_SPEC.loader.exec_module(NESTED)


class ReadinessError(RuntimeError):
    """The pre-owner readiness request is malformed or unsafe."""


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def generate(
    *,
    worktree: Path,
    jobs: Path,
    owner_harnesses: list[str],
    child_harnesses: list[str],
    disabled_harnesses: set[str] | None = None,
) -> dict[str, Any]:
    if os.environ.get("AGENT_DISPATCH_DEPTH", "0") != "0":
        raise ReadinessError("dispatch-readiness-inside-dispatch")
    worktree = worktree.expanduser().resolve()
    jobs = jobs.expanduser()
    if not jobs.is_absolute():
        raise ReadinessError("owner-registry-path-not-absolute")
    jobs = jobs.resolve()
    parents = _unique(owner_harnesses)
    children = _unique(child_harnesses)
    if not parents or not children:
        raise ReadinessError("dispatch-readiness-tuples-empty")
    disabled = disabled_harnesses or set()
    unknown = (set(parents) | set(children) | disabled) - {
        "claude", "codex", "opencode"
    }
    if unknown:
        raise ReadinessError("dispatch-readiness-harness-unknown")

    rows: list[dict[str, Any]] = []
    for parent in parents:
        parent_sandbox = NESTED.WRAPPER_PARENT_SANDBOXES[parent][0]
        for child in children:
            args = argparse.Namespace(
                parent_harness=parent,
                parent_transport="headless",
                parent_sandbox=parent_sandbox,
                child_harness=child,
                launch_authority="conductor",
                worktree=str(worktree),
                jobs=str(jobs),
                user_disabled=child in disabled,
                prospective_standard_owner=parent == "codex",
            )
            rows.append(NESTED.evaluate(args))
    return {
        "tuples": rows,
        "native_subagent": [],
        "candidates": _quick_candidates(rows),
    }


def _quick_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the checked tuples into `capability-route.py compile
    --registered-headless-evidence` candidates.

    The two consumers grew apart (observed 2026-08-21, memory 6b227d): compile
    validates `{"candidates": [{harness, transport, surface, status,
    probe_source, probe_time}]}` while this tool emitted only the
    dispatch-depth-2 `tuples` shape, so the documented direct/quick path —
    dispatch-readiness → compile — always failed `quick-headless-unavailable`.
    A candidate names the harness of the prospective registered-headless
    worker (the tuple's `child_harness`); one row per harness, a supported
    probe outranking an unsupported/unknown one."""
    projected: dict[str, dict[str, Any]] = {}
    for row in rows:
        harness = row.get("child_harness")
        if not harness:
            continue
        status = row.get("status")
        if status not in ("supported", "unsupported"):
            status = "unknown"
        candidate = {
            "harness": harness,
            "transport": "headless",
            "surface": "registered-headless",
            "status": status,
            "probe_source": row.get("probe_source") or "dispatch-readiness",
            "probe_time": row.get("probe_time") or "",
        }
        held = projected.get(harness)
        if held is None or (held["status"] != "supported" and status == "supported"):
            projected[harness] = candidate
    return sorted(projected.values(), key=lambda row: row["harness"])


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser()
    if not path.is_absolute():
        raise ReadinessError("dispatch-readiness-output-not-absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--owner-harness", action="append", required=True)
    parser.add_argument("--child-harness", action="append", required=True)
    parser.add_argument("--disable-harness", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = generate(
            worktree=args.worktree,
            jobs=args.jobs,
            owner_harnesses=args.owner_harness,
            child_harnesses=args.child_harness,
            disabled_harnesses=set(args.disable_harness),
        )
        _atomic_json(args.output, evidence)
    except (OSError, ReadinessError) as exc:
        print(f"dispatch-readiness: {exc}", file=sys.stderr)
        return 69
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
