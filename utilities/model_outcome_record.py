#!/usr/bin/env python3
"""Per-model outcome lines for Claude and Codex, kept in a file of their own.

One line says whether the provider confirmed it refuses a model for an account
scope (`unavailable`) or the model has answered since (`usable`). The line has
the shape of OpenCode's provider line in the usage cache -- provider, model,
account_scope, state, reason, observed_at, source -- but lives in
`<state>/hearting/model-outcomes/<harness>.json`, one file per harness. The usage
cache is rewritten wholesale by its own worker; keeping these lines out of it means
no writer of either file can undo the other, so no lock is shared with usage.

This module is the only writer of those files. Every change is read, edited and
replaced inside the file's lock; a writer that cannot take the lock within
`LOCK_WAIT` seconds drops its line instead of writing, so a writer that resumes
late never overwrites another's work. One close spends one wait, however many
calls it makes. Nothing here changes how a close ends, which harness is chosen, or
any retry: the lines are only recorded and read.

OpenCode keeps its own classification, record and reader in the usage cache;
`read` hands it to that reader so one call reads every harness.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

SCHEMA_VERSION = 1
LOCK_WAIT = 1.0
USABLE_MAX_AGE = 900.0
REJECTED_REASON = "runtime-model-rejected"
HARNESSES = frozenset({"claude", "codex"})

_ANSWERED_CLASSES = frozenset({"pass", "fail", "blocked"})
_MODEL_VARIANT_RE = re.compile(r"(\[[^\]]*\]|#.*)$")
# Names a row used before it carried `model_source`; only consulted without one.
_PLACEHOLDER_MODELS = frozenset({"inherit", "default", "runtime-default", "unknown"})


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "hearting", "model-outcomes")


def _path(harness: str) -> str:
    return os.path.join(state_dir(), "%s.json" % harness)


def model_key(value: object) -> str:
    return _MODEL_VARIANT_RE.sub("", str(value or "").strip()).lower()


def _line_key(provider, model, account_scope) -> str:
    return json.dumps([provider, model, account_scope or "unscoped"], separators=(",", ":"))


def _read(harness: str):
    """The file's parsed JSON, or None when it is absent or unreadable."""
    try:
        with open(_path(harness), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def _load(harness: str):
    """The file's lines, or None when it is absent or not ours."""
    item = _read(harness)
    if (not isinstance(item, dict) or item.get("schema_version") != SCHEMA_VERSION
            or item.get("harness") != harness or not isinstance(item.get("providers"), dict)):
        return None
    return item["providers"]


def _from_a_newer_version(item) -> bool:
    """A readable file a later version of this record wrote: its lines are not ours to replace."""
    version = item.get("schema_version") if isinstance(item, dict) else None
    return isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION


def _flock(fd: int, deadline: float) -> bool:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.005, remaining))


