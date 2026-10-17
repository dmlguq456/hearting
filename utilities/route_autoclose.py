"""Runtime closure of routes nobody works on any more.

A compiled route is a lease the runtime holds for the session that composed
it, not homework that session must remember.  `compose` and `campaign-status`
run one bounded background sweep that closes an open route only when time says
nobody works on it.  `compose` scopes cycle work to its campaign, while overdue
idle routes from other campaigns can still be closed after the same protections
are checked; `campaign-status` sweeps the whole root.  The rules:

* session-ended -- this host holds positive evidence that the composing Claude
  session is gone (its own session record, whose process is dead) and nothing
  wrote to the route or its cycle for `QUIET_SECONDS`;
* idle -- otherwise (no such evidence, or Codex/OpenCode, which expose none),
  nothing wrote to the route, its cycle, its composer's route-chain ledger or
  a bound resource run's log for `IDLE_SECONDS`.  An activity scan that hits
  its size limit counts as activity.

`campaign-close` is an explicit end: besides that sweep it closes the
campaign's own open member routes that are the closing session's, or quiet
for `QUIET_SECONDS` with no provably live session.

Never closed, whatever the time or trigger: a route of the session running
the sweep (outside `campaign-close`), a route a dispatch attempt still holds
(live, unverifiable or lease-held), one whose owner's runtime settlement the
runtime itself still classifies as pending, one whose workflow ledger waits on
a human gate, one a resource run without an end record is bound to or logs
into (on any host), one with an unfinished `finish`, and one whose cycle
directory a live process holds open.  When any of that evidence cannot be
read, the sweep closes nothing.

The closure claims no proof.  Every route closes the same way,
`close_route(allow_unproven=True)` with an `autoclose` record, so its terminal
proof is exactly what completion markers already show.  Its open cycle is then
sealed with the existing states: completed when that proof holds, abandoned
otherwise.  Both steps are idempotent, so the next sweep finishes whatever an
interrupted one left: a cycle behind a runtime-written closure is sealed at
once, any other closed-but-unsealed cycle after `QUIET_SECONDS`.  A session
that comes back to an automatically closed route is not refused: `finish`
reports it closed and succeeds, `start` hands back a compose command.
A cycle inside a closed lineage that other cycles began in seals on its own
stretch of the lineage; an abandoned seal leaves symbolic links out of the
manifest and records them.  A workflow-group member that ended with no durable
output is withdrawn from its campaign's declaration in the same sweep.

The sweep writes under the artifact root only and reads the checkout with
`git rev-parse` alone.  It takes no lock it would wait for and spends at most
`BUDGET_SECONDS`; whatever it did not reach waits for the next one.  A cycle it cannot read
within that budget stays open, and the next sweep continues from the digests already read.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

IDLE_SECONDS = 7 * 24 * 3600
QUIET_SECONDS = 3600
BUDGET_SECONDS = 20.0
# A route or cycle the evidence kept open is not re-judged sooner than this:
# the answer rarely changes within minutes, and asking costs a registry and
# process scan.  Closing later by at most this much is the safe direction.
RECHECK_SECONDS = 600
# Bumped when the rules that decide whether a cycle can be sealed change: an
# earlier sweep's "cannot seal" is then asked once more under the new rules.
SEAL_RULES = 2
WALK_LIMIT = 2000
# A terminal registry row may still have its wrapper process publishing; rows
# older than this are not re-probed on every sweep.
_RECENT_TERMINAL_SECONDS = 3600
_TERMINAL = {"done", "killed", "cancelled"}
_RESOURCE_OPEN = {"open", "running", "pending", "working"}   # artifact-quiescence RESOURCE_OPEN


class Unreadable(Exception):
    """Evidence a protection needs could not be read: the sweep closes nothing."""


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _newest_mtime(directory: Path, limit: int = WALK_LIMIT) -> float | None:
    """Newest mtime in a tree, or None when the tree is larger than `limit`."""
    newest, seen = _mtime(directory), 0
    for base, dirs, files in os.walk(directory):
        for name in files + dirs:
            newest = max(newest, _mtime(Path(base) / name))
            seen += 1
            if seen > limit:
                return None
    return newest


def _under(path: str, home: Path | None) -> bool:
    return home is not None and (path == str(home) or path.startswith(str(home) + os.sep))


def _open_paths(root: Path) -> set[str]:
    """Paths under `root` a live process holds open or runs in.  Non-dumpable
    daemons of this user (sd-pam, key agents) refuse inspection to everyone and
    are skipped; a scan that can read none of this user's other processes is
    blind, and a blind scan is unreadable evidence."""
    prefix = str(root) + os.sep
    found: set[str] = set()
    try:
        pids = [pid for pid in os.listdir("/proc") if pid.isdigit() and int(pid) != os.getpid()]
    except OSError as exc:
        raise Unreadable("process-table") from exc
    read = denied = 0
    for pid in pids:
        try:
            mine = os.stat(f"/proc/{pid}").st_uid == os.getuid()
            links = [os.readlink(f"/proc/{pid}/cwd")]
            links += [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")]
        except PermissionError:
            denied += mine
            continue
        except OSError:
            continue   # the process or one descriptor went away meanwhile
        read += mine
        found.update(link for link in links if link.startswith(prefix))
    if denied and not read:
        raise Unreadable("process-descriptors")
    return found


def _composers(api) -> tuple[dict, dict]:
    """route_id -> latest route-chain ledger holder, and (harness, sid) -> ledger mtime."""
    holders: dict[str, dict] = {}
    activity: dict[tuple[str, str], float] = {}
    try:
        rc = api._route_chain_module()
    except Exception:
        rc = None
    if rc is None:
        return holders, activity
    for harness in rc.HARNESSES:
        directory = Path(rc.state_root()) / harness
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            sid = name[:-len(".jsonl")]
            activity[(harness, sid)] = _mtime(directory / name)
            for line in rc.read_tail(harness, sid, max_bytes=1024 * 1024):
                ts = line.get("ts") or 0
                previous = holders.get(line["route_id"])
                if previous is None or ts >= previous["ts"]:
                    holders[line["route_id"]] = {"harness": harness, "session_id": sid, "ts": ts}
    return holders, activity


def _current_identity():
    """The interactive depth-0 session running this command, else None."""
    if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or os.environ.get("AGENT_DISPATCH_DEPTH", "0") not in ("", "0"):
        return None
    try:
        from dispatch_parent_completion import interactive_parent_identity
        harness, sid = interactive_parent_identity(os.environ)
    except Exception:
        return None
    return (harness, sid) if harness and sid else None


def _registries(api) -> list[Path]:
    import dispatch_contract as dispatch
    paths = []
    try:
        paths.append(Path(dispatch.resolve_global_registry(api.ROOT, None, 0, "read").path))
    except Exception:
        pass
    if os.environ.get("AGENT_DISPATCH_JOBS"):
        paths.append(Path(os.environ["AGENT_DISPATCH_JOBS"]))
    return list(dict.fromkeys(paths))


def _attempts(registries: list[Path]) -> tuple[set[str], dict]:
    """(route ids a registered attempt still holds -- live, unverifiable or
    lease-held --, route id -> its settled owner rows for the runtime's own
    settlement check)."""
    if not any(path.is_file() for path in registries):
        return set(), {}
    import artifact_cutover
    import codex_dispatch_terminal as terminal
    import dispatch_contract as dispatch
    held: set[str] = set()
    owners: dict[str, list] = {}
    now = time.time()
    for attempt_id, row in artifact_cutover._registry_attempts(registries).items():
        metadata, status = row["metadata"], row["status"]
        ids = {metadata.get(key, "") for key in ("route_id", "owner_route_id", "batch_route_id")} - {""}
        if not ids:
            continue
        if status == "done" and metadata.get("workflow_completion") == "runtime-v1":
            for route_id in ids:
                owners.setdefault(route_id, []).append(row)
        if status in _TERMINAL:
            try:
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                stamp = now
            if now - stamp >= _RECENT_TERMINAL_SECONDS:
                continue
        try:
            observed = dispatch.observed_attempt_liveness(
                status, metadata, terminal_envelope=terminal.terminal_envelope_observed(metadata.get("log_file")))
            state = observed.state
            lease = bool(metadata.get("supervisor_lease_file")) and dispatch.supervisor_lease_is_held(row["jobs"], metadata)
        except Exception:
            state, lease = "unverifiable", False
        if state in {"alive", "unverifiable"} or lease:
            try:
                current, _conflict = artifact_cutover._current_owner_route_id(attempt_id, metadata, row["jobs"])
            except Exception:
                current = None
            held |= ids | ({current} if current else set())
    return held, owners


def _settlement_pending(route_id: str, owners: dict) -> bool:
    """The runtime's own classifier: a PASS owner whose settlement is pending
    or unknown still owns the route's closure."""
    from dispatch_terminal_commit import owner_completion_pending
    for row in owners.get(route_id, ()):
        try:
            if owner_completion_pending(Path(row["jobs"]), row["status"], row["metadata"]):
                return True
        except Exception:
            return True
    return False


