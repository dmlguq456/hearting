#!/usr/bin/env python3
"""`experiment-board/v1`: the one summary every screen reads (shell panel, Fleet, card, steward).

This module aggregates `experiment_catalog.status()`; it never decides a name, a state, or whether
two values may be subtracted.  Those stay in `experiment_catalog` (`compare()` is the only place a
delta is made), and a screen that needs a number reads it from a row here.  What lives here is the
part every screen would otherwise repeat: counts per condition id, the one "look at this" line, rows
in catalog order, the eight-cell strip token, and a small cache that answers a redraw with `stat`
calls only.

Read-only.  Nothing here writes, gates, asks, or starts a process.
"""
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import experiment_catalog as E  # noqa: E402
import resource_progress as rp  # noqa: E402

CONTRACT = "experiment-board/v1"
TTL_SECONDS = 60.0      # a cached board is reused this long while no watched file changed; keeps the
#                         900 s `observation-stale` call from arriving later than one minute late
FAILED = ("status-failed", "nonfinite")
UNKNOWN = ("record-missing", "record-unreadable", "row-missing", "stagnation-unknown", "observation-stale")
STRIP_CAP = 9

_CACHE: Dict[str, Dict[str, Any]] = {}


def _count_pair(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    out = {"running": 0, "failed": 0, "stagnant": 0, "unknown": 0, "waiting": 0}
    for row in rows:
        out["failed"] += row["failed"]
        out["stagnant"] += row["stagnant"]
        out["unknown"] += row["unknown_flag"]
        out["running"] += row["run_status"] == "running" and not row["failed"] and not row["stagnant"]
        out["waiting"] += row["waiting"]
    return out


def _row(cond: Mapping[str, Any], group: Mapping[str, Any]) -> Dict[str, Any]:
    anomalies = list(cond.get("anomalies") or [])
    failed = any(a in FAILED for a in anomalies)
    stagnant = "stagnant" in anomalies and not failed
    reasons = [a for a in anomalies if a in UNKNOWN]
    unknown = cond.get("unknown") if isinstance(cond.get("unknown"), dict) else {}
    run_status = cond.get("run_status")
    if not cond.get("display_name"):
        reasons.append("name-missing")
    if unknown.get("run_status"):
        reasons.append(str(unknown["run_status"]))
    if run_status == "running" and cond.get("observed_at") is None:
        reasons.append("observation-unknown")
    unknown_flag = bool(reasons) and not failed and not stagnant
    no_run = cond.get("current_run") is None
    latest = cond.get("latest") if isinstance(cond.get("latest"), dict) else None
    waiting = no_run or (run_status == "running" and latest is None)
    if failed:
        state = "failed"
    elif stagnant:
        state = "stagnant"
    elif unknown_flag:
        state = "unknown"
    elif run_status == "running":
        state = "running"
    elif no_run:
        state = "waiting"
    else:
        state = "ended"
    comparison = cond.get("comparison") if isinstance(cond.get("comparison"), dict) else {}
    ok = comparison.get("state") == "ok"
    baseline = comparison.get("baseline") if isinstance(comparison.get("baseline"), dict) else None
    metric = group.get("metric") if isinstance(group.get("metric"), dict) else {}
    return {
        "condition_id": cond["id"], "name": cond.get("display_name") or None, "status": state,
        "failed": failed, "stagnant": stagnant, "unknown_flag": unknown_flag, "waiting": bool(waiting),
        "unknown_reasons": reasons, "run_status": run_status, "run_id": (cond.get("current_run") or {}).get("run_id"),
        "progress": cond.get("progress"),
        "latest": ({"value": latest.get("value"), "epoch": latest.get("epoch"), "key": latest.get("key"),
                    "nonfinite": bool(latest.get("nonfinite"))} if latest else None),
        "best": cond.get("best"),
        # The raw baseline number; the metric's unit is attached only when the comparison is valid
        # (RV-PR1596-R2: a baseline measured under another declaration is not in this unit).
        "baseline": ({"label": baseline.get("label"), "value": baseline.get("value"), "epoch": baseline.get("epoch")}
                     if baseline else None),
        "baseline_unit": (metric.get("unit") or None) if ok else None,
        "comparison_state": comparison.get("state"),
        "delta": comparison.get("delta") if ok else None,
        "delta_unit": comparison.get("delta_unit") if ok else None,
        "relative_percent": comparison.get("relative_percent") if ok else None,
        "better": comparison.get("better") if ok else None,
        "reason": comparison.get("reason"),
        "expected_at": cond.get("expected_at"),
        "since_last_progress_seconds": cond.get("since_last_progress_seconds"),
        "observed_at": cond.get("observed_at"), "observed_age_seconds": cond.get("observed_age_seconds"),
        "anomalies": anomalies, "changes": cond.get("changes"), "problem": cond.get("problem"),
        "compared_to": cond.get("compared_to"), "other_runs": (cond.get("other_runs") or {}).get("count", 0)}


def _headline(rows: Sequence[Mapping[str, Any]], metric: Mapping[str, Any]) -> Dict[str, Any]:
    def pick(kind: str, row: Mapping[str, Any], reason: Optional[str]) -> Dict[str, Any]:
        return {"kind": kind, "condition_id": row["condition_id"], "name": row["name"], "reason": reason}
    for row in rows:
        if row["failed"]:
            return pick("failed", row, next(a for a in row["anomalies"] if a in FAILED))
    for row in rows:
        if row["stagnant"]:
            return pick("stagnant", row, "stagnant")
    for row in rows:
        if row["unknown_flag"]:
            return pick("unknown", row, row["unknown_reasons"][0])
    for row in rows:
        if row["comparison_state"] == "not-comparable":
            return pick("not-comparable", row, row["reason"])
    for row in rows:
        if row["comparison_state"] == "no-data" or (row["waiting"] and row["comparison_state"] != "no-target"):
            return pick("waiting", row, row["reason"] or "no-observation")
    good = [row for row in rows if row["comparison_state"] == "ok"]
    if good:
        direction = metric.get("direction")
        best = good[0]
        for row in good[1:]:
            if direction == "min" and row["latest"]["value"] < best["latest"]["value"]:
                best = row
            elif direction == "max" and row["latest"]["value"] > best["latest"]["value"]:
                best = row
        return pick("best", best, None)
    for row in rows:
        if row["comparison_state"] == "no-target" and row["reason"] != "is-control":
            return pick("no-target", row, row["reason"])
    return {"kind": "none", "condition_id": None, "name": None, "reason": None}


def _group(group: Mapping[str, Any]) -> Dict[str, Any]:
    metric = group.get("metric") if isinstance(group.get("metric"), dict) else None
    rows = [_row(cond, group) for cond in group.get("conditions") or []]
    return {
        "id": group.get("id"), "name": group.get("display_name") or None, "goal": group.get("goal") or None,
        "goal_missing": not group.get("goal"),
        "metric": ({k: metric.get(k) for k in ("key", "unit", "direction", "split", "scoring")} if metric else None),
        "target": group.get("target") if isinstance(group.get("target"), dict) else None,
        "count": len(rows), "counts": _count_pair(rows), "headline": _headline(rows, metric or {}), "rows": rows}


def _strip(selected: Optional[Mapping[str, Any]], totals: Mapping[str, int], unlinked: int,
           catalog_unread: bool = False) -> Optional[str]:
    def capped(value: int) -> str:
        return f"{STRIP_CAP}+" if value > STRIP_CAP else str(value)
    attention = totals["failed"] + totals["stagnant"]
    if attention:
        return f"!{capped(attention)}LB"
    unconfirmed = totals["unknown"] + unlinked + (1 if catalog_unread else 0)
    if unconfirmed:
        return f"?{capped(unconfirmed)}LB"
    count = selected["count"] if selected else 0
    return None if count == 0 else (f"{count} LB" if count <= STRIP_CAP else f"{STRIP_CAP}+LB")


def board(status_doc: Mapping[str, Any]) -> Dict[str, Any]:
    """A pure summary of one `experiment-status/v1` document."""
    groups = [_group(g) for g in status_doc.get("groups") or []]
    totals = {k: sum(g["counts"][k] for g in groups) for k in ("running", "failed", "stagnant", "unknown", "waiting")}
    unlinked_doc = status_doc.get("unlinked_runs") or {}
    run_ids = list(unlinked_doc.get("run_ids") or [])
    unlinked = {"count": len(run_ids) + int(unlinked_doc.get("truncated") or 0), "run_ids": run_ids}
    catalog = dict(status_doc.get("catalog") or {})
    warnings = list(status_doc.get("warnings") or [])
    selected = groups[0] if groups else None
    other = [{"group_id": g["id"], "name": g["name"], "failed": g["counts"]["failed"],
              "stagnant": g["counts"]["stagnant"], "unknown": g["counts"]["unknown"]}
             for g in groups[1:] if g["counts"]["failed"] or g["counts"]["stagnant"] or g["counts"]["unknown"]]
    # A missing catalog and the per-run "no name" warnings are not news by themselves: the first is a
    # project with no experiments yet, the second is already counted in `unlinked`.
    news = [w for w in warnings if w.get("code") != "catalog-missing" and not str(w.get("where", "")).startswith("resource-runs/")]
    visible = bool(groups or unlinked["count"] or news or catalog.get("status") == "invalid")
    # The moment of the last progress, so a screen counts the age from its own clock like `observed_at`.
    read_at = E._ts(status_doc.get("generated_at"))
    for group in groups:
        for row in group["rows"]:
            since = row.get("since_last_progress_seconds")
            row["last_progress_at"] = None if read_at is None or since is None else E._rfc3339(read_at - since)
    return {
        "schema": CONTRACT, "generated_at": status_doc.get("generated_at"), "visible": visible,
        "catalog": {k: catalog.get(k) for k in ("status", "code", "path", "updated_at")},
        "groups": groups, "selected": selected["id"] if selected else None, "other_groups_attention": other,
        "totals": totals, "unlinked": unlinked, "warnings": warnings,
        "strip": (_strip(selected, totals, unlinked["count"], catalog.get("status") == "invalid")
                  if visible else None)}


# ---------------------------------------------------------------------------
# text pieces (numbers only; every screen words them the same way)
# ---------------------------------------------------------------------------


def value_text(value: float, unit: Optional[str]) -> str:
    return E._unit_text(value, unit) if unit else E._fmt(value)


def clock_text(iso: Optional[str], now: Optional[float] = None) -> str:
    """`21:10` today, `11-14 21:10` on another day; "" when unknown."""
    epoch = E._ts(iso)
    if epoch is None:
        return ""
    today = time.strftime("%m-%d", time.localtime(time.time() if now is None else now))
    stamp = time.localtime(epoch)
    return time.strftime("%H:%M" if time.strftime("%m-%d", stamp) == today else "%m-%d %H:%M", stamp)


def duration_text(seconds: Optional[float]) -> str:
    """`20s` / `42m` / `3h` / `2d`: how long ago, compactly; "" when unknown."""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    if seconds < 48 * 3600:
        hours = seconds / 3600
        return f"{hours:.0f}h" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f}h"
    return f"{round(seconds / 86400)}d"


