"""Optional, bounded resource observations; they never decide run lifecycle.

The counter reader uses the producer-declared file. Admission reads are
separate: they use only shallow roots already supplied by exact run, registry,
output, or mapped log provenance, and bind records by content. Neither reader
imports a producer, searches a cwd/project tree, writes a registry, or changes
the registered process lifecycle. A missing or invalid optional observation
remains unknown.
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
# Private read limit only (observation state and older public records); the
# public record is written under MAX_PUBLIC_RUN_RECORD_WRITE_BYTES.
MAX_PUBLIC_RUN_RECORD_BYTES = 4 * 1024 * 1024
MAX_PUBLIC_RUN_RECORD_WRITE_BYTES = 16 * 1024
MAX_PUBLIC_METRIC_DECLARATIONS = 16
MAX_ROUTE_FILE_BYTES = 1024 * 1024
MAX_SOURCE_BINDINGS = 8
MAX_SOURCE_BINDING_BYTES = 64 * 1024
MAX_SOURCE_BINDING_PROOFS = 8
MAX_SOURCE_BINDING_COUNTERS = 8
MAX_SOURCE_BINDING_ASSERTIONS = 8
MAX_ARMS = 32
OBSERVATION_VERSION = 2
CHECKPOINT_INTERVAL = 12 * 60 * 60
# While its observer has just seen the run alive (`observe_experiment(alive=True)`), the public
# record is written at least this often even with nothing new, so its observed time says the
# observer is watching. Readers call a running record older than 900 s stale
# (`experiment_catalog.STALE_OBSERVATION_SECONDS`); a third of that leaves room for the 60 s look.
PUBLIC_REFRESH_SECONDS = 300
# A progress value or metric sample at most this old is fresh enough for a rate or an ETA. An older
# valid value is still the run's last progress: it keeps the stagnation clock, never resets it.
PROGRESS_FRESH_SECONDS = 600
# The time rule: a predicted (or, unmeasured, elapsed) total of 1.5x the plan, or past
# six hours without a plan, is an attention.
ETA_ATTENTION_RATIO = 1.5
ETA_ATTENTION_UNPLANNED_SECONDS = 6 * 3600
ETA_ATTENTION_KINDS = frozenset({"eta-over-plan", "eta-over-six-hours"})
ATTENTION_IDENTITY_KEYS = ("kind", "metric", "epoch", "op", "threshold", "episode_id",
                           "generation", "since", "source_value_hash")
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
    progress_fresh = True
    if isinstance(progress, dict):
        if (isinstance(progress.get("age_s"), (int, float))
                and not isinstance(progress.get("age_s"), bool)
                and math.isfinite(float(progress["age_s"]))):
            progress_age = float(progress["age_s"])
            progress_fresh = 0 <= progress_age <= 600
        elif (isinstance(progress.get("updated_at"), (int, float))
              and not isinstance(progress.get("updated_at"), bool)
              and math.isfinite(float(progress["updated_at"]))):
            progress_age = now - float(progress["updated_at"])
            progress_fresh = 0 <= progress_age <= 600
    if (type(reached) is int and reached >= 0
            and (total is None or type(total) is int and total > 0 and reached <= total)
            and isinstance(previous_progress, dict)
            and type(previous_progress.get("completed")) is int
            and previous_progress.get("unit") == unit
            and previous_progress.get("total") == total
            and isinstance(previous_progress_at, (int, float))
            and not isinstance(previous_progress_at, bool) and progress_fresh
            and progress.get("_comparison_valid", True) is not False):
        delta = reached - previous_progress["completed"]
        duration = now - float(previous_progress_at)
        if delta > 0 and 0 < duration <= 600:
            rate = delta / duration
            if type(total) is int and total > 0 and reached <= total:
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
    if rate is not None and predicted_total is not None:
        if planned_seconds and predicted_total is not None \
                and predicted_total >= ETA_ATTENTION_RATIO * float(planned_seconds):
            attention.append({"kind": "eta-over-plan", "predicted_total_seconds": predicted_total})
        elif not planned_seconds and predicted_total is not None \
                and predicted_total > ETA_ATTENTION_UNPLANNED_SECONDS:
            attention.append({"kind": "eta-over-six-hours", "predicted_total_seconds": predicted_total})
    elif planned_seconds and elapsed >= ETA_ATTENTION_RATIO * float(planned_seconds):
        attention.append({"kind": "elapsed-over-plan", "elapsed_seconds": elapsed})
    elif not planned_seconds and elapsed > ETA_ATTENTION_UNPLANNED_SECONDS:
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


def eta_attention_episode(observation, previous, now):
    """The episode and size step of an ETA attention, for telling it apart.

    The same ETA attention stands while its prediction stays over the rule's boundary,
    wobbles around it, or goes unmeasured. It ends only when a measured prediction is
    back within the plan (the boundary divided by the rule's own 1.5x; four hours without
    a plan), so a later breach is a new episode (`since`). Within an episode, a
    prediction grown by that same 1.5x over the size it was last marked at is a new
    `step`; smaller changes are the same attention. Returns the tracker to keep (None
    when no episode stands).
    """
    tracker = previous if isinstance(previous, dict) else None
    attention = observation.get("attention") if isinstance(observation.get("attention"), list) else []
    item = next((row for row in attention if isinstance(row, dict)
                 and row.get("kind") in ETA_ATTENTION_KINDS), None)
    if item is None:
        planned = observation.get("planned_seconds")
        predicted = observation.get("predicted_total_seconds")
        boundary = (ETA_ATTENTION_RATIO * float(planned) if planned
                    else ETA_ATTENTION_UNPLANNED_SECONDS)
        if (observation.get("rate_per_second") is not None
                and isinstance(predicted, (int, float)) and not isinstance(predicted, bool)
                and predicted <= boundary / ETA_ATTENTION_RATIO):
            return None
        return tracker
    predicted = float(item["predicted_total_seconds"])
    size = tracker.get("size_seconds") if tracker else None
    if (tracker and tracker.get("kind") == item["kind"]
            and isinstance(tracker.get("since"), (int, float))
            and type(tracker.get("step")) is int
            and isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0):
        since, step, size = float(tracker["since"]), tracker["step"], float(size)
        if predicted >= ETA_ATTENTION_RATIO * size:
            step, size = step + 1, predicted
    else:
        since, step, size = float(now), 0, predicted
    return {"kind": item["kind"], "since": since, "step": step, "size_seconds": size}


def attention_identity(item, eta_episode=None):
    """What makes one attention the same as another (the notice digest's input)."""
    identity = {key: item.get(key) for key in ATTENTION_IDENTITY_KEYS}
    if item.get("kind") in ETA_ATTENTION_KINDS and isinstance(eta_episode, dict):
        identity["since"] = eta_episode.get("since")
        identity["step"] = eta_episode.get("step")
    return identity


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
    source_observations = (observation.get("source_observations")
                           if isinstance(observation.get("source_observations"), list) else [])
    source_descriptors = []
    descriptor_keys = set()
    for item in source_observations:
        if not isinstance(item, dict):
            continue
        descriptor = {key: item.get(key) for key in
            ("source_path", "writer_run_id", "target_run_id", "row_key", "scope",
             "source_generation")}
        descriptor["format"] = "independent-counter"
        descriptor["keys"] = [item.get("name")] if isinstance(item.get("name"), str) else []
        identity = json.dumps(descriptor, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False)
        if identity not in descriptor_keys:
            descriptor_keys.add(identity)
            source_descriptors.append(descriptor)
    result = {
        "schema_version": 1,
        "run_id": run.get("run_id"),
        "observer_of": run.get("observer_of"),
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
                     "unit": observation.get("unit", progress.get("unit")),
                     "observed_counters": observation.get("source_observations", [])},
        "eta": observation.get("eta_seconds"),
        "planned_seconds": observation.get("planned_seconds", run.get("planned_seconds")),
        "metrics": record_metrics,
        "sources": source_descriptors,
        "admission": observation.get("admission"),
        "stagnation": observation.get("stagnation"),
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
    result["output_dir_state"], result["output_dir_reason"] = _public_output_state(
        run, result["output_dir"], binding_given=False)
    try:
        return _compact_public_record(result)
    except (ValueError, TypeError, OverflowError):
        return result


def _public_metric_point(point):
    if not isinstance(point, dict):
        return None
    value = point.get("value")
    finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    return {"epoch": point.get("epoch"), "value": float(value) if finite else None,
            "nonfinite": not finite, "path": point.get("path")}


_TRAINING_OUTPUTS = {"experiment-artifact", "logs/**", "checkpoints/**"}
_ROW_DETAIL_KEYS = ("admission", "stagnation", "observed_counters", "eta", "clock_scope")
_ROW_KEYS = ("row_id", "started_at", "ended_at", "clock_scope", "output_dir",
             "last_checkpoint", "metric_summary", "elapsed_seconds", "planned_seconds",
             "eta", "admission", "stagnation", "observed_counters")
_SKELETON_IDENTITY = ("schema_version", "run_id", "observer_of", "campaign_id", "cycle_id",
                      "route_id", "node", "host", "output_dir", "output_dir_state",
                      "output_dir_reason", "status", "started_at", "ended_at")
_SKELETON_STRING_LIMIT = 600


def _public_output_state(run, output_dir, *, binding_given, discovered_reason=None):
    """Tell present/none/unknown apart; never invents an output folder."""
    if isinstance(output_dir, str) and output_dir:
        return "present", None
    if binding_given:
        return "unknown", "declared-unresolved"
    try:
        route = run.get("route") or run.get("route_file") if isinstance(run, dict) else None
        node_id = (run.get("node") or run.get("route_node")) if isinstance(run, dict) else None
        if isinstance(route, str) and isinstance(node_id, str) and os.path.isabs(route):
            info = os.lstat(route)
            if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_ROUTE_FILE_BYTES:
                fd = os.open(route, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as source:
                    nodes = json.loads(source.read(MAX_ROUTE_FILE_BYTES + 1)).get("nodes")
                for node in nodes if isinstance(nodes, list) else []:
                    if isinstance(node, dict) and node.get("id") == node_id:
                        outputs = node.get("outputs")
                        if (node.get("kind") == "resource-runner" and isinstance(outputs, list)
                                and not _TRAINING_OUTPUTS.intersection(
                                    item for item in outputs if isinstance(item, str))):
                            return "none", "node-declares-no-training-output"
                        break
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # A node without a training output stays `none` above; only then does a folder
    # the run declared on its host but this observer cannot map read as unmapped.
    if isinstance(run, dict) and run.get("output_dir_unmapped") is True:
        return "unknown", "remote-unmapped"
    if discovered_reason in ("declared-unresolved", "remote-unmapped"):
        return "unknown", discovered_reason
    return "unknown", "not-declared"


def _public_metric_declarations(incoming, existing=(), *, check_existing=False):
    """Fold metric declarations to one per (format, location).

    Returns (list, displaced, never_kept): `displaced` counts declarations that were in
    the existing record and are now dropped (overflow or vanished location); they leave
    the record, so they are counted once. `never_kept` counts dropped incoming
    declarations the existing record did not hold; the same input can offer them again
    on every update, so callers must not add it up across writes.
    """
    def fold(items):
        folded = []
        for item in items if isinstance(items, (list, tuple)) else ():
            if not isinstance(item, dict) or not isinstance(item.get("format"), str):
                continue
            path = item.get("path")
            if isinstance(path, list):
                paths = [entry for entry in path if isinstance(entry, str) and entry]
                if item["format"] == "epochs-jsonl":
                    groups = {entry: 1 for entry in paths}
                else:
                    groups = {}
                    for entry in paths:
                        folder = os.path.dirname(entry)
                        groups[folder] = groups.get(folder, 0) + 1
                folded.extend({**item, "path": location, "file_count": count}
                              if item["format"] != "epochs-jsonl"
                              else {**item, "path": location}
                              for location, count in groups.items() if location)
            elif isinstance(path, str) and path:
                folded.append(item)
        return folded

    merged, new_keys = {}, set()
    existing_keys = {(item["format"], item["path"]) for item in fold(existing)}
    for origin, items in (("new", fold(incoming)), ("old", fold(existing))):
        for item in items:
            key = (item["format"], item["path"])
            previous = merged.get(key)
            keys = {name for name in item.get("keys") or ()
                    if isinstance(name, str) and 1 <= len(name) <= 128}
            counts = [value for value in (item.get("file_count"),
                      (previous or {}).get("file_count")) if type(value) is int]
            declaration = {**(previous or {}), **item}
            if keys or "keys" in item or (previous and "keys" in previous):
                declaration["keys"] = sorted(keys | set((previous or {}).get("keys") or ()))
            if counts:
                declaration["file_count"] = max(counts)
            merged[key] = declaration
            if origin == "new":
                new_keys.add(key)
    ordered = [key for key in merged if key in new_keys] + [key for key in merged if key not in new_keys]
    kept, displaced, never_kept = [], 0, 0
    for key in ordered:
        declaration = merged[key]
        if len(kept) >= MAX_PUBLIC_METRIC_DECLARATIONS:
            if key in existing_keys:
                displaced += 1
            else:
                never_kept += 1
            continue
        if check_existing and key not in new_keys:
            try:
                mode = os.lstat(key[1]).st_mode
            except OSError:
                mode = None
            wanted = stat.S_ISREG if key[0] == "epochs-jsonl" else stat.S_ISDIR
            if mode is None or not wanted(mode):
                displaced += 1
                continue
        kept.append(declaration)
    return sorted(kept, key=lambda item: (item["format"], item["path"])), displaced, never_kept


def _public_summary(summary, *, drop_path, max_keys):
    if not isinstance(summary, dict):
        return summary

    def point(value):
        if drop_path and isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "path"}
        return value

    result = dict(summary)
    for key in ("latest", "best"):
        if key in result:
            result[key] = point(result[key])
    for key in ("latest_metrics", "best_metrics"):
        if isinstance(result.get(key), dict):
            names = sorted(result[key], key=lambda name: (name != summary.get("metric"), str(name)))
            result[key] = {name: point(result[key][name]) for name in names[:max_keys]}
    if isinstance(result.get("nonfinite"), list):
        result["nonfinite"] = result["nonfinite"][:4]
    return result


def _public_row(row, *, max_keys=8):
    result = {key: row[key] for key in _ROW_KEYS if key in row}
    if "metric_summary" in result:
        result["metric_summary"] = _public_summary(result["metric_summary"],
                                                   drop_path=True, max_keys=max_keys)
    return result


def _number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) else None)


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")) + 1