def _gate_ledger_roots(registries: list[Path]) -> list[Path]:
    import workflow_state
    try:
        roots = [workflow_state.ledger_root_for(jobs=jobs)[0] for jobs in registries]
        roots.append(workflow_state.ledger_root_for()[0])
    except Exception as exc:
        raise Unreadable("workflow-ledger-root") from exc
    return list(dict.fromkeys(roots))


def _waits_on_human(route_id: str, roots: list[Path]) -> bool:
    """A gate this route's workflow raised and nobody has resolved yet."""
    import workflow_state
    for root in roots:
        try:
            entries = workflow_state.WorkflowLedger(route_id, root=root).journal()
        except Exception:
            return True   # unreadable: treat as waiting
        gates = {(entry.get("evidence") or {}).get("gate") for entry in entries
                 if isinstance(entry, dict) and entry.get("workflow_state") == "BLOCKED_HUMAN_GATE"
                 and isinstance(entry.get("evidence"), dict)} - {None}
        if any(workflow_state.human_gate_resolution(entries, gate)["status"] == "blocked" for gate in gates):
            return True
    return False


def _resource_runs() -> tuple[set[str], list[str], dict]:
    """(route ids a still-live resource run is bound to, the paths such runs log
    into, route id -> newest bound log write).  A run with an end record, or one
    the registry proves exited here, no longer protects its route.  A run this
    host cannot verify (another host or namespace, an incomplete identity) stays
    protected until its log has been silent past the idle limit."""
    try:
        import resource_run_registry
        rows, diagnostics = resource_run_registry.scan()
    except Exception as exc:
        raise Unreadable("resource-run-index") from exc
    if any(str(row.get("kind", "")).startswith("malformed") for row in diagnostics):
        raise Unreadable("resource-run-index")
    routes, paths, activity = set(), [], {}
    for row in rows:
        bound = set()
        for value in (row.get("route_id"), row.get("route_file"), row.get("route")):
            if isinstance(value, str) and value:
                bound.add(Path(value).stem if value.endswith(".json") else value)
        log = row.get("log_path") if isinstance(row.get("log_path"), str) else ""
        written = _mtime(Path(log)) if log else 0.0
        for route_id in bound:
            activity[route_id] = max(activity.get(route_id, 0.0), written)
        status = str(row.get("registry_status") or "").lower()
        sentinel = row.get("sentinel")
        ended = (row.get("ended_at") is not None or row.get("exit_code") is not None
                 or (status and status not in _RESOURCE_OPEN)
                 or (isinstance(sentinel, str) and sentinel and os.path.exists(sentinel)))
        liveness = row.get("liveness")
        if ended or liveness == "exited":
            continue
        if liveness != "working" and written and time.time() - written >= IDLE_SECONDS:
            continue
        routes |= bound
        if log:
            paths.append(os.path.realpath(log))
    return routes, paths, activity


