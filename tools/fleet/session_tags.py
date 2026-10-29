"""Small, shared live-session number assignments; runtime identity stays untouched."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

_HEX = re.compile(r"^[0-9a-f]{2}$")


def _path():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "agent-fleet/tag-assignments.json"


def _read(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("assignments"), list):
        raise ValueError("invalid tag assignments")
    rows = {}
    for row in data["assignments"]:
        if not isinstance(row, dict):
            raise ValueError("invalid tag assignment")
        key = (row.get("harness"), row.get("session_id"))
        seen = row.get("first_seen")
        started = row.get("started_at")
        revision = row.get("observation_revision", 0)
        if (key[0] not in {"claude", "codex", "opencode"}
                or not isinstance(key[1], str) or not key[1]
                or not isinstance(row.get("tag"), str) or not _HEX.fullmatch(row["tag"])
                or not isinstance(seen, (int, float)) or isinstance(seen, bool) or not math.isfinite(seen)
                or not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started)
                or not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
                or key in rows):
            raise ValueError("invalid tag assignment")
        rows[key] = row
    claimed = {}
    for key, row in rows.items():
        prior = claimed.get(row["tag"])
        if prior and (key[0] != "claude" or prior[0] != "claude"):
            raise ValueError("duplicate mutable tag assignment")
        claimed[row["tag"]] = key
    return rows


def assigned_tag(harness, sid):
    """Read a confirmed snapshot row without allocation, discovery or locking."""
    try:
        row = _read(_path()).get((harness, sid))
        return row["tag"] if row else None
    except (OSError, ValueError, TypeError):
        return None


def _started(harness, sid, started=None):
    if isinstance(started, (int, float)) and not isinstance(started, bool) and math.isfinite(started):
        return started
    # Codex thread UUIDv7 encodes creation milliseconds. This also orders the
    # pre-install same-hash pair when Herdr exposes no creation timestamp.
    if harness == "codex" and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f-]{18}", sid):
        return int(sid[:8] + sid[9:13], 16) / 1000
    return time.time()


def _inventory(sessions):
    from .session_handle import resolve_tag
    from .collectors.herdr import list_agents
    from . import session_registry
    from .collectors.procscan import read_proc_start
    live = {}
    for sess in sessions:
        harness, sid = sess.harness, getattr(sess, "session_id", None)
        if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid:
            continue
        row = {"harness": harness, "session_id": sid,
               "started_at": _started(harness, sid, getattr(sess, "started_at", None)),
               "pid": getattr(sess, "pid", None), "proc_start": getattr(sess, "proc_start", None)}
        if harness == "claude":
            row["tag"] = getattr(sess, "session_tag", None) or resolve_tag(harness, sid)
        live[(harness, sid)] = row
    # Native/managed registry evidence also reserves sessions outside Herdr or
    # a harness-filtered collection. PID/start agreement prevents recycled PIDs.
    for harness in ("claude", "codex", "opencode"):
        directory = Path(session_registry._dir_for(harness))
        for path in directory.glob("*.json"):
            if not path.stem.isdecimal():
                continue
            pid = int(path.stem)
            record = session_registry.read(harness, pid)
            start = read_proc_start(pid)
            if not record or start is None or record.get("status") == "exited":
                continue
            if harness != "claude" and record.get("procStart") is None:
                continue
            if record.get("procStart") is not None and str(record["procStart"]) != str(start):
                continue
            sid = record.get("sessionId")
            if not isinstance(sid, str) or not sid:
                continue
            row = live.setdefault((harness, sid), {"harness": harness, "session_id": sid,
                                  "started_at": _started(harness, sid, session_registry._ms_to_sec(record.get("startedAt")))})
            row.update(pid=pid, proc_start=start)
            if harness == "claude" and not row.get("tag"):
                row["tag"] = resolve_tag(harness, sid)
    agents = list_agents()
    for agent in agents or []:
        identity = agent.get("agent_session") or {}
        if not isinstance(identity, dict) or identity.get("kind") != "id":
            continue
        harness, sid = agent.get("agent"), identity.get("value")
        if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid:
            continue
        row = live.setdefault((harness, sid), {"harness": harness, "session_id": sid,
                              "started_at": _started(harness, sid)})
        row["pane_id"] = agent.get("pane_id")
        if harness == "claude" and not row.get("tag"):
            row["tag"] = resolve_tag(harness, sid)
    return live, agents


def _keep(row, agents):
    """Release only proven exited reservations, never uncertain live processes."""
    pid, start = row.get("pid"), row.get("proc_start")
    if pid and start:
        from .collectors.procscan import read_proc_start, is_terminal_state
        actual = read_proc_start(pid)
        if actual is not None:
            return str(actual) == str(start)
        return Path("/proc/%s" % pid).exists() and not is_terminal_state(pid)
    if agents is None or not row.get("pane_id"):
        return True
    unknown = False
    for agent in agents:
        identity = agent.get("agent_session") or {}
        sid = identity.get("value") if isinstance(identity, dict) and identity.get("kind") == "id" else None
        if not isinstance(sid, str) or not sid:
            sid = None
        same_harness = agent.get("agent") == row["harness"]
        if same_harness and agent.get("pane_id") and sid is None:
            unknown = True
        if agent.get("pane_id") == row["pane_id"]:
            # A present pane without native identity is not an observed exit.
            if same_harness and (sid is None or sid == row["session_id"]):
                return True
    # An uncertain same-harness pane may be the session after a pane move.
    # This reserves its number only; it does not attribute that pane to a SID.
    return unknown


def assign(sessions):
    """Collect normal inventory before attempting the nonblocking shared writer."""
    try:
        try:
            baseline = _read(_path())
        except FileNotFoundError:
            baseline = {}
        live, agents = _inventory(sessions)
        return _assign(live, agents, baseline=baseline)
    except (OSError, ValueError, TypeError, ImportError):
        return []


def assign_observed(harness, sid, *, pane_id=None, pid=None, proc_start=None):
    """Attempt one already-proven native observation, without a native census.

    A narrow event proves no other session's absence. Keep every reservation and
    its evidence until the normal full inventory can establish an exit.
    """
    if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid.strip():
        return None
    row = {"harness": harness, "session_id": sid, "started_at": _started(harness, sid)}
    row.update({key: value for key, value in
                (("pane_id", pane_id), ("pid", pid), ("proc_start", proc_start))
                if value is not None})
    if harness == "claude":
        from .session_handle import resolve_tag
        row["tag"] = resolve_tag(harness, sid)
    _assign({(harness, sid): row}, None, observed_only=True)
    return assigned_tag(harness, sid)


def _assign(live, agents, *, observed_only=False, baseline=None):
    """Reserve against the latest snapshot and persist atomically under one lock."""
    from .session_handle import minted_tag
    observed = set(live)
    path = _path()
    try:
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / "tag-assignments.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                old = _read(path)
            except FileNotFoundError:
                old = {}
            baseline = baseline or {}
            if not observed_only:
                # A newer full writer's deletion wins over this stale census.
                for key in baseline.keys() - old.keys():
                    live.pop(key, None)
                    observed.discard(key)
            for key, row in old.items():
                # Only unchanged rows can be retired/refreshed by this census.
                # Newer pane/PID evidence wins even when live contains the SID.
                if ((not observed_only and row != baseline.get(key))
                        or (key not in live and (observed_only or _keep(row, agents)))):
                    live[key] = row.copy()
                    if not observed_only:
                        observed.discard(key)
            now = time.time()
            for key, row in live.items():
                previous = old.get(key, {})
                if previous:
                    # Missing fields in a narrow observation cannot erase prior
                    # PID/start/pane evidence or a Claude fixed reservation.
                    if observed_only:
                        row = {**previous, **{k: v for k, v in row.items() if v is not None}}
                        live[key] = row
                    row["started_at"] = previous["started_at"]
                row["first_seen"] = previous.get("first_seen", now)
                if key in observed:
                    # A real reaffirmation is newer even if pane/PID data match.
                    # Carried or superseded census rows are not new observations.
                    row["observation_revision"] = previous.get("observation_revision", 0) + 1
                if previous and row["harness"] != "claude":
                    row["tag"] = previous["tag"]
                elif row["harness"] != "claude":
                    row["tag"] = minted_tag(row["session_id"])
            for row in live.values():
                if row["harness"] == "claude" and not _HEX.fullmatch(str(row.get("tag") or "")):
                    row.pop("tag", None)
            fixed = {row["tag"] for row in live.values() if row["harness"] == "claude" and row.get("tag")}
            taken = set(fixed)
            movable = [(key, row) for key, row in live.items() if row["harness"] != "claude"]
            movable.sort(key=lambda item: (0 if item[0] in old else 1,
                                           item[1]["first_seen"], item[1]["started_at"], item[0]))
            # Reserve every surviving old number before placing newcomers or a
            # displaced number. A new hash must never evict an existing session.
            displaced = []
            for key, row in movable:
                tag = row.get("tag")
                if tag and tag not in taken:
                    taken.add(tag)
                else:
                    displaced.append(row)
            for row in displaced:
                base = int(minted_tag(row["session_id"]), 16)
                tag = next((f"{(base + step) % 256:02x}" for step in range(256)
                            if f"{(base + step) % 256:02x}" not in taken), None)
                if tag is None:
                    row.pop("tag", None)  # namespace exhausted: leave the number absent
                else:
                    row["tag"] = tag
                    taken.add(tag)
            rows = [row for row in live.values() if row.get("tag")]
            if not path.exists() or old != {(row["harness"], row["session_id"]): row for row in rows}:
                fd, temp = tempfile.mkstemp(prefix=".tag-assignments-", dir=path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as output:
                        json.dump({"version": 1, "assignments": rows}, output, sort_keys=True)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temp, path)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
        assigned = {(row["harness"], row["session_id"]) for row in rows}
        return [agent for agent in agents or []
                if (agent.get("agent"), (agent.get("agent_session") or {}).get("value")) in assigned]
    except (OSError, ValueError, TypeError, ImportError):
        return []  # observation must not block any session or consumer


def refresh(sessions):
    """Native metadata callers retain assignment followed by pane projection.

    Fleet publishes assign() results first and sends pane writes through its
    existing detail worker instead of serializing them in basic collection.
    """
    agents = assign(sessions)
    try:
        from .herdr_projection import refresh_tag_metadata
        refresh_tag_metadata(agents)
    except (OSError, ValueError, TypeError, ImportError):
        pass
