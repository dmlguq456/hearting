"""Fleet render: dispatch and session row text, frame lines, and diagnostics rows.

Split out of `render.py`, which re-exports every name defined here.
"""
import math
import os
import re
import time
from .model import fmt_min
from .config import ui_text
from .work_titles import resource_name as _resource_name

from .render_cells import (
    _BAR_EMPTY, _BAR_FULL, _clip_w, _cw, _dw, _HW, _IO_UNSET, _JOB_LIVE_RANK, _LIVE_RANK,
    _LOOPS_KEYS, _pad, _PIPE_STAGES, _route_current_index, _stage_role_label,
)

from .render_stages import (
    _compact_dispatch_name, _is_owner_mode_row, _is_plugin_agent, _route_rides_the_rail,
)


# Card inset: the left edge reserves the under-id relation column;
# the existing right-edge budget stays independent so only the interior shrinks.
_CARD_INSET = 1               # worker content placement stays unchanged
_STEWARD_LINE_COL = 5         # first character inside the MAIN [id] chip
_SESSION_DETAIL_COL = _STEWARD_LINE_COL + 3  # two blank cells after the connector
_OWNER_CARD_INSET = _SESSION_DETAIL_COL - 2  # box left edge at the shared detail column


def _dispatch_prefix(j, orphan=False, in_card=False, card_rail_col=None):
    # F-64c (v49, user 2026-08-05 연쇄 "depth=1을 조금 앞당겨서" → "세로선은 좀 더 들여쓰게"
    # → "아주 조금만 더 + depth=2의 들여쓰기를 완화"): dispatch-depth-1 sheds its ↳ arrow and sits at
    # a 4-cell inset — the capsule rail (assembler post-pass at `_RAIL_COL`) is what marks
    # the unit. depth≥2 keeps the ↳ spawn arrow (user 2026-07-16: indent-only dispatch-depth-2 rows
    # lost their arrow), TWO cells deeper per level past the dispatch-depth-1 seat — the rail now
    # carries the unit boundary, so the ladder no longer needs the 3-cell step. The harness
    # field absorbs the prefix (_HMW - len: dispatch-depth-1 40-4=36, dispatch-depth-2 40-8=32 ≥ the 26-cell
    # worst-case live label).
    #
    # Project-level orphans (parent dead/off-screen, surfaced as a project fallback) intentionally
    # drop the ↳ tree arrow — a parent-child arrow with no on-screen parent misleads readers into
    # attaching the orphan to whatever live session row happens to sit above it. Replace with a
    # same-width flat `··` "no-hierarchy" mark so the column stays aligned without implying nesting.
    #
    # Depth stays positional (indent + ↳ + rail). An explicit `d1`/`d2` text token was
    # tried on 2026-08-13 and reverted the same day: the user never asked for it (the
    # sourcing working-memory note carried no requester) and found it noisy. Depth
    # readability improvements, if any, go through explicit user direction.
    depth = max(1, min(3, int(getattr(j, "depth", 1) or 1)))
    if in_card and not orphan and card_rail_col is not None:
        return " " * (card_rail_col + 2)
    # A boxed row carries `_CARD_INSET` extra columns so the frame drawn over this
    # prefix lands one cell inside a plain group row. Orphans are never boxed
    # and keep the original 4-cell seat, which is what keeps their `··` mark aligned
    # with the session column.
    owner = _route_rides_the_rail(j, None, in_card)
    boxed = " " * (4 + (_OWNER_CARD_INSET if owner and in_card else _CARD_INSET))
    if in_card and not orphan:
        # F-66: the box already carries hierarchy. Every row inside it therefore
        # starts at the owner's content column; depth must not add a second ladder.
        return boxed
    if depth == 1:
        return "··  " if orphan else boxed
    marker = "··" if orphan else "  "
    return "    " + "  " * (depth - 1) + marker


# F-66 evolves F-64c/F-63's left rail into a complete dispatch box in every layout.
# The owner keeps its `╭─` cap; every interior row has both vertical edges; the close
# spans the card width. The whole frame shares the owner's breadcrumb tint while live
# and stays dim otherwise. Orphans remain outside this structure and retain `··`.
_RAIL_TOP = "╭─"
_RAIL_MID = "│ "
_RAIL_BOT = "╰"
_RAIL_TOP_RIGHT = "╮"
_RAIL_RIGHT = "│"
_RAIL_BOT_RIGHT = "╯"
_RAIL_DIV_LEFT = "├"     # header divider tees — descendants present OR a route breadcrumb
                          # needs a place to live (F-81)
_RAIL_DIV_RIGHT = "┤"
_RAIL_SOLO = "❙"
_RAIL_CHARS = (_RAIL_TOP, _RAIL_MID, _RAIL_MID.strip(), _RAIL_BOT, _RAIL_SOLO)
_RAIL_COL = 2 + _OWNER_CARD_INSET  # independent of the unchanged right-edge budget
_WORKER_RAIL_COL = 2 + _CARD_INSET
_CARD_EDGE_GAP = 2 + _CARD_INSET  # cells the box border keeps clear of the card's right edge


def _depth1_rail_color_index(key, stage, route_seq):
    """F-64c: the rail wears the SAME hue as the conductor breadcrumb's current token —
    route rows share `_route_current_index`, legacy rows the `_PIPE_STAGES` position, and
    everything else (`preparing…`/queued/raw stage) lands on the breadcrumb's stg0."""
    if route_seq is not None and route_seq:
        return _route_current_index(route_seq) % 5
    seq = _PIPE_STAGES.get(key)
    if seq and stage in seq:
        return seq.index(stage) % 5
    return 0


