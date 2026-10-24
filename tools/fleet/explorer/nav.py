"""Small read-only session helpers shared by the live snapshot adapter."""

import re
from collections.abc import Mapping


def _get(value, *names, default=None):
    for name in names:
        item = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if item is not None and item != "":
            return item
    return default


def is_main(session):
    return not _get(session, "is_child") and _get(session, "process_role") != "registered-worker"


def find_session(snapshot, sid):
    for session in _get(snapshot, "sessions", default=()) or ():
        ids = [_get(session, "session_id", "sid", "id")]
        ids.extend(_get(session, "session_aliases", default=()) or ())
        if sid and sid in ids:
            return session
    return None


def card_title(lines):
    """What a handover card says first: its ``# `` heading, else the first line that says something
    (past bare labels like ``진행 중:`` and list marks)."""
    lines = [str(line) for line in lines or ()]
    head = next((line for line in lines if line.startswith("# ")), None)
    if head:
        if "세션 인계 카드" in head:         # every card opens with this heading; its first in-progress item says more
            in_progress = False
            for line in lines:
                if line.startswith("#"):
                    in_progress = "진행 중" in line
                elif in_progress and re.match(r"^[-*]\s+\S", line):
                    text = line.replace("**", "")[1:].strip()
                    if len(text) > 8:
                        return text
        return head[2:].strip()
    for line in lines:
        text = re.sub(r"^[-*\s]*(진행 중\s*:?)?\s*", "", line.replace("**", "")).strip()
        if len(text) > 8:
            return text
    return ""


def generation_chain(entries, harness, sid):
    """Generations of a seat, oldest first: one per session-tidy card, then the running session (``now``).

    A generation follows the one before it as the same session (a compact: ``cont``) or a new session
    (``new``; a clear when its start source says so). A running session that took over by a retire
    handover is ``existing`` when the seat records say its start source was a retire.
    """
    entries = tuple(entries or ())
    items = []
    for entry in entries:
        for card in entry.get("cards") or ():
            items.append({"harness": entry.get("harness"), "sid": entry.get("sid"), "card": card,
                          "entry": entry, "now": False})
    running = next((entry for entry in entries if entry.get("current")), None)
    items.append({"harness": harness, "sid": sid, "card": None, "entry": running, "now": True})
    for index, item in enumerate(items):
        if index == 0:
            item["kind"], item["connection"] = "new", None
            continue
        previous = items[index - 1]
        if str(item["sid"] or "")[:8] == str(previous["sid"] or "")[:8]:
            item["kind"], item["connection"] = "cont", "compact · same session went on"
        else:
            source = (item["entry"] or {}).get("source")
            item["kind"] = "existing" if source == "retire" else "new"
            item["connection"] = ("clear · new session took over" if source == "clear" else
                                  "retire · a running session took over" if source == "retire"
                                  else "new session · no handover record")
        if item["harness"] != previous["harness"]:
            item["connection"] += f"  ·  {previous['harness']} → {item['harness']}"
    return items


def default_generation(chain):
    """The generation a seat opens on: the newest one that has a card, else the running one."""
    for index in range(len(chain) - 1, -1, -1):
        card = chain[index].get("card")
        if card and card.get("status") == "available":
            return index
    return len(chain) - 1
