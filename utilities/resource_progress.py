"""Optional, bounded resource progress; counters never decide process lifecycle.

This observes existing producer metadata. It never imports a producer, scans a
run tree, writes a registry, or changes the registered process lifecycle. Lab
workloads may opt in with ``write_progress(12, "epoch", total=50)``: the runner
supplies the path/run/node in AGENT_RESOURCE_PROGRESS_FILE/RUN_ID/NODE. With no
progress environment, the writer does nothing. Readers use only the declared
file, not logs; a missing or invalid observation stays unavailable.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time


MAX_BYTES = 65536
MAX_PUBLIC_RUN_RECORD_BYTES = 4 * 1024 * 1024
MAX_ARMS = 32
OBSERVATION_VERSION = 1
CHECKPOINT_INTERVAL = 12 * 60 * 60
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")


def metric_summary(samples, *, decision_metric=None, direction=None, baseline=None):
    """Summarize observed validation scalars without inventing a criterion."""
    rows = [row for row in samples if isinstance(row, dict)
            and str(row.get("split", "")).lower() in {"valid", "val", "validation"}
            and type(row.get("epoch")) is int and isinstance(row.get("metrics"), dict)]
    keys = sorted({key for row in rows for key in row["metrics"] if isinstance(key, str)})
    key = decision_metric if isinstance(decision_metric, str) and decision_metric else None
    if key is None:
        key = next((candidate for candidate in keys if candidate.lower() in
                    {"loss", "valid_loss", "val_loss", "l_se", "error", "wer", "cer"}), None)
    if key is None:
        key = next((candidate for candidate in keys if candidate.lower() in
                    {"sisnri", "si_sdri", "si_sdr", "accuracy", "acc", "psnr", "ssim"}), None)
    if direction not in {"min", "max"}:
        direction = _metric_direction(key)
    selected = []
    for row in rows:
        if key not in row["metrics"]:
            continue
        value = row["metrics"][key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            selected.append((row["epoch"], float(value), row.get("source_path")))
    selected.sort(key=lambda item: item[0])
    distinct = {}
    for item in selected:
        distinct[item[0]] = item
    selected = list(distinct.values())
    finite = [(epoch, value, path) for epoch, value, path in selected if math.isfinite(value)]
    bad = [(epoch, value, path) for epoch, value, path in selected if not math.isfinite(value)]
    trend = "판정 불가"
    worsening = 0
    if direction and len(finite) >= 2:
        tail = finite[-4:]
        deltas = [((right[1] - left[1]) * (1 if direction == "min" else -1))
                  for left, right in zip(tail, tail[1:])]
        epsilon = 1e-6 * max(1.0, *(abs(value) for _epoch, value, _path in tail))
        classes = ["flat" if abs(delta) <= epsilon else "improve" if delta < 0 else "worse"
                   for delta in deltas]
        worsening = 0
        for value in reversed(classes):
            if value != "worse": break
            worsening += 1
        trend = ("개선" if classes and all(value == "improve" for value in classes) else
                 "정체" if classes and all(value == "flat" for value in classes) else
                 "악화" if classes and all(value == "worse" for value in classes) else
                 "불안정" if "improve" in classes and "worse" in classes else "불안정")
    best = None
    if finite and direction:
        best = min(finite, key=lambda item: item[1]) if direction == "min" else max(finite, key=lambda item: item[1])
    latest = selected[-1] if selected else None
    comparison = None
    if latest and baseline is not None and isinstance(baseline, (int, float)) \
            and not isinstance(baseline, bool) and math.isfinite(float(baseline)) \
            and math.isfinite(latest[1]):
        comparison = float(latest[1]) - float(baseline)
    latest_metrics, best_metrics = {}, {}
    for candidate in keys:
        points = {}
        for row in rows:
            value = row["metrics"].get(candidate)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                points[row["epoch"]] = (row["epoch"], float(value), row.get("source_path"))
        ordered = [points[epoch] for epoch in sorted(points)]
        candidate_direction = direction if candidate == key else _metric_direction(candidate)
        latest_metrics[candidate] = _metric_point(ordered[-1]) if ordered else None
        finite_points = [point for point in ordered if math.isfinite(point[1])]
        best_point = (min(finite_points, key=lambda point: point[1])
                      if finite_points and candidate_direction == "min" else
                      max(finite_points, key=lambda point: point[1])
                      if finite_points and candidate_direction == "max" else None)
        best_metrics[candidate] = _metric_point(best_point)
    return {"metric": key, "direction": direction, "latest": _metric_point(latest),
            "best": _metric_point(best), "trend": trend, "worsening_transitions": worsening,
            "latest_metrics": latest_metrics, "best_metrics": best_metrics,
            "nonfinite": [{"epoch": epoch, "value": value, "path": path} for epoch, value, path in bad],
            "baseline": float(baseline) if isinstance(baseline, (int, float))
                        and not isinstance(baseline, bool) and math.isfinite(float(baseline)) else None,
            "baseline_delta": comparison}


def _metric_direction(key):
    lowered = (key or "").lower()
    return ("min" if any(part in lowered for part in ("loss", "l_se", "error", "wer", "cer"))
            else "max" if any(part in lowered for part in
                              ("sisnr", "si_sdr", "accuracy", "acc", "psnr", "ssim"))
            else None)


def _metric_point(point):
    if point is None:
        return None
    epoch, value, path = point
    return {"epoch": epoch, "value": value, "path": path}


def _path_has_symlink(path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _reverse_namespace_path(path, mapping):
    candidate = Path(path)
    if not isinstance(mapping, dict):
        return candidate, False
    matches = []
    for target, source in mapping.items():
        try:
            relative = candidate.relative_to(Path(target))
        except (ValueError, TypeError):
            continue
        matches.append(Path(source) / relative)
    return (matches[0], True) if len(matches) == 1 else (candidate, False)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"$hearting_float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _restore_special_numbers(value):
    if isinstance(value, dict):
        if set(value) == {"$hearting_float"}:
            return {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}.get(value["$hearting_float"])
        return {key: _restore_special_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_special_numbers(item) for item in value]
    return value


def _schedule(started_at, planned_seconds, now, last_checkpoint_at=None):
    elapsed = max(0.0, float(now) - float(started_at))
    if isinstance(planned_seconds, (int, float)) and not isinstance(planned_seconds, bool) \
            and math.isfinite(float(planned_seconds)) and planned_seconds > 0:
        planned = float(planned_seconds)
        first = min(planned * 0.10, 7200.0)
        slots = [first, planned * .25, planned * .50, planned * .75]
        if last_checkpoint_at is None:
            due_elapsed = first
        else:
            last_elapsed = max(0.0, float(last_checkpoint_at) - float(started_at))
            due_elapsed = next((slot for slot in slots if slot > last_elapsed + 1e-6),
                               max(slots[-1], last_elapsed) + CHECKPOINT_INTERVAL)
        due = elapsed >= due_elapsed - 1e-6
        next_elapsed = next((slot for slot in slots if slot > elapsed + 1e-6), None)
        if next_elapsed is None:
            next_elapsed = max(slots[-1], elapsed) + CHECKPOINT_INTERVAL
    else:
        planned = None
        first = 7200.0
        if last_checkpoint_at is None:
            due_elapsed = first
        else:
            last_elapsed = max(0.0, float(last_checkpoint_at) - float(started_at))
            due_elapsed = max(first, last_elapsed + CHECKPOINT_INTERVAL)
        due = elapsed >= due_elapsed - 1e-6
        next_elapsed = first if elapsed < first else first + (math.floor((elapsed - first) / CHECKPOINT_INTERVAL) + 1) * CHECKPOINT_INTERVAL
    return {"elapsed_seconds": elapsed, "planned_seconds": planned,
            "first_checkpoint_seconds": first,
            "due_checkpoint_at": float(started_at) + due_elapsed,
            "next_checkpoint_at": float(started_at) + next_elapsed,
            "checkpoint_due": due}


def evaluate_observation(*, now, started_at, planned_seconds=None, progress=None,
                         previous_progress=None, previous_progress_at=None,
                         samples=(), decision_metric=None, direction=None,
                         baseline=None, final=False, optional_rules=None,
                         last_checkpoint_at=None):
    """Pure virtual-clock evaluator shared by local and remote resource watchers."""
    now, started_at = float(now), float(started_at)
    schedule = _schedule(started_at, planned_seconds, now, last_checkpoint_at)
    elapsed = schedule["elapsed_seconds"]
    summary = metric_summary(samples, decision_metric=decision_metric,
                             direction=direction, baseline=baseline)
    reached = total = unit = None
    if isinstance(progress, dict):
        reached = progress.get("completed", progress.get("reached_epoch"))
        total = progress.get("total", progress.get("total_epochs"))
        unit = progress.get("unit") or ("epoch" if "reached_epoch" in progress else None)
    rate = eta = predicted_total = None
    progress_age = None
    if isinstance(progress, dict):
        if isinstance(progress.get("age_s"), (int, float)):
            progress_age = max(0.0, float(progress["age_s"]))
        elif isinstance(progress.get("updated_at"), (int, float)):
            progress_age = max(0.0, now - float(progress["updated_at"]))
    progress_fresh = progress_age is None or progress_age <= 600
    if (type(reached) is int and reached >= 0 and type(total) is int and total > 0
            and reached <= total and isinstance(previous_progress, dict)
            and type(previous_progress.get("completed")) is int
            and previous_progress.get("unit") == unit
            and previous_progress.get("total") == total
            and isinstance(previous_progress_at, (int, float)) and progress_fresh):
        delta = reached - previous_progress["completed"]
        duration = now - float(previous_progress_at)
        if delta > 0 and duration > 0:
            rate = delta / duration
            eta = (total - reached) / rate
            predicted_total = elapsed + eta
    attention = []
    if summary["nonfinite"]:
        attention.append({"kind": "nonfinite-metric", "metric": summary["metric"],
                          "epoch": summary["nonfinite"][-1]["epoch"]})
    if summary["worsening_transitions"] >= 3:
        attention.append({"kind": "validation-worsening", "metric": summary["metric"],
                          "epoch": summary["latest"]["epoch"] if summary["latest"] else None})
    if summary["direction"] and summary["best"] and summary["latest"]:
        best = float(summary["best"]["value"])
        latest = float(summary["latest"]["value"])
        loss_direction = summary["direction"] == "min"
        distance = latest - best if loss_direction else best - latest
        if distance > 10 * max(abs(best), 1.0):
            attention.append({"kind": "metric-divergence", "metric": summary["metric"],
                              "epoch": summary["latest"]["epoch"]})
    if rate is not None:
        if planned_seconds and predicted_total is not None and predicted_total >= 1.5 * float(planned_seconds):
            attention.append({"kind": "eta-over-plan", "predicted_total_seconds": predicted_total})
        elif not planned_seconds and predicted_total is not None and predicted_total > 6 * 3600:
            attention.append({"kind": "eta-over-six-hours", "predicted_total_seconds": predicted_total})
    elif planned_seconds and elapsed >= 1.5 * float(planned_seconds):
        attention.append({"kind": "elapsed-over-plan", "elapsed_seconds": elapsed})
    elif not planned_seconds and elapsed > 6 * 3600:
        attention.append({"kind": "elapsed-over-six-hours", "elapsed_seconds": elapsed})
    if isinstance(optional_rules, list):
        for rule in optional_rules[:16]:
            if not isinstance(rule, dict) or not isinstance(rule.get("metric"), str):
                continue
            op, limit = rule.get("op"), rule.get("threshold")
            candidates = [row for row in samples if isinstance(row, dict)
                          and str(row.get("split", "")).lower() in {"valid", "val", "validation"}
                          and isinstance(row.get("metrics"), dict)
                          and rule["metric"] in row["metrics"]]
            candidates.sort(key=lambda row: row.get("epoch", -1))
            latest_value = candidates[-1]["metrics"].get(rule["metric"]) if candidates else None
            if (op not in {"lt", "lte", "gt", "gte"} or latest_value is None
                    or not isinstance(latest_value, (int, float)) or isinstance(latest_value, bool)
                    or not math.isfinite(float(latest_value))
                    or not isinstance(limit, (int, float)) or isinstance(limit, bool)
                    or not math.isfinite(float(limit))):
                continue
            value = float(latest_value)
            fired = ((op == "lt" and value < limit) or (op == "lte" and value <= limit)
                     or (op == "gt" and value > limit) or (op == "gte" and value >= limit))
            if fired:
                attention.append({"kind": "declared-rule", "metric": rule["metric"],
                                  "op": op, "threshold": float(limit),
                                  "epoch": candidates[-1].get("epoch")})
    checkpoint = bool(final or schedule["checkpoint_due"])
    early_eta = bool(rate is not None and elapsed <= 600 and attention
                     and any(row["kind"].startswith("eta-") for row in attention))
    return {"at": now, "started_at": started_at, "elapsed_seconds": elapsed,
            "planned_seconds": schedule["planned_seconds"],
            "reached": reached, "total": total, "unit": unit, "rate_per_second": rate,
            "eta_seconds": eta, "predicted_total_seconds": predicted_total,
            "checkpoint_due": checkpoint, "early_eta_attention": early_eta,
            "attention": attention, "summary": summary,
            "due_checkpoint_at": schedule["due_checkpoint_at"],
            "next_checkpoint_at": now if final else schedule["next_checkpoint_at"],
            "final": bool(final)}


def public_run_record(run, *, status=None, observation=None, metrics=None,
                      started_at=None, ended_at=None):
    """Build the stable UI-consumer record from known runtime facts only."""
    observation = observation if isinstance(observation, dict) else {}
    record_metrics = metrics if isinstance(metrics, list) else []
    progress = observation.get("progress") if isinstance(observation.get("progress"), dict) else {}
    summary = observation.get("summary") if isinstance(observation.get("summary"), dict) else {}
    declared = run.get("observation") if isinstance(run.get("observation"), dict) else {}
    decision_metric = run.get("decision_metric", declared.get("decision_metric"))
    proxy = run.get("proxy", declared.get("proxy"))
    result = {
        "schema_version": 1,
        "run_id": run.get("run_id"),
        "campaign_id": run.get("campaign_id"),
        "cycle_id": run.get("cycle_id"),
        "route_id": run.get("route_id"),
        "node": run.get("node") or run.get("route_node"),
        "host": run.get("host") or run.get("compute_host"),
        "output_dir": run.get("output_dir"),
        "status": status or run.get("status"),
        "started_at": started_at if started_at is not None else run.get("started_at"),
        "ended_at": ended_at if ended_at is not None else run.get("ended_at"),
        "progress": {"reached": observation.get("reached", progress.get("completed")),
                     "total": observation.get("total", progress.get("total")),
                     "unit": observation.get("unit", progress.get("unit"))},
        "eta": observation.get("eta_seconds"),
        "planned_seconds": observation.get("planned_seconds", run.get("planned_seconds")),
        "metrics": record_metrics,
        "decision_metric": decision_metric,
        "proxy": proxy,
        "comparable_group": run.get("comparable_group", declared.get("comparable_group")),
        "baselines": run.get("baselines", declared.get("baselines"))
            if isinstance(run.get("baselines", declared.get("baselines")), list) else [],
        "last_checkpoint": {"at": observation.get("at"), "trend": summary.get("trend"),
                            "rule_fired": observation.get("attention", []),
                            "eta_vs_plan": (observation.get("predicted_total_seconds") /
                                observation.get("planned_seconds")
                                if observation.get("predicted_total_seconds") is not None
                                and observation.get("planned_seconds") else None)},
        "metric_summary": {
            "metric": summary.get("metric"), "direction": summary.get("direction"),
            "latest": _public_metric_point(summary.get("latest")),
            "best": _public_metric_point(summary.get("best")),
            "latest_metrics": {key: _public_metric_point(value)
                               for key, value in (summary.get("latest_metrics") or {}).items()},
            "best_metrics": {key: _public_metric_point(value)
                             for key, value in (summary.get("best_metrics") or {}).items()},
            "trend": summary.get("trend"), "baseline_delta": summary.get("baseline_delta"),
            "nonfinite": bool(summary.get("nonfinite")),
        },
    }
    verdict = run.get("reviewer_verdict")
    if isinstance(verdict, str) and verdict.strip():
        result["verdict"] = verdict
    return result


def _public_metric_point(point):
    if not isinstance(point, dict):
        return None
    value = point.get("value")
    finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    return {"epoch": point.get("epoch"), "value": float(value) if finite else None,
            "nonfinite": not finite, "path": point.get("path")}


def _observation_state_path(run, registry, row_key, *, create=True):
    jobs = run.get("jobs") or os.environ.get("AGENT_DISPATCH_JOBS")
    try:
        from resource_run_registry import agent_home
        from dispatch_contract import resolve_dispatch_state_root
        state_root = resolve_dispatch_state_root(agent_home(), explicit_jobs=jobs)
    except (OSError, ValueError, TypeError, RuntimeError):
        state_root = Path(os.environ.get("AGENT_RUNTIME_DIR") or
                          (Path.home() / ".cache" / "hearting"))
    execution = run.get("execution") if isinstance(run.get("execution"), dict) else {}
    identity = {key: run.get(key) for key in ("run_id", "pid", "starttime", "command_hash")}
    identity.update(host=run.get("host") or execution.get("host"),
                    compute_run_id=execution.get("run_id"), row_key=row_key)
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    directory = Path(state_root) / "resource-observations"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISDIR(mode)):
        raise ValueError("observation-state-directory-symlink")
    return directory / (digest + ".json")


def _merge_samples(previous, incoming, limit=4096):
    precedence = {"override": 0, "epoch-json-files": 1, "epochs-jsonl": 2, "tfevents": 3}
    by_key = {}
    for row in [*(previous if isinstance(previous, list) else []),
                *(incoming if isinstance(incoming, list) else [])]:
        if not isinstance(row, dict) or not isinstance(row.get("metrics"), dict):
            continue
        for name, value in row["metrics"].items():
            if not isinstance(name, str):
                continue
            key = (row.get("unit"), row.get("split"), row.get("epoch"), name)
            current = by_key.get(key)
            candidate = dict(row, metrics={name: value})
            if (current is None or precedence.get(candidate.get("format"), 99)
                    <= precedence.get(current.get("format"), 99)):
                by_key[key] = candidate
    values = list(by_key.values())
    values.sort(key=lambda row: (str(row.get("unit")), str(row.get("split")),
                                 int(row.get("epoch", -1)), str(row.get("format"))))
    return values[-limit:]


def _merge_metric_sources(previous, current, samples):
    if not current and isinstance(previous, list):
        return previous
    old = {}
    for row in previous if isinstance(previous, list) else []:
        if isinstance(row, dict) and isinstance(row.get("format"), str):
            old[(row["format"], json.dumps(row.get("path"), sort_keys=True))] = row
    result = []
    for item in current if isinstance(current, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("format"), str):
            continue
        key = (item["format"], json.dumps(item.get("path"), sort_keys=True))
        prior = old.get(key, {})
        names = set(prior.get("keys", [])) | set(item.get("keys", []))
        path_values = item.get("path") if isinstance(item.get("path"), list) else [item.get("path")]
        names.update(row_name for sample in samples
                     if sample.get("format") == item["format"]
                     and sample.get("source_path") in path_values
                     for row_name in sample.get("metrics", {}))
        result.append({**item, "keys": sorted(name for name in names if isinstance(name, str))})
    return result


def _notice_targets(run):
    values = []
    owner = run.get("owner_wait") if isinstance(run.get("owner_wait"), dict) else {}
    jobs = (run.get("jobs") or owner.get("jobs")
            or os.environ.get("AGENT_DISPATCH_JOBS"))

    def add(harness, session_id, attempt_id=None, binding_kind="provenance"):
        if harness not in {"claude", "codex", "opencode"} or not session_id:
            return
        lineage = {"kind": "provenance", "harness": str(harness),
                   "session_id": str(session_id)}
        if attempt_id and jobs:
            try:
                from dispatch_completion_join import exact_attempt_row
                row = exact_attempt_row(Path(jobs), str(attempt_id))
                metadata = row.metadata if row is not None else {}
                if binding_kind == "owner-session" and metadata.get("harness") == harness:
                    lineage = {"kind": "owner-session", "attempt_id": str(attempt_id),
                               "harness": str(harness), "session_id": str(session_id)}
                elif binding_kind == "attempt-parent" and metadata.get("parent_sid") == session_id:
                    from harness_capabilities import HARNESSES, parent_completion
                    parent_harness = next((name for name in HARNESSES
                        if parent_completion(name).get("carrier")
                        == metadata.get("parent_completion_delivery")), None)
                    if parent_harness == harness:
                        lineage = {"kind": "attempt-parent", "attempt_id": str(attempt_id),
                                   "harness": str(harness), "session_id": str(session_id)}
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
        values.append((str(harness), str(session_id), lineage))

    owner_harness, owner_session = owner.get("harness"), owner.get("session_id")
    owner_attempt = owner.get("parent_attempt_id") or run.get("parent_attempt_id")
    if owner_session and owner_attempt:
        try:
            from dispatch_completion_join import exact_attempt_row
            row = exact_attempt_row(Path(jobs), str(owner_attempt)) if jobs and owner_attempt else None
            metadata = row.metadata if row is not None else {}
            owner_harness = owner_harness or metadata.get("harness")
            if owner_harness:
                add(owner_harness, owner_session, owner_attempt, "owner-session")
            parent_session = metadata.get("parent_sid")
            parent_carrier = metadata.get("parent_completion_delivery")
            if parent_session and parent_carrier:
                from harness_capabilities import HARNESSES, parent_completion
                supervisor_harness = next((name for name in HARNESSES
                    if parent_completion(name).get("carrier") == parent_carrier), None)
                if supervisor_harness:
                    add(supervisor_harness, parent_session, owner_attempt, "attempt-parent")
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
    elif owner_harness and owner_session:
        add(owner_harness, owner_session)
    provenance = run.get("provenance") if isinstance(run.get("provenance"), dict) else {}
    for candidate in (run.get("notify"), run.get("session"), run.get("supervisor_session"),
                      provenance.get("notify"), provenance.get("session")):
        if isinstance(candidate, dict) and candidate.get("harness") and candidate.get("id"):
            candidate_attempt = candidate.get("parent_attempt_id") or candidate.get("attempt_id")
            add(candidate["harness"], candidate["id"], candidate_attempt,
                "attempt-parent" if candidate_attempt else "provenance")
    result = {}
    for harness, session, lineage in values:
        key = harness, session
        previous = result.get(key)
        if previous is None or (lineage["kind"] == "attempt-parent"
                                and previous["lineage"]["kind"] != "attempt-parent"):
            result[key] = {"harness": harness, "session_id": session, "lineage": lineage}
    return list(result.values())


def _notice_texts(run, event_kind, observation, record_path, sources):
    summary = observation.get("summary") or {}
    latest, best = summary.get("latest"), summary.get("best")
    metric = summary.get("metric")
    def points(field, fallback):
        values = summary.get(field) if isinstance(summary.get(field), dict) else {}
        names = list(values)
        if metric in names:
            names.remove(metric)
            names.insert(0, metric)
        rendered = []
        for name in names[:3]:
            value = values.get(name)
            if not isinstance(value, dict):
                continue
            number = value.get("value")
            shown = (number if isinstance(number, (int, float)) and math.isfinite(float(number))
                     else "NaN/Inf")
            rendered.append(f"{name}={shown}@{value.get('epoch')}")
        if rendered:
            return ",".join(rendered)
        return (f"{metric}={fallback['value']}@{fallback['epoch']}"
                if isinstance(fallback, dict) and metric else "지표 없음")
    record = str(record_path) if record_path else "기록 경로 없음"
    run_line = f"run={run.get('run_id') or 'unknown'}"
    if run.get("row_id"):
        run_line += f" row={run['row_id']}"
    entries = [run_line, f"public run record={record}"]
    if event_kind == "final":
        entries.append(f"마지막={points('latest_metrics', latest)}; 최고={points('best_metrics', best)}; 추세={summary.get('trend') or '판정 불가'}")
        entries.append(f"실제={_duration(observation.get('elapsed_seconds'))}; 계획={_duration(observation.get('planned_seconds'))}")
        if observation.get("attention"):
            entries.append("attention=" + ",".join(item.get("kind", "unknown") for item in observation["attention"][:3]))
    else:
        entries.append(f"최근={points('latest_metrics', latest)}; 최고={points('best_metrics', best)}; 기준 delta={summary.get('baseline_delta') if summary.get('baseline_delta') is not None else '기준 없음'}; 추세={summary.get('trend') or '판정 불가'}")
        eta = observation.get("predicted_total_seconds")
        entries.append(f"ETA 총 {_duration(eta)}; 계획 {_duration(observation.get('planned_seconds'))}; 경과 {_duration(observation.get('elapsed_seconds'))}")
        if observation.get("attention"):
            entries.append("attention=" + ",".join(item.get("kind", "unknown") for item in observation["attention"][:3]))
    source_lines = []
    for source in (sources or [])[:3]:
        path = source.get("path")
        if isinstance(path, list):
            path = path[0] if path else None
        source_lines.append(f"source {source.get('format') or 'unknown'}={path or '경로 없음'}")
    if not source_lines:
        source_lines.append("source metrics unavailable")
    entries.extend(source_lines)
    text = "; ".join(entries)
    if len(text) <= 600:
        return [text]
    # Keep every field in immutable bounded parts; numbered chunks can be
    # concatenated in order when a path is longer than one notice.
    output = []
    for label, value in [("run", run_line), ("record", record),
                         ("metrics", "; ".join(entries[2:]))]:
        value = f"{label}: {value}"
        if len(value) <= 600:
            output.append(value)
            continue
        width = 500
        chunks = [value[index:index + width] for index in range(0, len(value), width)]
        total = len(chunks)
        output.extend(f"part {index}/{total}: {chunk}"
                      for index, chunk in enumerate(chunks, 1))
    return output


def _duration(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return "미상"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _write_observation_state(path, value):
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(_json_safe(value), output, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp:
            try: os.unlink(temp)
            except OSError: pass


def _read_observation_state(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) \
            or info.st_size > MAX_PUBLIC_RUN_RECORD_BYTES:
        raise ValueError("observation-state-unbounded")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        raw = source.read(MAX_PUBLIC_RUN_RECORD_BYTES + 1)
        after = os.fstat(source.fileno())
    current = path.lstat()
    if (len(raw) > MAX_PUBLIC_RUN_RECORD_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
               != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
        raise ValueError("observation-state-changed")
    return _restore_special_numbers(_json(raw))


def read_experiment_observation(run, registry, row_key):
    """Read one persisted observation projection without creating runtime state."""
    try:
        path = _observation_state_path(run, registry, row_key, create=False)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        value = _read_observation_state(path)
        if (value.get("run_id") != run.get("run_id")
                or value.get("row_key") != str(row_key)):
            return None
        return {"available": True, "observation": value.get("observation"),
                "metrics": value.get("sources", []), "metric_root": None,
                "record_path": None, "delivery": [], "warnings": value.get("warnings", []),
                "state_path": str(path)}
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def observe_experiment(run, registry, now, *, root=None, row_key=None,
                       started_at=None, planned_seconds=None, progress=None,
                       final=False, status=None, notify_events=True,
                       poll_interval_seconds=60.0):
    """Advance one detached row's durable observation and public run record.

    Metric discovery is deliberately caller-bound: `root` must come from an
    exact run/config binding or explicit optional observation metadata.
    """
    run = run if isinstance(run, dict) else {}
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"available": False, "warnings": ["run-id-unknown"]}
    root_value = root or run.get("output_dir") or discover_output_root(run)
    metric_root = None
    if isinstance(root_value, str) and root_value:
        try:
            raw_root = Path(root_value)
            if not raw_root.is_absolute():
                cwd = run.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    raise ValueError("metric-root-relative-without-cwd")
                raw_root = Path(cwd) / raw_root
            if _path_has_symlink(raw_root):
                raise ValueError("metric-root-symlink")
            metric_root = raw_root.resolve(strict=True)
            if not metric_root.is_dir():
                metric_root = None
        except (OSError, RuntimeError, ValueError):
            metric_root = None
    identity = row_key or run.get("row_id") or "run"
    try:
        state_path = _observation_state_path(run, registry, str(identity))
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return {"available": False, "warnings": ["observation-state-unavailable:" + str(exc)]}
    lock_path = state_path.with_suffix(".lock")
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        return {"available": False, "warnings": ["observation-state-unavailable:" + str(exc)]}
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            previous = _read_observation_state(state_path) if state_path.exists() else {}
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError, TypeError):
            previous = {}
        previous_poll = previous.get("last_observer_poll_at")
        if (not final and isinstance(previous_poll, (int, float))
                and not isinstance(previous_poll, bool)
                and float(now) - float(previous_poll) < max(1.0, float(poll_interval_seconds))
                and status == previous.get("public_status")):
            cached_observation = dict(previous.get("observation") or {})
            cached_observation["checkpoint_due"] = False
            cached_observation["due_checkpoint_at"] = None
            return {"available": True, "observation": cached_observation,
                    "metrics": previous.get("sources", []), "metric_root": None,
                    "record_path": None, "delivery": [],
                    "warnings": previous.get("warnings", []), "state_path": str(state_path)}
        warnings, sources, new_samples, cursor = [], [], [], previous.get("cursor")
        last_observed_at = previous.get("last_observed_at")
        should_read_sources = (metric_root is not None and
            (final or not isinstance(last_observed_at, (int, float))
             or float(now) - float(last_observed_at) >= 60.0))
        if should_read_sources:
            try:
                decoded = __import__("resource_metric_sources").read_sources(metric_root, cursor=cursor)
                new_samples, sources, cursor = decoded["samples"], decoded["sources"], decoded["cursor"]
                new_samples, sources = _apply_declared_metric_keys(run, new_samples, sources)
                warnings.extend(decoded["warnings"])
                if decoded.get("conflicts"):
                    warnings.append("metric-source-conflicts:" + str(len(decoded["conflicts"])))
                if not decoded.get("sources") and previous.get("sources"):
                    warnings.append("previous-metric-source-not-currently-present")
                if decoded.get("progress") and progress is None:
                    value = decoded["progress"]
                    progress = {"completed": value.get("reached_epoch"),
                                "total": value.get("total_epochs"), "unit": "epoch",
                                "updated_at": value.get("updated_at"),
                                "age_s": max(0.0, float(now) - float(value["updated_at"]))
                                    if isinstance(value.get("updated_at"), (int, float)) else None}
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                warnings.append("metric-source-unavailable:" + str(exc))
        samples = _merge_samples(previous.get("samples"), new_samples)
        sources = _merge_metric_sources(previous.get("sources"), sources, samples)
        row_scoped = bool(row_key and str(row_key) != "run")
        started = (started_at if isinstance(started_at, (int, float)) else
                   None if row_scoped else run.get("started_at"))
        if not isinstance(started, (int, float)) or isinstance(started, bool) \
                or not math.isfinite(float(started)):
            started = None
        clock_known = started is not None
        if not clock_known:
            warnings.append("row-start-unknown")
        schedule_started = (float(started) if clock_known else
                            previous.get("schedule_started_at"))
        if not isinstance(schedule_started, (int, float)) or isinstance(schedule_started, bool) \
                or not math.isfinite(float(schedule_started)):
            schedule_started = float(now)
        plan = planned_seconds if planned_seconds is not None else _declared_plan(run)
        declared = run.get("observation") if isinstance(run.get("observation"), dict) else {}
        decision_metric = run.get("decision_metric", declared.get("decision_metric"))
        observation = evaluate_observation(
            now=now, started_at=schedule_started, planned_seconds=plan,
            progress=progress, previous_progress=previous.get("progress_sample"),
            previous_progress_at=previous.get("progress_at"), samples=samples,
            decision_metric=(decision_metric.get("key") if isinstance(decision_metric, dict)
                             else decision_metric),
            direction=(decision_metric.get("direction") if isinstance(decision_metric, dict)
                       else run.get("decision_direction", declared.get("direction"))),
            baseline=run.get("baseline_value", declared.get("baseline_value")), final=final,
            optional_rules=run.get("rules", declared.get("rules")),
            last_checkpoint_at=previous.get("last_checkpoint_at"))
        if not clock_known:
            # A wrapper clock is not a row birth. Keep the row's own clock
            # unknown while retaining its independently identified metrics.
            observation["elapsed_seconds"] = None
            observation["predicted_total_seconds"] = None
            observation["eta_seconds"] = None
            observation["eta_vs_plan"] = None
        next_progress = ({key: progress.get(key) for key in
                          ("completed", "total", "unit", "updated_at", "reached_epoch", "total_epochs")
                          if key in progress} if isinstance(progress, dict) else None)
        artifact_root = run.get("artifact_root")
        record_locator = public_record_path(artifact_root, run_id) if artifact_root else None
        event_specs = []
        if notify_events and final:
            event_specs.append(("final", f"final:{identity}"))
        elif notify_events and observation["attention"]:
            attention_identity = [{key: item.get(key) for key in
                ("kind", "metric", "epoch", "op", "threshold")}
                for item in observation["attention"]]
            attention_digest = hashlib.sha256(json.dumps(attention_identity, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]
            if observation["checkpoint_due"]:
                slot = int(observation["due_checkpoint_at"])
                event_specs.append(("attention", f"attention-checkpoint:{identity}:{slot}:{attention_digest}"))
            else:
                event_specs.append(("attention", f"attention:{identity}:{attention_digest}"))
        elif notify_events and observation["checkpoint_due"] and not final:
            slot = int(observation["due_checkpoint_at"])
            event_specs.append(("checkpoint", f"checkpoint:{identity}:{slot}"))
        seen = set(previous.get("event_keys", []))
        outbox = list(previous.get("outbox", []))
        known = {item.get("key") for item in outbox if isinstance(item, dict)}
        new_event = False
        for event_kind, event_key in event_specs:
            if event_key in seen or event_key in known:
                continue
            new_event = True
            notice_texts = _notice_texts({**run, "row_id": str(identity)}, event_kind,
                observation, record_locator, sources or previous.get("sources", []))
            outbox.append({"key": event_key, "kind": event_kind,
                           "recipients": _notice_targets(run),
                           "texts": notice_texts, "text": notice_texts[0]})
            known.add(event_key)
        next_state = {"schema_version": OBSERVATION_VERSION, "run_id": run_id,
                      "row_key": str(identity), "cursor": cursor, "samples": samples,
                      "sources": sources,
                      "schedule_started_at": schedule_started,
                      "clock_scope": "row" if clock_known else "unknown",
                      "row_started_at": float(started) if clock_known else None,
                      "last_observer_poll_at": float(now),
                      "last_observed_at": float(now) if should_read_sources else last_observed_at,
                      "progress_sample": next_progress or previous.get("progress_sample"),
                      "progress_at": (float(next_progress["updated_at"])
                                      if next_progress and isinstance(next_progress.get("updated_at"), (int, float))
                                      and not isinstance(next_progress.get("updated_at"), bool)
                                      else float(now) if next_progress else previous.get("progress_at")),
                      "last_checkpoint_at": (float(now) if observation["checkpoint_due"]
                                             else previous.get("last_checkpoint_at")),
                      "last_public_write_at": previous.get("last_public_write_at"),
                      "public_status": previous.get("public_status"),
                      "last_delivery_at": previous.get("last_delivery_at"),
                      "observation": observation,
                      "warnings": sorted(set([*previous.get("warnings", []), *warnings]))[-64:],
                      "event_keys": list(seen)[-256:], "outbox": outbox[-32:]}
        record_path = None
        publish_public = bool(artifact_root and (
            previous.get("last_public_write_at") is None or final
            or status != previous.get("public_status")
            or observation["checkpoint_due"] or new_event
            or bool(new_samples) or sources != previous.get("sources", [])))
        if publish_public:
            record = public_run_record(run, status=status, observation=observation,
                metrics=next_state["sources"], started_at=run.get("started_at"),
                ended_at=(run.get("ended_at") if run.get("ended_at") is not None else now)
                    if final else None)
            record["source_warnings"] = sorted(set(warnings))
            row_entry = {"row_id": str(identity),
                         "started_at": float(started) if clock_known else None,
                         "clock_scope": "row" if clock_known else "unknown",
                         "output_dir": str(metric_root) if metric_root else None,
                         "metrics": next_state["sources"],
                         "last_checkpoint": record["last_checkpoint"],
                         "metric_summary": record["metric_summary"],
                         "elapsed_seconds": observation.get("elapsed_seconds"),
                         "ended_at": (run.get("ended_at") if run.get("ended_at") is not None else now)
                            if final else None,
                         "planned_seconds": observation.get("planned_seconds"),
                         "eta": observation.get("eta_seconds")}
            record["rows"] = [row_entry]
            if metric_root is not None:
                record["output_dir"] = str(metric_root)
            record_path = write_public_run_record(artifact_root, run_id, record)
            if record_path:
                next_state["last_public_write_at"] = float(now)
                next_state["public_status"] = status
        state_changed = (not previous or new_event or should_read_sources
                         and (cursor != previous.get("cursor") or bool(new_samples)
                              or sources != previous.get("sources", []))
                         or next_progress != previous.get("progress_sample")
                         or observation["checkpoint_due"] or final
                         or warnings != []
                         or next_state["last_observer_poll_at"] != previous_poll)
        if record_path or state_changed:
            if record_path:
                next_state["last_public_write_at"] = float(now)
                next_state["public_status"] = status
            _write_observation_state(state_path, next_state)
        delivery = []
        received_events = set()
        should_deliver = (not isinstance(previous.get("last_delivery_at"), (int, float))
                          or float(now) - float(previous.get("last_delivery_at", 0)) >= 60.0
                          or new_event)
        if notify_events and outbox and should_deliver:
            try:
                from session_notice import notify
                attempted_delivery = False
                for event in outbox:
                    delivered_to = []
                    targets = event.get("recipients") if isinstance(event.get("recipients"), list) else []
                    if not targets:
                        delivery.append({"key": event.get("key"),
                                         "state": "recipient-unknown"})
                        continue
                    all_received = True
                    texts = event.get("texts") if isinstance(event.get("texts"), list) else [event.get("text", "")]
                    for part_index, part_text in enumerate(texts, 1):
                        for target in targets:
                            if isinstance(target, dict):
                                harness, session_id = target.get("harness"), target.get("session_id")
                                lineage = target.get("lineage")
                            else:
                                harness, session_id = target
                                lineage = None
                            try:
                                receipt = notify(harness, session_id,
                                    key=f"experiment:{run_id}:{event['key']}:{part_index}:{harness}:{session_id}",
                                    subject=f"experiment {event['kind']} ({part_index}/{len(texts)})",
                                    text=part_text, route_id=run.get("route_id") or "route-free",
                                    required_action="read-notice", lineage=lineage,
                                    jobs=(run.get("jobs")
                                          or (run.get("owner_wait") or {}).get("jobs")
                                          or os.environ.get("AGENT_DISPATCH_JOBS")))
                                state = receipt.get("state") if isinstance(receipt, dict) else "unavailable"
                            except (OSError, ValueError, TypeError, RuntimeError):
                                receipt, state = None, "unavailable"
                            attempted_delivery = True
                            delivered_to.append({"harness": harness, "session_id": session_id,
                                                 "part": part_index, "state": state})
                            all_received = all_received and state == "acked"
                    delivery.append({"key": event["key"], "recipients": delivered_to})
                    if delivered_to and all_received:
                        received_events.add(event["key"])
                if attempted_delivery:
                    next_state["last_delivery_at"] = float(now)
                    _write_observation_state(state_path, next_state)
            except (OSError, ValueError, TypeError, RuntimeError):
                delivery.append({"state": "pending-delivery-unavailable"})
        if received_events:
            next_state["event_keys"] = list(dict.fromkeys(
                [*next_state.get("event_keys", []), *sorted(received_events)]))[-256:]
            next_state["outbox"] = [event for event in next_state.get("outbox", [])
                                    if event.get("key") not in received_events]
            _write_observation_state(state_path, next_state)
        return {"available": True, "observation": observation,
                "metrics": next_state["sources"], "metric_root": str(metric_root) if metric_root else None,
                "record_path": str(record_path) if record_path else None,
                "delivery": delivery,
                "warnings": sorted(set(warnings)), "state_path": str(state_path)}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def public_record_path(artifact_root, run_id):
    raw_root = Path(artifact_root).expanduser()
    if not raw_root.is_absolute() or raw_root.is_symlink():
        return None
    current = Path(raw_root.anchor)
    for part in raw_root.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return None
        except FileNotFoundError:
            break
    root = raw_root.resolve(strict=False)
    leaf = run_id if isinstance(run_id, str) and _SAFE_RUN_ID.fullmatch(run_id) else None
    if not leaf:
        return None
    return root / "resource-runs" / (leaf + ".json")


def write_public_run_record(artifact_root, run_id, record):
    """Atomically replace one canonical-root public record; unknowns remain null."""
    path = public_record_path(artifact_root, run_id)
    if path is None or not isinstance(record, dict):
        return None
    value = dict(record)
    value["run_id"] = run_id
    value.setdefault("metrics", [])
    value.setdefault("last_checkpoint", {"at": None, "trend": None,
                                         "rule_fired": [], "eta_vs_plan": None})
    name = None
    directory_fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            return None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0))
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        existing = read_public_run_record(artifact_root, run_id)
        if isinstance(existing, dict):
            identity_fields = ("campaign_id", "cycle_id", "route_id", "node", "host",
                               "output_dir", "started_at", "verdict")
            for key in identity_fields:
                if value.get(key) in (None, "") and existing.get(key) not in (None, ""):
                    value[key] = existing[key]
            metric_rows = {}
            for item in [*(existing.get("metrics") or []), *(value.get("metrics") or [])]:
                if not isinstance(item, dict) or not isinstance(item.get("format"), str):
                    continue
                source_key = (item["format"], json.dumps(item.get("path"), sort_keys=True))
                previous_item = metric_rows.get(source_key, {})
                metric_rows[source_key] = {**previous_item, **item,
                    "keys": sorted(set(previous_item.get("keys", [])) | set(item.get("keys", [])))}
            value["metrics"] = list(metric_rows.values())
            row_map = {row.get("row_id"): row for row in existing.get("rows", [])
                       if isinstance(row, dict) and isinstance(row.get("row_id"), str)}
            row_map.update({row.get("row_id"): row for row in value.get("rows", [])
                            if isinstance(row, dict) and isinstance(row.get("row_id"), str)})
            value["rows"] = list(row_map.values())
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_PUBLIC_RUN_RECORD_BYTES:
            return None
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(encoded + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        os.fsync(directory_fd)
        return path
    except (OSError, ValueError, TypeError, OverflowError):
        return None
    finally:
        if name:
            try: os.unlink(name)
            except OSError: pass
        if directory_fd is not None:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
                os.close(directory_fd)
            except OSError:
                pass


def read_public_run_record(artifact_root, run_id):
    path = public_record_path(artifact_root, run_id)
    if path is None:
        return None
    try:
        root = Path(path.parent.parent).resolve(strict=True)
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_PUBLIC_RUN_RECORD_BYTES:
                return None
            raw = source.read(MAX_PUBLIC_RUN_RECORD_BYTES + 1)
            after = os.fstat(source.fileno())
        if (len(raw) > MAX_PUBLIC_RUN_RECORD_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return None
        value = _json(raw)
        return value if value.get("run_id") == run_id else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def environment(run):
    """Bind the payload to its own row, replacing any inherited resource context."""
    return {
        "AGENT_RESOURCE_PROGRESS_FILE": str(run.get("progress_file") or ""),
        "AGENT_RESOURCE_RUN_ID": str(run.get("run_id") or ""),
        "AGENT_RESOURCE_NODE": str(run.get("node") or ""),
    }


def _valid_progress(value, run_id, node):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or not isinstance(run_id, str) or not run_id or not isinstance(node, str) or not node
            or value.get("run_id") != run_id or value.get("node") != node
            or not _count(value.get("completed"))):
        return False
    updated = value.get("updated_at")
    if (not isinstance(updated, (int, float)) or isinstance(updated, bool)
            or not math.isfinite(updated) or updated < 0):
        return False
    for name, maximum in (("unit", 40), ("detail", 240)):
        if name == "detail" and name not in value:
            continue
        text = value.get(name)
        if (not isinstance(text, str) or not text.strip() or len(text) > maximum
                or any(ord(char) < 32 or ord(char) == 127 or char in "\x85\u2028\u2029" for char in text)):
            return False
    return "total" not in value or (_count(value["total"]) and value["completed"] <= value["total"])


def write_progress(completed, unit, *, total=None, detail=None):
    """Atomically publish schema v1 {run_id,node,updated_at,completed,unit,…}.

    Counts are non-negative integers (booleans are not counts); optional total
    bounds completed. Text is short and single-line. The optional observation
    is best effort: no environment, invalid input or an I/O failure returns
    False, without interrupting the workload. Exit/sentinel still own success.
    """
    raw_path = os.environ.get("AGENT_RESOURCE_PROGRESS_FILE", "")
    run_id = os.environ.get("AGENT_RESOURCE_RUN_ID", "")
    node = os.environ.get("AGENT_RESOURCE_NODE", "")
    path = Path(raw_path)
    value = {"schema_version": 1, "run_id": run_id, "node": node,
             "updated_at": time.time(), "completed": completed, "unit": unit}
    if total is not None:
        value["total"] = total
    if detail is not None:
        value["detail"] = detail
    if not raw_path or not path.is_absolute() or not _valid_progress(value, run_id, node):
        return False
    name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass


def read_progress(run, now):
    """One declared regular file, at most MAX_BYTES, exact run/node; fail soft.

    updated_at is a Unix timestamp; age_s is observation age, not a liveness or
    completion verdict. Old but valid counters remain visible with their age.
    """
    try:
        raw_path = run.get("progress_file")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            return None
        raw, _mtime = _read(path, Path(path.anchor))
        value = _json(raw)
        if not _valid_progress(value, run.get("run_id"), run.get("node")):
            return None
        keys = ("schema_version", "run_id", "node", "updated_at", "completed", "unit", "total", "detail")
        return {**{key: value[key] for key in keys if key in value},
                "age_s": max(0.0, float(now) - value["updated_at"])}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path: Path, root: Path):
    """One regular, root-contained file; reject links, swaps and growing reads."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("path-traversal")
    relative = path.relative_to(root)
    parents = []
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("progress-link")
        parents.append((current, info.st_dev, info.st_ino))
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        first = os.fstat(stream.fileno())
        if not stat.S_ISREG(first.st_mode) or first.st_size > MAX_BYTES:
            raise ValueError("progress-not-bounded-file")
        data = stream.read(MAX_BYTES + 1)
        last = os.fstat(stream.fileno())
    if len(data) > MAX_BYTES or _signature(first) != _signature(last) \
            or _signature(last) != _signature(path.stat()):
        raise ValueError("progress-changed-during-read")
    for parent, device, inode in parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
            raise ValueError("progress-path-changed")
    return data, last.st_mtime


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-progress-key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("nonfinite-progress")

    value = json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError("progress-not-object")
    return value