def _overwrite_rail_cell(segs, col, char, key):
    """Overwrite ONE display cell of a row with the rail mark, splitting whatever seg
    covers it. Fail closed: if the target cell holds real content (not a space or the
    dispatch-depth-1 ↳ the rail replaces), return the row untouched."""
    out, pos, replaced = [], 0, False
    for text, k in segs:
        width = _dw(text) if text else 0
        if replaced or pos + width <= col:
            out.append((text, k)); pos += width; continue
        acc, i = 0, 0
        while i < len(text) and pos + acc + _cw(text[i]) <= col:
            acc += _cw(text[i]); i += 1
        if i >= len(text) or text[i] not in (" ", "↳"):
            return segs
        if text[:i]:
            out.append((text[:i], k))
        out.append((char, key))
        if text[i + 1:]:
            out.append((text[i + 1:], k))
        pos += width
        replaced = True
    return out if replaced else segs


def _overwrite_rail_text(segs, col, mark, key):
    """Paint a multi-cell capsule edge over whitespace at ``col``."""
    out, pos = [], 0
    mark_width = _dw(mark)
    for seg_i, (text, old_key) in enumerate(segs):
        width = _dw(text) if text else 0
        if pos <= col and col + mark_width <= pos + width:
            left = col - pos
            right = left + mark_width
            if text[left:right].strip():
                return segs
            if text[:left]:
                out.append((text[:left], old_key))
            out.append((mark, key))
            if text[right:]:
                out.append((text[right:], old_key))
            out.extend(segs[seg_i + 1:])
            return out
        out.append((text, old_key))
        pos += width
    return segs


def _clip_segs(segs, max_width):
    """Display-width clip that preserves segment color boundaries."""
    out, used = [], 0
    for text, key in segs:
        if used >= max_width:
            break
        piece = _clip_w(text, max_width - used, ellipsis="")
        if piece:
            out.append((piece, key))
            used += _dw(piece)
        if _dw(piece) < _dw(text):
            break
    return out, used


def _reserve_age_tail(segs, max_width):
    """Clip styled prose before its final observed-age suffix."""
    if not segs or max_width is None:
        return segs
    text, key = segs[-1]
    match = re.search(r"(?: · |  )(?:\d+[mhd] ago|age unknown)$", text)
    if not match:
        return segs
    suffix = match.group(0)
    body = segs[:-1] + [(text[:match.start()], key)]
    room = max_width - _dw(suffix)
    if room < 0:
        age = suffix.lstrip(" ·")
        return [(age, key)] if _dw(age) <= max_width else []
    out, _ = _clip_segs(body, room)
    return out + [(suffix, key)]


def _reserve_frame_prefix(segs, rail_col, elapsed_only=False):
    """Make room for the border within the existing leading slot, not the model/name."""
    boundary = 4 + _HW
    width = 0
    for value, style in segs:
        if isinstance(style, str) and style.startswith(("nm_", "nmd_")):
            boundary = width
            break
        if value and all(ch in (_BAR_FULL, _BAR_EMPTY) for ch in value):
            boundary = width  # context word/padding ends at its own gauge anchor
            break
        width += _dw(value)
    end, width = 0, 0
    while end < len(segs) and width < boundary:
        width += _dw(segs[end][0])
        end += 1
    if width != boundary:
        return segs
    prefix = segs[:end]
    content = "".join(value for value, _ in prefix).strip()
    if content and re.fullmatch(r"[0-9dhms —-]+", content):
        lead = rail_col + 3
        return [(" " * lead, None), (_pad(content, boundary - lead), "dim")] + segs[end:]
    if elapsed_only:
        return segs
    chars = [(ch, None if ch == " " else style) for value, style in prefix for ch in value]
    # Border positions are display cells. A Korean telemetry label may occupy
    # two cells per character; retain whole characters while moving its padding.
    starts, cell = {}, 0
    for index, (ch, _style) in enumerate(chars):
        starts[cell] = index
        cell += _cw(ch)
    starts[cell] = len(chars)
    if rail_col not in starts:
        return segs
    rail_index = starts[rail_col]
    rail_end = next((index for col, index in starts.items() if col >= rail_col + 2), len(chars))
    occupied = any(ch != " " for ch, _ in chars[rail_index:rail_end])
    shift = 2 if occupied else 0
    spares = []
    for index in range(len(chars) - 1, rail_end - 1, -1):
        if chars[index][0] != " ":
            break
        spares.append(index)
    if len(spares) < shift:
        return segs
    for index in spares[:shift]:
        chars.pop(index)
    chars[rail_index:rail_index] = [(" ", None)] * shift
    rebuilt = []
    for char, style in chars:
        if rebuilt and rebuilt[-1][1] == style:
            rebuilt[-1] = (rebuilt[-1][0] + char, style)
        else:
            rebuilt.append((char, style))
    return rebuilt + segs[end:]


