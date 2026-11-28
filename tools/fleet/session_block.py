"""Question-first session block; values and authority keep their existing owners."""
import re

from .session_line import route_text, status_lines, clip_segments, wrap_segments as wrap_parts, wrap_words
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
# The answer record's states once the person's answer was sent (rule 5, 11-14): the panel's one
# dim line, in no box number or pane count.
_SENT_STATES = frozenset({"submitted", "received", "applied"})


# What an answer still on its way waits for, in the person's words (``session_shell.answer_delivery_wait``):
# an older shell takes it once its window is reopened or typed in (the window's own `새 판 대기`), a Claude
# window outside the shell at the next prompt typed there, a window whose screen is too large once it is smaller.
_ANSWER_WAIT_WORDS = {"old-shell": ("session_shell.upgrade_pending", "fleet.session_block.wait_old_shell"),
                      "no-shell": ("fleet.session_block.wait_no_shell",),
                      # The window's screen is too large to read: the words its waiting messages carry.
                      "screen-large": ("peer.delivery.hold_screen_large",),
                      "old-shell-large": ("peer.delivery.hold_old_shell_large",)}


# What a drawn value may not carry, whatever draws it (Fleet's grid drops these; the panel may not):
# C0/C1 controls (ESC, NUL), DEL and the bidirectional overrides and isolates.
_UNDRAWABLE = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


# A choice's words under its name (11-27, design 가): drawn dim (`style.KEY_ALIASES`); the panel's editor
# knows them by this key's role (`choice-detail`) and keeps them dim on the chosen choice's rows too.
CHOICE_DETAIL = "choice_detail"


def _drawable(value):
    return _UNDRAWABLE.sub("", " ".join(str(value).split()))


def sent_values(sent, questions=()):
    """What was sent, as the answer record says it: choice labels, free text, a decision; secrets
    hidden (``secret``, ``is_secret`` or ``isSecret`` on the question), controls dropped."""
    if isinstance(sent, (list, tuple)):
        return [value for value in map(_drawable, sent) if value.strip()]
    if not isinstance(sent, dict):
        return []
    if isinstance(sent.get("decision"), str):
        return [_drawable(sent["decision"])]
    by_id = {str(q.get("id")): q for q in questions or () if isinstance(q, dict)}
    out = []
    for qid, listed in (sent.get("answers") if isinstance(sent.get("answers"), dict) else {}).items():
        question = by_id.get(str(qid)) or {}
        for value in listed if isinstance(listed, list) else ():
            if question.get("secret") or question.get("is_secret") or question.get("isSecret"):
                out.append("•••")
                continue
            label = next((choice.get("label") for choice in question.get("choices") or ()
                          if isinstance(choice, dict) and choice.get("value") == value and choice.get("label")), None)
            shown = _drawable(label or value)
            if shown.strip():
                out.append(shown)
    return out[:4]


def sent_answer_line(sent=(), *, wait="", mark=True, questions=()):
    """The one line of an answer the person sent that its session has not taken yet (rule 5).

    ``✓ answer sent · <what was sent> · awaiting delivery``, then, when it waits on the person, why
    and what takes it (``wait``). Fleet's QUESTIONS row and the shell panel's sent line draw these
    same parts; it is never a question to answer again and is in no count.
    """
    word = ui_text("fleet.session_block.sent")
    parts = [(word if mark else word.removeprefix("✓").strip(), "lvl_g")]
    said = " ".join(", ".join(sent_values(sent, questions)).split())
    if said:
        parts += [(" · ", "dim"), (said, "loc_repo")]
    parts += [(" · ", "dim"), (ui_text("fleet.session_block.delivery_waiting"), "dim")]
    for key in _ANSWER_WAIT_WORDS.get(wait, ()):
        parts += [(" · ", "dim"), (ui_text(key), "dim")]
    return parts


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