def observe_process(pid):
    """Stable same-EUID procfs observation, including the actual argv and cwd."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        root = Path("/proc") / str(pid)
        if root.stat().st_uid != os.geteuid():
            return None
        before = (root / "stat").read_bytes()
        fields = before.rsplit(b") ", 1)[1].split()
        raw = (root / "cmdline").read_bytes()
        cwd = os.readlink(root / "cwd")
        after = (root / "stat").read_bytes().rsplit(b") ", 1)[1].split()
        if not raw or len(raw) > MAX_BYTES or fields[19] != after[19] or fields[2] != after[2]:
            return None
        if root.stat().st_uid != os.geteuid() or (root / "cmdline").read_bytes() != raw \
                or os.readlink(root / "cwd") != cwd:
            return None
        return {"pid": pid, "starttime": fields[19].decode("ascii"),
                "command_hash": hashlib.sha256(raw).hexdigest(),
                "process_group": int(fields[2]), "cwd": cwd,
                "argv": [part.decode("utf-8", "strict") for part in raw.rstrip(b"\0").split(b"\0")]}
    except (OSError, ValueError, TypeError, IndexError, UnicodeError):
        return None


def _matches(actual, expected):
    return isinstance(actual, dict) and isinstance(expected, dict) and all(
        expected.get(key) not in (None, "")
        and str(actual.get(key)) == str(expected[key])
        for key in ("pid", "starttime", "command_hash"))


def _config_argument(argv):
    refs = []
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            refs.append(argv[index + 1])
        elif arg.startswith("--config="):
            refs.append(arg[len("--config="):])
    return refs[0] if len(refs) == 1 and refs[0] else None


def discover_output_root(run):
    """Resolve only explicit output-directory keys in an exact run config."""
    if not isinstance(run, dict):
        return None
    override = run.get("observation")
    namespace = run.get("path_namespace")
    raw_run_root = run.get("run_root")
    if isinstance(override, dict) and isinstance(override.get("path"), str):
        candidate = Path(override["path"])
        candidate, mapped = _reverse_namespace_path(candidate, namespace)
        shared = False
        if raw_run_root:
            shared_root, _ = _reverse_namespace_path(Path(raw_run_root), namespace)
            try: shared = candidate.is_relative_to(shared_root)
            except ValueError: shared = False
        if candidate.is_absolute() and (mapped or not namespace or shared) and not _path_has_symlink(candidate):
            try:
                resolved = candidate.resolve(strict=True)
                return str(resolved) if resolved.is_dir() else None
            except (OSError, RuntimeError, ValueError):
                return None
    command = run.get("command")
    cwd = run.get("cwd")
    reference = _config_argument(command) if isinstance(command, list) else None
    if not reference or not isinstance(cwd, str) or not cwd:
        return None
    try:
        raw_cwd = Path(cwd)
        mapped_cwd, _cwd_mapped = _reverse_namespace_path(raw_cwd, namespace)
        base = mapped_cwd.resolve(strict=True)
        config_ref = Path(reference)
        if config_ref.is_absolute():
            config_ref, _config_mapped = _reverse_namespace_path(config_ref, namespace)
            reference = str(config_ref)
        resolved = resolve_config(base, reference)
        config_path = Path(resolved["path"])
        raw, _ = _read_unrooted(config_path)
        expected = run.get("config_sha256")
        digest = hashlib.sha256(raw).hexdigest()
        if expected and digest != _hash(expected):
            return None
        if config_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
                config = yaml.safe_load(raw)
            except (ImportError, ValueError, TypeError):
                return None
        else:
            config = _json(raw)
        if not isinstance(config, dict):
            return None
        training = config.get("training") if isinstance(config.get("training"), dict) else {}
        candidates = []
        for value in (config.get("output_dir"), config.get("output_folder"),
                      config.get("save_dir"), training.get("output_dir")):
            if value is None:
                continue
            if not isinstance(value, str) or not value or len(value) > 4096 \
                    or any(ord(char) < 32 for char in value):
                return None
            raw_path = Path(value)
            candidate = raw_path if raw_path.is_absolute() else base / raw_path
            mapped = False
            if raw_path.is_absolute():
                candidate, mapped = _reverse_namespace_path(candidate, namespace)
                if namespace and not mapped:
                    shared_root = None
                    if raw_run_root:
                        shared_root, _ = _reverse_namespace_path(Path(raw_run_root), namespace)
                    try: shared = shared_root is not None and candidate.is_relative_to(shared_root)
                    except ValueError: shared = False
                    if not shared:
                        return None
            if _path_has_symlink(candidate):
                return None
            candidates.append(candidate)
        unique = {str(path.resolve(strict=True)) for path in candidates}
        if len(unique) != 1:
            return None
        output = Path(next(iter(unique)))
        return str(output) if output.is_dir() else None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError,
            subprocess.SubprocessError, OverflowError):
        return None


def _declared_plan(run):
    observation = run.get("observation") if isinstance(run.get("observation"), dict) else {}
    value = run.get("planned_seconds")
    if value is None:
        value = run.get("time_budget", observation.get("time_budget"))
    if isinstance(value, dict):
        value = value.get("planned_seconds", value.get("seconds"))
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(float(value)) and value > 0:
        return float(value)
    return None


def _apply_declared_metric_keys(run, samples, sources):
    observation = run.get("observation") if isinstance(run.get("observation"), dict) else {}
    declared = observation.get("keys")
    if isinstance(declared, list) and all(isinstance(key, str) and key for key in declared):
        allowed = set(declared)
    elif isinstance(declared, dict):
        allowed = {key for values in declared.values()
                   for key in (values if isinstance(values, list) else [values])
                   if isinstance(key, str) and key}
    else:
        return samples, sources
    filtered = []
    for row in samples:
        metrics = {key: value for key, value in row.get("metrics", {}).items() if key in allowed}
        if metrics:
            filtered.append({**row, "metrics": metrics})
    return filtered, [{**source, "keys": sorted(set(source.get("keys", [])) & allowed)}
                      for source in sources]


def resolve_config(cwd, reference):
    """Reuse the normal resolver's CLI, keeping its failures off the Fleet TUI."""
    tool = Path(__file__).resolve().parents[1] / "tools" / "lab-config-provenance.py"
    result = subprocess.run(
        [sys.executable, "-B", str(tool), "resolve", "--repo", str(cwd), "--ref", reference],
        capture_output=True, timeout=2, check=False,
    )
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ValueError("config-reference-unresolved")
    return _json(result.stdout)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63


