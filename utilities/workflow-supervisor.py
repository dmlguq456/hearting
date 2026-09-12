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
  workflow-supervisor.py gate    --route R --gate G --release|--block [--artifact P]
  workflow-supervisor.py release --route R --gate G --decision proceed|revise|stop
  workflow-supervisor.py await-release --route R --gate G [--max S] [--interval S]
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
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

from dispatch_contract import SUCCESS_NOTES  # noqa: E402
import workflow_state as WS  # noqa: E402
import resource_run_registry as RR  # noqa: E402
import dispatch_pending_delivery as PENDING  # noqa: E402
import frame_interview as INTERVIEW  # noqa: E402
import human_gate_receipt as HUMAN_GATE  # noqa: E402

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


def ledger_for(route, jobs=None):
    return WS.WorkflowLedger(route["route_id"], route.get("route_hash", ""), jobs=jobs)


def ledger_metadata(jobs=None, ledger=None):
    root, source = WS.ledger_root_for(jobs)
    selected = None
    if source == "explicit-jobs":
        selected = jobs
    elif source == "AGENT_DISPATCH_JOBS":
        selected = os.environ.get("AGENT_DISPATCH_JOBS")
    return {"workflow_root": str(root),
            "ledger_root": str(ledger.root) if ledger is not None else str(root),
            "ledger_root_source": source,
            "jobs_path": str(Path(selected).expanduser().resolve(strict=True)) if selected else None}


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
    # SUCCESS_NOTES gained `completed-subsession` (SD-130), so a slice row can
    # now satisfy an armed stage's terminal evidence. That is not an authority
    # grant: arming names an exact attempt id, so a slice only counts where a
    # supervisor was armed on that slice deliberately.
    succeeded = (
        note in (*SUCCESS_NOTES, "completed")
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
    ledger = ledger_for(route, getattr(args, "jobs", None))
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
    print(json.dumps({"armed": args.node, **ledger_metadata(getattr(args, "jobs", None), ledger),
                      "successors": successors,
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
    ledger = ledger_for(route, getattr(args, "jobs", None))
    results = poll_once(route, ledger)
    print(json.dumps({"route_id": route["route_id"], **ledger_metadata(getattr(args, "jobs", None), ledger),
                      "workflow_state": ledger.state()["workflow_state"],
                      "results": results}, sort_keys=True))
    return 0


def cmd_watch(args):
    route = load_route(args.route)
    ledger = ledger_for(route, getattr(args, "jobs", None))
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
            print(json.dumps({"route_id": route["route_id"],
                              **ledger_metadata(getattr(args, "jobs", None), ledger),
                              "timeout": True,
                              "workflow_state": state, "results": last}, sort_keys=True))
            return 3
        time.sleep(interval)
    print(json.dumps({"route_id": route["route_id"],
                      **ledger_metadata(getattr(args, "jobs", None), ledger),
                      "timeout": False,
                      "workflow_state": ledger.state()["workflow_state"],
                      "results": last}, sort_keys=True))
    return 0


def cmd_gate(args):
    route = load_route(args.route)
    if args.block and not getattr(args, "jobs", None) \
            and os.environ.get("AGENT_DISPATCH_REGISTERED_WORKER") == "1" \
            and os.environ.get("AGENT_HARNESS", "codex").lower() == "codex":
        raise SupervisorError(
            "human-gate-jobs-required: Codex strict gate --block requires explicit --jobs"
        )
    ledger = ledger_for(route, getattr(args, "jobs", None))
    gates = {row["gate"]: row for row in (route.get("human_gate_bindings") or [])}
    if args.gate not in gates:
        raise SupervisorError(f"route declares no human gate {args.gate!r}")
    payload = {"gate": args.gate,
               **ledger_metadata(getattr(args, "jobs", None), ledger)}
    with ledger.lock():
        if args.release:
            state = ledger.state()["workflow_state"]
            if state != "BLOCKED_HUMAN_GATE":
                refuse_gate_not_blocked(route, args.gate, state)
            actor_kind = release_actor_kind()
            released_by = resolved_released_by(actor_kind, args.by)
            resolution = WS.human_gate_resolution(ledger.journal(), args.gate)
            assert_release_authority(actor_kind, resolution, gates[args.gate], args.gate)
            if any(args.gate in n.get("inline_human_gates", []) for n in route.get("nodes", [])):
                WS.require_gate_artifact_current(resolution)
            if resolution["interview"]:
                # The legacy surface has no --answers, and an interview gate
                # released without them is the guess the interview replaces
                # (review round 1, M3).
                raise SupervisorError(
                    "interview-answers-required: this gate carries an interview; release it "
                    "with `release --decision proceed --answers <file>` (or revise/stop)")
            ledger.set_workflow_state("RUNNING", evidence={"released_gate": args.gate,
                                                           "released_by": released_by,
                                                           "actor_kind": actor_kind},
                                      actor="gate")
            record_gate_release(route, args.route, gate=args.gate, decision="proceed",
                                released_by=released_by, actor_kind=actor_kind)
            retired = retire_gate_delivery(route, args.gate, args.jobs)
            payload.update({"released_by": released_by, "actor_kind": actor_kind,
                            "delivery_retired": retired, "route_id": route["route_id"]})
            action = "released"
        else:
            # SD-123 (8)(a): the record and the transition are one transaction.
            # Creating the record first is what makes "transition failed ->
            # zero records" recoverable at all: the reverse order can leave a
            # blocked gate nobody can be told about, and there is no compensating
            # action for that. `delivery_id` is deterministic, so the unlink
            # below can only remove the record this call just created.
            blocked_on = currently_blocked_gate(ledger)
            if blocked_on is not None and blocked_on != args.gate:
                raise SupervisorError(
                    f"gate-already-blocked: workflow is blocked on {blocked_on!r}, "
                    f"not {args.gate!r}"
                )
            if blocked_on == args.gate:
                # `assert_transition` returns early when current == target and
                # `set_workflow_state` appends anyway, so without this guard a
                # repeated `--block` minted a SECOND raise epoch and a second
                # record. Releasing then acked only the newest, leaving the older
                # one pending forever — the sweep would announce a closed gate on
                # every prompt, the exact symptom the release-time retirement
                # exists to remove. Converge on the raise already made, and hand
                # back the same payload a first raise gets (review round 1, minor 7).
                current = WS.human_gate_resolution(ledger.journal(), args.gate)
                payload.update(existing_gate_delivery(route, args.gate, args.jobs))
                payload.update({"action": "blocked",
                                "interview": current["interview"],
                                "questions": current["questions"],
                                "artifact": current["artifact"],
                                "await_command": await_release_command(args.route, args.gate, getattr(args, "jobs", None)),
                                "workflow_state": ledger.state()["workflow_state"]})
                print(json.dumps(payload, sort_keys=True))
                return 0
            if not args.artifact or args.artifact == "-":
                # Contract (a) names the reviewable artifact as part of the
                # record. A gate that arrives saying `artifact=-` tells the
                # person nothing to look at, which defeats the delivery.
                raise SupervisorError(
                    "gate-artifact-required: --artifact must name the artifact a "
                    "person reviews at this gate"
                )
            jobs_path = Path(args.jobs) if args.jobs else default_jobs_path()
            epoch = gate_raise_epoch(ledger, args.gate)
            # The raise happens in the owner's cwd and the release in the
            # depth-0 session's; a relative artifact path would name two files
            # (review round 1, B2). Seal the absolute path in the record and
            # the journal.
            args.artifact = str(Path(str(args.artifact)).expanduser().resolve(strict=False))
            interview = load_interview_artifact(args.artifact)
            if (args.gate == "frame-review"
                    and any(n.get("worker_type") == "frame" and n.get("dispatch_depth") == 1
                            for n in route.get("nodes", []))
                    and interview is None):
                raise SupervisorError("frame-interview-required: bootstrap confirmation requires an interview and the person's answers")
            if interview is not None:
                # SD-129: an interview is refused BEFORE it reaches a person when
                # a tired reader could not answer it -- the validator is the
                # acceptance bar, not a style hint.
                errors = INTERVIEW.validate(
                    interview, intensity=str(route.get("effective_intensity") or "standard"))
                if errors:
                    raise SupervisorError("interview-invalid: " + "; ".join(errors[:8]))
                if interview.get("round", 1) != epoch + 1:
                    raise SupervisorError(
                        f"interview-round-mismatch: interview round {interview.get('round', 1)} "
                        f"but this is raise {epoch + 1} of {args.gate!r}")
                if interview.get("route_id") != route["route_id"]:
                    raise SupervisorError(
                        f"interview-route-mismatch: {interview.get('route_id')!r} is not this route")
            release_authority = gate_release_authority_at_raise(
                gates[args.gate], interview, args.artifact)
            if args.gate == "frame-review" and any(n.get("worker_type") == "frame" for n in route.get("nodes", [])):
                release_authority = "depth-0"
            inline_gate = any(args.gate in n.get("inline_human_gates", []) for n in route.get("nodes", []))
            if inline_gate:
                if not Path(args.artifact).is_file():
                    raise SupervisorError("inline-gate-preview-unreadable")
                preview_digest = hashlib.sha256(Path(args.artifact).read_bytes()).hexdigest()
            current_state = ledger.state()["workflow_state"]
            revised = (current_state == "FAILED_RETRYABLE"
                       and WS.human_gate_resolution(ledger.journal(), args.gate)["status"] == "revise")
            # Re-enter an explicitly revised gate through the existing retry
            # state path. Validate both hops before creating a delivery record.
            if revised:
                WS.assert_transition(current_state, "READY")
            WS.assert_transition("READY" if revised else current_state, "BLOCKED_HUMAN_GATE")
            record_path, created = create_gate_delivery(
                route, args.gate, args.artifact, jobs_path, epoch,
                route_path=args.route, release_authority=release_authority,
                interview=interview is not None,
                questions=len(interview.get("questions") or []) if interview is not None else 0,
            )
            try:
                if revised:
                    ledger.set_workflow_state("READY", evidence={"retry_gate": args.gate}, actor="gate")
                # `artifact` rides in the journal so every later reader -- the
                # owner's await-release, the launch fence, a release that
                # validates interview answers -- finds the reviewable path
                # from the ledger alone, without reopening the delivery record.
                ledger.set_workflow_state("BLOCKED_HUMAN_GATE",
                                          evidence={"gate": args.gate,
                                                    "binding": gates[args.gate],
                                                    "delivery": str(record_path),
                                                    "artifact": str(args.artifact),
                                                    "interview": interview is not None,
                                                    "questions": len(interview.get("questions") or [])
                                                    if interview is not None else 0,
                                                    "release_authority": release_authority,
                                                    **({"artifact_sha256": preview_digest} if inline_gate else {})},
                                          actor="gate")
            except BaseException:
                if created:
                    _rollback_gate_delivery(record_path)
                raise
            payload.update({"delivery": str(record_path), "delivery_created": created,
                            "interview": interview is not None,
                            "questions": len(interview.get("questions") or []) if interview is not None else 0,
                            "release_authority": release_authority,
                            "await_command": await_release_command(args.route, args.gate, getattr(args, "jobs", None))})
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
# One kind, two carriers: asyncRewake and the UserPromptSubmit sweep both
# deliver to a `claude-parent-runtime` recipient.
GATE_CARRIER_KINDS = frozenset({
    "claude-parent-runtime", "codex-managed-gateway",
})

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
            # N3, second door: this shortcut identifies the CALLER, and a frame
            # leg can be the caller. Excluding frame legs only from the depth
            # walk below would leave the same wrong answer reachable here --
            # a frame leg asking who owns the gate would be told "you do".
            # A frame leg is never the gate recipient, by either route in.
            if meta.get("worker_type") == "frame":
                break
            if route_id in (meta.get("owner_route_id"), meta.get("route_id")):
                return row
            break
    for row in reversed(rows):
        meta = row["meta"]
        if meta.get("dispatch_depth") != "1":
            continue
        # N3: depth 1 is no longer a synonym for "the owner". A route's frame
        # legs register at depth 1 too, and this walk returns the most RECENTLY
        # registered match -- so without this skip the direction-confirmation
        # gate is handed to a frame leg (a headless worker that cannot answer
        # it) instead of the session that opened the route, and the gate never
        # reaches the user. Registration ORDER must not decide the recipient.
        if meta.get("worker_type") == "frame":
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
        # A carrier may be selected only after its receipt vocabulary and live
        # recipient proof exist. OpenCode and the legacy Codex stop hook still do
        # not carry this contract and therefore fail closed here.
        raise SupervisorError(
            "gate-carrier-unsupported: no human-gate carrier for recipient kind "
            f"{recipient_kind!r} (SD-OPEN-33); supported: "
            + ", ".join(sorted(GATE_CARRIER_KINDS))
        )
    return recipient_key, recipient_kind, attempt_id, meta.get("harness", "-")


def currently_blocked_gate(ledger):
    """The gate the workflow is blocked on right now, or None.

    Read from the journal rather than inferred: `state()` carries the workflow
    state but not which gate produced it.
    """
    if ledger.state()["workflow_state"] != "BLOCKED_HUMAN_GATE":
        return None
    for entry in reversed(ledger.journal()):
        if entry.get("workflow_state") != "BLOCKED_HUMAN_GATE":
            continue
        gate = (entry.get("evidence") or {}).get("gate")
        if gate:
            return gate
    return None


def existing_gate_delivery(route, gate, jobs):
    """The record for the raise already in force, for a repeated `--block`.

    Reports `delivery_created: False` and never allocates a new raise epoch, so
    one raise keeps one record.
    """
    ledger = ledger_for(route, jobs)
    epoch = max(gate_raise_epoch(ledger, gate) - 1, 0)
    for entry in reversed(ledger.journal()):
        evidence = entry.get("evidence") or {}
        if entry.get("workflow_state") == "BLOCKED_HUMAN_GATE" and evidence.get("gate") == gate:
            recorded = evidence.get("delivery")
            if recorded and Path(recorded).is_file():
                return {"delivery": recorded, "delivery_created": False}
            break
    try:
        jobs_path = Path(jobs) if jobs else default_jobs_path()
        recipient_key, _kind, attempt_id, _harness = gate_recipient(route, jobs_path)
        root = Path(jobs_path).resolve(strict=False).parent
    except (SupervisorError, OSError):
        return {"delivery": None, "delivery_created": False}
    delivery_id = gate_delivery_id(recipient_key, route["route_id"], gate,
                                   attempt_id, epoch)
    path = PENDING.record_path(root, recipient_key, delivery_id)
    return {"delivery": str(path) if path.is_file() else None,
            "delivery_created": False}


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
    Repeated work inside one raise converges because `cmd_gate` refuses to mint a
    second raise while one is in force. It is NOT the ledger that prevents it:
    `assert_transition` returns early when current == target and
    `set_workflow_state` appends regardless, so a second `--block` really did
    create a second record before that guard existed.
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


def create_local_frame_gate_delivery(route, gate, artifact, jobs_path, epoch, *, route_path,
                                     release_authority, interview, questions):
    """A frame interview belongs to the already interactive parent, before any owner.

    Keep a durable local handback record. No fake owner or asynchronous receipt is
    issued: the raising depth-0 session displays this command's artifact/questions.
    """
    if gate != "frame-review" or _owner_row(_registry_rows(jobs_path), route["route_id"]) is not None:
        return None
    frames = [n for n in route.get("nodes", []) if n.get("worker_type") == "frame"
              and n.get("dispatch_depth") == 1]
    if {n.get("id") for n in frames} != {"frame", "frame-alternative"}:
        return None
    if release_actor_kind() == "headless-owner" or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"):
        raise SupervisorError("frame-gate-depth0-required")
    from dispatch_parent_completion import interactive_parent_identity
    try:
        parent_harness, session = interactive_parent_identity()
    except ValueError as exc:
        raise SupervisorError(str(exc)) from exc
    if not session:
        raise SupervisorError("frame-gate-parent-identity-missing")
    bindings = [b for b in route.get("human_gate_bindings", []) if b.get("gate") == gate]
    if len(bindings) != 1:
        raise SupervisorError("frame-gate-binding-invalid")
    from dispatch_contract import completion_marker_gate, dispatch_state_roots, parse_registry_metadata
    import dispatch_contract as contract
    rows = Path(jobs_path).read_text().splitlines()
    try:
        completion_marker_gate(str(route_path), bindings[0]["node"], "start", ROOT,
                               Path(jobs_path), registry_lines=rows, _raising_frame_gate=True)
    except contract.DispatchContractError as exc:
        raise SupervisorError(f"frame-gate-not-ready: {exc.reason}: {exc.detail}") from exc
    attempts = []
    for frame in frames:
        candidates = [root / "completion" / route["route_id"] / (frame["id"] + ".json")
                      for root in dispatch_state_roots(ROOT, Path(jobs_path))]
        marker_path = next(path for path in candidates if path.is_file())
        attempt = json.loads(marker_path.read_text())["attempt_id"]
        metadata = [parse_registry_metadata(line.split("\t")[5]) for line in rows
                    if len(line.split("\t")) == 6
                    and parse_registry_metadata(line.split("\t")[5]).get("attempt_id") == attempt]
        if len(metadata) != 1 or metadata[0].get("parent_sid") != session:
            raise SupervisorError("frame-gate-parent-binding-mismatch")
        attempts.append(attempt)
    if parent_harness == "codex":
        control = os.environ.get("AGENT_CODEX_MANAGED_CONTROL_SOCKET")
        if not control:
            raise SupervisorError("frame-gate-managed-parent-required")
        try:
            HUMAN_GATE.probe_consumer(Path(control), expected_thread_id=session)
        except HUMAN_GATE.HumanGateReceiptError as exc:
            raise SupervisorError(f"frame-gate-parent-unavailable: {exc}") from exc
    artifact_path = Path(artifact)
    if not artifact_path.is_absolute() or not artifact_path.is_file():
        raise SupervisorError("frame-gate-artifact-unreadable")
    ledger = ledger_for(route, jobs_path)
    path = ledger.root / "local-frame-gates" / f"raise-{epoch + 1}.json"
    record = {"schema_version": 1, "kind": "interactive-frame-handback", "state": "pending",
              "route_id": route["route_id"], "route_hash": route.get("route_hash"),
              "gate": gate, "epoch": epoch + 1, "recipient_session": session,
              "attempt_ids": attempts, "artifact": str(artifact), "interview": interview,
              "questions": questions, "release_authority": "depth-0"}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise SupervisorError("frame-gate-record-conflict")
        return path, False
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return path, True


def create_gate_delivery(
    route, gate, artifact, jobs_path, epoch, *, route_path,
    release_authority, interview, questions,
):
    """Write the one durable record a gate transition owes its depth-0 session.

    Returns `(record_path, created)`. Raises `SupervisorError` on any refusal --
    the caller must not take the transition if this fails, so a gate never exists
    without a way to reach a person.
    """
    local = create_local_frame_gate_delivery(
        route, gate, artifact, jobs_path, epoch, route_path=route_path,
        release_authority=release_authority, interview=interview, questions=questions)
    if local is not None:
        return local
    recipient_key, recipient_kind, attempt_id, harness = gate_recipient(route, jobs_path)
    delivery_id = gate_delivery_id(
        recipient_key, route["route_id"], gate, attempt_id, epoch
    )
    route_node = _gate_route_node(route, gate)
    if recipient_kind == HUMAN_GATE.RECIPIENT_KIND:
        control = os.environ.get("AGENT_CODEX_MANAGED_CONTROL_SOCKET")
        if not control:
            raise SupervisorError("gate-carrier-unavailable: managed control socket missing")
        try:
            capability = HUMAN_GATE.probe_consumer(
                Path(control), expected_thread_id=recipient_key
            )
        except HUMAN_GATE.HumanGateReceiptError as exc:
            raise SupervisorError(f"gate-carrier-unavailable: {exc}") from exc
        owner = _owner_row(_registry_rows(jobs_path), route["route_id"])
        metadata = owner["meta"] if owner is not None else {}
        sealed_batch_id = metadata.get("managed_sealed_batch_id") or ""
        if (
            owner is None or owner["status"] not in {"open", "running"}
            or metadata.get("attempt_id") != attempt_id
            or not sealed_batch_id
        ):
            raise SupervisorError("gate-carrier-unavailable: live sealed owner missing")
        try:
            receipt = HUMAN_GATE.make_receipt(
                route_file=Path(route_path).resolve(strict=False), route=route,
                route_node=route_node, gate=gate, gate_epoch=epoch + 1,
                owner_attempt_id=attempt_id, sealed_batch_id=sealed_batch_id,
                jobs=Path(jobs_path).resolve(strict=False),
                recipient_thread_id=recipient_key,
                recipient_epoch=capability["epoch"], artifact_path=Path(artifact),
                release_authority=release_authority, interview=bool(interview),
                questions=int(questions), pending_delivery_id=delivery_id,
            )
        except HUMAN_GATE.HumanGateReceiptError as exc:
            raise SupervisorError(f"gate-delivery-refused: {exc}") from exc
        session_generation = str(capability["epoch"])
        generation_supported = "1"
        receipt_digest = HUMAN_GATE.digest(receipt)
        row_revision = f"human-gate:{gate}:{epoch + 1}"
    else:
        receipt = gate_receipt(
            attempt_id=attempt_id, jobs_path=jobs_path, gate=gate,
            artifact=artifact, harness=harness,
        )
        session_generation = GATE_SESSION_GENERATION
        generation_supported = GATE_SESSION_GENERATION_SUPPORTED
        receipt_digest = PENDING._canonical_receipt_digest(receipt)
        row_revision = f"human-gate:{gate}"
    root = Path(jobs_path).resolve(strict=False).parent
    path = PENDING.record_path(root, recipient_key, delivery_id)
    # Reading existence before `create` was a TOCTOU, and holding
    # `PENDING._record_lock` across the call deadlocks because `create` takes the
    # same flock on its own descriptor. So ask the record itself: `create` stamps
    # `created_at_ns` inside that lock, and a record that already existed carries
    # an older stamp than this instant.
    #
    # The clock has to be the SAME clock. `create` uses `time.monotonic_ns()`;
    # comparing against `time.time_ns()` made this test always false, so
    # `delivery_created` was always reported False and the compensating rollback
    # below could never fire. CLOCK_MONOTONIC is system-wide on Linux, so the
    # comparison holds across processes.
    before_ns = time.monotonic_ns()
    try:
        record = PENDING.create(
            root,
            recipient_kind=recipient_kind,
            recipient_key=recipient_key,
            delivery_id=delivery_id,
            session_generation=session_generation,
            session_generation_supported=generation_supported,
            attempt_ids=[attempt_id],
            parent_attempt_id=attempt_id,
            route_id=route["route_id"],
            route_node=route_node,
            receipt=receipt,
            receipt_digest=receipt_digest,
            row_revisions={attempt_id: row_revision},
        )
    except PENDING.PendingDeliveryError as exc:
        raise SupervisorError(f"gate-delivery-refused: {exc}") from exc
    created = bool(record) and (record.get("created_at_ns") or 0) >= before_ns
    return path, created


def _rollback_gate_delivery(record_path):
    """Remove a gate record whose transition did not happen — but only while it is
    still untouched, so a carrier that already claimed it never loses it."""
    try:
        record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(record, dict) or record.get("state") != "pending":
        return
    try:
        Path(record_path).unlink()
    except OSError:
        pass


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


RELEASE_AUTHORITIES = ("depth-0", "any")
GATE_NOT_BLOCKED_REFUSAL = "gate-not-blocked"


def refuse_gate_not_blocked(route, gate, state):
    """One typed JSON line on stdout, then the usual prose refusal.

    SD-OPEN-48 (#13): the depth-0 carrier (`hooks/dispatch-owner-rewake.py`)
    re-arms its wait on this route's running owner from this line's
    `route_id` + `refusal` token -- never from the prose, never from the
    `--route` literal (review finding 13).
    """
    print(json.dumps({"gate": gate, "route_id": route["route_id"],
                      "refusal": GATE_NOT_BLOCKED_REFUSAL, "workflow_state": state},
                     sort_keys=True))
    raise SupervisorError(f"workflow is {state}, not blocked on a human gate")


def gate_release_authority_at_raise(binding, interview, artifact):
    """Who may release this raise: sealed into the journal at the raise.

    `depth-0` when the route binding declares it, when the artifact is an
    interview (its answers are the user's -- SD-129), or when the artifact
    itself declares `release_authority: depth-0` (the cairn W15b owner wrote
    exactly that into its own artifact and then released the gate itself,
    2026-09-06, rt-bf75754935faf8de). Otherwise `any`: the SD-123 (8)(d)
    allowance for a headless owner to release a plain, non-interview gate
    rather than die at it after 53 minutes stays as it was.
    """
    if (binding or {}).get("gate") == "preview-disposition":
        return "depth-0"  # Applying an edit always requires the person's decision.
    declared = str((binding or {}).get("release_authority") or "").strip()
    if declared:
        if declared not in RELEASE_AUTHORITIES:
            raise SupervisorError(
                f"gate-release-authority-invalid: binding declares {declared!r}; "
                f"expected one of {list(RELEASE_AUTHORITIES)}")
        return declared
    if interview is not None:
        return "depth-0"
    if artifact_declares_depth0_authority(artifact):
        return "depth-0"
    return "any"


def artifact_declares_depth0_authority(artifact):
    """True when a JSON artifact says `release_authority: depth-0` about itself."""
    if not artifact or artifact == "-":
        return False
    path = Path(str(artifact))
    if not path.is_file() or path.suffix.lower() != ".json":
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and str(value.get("release_authority") or "").strip() == "depth-0"


def assert_release_authority(actor_kind, resolution, binding, gate):
    """SD-OPEN-48: a registered headless owner may not release a gate whose
    raise sealed `release_authority=depth-0`.

    Contract (d) made an owner's release distinguishable; it did not make it
    acceptable for a gate that exists so a person decides. An interview gate
    released by its own owner records answers nobody gave, and the plan it
    unblocks was never confirmed -- and the depth-0 session's later release
    is then refused (`workflow is RUNNING, not blocked`), which is how the
    completion wake was lost on 2026-09-07 (#13). Legacy raises that recorded
    no authority keep the `any` allowance; a binding that declares `depth-0`
    is honoured even when the raise predates this field.
    """
    if actor_kind != "headless-owner":
        return
    if gate == "preview-disposition":
        raise SupervisorError("gate-release-authority-refused: preview-disposition requires the person's decision")
    authority = str((resolution or {}).get("release_authority") or "").strip()
    if not authority and (resolution or {}).get("interview"):
        # A raise that predates the field but recorded an interview: the
        # interview flag is itself sealed at the raise, so this is not a
        # re-read of the artifact (review finding 4).
        authority = "depth-0"
    if not authority:
        authority = str((binding or {}).get("release_authority") or "any").strip()
    if authority == "depth-0":
        raise SupervisorError(
            f"gate-release-authority-refused: {gate!r} is released by the depth-0 "
            "session (release_authority=depth-0), not by the registered owner that "
            "raised it; keep waiting with `await-release` -- the person records the "
            "decision with `release --decision proceed|revise|stop`")


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
        ledger = ledger_for(route, jobs)
        for entry in reversed(ledger.journal()):
            evidence = entry.get("evidence") or {}
            if entry.get("workflow_state") != "BLOCKED_HUMAN_GATE" or evidence.get("gate") != gate:
                continue
            path = Path(evidence.get("delivery") or "/missing")
            if path.parent != ledger.root / "local-frame-gates":
                break
            record = json.loads(path.read_text())
            if record.get("kind") != "interactive-frame-handback" or record.get("route_id") != route["route_id"]:
                break
            record["state"] = "acked"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, sort_keys=True))
            os.replace(temporary, path)
            return "acked"
        recipient_key, _kind, attempt_id, _harness = gate_recipient(route, jobs_path)
        root = Path(jobs_path).resolve(strict=False).parent
        # Inside the try as well: the release transition and its sidecar row are
        # already committed by the time this runs, so a `WorkflowStateError` from
        # reading the ledger would abort the CLI with no payload and leave a retry
        # failing with "workflow is RUNNING". Retirement is best-effort by design.
        ledger = ledger_for(route, jobs)
        highest = gate_raise_epoch(ledger, gate)
    except Exception:
        return None
    # Every raise of this gate, newest first: a release closes whichever raise is
    # still outstanding, and older ones may legitimately be acked already.
    for epoch in range(highest, -1, -1):
        delivery_id = gate_delivery_id(recipient_key, route["route_id"], gate,
                                       attempt_id, epoch)
        try:
            record = PENDING.read(root, recipient_key, delivery_id)
            if record is None or record.get("state") in {"acked", "expired"}:
                continue
            if record.get("state") not in {"claimed", "sent-ambiguous"}:
                # `pending`: nobody holds it, so take the ordinary claim path.
                PENDING.claim(root, recipient_key, delivery_id,
                              claim_owner=f"gate-release:{os.getpid()}",
                              lease_seconds=60.0, require_generation_proof=False)
            # `claimed` / `sent-ambiguous`: ack straight from the carrier's own
            # state. The release supersedes the record whoever holds its lease
            # -- a hook that claimed it seconds ago (lease unexpired) must not
            # keep the record alive past the release, or the next prompt sweep
            # re-announces a gate that is already closed (rt-94b7f5a5,
            # 2026-09-06: `reclaim` refused `lease-not-expired`, the record
            # stayed `sent-ambiguous`, and the sweep re-delivered it once).
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


def record_gate_release(route, route_path, *, gate, decision, released_by, actor_kind,
                        answers=None):
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
    if answers is not None:
        row["answers"] = answers
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
        if WS.node_raises_human_gate(node, gate_name):
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
    ledger = ledger_for(route, getattr(args, "jobs", None))
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
            refuse_gate_not_blocked(route, args.gate, state)
        assert_release_authority(
            actor_kind, WS.human_gate_resolution(ledger.journal(), args.gate),
            gates[args.gate], args.gate)
        inline_gate = args.gate in predecessor.get("inline_human_gates", [])
        if inline_gate and args.decision == "proceed":
            WS.require_gate_artifact_current(WS.human_gate_resolution(ledger.journal(), args.gate))
        answers = release_answers(ledger, args.gate, args.decision, getattr(args, "answers", None))
        if args.decision == "proceed":
            ledger.set_workflow_state(
                "RUNNING",
                evidence={"released_gate": args.gate, "released_by": actor,
                          "actor_kind": actor_kind, "decision": "proceed",
                          "answers": answers},
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
                evidence={"released_gate": args.gate, "released_by": actor,
                          "actor_kind": actor_kind, "decision": "revise",
                          "answers": answers},
                actor="release",
            )
            if not inline_gate:
                ledger.set_workflow_state(
                    "FAILED_RETRYABLE",
                    evidence={"gate": args.gate, "released_by": actor,
                              "retry_boundary": "frame", "next_stage": "code-refine"},
                    actor="release",
                )
            payload = {"gate": args.gate, "decision": "revise", "node": node_id,
                      "retry_boundary": "preview" if inline_gate else "frame",
                      "workflow_state": ledger.state()["workflow_state"]}
        elif args.decision == "stop":
            ledger.set_workflow_state(
                "CANCELLED",
                evidence={"gate": args.gate, "released_gate": args.gate,
                          "released_by": actor, "actor_kind": actor_kind,
                          "abandon_reason": "operator-decision"},
                actor="release",
            )
            payload = {"gate": args.gate, "decision": "stop", "node": node_id,
                      "workflow_state": ledger.state()["workflow_state"]}
        else:
            raise SupervisorError(f"unknown --decision: {args.decision!r}")
        payload["released_by"] = actor
        payload["actor_kind"] = actor_kind
        payload["answers_recorded"] = len((answers or {}).get("answers") or {}) if answers else 0
        # `route_id` lets the depth-0 carrier (`hooks/dispatch-owner-rewake.py`)
        # re-arm its wait on this route's owner from the release output alone,
        # even when the command spelled the route through a shell variable.
        payload["route_id"] = route["route_id"]
        payload.update(ledger_metadata(getattr(args, "jobs", None), ledger))
        record_gate_release(route, args.route, gate=args.gate, decision=args.decision,
                            released_by=actor, actor_kind=actor_kind, answers=answers)
        retire_gate_delivery(route, args.gate, args.jobs)
    print(json.dumps(payload, sort_keys=True))
    return 0


def load_interview_artifact(artifact):
    """The interview at `artifact`, or None when the artifact is something else
    (a legacy frame summary, a directory, a non-JSON file). Unreadable JSON that
    claims to be an interview is an error, not None."""
    if not artifact or artifact == "-":
        return None
    path = Path(str(artifact))
    if not path.is_file() or path.suffix.lower() != ".json":
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"interview-unreadable: {path}: {exc}") from exc
    if not INTERVIEW.is_interview(value):
        # SD-OPEN-48 (#12): an artifact that calls itself an interview under
        # some other schema (`cairn-frame-interview/v1`, 2026-09-07) used to
        # pass here as "not an interview", so the gate was raised without
        # answers owed and `validate-answers` later refused every real answer
        # with `no such question`. Refuse it where the owner can still fix it.
        foreign = INTERVIEW.foreign_interview_schema(value)
        if foreign is not None:
            raise SupervisorError(
                f"interview-schema-unsupported: {path} declares schema {foreign!r}; the frame "
                f"gate accepts only {INTERVIEW.SCHEMA!r} (write it with the shape "
                "`frame_interview.py answers-template` reads, then `frame_interview.py "
                "validate`), or raise the gate with a plain non-interview artifact")
        return None
    value.setdefault("self_path", str(path))
    return value


def release_answers(ledger, gate, decision, answers_path):
    """The validated answers this release records, or None.

    Whether answers are owed is decided by what the RAISE recorded in the
    journal (`interview`, `questions`), never by re-reading the artifact at
    release time (review round 1, B2: a relative path, a moved file or a
    rewritten round-2 interview each let `proceed` through without answers).
    An interview gate released `proceed` without answers is refused: the
    questions were the point, and a plan written without the answers is the
    guess the interview exists to replace. `revise`/`stop` may carry answers or
    not. Answers offered for a gate whose raise was not an interview are
    refused rather than dropped silently. Validating the answers still needs
    the interview text; when it cannot be read the release is refused, not
    waved through."""
    resolution = WS.human_gate_resolution(ledger.journal(), gate)
    is_interview = bool(resolution.get("interview"))
    if answers_path is None:
        if is_interview and decision == "proceed":
            raise SupervisorError(
                "interview-answers-required: this gate carries "
                f"{resolution.get('questions') or 0} question(s); record the user's "
                "answers with --answers <file> (template: frame_interview.py answers-template)")
        return None
    if not is_interview:
        raise SupervisorError("interview-absent: --answers given but this gate was not raised with an interview")
    interview = load_interview_artifact(resolution.get("artifact"))
    if interview is None:
        raise SupervisorError(
            f"interview-artifact-unreadable: the raise named {resolution.get('artifact')!r} as its "
            "interview and it is no longer a readable interview file; restore it or release with "
            "revise/stop")
    try:
        answers = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"interview-answers-unreadable: {answers_path}: {exc}") from exc
    errors = INTERVIEW.validate_answers(interview, answers)
    if errors:
        raise SupervisorError("interview-answers-invalid: " + "; ".join(errors[:8]))
    return answers