def question_presentations(items, columns, *, native_wait=None, own=True, brief_columns=None):
    """Private display-only rows consumed by the local editor, not input proof.

    ``columns`` is the width the editor draws in (the question popup, 11-15); ``brief_columns``,
    when given (the shell panel's), adds each waiting question's first line (``brief_line``) for
    its one row in the panel (11-28; the panel cuts it at its own width).
    """
    listed = [(item, question) for item in items for question in item.get("questions") or ()]
    # Rule 5 (11-14): a sent answer is one dim line, never a box; `N · i/N` numbers the boxes.
    boxes = sum(1 for item, _question in listed if question_state(item, own=own) not in _SENT_STATES)
    number = 0
    width = max(1, columns - 4)
    for item in items:
        state = question_state(item, own=own)
        if state.startswith("answer_"):
            item["answer_state"] = state
        else:
            item.pop("answer_state", None)
        # The sent line's parts for the panel, from the same function Fleet's row draws (rule 5).
        if state == "submitted":
            item["sent_line"] = sent_answer_line(item.get("sent") or (), wait=item.get("answer_wait") or "",
                                                 questions=item.get("questions") or ())
        else:
            item.pop("sent_line", None)
    for item, question in listed:
        state = question_state(item, own=own)
        key = "lvl_g" if state == "applied" else "g_blocked" if state == "open" else "dim"
        if state in _SENT_STATES:
            count = [(ui_text("fleet.session_block.questions") + " · ", "dim")]
        else:
            number += 1
            # The count leads its name tag, as `4 ROUTE` and `4 LAB` do (11-15 design F 12).
            count = [(str(boxes) + " " + ui_text("fleet.session_block.questions"), "dim"),
                     (f" · {number}/{boxes} · ", "dim")]
        heading = [("" if native_wait is not None else "◑ ", key), *count,
                   (ui_text("fleet.session_block." + ("compact_" if columns < 64 else "") + state), key)]
        source = []
        decision = item.get("answer_kind") == "decision"
        choices = ["approve", "reject", "defer"] if decision else question.get("choices") or []
        described = () if decision else question.get("descriptions") or ()
        options = []
        # The choice's own number; `number` stays the box's `i` of `i/N`.
        for choice_number, choice in enumerate(choices, 1):
            label = choice.get("label", choice.get("value", "?")) if isinstance(choice, dict) else choice
            # 11-17: the number in the question's rose, two cells before the words (the popup bolds it).
            prefix = str(choice_number) + "  "
            parts = [(str(label), "loc_repo")]
            if question.get("recommended") == choice_number - 1:
                parts.append((" (" + ui_text("fleet.session_block.recommended") + ")", "dim"))
            # Two cells stay free before the number for the editor's mark column (spec E9 rule 53).
            room = max(1, width - cell_width(prefix) - 2)
            for line_index, line in enumerate(wrap_words(parts, room)):
                options.append([(prefix, "g_blocked") if line_index == 0 else (" " * cell_width(prefix), "dim"), *line])
            # 11-27 (design 가): the choice's words on the rows under its name, from its name's first cell,
            # dim (`CHOICE_DETAIL`); they belong to the choice as its name's own rows do.
            words = str(described[choice_number - 1] or "") if choice_number <= len(described) else ""
            for said in filter(str.strip, map(_drawable, words.splitlines())):
                for line in wrap_words([(said, CHOICE_DETAIL)], room):
                    options.append([(" " * cell_width(prefix), "dim"), *line])
        body = []
        # 11-15: a markdown table in the question's text reads as aligned columns (the documents' one
        # table rule, ``transcript.table_parts``); every other line wraps at its spaces (``wrap_words``).
        from .explorer.transcript import table_parts
        for source_line in table_parts(str(question.get("question") or "?").splitlines(), width):
            if isinstance(source_line, list):
                body.extend(source_line)
            elif source_line.strip():
                body.extend(wrap_words([(source_line, "loc_repo")], width))
        question["display"] = {
            "head": heading, "source": source,
            "body": body,
            # Not open: the state's whole words (rule 54), never the machine reason.
            "options": (options or [[(ui_text("fleet.session_block.text_hint"), "dim")]])
                       if state == "open" else [[(ui_text("fleet.session_block." + state), "dim")]],
        }
        if brief_columns is not None and state not in _SENT_STATES:
            question["display"]["brief_line"] = _brief_line(question)
    return items


def _brief_line(question):
    """The panel's row of one waiting question (11-28, the person: "선지는 미리 보기를 날리고"): its
    first line that draws (fix 1: a line of control characters alone is blank), after the panel's `◑ 답 대기 · `;
    its choices are the popup's alone."""
    first = next(filter(str.strip, map(_drawable, str(question.get("question") or "?").splitlines())), "?")
    return [(first, "loc_repo")]


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
