#!/usr/bin/env python3
"""Tier C stage-boundary write-scope audit (C-2b, Step 3.6). stdlib-only.

`snapshot` records a sealed {relpath: {digest,size,mtime_ns,mode}} map of a
cycle subtree. `compare` diffs two such snapshots against a route's node
write_scope and reports any relpath outside scope: new files, digest
changes, mode changes, and deletions (deletion is judged, not silently
passed — the plan's "created-then-deleted" gap is a documented residual
limitation, not something this script claims to close).

Canary-only: no PostToolUse wiring exists in settings.json for this script.
It is a code path gated behind HEARTING_ARTIFACT_POSTSCAN=1, invoked only by
callers that opt in explicitly (owner runbook, not automatic).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

SCHEMA = 1
SAMPLE_RATE = 0.05
RETRY_LIMIT = 2
LOCK_TIMEOUT_S = 30

WORKTREE_ONLY = {"source-scoped"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            try:
                st = p.stat()
                digest = _sha256_file(p)
            except OSError:
                continue
            out[rel] = {"digest": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "mode": st.st_mode}
    return out


def _walk_with_reuse(root: Path, previous: dict[str, dict] | None) -> dict[str, dict]:
    """Fast path (owner addendum B): reuse a previous digest only when both
    size and mtime_ns are unchanged. digest is authoritative for any new,
    size-changed, or mtime-changed file; unchanged-metadata files are
    observed only via a 5% resample at compare time (see `_resample`).
    """
    prev = previous or {}
    out: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            try:
                st = p.stat()
            except OSError:
                continue
            prior = prev.get(rel)
            if prior and prior["size"] == st.st_size and prior["mtime_ns"] == st.st_mtime_ns:
                out[rel] = {"digest": prior["digest"], "size": st.st_size, "mtime_ns": st.st_mtime_ns, "mode": st.st_mode}
            else:
                try:
                    digest = _sha256_file(p)
                except OSError:
                    continue
                out[rel] = {"digest": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "mode": st.st_mode}
    return out


def _resample(root: Path, state: dict[str, dict]) -> tuple[dict[str, dict], bool]:
    """Force-rehash a random 5% sample to self-verify the fast path. Returns
    (possibly-corrected state, mismatch_found).
    """
    candidates = list(state.keys())
    if not candidates:
        return state, False
    k = max(1, int(len(candidates) * SAMPLE_RATE))
    sample = random.sample(candidates, min(k, len(candidates)))
    mismatch = False
    corrected = dict(state)
    for rel in sample:
        p = root / rel
        try:
            actual = _sha256_file(p)
        except OSError:
            continue
        if actual != state[rel]["digest"]:
            mismatch = True
            corrected[rel] = {**state[rel], "digest": actual}
    return corrected, mismatch


def _lock_path(artifact_root: Path, route_id: str, node: str) -> Path:
    d = artifact_root / ".runtime" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"postscan-{route_id}-{node}.lock"


@contextlib.contextmanager
def _flock(path: Path, timeout_s: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.monotonic() + timeout_s
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(0.2)
        if not acquired:
            raise SystemExit("postscan-lock-timeout")
        yield
    finally:
        if acquired:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def cmd_snapshot(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    with _flock(_lock_path(Path(args.artifact_root).resolve(), args.route_id, args.node), LOCK_TIMEOUT_S):
        previous = None
        out_path = Path(args.out)
        if out_path.exists():
            try:
                previous = json.loads(out_path.read_text(encoding="utf-8")).get("entries")
            except Exception:
                previous = None
        entries = _walk_with_reuse(root, previous)
        payload = {
            "route_id": args.route_id,
            "route_node": args.node,
            "attempt_id": args.attempt_id,
            "artifact_root": str(Path(args.artifact_root).resolve()),
            "taken_at_ns": time.time_ns(),
            "schema": SCHEMA,
            "entries": entries,
        }
        out_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return 0


def _patterns(scope: str) -> list[str]:
    if scope == "target-artifact":
        return ["^documents/*/*", "^research/*/*"]
    root = scope[:-3] if scope.endswith("/**") else scope
    if scope in WORKTREE_ONLY or root == "source" or root.startswith("source/"):
        return []
    import re

    return [re.sub(r"<[a-z_]+>", "*", scope)]


def _component_match(value: str, pattern: str) -> bool:
    values = value.split("/") if value else []
    parts = pattern.split("/") if pattern else []
    recursive = bool(parts and parts[-1] == "**")
    if recursive:
        parts = parts[:-1]
        if len(values) < len(parts):
            return False
    elif len(values) != len(parts):
        return False
    return all(fnmatch.fnmatchcase(vp, pp) for vp, pp in zip(values, parts))


def _bound(rel: str, pattern: str) -> bool:
    if pattern.startswith("^"):
        return _component_match(rel, pattern[1:])
    segments = rel.split("/")
    return any(_component_match("/".join(segments[i:]), pattern) for i in range(len(segments)))


def _in_scope(rel: str, write_scope: list[str]) -> bool:
    if any(part.startswith(".") for part in rel.split("/")):
        return True
    pats = [pat for scope in write_scope for pat in _patterns(scope)]
    return any(_bound(rel, pat) for pat in pats)


def cmd_compare(args: argparse.Namespace) -> int:
    artifact_root = Path(args.artifact_root).resolve()
    root = Path(args.root).resolve()
    with _flock(_lock_path(artifact_root, args.route_id, args.node), LOCK_TIMEOUT_S):
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        for key in ("route_id", "route_node", "attempt_id", "artifact_root"):
            expected = {"route_id": args.route_id, "route_node": args.node, "attempt_id": args.attempt_id, "artifact_root": str(artifact_root)}[key]
            if state.get(key) != expected:
                print("postscan-state-identity-mismatch", file=sys.stderr)
                return 2

        before = state["entries"]
        unstable: set[str] = set()
        after = None
        for attempt in range(RETRY_LIMIT + 1):
            try:
                after = _walk_with_reuse(root, before)
                break
            except OSError:
                unstable.add("<walk>")
                if attempt == RETRY_LIMIT:
                    print(json.dumps({"error": "postscan-unstable-tree", "relpaths": sorted(unstable)}))
                    return 2
                continue
        assert after is not None
        after, _mismatch = _resample(root, after)

        route = json.loads(Path(args.route).read_text(encoding="utf-8"))
        node_row = next((n for n in route.get("nodes", []) if n["id"] == args.node), None)
        write_scope = node_row["write_scope"] if node_row else []

        violations = []
        all_rel = set(before) | set(after)
        for rel in sorted(all_rel):
            b, a = before.get(rel), after.get(rel)
            if b is None and a is not None:
                change_kind = "new"
            elif b is not None and a is None:
                change_kind = "deleted"
            elif b["digest"] != a["digest"]:
                change_kind = "digest-changed"
            elif b["mode"] != a["mode"]:
                change_kind = "mode-changed"
            else:
                continue
            if not _in_scope(rel, write_scope):
                violations.append({"relpath": rel, "change_kind": change_kind, "route_id": args.route_id, "route_node": args.node, "attempt_id": args.attempt_id})

        if violations:
            print(json.dumps(violations, sort_keys=True))
            return 2
        print(json.dumps({"violations": []}))
        return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--root", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--route-id", required=True)
    snap.add_argument("--node", required=True)
    snap.add_argument("--attempt-id", required=True)
    snap.add_argument("--artifact-root", required=True)

    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--root", required=True)
    cmp_.add_argument("--state", required=True)
    cmp_.add_argument("--route", required=True)
    cmp_.add_argument("--route-id", required=True)
    cmp_.add_argument("--node", required=True)
    cmp_.add_argument("--attempt-id", required=True)
    cmp_.add_argument("--artifact-root", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    return cmd_compare(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
