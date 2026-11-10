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
  * the route outcome sidecar ``<rt>.outcome.json`` (terminal gates, close time,
    head commit and the owner's final report text).

Zero-dep stdlib only. NEVER writes: no write-mode open, no replace, no stream write.

Contract:
  describe(record, view=None, outcome=None)  -> dict, never raises (empty dict on junk).
  read_outcome(record)                        -> full outcome sidecar dict | None, cached.
  summary_text(desc, current=True)            -> one short line for a route card / session row.
  detail_lines(desc)                          -> [(label, text, tone), ...] for a detail page.
  bounded(desc)                               -> the same dict clipped for a shell projection.

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
                   r"passing|passed|\d+/\d+)\b|시험|테스트|회귀", re.I)
_REVIEW = re.compile(r"\b(?:review(?:er|ed)?|impl-review|plan-check)\b|검토", re.I)
_BAD = re.compile(r"\b(?:FAIL(?:ED|URE)?|failing|error|regress(?:ed|ion)? found)\b|실패", re.I)
_GOOD = re.compile(r"\b(?:PASS(?:ED|ES|ING)?|SUCCESS|green|all \d+ .*pass)\b|통과|성공", re.I)
_STATE_WORDS = {"unrecorded": "no marker", "done": "done", "active": "running", "reconciling": "reconciling",
                "recovering": "recovering", "attention": "attention", "failed": "failed",
                "degraded": "degraded", "pending": "pending"}
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
    rows = {row.get("axis"): row for row in selection.get("selection_basis") or ()
            if isinstance(row, dict)}
    intensity = rows.get("compose-intensity") or {}
    requested = record.get("requested_intensity")
    effective = record.get("effective_intensity")
    escalation = [row for row in selection.get("escalation_basis") or () if isinstance(row, dict)]
    notes = []
    if isinstance(record.get("route_plan"), dict):
        kind = "frame-proposal"
        notes.append("approved frame proposal")
    elif intensity.get("source") == "explicit":
        kind = "user"
        notes.append("intensity %s given" % _text(intensity.get("signal") or requested, 24))
    elif selection.get("route_origin") == "preset":
        kind = "preset"
        notes.append("capability preset")
    elif rows:
        kind = "default"
        if shape == "direct" and selection.get("direct_predicates"):
            notes.append("all direct predicates held")
        elif shape == "framed":
            notes.append("frame shape requested")
        else:
            notes.append("intensity unspecified → %s" % _text(effective, 24))
    else:
        kind = "unknown"
    if requested and effective and requested != effective:
        notes.append("requested %s → %s" % (_text(requested, 24), _text(effective, 24)))
    for row in escalation[:2]:
        notes.append("escalated: %s" % _text(row.get("signal") or row.get("axis"), 48))
    if selection.get("inline_reason"):
        notes.append("inline: %s" % _text(selection.get("inline_reason"), 48))
    return {"kind": kind, "notes": notes}


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
        if not line or _SECRET.search(line):
            continue
        for piece in re.split(r"(?<=[.;])\s+(?=[A-Z가-힣`])", line):
            piece = piece.strip()
            if piece:
                found.append(piece)
    return found


def _tone(text):
    if _BAD.search(text) and not re.search(r"\b0 (?:errors?|failures?)\b|0 fail", text, re.I):
        return "bad" if not _GOOD.search(text) else "mixed"
    return "good" if _GOOD.search(text) else "info"


def _evidence(sentences, pattern, exclude=()):
    rows = []
    for sentence in sentences:
        if pattern.search(sentence) and not any(p.search(sentence) for p in exclude):
            rows.append({"text": _text(sentence), "tone": _tone(sentence)})
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
        passed = True if tg.get("passed") is True or stage.get("gate_passed") else None
        gates.append({"node": nid, "gate": stage.get("gate"), "passed": passed,
                      "reason": _text(tg.get("reason"), 48) or None})
    closed = (outcome or {}).get("closed_at") if isinstance((outcome or {}).get("closed_at"), str) else None
    return {
        "test_stages": [{"id": s["id"], "state": s["state"], "gate_passed": s["gate_passed"],
                         "harness": s["harness"]} for s in tests],
        "tests": _evidence(sentences, _TEST, (_CI,)),
        "reviewers": [{"id": s["id"], "state": s["state"], "gate_passed": s["gate_passed"],
                       "harness": s["harness"], "model": s["model"]} for s in reviews],
        "review_notes": _evidence(sentences, _REVIEW, (_CI, _RECORD)),
        "independent_review": bool(reviews) or any(r["kind"] == "RV" for r in worker_records),
        "worker_records": worker_records,
        "ci": ci,
        "head_commit": head,
        "completion_gates": gates,
        "terminal_gate_proven": (outcome or {}).get("terminal_gate_proven") is True,
        "closed_at": closed,
        "finish_id": bool((outcome or {}).get("inline_finish_id")),
        "result": _text(sentences[0]) if sentences else None,
    }


