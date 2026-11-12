"""Use the repo page's bounded read-only work source for one session's repo."""
import datetime
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


def latest_memory(cwd):
    """Spec E9 rule 37 (b): this repo's newest saved memory row from the journal Fleet reads
    (``memory.by_repo``'s grouping), and the journal path to watch; ``(None, [])`` without a cwd.
    A supersede names its replacement in the snippet; the replacement's headline is asked for."""
    if not cwd:
        return None, []
    row, path = memory.latest_saved(project_of(cwd))
    if row:
        snippet = str(row.get("snippet") or "")
        target = snippet[len("superseded_by="):].strip() if snippet.startswith("superseded_by=") else ""
        row["headline_id"] = target or row.get("id")
    return row, [path]


def open_count(work):
    """The ``N open`` number of a work list: the repo total, not the bounded list length."""
    return int(getattr(work, "total", len(work or ())))
