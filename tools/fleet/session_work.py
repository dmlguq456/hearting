"""Use the repo page's bounded read-only work source for one session's repo."""
import datetime
from .collectors import memory
from .model import project_of


def observe(cwd):
    if not cwd:
        return [], []
    path = memory._db_path()
    repo = project_of(cwd)
    data = memory._read_project_memory(datetime.date.today().isoformat(), decisions=0, open_limit=32, repo=repo)
    project = data.get("by_repo", {}).get(repo, {})
    return list(project.get("open") or ()), [str(path), str(path) + "-wal"]