def _bounded_scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if _number(value) is not None and len(repr(value)) <= 32 else None
    if isinstance(value, str):
        return value if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) \
            <= _SKELETON_STRING_LIMIT else None
    return None


def _skeleton(value, declarations):
    result, oversize = {}, []
    for key in _SKELETON_IDENTITY:
        if key in value:
            result[key] = _bounded_scalar(value[key])
            if result[key] is None and value[key] is not None:
                oversize.append(key)
    progress = value.get("progress") if isinstance(value.get("progress"), dict) else {}
    result["progress"] = {key: _bounded_scalar(progress.get(key)) for key in ("reached", "total", "unit")}
    summary = value.get("metric_summary") if isinstance(value.get("metric_summary"), dict) else {}
    result["metric_summary"] = {"metric": _bounded_scalar(summary.get("metric")),
                                "trend": _bounded_scalar(summary.get("trend"))}
    for key in ("latest", "best"):
        point = summary.get(key) if isinstance(summary.get(key), dict) else None
        result["metric_summary"][key] = None if point is None else {
            name: _bounded_scalar(point.get(name)) for name in ("epoch", "value", "nonfinite")}
    result["rows"], result["metrics"] = [], []
    result["compaction"] = {}
    return result, oversize


def _compact_public_record(value, existing=None, *, incoming_row_ids=(), check_existing=False):
    """Project a public record into the 16 KiB budget; idempotent and deterministic."""
    value = dict(value)
    existing = existing if isinstance(existing, dict) else {}
    own = value.get("compaction") if isinstance(value.get("compaction"), dict) else {}
    prior = existing.get("compaction") if isinstance(existing.get("compaction"), dict) else {}

    def count(*items):
        return max([item for item in items if type(item) is int and item >= 0] or [0])

    # output folder state: a present folder is carried over a later unknown.
    state, reason = value.get("output_dir_state"), value.get("output_dir_reason")
    if state not in ("present", "none", "unknown"):
        state, reason = (("present", None) if isinstance(value.get("output_dir"), str)
                         and value["output_dir"] else ("unknown", "not-declared"))
    old_dir = existing.get("output_dir")
    old_present = (isinstance(old_dir, str) and old_dir
                   and existing.get("output_dir_state", "present") == "present")
    if state != "present" and old_present:
        value["output_dir"], state, reason = old_dir, "present", None
    if state != "present":
        value["output_dir"] = None
    value["output_dir_state"] = state
    value["output_dir_reason"] = None if state == "present" else reason

    declarations, displaced, never_kept = _public_metric_declarations(
        value.get("metrics"), existing.get("metrics"), check_existing=check_existing)
    value["metrics"] = declarations
    # metrics_omitted: declarations this file has left out. Ones pushed out of the
    # existing record add once; ones merely offered again by the same input do not grow
    # it (the carried count is a floor, raised only by a larger current shortfall).
    carried = count(own.get("metrics_omitted"), prior.get("metrics_omitted"))
    metrics_omitted = displaced + max(carried, never_kept)

    if "source_warnings" in value:
        value["source_warnings"] = sorted(item for item in set(value["source_warnings"] or ())
                                          if isinstance(item, str))[:16]
    if isinstance(value.get("last_checkpoint"), dict) \
            and isinstance(value["last_checkpoint"].get("rule_fired"), list):
        value["last_checkpoint"] = {**value["last_checkpoint"],
                                    "rule_fired": value["last_checkpoint"]["rule_fired"][:16]}
    value["metric_summary"] = _public_summary(value.get("metric_summary"),
                                              drop_path=False, max_keys=16)

    incoming = set(incoming_row_ids)
    by_id = {}
    for row in [*(existing.get("rows") or []), *(value.get("rows") or [])]:
        if isinstance(row, dict) and isinstance(row.get("row_id"), str):
            by_id[row["row_id"]] = row
    rows_omitted = count(own.get("rows_omitted"), prior.get("rows_omitted"))
    if by_id or "rows" in value:
        def order(row):
            recent = next((n for n in (_number((row.get("last_checkpoint") or {}).get("at"))
                           if isinstance(row.get("last_checkpoint"), dict) else None,
                           _number(row.get("ended_at")), _number(row.get("started_at")))
                           if n is not None), float("-inf"))
            return (row["row_id"] not in incoming, row.get("ended_at") is not None,
                    -recent, row["row_id"])
        rows = sorted((_public_row(row) for row in by_id.values()), key=order)
        rows_omitted += max(0, len(rows) - MAX_ARMS)
        value["rows"] = rows[:MAX_ARMS]
    else:
        order = None

    def stage1(record):
        record["metric_summary"] = _public_summary(record["metric_summary"], drop_path=True, max_keys=3)
        record["rows"] = [_public_row(row, max_keys=3) for row in record["rows"]]

    def stage2(record):
        for row in record["rows"]:
            for key in _ROW_DETAIL_KEYS:
                row.pop(key, None)
            if isinstance(row.get("last_checkpoint"), dict):
                row["last_checkpoint"] = {"at": row["last_checkpoint"].get("at")}
            for key in [key for key, item in row.items() if item is None]:
                del row[key]

    def stage3(record):
        for row in record["rows"]:
            row.pop("output_dir", None)
            summary = row.get("metric_summary")
            if isinstance(summary, dict):
                row["metric_summary"] = {key: summary[key] for key in
                                         ("metric", "latest", "best", "trend") if key in summary}

    def stage4(record):
        live = [row for row in record["rows"] if row.get("ended_at") is None]
        done = [row for row in record["rows"] if row.get("ended_at") is not None]
        record["rows"] = live + done[:1]
        record["rows"].sort(key=order)

    def stage6(record):
        record["metrics"] = [{key: item for key, item in declaration.items() if key != "keys"}
                             for declaration in record["metrics"]]
        record["source_warnings"] = []
        if isinstance(record.get("progress"), dict):
            record["progress"] = {**record["progress"], "observed_counters": []}
        if isinstance(record.get("last_checkpoint"), dict):
            record["last_checkpoint"] = {**record["last_checkpoint"], "rule_fired": []}

    def finish(record, level, omitted_rows, oversize=()):
        record["compaction"] = {"budget_bytes": MAX_PUBLIC_RUN_RECORD_WRITE_BYTES,
                                "level": max(level, count(own.get("level"))),
                                "rows_omitted": rows_omitted + omitted_rows,
                                "metrics_omitted": metrics_omitted}
        oversize_all = {*oversize, *(own.get("oversize_fields") or ())}
        if oversize_all:
            record["compaction"]["oversize_fields"] = sorted(oversize_all)
        return record

    omitted = 0
    for level, step in ((0, None), (1, stage1), (2, stage2), (3, stage3), (4, stage4)):
        if step and "rows" in value:
            before = len(value["rows"]); step(value)
            omitted += before - len(value["rows"])
        if _encoded_size(finish(value, level, omitted)) <= MAX_PUBLIC_RUN_RECORD_WRITE_BYTES:
            return value
    while any(row["row_id"] not in incoming for row in value.get("rows") or []):
        value["rows"].remove([row for row in value["rows"] if row["row_id"] not in incoming][-1])
        omitted += 1
        if _encoded_size(finish(value, 5, omitted)) <= MAX_PUBLIC_RUN_RECORD_WRITE_BYTES:
            return value
    stage6(value)
    if _encoded_size(finish(value, 6, omitted)) <= MAX_PUBLIC_RUN_RECORD_WRITE_BYTES:
        return value
    skeleton, oversize = _skeleton(value, declarations)
    omitted += len(value.get("rows") or [])
    finish(skeleton, 7, omitted, oversize)
    for declaration in sorted(declarations, key=lambda item: _encoded_size(item)):
        trial = {**skeleton, "metrics": [*skeleton["metrics"], declaration]}
        if _encoded_size(trial) <= MAX_PUBLIC_RUN_RECORD_WRITE_BYTES:
            skeleton = trial
    skeleton["compaction"]["metrics_omitted"] = displaced + max(
        carried, never_kept + len(declarations) - len(skeleton["metrics"]))
    return skeleton


def _observation_state_path(run, registry, row_key, *, create=True):
    jobs = run.get("jobs") or os.environ.get("AGENT_DISPATCH_JOBS")
    try:
        from resource_run_registry import agent_home
        from dispatch_contract import resolve_dispatch_state_root
        from resource_run_registry import passive_memo
        state_root = passive_memo(('observation-root', str(jobs or '')),
            lambda: resolve_dispatch_state_root(agent_home(), explicit_jobs=jobs))
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