def progress_text(row: Mapping[str, Any]) -> str:
    """`36/60 ep`; only what was confirmed (`36 ep` without a total); "" when progress is unknown."""
    progress = row.get("progress")
    if not progress or progress.get("reached") is None:
        return ""
    unit = {"epoch": "ep"}.get(progress.get("unit"), progress.get("unit") or "")
    total = progress.get("total")
    return f"{progress['reached']}/{total}" + (f" {unit}" if unit else "") if total is not None \
        else f"{progress['reached']}" + (f" {unit}" if unit else "")


def _point_text(point: Optional[Mapping[str, Any]], metric: Mapping[str, Any], unit: bool) -> str:
    if not point or point.get("value") is None:
        return ""
    text = E._fmt(point["value"])
    if point.get("key") not in (None, metric.get("key")):
        return f"{text} ({point['key']})"  # another metric: no shared unit, and it says so
    if unit and metric.get("unit"):
        text = E._unit_text(point["value"], metric["unit"])
    return text + (f"@{point['epoch']}" if point.get("epoch") is not None else "")


def latest_text(row: Mapping[str, Any], metric: Mapping[str, Any], unit: bool = False) -> str:
    return _point_text(row.get("latest"), metric, unit)


def best_text(row: Mapping[str, Any], metric: Mapping[str, Any], unit: bool = False) -> str:
    return _point_text(row.get("best"), metric, unit)


