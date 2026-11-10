"""Bounded shell presentation of Fleet-owned facts, never input authority."""
import re
from .config import ui_text
from .session_line import status_lines, _clean_model, _model_key, _effort_text, _eff_key, _percent, _level
from .session_handle import sanitize_title
from .token_budget import compaction_imminent


def state_label(value):
    code = {"active": "working", "running": "working", "working": "working",
            "done": "done", "completed": "done", "pending": "waiting", "todo": "waiting",
            "waiting": "waiting", "wait": "waiting", "idle": "idle", "failed": "failed",
            "dead": "failed", **{key: key for key in ("reconciling", "recovering", "attention", "degraded", "unrecorded")}}.get(str(value or ""), "unknown")
    return ui_text("fleet.session_panel.state_" + code)


def evidence_text(value):
    text = sanitize_title(str(value or ""))
    text = re.sub(r"\b[0-9a-f]{40}\b", lambda match: match[0][:7], text, flags=re.I)
    return " · ".join(part.strip() for part in re.split(r"\s*·\s*", text) if part.strip() not in {"", "·"})


def route_evidence(visibility):
    """Translate reader codes only; quoted evidence is never a verdict."""
    codes = visibility.get("codes")
    if not isinstance(codes, dict) or codes.get("schema") != "route_visibility_codes_v1":
        return {"summary": ui_text("fleet.session_panel.evidence_unknown"), "next": "", "lines": []}
    text = lambda key: ui_text("fleet.session_panel." + key)
    verification = codes.get("verification") or {}
    stages = codes.get("stages") or []
    shape = codes.get("shape") or "unknown"
    shape = shape if shape in {"direct", "solo", "staged", "framed"} else "unknown"
    summary = [text("shape_" + shape)]
    tests = [row for row in stages if len(row) == 4 and row[1] == "test"]
    test_state = "pass" if tests and all(row[2] == "done" and row[3] == "1" for row in tests) else "recorded" if tests else "none"
    summary.append(text("short_test_" + test_state))
    review = verification.get("review")
    passed, total = verification.get("review_passed"), verification.get("review_total")
    review_state = "pass" if review == "stages" and total and total != "0" and passed == total else "recorded" if review not in {None, "none"} else "none"
    summary.append(text("short_review_" + review_state))
    if verification.get("ci") == "same-head":
        summary.append(text("short_ci_same_head"))
    else:
        summary.append("CI ?")
    lines = [[text("evidence_route"), text("shape_" + shape), "info"]]
    basis = codes.get("basis") or {}
    kind = basis.get("kind")
    lines.append([text("evidence_why"), text("basis_" + (kind if kind in {"user", "default", "frame-proposal", "preset"} else "unknown")), "dim"])
    for row in stages[:16]:
        if len(row) != 4: continue
        role = row[1] if row[1] in {"work", "review", "test", "gate", "frame", "resource"} else "work"
        lines.append([text("evidence_stages"), evidence_text(" · ".join([row[0], text("role_" + role), state_label(row[2])])), "info"])
    lines.extend([[text("evidence_test"), text("test_" + test_state), "dim"],
                  [text("evidence_review"), text("review_" + review_state), "dim"]])
    if verification.get("ci") in {"recorded", "same-head"}:
        lines.append(["CI", evidence_text(verification.get("ci_text_short")), "info"])
    else:
        lines.append(["CI", text("not_recorded"), "dim"])
    gate = verification.get("gate")
    finish = [text("gate_" + (gate if gate in {"none", "proven", "unproven", "failed"} else "none")),
              verification.get("head"), verification.get("closed_at")]
    lines.append([text("evidence_gate"), evidence_text(" · ".join(str(part) for part in finish if part)), "dim"])
    for label, quote in (codes.get("quoted") or [])[:8]:
        if label in {"test", "review", "CI", "result"} and quote:
            lines.append([text("evidence_" + label.lower()), evidence_text(quote), "info"])
    nxt = codes.get("next") or {}
    kind = nxt.get("kind")
    kind = kind if kind in {"closed", "attention", "no-response", "wait", "resume", "person", "start", "close"} else "unknown"
    following = evidence_text(" · ".join(filter(None, [text("next_" + kind), nxt.get("node"), nxt.get("harness")])))
    lines.append([text("evidence_next"), following, "info"])
    return {"summary": summary[0] + " · " + " ".join(summary[1:]), "next": following, "lines": lines}


def compact_status(values, columns, native_owned=()):
    """One common bottom row; native-owned fields and full IDs are not repeated."""
    groups = []
    if "context" not in native_owned:
        percentage = _percent(values.get("ctx_pct"))
        groups.append([(ui_text("fleet.session_panel.context") + " ", "dim"),
                       (percentage, _level(values.get("ctx_pct")) if percentage != "?" else "dim")])
    if "quota" not in native_owned:
        for label, used, _reset in values.get("quota_windows") or [("5h", None, None), ("7d", values.get("weekly_pct"), None)]:
            groups.append([(str(label) + " ", "dim"), (_percent(used), _level(used) if used is not None else "dim")])
    parts = []
    for group in groups:
        if parts:parts.append((" · ", "dim"))
        parts.extend(group)
    return [parts] if parts else []


NOTICE_NEWS_SECONDS = 600