def _hash(value):
    return value.removeprefix("sha256:") if isinstance(value, str) else None


def _schedule_epoch(training, attempt, total):
    """Optional schedule intervals, derived only from an explicit exact cadence."""
    epochs, blocks, updates = (training.get(key) for key in
                               ("epochs", "blocks_per_epoch", "updates_per_block"))
    if not all(_count(value) and value > 0 for value in (epochs, blocks, updates)):
        return None
    width = blocks * updates
    if epochs * width != total:
        return None
    completed, position = divmod(attempt, width)
    state = ("start" if attempt == 0 else "complete" if attempt == total
             else "boundary" if position == 0 else "partial")
    return {"current": completed + int(position > 0), "total": epochs,
            "attempts_per_epoch": width, "completed": completed,
            "attempt_in_epoch": position, "state": state}


def _arm_production_view(arm):
    """Split one arm into local-verifiable fields plus production origin.

    Bridge arms nest identity/directory/config inside ``arm["production"]``;
    plain arms keep them top-level. In both shapes an explicit ``host`` (or
    ``compute_host_receipt.host``) marks remote origin. Returns ``(fields,
    remote)``; ``remote`` is True when such a host is present. Remote arms
    must never be resolved in local /proc and their metadata alone never
    proves live/done, so the caller skips them without ``process_reader``. The
    embedded ``production.progress`` counters are never consumed here;
    attempted/phase summary stays with oc-attempt-summary.
    """
    if not isinstance(arm, dict):
        return None, False
    production = arm.get("production")
    if isinstance(production, dict):
        identity = production.get("process_identity")
        if not isinstance(identity, dict):
            return None, False
        state = arm.get("state")
        if not isinstance(state, str):
            state = production.get("state")
        name = arm.get("name")
        if not isinstance(name, str):
            name = production.get("run_name")
        if not isinstance(name, str):
            name = None
        host = production.get("host")
        if not isinstance(host, str) or not host:
            receipt = arm.get("compute_host_receipt")
            host = receipt.get("host") if isinstance(receipt, dict) else None
            if not isinstance(host, str) or not host:
                host = None
        origin = {}
        for key in ("schema", "host", "compute_run_id", "capacity", "gpu_index"):
            value = production.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) \
                    and value != "":
                origin[key] = value
        run_id = arm.get("run_id")
        if isinstance(run_id, str) and run_id:
            origin["run_id"] = run_id
        return {"identity": identity, "directory": production.get("directory"),
                "config_sha256": production.get("config_sha256"),
                "state": state, "name": name,
                "started_at": production.get("started_at", arm.get("started_at")),
                "origin": origin or None}, \
            host is not None
    if not isinstance(arm.get("process_identity"), dict):
        return None, False
    host = arm.get("host")
    if not isinstance(host, str) or not host:
        receipt = arm.get("compute_host_receipt")
        host = receipt.get("host") if isinstance(receipt, dict) else None
        if not isinstance(host, str) or not host:
            host = None
    return {"identity": arm.get("process_identity"),
            "directory": arm.get("directory"),
            "config_sha256": arm.get("config_sha256"),
            "state": arm.get("state"), "name": arm.get("name"),
            "started_at": arm.get("started_at"),
            "origin": None}, host is not None


