"""Pure text layout helpers for native style history cards and transcripts."""
from __future__ import annotations

import re
from .. import mdescape
from .grid import cell_width


def wrap_spans(spans, width):
    rows, current, used = [], [], 0
    width = max(1, int(width))
    for text, key in spans:
        part = ""
        for char in str(text):
            cells = cell_width(char)
            if used + cells > width and (current or part):
                if part:
                    current.append((part, key))
                    part = ""
                rows.append(current)
                current, used = [], 0
            if char == " " and not current and not part and rows:
                continue
            part += char
            used += cells
        if part:
            current.append((part, key))
    return rows + ([current] if current else [[]])


def markdown_spans(text, base="soft"):
    spans = []
    for index, segment in enumerate(re.split(r"(`[^`]+`)", str(text))):
        if not segment:
            continue
        if segment.startswith("`") and segment.endswith("`"):
            spans.append((segment[1:-1], "code"))
            continue
        pieces = segment.split("**")
        for offset, piece in enumerate(pieces):
            if piece:
                spans.append((piece, "bold" if offset % 2 else base))
    return spans


_LONG_ID = re.compile(r"\b(?:rrev|ref|rt|att|tidy|ss|ssc|input|delivery)[-_][0-9A-Za-z_-]{6,}\b|\b[0-9a-f]{12,}\b")


def card_spans(text, base="soft"):
    result = []
    for value, key in markdown_spans(text, base):
        if key == "code":
            result.append((value, key))
            continue
        for index, part in enumerate(_LONG_ID.split(value)):
            if part:
                result.append((part, "dim" if index % 2 else key))
    return result


def card_rows(lines, width, spans=None):
    """Return wrapped (segments, tint) rows for the approved markdown card rules.

    ``spans`` words a text's inline spans (default ``card_spans``, which sets a long id apart as
    ``dim``); ``markdown_spans`` keeps long ids in the line's own key."""
    spans = spans or card_spans
    result = []
    for line in lines:
        line = str(line)
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            if level == 2 and result:
                result.append(([], None))
            key = {1: "v", 2: "c", 3: "head"}[level]
            result.extend((row, None) for row in wrap_spans(spans(heading.group(2), key), width))
            continue
        item = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if item:
            depth = len(item.group(1).expandtabs(2)) // 2
            mark = ("•", "◦", "·")[min(depth, 2)] if item.group(2) in ("-", "*") else item.group(2)
            lead = "  " * depth + mark + " "
            hang = " " * cell_width(lead)
            body = wrap_spans(spans(item.group(3), "soft"), max(8, width - cell_width(lead)))
            for index, row in enumerate(body):
                result.append((([(lead, "dim")] if index == 0 else [(hang, None)]) + row, None))
            continue
        result.extend((row, None) for row in wrap_spans(spans(line.strip(), "soft"), width))
    return result


# -- a document drawn for a popup: cards for prose, aligned tables, fences as written ----------------------
#
# ``doc_rows(lines, width)`` -> rows of ``(text, key)`` segments (``key`` None for padding), none wider than
# ``width``. Prose is ``card_rows`` (long ids kept; ``#``/``##`` in Fleet's section key); a code fence
# stands as written, one row per line, cut with ``…``; a GFM table (a ``|`` line followed by a delimiter
# row of the same cell count) is ``table_rows``. Fleet's own screens still draw tables as cards.

_LINK = re.compile(r"!?\[([^\][]*)\]\([^)]*\)")      # no ``[`` in the words: a run of ``[`` is not a quadratic scan
_CODE = re.compile(r"(`[^`]+`)")
_DELIM_CELL = re.compile(r"^:?-+:?$")
_GAP = 2                # columns two cells apart, no vertical line (Fleet's LAB table)
_FLOOR = 6              # a column narrows to this (or its own width) before the table gives up
_HEAD_FLOOR = 12        # ... a wider header keeps up to this much


_protect, _restore = mdescape.protect, mdescape.restore
_COMMENT = re.compile(r"^<!--(?:(?!-->).)*-->$")   # one comment spanning the whole line, not two with text between