def _frame_dispatch_line(segs, box_width, edge, key, run_key=None, rail_col=None,
                         owner_identity=False):
    """Add one complete F-66 box row, resolving right flush within the frame.

    F-68 grain contract (user 2026-08-14): horizontal runs — the owner-row gap
    fill and the bottom rule — wear the STEADY `run_key` hue while corners and
    verticals wear `key` (the blinking rail hue), so a working card pulses at
    its edges without strobing a full-width bright bar."""
    rail_col = _RAIL_COL if rail_col is None else rail_col
    mark = _RAIL_TOP if edge == "top" else _RAIL_MID
    segs = _reserve_frame_prefix(segs, rail_col, elapsed_only=True)
    left = _overwrite_rail_text(segs, rail_col, mark, key)
    if left is segs:
        left = _overwrite_rail_text(_reserve_frame_prefix(segs, rail_col), rail_col, mark, key)
    if edge == "top":
        # F-69 (user): the owner row IS the box's top edge — its interior padding
        # runs (harness -> session -> stage) become rule, so the line reads as one
        # continuous border instead of a row that happens to sit under a corner.
        # Only genuine layout padding is converted: a run of 3+ spaces owned by no
        # segment key, kept one cell clear on each side so text never touches it.
        merged = []
        for text, color in left:
            if (color is None and text.strip() == "" and text != _CARD_TAG
                    and merged and merged[-1][1] is None
                    and merged[-1][0].strip() == "" and merged[-1][0] != _CARD_TAG):
                merged[-1] = (merged[-1][0] + text, None)   # join adjacent padding runs
            else:
                merged.append((text, color))
        filled = []
        past_corner = False
        for text, color in merged:
            if "╭" in text and color == key:
                past_corner = True
            if (past_corner and color is None and len(text) >= 5 and text.strip() == ""
                    and text != _CARD_TAG):
                filled.append((" ", None))
                filled.append(("─" * (len(text) - 2), run_key or key))
                filled.append((" ", None))
            else:
                filled.append((text, color))
        left = filled
    right = []
    for i, (text, color) in enumerate(left):
        if text == _RFLUSH:
            right = left[i + 1:]
            left = left[:i]
            break
    tag = []
    for i, (text, _color) in enumerate(left):
        if text == _CARD_TAG:
            tag = left[i + 1:]
            left = left[:i]
            break
    tag_w = (sum(_dw(text) for text, _color in tag) + 2) if tag else 0
    # F-68c (user "우측엔 그냥 너무 딱 붙어있잖아"): the border keeps a two-cell inner
    # margin on the right, mirroring the rail-side gap, so nothing — content or
    # the inline tag — ever touches the frame.
    margin = 2 if tag else 1
    if owner_identity and not tag:
        position = 0
        for value, style in left:
            if style == "name_dim" and position + _dw(value) == box_width - 1:
                # At 60 cells the complete capability may consume the last
                # interior cell. Keep that whole identity, folding its dial.
                margin = 0
                break
            position += _dw(value)
    right_w = sum(_dw(text) for text, _color in right)
    # Symmetric breathing: content stops two cells short of the border, matching
    # the rail-side inner margin (user: "우측엔 너무 딱 붙어"). The in-card tail tag
    # (elapsed) is protected: the CONTENT ellipsizes first, the tag never drops.
    # Best-effort margin: the wide-row ledger already reserves the two-cell
    # breathing room; a tight narrow row may use the full interior rather than
    # losing content it historically displayed.
    left_limit = max(0, box_width - 1 - right_w - tag_w - margin)
    kept_w_before = sum(_dw(text) for text, _color in left)
    full = list(left)
    left, left_w = _clip_segs(left, left_limit)
    if left_w < kept_w_before:
        # make the cut visible — unless the fold already ends in its own ellipsis.
        # The marker rides INSIDE the limit: re-clip one cell shorter first.
        tail_txt = "".join(t for t, _k in left[-2:])
        whole_label = bool(left and left[-1][1] == "name_dim" and left[-1] in full)
        if not tail_txt.rstrip().endswith("…") and not whole_label:
            left, left_w = _clip_segs(full, max(0, left_limit - 1))
            left.append(("…", "dim"))
            left_w += 1
    if tag:
        left.append(("  ", None))
        left.extend(tag)
        left.append((" " * margin, None))
        left_w += tag_w + margin
    pad = max(0, box_width - 1 - left_w - right_w)
    if pad:
        if edge == "top" and pad >= 3 and not right:
            # Owner row: fill the trailing gap with a rule so the top edge reads
            # as the box's horizontal line, breathing one cell on the TEXT side only.
            # User 2026-08-19 ("박스의 우측 상단 모서리가 살짝 끊어지는것도 거슬리니까"):
            # the symmetric one-cell breather also sat between the rule and `╮`, so the
            # corner floated off its own top edge — the only corner on the card that did
            # not touch its rule. The gap belongs between TEXT and rule, not between rule
            # and corner; the run now closes onto the corner and the width is unchanged.
            left.append((" ", None))
            left.append(("─" * (pad - 1), run_key or key))
        else:
            left.append((" " * pad, None))
    left.extend(right)
    left.append((_RAIL_TOP_RIGHT if edge == "top" else _RAIL_RIGHT, key))
    return left


_BOT_LABEL_TAIL = 3      # rule cells kept to the RIGHT of the close-rail label, so the
                         # breadcrumb never touches `╯` and every card's label ends on the
                         # same column (F-75: the right flush the user chose over centering —
                         # a centered label had no shared baseline across stacked cards and
                         # left a short route floating mid-rule).
_BOT_LABEL_MIN_LEAD = 2  # minimum rule left of the label — below this the line stops
                         # reading as a border, so the label is dropped whole instead.


def bottom_label_budget(box_width, rail_col=None):
    """Display cells a close-rail label may occupy on a card of `box_width`.

    ONE ledger, read the same way by the caller that BUILDS the breadcrumb and by
    `_dispatch_box_bottom` which places it — a label wider than this is folded by
    `_route_stage_segs`'s own `_drop_past_stages` at build time, never tail-cut
    against the corner."""
    run = max(0, box_width - (_RAIL_COL if rail_col is None else rail_col) - 2)
    return max(0, run - _BOT_LABEL_TAIL - _BOT_LABEL_MIN_LEAD - 2)   # 2 = the label's own spaces


def _dispatch_rail_label_layout(box_width, label_segs, rail_col=None):
    """Right-flush placement shared by the divider and the close rail (F-81).

    Both rails place an optional route-breadcrumb label with the exact same budget and
    lead-run arithmetic (F-75's tail-3/lead-2 baseline) so the breadcrumb lands in the same
    column whether it rides the header divider or the close rail — one ledger, not two
    copies. Returns ``(run, rule_lead_run_or_None)``; the second value is None when the
    label does not fit (caller draws a bare rule instead).
    """
    run = max(0, box_width - (_RAIL_COL if rail_col is None else rail_col) - 2)
    label_w = sum(_dw(text) for text, _k in (label_segs or ()))
    if not label_w or label_w > bottom_label_budget(box_width, rail_col):
        return run, None
    return run, run - label_w - 2 - _BOT_LABEL_TAIL


