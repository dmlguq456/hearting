"""Open memory records and exact native ownership shared by tidy, Fleet and steward."""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

from harness_capabilities import HARNESSES


# Records that may be open work: the obligation and backlog types, a ``backlog`` tag, or a
# ``[marker]`` headline (the explorer reads the marker). Everything else is not read.
OPEN_TYPES = ("unresolved-obligation", "backlog", "task", "todo", "obligation")
# Kinds that record a finished outcome, an observation or a pointer, never open work by
# themselves; a ``backlog`` tag on them names the topic, not a pending item.
_FINISHED_TYPES = frozenset(("completion", "observation", "artifact-pointer", "feedback",
                             "user-correction", "decision"))
_FINISHED_TAGS = frozenset(("resolved", "done", "closed"))
_OPEN_MARKER_WORDS = ("백로그", "진행", "미확인", "부분 설치", "미해결", "수리")
_HEADLINE_MARKER = re.compile(r"^\s*\[([^\]]{1,40})\]")
OPEN_STAGES = ("doing", "waiting", "open")


def _refers_to_route(route_id, references):
    if not route_id:
        return False
    for reference in references or ():
        text = str(reference)
        if text == route_id or re.search(r"(?<![A-Za-z0-9])" + re.escape(route_id) +
                                         r"(?![A-Za-z0-9])", text):
            return True
    return False


def open_stage(record, routes=()):
    """How far a possibly-open record got: ``doing``, ``waiting``, ``open`` or None (not open work).

    Only what the record and the routes already say is used: its type, tags, headline marker
    (``[진행 중]``, ``[설치 완료·실사용 미확인]``, ``[해결 확인]`` …) and an exact route link.
    An obligation or backlog record with neither stays open on record, never "in progress".
    """
    headline = record.get("headline")
    match = _HEADLINE_MARKER.match(headline if isinstance(headline, str) else "")
    marker = match.group(1) if match else ""
    if "해결" in marker and "미해결" not in marker:
        return None
    rtype = str(record.get("type") or "").casefold()
    tags = {str(tag).strip("[]\"' ").casefold() for tag in record.get("tags") or ()}
    if rtype in _FINISHED_TYPES or tags & _FINISHED_TAGS:
        return None
    if not (rtype in OPEN_TYPES or "backlog" in tags
            or any(word in marker for word in _OPEN_MARKER_WORDS)):
        return None
    refs = tuple(record.get("links") or ()) + tuple(record.get("artifact_refs") or ())
    linked = [route for route in routes if _refers_to_route(str(route.get("id") or ""), refs)]
    if "진행" in marker or any(route.get("state") in ("working", "blocked") or
                              any(stage.get("st") == "active" for stage in route.get("stages") or ())
                              for route in linked):
        return "doing"
    if "미확인" in marker or "부분 설치" in marker or any(
            route.get("state") == "done" and route.get("outcome") == "success" for route in linked):
        return "waiting"
    return "open"


_OWNER_PREFIX = "session-owner:"
_OWNER_LINK = re.compile(_OWNER_PREFIX + r"[0-9a-f]{64}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")


def owner_identity(row):
    """The exact (harness, session ID) from a resolved target or an owner declaration."""
    if not isinstance(row, dict) or row.get("unresolved"):
        return None
    return _owner_key(row.get("harness"), row.get("session_id"))


def _owner_key(harness, sid):
    if harness in HARNESSES and isinstance(sid, str) and _SESSION_ID.fullmatch(sid):
        return harness, sid
    return None


class OwnerScope(NamedTuple):
    confirmed: frozenset
    unresolved: frozenset = frozenset()
    targets: tuple = ()
    sessions: tuple = ()
    complete: bool = True
    foreign: frozenset = frozenset()


def target_owners(target):
    """Resolved target keys share one label. Unresolved references have none."""
    owner = owner_identity(target)
    return target.get("owner_keys", (owner,)) if owner is not None else ()


