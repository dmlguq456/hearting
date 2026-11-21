"""The model-follow facts the MODELS view draws, taken only from the answer of ``model_follow.fleet_view()``.

``normalize`` keeps the words of that answer a screen needs and drops the rest (a shape it does not
know gives ``None``: nothing about following is drawn). ``Follow`` reads the normalized answer against
the profile table and the day on the explorer's clock. The end-of-line records of the table are never
read here: the state, the dates and the model before and after all come from the answer.
"""
import datetime
import re
from collections.abc import Mapping

HARNESSES = ("claude", "codex", "opencode")
STATES = ("follow", "keep", "held")
RECENT_DAYS = 3                     # a followed cell shows as recent for this many days after its record
GROUPS = ("followed", "next", "held", "proposal")
GLYPH = {"followed": "^", "next": ">", "held": "!", "proposal": "?"}
MARK = {"followed": "^", "keep": "=", "held": "!"}
LEGEND = (("^", "Followed"), ("=", "Keep"), ("!", "Held"))
_CLI_NAME = {"claude": "Claude Code", "codex": "Codex"}


def _text(value):
    return value if isinstance(value, str) and value else None


def _seq(value):
    return isinstance(value, (list, tuple))


def _cell(item):
    if not isinstance(item, Mapping):
        return None
    target, model, state = _text(item.get("target")), _text(item.get("model")), item.get("state")
    if not target or not model or state not in STATES:
        return None
    since = item.get("since")
    if isinstance(since, Mapping) and _text(since.get("date")):
        since = {"date": since["date"], "from": _text(since.get("from")), "to": _text(since.get("to"))}
    else:
        since = None
    following = item.get("next")
    if isinstance(following, Mapping) and _text(following.get("model")) and _text(following.get("branch")):
        following = {"model": following["model"], "effort": _text(following.get("effort")),
                     "branch": following["branch"], "reason": _text(following.get("reason"))}
    else:
        following = None
    lines = [{"key": line["key"], "from": str(line.get("from") or ""), "to": str(line.get("to") or "")}
             for line in (item.get("lines") or ()) if isinstance(line, Mapping) and _text(line.get("key"))]
    return {"target": target, "model": model, "effort": _text(item.get("effort")), "state": state,
            "why": _text(item.get("why")) or "", "since": since, "held": _text(item.get("held")),
            "next": following, "lines": lines}


def _note(item):
    if not isinstance(item, Mapping) or item.get("harness") not in HARNESSES:
        return None
    kind, models = item.get("kind"), item.get("models")
    if kind not in ("previous-generation", "min-version") or not _seq(models):
        return None
    if kind == "previous-generation":
        models = [str(model) for model in models if _text(model)]
    else:
        models = [[str(pair[0]), str(pair[1])] for pair in models
                  if _seq(pair) and len(pair) == 2 and _text(pair[0]) and _text(pair[1])]
    version = _text(item.get("version"))
    if not models or not version:
        return None
    return {"harness": item["harness"], "version": version, "models": models, "kind": kind,
            "command": _text(item.get("command"))}


def normalize(answer, unlisted=()):
    """``{"cells": {harness: [cell]}, "cli": [note]}`` from ``fleet_view()``'s answer, or None.

    ``unlisted`` holds ``(harness, model)`` pairs the harness's model list does not hold: such a cell
    (a Claude alias, a model the list does not know) has nothing to follow, so one with no record and
    no newer model is left out.
    """
    if not _seq(answer):
        return None
    cells, notes = {}, []
    for item in answer:
        if (not isinstance(item, Mapping) or item.get("harness") not in HARNESSES
                or not _seq(item.get("cells"))):
            return None
        harness = item["harness"]
        found = [cell for cell in map(_cell, item["cells"]) if cell is not None]
        cells[harness] = [cell for cell in found
                          if cell["since"] or cell["next"] or (harness, cell["model"]) not in unlisted]
        note = _note(item.get("cli"))
        if note:
            notes.append(note)
    return {"cells": cells, "cli": notes}