def _dispatch_box_bottom(box_width, key, run_key=None, label_segs=None, rail_col=None):
    """The card's close rail, optionally carrying the owner's route breadcrumb.

    F-75 (user 2026-08-14 "박스를 만들면서 하단 가로줄이 한줄을 잡아먹는데, 거기에 …
    우측 정렬 같은걸 해서 놓는건 어떨까"): the bottom rule was pure decoration while the
    depth-1 conductor breadcrumb had nowhere to go but the owner row, where it clipped to
    `re…` at 140 columns and shoved the frame around at 200. The rule now hosts it
    right-flushed — the same text-inside-a-rule idiom the owner row already uses for its
    padding-run fill — so the box reads as identity on top, pipeline on the bottom, and
    gains no line.

    F-81 (user 2026-08-20, restored 2026-10-09): the breadcrumb rides the header
    divider when the card has children. Only a childless card carries it here;
    F-75's `╰───` four-cell stub, width, corner, and grain are unchanged.

    F-68's grain contract still holds: the RULE stays steady (`run_key`) while corners keep
    `key`. Only the breadcrumb's own current token blinks, exactly as it did on the owner
    row — F-70 kept that pulse on the token and off the outline."""
    lead = [(" " * (_RAIL_COL if rail_col is None else rail_col), None), (_RAIL_BOT[0], key)]
    rule_key = run_key or key
    run, lead_run = _dispatch_rail_label_layout(box_width, label_segs, rail_col)
    if lead_run is None:
        return lead + [("─" * run, rule_key), (_RAIL_BOT_RIGHT, key)]
    return (lead
            + [("─" * lead_run, rule_key), (" ", None)]
            + list(label_segs)
            + [(" ", None), ("─" * _BOT_LABEL_TAIL, rule_key), (_RAIL_BOT_RIGHT, key)])


def _dispatch_box_divider(box_width, key, run_key=None, label_segs=None, rail_col=None):
    """One in-card rule closing the depth-1 owner's own rows, above its first descendant.

    User 2026-08-19 ("depth=1,2 사이에 구분감이 없는데(두줄이라서 특히) … depth=1이 일종의
    박스의 헤더처럼"): every row in the card carries two lines, so an owner block and a
    stage-worker block presented the same two-line silhouette and the eye had no boundary
    to land on — the owner read as just another sibling inside its own box. This rule gives
    the owner a header band.

    F-81 (user 2026-08-20 "파이프라인은 카드 헤더 밴드가 소유한다"): the owner's route
    breadcrumb now rides THIS rule instead of the close rail, using the exact placement
    `_dispatch_box_bottom` used to (`_dispatch_rail_label_layout` — one shared helper, not
    a parallel copy). Emitted when worker or resource children exist, even when
    completed workers folded out of view. A childless card has nothing to divide
    and carries its stage/status on the close rail instead.

    Geometry and grain match `_dispatch_box_bottom` exactly (same `run`, same steady
    `run_key` rule against `key` corners), so the divider reads as part of the frame rather
    than as content.
    """
    rule_key = run_key or key
    rail_col = _RAIL_COL if rail_col is None else rail_col
    run, lead_run = _dispatch_rail_label_layout(box_width, label_segs, rail_col)
    if lead_run is None:
        return [(" " * rail_col, None), (_RAIL_DIV_LEFT, key),
                ("─" * run, rule_key), (_RAIL_DIV_RIGHT, key)]
    return ([(" " * rail_col, None), (_RAIL_DIV_LEFT, key)]
            + [("─" * lead_run, rule_key), (" ", None)]
            + list(label_segs)
            + [(" ", None), ("─" * _BOT_LABEL_TAIL, rule_key), (_RAIL_DIV_RIGHT, key)])


_ORPHAN_DIVIDER_LABEL = "  ⌄ orphaned dispatch rows"


def _orphan_divider(term_width=None):
    """One dim in-band rule above a group card's project-level orphan rows.

    Silent by default — only emitted when the group actually has orphaned
    dispatch rows, so live-only groups render unchanged. Mirrors `_mem_divider`'s
    in-band rule convention so the body-tint loop can tint it like any other row.
    """
    width = term_width or 78
    rule = "─" * max(8, width - len(_ORPHAN_DIVIDER_LABEL) - 1)
    return [(_ORPHAN_DIVIDER_LABEL, "dim"), (" ", None), (rule, "dim")]


_LEVEL_SHORT = {
    "direct": "direct",
    "quick": "q",
    "light": "lt",
    "standard": "std",
    "strong": "strong",
    "thorough": "thr",
    "adversarial": "adv",
}

_ROLE_SHORT = {
    "capability-owner": "owner",
    "deep-reviewer": "review",
    "fast-reviewer": "review",
    "fast-implementer": "impl",
    "dev-refactor": "impl",
    "dev-new-lib": "impl",
    "research-survey": "research",
}

# drill/loop case ids (g6_worktree_dispatch, g9_cross_harness_depth2_dispatch, g8b_...) shrink
# to their gN prefix by a GENERAL rule instead of a per-case hardcoded entry above (F-9(b) —
# each new drill case used to need a code change here).
_G_CASE_PREFIX = re.compile(r"^(g\d+[a-z]?)")


def _short_level(value):
    if not value:
        return ""
    prefix = "~" if str(value).startswith("~") else ""
    raw = str(value)[1:] if prefix else str(value)
    return prefix + _LEVEL_SHORT.get(raw, raw)


def _short_role(value):
    if not value:
        return ""
    label, suffix = _stage_role_label(value)
    if label is not None:
        # stage suffix (":phase-A") rides along as part of the same dim profile tag — the
        # whole tag already renders "dim" (see _opts_segs), so no separate color key is needed.
        return _compact_dispatch_name(label + suffix, 14)
    m = _G_CASE_PREFIX.match(value)
    role = _ROLE_SHORT.get(value) or (m.group(1) if m else value.replace("-", "_"))
    return _compact_dispatch_name(role, 14)


