"""Question-first session block; values and authority keep their existing owners."""
from .session_line import route_text, status_lines, clip_segments, wrap_segments as wrap_parts
from .explorer.grid import cell_width
from .config import ui_text


def capacity(rows):
    return min(10, max(1, int(rows) - 6))


# Spec E9 rule 54: a wait the person cannot answer from here says what to do instead, by its
# reason (the selected request's support reason, else the board's own): another answer is on its
# way (`answer_sending`), a blocked worker's answer whose parent could not be confirmed
# (`answer_parent`), a request already gone (`answer_closed`); any other reason, named or not, is
# answered in that session's own window: below in this session's own panel (`answer_below`), in
# its window from Fleet or another session (`answer_there`).
_SENDING_REASONS = frozenset({"action-pending", "owner-correction-already-pending"})
# A registered question whose seat another session took (11-14: the same session in a new
# process keeps it) is closed for this window too.
_CLOSED_MARKS = ("no-longer-current", "not-current", "not-pending", "cancelled", "incarnation-changed")


def unanswerable_state(reason, *, own=True):
    reason = str(reason or "")
    if reason in _SENDING_REASONS:
        return "answer_sending"
    if reason.startswith("owner-correction-"):
        return "answer_parent"
    if any(mark in reason for mark in _CLOSED_MARKS):
        return "answer_closed"
    return "answer_below" if own else "answer_there"


def question_state(item, *, own=True):
    state = item.get("request_state")
    if state in {"submitted", "received", "applied", "expired", "cancelled", "unknown"}:
        return state
    return "open" if item.get("answerable") is True else unanswerable_state(item.get("reason"), own=own)


def question_presentations(items, columns, *, native_wait=None, own=True):
    """Private display-only rows consumed by the local editor, not input proof."""
    listed = [(item, question) for item in items for question in item.get("questions") or ()]
    width = max(1, columns - 4)
    for item in items:
        state = question_state(item, own=own)
        if state.startswith("answer_"):
            item["answer_state"] = state
        else:
            item.pop("answer_state", None)
    for index, (item, question) in enumerate(listed):
        state = question_state(item, own=own)
        key = "lvl_g" if state == "applied" else "g_blocked" if state == "open" else "dim"
        heading = [("" if native_wait is not None else "◑ ", key),
                   (ui_text("fleet.session_block.questions") + " " + str(len(listed)), "dim"),
                   (f" · {index+1}/{len(listed)} · ", "dim"),
                   (ui_text("fleet.session_block." + ("compact_" if columns < 64 else "") + state), key)]
        source = []
        choices = (["approve", "reject", "defer"] if item.get("answer_kind") == "decision"
                   else question.get("choices") or [])
        options = []
        for number, choice in enumerate(choices, 1):
            label = choice.get("label", choice.get("value", "?")) if isinstance(choice, dict) else choice
            prefix = str(number) + " "
            parts = [(str(label), "loc_repo")]
            if question.get("recommended") == number - 1:
                parts.append((" (" + ui_text("fleet.session_block.recommended") + ")", "dim"))
            # Two cells stay free before the number for the editor's mark column (spec E9 rule 53).
            for line_index, line in enumerate(wrap_parts(parts, max(1, width - cell_width(prefix) - 2))):
                options.append([(prefix if line_index == 0 else " " * cell_width(prefix), "dim"), *line])
        body = []
        for source_line in str(question.get("question") or "?").splitlines():
            if source_line.strip():
                body.extend(wrap_parts([(source_line, "loc_repo")], width))
        question["display"] = {
            "head": heading, "source": source,
            "body": body,
            # Not open: the state's whole words (rule 54), never the machine reason.
            "options": (options or [[(ui_text("fleet.session_block.text_hint"), "dim")]])
                       if state == "open" else [[(ui_text("fleet.session_block." + state), "dim")]],
        }
    return items


def block_sections(values, *, columns, route="", received=(), human="", work="", gpu=""):
    prefix = [] if values.get("harness") == "claude" else status_lines({**values, "route_label": ""}, columns - 4, wrap_long=columns < 64)
    if route and route != values.get("native_route"):
        prefix += wrap_parts([(route_text(route), "g_idle")], columns - 4)
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
            board = [view["head"], *view["body"], *view["options"], *view["source"]]
            break
    lines = prefix + (list(detail) if detail else board) + tail
    if focused:
        lines.append([("Ctrl+] ", "dim"), (ui_text("fleet.session_block.help"), "dim")])
    limit = capacity(rows)
    if len(lines) > limit:
        omitted = len(lines) - limit + 1
        lines = lines[:limit-1] + [[(f"+{omitted} · " + ui_text("fleet.session_block.more"), "dim")]]
    return [clip_segments(line, columns) for line in lines]