def model_names(follow):
    """Every model id a normalized answer mentions (the view needs a shown name for each)."""
    names = set()
    for cells in (follow or {}).get("cells", {}).values():
        for cell in cells:
            names.update(name for name in (cell["model"], cell["held"]) if name)
            if cell["since"]:
                names.update(name for name in (cell["since"]["from"], cell["since"]["to"]) if name)
            if cell["next"]:
                names.add(cell["next"]["model"])
    for note in (follow or {}).get("cli", ()):
        names.update(item if isinstance(item, str) else item[0] for item in note["models"])
    return names


def today_of(clock):
    """The local day of the explorer's clock (epoch seconds), or None when it carries none."""
    if isinstance(clock, bool) or not isinstance(clock, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(clock).date()
    except (OverflowError, OSError, ValueError):
        return None


def undo_request(cell):
    """``<model before>@<effort>`` that puts a followed cell back, or None when it is not one.

    The answer carries the model before and the cell's effort now; an effort the follow had to
    lower is not in it, so the cell goes back at the effort it has now."""
    record = cell.get("since") if cell["state"] == "follow" and cell["why"] == "followed" else None
    if not record or not record["from"]:
        return None
    return record["from"] + (f"@{cell['effort']}" if cell["effort"] else "")


def _day(text):
    try:
        return datetime.date.fromisoformat(str(text))
    except ValueError:
        return None


def _diff(before, after):
    old, new = before.split(), after.split()
    if len(old) == len(new):
        return ", ".join(f"{a} -> {b}" for a, b in zip(old, new) if a != b)
    return ", ".join([f"-{t}" for t in old if t not in new] + [f"+{t}" for t in new if t not in old])


def line_label(key):
    """The name a companion line goes by on the screen (the words ``hearting update`` uses)."""
    cascade = re.fullmatch(r"CFG_TIER_([A-Z0-9_]+)_FAILOVER_CASCADE", key)
    if cascade:
        return "cascade/" + cascade.group(1).lower().replace("_", "-")
    return {"CFG_V2_AUTO_FALLBACKS": "v2-auto-fallbacks",
            "CFG_MAIN_SESSION_ONLY_MODELS": "main-session-only"}.get(key, key.removeprefix("CFG_").lower())


class Follow:
    """What the screen shows about following, for one normalized answer, one table and one day."""

    def __init__(self, follow, targets, today):
        self.today = today if isinstance(today, datetime.date) else None
        self._cells = {harness: {cell["target"]: cell for cell in cells}
                       for harness, cells in (follow or {}).get("cells", {}).items()}
        self._targets = targets if isinstance(targets, Mapping) else {}
        self.cli = tuple((follow or {}).get("cli", ()))

    @classmethod
    def read(cls, models, clock):
        """The view of ``models["follow"]`` on the day of ``clock``; None when there is none to draw."""
        follow = models.get("follow") if isinstance(models, Mapping) else None
        if not isinstance(follow, Mapping) or not isinstance(follow.get("cells"), Mapping):
            return None
        return cls(follow, models.get("profile_target"), today_of(clock))

    # -- the table's cells ---------------------------------------------------------------

    def target(self, harness, profile):
        """The core cell a table cell gets its value from: ``profile/<name>`` or ``tier/<name>``."""
        mine = self._targets.get(harness)
        named = mine.get(profile) if isinstance(mine, Mapping) else None
        return named or f"profile/{profile}"

    def cell(self, harness, profile):
        return self._cells.get(harness, {}).get(self.target(harness, profile))

    def recent(self, cell):
        """A followed cell whose record is at most ``RECENT_DAYS`` days old (an unreadable date is not)."""
        if cell["state"] != "follow" or cell["why"] != "followed" or not cell["since"] or self.today is None:
            return False
        day = _day(cell["since"]["date"])
        return day is not None and 0 <= (self.today - day).days <= RECENT_DAYS

    def mark(self, harness, profile):
        """``!`` held, ``=`` kept, ``^`` followed recently, else ``""`` (one of them: held first)."""
        cell = self.cell(harness, profile)
        if cell is None:
            return ""
        if cell["state"] == "held":
            return "!"
        if cell["state"] == "keep":
            return "="
        return "^" if self.recent(cell) else ""

    def profile_of(self, harness, target, profiles):
        """The first table profile whose cell is ``target``, or None (a line the table does not show)."""
        return next((profile for profile in profiles if self.target(harness, profile) == target), None)

    # -- the lines under the table -------------------------------------------------------

    def _all(self):
        for harness in HARNESSES:
            for cell in self._cells.get(harness, {}).values():
                yield harness, cell

    def groups(self, profiles):
        """``[(name, [row])]`` in the order followed, next, held, proposal; a group with no row is absent.

        A row is ``{"harness", "target", "cell", "text", "profile"}`` where ``text`` is what an opened
        group lists for it and ``profile`` the table profile it selects (None when the table has no
        cell for it). A cell the user keeps makes no row of its own.
        """
        found = {name: [] for name in GROUPS}
        for harness, cell in self._all():
            if cell["state"] == "keep":
                continue
            row = {"harness": harness, "target": cell["target"], "cell": cell,
                   "profile": self.profile_of(harness, cell["target"], profiles)}
            effort = f"@{cell['effort']}" if cell["effort"] else ""
            if self.recent(cell):
                before = cell["since"]["from"] or "?"
                found["followed"].append({**row, "text": f"{harness} {cell['target']}: {before}{effort} -> "
                                                         f"{cell['model']}{effort}"})
            following = cell["next"]
            if following and following["branch"] == "will-follow":
                after = f"@{following['effort']}" if following["effort"] else ""
                found["next"].append({**row, "text": f"{harness} {cell['target']}: {cell['model']}{effort} -> "
                                                     f"{following['model']}{after}"})
            if cell["state"] == "held":
                refused = cell["held"] or (cell["since"] or {}).get("to") or "?"
                found["held"].append({**row, "text": f"{harness} {cell['target']}: {refused} rejected · "
                                                     f"back on {cell['model']}{effort}"})
            if following and following["branch"] == "proposal":
                why = f" ({following['reason']})" if following["reason"] else ""
                found["proposal"].append({**row, "text": f"{harness} {cell['target']}: {cell['model']} -> "
                                                         f"{following['model']}{why}"})
        return [(name, found[name]) for name in GROUPS if found[name]]

    def linked(self, rows):
        """``[text]`` the companion lines the given rows' cells move with them, one per line, merged."""
        merged = {}
        for row in rows:
            for line in row["cell"]["lines"]:
                pieces = merged.setdefault(line_label(line["key"]), [])
                for piece in _diff(line["from"], line["to"]).split(", "):
                    if piece and piece not in pieces:
                        pieces.append(piece)
        return [f"linked {label}: {', '.join(pieces)}" for label, pieces in merged.items() if pieces]

    def cli_lines(self):
        """The CLI-too-old notes in the words ``hearting update`` prints, without their head."""
        out = []
        for note in self.cli:
            how = note["command"] or f"update the {_CLI_NAME.get(note['harness'], note['harness'])} CLI yourself"
            if note["kind"] == "previous-generation":
                out.append(f"{note['harness']} CLI {note['version']} lists {', '.join(note['models'])} "
                           f"as previous generation with no successor · {how}")
            else:
                needs = ", ".join(f"{model} (needs {version})" for model, version in note["models"][:3])
                more = len(note["models"]) - 3
                out.append(f"{note['harness']} CLI {note['version']} is older than {needs}"
                           f"{f' and {more} more' if more > 0 else ''} · {how}")
        return out
