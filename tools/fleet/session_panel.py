"""Bounded shell presentation of Fleet-owned facts, never input authority."""
import re
from .config import ui_text
from .session_line import route_text, status_lines, _clean_model, _model_key, _effort_text, _eff_key, _percent, _level
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
    # The route's completion gate, as Fleet's route card marks it (``gate ✓``): a one-shot
    # route has no test or review stage, so this is its one recorded verification.
    if verification.get("gate") in {"proven", "failed", "unproven"}:
        summary.append(text("short_gate_" + verification["gate"]))
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


def stage_mark(state):
    """A stage's mark as Fleet's stage rail draws it: done ✓, running ●, failed ✕, else none."""
    return {"active": "●", "running": "●", "working": "●", "reconciling": "●", "recovering": "●",
            "done": "✓", "completed": "✓", "failed": "✕", "dead": "✕"}.get(str(state or ""), "")


def stage_doer(node):
    """The stage's own doer values from Fleet's route record: harness, model, effort, time, round."""
    from .session_line import _clean_model, effort_label
    shown = {}
    harness = sanitize_title(str(node.get("h") or ""))[:32]
    if harness:
        shown["harness"] = harness
    if node.get("m"):
        shown["model"] = _clean_model(node["m"]) or sanitize_title(str(node["m"]))[:64]
    if effort_label(node.get("e")):
        shown["effort"] = effort_label(node.get("e"))
    if node.get("el"):
        shown["elapsed"] = sanitize_title(str(node["el"]))[:32]
    if type(node.get("r")) is int and node["r"] > 1:
        shown["round"] = node["r"]
    if node.get("gate") is True:
        shown["gate"] = True
    for key in ("verdict", "when", "unit", "out", "note", "says"):
        if node.get(key):
            shown[key] = sanitize_title(str(node[key]))[:160]
    return shown


def route_kind(row):
    """``autopilot-code (dev·q)``, the owner's model words and context: Fleet's unfolded kind line."""
    from .session_line import _clean_model, effort_label, token_text, _percent
    short = {"standard": "std", "light": "lt", "quick": "q", "thorough": "thr", "adversarial": "adv"}
    suffix = "·".join(short.get(value, value) for value in (row.get("mode"), row.get("intensity")) if value)
    kind = sanitize_title(str(row.get("capability") or ""))[:64]
    shown = {"kind_text": (kind or "·") + ("(" + suffix + ")" if suffix else "") if kind or suffix else ""}
    profile = sanitize_title(str(row.get("profile") or ""))[:32]
    if profile and profile != "deep":
        shown["profile"] = profile
    if _percent(row.get("owner_ctx_pct")) != "?":
        shown["owner_ctx"] = _percent(row["owner_ctx_pct"])
    elif token_text(row.get("owner_ctx_tokens")):
        shown["owner_ctx"] = token_text(row["owner_ctx_tokens"])
    for key in ("age", "resume", "worker_unit"):
        if row.get(key):
            shown[key] = sanitize_title(str(row[key]))[:64]
    if type(row.get("legs")) is int and row["legs"] > 0:
        shown["legs"] = row["legs"]
    return shown


def route_workers(row):
    """Each live worker's stage, harness, model and context (Fleet's ``└ stage · n`` rows)."""
    from .session_line import _clean_model, _percent
    workers = []
    for item in row.get("worker_contexts") or ():
        if not isinstance(item, dict) or not item.get("stage"):
            continue
        worker = {"stage": sanitize_title(str(item["stage"]))[:64]}
        if item.get("harness"):
            worker["harness"] = sanitize_title(str(item["harness"]))[:32]
        if item.get("model"):
            worker["model"] = _clean_model(item["model"]) or sanitize_title(str(item["model"]))[:64]
        if _percent(item.get("ctx_pct")) != "?":
            worker["ctx"] = _percent(item["ctx_pct"])
        workers.append(worker)
    return {"workers": workers[:8]} if workers else {}


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
    mcp = values.get("mcp")
    if isinstance(mcp, dict) and mcp.get("total"):
        # OpenCode's hidden native sidebar listed these servers; keep the count.
        groups.append([("MCP ", "dim"), (f"{mcp.get('connected', 0)}/{mcp['total']}",
                                         "lvl_r" if mcp.get("failed") else "lvl_g")])
    parts = []
    for group in groups:
        if parts:parts.append((" · ", "dim"))
        parts.extend(group)
    return [parts] if parts else []


