"""Pure text layout helpers for native style history cards and transcripts."""
from __future__ import annotations

import re
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


def card_rows(lines, width):
    """Return wrapped (segments, tint) rows for the approved markdown card rules."""
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
            result.extend((row, None) for row in wrap_spans(card_spans(heading.group(2), key), width))
            continue
        item = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if item:
            depth = len(item.group(1).expandtabs(2)) // 2
            mark = ("•", "◦", "·")[min(depth, 2)] if item.group(2) in ("-", "*") else item.group(2)
            lead = "  " * depth + mark + " "
            hang = " " * cell_width(lead)
            body = wrap_spans(card_spans(item.group(3), "soft"), max(8, width - cell_width(lead)))
            for index, row in enumerate(body):
                result.append((([(lead, "dim")] if index == 0 else [(hang, None)]) + row, None))
            continue
        result.extend((row, None) for row in wrap_spans(card_spans(line.strip(), "soft"), width))
    return result


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