def _unescaped_spans(text, base="soft"):
    return [(_restore(value), key) for value, key in markdown_spans(text, base)]


def _unlink(text):
    """A link is its words (``[a](url)`` -> ``a``); code spans are left as they are."""
    return "".join(part if index % 2 else _LINK.sub(r"\1", part)
                   for index, part in enumerate(_CODE.split(text)))


_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")     # C0 (not tab, newline), DEL, C1: ESC, BEL, NUL, 0x9b ...
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")           # text-direction overrides (U+200D, the emoji joiner, stays)


def clean(text):
    """Text safe to hand to a terminal: control characters become a space, direction overrides go."""
    return _BIDI.sub("", _CONTROL.sub(" ", str(text)))


def _plain(text):
    return clean(str(text).replace("\t", "    ").replace("\r", ""))


def split_cells(line):
    """A table line's cells: ``|`` splits unless escaped (backslash-bar) or inside a code span; the outer bars go."""
    text = str(line).strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells, current, index = [], "", 0
    while index < len(text):
        char = text[index]
        if char == "\\" and text[index + 1:index + 2] == "|":
            current += "|"
            index += 2
        elif char == "`" and text.find("`", index + 1) != -1:
            end = text.find("`", index + 1)
            current += text[index:end + 1].replace("\\|", "|")
            index = end + 1
        elif char == "|":
            cells.append(current.strip())
            current, index = "", index + 1
        else:
            current += char
            index += 1
    cells.append(current.strip())
    return cells


def table_aligns(line, count):
    """The column alignments when ``line`` is a delimiter row for ``count`` header cells, else None."""
    line = str(line)
    if "|" not in line or "-" not in line:
        return None
    cells = split_cells(line)
    if len(cells) != count or not all(_DELIM_CELL.match(cell) for cell in cells):
        return None
    return ["center" if cell.startswith(":") and cell.endswith(":") else "right" if cell.endswith(":")
            else "left" for cell in cells]


def _cell_spans(text, base):
    return [(value, (base if base != "soft" else "bold") if key == "bold" else key)
            for value, key in _unescaped_spans(_unlink(_protect(text)), base)]


def _width(segments):
    return sum(cell_width(text) for text, _key in segments)


def _wrap_cell(segments, width):
    """A cell's rows at ``width`` cells: broken at spaces, a word longer than a row broken by characters."""
    words, word = [], []
    for text, key in segments:
        for index, part in enumerate(re.split(r"( +)", text)):
            if not part:
                continue
            if part.isspace():
                if word:
                    words.append(word)
                word = []
            else:
                word.append((part, key))
    if word:
        words.append(word)
    rows, row, used = [], [], 0
    for word in words:
        size = _width(word)
        if row and used + 1 + size <= width:
            row.append((" ", None))
            row.extend(word)
            used += 1 + size
            continue
        if row and size <= width:
            rows.append(row)
            row, used = [], 0
        elif row and width - used - 1 < 2:
            rows.append(row)
            row, used = [], 0
        if row:                          # a word longer than a row starts where the row ends
            row.append((" ", None))
            used += 1
        for text, key in word:
            for char in text:
                cells = cell_width(char)
                if used + cells > width and (row or used):
                    rows.append(row)
                    row, used = [], 0
                if row and row[-1][1] == key and row[-1][0] != " ":
                    row[-1] = (row[-1][0] + char, key)
                else:
                    row.append((char, key))
                used += cells
    if row or not rows:
        rows.append(row)
    return rows


def _cut(segments, width):
    """One row as written, cut at ``width`` cells with ``…`` where it runs over."""
    if _width(segments) <= width:
        return list(segments)
    out, used = [], 0
    for text, key in segments:
        part = ""
        for char in text:
            cells = cell_width(char)
            if used + cells > width - 1:
                return out + [((part + "…") if part else "…", key)]
            part += char
            used += cells
        out.append((part, key))
    return out