def _known_foreign_owners(harness, sid, collector, cwd, rows):
    """A failed role read still leaves unambiguous native repository evidence usable."""
    if not rows:
        return frozenset()
    try:
        repository = (collector._repository_key(cwd) if cwd else
                      collector.repository_for_session(harness, sid, sessions=rows))
        if repository is None:
            return frozenset()
        by_cwd = {cwd: collector._repository_key(cwd) for cwd in {row.cwd for row in rows}}
        origins = {}
        for row in rows:
            for key in collector._session_keys(row):
                origins.setdefault(key, set()).add(by_cwd[row.cwd])
        return frozenset(key for key, repositories in origins.items()
                         if None not in repositories and repository not in repositories)
    except Exception:
        return frozenset()


def scoped_owners(harness, sid, collector, *, cwd=None, predecessors=()):
    """Project the existing role and session lineage onto obligation owners.

    Confirmed keys can write. An active unresolved relation can retain an
    existing obligation under its exact id, without proving a new same-repo
    write or a target label. An unavailable read leaves the scope incomplete.
    """
    caller = _owner_key(harness, sid)
    if caller is None:
        return OwnerScope(frozenset())
    own = {caller, *(key for prior in predecessors if (key := _owner_key(harness, prior)))}
    rows = ()
    try:
        rows = tuple(collector._registry_sessions(strict=True, include_aliases=True))
        markers = collector.read_markers(strict=True)
        relations = collector.read_relations(strict=True, markers=markers)

        joined = {}
        by_owner = {}
        for row in rows:
            owner = _owner_key(row.harness, getattr(row, "session_id", None))
            if owner is None:
                continue
            for key in collector._session_keys(row):
                joined.setdefault(key, set()).add(owner)
                by_owner.setdefault(owner, set()).add(key)
        def keys(owner):
            return (owner, *sorted(key for key in by_owner.get(owner, ())
                                   if key != owner and joined[key] == {owner}))

        caller_keys = keys(caller)
        own.update(caller_keys)
        # The role reader already selected the current seat holder. Consume its
        # chain, including the origin even if the old native row has ended.
        prior = set()
        for relation in relations:
            holder = relation.get("holder") or {}
            if _owner_key(holder.get("harness"), holder.get("sid")) not in own:
                continue
            origin = relation.get("origin") or {}
            if origin.get("harness") == harness:
                prior.add(origin.get("sid"))
            prior.update(edge["from"] for edge in relation.get("lineage") or () if edge.get("from"))
        if prior:
            same_repo = (collector._repository_key(cwd) if cwd else
                         collector.repository_for_session(harness, sid, sessions=rows))
            for former_sid in prior:
                key = _owner_key(harness, former_sid)
                # A native row that can still be joined may now be in another repo.
                known = {collector._repository_key(row.cwd) for row in rows
                         if key in collector._session_keys(row)} - {None}
                if key is not None and same_repo and not known.difference({same_repo}):
                    own.add(key)

        resolved = collector.role_targets(
            harness, sid, aliases=tuple(key[1] for key in caller_keys[1:]),
            cwd=cwd, sessions=rows, include_native=True, markers=markers, _relations=relations)
        unresolved = collector.unresolved_role_targets(harness, sid, sessions=rows, _relations=relations)
        labelled = [dict(target, owner_keys=keys(owner)) for target in resolved
                    if (owner := owner_identity(target)) is not None]
        retained = frozenset(key for target in unresolved
                             if (key := _owner_key(target.get("harness"), target.get("session_id"))))
        confirmed = frozenset(own.union(*(target["owner_keys"] for target in labelled)))
        return OwnerScope(confirmed, retained, (*labelled, *unresolved), rows)
    except Exception:
        foreign = _known_foreign_owners(harness, sid, collector, cwd, rows)
        return OwnerScope(frozenset(own) - foreign, sessions=rows, complete=False, foreign=foreign)


def owner_link(owner):
    """Use the existing links field without persisting or displaying a raw session ID."""
    if owner is None:
        return None
    return _OWNER_PREFIX + hashlib.sha256(":".join(owner).encode()).hexdigest()


def record_owner(record):
    """Only a single explicit owner link binds a record. Mentions and route links do not."""
    owners = {link for link in record.get("links") or ()
              if isinstance(link, str) and _OWNER_LINK.fullmatch(link)}
    return owners.pop() if len(owners) == 1 else None
