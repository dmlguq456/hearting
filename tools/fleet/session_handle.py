"""Pure, shared projection of opaque runtime session IDs into display handles."""

from __future__ import annotations

import unicodedata


_PREFIXES = {"claude": "CL", "codex": "CX", "opencode": "OC"}


def session_handle(harness: object, session_id: object) -> str:
    prefix = _PREFIXES.get(str(harness or "").lower())
    if not prefix or not isinstance(session_id, str) or not session_id:
        return ""
    return f"{prefix}/{session_id[:8]}"


def sanitize_title(title: object) -> str:
    if not isinstance(title, str):
        return ""
    single_line = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch
                          for ch in title)
    return " ".join(single_line.split())


def _cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(ch) else
               (2 if unicodedata.east_asian_width(ch) in "WFA" else 1)
               for ch in text)


def clip_cells(text: object, budget: object) -> str:
    value = str(text or "")
    try:
        limit = max(0, int(budget))
    except (TypeError, ValueError):
        limit = 0
    if _cell_width(value) <= limit:
        return value
    if limit <= 0:
        return ""
    if limit == 1:
        return "…"
    out = []
    used = 0
    for ch in value:
        width = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in "WFA" else 1)
        if used + width > limit - 1:
            break
        out.append(ch)
        used += width
    return "".join(out) + "…"


def session_display_name(harness: object, session_id: object, title: object,
                         budget: object = None, fallback: object = "") -> str:
    handle = session_handle(harness, session_id)
    if not handle:
        return sanitize_title(fallback)
    name = handle
    clean_title = sanitize_title(title)
    if clean_title:
        candidate = f"{handle} · {clean_title}"
        if budget is None or _cell_width(candidate) <= int(budget):
            return candidate
        separator = " · "
        remaining = int(budget) - _cell_width(handle) - _cell_width(separator)
        # A lone ellipsis is not a meaningful title; drop the whole optional
        # segment rather than leaving a separator-only display behind.
        if remaining > 1:
            name = f"{handle}{separator}{clip_cells(clean_title, remaining)}"
    if budget is None:
        return name
    return clip_cells(name, max(0, int(budget)))