@functools.lru_cache(maxsize=256)
def _claude_state(sid: str) -> str:
    """alive | gone | unknown.  `gone` only on positive evidence on this host:
    the session's own registry record, whose process is dead."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        from fleet import session_handle, session_registry
        from dispatch_contract import process_identity_disposition
        directory = session_handle._claude_sessions_dir()
        names = os.listdir(directory) if directory else []
    except Exception:
        return "unknown"
    dead = unresolved = False
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        pid = int(name[:-5])
        record = session_registry.read("claude", pid)
        if not record:
            continue
        verdict = process_identity_disposition(pid, str(record.get("procStart") or ""))
        if record.get("sessionId") == sid:
            if verdict == "live":
                return "alive"
            dead |= verdict == "dead"
            unresolved |= verdict != "dead"
        elif verdict == "live":
            # A resumed session runs under a new id with the old one on argv.
            try:
                if sid in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace"):
                    return "alive"
            except OSError:
                pass
    return "gone" if dead and not unresolved else "unknown"


def _session_state(holder: Mapping[str, Any] | None) -> str:
    if holder and holder["harness"] == "claude":
        return _claude_state(holder["session_id"])
    return "unknown"


def _open_route_files(root: Path, api) -> list[Path]:
    """Canonical route records with no closure beside them (names only)."""
    directory = api.canonical_routes_dir(root)
    try:
        names = set(os.listdir(directory))
    except OSError:
        return []
    return [directory / name for name in sorted(names)
            if api.ROUTE_RECORD_BASENAME.fullmatch(name) and name[:-5] + ".outcome.json" not in names]


def _read_route(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("route_id") == path.stem else None


def _cycle_home(root: Path, record) -> Path | None:
    import artifact_producer
    if not record:
        return None
    try:
        return artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    except Exception:
        return None


def _last_activity(root: Path, path: Path, raw: Mapping[str, Any], holder, api, record, now: float, *,
                   deep: bool = False, resource_activity: Mapping[str, float] | None = None) -> float:
    """Newest write to the route, its ledger line and its cycle.  The cheap form
    stats the top of the cycle; `deep` walks it and the completion markers, and
    a tree past `WALK_LIMIT` counts as written now."""
    newest = max(_mtime(path), float(holder.get("ts") or 0) if holder else 0.0,
                 (resource_activity or {}).get(str(raw.get("route_id")), 0.0))
    homes = [root / ".runtime" / "inline-finish" / "v1" / str(raw.get("route_id"))]
    if deep:
        try:
            homes.append(Path(api.completion_dir(raw["route_id"])))
        except Exception:
            pass
    if record:
        newest = max(newest, _mtime(root / ".runtime/artifact-producer/v1/cycles" / (record["cycle_id"] + ".json")))
        home = _cycle_home(root, record)
        if home is not None:
            homes.append(home)
            newest = max(newest, _mtime(home / "artifacts"))
    for home in homes:
        seen = _newest_mtime(home) if deep else _mtime(home)
        if seen is None:
            return now
        newest = max(newest, seen)
    return newest


def _decide(holder, *, activity, current, idle_since, now, member=False) -> tuple[str | None, str | None]:
    """(reason to close | None, reason kept).  `member`: a member route of the
    campaign `campaign-close` is closing."""
    identity = (holder["harness"], holder["session_id"]) if holder else None
    if member:
        if current and identity == current:
            return "campaign-close", None
        if _session_state(holder) == "alive":
            return None, "session-alive"
        if now - idle_since >= QUIET_SECONDS:
            return "campaign-close", None
        return None, "recent-activity"
    if current and identity == current:
        return None, "current-session"
    state = _session_state(holder)
    if state == "alive":
        return None, "session-alive"
    if state == "gone" and now - idle_since >= QUIET_SECONDS:
        return "session-ended", None
    recent = max(idle_since, activity.get(identity, 0.0) if identity else 0.0)
    if now - recent >= IDLE_SECONDS:
        return "idle", None
    return None, "recent-activity"


_TRANSIENT = ("busy", "lock", "finish-in-progress", "in-progress")


def _left_open(result: str) -> bool:
    return result.startswith(("cycle-left-open", "cycle-unresolved"))


def _transient(result: str) -> bool:
    """A failure the next sweep may clear by itself (a held lock, a finish in flight)."""
    return any(word in result for word in _TRANSIENT)


def _closed_lineage(root: Path, record: Mapping[str, Any] | None) -> bool:
    """Whether the cycle's whole lineage is closed, so "which cycle continues
    this route" is no longer a live question."""
    import artifact_producer
    if record is None:
        return False
    try:
        return artifact_producer.closed_lineage_handover(root, record).closed
    except Exception:
        return False