def baseline_text(row: Mapping[str, Any]) -> str:
    """`fix 8` / `기준 9.1@5`; the metric's unit only when the comparison was valid."""
    base = row.get("baseline")
    if not base or base.get("value") is None:
        return ""
    value = E._unit_text(base["value"], row.get("baseline_unit")) if row.get("baseline_unit") else E._fmt(base["value"])
    return (f"{base['label']} " if base.get("label") else "") + value \
        + (f"@{base['epoch']}" if base.get("epoch") is not None else "")


def delta_text(row: Mapping[str, Any]) -> str:
    """`+1.1 dB` / `+0.4%p`; "" unless `compare()` said the two values may be subtracted."""
    if row.get("delta") is None:
        return ""
    unit = row.get("delta_unit")
    return E._signed(row["delta"]) + ("%p" if unit == "pp" else f" {unit}" if unit else "")


def age_seconds(row: Mapping[str, Any], now: float, field: str = "observed_at") -> Optional[int]:
    """The age of a row's observation (or, with `field="last_progress_at"`, of its last progress) at
    `now` (render time), never the cached age."""
    at = E._ts(row.get(field))
    if at is None:
        return None
    if now < at:
        # a screen counts from its clock rounded down to the minute, so a moment inside the current
        # minute is "0s old", not unknown; a moment further ahead is a clock that disagrees
        return 0 if at - now < 60 else None
    return int(now - at)


