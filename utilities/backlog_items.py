"""The one reading of ``<artifact root>/backlog.md``: its jobs and their optional lines (core/ARTIFACT_META.md ``backlog.md``).

The panel's BACKLOG part, its board (the popup) and the artifact entrance read the document through this module and no
other way, so the three count the same jobs and see the same kind, state, area, date and priority.  It reads lines it is given:
it opens no file, writes nothing, warns of nothing and refuses nothing.  A value that is wrong leaves that one cell
undecided (kind ``None``: drawn ``TBD``; state, area or date ``None``: drawn ``—`` or ``No area``; priority ``None``: the job is
not among the panel's chosen ones); the document still opens.

``parse(lines, detail=True)`` is ``{"title", "updated", "updated_text", "groups"}``: ``groups`` is ``[{"line", "heading", "jobs"}]``
in the document's order (``line`` the heading line, its blanks cut; ``heading`` the text after ``##``), and a job is::

    {"title": <the raw text after "- ">, "kind": "fix"|"task"|"note"|None, "state": "discuss"|"ready"|"waiting"|None,
     "area": <the area's name>|None, "area_key": <its folded name>|None, "date": "YYYY-MM-DD"|None,
     "priority": <a positive whole number>|None,
     "source": <first source: value>, "note": <first note: value>, "lines": [(name, value)], "has_detail": bool, "at": <order>}

``priority`` (11-26, panel B) is the order the person gave (1 first); a session writes it only where the person said the order.
``lines`` (kept only with ``detail``) are the job's indented lines in order, ``name`` the lower-case line name
(``kind``, ``area``, ``state``, ``date``, ``priority``, ``source``, ``note``) or ``None`` for a line of other text.
``counts(parsed)`` is the number of jobs of each kind and state and in all.
"""
import datetime
import re

KINDS = ("fix", "task", "note")
STATES = ("discuss", "ready", "waiting")
# The kind values read: the three, and the earlier `improve`, read as `task` (11-26, scheme A: what can be used and is to be made
# better is a task like any other).  Any other value is no kind.
KIND_VALUES = {"fix": "fix", "task": "task", "note": "note", "improve": "task"}
DETAIL_LINES = 12          # indented lines kept under one job
_NAMED = re.compile(r"^(kind|area|state|date|priority|source|note):(.*)$", re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}##(?:[ \t]|$)")
_TITLE = re.compile(r"^#[ \t]+(.*?)[ \t]*#*[ \t]*$")
_UPDATED = re.compile(r"^갱신 ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})$")
_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_RANK = re.compile(r"^[0-9]{1,6}$")


def area_key(value):
    """The area's folded name: case folded, runs of blanks one blank, the ends cut ('' for none)."""
    return " ".join(str(value).split()).casefold()


def _stamp(line):
    found = _UPDATED.match(line.strip())
    if not found:
        return None
    try:
        when = datetime.datetime.strptime(found.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (when.year, when.month, when.day, when.hour, when.minute), found.group(1)


def _day(value):
    if not _DAY.match(value):
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _rank(value):
    """A ``priority:`` value: a positive whole number of digits alone (``1``, ``12``), else None."""
    return int(value) if _RANK.match(value) and int(value) > 0 else None


def _one(values):
    """The one value a job's lines of a name gave, or None (none, or two different ones)."""
    return next(iter(values)) if len(values) == 1 else None


def parse(lines, detail=True):
    """The document's title, ``갱신`` time and jobs by the counting rule (core/ARTIFACT_META.md ``backlog.md``)."""
    title, updated, updated_text, groups = "", None, "", []
    group, job, fence, seen = None, None, None, {}
    count = 0

    def close():
        if job is None:
            return
        values = job.pop("_values")
        kinds = {KIND_VALUES.get(value, "?") for value in values["kind"]}
        job["kind"] = _one(kinds) if _one(kinds) in KINDS else None
        states = values["state"]
        job["state"] = _one(states) if _one(states) in STATES else None
        dates = {_day(value) for value in values["date"]}
        job["date"] = _one(dates)
        job["priority"] = _one({_rank(value) for value in values["priority"]})
        areas = {area_key(value): " ".join(value.split()) for value in values["area"]}
        key = _one(set(areas))
        if key:
            # (the first name a folded area was written with is the name its group goes by in the whole document)
            job["area"], job["area_key"] = seen.setdefault(key, areas[key]), key
    for raw in lines:
        raw = str(raw)
        opened = _FENCE.match(raw)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= fence[1] and not raw.strip().strip(fence[0]):
                fence = None
            continue
        if opened:
            fence = (opened.group(1)[0], len(opened.group(1)))
            close()
            job = None
            continue
        if _HEADING.match(raw):
            close()
            job = None
            group = {"line": raw.strip(), "heading": raw.strip()[2:].strip().rstrip("#").strip(), "jobs": []}
            groups.append(group)
            continue
        if group is None and updated is None:
            found = _stamp(raw)
            if found:
                updated, updated_text = found
        stripped = raw.strip()
        if not stripped:
            continue
        if raw[:1] in (" ", "\t"):
            if job is not None:
                job["has_detail"] = True
                named = _NAMED.match(stripped)
                name, value = (named.group(1).lower(), named.group(2).strip()) if named else (None, stripped)
                if name in ("kind", "state"):
                    job["_values"][name].add(value.lower())
                elif name in ("area", "date", "priority"):
                    job["_values"][name].add(value)
                elif name in ("source", "note") and not job[name]:
                    job[name] = value
                if detail and len(job["lines"]) < 4 * DETAIL_LINES:
                    job["lines"].append((name, value))
            continue
        close()
        job = None
        if group is None:
            found = _TITLE.match(stripped)
            if found and not title:
                title = found.group(1)
            continue
        if raw.startswith("- "):
            job = {"title": raw[2:], "kind": None, "state": None, "area": None, "area_key": None, "date": None, "priority": None,
                   "source": "", "note": "", "lines": [], "has_detail": False, "at": count,
                   "_values": {"kind": set(), "state": set(), "area": set(), "date": set(), "priority": set()}}
            count += 1
            group["jobs"].append(job)
    close()
    return {"title": title, "updated": updated, "updated_text": updated_text, "groups": groups}


def jobs(parsed):
    """Every job in the document's order, with the heading of its group: ``[(heading, job)]``."""
    return [(group["heading"], job) for group in parsed["groups"] for job in group["jobs"]]


def counts(parsed):
    """``{fix, task, note, tbd, discuss, ready, waiting, total}``: the jobs of each kind (``tbd``: no kind) and state, and all."""
    out = {name: 0 for name in KINDS + ("tbd",) + STATES + ("total",)}
    for _heading, job in jobs(parsed):
        out[job["kind"] or "tbd"] += 1
        if job["state"]:
            out[job["state"]] += 1
        out["total"] += 1
    return out
