#!/usr/bin/env python3
"""Append-only allocation receipt ledger: one row per realized harness choice.

Until 2026-08-29 the allocation verdict (`allocation_rank`, `capacity_headroom.*`,
strategy, preferred harness) was printed to the conductor's stdout and nowhere
else, so "did the configured policy actually fire?" could not be answered after
the fact — the depth-affinity keys sat inert on a legacy strategy for a day and
the balanced-first decision for two weeks before anyone could tell. This ledger
makes that a one-line query:

    python3 utilities/dispatch_allocation_receipt.py list --since 24h
    python3 utilities/dispatch_allocation_receipt.py summary --since 7d

Rows live under ``<dispatch state root>/allocation/<route_id>.jsonl`` next to
the SD-93 degradation ledger and share its best-effort contract: a write
failure never changes the launch, the exit code, or the stdout receipt. A row
is keyed by ``attempt_id`` so it joins the jobs registry directly.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_allocation import inert_allocation_keys  # noqa: E402
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root as _resolve_dispatch_state_root  # noqa: E402

SCHEMA_VERSION = 1
KIND = "allocation"
LEDGER_DIR = "allocation"
_WRITERS = {"stage-dispatch-fallback.py", "dispatch-batch.py"}
_HARNESSES = ("claude", "codex", "opencode")
_POLICY_KEYS = (
    "strategy", "window", "usage_gate_used_percent",
    "depth_affinity_weight", "usage_headroom_exponent",
)


def _event_id(row: dict) -> str:
    identity = [row.get(k) for k in (
        "route_id", "route_node", "attempt_id", "child_harness",
        "parallel_leg_index", "writer", "action",
    )]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return "al-" + digest[:24]


def ledger_root(*, agent_home=None, jobs=None) -> Path:
    home = Path(agent_home) if agent_home else _resolve_agent_home()
    return _resolve_dispatch_state_root(home, jobs) / LEDGER_DIR


def ledger_path(route_id, *, agent_home=None, jobs=None) -> Path:
    name = route_id + ".jsonl" if isinstance(route_id, str) and route_id else "_unattributed.jsonl"
    return ledger_root(agent_home=agent_home, jobs=jobs) / name


def _policy_fields(allocation) -> dict:
    if not isinstance(allocation, dict):
        return {"strategy": None, "inert_keys": {}}
    fields = {key: allocation.get(key) for key in _POLICY_KEYS}
    affinity = allocation.get("depth_affinity")
    fields["depth_affinity"] = dict(affinity) if isinstance(affinity, dict) else None
    fields["inert_keys"] = inert_allocation_keys(allocation)
    return fields


def _append(path: Path, payload: bytes) -> bool:
    os.makedirs(path.parent, exist_ok=True)
    deadline = time.monotonic() + 0.25
    with open(str(path) + ".lock", "a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    return False
                time.sleep(0.005)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return True


def record_allocation_receipt(
    *,
    route_id,
    route_node,
    writer,
    child_harness,
    route_hash=None,
    dispatch_depth=2,
    action=None,
    attempt_id=None,
    slug=None,
    unit=None,
    fallback_hop=None,
    allocation=None,
    preferred=None,
    rank=None,
    capacity=None,
    counts=None,
    states=None,
    quality_band=None,
    relief_promoted=None,
    parent_cross=None,
    sole_gate=None,
    affinity=None,
    owner_family=None,
    parallel_group=None,
    parallel_leg_index=None,
    parallel_leg_count=None,
    agent_home=None,
    jobs=None,
):
    """Append one receipt row; returns ``{"path", "event_id"}`` or ``None``.

    Every failure is swallowed on purpose (same contract as the degradation
    ledger): the receipt is evidence about a launch, never a gate on it.
    """
    try:
        if writer not in _WRITERS or child_harness not in _HARNESSES:
            return None
        row = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "ts": time.time(),
            "route_id": route_id if isinstance(route_id, str) else None,
            "route_node": route_node if isinstance(route_node, str) else None,
            "route_hash": route_hash if isinstance(route_hash, str) else None,
            "dispatch_depth": int(dispatch_depth),
            "writer": writer,
            "action": action,
            "attempt_id": attempt_id,
            "slug": slug,
            "unit": unit,
            "child_harness": child_harness,
            "fallback_hop": fallback_hop,
            "preferred": preferred,
            "rank": list(rank) if rank else None,
            "capacity": {h: capacity.get(h) for h in _HARNESSES} if isinstance(capacity, dict) else None,
            "counts": {h: int(counts.get(h, 0)) for h in _HARNESSES} if isinstance(counts, dict) else None,
            "states": {h: states.get(h) for h in _HARNESSES} if isinstance(states, dict) else None,
            "quality_band": quality_band,
            "relief_promoted": bool(relief_promoted) if relief_promoted is not None else None,
            "parent_cross": parent_cross,
            "sole_gate": sole_gate,
            "affinity": affinity,
            "owner_family": owner_family,
            "parallel_group": parallel_group,
            "parallel_leg_index": parallel_leg_index,
            "parallel_leg_count": parallel_leg_count,
            **_policy_fields(allocation),
        }
        row["preferred_honored"] = (
            None if preferred is None else bool(preferred == child_harness)
        )
        row["event_id"] = _event_id(row)
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        path = ledger_path(row["route_id"], agent_home=agent_home, jobs=jobs)
        if not _append(path, payload):
            return None
        return {"path": str(path), "event_id": row["event_id"]}
    except BaseException:
        return None


# --- read side -------------------------------------------------------------

def _parse_since(value: str | None) -> float | None:
    if not value:
        return None
    units = {"h": 3600, "d": 86400, "m": 60}
    try:
        if value[-1] in units:
            return time.time() - float(value[:-1]) * units[value[-1]]
        return float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --since {value!r}") from exc


def read_rows(*, route_id=None, since=None, agent_home=None, jobs=None) -> list[dict]:
    root = ledger_root(agent_home=agent_home, jobs=jobs)
    if not root.is_dir():
        return []
    files = [ledger_path(route_id, agent_home=agent_home, jobs=jobs)] if route_id else sorted(root.glob("*.jsonl"))
    rows = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") != KIND:
                continue
            if since is not None and float(row.get("ts") or 0) < since:
                continue
            rows.append(row)
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    return rows


def _fmt_map(values, *, digits=1) -> str:
    if not isinstance(values, dict):
        return "-"
    cells = []
    for name in _HARNESSES:
        value = values.get(name)
        if value is None:
            continue
        cells.append(f"{name}:{round(value, digits) if isinstance(value, float) else value}")
    return "|".join(cells) or "-"


def format_row(row: dict) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(row.get("ts") or 0)))
    inert = ",".join(sorted((row.get("inert_keys") or {}).keys())) or "-"
    honored = row.get("preferred_honored")
    return (
        f"{ts} {row.get('route_id') or '-'} {row.get('route_node') or '-'} "
        f"unit={row.get('unit') or '-'} d={row.get('dispatch_depth')} "
        f"child={row.get('child_harness')} hop={row.get('fallback_hop') or '-'} "
        f"strategy={row.get('strategy') or '-'} preferred={row.get('preferred') or '-'} "
        f"honored={'-' if honored is None else int(honored)} "
        f"rank={'>'.join(row.get('rank') or []) or '-'} "
        f"headroom={_fmt_map(row.get('capacity'))} counts={_fmt_map(row.get('counts'))} "
        f"inert={inert} writer={row.get('writer')} action={row.get('action') or '-'} "
        f"attempt={row.get('attempt_id') or '-'} event={row.get('event_id')}"
    )


def summarize(rows: list[dict]) -> dict:
    by_harness: dict[str, int] = {}
    by_unit: dict[str, dict[str, int]] = {}
    by_strategy: dict[str, int] = {}
    honored = {"yes": 0, "no": 0, "n/a": 0}
    inert_rows = 0
    for row in rows:
        harness = row.get("child_harness") or "-"
        by_harness[harness] = by_harness.get(harness, 0) + 1
        unit = row.get("unit") or "-"
        by_unit.setdefault(unit, {})[harness] = by_unit.setdefault(unit, {}).get(harness, 0) + 1
        strategy = row.get("strategy") or "-"
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        value = row.get("preferred_honored")
        honored["n/a" if value is None else ("yes" if value else "no")] += 1
        if row.get("inert_keys"):
            inert_rows += 1
    return {
        "rows": len(rows),
        "by_harness": by_harness,
        "by_unit": by_unit,
        "by_strategy": by_strategy,
        "preferred_honored": honored,
        "rows_with_inert_keys": inert_rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("operation", choices=("list", "summary", "path"))
    parser.add_argument("--route", help="route id (rt-…); default: every route")
    parser.add_argument("--since", type=_parse_since, help="window such as 24h, 7d, 90m, or an epoch")
    parser.add_argument("--jobs", type=Path, help="explicit jobs registry (state root is derived from it)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.operation == "path":
        print(ledger_root(jobs=args.jobs))
        return 0
    rows = read_rows(route_id=args.route, since=args.since, jobs=args.jobs)
    if args.operation == "list":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True) if args.json else format_row(row))
        return 0
    summary = summarize(rows)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    print(f"rows={summary['rows']}")
    print("by_harness=" + (",".join(f"{k}:{v}" for k, v in sorted(summary["by_harness"].items())) or "-"))
    print("by_strategy=" + (",".join(f"{k}:{v}" for k, v in sorted(summary["by_strategy"].items())) or "-"))
    print("preferred_honored=" + ",".join(f"{k}:{v}" for k, v in summary["preferred_honored"].items()))
    print(f"rows_with_inert_keys={summary['rows_with_inert_keys']}")
    for unit, cells in sorted(summary["by_unit"].items()):
        print(f"unit.{unit}=" + ",".join(f"{k}:{v}" for k, v in sorted(cells.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