# --- SD-129: the owner waits for the release on a checked, bounded surface ------
#
# SD-123 (8) gave the gate a way to reach a person. It gave the owner nothing to
# wait on: "wait for the release rather than polling or sleeping" was prose, and
# a headless owner has exactly two ways to wait -- poll or exit. Measured on the
# eight real `frame-review` raises before this cycle: three owners released
# their own gate, one wrote an ad-hoc polling script and sat 53 minutes, one
# did not wait at all and spawned `plan` before the release (defect M). This is
# the one checked wait: bounded, read-only, and answered from the same journal
# rule the launch fence uses, so the owner and the fence can never disagree.

AWAIT_RELEASE_EXIT = {"proceed": 0, "blocked": 2, "revise": 3, "stop": 4}
DEFAULT_AWAIT_MAX_SECONDS = 110.0   # one foreground Bash call in an owner turn
MAX_AWAIT_SECONDS = 600.0


def await_release_command(route_path, gate, jobs=None):
    command = (f"python3 <agent-home>/utilities/workflow-supervisor.py await-release "
               f"--route {shlex.quote(str(route_path))} --gate {shlex.quote(str(gate))}")
    if jobs:
        command += f" --jobs {shlex.quote(str(jobs))}"
    return command + f" --max {int(DEFAULT_AWAIT_MAX_SECONDS)}"


