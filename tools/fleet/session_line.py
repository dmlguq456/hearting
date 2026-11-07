"""Compact session line sharing Fleet's values, glyphs and semantic style keys."""
from __future__ import annotations
import math
import time
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


def _level(value):
    return "lvl_r" if type(value) in (int, float) and value >= 80 else (
        "lvl_y" if type(value) in (int, float) and value >= 50 else "lvl_g")


def _remaining(reset):
    if type(reset) not in (int, float) or not math.isfinite(reset):
        return ""
    seconds = int(reset - time.time())
    if seconds <= 0:
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d{hours}h" if days else (f"{hours}h{minutes:02d}m" if hours else f"{minutes}m")


def line_segments(values, columns):
    """Shorten words before losing a known count; never invent an unknown value.

    Input is the same observed Session fields plus the shared question/route
    projection and the account's seven-day window. Exact native display telemetry
    may supply the session values; it never establishes input readiness.
    """
    columns = max(1, int(columns))
    harness = values.get("harness")
    glyph, state_key = _glyph(values.get("state", "unknown"))
    tag = _text(values.get("tag"), "?").strip("[]")
    prefix = [("[" + tag + "] ", "tag"), (glyph + " ", state_key)]
    effort = _effort_text(values.get("effort"))
    suffix = [(" ", "dim"), (effort, _eff_key(values.get("effort"), False))] if effort else []
    groups = []

    def words(name, text, key, maximum, priority, before=(), after=(), minimum=1):
        text = _text(text)
        groups.append({"name": name, "text": text, "key": key, "budget": min(maximum, cell_width(text)),
                       "minimum": min(minimum, cell_width(text)), "priority": priority,
                       "before": list(before), "after": list(after)})

    def fixed(name, text, key):
        words(name, text, key, cell_width(text), 99, minimum=cell_width(text))

    words("folder", project_of(values.get("cwd")) if values.get("cwd") else None, "loc_repo", 24, 2,
          before=[("📁 ", "dim")])
    words("title", values.get("title"), "hb_" + str(harness), 40, 0, prefix)
    git = values.get("git") or {}
    branch_after = []
    if git and git.get("repo") != 0:
        operation = git.get("operation")
        flag = "⚠️" + operation if operation in {"merge", "rebase", "cherry-pick"} else (
            "💀merged" if operation == "none" and (git.get("merged") == 1 or git.get("branch_done") == 1) else "")
        if operation is None or operation == "unknown":
            flag = "⚠️?"
        if flag:
            branch_after.append((" " + flag, "lvl_r"))
        for key, glyph in (("ahead", "↑"), ("behind", "↓")):
            if type(git.get(key)) is int and git[key] > 0:
                branch_after.append((" " + glyph + str(git[key]), "branch_s"))
        wt = git.get("extra_worktrees")
        if wt is None:
            branch_after.append((" 🚧 ?", "dim"))
        elif wt > 0:
            branch_after.append((" 🚧 " + str(wt), "branch_s"))
        branch_after.append((" " + str(git.get("observation") or "unknown"), "dim"))
    if git:
        branch_after.append((SEPARATOR + str(git.get("observed_label") or "?"), "dim"))
    words("branch", values.get("branch"), "branch_s", 24, 1,
          before=[("⎇ ", "dim")], after=branch_after)
    percent = values.get("ctx_pct")
    level = _level(percent)
    gauge = []
    if _percent(percent) != "?":
        filled = max(0, min(10, _half_up(percent / 10)))
        gauge = [("━" * filled, level), ("─" * (10 - filled), "dim"), (" ", "dim")]
    words("context", _percent(percent), level if _percent(percent) != "?" else "dim",
          cell_width(_percent(percent)), 99, before=[("context ", "dim"), *gauge], minimum=cell_width(_percent(percent)))
    words("model", _clean_model(values.get("model")), _model_key(values.get("model")), 24, 4, after=suffix)
    for label, used, reset in values.get("quota_windows") or [("7d", values.get("weekly_pct"), None)]:
        remaining = _remaining(reset)
        words("quota", _percent(used), _level(used) if used is not None else "dim", 8, 99,
              before=[(_text(label) + " ", "dim")],
              after=[("(↻ " + remaining + ")", "dim")] if remaining else [], minimum=8)
    for label, used in values.get("model_quotas") or ():
        words("quota", _percent(used), _level(used), 8, 99,
              before=[(_text(label) + " ", _model_key(label))], minimum=8)
    route = _text(values.get("route_label"), "")
    if route:
        count = route.split(SEPARATOR, 1)[0]
        words("route", route, "g_idle", 256, 3,
              minimum=cell_width(count) + (1 if count != route else 0))
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


def status_lines(values, columns):
    """Wrap complete native fields in expanded mode; compact mode may shorten."""
    parts = line_segments(values, 10000)
    groups = [[]]
    for part in parts:
        if part == (SEPARATOR, "dim"):
            groups.append([])
        else:
            groups[-1].append(part)
    result, line, width = [], [], 0
    for group in groups:
        size = sum(cell_width(text) for text, _ in group)
        if line and width + cell_width(SEPARATOR) + size > columns:
            result.append(line);line=[];width=0
        if line:
            line.append((SEPARATOR, "dim"));width+=cell_width(SEPARATOR)
        if size > columns:
            # A single long name cannot consume rows intended for questions.
            clipped, left = [], columns
            for text, key in group:
                text = clip_cells(text, max(0, left))
                clipped.append((text, key));left -= cell_width(text)
            group = clipped
            size = sum(cell_width(text) for text, _ in group)
        line.extend(group);width+=size
    if line:
        result.append(line)
    if values.get("jobs"):
        result.append([(">_", "lvl_g"), (" running: ", "dim"), (_text(values["jobs"]), "loc_repo")])
    return result
