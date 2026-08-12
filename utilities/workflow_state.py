#!/usr/bin/env python3
"""Portable tracked-workflow state machine, ledger, and exactly-once claims.

`core/WORKFLOW.md §0.6` defines the contract; `capabilities/topologies.json` owns the
vocabulary; this module is its executable form and the single implementation every
capability shares. It deliberately holds no capability knowledge: lab, code, ship,
spec/research, CI cycles, monitors, and loops all use the same states.

Two durability rules make restart and duplicate supervisors safe:

* the append-only journal is the source of truth and `state.json` is a derived cache,
  so a crash between the two is repaired on the next read instead of losing a stage;
* a successor is started only by the process that wins an `O_CREAT|O_EXCL` claim file
  keyed by the sealed route, the predecessor node, the predecessor's exact terminal
  identity, and the successor node — so a second supervisor, a restart, or an operator
  poll reads the existing claim and starts nothing.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root  # noqa: E402

LEDGER_SCHEMA_VERSION = 1

#: Node-level states are the subset of the portable vocabulary one stage can occupy.
NODE_STATES = (
    "READY", "RUNNING", "STAGE_SUCCEEDED",
    "BLOCKED_HUMAN_GATE", "FAILED_RETRYABLE", "FAILED_TERMINAL", "CANCELLED",
)


class WorkflowStateError(ValueError):
    """Raised for an invalid state, transition, or ledger operation."""


def _registry_path() -> Path:
    override = os.environ.get("AGENT_TOPOLOGY_REGISTRY")
    return Path(override) if override else ROOT / "capabilities" / "topologies.json"


_VOCABULARY_CACHE: dict = {}


def vocabulary(registry_path=None) -> dict:
    """Read states/transitions/continuations from the registry — never re-declare them."""
    path = Path(registry_path) if registry_path else _registry_path()
    key = str(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError as exc:
        raise WorkflowStateError(f"topology registry unreadable: {exc}") from exc
    cached = _VOCABULARY_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowStateError(f"topology registry unreadable: {exc}") from exc
    try:
        data = {
            "states": tuple(registry["workflow_states"]),
            "failure_states": tuple(registry["workflow_failure_states"]),
            "transitions": {k: tuple(v) for k, v in registry["workflow_transitions"].items()},
            "continuation_kinds": dict(registry["continuation_kinds"]),
            "human_gate_positions": tuple(registry["human_gate_positions"]),
        }
    except (KeyError, TypeError) as exc:
        raise WorkflowStateError(f"topology registry lacks workflow vocabulary: {exc}") from exc
    _VOCABULARY_CACHE[key] = (stamp, data)
    return data


def known_states(registry_path=None) -> frozenset:
    vocab = vocabulary(registry_path)
    return frozenset(vocab["states"]) | frozenset(vocab["failure_states"])


def can_transition(current: str, target: str, registry_path=None) -> bool:
    vocab = vocabulary(registry_path)
    if current not in vocab["transitions"] or target not in known_states(registry_path):
        return False
    return target in vocab["transitions"][current]


def assert_transition(current: str, target: str, registry_path=None) -> str:
    if current == target:
        return target
    if not can_transition(current, target, registry_path):
        raise WorkflowStateError(f"illegal workflow transition {current} -> {target}")
    return target


def assert_node_state(state: str) -> str:
    if state not in NODE_STATES:
        raise WorkflowStateError(f"invalid node state: {state!r}")
    return state


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def successor_key(route_hash: str, predecessor_node: str, predecessor_identity: str,
                  successor_node: str) -> str:
    """Stable, collision-resistant identity for one predecessor→successor advance.

    Binding the predecessor's *exact terminal identity* (run/attempt id plus pid, start
    time, and exit result) means a legitimate retry of the predecessor produces a
    different key and may advance again, while any number of observers of the *same*
    termination converge on one claim.
    """
    # NUL separator: no field value can contain it, so distinct tuples cannot
    # collide by concatenation.
    payload = "\\0".join(
        (route_hash, predecessor_node, predecessor_identity, successor_node)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def default_ledger_root() -> Path:
    """`AGENT_WORKFLOW_ROOT` stays the explicit override; otherwise the ledger root is
    the same validated agent-home every other dispatch-state writer/reader agrees on.

    A bare `AGENT_HOME`-or-`ROOT` fallback resolves to the *caller's own worktree* when
    `AGENT_HOME` is unset, silently splitting the ledger from the real install — this is
    the exact false-clean bug this cycle exists to fix (a linked worktree running
    `workflow-supervisor.py status` reported `CREATED`/empty instead of the real
    `RUNNING` state recorded under the actual `AGENT_HOME`).
    """
    override = os.environ.get("AGENT_WORKFLOW_ROOT")
    if override:
        return Path(override).expanduser()
    return resolve_dispatch_state_root(resolve_agent_home()) / "workflow"


class WorkflowLedger:
    """Durable per-route workflow ledger: journal, derived cache, and claim set."""

    def __init__(self, route_id: str, route_hash: str = "", root=None, registry_path=None):
        if not route_id or "/" in route_id or route_id.startswith("."):
            raise WorkflowStateError(f"unsafe route id: {route_id!r}")
        self.route_id = route_id
        self.route_hash = route_hash
        self.registry_path = registry_path
        self.root = Path(root or default_ledger_root()).expanduser() / route_id
        self.journal_path = self.root / "journal.jsonl"
        self.state_path = self.root / "state.json"
        self.claims_dir = self.root / "claims"
        self.lock_path = self.root / ".workflow.lock"

    # -- locking -----------------------------------------------------------------
    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- journal -----------------------------------------------------------------
    def journal(self) -> list:
        try:
            raw = self.journal_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                # A torn final line from a crash mid-append is dropped, never guessed at.
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def _append(self, entry: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- derived state -----------------------------------------------------------
    def _rebuild(self, entries) -> dict:
        nodes: dict = {}
        workflow_state = "CREATED"
        for entry in entries:
            node = entry.get("node")
            state = entry.get("state")
            if node and state in NODE_STATES:
                nodes[node] = {
                    "state": state,
                    "updated_at": entry.get("at"),
                    "evidence": entry.get("evidence"),
                }
            candidate = entry.get("workflow_state")
            if candidate in known_states(self.registry_path):
                workflow_state = candidate
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "route_id": self.route_id,
            "route_hash": self.route_hash,
            "workflow_state": workflow_state,
            "nodes": nodes,
            "journal_entries": len(entries),
            "updated_at": entries[-1].get("at") if entries else None,
        }

    def state(self) -> dict:
        """Return the current derived state, repairing the cache after a crash."""
        entries = self.journal()
        rebuilt = self._rebuild(entries)
        try:
            cached = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached != rebuilt and entries:
            _atomic_write(self.state_path, json.dumps(rebuilt, indent=2, sort_keys=True) + "\n")
        return rebuilt

    def read_only_state(self) -> dict:
        """Derive workflow state from the journal alone, mutating nothing on disk.

        Unlike `state()`, this never repairs `state.json`, never creates the ledger root
        directory, and never touches locks, claims, armed rows, registry entries, or
        outcome files -- `journal()` only reads, and `_rebuild()` is pure computation. A
        read-only survey over a whole artifact root must use this, not `state()`: calling
        `state()` on a route with a stale cache silently writes a repair, which is a
        mutation a survey has no business making.

        The returned dict adds three typed markers `state()` does not need:
        - `ledger_dir_exists`: the route's ledger directory is present at all.
        - `journal_exists`: `journal.jsonl` specifically is present.
        - `journal_unreadable`: the journal file exists but every line failed to parse
          (as opposed to a legitimately empty/absent journal), so a caller can tell
          "no history yet" from "history is corrupt" instead of treating both as CREATED.
        """
        journal_exists = self.journal_path.is_file()
        entries = self.journal()
        rebuilt = self._rebuild(entries)
        raw_nonblank = False
        if journal_exists:
            try:
                raw = self.journal_path.read_text(encoding="utf-8", errors="replace")
                raw_nonblank = any(line.strip() for line in raw.splitlines())
            except OSError:
                raw_nonblank = False
        return {
            **rebuilt,
            "ledger_dir_exists": self.root.is_dir(),
            "journal_exists": journal_exists,
            "journal_unreadable": journal_exists and raw_nonblank and not entries,
        }

    def record(self, node: str, state: str, *, evidence=None, workflow_state=None,
               actor="workflow-supervisor") -> dict:
        """Append one confirmed transition, then refresh the derived cache."""
        assert_node_state(state)
        current = self.state()
        previous = (current["nodes"].get(node) or {}).get("state")
        if previous and previous != state:
            assert_transition(previous, state, self.registry_path)
        if workflow_state is not None:
            assert_transition(current["workflow_state"], workflow_state, self.registry_path)
        entry = {
            "at": now_iso(),
            "route_id": self.route_id,
            "route_hash": self.route_hash,
            "node": node,
            "state": state,
            "workflow_state": workflow_state,
            "evidence": evidence,
            "actor": actor,
            "pid": os.getpid(),
        }
        self._append(entry)
        return self.state()

    def set_workflow_state(self, workflow_state: str, *, evidence=None,
                           actor="workflow-supervisor") -> dict:
        current = self.state()
        assert_transition(current["workflow_state"], workflow_state, self.registry_path)
        self._append({
            "at": now_iso(),
            "route_id": self.route_id,
            "route_hash": self.route_hash,
            "node": None,
            "state": None,
            "workflow_state": workflow_state,
            "evidence": evidence,
            "actor": actor,
            "pid": os.getpid(),
        })
        return self.state()

    # -- exactly-once claims -----------------------------------------------------
    def claim_path(self, key: str) -> Path:
        if not key or not all(character in "0123456789abcdef" for character in key):
            raise WorkflowStateError(f"unsafe claim key: {key!r}")
        return self.claims_dir / f"{key}.claim"

    def claim(self, key: str, payload: dict):
        """Create the claim exclusively. Returns (created, stored_payload).

        The filesystem, not a scheduler, is the arbiter: whoever creates the file starts
        the successor, and every later observer — duplicate supervisor, restart, manual
        poll — reads it back and does nothing.
        """
        path = self.claim_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({**payload, "claimed_at": now_iso(), "claimed_by_pid": os.getpid()},
                          ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                return False, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False, {}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return True, json.loads(body)

    def claims(self) -> dict:
        rows = {}
        if not self.claims_dir.is_dir():
            return rows
        for path in sorted(self.claims_dir.glob("*.claim")):
            try:
                rows[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rows[path.stem] = {"malformed": True}
        return rows


def derive_workflow_state(node_states: dict, terminal_nodes, *, terminal_gates_passed=False,
                          open_human_gate=False, pending_claims=0,
                          registry_path=None) -> str:
    """Aggregate node states into one workflow state.

    Order is deliberate: a failure or an open human gate outranks any amount of
    downstream success, and `COMPLETE` is reported only when every declared terminal
    node succeeded *and* its completion gate was independently verified.
    """
    del registry_path  # vocabulary is fixed; kept for signature symmetry
    states = {node: row.get("state") for node, row in (node_states or {}).items()}
    if not states:
        return "CREATED"
    values = set(states.values())
    if "FAILED_TERMINAL" in values:
        return "FAILED_TERMINAL"
    if "CANCELLED" in values:
        return "CANCELLED"
    if "BLOCKED_HUMAN_GATE" in values or open_human_gate:
        return "BLOCKED_HUMAN_GATE"
    if "FAILED_RETRYABLE" in values:
        return "FAILED_RETRYABLE"
    terminal = list(terminal_nodes or [])
    if terminal and all(states.get(node) == "STAGE_SUCCEEDED" for node in terminal):
        return "COMPLETE" if terminal_gates_passed else "TERMINAL_VERIFY"
    if "RUNNING" in values:
        return "NEXT_RUNNING" if len(states) > 1 else "RUNNING"
    if pending_claims:
        return "NEXT_REGISTERED"
    if values == {"STAGE_SUCCEEDED"}:
        return "STAGE_SUCCEEDED"
    return "READY"


def route_terminal_nodes(route: dict) -> list:
    return [node["id"] for node in route.get("nodes", []) if node.get("terminal") is True]


def route_continuation(route: dict, node_id: str):
    for node in route.get("nodes", []):
        if node.get("id") == node_id:
            return node.get("continuation")
    return None


def route_node(route: dict, node_id: str):
    for node in route.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


def route_successors(route: dict, node_id: str) -> list:
    return [node["id"] for node in route.get("nodes", [])
            if node_id in (node.get("depends_on") or [])]


def unix_now() -> float:
    return time.time()