def _next_action(record, stages, verification, outcome):
    if verification["closed_at"] and verification["terminal_gate_proven"]:
        return {"kind": "closed", "text": "closed · nothing pending"}
    if outcome is not None and verification["closed_at"] and not verification["terminal_gate_proven"]:
        return {"kind": "attention", "text": "closed without a proven terminal gate"}
    by_id = {s["id"]: s for s in stages}
    active = [s for s in stages if s["state"] in ("active", "reconciling", "recovering")]
    if active:
        names = ", ".join(s["id"] + (" (" + s["harness"] + ")" if s["harness"] else "") for s in active[:3])
        return {"kind": "wait", "text": "wait · %s running" % names}
    failed = [s for s in stages if s["state"] in ("failed", "attention", "degraded")]
    if failed:
        return {"kind": "resume", "text": "inspect · %s %s" % (failed[0]["id"], failed[0]["state"])}
    known = any(s["state"] not in ("unknown",) for s in stages)
    for stage in stages:
        if stage["state"] in ("done",) or stage["gate_passed"]:
            continue
        deps = [by_id.get(d) for d in stage["depends_on"]]
        if all(d is None or d["state"] == "done" or d["gate_passed"] for d in deps):
            gates = [g for g in record.get("human_gates") or () if isinstance(g, str)]
            if stage["role"] == "gate" and gates and deps:
                return {"kind": "person", "text": "answer · %s" % ", ".join(gates)}
            if not known:
                return {"kind": "unknown", "text": "no live stage evidence · %s next" % stage["id"]}
            return {"kind": "start", "text": "start · %s" % stage["id"]}
    if stages and all(s["state"] == "done" or s["gate_passed"] for s in stages):
        return {"kind": "close", "text": "all stages done · route close pending"}
    return {"kind": "unknown", "text": "no next step recorded"}


def describe(record, view=None, outcome=None):
    """Read-only visibility summary of one route. Never raises; {} for unusable input."""
    try:
        return _describe(record, view, outcome)
    except Exception:  # Display-only: a malformed record never breaks a screen.
        return {}


def _describe(record, view, outcome):
    if not isinstance(record, dict) or not isinstance(record.get("route_id"), str):
        return {}
    if outcome is None:
        outcome = read_outcome(record)
    shape = _shape(record)
    terminal = (outcome or {}).get("terminal_gates") if isinstance((outcome or {}).get("terminal_gates"), dict) else {}
    closed = bool((outcome or {}).get("closed_at"))
    stages = _stages(record, view, terminal, closed)
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
        "next": _next_action(record, stages, verification, outcome),
        "links": links,
    }


_BASIS_WORD = {"user": "user", "default": "default", "frame-proposal": "frame proposal",
               "preset": "preset", "unknown": "?"}
_MARK = {True: "✓", None: "·", False: "✕"}


def _review_mark(verification, desc=None):
    reviewers = verification.get("reviewers") or ()
    frames = [s for s in (desc or {}).get("stages") or () if s.get("role") == "frame"]
    if not reviewers and frames:
        return "frame %d-way" % len(frames)
    if not reviewers:
        rv = [r for r in verification.get("worker_records") or () if r.get("kind") == "RV"]
        return "RV record" if rv else "no reviewer"
    passed = sum(1 for r in reviewers if r.get("gate_passed"))
    failed = sum(1 for r in reviewers if r.get("state") == "failed")
    return "review %d/%d%s" % (passed, len(reviewers), " ✕" if failed else "")