def notice_line(values, routes, questions, display, now):
    """The one line under Claude's input while the panel paints the status area.

    Only what the person must know now, one item by priority: an answer this
    session owes, a warning already computed elsewhere, then fresh news. No
    item means an empty line. Values, glyphs and colors are the panel's own;
    nothing here collects, judges or authorizes anything new.
    """
    from .render_cells import _LIVE_GLYPH
    waiting = sum(1 for item in questions or () if isinstance(item, dict) and item.get("answerable") is True
                  and (item.get("request_state") or "open") == "open")
    if waiting:
        return [(_LIVE_GLYPH["blocked"] + " ", "g_blocked"),
                (ui_text("fleet.session_panel.answer") + " " + str(waiting), "lvl_y"),
                (" · Ctrl+]", "dim")]
    if compaction_imminent(values.get("compaction_pressure")):
        return [("⚠ ", "lvl_r"), (ui_text("fleet.context.compaction_imminent"), "lvl_r")]
    readings = [(ui_text("fleet.session_panel.context"), values.get("ctx_pct"))]
    readings += [(str(label), used) for label, used, _reset in values.get("quota_windows") or ()]
    readings += [(str(label), used) for label, used in values.get("model_quotas") or ()]
    for label, used in readings:
        if _percent(used) != "?" and _level(used) == "lvl_r":
            return [("⚠ ", "lvl_r"), (label + " ", "dim"), (_percent(used), "lvl_r")]
    for route in routes or ():
        if route.get("state") in {"dead", "failed", "attention", "degraded"}:
            return [(_LIVE_GLYPH["dead"] + " ", "g_dead"), (route.get("state_label") or "", "g_dead"),
                    (" · " + (route.get("title") or route.get("id") or ""), "loc_repo")]
    human = (display or {}).get("human") or {}
    since = human.get("at") if type(human.get("at")) in (int, float) else 0
    news = []
    for row in (display or {}).get("received") or ():
        at = row.get("at")
        if type(at) in (int, float) and at > since and 0 <= now - at <= NOTICE_NEWS_SECONDS:
            news.append((at, [("⇄ ", "loc_repo"), (sanitize_title(str(row.get("sender") or "peer"))[:48] + " · ", "loc_repo"),
                              (sanitize_title(str(row.get("text") or ""))[:160], "dim")]))
    for route in routes or ():
        at = route.get("result_at")
        if (route.get("state") == "done" and route.get("outcome") and type(at) in (int, float)
                and at > since and 0 <= now - at <= NOTICE_NEWS_SECONDS):
            news.append((at, [(_LIVE_GLYPH["done"] + " ", "lvl_g"),
                              (ui_text("fleet.session_panel.ready") + " · ", "lvl_g"),
                              (route.get("title") or route.get("id") or "", "loc_repo"),
                              (" · " + route["outcome"], "dim")]))
    return max(news, key=lambda item: item[0])[1] if news else []


def presentation(values, view, harness, sid, columns, *, native_owned=()):
    key = harness + "\0" + sid
    raw = (view.get("route_panels") or {}).get(key)
    routes = []
    for row in raw[:8] if isinstance(raw, list) else ():
        if not isinstance(row, dict):
            continue
        clean = lambda value: sanitize_title(str(value or ""))[:320]
        stages = []
        for node in (row.get("stages") or ())[:16]:
            if isinstance(node, dict):
                stages.append({"name": clean(node.get("name")), "state": state_label(node.get("st")), "current": node.get("st") in {"active", "running", "working"}})
        worker = row.get("worker") or []
        reason = "".join(str(part[0]) for part in worker if isinstance(part, (list, tuple)) and part)
        visibility = row.get("visibility") if isinstance(row.get("visibility"), dict) else {}
        routes.append({"id": clean(row.get("id")), "title": clean(row.get("title")),
                       "state": clean(row.get("state")), "state_label": state_label(row.get("state")), "stage": clean(row.get("worker_stage")),
                       "stages": stages, "stage_more": row.get("stage_more", 0),
                       "visibility": route_evidence(visibility),
                       "reason": clean(reason), "outcome": clean(row.get("outcome")),
                       "result_at": row.get("result_at") if type(row.get("result_at")) in (int, float) else None})
    model = values.get("model")
    model_line = [(_clean_model(model) or ui_text("fleet.session_panel.checking"), _model_key(model))]
    effort = _effort_text(values.get("effort"))
    model_line += [(" · " + (effort or "?"), _eff_key(values.get("effort"), False) if effort else "dim")]
    labels = {name: ui_text("fleet.session_panel." + name) for name in (
        "settings", "settings_title", "checking", "work", "no_work", "routes", "no_routes",
        "results", "details", "back", "apply", "cancel", "defaults", "layout", "width",
        "narrow", "auxiliary", "height", "threshold", "side", "bottom", "compact", "summary",
        "detail", "working", "answer", "ready", "unknown", "fixed_width", "saved", "save_failed")}
    return {"model": model_line, "model_value": model, "effort_value": values.get("effort"),
            "warning": ui_text("fleet.context.compaction_imminent") if compaction_imminent(values.get("compaction_pressure")) else "",
            "routes": routes, "routes_known": raw is not None,
            "route_label": values.get("route_label") or "",
            "title": routes[0]["title"] if routes else "", "labels": labels,
            "summary": compact_status(values, columns, native_owned),
            "status": status_lines(values, max(20, columns - 4), wrap_long=True)}
