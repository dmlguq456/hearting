"""The user's own global rules, carried into OpenCode as one marked block.

OpenCode has no user-owned global rule file: it auto-loads only
``<config>/AGENTS.md``, which Hearting owns as its bootstrap. The user's global
rules live in Claude's user rule folder (``$CLAUDE_CONFIG_DIR/rules``, default
``~/.claude/rules``). When that folder holds rules, activation writes the
OpenCode ``AGENTS.md`` as the rules block followed by the bootstrap; without
rules the file stays the plain bootstrap link. The block holds each rule's text
byte for byte. Rules scoped to paths by front matter are left out, because
OpenCode would apply them everywhere, and a file that is not UTF-8 text is
skipped and reported. The carry is on unless the user sets
``user_rules: opencode: off`` in ``~/.config/hearting/dispatch-defaults.yaml``;
off renders nothing, so activation returns the plain link. Standard library
only, so worker homes can load this file by path.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

BEGIN = b"<!-- hearting:user-rules begin"
END = b"<!-- hearting:user-rules end -->"
# The header names no path, so the bytes do not depend on which config dir ran.
HEADER = (BEGIN + b" (Hearting writes this whole file: the user's own rules from Claude's"
          b" user rule folder, $CLAUDE_CONFIG_DIR/rules or ~/.claude/rules, then the"
          b" Hearting bootstrap. Edit the rules there; install and update rewrite this"
          b" file and uninstall removes it. To stop carrying them, set user_rules:"
          b" opencode: off in ~/.config/hearting/dispatch-defaults.yaml.) -->\n")
_SCOPED = re.compile(rb"\A(?:\xef\xbb\xbf)?---\r?\n(?:(?!---\r?\n).*\r?\n)*?paths\s*:", re.M)


def collect(claude_home: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Unscoped UTF-8 ``*.md`` rules under ``claude_home/rules`` and skip notes."""
    root = Path(claude_home) / "rules"
    found, skipped = [], []
    if not root.is_dir():
        return found, skipped
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if not name.endswith(".md") or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                data = path.read_bytes()
                data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{relative} is not UTF-8 text")
                continue
            except OSError as exc:
                skipped.append(f"{relative} is unreadable ({type(exc).__name__})")
                continue
            if data.strip() and not _SCOPED.match(data):
                found.append((relative, data))
    return sorted(found), sorted(skipped)


def carry_enabled(environ=None) -> bool:
    """The user's ``user_rules.opencode`` switch; absent means on."""
    path = Path(__file__).resolve().parents[2] / "utilities" / "dispatch-defaults.py"
    spec = importlib.util.spec_from_file_location("hearting_user_rules_defaults", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_user_rules_opencode(module.default_config_path(environ)) == "carry"


def block(claude_home: Path, environ=None) -> bytes:
    """The marked rules block, or empty bytes when there is no rule to carry."""
    if not carry_enabled(environ):
        return b""
    rules = collect(claude_home)[0]
    if not rules:
        return b""
    body = b"\n".join(data if data.endswith(b"\n") else data + b"\n" for _name, data in rules)
    return HEADER + body + END + b"\n\n"