def collect(run, registry, now, process_reader=observe_process, config_resolver=resolve_config):
    """Return one optional active-arm observation; unknown or ambiguous shapes fail soft."""
    try:
        wrapper = process_reader(run.get("pid"))
        if not _matches(wrapper, run) or wrapper["process_group"] != run.get("process_group"):
            return None
        root = Path(registry).parent.resolve(strict=True)
        metadata_bytes, metadata_mtime = _read(root / "run.json", root)
        metadata = _json(metadata_bytes)
        arms = metadata.get("arms", [metadata])
        if not isinstance(arms, list) or not 0 < len(arms) <= MAX_ARMS:
            return None
        candidates = []
        for arm in arms:
            view, remote = _arm_production_view(arm)
            if view is None:
                continue
            if remote:
                # Remote-origin production: never resolve its PID in local
                # /proc and never treat its metadata as live/done proof.
                continue
            identity = view["identity"]
            expected = {**identity, "command_hash": identity.get("command_hash")
                        or identity.get("cmdline_sha256")}
            child = process_reader(identity.get("pid"))
            if _matches(child, expected) and child["process_group"] == wrapper["process_group"]:
                candidates.append((view, child))
        if len(candidates) != 1:
            return None
        view, child = candidates[0]
        if Path(child["cwd"]).resolve(strict=True) != Path(run["cwd"]).resolve(strict=True):
            return None
        directory = view["directory"]
        if not isinstance(directory, str) or not directory:
            return None
        directory = Path(directory)
        if not directory.is_absolute():
            directory = root / directory
        if _path_has_symlink(directory):
            return None
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            return None
        progress_path = directory / "progress.json"
        data, progress_mtime = _read(progress_path, directory)
        progress = _json(data)
        attempt, successful, skipped = (progress.get(key) for key in ("attempt", "successful", "skipped"))
        if not all(_count(value) for value in (attempt, successful, skipped)) \
                or attempt != successful + skipped:
            return None
        reference = _config_argument(child["argv"])
        if reference is None:
            return None
        resolved = config_resolver(child["cwd"], reference)
        config_path = Path(resolved["path"])
        config_bytes, _ = _read(config_path, Path(child["cwd"]).resolve(strict=True))
        digest = hashlib.sha256(config_bytes).hexdigest()
        if digest != _hash(view["config_sha256"]):
            return None
        config = _json(config_bytes)
        training = config.get("training")
        total = training.get("attempts") if isinstance(training, dict) else None
        if not _count(total) or total <= 0 or attempt > total:
            return None
        epoch = _schedule_epoch(training, attempt, total)
        last = progress.get("last")
        loss = None
        if isinstance(last, dict) and _count(last.get("attempt")) and last["attempt"] == attempt:
            metrics = last.get("metrics")
            value = metrics.get("loss") if isinstance(metrics, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                loss = value
        # No read may outlive the exact wrapper/child tuple it was attributed to.
        if process_reader(run["pid"]) != wrapper or process_reader(child["pid"]) != child:
            return None
        return {
            "pid": child["pid"], "starttime": child["starttime"],
            "command_hash": child["command_hash"], "process_group": child["process_group"],
            "phase": view["state"], "arm": view["name"],
            "attempt": attempt, "attempt_total": total,
            "percent": attempt * 100.0 / total, "successful": successful, "skipped": skipped,
            "loss": loss, "loss_kind": "last-batch" if loss is not None else None,
            "observed_at": float(now), "progress_updated_at": progress_mtime,
            "progress_age_s": max(0.0, float(now) - progress_mtime),
            "metadata_path": str(root / "run.json"),
            "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
            "metadata_updated_at": metadata_mtime,
            "progress_path": str(progress_path), "progress_sha256": hashlib.sha256(data).hexdigest(),
            "config_path": str(config_path), "config_ref": resolved["config_ref"],
            "config_sha256": digest,
            "directory": str(directory), "row_started_at": view.get("started_at"),
            **({"production_origin": view["origin"]} if view["origin"] is not None else {}),
            **({"schedule_epoch": epoch} if epoch is not None else {}),
        }
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError,
            OverflowError, subprocess.SubprocessError):
        return None


