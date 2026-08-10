"""Route record consumption (F-28a, prd.md:302) — read-only, tolerant.

`route.py` is the sibling of `model.py`: `model` owns the cross-harness Session/DispatchJob
schema + F-25 classifier, `route` owns immutable stage-dispatch route-record parsing, its DAG,
and per-route view assembly. Both `render.py` (F-28b breadcrumb, F-30 process view) and
`fleet.py` (`--json route`) need this — it is not collector-only — so it lives at the package
root next to `model.py`, not under `collectors/`.

Zero-dep stdlib only. **NEVER writes** — this module contains no file-write call of any kind
(no write-mode file open, no path-replace, no stream-write method call). The read-only invariant
(prd.md:287) is enforced by a static grep gate (plan §7 G3), not just by convention — see
`_internal/dev_reviews/` for the exact pattern the gate checks.

Contract:
  load(path, expect_hash=None, expect_id=None)  -> record dict | None, never raises.
  gate_mark(record, node_id)                     -> True | None — completion-marker verdict
                                                     (prd.md:308). NEVER False: marker absence
                                                     is "no claim", not "not passed".
  resolve_gate_marks(records)                    -> {route_id: {node_id: True}} — the second
                                                     impure entry point (sibling of
                                                     resolve_records); build_views stays PURE.
  route_hash(record)                             -> "sha256:..." (utilities/capability-route.py
                                                     :21-26 reproduced verbatim, P1).
  node_order(record)                             -> [[node_id, ...], ...] Kahn levels.
  resolve_records(jobs, node_evidence=None)      -> {route_id: record} — the ONE impure entry
                                                     point (calls load() per distinct route_file
                                                     seen on a live job OR in terminal-row
                                                     evidence — a route with no live job left
                                                     still resolves via node_evidence).
  build_views(jobs, node_evidence, records, now, gate_marks=None)
                                                 -> [view, ...] — PURE (no I/O, no clock read);
                                                     `now` is always an argument, never sampled
                                                     internally, so it is hermetically testable.
                                                     `gate_marks` is optional so every existing
                                                     4-positional caller is unaffected.
  collect_views(jobs, node_evidence, now=None)   -> [view, ...] — convenience wrapper:
                                                     resolve_records() + build_views().
  summary(views)                                 -> --json-shaped list (drops internal refs).
  clear_cache()                                  -> test hermeticity (model.reset_state_tracker
                                                     precedent).
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass

_CACHE = {}          # {abspath: (mtime, size, record|None)}
_MARKER_CACHE = {}   # {abspath: (mtime, size, marker|None)} — same shape, separate namespace


@dataclass(frozen=True)
class RouteDiagnostic:
    """Stable result for explicit route evidence; parser details stay private."""
    record: object = None
    valid: bool = False
    ambiguity: str = "route-record-mismatch"


def load_diagnostic(path, expect_hash=None, expect_id=None):
    """Load a route while distinguishing absence from explicit invalid evidence."""
    record = load(path, expect_hash=expect_hash, expect_id=expect_id)
    if record is not None:
        return RouteDiagnostic(record=record, valid=True, ambiguity=None)
    if not path:
        return RouteDiagnostic(ambiguity="route-record-mismatch")
    try:
        if not os.path.exists(os.path.abspath(path)):
            return RouteDiagnostic(ambiguity="route-record-missing")
    except (OSError, TypeError, ValueError):
        pass
    return RouteDiagnostic(ambiguity="route-record-mismatch")


# --- hashing (P1 — utilities/capability-route.py:21-26, reproduced exactly) ---
def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def route_hash(record):
    bare = {
        k: v for k, v in record.items()
        if k not in ("route_hash", "route_id") and not k.startswith("_fleet_")
    }
    return "sha256:" + hashlib.sha256(_canonical(bare)).hexdigest()


def clear_cache():
    """Test hermeticity: drop the mtime+size caches (model.reset_state_tracker() precedent)."""
    _CACHE.clear()
    _MARKER_CACHE.clear()


def _valid_attempt_axes(marker, node):
    """Validate the schema-2 attempt tuple before it can publish a gate."""
    depth = marker.get("dispatch_depth")
    transport = marker.get("transport")
    surface = marker.get("execution_surface")
    registered = marker.get("registered_worker")
    fallback = marker.get("fallback_hop")
    if depth != node.get("dispatch_depth") or depth not in {0, 1, 2}:
        return False
    if transport not in {"headless", "interactive"}:
        return False
    if surface not in {
        "registered-headless", "codex-native-subagent",
        "claude-subagent", "inline",
    }:
        return False
    if not isinstance(registered, bool):
        return False
    if registered != (surface == "registered-headless"):
        return False
    if depth == 0:
        return (
            transport == "interactive"
            and surface == "inline"
            and registered is False
            and fallback is None
        )
    if surface == "registered-headless":
        return (
            transport == "headless"
            and fallback in {
                "same-harness-headless", "cross-harness-headless",
            }
        )
    if surface in {"codex-native-subagent", "claude-subagent"}:
        return transport == "headless" and fallback == "native-subagent"
    return surface == "inline" and fallback == "inline"


def _load_uncached(abspath):
    try:
        with open(abspath, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    schema_version = record.get("schema_version", 1)
    if schema_version not in {1, 2}:
        return None
    digest = record.get("route_hash")
    if not isinstance(digest, str):
        return None
    try:
        if digest != route_hash(record):
            return None
    except Exception:
        return None
    expected_id = ("rt-" + digest.split(":", 1)[1][:16]) if digest.startswith("sha256:") else None
    if record.get("route_id") != expected_id:
        return None
    nodes = record.get("nodes")
    if not isinstance(nodes, list):
        return None
    for n in nodes:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str):
            return None
    if schema_version == 2:
        if record.get("dispatch_contract_version") != 3:
            return None
        if record.get("owner_dispatch_depth") not in {0, 1}:
            return None
        if record.get("max_dispatch_depth") not in {0, 1, 2}:
            return None
        if any(key in record for key in ("depth", "owner_depth", "max_depth")):
            return None
        observed_dispatch_depths = [record["owner_dispatch_depth"]]
        for node in nodes:
            if node.get("kind") == "resource-runner":
                if any(
                    key in node
                    for key in (
                        "depth", "owner_depth", "max_depth", "dispatch_depth",
                        "transport", "fallback_hops",
                    )
                ):
                    return None
                if node.get("resource_transport") != "detached-process":
                    return None
                continue
            if (
                any(key in node for key in ("depth", "owner_depth", "max_depth"))
                or node.get("dispatch_depth") not in {0, 1, 2}
            ):
                return None
            observed_dispatch_depths.append(node["dispatch_depth"])
            if "dispatch_fallback" in node:
                return None
            for hop in node.get("fallback_hops", []):
                if not isinstance(hop, dict) or hop.get("fallback_hop") not in {
                    "same-harness-headless", "cross-harness-headless",
                    "native-subagent", "inline",
                }:
                    return None
        if record["max_dispatch_depth"] != max(observed_dispatch_depths):
            return None
    result = dict(record)
    result["_fleet_schema_status"] = (
        "current" if schema_version == 2 else "legacy-read-only"
    )
    return result


def load(path, expect_hash=None, expect_id=None):
    """record dict | None. Never raises — any failure (missing file, bad json, hash mismatch,
    future schema, malformed nodes[], pipe/record mismatch) is a quiet None → caller falls back
    to the existing pipe heuristic (F-28 v8 contract, unchanged)."""
    if not path or not isinstance(path, (str, bytes, os.PathLike)):
        return None   # I2: a non-path-like value (int/dict/list/...) is a caller bug, not a
                       # crash — a pipe field is always text, so this can only happen via a
                       # malformed caller, and "never raises" wins over surfacing that loudly.
    try:
        abspath = os.path.abspath(path)
        st = os.stat(abspath)
        key = (st.st_mtime, st.st_size)
    except (OSError, TypeError, ValueError):
        return None
    cached = _CACHE.get(abspath)
    if cached is not None and (cached[0], cached[1]) == key:
        record = cached[2]
    else:
        record = _load_uncached(abspath)
        _CACHE[abspath] = (key[0], key[1], record)
    if record is None:
        return None
    if expect_hash is not None and record.get("route_hash") != expect_hash:
        return None
    if expect_id is not None and record.get("route_id") != expect_id:
        return None
    return record


# --- completion gate markers (prd.md:308, v10 minor #2 — read-only) ---
def _completion_home():
    """Agent home holding `.dispatch/completion/`. Reproduces `collectors/dispatch._registry_home`
    (AGENT_HOME → CLAUDE_HOME → $HOME/hearting → legacy $HOME/agent_setting → ~/.claude) rather than importing
    it: route.py has no collectors dependency today (it is a peer of model.py, imported BY
    dispatch.py's consumers), and one four-line resolver is cheaper than inverting that edge.
    Kept in sync with `utilities/capability-route.py:230 completion_dir()`, the writer."""
    h = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
    if h:
        return h
    for cand in (os.path.expanduser("~/hearting"), os.path.expanduser("~/agent_setting")):
        if os.path.isdir(cand):
            return cand
    return os.path.expanduser("~/.claude")


def _load_marker_uncached(abspath):
    try:
        with open(abspath, encoding="utf-8") as f:
            marker = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(marker, dict):
        return None
    if (
        marker.get("schema_version") != 2
        or not isinstance(marker.get("route_id"), str)
        or not isinstance(marker.get("route_hash"), str)
        or not isinstance(marker.get("sequence"), int)
        or marker.get("sequence") < 1
    ):
        return None
    return marker


def _load_marker(abspath):
    """marker dict | None. Never raises — mtime+size cached exactly like load()'s record cache."""
    try:
        st = os.stat(abspath)
        key = (st.st_mtime, st.st_size)
    except (OSError, TypeError, ValueError):
        return None
    cached = _MARKER_CACHE.get(abspath)
    if cached is not None and (cached[0], cached[1]) == key:
        return cached[2]
    marker = _load_marker_uncached(abspath)
    _MARKER_CACHE[abspath] = (key[0], key[1], marker)
    return marker


def _latest_marker_path(directory, node_id):
    """The one authoritative marker file for a node, or None.

    `capability-route.py:241 write_completion_marker` writes an append-only history
    (`<node_id>.<seq>.json`) and then atomically replaces the canonical `<node_id>.json` with
    the newest of them — so canonical, when present, IS the latest and no glob is needed
    (prd.md:308 "이력 파일 중 최신만 authoritative"). The history scan is the fallback for the
    torn window between those two writes (history landed, canonical replace not yet done),
    where the highest sequence is the latest."""
    canonical = os.path.join(directory, node_id + ".json")
    if os.path.isfile(canonical):
        return canonical
    best = None
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    prefix = node_id + "."
    for name in entries:
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        middle = name[len(prefix):-len(".json")]
        if not middle.isdigit():
            continue
        seq = int(middle)
        if best is None or seq > best[0]:
            best = (seq, os.path.join(directory, name))
    return best[1] if best else None


def gate_mark(record, node_id, home=None):
    """`True` if this node's completion gate is PROVEN passed, else `None` — never `False`.

    prd.md:308: "marker 존재 + record의 route_id/route_hash 일치 = 통과 / marker 부재 =
    무주장(실패·미통과로 표시 금지)". Every other failure mode (route_id mismatch, route_hash
    mismatch, garbage json, unreadable dir) collapses to that same no-claim `None` — a marker
    we cannot tie to THIS record is not counter-evidence, it is silence."""
    if not isinstance(record, dict) or not isinstance(node_id, str) or not node_id:
        return None
    route_id = record.get("route_id")
    route_hash_val = record.get("route_hash")
    if not isinstance(route_id, str) or not isinstance(route_hash_val, str):
        return None
    if os.sep in node_id or (os.altsep and os.altsep in node_id):
        return None   # a node id is an identifier, never a path — refuse to traverse
    if record.get("schema_version") != 2 or record.get("dispatch_contract_version") != 3:
        return None
    node = next(
        (candidate for candidate in record.get("nodes", []) if candidate.get("id") == node_id),
        None,
    )
    if node is None:
        return None
    directory = os.path.join(home or _completion_home(), ".dispatch", "completion", route_id)
    path = os.path.join(directory, node_id + ".json")
    marker = _load_marker(path)
    if marker is None:
        return None
    if (
        marker.get("route_id") != route_id
        or marker.get("route_hash") != route_hash_val
        or marker.get("registry_digest") != record.get("registry_digest")
        or marker.get("node_id") != node_id
        or marker.get("completion_gate") != node.get("completion_gate")
    ):
        return None
    history_path = os.path.join(directory, "%s.%d.json" % (node_id, marker["sequence"]))
    history = _load_marker(history_path)
    if history != marker:
        return None
    evidence = marker.get("evidence")
    if not isinstance(evidence, dict) or not os.path.isabs(str(evidence.get("path", ""))):
        return None
    try:
        with open(evidence["path"], "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != evidence.get("sha256"):
                return None
    except OSError:
        return None
    if node.get("kind") == "resource-runner":
        if (
            marker.get("attempt_id") is not None
            or marker.get("dispatch_depth") is not None
            or marker.get("transport") is not None
            or marker.get("execution_surface") is not None
            or marker.get("registered_worker") is not False
            or marker.get("fallback_hop") is not None
        ):
            return None
        return True
    attempt_id = marker.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    if marker.get("dispatch_depth") != node.get("dispatch_depth"):
        return None
    if not _valid_attempt_axes(marker, node):
        return None
    safe_attempt = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in attempt_id
    )
    link_path = os.path.join(directory, "%s.%s.attempt.json" % (node_id, safe_attempt))
    try:
        with open(link_path, encoding="utf-8") as handle:
            link = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    expected_link = {
        "schema_version": 2,
        "route_id": route_id,
        "node_id": node_id,
        "attempt_id": attempt_id,
        "dispatch_depth": marker.get("dispatch_depth"),
        "transport": marker.get("transport"),
        "execution_surface": marker.get("execution_surface"),
        "registered_worker": marker.get("registered_worker"),
        "fallback_hop": marker.get("fallback_hop"),
        "evidence_sha256": evidence.get("sha256"),
        "completion_marker": path,
        "completion_marker_history": history_path,
    }
    if any(link.get(key) != value for key, value in expected_link.items()):
        return None
    return True


def resolve_gate_marks(records, home=None):
    """{route_id: {node_id: True}} — the second impure entry point (sibling of resolve_records).
    Only PASSED nodes appear; a missing key is the no-claim default, so callers never have to
    distinguish "absent" from "False" (there is no False)."""
    marks = {}
    for rid, record in (records or {}).items():
        per_node = {}
        for n in (record.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            nid = n.get("id")
            if isinstance(nid, str) and gate_mark(record, nid, home=home):
                per_node[nid] = True
        if per_node:
            marks[rid] = per_node
    return marks


# --- DAG (Step 2/3 shared source) ---
def node_order(record):
    """[[node_id, ...], ...] — Kahn levels. Same-level order = record's `nodes[]` original
    order (the compiler's order, not sorted — deterministic across ticks). A cycle or an
    unresolvable `depends_on` reference never raises: the unresolved remainder is placed as one
    final level, in original order (tolerant — a slightly-off render beats a crash, plan §3.1)."""
    nodes = (record or {}).get("nodes") or []
    ids = [n.get("id") for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)]
    id_set = set(ids)
    deps = {}
    for n in nodes:
        nid = n.get("id")
        if nid in id_set:
            deps[nid] = [x for x in (n.get("depends_on") or []) if isinstance(x, str)]
    remaining = list(ids)
    placed = set()
    levels = []
    while remaining:
        ready = [nid for nid in remaining
                 if all((d in placed) or (d not in id_set) for d in deps.get(nid, []))]
        if not ready:
            levels.append(remaining)   # cycle/unresolved remainder — dump as-is, tolerant
            break
        levels.append(ready)
        placed.update(ready)
        remaining = [nid for nid in remaining if nid not in placed]
    return levels


# --- node state (§3.3 single source — Step 2 breadcrumb AND Step 3 card both call this) ---
def _is_reconciling_attempt(job):
    """True only for the exact F-41 completion-marker reconciliation shape.

    The dispatch classifier deliberately projects a shared-observer
    ``reconcile-needed`` verdict to job liveness ``stale`` so the open registry row remains
    visible until its owner publishes the exact completion marker. Route state must retain
    that fail-closed lifecycle without mislabeling it as an established failure. Require the
    classifier-owned nested evidence and matching attempt axes; a note, stage label, or generic
    stale row can never mint this state.
    """
    evidence = getattr(job, "state_evidence", None)
    attempt = evidence.get("attempt") if isinstance(evidence, dict) else None
    observed = attempt.get("observed_liveness") if isinstance(attempt, dict) else None
    if not isinstance(observed, dict):
        return False
    if not (
        getattr(job, "liveness", None) == "stale"
        and attempt.get("state") == "stale"
        and attempt.get("source") == "shared-observer"
        and attempt.get("rule") == "terminal-observed/reconcile-needed"
        and observed.get("state") == "reconcile-needed"
    ):
        return False
    for field in ("attempt_id", "route_id", "route_node"):
        expected = getattr(job, field, None)
        if expected is not None and attempt.get(field) != expected:
            return False
    return True


def _node_state(node_id, route_jobs, ev_by_node, now, completion_marked=False, degradation=None):
    """One node's state, per the priority table (PRD F-41): active (live job) beats a
    published completion marker (done); explicit killed/cancelled/fail-note evidence beats
    exact reconciliation; the current exact ``reconcile-needed`` attempt becomes
    ``reconciling``; generic dead/stale becomes failed; a clean registry row becomes done;
    no evidence is pending. `now` is the ONLY clock input — never read internally, so this
    stays hermetically testable (T1-14/T1-15).

    `completion_marked` (2026-07-24): an immutable completion marker is authoritative proof
    the node's completion gate passed, so it SUPERSEDES a dead/stale EARLIER attempt row or
    a `dead-*`/`fleet-kill` note — the exact shape of a node that died once (watchdog, supervisor
    race) and was then re-run to a marker or recovered inline. Without this, such a node rendered
    `✕` despite being done (user 2026-07-24: "execute도 X로 뜨는데 왜?"). `active` still wins above:
    a live job means the node is being re-run right now, which outranks a past marker."""
    live = [j for j in route_jobs if getattr(j, "route_node", None) == node_id]
    active = [j for j in live if j.liveness == "working"]
    if active:
        j = max(active, key=lambda row: (
            -(getattr(row, "registry_priority", None)
              if getattr(row, "registry_priority", None) is not None else 0),
            getattr(row, "registry_order", None) if getattr(row, "registry_order", None) is not None else -1,
            -(getattr(row, "elapsed_min", None) if getattr(row, "elapsed_min", None) is not None else 10**9),
        ))
        return {"state": "active", "elapsed_min": j.elapsed_min, "model": j.model,
                "harness": j.harness, "effort": j.effort, "pid": j.pid, "note": None, "job": j}
    failed_live = [j for j in live if j.liveness in ("stale", "dead")]

    def _best(rows):
        return max(rows, key=lambda row: (
            -(getattr(row, "registry_priority", None)
              if getattr(row, "registry_priority", None) is not None else 0),
            getattr(row, "registry_order", None) if getattr(row, "registry_order", None) is not None else -1,
            -(getattr(row, "elapsed_min", None) if getattr(row, "elapsed_min", None) is not None else 10**9),
        ))

    ev = ev_by_node.get(node_id) or {}
    note = ev.get("note")
    fail_note = bool(note) and (str(note).startswith("fleet-kill") or str(note).startswith("dead-"))
    ev_status = ev.get("status")
    explicit_failure = (
        ev_status in ("killed", "cancelled")
        or (ev_status == "done" and fail_note)
    )
    is_failed = bool(failed_live) or explicit_failure
    if is_failed and completion_marked:
        # Marker supersedes the SUPERSEDED dead attempt -> done. Narrowed to the failed case
        # ONLY: a `pending` node with a marker stays pending (prd.md:308 keeps `state` and
        # `gate_passed` independent for the done/no-claim/marker-outlives-job cases). We keep
        # the dead attempt's telemetry so elapsed/model still render, but drop the job handle —
        # the node is complete, not a live target.
        src = _best(failed_live) if failed_live else None
        if src is not None:
            return {"state": "done", "elapsed_min": src.elapsed_min, "model": src.model,
                    "harness": src.harness, "effort": src.effort, "pid": None,
                    "note": note, "job": None}
        return {"state": "done", "elapsed_min": _ev_elapsed(ev, now), "model": ev.get("model"),
                "harness": ev.get("harness"), "effort": ev.get("effort"), "pid": None,
                "note": note, "job": None}
    if explicit_failure:
        if failed_live:
            j = _best(failed_live)
            return {"state": "failed", "elapsed_min": j.elapsed_min, "model": j.model,
                    "harness": j.harness, "effort": j.effort, "pid": j.pid,
                    "note": note, "job": j}
        return {"state": "failed", "elapsed_min": _ev_elapsed(ev, now), "model": ev.get("model"),
                "harness": ev.get("harness"), "effort": ev.get("effort"), "pid": ev.get("pid"),
                "note": note, "job": None}
    if failed_live:
        # Choose the same canonical/newest attempt used for telemetry. An older failed retry
        # must not turn a newer exact reconciliation attempt back into a red failure, while a
        # newer generic stale/dead attempt still wins and remains failed.
        j = _best(failed_live)
        if _is_reconciling_attempt(j):
            return {"state": "reconciling", "elapsed_min": j.elapsed_min, "model": j.model,
                    "harness": j.harness, "effort": j.effort, "pid": j.pid,
                    "note": getattr(j, "note", None) or note or "reconcile-needed", "job": j}
        return {"state": "failed", "elapsed_min": j.elapsed_min, "model": j.model,
                "harness": j.harness, "effort": j.effort, "pid": j.pid,
                "note": note, "job": j}
    if ev_status == "done":
        return {"state": "done", "elapsed_min": _ev_elapsed(ev, now), "model": ev.get("model"),
                "harness": ev.get("harness"), "effort": ev.get("effort"), "pid": ev.get("pid"),
                "note": note, "job": None}
    if isinstance(degradation, dict) and degradation.get("route_node") == node_id:
        return {"state": "degraded", "elapsed_min": _ev_elapsed(degradation, now),
                "model": None, "harness": None, "effort": None, "pid": None,
                "note": degradation.get("reason"), "job": None}
    return {"state": "pending", "elapsed_min": None, "model": None, "harness": None,
            "effort": None, "pid": None, "note": None, "job": None}


def _ev_elapsed(ev, now):
    ts = ev.get("ts")
    if ts is None:
        return ev.get("elapsed_min")
    try:
        return max(0, int((now - ts) / 60))
    except Exception:
        return ev.get("elapsed_min")


def _heuristic_view(route_id, route_jobs):
    """Represent explicit-invalid evidence without fabricating a fixed pipeline."""
    return {"route_id": route_id, "route_hash": None, "source": "unknown",
            "capability": None, "capability_mode": None, "execution_topology": None,
            "unit_catalog_digest": None, "composed": False,
            "effective_intensity": None, "progress": None, "ambiguity": "route-record-mismatch",
            "nodes": [], "key": route_id}


def _record_view(record, route_id, route_jobs, ev_by_node, now, gate_marks_for_route=None,
                 degradations_for_route=None):
    levels = node_order(record)
    node_by_id = {n["id"]: n for n in (record.get("nodes") or [])
                  if isinstance(n, dict) and isinstance(n.get("id"), str)}
    marks = gate_marks_for_route or {}
    nodes = []
    done = 0
    for level_i, level in enumerate(levels):
        for nid in level:
            rn = node_by_id.get(nid, {})
            degradation_candidates = [item for item in (degradations_for_route or ())
                                      if isinstance(item, dict)
                                      and item.get("kind") == "degradation"
                                      and item.get("dispatch_depth") != 1
                                      and item.get("route_node") == nid
                                      and item.get("route_hash") == record.get("route_hash")]
            degradation = (max(degradation_candidates, key=lambda item: item.get("ts", 0))
                           if degradation_candidates else None)
            st = _node_state(nid, route_jobs, ev_by_node, now,
                             completion_marked=bool(marks.get(nid)), degradation=degradation)
            unit = rn.get("unit") if isinstance(rn.get("unit"), str) else None
            raw_unit_choices = rn.get("unit_choices")
            unit_choices = (
                list(raw_unit_choices)
                if isinstance(raw_unit_choices, list)
                and all(isinstance(choice, str) for choice in raw_unit_choices)
                else []
            )
            if st["state"] == "done":
                done += 1
            parallel_group = rn.get("parallel_group") or rn.get("replica_group")
            replica_group = rn.get("replica_group")
            nodes.append({
                "id": nid, "depends_on": list(rn.get("depends_on") or []), "level": level_i,
                "unit": unit, "unit_choices": unit_choices,
                "parallel_group": parallel_group if isinstance(parallel_group, str) else None,
                "replica_group": replica_group if isinstance(replica_group, str) else None,
                "model_profile": rn.get("model_profile") if isinstance(rn.get("model_profile"), str) else None,
                "perspective": rn.get("perspective") if isinstance(rn.get("perspective"), str) else None,
                "write_scope": rn.get("write_scope"),
                "state": st["state"], "gate": rn.get("completion_gate"),
                # True | None — a DIMENSION SEPARATE from `state` (prd.md:308). `state` says what
                # the runner is doing; `gate_passed` says whether the completion gate is proven
                # passed. They are not derivable from each other: a `done` node with no marker
                # stays no-claim, and a marker outlives the job row that produced it.
                "gate_passed": marks.get(nid) or None,
                "note": st["note"],
                "elapsed_min": st["elapsed_min"], "model": st["model"], "harness": st["harness"],
                "effort": st["effort"], "pid": st["pid"], "job": st["job"],
                "degradation": degradation if st["state"] == "degraded" else None,
            })
    return {"route_id": route_id, "route_hash": record.get("route_hash"), "source": "record",
            "capability": record.get("capability"), "capability_mode": record.get("capability_mode"),
            "execution_topology": record.get("execution_topology"),
            "unit_catalog_digest": record.get("unit_catalog_digest"),
            "composed": bool(record.get("composed")),
            "effective_intensity": record.get("effective_intensity"),
            "progress": {"done": done, "total": len(nodes)}, "nodes": nodes, "key": route_id}


def build_views(jobs, node_evidence, records, now, gate_marks=None, degradations=None):
    """PURE — jobs/node_evidence/records/now/gate_marks are the entire input; no file I/O, no
    clock read. `gate_marks` (resolve_gate_marks()'s output) is optional and defaults to "no
    marks resolved" = every node no-claim, which is exactly the honest answer for a caller that
    never looked.
    One view per distinct route_id referenced by `jobs` (via job.route_id); a job without a
    route_id contributes nothing (prd.md:302 — record-less jobs keep the existing breadcrumb,
    they never enter the route summary)."""
    node_evidence = node_evidence or {}
    records = records or {}
    gate_marks = gate_marks or {}
    by_route = {}
    order = []
    for j in jobs:
        rid = getattr(j, "route_id", None)
        if not rid:
            continue
        if rid not in by_route:
            by_route[rid] = []
            order.append(rid)
        by_route[rid].append(j)
    # A route with no LIVE job (every node done/failed, or a hermetic test exercising
    # evidence/records directly — T1-15/pending-only cases) still gets a view as long as a
    # record or terminal evidence names it — a route does not stop existing just because
    # nothing about it is currently running.
    for rid in list(records) + list(node_evidence):
        if rid not in by_route:
            by_route[rid] = []
            order.append(rid)
    views = []
    for rid in order:
        route_jobs = by_route[rid]
        record = (records or {}).get(rid)
        if record is None:
            views.append(_heuristic_view(rid, route_jobs))
        else:
            views.append(_record_view(record, rid, route_jobs, node_evidence.get(rid) or {}, now,
                                      gate_marks.get(rid), (degradations or {}).get(rid)))
    return views


def resolve_records(jobs, node_evidence=None):
    """The one impure entry point: calls load() once per distinct (route_file, route_id,
    route_hash) tuple seen across `jobs` ∪ `node_evidence` (code-test verification.md §10 — a
    route whose every carrying job has already finished has NO live job left to carry
    route_file; `node_evidence` — `_scan_route_nodes`'s terminal-row pass — is the only
    remaining place that path survives). Best-effort — a job with route_id but no route_file
    (proc jobs never export AGENT_ROUTE_HASH — §3.2.2) still gets a load() attempt with
    expect_hash=None; a failed load simply omits that route_id from the result (build_views
    then produces a heuristic view instead of raising)."""
    records = {}
    seen = set()
    for j in jobs:
        rid = getattr(j, "route_id", None)
        rf = getattr(j, "route_file", None)
        if not rid or not rf or rid in records:
            continue
        rh = getattr(j, "route_hash", None)
        cache_key = (rf, rid, rh)
        if cache_key in seen:
            continue
        seen.add(cache_key)
        rec = load(rf, expect_hash=rh, expect_id=rid)
        if rec is not None:
            records[rid] = rec
    # ★ code-test verification.md §10 fix — jobs alone under-covers a route whose surviving
    # trace is entirely terminal (registry) evidence: every node's row is `done`/`killed`/
    # `cancelled`, so `_scan_jobs_log` dropped all of them before this function ever sees a
    # job for that route_id (the same "terminal rows vanish before classification" gap
    # `_scan_route_nodes` exists to patch for NODE STATE — this patches it for RECORD LOOKUP).
    # Any one node's evidence carrying `route_file` is enough; they all point at the same file.
    for rid, nodes in (node_evidence or {}).items():
        if rid in records:
            continue
        for node_ev in (nodes or {}).values():
            rf = (node_ev or {}).get("route_file")
            if not rf:
                continue
            rh = (node_ev or {}).get("route_hash")
            cache_key = (rf, rid, rh)
            if cache_key in seen:
                continue
            seen.add(cache_key)
            rec = load(rf, expect_hash=rh, expect_id=rid)
            if rec is not None:
                records[rid] = rec
                break
    return records


def collect_views(jobs, node_evidence=None, now=None, degradations=None):
    """Convenience wrapper used by fleet.py/render.py: resolve_records() (impure) + build_views
    (pure). `now` defaults to time.time() here — the ONLY place in this module that reads the
    clock — so build_views itself never needs to."""
    now = time.time() if now is None else now
    node_evidence = node_evidence or {}
    records = resolve_records(jobs, node_evidence)
    return build_views(jobs, node_evidence, records, now, resolve_gate_marks(records), degradations)


def summary(views):
    """--json `route` key shape (prd.md:302 — "요약: capability/topology/nodes 진행/route_id").
    Drops internal fields (`job` object refs, `key`) that never belong in a JSON snapshot.
    A view with no route_id is never emitted (§3.4 — no empty-key ghost entries)."""
    out = []
    for v in views:
        if not v.get("route_id"):
            continue
        nodes = [{"id": n["id"], "depends_on": n.get("depends_on") or [], "level": n.get("level"),
                  "unit": n.get("unit"), "unit_choices": n.get("unit_choices") or [],
                  "write_scope": n.get("write_scope"),
                  "state": n.get("state"), "gate": n.get("gate"),
                  "gate_passed": n.get("gate_passed"), "note": n.get("note"),
                  "elapsed_min": n.get("elapsed_min"), "model": n.get("model"),
                  "harness": n.get("harness"), "effort": n.get("effort")}
                 for n in v.get("nodes") or []]
        for node, source in zip(nodes, v.get("nodes") or []):
            if source.get("degradation") is not None:
                node["degradation"] = source.get("degradation")
        out.append({"route_id": v.get("route_id"), "route_hash": v.get("route_hash"),
                    "source": v.get("source"), "capability": v.get("capability"),
                    "capability_mode": v.get("capability_mode"),
                    "execution_topology": v.get("execution_topology"),
                    "unit_catalog_digest": v.get("unit_catalog_digest"),
                    "composed": bool(v.get("composed")),
                    "effective_intensity": v.get("effective_intensity"),
                    "progress": v.get("progress"), "ambiguity": v.get("ambiguity"), "nodes": nodes})
    return out