_PROFILE_MAX = 28


def _strip_autopilot(name):
    if name and name.startswith("autopilot-"):
        return name[len("autopilot-"):]
    return name


def _owner_route_shape(j):
    """The bound route's compose `shape`, only for an owner row whose route was actually
    composed (never a compile-shaped preset route) — F-<next> plan §3 C-4.4."""
    if not _is_owner_mode_row(j):
        return None
    backing = getattr(getattr(j, "work_projection", None), "_route_view", None) or {}
    record = backing.get("record") if isinstance(backing.get("record"), dict) else {}
    selection = record.get("selection") if isinstance(record.get("selection"), dict) else {}
    if selection.get("route_origin") != "compose":
        return None
    shape = selection.get("shape")
    return shape if isinstance(shape, str) and shape else None


def _mode_axis_conflict(j):
    legacy = getattr(j, "mode", None) or ""
    worker_mode = getattr(j, "worker_mode", None)
    return bool(
        getattr(j, "mode_axis_conflict", False)
        or (
            _is_owner_mode_row(j)
            and (worker_mode or "/" in legacy)
        )
    )


def _display_capability_mode(j):
    """Return only an honest owner-capability mode for the options dial."""

    current = getattr(j, "capability_mode", None)
    if current:
        return current
    if _mode_axis_conflict(j):
        return None
    legacy = getattr(j, "mode", None)
    if getattr(j, "key", None) in _LOOPS_KEYS:
        return legacy
    return legacy if legacy and "/" not in legacy else None


def _entry_skill(j):
    """Skill identity for the options dial.

    A dispatch-depth-1 row names its entry capability. A dispatch-depth-2 row instead names the
    assigned stage: route node when present, otherwise the registered capability
    key. This prevents inherited owner mode (``dev/refactor``) from masquerading
    as the child's own work.
    """
    if max(1, int(getattr(j, "depth", 1) or 1)) >= 2 and not _is_owner_mode_row(j):
        owner = getattr(j, "capability_owner", None)
        contract = getattr(j, "assigned_contract", None)
        assigned = (
            contract
            if contract and _strip_autopilot(contract) != _strip_autopilot(owner or "")
            else getattr(j, "route_node", None) or getattr(j, "key", None)
        )
        return _compact_dispatch_name(_strip_autopilot(assigned), _PROFILE_MAX)
    cap = _strip_autopilot(getattr(j, "capability_owner", None) or "")
    key = getattr(j, "key", None)
    skill = cap or (key if key in _PIPE_STAGES else None)
    if not skill:
        return None
    projected_mode = (
        getattr(j, "worker_mode", None)
        or getattr(j, "mode", None)
        or ""
    )
    if skill in projected_mode.split("/"):
        return None
    return skill


def _dispatch_role_suffix(j, max_width=None):
    # qa is data-only now (kept in --json): the retired qa axis left the display entirely
    # (user 2026-07-16 — rigor derives from intensity, CONVENTIONS §1.1).
    worker_type = getattr(j, "worker_type", None)
    raw_role = worker_type if worker_type in {"owner", "stage", "review", "support", "frame"} else getattr(j, "worker_role", None)
    if getattr(j, "key", None) in _LOOPS_KEYS and raw_role == getattr(j, "slug", None):
        raw_role = None
    # A capability name is not a role (user 2026-07-20: "code가 표시가 되어있는데 굳이
    # autopilot-code가 왜 표시되는지") — a worker_role that restates the job's own key/
    # capability_owner drops, and a '<capability>-<role>' composite ('autopilot-code-owner')
    # keeps only its role tail instead of rendering as 'autopilot_cod…'.
    # Writer-vocabulary normalization (user 2026-07-20: "codex랑 claude가 서로 다르게 뜨는데")
    # — legacy rows may misfile PORTABLE MODEL-ROLE phrases ('deep orchestrator',
    # 'deep maker') and retired team metadata ('plan-team') into worker_role. Current writers
    # leave worker_role unset and use the independent fields above. Legacy harness role tokens
    # are always kebab, so
    # a space marks the model-role phrase: the orchestrator phrase IS the conductor
    # ('owner'), every other model-role/persona restates an axis the row already shows
    # (model cell / name-zone stage label) and drops. Display-only; the registry row keeps
    # the raw value.
    if raw_role and " " in raw_role:
        raw_role = "capability-owner" if raw_role.endswith("orchestrator") else None
    if raw_role and raw_role.endswith("-team"):
        raw_role = None
    if raw_role:
        bare = _strip_autopilot(raw_role)
        dups = [d for d in (_strip_autopilot(getattr(j, "capability_owner", None) or "") or None,
                            getattr(j, "key", None)) if d]
        if bare in dups:
            raw_role = None
        else:
            for d in dups:
                if bare.startswith(d + "-"):
                    raw_role = bare[len(d) + 1:]
                    break
    role = _short_role(raw_role)
    # intensity left this suffix for the dial's paren knob group (user 2026-07-20
    # hierarchical dial, _opts_segs) — it is a BEHAVIOUR axis, not part of the
    # environment tail this function feeds.
    if not role:
        return ""
    if max_width is not None and len(role) > max_width:
        # F-9(c): drop the whole component instead of tail-cutting it mid-token.
        return ""
    return role


def _dispatch_profile(j):
    # environment tail ONLY now — the worker role left for the dial's paren knob group
    # (user 2026-07-20: "owner의 위치가 애매" — 'boot/owner' read as one env path).
    profile = getattr(j, "profile", None)
    return _compact_dispatch_name(profile, _PROFILE_MAX) if profile else None


