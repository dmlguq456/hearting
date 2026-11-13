#!/usr/bin/env python3
"""Project experiment catalog: what each experiment condition is called, and what it is compared with.

File (public, one per project artifact root):
  .runtime/artifact-producer/v1/experiment-catalog.json     contract `experiment-catalog/v1`

This module owns names, goals, and comparison meaning only.  Live values (progress, ETA, metric
points, stagnation) stay in the public run record `<artifact-root>/resource-runs/<run_id>.json`,
which `resource_progress` owns; `status` reads both and never copies a live value into the catalog.

Every writer goes through `run_write`: one admission lock, one validation, one atomic replace, one
history recorder (`artifact_history`, `kind=meta`, `target.type=project`).  Reads, `show`, `status`,
`lint`, and `--dry-run` take no lock and create nothing.

Nothing here is a gate, an input requirement, or an obligation for an agent or a user.  A missing
name, goal, or target is a warning that names its place in the catalog; it never stops a run or a
write.  A missing value stays missing: no zero, no estimate, no difference between values that are
not measured under the same conditions.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_admission as admission  # noqa: E402
import artifact_history as H  # noqa: E402
import artifact_lifecycle as lifecycle  # noqa: E402
import artifact_meta as meta  # noqa: E402
import artifact_producer as producer  # noqa: E402
import artifact_workflow_groups as W  # noqa: E402
import resource_progress as rp  # noqa: E402

CONTRACT = "experiment-catalog/v1"
STATUS_CONTRACT = "experiment-status/v1"
CATALOG_REL = ".runtime/artifact-producer/v1/experiment-catalog.json"
SIZE_CAP = 1024 * 1024

MAX_CONDITIONS = 256
MAX_GROUPS = 64
MAX_TERMS = 128
MAX_RUN_REFS = 32
MAX_ALIASES = 16
MAX_FORMER = 16
MAX_PATH_REFS = 16
NAME_MAX = 80
GOAL_MAX = 200
MEANING_MAX = 200
CHANGES_MAX = 300
PROBLEM_MAX = 200
PATH_MAX = 300
UNLINKED_MAX = 64
# The raw observation cadence is 60 s; a running row not observed for 15 cadences is called stale.
# One constant with its reason, not a setting: a remote run that is observed rarely may read stale.
STALE_OBSERVATION_SECONDS = 900

GROUP_STATUSES = ("planned", "running", "done", "stopped", "unknown")
DIRECTIONS = ("min", "max")
TARGET_KINDS = ("fixed", "condition")
VALIDATION_SPLITS = frozenset(("valid", "val", "validation"))
FAILED_STATUSES = frozenset(("failed", "error", "crashed"))
ID_RE = {"grp": re.compile(r"grp_[0-9a-f]{12}\Z"), "cnd": re.compile(r"cnd_[0-9a-f]{12}\Z")}
RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")

TOP_ORDER = ("schema_version", "contract", "artifact_root_id", "revision", "updated_at",
             "terms", "groups", "conditions", "merged")
TERM_ORDER = ("term", "meaning", "note")
GROUP_ORDER = ("id", "display_name", "goal", "metric", "target", "status", "results")
METRIC_ORDER = ("key", "unit", "direction", "split", "scoring")
TARGET_ORDER = ("kind", "label", "value", "condition", "split", "scoring")
CONDITION_ORDER = ("id", "group", "display_name", "aliases", "former_names", "changes",
                   "compared_to", "problem", "refs")
REFS_ORDER = ("runs", "output_dirs", "configs")
MERGED_ORDER = ("from", "into", "at")


class CatalogError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class CatalogRead:
    status: str  # missing | ok | invalid
    path: Path
    doc: Optional[Dict[str, Any]] = None
    code: Optional[str] = None
    revision: Optional[int] = None
    digest: Optional[str] = None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _rfc3339(epoch: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def _ts(value: Any) -> Optional[float]:
    """A time as epoch seconds from a number or an RFC 3339 `...Z` string; anything else is unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str) and RFC3339.fullmatch(value):
        import calendar
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _fold(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _new_id(kind: str) -> str:
    return f"{kind}_{os.urandom(6).hex()}"


def catalog_path(root: Path) -> Path:
    return Path(root) / CATALOG_REL


def _dumps(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(_ordered(doc), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _order_map(value: Mapping[str, Any], order: Sequence[str]) -> Dict[str, Any]:
    known = {key: value[key] for key in order if key in value}
    extra = {key: value[key] for key in sorted(value) if key not in order}
    return {**known, **extra}


def _ordered(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Keys in a fixed order so the same catalog is the same bytes; unknown fields are kept."""
    out = _order_map(doc, TOP_ORDER)
    out["terms"] = [_order_map(row, TERM_ORDER) for row in doc.get("terms", [])]
    groups = []
    for row in doc.get("groups", []):
        row = _order_map(row, GROUP_ORDER)
        if isinstance(row.get("metric"), dict):
            row["metric"] = _order_map(row["metric"], METRIC_ORDER)
        if isinstance(row.get("target"), dict):
            row["target"] = _order_map(row["target"], TARGET_ORDER)
        groups.append(row)
    out["groups"] = groups
    conditions = []
    for row in doc.get("conditions", []):
        row = _order_map(row, CONDITION_ORDER)
        if isinstance(row.get("refs"), dict):
            row["refs"] = _order_map(row["refs"], REFS_ORDER)
        conditions.append(row)
    out["conditions"] = conditions
    out["merged"] = [_order_map(row, MERGED_ORDER) for row in doc.get("merged", [])]
    return out


# ---------------------------------------------------------------------------
# validation (known fields only; unknown fields are kept and never rejected)
# ---------------------------------------------------------------------------


def _text(value: Any, maximum: int, where: str, *, allow_empty: bool = True) -> str:
    try:
        return meta.check_text(value, maximum, "text-invalid", allow_empty=allow_empty)
    except meta.MetaError as exc:
        raise CatalogError("schema-invalid", where) from exc


def _list(value: Any, maximum: int, where: str) -> List[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CatalogError("schema-invalid", where)
    return value


def _dict(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError("schema-invalid", where)
    return value


def _check_run_ref(ref: Any, where: str) -> None:
    ref = _dict(ref, where)
    run_id = ref.get("run_id")
    if not isinstance(run_id, str) or not rp._SAFE_RUN_ID.fullmatch(run_id):
        raise CatalogError("schema-invalid", where + ".run_id")
    row = ref.get("row_id")
    if row is not None:
        _text(row, 160, where + ".row_id", allow_empty=False)


def _check_metric(metric: Any, where: str) -> None:
    metric = _dict(metric, where)
    _text(metric.get("key"), 80, where + ".key", allow_empty=False)
    _text(metric.get("unit", ""), 20, where + ".unit")
    if metric.get("direction") not in DIRECTIONS:
        raise CatalogError("schema-invalid", where + ".direction")
    for name in ("split", "scoring"):
        if metric.get(name) is not None:
            _text(metric[name], 120, f"{where}.{name}")


def _check_target(target: Any, where: str) -> None:
    target = _dict(target, where)
    kind = target.get("kind")
    if kind not in TARGET_KINDS:
        raise CatalogError("schema-invalid", where + ".kind")
    if kind == "fixed":
        if _num(target.get("value")) is None:
            raise CatalogError("schema-invalid", where + ".value")
        _text(target.get("label", ""), NAME_MAX, where + ".label")
    elif not isinstance(target.get("condition"), str) or not ID_RE["cnd"].fullmatch(target["condition"]):
        raise CatalogError("schema-invalid", where + ".condition")
    for name in ("split", "scoring"):
        if target.get(name) is not None:
            _text(target[name], 120, f"{where}.{name}")


def check_doc(doc: Any, *, strict: bool = True) -> Dict[str, Any]:
    """Raise CatalogError('schema-invalid', where) on a known-field violation.

    `strict` (writes) also checks text limits and that every reference points somewhere; a read only
    checks the structure it relies on, so a hand edit with a long name is still readable."""
    if not isinstance(doc, dict):
        raise CatalogError("schema-invalid", "root")
    if type(doc.get("schema_version")) is not int or doc.get("schema_version") != 1 \
            or doc.get("contract") != CONTRACT:
        raise CatalogError("contract-unknown")
    if not isinstance(doc.get("artifact_root_id"), str) or not doc["artifact_root_id"]:
        raise CatalogError("schema-invalid", "artifact_root_id")
    if type(doc.get("revision")) is not int or doc["revision"] < 0:
        raise CatalogError("schema-invalid", "revision")
    if not isinstance(doc.get("updated_at"), str) or not RFC3339.fullmatch(doc["updated_at"]):
        raise CatalogError("schema-invalid", "updated_at")
    terms = _list(doc.get("terms"), MAX_TERMS, "terms")
    groups = _list(doc.get("groups"), MAX_GROUPS, "groups")
    conditions = _list(doc.get("conditions"), MAX_CONDITIONS, "conditions")
    merged = _list(doc.get("merged"), MAX_CONDITIONS * 4, "merged")

    def text(value: Any, maximum: int, where: str, allow_empty: bool = True) -> None:
        if strict:
            _text(value, maximum, where, allow_empty=allow_empty)
        elif not isinstance(value, str):
            raise CatalogError("schema-invalid", where)

    seen_terms = set()
    for index, row in enumerate(terms):
        where = f"terms[{index}]"
        row = _dict(row, where)
        text(row.get("term"), NAME_MAX, where + ".term", False)
        text(row.get("meaning", ""), MEANING_MAX, where + ".meaning")
        text(row.get("note", ""), MEANING_MAX, where + ".note")
        if row["term"] in seen_terms:
            raise CatalogError("schema-invalid", where + ".term-duplicate")
        seen_terms.add(row["term"])
    group_ids = set()
    for index, row in enumerate(groups):
        where = f"groups[{index}]"
        row = _dict(row, where)
        if not isinstance(row.get("id"), str) or not ID_RE["grp"].fullmatch(row["id"]) or row["id"] in group_ids:
            raise CatalogError("schema-invalid", where + ".id")
        group_ids.add(row["id"])
        text(row.get("display_name", ""), NAME_MAX, where + ".display_name")
        text(row.get("goal", ""), GOAL_MAX, where + ".goal")
        if row.get("metric") is not None:
            _check_metric(row["metric"], where + ".metric")
        if row.get("target") is not None:
            _check_target(row["target"], where + ".target")
        if row.get("status", "unknown") not in GROUP_STATUSES:
            raise CatalogError("schema-invalid", where + ".status")
        if row.get("results") is not None:
            text(row["results"], PATH_MAX, where + ".results")
    condition_ids = set()
    for index, row in enumerate(conditions):
        where = f"conditions[{index}]"
        row = _dict(row, where)
        if not isinstance(row.get("id"), str) or not ID_RE["cnd"].fullmatch(row["id"]) \
                or row["id"] in condition_ids:
            raise CatalogError("schema-invalid", where + ".id")
        condition_ids.add(row["id"])
        text(row.get("display_name", ""), NAME_MAX, where + ".display_name")
        text(row.get("changes", ""), CHANGES_MAX, where + ".changes")
        text(row.get("problem", ""), PROBLEM_MAX, where + ".problem")
        for name, maximum in (("aliases", MAX_ALIASES), ("former_names", MAX_FORMER)):
            for item in _list(row.get(name, []), maximum, f"{where}.{name}"):
                text(item, NAME_MAX, f"{where}.{name}", False)
        refs = _dict(row.get("refs", {}), where + ".refs")
        for ref_index, ref in enumerate(_list(refs.get("runs", []), MAX_RUN_REFS, where + ".refs.runs")):
            _check_run_ref(ref, f"{where}.refs.runs[{ref_index}]")
        for name in ("output_dirs", "configs"):
            for item in _list(refs.get(name, []), MAX_PATH_REFS, f"{where}.refs.{name}"):
                text(item, PATH_MAX, f"{where}.refs.{name}", False)
    for index, row in enumerate(merged):
        where = f"merged[{index}]"
        row = _dict(row, where)
        if not isinstance(row.get("from"), str) or not isinstance(row.get("into"), str):
            raise CatalogError("schema-invalid", where)
    if strict:
        for index, row in enumerate(conditions):
            if row.get("group") is not None and row["group"] not in group_ids:
                raise CatalogError("group-unknown", f"conditions[{index}].group")
            if row.get("compared_to") is not None and row["compared_to"] not in condition_ids:
                raise CatalogError("compared-to-unknown", f"conditions[{index}].compared_to")
        for index, row in enumerate(groups):
            target = row.get("target")
            if isinstance(target, dict) and target.get("kind") == "condition" \
                    and target["condition"] not in condition_ids:
                raise CatalogError("compared-to-unknown", f"groups[{index}].target.condition")
    return doc


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def load(root: Path) -> CatalogRead:
    """Read the catalog without a lock, without writing, without creating a directory."""
    root = Path(root)
    path = catalog_path(root)
    try:
        W._no_symlink(root, Path(CATALOG_REL))
        raw = W._regular(path, missing=True, cap=SIZE_CAP)
    except W.WorkflowGroupError as exc:
        return CatalogRead("invalid", path, code="path-unsafe")
    if raw is None:
        return CatalogRead("missing", path)
    digest = H.digest_of(raw)
    try:
        doc = W._json(raw)
    except W.WorkflowGroupError:
        return CatalogRead("invalid", path, code="json-invalid", digest=digest)
    try:
        ident = lifecycle.read_root_identity(root)
    except lifecycle.LifecycleError:
        return CatalogRead("invalid", path, code="root-identity-unreadable", digest=digest)
    if ident is None:
        return CatalogRead("invalid", path, code="root-identity-missing", digest=digest)
    try:
        check_doc(doc, strict=False)
    except CatalogError as exc:
        return CatalogRead("invalid", path, code=exc.code, digest=digest)
    if doc["artifact_root_id"] != ident.artifact_root_id:
        return CatalogRead("invalid", path, code="foreign-root", digest=digest)
    return CatalogRead("ok", path, doc=doc, revision=doc["revision"], digest=digest)


def _conditions_by_id(doc: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["id"]: row for row in doc.get("conditions", [])}


def _groups_by_id(doc: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["id"]: row for row in doc.get("groups", [])}


def _run_ref_keys(cond: Mapping[str, Any]) -> List[Tuple[str, Optional[str]]]:
    return [(ref["run_id"], ref.get("row_id")) for ref in (cond.get("refs") or {}).get("runs", [])]


def _claims(doc: Mapping[str, Any], token: str) -> List[Tuple[Dict[str, Any], str]]:
    """Every (condition, place) that answers to a token other than its stable id: display name,
    alias, former name (all folded), run id, or `run:row`."""
    folded = _fold(token)
    run_id, colon, row_id = token.partition(":")
    out: List[Tuple[Dict[str, Any], str]] = []
    for cond in doc.get("conditions", []):
        if cond.get("display_name") and _fold(cond["display_name"]) == folded:
            out.append((cond, "display_name"))
        for place in ("aliases", "former_names"):
            if any(_fold(name) == folded for name in cond.get(place, [])):
                out.append((cond, place))
        keys = _run_ref_keys(cond)
        if (any(key[0] == token for key in keys) if not colon
                else any(key in ((run_id, row_id), (run_id, None)) for key in keys)):
            out.append((cond, "refs.runs"))
    return out


def _ambiguity_detail(doc: Mapping[str, Any], token: Any) -> str:
    """`token → cnd_a(former_names), cnd_b(display_name)`: what a refused write overlaps with."""
    places = ", ".join(f"{cond['id']}({place})" for cond, place in _claims(doc, str(token)))
    return f"{str(token)[:80]} → {places}"[:400]


def resolve_ex(doc: Optional[Mapping[str, Any]], token: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """Find one condition by id, display name, alias, former name, run id, `run:row`, or merged id.

    A stable id decides alone.  Every other kind of name is gathered together, so a former name or
    a run id is never hidden behind another condition's current name: two different conditions
    answering to the token is `ambiguous` and nothing is picked (the condition id still works).
    Returns `(condition, "ok" | "unknown" | "ambiguous")`."""
    if not doc or not isinstance(token, str) or not token:
        return None, "unknown"
    by_id = _conditions_by_id(doc)
    if token in by_id:
        return by_id[token], "ok"
    claims = _claims(doc, token)
    owners = {cond["id"] for cond, _place in claims}
    if len(owners) == 1:
        return claims[0][0], "ok"
    if len(owners) > 1:
        return None, "ambiguous"
    current = token
    for _ in range(64):  # a merged id follows its chain to a condition that still exists
        hop = next((row["into"] for row in doc.get("merged", []) if row.get("from") == current), None)
        if hop is None:
            break
        current = hop
        if current in by_id:
            return by_id[current], "ok"
    return None, "unknown"


def resolve(doc: Optional[Mapping[str, Any]], token: Any) -> Optional[Dict[str, Any]]:
    return resolve_ex(doc, token)[0]


def _resolve_group(doc: Mapping[str, Any], token: Any) -> Optional[Dict[str, Any]]:
    groups = doc.get("groups", [])
    by_id = _groups_by_id(doc)
    if isinstance(token, str) and token in by_id:
        return by_id[token]
    found = [g for g in groups if isinstance(token, str) and g.get("display_name")
             and _fold(g["display_name"]) == _fold(token)]
    return found[0] if len(found) == 1 else None


def describe_run(doc: Optional[Mapping[str, Any]], run_id: str, row_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The catalog name for a run (and row): what notices, cards, and tables call it.  None when
    the run is not linked, when two conditions claim it, or when another condition answers to the
    run id by name — the same judgment `resolve_ex` makes for that token (nothing is guessed)."""
    if not doc or not isinstance(run_id, str):
        return None
    found = []
    for cond in doc.get("conditions", []):
        for ref_run, ref_row in _run_ref_keys(cond):
            if ref_run == run_id and (row_id is None or ref_row is None or ref_row == row_id):
                found.append(cond)
                break
    if len(found) != 1:
        return None
    cond = found[0]
    for token in [run_id] + ([f"{run_id}:{row_id}"] if row_id else []):
        if any(other["id"] != cond["id"] for other, place in _claims(doc, token) if place != "refs.runs"):
            return None
    group = _groups_by_id(doc).get(cond.get("group"))
    return {"name": cond.get("display_name") or None, "condition_id": cond["id"],
            "group_id": group["id"] if group else None,
            "group_name": (group or {}).get("display_name") or None,
            "goal": (group or {}).get("goal") or None, "path": CATALOG_REL}


def resolve_root(path: Optional[str] = None) -> Path:
    """The artifact root for a project path, through `artifact-root.sh`.

    With a path the caller's `AGENT_ARTIFACT_ROOT` is dropped, so asking about another project does
    not return this session's own root.  Without a path the environment decides, as for any agent."""
    script = Path(__file__).resolve().parent / "artifact-root.sh"
    env = dict(os.environ)
    if path is not None:
        env.pop("AGENT_ARTIFACT_ROOT", None)
    command = ["sh", str(script)] + ([str(path)] if path is not None else [])
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=10, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CatalogError("root-unresolved", type(exc).__name__) from exc
    out = done.stdout.strip()
    if done.returncode != 0 or not out:
        raise CatalogError("root-unresolved", (done.stderr or "").strip()[:120])
    return Path(out)


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------


def _split_name(value: Any) -> str:
    text = _fold(value).strip()
    return "validation" if text in VALIDATION_SPLITS else text


def _point_view(point: Optional[Mapping[str, Any]], label: Optional[str] = None) -> Dict[str, Any]:
    point = point or {}
    return {"value": point.get("value"), "epoch": point.get("epoch"), "label": label}


def _declaration_mismatch(metric: Mapping[str, Any], other: Any) -> Optional[str]:
    """Why a control measured under the declaration `other` cannot be subtracted from a value
    measured under `metric`; None when the two agree.  A control without a declaration (no group,
    or a group without a metric) cannot be checked, so it is unverified, never assumed alike."""
    if not isinstance(other, Mapping) or not other.get("key"):
        return "split-or-scoring-unverified"
    if other["key"] != metric["key"] or other.get("direction") != metric.get("direction"):
        return "metric-mismatch"
    if _fold(other.get("unit") or "").strip() != _fold(metric.get("unit") or "").strip():
        return "unit-mismatch"
    for name, normal in (("split", _split_name), ("scoring", _fold)):
        mine, theirs = metric.get(name) or None, other.get(name) or None
        if mine is None and theirs is None:
            continue  # neither side declares it: the same (silent) declaration, as within one group
        if mine is None or theirs is None:
            return "split-or-scoring-unverified"
        if normal(mine) != normal(theirs):
            return f"{name}-mismatch"
    return None


def compare(group: Optional[Mapping[str, Any]], cond_point: Optional[Mapping[str, Any]],
            target_point: Optional[Mapping[str, Any]] = None,
            target_metric: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The only place that decides whether two values may be subtracted.

    `cond_point` is `{"key", "epoch", "value", "nonfinite"}` for the condition's LATEST value (a
    best value is never mixed in).  A `fixed` target comes from the group; a `condition` target
    comes in as `target_point` (the control's latest point) with `target_metric`, the metric
    declaration of the control's own group: key, unit, direction, split, and scoring must agree
    with this group's before an epoch is looked at.  The first rule that applies wins."""
    group = group or {}
    metric = group.get("metric") if isinstance(group.get("metric"), dict) else None
    target = group.get("target") if isinstance(group.get("target"), dict) else None

    def result(state: str, reason: Optional[str], **extra: Any) -> Dict[str, Any]:
        base = {"state": state, "reason": reason, "value": _point_view(cond_point), "baseline": None,
                "delta": None, "delta_unit": None, "relative_percent": None, "better": None}
        base.update(extra)
        return base

    if not metric or not metric.get("key"):
        return result("no-target", "metric-undeclared")
    if not target:
        return result("no-target", "target-undeclared")
    missing = not isinstance(cond_point, Mapping) or (cond_point.get("value") is None
                                                      and not cond_point.get("nonfinite"))
    if missing:
        return result("no-data", "no-observation")
    fixed = target.get("kind") == "fixed"
    if not fixed and (target_point is None or (target_point.get("value") is None
                                               and not target_point.get("nonfinite"))):
        return result("no-data", "control-no-observation")
    baseline = ({"value": target.get("value"), "epoch": None, "label": target.get("label") or None}
                if fixed else _point_view(target_point))
    if cond_point.get("key") != metric["key"] or (not fixed and target_point.get("key") != metric["key"]):
        return result("not-comparable", "metric-mismatch", baseline=baseline)
    value, other = _num(cond_point.get("value")), _num(baseline["value"])
    if cond_point.get("nonfinite") or value is None or other is None:
        return result("not-comparable", "nonfinite", baseline=baseline)
    declared_split = metric.get("split") or None
    if fixed:
        if not target.get("split") or not target.get("scoring") or not declared_split \
                or not metric.get("scoring"):
            return result("not-comparable", "split-or-scoring-unverified", baseline=baseline)
        if _split_name(target["split"]) != _split_name(declared_split):
            return result("not-comparable", "split-mismatch", baseline=baseline)
        if _fold(target["scoring"]) != _fold(metric["scoring"]):
            return result("not-comparable", "scoring-mismatch", baseline=baseline)
    else:
        mismatch = _declaration_mismatch(metric, target_metric)
        if mismatch:
            return result("not-comparable", mismatch, baseline=baseline)
    if declared_split and _split_name(declared_split) != "validation":
        return result("not-comparable", "split-not-observed", baseline=baseline)
    if not fixed:
        epochs = (cond_point.get("epoch"), target_point.get("epoch"))
        if not all(type(e) is int for e in epochs):
            return result("not-comparable", "epoch-unverified", baseline=baseline)
        if epochs[0] != epochs[1]:
            return result("not-comparable", "epoch-mismatch", baseline=baseline)
    delta = round(value - other, 9)
    unit = metric.get("unit") or None
    relative = round(delta / abs(other) * 100, 6) if unit == "%" and other != 0 else None
    better = (delta < 0 if metric.get("direction") == "min" else delta > 0 if metric.get("direction") == "max"
              else None) if delta != 0 else None
    return result("ok", None, baseline=baseline, delta=delta, delta_unit="pp" if unit == "%" else unit,
                  relative_percent=relative, better=better)


# ---------------------------------------------------------------------------
# lint (warnings only; never blocks)
# ---------------------------------------------------------------------------


def _warn(code: str, where: str, detail: str = "") -> Dict[str, str]:
    return {"code": code, "where": where, "detail": detail}


def _identifiers(cond: Mapping[str, Any]) -> List[str]:
    refs = cond.get("refs") or {}
    names = [cond.get("id", "")]
    for ref in refs.get("runs", []):
        names += [ref.get("run_id") or "", ref.get("row_id") or ""]
    names += [os.path.basename(str(p).rstrip("/")) for p in refs.get("output_dirs", [])]
    names += [os.path.splitext(os.path.basename(str(p)))[0] for p in refs.get("configs", [])]
    names += list(cond.get("aliases", []))
    return [_fold(name) for name in names if name]


def _name_overlaps(conditions: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """`name-overlap`: one name that two conditions answer to, so `resolve` picks neither.

    A rename or register that reuses another condition's former name or run id lands; this says
    where.  Same-kind pairs already have their own code (`display-name-duplicate`,
    `alias-duplicate`), and one run split into rows across conditions is ordinary."""
    claims: Dict[str, Dict[str, List[str]]] = {}
    spelled: Dict[str, str] = {}
    for cond in conditions:
        names = [("display_name", cond.get("display_name") or "")]
        names += [(place, name) for place in ("aliases", "former_names") for name in cond.get(place, [])]
        for run_id, row_id in _run_ref_keys(cond):
            names.append(("refs.runs", run_id))
            if row_id:
                names.append(("refs.runs", f"{run_id}:{row_id}"))
        for place, name in names:
            if not name:
                continue
            folded = _fold(name)
            spelled.setdefault(folded, name)
            places = claims.setdefault(folded, {}).setdefault(cond["id"], [])
            if place not in places:
                places.append(place)
    out = []
    for folded, owners in claims.items():
        kinds = {place for places in owners.values() for place in places}
        if len(owners) < 2 or any(kinds <= {same} for same in ("display_name", "aliases", "refs.runs")):
            continue
        where = ", ".join(f"conditions[{cid}].{place}" for cid, places in owners.items() for place in places)
        out.append(_warn("name-overlap", where, spelled[folded]))
    return out


def lint(doc: Optional[Mapping[str, Any]], expect: Sequence[str] = (),
         unlinked: Sequence[str] = ()) -> List[Dict[str, str]]:
    """Warnings with the place they are about.  Always a list; callers never fail on it."""
    out: List[Dict[str, str]] = []
    for index, name in enumerate(expect):
        found, why = resolve_ex(doc, name)
        if found is None:
            out.append(_warn("name-overlap" if why == "ambiguous" else "condition-missing", f"expect[{index}]", str(name)))
    for run_id in unlinked:
        out.append(_warn("condition-missing", f"resource-runs/{run_id}.json", run_id))
    if not doc:
        return out
    where_name = lambda c: f"conditions[{c['id']}].display_name"  # noqa: E731
    conditions = doc.get("conditions", [])
    seen_names: Dict[str, str] = {}
    seen_alias: Dict[str, str] = {}
    term_names = {_fold(row["term"]): row["term"] for row in doc.get("terms", [])}
    ids = {c["id"] for c in conditions}
    for cond in conditions:
        name = cond.get("display_name") or ""
        if not name:
            out.append(_warn("display-name-missing", where_name(cond)))
        else:
            folded = _fold(name)
            if folded in _identifiers(cond):
                out.append(_warn("display-name-is-identifier", where_name(cond), name))
            if folded in seen_names:
                out.append(_warn("display-name-duplicate", f"{seen_names[folded]}, {where_name(cond)}", name))
            else:
                seen_names[folded] = where_name(cond)
            if folded in term_names:
                out.append(_warn("term-conflict", where_name(cond), term_names[folded]))
        for alias in cond.get("aliases", []):
            folded = _fold(alias)
            if folded in seen_alias and seen_alias[folded] != cond["id"]:
                out.append(_warn("alias-duplicate", f"conditions[{cond['id']}].aliases", alias))
            seen_alias.setdefault(folded, cond["id"])
            if folded in term_names:
                out.append(_warn("term-conflict", f"conditions[{cond['id']}].aliases", alias))
        if cond.get("compared_to") is not None and cond["compared_to"] not in ids:
            out.append(_warn("compared-to-missing", f"conditions[{cond['id']}].compared_to", str(cond["compared_to"])))
    out.extend(_name_overlaps(conditions))
    for group in doc.get("groups", []):
        gid = f"groups[{group['id']}]"
        if not group.get("goal"):
            out.append(_warn("group-goal-missing", gid + ".goal"))
        metric = group.get("metric")
        if not isinstance(metric, dict) or not metric.get("key"):
            out.append(_warn("metric-undeclared", gid + ".metric"))
        elif not metric.get("unit"):
            out.append(_warn("metric-unit-missing", gid + ".metric.unit"))
        if not isinstance(group.get("target"), dict):
            out.append(_warn("target-undeclared", gid + ".target"))
    return out


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


class Ctx:
    """What a mutation sees: a private copy of the latest catalog and its own warnings."""

    def __init__(self, doc: Dict[str, Any]) -> None:
        self.doc = doc

    def condition(self, token: Any, unknown: str = "condition-unknown") -> Dict[str, Any]:
        """The one condition a write names; an ambiguous name is refused with what it overlaps."""
        cond, why = resolve_ex(self.doc, token)
        if cond is None:
            if why == "ambiguous":
                raise CatalogError("condition-ambiguous", _ambiguity_detail(self.doc, token))
            raise CatalogError(unknown, str(token)[:80])
        return cond

    def linked_elsewhere(self, run_id: str, row_id: Optional[str], owner: Optional[str]) -> Optional[str]:
        for cond in self.doc["conditions"]:
            if cond["id"] == owner:
                continue
            for ref_run, ref_row in _run_ref_keys(cond):
                if ref_run == run_id and (row_id is None or ref_row is None or ref_row == row_id):
                    return cond["id"]
        return None


def _empty_doc(root_id: str, now: float) -> Dict[str, Any]:
    return {"schema_version": 1, "contract": CONTRACT, "artifact_root_id": root_id, "revision": 0,
            "updated_at": _rfc3339(now), "terms": [], "groups": [], "conditions": [], "merged": []}


def _set_name(cond: Dict[str, Any], new: str) -> None:
    old = cond.get("display_name") or ""
    if old and old != new:
        former = [n for n in cond.get("former_names", []) if n != old and n != new]
        cond["former_names"] = (former + [old])[-MAX_FORMER:]
    cond["display_name"] = new


def _diff_events(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[Tuple[str, str, Any, Any]]:
    """(operation, field, before value, after value) for every changed place, by stable id."""
    changes: List[Tuple[str, str, Any, Any]] = []

    def table(name: str, key: str) -> None:
        old = {row[key]: row for row in before.get(name, [])}
        new = {row[key]: row for row in after.get(name, [])}
        for ident in new:
            path = f"experiments.{name}.{ident}"
            if ident not in old:
                changes.append(("add", path, None, new[ident]))
                continue
            if old[ident] == new[ident]:
                continue
            for field in [*new[ident], *[k for k in old[ident] if k not in new[ident]]]:
                if old[ident].get(field) != new[ident].get(field):
                    changes.append(("update", f"{path}.{field}", old[ident].get(field), new[ident].get(field)))
        for ident in old:
            if ident not in new:
                changes.append(("delete", f"experiments.{name}.{ident}", old[ident], None))

    table("terms", "term")
    table("groups", "id")
    table("conditions", "id")
    for index in range(len(before.get("merged", [])), len(after.get("merged", []))):
        changes.append(("add", f"experiments.merged[{index}]", None, after["merged"][index]))
    return changes


def run_write(root: Path, mutate: Callable[[Ctx], Any], *, dry_run: bool = False, reason: str = "요청에 따른 실험 정본 수정",
              now: Optional[float] = None, lock_timeout: Optional[float] = None) -> Dict[str, Any]:
    """The one write path.  `--dry-run` takes no lock and writes nothing, not even a directory."""
    root = Path(root).resolve()
    stamp = time.time() if now is None else now
    if dry_run:
        read = load(root)
        if read.status == "invalid":
            raise CatalogError("catalog-invalid", read.code or "")
        try:
            ident = lifecycle.read_root_identity(root)
        except lifecycle.LifecycleError:
            ident = None
        doc = read.doc if read.doc is not None else _empty_doc(ident.artifact_root_id if ident else "root_pending", stamp)
        return _apply(root, doc, mutate, stamp, reason, write=False)
    root.mkdir(parents=True, exist_ok=True)
    try:
        lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT if lock_timeout is None else lock_timeout)
    except (admission.AdmissionBusy, admission.dispatch_lock_order.LockOrderError) as exc:
        raise CatalogError("admission-busy") from exc
    try:
        ident = admission.ensure_root_identity(root)
        read = load(root)
        if read.status == "invalid":
            raise CatalogError("catalog-invalid", read.code or "")
        doc = read.doc if read.doc is not None else _empty_doc(ident.artifact_root_id, stamp)
        return _apply(root, doc, mutate, stamp, reason, write=True)
    finally:
        admission._release_lock(root, lock)


def _apply(root: Path, doc: Dict[str, Any], mutate: Callable[[Ctx], Any], stamp: float, reason: str,
           *, write: bool) -> Dict[str, Any]:
    before = copy.deepcopy(doc)
    ctx = Ctx(copy.deepcopy(doc))
    result = mutate(ctx)
    after = ctx.doc
    changes = _diff_events(before, after)
    out: Dict[str, Any] = {"path": CATALOG_REL, "result": result, "revision": before["revision"],
                           "changes": [row[1] for row in changes], "events": 0}
    if not changes:
        out["status"] = "dry-run" if not write else "no-change"
        out["warnings"] = lint(after)
        return out
    after["revision"] = before["revision"] + 1
    after["updated_at"] = _rfc3339(stamp)
    check_doc(after, strict=True)
    out["revision"] = after["revision"]
    out["warnings"] = lint(after)
    if not write:
        out["status"] = "dry-run"
        return out
    path = catalog_path(root)
    try:
        W._no_symlink(root, Path(CATALOG_REL).parent)
    except W.WorkflowGroupError as exc:
        raise CatalogError("path-unsafe") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _dumps(after)
    if len(raw) > SIZE_CAP:
        raise CatalogError("catalog-too-large")
    producer._write_atomic(path, raw, 0o644)
    out["status"] = "applied"
    txn = H.new_transaction_id()
    actor = H.actor_from_env("human")
    try:
        events = [H.make_event(
            kind="meta", target_type="project", target_id=after["artifact_root_id"], target_path=CATALOG_REL,
            operation=operation, field=field, before=H.value_ref(old), after=H.value_ref(new),
            reason=reason[:400] or "요청에 따른 실험 정본 수정", transaction_id=txn, actor=actor, now=stamp)
            for operation, field, old, new in changes]
        H.publish_events_locked(root, events)
        out["events"] = len(events)
    except (H.HistoryError, OSError):
        # The file is already replaced; nothing is rolled back and the caller is told.
        out["warnings"] = out["warnings"] + [_warn("history-publish-failed", CATALOG_REL)]
    return out


# -- operations ---------------------------------------------------------------


def _merge_fields(target: Dict[str, Any], spec: Mapping[str, Any], names: Sequence[str]) -> None:
    for name in names:
        if name in spec:
            target[name] = copy.deepcopy(spec[name])


def _upsert_group(ctx: Ctx, spec: Mapping[str, Any]) -> Dict[str, Any]:
    spec = _dict(spec, "group")
    if spec.get("id") is not None:
        group = _groups_by_id(ctx.doc).get(spec["id"])
        if group is None:
            raise CatalogError("group-unknown", str(spec["id"])[:40])
    else:
        group = {"id": _new_id("grp"), "display_name": "", "goal": "", "metric": None, "target": None,
                 "status": "unknown"}
        ctx.doc["groups"].append(group)
    _merge_fields(group, spec, ("display_name", "goal", "metric", "target", "status", "results"))
    return group


def _upsert_condition(ctx: Ctx, spec: Mapping[str, Any], pending: List[Tuple[Dict[str, Any], Any]]) -> Dict[str, Any]:
    spec = _dict(spec, "condition")
    if spec.get("id") is not None:
        cond = _conditions_by_id(ctx.doc).get(spec["id"])
        if cond is None:
            raise CatalogError("condition-unknown", str(spec["id"])[:40])
    else:
        cond = {"id": _new_id("cnd"), "group": None, "display_name": "", "aliases": [], "former_names": [],
                "changes": "", "compared_to": None, "problem": "", "refs": {"runs": [], "output_dirs": [], "configs": []}}
        ctx.doc["conditions"].append(cond)
    if "display_name" in spec:
        _text(spec["display_name"], NAME_MAX, "display_name")
        _set_name(cond, spec["display_name"])
    _merge_fields(cond, spec, ("aliases", "changes", "problem"))
    if "former_names" in spec:
        cond["former_names"] = copy.deepcopy(spec["former_names"])
    if "group" in spec:
        if spec["group"] is None:
            cond["group"] = None
        else:
            group = _resolve_group(ctx.doc, spec["group"])
            if group is None:
                raise CatalogError("group-unknown", str(spec["group"])[:40])
            cond["group"] = group["id"]
    if "refs" in spec:
        refs = _dict(spec["refs"], "refs")
        current = cond.setdefault("refs", {"runs": [], "output_dirs": [], "configs": []})
        for name in ("runs", "output_dirs", "configs"):
            if name in refs:
                current[name] = copy.deepcopy(refs[name])
        for ref in current.get("runs", []):
            _check_run_ref(ref, "refs.runs")
            other = ctx.linked_elsewhere(ref["run_id"], ref.get("row_id"), cond["id"])
            if other:
                raise CatalogError("run-linked-elsewhere", f"{ref['run_id']}→{other}")
    if "compared_to" in spec:
        pending.append((cond, spec["compared_to"]))
    return cond


def op_register(ctx: Ctx, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Groups, conditions, and terms in one call (the experiment-plan grid goes in as one spec)."""
    spec = _dict(spec, "spec")
    out: Dict[str, Any] = {"groups": [], "conditions": [], "terms": 0}
    for term in _list(spec.get("terms", []), MAX_TERMS, "terms"):
        op_add_term(ctx, term.get("term") if isinstance(term, dict) else None,
                    term.get("meaning", "") if isinstance(term, dict) else "",
                    term.get("note", "") if isinstance(term, dict) else "")
        out["terms"] += 1
    for group in _list(spec.get("groups", []), MAX_GROUPS, "groups"):
        row = _upsert_group(ctx, group)
        out["groups"].append({"id": row["id"], "display_name": row.get("display_name")})
    pending: List[Tuple[Dict[str, Any], Any]] = []
    for cond in _list(spec.get("conditions", []), MAX_CONDITIONS, "conditions"):
        row = _upsert_condition(ctx, cond, pending)
        out["conditions"].append({"id": row["id"], "display_name": row.get("display_name")})
    for cond, token in pending:  # a comparison may name a condition from the same spec
        if token is None:
            cond["compared_to"] = None
            continue
        cond["compared_to"] = ctx.condition(token, "compared-to-unknown")["id"]
    for row, spec_group in zip(out["groups"], spec.get("groups", [])):
        target = spec_group.get("target") if isinstance(spec_group, dict) else None
        if isinstance(target, dict) and target.get("kind") == "condition":
            other = ctx.condition(target.get("condition"), "compared-to-unknown")
            _groups_by_id(ctx.doc)[row["id"]]["target"]["condition"] = other["id"]
    return out


def op_add_term(ctx: Ctx, term: Any, meaning: Any, note: Any = "") -> Dict[str, Any]:
    _text(term, NAME_MAX, "term", allow_empty=False)
    _text(meaning, MEANING_MAX, "meaning")
    _text(note, MEANING_MAX, "note")
    for row in ctx.doc["terms"]:
        if row["term"] == term:
            row["meaning"], row["note"] = meaning, note
            return {"term": term}
    ctx.doc["terms"].append({"term": term, "meaning": meaning, "note": note})
    return {"term": term}


def op_rename(ctx: Ctx, token: str, new_name: str) -> Dict[str, Any]:
    cond = ctx.condition(token)
    _text(new_name, NAME_MAX, "display_name", allow_empty=False)
    if cond.get("display_name") != new_name:
        _set_name(cond, new_name)
    return {"id": cond["id"], "display_name": new_name}


def op_merge(ctx: Ctx, from_token: str, into_token: str, now: float) -> Dict[str, Any]:
    source, target = ctx.condition(from_token), ctx.condition(into_token)
    if source["id"] == target["id"]:
        raise CatalogError("merge-same-condition")
    if source.get("group") != target.get("group"):
        raise CatalogError("merge-group-mismatch")
    former = list(target.get("former_names", []))
    for name in [source.get("display_name"), *source.get("former_names", [])]:
        if name and name != target.get("display_name") and name not in former:
            former.append(name)
    target["former_names"] = former[-MAX_FORMER:]
    target["aliases"] = [*target.get("aliases", []),
                         *[a for a in source.get("aliases", []) if a not in target.get("aliases", [])]]
    tr, sr = target.setdefault("refs", {}), source.get("refs") or {}
    for name in ("runs", "output_dirs", "configs"):
        merged = list(tr.get(name, []))
        merged += [item for item in sr.get(name, []) if item not in merged]
        tr[name] = merged
    for cond in ctx.doc["conditions"]:
        if cond.get("compared_to") == source["id"]:
            cond["compared_to"] = target["id"]
    if target.get("compared_to") == target["id"]:
        target["compared_to"] = None
    for group in ctx.doc["groups"]:
        if isinstance(group.get("target"), dict) and group["target"].get("condition") == source["id"]:
            group["target"]["condition"] = target["id"]
    ctx.doc["conditions"] = [c for c in ctx.doc["conditions"] if c["id"] != source["id"]]
    ctx.doc["merged"].append({"from": source["id"], "into": target["id"], "at": _rfc3339(now)})
    return {"from": source["id"], "into": target["id"]}


def op_link(ctx: Ctx, token: str, run_id: str, row_id: Optional[str] = None) -> Dict[str, Any]:
    cond = ctx.condition(token)
    ref: Dict[str, Any] = {"run_id": run_id, "row_id": row_id}
    _check_run_ref(ref, "run")
    other = ctx.linked_elsewhere(run_id, row_id, cond["id"])
    if other:
        raise CatalogError("run-linked-elsewhere", f"{run_id}→{other}")
    runs = cond.setdefault("refs", {}).setdefault("runs", [])
    if ref not in runs:
        runs.append(ref)
    return {"id": cond["id"], "run_id": run_id, "row_id": row_id}


# ---------------------------------------------------------------------------
# status (read only)
# ---------------------------------------------------------------------------


def _public_point(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {"epoch": raw.get("epoch"), "value": _num(raw.get("value")), "nonfinite": bool(raw.get("nonfinite"))}


def _keyed_point(key: str, raw: Any) -> Optional[Dict[str, Any]]:
    point = _public_point(raw)
    return None if point is None else {"key": key, **point}


def _observed(summary: Mapping[str, Any], key: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(latest, best) for the declared metric; falls back to the run's own decision metric so a
    different metric shows up as a mismatch rather than as no value.  Both points carry the key
    they were measured in, so another metric's best never reads as the declared metric's value."""
    latest_metrics = summary.get("latest_metrics") if isinstance(summary.get("latest_metrics"), dict) else {}
    best_metrics = summary.get("best_metrics") if isinstance(summary.get("best_metrics"), dict) else {}
    if key and isinstance(latest_metrics.get(key), dict):
        return _keyed_point(key, latest_metrics[key]), _keyed_point(key, best_metrics.get(key))
    own = summary.get("metric") if isinstance(summary.get("metric"), str) else None
    if own and isinstance(summary.get("latest"), dict):
        return _keyed_point(own, summary.get("latest")), _keyed_point(own, summary.get("best"))
    return None, None


def _record_view(root: Path, ref: Mapping[str, Any], now: float, key: Optional[str]) -> Dict[str, Any]:
    run_id, row_id = ref["run_id"], ref.get("row_id")
    view: Dict[str, Any] = {"run_id": run_id, "row_id": row_id, "readable": False, "anomalies": [], "unknown": {}}
    path = rp.public_record_path(root, run_id)
    try:
        record = rp.read_public_run_record(root, run_id)
    except Exception:  # noqa: BLE001 -- one broken record never hides the others
        record = None
    if record is None:
        exists = path is not None and os.path.lexists(path)
        view["anomalies"].append("record-unreadable" if exists else "record-missing")
        return view
    view["readable"] = True
    status = record.get("status")
    view["status"] = status if isinstance(status, str) else None
    view["started_at"] = _ts(record.get("started_at"))
    source: Mapping[str, Any] = record
    if row_id is not None:
        rows = [r for r in record.get("rows") or [] if isinstance(r, dict) and r.get("row_id") == row_id]
        if not rows:
            view["readable"] = False  # a dangling row link has no values; it is reported, not used
            view["anomalies"].append("row-missing")
            return view
        source = rows[0]
        # A row is read on its own.  The run's start, progress, and ETA belong to the run as a
        # whole (often a later row), so none of them stands in for a value the row lacks.
        view["started_at"] = _ts(source.get("started_at"))
        ended = _ts(source.get("ended_at"))
        if ended is not None and (view["status"] in (None, "running") or _ts(record.get("ended_at")) != ended):
            # The row ended and the public row records no outcome: not running, and not a guess.
            # Only a row that ended together with a finished run carries that run's status.
            view["status"] = None
            view["unknown"]["run_status"] = "row-ended"
    summary = source.get("metric_summary") if isinstance(source.get("metric_summary"), dict) else {}
    checkpoint = source.get("last_checkpoint") if isinstance(source.get("last_checkpoint"), dict) else {}
    view["latest"], view["best"] = _observed(summary, key)
    view["nonfinite"] = bool(summary.get("nonfinite")) or bool(view["latest"] and view["latest"].get("nonfinite"))
    progress = source.get("progress") if isinstance(source.get("progress"), dict) else {}
    view["progress"] = ({"reached": progress.get("reached"), "total": progress.get("total"),
                         "unit": progress.get("unit")}
                        if any(progress.get(k) is not None for k in ("reached", "total")) else None)
    if view["progress"] is None:
        view["unknown"]["progress"] = "progress-unknown"
    running = view["status"] == "running"
    observed_at, eta = _ts(checkpoint.get("at")), _num(source.get("eta"))
    view["observed_at"] = _rfc3339(observed_at) if observed_at is not None else None
    age = None if observed_at is None else now - observed_at
    if age is not None and age < 0:
        age = None
        view["unknown"]["observed_age_seconds"] = "observed-in-future"
    view["observed_age_seconds"] = None if age is None else int(age)
    if observed_at is None:
        view["unknown"].setdefault("observed_age_seconds", "observation-unknown")
    if observed_at is not None and eta is not None and running:
        view["expected_at"] = _rfc3339(observed_at + eta)
    else:
        view["expected_at"] = None
        view["unknown"]["expected_at"] = "eta-unknown" if running else "not-running"
    stagnation = source.get("stagnation") if isinstance(source.get("stagnation"), dict) else None
    seconds = _num(stagnation.get("seconds_since_anchor")) if stagnation else None
    view["since_last_progress_seconds"] = None if seconds is None else int(seconds)
    if stagnation is None:
        view["unknown"]["since_last_progress_seconds"] = "stagnation-unknown"
        if running:
            view["anomalies"].append("stagnation-unknown")
    elif stagnation.get("state") == "stagnant":
        view["anomalies"].append("stagnant")
    if view["status"] in FAILED_STATUSES:
        view["anomalies"].append("status-failed")
    if view["nonfinite"]:
        view["anomalies"].append("nonfinite")
    if running and age is not None and age > STALE_OBSERVATION_SECONDS:
        view["anomalies"].append("observation-stale")
    return view


def _list_runs(root: Path) -> List[str]:
    """Public run records by recency; symlinks and odd names are not runs."""
    folder = Path(root) / "resource-runs"
    rows = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not entry.name.endswith(".json") or entry.is_symlink():
                    continue
                run_id = entry.name[:-5]
                if not rp._SAFE_RUN_ID.fullmatch(run_id):
                    continue
                try:
                    rows.append((entry.stat(follow_symlinks=False).st_mtime_ns, run_id))
                except OSError:
                    continue
    except OSError:
        return []
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [run_id for _mtime, run_id in rows]


def _condition_status(root: Path, group: Optional[Mapping[str, Any]], cond: Mapping[str, Any], doc: Mapping[str, Any],
                      by_id: Mapping[str, Mapping[str, Any]], now: float, views: Dict[str, Any]) -> Dict[str, Any]:
    metric = (group or {}).get("metric") if isinstance((group or {}).get("metric"), dict) else None
    key = (metric or {}).get("key")
    refs = _run_ref_keys(cond)
    views_for = [_record_view(root, {"run_id": r, "row_id": row}, now, key) for r, row in refs]
    readable = [v for v in views_for if v["readable"]]
    current = (max(readable, key=lambda v: (v.get("started_at") is not None, v.get("started_at") or 0.0))
               if readable else (views_for[-1] if views_for else None))
    others = [v["run_id"] for v in views_for if current is not None and v is not current]
    out: Dict[str, Any] = {
        "id": cond["id"], "display_name": cond.get("display_name") or None, "changes": cond.get("changes") or None,
        "problem": cond.get("problem") or None, "aliases": list(cond.get("aliases", [])),
        "compared_to": cond.get("compared_to"), "current_run": None, "other_runs": {"count": len(others), "run_ids": others},
        "progress": None, "latest": None, "best": None, "comparison": None, "expected_at": None,
        "observed_at": None, "observed_age_seconds": None, "since_last_progress_seconds": None,
        "run_status": None, "anomalies": [], "unknown": {}}
    views[cond["id"]] = current
    if current is None:
        out["unknown"]["run"] = "no-run-linked"
        return out
    out["current_run"] = {"run_id": current["run_id"], "row_id": current["row_id"],
                          "status": current.get("status"), "started_at": current.get("started_at")}
    out["anomalies"] = list(current["anomalies"])
    for other in views_for:  # a broken link on another attempt is not hidden by a healthy one
        for anomaly in other["anomalies"]:
            if other is not current and anomaly in ("record-missing", "record-unreadable", "row-missing") \
                    and anomaly not in out["anomalies"]:
                out["anomalies"].append(anomaly)
    out["unknown"] = dict(current["unknown"])
    for name in ("progress", "latest", "best", "expected_at", "observed_at", "observed_age_seconds",
                 "since_last_progress_seconds"):
        if name in current:
            out[name] = current[name]
    out["run_status"] = current.get("status")
    return out


def status(root: Path, now: Optional[float] = None, expect: Sequence[str] = ()) -> Dict[str, Any]:
    """`experiment-status/v1`: the catalog and the public run records, read together, read only."""
    root = Path(root)
    stamp = time.time() if now is None else now
    read = load(root)
    doc = read.doc if read.status == "ok" else None
    out: Dict[str, Any] = {
        "schema": STATUS_CONTRACT, "generated_at": _rfc3339(stamp), "artifact_root": str(root),
        "catalog": {"status": read.status, "path": CATALOG_REL, "revision": read.revision, "code": read.code,
                    "updated_at": (doc or {}).get("updated_at")},
        "terms": list((doc or {}).get("terms", [])), "groups": [], "waiting": [],
        "unlinked_runs": {"run_ids": [], "truncated": 0}, "warnings": []}
    linked = set()
    views: Dict[str, Any] = {}
    if doc is not None:
        by_id = _conditions_by_id(doc)
        groups_by_id = _groups_by_id(doc)
        group_rows: List[Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]] = [
            (g, []) for g in doc.get("groups", [])]
        loose: List[Dict[str, Any]] = []
        for cond in doc.get("conditions", []):
            linked.update(r for r, _row in _run_ref_keys(cond))
            owner = next((rows for g, rows in group_rows if g["id"] == cond.get("group")), loose)
            owner.append(cond)
        if loose:
            group_rows.append((None, loose))
        # Every condition is read once; a control's point is looked up afterward.
        statuses: Dict[str, Dict[str, Any]] = {}
        for group, conds in group_rows:
            for cond in conds:
                statuses[cond["id"]] = _condition_status(root, group, cond, doc, by_id, stamp, views)
        for group, conds in group_rows:
            rows = []
            target = (group or {}).get("target") if isinstance((group or {}).get("target"), dict) else None
            control_id = target.get("condition") if target and target.get("kind") == "condition" else None
            control = statuses.get(control_id) if control_id else None
            # The control's value was measured under its own group's declaration, not this one's.
            control_group = groups_by_id.get((by_id.get(control_id) or {}).get("group")) if control_id else None
            control_metric = (control_group or {}).get("metric")
            for cond in conds:
                row = statuses[cond["id"]]
                if control_id == cond["id"]:
                    row["comparison"] = {"state": "no-target", "reason": "is-control", "value": None, "baseline": None,
                                         "delta": None, "delta_unit": None, "relative_percent": None, "better": None}
                else:
                    control_point = control.get("latest") if control else None
                    comparison = compare(group, row.get("latest"), control_point,
                                         control_metric if isinstance(control_metric, dict) else None)
                    if comparison["baseline"] and control_id and by_id.get(control_id):
                        comparison["baseline"]["label"] = by_id[control_id].get("display_name") or None
                    row["comparison"] = comparison
                if row["run_status"] == "running" and row["latest"] is None:
                    out["waiting"].append({"condition_id": cond["id"], "display_name": cond.get("display_name") or None,
                                           "expected_at": row["expected_at"]})
                rows.append(row)
            out["groups"].append({
                "id": group["id"] if group else None,
                "display_name": (group or {}).get("display_name") or None, "goal": (group or {}).get("goal") or None,
                "metric": (group or {}).get("metric"), "target": target,
                "status": (group or {}).get("status"), "results": (group or {}).get("results"),
                "conditions": rows})
    runs = _list_runs(root)
    unlinked = [run_id for run_id in runs if run_id not in linked]
    out["unlinked_runs"] = {"run_ids": unlinked[:UNLINKED_MAX], "truncated": max(0, len(unlinked) - UNLINKED_MAX)}
    out["warnings"] = lint_read(read, expect=expect, unlinked=unlinked[:UNLINKED_MAX])
    return out


def lint_read(read: CatalogRead, expect: Sequence[str] = (), unlinked: Sequence[str] = ()) -> List[Dict[str, str]]:
    head: List[Dict[str, str]] = []
    if read.status == "missing":
        head.append(_warn("catalog-missing", CATALOG_REL))
    elif read.status == "invalid":
        head.append(_warn("catalog-invalid", CATALOG_REL, read.code or ""))
    return head + lint(read.doc if read.status == "ok" else None, expect, unlinked)


# ---------------------------------------------------------------------------
# text output (shared catalog `experiment.*`)
# ---------------------------------------------------------------------------


def _plain(message: str, /, **fields: Any) -> str:
    return "; ".join(f"{k}={v}" for k, v in fields.items()) if fields else message.rsplit(".", 1)[-1].replace("_", " ")


def _translator(lang: Optional[str] = None) -> Callable[..., str]:
    try:
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "i18n" / "ui_text.py"
        spec = importlib.util.spec_from_file_location("ui_text", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.translator(lang)
    except Exception:  # noqa: BLE001 -- a missing catalog never blocks a status
        return _plain


def _fmt(value: float) -> str:
    return f"{value:.4g}" if abs(value) < 1 else f"{value:.2f}"


def _signed(value: float) -> str:
    return ("+" if value > 0 else "") + _fmt(value)


def _unit_text(value: float, unit: Optional[str]) -> str:
    return _fmt(value) + (unit if unit == "%" else f" {unit}" if unit else "")


def _clock(iso: Optional[str]) -> str:
    epoch = _ts(iso)
    return "" if epoch is None else time.strftime("%m-%d %H:%M", time.localtime(epoch))


def _age_text(seconds: int, t: Callable[..., str]) -> str:
    if seconds < 90:
        return t("experiment.age_seconds", n=seconds)
    if seconds < 5400:
        return t("experiment.age_minutes", n=round(seconds / 60))
    return t("experiment.age_hours", n=round(seconds / 3600, 1))


def _condition_line(row: Mapping[str, Any], group: Mapping[str, Any], t: Callable[..., str]) -> str:
    name = row.get("display_name") or t("experiment.unnamed")
    metric = group.get("metric") or {}
    unit = metric.get("unit") or None
    parts: List[str] = []
    # The row's own run state in words (RV-PR1596-R2): running, or ended without a recorded outcome.
    if row.get("run_status") == "running":
        parts.append(t("experiment.run_state.running"))
    elif (row.get("unknown") or {}).get("run_status") == "row-ended":
        parts.append(t("experiment.run_state.row-ended"))
    progress = row.get("progress")
    if progress and progress.get("reached") is not None:
        parts.append(t("experiment.progress", reached=progress["reached"],
                       total=progress.get("total") if progress.get("total") is not None else "?",
                       unit=progress.get("unit") or ""))
    latest = row.get("latest")
    if latest and latest.get("value") is not None:
        parts.append(t("experiment.latest", value=_unit_text(latest["value"], unit), epoch=latest.get("epoch")))
    elif latest and latest.get("nonfinite"):
        parts.append(t("experiment.latest_nonfinite", epoch=latest.get("epoch")))
    else:
        parts.append(t("experiment.latest_none"))
    comparison = row.get("comparison") or {}
    state, reason = comparison.get("state"), comparison.get("reason")
    if state == "ok":
        baseline = comparison["baseline"]
        relative = comparison.get("relative_percent")
        parts.append(t("experiment.vs_ok", label=baseline.get("label") or t("experiment.baseline"),
                       base=_unit_text(baseline["value"], unit),
                       delta=_signed(comparison["delta"]) + ("%p" if comparison.get("delta_unit") == "pp"
                                                             else f" {comparison['delta_unit']}" if comparison.get("delta_unit") else ""),
                       relative=(t("experiment.relative", value=_signed(round(relative, 1))) if relative is not None else "")))
    elif state == "not-comparable":
        parts.append(t("experiment.not_comparable", reason=t(f"experiment.reason.{reason}")))
    elif state == "no-target":
        parts.append(t(f"experiment.reason.{reason}"))
    elif state == "no-data" and reason == "control-no-observation":
        parts.append(t("experiment.reason.control-no-observation"))
    if row.get("run_status") == "running":
        if row.get("expected_at"):
            parts.append(t("experiment.expected", time=_clock(row["expected_at"])))
        else:
            parts.append(t("experiment.expected_unknown"))
    if row.get("observed_age_seconds") is not None:
        parts.append(t("experiment.observed", age=_age_text(row["observed_age_seconds"], t)))
    elif row.get("current_run"):
        parts.append(t("experiment.observed_unknown"))
    if row.get("anomalies"):
        parts.append(t("experiment.anomalies", items=", ".join(t(f"experiment.anomaly.{a}") for a in row["anomalies"])))
    return f"- {name} — " + " · ".join(parts)


def render_status(doc: Mapping[str, Any], lang: Optional[str] = None) -> str:
    t = _translator(lang)
    catalog = doc["catalog"]
    lines: List[str] = []
    if catalog["status"] == "ok":
        lines.append(t("experiment.header", path=catalog["path"], revision=catalog["revision"],
                       updated=catalog.get("updated_at") or "?"))
    elif catalog["status"] == "missing":
        lines.append(t("experiment.header_missing", path=catalog["path"]))
    else:
        lines.append(t("experiment.header_invalid", path=catalog["path"], code=catalog.get("code") or "?"))
    for group in doc["groups"]:
        metric = group.get("metric") or {}
        if group.get("display_name") or group.get("goal"):
            head = f"[{group.get('display_name') or t('experiment.unnamed')}]"
        else:
            head = f"[{t('experiment.no_group')}]"
        if group.get("goal"):
            tail = t("experiment.goal", goal=group["goal"])
            if metric.get("key"):
                tail += " " + t("experiment.metric", key=metric["key"], unit=metric.get("unit") or "?",
                                direction=t(f"experiment.direction.{metric.get('direction') or 'min'}"))
            head += " " + tail
        lines.append(head)
        lines.extend(_condition_line(row, group, t) for row in group["conditions"])
    unlinked = doc["unlinked_runs"]
    if unlinked["run_ids"]:
        shown = unlinked["run_ids"][:3]
        extra = len(unlinked["run_ids"]) + unlinked["truncated"] - len(shown)
        lines.append(t("experiment.unlinked", count=len(unlinked["run_ids"]) + unlinked["truncated"],
                       ids=", ".join(shown) + (t("experiment.and_more", n=extra) if extra > 0 else "")))
    for warning in doc["warnings"]:
        if warning["where"].startswith("resource-runs/"):
            continue  # summarized by the unnamed-runs line above
        lines.append(t("experiment.warning", code=t(f"experiment.warning.{warning['code']}"),
                       where=warning["where"], detail=warning.get("detail") or "-"))
    return "\n".join(lines)


def render_show(read: CatalogRead, lang: Optional[str] = None) -> str:
    t = _translator(lang)
    if read.status != "ok":
        return t("experiment.header_missing" if read.status == "missing" else "experiment.header_invalid",
                 **({"path": CATALOG_REL} if read.status == "missing" else {"path": CATALOG_REL, "code": read.code or "?"}))
    doc = read.doc
    lines = [t("experiment.header", path=CATALOG_REL, revision=doc["revision"], updated=doc["updated_at"])]
    names = {c["id"]: c.get("display_name") or c["id"] for c in doc["conditions"]}
    for row in doc["terms"]:
        lines.append(t("experiment.show_term", term=row["term"], meaning=row.get("meaning") or "-"))
    for group in [*doc["groups"], None]:
        conds = [c for c in doc["conditions"] if c.get("group") == (group["id"] if group else None)]
        if group is None and not conds:
            continue
        lines.append(f"[{(group or {}).get('display_name') or t('experiment.no_group')}] "
                     + t("experiment.goal", goal=(group or {}).get("goal") or "-"))
        for cond in conds:
            extra = []
            if cond.get("aliases"):
                extra.append(t("experiment.show_aliases", items=", ".join(cond["aliases"])))
            if cond.get("compared_to"):
                extra.append(t("experiment.show_compared", name=names.get(cond["compared_to"], cond["compared_to"])))
            if cond.get("changes"):
                extra.append(cond["changes"])
            runs = [ref["run_id"] + (f":{ref['row_id']}" if ref.get("row_id") else "")
                    for ref in (cond.get("refs") or {}).get("runs", [])]
            if runs:
                extra.append(t("experiment.show_runs", items=", ".join(runs)))
            lines.append(f"- {cond.get('display_name') or t('experiment.unnamed')} ({cond['id']})"
                         + (" — " + " · ".join(extra) if extra else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="experiment_catalog.py", description=__doc__.split("\n")[0])
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--artifact-root", help="the artifact root itself")
    where.add_argument("--path", help="a project path; the root is resolved from it")
    where.add_argument("--json", action="store_true", help="machine-readable output")
    where.add_argument("--lang", choices=("en", "ko"), help=argparse.SUPPRESS)
    write = argparse.ArgumentParser(add_help=False)
    write.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    write.add_argument("--reason", default="요청에 따른 실험 정본 수정")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", parents=[where], help="the catalog as written")
    st = sub.add_parser("status", parents=[where], help="catalog + public run records, read together")
    st.add_argument("--expect", action="append", default=[], metavar="NAME")
    li = sub.add_parser("lint", parents=[where], help="warnings with their place; never blocks")
    li.add_argument("--expect", action="append", default=[], metavar="NAME")
    reg = sub.add_parser("register", parents=[where, write], help="add or update groups, conditions, and terms")
    reg.add_argument("--file", help="a JSON spec: {terms, groups, conditions}")
    reg.add_argument("--group-name")
    reg.add_argument("--goal")
    reg.add_argument("--condition-name")
    reg.add_argument("--group", help="group id or name for --condition-name")
    reg.add_argument("--changes")
    reg.add_argument("--alias", action="append", default=[])
    reg.add_argument("--compared-to")
    reg.add_argument("--run", action="append", default=[], metavar="RUN[:ROW]")
    ren = sub.add_parser("rename", parents=[where, write], help="rename one condition; the old name stays resolvable")
    ren.add_argument("token")
    ren.add_argument("--name", required=True)
    mer = sub.add_parser("merge", parents=[where, write], help="fold one condition into another")
    mer.add_argument("token")
    mer.add_argument("--into", required=True)
    lnk = sub.add_parser("link", parents=[where, write], help="attach a run (and row) to one condition")
    lnk.add_argument("token")
    lnk.add_argument("--run", required=True, metavar="RUN[:ROW]")
    term = sub.add_parser("add-term", parents=[where, write], help="add or update a vocabulary term")
    term.add_argument("--term", required=True)
    term.add_argument("--meaning", required=True)
    term.add_argument("--note", default="")
    return parser


def _root_from(args: argparse.Namespace) -> Path:
    if args.artifact_root:
        return Path(args.artifact_root).resolve()
    return resolve_root(args.path).resolve()


def _split_run(value: str) -> Tuple[str, Optional[str]]:
    run_id, _, row = value.partition(":")
    return run_id, row or None


def _spec_from(args: argparse.Namespace) -> Dict[str, Any]:
    if args.file:
        try:
            return json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise CatalogError("spec-unreadable", type(exc).__name__) from exc
    spec: Dict[str, Any] = {}
    if args.group_name:
        group = {"display_name": args.group_name}
        if args.goal is not None:
            group["goal"] = args.goal
        spec["groups"] = [group]
    if args.condition_name:
        cond: Dict[str, Any] = {"display_name": args.condition_name}
        if args.group:
            cond["group"] = args.group
        elif args.group_name:
            cond["group"] = args.group_name
        if args.changes is not None:
            cond["changes"] = args.changes
        if args.alias:
            cond["aliases"] = args.alias
        if args.compared_to:
            cond["compared_to"] = args.compared_to
        if args.run:
            cond["refs"] = {"runs": [{"run_id": r, "row_id": row} for r, row in map(_split_run, args.run)]}
        spec["conditions"] = [cond]
    if not spec:
        raise CatalogError("nothing-to-register")
    return spec


def _read_command(args: argparse.Namespace) -> int:
    try:
        root = _root_from(args)
        if args.command == "show":
            read = load(root)
            if args.json:
                _print({"catalog": {"status": read.status, "path": CATALOG_REL, "revision": read.revision,
                                    "code": read.code}, "doc": read.doc, "warnings": lint_read(read)})
            else:
                print(render_show(read, args.lang))
            return 0
        report = status(root, expect=getattr(args, "expect", []))
    except Exception as exc:  # noqa: BLE001 -- a read never fails the caller
        code = exc.code if isinstance(exc, CatalogError) else "read-failed"
        report = {"schema": STATUS_CONTRACT, "generated_at": _rfc3339(), "artifact_root": None,
                  "catalog": {"status": "invalid", "path": CATALOG_REL, "revision": None, "code": code,
                              "updated_at": None},
                  "terms": [], "groups": [], "waiting": [], "unlinked_runs": {"run_ids": [], "truncated": 0},
                  "warnings": [_warn("catalog-invalid", CATALOG_REL, code)]}
    if args.command == "lint":
        _print({"catalog": report["catalog"], "warnings": report["warnings"]}) if args.json else \
            print(render_status({**report, "groups": [], "unlinked_runs": {"run_ids": [], "truncated": 0}}, args.lang))
    elif args.json:
        _print(report)
    else:
        print(render_status(report, args.lang))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command in ("show", "status", "lint"):
        return _read_command(args)
    try:
        root = _root_from(args)
        now = time.time()
        if args.command == "register":
            spec = _spec_from(args)
            mutate: Callable[[Ctx], Any] = lambda ctx: op_register(ctx, spec)  # noqa: E731
        elif args.command == "rename":
            mutate = lambda ctx: op_rename(ctx, args.token, args.name)  # noqa: E731
        elif args.command == "merge":
            mutate = lambda ctx: op_merge(ctx, args.token, args.into, now)  # noqa: E731
        elif args.command == "link":
            run_id, row_id = _split_run(args.run)
            mutate = lambda ctx: op_link(ctx, args.token, run_id, row_id)  # noqa: E731
        else:
            mutate = lambda ctx: op_add_term(ctx, args.term, args.meaning, args.note)  # noqa: E731
        _print(run_write(root, mutate, dry_run=args.dry_run, reason=args.reason, now=now))
        return 0
    except CatalogError as exc:
        _print({"error": exc.code, "detail": exc.detail})
        return 2


if __name__ == "__main__":
    sys.exit(main())