def _read_unrooted(path):
    """Bounded regular-file read without a containing root.

    Display-only path for remote production configs, which live outside the
    run root. Same size/stability guards as `_read`, no root pinning, no
    liveness meaning: hash equality against the producer declaration is still
    required by the caller.
    """
    raw = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(raw, "rb") as stream:
        first = os.fstat(stream.fileno())
        if not stat.S_ISREG(first.st_mode) or first.st_size > MAX_BYTES:
            raise ValueError("remote-not-bounded-file")
        data = stream.read(MAX_BYTES + 1)
        last = os.fstat(stream.fileno())
    if len(data) > MAX_BYTES or _signature(first) != _signature(last):
        raise ValueError("remote-changed-during-read")
    return data, last.st_mtime


def remote_candidates(run, registry, now):
    """Display-only remote production arms; never a liveness verdict.

    Preserves the indexed bridge's recognized remote arms as joinable display
    candidates without any local /proc lookup and without treating metadata
    as live/done proof. Each candidate binds the producer-declared identity
    (host-qualified pid/start/command hash), the NAS-readable progress file
    under the run root, and the hash-checked config outside it. Anything
    missing, mismatched, or raced fails soft to `[]`; the caller joins each
    candidate to its own host/GPU independently, never through the local
    single-candidate shape. `run` reserves wrapper context and is unused.
    """
    found = []
    try:
        root = Path(registry).parent.resolve(strict=True)
        metadata_bytes, metadata_mtime = _read(root / "run.json", root)
        metadata = _json(metadata_bytes)
        arms = metadata.get("arms", [metadata])
        if not isinstance(arms, list) or not 0 < len(arms) <= MAX_ARMS:
            return []
        for arm in arms:
            try:
                view, remote = _arm_production_view(arm)
                if view is None or not remote:
                    continue
                identity = view["identity"]
                pid = identity.get("pid")
                starttime = identity.get("starttime")
                command_hash = identity.get("command_hash") \
                    or identity.get("cmdline_sha256")
                host = (view["origin"] or {}).get("host")
                if not isinstance(host, str) or not host:
                    receipt = arm.get("compute_host_receipt") \
                        if isinstance(arm, dict) else None
                    host = receipt.get("host") if isinstance(receipt, dict) else None
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 \
                        or starttime in (None, "") \
                        or not isinstance(command_hash, str) or not command_hash \
                        or not isinstance(host, str) or not host:
                    continue
                directory = view["directory"]
                if not isinstance(directory, str) or not directory:
                    continue
                directory = Path(directory)
                if not directory.is_absolute():
                    directory = root / directory
                progress_path = directory / "progress.json"
                data, progress_mtime = _read(progress_path, root)
                progress = _json(data)
                attempt, successful, skipped = (
                    progress.get(key) for key in ("attempt", "successful", "skipped"))
                if not all(_count(value) for value in (attempt, successful, skipped)) \
                        or attempt != successful + skipped:
                    continue
                production = arm.get("production") if isinstance(arm, dict) else None
                argv = production.get("command") if isinstance(production, dict) else None
                if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
                    continue
                reference = _config_argument(argv)
                if reference is None:
                    continue
                config_path = Path(reference)
                if not config_path.is_absolute():
                    continue
                config_bytes, _ = _read_unrooted(config_path)
                digest = hashlib.sha256(config_bytes).hexdigest()
                if digest != _hash(view["config_sha256"]):
                    continue
                config = _json(config_bytes)
                training = config.get("training")
                total = training.get("attempts") if isinstance(training, dict) else None
                if not _count(total) or total <= 0 or attempt > total:
                    continue
                epoch = _schedule_epoch(training, attempt, total)
                last = progress.get("last")
                loss = None
                if isinstance(last, dict) and _count(last.get("attempt")) \
                        and last["attempt"] == attempt:
                    metrics = last.get("metrics")
                    value = metrics.get("loss") if isinstance(metrics, dict) else None
                    if isinstance(value, (int, float)) and not isinstance(value, bool) \
                            and math.isfinite(value):
                        loss = value
                found.append({
                    "remote": True, "host": host,
                    "pid": pid, "starttime": starttime, "command_hash": command_hash,
                    "phase": view["state"], "arm": view["name"],
                    "attempt": attempt, "attempt_total": total,
                    "percent": attempt * 100.0 / total,
                    "successful": successful, "skipped": skipped,
                    "loss": loss, "loss_kind": "last-batch" if loss is not None else None,
                    "observed_at": float(now), "progress_updated_at": progress_mtime,
                    "progress_age_s": max(0.0, float(now) - progress_mtime),
                    "metadata_path": str(root / "run.json"),
                    "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                    "metadata_updated_at": metadata_mtime,
                    "progress_path": str(progress_path),
                    "progress_sha256": hashlib.sha256(data).hexdigest(),
                    "config_path": str(config_path),
                    "config_ref": "config:" + config_path.name,
                    "config_sha256": digest,
                    "directory": str(directory),
                    "row_started_at": (production.get("started_at")
                                       if isinstance(production, dict) else None),
                    "production_origin": view["origin"],
                    "compute_run_id": (view["origin"] or {}).get("compute_run_id"),
                    "run_id": (view["origin"] or {}).get("run_id"),
                    "capacity": (view["origin"] or {}).get("capacity"),
                    "gpu_index": (view["origin"] or {}).get("gpu_index"),
                    **({"schedule_epoch": epoch} if epoch is not None else {}),
                })
            except (OSError, ValueError, TypeError, KeyError, IndexError,
                    OverflowError):
                continue
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return []
    return found
