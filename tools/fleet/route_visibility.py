"""Route visibility: one read-only summary of a route for Fleet and the session shell.

It answers, from records that already exist, what a person cannot see from a session:
which shape the route took and why, which stages it has and where it is, what was
verified (tests, independent review, same-head CI, the completion gate) and what
happens next. Nothing here decides or proves anything; it only reads and labels.

Sources, all existing and read-only:
  * the sealed route record ``<artifact_root>/.runtime/routes/<rt>.json``
    (``selection``, ``nodes``, ``completion_gates``, ``human_gates``,
    ``tracked_gate_evidence``, ``route_plan``);
  * Fleet's own route view (``route.build_views``): node state and gate marks;
  * the dispatch registry (``jobs.log``) owner attempt: liveness, start, decision;
  * the route outcome sidecar ``<rt>.outcome.json`` (terminal gates, close time,
    head commit and the owner's final report text).

Zero-dep stdlib only. NEVER writes: no write-mode open, no replace, no stream write.

Contract:
  describe(record, view=None, outcome=None)  -> dict, never raises (empty dict on junk).
  read_outcome(record)                        -> full outcome sidecar dict | None, cached.
  summary_text(desc, current=True)            -> one short line for a route card / session row.
  detail_lines(desc)                          -> [(label, text, tone), ...] for a detail page.
  owner_from_job(job)                         -> owner attempt progress from a registry row.
  observe(record, jobs=None, node_evidence=None, now=None)
                                              -> (view, owner) from the dispatch registry.
  codes(desc)                                 -> string-only enums/codes of the same reading.
  bounded(desc)                               -> the same dict clipped for a shell projection,
                                                 English sentences plus ``codes``.

CLI: ``python3 route_visibility.py <rt-id|route.json> [--artifact-root DIR] [--json]``.
"""
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = "route_visibility_v1"
_OUTCOME_CACHE = {}  # {path: (mtime, size, data|None)}

_SHAPE_BY_TOPOLOGY = {"inline": "direct", "one-shot-owner": "solo", "staged": "staged",
                      "transactional-owner": "staged"}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x1b]")
_SECRET = re.compile(
    r"(?i)(?:\b(?:bearer|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"password|passwd|secret|authorization)(?:[_ -]?(?:sentinel|value|token|key))?\s*[:=]|"
    r"\b(?:sk-[a-z0-9]{16,}|gh[pousr]_[a-z0-9]{20,})\b)")
_CI = re.compile(r"\bCI(?:\b|(?=\d))|GitHub Actions", re.I)
_RECORD = re.compile(r"(?:[\w.~/-]*/)?(?:RV|BC)-[\w.-]+\.md")
_TEST = re.compile(r"\bchecks?\b(?=.*\b(?:pass|fail))|\b(?:tests?|suites?|pytest|unittest|assertions?|regression|"
                   r"passing|passed)\b|\b\d+/\d+\s*(?:pass(?:ed|ing)?|ok|tests?|green|통과|성공)|시험|테스트|회귀", re.I)
_REVIEW = re.compile(r"\b(?:review(?:er|ed)?|impl-review|plan-check)\b|검토", re.I)
_STATE_WORDS = {"unrecorded": "no marker", "done": "done", "active": "running", "reconciling": "reconciling",
                "recovering": "recovering", "attention": "attention", "failed": "failed",
                "degraded": "degraded", "pending": "pending"}
_HEADING = re.compile(r"#{1,6}(?:\s|$)|```")
_FULL_HEAD = re.compile(r"\b[0-9a-f]{40}\b")
_MAX_EVIDENCE = 3
_MAX_LINE = 220