def table_rows(header, aligns, body, width):
    """An aligned table as rows: the header in Fleet's table-head key, a ``┈`` rule the table's width,
    then the body. Over ``width`` the widest columns narrow first and their cells wrap inside the column
    (no column goes under 6 cells, 12 for a wider header; nothing is cut). A table that cannot fit even
    so is ``None`` (the caller draws its lines as written). A short row gets empty cells; cells over
    the header's count join the last one with `` | ``."""
    count = len(header)
    rows = []
    for cells in body:
        cells = list(cells)
        if len(cells) < count:
            cells += [""] * (count - len(cells))
        elif len(cells) > count:
            cells = cells[:count - 1] + [" | ".join(cells[count - 1:])]
        rows.append(cells)
    head = [_cell_spans(cell, "head") for cell in header]
    grid = [[_cell_spans(cell, "soft") for cell in row] for row in rows]
    natural = [max([_width(head[i])] + [_width(row[i]) for row in grid]) for i in range(count)]
    floors = [min(natural[i], max(_FLOOR, min(_width(head[i]), _HEAD_FLOOR))) for i in range(count)]
    room = width - _GAP * (count - 1)
    if sum(floors) > room:
        return None
    sizes = list(natural)
    while sum(sizes) > room:
        index = max((i for i in range(count) if sizes[i] > floors[i]), key=lambda i: sizes[i], default=None)
        if index is None:
            return None
        sizes[index] -= 1

    def draw(cells):
        wrapped = [_wrap_cell(cells[i], sizes[i]) for i in range(count)]
        out = []
        for line in range(max(len(rows_) for rows_ in wrapped)):
            segments = []
            for i in range(count):
                part = wrapped[i][line] if line < len(wrapped[i]) else []
                spare = sizes[i] - _width(part)
                left = spare if aligns[i] == "right" else spare // 2 if aligns[i] == "center" else 0
                if i:
                    segments.append((" " * _GAP, None))
                if left:
                    segments.append((" " * left, None))
                segments.extend(part)
                if spare - left and i < count - 1:
                    segments.append((" " * (spare - left), None))
            while segments and segments[-1][1] is None:
                segments.pop()
            out.append(segments)
        return out

    out = draw(head)
    out.append([("┈" * (sum(sizes) + _GAP * (count - 1)), "line")])
    for cells in grid:
        out.extend(draw(cells))
    return out


def _indented(line):
    """Four spaces or more: GFM's code block, so a ``|`` line there is not a table's."""
    return len(line) - len(line.lstrip(" ")) >= 4


def _table_end(lines, index):
    """The index past the markdown table that starts at ``lines[index]`` (GFM: a ``|`` line, then a
    delimiter row with as many cells; it runs on while non-empty ``|`` lines follow), else None."""
    line = lines[index]
    if not ("|" in line.strip() and index + 1 < len(lines) and not _indented(line) and not _indented(lines[index + 1])):
        return None
    if not table_aligns(lines[index + 1], len(split_cells(line))):
        return None
    end = index + 2
    while end < len(lines) and "|" in lines[end] and lines[end].strip():
        end += 1
    return end


def _drawn_table(block, width):
    """A table's lines (header, delimiter, body) drawn at ``width``: aligned columns, else as written."""
    header = split_cells(block[0])
    drawn = table_rows(header, table_aligns(block[1], len(header)), [split_cells(row) for row in block[2:]], width)
    if drawn is None:                     # no room even at the floors: as written, one row each
        drawn = [_cut([(row.strip(), "soft")], width) for row in block]
    return drawn


def table_parts(lines, width):
    """``lines`` with each markdown table drawn at ``width`` cells, the rest untouched (11-15).

    A list of items in order: a line that is no table's is the ``str`` as it came (the caller wraps it
    as before); a table is the list of its drawn ``(text, key)`` rows (``doc_rows``' one table rule).
    Inside a code fence nothing is a table. A message body or a question's text keeps its own look and
    only its tables read aligned."""
    width = max(1, int(width))
    lines = [str(line) for line in lines]
    plain = [_plain(line) for line in lines]
    out, fence, index = [], None, 0
    while index < len(lines):
        text = plain[index].strip()
        marker = re.match(r"^(```|~~~)", text)
        if fence is not None:
            if marker and marker.group(1) == fence and not text.strip(marker.group(1)):
                fence = None
        elif marker:
            fence = marker.group(1)
        else:
            end = _table_end(plain, index)
            if end:
                out.append([row if _width(row) <= width else _cut(row, width) for row in _drawn_table(plain[index:end], width)])
                index = end
                continue
        out.append(lines[index])
        index += 1
    return out


