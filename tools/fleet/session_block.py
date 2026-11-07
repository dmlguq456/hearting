"""Small, native-like session block over Fleet's existing display projections.

Only presentation lives here. Questions and answers retain their existing owners.
"""
from .session_line import line_segments, status_lines, clip_segments as _clip
from .explorer.grid import clip_cells, cell_width
from .config import ui_text


def capacity(rows):
    return min(10, max(4, int(rows) // 3), max(1, int(rows) - 4))


def block_lines(values, *, columns, rows, questions=(), route="", received=(),
                human="", work="", gpu="", detail=(), focused=False):
    """Question space wins; absent sections never grow an empty labeled row."""
    limit = capacity(rows)
    # Expanded rows already carry the actionable route and question counts.
    lines = [] if values.get("harness") == "claude" else status_lines({**values, "route_label": ""}, columns)
    # Preserve space for an actual question at small terminal heights.
    if questions:
        lines = lines[:max(1, limit - 2 - bool(route))]
    # These labels deliberately follow the status-line idiom, not a Fleet frame.
    if route:
        lines.append([("⏳ ", "g_idle"), (route.removeprefix("⏳ "), "g_idle"),
                      ("  ↵", "dim")])
    if questions:
        total = sum(max(1, len(item.get("questions") or ())) for item in questions)
        lines.append([("◑ ", "g_blocked"), (ui_text("fleet.session_block.questions") + " ", "dim"),
                      (str(total), "g_blocked"), ("  Ctrl+]", "dim")])
        for item in questions:
            for question in item.get("questions") or ({"question": item.get("reason") or "?"},):
                options = []
                for index, choice in enumerate(question.get("choices") or (), 1):
                    label = choice.get("label", choice.get("value", "?")) if isinstance(choice, dict) else choice
                    options.append(str(index) + " " + str(label) + (" ✓" if question.get("recommended") == index - 1 else ""))
                text = str(question.get("question") or "?")
                lines.append([("  " + text, "loc_repo"),
                              ("  " + " · ".join(options) if options else "  › text", "dim")])
                if len(lines) >= limit:
                    break
            if len(lines) >= limit:
                break
    for line in received:
        lines.append([("✉ ", "dim"), (str(line).removeprefix("✉ "), "loc_repo"), ("  r", "dim")])
    if work:
        lines.append([("◌ ", "dim"), (work, "loc_repo"), ("  w", "dim")])
    if gpu:
        lines.append([("⚙ ", "dim"), (gpu, "resource_active")])
    if human and human != "› ·":
        lines.append([(human, "dim")])
    if detail:
        # Focused actions and editors are visible at the expense of summaries.
        lines = lines[:max(1, limit - len(detail))] + list(detail)[-max(1, limit - 1):]
    if focused and len(lines) < limit:
        lines.append([("Ctrl+] ", "dim"), (ui_text("fleet.session_block.help"), "dim")])
    lines = lines[:limit]
    while len(lines) < min(4, limit):
        lines.append([])
    return [_clip(line, columns) for line in lines]