def _text(value, limit=_MAX_LINE):
    if value is None:
        return ""
    text = _CONTROL.sub(" ", str(value)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _route_file(record):
    rid = record.get("route_id")
    root = record.get("artifact_root")
    if not isinstance(rid, str) or not isinstance(root, str) or os.sep in rid:
        return None
    return Path(root) / ".runtime" / "routes" / (rid + ".json")


def read_outcome(record):
    """The full outcome sidecar of this exact route, or None. Cached by mtime/size."""
    if not isinstance(record, dict):
        return None
    route_file = _route_file(record)
    if route_file is None:
        return None
    path = str(route_file.with_name(route_file.stem + ".outcome.json"))
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _OUTCOME_CACHE.get(path)
    if cached is not None and cached[:2] == (st.st_mtime, st.st_size):
        data = cached[2]
    else:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            data = None
        _OUTCOME_CACHE[path] = (st.st_mtime, st.st_size, data)
    if data is None or data.get("route_id") != record.get("route_id"):
        return None
    if record.get("route_hash") and data.get("route_hash") not in (None, record.get("route_hash")):
        return None
    return dict(data, _path=path)


def _shape(record):
    selection = record.get("selection") if isinstance(record.get("selection"), dict) else {}
    shape = selection.get("shape")
    if shape in ("direct", "solo", "staged", "framed"):
        return shape
    if record.get("capability") == "route-frame":
        return "framed"
    return _SHAPE_BY_TOPOLOGY.get(record.get("execution_topology")) or "unknown"


def _basis(record, shape):
    """Why the route has this shape: the person's choice, the default, or a frame proposal."""
    selection = record.get("selection") if isinstance(record.get("selection"), dict) else {}
    basis_rows = [row for row in selection.get("selection_basis") or () if isinstance(row, dict)]
    rows = {row.get("axis"): row for row in basis_rows}
    gaps = [_text(row.get("signal"), 48) for row in basis_rows
            if row.get("axis") == "direct-predicate-gap" and row.get("signal")]
    intensity = rows.get("compose-intensity") or {}
    requested = record.get("requested_intensity")
    effective = record.get("effective_intensity")
    escalation = [row for row in selection.get("escalation_basis") or () if isinstance(row, dict)]
    notes, codes = [], []
    if isinstance(record.get("route_plan"), dict):
        kind = "frame-proposal"
        notes.append("approved frame proposal")
    elif intensity.get("source") == "explicit":
        kind = "user"
        notes.append("intensity %s given" % _text(intensity.get("signal") or requested, 24))
        codes.append("intensity-explicit")
    elif selection.get("route_origin") == "preset":
        kind = "preset"
        notes.append("capability preset")
    elif rows:
        kind = "default"
        if shape == "direct" and selection.get("direct_predicates"):
            notes.append("all direct predicates held")
            codes.append("direct-predicates-held")
        elif shape == "framed":
            notes.append("frame shape requested")
        else:
            notes.append("intensity unspecified → %s" % _text(effective, 24))
            codes.append("intensity-default")
    else:
        kind = "unknown"
    if gaps and shape != "direct":
        # The compiler's own record of the direct predicates that did not hold.
        notes.append("not direct: %s%s" % (", ".join(gaps[:3]), " +%d" % (len(gaps) - 3) if len(gaps) > 3 else ""))
        codes.extend("not-direct:" + gap for gap in gaps)
    if requested and effective and requested != effective:
        notes.append("requested %s → %s" % (_text(requested, 24), _text(effective, 24)))
        codes.append("requested:%s>%s" % (_text(requested, 24), _text(effective, 24)))
    for row in escalation[:2]:
        signal = _text(row.get("signal") or row.get("axis"), 48)
        notes.append("escalated: %s" % signal)
        codes.append("escalated:" + signal)
    if selection.get("inline_reason"):
        notes.append("inline: %s" % _text(selection.get("inline_reason"), 48))
        codes.append("inline:" + _text(selection.get("inline_reason"), 48))
    return {"kind": kind, "notes": notes, "codes": codes}


def _view_nodes(view):
    if not isinstance(view, dict):
        return {}
    return {node.get("id"): node for node in view.get("nodes") or ()
            if isinstance(node, dict) and isinstance(node.get("id"), str)}


def _node_role(node):
    unit = str(node.get("unit") or "")
    kind = str(node.get("kind") or "")
    nid = str(node.get("id") or "")
    if kind == "review-worker" or unit.startswith(("qa/code-review", "qa/plan-review")) \
            or "review" in unit or nid in ("impl-review", "plan-check", "review"):
        return "review"
    if unit == "qa/test" or nid == "test" or unit.endswith("/verifier"):
        return "test"
    if kind == "runtime-terminal":
        return "gate"
    if unit == "plan/frame":
        return "frame"
    if kind == "resource-runner":
        return "resource"
    return "work"


def _stages(record, view, terminal, closed=False):
    seen = _view_nodes(view)
    stages = []
    for node in record.get("nodes") or ():
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        nid = node["id"]
        live = seen.get(nid) or {}
        state = live.get("state")
        gate = live.get("gate_passed") is True
        tg = terminal.get(nid) if isinstance(terminal.get(nid), dict) else None
        if tg and tg.get("passed") is True:
            gate = True
        if gate and state in (None, "pending"):
            state = "done"          # an exactly validated marker is proof the node finished
        elif closed and state in (None, "pending"):
            state = "unrecorded"    # closed route, no marker left for this node: no claim either way
        stages.append({
            "id": nid, "role": _node_role(node), "unit": _text(node.get("unit"), 64) or None,
            "state": state or ("unknown" if not seen else "pending"),
            "gate": node.get("completion_gate"), "gate_passed": gate,
            "harness": _text(live.get("harness"), 24) or None,
            "model": _text(live.get("model"), 48) or None,
            "depends_on": [d for d in node.get("depends_on") or () if isinstance(d, str)],
            "note": _text(live.get("note"), 96) or None,
        })
    return stages


def _sentences(summary):
    if not isinstance(summary, str):
        return []
    found = []
    for line in summary.splitlines():
        line = line.strip(" -*\t")
        # A markdown heading (``# Title``) or fence is layout, not a sentence; ``#1403`` is.
        if not line or _HEADING.match(line) or _SECRET.search(line):
            continue
        for piece in re.split(r"(?<=[.;])\s+(?=[A-Z가-힣`])", line):
            piece = piece.strip()
            if piece:
                found.append(piece)
    return found


def _evidence(sentences, pattern, exclude=()):
    rows = []
    for sentence in sentences:
        if pattern.search(sentence) and not any(p.search(sentence) for p in exclude):
            # Shown as recorded: the reader does not judge a sentence pass or fail.
            rows.append({"text": _text(sentence), "tone": "info"})
            if len(rows) >= _MAX_EVIDENCE:
                break
    return rows


def _records(record, summary):
    """Worker brief (BC-) and review (RV-) records the final report names, as readable paths."""
    if not isinstance(summary, str):
        return []
    root = Path(str(record.get("artifact_root") or "."))
    found, seen = [], set()
    for match in _RECORD.findall(summary):
        name = os.path.basename(match)
        if name in seen:
            continue
        seen.add(name)
        if match.startswith("/"):
            path = Path(match)
        elif match.startswith(root.name + "/"):
            path = root.parent / match
        elif "/" in match:
            path = root / match
        else:
            path = None
        if path is None or not path.is_file():
            # A short ``inputs/RV-….md`` names the campaign's existing inputs folder.
            path = next((candidate for pattern in ("research/*/inputs/", "campaigns/*/inputs/")
                         for candidate in sorted(root.glob(pattern + name)) if candidate.is_file()), None)
        kind = "RV" if name.startswith("RV-") else "BC"
        found.append({"kind": kind, "name": _text(name, 96),
                      "path": str(path) if path is not None and path.is_file() else None})
        if len(found) >= 6:
            break
    return found


def _verification(record, stages, outcome):
    sentences = _sentences((outcome or {}).get("summary"))
    worker_records = _records(record, (outcome or {}).get("summary"))
    head = (outcome or {}).get("head_commit")
    head = head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{7,40}", head) else None
    tests = [s for s in stages if s["role"] == "test"]
    reviews = [s for s in stages if s["role"] == "review"]
    independence = (outcome or {}).get("review_independence")
    independence = independence if isinstance(independence, dict) else {}
    ci = _evidence(sentences, _CI)
    for row in ci:
        row["same_head"] = bool(head and (head in row["text"] or head[:7] in row["text"]))
    terminal = (outcome or {}).get("terminal_gates") if isinstance((outcome or {}).get("terminal_gates"), dict) else {}
    gates = []
    contract = record.get("workflow_contract") if isinstance(record.get("workflow_contract"), dict) else {}
    terminal_ids = [nid for nid in contract.get("terminal_nodes") or () if isinstance(nid, str)] or \
        [node.get("id") for node in record.get("nodes") or ()
         if isinstance(node, dict) and node.get("terminal") is True and isinstance(node.get("id"), str)]
    for nid in terminal_ids or list(terminal):
        tg = terminal.get(nid) if isinstance(terminal.get(nid), dict) else {}
        stage = next((s for s in stages if s["id"] == nid), {})
        passed = True if tg.get("passed") is True or stage.get("gate_passed") else \
            False if tg.get("passed") is False else None
        gates.append({"node": nid, "gate": stage.get("gate"), "passed": passed,
                      "reason": _text(tg.get("reason"), 48) or None})
    closed = (outcome or {}).get("closed_at") if isinstance((outcome or {}).get("closed_at"), str) else None
    tests_found = _evidence(sentences, _TEST, (_CI,))
    review_notes = _evidence(sentences, _REVIEW, (_CI, _RECORD))
    shown = {row["text"] for row in tests_found + review_notes + ci}
    result = _text(sentences[0]) if sentences else None

    def _independence(nid):
        row = independence.get(nid) if isinstance(independence.get(nid), dict) else {}
        return (_text(row.get("review_independence"), 32) or None,
                _text(row.get("reviewer_kind"), 32) or None)
    return {
        "test_stages": [{"id": s["id"], "state": s["state"], "gate_passed": s["gate_passed"],
                         "harness": s["harness"]} for s in tests],
        "tests": tests_found,
        "reviewers": [{"id": s["id"], "state": s["state"], "gate_passed": s["gate_passed"],
                       "harness": s["harness"], "model": s["model"],
                       "independence": _independence(s["id"])[0],
                       "reviewer_kind": _independence(s["id"])[1]} for s in reviews],
        "review_notes": review_notes,
        "independent_review": bool(reviews) or any(r["kind"] == "RV" and r["path"] for r in worker_records),
        "worker_records": worker_records,
        "ci": ci,
        "head_commit": head,
        "completion_gates": gates,
        "terminal_gate_proven": (outcome or {}).get("terminal_gate_proven") is True,
        "closed_at": closed,
        "finish_id": bool((outcome or {}).get("inline_finish_id")),
        # The first report sentence, unless it already shows as test, review or CI evidence.
        "result": result if result not in shown else None,
    }


_LIVE = ("working", "idle", "blocked")


def _field(job, *names):
    for name in names:
        value = job.get(name) if isinstance(job, dict) else getattr(job, name, None)
        if value not in (None, "", "-"):
            return value
    return None


def owner_from_job(job):
    """The owner attempt's progress as recorded by the dispatch registry (jobs.log), or None."""
    if job is None:
        return None
    decision = _field(job, "attempt_decision")
    decision = decision if isinstance(decision, dict) else {}
    started = _field(job, "_context_execution_started_at", "started_at")
    elapsed = _field(job, "elapsed_min")
    return {
        "attempt_id": _text(_field(job, "attempt_id"), 64) or None,
        "liveness": _text(_field(job, "liveness"), 16) or None,
        "status": _text(_field(job, "status"), 16) or None,
        "harness": _text(_field(job, "harness"), 16) or None,
        "model": _text(_field(job, "model", "resolved_model"), 48) or None,
        "effort": _text(_field(job, "effort"), 16) or None,
        "node": _text(_field(job, "route_node"), 64) or None,
        "started_at": float(started) if isinstance(started, (int, float)) else None,
        "elapsed_min": int(elapsed) if isinstance(elapsed, (int, float)) else None,
        "action": _text(decision.get("action"), 24) or None,
        "reason": _text(decision.get("reason") or _field(job, "note", "attention_reason"), 64) or None,
    }


def _clock(epoch):
    try:
        import time as _time
        return _time.strftime("%m-%d %H:%M", _time.localtime(float(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _inline_state(record):
    """A direct route runs in the main session: its pending inline finish, if recorded."""
    try:
        here = Path(__file__).resolve()
        for parent in here.parents:
            utilities = parent / "utilities"
            if (utilities / "inline_finish.py").is_file():
                if str(utilities) not in sys.path:
                    sys.path.insert(0, str(utilities))
                import inline_finish
                pending = inline_finish.pending_state(Path(record.get("artifact_root", "")),
                                                      record.get("route_id", ""))
                return _text((pending or {}).get("state"), 32) or None
    except Exception:
        return None
    return None


def _sealed_at(record):
    route_file = _route_file(record)
    try:
        return os.stat(route_file).st_mtime if route_file is not None else None
    except OSError:
        return None


def _no_response(view, verification):
    """Fleet's own read (``route.route_liveness``): a route still in flight with nothing running."""
    return isinstance(view, dict) and view.get("liveness") == "unknown" and not verification["closed_at"]


def _next_action(record, stages, verification, outcome, owner=None, inline=None, view=None):
    nxt = _next_step(record, stages, verification, outcome, owner, inline, view)
    nxt.setdefault("node", None)
    nxt.setdefault("harness", None)
    return nxt


def _next_step(record, stages, verification, outcome, owner, inline, view):
    if verification["closed_at"] and verification["terminal_gate_proven"]:
        return {"kind": "closed", "text": "closed · nothing pending"}
    if outcome is not None and verification["closed_at"] and not verification["terminal_gate_proven"]:
        return {"kind": "attention", "text": "closed without a proven terminal gate"}
    if owner and owner.get("liveness") in ("dead", "done", "stale") and not verification["closed_at"]:
        return {"kind": "attention", "text": "owner %s ended (%s) · route not closed" % (
            owner.get("attempt_id") or "·", owner.get("reason") or owner.get("liveness"))}
    by_id = {s["id"]: s for s in stages}
    active = [s for s in stages if s["state"] in ("active", "reconciling", "recovering")]
    if _no_response(view, verification):
        last = active[0] if active else next(
            (s for s in stages if s["state"] not in ("done", "unrecorded") and not s["gate_passed"]), None)
        return {"kind": "no-response", "node": last["id"] if last else None,
                "harness": last["harness"] if last else None,
                "text": "no response" + (" · %s last %s" % (
                    last["id"], _STATE_WORDS.get(last["state"], last["state"])) if last else "")}
    if active:
        names = ", ".join(s["id"] + (" (" + s["harness"] + ")" if s["harness"] else "") + " "
                          + _STATE_WORDS.get(s["state"], s["state"]) for s in active[:3])
        return {"kind": "wait", "node": active[0]["id"], "harness": active[0]["harness"],
                "text": "wait · %s" % names}
    failed = [s for s in stages if s["state"] in ("failed", "attention", "degraded")]
    if failed:
        return {"kind": "resume", "node": failed[0]["id"], "harness": failed[0]["harness"],
                "text": "inspect · %s %s" % (failed[0]["id"], failed[0]["state"])}
    if owner and owner.get("liveness") in _LIVE:
        since = _clock(owner.get("started_at"))
        return {"kind": "wait", "node": owner.get("node"), "harness": owner.get("harness"),
                "text": "wait · owner %s %s%s" % (
                    owner.get("harness") or "·", owner.get("liveness"), " since " + since if since else "")}
    if inline and _shape(record) == "direct":
        return {"kind": "start" if inline != "finished" else "close",
                "text": "inline finish %s in the main session" % inline}
    known = any(s["state"] not in ("unknown",) for s in stages)
    for stage in stages:
        if stage["state"] in ("done",) or stage["gate_passed"]:
            continue
        deps = [by_id.get(d) for d in stage["depends_on"]]
        if all(d is None or d["state"] == "done" or d["gate_passed"] for d in deps):
            gates = [g for g in record.get("human_gates") or () if isinstance(g, str)]
            if stage["role"] == "gate" and gates and deps:
                return {"kind": "person", "node": stage["id"], "text": "answer · %s" % ", ".join(gates)}
            if not known:
                return {"kind": "unknown", "node": stage["id"],
                        "text": "no live stage evidence · %s next" % stage["id"]}
            return {"kind": "start", "node": stage["id"], "text": "start · %s" % stage["id"]}
    if stages and all(s["state"] == "done" or s["gate_passed"] for s in stages):
        return {"kind": "close", "text": "all stages done · route close pending"}
    return {"kind": "unknown", "text": "no next step recorded"}


def describe(record, view=None, outcome=None, owner=None):
    """Read-only visibility summary of one route. Never raises; {} for unusable input.

    ``owner`` is ``owner_from_job(job)`` for the route's owner attempt (Fleet passes its
    own; ``observe`` reads it from the dispatch registry)."""
    try:
        return _describe(record, view, outcome, owner)
    except Exception:  # Display-only: a malformed record never breaks a screen.
        return {}


def _describe(record, view, outcome, owner=None):
    if not isinstance(record, dict) or not isinstance(record.get("route_id"), str):
        return {}
    if outcome is None:
        outcome = read_outcome(record)
    shape = _shape(record)
    terminal = (outcome or {}).get("terminal_gates") if isinstance((outcome or {}).get("terminal_gates"), dict) else {}
    closed = bool((outcome or {}).get("closed_at"))
    stages = _stages(record, view, terminal, closed)
    if owner is not None and not isinstance(owner, dict):
        owner = owner_from_job(owner)
    if owner and owner.get("node") and owner.get("liveness") in _LIVE and not (
            isinstance(view, dict) and view.get("liveness") == "unknown" and not closed):
        for stage in stages:      # the owner's own node runs while its attempt is live
            if stage["id"] == owner["node"] and stage["state"] in ("unknown", "pending"):
                stage["state"] = "active"
                stage["harness"] = stage["harness"] or owner.get("harness")
    inline = _inline_state(record) if shape == "direct" and not closed else None
    verification = _verification(record, stages, outcome)
    current = next((s for s in stages if s["state"] in ("active", "reconciling", "recovering",
                                                          "attention", "failed")), None) \
        or next((s for s in stages if s["state"] not in ("done", "unrecorded") and not s["gate_passed"]), None)
    links = []
    route_file = _route_file(record)
    if route_file is not None:
        links.append({"label": "route", "path": str(route_file)})
    if outcome and outcome.get("_path"):
        links.append({"label": "outcome", "path": outcome["_path"]})
    for nid, tg in sorted(terminal.items()):
        if isinstance(tg, dict) and isinstance(tg.get("evidence"), str):
            links.append({"label": "evidence " + _text(nid, 32), "path": _text(tg["evidence"], 400)})
    for item in verification["worker_records"]:
        if item.get("path"):
            links.append({"label": item["kind"], "path": item["path"]})
    tracked = record.get("tracked_gate_evidence") if isinstance(record.get("tracked_gate_evidence"), dict) else {}
    spec_read = tracked.get("spec_read") if isinstance(tracked.get("spec_read"), dict) else {}
    return {
        "schema": SCHEMA,
        "route_id": record["route_id"],
        "capability": _text(record.get("capability"), 48) or None,
        "intensity": _text(record.get("effective_intensity"), 24) or None,
        "topology": _text(record.get("execution_topology"), 32) or None,
        "tracking": _text(record.get("tracking"), 24) or None,
        "shape": shape,
        "basis": _basis(record, shape),
        "stages": stages,
        "current": current["id"] if current else None,
        "current_state": current["state"] if current else ("done" if stages else None),
        "verification": verification,
        "spec": {"read": spec_read.get("satisfied") is True,
                 "drift": _text(tracked.get("drift_verdict"), 120) or None},
        "owner": owner or None,
        "inline_finish": inline,
        "sealed_at": _sealed_at(record),
        "next": _next_action(record, stages, verification, outcome, owner, inline, view),
        "links": links,
    }


_BASIS_WORD = {"user": "user", "default": "default", "frame-proposal": "frame proposal",
               "preset": "preset", "unknown": "basis unrecorded"}
_MARK = {True: "✓", None: "·", False: "✕"}


def _review_mark(verification, desc=None):
    reviewers = verification.get("reviewers") or ()
    frames = [s for s in (desc or {}).get("stages") or () if s.get("role") == "frame"]
    if not reviewers and frames:
        return "frame %d-way" % len(frames)
    if not reviewers:
        # Only an RV record that was found on disk marks the card.
        rv = [r for r in verification.get("worker_records") or () if r.get("kind") == "RV" and r.get("path")]
        return "RV record" if rv else "no reviewer"
    passed = sum(1 for r in reviewers if r.get("gate_passed"))
    failed = sum(1 for r in reviewers if r.get("state") == "failed")
    return "review %d/%d%s" % (passed, len(reviewers), " ✕" if failed else "")


def _ci_code(verification):
    rows = verification.get("ci") or ()
    if not rows:
        return "none"
    return "same-head" if any(row.get("same_head") for row in rows) else "recorded"


def _ci_mark(verification):
    """The card names CI only when a recorded CI sentence names this route's own head.

    Any other CI sentence (not run, failed, a mention) stays on the detail line as written."""
    return "CI same-head" if _ci_code(verification) == "same-head" else None


def _gate_code(verification):
    gates = verification.get("completion_gates") or ()
    if not gates:
        return "proven" if verification.get("terminal_gate_proven") else "none"
    if verification.get("terminal_gate_proven") or all(g.get("passed") is True for g in gates):
        return "proven"
    if any(g.get("passed") is False for g in gates):
        return "failed"
    return "unproven"


def summary_text(desc, current=True):
    """One line: ``solo·default · one-shot running · no reviewer · CI recorded same-head · gate ✓``."""
    if not desc:
        return ""
    verification = desc.get("verification") or {}
    parts = ["%s·%s" % (desc.get("shape") or "unknown",
                        _BASIS_WORD.get((desc.get("basis") or {}).get("kind"), "basis unrecorded"))]
    if current and desc.get("current"):
        if (desc.get("next") or {}).get("kind") == "no-response":
            parts.append("%s no response" % desc["current"])
        else:
            parts.append("%s %s" % (desc["current"], _STATE_WORDS.get(desc.get("current_state"),
                                                                      desc.get("current_state") or "")))
    parts.append(_review_mark(verification, desc))
    ci = _ci_mark(verification)
    if ci:
        parts.append(ci)
    if verification.get("completion_gates"):
        parts.append("gate " + {"proven": "✓", "failed": "✕"}.get(_gate_code(verification), "·"))
    # The card (current=False) sits beside a stage rail in a narrow column: tighter joins.
    return (" · " if current else "  ").join(part.strip() for part in parts if part)


def detail_lines(desc):
    """``[(label, text, tone)]`` — tone is good|bad|mixed|info|dim; the reader picks colours."""
    if not desc:
        return []
    basis = desc.get("basis") or {}
    verification = desc.get("verification") or {}
    lines = [("route", "%s · %s · %s %s" % (
        desc.get("shape"), _BASIS_WORD.get(basis.get("kind"), "basis unrecorded"),
        desc.get("capability") or "·", desc.get("intensity") or ""), "info")]
    if basis.get("notes"):
        lines.append(("why", "; ".join(basis["notes"]), "dim"))
    owner = desc.get("owner") or {}
    if owner:
        since = _clock(owner.get("started_at"))
        lines.append(("owner", " · ".join(x for x in (
            " ".join(x for x in (owner.get("harness"), owner.get("model"), owner.get("effort")) if x) or None,
            "%s%s" % (owner.get("liveness") or owner.get("status") or "·",
                      ((" since " if owner.get("liveness") in _LIVE else " · started ") + since)
                      if since else ""),
            "%dm" % owner["elapsed_min"] if owner.get("elapsed_min") is not None else None,
            owner.get("attempt_id"),
            "registry: %s (%s)" % (owner["action"], owner.get("reason") or "·") if owner.get("action") else None)
            if x), "info" if owner.get("liveness") in _LIVE else "dim"))
    elif desc.get("shape") == "direct":
        sealed = _clock(desc.get("sealed_at"))
        lines.append(("owner", "main session (inline)%s%s" % (
            " · sealed " + sealed if sealed else "",
            " · finish " + desc["inline_finish"] if desc.get("inline_finish") else ""), "dim"))
    elif desc.get("shape") != "framed" and not (desc.get("verification") or {}).get("closed_at"):
        lines.append(("owner", "no owner attempt in the dispatch registry", "dim"))
    stage_text = "  ".join("%s %s%s" % (
        s["id"], _STATE_WORDS.get(s["state"], s["state"]), " ✓" if s["gate_passed"] else "")
        for s in desc.get("stages") or ())
    lines.append(("stages", stage_text or "·", "info"))
    for stage in verification.get("test_stages") or ():
        lines.append(("test", "%s %s%s" % (stage["id"], _STATE_WORDS.get(stage["state"], stage["state"]),
                                           " · gate ✓" if stage["gate_passed"] else ""),
                      "good" if stage["gate_passed"] else "info"))
    for row in verification.get("tests") or ():
        lines.append(("test", row["text"], row["tone"]))
    if verification.get("reviewers"):
        for reviewer in verification["reviewers"]:
            who = " ".join(x for x in (reviewer.get("harness"), reviewer.get("model")) if x) or "harness ·"
            verdict = "gate ✓" if reviewer.get("gate_passed") else \
                _STATE_WORDS.get(reviewer.get("state"), reviewer.get("state") or "·")
            if reviewer.get("independence"):
                verdict += " · %s%s" % (reviewer["independence"], " (%s)" % reviewer["reviewer_kind"]
                                         if reviewer.get("reviewer_kind") else "")
            lines.append(("review", "%s · %s · %s" % (reviewer["id"], who, verdict),
                          "good" if reviewer.get("gate_passed") else
                          "bad" if reviewer.get("state") == "failed" else "info"))
    elif any(s.get("role") == "frame" for s in desc.get("stages") or ()):
        frames = [s for s in desc["stages"] if s.get("role") == "frame"]
        lines.append(("review", "%d independent frame legs; reviewers belong to the route it proposes"
                      % len(frames), "dim"))
    else:
        lines.append(("review", "no independent reviewer stage in this route", "dim"))
    for item in verification.get("worker_records") or ():
        lines.append(("review" if item["kind"] == "RV" else "brief",
                      "%s record %s%s" % (item["kind"], item["name"], "" if item.get("path") else " (not found)"),
                      "info"))
    for row in verification.get("review_notes") or ():
        lines.append(("review", row["text"], row["tone"]))
    for row in verification.get("ci") or ():
        lines.append(("CI", row["text"] + ("  [same head]" if row.get("same_head") else ""), row["tone"]))
    if not verification.get("ci"):
        lines.append(("CI", "no CI result in the route outcome", "dim"))
    gates = verification.get("completion_gates") or ()
    gate_text = ", ".join("%s %s%s" % (g.get("gate") or g.get("node"), _MARK.get(g.get("passed"), "·"),
                                       " " + g["reason"] if g.get("passed") is False and g.get("reason") else "")
                          for g in gates) or "·"
    if verification.get("closed_at"):
        gate_text += " · closed " + verification["closed_at"][:19].replace("T", " ")
    if verification.get("head_commit"):
        gate_text += " · head " + verification["head_commit"][:10]
    lines.append(("gate", gate_text, "good" if verification.get("terminal_gate_proven") else
                  "bad" if _gate_code(verification) == "failed" else "info"))
    if verification.get("result"):
        lines.append(("result", verification["result"], "info"))
    nxt = desc.get("next") or {}
    lines.append(("next", nxt.get("text") or "·",
                  "good" if nxt.get("kind") == "closed" else
                  "bad" if nxt.get("kind") in ("resume", "attention", "no-response") else "info"))
    return lines


CODES_SCHEMA = "route_visibility_codes_v1"


def _s(value, limit=64):
    """A code value as a string; ``""`` when absent (never a placeholder like ``?``)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _text(value, limit)


def _review_code(verification, desc):
    if verification.get("reviewers"):
        return "stages"
    if any(s.get("role") == "frame" for s in desc.get("stages") or ()):
        return "frame"
    if any(r.get("kind") == "RV" and r.get("path") for r in verification.get("worker_records") or ()):
        return "rv-record"
    return "none"


def codes(desc):
    """Structured, string-only codes beside the English sentences, for a display layer that
    translates or abbreviates on its own (the session shell). Enums, not prose:

    * ``stages``: ``[[id, role, state, gate_passed "1"|"0"], ...]``; ``state`` is one of
      done|active|reconciling|recovering|pending|failed|attention|degraded|unrecorded|unknown;
    * ``basis``: ``{kind: user|default|frame-proposal|preset|unknown, codes: [...]}``;
    * ``next``: ``{kind: closed|attention|no-response|wait|resume|person|start|close|unknown,
      node, harness}``;
    * ``verification``: review none|stages|rv-record|frame, review_passed/total, independence,
      ci none|recorded|same-head, ci_text (as written), ci_text_short (40-hex heads cut to 7),
      gate none|proven|unproven|failed, gate_reasons, head (7), head_full, closed_at (ISO);
    * ``owner``: liveness, started_at (epoch seconds), elapsed_min, harness, attempt_id;
    * ``quoted``: ``[[label, text], ...]`` report sentences as written (never translate).
    Absent values are ``""``."""
    if not desc:
        return {}
    verification = desc.get("verification") or {}
    basis = desc.get("basis") or {}
    nxt = desc.get("next") or {}
    owner = desc.get("owner") or {}
    reviewers = verification.get("reviewers") or ()
    head = verification.get("head_commit") or ""
    ci_rows = verification.get("ci") or ()
    ci_text = ci_rows[0]["text"] if ci_rows else ""
    independence = sorted({r.get("independence") for r in reviewers if r.get("independence")})
    quoted = [[label, row["text"]] for label, key in (("test", "tests"), ("review", "review_notes"), ("CI", "ci"))
              for row in verification.get(key) or ()]
    if verification.get("result"):
        quoted.append(["result", verification["result"]])
    return {
        "schema": CODES_SCHEMA,
        "stages": [[_s(s.get("id")), _s(s.get("role"), 16), _s(s.get("state"), 16),
                    "1" if s.get("gate_passed") else "0"] for s in desc.get("stages") or ()],
        "basis": {"kind": _s(basis.get("kind"), 24) or "unknown",
                  "codes": [_s(code, 80) for code in basis.get("codes") or ()]},
        "next": {"kind": _s(nxt.get("kind"), 16) or "unknown", "node": _s(nxt.get("node")),
                 "harness": _s(nxt.get("harness"), 24)},
        "verification": {
            "review": _review_code(verification, desc),
            "review_passed": str(sum(1 for r in reviewers if r.get("gate_passed"))),
            "review_total": str(len(reviewers)),
            "independence": ",".join(independence),
            "ci": _ci_code(verification),
            "ci_text": _s(ci_text, 200),
            "ci_text_short": _s(_FULL_HEAD.sub(lambda m: m.group(0)[:7], ci_text), 200),
            "gate": _gate_code(verification),
            "gate_reasons": [_s(g.get("reason"), 48) for g in verification.get("completion_gates") or ()
                             if g.get("passed") is not True and g.get("reason")],
            "head": head[:7], "head_full": head,
            "closed_at": _s(verification.get("closed_at"), 40),
        },
        "owner": {"liveness": _s(owner.get("liveness"), 16), "started_at": _s(owner.get("started_at"), 24),
                  "elapsed_min": _s(owner.get("elapsed_min"), 12), "harness": _s(owner.get("harness"), 16),
                  "attempt_id": _s(owner.get("attempt_id"))},
        "quoted": [[_s(label, 12), _s(text, 200)] for label, text in quoted],
    }


def bounded(desc, max_lines=14):
    """A compact, string-only projection for another display surface (session shell).

    The sentences (``summary``/``card``/``next``/``owner``/``lines``) are the English fallback;
    ``codes`` carries the same reading as enums and codes (see ``codes``)."""
    if not desc:
        return {}
    return {"schema": SCHEMA, "route_id": _text(desc.get("route_id"), 40),
            "summary": _text(summary_text(desc), 160),
            "card": _text(summary_text(desc, current=False), 96),
            "shape": _text(desc.get("shape"), 16),
            "basis": _text((desc.get("basis") or {}).get("kind"), 24),
            "next": _text((desc.get("next") or {}).get("text"), 120),
            "owner": _text(" ".join(x for x in (
                ((desc.get("owner") or {}).get("liveness")),
                _clock((desc.get("owner") or {}).get("started_at"))) if x), 48),
            "lines": [[_text(label, 12), _text(text, 200), _text(tone, 8)]
                      for label, text, tone in detail_lines(desc)[:max_lines]],
            "codes": codes(desc)}


def observe(record, jobs=None, node_evidence=None, now=None):
    """``(view, owner)`` read from the dispatch registry for callers without Fleet's snapshot.

    Uses Fleet's own collector and route view (read-only). Pass ``jobs``/``node_evidence``
    from an existing collection to avoid a second registry read."""
    import time as _time
    tools_dir = str(Path(__file__).resolve().parents[1])
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from fleet import route as route_mod
    if jobs is None:
        from fleet.collectors import dispatch
        jobs = dispatch.collect()
        node_evidence = dispatch.collect.last_route_nodes
    rid = record["route_id"]
    route_jobs = [job for job in jobs if _field(job, "route_id", "owner_route_id") == rid]
    now = _time.time() if now is None else now
    marks = route_mod.resolve_gate_marks({rid: record}, jobs=route_jobs, node_evidence=node_evidence)
    views = route_mod.build_views(route_jobs, node_evidence or {}, {rid: record}, now, marks)
    view = next((v for v in views if v.get("route_id") == rid), None)
    expected = record.get("owner_attempt_id")
    owners = [job for job in route_jobs if expected not in (None, "", "-") and _field(job, "attempt_id") == expected] \
        or [job for job in route_jobs if _field(job, "worker_type") == "owner"
            or _field(job, "dispatch_depth") == 1]
    owner = None
    if owners:
        try:
            from fleet.model import current_attempt_rank
            owner = max(owners, key=current_attempt_rank)
        except Exception:
            owner = owners[-1]
    return view, owner_from_job(owner)


def _load_record(arg, artifact_root=None):
    path = Path(arg)
    if not path.suffix:
        root = Path(artifact_root or os.environ.get("AGENT_ARTIFACT_ROOT") or ".agent_reports")
        path = root / ".runtime" / "routes" / (arg + ".json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Show what a route did and what was verified (read-only).")
    parser.add_argument("route", help="route id (rt-...) or route record path")
    parser.add_argument("--artifact-root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        record = _load_record(args.route, args.artifact_root)
    except (OSError, ValueError) as exc:
        print("route record unreadable: %s" % exc, file=sys.stderr)
        return 2
    try:
        view, owner = observe(record)
    except Exception as exc:  # The record alone still describes shape, basis and outcome.
        print("dispatch registry unreadable (%s); showing the record only" % type(exc).__name__,
              file=sys.stderr)
        view, owner = None, None
    desc = describe(record, view, owner=owner)
    if args.json:
        print(json.dumps(desc, ensure_ascii=False, indent=2))
    else:
        print(summary_text(desc))
        for label, text, _tone in detail_lines(desc):
            print("  %-7s %s" % (label, text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
