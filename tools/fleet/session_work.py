"""Use the repo page's bounded read-only work source for one session's repo."""
import datetime
from .collectors import memory
from .model import project_of


class WorkRows(list):
    """The newest open rows (bounded) and ``total``, every open record of the repo."""

    def __init__(self, rows=(), total=None):
        super().__init__(rows)
        self.total = len(self) if total is None else max(len(self), int(total))


def observe(cwd):
    if not cwd:
        return WorkRows(), []
    path = memory._db_path()
    repo = project_of(cwd)
    # Parity register rows stay on the Fleet screen; they are not every session's task.
    data = memory._read_project_memory(datetime.date.today().isoformat(), decisions=0, repo=repo,
                                       parity=False)
    project = data.get("by_repo", {}).get(repo, {})
    # The collector applies the repo page's classification before its display cut, so
    # finished records never count and never hide open work behind the cut.
    rows = [row for row in project.get("open") or () if memory.open_stage(row) is not None]
    return WorkRows(rows, project.get("open_total")), [str(path), str(path) + "-wal"]


def open_count(work):
    """The ``N open`` number of a work list: the repo total, not the bounded list length."""
    return int(getattr(work, "total", len(work or ())))