def _dispatch_model_profile(j):
    profile = getattr(j, "model_profile", None)
    if not profile or profile == "unsealed":
        return None
    reduced = getattr(j, "profile_granularity", None) not in {None, "", "full"}
    source = getattr(j, "profile_selection_source", None)
    provenance = {"matrix": ":m", "explicit": ":e", "legacy": ":l"}.get(source, "")
    label = "mp:" + str(profile) + ("~" if reduced else "") + provenance
    return _compact_dispatch_name(label, _PROFILE_MAX)


def _stack_split(l2):
    """Index where the gauge/stage part of a narrow L2 begins (the '[' of the bracket meter,
    a stage-track segment, or the 'key: ' label) — the ultra-narrow card breaks there."""
    for i, (t, k) in enumerate(l2):
        if (t == "[" and k == "dim") or (isinstance(k, str) and k.startswith("stg")) \
                or (k == "name_dim" and t.endswith(": ")):
            return i
    return len(l2)


# ---------- grouping assembler ----------
_SYSTEM_GROUP = "⚙ system"


def _project_fallback(cwd):
    from .display_snapshot import path_label
    return path_label(cwd)


def _project_for_display(cwd, display_snapshot=None):
    if display_snapshot is not None:
        return display_snapshot.project_for(cwd, _project_fallback)
    return _project_fallback(cwd)


def _group_key_session(s, display_snapshot=None):
    return _project_for_display(s.cwd, display_snapshot)


def _mem_row(s, layout="wide"):
    """Render one automation worker inside the dedicated system group."""
    name = _clip_w(s.title or s.slug or (s.harness or "?"), 40)
    seg = [("  🧠 ", "dim"), ("mem ", "dim"),
           (name, "dim"), ("  ", None),
           ((s.harness or "—"), "dim"), ("  ", None),
           (fmt_min(s.elapsed_min), "dim")]
    return [seg]


def _periodic_curate_row(memory, display_snapshot=None):
    try:
        item = (memory or {}).get("periodic_curate")
        if not isinstance(item, dict) or not item.get("total"):
            return None
        slug = _project_for_display(item.get("current_cwd"), display_snapshot)
        elapsed = _fmt_exec_age(item.get("elapsed_s", 0))
        return [("  ⚙ ", "g_work"),
                ("curator %d/%d %s %s" %
                 (int(item.get("done", 0)) + 1, int(item["total"]), slug, elapsed), "g_work")]
    except (KeyError, TypeError, ValueError):
        return None


_GOVERNOR_QUIET_FRACTION = 0.5   # F-28c "healthy 무음" (prd.md:288/311, plan §6a) — hide the row
                                  # below half the cap. Real observed live state (this cycle):
                                  # 1 active lease / cap 5 = 20% — comfortably below half, so a
                                  # single background lease (the normal steady-state) stays
                                  # silent; the row only earns its place once congestion is
                                  # actually worth a glance.


def _governor_segs(snapshot=_IO_UNSET):
    """`  ⚙ governor N/cap` — F-28c (prd.md:288/311). Pulse-ADJACENT, never merged into the
    pulse row's own session/job counts (I8 — this is a wholly separate line/collector). `None`
    (source absent OR healthy-quiet) = caller omits the row entirely."""
    if snapshot is _IO_UNSET:
        try:
            from .collectors import governor
            g = governor.collect()
        except Exception:
            g = None
    else:
        g = snapshot
    if not g:
        return None
    active, cap = g.get("active", 0), g.get("cap", 0)
    if cap <= 0 or active < cap * _GOVERNOR_QUIET_FRACTION:
        return None
    return [("  ⚙ ", "dim"), ("governor %d/%d" % (active, cap), "dim")]


def _memory_event_key(event):
    return (event.get("cwd") or event.get("project"), event.get("scope"),
            event.get("action"), event.get("snippet"))


def _memory_event_groups(events):
    """Count equal changes only when their observed source is the same."""
    groups = []
    by_key = {}
    for event in events:
        origin = event.get("cwd") or event.get("project")
        key = _memory_event_key(event)
        if origin and key in by_key:
            by_key[key][1] += 1
        else:
            group = [event, 1]
            groups.append(group)
            if origin:
                by_key[key] = group
    return groups