def doc_rows(lines, width):
    """A document's lines drawn at ``width`` cells: ``(text, key)`` rows, none wider than ``width``."""
    width = max(1, int(width))
    lines = [_plain(line) for line in lines]
    out, prose, fence, index = [], [], None, 0

    def flush():
        if not prose:
            return
        first = next((line for line in prose if line.strip()), "")
        if out and out[-1] and re.match(r"^##\s", first):
            out.append([])               # Fleet's cards part a ``##`` from what came before it
        for segments, _tint in card_rows(prose, width, spans=_unescaped_spans):
            out.append([(text, "sect" if key in ("v", "c") else key) for text, key in segments])
        prose.clear()

    while index < len(lines):
        line = lines[index]
        text = line.strip()
        marker = re.match(r"^(```|~~~)", text)
        if fence is not None:
            if marker and marker.group(1) == fence and not text.strip(marker.group(1)):
                fence = None
            else:
                out.append(_cut([("  " + line, "code")], width))
            index += 1
            continue
        if marker:
            flush()
            fence = marker.group(1)
            index += 1
            continue
        end = _table_end(lines, index)
        if end:
            flush()
            out.extend(_drawn_table(lines[index:end], width))
            out.append([])                # one empty row parts the table from what follows
            index = end
            continue
        if _COMMENT.match(text):          # a whole-line HTML comment (a file's own marker) is not drawn
            index += 1
            continue
        if not text and prose and not prose[-1].strip():
            index += 1
            continue
        prose.append(_unlink(_protect(line)))
        index += 1
    flush()
    return [row if _width(row) <= width else _cut(row, width) for row in out]


_ROLE = re.compile(r"^(user|assistant|thinking|tool output|tool [^:]{1,32}|description|attachment|"
                   r"runtime_wait|supervisor): ?(.*)$")
_OUT_ROWS = 3
_DESC_INLINE = ("Bash", "Agent", "Task", "Monitor")
_LOOK = {"claude": (">", "●", "●", "⎿"), "codex": ("›", "•", "•", "└"),
         "opencode": (">", "●", "●", "⎿")}
_CODEX_VERB = {"exec_command": "Ran", "shell": "Ran", "wait": "Waited", "apply_patch": "Edited"}
_ERR = re.compile(r"(^|\b)(error|Error|ERROR|Traceback|exit code [1-9]|Exit code [1-9])|<tool_use_error>")
_ST_USER, _ST_AGENT, _ST_CODE = ("v", 0, 0), ("w", 0, 0), ("l", 0, 0)
_ST_DIM, _ST_NAME, _ST_ARG = ("d", 0, 1), ("w", 1, 0), ("d", 0, 0)


def _md_spans(text, base):
    spans = []
    for index, part in enumerate(re.split(r"(`[^`]+`)", text)):
        if index % 2:
            spans.append((part[1:-1], _ST_CODE))
            continue
        for offset, piece in enumerate(part.split("**")):
            if piece:
                spans.append((piece, (base[0], 1, base[2]) if offset % 2 else base))
    return spans


def _clip_spans(spans, width):
    rows = wrap_spans(spans, width)
    return rows[0] if len(rows) == 1 else wrap_spans(spans, width - 1)[0] + [("…", _ST_DIM)]


def _log_events(lines):
    events, current = [], None
    for item in lines:
        if isinstance(item, dict):
            item = f"{item.get('role', 'assistant')}: {item.get('text', '')}"
        line = str(item)
        match = _ROLE.match(line)
        if not match:
            if current is None:
                current = {"kind": "assistant", "text": [], "out": None, "cont": True}
                events.append(current)
            (current["out"] if current["kind"] == "tool" and current["out"] is not None
             else current["text"]).append(line)
            continue
        head, text = match.groups()
        if current and current["kind"] == "tool" and current["out"] is None and head in (
                "description", "tool output"):
            if head == "description":
                current["desc"] = text
            else:
                current["out"] = [text]
            continue
        if head == "supervisor" and current and current["kind"] == "supervisor":
            current["text"].append(text)
            continue
        if head.startswith("tool ") and head != "tool output":
            name = head[5:]
            desc_only = name in _DESC_INLINE and text
            current = {"kind": "tool", "name": name, "text": [] if desc_only or not text else [text],
                       "desc": text if desc_only else "", "out": None}
        else:
            current = {"kind": head, "text": [text], "out": None}
        events.append(current)
    return events


