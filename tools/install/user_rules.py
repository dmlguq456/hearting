"""The user's own global rules, carried into OpenCode as one marked block.

OpenCode has no user-owned global rule file: it auto-loads only
``<config>/AGENTS.md``, which Hearting owns as its bootstrap. The user's global
rules live in Claude's user rule folder (``$CLAUDE_CONFIG_DIR/rules``, default
``~/.claude/rules``). When that folder holds rules, activation writes the
OpenCode ``AGENTS.md`` as the rules block followed by the bootstrap; without
rules the file stays the plain bootstrap link. The block holds each rule's text
byte for byte. Rules scoped to paths by front matter are left out, because
OpenCode would apply them everywhere. Standard library only, so worker homes
can load this file by path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BEGIN = b"<!-- hearting:user-rules begin"
END = b"<!-- hearting:user-rules end -->"
_SCOPED = re.compile(rb"\A---\r?\n(?:(?!---\r?\n).*\r?\n)*?paths\s*:", re.M)


def rule_files(claude_home: Path) -> list[tuple[str, bytes]]:
    """Unscoped ``*.md`` rules under ``claude_home/rules``, sorted by relative path."""
    root = Path(claude_home) / "rules"
    found = []
    if not root.is_dir():
        return found
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if not name.endswith(".md") or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if data.strip() and not _SCOPED.match(data):
                found.append((path.relative_to(root).as_posix(), data))
    return sorted(found)


def block(claude_home: Path) -> bytes:
    """The marked rules block, or empty bytes when there is no rule to carry."""
    rules = rule_files(claude_home)
    if not rules:
        return b""
    begin = (BEGIN + b" (the user's own rules from " + str(Path(claude_home) / "rules").encode()
             + b"; managed by Hearting: install and update refresh this block, uninstall removes it) -->\n")
    body = b"\n".join(data if data.endswith(b"\n") else data + b"\n" for _name, data in rules)
    return begin + body + END + b"\n\n"


def strip(data: bytes) -> bytes:
    """``data`` without a leading rules block; other input is returned unchanged."""
    if not data.startswith(BEGIN):
        return data
    end = data.find(END)
    if end < 0:
        return data
    rest = data[end + len(END):]
    return rest[2:] if rest.startswith(b"\n\n") else rest.lstrip(b"\n")
