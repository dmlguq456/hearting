"""Use the repo page's bounded read-only work source for one session's repo."""
import datetime
from .collectors import memory
from .model import project_of


def observe(cwd):
    if not cwd:
        return [], []
    path = memory._db_path()
    repo = project_of(cwd)
    data = memory._read_project_memory(datetime.date.today().isoformat(), decisions=0, repo=repo)
    project = data.get("by_repo", {}).get(repo, {})
    # The same classification as the repo page: finished, observed and resolved records
    # are not open work, so the block count matches what Explorer shows.
    rows = [row for row in project.get("open") or () if memory.open_stage(row) is not None]
    return rows, [str(path), str(path) + "-wal"]