def _seal_cycle(root: Path, route: Mapping[str, Any], proven, *, record: Mapping[str, Any] | None = None,
                deadline: float | None = None) -> str:
    """Seal the route's open cycle with an existing state; never waits on a lock.
    A `record` whose lineage is closed is sealed as it is; otherwise the route
    is asked which cycle it continues, and an ambiguous answer stops the seal.
    With the sweep's `deadline` the read of the cycle's files and the walk of its
    lineage stop when it passes: the cycle stays open and the next sweep goes on
    from the digests already read."""
    import artifact_producer
    budget = None if deadline is None else artifact_producer.RefreshBudget(
        float("inf"), float("inf"), max(0.0, deadline - time.monotonic()))
    if _closed_lineage(root, record):
        pass
    else:
        try:
            record = artifact_producer.route_cycle_for(root, route)
        except Exception as exc:
            return "cycle-unresolved:" + str(exc)[:80]
    if not record:
        return "no-open-cycle"
    try:
        leaf = artifact_producer._finalize_route(root, record, deadline=deadline)
    except Exception as exc:
        return "cycle-left-open:" + str(exc)[:80]
    if leaf.get("route_id") != route.get("route_id"):
        return "cycle-held-by-continuation"
    if proven is True:
        try:
            result = artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="completed",
                                                lock_timeout=0, _scan_budget=budget)
            return "cycle-left-open:scan-in-progress" if result.get("status") == "deferred" else "cycle-completed"
        except Exception:
            pass
    try:
        result = artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="abandoned",
                                            abandon_reason="route-unrecoverable", lock_timeout=0,
                                            exclude_symlinks=True, _scan_budget=budget)
    except Exception as exc:
        return "cycle-left-open:" + str(exc)[:80]
    if result.get("status") == "deferred":
        return "cycle-left-open:scan-in-progress"
    return "cycle-abandoned-empty" if result.get("status") == "no-lineage" else "cycle-abandoned"


def _summary_text(raw: Mapping[str, Any]) -> str:
    request = raw.get("work_request") if isinstance(raw.get("work_request"), dict) else {}
    text = str(request.get("text") or "").strip()
    return text or f"{raw.get('capability')} {raw.get('slug') or raw.get('route_id')}"


def close_one(root: Path, path: Path, raw: Mapping[str, Any], reason: str, api, *,
              trigger: str, now: float, deadline: float | None = None) -> dict:
    """Close one route nobody works on, claiming no proof.  Idempotent: a rerun
    replays the existing closure and seals whatever is still open."""
    route = api.verify_route(dict(raw), None, allow_stale_registry=True)
    outcome, _created = api.close_route(
        route, path, None, _summary_text(raw), allow_unproven=True,
        autoclose={"reason": reason, "trigger": trigger, "closed_by": "runtime",
                   "proof": "not-claimed", "at": _iso(now)})
    proven = outcome.get("terminal_gate_proven")
    return {"route_id": route["route_id"], "reason": reason,
            "closed": "proven" if proven else "unproven",
            "cycle": _seal_cycle(root, route, proven, deadline=deadline)}


def _signature(path: Path) -> list | None:
    try:
        meta = path.stat()
    except OSError:
        return None
    return [meta.st_mtime_ns, meta.st_size]


class _Memory:
    """What earlier sweeps learned, kept under the artifact root: cycle records
    already past `open` (not re-read while unchanged) and cycles or routes that
    failed to close, each with the signature of the evidence it failed on --
    retried only once that evidence changes."""

    def __init__(self, runtime: Path):
        self.path = runtime / "route-autoclose" / "state.json"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != 1:
                raise ValueError
        except (OSError, ValueError):
            data = {"schema": 1}
        self.settled = data.get("settled_cycles") if isinstance(data.get("settled_cycles"), dict) else {}
        self.unsealable = data.get("unsealable") if isinstance(data.get("unsealable"), dict) else {}
        self.failed_routes = data.get("failed_routes") if isinstance(data.get("failed_routes"), dict) else {}
        self.kept = data.get("kept") if isinstance(data.get("kept"), dict) else {}
        self.workflow_groups = data.get("workflow_groups") if isinstance(data.get("workflow_groups"), dict) else {}
        self.changed = False

    def recently_kept(self, key: str, now: float) -> str | None:
        row = self.kept.get(key)
        return row.get("reason") if isinstance(row, dict) and float(row.get("until") or 0) > now else None

    def keep(self, key: str, reason: str, now: float) -> None:
        self.kept[key] = {"reason": reason, "until": now + RECHECK_SECONDS}
        self.changed = True

    def save(self) -> None:
        if not self.changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".state.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"schema": 1, "settled_cycles": self.settled,
                                         "unsealable": self.unsealable, "failed_routes": self.failed_routes,
                                         "kept": self.kept, "workflow_groups": self.workflow_groups},
                                        sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