def cmd_await_release(args):
    try:
        route = load_route(args.route)
    except SupervisorError:
        print(json.dumps({"route": str(args.route), "gate": args.gate,
                          "status": "error", "reason": "route-unreadable"}, sort_keys=True))
        raise
    ledger = ledger_for(route, getattr(args, "jobs", None))
    gates = {row["gate"]: row for row in (route.get("human_gate_bindings") or [])}
    if args.gate not in gates:
        # Every refusal of this surface prints one typed JSON line on stdout
        # before the exit-64 prose (review round 1, minor 6): an owner script
        # branches on `reason`, not on the message.
        print(json.dumps({"route_id": route["route_id"], "gate": args.gate,
                          **ledger_metadata(getattr(args, "jobs", None), ledger),
                          "status": "error", "reason": "gate-undeclared"}, sort_keys=True))
        raise SupervisorError(f"route declares no human gate {args.gate!r}")
    interval = max(1.0, float(args.interval))
    maximum = min(max(0.0, float(args.max)), MAX_AWAIT_SECONDS)
    started = time.monotonic()
    deadline = started + maximum
    while True:
        # Read-only on purpose: `read_only_state()` never repairs the cache or
        # creates the ledger directory, so a waiting owner mutates nothing.
        resolution = WS.human_gate_resolution(ledger.journal(), args.gate)
        status = resolution["status"]
        if status == "not-raised":
            print(json.dumps({"route_id": route["route_id"], "gate": args.gate,
                              **ledger_metadata(getattr(args, "jobs", None), ledger),
                              "status": "not-raised", "reason": "gate-never-raised"},
                             sort_keys=True))
            raise SupervisorError(
                f"gate-never-raised: {args.gate!r} has no BLOCKED_HUMAN_GATE entry; "
                "raise it with `gate --block --artifact <path>` first"
            )
        if status != "blocked" or time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    payload = {
        "route_id": route["route_id"], "gate": args.gate, "status": status,
        **ledger_metadata(getattr(args, "jobs", None), ledger),
        "epoch": resolution["epoch"], "waited_seconds": round(time.monotonic() - started, 1),
        "released_by": resolution["released_by"], "actor_kind": resolution["actor_kind"],
        "artifact": resolution["artifact"], "answers": resolution["answers"],
        "workflow_state": ledger.read_only_state()["workflow_state"],
    }
    if status == "proceed":
        payload["successor"] = [str(b.get("node")) for b in route.get("human_gate_bindings") or []
                                if b.get("gate") == args.gate]
    answers_out = getattr(args, "answers_out", None)
    if answers_out and resolution["answers"] is not None and status in ("proceed", "revise"):
        out = Path(answers_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(resolution["answers"], ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(out))
        payload["answers_file"] = str(out)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return AWAIT_RELEASE_EXIT[status]


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
    ledger = ledger_for(route, getattr(args, "jobs", None))
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
        **ledger_metadata(getattr(args, "jobs", None), ledger),
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
    ledger = ledger_for(route, getattr(args, "jobs", None))
    terminal_nodes = WS.route_terminal_nodes(route)
    if not terminal_nodes:
        raise SupervisorError("route declares no terminal node")
    with ledger.lock():
        state = ledger.state()
        gates = terminal_gate_state(route)
        unproven = {node: row for node, row in gates.items() if row.get("passed") is not True}
        unproven.update({node: {"passed": False, "reason": "missing-terminal-gate"}
                         for node in terminal_nodes if node not in gates})
        if unproven:
            print(json.dumps({"complete": False, "reason": "terminal-gate-unproven",
                              **ledger_metadata(getattr(args, "jobs", None), ledger),
                              "unproven": unproven,
                              "workflow_state": state["workflow_state"]}, sort_keys=True))
            return 3
        ledger.complete(terminal_nodes, gates)
    print(json.dumps({"complete": True, **ledger_metadata(getattr(args, "jobs", None), ledger),
                      "terminal_nodes": terminal_nodes,
                      "workflow_state": ledger.state()["workflow_state"]}, sort_keys=True))
    return 0


def cmd_recover_gate_delivery(args):
    """Preview, or explicitly apply, one exact unreleasable gate expiry."""
    route = load_route(args.route)
    raw_jobs_path = Path(args.jobs).expanduser()
    try:
        # Use the same authority validation as every workflow reader/writer;
        # importantly, inspect the caller's spelling before resolving it so a
        # symlink cannot disappear into an apparently regular target.
        WS.ledger_root_for(raw_jobs_path)
    except WS.WorkflowStateError as exc:
        raise SupervisorError(
            f"gate-delivery-recovery-refused: {exc}"
        ) from exc
    jobs_path = raw_jobs_path.resolve(strict=True)
    if args.raise_epoch < 1:
        raise SupervisorError("gate-delivery-recovery-refused: raise-epoch-invalid")
    rows = _registry_rows(jobs_path)
    latest = {}
    for row in rows:
        attempt_id = row["meta"].get("attempt_id")
        if attempt_id:
            latest[attempt_id] = row
    owner = latest.get(args.source_attempt_id)
    if owner is None:
        raise SupervisorError("gate-delivery-recovery-refused: source-attempt-missing")
    meta = owner["meta"]
    if (
        owner["status"] != "done" or meta.get("dispatch_depth") != "1"
        or meta.get("worker_type") != "owner"
        or meta.get("registered_worker") != "1"
        or meta.get("execution_surface") != "registered-headless"
    ):
        raise SupervisorError("gate-delivery-recovery-refused: source-owner-not-terminal")
    if route["route_id"] not in (meta.get("owner_route_id"), meta.get("route_id")):
        raise SupervisorError("gate-delivery-recovery-refused: source-route-mismatch")
    if meta.get("owner_route_hash") and meta["owner_route_hash"] != route.get("route_hash"):
        raise SupervisorError("gate-delivery-recovery-refused: source-route-hash-mismatch")
    if meta.get("owner_route_file") and Path(meta["owner_route_file"]).resolve(strict=False) \
            != Path(args.route).resolve(strict=False):
        raise SupervisorError("gate-delivery-recovery-refused: source-route-file-mismatch")
    recipient_key = meta.get("parent_sid") or ""
    kind = meta.get("parent_completion_delivery") or ""
    if recipient_key != args.recipient or kind not in PENDING.RECIPIENT_KINDS:
        raise SupervisorError("gate-delivery-recovery-refused: recipient-or-kind-mismatch")
    for attempt_id, row in latest.items():
        other = row["meta"]
        if attempt_id == args.source_attempt_id or row["status"] == "done":
            continue
        if other.get("dispatch_depth") == "1" and route["route_id"] in (
            other.get("owner_route_id"), other.get("route_id")
        ):
            raise SupervisorError("gate-delivery-recovery-refused: route-owner-still-live")
    expected_id = gate_delivery_id(recipient_key, route["route_id"], args.gate,
                                    args.source_attempt_id, args.raise_epoch - 1)
    if expected_id != args.delivery_id:
        raise SupervisorError("gate-delivery-recovery-refused: delivery-id-mismatch")
    ledger = ledger_for(route, jobs_path)
    resolution = WS.human_gate_resolution(ledger.journal(), args.gate)
    if resolution["status"] == "not-raised" or resolution["epoch"] != args.raise_epoch:
        raise SupervisorError("gate-delivery-recovery-refused: raise-epoch-mismatch")
    current_gate = currently_blocked_gate(ledger)
    if current_gate not in (None, args.gate):
        raise SupervisorError("gate-delivery-recovery-refused: different-live-gate")
    root = jobs_path.parent
    record = PENDING.read(root, recipient_key, args.delivery_id)
    if record is None:
        raise SupervisorError("gate-delivery-recovery-refused: delivery-missing")
    receipt = record.get("receipt")
    if not isinstance(receipt, dict):
        raise SupervisorError("gate-delivery-recovery-refused: receipt-invalid")
    if receipt.get("kind") == HUMAN_GATE.KIND:
        try:
            normalized = HUMAN_GATE.validate_receipt(
                receipt, jobs=jobs_path, expected_thread_id=recipient_key,
                expected_epoch=int(record.get("session_generation") or 0),
                expected_attempts={args.source_attempt_id},
                expected_sealed_batch_id=(meta.get("managed_sealed_batch_id")
                                          or receipt.get("sealed_batch_id")),
                validate_live=False,
            )
            HUMAN_GATE.validate_digest(normalized, record.get("receipt_digest"))
        except (HUMAN_GATE.HumanGateReceiptError, ValueError) as exc:
            raise SupervisorError(
                "gate-delivery-recovery-refused: strict-receipt-invalid"
            ) from exc
        if (
            normalized.get("route_id") != route["route_id"]
            or normalized.get("route_hash") != route.get("route_hash")
            or normalized.get("gate") != args.gate
            or normalized.get("gate_epoch") != args.raise_epoch
            or normalized.get("owner_attempt_id") != args.source_attempt_id
            or normalized.get("pending_delivery_id") != args.delivery_id
            or normalized.get("job_registry") != str(jobs_path)
            or normalized.get("route_node") != _gate_route_node(route, args.gate)
        ):
            raise SupervisorError("gate-delivery-recovery-refused: strict-receipt-identity")
        expected_receipt_digest = HUMAN_GATE.digest(normalized)
        expected_revision = f"human-gate:{args.gate}:{args.raise_epoch}"
    else:
        children = receipt.get("children")
        child = children[0] if isinstance(children, list) and len(children) == 1 else {}
        if (
            receipt.get("parent_attempt_id") != args.source_attempt_id
            or receipt.get("job_registry") != str(jobs_path)
            or child.get("attempt_id") != args.source_attempt_id
            or child.get("required_action") != f"human-gate:{args.gate}"
        ):
            raise SupervisorError("gate-delivery-recovery-refused: legacy-receipt-identity")
        expected_receipt_digest = PENDING._canonical_receipt_digest(receipt)
        expected_revision = f"human-gate:{args.gate}"
    revision = (record.get("row_revisions") or {}).get(args.source_attempt_id)
    if revision != expected_revision:
        raise SupervisorError("gate-delivery-recovery-refused: receipt-row-mismatch")
    expected = {
        "recipient_kind": kind,
        "attempt_ids": [args.source_attempt_id],
        "parent_attempt_id": args.source_attempt_id,
        "route_id": route["route_id"],
            "route_node": _gate_route_node(route, args.gate),
        "receipt_digest": expected_receipt_digest,
        "row_revisions": {args.source_attempt_id: expected_revision},
    }
    result = PENDING.expire_recovery(
        root, recipient_key, args.delivery_id, expected=expected,
        actor=args.actor, reason=args.reason, apply=args.apply,
    )
    result.update({"eligible": True, "apply": bool(args.apply), "route_id": route["route_id"],
                   "gate": args.gate, "delivery_id": args.delivery_id,
                   "recipient": recipient_key, "recipient_kind": kind,
                   "source_attempt_id": args.source_attempt_id,
                   "raise_epoch": args.raise_epoch,
                   **ledger_metadata(jobs_path, ledger)})
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
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

    ledger = ledger_for(route, None)
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
    poll.add_argument("--jobs")

    watch = sub.add_parser("watch", help="poll until terminal or the bounded deadline")
    watch.add_argument("--route", required=True)
    watch.add_argument("--jobs")
    watch.add_argument("--max", type=float, default=3600.0)
    watch.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL)

    gate = sub.add_parser("gate", help="record or release a declared human gate")
    gate.add_argument("--route", required=True)
    gate.add_argument("--gate", required=True)
    gate.add_argument("--by")
    gate.add_argument("--jobs", help="canonical registry path (default: dispatch state root)")
    gate.add_argument("--artifact", default="-",
                      help="path to the artifact a person reviews at this gate; "
                           "carried in the delivery record, never its contents. "
                           "Required with --block, and \"-\" is not accepted "
                           "there; --release ignores it")
    group = gate.add_mutually_exclusive_group(required=True)
    group.add_argument("--release", action="store_true")
    group.add_argument("--block", action="store_true")

    release = sub.add_parser("release", help="resolve a declared human gate: proceed, revise, or stop")
    release.add_argument("--route", required=True)
    release.add_argument("--gate", required=True)
    release.add_argument("--decision", required=True, choices=("proceed", "revise", "stop"))
    release.add_argument("--actor")
    release.add_argument("--answers",
                         help="frame interview answers file (frame_interview.py answers-template); "
                              "required for --decision proceed when the gate artifact is an interview")
    release.add_argument("--jobs",
                         help="canonical registry, used to retire the gate's pending "
                              "delivery record; without it retirement is skipped and a "
                              "released gate keeps being announced by the sweep")

    await_release = sub.add_parser(
        "await-release",
        help="bounded, read-only wait for a raised human gate to be released "
             "(exit 0 proceed, 2 still blocked, 3 revise, 4 stop)")
    await_release.add_argument("--route", required=True)
    await_release.add_argument("--gate", required=True)
    await_release.add_argument("--jobs")
    await_release.add_argument("--max", type=float, default=DEFAULT_AWAIT_MAX_SECONDS,
                               help=f"seconds to wait before exit 2 (clamped to {int(MAX_AWAIT_SECONDS)})")
    await_release.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL)
    await_release.add_argument("--answers-out",
                               help="write the recorded interview answers here (owner-owned path) "
                                    "so `frame_interview.py render-intent` can consume them")

    status = sub.add_parser("status", help="portable workflow/stage/resource projection")
    status.add_argument("--route", required=True)
    status.add_argument("--jobs")
    status.add_argument("--json", action="store_true")

    complete = sub.add_parser("complete", help="verify terminal gates, then close the workflow")
    complete.add_argument("--route", required=True)
    complete.add_argument("--jobs")

    recover = sub.add_parser("recover-gate-delivery",
                             help="preview or atomically expire one unreleasable gate delivery")
    recover.add_argument("--route", required=True)
    recover.add_argument("--gate", required=True)
    recover.add_argument("--delivery-id", required=True)
    recover.add_argument("--recipient", required=True)
    recover.add_argument("--source-attempt-id", required=True)
    recover.add_argument("--raise-epoch", required=True, type=int)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--jobs", required=True)
    recover.add_argument("--apply", action="store_true")

    survey = sub.add_parser("survey", help="read-only, root-scoped abandoned/stuck workflow report")
    survey.add_argument("--artifact-root", required=True)
    survey.add_argument("--stale-after-seconds", type=float, default=DEFAULT_STALE_AFTER_SECONDS)
    survey.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handler = {
        "arm": cmd_arm, "poll": cmd_poll, "watch": cmd_watch, "gate": cmd_gate,
        "release": cmd_release, "await-release": cmd_await_release,
        "status": cmd_status, "complete": cmd_complete, "recover-gate-delivery": cmd_recover_gate_delivery,
        "survey": cmd_survey,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SupervisorError, WS.WorkflowStateError) as exc:
        print(f"workflow-supervisor: {exc}", file=sys.stderr)
        raise SystemExit(64)
