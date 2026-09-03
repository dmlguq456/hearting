#!/usr/bin/env python3
"""Portable continuation supervisor for tracked workflows.

One implementation serves every capability (`core/OPERATIONS.md §5.12`). It is a
non-model process: it starts only the successor its sealed route already declares, it
opens no dispatch depth, and it never decides *what* work to do — only whether the
declared next stage may start yet.

Advance requires four independent proofs about the predecessor — exact process
identity, a terminal result, a sentinel or typed terminal handoff, and the declared
output artifacts — and it is claimed exactly once through the filesystem. Anything
missing is a refusal, not an assumption: the 2026-08-04 BC_ResNet_tf run finished
training and nothing owned what came next, so "the process is gone" must never be
read as "the stage succeeded".

  workflow-supervisor.py arm     --route R --node N --predecessor-kind resource|registered ...
  workflow-supervisor.py poll    --route R
  workflow-supervisor.py watch   --route R --max 3600
  workflow-supervisor.py gate    --route R --gate G --release|--block
  workflow-supervisor.py status  --route R [--json]
  workflow-supervisor.py complete --route R
  workflow-supervisor.py survey  --artifact-root ROOT [--stale-after-seconds S] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import workflow_state as WS  # noqa: E402
import resource_run_registry as RR  # noqa: E402
import dispatch_pending_delivery as PENDING  # noqa: E402

ARMED_SCHEMA_VERSION = 1
PREDECESSOR_KINDS = ("resource", "registered")
DEFAULT_POLL_INTERVAL = 5.0
MAX_WATCH_SECONDS = 86400.0
SURVEY_SCHEMA_VERSION = 1
DEFAULT_STALE_AFTER_SECONDS = 86400.0
# Highest risk first. `complete` is not one of the plan's six ranked tiers -- it is the
# positively-proven safe label, so it sorts last, below `unknown`.
RISK_TIER_ORDER = (
    "abandoned", "closure-mismatch", "stale-open", "active-or-owned", "parked",
    "unknown", "complete",
)
RISK_TIER_RANK = {tier: index for index, tier in enumerate(RISK_TIER_ORDER)}


class SupervisorError(ValueError):
    pass


def _load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_RUNNER = None
_ROUTE = None


def runner():
    """Load `resource-runner.py` (dashed name) for its shared settle/sentinel logic."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = _load_module("resource_runner_cli", "utilities/resource-runner.py")
    return _RUNNER


def route_module():
    """Load `capability-route.py` for the shared read-only terminal-gate seam.

    One-way dependency only: this module reaches into `capability-route.py`, which
    must never import anything from `workflow_state`/`workflow-supervisor`/
    `resource-runner` back.
    """
    global _ROUTE
    if _ROUTE is None:
        _ROUTE = _load_module("capability_route_cli", "utilities/capability-route.py")
    return _ROUTE


def load_route(path):
    try:
        route = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"route unreadable: {exc}") from exc
    if not isinstance(route, dict) or "route_id" not in route or "nodes" not in route:
        raise SupervisorError("route record is not a compiled capability route")
    return route


def ledger_for(route):
    return WS.WorkflowLedger(route["route_id"], route.get("route_hash", ""))


def armed_dir(ledger):
    return ledger.root / "armed"


def read_armed(ledger):
    rows = {}
    directory = armed_dir(ledger)
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("node"):
            rows[row["node"]] = row
    return rows


# --------------------------------------------------------------------------------
# predecessor evidence
# --------------------------------------------------------------------------------

def resource_evidence(armed):
    """Terminal evidence for a detached resource child, settled if it is gone."""
    registry = Path(armed["resource_registry"])
    run_id = armed["predecessor_id"]
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        row = (data.get("runs") or {}).get(run_id)
    except (OSError, ValueError) as exc:
        return {"terminal": False, "reason": f"resource-registry-unreadable:{exc}"}
    if not isinstance(row, dict):
        return {"terminal": False, "reason": "resource-run-absent"}
    row, _settled = runner().settle(registry, run_id, row)
    liveness, _current, reason = RR.classify_identity(row)
    identity = f"{run_id}:{row.get('pid')}:{row.get('starttime')}:{row.get('exit_code')}"
    if liveness == "working":
        return {"terminal": False, "reason": "resource-still-running", "liveness": liveness,
                "identity": identity}
    status = row.get("status")
    if status not in ("succeeded", "failed"):
        # Gone but unsettled means the observation could not be persisted; refuse.
        return {"terminal": False, "reason": f"resource-unsettled:{status}", "liveness": liveness,
                "identity": identity}
    return {
        "terminal": True,
        "succeeded": status == "succeeded",
        "identity": identity,
        "liveness": liveness,
        "exit_code": row.get("exit_code"),
        "sentinel": row.get("sentinel"),
        "sentinel_present": bool(row.get("sentinel")) and Path(str(row["sentinel"])).is_file(),
        "ended_at": row.get("ended_at"),
        "failure_class": row.get("failure_class"),
        "reason": reason,
        "log": row.get("log"),
        "parent_attempt_id": row.get("parent_attempt_id"),
    }


