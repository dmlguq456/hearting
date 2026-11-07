"""Compact session line sharing Fleet's values, glyphs and semantic style keys."""
from __future__ import annotations
import math
from .explorer.grid import cell_width, clip_cells
from .model import project_of
from .render_cells import _clean_model, _effort_text, _EFF_SHORT, _glyph, _model_key, _half_up, _LIVE_GLYPH, _eff_key
from .session_handle import sanitize_title

SEPARATOR = " · "


def _text(value, fallback="?"):
    return sanitize_title(value) or fallback


def _percent(value):
    return (str(_half_up(value)) + "%" if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else "?")


def line_segments(values, columns):
    """Shorten words before losing a known count; never invent an unknown value.

    Input is the same observed Session fields plus the shared question/route
    projection and the account's seven-day window, not harness statusline input.
    """
    columns = max(1, int(columns))
    harness = values.get("harness")
    glyph, state_key = _glyph(values.get("state", "unknown"))
    tag = _text(values.get("tag"), "?").strip("[]")
    prefix = [("[" + tag + "] ", "tag"), (glyph + " ", state_key)]
    effort = _effort_text(values.get("effort"))
    effort = _EFF_SHORT.get(effort, effort)
    suffix = [("·", "dim"), (effort, _eff_key(values.get("effort"), False))] if effort else []
    groups = []

    def words(name, text, key, maximum, priority, before=(), after=(), minimum=1):
        text = _text(text)
        groups.append({"name": name, "text": text, "key": key, "budget": min(maximum, cell_width(text)),
                       "minimum": min(minimum, cell_width(text)), "priority": priority,
                       "before": list(before), "after": list(after)})

    def fixed(name, text, key):
        words(name, text, key, cell_width(text), 99, minimum=cell_width(text))

    words("folder", project_of(values.get("cwd")) if values.get("cwd") else None, "loc_repo", 24, 2)
    words("title", values.get("title"), "hb_" + str(harness), 40, 0, prefix)
    words("branch", values.get("branch"), "branch_s", 20, 1)
    fixed("context", "CTX " + _percent(values.get("ctx_pct")), "dim")
    words("model", _clean_model(values.get("model")), _model_key(values.get("model")), 24, 4, after=suffix)
    fixed("weekly", "7d " + _percent(values.get("weekly_pct")), "dim")
    route = _text(values.get("route_label"), "")
    if route:
        words("route", route, "g_idle", 256, 3, minimum=cell_width(route.split(SEPARATOR, 1)[0]))
    count = values.get("answer_wait")
    if count is None or isinstance(count, int) and not isinstance(count, bool) and count > 0:
        fixed("wait", _LIVE_GLYPH["blocked"] + " " + (str(count) if count is not None else "?"), "g_blocked")

    separator = SEPARATOR

    def render():
        output = []
        for index, group in enumerate(groups):
            if index:
                output.append((separator, "dim"))
            output.extend(group["before"])
            output.append((clip_cells(group["text"], group["budget"]), group["key"]))
            output.extend(group["after"])
        return output

    def size():
        return sum(cell_width(text) for text, _key in render())

    for group in sorted(groups, key=lambda group: group["priority"]):
        extra = max(0, size() - columns)
        group["budget"] -= min(extra, group["budget"] - group["minimum"])
    if size() > columns:
        separator = " "
    for name in ("title", "branch", "folder", "model"):
        if size() <= columns:
            break
        groups[:] = [group for group in groups if group["name"] != name]
    output = render()
    if size() > columns:
        # At exceptionally small sizes the native terminal still gets its full
        # viewport. Preserve the two action counts before optional telemetry.
        groups[:] = [group for group in groups if group["name"] in {"route", "wait"}]
        output = render()
    return output
