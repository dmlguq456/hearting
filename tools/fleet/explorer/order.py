"""Stable, atomic ordering state for explorer repositories and sessions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def order_path():
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "hearting" / "fleet2-order.json"


def _move(values, source, target):
    values = list(values)
    if source not in values:
        return values
    values.remove(source)
    if target in values:
        values.insert(values.index(target), source)
    else:
        values.append(source)
    return values


def move_repo(order, source, target=None):
    """Return a repo order with source inserted before target (or at the end)."""
    result = dict(order or {})
    result["repos"] = _move(result.get("repos", ()), source, target)
    return result


def move_session(order, repo, source, target=None):
    """Return an in-repo session order with source inserted before target."""
    result = dict(order or {})
    sessions = {key: list(value) for key, value in (result.get("sessions") or {}).items()}
    sessions[str(repo)] = _move(sessions.get(str(repo), ()), source, target)
    result["sessions"] = sessions
    return result


def reconcile_order(saved, repos, sessions):
    """Prune ended identities and append new identities in current natural order."""
    saved = saved if isinstance(saved, dict) else {}
    repo_ids = [str(item.get("id", item.get("name", ""))) for item in repos]
    repo_ids = list(dict.fromkeys(repo_ids))
    repo_order = [key for key in saved.get("repos", ()) if key in repo_ids]
    repo_order += [key for key in repo_ids if key not in repo_order]
    current = {}
    for session in sessions:
        repo = str(session.get("repo_id", session.get("repo", "")))
        key = str(session.get("order_key", ""))
        if key:
            current.setdefault(repo, []).append(key)
    stored = saved.get("sessions", {})
    session_order = {}
    for repo, keys in current.items():
        prior = stored.get(repo, ()) if isinstance(stored, dict) else ()
        ordered = [key for key in prior if key in keys]
        ordered += [key for key in keys if key not in ordered]
        session_order[repo] = ordered
    ordered_repos = [next(item for item in repos
                          if str(item.get("id", item.get("name", ""))) == repo)
                     for repo in repo_order]
    session_by_repo = {repo: [next(row for row in sessions if
                                   str(row.get("repo_id", row.get("repo", ""))) == repo and
                                   str(row.get("order_key", "")) == key)
                              for key in keys]
                       for repo, keys in session_order.items()}
    ordered_sessions = [row for repo in repo_order for row in session_by_repo.get(repo, ())]
    return {"repos": ordered_repos, "sessions": ordered_sessions,
            "repo_order": repo_order, "session_order": session_order}


def load_order(path=None):
    try:
        value = json.loads(Path(path or order_path()).read_text(encoding="utf-8"))
        repos = value.get("repos", ()) if isinstance(value, dict) else None
        sessions = value.get("sessions", {}) if isinstance(value, dict) else None
        if (isinstance(value, dict) and value.get("schema") == 1
                and isinstance(repos, list) and isinstance(sessions, dict)
                and all(isinstance(items, list) for items in sessions.values())):
            return {"repos": [str(item) for item in repos if isinstance(item, str)],
                    "sessions": {str(key): [str(item) for item in items if isinstance(item, str)]
                                 for key, items in sessions.items()}}
    except (OSError, ValueError, TypeError):
        pass
    return {"repos": [], "sessions": {}}


def save_order(order, path=None):
    target = Path(path or order_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": 1, "repos": list(order.get("repos", ())),
            "sessions": {str(key): list(items) for key, items in
                         (order.get("sessions") or {}).items()}}
    fd, name = tempfile.mkstemp(prefix=".fleet2-order-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
