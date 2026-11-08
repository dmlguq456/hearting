"""Question-first session block; values and authority keep their existing owners."""
from .session_line import status_lines, clip_segments
from .explorer.grid import cell_width
from .config import ui_text


def capacity(rows):
    return min(10, max(1, int(rows) - 6))


def wrap_parts(parts, columns):
    """Wrap semantic spans at cell boundaries, retaining their Fleet colors."""
    columns = max(1, columns)
    result, row, width = [], [], 0
    for text, key in parts:
        for char in str(text):
            if ord(char) < 32 or ord(char) == 127:
                char = " "
            size = cell_width(char)
            if width + size > columns and row:
                result.append(row); row=[]; width=0
            if row and row[-1][1] == key:
                row[-1] = (row[-1][0] + char, key)
            else:
                row.append((char, key))
            width += size
    if row:
        result.append(row)
    return result


def question_state(item):
    state = item.get("request_state")
    if state in {"submitted", "received", "applied", "expired", "cancelled", "unknown"}:
        return state
    return "open" if item.get("answerable") is True else "unavailable"


def question_presentations(items, columns, *, native_wait=None):
    """Private display-only rows consumed by the local editor, not input proof."""
    listed = [(item, question) for item in items for question in item.get("questions") or ()]
    width = max(1, columns - 4)
    for index, (item, question) in enumerate(listed):
        state = question_state(item)
        key = "lvl_g" if state == "applied" else "g_blocked" if state == "open" else "dim"
        heading = [("" if native_wait is not None else "◑ ", key),
                   (ui_text("fleet.session_block.questions") + " " + str(len(listed)), "dim"),
                   (f" · {index+1}/{len(listed)} · ", "dim"),
                   (ui_text("fleet.session_block." + state), key),
                   (" · " + ui_text("fleet.session_block.this_session") + " · " + str(item.get("age") or "?"), "dim")]
        choices = (["approve", "reject", "defer"] if item.get("answer_kind") == "decision"
                   else question.get("choices") or [])
        options = []
        for number, choice in enumerate(choices, 1):
            if options:
                options.append((" · ", "dim"))
            label = choice.get("label", choice.get("value", "?")) if isinstance(choice, dict) else choice
            options.append((str(number) + " " + str(label), "loc_repo"))
            if question.get("recommended") == number - 1:
                options.append((" (" + ui_text("fleet.session_block.recommended") + ")", "dim"))
        question["display"] = {
            "head": heading,
            "body": wrap_parts([(question.get("question") or "?", "loc_repo")], width),
            "options": wrap_parts(options or [(ui_text("fleet.session_block.text_hint"), "dim")], width)
                       if state == "open" else [[(str(item.get("reason") or ui_text("fleet.session_block." + state)), "dim")]],
        }
    return items


def block_sections(values, *, columns, route="", received=(), human="", work="", gpu=""):
    prefix = [] if values.get("harness") == "claude" else status_lines({**values, "route_label": ""}, columns - 4)
    if route and route != values.get("native_route"):
        prefix += wrap_parts([("⏳ ", "g_idle"), (route.removeprefix("⏳ "), "g_idle")], columns - 4)
    tail = []
    if received:
        tail.append([("✉ ", "dim"), (str(received[-1]).removeprefix("✉ "), "loc_repo")])
    if work:
        tail.append([("◌ ", "dim"), (work, "loc_repo")])
    if gpu:
        tail.append([("⚙ ", "dim"), (gpu, "resource_active")])
    if human and human != "› ·":
        tail.append([(human, "dim")])
    return prefix, tail


def block_lines(values, *, columns, rows, questions=(), route="", received=(),
                human="", work="", gpu="", detail=(), focused=False):
    """Static first-question paint; the editor uses the same prepared rows."""
    prefix, tail = block_sections(values, columns=columns, route=route, received=received,
                                  human=human, work=work, gpu=gpu)
    question_presentations(questions, columns, native_wait=values.get("native_wait"))
    board = []
    for item in questions:
        if item.get("questions"):
            view = item["questions"][0]["display"]
            board = [view["head"], *view["body"], *view["options"]]
            break
    lines = prefix + (list(detail) if detail else board) + tail
    if focused:
        lines.append([("Ctrl+] ", "dim"), (ui_text("fleet.session_block.help"), "dim")])
    limit = capacity(rows)
    if len(lines) > limit:
        omitted = len(lines) - limit + 1
        lines = lines[:limit-1] + [[(f"+{omitted} · " + ui_text("fleet.session_block.more"), "dim")]]
    return [clip_segments(line, columns) for line in lines]