# No thread ID: a session is told apart by its number and title. The shell
# conceals Codex's own thread-id field instead (session_shell._codex_footer_plan).
SESSION_FIELDS = frozenset({"folder", "title", "branch", "model_quota"})


def session_status(values, columns):
    """The statusline items the panel paints besides its model row and summary.

    Folder, title, branch with its Git state and observation, per-model
    quotas, quota resets and running jobs, in the native statusline's order and
    glyphs. Context, 5h/7d, model, route and answer counts are already painted
    elsewhere in the panel; an item without a value is left out.
    """
    from .session_line import _remaining, SEPARATOR, _text
    from .explorer.grid import cell_width
    known = {**values, "jobs": None, "model_quotas": [(label, used) for label, used in values.get("model_quotas") or ()
                                                      if _percent(used) != "?"]}
    rows = status_lines(known, columns, wrap_long=True, fields=SESSION_FIELDS)
    if values.get("jobs"):
        # Running jobs join the last row when they fit, saving a row at 80.
        job = [(">_", "lvl_g"), (" running: ", "dim"), (_text(values["jobs"]), "loc_repo")]
        width = lambda parts: sum(cell_width(text) for text, _key in parts)
        if rows and width(rows[-1]) + cell_width(SEPARATOR) + width(job) <= columns:
            rows[-1] = rows[-1] + [(SEPARATOR, "dim")] + job
        else:
            rows.extend(status_lines({"jobs": values["jobs"]}, columns, wrap_long=True, fields=()))
    resets = [(str(label), _remaining(reset)) for label, _used, reset in values.get("quota_windows") or ()]
    resets = [(label, left) for label, left in resets if left]
    if resets:
        parts = [("↻ ", "dim")]
        for index, (label, left) in enumerate(resets):
            parts += [(" · " if index else "", "dim"), (label + " ", "dim"), (left, "dim")]
        rows.append([part for part in parts if part[0]])
    return rows