def _registry_rows(jobs_path):
    try:
        raw = Path(jobs_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SupervisorError(f"jobs registry unreadable: {exc}") from exc
    rows = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(
            part.split("=", 1) for part in fields[5].split(",") if "=" in part
        )
        rows.append({"time": fields[0], "status": fields[1], "repo": fields[2],
                     "worktree": fields[3], "slug": fields[4], "meta": metadata})
    return rows


def registered_evidence(armed):
    """Terminal evidence for a registered headless attempt."""
    attempt_id = armed["predecessor_id"]
    try:
        rows = _registry_rows(armed["jobs"])
    except SupervisorError as exc:
        return {"terminal": False, "reason": str(exc)}
    matches = [row for row in rows if row["meta"].get("attempt_id") == attempt_id]
    if not matches:
        return {"terminal": False, "reason": "attempt-row-absent"}
    row = matches[-1]
    meta = row["meta"]
    identity = f"{attempt_id}:{meta.get('pid')}:{meta.get('pid_start')}:{row['status']}"
    if row["status"] != "done":
        return {"terminal": False, "reason": f"attempt-open:{row['status']}",
                "identity": identity}
    note = meta.get("note") or ""
    failure_class = meta.get("failure_class") or ""
    succeeded = (
        note in ("completed-marker", "completed-supervisor", "completed")
        and failure_class in ("", "pass")
    )
    # A live exact PID after a terminal row is draining, not quiescent: the successor
    # must not start while the predecessor's process group still holds resources.
    quiescent = True
    pid, pid_start = meta.get("pid"), meta.get("pid_start")
    if pid and pid_start:
        current = RR.proc_identity(pid)
        if current and str(current["starttime"]) == str(pid_start):
            quiescent = False
    return {
        "terminal": True,
        "succeeded": succeeded,
        "quiescent": quiescent,
        "identity": identity,
        "note": note,
        "failure_class": failure_class,
        "reason": "attempt-terminal",
    }


def artifact_evidence(armed):
    """Declared outputs must actually exist before a successor may consume them.

    Only concrete declared names are checked. A glob (`logs/**`) and an abstract
    handoff name are deliberately not invented into paths — an unverifiable check that
    silently passes is worse than a recorded `checked: false`.
    """
    base = armed.get("artifact_base")
    concrete = [name for name in (armed.get("declared_outputs") or [])
                if isinstance(name, str) and "*" not in name and "/" not in name and "." in name]
    if not base:
        return {"checked": False, "reason": "no-artifact-base", "missing": []}
    if not concrete:
        return {"checked": False, "reason": "no-concrete-declared-output", "missing": []}
    missing = [name for name in concrete if not (Path(base) / name).exists()]
    return {"checked": True, "missing": missing, "reason": "artifacts-present" if not missing
            else "declared-artifact-missing"}


# --------------------------------------------------------------------------------
# completion markers
# --------------------------------------------------------------------------------

def terminal_gate_state(route):
    """Report, per terminal node, whether its completion gate is actually proven.

    Delegates to the shared `capability-route.py` seam so `status`/`complete` here and
    `close`'s outcome sidecar always agree on gate truth from the same evidence.
    """
    return route_module().terminal_gate_observation(route)


# --------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------

def cmd_arm(args):
    route = load_route(args.route)
    node = WS.route_node(route, args.node)
    if node is None:
        raise SupervisorError(f"unknown route node: {args.node}")
    continuation = node.get("continuation")
    if not isinstance(continuation, dict):
        raise SupervisorError(
            f"node {args.node} is terminal or declares no continuation; nothing to supervise"
        )
    kind = continuation["kind"]
    if kind not in ("supervised", "monitor"):
        raise SupervisorError(
            f"node {args.node} declares continuation {kind}; a supervisor governs only "
            "supervised and monitor continuations"
        )
    if args.predecessor_kind not in PREDECESSOR_KINDS:
        raise SupervisorError("invalid predecessor kind")
    if args.predecessor_kind == "resource" and not args.resource_registry:
        raise SupervisorError("--resource-registry is required for a resource predecessor")
    if args.predecessor_kind == "registered" and not args.jobs:
        raise SupervisorError("--jobs is required for a registered predecessor")
    successors = WS.route_successors(route, args.node)
    if not successors:
        raise SupervisorError(f"node {args.node} has no declared successor")
    command = None
    if args.successor_command:
        try:
            command = json.loads(args.successor_command)
        except ValueError as exc:
            raise SupervisorError(f"--successor-command must be a JSON argv array: {exc}")
        if not isinstance(command, list) or not command or not all(
                isinstance(part, str) for part in command):
            raise SupervisorError("--successor-command must be a non-empty JSON array of strings")
    elif not args.successor_external:
        # An armed watch with no way to start the next stage is the failure this tool
        # exists to prevent, so the caller must say so out loud.
        raise SupervisorError(
            "supervised continuation requires --successor-command, or an explicit "
            "--successor-external declaring that another checked surface starts it"
        )
    if kind == "monitor" and not args.monitor_evidence:
        raise SupervisorError("monitor continuation requires --monitor-evidence")
    record = {
        "schema_version": ARMED_SCHEMA_VERSION,
        "route_id": route["route_id"],
        "route_hash": route.get("route_hash"),
        "route_file": str(Path(args.route).resolve()),
        "node": args.node,
        "continuation_kind": kind,
        "monitor": continuation.get("monitor"),
        "monitor_evidence": args.monitor_evidence,
        "predecessor_kind": args.predecessor_kind,
        "predecessor_id": args.predecessor_id,
        "resource_registry": str(Path(args.resource_registry).resolve())
        if args.resource_registry else None,
        "jobs": str(Path(args.jobs).resolve()) if args.jobs else None,
        "successors": successors,
        "successor_command": command,
        "successor_external": bool(args.successor_external),
        "successor_cwd": args.successor_cwd or route.get("cwd"),
        "successor_log": args.successor_log,
        "artifact_base": str(Path(args.artifact_base).resolve()) if args.artifact_base else None,
        "declared_outputs": list(node.get("outputs") or []),
        "armed_at": WS.now_iso(),
    }
    ledger = ledger_for(route)
    with ledger.lock():
        armed_dir(ledger).mkdir(parents=True, exist_ok=True)
        target = armed_dir(ledger) / f"{args.node}.json"
        target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        state = ledger.state()
        if state["workflow_state"] == "CREATED":
            ledger.set_workflow_state("READY", evidence={"armed": args.node}, actor="arm")
        if args.node not in state["nodes"]:
            ledger.record(args.node, "RUNNING", evidence={"armed": True}, actor="arm")
        # A watch is armed on a stage that is already executing, so the workflow is
        # RUNNING from this point; leaving it READY would make the first legitimate
        # failure an illegal transition.
        if ledger.state()["workflow_state"] == "READY":
            ledger.set_workflow_state("RUNNING", evidence={"armed": args.node}, actor="arm")
    print(json.dumps({"armed": args.node, "successors": successors,
                      "continuation": kind}, sort_keys=True))
    return 0


def _start_successor(armed, successor, key):
    command = armed.get("successor_command")
    if not command:
        return {"started": False, "surface": "external",
                "reason": "successor start is owned by a declared external checked surface"}
    log_path = armed.get("successor_log")
    stdout = None
    handle = None
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab", buffering=0)
        stdout = handle
    try:
        environment = {
            **os.environ,
            "AGENT_WORKFLOW_ROUTE_ID": armed["route_id"],
            "AGENT_WORKFLOW_NODE": successor,
            "AGENT_WORKFLOW_CLAIM": key,
        }
        proc = subprocess.Popen(
            command, cwd=armed.get("successor_cwd") or None, env=environment,
            stdout=stdout, stderr=subprocess.STDOUT if stdout else None,
            start_new_session=True,
        )
    finally:
        if handle is not None:
            handle.close()
    identity = RR.proc_identity(proc.pid) or {}
    return {"started": True, "surface": "detached", "pid": proc.pid,
            "starttime": identity.get("starttime"), "command": command,
            "log": log_path}


def _claim_successors(route, ledger, armed, node_id, successors, evidence=None):
    """Exactly-once successor claim + start, factored out of `_evaluate` (A50-8)
    so ordinary poll advancement and an explicit `release --decision proceed`
    share the one claim primitive (`ledger.claim`) instead of each growing its
    own copy of this loop."""
    started = []
    for successor in successors:
        key = WS.successor_key(route.get("route_hash", ""), node_id,
                               str((evidence or {}).get("identity")), successor)
        created, claim = ledger.claim(key, {
            "route_id": route["route_id"], "predecessor": node_id, "successor": successor,
            "predecessor_identity": (evidence or {}).get("identity"),
        })
        if not created:
            started.append({"successor": successor, "claim": key, "created": False,
                            "note": "already claimed", "claim_record": claim})
            continue
        outcome = _start_successor(armed, successor, key)
        started.append({"successor": successor, "claim": key, "created": True, **outcome})
    return started


def _evaluate(route, ledger, armed, results):
    node_id = armed["node"]
    kind = armed["continuation_kind"]
    if armed["predecessor_kind"] == "resource":
        evidence = resource_evidence(armed)
    else:
        evidence = registered_evidence(armed)
    artifacts = artifact_evidence(armed)
    evidence["artifacts"] = artifacts
    row = {"node": node_id, "evidence": evidence}

    if not evidence.get("terminal"):
        row["action"] = "wait"
        results.append(row)
        return
    if evidence.get("quiescent") is False:
        row["action"] = "wait-draining"
        results.append(row)
        return
    if not evidence.get("succeeded"):
        ledger.record(node_id, "FAILED_RETRYABLE", evidence=evidence, actor="poll")
        ledger.set_workflow_state("FAILED_RETRYABLE", evidence={"node": node_id}, actor="poll")
        row["action"] = "halt-failed"
        results.append(row)
        return
    if artifacts.get("checked") and artifacts.get("missing"):
        ledger.record(node_id, "FAILED_RETRYABLE", evidence=evidence, actor="poll")
        ledger.set_workflow_state("FAILED_RETRYABLE", evidence={"node": node_id}, actor="poll")
        row["action"] = "halt-missing-artifact"
        results.append(row)
        return
    if kind == "monitor":
        matched = False
        try:
            monitor = json.loads(Path(armed["monitor_evidence"]).read_text(encoding="utf-8"))
            matched = monitor.get("condition") == "matched"
        except (OSError, ValueError, AttributeError, TypeError):
            matched = False
        if not matched:
            row["action"] = "wait-monitor"
            results.append(row)
            return
        evidence["monitor"] = "matched"

    ledger.record(node_id, "STAGE_SUCCEEDED", evidence=evidence, actor="poll")
    started = _claim_successors(route, ledger, armed, node_id, armed["successors"], evidence)
    row["action"] = "advanced"
    row["successors"] = started
    if any(entry.get("created") for entry in started):
        current = ledger.state()["workflow_state"]
        if WS.can_transition(current, "NEXT_REGISTERED"):
            ledger.set_workflow_state("NEXT_REGISTERED",
                                      evidence={"node": node_id, "successors": armed["successors"]},
                                      actor="poll")
        if any(entry.get("started") for entry in started):
            current = ledger.state()["workflow_state"]
            if WS.can_transition(current, "NEXT_RUNNING"):
                ledger.set_workflow_state("NEXT_RUNNING", evidence={"node": node_id},
                                          actor="poll")
    results.append(row)


def poll_once(route, ledger):
    results = []
    with ledger.lock():
        for node_id, armed in sorted(read_armed(ledger).items()):
            state = ledger.state()["nodes"].get(node_id, {}).get("state")
            if state in ("STAGE_SUCCEEDED", "FAILED_TERMINAL", "CANCELLED"):
                results.append({"node": node_id, "action": "settled", "state": state})
                continue
            if state == "FAILED_RETRYABLE":
                results.append({"node": node_id, "action": "halted", "state": state})
                continue
            if armed["continuation_kind"] == "human-gate":
                results.append({"node": node_id, "action": "human-gate"})
                continue
            _evaluate(route, ledger, armed, results)
    return results


def cmd_poll(args):
    route = load_route(args.route)
    ledger = ledger_for(route)
    results = poll_once(route, ledger)
    print(json.dumps({"route_id": route["route_id"],
                      "workflow_state": ledger.state()["workflow_state"],
                      "results": results}, sort_keys=True))
    return 0


def cmd_watch(args):
    route = load_route(args.route)
    ledger = ledger_for(route)
    interval = max(1.0, float(args.interval))
    deadline = time.monotonic() + min(max(1.0, float(args.max)), MAX_WATCH_SECONDS)
    last = []
    while True:
        last = poll_once(route, ledger)
        state = ledger.state()["workflow_state"]
        if state in ("COMPLETE", "TERMINAL_VERIFY", "FAILED_TERMINAL", "FAILED_RETRYABLE",
                     "CANCELLED", "BLOCKED_HUMAN_GATE"):
            break
        if all(row.get("action") in ("advanced", "settled", "halted", "human-gate")
               for row in last) and last:
            break
        if time.monotonic() >= deadline:
            print(json.dumps({"route_id": route["route_id"], "timeout": True,
                              "workflow_state": state, "results": last}, sort_keys=True))
            return 3
        time.sleep(interval)
    print(json.dumps({"route_id": route["route_id"], "timeout": False,
                      "workflow_state": ledger.state()["workflow_state"],
                      "results": last}, sort_keys=True))
    return 0


def cmd_gate(args):
    route = load_route(args.route)
    ledger = ledger_for(route)
    gates = {row["gate"]: row for row in (route.get("human_gate_bindings") or [])}
    if args.gate not in gates:
        raise SupervisorError(f"route declares no human gate {args.gate!r}")
    payload = {"gate": args.gate}
    with ledger.lock():
        if args.release:
            state = ledger.state()["workflow_state"]
            if state != "BLOCKED_HUMAN_GATE":
                raise SupervisorError(f"workflow is {state}, not blocked on a human gate")
            actor_kind = release_actor_kind()
            released_by = resolved_released_by(actor_kind, args.by)
            ledger.set_workflow_state("RUNNING", evidence={"released_gate": args.gate,
                                                           "released_by": released_by,
                                                           "actor_kind": actor_kind},
                                      actor="gate")
            record_gate_release(route, args.route, gate=args.gate, decision="proceed",
                                released_by=released_by, actor_kind=actor_kind)
            retired = retire_gate_delivery(route, args.gate, args.jobs)
            payload.update({"released_by": released_by, "actor_kind": actor_kind,
                            "delivery_retired": retired})
            action = "released"
        else:
            # SD-123 (8)(a): the record and the transition are one transaction.
            # Creating the record first is what makes "transition failed ->
            # zero records" recoverable at all: the reverse order can leave a
            # blocked gate nobody can be told about, and there is no compensating
            # action for that. `delivery_id` is deterministic, so the unlink
            # below can only remove the record this call just created.
            if not args.artifact or args.artifact == "-":
                # Contract (a) names the reviewable artifact as part of the
                # record. A gate that arrives saying `artifact=-` tells the
                # person nothing to look at, which defeats the delivery.
                raise SupervisorError(
                    "gate-artifact-required: --artifact must name the artifact a "
                    "person reviews at this gate"
                )
            jobs_path = Path(args.jobs) if args.jobs else default_jobs_path()
            record_path, created = create_gate_delivery(
                route, args.gate, args.artifact, jobs_path,
                gate_raise_epoch(ledger, args.gate),
            )
            try:
                ledger.set_workflow_state("BLOCKED_HUMAN_GATE",
                                          evidence={"gate": args.gate,
                                                    "binding": gates[args.gate],
                                                    "delivery": str(record_path)},
                                          actor="gate")
            except BaseException:
                if created:
                    try:
                        record_path.unlink()
                    except OSError:
                        pass
                raise
            payload.update({"delivery": str(record_path), "delivery_created": created})
            action = "blocked"
    payload.update({"action": action, "workflow_state": ledger.state()["workflow_state"]})
    print(json.dumps(payload, sort_keys=True))
    return 0


# --- SD-123 (8): the gate has to reach a person -------------------------------
#
# Until v59 a human gate was a ledger state and nothing else. The two carriers
# that wake a depth-0 session -- `hooks/dispatch-owner-rewake.py` (asyncRewake)
# and `hooks/dispatch-session-sweep.py` (UserPromptSubmit) -- both read only
# `<dispatch-state-root>/pending-delivery/<sha256(session_id)>/`, and nothing
# wrote a gate there, because a gate is workflow state while those carriers wait
# on an *attempt*. So an owner that raised a gate had two options and both were
# wrong: wait forever (one owner sat 53 minutes and died BLOCKED) or press its
# own gate (two cycles did). A gate that does not reach a person is not a gate.
#
# The fix is deliberately small: the one transition into BLOCKED_HUMAN_GATE also
# writes one SD-111 record, in the same storage, with the same lock discipline.
# No new polling surface, no new command, no schema change.

# The recipient kinds whose carriers were actually taught `human-gate:` (v59).
GATE_CARRIER_KINDS = frozenset({"claude-parent-runtime"})

GATE_SESSION_GENERATION = "unsupported"
GATE_SESSION_GENERATION_SUPPORTED = "0"


def default_jobs_path():
    from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root

    return resolve_dispatch_state_root(
        resolve_agent_home(), os.environ.get("AGENT_DISPATCH_JOBS") or None
    ) / "jobs.log"


def _owner_row(rows, route_id):
    """The depth-1 owner row for this route, latest wins.

    `AGENT_DISPATCH_ATTEMPT_ID` is a shortcut for the common case where the
    caller *is* this route's owner, but it describes the CALLER, not the subject.
    So the shortcut counts only when that row also belongs to `route_id`;
    otherwise it is ignored and the route scan decides. Without the check, an
    owner of route A that blocks a gate on route B derives the recipient from
    A's row and the gate is delivered to the wrong depth-0 session — the person
    who owns route B is never told, and someone else is handed a gate that is
    not theirs.
    """
    attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if attempt_id:
        for row in reversed(rows):
            meta = row["meta"]
            if meta.get("attempt_id") != attempt_id:
                continue
            if route_id in (meta.get("owner_route_id"), meta.get("route_id")):
                return row
            break
    for row in reversed(rows):
        meta = row["meta"]
        if meta.get("dispatch_depth") != "1":
            continue
        if route_id in (meta.get("owner_route_id"), meta.get("route_id")):
            return row
    return None


def gate_recipient(route, jobs_path):
    """`(recipient_key, recipient_kind, owner_attempt_id, harness)` for one gate.

    The recipient is not in the route record: `owner_attempt_id` there is
    `AGENT_DISPATCH_ATTEMPT_ID or "-"`, and a standard+ route is compiled by the
    depth-0 session, which has no attempt id. The registry row is the only place
    that names the session that opened the route, under the same key SD-111
    already delivers to -- `parent_sid`.
    """
    row = _owner_row(_registry_rows(jobs_path), route["route_id"])
    if row is None:
        raise SupervisorError("gate-recipient-unresolved: no depth-1 owner row for this route")
    meta = row["meta"]
    recipient_key = meta.get("parent_sid", "")
    recipient_kind = meta.get("parent_completion_delivery", "")
    attempt_id = meta.get("attempt_id", "")
    if not recipient_key or not attempt_id:
        raise SupervisorError("gate-recipient-unresolved: owner row names no parent session")
    if recipient_kind not in PENDING.RECIPIENT_KINDS:
        raise SupervisorError(f"gate-recipient-unresolved: recipient kind {recipient_kind!r}")
    if recipient_kind not in GATE_CARRIER_KINDS:
        # Only the two Claude carriers learned the `human-gate:` vocabulary in
        # v59. `utilities/codex-managed-gateway.py` rejects this receipt on three
        # counts (readiness, required_action, reason), so writing the record for a
        # Codex or OpenCode parent produces something that can never be delivered
        # AND poisons that session's delivery pass. Refuse loudly instead, and
        # keep the refusal typed so the caller can act on it. Extending the other
        # two vocabularies is SD-OPEN-33; until then this is the honest boundary.
        raise SupervisorError(
            "gate-carrier-unsupported: no human-gate carrier for recipient kind "
            f"{recipient_kind!r} (SD-OPEN-33); supported: "
            + ", ".join(sorted(GATE_CARRIER_KINDS))
        )
    return recipient_key, recipient_kind, attempt_id, meta.get("harness", "-")


def gate_raise_epoch(ledger, gate):
    """How many times this gate has already been raised on this route.

    Read from the append-only ledger journal before the new transition is
    appended, so it is stable for the raise being made and increments for the
    next one.
    """
    epoch = 0
    for entry in ledger.journal():
        if entry.get("workflow_state") != "BLOCKED_HUMAN_GATE":
            continue
        if ((entry.get("evidence") or {}).get("gate")) == gate:
            epoch += 1
    return epoch


def gate_delivery_id(recipient_key, route_id, gate, attempt_id, epoch):
    """Identity for one RAISE of a gate, not for the gate.

    Keying only on `(recipient, route, gate)` looked idempotent and was in fact
    two bugs, because `dispatch_pending_delivery.IMMUTABLE_FIELDS` includes
    `attempt_ids` and `receipt_digest`, which are per-attempt:

      · a NEW owner attempt re-raising the same gate hit
        `pending-delivery-identity-conflict: attempt_ids`, `create_gate_delivery`
        turned that into a refusal, and the transition was refused with it — the
        owner could not raise its gate at all. This branch's own
        `release --decision revise` -> retry path lands exactly here.
      · the SAME attempt re-raising after the first record was acked got the
        acked record back with `created=False`, the transition proceeded, and the
        gate reached nobody — the precise failure SD-123 (8) exists to end.

    So the discriminator is the raise: attempt id plus the ledger's raise epoch.
    Repeated work inside one raise still converges (the epoch is read before the
    transition is appended), and the ledger refuses a second BLOCKED transition
    while one is open, so no legitimate caller creates two records for one raise.
    """
    payload = json.dumps(
        {"attempt_id": attempt_id, "epoch": epoch, "gate": gate,
         "recipient": recipient_key, "route_id": route_id},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return "delivery-" + hashlib.sha256(payload).hexdigest()[:32]


def gate_receipt(*, attempt_id, jobs_path, gate, artifact, harness):
    """The gate receipt, built from the existing canonical key vocabulary only.

    `CANONICAL_RECEIPT_KEYS`/`CANONICAL_CHILD_KEYS` are duplicated by hand across
    three modules and any widening breaks the digest in all of them, so the
    artifact **path** rides in `reason` -- already a free-form string, and a path
    is neither a body nor a summary. Contract (a) forbids body and summary text;
    it does not forbid reusing a field.
    """
    child = {
        "attempt_id": attempt_id,
        "status": "open",
        "readiness": "human-gate",
        "reason": str(artifact),
        "required_action": f"human-gate:{gate}",
        "harness": harness or "-",
        "delivery_classification": "attention",
    }
    return {
        "schema_version": 2,
        "state": "attention",
        "parent_attempt_id": attempt_id,
        "job_registry": str(jobs_path),
        "children": [child],
        "delivery_classification": "attention",
    }


def create_gate_delivery(route, gate, artifact, jobs_path, epoch):
    """Write the one durable record a gate transition owes its depth-0 session.

    Returns `(record_path, created)`. Raises `SupervisorError` on any refusal --
    the caller must not take the transition if this fails, so a gate never exists
    without a way to reach a person.
    """
    recipient_key, recipient_kind, attempt_id, harness = gate_recipient(route, jobs_path)
    receipt = gate_receipt(
        attempt_id=attempt_id, jobs_path=jobs_path, gate=gate,
        artifact=artifact, harness=harness,
    )
    delivery_id = gate_delivery_id(
        recipient_key, route["route_id"], gate, attempt_id, epoch
    )
    root = Path(jobs_path).resolve(strict=False).parent
    path = PENDING.record_path(root, recipient_key, delivery_id)
    try:
        with PENDING._record_lock(path):
            # Read existence under the same lock `create` takes, so the caller's
            # rollback can only ever unlink the record this call created. Reading
            # it outside let a concurrent raise of the same gate turn our
            # compensating `unlink` into the deletion of someone else's record.
            existed = PENDING._read_unlocked(path) is not None
        PENDING.create(
            root,
            recipient_kind=recipient_kind,
            recipient_key=recipient_key,
            delivery_id=delivery_id,
            session_generation=GATE_SESSION_GENERATION,
            session_generation_supported=GATE_SESSION_GENERATION_SUPPORTED,
            attempt_ids=[attempt_id],
            parent_attempt_id=attempt_id,
            route_id=route["route_id"],
            route_node=_gate_route_node(route, gate),
            receipt=receipt,
            receipt_digest=PENDING._canonical_receipt_digest(receipt),
            row_revisions={attempt_id: f"human-gate:{gate}"},
        )
    except PENDING.PendingDeliveryError as exc:
        raise SupervisorError(f"gate-delivery-refused: {exc}") from exc
    return path, not existed


def _gate_route_node(route, gate_name):
    node = _gate_predecessor_node(route, gate_name)
    if node is not None:
        return str(node["id"])
    binding = next(
        (row for row in (route.get("human_gate_bindings") or []) if row.get("gate") == gate_name),
        None,
    )
    return str((binding or {}).get("node") or "_gate")


def resolved_released_by(actor_kind, requested):
    """`released_by` for this release, refusing a registered worker's own label.

    The first cut only compared the requested value against the literal
    `"user"`, so `--by shinuh` from a headless owner recorded
    `released_by=shinuh` and only `actor_kind` still betrayed it — the exact
    indistinguishability contract (d) exists to prevent. A registered worker now
    may not name the releaser at all; its label is derived.
    """
    if actor_kind == "headless-owner" and requested:
        raise SupervisorError(
            "gate-release-actor-refused: a registered headless owner may not name "
            "the releaser; released_by is derived as headless-owner"
        )
    return requested or actor_kind


def retire_gate_delivery(route, gate, jobs):
    """Retire the pending gate record once the gate is released.

    Without this the record stays `pending` after a release, and the next
    `UserPromptSubmit` sweep keeps announcing a gate that is already closed.
    Fail-soft on purpose: the ledger transition is the authoritative release, so
    a record that cannot be retired must never fail the release. Returns the
    state it reached, or None when there was nothing to retire.
    """
    try:
        jobs_path = Path(jobs) if jobs else default_jobs_path()
        recipient_key, _kind, attempt_id, _harness = gate_recipient(route, jobs_path)
        root = Path(jobs_path).resolve(strict=False).parent
    except (SupervisorError, OSError):
        return None
    ledger = ledger_for(route)
    # Every raise of this gate, newest first: a release closes whichever raise is
    # still outstanding, and older ones may legitimately be acked already.
    for epoch in range(gate_raise_epoch(ledger, gate), -1, -1):
        delivery_id = gate_delivery_id(recipient_key, route["route_id"], gate,
                                       attempt_id, epoch)
        try:
            record = PENDING.read(root, recipient_key, delivery_id)
            if record is None or record.get("state") in {"acked", "expired"}:
                continue
            if record.get("state") in {"claimed", "sent-ambiguous"}:
                PENDING.reclaim(root, recipient_key, delivery_id, now_ns=time.time_ns())
            PENDING.claim(root, recipient_key, delivery_id,
                          claim_owner=f"gate-release:{os.getpid()}",
                          lease_seconds=60.0, require_generation_proof=False)
            PENDING.ack(root, recipient_key, delivery_id,
                        acked_by=f"gate-released:{gate}")
            return "acked"
        except (PENDING.PendingDeliveryError, OSError, TypeError):
            continue
    return None


def release_actor_kind():
    """`headless-owner` when the process recording the release is a registered
    worker, `user` otherwise.

    Contract (d) does not forbid a headless owner from releasing its own gate --
    forbidding it just makes the 53-minute death the only ending. It forbids that
    release from being *indistinguishable* from a person's, which is what
    actually happened on 2026-09-03: both cycles were honest in prose and the
    data could not tell. The discriminator is deliberately harness-neutral, so
    Codex/OpenCode parity (SD-OPEN-33) needs no new predicate here.
    """
    if os.environ.get("AGENT_DISPATCH_REGISTERED_WORKER") == "1":
        return "headless-owner"
    return "user"


def gate_release_sidecar_path(route_path):
    """The sidecar beside the route being released — the path the caller named.

    This used to read `AGENT_OWNER_ROUTE_FILE` first and fall back to
    `route["route_file"]`. Both halves were wrong. A live route record
    (`rt-*.json`) carries no `route_file` at all — only its `.outcome.json` does,
    written from `args.route` at close — so the fallback never fired and the
    function was effectively env-only. And the env names the route the CALLING
    owner runs under, not the route being released.

    Measured 2026-09-03: an owner running its own suite put eight fixture
    releases (`route_hash: "sha256:fixture"`) into the real
    `rt-6579b69141dc0c00.gate-release.json`, and `close_route` folds that
    sidecar into the route outcome, so those eight would have entered a real
    route's history. The same precedence misfiles a genuine release whenever an
    owner releases a gate on a nested or continuation route.

    So the subject is passed in, the way `close_route` already builds
    `route_file` from `args.route`. No env, no guessing: without a path there is
    no sidecar, and the ledger remains the authoritative record of the release.
    """
    if not route_path:
        return None
    path = Path(route_path)
    return path.with_name(path.stem + ".gate-release.json")


def record_gate_release(route, route_path, *, gate, decision, released_by, actor_kind):
    """Append one gate release to the route's sidecar; `close_route` folds it into
    the outcome. Fail-soft: a release must never be lost because a sidecar could
    not be written, and the ledger already holds the authoritative transition.

    `route_path` is the file the caller named, kept separate from `route` because
    a loaded route record does not know its own path (see
    `gate_release_sidecar_path`)."""
    path = gate_release_sidecar_path(route_path)
    if path is None:
        return None
    row = {
        "gate": gate, "decision": decision, "released_by": released_by,
        "actor_kind": actor_kind, "route_hash": route.get("route_hash", ""),
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        rows = existing.get("gate_releases") if isinstance(existing, dict) else None
        rows = list(rows) if isinstance(rows, list) else []
        rows.append(row)
        payload = json.dumps(
            {"schema_version": 1, "route_id": route["route_id"], "gate_releases": rows},
            sort_keys=True, indent=2,
        ) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        return None
    return path


def _gate_predecessor_node(route, gate_name):
    """The node whose declared continuation names this gate -- the node the
    workflow was blocked leaving, as opposed to `human_gate_bindings`' own
    `node`/`position`, which name where the gate blocks *entry*."""
    for node in route.get("nodes", []) or []:
        continuation = node.get("continuation") or {}
        if continuation.get("kind") == "human-gate" and continuation.get("gate") == gate_name:
            return node
    return None


def cmd_release(args):
    """A50-8: `release --decision proceed|revise|stop`, semantically
    consistent with `gate --release|--block` but closing the gap `gate
    --release` alone leaves open -- `poll_once` reports `action=human-gate`
    for a human-gate continuation and stops there (:465-471); nothing else
    ever calls `_evaluate` for that node, so a bare `gate --release` leaves
    the workflow RUNNING with no successor claimed or started. `proceed`
    performs both, atomically, inside one `ledger.lock()`.
    """
    route = load_route(args.route)
    ledger = ledger_for(route)
    gates = {row["gate"]: row for row in (route.get("human_gate_bindings") or [])}
    if args.gate not in gates:
        raise SupervisorError(f"route declares no human gate {args.gate!r}")
    predecessor = _gate_predecessor_node(route, args.gate)
    if predecessor is None:
        raise SupervisorError(
            f"route declares no node continuing into human gate {args.gate!r}"
        )
    node_id = str(predecessor["id"])
    # SD-123 (8)(d): `released_by` is derived, not asserted. A registered
    # headless owner may release its own gate -- forbidding it leaves only the
    # 53-minute BLOCKED death -- but it may not sign that release as a person.
    actor_kind = release_actor_kind()
    actor = resolved_released_by(actor_kind, args.actor)
    with ledger.lock():
        state = ledger.state()["workflow_state"]
        if state != "BLOCKED_HUMAN_GATE":
            raise SupervisorError(f"workflow is {state}, not blocked on a human gate")
        if args.decision == "proceed":
            ledger.set_workflow_state(
                "RUNNING",
                evidence={"released_gate": args.gate, "released_by": actor, "decision": "proceed"},
                actor="release",
            )
            successors = WS.route_successors(route, node_id)
            # No armed record exists for a human-gate node (cmd_arm governs only
            # supervised/monitor continuations), so the successor start is always
            # the declared-external-surface shape of `_start_successor`: the
            # owner conductor itself dispatches the next stage after this call
            # returns. Only the exactly-once claim is this function's job.
            armed_like = {"route_id": route["route_id"], "successor_command": None}
            started = _claim_successors(route, ledger, armed_like, node_id, successors)
            if any(entry.get("created") for entry in started):
                current = ledger.state()["workflow_state"]
                if WS.can_transition(current, "NEXT_REGISTERED"):
                    ledger.set_workflow_state(
                        "NEXT_REGISTERED",
                        evidence={"node": node_id, "successors": successors},
                        actor="release",
                    )
            payload = {"gate": args.gate, "decision": "proceed", "node": node_id,
                      "successors": started,
                      "workflow_state": ledger.state()["workflow_state"]}
        elif args.decision == "revise":
            # BLOCKED_HUMAN_GATE has no direct transition to FAILED_RETRYABLE in
            # the topology registry's workflow_transitions -- both hops
            # (-> RUNNING -> FAILED_RETRYABLE) are declared, so revise takes
            # them in the same lock rather than widening the vocabulary.
            ledger.set_workflow_state(
                "RUNNING",
                evidence={"released_gate": args.gate, "released_by": actor, "decision": "revise"},
                actor="release",
            )
            ledger.set_workflow_state(
                "FAILED_RETRYABLE",
                evidence={"gate": args.gate, "released_by": actor,
                          "retry_boundary": "frame", "next_stage": "code-refine"},
                actor="release",
            )
            payload = {"gate": args.gate, "decision": "revise", "node": node_id,
                      "retry_boundary": "frame",
                      "workflow_state": ledger.state()["workflow_state"]}
        elif args.decision == "stop":
            ledger.set_workflow_state(
                "CANCELLED",
                evidence={"gate": args.gate, "released_by": actor,
                          "abandon_reason": "operator-decision"},
                actor="release",
            )
            payload = {"gate": args.gate, "decision": "stop", "node": node_id,
                      "workflow_state": ledger.state()["workflow_state"]}
        else:
            raise SupervisorError(f"unknown --decision: {args.decision!r}")
        payload["released_by"] = actor
        payload["actor_kind"] = actor_kind
        record_gate_release(route, args.route, gate=args.gate, decision=args.decision,
                            released_by=actor, actor_kind=actor_kind)
        retire_gate_delivery(route, args.gate, getattr(args, "jobs", None))
    print(json.dumps(payload, sort_keys=True))
    return 0


def resource_children(route, ledger):
    """Child resource jobs of this route, from the shared resource-run global index.

    A resource row belongs to this workflow when an armed watch names its run id, or
    when the row's own `route` record resolves to this route id. Visibility is a
    requirement, so an unreadable index degrades to the armed set rather than to
    silence.
    """
    armed_runs, armed_registries = set(), set()
    for armed in read_armed(ledger).values():
        if armed.get("predecessor_kind") == "resource":
            if armed.get("predecessor_id"):
                armed_runs.add(armed["predecessor_id"])
            if armed.get("resource_registry"):
                armed_registries.add(armed["resource_registry"])
    try:
        rows, _diagnostics = RR.scan()
    except Exception:
        rows = []
    unique = {}
    for row in rows:
        run_id = row.get("run_id")
        owned = run_id in armed_runs
        if not owned and str(row.get("registry_path")) in armed_registries:
            owned = True
        if not owned and row.get("route"):
            try:
                owned = json.loads(
                    Path(str(row["route"])).read_text(encoding="utf-8")
                ).get("route_id") == route["route_id"]
            except (OSError, ValueError):
                owned = False
        if owned:
            unique[run_id] = row
    # A child the global index has not seen yet is still this workflow's child: read it
    # from the registry the armed watch already names, so the fallback carries real
    # identity instead of the word "unknown".
    for armed in read_armed(ledger).values():
        run_id = armed.get("predecessor_id")
        if (armed.get("predecessor_kind") != "resource" or run_id in unique
                or not armed.get("resource_registry")):
            continue
        registry = Path(armed["resource_registry"])
        try:
            row = (json.loads(registry.read_text(encoding="utf-8")).get("runs")
                   or {}).get(run_id)
            unique[run_id] = RR.normalize_run(run_id, row, registry)
            unique[run_id]["index_state"] = "registry-direct"
        except Exception:
            unique[run_id] = {"run_id": run_id, "liveness": "unknown",
                              "registry_path": str(registry),
                              "state_evidence": {"reason": "resource-registry-unreadable"}}
    for run_id in sorted(armed_runs - set(unique)):
        unique[run_id] = {"run_id": run_id, "liveness": "unknown",
                          "state_evidence": {"reason": "not-in-resource-run-index"}}
    return [unique[key] for key in sorted(unique)]


def _stage_projection(route, node_states):
    """Current running nodes and the declared-but-not-yet-satisfied next stage.

    Shared by `status` and `survey` so both report the same stage projection from the
    same node-state dict, instead of two independently maintained copies drifting apart.
    """
    running = sorted(node for node, row in node_states.items() if row.get("state") == "RUNNING")
    next_stage = sorted({successor
                         for node, row in node_states.items()
                         if row.get("state") == "STAGE_SUCCEEDED"
                         for successor in WS.route_successors(route, node)
                         if node_states.get(successor, {}).get("state") != "STAGE_SUCCEEDED"})
    if not next_stage:
        satisfied = {node for node, row in node_states.items()
                     if row.get("state") == "STAGE_SUCCEEDED"}
        next_stage = sorted({
            node["id"] for node in route.get("nodes", [])
            if node["id"] not in node_states
            and set(node.get("depends_on") or []) <= satisfied
        })
    return running, next_stage


def cmd_status(args):
    route = load_route(args.route)
    ledger = ledger_for(route)
    state = ledger.state()
    armed = read_armed(ledger)
    terminal_nodes = WS.route_terminal_nodes(route)
    gates = terminal_gate_state(route)
    node_states = state["nodes"]
    failed = {node: row for node, row in node_states.items()
              if str(row.get("state", "")).startswith("FAILED")}
    running, next_stage = _stage_projection(route, node_states)
    derived = WS.derive_workflow_state(
        node_states, terminal_nodes,
        terminal_gates_passed=bool(gates) and all(row["passed"] for row in gates.values()),
        pending_claims=len(ledger.claims()),
    )
    payload = {
        "route_id": route["route_id"],
        "route_file": str(Path(args.route).resolve()),
        "capability": route.get("capability"),
        "capability_mode": route.get("capability_mode"),
        "effective_intensity": route.get("effective_intensity"),
        "workflow_state": state["workflow_state"],
        "derived_workflow_state": derived,
        "updated_at": state["updated_at"],
        "current_stage": sorted(running),
        "next_stage": next_stage,
        "terminal_nodes": terminal_nodes,
        "terminal_gates": gates,
        "human_gate_bindings": route.get("human_gate_bindings") or [],
        "failure_reason": {node: row.get("evidence", {}).get("reason")
                           for node, row in failed.items()} or None,
        "nodes": node_states,
        "armed": {node: {"kind": row.get("continuation_kind"),
                         "predecessor_kind": row.get("predecessor_kind"),
                         "predecessor_id": row.get("predecessor_id"),
                         "successors": row.get("successors"),
                         "successor_external": row.get("successor_external")}
                  for node, row in armed.items()},
        "claims": ledger.claims(),
        "resource_children": resource_children(route, ledger),
        "ledger_root": str(ledger.root),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(f"route      {payload['route_id']} ({payload['capability']}/"
              f"{payload['capability_mode']} {payload['effective_intensity']})")
        print(f"workflow   {payload['workflow_state']} (derived {derived})")
        print(f"stage      current={payload['current_stage'] or '-'} "
              f"next={payload['next_stage'] or '-'}")
        print(f"terminal   {terminal_nodes} gates="
              f"{ {k: v['passed'] for k, v in gates.items()} }")
        for child in payload["resource_children"]:
            print(f"resource   {child.get('run_id')} {child.get('liveness')} "
                  f"class={child.get('resource_class')} log={child.get('log_path')}")
        if payload["failure_reason"]:
            print(f"failure    {payload['failure_reason']}")
    return 0


def cmd_complete(args):
    route = load_route(args.route)
    ledger = ledger_for(route)
    terminal_nodes = WS.route_terminal_nodes(route)
    if not terminal_nodes:
        raise SupervisorError("route declares no terminal node")
    gates = terminal_gate_state(route)
    unproven = {node: row for node, row in gates.items() if not row["passed"]}
    with ledger.lock():
        state = ledger.state()
        if unproven:
            print(json.dumps({"complete": False, "reason": "terminal-gate-unproven",
                              "unproven": unproven,
                              "workflow_state": state["workflow_state"]}, sort_keys=True))
            return 3
        for node in terminal_nodes:
            if state["nodes"].get(node, {}).get("state") != "STAGE_SUCCEEDED":
                ledger.record(node, "STAGE_SUCCEEDED",
                              evidence={"terminal_gate": gates[node]}, actor="complete")
        if ledger.state()["workflow_state"] != "TERMINAL_VERIFY":
            ledger.set_workflow_state("TERMINAL_VERIFY", evidence={"terminal_gates": gates},
                                      actor="complete")
        ledger.set_workflow_state("COMPLETE", evidence={"terminal_gates": gates},
                                  actor="complete")
    print(json.dumps({"complete": True, "terminal_nodes": terminal_nodes,
                      "workflow_state": ledger.state()["workflow_state"]}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------------
# survey: read-only, root-scoped "what is stuck" report
# --------------------------------------------------------------------------------

def _iso_to_epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def resource_liveness_readonly(armed):
    """Read-only liveness for a resource predecessor: registry parse + `classify_identity()`
    only. Never calls `runner().settle()` -- settling can persist terminal state, which a
    read-only survey must not do."""
    registry_path = armed.get("resource_registry")
    if not registry_path:
        return {"liveness": "unknown", "reason": "resource-registry-not-recorded"}
    try:
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        row = (data.get("runs") or {}).get(armed.get("predecessor_id"))
    except (OSError, ValueError) as exc:
        return {"liveness": "unknown", "reason": f"resource-registry-unreadable:{exc}"}
    if not isinstance(row, dict):
        return {"liveness": "unknown", "reason": "resource-run-absent"}
    liveness, _current, reason = RR.classify_identity(row)
    return {"liveness": liveness, "reason": reason}


def predecessor_liveness_readonly(armed):
    """Read-only liveness for either predecessor kind, without settling anything.

    `registered_evidence()` is already a pure read (jobs-registry parse plus a live PID
    probe), so it is reused as-is; only the resource path needed a settle-free variant.
    """
    if armed.get("predecessor_kind") == "resource":
        info = resource_liveness_readonly(armed)
        return {"kind": "resource", "liveness": info["liveness"], "reason": info["reason"]}
    evidence = registered_evidence(armed)
    if not evidence.get("terminal"):
        liveness = "working"
    elif evidence.get("quiescent") is False:
        liveness = "working"
    else:
        liveness = "exited"
    return {"kind": "registered", "liveness": liveness, "reason": evidence.get("reason")}


def _has_claim_or_progress(node_states, claims, node_id, successors):
    """A claim naming this predecessor/successor pair, or any recorded successor node
    state at all, both mean something already owns the advance -- read-only, no exact
    predecessor-identity recomputation required."""
    for row in claims.values():
        if isinstance(row, dict) and row.get("predecessor") == node_id and row.get("successor") in successors:
            return True
    return any((node_states.get(successor) or {}).get("state") for successor in successors)


def _diagnostic_row(diagnostic):
    """A malformed/unreadable route candidate stays visible as its own `unknown` row --
    D-2 discovery drops it silently today; survey must not repeat that silence."""
    location = diagnostic.get("location")
    return {
        "route_id": None, "route_file": diagnostic.get("path"), "location": location,
        "read_only": location in route_module()._LEGACY_LOCATIONS if location else None,
        "closed": None, "route_read": {"status": "unknown", "reason": diagnostic.get("reason")},
        "workflow_state": "unknown", "derived_workflow_state": "unknown",
        "current_stage": [], "next_stage": [], "terminal_nodes": [],
        "terminal_gate_proven": None, "terminal_gates": {},
        "armed": {}, "claims": {},
        "evidence_freshness": {"newest_at": None, "age_seconds": None, "stale": True},
        "risk": {"tier": "unknown", "score": 0, "reasons": [diagnostic.get("reason")]},
    }


def _survey_route_row(route_row, stale_after_seconds, now):
    """One ranked survey row for one route candidate `route_status()` already found.

    Re-reads the route file (read-only) for its node graph, recomputes the terminal
    gate live through the shared `capability-route.py` seam (never from a stored outcome
    sidecar -- a pre-existing v2 sidecar has no gate fields at all), and derives workflow
    state through `WorkflowLedger.read_only_state()`, which mutates nothing.
    """
    path = route_row["route_file"]
    try:
        route = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(route, dict) or "route_id" not in route or "nodes" not in route:
            raise ValueError("route-malformed-missing-required-keys")
    except (OSError, ValueError) as exc:
        return {
            "route_id": route_row.get("route_id"), "route_file": path,
            "location": route_row.get("location"), "read_only": route_row.get("read_only"),
            "closed": route_row.get("closed"),
            "route_read": {"status": "unknown", "reason": f"route-unreadable:{exc}"},
            "workflow_state": "unknown", "derived_workflow_state": "unknown",
            "current_stage": [], "next_stage": [], "terminal_nodes": [],
            "terminal_gate_proven": None, "terminal_gates": {},
            "armed": {}, "claims": {},
            "evidence_freshness": {"newest_at": None, "age_seconds": None, "stale": True},
            "risk": {"tier": "unknown", "score": 0, "reasons": [f"route-unreadable:{exc}"]},
        }

    terminal_nodes = WS.route_terminal_nodes(route)
    gates = route_module().terminal_gate_observation(route)
    proven = route_module().terminal_gate_proven(gates)

    ledger = ledger_for(route)
    ledger_state = ledger.read_only_state()
    entries = ledger.journal()
    armed = read_armed(ledger)
    claims = ledger.claims()
    node_states = ledger_state.get("nodes", {})

    reasons = []
    ledger_known = True
    if not ledger_state.get("ledger_dir_exists"):
        ledger_known = False
        reasons.append("ledger-absent")
    elif ledger_state.get("journal_unreadable"):
        ledger_known = False
        reasons.append("ledger-unreadable")
    elif route.get("route_hash") and any(
            entry.get("route_hash") and entry.get("route_hash") != route.get("route_hash")
            for entry in entries):
        ledger_known = False
        reasons.append("route-hash-mismatch")

    candidates = [t for t in (
        [_iso_to_epoch(ledger_state.get("updated_at"))]
        + [_iso_to_epoch(row.get("armed_at")) for row in armed.values()]
        + [_iso_to_epoch(row.get("claimed_at")) for row in claims.values() if isinstance(row, dict)]
    ) if t is not None]
    newest_epoch = max(candidates) if candidates else None
    age_seconds = (now - newest_epoch) if newest_epoch is not None else None
    stale = bool(ledger_known and entries and age_seconds is not None
                and age_seconds > stale_after_seconds)
    if stale:
        ledger_known = False
        reasons.append("evidence-stale")
    evidence_freshness = {
        "newest_at": (
            datetime.fromtimestamp(newest_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            if newest_epoch is not None else None
        ),
        "age_seconds": age_seconds,
        "stale": stale,
    }

    if ledger_known:
        workflow_state = ledger_state.get("workflow_state", "CREATED")
        running, next_stage = _stage_projection(route, node_states)
        derived = WS.derive_workflow_state(
            node_states, terminal_nodes,
            terminal_gates_passed=bool(gates) and proven is True,
            pending_claims=len(claims))
    else:
        workflow_state, derived, running, next_stage = "unknown", "unknown", [], []

    open_gate = ledger_known and workflow_state == "BLOCKED_HUMAN_GATE"

    armed_out = {}
    any_abandoned = False
    abandoned_external = False
    any_active = False
    for node_id, row in armed.items():
        if ledger_known:
            liveness_info = predecessor_liveness_readonly(row)
            claimed_or_progressed = _has_claim_or_progress(
                node_states, claims, node_id, row.get("successors") or [])
        else:
            liveness_info = {"liveness": "unknown", "reason": "ledger-unknown"}
            claimed_or_progressed = False
        successor_external = bool(row.get("successor_external"))
        armed_out[node_id] = {
            "continuation_kind": row.get("continuation_kind"),
            "predecessor_kind": row.get("predecessor_kind"),
            "predecessor_id": row.get("predecessor_id"),
            "predecessor_liveness": liveness_info.get("liveness"),
            "successors": row.get("successors") or [],
            "successor_external": successor_external,
            "claimed_or_progressed": claimed_or_progressed,
        }
        settled = node_states.get(node_id, {}).get("state") in (
            "STAGE_SUCCEEDED", "FAILED_TERMINAL", "CANCELLED")
        if (ledger_known and not open_gate and not settled
                and row.get("continuation_kind") == "supervised"
                and liveness_info.get("liveness") == "exited"
                and not claimed_or_progressed):
            any_abandoned = True
            abandoned_external = abandoned_external or successor_external
        if liveness_info.get("liveness") == "working" or claimed_or_progressed:
            any_active = True

    reasons_out = list(reasons)
    if any_abandoned:
        tier = "abandoned"
        score = 10 + (5 if abandoned_external else 0)
        reasons_out.append("supervised-predecessor-exited-unclaimed")
        if abandoned_external:
            reasons_out.append("successor-external")
    elif route_row.get("closed") and proven is False:
        tier, score = "closure-mismatch", 8
        reasons_out.append("closed-with-unproven-terminal-gate")
    elif (not route_row.get("closed")) and terminal_nodes and proven is False and ledger_known:
        tier, score = "stale-open", 5
        reasons_out.append("open-with-unproven-terminal-gate")
    elif any_active:
        tier, score = "active-or-owned", 3
        reasons_out.append("live-predecessor-or-claimed-successor")
    elif open_gate:
        tier, score = "parked", 2
        reasons_out.append("blocked-human-gate")
    elif proven is True and (route_row.get("closed") or derived == "COMPLETE"):
        tier, score = "complete", 0
        reasons_out.append("terminal-gate-proven")
    elif not ledger_known:
        tier, score = "unknown", 1
    else:
        tier, score = "unknown", 0
        reasons_out.append("no-actionable-risk-signal")

    row = {
        "route_id": route.get("route_id"), "route_file": path,
        "location": route_row.get("location"), "read_only": route_row.get("read_only"),
        "closed": route_row.get("closed"), "route_read": {"status": "ok", "reason": None},
        "workflow_state": workflow_state, "derived_workflow_state": derived,
        "current_stage": running, "next_stage": next_stage, "terminal_nodes": terminal_nodes,
        "terminal_gate_proven": proven, "terminal_gates": gates,
        "armed": armed_out, "claims": claims,
        "evidence_freshness": evidence_freshness,
        "risk": {"tier": tier, "score": score, "reasons": reasons_out},
    }
    if "duplicate_locations" in route_row:
        row["duplicate_locations"] = route_row["duplicate_locations"]
    return row


def _survey_sort_key(row):
    risk = row["risk"]
    return (RISK_TIER_RANK.get(risk["tier"], len(RISK_TIER_ORDER)), -risk["score"],
            row.get("route_id") or "", row["route_file"])


def cmd_survey(args):
    if args.stale_after_seconds <= 0:
        raise SupervisorError("--stale-after-seconds must be a positive float")
    artifact_root = Path(args.artifact_root).resolve()
    diagnostics = []
    route_rows = route_module().route_status(str(artifact_root), diagnostics=diagnostics)
    now = time.time()
    rows = [_survey_route_row(row, args.stale_after_seconds, now) for row in route_rows]
    rows.extend(_diagnostic_row(diagnostic) for diagnostic in diagnostics)
    rows.sort(key=_survey_sort_key)
    payload = {
        "schema_version": SURVEY_SCHEMA_VERSION,
        "artifact_root": str(artifact_root),
        "ledger_root": str(WS.default_ledger_root()),
        "completion_root": str(
            route_module().resolve_dispatch_state_root(route_module().resolve_agent_home())
            / "completion"
        ),
        "stale_after_seconds": args.stale_after_seconds,
        "rows": rows,
        "diagnostics": diagnostics,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        for row in rows:
            print(f"{row['risk']['tier']:16} score={row['risk']['score']:<3} "
                  f"{row.get('route_id') or row['route_file']} "
                  f"workflow={row['workflow_state']} closed={row['closed']} "
                  f"reasons={row['risk']['reasons']}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="workflow-supervisor")
    sub = parser.add_subparsers(dest="command", required=True)

    arm = sub.add_parser("arm", help="register a continuation watch for one node")
    arm.add_argument("--route", required=True)
    arm.add_argument("--node", required=True)
    arm.add_argument("--predecessor-kind", required=True, choices=PREDECESSOR_KINDS)
    arm.add_argument("--predecessor-id", required=True)
    arm.add_argument("--resource-registry")
    arm.add_argument("--jobs")
    arm.add_argument("--successor-command", help="JSON argv array that starts the next stage")
    arm.add_argument("--successor-external", action="store_true",
                     help="another checked surface owns the successor start; recorded explicitly")
    arm.add_argument("--successor-cwd")
    arm.add_argument("--successor-log")
    arm.add_argument("--artifact-base", help="directory the node's declared outputs live under")
    arm.add_argument("--monitor-evidence")

    poll = sub.add_parser("poll", help="evaluate every armed watch once")
    poll.add_argument("--route", required=True)

    watch = sub.add_parser("watch", help="poll until terminal or the bounded deadline")
    watch.add_argument("--route", required=True)
    watch.add_argument("--max", type=float, default=3600.0)
    watch.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL)

    gate = sub.add_parser("gate", help="record or release a declared human gate")
    gate.add_argument("--route", required=True)
    gate.add_argument("--gate", required=True)
    gate.add_argument("--by")
    gate.add_argument("--jobs", help="canonical registry path (default: dispatch state root)")
    gate.add_argument("--artifact", default="-",
                      help="path to the artifact a person reviews at this gate; "
                           "carried in the delivery record, never its contents")
    group = gate.add_mutually_exclusive_group(required=True)
    group.add_argument("--release", action="store_true")
    group.add_argument("--block", action="store_true")

    release = sub.add_parser("release", help="resolve a declared human gate: proceed, revise, or stop")
    release.add_argument("--route", required=True)
    release.add_argument("--gate", required=True)
    release.add_argument("--decision", required=True, choices=("proceed", "revise", "stop"))
    release.add_argument("--actor")

    status = sub.add_parser("status", help="portable workflow/stage/resource projection")
    status.add_argument("--route", required=True)
    status.add_argument("--json", action="store_true")

    complete = sub.add_parser("complete", help="verify terminal gates, then close the workflow")
    complete.add_argument("--route", required=True)

    survey = sub.add_parser("survey", help="read-only, root-scoped abandoned/stuck workflow report")
    survey.add_argument("--artifact-root", required=True)
    survey.add_argument("--stale-after-seconds", type=float, default=DEFAULT_STALE_AFTER_SECONDS)
    survey.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handler = {
        "arm": cmd_arm, "poll": cmd_poll, "watch": cmd_watch, "gate": cmd_gate,
        "release": cmd_release,
        "status": cmd_status, "complete": cmd_complete, "survey": cmd_survey,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SupervisorError, WS.WorkflowStateError) as exc:
        print(f"workflow-supervisor: {exc}", file=sys.stderr)
        raise SystemExit(64)