def _admission_roots(run, registry, metric_root):
    """Return only existing roots already tied to this run by provenance."""
    roots = []

    def add(path):
        if not isinstance(path, (str, Path)) or not str(path):
            return
        candidate = Path(path)
        if not candidate.is_absolute():
            return
        try:
            info = candidate.lstat()
            if stat.S_ISREG(info.st_mode):
                candidate = candidate.parent
                info = candidate.lstat()
            if stat.S_ISDIR(info.st_mode) and str(candidate) not in roots:
                roots.append(str(candidate))
        except OSError:
            return

    add(metric_root)
    try:
        registry_root = Path(registry).absolute().parent
        add(registry_root)
    except (OSError, TypeError, ValueError):
        registry_root = None
    mapping = run.get("path_namespace")
    for key in ("log", "log_path", "run_root"):
        value = run.get(key)
        if not isinstance(value, str) or not value:
            continue
        mapped = _reverse_namespace_path(value, mapping) if isinstance(mapping, dict) else None
        if mapped is not None:
            add(mapped)
        elif run.get("host_is_local") is True:
            add(value)
        elif registry_root is not None:
            try:
                candidate = Path(value)
                if candidate.is_absolute() and candidate.resolve(strict=False).is_relative_to(
                        registry_root.resolve(strict=False)):
                    add(value)
            except (OSError, RuntimeError, ValueError):
                pass
    return roots