def _mem_change_rows(events, sid_titles=None, limit=8, term_width=None):
    from .collectors.memory import ADDED_ACTIONS, EXPIRED_ACTIONS, PRUNED_ACTIONS
    rows = []
    width = term_width or 120
    for event, count in _memory_event_groups(events)[:limit]:
        action = event.get("action")
        if action in ADDED_ACTIONS:
            label, sign, color = 'mem saved', "+", "lvl_g"
        elif action in EXPIRED_ACTIONS or action in PRUNED_ACTIONS:
            label, sign, color = 'mem pruned', "−", "dim"
        elif action == "decision-record":
            label, sign, color = 'mem saved', "·", "dim"
        else:
            label, sign, color = 'mem changed', "·", "dim"
        origin = event.get("cwd") or event.get("project")
        if not origin:
            origin = "global" if event.get("scope") == "global" else "source unknown"
        # Source is preserved in JSON. Keep its distinctive last path components
        # in a narrow row, after the content and before optional session/time.
        origin = str(origin)
        source_room = max(12, min(36, width // 3))
        if _dw(origin) > source_room:
            while origin and _dw(origin) > source_room - 1:
                origin = origin[1:]
            origin = "…" + origin
        suffix = " · " + origin + (" · ×%d" % count if count > 1 else "")
        title = (sid_titles or {}).get(event.get("sid"))
        stamp = str(event.get("ts") or "")
        if "T" in stamp:
            stamp = stamp.replace("T", " ")[:16]
        prefix = [("  🧠 ", "dim"), (sign, color), (" " + label + " · ", "dim")]
        room = max(0, width - sum(_dw(t) for t, _ in prefix) - _dw(suffix))
        snippet = _clip_w(str(event.get("snippet") or "content unknown"), room)
        row = prefix + [(snippet, "dim"), (suffix, "dim")]
        optional = (" ⟵ " + _clip_w(title, 22) if title else "") + (" · " + stamp if stamp else "")
        remaining = width - sum(_dw(t) for t, _ in row)
        if _dw(optional) <= remaining:
            row.append((optional, "dim"))
        rows.append(row)
    return rows


def _mem_event_rows(memory, limit=8, term_width=None, excluded_ids=()):
    if not memory:
        return []
    events = [event for event in memory.get("recent") or []
              if not event.get("id") or event["id"] not in excluded_ids]
    return _mem_change_rows(events, limit=limit, term_width=term_width)


_MEM_DIVIDER_MARGIN = 12
_MEM_REPO_ROW_LIMIT = 2


def _mem_divider(term_width=None):
    return [(" ", None), ("─" * max(8, (term_width or 78) - _MEM_DIVIDER_MARGIN), "dim")]


def _mem_repo_rows(events, sid_titles, limit=_MEM_REPO_ROW_LIMIT, term_width=None):
    return _mem_change_rows(events or [], sid_titles, limit, term_width)


def _delivery_pending_rows(counts):
    if isinstance(counts, dict) and counts.get("pending", 0):
        line = "  delivery pending: %d · awaiting recipient" % counts["pending"]
        no_recipient = counts.get("no_recipient")
        if isinstance(no_recipient, int) and not isinstance(no_recipient, bool):
            line += ui_text("fleet.delivery.no_recipient", count=no_recipient)
        return [[(line, "lvl_y")]]
    return []


def _unique_managed_parents(sessions):
    from .model import unique_managed_parents
    return unique_managed_parents(sessions)


def _group_key_job(j, session_groups=None, job_groups=None, managed_session_groups=None,
                   display_snapshot=None):
    session_groups = session_groups or {}
    job_groups = job_groups or {}
    managed_session_groups = managed_session_groups or {}
    if getattr(j, "parent_slug", None) and j.parent_slug in job_groups:
        return job_groups[j.parent_slug]
    edge_sid = getattr(j, "_parent_edge_sid", None)
    if edge_sid in session_groups:
        return session_groups[edge_sid]
    if not hasattr(j, "_parent_edge_sid") and getattr(j, "parent_sid", None) and j.parent_sid in session_groups:
        return session_groups[j.parent_sid]
    parent_managed_dir = getattr(j, "parent_managed_dir", None)
    if (
        not hasattr(j, "_parent_edge_sid")
        and getattr(j, "source", None) != "plugin-queue"
        and parent_managed_dir
        and os.path.normpath(parent_managed_dir) in managed_session_groups
    ):
        return managed_session_groups[os.path.normpath(parent_managed_dir)]
    if getattr(j, "parent_cwd", None):
        return _project_for_display(j.parent_cwd, display_snapshot)
    # Drill is fixture-rooted: keep its runner + dispatch-depth-1 owner + dispatch-depth-2 workers
    # together in one /tmp/drill-* card. Other scheduled loops stay in the shared
    # control-plane group.
    if j.key in _LOOPS_KEYS:
        drill_group = _project_for_display(j.cwd, display_snapshot)
        if j.key == "drill" and drill_group.startswith("drill:"):
            return drill_group
        return "loops"
    return _project_for_display(j.cwd, display_snapshot)


def _visible_group_jobs(jobs, show_sessions, show_jobs):
    """Section routing for mixed storage: plugin agents belong to Fleet, not dispatch."""
    return [
        job for job in jobs
        if (show_sessions if _is_plugin_agent(job) else show_jobs)
    ]


def _group_activity_rank(g):
    members_live = [s.liveness for s in g["sessions"]] + [j.liveness for j in g["jobs"]]
    if g.get("gpu") or _group_running_resources(g):
        members_live.append("working")
    if "working" in members_live:
        return 0
    elif "idle" in members_live:
        return 1
    return 2


def _group_running_resources(g):
    """Rendering placement does not change the project's activity census."""
    candidates = list(g.get("resources") or ())
    candidates.extend(child for job in g["jobs"]
                      for child in getattr(job, "resource_children", ()))
    return {id(child): child for child in candidates if child.liveness == "working"}


def _group_last_result(sessions, jobs, resources):
    candidates = []
    for entity in list(sessions) + list(jobs):
        projection = getattr(entity, "work_projection", None)
        candidates.append(getattr(projection, "result", None))
        candidates.extend(node.get("result") for node in
                          (getattr(entity, "route_chain", None) or {}).get("nodes", ()))
    children = list(resources)
    children.extend(child for job in jobs for child in getattr(job, "resource_children", ()))
    for child in children:
        if (child.liveness in ("exited", "dead") and type(child.exit_code) is int
                and isinstance(child.ended_at, (int, float)) and not isinstance(child.ended_at, bool)):
            candidates.append({"name": 'resource ' + _resource_name(child),
                               "result": "failure" if child.exit_code else "success",
                               "at": child.ended_at, "source": "resource-exit"})
    return max((value for value in candidates if isinstance(value, dict)
                and value.get("name") and value.get("result") in ("success", "failure")
                and type(value.get("at")) in (int, float)
                and math.isfinite(value["at"]) and value["at"] <= time.time()),
               key=lambda value: value["at"], default=None)


def _group_sort_key(name, g):
    activity_rank = _group_activity_rank(g)
    mtimes = [s.mtime for s in g["sessions"] if s.mtime is not None]
    recency = max(mtimes) if mtimes else None
    # None mtime sorts as oldest (i.e. last) — use a very negative sentinel for the desc sort.
    recency_sort = recency if recency is not None else -1.0
    return (activity_rank, -recency_sort, name)


def _sort_group_sessions(ss):
    def k(s):
        r = _LIVE_RANK.get(s.liveness, 9)
        if s.detached and r < 3:
            r = 3          # Detached sessions sort below working and idle sessions.
        # F-100c (user 2026-09-03 "steward 세션은 repo의 가장 상단"): a stewarding session
        # (depth −1 role) leads its repo group regardless of liveness; the ranks below are
        # unchanged among non-stewards, so existing stable-order anchors keep their order.
        steward_rank = 0 if getattr(s, "steward", False) else 1
        return (steward_rank, r, -(s.elapsed_min or 0), repr(_live_session_identity(s)))
    return _steward_hierarchy(sorted(ss, key=k))[0]


def _steward_hierarchy(rows):
    """Flat supervisor groups over enriched role targets and proven session joins.

    The shared role collector has already removed supervisor-to-supervisor
    relations. Each session is emitted once, with no probing or nested lanes.
    """
    from .collectors.steward import _session_keys
    unique = {}
    for s in rows:
        unique.setdefault(_live_session_identity(s), s)
    rows = list(unique.values())
    by_key = {}
    for s in rows:
        if s.liveness in ("stale", "dead") or s.app_server or s.mem_worker:
            continue
        for key in _session_keys(s):
            by_key.setdefault(key, []).append(s)
    targets = {}
    for s in rows:
        children = []
        if getattr(s, "steward", False):
            for target in getattr(s, "steward_targets", ()) or ():
                key = (str(target.get("harness") or "").lower(), target.get("session_id"))
                candidates = by_key.get(key, ())
                if len(candidates) != 1:
                    continue
                child = candidates[0]
                if child is not s and child not in children:
                    children.append(child)
        targets[id(s)] = children
    ordered, seen, edges = [], set(), {}

    def visit(s):
        if id(s) in seen:
            return
        seen.add(id(s))
        ordered.append(s)
        for child in targets[id(s)]:
            if id(child) not in seen:
                edges.setdefault(id(s), []).append(child)
                visit(child)

    stewards = sorted((s for s in rows if getattr(s, "steward", False)),
                      key=lambda s: repr(_live_session_identity(s)))
    for s in stewards + rows:
        visit(s)
    return ordered, edges


# Dashed so a supervisor line never reads as an owner box border; the dashes
# already leave a gap beside each ID, so every row uses the same mark.
_STEWARD_LINE_MARK = "┆"


def _under_id_connector(segs, mark=_STEWARD_LINE_MARK, key="dim"):
    """Paint only the reserved cell; detail layout never depends on a relation."""
    return _overwrite_rail_text(segs, _STEWARD_LINE_COL, mark, key)


def _live_session_identity(s):
    """Stable, display-independent identity for a live-order anchor."""
    if s.session_id:
        if getattr(s, "app_server", False):
            kind = "app-server"
        elif getattr(s, "mem_worker", False):
            kind = "memory-worker"
        else:
            kind = "session"
        return ("sid", s.harness, s.session_id, kind)
    if s.pid:
        return ("proc", s.harness, s.pid, getattr(s, "proc_start", None))
    return ("fallback", s.harness, s.cwd, s.slug)


def _reconcile_live_order(anchors, current, identity):
    """Keep visible survivors in anchor order, then append current newcomers.

    A list per identity deliberately retains every current object if imperfect
    collection data produces a collision; snapshot order is the tie-breaker.
    """
    buckets = {}
    for item in current:
        buckets.setdefault(identity(item), []).append(item)
    ordered = []
    for key in anchors:
        bucket = buckets.get(key)
        if bucket:
            ordered.append(bucket.pop(0))
    for item in current:
        key = identity(item)
        bucket = buckets.get(key)
        if bucket and bucket[0] is item:
            ordered.append(bucket.pop(0))
    return ordered, [identity(item) for item in ordered]


class _LiveOrderState:
    """Run-local group and session anchors for the live curses renderer."""
    def __init__(self):
        self.groups = []
        self.group_tiers = {}
        self.sessions = {}

    def reconcile_groups(self, names, tiers=None):
        """Stabilize survivors within their current activity tier.

        A tier transition is intentionally treated as a new entry into the
        destination tier: activity changes take effect immediately, while
        mtime churn cannot reshuffle groups that stayed peers.
        """
        current_tiers = ({name: 0 for name in names} if tiers is None
                         else {name: tiers[name] for name in names})
        ordered = []
        anchors = []
        for tier in sorted(set(current_tiers.values())):
            tier_names = [name for name in names if current_tiers[name] == tier]
            tier_anchors = [name for name in self.groups
                            if self.group_tiers.get(name) == tier]
            tier_order, tier_keys = _reconcile_live_order(
                tier_anchors, tier_names, lambda name: name)
            ordered.extend(tier_order)
            anchors.extend(tier_keys)
        self.groups = anchors
        self.group_tiers = current_tiers
        current = set(names)
        self.sessions = {name: anchors for name, anchors in self.sessions.items()
                         if name in current}
        return ordered

    def reconcile_sessions(self, group, rows):
        ordered, self.sessions[group] = _reconcile_live_order(
            self.sessions.get(group, []), rows, _live_session_identity)
        ordered = _steward_hierarchy(ordered)[0]
        self.sessions[group] = [_live_session_identity(s) for s in ordered]
        return ordered


def _sort_group_jobs(js):
    return sorted(js, key=lambda j: (_JOB_LIVE_RANK.get(j.liveness, 9), -(j.elapsed_min or 0)))


def _fmt_exec_age(seconds):
    """Sub-minute exec ages need seconds; past that reuse `fmt_min`'s one vocabulary."""
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % max(0, seconds)
    return fmt_min(seconds // 60)

_RFLUSH = "\x00 \x00"
# F-68b: marks an in-card inline tail tag (the elapsed). The frame keeps the
# tag whole and ellipsizes the CONTENT when the row runs long.
_CARD_TAG = "\x00t\x00"