def _ci_mark(verification):
    rows = verification.get("ci") or ()
    if not rows:
        return None
    row = rows[0]
    mark = {"good": "✓", "bad": "✕", "mixed": "!"}.get(row.get("tone"), "·")
    return "CI " + mark + (" same-head" if row.get("same_head") else "")


def summary_text(desc, current=True):
    """One line: ``solo·default · quick · one-shot ● · no reviewer · CI ✓ · gate ✓``."""
    if not desc:
        return ""
    verification = desc.get("verification") or {}
    parts = ["%s·%s" % (desc.get("shape") or "?",
                        _BASIS_WORD.get((desc.get("basis") or {}).get("kind"), "?"))]
    if current and desc.get("current"):
        parts.append("%s %s" % (desc["current"], _STATE_WORDS.get(desc.get("current_state"),
                                                                  desc.get("current_state") or "")))
    parts.append(_review_mark(verification, desc))
    ci = _ci_mark(verification)
    if ci:
        parts.append(ci)
    gates = verification.get("completion_gates") or ()
    if gates:
        proven = verification.get("terminal_gate_proven") or all(g.get("passed") for g in gates)
        parts.append("gate " + ("✓" if proven else "·"))
    # The card (current=False) sits beside a stage rail in a narrow column: tighter joins.
    return (" · " if current else "  ").join(part.strip() for part in parts if part)


def detail_lines(desc):
    """``[(label, text, tone)]`` — tone is good|bad|mixed|info|dim; the reader picks colours."""
    if not desc:
        return []
    basis = desc.get("basis") or {}
    verification = desc.get("verification") or {}
    lines = [("route", "%s · %s · %s %s" % (
        desc.get("shape"), _BASIS_WORD.get(basis.get("kind"), "?"),
        desc.get("capability") or "·", desc.get("intensity") or ""), "info")]
    if basis.get("notes"):
        lines.append(("why", "; ".join(basis["notes"]), "dim"))
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
            verdict = "PASS (gate ✓)" if reviewer.get("gate_passed") else \
                "FAIL" if reviewer.get("state") == "failed" else _STATE_WORDS.get(reviewer.get("state"), "·")
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
    gate_text = ", ".join("%s %s" % (g.get("gate") or g.get("node"), _MARK.get(g.get("passed"), "·"))
                          for g in gates) or "·"
    if verification.get("closed_at"):
        gate_text += " · closed " + verification["closed_at"][:19].replace("T", " ")
    if verification.get("head_commit"):
        gate_text += " · head " + verification["head_commit"][:10]
    lines.append(("gate", gate_text, "good" if verification.get("terminal_gate_proven") else "info"))
    if verification.get("result"):
        lines.append(("result", verification["result"], "info"))
    nxt = desc.get("next") or {}
    lines.append(("next", nxt.get("text") or "·",
                  "good" if nxt.get("kind") == "closed" else
                  "bad" if nxt.get("kind") in ("resume", "attention") else "info"))
    return lines


def bounded(desc, max_lines=14):
    """A compact, string-only projection for another display surface (session shell)."""
    if not desc:
        return {}
    return {"schema": SCHEMA, "route_id": _text(desc.get("route_id"), 40),
            "summary": _text(summary_text(desc), 160),
            "card": _text(summary_text(desc, current=False), 96),
            "shape": _text(desc.get("shape"), 16),
            "basis": _text((desc.get("basis") or {}).get("kind"), 24),
            "next": _text((desc.get("next") or {}).get("text"), 120),
            "lines": [[_text(label, 12), _text(text, 200), _text(tone, 8)]
                      for label, text, tone in detail_lines(desc)[:max_lines]]}


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
    view = None
    try:
        tools_dir = str(Path(__file__).resolve().parents[1])
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from fleet import route as route_mod
        marks = route_mod.resolve_gate_marks({record["route_id"]: record})
        views = route_mod.build_views([], {}, {record["route_id"]: record}, __import__("time").time(), marks)
        view = next((v for v in views if v.get("route_id") == record["route_id"]), None)
    except Exception:
        view = None
    desc = describe(record, view)
    if args.json:
        print(json.dumps(desc, ensure_ascii=False, indent=2))
    else:
        print(summary_text(desc))
        for label, text, _tone in detail_lines(desc):
            print("  %-7s %s" % (label, text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