def unreadable(root: Path, now: float, why: str = "") -> Dict[str, Any]:
    """A board that says "could not read", never an empty one (a read failure is not "no experiments")."""
    stamp = E._rfc3339(now)
    return {"schema": CONTRACT, "generated_at": stamp, "visible": True,
            "catalog": {"status": "unreadable", "code": why or "read-failed", "path": E.CATALOG_REL, "updated_at": None},
            "groups": [], "selected": None, "other_groups_attention": [],
            "totals": {"running": 0, "failed": 0, "stagnant": 0, "unknown": 0, "waiting": 0},
            "unlinked": {"count": 0, "run_ids": []}, "warnings": [], "strip": "?1LB"}


def _stat(path: str) -> Optional[Tuple[int, int, int, int, int]]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    # Same identity `tools/fleet/route.py` uses: a rewrite that restores the size and the mtime
    # (a copy with `touch -r`, a same-length edit) still moves the inode or the ctime.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _watch_paths(root: Path, doc: Mapping[str, Any]) -> List[str]:
    paths = [str(E.catalog_path(root)), str(Path(root) / "resource-runs")]
    for group in doc.get("groups") or []:
        for cond in group.get("conditions") or []:
            run_ids = [(cond.get("current_run") or {}).get("run_id")] + list((cond.get("other_runs") or {}).get("run_ids") or [])
            for run_id in run_ids:
                path = rp.public_record_path(root, run_id) if run_id else None
                if path is not None and str(path) not in paths:
                    paths.append(str(path))
    return paths


def read(root: Path, *, now: Optional[float] = None, cache: Optional[Dict[str, Any]] = None,
         ttl: float = TTL_SECONDS) -> Tuple[Dict[str, Any], List[str]]:
    """`(board, watch_paths)`; a redraw inside the TTL with unchanged files costs `stat` calls only.

    The key is the stat of the catalog, the `resource-runs` folder, and each linked public record.
    A changed key, or a cache older than `ttl`, reads `status()` again; the TTL is what lets a
    record that did not change still become `observation-stale` within a minute of the 900 s mark.
    The stat that keys an entry is taken *before* `status()` reads, never after: a file that changes
    while `status()` runs then differs from its key on the next call and is read again, instead of
    a stale board being filed under the new stat for a whole TTL.  A record linked for the first time
    has no earlier stat, so it is keyed by the stat after the read (a window of one read, and only
    for a file edited in place during it).
    Any failure returns the "unreadable" board with the same watch paths, never an empty one."""
    store = _CACHE if cache is None else cache
    stamp = time.time() if now is None else now
    key = str(root)
    entry = store.get(key)
    if entry is not None and 0 <= stamp - entry["at"] < ttl \
            and all(_stat(p) == s for p, s in entry["signature"]):
        return entry["board"], list(entry["watch"])
    base = [str(E.catalog_path(Path(root))), str(Path(root) / "resource-runs")]
    before = {p: _stat(p) for p in base + [p for p in (entry["watch"] if entry else ()) if p not in base]}
    try:
        doc = E.status(Path(root), now=stamp)
        result = board(doc)
        watch = _watch_paths(Path(root), doc)
    except Exception as exc:  # noqa: BLE001 -- a screen never fails because the catalog could not be read
        result = unreadable(Path(root), stamp, type(exc).__name__)
        watch = list(base)
    store[key] = {"at": stamp, "board": result, "watch": watch,
                  "signature": [(p, before[p] if p in before else _stat(p)) for p in watch]}
    return result, list(watch)


def clear_cache(cache: Optional[Dict[str, Any]] = None) -> None:
    (_CACHE if cache is None else cache).clear()