def _edit(harness: str, deadline: float, edit) -> bool:
    """Read, edit and replace the file inside its lock; False (nothing written) past the deadline.

    The lock is a flock on a stable sibling file opened fresh by every call, so
    it excludes other threads and processes alike and carries no state a fork
    could inherit. Only this file is touched while it is held.
    """
    fd = None
    try:
        directory = state_dir()
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, ".%s.lock" % harness), os.O_CREAT | os.O_RDWR, 0o600)
        if not _flock(fd, deadline):
            return False
        if _from_a_newer_version(_read(harness)):
            return False
        lines = _load(harness)
        lines = dict(lines) if lines is not None else {}
        if not edit(lines):
            return False
        item = {"schema_version": SCHEMA_VERSION, "harness": harness, "providers": lines}
        handle, temp = tempfile.mkstemp(prefix=".%s." % harness, dir=directory, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(item, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, _path(harness))
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def record_unavailable(harness, provider, model, account_scope, reason=None, *,
                       now=None, source=None, deadline=None) -> bool:
    """Keep a confirmed refusal of `model` for `account_scope`."""
    if (harness not in HARNESSES or not isinstance(provider, str) or not provider
            or not isinstance(model, str) or not model):
        return False
    now = time.time() if now is None else float(now)
    deadline = time.monotonic() + LOCK_WAIT if deadline is None else deadline

    def edit(lines):
        lines[_line_key(provider, model, account_scope)] = {
            "provider": provider, "model": model, "account_scope": account_scope or "unscoped",
            "state": "unavailable", "reason": reason, "observed_at": now, "source": source}
        return True

    return _edit(harness, deadline, edit)


def lift_unavailable(harness, provider, model, account_scope, *,
                     now=None, source=None, deadline=None) -> bool:
    """The model answered: turn an `unavailable` line of the same model and scope to `usable`.

    With no such line there is nothing to lift, and the call takes no lock and
    writes nothing (the usual case).
    """
    if harness not in HARNESSES:
        return False
    key = _line_key(provider, model, account_scope)

    def refused(lines):
        row = lines.get(key) if lines else None
        return isinstance(row, dict) and row.get("state") == "unavailable"

    if not refused(_load(harness)):
        return False
    now = time.time() if now is None else float(now)
    deadline = time.monotonic() + LOCK_WAIT if deadline is None else deadline

    def edit(lines):
        if not refused(lines):
            return False
        lines[key] = {"provider": provider, "model": model,
                      "account_scope": account_scope or "unscoped", "state": "usable",
                      "reason": None, "observed_at": now, "source": source}
        return True

    return _edit(harness, deadline, edit)


def read(harness, provider, model, account_scope, *, now=None, usable_max_age=USABLE_MAX_AGE) -> dict:
    """The current line for a harness, model and account scope; `state` is `unknown` without one.

    Claude and Codex read this module's file. OpenCode goes to its own reader.
    """
    if harness == "opencode":
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors import usage_cache
        return usage_cache.read_provider("opencode", provider, model, account_scope,
                                         now=now, usable_max_age=usable_max_age)
    now = time.time() if now is None else float(now)
    lines = _load(harness) if harness in HARNESSES else None
    if lines is None:
        return {"state": "unknown", "reason": "no-observation", "observed_at": None}
    row = lines.get(_line_key(provider, model, account_scope))
    if not isinstance(row, dict) or row.get("state") not in ("usable", "unavailable"):
        return {"state": "unknown", "reason": "no-observation", "observed_at": None}
    observed = row.get("observed_at")
    if isinstance(observed, bool) or not isinstance(observed, (int, float)) or observed > now:
        return {"state": "unknown", "reason": "observation-invalid", "observed_at": None}
    if row["state"] == "usable" and now - observed > usable_max_age:
        return {"state": "unknown", "reason": "observation-stale", "observed_at": observed}
    return dict(row)


def _model_launched(meta) -> str:
    """The concrete model a closed attempt launched, or "" when none was settled.

    `model_source` is the row's own record of where the model came from; a launch
    that inherited the runtime's settings carries `inherit` and names no model.
    Only a row without that record falls back to the placeholder names.
    """
    launched = str(meta.get("model") or "").strip()
    if not launched:
        return ""
    source = str(meta.get("model_source") or "")
    if source:
        return "" if source == "inherit" else launched
    return "" if model_key(launched) in _PLACEHOLDER_MODELS else launched


def _answered(note: str, failure_class: str, reason: str) -> bool:
    """The model answered: a handoff verdict was read from its reply.

    A `dead-contract` stands for it only when its reason is one a matched handoff
    block raised (such as a PASS whose blocker is not `none`). `final-handoff-invalid`
    is also what a completion event with no reply text gets, and the evidence columns
    cannot tell the two apart, so it is not taken as an answer.
    """
    if failure_class in _ANSWERED_CLASSES or note.startswith("completed-"):
        return True
    return note == "dead-contract" and bool(reason) and reason != "final-handoff-invalid"


def note_row_closed(meta) -> bool:
    """Keep the per-model line for a Claude/Codex attempt row that has just closed.

    `meta` is the row's metadata as closed. Called once by each registry close,
    on the one transition that closes the row, after the registry lock is released;
    a row seen again closed never reaches here, so an old refusal cannot revive over
    a newer answer. Best effort: it never raises and never changes the close.
    """
    try:
        harness = str(meta.get("harness") or "")
        if harness not in HARNESSES:
            return False
        note = str(meta.get("note") or "")
        reason = str(meta.get("reconcile_reason") or "")
        rejected = reason.startswith(REJECTED_REASON)
        if not rejected and not _answered(note, str(meta.get("failure_class") or ""), reason):
            return False
        launched = _model_launched(meta)
        if not launched:
            return False  # no settled model to put a line on
        model = model_key(launched)
        if rejected:
            named = reason[len(REJECTED_REASON) + 1:]
            if named and model_key(named) != model:
                return False  # a refusal that points at another model says nothing about this one
        scope = str(meta.get("quota_scope") or "") if harness == "claude" else ""
        deadline = time.monotonic() + LOCK_WAIT
        if rejected:
            return record_unavailable(harness, harness, model, scope or "unscoped",
                                      "model-unavailable", source="registry-close",
                                      deadline=deadline)
        return lift_unavailable(harness, harness, model, scope or "unscoped",
                                source="registry-close", deadline=deadline)
    except Exception:  # noqa: BLE001 - a record that cannot be kept never changes the close
        return False