def _progress_signal(progress, samples, metric_root, run, decision_metric, now):
    """Choose one comparable counter or one exact metric stream.

    A valid value older than `PROGRESS_FRESH_SECONDS` is still the same stream's last value: it keeps
    the stagnation tracker (and its last-increase clock) alive instead of resetting it, and gives no
    rate or ETA (`evaluate_observation` reads its age). A value from the future, or a metric sample
    older than the run, is not this run's progress (RV-PR1619-1114)."""
    progress = progress if isinstance(progress, dict) else None
    if progress is not None:
        completed = progress.get("completed", progress.get("reached_epoch"))
        if type(completed) is int and completed >= 0:
            total = progress.get("total", progress.get("total_epochs"))
            if not (type(total) is int and total > 0 and completed <= total):
                total = None
            age = progress.get("age_s")
            updated = progress.get("updated_at")
            if age is not None:
                if not isinstance(age, (int, float)) or isinstance(age, bool):
                    return None
                try:
                    age = float(age)
                except OverflowError:
                    return None
                if not math.isfinite(age) or age < 0:
                    return None
            if updated is not None:
                if not isinstance(updated, (int, float)) or isinstance(updated, bool):
                    return None
                try:
                    updated = float(updated)
                except OverflowError:
                    return None
                if not math.isfinite(updated):
                    return None
            if (isinstance(updated, (int, float)) and not isinstance(updated, bool)
                    and float(now) - updated < 0):
                return None
            source_generation = progress.get("source_generation")
            if source_generation is None and metric_root:
                try:
                    info = Path(metric_root).stat()
                    source_generation = f"{info.st_dev}:{info.st_ino}"
                except OSError:
                    source_generation = None
            execution = run.get("execution") if isinstance(run.get("execution"), dict) else {}
            generation_data = {key: run.get(key) for key in
                               ("run_id", "pid", "starttime", "command_hash", "config_sha256")}
            generation_data.update(compute_run_id=execution.get("run_id"),
                                   source_generation=source_generation)
            generation = hashlib.sha256(json.dumps(generation_data, sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest()[:20]
            stream = progress.get("stream_id") or progress.get("source_path") or "counter"
            return {"completed": completed, "total": total,
                    "unit": str(progress.get("unit") or "item"),
                    "stream_id": str(stream), "generation": generation,
                    "updated_at": (float(updated) if isinstance(updated, (int, float))
                                   and not isinstance(updated, bool)
                                   and math.isfinite(float(updated)) else None),
                    "age_s": (float(age) if isinstance(age, (int, float))
                              and not isinstance(age, bool)
                              and math.isfinite(float(age)) else None)}

    decision_key = (decision_metric.get("key") if isinstance(decision_metric, dict)
                    else decision_metric)
    run_started = run.get("started_at") if isinstance(run, dict) else None
    run_started = (float(run_started) if isinstance(run_started, (int, float))
                   and not isinstance(run_started, bool) and math.isfinite(float(run_started)) else None)
    streams = {}
    for sample in samples if isinstance(samples, list) else []:
        if not isinstance(sample, dict) or type(sample.get("epoch")) is not int \
                or sample["epoch"] < 0 or not isinstance(sample.get("metrics"), dict):
            continue
        split = str(sample.get("split") or "unknown")
        unit = str(sample.get("unit") or "step")
        fmt = str(sample.get("format") or "unknown")
        source_time = sample.get("source_updated_at")
        if isinstance(source_time, (int, float)) and not isinstance(source_time, bool):
            age = float(now) - float(source_time)
            if not math.isfinite(age) or age < 0:
                continue
            # An old sample still counts for this run's stream; one written before the run is not its own.
            if age > PROGRESS_FRESH_SECONDS and run_started is not None and float(source_time) < run_started:
                continue
        for metric in sample["metrics"]:
            if not isinstance(metric, str) or (decision_key and metric != decision_key):
                continue
            key = (fmt, split, unit, metric)
            prior = streams.get(key)
            if prior is None or sample["epoch"] > prior["completed"]:
                streams[key] = {"completed": sample["epoch"], "total": None,
                    "unit": unit, "stream_id": "|".join(key),
                    "generation": str(sample.get("source_generation") or
                                       (str(Path(metric_root).resolve()) if metric_root else fmt)),
                    "updated_at": None, "age_s": None}
    if not streams:
        return None
    # Observe one deterministic stream; never construct progress by combining
    # step counts from unrelated metrics or validation splits.
    chosen = min(streams, key=lambda key:
        (0 if key[1].lower() in {"valid", "validation", "val"} else 1, key))
    return streams[chosen]


def _advance_progress_tracker(previous, signal, now):
    if not isinstance(signal, dict):
        return None, None, None
    value = signal.get("completed")
    total = signal.get("total")
    unit = signal.get("unit")
    if type(value) is not int or value < 0 or not isinstance(unit, str):
        return None, None, None
    identity = (signal.get("stream_id"), signal.get("generation"), unit, total)
    previous = previous if isinstance(previous, dict) else {}
    previous_identity = (previous.get("stream_id"), previous.get("generation"),
                         previous.get("unit"), previous.get("total"))
    source_at = signal.get("updated_at")
    has_source_time = (isinstance(source_at, (int, float)) and not isinstance(source_at, bool)
                       and math.isfinite(float(source_at)))
    source_at_valid = (has_source_time and float(source_at) <= float(now))
    previous_at = previous.get("last_sample_at")
    if source_at_valid and isinstance(previous_at, (int, float)) \
            and not isinstance(previous_at, bool) and float(source_at) <= float(previous_at):
        source_at_valid = False
    sample_at = float(source_at) if source_at_valid else float(now)
    rate_time_valid = source_at_valid or not has_source_time
    if (not previous or identity != previous_identity
            or type(previous.get("last_sample")) is not int
            or value < previous.get("last_sample", value)):
        tracker = {"stream_id": signal.get("stream_id"),
                   "generation": signal.get("generation"), "unit": unit, "total": total,
                   "first_observed_at": sample_at, "last_sample": value,
                   "last_sample_at": sample_at, "last_increase_at": None,
                   "active_episode_id": None}
        return tracker, {"completed": value, "total": total, "unit": unit}, \
            {"rate_per_second": None, "comparison_valid": False, "stagnation": None}

    tracker = dict(previous)
    last_sample = previous.get("last_sample")
    last_sample_at = previous.get("last_sample_at")
    if type(last_sample) is not int or not isinstance(last_sample_at, (int, float)) \
            or isinstance(last_sample_at, bool):
        tracker = {"stream_id": signal.get("stream_id"), "generation": signal.get("generation"),
                   "unit": unit, "total": total, "first_observed_at": sample_at,
                   "last_sample": value, "last_sample_at": sample_at,
                   "last_increase_at": None, "active_episode_id": None}
        return tracker, {"completed": value, "total": total, "unit": unit}, \
            {"rate_per_second": None, "comparison_valid": False, "stagnation": None}
    delta = value - last_sample
    if delta < 0:
        tracker.update(first_observed_at=sample_at, last_sample=value,
                       last_sample_at=sample_at, last_increase_at=None,
                       active_episode_id=None)
        return tracker, {"completed": value, "total": total, "unit": unit}, \
            {"rate_per_second": None, "comparison_valid": False, "stagnation": None}
    rate = None
    if delta > 0:
        duration = sample_at - float(last_sample_at)
        if rate_time_valid and 0 < duration <= 600:
            rate = delta / duration
        tracker.update(last_increase_at=sample_at, active_episode_id=None)
    tracker.update(last_sample=value, last_sample_at=sample_at)
    anchor = tracker.get("last_increase_at")
    anchor_kind = "last-increase"
    if not isinstance(anchor, (int, float)) or isinstance(anchor, bool):
        anchor, anchor_kind = tracker.get("first_observed_at"), "first-observation"
    stagnation = None
    if isinstance(anchor, (int, float)) and not isinstance(anchor, bool):
        seconds = float(now) - float(anchor)
        if seconds >= 0:
            episode = tracker.get("active_episode_id")
            if seconds >= 7200 and not isinstance(episode, str):
                grace = tracker.get("stagnation_grace_until")
                episode_material = f"{identity!r}|{anchor_kind}|{float(anchor):.6f}|{grace!r}"
                episode = hashlib.sha256(episode_material.encode()).hexdigest()[:20]
                tracker["active_episode_id"] = episode
            if seconds >= 7200:
                stagnation = {"state": "stagnant", "signal": "counter", "unit": unit,
                    "since": anchor_kind, "seconds_since_anchor": seconds,
                    "last_increase_at": tracker.get("last_increase_at"),
                    "first_observed_at": tracker.get("first_observed_at"),
                    "episode_id": episode,
                    "generation": signal.get("generation")}
    comparison = {"completed": value, "total": total, "unit": unit}
    return tracker, comparison, {"rate_per_second": rate,
        "comparison_valid": bool(delta > 0 and rate is not None), "stagnation": stagnation}


def _admission_summary(previous, decoded, now, run_id, row_key, started_at):
    previous = previous if isinstance(previous, dict) else {}
    decoded = decoded if isinstance(decoded, dict) else {}
    counts = dict(previous.get("admission_counts", {}))
    if (decoded.get("generation") is not None
            and decoded.get("generation") != previous.get("admission_generation")):
        # A replacement exact source starts a new sample population. Do not
        # carry counts/latest freshness across a different run/row/source.
        counts = {}
        previous = {**previous, "admission_from_at": None,
                    "admission_to_at": None, "admission_latest": None,
                    "admission_current_state": None,
                    "admission_wait_episode_id": None}
    for key in ("admitted", "denied", "unknown"):
        counts[key] = max(0, int(counts.get(key, 0))) if type(counts.get(key, 0)) is int else 0
    observed_from, observed_to = previous.get("admission_from_at"), previous.get("admission_to_at")
    for sample in decoded.get("samples", []):
        state = sample.get("state") if isinstance(sample, dict) else None
        bucket = state if state in {"admitted", "denied"} else "unknown"
        counts[bucket] += 1
        stamp = sample.get("at")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(float(stamp)):
            observed_from = float(stamp) if observed_from is None else min(float(observed_from), float(stamp))
            observed_to = float(stamp) if observed_to is None else max(float(observed_to), float(stamp))
    latest = decoded.get("latest") if isinstance(decoded.get("latest"), dict) else None
    freshness = None
    current = "unknown"
    generation = decoded.get("generation")
    if (decoded.get("coverage") == "observed" and latest is not None
            and isinstance(latest.get("at"), (int, float))
            and not isinstance(latest.get("at"), bool)
            and math.isfinite(float(latest["at"]))):
        freshness = float(now) - float(latest["at"])
        if (0 <= freshness <= 600 and latest.get("state") in {"admitted", "denied"}
                and isinstance(started_at, (int, float)) and not isinstance(started_at, bool)
                and float(latest["at"]) >= float(started_at)):
            current = "waiting" if latest["state"] == "denied" else "admitted"
    valid = counts["admitted"] + counts["denied"]
    ratio = counts["denied"] / valid if valid else None
    old_generation = previous.get("admission_generation")
    old_episode = previous.get("admission_wait_episode_id")
    if current == "waiting":
        if old_generation == generation and previous.get("admission_current_state") == "waiting" \
                and isinstance(old_episode, str):
            episode = old_episode
        else:
            sample_id = latest.get("sample_id") if latest else "unknown"
            material = f"{run_id}|{row_key}|{generation}|{sample_id}"
            episode = hashlib.sha256(material.encode()).hexdigest()[:20]
    else:
        episode = None
    summary = {"state": current, "coverage": decoded.get("coverage", "unknown"),
        "admitted_samples": counts["admitted"], "denied_samples": counts["denied"],
        "unknown_samples": counts["unknown"], "valid_samples": valid,
        "sample_count": sum(counts.values()), "denied_sample_ratio": ratio,
        "observed_from_at": observed_from, "observed_to_at": observed_to,
        "latest_at": latest.get("at") if latest else None,
        "freshness_seconds": freshness,
        "reasons": latest.get("reasons", []) if current == "waiting" and latest else [],
        "priority_blockers": latest.get("priority_blockers", [])
            if current == "waiting" and latest else [],
        "wait_duration_seconds": None, "time_ratio": None,
        "episode_id": episode, "generation": generation}
    generation_for_state = (generation if generation is not None else
                            previous.get("admission_generation"))
    return summary, {"admission_counts": counts,
        "admission_from_at": observed_from, "admission_to_at": observed_to,
        "admission_generation": generation_for_state,
        "admission_current_state": current,
        "admission_wait_episode_id": episode,
        "admission_cursor": decoded.get("cursor", previous.get("admission_cursor")),
        "admission_warnings": decoded.get("warnings", [])}


def _json_pointer(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 512:
        raise ValueError("source-pointer-invalid")
    current = value
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("source-pointer-escape-invalid")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif (isinstance(current, list)
              and (token == "0" or re.fullmatch(r"[1-9][0-9]*", token))
              and int(token) < len(current)):
            current = current[int(token)]
        else:
            raise ValueError("source-pointer-missing")
    return current


def _bounded_source_file(raw_path, remaining, path_namespace=None):
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
        raise ValueError("source-path-invalid")
    declared = Path(raw_path)
    path, _mapped = _reverse_namespace_path(declared, path_namespace)
    if not declared.is_absolute() or not path.is_absolute() or _path_has_symlink(path):
        raise ValueError("source-path-invalid")
    path = Path(os.path.abspath(path))
    root = path.parent
    try:
        raw, info = __import__("resource_metric_sources")._read_file(
            path, root, min(MAX_SOURCE_BINDING_BYTES, max(0, int(remaining))))
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise ValueError("source-file-unavailable") from exc
    return path, raw, info


def _observe_source_bindings(bindings, run_id, row_key, now, path_namespace=None):
    """Validate caller-declared exact provenance without importing its writer."""
    if not isinstance(bindings, list):
        return [], [], ["source-bindings-invalid"]
    if len(bindings) > MAX_SOURCE_BINDINGS:
        return [], [], ["source-bindings-limit"]
    observations, retained, warnings = [], [], []
    total_bytes = metadata_bytes = 0
    for raw_binding in bindings:
        if not isinstance(raw_binding, dict):
            warnings.append("source-binding-invalid")
            continue
        binding = {key: raw_binding.get(key) for key in
            ("target_run_id", "row_key", "path", "writer_run_id", "timestamp_pointer",
             "scope", "counters", "proofs")}
        try:
            binding_bytes = len(json.dumps(binding, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if binding_bytes > MAX_SOURCE_BINDING_BYTES \
                    or metadata_bytes + binding_bytes > MAX_SOURCE_BINDING_BYTES:
                raise ValueError("source-binding-metadata-limit")
            metadata_bytes += binding_bytes
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            warnings.append("source-binding-unknown:" + str(exc)[:96])
            continue
        retained.append(binding)
        name = None
        first_counter = (binding.get("counters", [None])[0]
                         if isinstance(binding.get("counters"), list)
                         and binding.get("counters") else None)
        if isinstance(first_counter, dict) and isinstance(first_counter.get("name"), str):
            name = first_counter["name"][:128]
        base = {"name": name, "value": None, "source_path": binding.get("path"),
                "json_pointer": first_counter.get("pointer")
                    if isinstance(first_counter, dict) else None,
                "writer_run_id": binding.get("writer_run_id"),
                "target_run_id": binding.get("target_run_id"),
                "row_key": binding.get("row_key"),
                "scope": binding.get("scope"), "comparison_eligible": False,
                "observed_at": None, "source_generation": None,
                "freshness": "unknown", "state": "unknown"}
        try:
            if (binding.get("target_run_id") != run_id
                    or binding.get("row_key") != str(row_key)
                    or not isinstance(binding.get("writer_run_id"), str)
                    or not binding.get("writer_run_id")
                    or len(binding["writer_run_id"]) > 256
                    or binding.get("writer_run_id") == run_id
                    or not isinstance(binding.get("scope"), str)
                    or len(binding["scope"]) > 128):
                raise ValueError("source-binding-target-mismatch")
            counters = binding.get("counters")
            proofs = binding.get("proofs")
            if (not isinstance(counters, list) or not counters
                    or len(counters) > MAX_SOURCE_BINDING_COUNTERS
                    or not isinstance(proofs, list)
                    or not proofs or len(proofs) > MAX_SOURCE_BINDING_PROOFS):
                raise ValueError("source-binding-shape-invalid")
            source_path, source_raw, source_info = _bounded_source_file(
                binding.get("path"), MAX_SOURCE_BINDING_BYTES - total_bytes,
                path_namespace)
            total_bytes += len(source_raw)
            source_doc = __import__("resource_metric_sources")._admission_json_object(source_raw)
            if not isinstance(source_doc, dict):
                raise ValueError("source-json-not-object")
            observed_at = _json_pointer(source_doc, binding.get("timestamp_pointer"))
            if (not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)
                    or not math.isfinite(float(observed_at))):
                raise ValueError("source-timestamp-invalid")
            proof_hashes = []
            asserted_identity_values = set()
            for proof in proofs:
                if not isinstance(proof, dict) or not isinstance(proof.get("sha256"), str):
                    raise ValueError("source-proof-invalid")
                expected_hash = proof["sha256"].lower()
                if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                    raise ValueError("source-proof-hash-invalid")
                assertions = proof.get("assertions", [])
                if (not isinstance(assertions, list)
                        or len(assertions) > MAX_SOURCE_BINDING_ASSERTIONS):
                    raise ValueError("source-proof-assertions-invalid")
                _proof_path, proof_raw, _proof_info = _bounded_source_file(
                    proof.get("path"), MAX_SOURCE_BINDING_BYTES - total_bytes,
                    path_namespace)
                total_bytes += len(proof_raw)
                if total_bytes > MAX_SOURCE_BINDING_BYTES:
                    raise ValueError("source-proof-byte-limit")
                actual_hash = hashlib.sha256(proof_raw).hexdigest()
                if actual_hash != expected_hash:
                    raise ValueError("source-proof-changed")
                if assertions:
                    proof_doc = __import__("resource_metric_sources")._admission_json_object(proof_raw)
                    for assertion in assertions:
                        if not isinstance(assertion, dict) or "equals" not in assertion:
                            raise ValueError("source-proof-assertion-invalid")
                        proved = _json_pointer(proof_doc, assertion.get("pointer"))
                        expected = assertion["equals"]
                        if type(proved) is not type(expected) or proved != expected:
                            raise ValueError("source-proof-assertion-mismatch")
                        if (isinstance(expected, str)
                                and expected in {binding["target_run_id"],
                                                 binding["writer_run_id"]}):
                            asserted_identity_values.add(expected)
                proof_hashes.append(actual_hash)
            required_identity_values = {binding["target_run_id"], binding["writer_run_id"]}
            if not required_identity_values.issubset(asserted_identity_values):
                raise ValueError("source-proof-identity-incomplete")
            generation_material = {"run_id": run_id, "row_key": str(row_key),
                "writer_run_id": binding["writer_run_id"], "path": binding.get("path"),
                "timestamp_pointer": binding["timestamp_pointer"],
                "scope": binding["scope"],
                "counters": [{"name": item.get("name"), "pointer": item.get("pointer"),
                              "comparison_eligible": item.get("comparison_eligible")}
                    for item in counters if isinstance(item, dict)],
                "proofs": sorted(proof_hashes)}
            generation = hashlib.sha256(json.dumps(generation_material, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:24]
            for counter in counters:
                if (not isinstance(counter, dict)
                        or not isinstance(counter.get("name"), str)
                        or len(counter["name"]) > 128
                        or not isinstance(counter.get("comparison_eligible"), bool)):
                    raise ValueError("source-counter-invalid")
                value = _json_pointer(source_doc, counter.get("pointer"))
                if (not (value is None or isinstance(value, (str, int, float, bool)))
                        or isinstance(value, float) and not math.isfinite(value)
                        or isinstance(value, str) and len(value) > 256):
                    raise ValueError("source-counter-value-invalid")
                age = float(now) - float(observed_at)
                fresh = 0 <= age <= 600
                observations.append({**base, "name": counter["name"], "value": value,
                    "source_path": binding.get("path"), "read_path": str(source_path),
                    "json_pointer": counter["pointer"],
                    "comparison_eligible": counter["comparison_eligible"],
                    "observed_at": float(observed_at), "source_generation": generation,
                    "source_stat_generation": f"{source_info.st_dev}:{source_info.st_ino}",
                    "freshness_seconds": age if fresh else None,
                    "freshness": "fresh" if fresh else "stale", "state": "observed" if fresh else "unknown"})
        except (OSError, ValueError, TypeError, UnicodeError, OverflowError,
                RecursionError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "source-proof-unavailable"
            warnings.append("source-binding-unknown:" + reason[:96])
            observations.append({**base, "reason": reason[:96]})
    return observations, retained, sorted(set(warnings))


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


def _latest_source_update(samples, progress, now):
    """When the run's progress or metric files last changed, as this observer saw it (the newest
    `source_updated_at` of a sample or the progress file), or None when nothing was read."""
    times = [sample.get("source_updated_at") for sample in samples or () if isinstance(sample, dict)]
    if isinstance(progress, dict):
        if isinstance(progress.get("age_s"), (int, float)) and not isinstance(progress.get("age_s"), bool):
            times.append(float(now) - float(progress["age_s"]))
        times.append(progress.get("updated_at"))
    times = [float(value) for value in times if isinstance(value, (int, float))
             and not isinstance(value, bool) and math.isfinite(float(value)) and value <= float(now)]
    return max(times) if times else None


# What a notice says, in one phrase, for a run whose observer has no progress file or output
# folder to read (`_public_output_state` reasons), instead of five "no metric" cells.
_UNDECLARED_TEXT = {"not-declared": "resource.no_progress_declared",
                    "node-declares-no-training-output": "resource.no_progress_declared",
                    "remote-unmapped": "resource.progress_not_visible",
                    "declared-unresolved": "resource.progress_not_visible"}


def declaration_note(run):
    """The one line a launch prints when its run declares no progress file or output folder.

    A note, never a refusal: the run starts all the same and its notices say so in one phrase.
    None when the run declares one (a folder that does not exist yet, or a progress file, included),
    when its route node declares no training output, or when this cannot be told."""
    try:
        observation = run.get("observation") if isinstance(run.get("observation"), dict) else {}
        if run.get("output_dir") or run.get("progress_file") or observation.get("source_bindings"):
            return None
        folder, reason = discover_output_binding(run)
        if folder or reason not in ("not-declared", "remote-unmapped"):
            return None
        if reason == "not-declared" and _public_output_state(run, None, binding_given=False)[0] == "none":
            return None
        t = _ui_translator()
        return t("resource.declare_progress_note" if reason == "not-declared"
                 else "resource.declared_progress_unmapped_note")
    except Exception:  # noqa: BLE001 -- a note never stops a launch
        return None


def _observation_as_of(observation, at, now):
    """The same observation stated at an earlier observed time `at` (a refresh carries the run's
    last evidence, not "now"): its observed time and the ages measured at it move back together,
    so a reader's "observed time minus age" moments stay the same (RV-PR1606)."""
    shift = float(now) - float(at)
    result = dict(observation)
    result["at"] = float(at)

    def earlier(value, sign=-1):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return max(0.0, float(value) + sign * shift)
        return value
    result["elapsed_seconds"] = earlier(observation.get("elapsed_seconds"))
    result["eta_seconds"] = earlier(observation.get("eta_seconds"), sign=1)
    for key, field in (("stagnation", "seconds_since_anchor"), ("admission", "freshness_seconds")):
        if isinstance(observation.get(key), dict) and field in observation[key]:
            result[key] = {**observation[key], field: earlier(observation[key][field])}
    return result


def _ui_translator():
    """The shared UI-text catalog (English by default, Korean when declared).

    An observer never stops over its words: without a readable catalog a notice names its fields.
    """
    try:
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "i18n" / "ui_text.py"
        spec = importlib.util.spec_from_file_location("ui_text", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.translator()
    except Exception:  # noqa: BLE001 -- a missing or broken catalog must not end the observation
        return _plain_text


def _plain_text(message, /, **fields):
    if fields:
        return "; ".join(f"{name}={value}" for name, value in fields.items())
    return message.rsplit(".", 1)[-1].replace("_", " ")


# ``summary.trend`` is stored (registry rows, observation state) and read by other releases in
# these Korean words; only the notice text names them in the reader's language.
_TREND_TEXT = {"개선": "resource.trend.improving", "정체": "resource.trend.flat",
               "악화": "resource.trend.worsening", "불안정": "resource.trend.unstable",
               "판정 불가": "resource.trend.undetermined"}


def _catalog_module():
    """`experiment_catalog`, imported when a notice needs it (it imports this module at its top)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import experiment_catalog
    return experiment_catalog


def _experiment_context(run, identity, observation, has_decision_metric):
    """What the project's experiment catalog says about this live row, or None when it cannot say.

    `{"info", "catalog", "has_decision_metric"}`: `info` is `describe_observation`'s answer (None when the
    run is not linked or two conditions claim it).  Any failure reading the catalog gives None: a notice is
    never held back, and never changed, because the catalog could not be read."""
    root = run.get("artifact_root")
    if not root:
        return None
    try:
        catalog = _catalog_module()
        read = catalog.load(Path(root))
        if read.status == "invalid":
            return None
        doc = read.doc if read.status == "ok" else None
        info = catalog.describe_observation(Path(root), run.get("run_id"), str(identity),
                                            observation.get("summary") or {}, doc=doc)
        if info is not None and not info.get("name"):
            info = None  # a condition without a name cannot give one
        return {"info": info, "catalog": read.status, "has_decision_metric": bool(has_decision_metric)}
    except Exception:  # noqa: BLE001 -- the catalog is read-only context for a notice
        return None


def _experiment_lines(context, *, first, final):
    """The catalog name line for a notice, plus the "register this run" line on a run's first notice and its
    final one (not on every checkpoint)."""
    if not context:
        return []
    try:
        catalog = _catalog_module()
        if context["info"] is not None:
            return [catalog.notice_line(context["info"])]
        if (first or final) and (context["catalog"] == "ok" or context["has_decision_metric"]):
            return [catalog.unregistered_line()]
    except Exception:  # noqa: BLE001
        pass
    return []


def _decision_mark(context, samples):
    """What a notice has told about the catalog's deciding metric: its latest point plus a digest of
    every validation evaluation of that metric, so a first evaluation of an earlier epoch that arrives
    late, or an earlier epoch rewritten, is new even when the latest point stays the same.  None when
    the catalog declares no metric this run has reported."""
    point = ((context or {}).get("info") or {}).get("decision_point")
    if not point:
        return None
    key = point.get("metric")
    evaluations = sorted(
        [str(row.get("split")), row["epoch"], repr(float(row["metrics"][key]))]
        for row in samples if isinstance(row, dict) and isinstance(row.get("metrics"), dict)
        and str(row.get("split", "")).lower() in {"valid", "val", "validation"}
        and type(row.get("epoch")) is int and isinstance(row["metrics"].get(key), (int, float))
        and not isinstance(row["metrics"].get(key), bool))
    digest = hashlib.sha256(json.dumps(evaluations, separators=(",", ":")).encode()).hexdigest()[:16]
    return {**point, "points": digest}


def _quiet_checkpoint(context, previous, samples):
    """A periodic checkpoint with nothing new is not worth the conversation: when the project's catalog
    declares the deciding metric and this run has reported it, a checkpoint is made only when that
    metric has a new evaluation (any epoch, a late one or a corrected one included) since the last
    notice.  Anything else (no catalog, no declared metric, a metric never reported, a catalog that
    cannot be read) keeps the existing checkpoint, so an unconfirmed run does not go quiet.  A told point
    saved before the digest existed (`{metric, epoch, value}` only) is compared on those fields alone, so
    an install does not make every running experiment speak once; the caller then stores the digest."""
    mark = _decision_mark(context, samples)
    if not mark:
        return False
    told = previous.get("notified_decision_point")
    if isinstance(told, dict) and "points" not in told:
        return told == {key: value for key, value in mark.items() if key != "points"}
    return told == mark


def _notice_texts(run, event_kind, observation, record_path, sources, name_lines=()):
    t = _ui_translator()
    undeclared = observation.get("undeclared")
    summary = observation.get("summary") or {}
    admission = observation.get("admission") if isinstance(observation.get("admission"), dict) else {}
    stagnation = observation.get("stagnation") if isinstance(observation.get("stagnation"), dict) else {}
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
                if isinstance(fallback, dict) and metric else t("resource.no_metrics"))
    record = str(record_path) if record_path else t("resource.no_record_path")
    trend = summary.get("trend") or "판정 불가"
    trend = t(_TREND_TEXT[trend]) if trend in _TREND_TEXT else trend
    run_line = f"run={run.get('run_id') or 'unknown'}"
    if run.get("row_id"):
        run_line += f" row={run['row_id']}"
    entries = [run_line, f"public run record={record}"]
    admission_line = (f"admission={admission.get('state', 'unknown')}; coverage="
        f"{admission.get('coverage', 'unknown')}; samples denied/valid="
        f"{admission.get('denied_samples', 0)}/{admission.get('valid_samples', 0)}; "
        f"sample ratio={admission.get('denied_sample_ratio') if admission.get('denied_sample_ratio') is not None else 'unknown'}")
    if admission.get("state") == "waiting" and admission.get("reasons"):
        admission_line += "; reason=" + ", ".join(admission["reasons"][:3])
    blockers = admission.get("priority_blockers")
    if admission.get("state") == "waiting" and isinstance(blockers, list) and blockers:
        labels = []
        for item in blockers[:3]:
            labels.append(",".join(f"{key}={value}" for key, value in item.items()
                                   if key in {"task", "host", "gpu", "owner"}))
        labels = [item for item in labels if item]
        if labels:
            admission_line += "; observed blockers=" + " | ".join(labels)
    if admission.get("sample_count", 0) or admission.get("coverage") != "unknown":
        entries.append(admission_line)
    for counter in observation.get("source_observations", [])[:3]:
        if not isinstance(counter, dict):
            continue
        value = counter.get("value") if counter.get("state") == "observed" else "unknown"
        entries.append("raw counter=" + str(counter.get("name") or "unknown")
            + ":" + str(value) + " scope=" + str(counter.get("scope") or "unknown")
            + " writer=" + str(counter.get("writer_run_id") or "unknown")
            + " target=" + str(counter.get("target_run_id") or "unknown")
            + " at=" + str(counter.get("observed_at") or "unknown"))
    if stagnation.get("state") == "stagnant":
        anchor = ("last actual increase" if stagnation.get("since") == "last-increase"
                  else "first comparable observation")
        entries.append(f"no observed increase for {_duration(stagnation.get('seconds_since_anchor'), t)} since {anchor}")
    elif stagnation.get("state") == "unknown":
        entries.append("progress stagnation=unknown")
    # Nothing was read because nothing was declared (or it is not visible here): one phrase.
    bare = (undeclared in _UNDECLARED_TEXT and not sources
            and not (latest or summary.get("latest_metrics"))
            and observation.get("reached") is None and not observation.get("source_observations"))
    if bare:
        if event_kind == "final":
            entries.append(f"status={run.get('status') or 'unknown'}; exit={run.get('exit_code')}")
            entries.append(t("resource.final_time", actual=_duration(observation.get("elapsed_seconds"), t),
                             plan=_duration(observation.get("planned_seconds"), t))
                           if observation.get("planned_seconds")
                           else t("resource.actual_time", actual=_duration(observation.get("elapsed_seconds"), t)))
        else:
            entries.append(t("resource.elapsed", elapsed=_duration(observation.get("elapsed_seconds"), t)))
        if observation.get("attention"):
            entries.append("attention=" + ",".join(item.get("kind", "unknown") for item in observation["attention"][:3]))
        entries.append(t(_UNDECLARED_TEXT[undeclared]))
    elif event_kind == "final":
        entries.append(f"status={run.get('status') or 'unknown'}; exit={run.get('exit_code')}")
        entries.append(t("resource.final_metrics", latest=points("latest_metrics", latest),
                         best=points("best_metrics", best), trend=trend))
        entries.append(t("resource.final_time", actual=_duration(observation.get("elapsed_seconds"), t),
                         plan=_duration(observation.get("planned_seconds"), t)))
        if observation.get("attention"):
            entries.append("attention=" + ",".join(item.get("kind", "unknown") for item in observation["attention"][:3]))
    else:
        delta = summary.get("baseline_delta")
        entries.append(t("resource.checkpoint_metrics", latest=points("latest_metrics", latest),
                         best=points("best_metrics", best),
                         delta=delta if delta is not None else t("resource.no_baseline"), trend=trend))
        eta = observation.get("predicted_total_seconds")
        entries.append(t("resource.eta", total=_duration(eta, t),
                         plan=_duration(observation.get("planned_seconds"), t),
                         elapsed=_duration(observation.get("elapsed_seconds"), t)))
        if observation.get("attention"):
            entries.append("attention=" + ",".join(item.get("kind", "unknown") for item in observation["attention"][:3]))
    if not bare:
        # Step or epoch reached, and how long ago the progress or metric files last changed.
        if observation.get("reached") is not None:
            entries.append(t("resource.progress", reached=observation["reached"],
                             total=observation.get("total") if observation.get("total") is not None else "?",
                             unit=observation.get("unit") or ""))
        updated = observation.get("source_updated_at")
        if isinstance(updated, (int, float)) and not isinstance(updated, bool):
            entries.append(t("resource.last_update",
                             age=_duration(float(observation.get("at", updated)) - float(updated), t)))
    source_lines = []
    for source in (sources or [])[:3] if not bare else ():
        path = source.get("path")
        if isinstance(path, list):
            path = path[0] if path else None
        source_lines.append(f"source {source.get('format') or 'unknown'}={path or t('resource.no_path')}")
    if not source_lines and not bare:
        source_lines.append("source metrics unavailable")
    entries.extend(source_lines)
    text = "; ".join([*name_lines, *entries])
    if len(text) <= 600:
        return [text]
    # Keep every field in immutable bounded parts; numbered chunks can be
    # concatenated in order when a path is longer than one notice.
    output = []
    for label, value in [("run", "; ".join([*name_lines, run_line])), ("record", record),
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


def _duration(value, t=None):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return (t or _ui_translator())("resource.unknown_duration")
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
    from resource_run_registry import passive_parse
    return passive_parse(('observation', str(path)), raw, (_json, _restore_special_numbers),
                         lambda: _restore_special_numbers(_json(raw)))


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
        counts = value.get("admission_counts")
        has_admission_history = (
            isinstance(value.get("admission_cursor"), dict)
            and bool(value["admission_cursor"])) or (
            isinstance(counts, dict) and any(type(counts.get(key)) is int
                and counts[key] > 0 for key in ("admitted", "denied", "unknown"))) or (
            isinstance(value.get("admission_latest"), dict)
            or isinstance(value.get("admission_summary"), dict)
            or isinstance(value.get("admission_generation"), str)
            and bool(value["admission_generation"]))
        source_observations = value.get("source_observations")
        return {"available": True, "observation": value.get("observation"),
                "metrics": value.get("sources", []), "metric_root": None,
                "has_persisted_observation": bool(has_admission_history
                    or isinstance(source_observations, list) and source_observations),
                "record_path": None, "delivery": [], "warnings": value.get("warnings", []),
                "state_path": str(path)}
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def observe_experiment(run, registry, now, *, root=None, row_key=None,
                       started_at=None, planned_seconds=None, progress=None,
                       final=False, status=None, notify_events=True,
                       poll_interval_seconds=60.0, row_scoped=None, alive=None, alive_at=None):
    """Advance one detached row's durable observation and public run record.

    Metric discovery is deliberately caller-bound: `root` must come from an
    exact run/config binding or explicit optional observation metadata.
    `alive=True` says the caller has seen the run live, at `alive_at` (default: now; a compute
    run's own files last changed then): the public record's observed time is then kept up to
    that evidence (`PUBLIC_REFRESH_SECONDS`) even when nothing new was measured, so a long epoch
    does not read as a lost observation. Without that evidence nothing is refreshed and a silent
    run still turns stale; so does a run whose tracked progress source can no longer be read.
    The observed time is never the progress time: the last increase (`stagnation`) is kept
    apart and a value that stopped turns stagnant under the two-hour rule.
    """
    run = run if isinstance(run, dict) else {}
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"available": False, "warnings": ["run-id-unknown"]}
    root_value = root or run.get("output_dir")
    discovered_reason = None
    if not root_value:
        root_value, discovered_reason = discover_output_binding(run)
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
    row_scoped = (bool(row_key and str(row_key) != "run")
                  if row_scoped is None else bool(row_scoped))
    started = (started_at if isinstance(started_at, (int, float)) else
               None if row_scoped else run.get("started_at"))
    if not isinstance(started, (int, float)) or isinstance(started, bool) \
            or not math.isfinite(float(started)):
        started = None
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
        declared_observation = (run.get("observation")
            if isinstance(run.get("observation"), dict) else {})
        incoming_source_bindings = declared_observation.get("source_bindings")
        previous_poll = previous.get("last_observer_poll_at")
        if (not final and isinstance(previous_poll, (int, float))
                and not isinstance(previous_poll, bool)
                and float(now) - float(previous_poll) < max(1.0, float(poll_interval_seconds))
                and status == previous.get("public_status")
                and not (isinstance(incoming_source_bindings, list)
                         and incoming_source_bindings)):
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
                value = decoded.get("progress") if isinstance(decoded.get("progress"), dict) else None
                stamp = value.get("source_updated_at") if value else None
                if (value and isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                        and started is not None and float(stamp) < float(started)
                        and float(now) - float(stamp) > PROGRESS_FRESH_SECONDS):
                    value = None   # an old progress file left in the folder before this run began
                if value and progress is None:
                    progress = {"completed": value.get("reached_epoch"),
                                "total": value.get("total_epochs"), "unit": "epoch",
                                "updated_at": value.get("updated_at"),
                                "age_s": (float(now) - float(value["source_updated_at"])
                                    if isinstance(value.get("source_updated_at"), (int, float))
                                    else None),
                                "source_generation": value.get("source_generation"),
                                "stream_id": value.get("source_path") or "counter"}
            except (OSError, ValueError, TypeError, RuntimeError, OverflowError, RecursionError) as exc:
                warnings.append("metric-source-unavailable:" + str(exc))
        last_admission_observed_at = previous.get("last_admission_observed_at")
        admission_roots = _admission_roots(run, registry, metric_root)
        should_read_admission = bool(admission_roots and (final
            or not isinstance(last_admission_observed_at, (int, float))
            or float(now) - float(last_admission_observed_at) >= 60.0))
        if should_read_admission:
            try:
                admission_decoded = __import__("resource_metric_sources").read_admission_sources(
                    admission_roots, run_id=run_id,
                    row_key=(str(identity) if row_scoped else None),
                    registry_path=registry, started_at=started, now=now,
                    cursor=previous.get("admission_cursor"))
                warnings.extend(admission_decoded.get("warnings", []))
            except (OSError, ValueError, TypeError, RuntimeError, OverflowError, RecursionError) as exc:
                admission_decoded = {"samples": [], "latest": None, "generation": None,
                    "cursor": previous.get("admission_cursor"), "coverage": "partial",
                    "warnings": ["admission-source-unavailable:" + str(exc)]}
                warnings.extend(admission_decoded["warnings"])
        elif admission_roots:
            admission_decoded = {"samples": [],
                "latest": previous.get("admission_latest"),
                "generation": previous.get("admission_generation"),
                "cursor": previous.get("admission_cursor"),
                "coverage": previous.get("admission_coverage", "unknown"),
                "warnings": []}
        else:
            admission_decoded = {"samples": [], "latest": None,
                "generation": None, "cursor": previous.get("admission_cursor"),
                "coverage": "unknown", "warnings": []}
        admission_summary, admission_state = _admission_summary(
            previous, admission_decoded, now, run_id, str(identity), started)
        admission_changed = any(admission_summary.get(key) !=
            (previous.get("admission_summary") or {}).get(key) for key in
            ("state", "coverage", "admitted_samples", "denied_samples", "unknown_samples",
             "denied_sample_ratio", "observed_from_at", "observed_to_at", "latest_at",
             "reasons", "priority_blockers", "episode_id", "generation"))
        source_bindings = (incoming_source_bindings if isinstance(incoming_source_bindings, list)
                           else previous.get("source_bindings", []))
        source_observations, retained_bindings, source_binding_warnings = _observe_source_bindings(
            source_bindings, run_id, str(identity), now, run.get("path_namespace"))
        warnings.extend(source_binding_warnings)
        previous_counters = previous.get("source_observations", [])
        old_counter_identity = [(item.get("name"), item.get("value"),
            item.get("source_generation"), item.get("state")) for item in previous_counters
            if isinstance(item, dict)] if isinstance(previous_counters, list) else []
        new_counter_identity = [(item.get("name"), item.get("value"),
            item.get("source_generation"), item.get("state")) for item in source_observations]
        source_observations_changed = source_observations != previous_counters
        source_signal_changed = old_counter_identity != new_counter_identity
        samples = _merge_samples(previous.get("samples"), new_samples)
        sources = _merge_metric_sources(previous.get("sources"), sources, samples)
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
        progress_signal = _progress_signal(progress, samples, metric_root, run,
                                           decision_metric, now)
        old_tracker = previous.get("progress_tracker")
        progress_tracker, next_progress, tracker_result = _advance_progress_tracker(
            old_tracker, progress_signal, now)
        # A progress source this observer was tracking can no longer be read: progress cannot be
        # judged. Keep the tracker (its last-increase clock survives a gap) and say so; the observer
        # does not vouch for such a run (`refresh_due` below), so its record turns stale.
        progress_lost = progress_signal is None and isinstance(old_tracker, dict)
        if progress_lost:
            progress_tracker, next_progress = old_tracker, previous.get("progress_sample")
        tracker_result = tracker_result or {"rate_per_second": None,
                                             "comparison_valid": False,
                                             "stagnation": None}
        was_waiting = previous.get("admission_current_state") == "waiting"
        is_waiting = admission_summary.get("state") == "waiting"
        if isinstance(progress_tracker, dict) and is_waiting:
            progress_tracker["active_episode_id"] = None
            tracker_result["stagnation"] = None
        elif isinstance(progress_tracker, dict) and was_waiting:
            progress_tracker["active_episode_id"] = None
            progress_tracker["stagnation_grace_until"] = float(now) + 7200.0
            tracker_result["stagnation"] = None
        elif (isinstance(progress_tracker, dict)
              and isinstance(progress_tracker.get("stagnation_grace_until"), (int, float))
              and float(now) < float(progress_tracker["stagnation_grace_until"])):
            progress_tracker["active_episode_id"] = None
            tracker_result["stagnation"] = None
        evaluation_progress = dict(progress_signal) if progress_signal else None
        if evaluation_progress is not None:
            evaluation_progress["_comparison_valid"] = tracker_result["comparison_valid"]
        progress_identity_matches = bool(progress_signal and isinstance(old_tracker, dict)
            and old_tracker.get("stream_id") == progress_signal.get("stream_id")
            and old_tracker.get("generation") == progress_signal.get("generation")
            and old_tracker.get("unit") == progress_signal.get("unit")
            and old_tracker.get("total") == progress_signal.get("total"))
        previous_progress = (previous.get("progress_sample")
            if progress_identity_matches and type(old_tracker.get("last_sample")) is int
            and type(progress_signal.get("completed")) is int
            and progress_signal["completed"] > old_tracker["last_sample"] else None)
        previous_progress_at = (old_tracker.get("last_sample_at")
                                if previous_progress is not None else None)
        observation = evaluate_observation(
            now=now, started_at=schedule_started, planned_seconds=plan,
            progress=evaluation_progress, previous_progress=previous_progress,
            previous_progress_at=previous_progress_at, samples=samples,
            decision_metric=(decision_metric.get("key") if isinstance(decision_metric, dict)
                             else decision_metric),
            direction=(decision_metric.get("direction") if isinstance(decision_metric, dict)
                       else run.get("decision_direction", declared.get("direction"))),
            baseline=run.get("baseline_value", declared.get("baseline_value")), final=final,
            optional_rules=run.get("rules", declared.get("rules")),
            last_checkpoint_at=previous.get("last_checkpoint_at"))
        eta_episode = eta_attention_episode(observation, previous.get("eta_attention"), now)
        stagnation = tracker_result.get("stagnation")
        if stagnation is None and isinstance(progress_tracker, dict) and not progress_lost:
            stagnation = {"state": "observing", "signal": "counter",
                "unit": progress_tracker.get("unit"),
                "first_observed_at": progress_tracker.get("first_observed_at"),
                "last_increase_at": progress_tracker.get("last_increase_at"),
                "episode_id": None, "generation": progress_tracker.get("generation")}
            # The progress time, apart from the observed time: how long since the last increase
            # (or the first comparable value), measured at this observation like a stagnant one.
            anchor = progress_tracker.get("last_increase_at")
            if not isinstance(anchor, (int, float)) or isinstance(anchor, bool):
                anchor = progress_tracker.get("first_observed_at")
            if (isinstance(anchor, (int, float)) and not isinstance(anchor, bool)
                    and 0 <= float(now) - float(anchor)):
                stagnation["seconds_since_anchor"] = float(now) - float(anchor)
        if stagnation is None:
            stagnation = {"state": "unknown"}
        observation["admission"] = admission_summary
        observation["stagnation"] = stagnation
        observation["source_observations"] = source_observations
        if source_signal_changed and source_observations:
            for item in source_observations[:MAX_SOURCE_BINDINGS * MAX_SOURCE_BINDING_COUNTERS]:
                observation["attention"].append({"kind": "source-observation",
                    "generation": item.get("source_generation"),
                    "source_value_hash": hashlib.sha256(json.dumps(
                        item.get("value"), sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:16]
                        if item.get("state") == "observed" else "unknown"})
        if stagnation.get("state") == "stagnant" and stagnation.get("episode_id"):
            observation["attention"].append({"kind": "progress-stagnation",
                "episode_id": stagnation["episode_id"],
                "generation": stagnation.get("generation"),
                "since": stagnation.get("since")})
        if admission_summary.get("state") == "waiting" and admission_summary.get("episode_id"):
            observation["attention"].append({"kind": "admission-wait",
                "episode_id": admission_summary["episode_id"],
                "generation": admission_summary.get("generation")})
        if not clock_known:
            # A wrapper clock is not a row birth. Keep the row's own clock
            # unknown while retaining its independently identified metrics.
            observation["elapsed_seconds"] = None
            observation["predicted_total_seconds"] = None
            observation["eta_seconds"] = None
            observation["eta_vs_plan"] = None
        observation["source_updated_at"] = _latest_source_update(samples, progress, now)
        # Why there is nothing to read, when there is no folder and no progress value was ever read
        # (the public record's reason). A progress file read at any age is a declaration.
        observation["undeclared"] = (None if metric_root is not None or isinstance(progress_tracker, dict)
                                     else _public_output_state(run, None, binding_given=bool(root_value),
                                                               discovered_reason=discovered_reason)[1])
        artifact_root = run.get("artifact_root")
        record_locator = public_record_path(artifact_root, run_id) if artifact_root else None
        catalog_context = []

        def experiment():
            if not catalog_context:
                catalog_context.append(_experiment_context(run, identity, observation, bool(
                    decision_metric.get("key") if isinstance(decision_metric, dict) else decision_metric)))
            return catalog_context[0]
        event_specs = []
        quiet_checkpoint = False
        seen = set(previous.get("event_keys", []))
        outbox = list(previous.get("outbox", []))
        known = {item.get("key") for item in outbox if isinstance(item, dict)}
        if notify_events and final:
            event_specs.append(("final", f"final:{identity}"))
        if (notify_events and observation["attention"]
                and (not final or source_signal_changed and source_observations)):
            # The same attention is the same digest; one that cleared and came back, or
            # whose size grew by the rule's own step, is a different one (an ETA attention
            # carries its episode and size step; the others carry their epoch or episode).
            identities = [attention_identity(item, eta_episode)
                          for item in observation["attention"]]
            attention_digest = hashlib.sha256(json.dumps(identities, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]
            # A new or changed attention is `attention:` whenever it appears; the same
            # attention again at a periodic checkpoint is `attention-checkpoint:`, which
            # waits for the recipient's next input instead of waking it (session_notice).
            attention_told = any(
                key == f"attention:{identity}:{attention_digest}"
                or (key.startswith(f"attention-checkpoint:{identity}:")
                    and key.endswith(f":{attention_digest}"))
                for key in (seen | known) if isinstance(key, str))
            if observation["checkpoint_due"] and attention_told:
                slot = int(observation["due_checkpoint_at"])
                event_specs.append(("attention", f"attention-checkpoint:{identity}:{slot}:{attention_digest}"))
            else:
                event_specs.append(("attention", f"attention:{identity}:{attention_digest}"))
        elif notify_events and observation["checkpoint_due"] and not final:
            slot = int(observation["due_checkpoint_at"])
            quiet_checkpoint = _quiet_checkpoint(experiment(), previous, samples)
            if not quiet_checkpoint:
                event_specs.append(("checkpoint", f"checkpoint:{identity}:{slot}"))
        first_notice = not seen and not outbox
        new_event = False
        for event_kind, event_key in event_specs:
            if event_key in seen or event_key in known:
                continue
            new_event = True
            notice_texts = _notice_texts({**run, "row_id": str(identity)}, event_kind,
                observation, record_locator, sources or previous.get("sources", []),
                _experiment_lines(experiment(), first=first_notice, final=event_kind == "final"))
            outbox.append({"key": event_key, "kind": event_kind,
                           "recipients": _notice_targets(run),
                           "texts": notice_texts, "text": notice_texts[0]})
            known.add(event_key)
        previous_observation = previous.get("observation") if isinstance(previous.get("observation"), dict) else {}
        previous_stagnation = previous_observation.get("stagnation") \
            if isinstance(previous_observation.get("stagnation"), dict) else {}
        stagnation_changed = any(stagnation.get(key) != previous_stagnation.get(key)
            for key in ("state", "episode_id", "last_increase_at", "first_observed_at", "generation"))
        next_state = {"schema_version": OBSERVATION_VERSION, "run_id": run_id,
                      "row_key": str(identity), "cursor": cursor, "samples": samples,
                      "sources": sources,
                      "schedule_started_at": schedule_started,
                      "clock_scope": "row" if clock_known else "unknown",
                      "row_started_at": float(started) if clock_known else None,
                      "last_observer_poll_at": float(now),
                      "last_observed_at": float(now) if should_read_sources else last_observed_at,
                      "last_admission_observed_at": (float(now) if should_read_admission
                          else last_admission_observed_at),
                      "progress_tracker": progress_tracker,
                      "eta_attention": eta_episode,
                      "progress_sample": next_progress,
                      "progress_at": (progress_tracker.get("last_sample_at")
                                      if isinstance(progress_tracker, dict) else None),
                      "admission_latest": admission_decoded.get("latest"),
                      "admission_coverage": admission_decoded.get("coverage", "unknown"),
                      "admission_summary": admission_summary,
                      "source_bindings": retained_bindings,
                      "source_observations": source_observations,
                      "last_checkpoint_at": (float(now) if observation["checkpoint_due"]
                                             else previous.get("last_checkpoint_at")),
                      "notified_decision_point": (
                          _decision_mark(experiment(), samples) or
                          previous.get("notified_decision_point")) if new_event or quiet_checkpoint
                          else previous.get("notified_decision_point"),
                      "last_public_write_at": previous.get("last_public_write_at"),
                      "last_public_observed_at": previous.get("last_public_observed_at"),
                      "public_status": previous.get("public_status"),
                      "last_delivery_at": previous.get("last_delivery_at"),
                      **admission_state,
                      "observation": observation,
                      "warnings": sorted(set([*previous.get("warnings", []), *warnings]))[-64:],
                      "event_keys": list(seen)[-256:], "outbox": outbox[-32:]}
        record_path = None
        # The observed time a record carries is when the run was last seen: a change measured now, or
        # the caller's own evidence that it is alive (`alive_at`: a process confirmed now, a compute
        # run's own files last changed). A refresh with nothing new carries that evidence time, never
        # "now", so the 900 s the board allows is counted once from the run's last sign of life; a
        # tracked progress source that can no longer be read is not vouched for (RV-PR1619-1114).
        last_seen = previous.get("last_public_observed_at", previous.get("last_public_write_at"))
        evidence_at = None
        if alive is True and not final:
            evidence_at = float(now)
            if (isinstance(alive_at, (int, float)) and not isinstance(alive_at, bool)
                    and math.isfinite(float(alive_at))):
                evidence_at = min(float(now), float(alive_at))
        refresh_due = bool(evidence_at is not None and not progress_lost
                           and isinstance(last_seen, (int, float)) and not isinstance(last_seen, bool)
                           and evidence_at - float(last_seen) >= PUBLIC_REFRESH_SECONDS)
        news = bool(
            previous.get("last_public_write_at") is None or final
            or status != previous.get("public_status")
            or observation["checkpoint_due"] or new_event
            or bool(new_samples) or sources != previous.get("sources", [])
            or source_observations_changed
            or admission_changed or stagnation_changed
            or should_read_admission and bool(admission_summary.get("sample_count")))
        publish_public = bool(artifact_root and (news or refresh_due))
        observed_at = float(now) if news else evidence_at
        if publish_public:
            published = (observation if observed_at == float(now)
                         else _observation_as_of(observation, observed_at, now))
            record = public_run_record(run, status=status, observation=published,
                metrics=next_state["sources"], started_at=run.get("started_at"),
                ended_at=(run.get("ended_at") if run.get("ended_at") is not None else now)
                    if final else None)
            record["source_warnings"] = sorted(set(warnings))
            row_entry = {"row_id": str(identity),
                         "started_at": float(started) if clock_known else None,
                         "clock_scope": "row" if clock_known else "unknown",
                         "output_dir": str(metric_root) if metric_root else None,
                         "observed_counters": source_observations,
                         "last_checkpoint": record["last_checkpoint"],
                         "metric_summary": record["metric_summary"],
                         "elapsed_seconds": published.get("elapsed_seconds"),
                         "ended_at": (run.get("ended_at") if run.get("ended_at") is not None else now)
                            if final else None,
                         "planned_seconds": published.get("planned_seconds"),
                         "eta": published.get("eta_seconds"),
                         "admission": published.get("admission"),
                         "stagnation": published.get("stagnation")}
            record["rows"] = [row_entry]
            record["output_dir"] = str(metric_root) if metric_root is not None else None
            record["output_dir_state"], record["output_dir_reason"] = _public_output_state(
                run, record["output_dir"], binding_given=bool(root_value) and metric_root is None,
                discovered_reason=discovered_reason)
            record_path = write_public_run_record(artifact_root, run_id, record)
            if record_path:
                next_state["last_public_write_at"] = float(now)
                next_state["last_public_observed_at"] = observed_at
                next_state["public_status"] = status
        state_changed = (not previous or new_event or should_read_sources
                         and (cursor != previous.get("cursor") or bool(new_samples)
                              or sources != previous.get("sources", []))
                         or next_progress != previous.get("progress_sample")
                         or eta_episode != previous.get("eta_attention")
                         or admission_changed or stagnation_changed
                         or source_observations_changed
                         or should_read_admission
                         or observation["checkpoint_due"] or final
                         or warnings != []
                         or next_state["last_observer_poll_at"] != previous_poll)
        if record_path or state_changed:
            if record_path:
                next_state["last_public_write_at"] = float(now)
                next_state["last_public_observed_at"] = observed_at
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
                "admission": admission_summary, "stagnation": stagnation,
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
    if "compaction" not in value:
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
                               "started_at", "verdict")
            for key in identity_fields:
                if value.get(key) in (None, "") and existing.get(key) not in (None, ""):
                    value[key] = existing[key]
        value = _compact_public_record(
            value, existing, check_existing=True,
            incoming_row_ids=[row["row_id"] for row in record.get("rows") or []
                              if isinstance(row, dict) and isinstance(row.get("row_id"), str)])
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) + 1 > MAX_PUBLIC_RUN_RECORD_WRITE_BYTES:
            return None  # unreachable: the compaction ladder ends in a bounded skeleton
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


class _PublicRunDirectory:
    def __init__(self, fd, path):
        self.fd, self.path = fd, path

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)


def _open_public_directory(artifact_root):
    """Bind every ancestor without following a link, once for this passive scan."""
    path = public_record_path(artifact_root, 'read-root-probe')
    if path is None:
        return None
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return _PublicRunDirectory(fd, path.parent)
    except BaseException:
        os.close(fd)
        raise


def _read_public_in_scan(artifact_root, run_id):
    from resource_run_registry import passive_handle, passive_parse
    if not isinstance(run_id, str) or not _SAFE_RUN_ID.fullmatch(run_id):
        return None
    def directory():
        try:
            return _open_public_directory(artifact_root)
        except (OSError, ValueError, TypeError, RuntimeError):
            return None
    held = passive_handle(('public-directory', str(artifact_root)), directory)
    if held is None:
        return None
    name = run_id + '.json'
    signature = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_size,
                            st.st_mtime_ns, st.st_ctime_ns)
    key = ('public', str(held.path / name))
    try:
        present = os.stat(name, dir_fd=held.fd, follow_symlinks=False)
    except FileNotFoundError:
        present = None
    if present is not None and stat.S_ISREG(present.st_mode):
        raw = _generation_hit(key, signature(present))
        if raw is not None:
            value = passive_parse(key, raw, (_json,), lambda: _json(raw))
            return value if value.get('run_id') == run_id else None
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=held.fd)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_PUBLIC_RUN_RECORD_BYTES:
            return None
        raw = source.read(MAX_PUBLIC_RUN_RECORD_BYTES + 1)
        after = os.fstat(source.fileno())
    current = os.stat(name, dir_fd=held.fd, follow_symlinks=False)
    if (len(raw) > MAX_PUBLIC_RUN_RECORD_BYTES or signature(before) != signature(after)
            or signature(after) != signature(current)):
        return None
    _generation_store(key, signature(current), raw)
    value = passive_parse(key, raw, (_json,), lambda: _json(raw))
    return value if value.get('run_id') == run_id else None


def read_public_run_record(artifact_root, run_id):
    from resource_run_registry import passive_read_active
    if passive_read_active() and hasattr(os, 'O_PATH'):
        try:
            return _read_public_in_scan(artifact_root, run_id)
        except (OSError, ValueError, TypeError, KeyError):
            return None
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
        from resource_run_registry import passive_parse
        value = passive_parse(('public', str(path)), raw, (_json,), lambda: _json(raw))
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


# Settled resource files are re-read every Fleet pass although they rarely
# change. A file whose full generation (device, inode, size, mtime, ctime) is
# unchanged since a verified read yields that read's bytes again; any change,
# link or failure takes the normal verified read.
_GENERATION_LIMIT = 4096
_GENERATION_READS = {}


def _generation_hit(key, signature):
    cached = _GENERATION_READS.get(key)
    return cached[1] if cached is not None and cached[0] == signature else None


def _generation_store(key, signature, value):
    if len(_GENERATION_READS) >= _GENERATION_LIMIT and key not in _GENERATION_READS:
        _GENERATION_READS.pop(next(iter(_GENERATION_READS)))
    _GENERATION_READS[key] = (signature, value)


def _read(path: Path, root: Path):
    """One regular, root-contained file; reject links, swaps and growing reads."""
    from resource_run_registry import passive_parent_identity
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("path-traversal")
    relative = path.relative_to(root)
    parents = []
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if current == path:
            info = current.lstat()
            device, inode, mode = info.st_dev, info.st_ino, info.st_mode
        else:
            device, inode, mode = passive_parent_identity(current)
        if stat.S_ISLNK(mode):
            raise ValueError("progress-link")
        parents.append((current, device, inode))
    key = ("progress", str(path))
    if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES:
        reused = _generation_hit(key, _signature(info))
        if reused is not None:
            # Same bytes; the closing parent check still observes this pass.
            for parent, device, inode in parents:
                current_info = parent.lstat()
                if stat.S_ISLNK(current_info.st_mode) or (current_info.st_dev, current_info.st_ino) != (device, inode):
                    raise ValueError("progress-path-changed")
            return reused
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
    _generation_store(key, _signature(last), (data, last.st_mtime))
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


def _request_output(run):
    """A launch's hash-bound request may name the output directly (C1, 2026-10-25).

    Returns (folder, reason) like `discover_output_binding`; (None, None) means the
    request is absent or unproven, so discovery continues with the config reference.
    """
    argv = run.get("command")
    if not isinstance(argv, list):
        return None, None
    def argument(name):
        values = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == name]
        values += [value[len(name) + 1:] for value in argv if value.startswith(name + '=')]
        return values[0] if len(values) == 1 else None
    reference, expected = argument('--request'), argument('--request-sha256')
    if not reference or not expected:
        return None, None
    try:
        path = Path(reference)
        if not path.is_absolute():
            path = Path(run['cwd']) / path
        if _namespace_source_missing(path, run.get('path_namespace')):
            return None, "remote-unmapped"
        path, mapped = _reverse_namespace_path(path, run.get('path_namespace'))
        if run.get('path_namespace') and not mapped:
            return None, None
        raw, _ = _read_unrooted(path)
        if hashlib.sha256(raw).hexdigest() != _hash(expected):
            return None, None
        request = _json(raw)
        if not isinstance(request, dict):
            return None, None
        values = {value for key in ('output', 'output_dir', 'output_folder', 'save_dir')
                  if isinstance((value := request.get(key)), str) and value}
        if len(values) != 1:
            return None, None
        value = next(iter(values))
        # Reuse the existing namespace, link and directory checks.
        return discover_output_binding({**run, 'observation': {'path': value}, 'command': []})
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None, None


def _namespace_source_missing(path, namespace):
    """A run-side path maps through a namespace source this observer does not mount."""
    if not isinstance(namespace, dict):
        return False
    for target, source in namespace.items():
        try:
            Path(path).relative_to(Path(target))
        except (ValueError, TypeError):
            continue
        try:
            return not Path(source).is_dir()
        except (OSError, TypeError, ValueError):
            return True
    return False


def discover_output_root(run):
    """Resolve only explicit output-directory keys in an exact run config."""
    return discover_output_binding(run)[0]


def discover_output_binding(run):
    """Return (folder, reason) from the run's explicit output binding evidence.

    `folder` is an existing directory and `reason` is None, or `folder` is None and
    `reason` is `not-declared` (no output binding evidence), `declared-unresolved`
    (a named folder is not an existing directory here) or `remote-unmapped` (a named
    path cannot be mapped from the run's host to this observer). Nothing is guessed.
    """
    if not isinstance(run, dict):
        return None, "not-declared"
    override = run.get("observation")
    namespace = run.get("path_namespace")
    raw_run_root = run.get("run_root")
    override_unmapped = False
    if isinstance(override, dict) and isinstance(override.get("path"), str):
        declared = Path(override["path"])
        candidate, mapped = _reverse_namespace_path(declared, namespace)
        shared = False
        if raw_run_root:
            shared_root, _ = _reverse_namespace_path(Path(raw_run_root), namespace)
            try: shared = candidate.is_relative_to(shared_root)
            except ValueError: shared = False
        if candidate.is_absolute() and (mapped or not namespace or shared) and not _path_has_symlink(candidate):
            if mapped and _namespace_source_missing(declared, namespace):
                return None, "remote-unmapped"
            try:
                resolved = candidate.resolve(strict=True)
                return (str(resolved), None) if resolved.is_dir() else (None, "declared-unresolved")
            except (OSError, RuntimeError, ValueError):
                return None, "declared-unresolved"
        override_unmapped = candidate.is_absolute() and bool(namespace) and not (mapped or shared)
    command = run.get("command")
    request_output, request_reason = _request_output(run)
    if request_output:
        return request_output, None
    fallback = request_reason or ("remote-unmapped" if override_unmapped else "not-declared")
    cwd = run.get("cwd")
    reference = _config_argument(command) if isinstance(command, list) else None
    if not reference or not isinstance(cwd, str) or not cwd:
        return None, fallback
    try:
        raw_cwd = Path(cwd)
        config_ref = Path(reference)
        if (_namespace_source_missing(raw_cwd, namespace) or config_ref.is_absolute()
                and _namespace_source_missing(config_ref, namespace)):
            return None, "remote-unmapped"
        mapped_cwd, _cwd_mapped = _reverse_namespace_path(raw_cwd, namespace)
        base = mapped_cwd.resolve(strict=True)
        if config_ref.is_absolute():
            config_ref, _config_mapped = _reverse_namespace_path(config_ref, namespace)
            reference = str(config_ref)
        resolved = resolve_config(base, reference)
        config_path = Path(resolved["path"])
        raw, _ = _read_unrooted(config_path)
        expected = run.get("config_sha256")
        digest = hashlib.sha256(raw).hexdigest()
        if expected and digest != _hash(expected):
            return None, fallback
        if config_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
                config = yaml.safe_load(raw)
            except (ImportError, ValueError, TypeError):
                return None, fallback
        else:
            config = _json(raw)
        if not isinstance(config, dict):
            return None, fallback
        training = config.get("training") if isinstance(config.get("training"), dict) else {}
        candidates = []
        for value in (config.get("output_dir"), config.get("output_folder"),
                      config.get("save_dir"), training.get("output_dir")):
            if value is None:
                continue
            if not isinstance(value, str) or not value or len(value) > 4096 \
                    or any(ord(char) < 32 for char in value):
                return None, fallback
            raw_path = Path(value)
            candidate = raw_path if raw_path.is_absolute() else base / raw_path
            mapped = False
            if raw_path.is_absolute():
                if _namespace_source_missing(raw_path, namespace):
                    return None, "remote-unmapped"
                candidate, mapped = _reverse_namespace_path(candidate, namespace)
                if namespace and not mapped:
                    shared_root = None
                    if raw_run_root:
                        shared_root, _ = _reverse_namespace_path(Path(raw_run_root), namespace)
                    try: shared = shared_root is not None and candidate.is_relative_to(shared_root)
                    except ValueError: shared = False
                    if not shared:
                        return None, "remote-unmapped"
            if _path_has_symlink(candidate):
                return None, "declared-unresolved"
            candidates.append(candidate)
        unique = {str(path.resolve(strict=False)) for path in candidates}
        if len(unique) != 1:
            # No key, or keys naming different folders: no single binding.
            return None, fallback
        output = Path(next(iter(unique)))
        return (str(output), None) if output.is_dir() else (None, "declared-unresolved")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError,
            subprocess.SubprocessError, OverflowError):
        return None, fallback


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
