"""Shared card format from capabilities/session-tidy.md; no operator configuration."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re


@lru_cache(maxsize=1)
def policy() -> dict:
    source = Path(__file__).resolve().parents[1] / "capabilities/session-tidy.md"
    text = source.read_text(encoding="utf-8")
    block = text.split("```json session-card-format\n", 1)[1].split("\n```", 1)[0]
    return json.loads(block)


def fields() -> list[dict]:
    rules = policy()
    return rules["core"] + rules["preserved"]


def select_module(*, role: str = "", capability: str = "", cwd: str = "") -> dict | None:
    """Use already confirmed context only, in the portable contract's order."""
    modules = policy()["modules"]
    for kind in policy()["selection_order"]:
        if kind == "confirmed_role":
            found = [m for m in modules if role.casefold() in
                     {r.casefold() for r in [m["id"], m["label"], *m["roles"]]}]
        elif kind == "main_route_capability":
            found = [m for m in modules if capability in m["capabilities"]]
        else:
            parts = set(Path(cwd).parts) if cwd else set()
            found = [m for m in modules if parts.intersection(m["folders"])]
        if found:
            return found[0] if len(found) == 1 else None
    return None


def writing_guide(*, role: str = "", capability: str = "", cwd: str = "") -> str:
    """One existing writer response chooses from its known conversation context."""
    rules = policy()
    # Folder evidence must not hide a stronger role held in the writer's context.
    confirmed = select_module(role=role, capability=capability)
    options = [confirmed] if confirmed else rules["modules"]
    folder = select_module(cwd=cwd) if not confirmed else None
    hint = f"Work-folder hint only: {folder['id']}. " if folder else ""
    modules = "; ".join(f'{m["id"]}: {m["keep"]}' for m in options)
    order = " > ".join(key.replace("_", " ") for key in rules["selection_order"])
    return (f"Core first, then only one module selected by {order}; "
            "if unknown, core only. No question or classification call. "
            f"{hint}Modules ({modules}). Preserve dated quotes and PRD verbatim after the module. "
            "Keep unique restrictions and unresolved obligations; replace completed chronology "
            "with result/history references. Read the exact history source when a field refers to it. ")


def complete_lines(text: str, limit: int) -> str:
    """Keep a prefix of whole lines; an oversized first line is not a safe prefix."""
    out = []
    used = 0
    for line in text.splitlines(keepends=True):
        size = len(line.encode("utf-8"))
        if used + size > limit:
            break
        out.append(line)
        used += size
    return "".join(out).rstrip("\r\n")


def _sections(text: str) -> list[tuple[str, str, str]]:
    rules = policy()
    entries = rules["core"] + rules["preserved"] + rules["legacy"] + rules["modules"]
    labels = {name.casefold(): f["id"] for f in entries
              for name in [f["label"], f["id"], *f.get("aliases", [])]}
    names = sorted(labels, key=len, reverse=True)
    pattern = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:\d+[.)]\s*)?(" +
                         "|".join(re.escape(n) for n in names) +
                         r")(?:\*\*)?(?:\s*[:：]\s*(.*)|\s*)$", re.I)
    result: list[tuple[str, str, str]] = []
    key, title, lines = "preamble", "", []
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
            lines.append(line)
            continue
        if fence:
            lines.append(line)
            continue
        match = pattern.match(line)
        heading = re.match(r"^#{1,2}\s+(.+)$", line)
        if match or heading:
            if lines or title:
                result.append((key, title, "\n".join(lines).strip("\n")))
            if match:
                title = match[1]
                key = labels[title.casefold()]
                lines = [match[2]] if match[2] else []
            else:
                key, title, lines = "extra", heading[1], []
        else:
            lines.append(line)
    if lines or title:
        result.append((key, title, "\n".join(lines).strip("\n")))
    return result


def bounded_body(text: str, limit: int, source: str = "") -> str:
    """Preserve core slots and complete lines, referencing the unabridged history."""
    if len(text.encode("utf-8")) <= limit:
        return text
    rules = policy()
    sections = _sections(text)
    korean = any("가" <= c <= "힣" for c in text)
    known = {f["id"]: f["label"] if korean else f["aliases"][0] for f in fields()}
    required = list(rules["core"])
    required += [f for f in rules["preserved"] if any(s[0] == f["id"] for s in sections)]
    required_ids = {f["id"] for f in required}
    ordered = []
    # Preserve every core slot. A missing value is not an inferred fact.
    for f in required:
        bodies = [body for key, _, body in sections if key == f["id"]]
        ordered.append((f["id"], known[f["id"]], "\n".join(bodies), bool(bodies)))
    # Modules immediately follow the core, with preserved excerpts following modules.
    core_count = len(rules["core"])
    extras = [(key, title, body, True) for key, title, body in sections if key not in required_ids]
    module_ids = {m["id"] for m in rules["modules"]}
    modules = []
    for key in dict.fromkeys(s[0] for s in extras if s[0] in module_ids):
        repeated = [s for s in extras if s[0] == key]
        modules.append((key, repeated[0][1], "\n".join(s[2] for s in repeated), True))
    known.update({key: title for key, title, _, _ in modules})
    required_ids.update(key for key, _, _, _ in modules)
    ordered[core_count:core_count] = modules
    ordered += [s for s in extras if s[0] not in module_ids]
    note = "원문 참조" if korean else "Full source"
    missing = "미기록" if korean else "Not recorded"
    footer = f"\n\n{note}: {source}" if source else f"\n\n{note}: card-history/"
    rendered = []
    for key, title, body, present in ordered:
        if key in required_ids:
            rendered.append(f"## {known[key]}\n{note}" if present else f"## {known[key]}\n{missing}")
        else:
            rendered.append("")
    def joined() -> str:
        return "\n\n".join(s for s in rendered if s) + footer
    # Source paths are never shortened. Storage uses the normal state path, so
    # even the fixed skeleton fits comfortably in the existing 8 KiB allowance.
    if len(joined().encode("utf-8")) > limit:
        return complete_lines(text, limit)
    for i, (key, title, body, present) in enumerate(ordered):
        if not present:
            continue
        head = f"## {known.get(key, title)}\n" if title or key in known else ""
        previous = rendered[i]
        candidate = head + body
        rendered[i] = candidate
        if len(joined().encode("utf-8")) <= limit:
            continue
        rendered[i] = previous
        # A required slot reserves its reference while optional sections are
        # omitted unless at least one complete content line can fit.
        room = limit - len(joined().encode("utf-8")) + len(previous.encode("utf-8"))
        room -= len((head + "\n" + note).encode("utf-8"))
        prefix = complete_lines(body, max(0, room))
        if prefix:
            rendered[i] = head + prefix + "\n" + note
            if len(joined().encode("utf-8")) > limit:
                rendered[i] = previous
    return joined()