def session_brief(values):
    """Title, running jobs and branch as short items, most needed first.

    The panel packs these into whatever is left (a spare row or its bottom
    edge) when a question takes the rows the full session rows would use, so
    no observed value leaves the screen while the question is shown first.
    """
    from .session_line import line_segments, _text
    items = []
    title = line_segments(values, 10000, complete=True, fields={"title"})
    if title:
        items.append(title)
    if values.get("jobs"):
        items.append([(">_ ", "lvl_g"), (_text(values["jobs"]), "loc_repo")])
    if values.get("branch"):
        items.append([("⎇ ", "dim"), (_text(values["branch"]), "branch_s")])
    return items


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
        from .session_line import answer_wait_word
        return [(answer_wait_word() + " " + str(waiting), "g_blocked"), (" │ Ctrl+]", "dim")]
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
        if route.get("state") == "blocked":
            # The common reader's blocked route waits on a person: Fleet's ◑ and
            # its "answer needed" wording, not a failure.
            return [(_LIVE_GLYPH["blocked"] + " ", "g_blocked"), (ui_text("fleet.session_block.open"), "lvl_y"),
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


def route_row(row, now=None):
    """A route row's painted values (spec rules 5-7); an unreported value is left out."""
    import time
    from .session_line import (route_glyph, effort_label, gauge_parts, elapsed_text,
                               stage_glyph, token_text, _number)
    now = time.time() if now is None else now
    # The producer's Fleet marker; an older cache row has only its state.
    marker = row.get("marker")
    if marker not in ("working", "idle", "blocked", "dead", "done", "stale"):
        state = str(row.get("state") or "")
        marker = {"failed": "dead", "attention": "dead", "degraded": "dead"}.get(state, state)
        marker = marker if marker in ("working", "idle", "blocked", "dead", "done", "stale") else "idle"
    glyph, key = route_glyph(marker)
    shown = {"glyph": glyph, "glyph_key": key, "kind": marker}
    harness = sanitize_title(str(row.get("harness") or ""))[:32]
    if harness:
        shown.update(harness=harness, harness_key="hb_" + harness)
    model = row.get("model")
    if model:
        shown.update(model_text=_clean_model(model) or "", model_key=_model_key(model))
    effort = effort_label(row.get("effort"))
    if effort:
        shown.update(effort=effort, effort_key=_eff_key(row.get("effort"), False))
    percent = row.get("ctx_pct")
    if _percent(percent) != "?":
        shown.update(ctx=_percent(percent), ctx_key=_level(percent), gauge=[list(part) for part in gauge_parts(percent)],
                     gauge5=[list(part) for part in gauge_parts(percent, cells=5)])
    elif token_text(row.get("ctx_tokens")):
        # No window known: the exact token count, as Fleet writes it, never a made-up percent.
        tokens = token_text(row["ctx_tokens"])
        shown.update(ctx=tokens, ctx_key="dim", gauge=[[tokens, "dim"]], gauge5=[[tokens, "dim"]])
    minutes = None
    if _number(row.get("since")) is not None:
        minutes = max(0.0, (now - row["since"]) / 60)
    elif _number(row.get("elapsed_min")) is not None:
        minutes = row["elapsed_min"]
    if minutes is not None:
        shown["elapsed"] = elapsed_text(minutes)
    # The doer reports its open tool only while it works (Fleet's ``_route_doer``).
    tool = sanitize_title(str(row.get("tool") or ""))[:48]
    if tool:
        shown["tool"] = tool
    if type(row.get("agents")) is int and row["agents"] > 0:
        shown["agents"] = row["agents"]
    stages = [node for node in (row.get("stages") or ()) if isinstance(node, dict)][:16]
    if len(stages) > 1:
        flow = []
        for node in stages:
            name = sanitize_title(str(node.get("name") or ""))[:64]
            mark = stage_glyph(node.get("st"))
            flow.append(name + (" " + mark if mark else ""))
        shown["flow"] = " › ".join(flow)
        current = next((i for i, node in enumerate(stages) if stage_glyph(node.get("st")) in {"●", "✕"}), None)
        if current is not None:
            shown["step"] = (sanitize_title(str(stages[current].get("name") or ""))[:32] + " "
                             + stage_glyph(stages[current].get("st")) + " " + str(current + 1) + "/" + str(len(stages)))
    return shown


def _git_part(part):
    return part[1] == "dim" and str(part[0]).startswith(" (")


def session_head(values):
    """``[e9] hearting``: the session number and project (the panel's top row)."""
    from .session_line import line_segments
    return [part for part in line_segments(values, 10000, complete=True, fields={"folder"}) if not _git_part(part)]


def session_git(values):
    """``(main 🚧 3)``: the dim Git observation the panel paints on the row below."""
    from .session_line import line_segments
    return [(str(text).lstrip(), key) for text, key in line_segments(values, 10000, complete=True, fields={"folder"})
            if _git_part((text, key))]


def quota_row(values):
    """``5h 12%(↻ 2h) │ 7d 66%(↻ 5d)`` and per-model quotas; unreported values left out."""
    from .session_line import _remaining, _text
    parts = []
    windows = [window for window in values.get("quota_windows") or () if _percent(window[1]) != "?"]
    if not windows and _percent(values.get("weekly_pct")) != "?":
        windows = [("7d", values["weekly_pct"], None)]
    for label, used, reset in windows:
        if parts:
            parts.append((" │ ", "dim"))
        parts += [(_text(label) + " ", "dim"), (_percent(used), _level(used))]
        left = _remaining(reset)
        if left:
            parts.append(("(↻ " + left + ")", "dim"))
    for label, used in values.get("model_quotas") or ():
        if _percent(used) != "?":
            if parts:
                parts.append((" │ ", "dim"))
            parts += [(_text(label) + " ", _model_key(label)), (_percent(used), _level(used))]
    return parts


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
                stage = {"name": clean(node.get("name")), "state": state_label(node.get("st")),
                         "current": node.get("st") in {"active", "running", "working"},
                         "mark": stage_mark(node.get("st"))}
                stage.update(stage_doer(node))
                stages.append(stage)
        worker = row.get("worker") or []
        reason = "".join(str(part[0]) for part in worker if isinstance(part, (list, tuple)) and part)
        # The route's depth-1 owner, as Fleet's route row reads it. A one-shot owner runs its
        # node itself, so its body line already says ``owner …`` and no second line repeats it.
        owner = clean(row.get("owner"))
        owner_text = "owner · " + owner if owner else ""
        runs_itself = bool(worker) and isinstance(worker[0], (list, tuple)) and worker[0][:1] == ["owner"]
        visibility = row.get("visibility") if isinstance(row.get("visibility"), dict) else {}
        routes.append({**route_row(row), "id": clean(row.get("id")), "title": clean(row.get("title")),
                       "state": clean(row.get("state")), "state_label": state_label(row.get("state")), "stage": clean(row.get("worker_stage")),
                       "stages": stages, "stage_more": row.get("stage_more", 0),
                       "visibility": route_evidence(visibility),
                       "reason": clean(reason), "outcome": clean(row.get("outcome")),
                       "owner": owner_text, "owner_line": "" if runs_itself else owner_text,
                       **route_kind(row), **route_workers(row),
                       "result_at": row.get("result_at") if type(row.get("result_at")) in (int, float) else None})
    model = values.get("model")
    model_line = [(_clean_model(model) or ui_text("fleet.session_panel.checking"), _model_key(model))]
    effort = _effort_text(values.get("effort"))
    model_line += [(" · " + (effort or "?"), _eff_key(values.get("effort"), False) if effort else "dim")]
    labels = {name: ui_text("fleet.session_panel." + name) for name in (
        "settings", "settings_title", "checking", "work", "no_work", "routes", "no_routes",
        "results", "details", "back", "apply", "cancel", "defaults", "layout", "width",
        "narrow", "auxiliary", "height", "threshold", "side", "bottom", "compact", "summary",
        "detail", "working", "answer", "ready", "unknown", "saved", "save_failed",
        "position", "position_right", "position_left", "theme", "theme_terminal", "theme_light",
        "theme_mono", "shade", "shade_none", "shade_light", "shade_medium")}
    # Fixed words are English in every language (spec rule 3).
    labels.update(settings="Settings", routes="route", context="context", route_line="route line")
    from .session_line import effort_label, gauge_parts, answer_wait_word
    selected = [(_clean_model(model) or ui_text("fleet.session_panel.checking"), _model_key(model))]
    if effort_label(values.get("effort")):
        selected += [(" ", "dim"), (effort_label(values.get("effort")), _eff_key(values.get("effort"), False))]
    gauge = gauge_parts(values.get("ctx_pct"))
    count = values.get("answer_wait")
    # The producer's count is the session's whole current-route count; the panel
    # keeps at most eight rows, so the kept current rows are counted apart from it.
    current, total = len(routes), None
    from .session_line import ROUTE_LABEL
    match = ROUTE_LABEL.match(values.get("route_label") or "")
    if match:
        total = int(match.group(1))
        current = min(len(routes), total)
    mcp = values.get("mcp")
    # OpenCode's hidden native sidebar listed these servers; keep the count.
    mcp_parts = ([("MCP ", "dim"), (f"{mcp.get('connected', 0)}/{mcp['total']}",
                                    "lvl_r" if mcp.get("failed") else "lvl_g")]
                 if isinstance(mcp, dict) and mcp.get("total") else [])
    return {"head": session_head(values), "git": session_git(values), "selected": selected,
            "context": [("context ", "dim"), *gauge] if gauge else [],
            "wait": answer_wait_word() + " " + (str(count) if count is not None else "?")
                    if count is None or type(count) is int and count > 0 else "",
            "session_title": sanitize_title(str(values.get("title") or ""))[:320],
            "jobs": [(">_", "lvl_g"), (" running: ", "dim"), (sanitize_title(str(values["jobs"]))[:200], "loc_repo")]
                    if values.get("jobs") else [],
            "quota": quota_row(values), "route_count": current, "route_total": total, "mcp": mcp_parts,
            "model": model_line, "model_value": model, "effort_value": values.get("effort"),
            "warning": ui_text("fleet.context.compaction_imminent") if compaction_imminent(values.get("compaction_pressure")) else "",
            "routes": routes, "routes_known": raw is not None,
            "route_label": route_text(values.get("route_label") or ""),
            "title": routes[0]["title"] if routes else "", "labels": labels,
            "summary": compact_status(values, columns, native_owned),
            "session": session_status(values, max(20, columns - 4)),
            "brief": session_brief(values),
            "status": status_lines(values, max(20, columns - 4), wrap_long=True)}
