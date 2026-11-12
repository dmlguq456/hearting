"""One collection pass as a fresh observation shared by its readers.

Fleet's basic and detail passes ask the same questions many times (the
process fd table, the realpath of a project directory). Inside one pass the
first answer is that pass's fresh observation; the next pass asks again.
Scopes are thread-local: a reader on another thread observes for itself.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import posixpath
import threading

_LOCAL = threading.local()


@contextmanager
def collection_pass():
    outer = getattr(_LOCAL, "memo", None)
    if outer is None:
        _LOCAL.memo = {}
    try:
        yield
    finally:
        if outer is None:
            _LOCAL.memo = None


def active():
    return getattr(_LOCAL, "memo", None) is not None


def cached(key, producer):
    """``producer()`` once per pass for ``key``; outside a pass, every call is fresh."""
    memo = getattr(_LOCAL, "memo", None)
    if memo is None:
        return producer()
    try:
        return memo[key]
    except KeyError:
        value = memo[key] = producer()
        return value


_REALPATH = posixpath.realpath


def _pass_realpath(filename, *, strict=False):
    memo = getattr(_LOCAL, "memo", None)
    if memo is None:
        return _REALPATH(filename, strict=strict)
    path = os.fspath(filename)
    if not (path.startswith("/") if isinstance(path, str) else path.startswith(b"/")):
        return _REALPATH(filename, strict=strict)  # relative to a cwd that may move
    key = ("realpath", path, bool(strict))
    try:
        return memo[key]
    except KeyError:
        value = memo[key] = _REALPATH(filename, strict=strict)
        return value


def install():
    """Route ``os.path.realpath`` (and ``Path.resolve``) through the pass memo."""
    if posixpath.realpath is not _pass_realpath and os.path is posixpath:
        posixpath.realpath = _pass_realpath
