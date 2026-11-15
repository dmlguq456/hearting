"""Use the repo page's bounded read-only work source for one session's repo."""
import datetime
import re
from .collectors import memory
from .model import project_of


class WorkRows(list):
    """The newest open rows (bounded) and ``total``, every open record of the repo, and the
    stored headlines of the journal ids the caller asked about (``headlines``)."""

    def __init__(self, rows=(), total=None, headlines=None):
        super().__init__(rows)
        self.total = len(self) if total is None else max(len(self), int(total))
        self.headlines = dict(headlines or {})


def observe(cwd, journal_ids=()):
    if not cwd:
        return WorkRows(), []
    path = memory._db_path()
    repo = project_of(cwd)
    # Parity register rows stay on the Fleet screen; they are not every session's task.
    # ``journal_ids`` ride the same read-only query (the latest saved record's headline).
    data = memory._read_project_memory(datetime.date.today().isoformat(), tuple(journal_ids), decisions=0,
                                       repo=repo, parity=False)
    project = data.get("by_repo", {}).get(repo, {})
    # The collector applies the repo page's classification before its display cut, so
    # finished records never count and never hide open work behind the cut.
    rows = [row for row in project.get("open") or () if memory.open_stage(row) is not None]
    return WorkRows(rows, project.get("open_total"), data.get("headlines")), [str(path), str(path) + "-wal"]


_ROUTE_ID = re.compile(r"(?<![A-Za-z0-9])rt-[0-9a-f]{16}(?![A-Za-z0-9])")


def route_ids(row):
    """The route ids a record names in its headline, links or artifact refs."""
    row = row if isinstance(row, dict) else {}
    texts = [str(row.get("headline") or ""), *map(str, row.get("links") or ()),
             *map(str, row.get("artifact_refs") or ())]
    return {found for text in texts for found in _ROUTE_ID.findall(text)}


def delegated(rows, running_routes):
    """Rows whose work sits wholly with routes that run now (a hand-off, not this session's
    remaining work): ``running_routes(ids)`` names which of the ids are this session's running
    routes. A record naming a route that is not running (a finished route's unreceived result,
    another session's route), or no route at all, stays open work."""
    named = {id(row): route_ids(row) for row in rows}
    candidates = set().union(*named.values()) if named else set()
    live = set(running_routes(candidates)) if candidates else set()
    return [row for row in rows if named[id(row)] and named[id(row)] <= live]


def recent_memory(cwd, limit=2):
    """Spec E9 rules 37 (b), 46 (c): this repo's and the global newest saved memory records
    (11-15, panel polish 5: both, newest first) from the journal Fleet reads (``memory.by_repo``'s
    grouping and the global group of the same read), ``limit`` at most, and the journal path to
    watch; ``([], [])`` without a cwd. A supersede names its replacement in the snippet; the
    replacement's headline is asked for, and the replacement's own add right beside it names the
    same record, so it is shown once; the retired record's own older add is not shown. A global
    record written from this repo is in both groups and is shown once."""
    if not cwd:
        return [], []
    own, path = memory.recent_saved(project_of(cwd))
    everywhere, _path = memory.recent_saved_global()
    rows, listed = [], set()
    for row in sorted(own + everywhere, key=lambda row: str(row.get("ts") or ""), reverse=True):
        mark = (row.get("id"), row.get("action"), row.get("ts"))
        if mark not in listed:
            listed.add(mark)
            rows.append(row)
    out, seen = [], set()
    for row in rows:
        snippet = str(row.get("snippet") or "")
        target = snippet[len("superseded_by="):].strip() if snippet.startswith("superseded_by=") else ""
        row["headline_id"] = target or row.get("id")
        retired = row["headline_id"] in seen or (target and row.get("id") in seen)
        seen.update({row["headline_id"], row.get("id")} if target else {row["headline_id"]})
        if retired:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out, [path]


def latest_memory(cwd):
    """The newest of ``recent_memory`` (spec E9 rule 37 (b)), or None, and the journal path."""
    if not cwd:
        return None, []
    rows, paths = recent_memory(cwd, 1)
    return (rows[0] if rows else None), paths


def open_count(work):
    """The ``N open`` number of a work list: the repo total, not the bounded list length."""
    return int(getattr(work, "total", len(work or ())))