def _open_cycle_entry(root: Path, directory: Path, name: str, memory: _Memory) -> dict | None:
    """The open cycle record in `directory/name`, re-read only when its file changed."""
    import artifact_producer
    signature = _signature(directory / name)
    if signature is None or memory.settled.get(name) == signature:
        return None
    try:
        record = artifact_producer.read_cycle_record(root, name[:-len(".json")])
    except Exception:
        return None
    if not record:
        return None
    if record.get("state") != "open":
        memory.settled[name] = signature
        memory.changed = True
        return None
    parent = record.get("parent_cycle_id")
    if isinstance(parent, str) and parent:
        # A parent sealed later is new evidence for a child that could not seal.
        signature = signature + (_signature(directory / f"{parent}.json") or [])
    return dict(record, _signature=signature)


def _scoped_cycle_names(scope_campaign_id: str, scope_dir: Path) -> list[str]:
    """Record names of the cycles bound to one campaign, from its own folder's `.cycle.json`
    files; no other campaign's folder and no root-wide cycle listing is read."""
    import artifact_locator
    try:
        with os.scandir(scope_dir) as listing:
            children = [Path(entry.path) for entry in listing
                        if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    names = []
    for child in children:
        try:
            binding = artifact_locator.read_cycle_binding(child)
        except (artifact_locator.LocatorError, OSError):
            continue
        if binding and binding.get("campaign_id") == scope_campaign_id:
            names.append(f"{binding['cycle_id']}.json")
    return names


def _open_cycles(root: Path, memory: _Memory, *, scope: tuple[str, Path] | None = None) -> list[dict]:
    """Open cycle records, re-reading only records whose file changed.  `scope` is
    `(campaign_id, campaign folder)`: only that campaign's cycles are looked at, found
    before any record is read.  Without it every record under the root is listed."""
    directory = root / ".runtime" / "artifact-producer" / "v1" / "cycles"
    if scope is not None:
        names = _scoped_cycle_names(*scope)
    else:
        try:
            names = [entry.name for entry in os.scandir(directory) if entry.name.endswith(".json")]
        except OSError:
            return []
    records = []
    for name in names:
        record = _open_cycle_entry(root, directory, name, memory)
        if record is not None:
            records.append(record)
    return records


def _cycles_by_route(records: list[dict]) -> dict:
    table = {}
    for record in records:
        table.setdefault(record.get("route_id"), record)
        for binding in record.get("route_bindings") or []:
            route_id = binding.get("route_id") if isinstance(binding, dict) else binding
            if isinstance(route_id, str):
                table.setdefault(route_id, record)
    return table


def _seal_unsealed_cycles(root: Path, api, records: list[dict], evidence, memory: _Memory, *,
                          deadline, now, campaign_id=None) -> list[dict]:
    """Open cycles behind an already closed route: the runtime's own closure at
    once (an interrupted sweep), anyone else's after `QUIET_SECONDS`.  A cycle
    that fails to seal is remembered with its evidence and skipped until that
    evidence changes.  `evidence` gathers the never-close evidence on first use."""
    import artifact_producer
    results = []
    runtime = root / ".runtime"
    for record in records:
        if time.monotonic() >= deadline:
            break
        begin_outcome = api.outcome_path(api.canonical_route_path(root, record["route_id"]))
        outcome_signature = _signature(begin_outcome)
        if outcome_signature is None:
            continue   # the begin route is open: the route pass owns it
        cycle_id = record["cycle_id"]
        if record.get("campaign_id") != campaign_id and memory.recently_kept("cycle:" + cycle_id, now):
            continue
        remembered = memory.unsealable.get(cycle_id)
        if (remembered and remembered.get("signature") == record["_signature"]
                and remembered.get("outcome_signature") == outcome_signature
                and remembered.get("rules") == SEAL_RULES):
            continue
        try:
            quick = json.loads(begin_outcome.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not quick.get("autoclose") and now - _mtime(begin_outcome) < QUIET_SECONDS:
            continue
        try:
            leaf = artifact_producer._finalize_route(root, record, deadline=deadline)
            outcome_file = api.outcome_path(api.canonical_route_path(root, leaf["route_id"]))
            outcome = json.loads(outcome_file.read_text(encoding="utf-8"))
        except Exception as exc:
            result = "cycle-left-open:" + str(exc)[:80]
        else:
            if not outcome.get("autoclose") and now - _mtime(outcome_file) < QUIET_SECONDS:
                continue
            why = evidence().kept(leaf["route_id"], lambda: _cycle_home(root, record))
            if why:
                memory.keep("cycle:" + cycle_id, why, now)
                continue
            result = _seal_cycle(root, leaf, outcome.get("terminal_gate_proven"), record=record,
                                 deadline=deadline)
        results.append({"cycle_id": cycle_id, "route_id": record["route_id"], "cycle": result})
        if _left_open(result) and not _transient(result):
            memory.unsealable[cycle_id] = {"signature": record["_signature"],
                                           "outcome_signature": outcome_signature,
                                           "rules": SEAL_RULES, "reason": result, "at": _iso(now)}
            memory.changed = True
    return results


def _ended_empty(root: Path, record: Mapping[str, Any] | None) -> bool:
    """A cycle that ended with no durable output: abandoned (or a completed
    request that found nothing), and no manifest was ever published."""
    import artifact_producer
    if not record or record.get("state") not in ("abandoned", "no-lineage") or record.get("manifest_digest"):
        return False
    try:
        directory = artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    except Exception:
        return True   # the directory went with the empty cycle
    return not os.path.lexists(directory / "manifest.json")


def _declared_members(path: Path) -> list[str]:
    """Cycle ids a declaration names, read loosely: anything unreadable names none."""
    try:
        groups = json.loads(path.read_text(encoding="utf-8")).get("groups")
        return [item["cycle_id"] for group in groups for item in group["members"]]
    except Exception:
        return []


class _Folder:
    """The one campaign folder a scoped sweep looks at, shaped like a `scandir` entry."""

    def __init__(self, path: Path):
        self.path, self.name = str(path), path.name

    def is_symlink(self) -> bool:
        return Path(self.path).is_symlink()

    def is_dir(self) -> bool:
        return Path(self.path).is_dir()


def _withdraw_empty_members(root: Path, memory: _Memory, *, deadline, now,
                            only_dir: Path | None = None) -> list[dict]:
    """Take workflow-group members that ended with no durable output out of their
    campaign's declaration, with the existing prepare -> apply -> verify, and
    record why.  A declaration that is missing or invalid, or a lock that is held,
    only defers this; it never touches a seal.  A declaration is not re-read
    until it or one of its members' cycle records changes."""
    import artifact_admission
    import artifact_producer
    import artifact_workflow_group_review as review
    import artifact_workflow_groups as groups
    rows: list[dict] = []
    records = root / ".runtime" / "artifact-producer" / "v1" / "cycles"
    if only_dir is not None:
        entries = [_Folder(only_dir)]
    else:
        try:
            entries = sorted(os.scandir(root / "campaigns"), key=lambda entry: entry.name)
        except OSError:
            return rows
    for entry in entries:
        if time.monotonic() >= deadline:
            break
        path = Path(entry.path) / groups.NAME
        try:
            if entry.is_symlink() or not entry.is_dir() or path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        signature = _signature(path)
        memo = memory.workflow_groups.get(entry.name)
        if (isinstance(memo, dict) and memo.get("declaration") == signature
                and all(_signature(records / f"{cycle_id}.json") == stamp
                        for cycle_id, stamp in (memo.get("members") or {}).items())):
            continue
        members = _declared_members(path)
        try:
            row = _withdraw_from(root, path, members, groups, review, artifact_producer, now=now)
        except artifact_admission.AdmissionBusy:
            continue   # a held lock: the next sweep asks again
        except groups.WorkflowGroupError as exc:
            if exc.code == "declaration-preimage-conflict":
                continue   # the declaration changed under us: so will the next look
            row = None
        except Exception:
            row = None
        if row:
            rows.append(row)
        memory.workflow_groups[entry.name] = {
            "declaration": _signature(path),
            "members": {cycle_id: _signature(records / f"{cycle_id}.json") for cycle_id in members}}
        memory.changed = True
    return rows


def _withdraw_from(root: Path, path: Path, members: list[str], groups, review, artifact_producer, *, now):
    """The withdrawal for one declaration, or None when there is nothing to withdraw."""
    ended = {}
    for cycle_id in members:
        record = artifact_producer.read_cycle_record(root, cycle_id)
        if _ended_empty(root, record):
            ended[cycle_id] = record
    if not ended:
        return None
    declaration = json.loads(path.read_text(encoding="utf-8"))
    campaign_id = declaration["campaign_id"]
    plan = groups.prepare_withdrawal(root, campaign_id, list(ended))
    if plan is None:
        return None
    groups.apply(root, plan, lock_timeout=0)
    groups.verify(root, campaign_id, expected=plan["after_sha256"])
    left = {item["cycle_id"]: group["group_id"] for group in declaration["groups"] for item in group["members"]}
    review.record_outcomes(
        root, [review.Outcome(
            cycle_id=cycle_id, campaign_id=campaign_id, verdict="withdrawn-empty", group_id=left.get(cycle_id),
            reason="cycle ended with no durable output (no manifest); withdrawn by route auto-close",
            cycle_state=record["state"], declaration_sha256=plan["after_sha256"], profile=None)
            for cycle_id, record in ended.items() if cycle_id in left],
        mode="autoclose", now=now, lock_timeout=0)
    return {"campaign_id": campaign_id, "cycle_ids": [cycle_id for cycle_id in ended if cycle_id in left],
            "groups_removed": len(declaration["groups"]) - len(plan["document"]["groups"]),
            "result": "withdrawn"}


def campaign_key_of(route: Mapping[str, Any]) -> str | None:
    """The campaign key a composed route names; None for a route that joins its parent cycle's campaign."""
    key = route.get("campaign_key")
    if isinstance(key, str) and key:
        return key
    if route.get("campaign_unassigned"):
        import artifact_producer
        return artifact_producer.UNASSIGNED_KEY
    return None


def campaign_of_route(root, route: Mapping[str, Any]) -> tuple[str, Path] | None:
    """`(campaign_id, campaign folder)` of the campaign a composed route belongs to, or None.

    The folder is found from the `campaigns/` name list; only a folder whose name carries the
    key's slug has its `campaign.json` read, so no unrelated campaign is opened.  A campaign the
    names do not reveal (a new one, begun by `start`, or an old locator name) is None: nothing
    of this route's campaign needs closing yet, and `campaign-status` still sweeps every one."""
    try:
        import artifact_campaign
        import artifact_locator
        import artifact_producer
        key = campaign_key_of(route)
        if key is None:
            return None
        slug = artifact_producer._campaign_naming(
            None if key == artifact_producer.UNASSIGNED_KEY else str(key))[0]
        root = Path(root)
        for name in sorted(artifact_locator._campaign_entries(root)):
            rest = name.partition("_")[2]
            if rest != slug and not rest.startswith(slug + "-"):
                continue
            path = root / "campaigns" / name / "campaign.json"
            record = artifact_producer._read_json(path)
            if not record or record.get("key") != key:
                continue
            folded = artifact_campaign.fold_campaign(root, path, record)
            if folded.get("state") == "active":
                return str(folded["campaign_id"]), path.parent
    except Exception:
        return None
    return None


def sweep(artifact_root, *, api, trigger: str, campaign_id: str | None = None,
          now: float | None = None, budget: float = BUDGET_SECONDS,
          scope_campaign_id: str | None = None, scope_dir: Path | None = None,
          scope_key: str | None = None) -> dict:
    """Close what nobody works on under one artifact root.  Never raises.
    `campaign_id` names the campaign `campaign-close` is closing.  `scope_campaign_id` and
    `scope_dir` (both from `campaign_of_route`) limit the sweep to that one campaign; `scope_key` is the
    campaign key, which lets a route with no cycle yet be recognised as that campaign's."""
    summary: dict[str, Any] = {"closed": [], "cycles": [], "kept": {}, "errors": [], "deferred": 0}
    # Reading old routes prints lineage/registry advisories meant for their
    # owners; this bookkeeping pass reports one line of its own instead.
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            _sweep(summary, Path(artifact_root).resolve(), api=api, trigger=trigger,
                   campaign_id=campaign_id, now=time.time() if now is None else now, budget=budget,
                   scope=(scope_campaign_id, Path(scope_dir) if scope_dir else None, scope_key)
                   if scope_campaign_id else None)
        except Unreadable as exc:
            summary["kept"]["evidence-unreadable:" + str(exc)] = 1
        except Exception as exc:
            summary["errors"].append({"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return summary


def _campaign_members(root: Path, records: list[dict], campaign_id: str | None, *,
                      deadline: float | None = None) -> set[str]:
    """Route ids that seal the named campaign's open cycles."""
    import artifact_producer
    members = set()
    for record in records:
        if campaign_id and record.get("campaign_id") == campaign_id:
            try:
                members.add(artifact_producer._finalize_route(root, record, deadline=deadline)["route_id"])
            except Exception:
                continue
    return members


def _decision_unfinished(route: Mapping[str, Any], home, root: Path | None = None,
                         now: float | None = None) -> bool:
    """A framed route whose decision record is fixed while the route is still open: the
    runtime is between fixing the decision and closing (the same start resumes it), so
    the sweep leaves it alone -- while that record is recent.  A decision older than
    the idle limit, or one whose recorded first leg has already closed, no longer
    protects the frame (it protected forever and kept decided frames open for weeks)."""
    import route_plan
    if not route_plan.is_framed_route(route):
        return False
    directory = home()
    if not directory:
        return False
    record_path = Path(directory) / "artifacts" / route_plan.RECORD_RELATIVE
    try:
        written = record_path.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return True   # an unreadable record is not an expired one
    if (time.time() if now is None else now) - written >= IDLE_SECONDS:
        return False
    if root is None:
        return True
    try:
        record = route_plan.read_record(record_path)
    except (OSError, ValueError):
        return True
    first_leg = record.get("first_leg") if isinstance(record, dict) else None
    leg = first_leg.get("route") if isinstance(first_leg, dict) else None
    leg_id = leg.get("route_id") if isinstance(leg, dict) else None
    if not isinstance(leg_id, str) or not re.fullmatch(r"rt-[0-9a-f]{8,64}", leg_id):
        return True
    return not (Path(root) / ".runtime" / "routes" / f"{leg_id}.outcome.json").exists()


class _Evidence:
    """The never-close evidence, gathered once and only when a route is due."""

    def __init__(self, root: Path, api):
        registries = _registries(api)
        self.root, self.runtime = root, root / ".runtime"
        self.resource_routes, self.resource_paths, self.resource_activity = _resource_runs()
        self.held, self.owners = _attempts(registries)
        self.gate_roots = _gate_ledger_roots(registries)
        self.open_paths = _open_paths(root)

    def kept(self, route_id: str, home, route: Mapping[str, Any] | None = None,
             now: float | None = None) -> str | None:
        """Why this route stays open whatever the time; `home` yields its cycle
        directory and is asked only when the cheaper evidence is silent."""
        import inline_finish
        if route_id in self.held:
            return "owner-live"
        if _settlement_pending(route_id, self.owners):
            return "owner-settlement-pending"
        try:
            pending = inline_finish.pending_state(self.root, route_id)
        except Exception:
            return "finish-pending"
        if pending and pending.get("state") != "finished":
            return "finish-pending"
        if _waits_on_human(route_id, self.gate_roots):
            return "human-gate"
        if route is not None and _decision_unfinished(route, home, self.root,
                                                       time.time() if now is None else now):
            return "decision-pending"
        if route_id in self.resource_routes:
            return "resource-run"
        directory = home()
        if any(_under(item, directory) for item in self.resource_paths):
            return "resource-run"
        if any(_under(item, directory) for item in self.open_paths):
            return "cycle-in-use"
        return None


def _sweep(summary, root: Path, *, api, trigger, campaign_id, now, budget, scope=None) -> None:
    runtime = root / ".runtime"
    if not (runtime / "routes").is_dir():
        return
    if scope is not None and (scope[1] is None or not (scope[1] / "campaign.json").is_file()):
        summary["kept"]["scope-campaign-unresolved"] = 1   # nothing to sweep; the next one asks again
        return
    deadline = time.monotonic() + budget
    with (runtime / "route-autoclose.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            summary["busy"] = True
            return
        memory = _Memory(runtime)
        try:
            _pass(summary, root, api, memory, trigger=trigger, campaign_id=campaign_id, now=now,
                  deadline=deadline, scope=scope)
        finally:
            memory.save()


def _pass(summary, root: Path, api, memory: _Memory, *, trigger, campaign_id, now, deadline,
          scope=None) -> None:
    def kept(reason):
        summary["kept"][reason] = summary["kept"].get(reason, 0) + 1

    _claude_state.cache_clear()   # one answer per session per sweep
    holders, activity = _composers(api)
    current = _current_identity()
    cycle_scope = scope[:2] if scope is not None else None
    records = _open_cycles(root, memory, scope=cycle_scope)
    cycles = _cycles_by_route(records)
    members = _campaign_members(root, records, campaign_id, deadline=deadline)
    import artifact_cross_root_move
    historical_routes = artifact_cross_root_move.historical_route_ids(root)
    files = sorted(_open_route_files(root, api), key=_mtime)
    evidence = []

    def gather() -> _Evidence:
        """The never-close evidence, read once, and only once something needs it."""
        if not evidence:
            evidence.append(_Evidence(root, api))
        return evidence[0]

    for index, path in enumerate(files):
        if time.monotonic() >= deadline:
            summary["deferred"] += len(files) - index
            break
        route_id = path.stem
        if route_id in historical_routes:
            kept("relocated-history")
            continue
        remembered = memory.failed_routes.get(route_id)
        if remembered and remembered.get("signature") == _signature(path):
            kept("close-failed-before")
            continue
        holder = holders.get(route_id)
        rules = dict(activity=activity, current=current, now=now, member=route_id in members)
        # Time first, from what costs a stat: a route written recently is kept
        # without reading any evidence.
        reason, why = _decide(holder, idle_since=max(_mtime(path), float(holder.get("ts") or 0) if holder else 0.0),
                              **rules)
        if reason is None:
            kept(why)
            continue
        raw = None
        if scope is not None and route_id not in cycles and reason != "idle":
            # Session-ended cleanup remains campaign-scoped. Overdue idle routes
            # proceed through the shared protection checks below.
            if memory.recently_kept("foreign:" + route_id, now):
                kept("other-campaign")
                continue
            raw = _read_route(path)
            if raw is None:
                continue
            if scope[2] is None or campaign_key_of(raw) != scope[2]:
                memory.keep("foreign:" + route_id, "other-campaign", now)
                kept("other-campaign")
                continue
        # campaign-close is the user's explicit end: judge its members now.
        remembered_why = None if route_id in members else memory.recently_kept("route:" + route_id, now)
        if remembered_why:
            kept(remembered_why)
            continue
        raw = raw or _read_route(path)
        if raw is None:
            continue
        record = cycles.get(route_id)
        home = _cycle_home(root, record) if record else None
        why = gather().kept(route_id, lambda: home, raw, now=now)
        if why is None:
            reason, why = _decide(holder, idle_since=_last_activity(
                root, path, raw, holder, api, record, now, deep=True,
                resource_activity=gather().resource_activity), **rules)
        if why is not None:
            memory.keep("route:" + route_id, why, now)
            kept(why)
            continue
        try:
            summary["closed"].append(close_one(root, path, raw, reason, api, trigger=trigger, now=now,
                                                 deadline=deadline))
        except Exception as exc:
            summary["errors"].append({"route_id": route_id, "error": str(exc)[:160]})
            if not _transient(str(exc)):
                memory.failed_routes[route_id] = {"signature": _signature(path), "reason": str(exc)[:160],
                                                  "at": _iso(now)}
                memory.changed = True
    if time.monotonic() < deadline:
        if summary["closed"]:
            records = _open_cycles(root, memory, scope=cycle_scope)
        summary["cycles"] = _seal_unsealed_cycles(root, api, records, gather, memory,
                                                  deadline=deadline, now=now, campaign_id=campaign_id)
    if time.monotonic() < deadline:
        withdrawn = _withdraw_empty_members(root, memory, deadline=deadline, now=now,
                                            only_dir=scope[1] if scope is not None else None)
        if withdrawn:
            summary["withdrawn"] = withdrawn


def report(summary: Mapping[str, Any], stream=None) -> None:
    """One stderr line; silent when nothing happened."""
    stream = stream or sys.stderr
    closed, cycles, errors = summary.get("closed") or [], summary.get("cycles") or [], summary.get("errors") or []
    withdrawn = sum(len(row["cycle_ids"]) for row in summary.get("withdrawn") or [])
    if not (closed or cycles or errors or summary.get("deferred") or withdrawn):
        return
    reasons: dict[str, int] = {}
    for row in closed:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    left_open = sum(_left_open(row["cycle"]) for row in cycles)
    print("route_autoclose closed=%d cycles_sealed=%d cycles_left_open=%d deferred=%d errors=%d reasons=%s" % (
        len(closed), len(cycles) - left_open, left_open, summary.get("deferred", 0), len(errors),
        ",".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "-") + (
        f" withdrawn={withdrawn}" if withdrawn else ""), file=stream)
