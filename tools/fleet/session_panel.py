"""Bounded shell presentation of Fleet-owned facts, never input authority."""
from .config import ui_text
from .session_line import status_lines, _clean_model, _model_key, _effort_text, _eff_key
from .session_handle import sanitize_title
from .token_budget import compaction_imminent


def presentation(values, view, harness, sid, columns):
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
                stages.append({"name": clean(node.get("name")), "state": clean(node.get("st"))})
        worker = row.get("worker") or []
        reason = "".join(str(part[0]) for part in worker if isinstance(part, (list, tuple)) and part)
        routes.append({"id": clean(row.get("id")), "title": clean(row.get("title")),
                       "state": clean(row.get("state")), "stage": clean(row.get("worker_stage")),
                       "stages": stages, "stage_more": row.get("stage_more", 0),
                       "reason": clean(reason), "outcome": clean(row.get("outcome"))})
    model = values.get("model")
    model_line = [(_clean_model(model) or ui_text("fleet.session_panel.checking"), _model_key(model))]
    effort = _effort_text(values.get("effort"))
    if effort:
        model_line += [(" · " + effort, _eff_key(values.get("effort"), False))]
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
            "status": status_lines(values, max(20, columns - 4), wrap_long=True)}
