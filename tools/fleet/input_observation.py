"""Optional dependency recording for Fleet's passive parser generations.

Keep the standalone route/session readers usable with only tools on sys.path.
The detail worker loads the shared observer before opening its explicit scope.
Outside that scope these are ordinary filesystem observations.
"""
import os
import sys


def observe(path, info=None, *, link=False):
    observer = sys.modules.get("runtime_observation")
    if observer is not None:
        observer.observe_input(path, info, link=link)


def stat(path, *, follow_symlinks=True):
    try:
        info = os.stat(path, follow_symlinks=follow_symlinks)
    except OSError:
        observe(path)
        raise
    observe(path, info, link=not follow_symlinks)
    return info


def lstat(path):
    return stat(path, follow_symlinks=False)


def path_call(path, method, *args, **kwargs):
    observer = sys.modules.get("runtime_observation")
    if observer is not None:
        return observer.input_path_call(path, method, *args, **kwargs)
    return getattr(path, method)(*args, **kwargs)


def exists(path):
    observe(path)
    return os.path.exists(path)


def isfile(path):
    observe(path)
    return os.path.isfile(path)


def isdir(path):
    observe(path)
    return os.path.isdir(path)


def clock_key(row):
    return tuple(getattr(row, field, None) for field in (
        "harness", "session_id", "attempt_id", "pid", "proc_start", "cwd", "source"))


def clock(row, boundary, now):
    observer = sys.modules.get("runtime_observation")
    if observer is not None:
        observer.observe_clock(clock_key(row), boundary,
                               getattr(row, "elapsed_min", None) or 0, now)


def deadline(boundary, now):
    observer = sys.modules.get("runtime_observation")
    if observer is not None:
        observer.observe_clock(None, boundary, now=now)