def dialogue_rows(lines, width, harness="claude"):
    """Raw lines drawn the way the harness's own screen draws its transcript, in Fleet colours."""
    from .output import clip
    you, said, called, elbow = _LOOK.get(str(harness), _LOOK["claude"])
    rows = []

    def para(lead, texts, base, tint=None):
        indent = [(" " * sum(cell_width(text) for text, _key in lead), None)]
        first = True
        for text in texts:
            spans = _md_spans(text, base)
            if text.lstrip().startswith("#"):
                spans = [(text.lstrip("# ").replace("**", ""), (base[0], 1, base[2]))]
            for part in wrap_spans(spans, max(8, width - sum(cell_width(t) for t, _k in indent))):
                rows.append(((lead if first else indent) + part, tint))
                first = False

    for event in _log_events(lines):
        kind = event["kind"]
        if kind in ("attachment", "description"):
            continue
        if rows:
            rows.append(([], None))
        if kind == "user":
            para([(you + " ", _ST_DIM)], event["text"], _ST_USER, 234)
        elif kind == "assistant":
            para([("  ", None)] if event.get("cont") else [(said + " ", _ST_AGENT)],
                 event["text"], _ST_AGENT)
        elif kind == "thinking":
            rows.append(([("✻ Thinking…", _ST_DIM)], None))
        elif kind == "tool":
            out = [text for text in (event["out"] or [])
                   if text.strip() not in ("<persisted-output>", "</persisted-output>")]
            dot = ("r", 0, 0) if any(_ERR.search(text) for text in out[:2]) else ("g", 0, 0)
            arg = next((text.strip() for text in event["text"] if text.strip()), "")
            arg = re.sub(r"^(file_path|path|pattern|url|query|command|prompt): ", "", arg)
            name = _CODEX_VERB.get(event["name"], event["name"]) if harness == "codex" else event["name"]
            head = [(called + " ", dot), (name, _ST_NAME)]
            room = width - sum(cell_width(text) for text, _key in head) - 2
            if arg and event.get("desc"):
                arg = clip(arg, max(20, room * 3 // 5))
            if arg:
                head += [(" " + arg, _ST_ARG)] if harness == "codex" else [("(" + arg + ")", _ST_ARG)]
            if event.get("desc"):
                head.append(("  " + event["desc"], _ST_DIM if arg else _ST_ARG))
            rows.append((_clip_spans(head, width), None))
            for index, text in enumerate(out[:_OUT_ROWS]):
                lead = "  %s  " % elbow if index == 0 else "     "
                rows.append(([(lead, _ST_DIM)] + _clip_spans([(text, _ST_DIM)], width - 5), None))
            if len(out) > _OUT_ROWS:
                rows.append(([("     … +%d lines" % (len(out) - _OUT_ROWS), _ST_DIM)], None))
        elif kind == "runtime_wait":
            rows.append(([("⏸ waiting · " + " ".join(event["text"]), _ST_DIM)], None))
        elif kind == "supervisor":
            seen = []
            for text in (text.replace("event ", "") for text in event["text"]):
                if not seen or seen[-1][0] != text:
                    seen.append([text, 1])
                else:
                    seen[-1][1] += 1
            line = " · ".join(text + (" ×%d" % count if count > 1 else "") for text, count in seen)
            rows.append((_clip_spans([("⋯ runtime  ", _ST_DIM), (line, _ST_DIM)], width), None))
        else:
            para([("  ", None)], event["text"], _ST_DIM)
    return rows


def _strip_token(text):
    return text if text.strip() not in ("<persisted-output>", "</persisted-output>") else ""
