"""Start a sealed request through the existing selector, join and human gate.

This is orchestration, not another outcome or retry policy. Stable attempt ids
reuse the adapter's atomic claim; all completion decisions use the shared join.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

from dispatch_contract import (
    DispatchContractError, completion_marker_gate, owner_frame_launch_gate,
    parse_registry_metadata,
)
from dispatch_completion_join import (
    join_selected_attempts, current_delivery_state, delivery_classification,
    delivery_required_action, completion_harvest_command,
)
from dispatch_parent_completion import default_parent_session_id
from parent_next_directive import parent_next

ROOT = Path(__file__).resolve().parents[1]


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"text", "owner_harness"}:
        raise ValueError("work-request-invalid")
    if not isinstance(value["text"], str) or not value["text"].strip():
        raise ValueError("work-request-empty")
    if value["owner_harness"] not in {None, "claude", "codex", "opencode"}:
        raise ValueError("work-request-owner-invalid")
    return value


def attempt_id(route, node):
    digest = hashlib.sha256((route["route_id"] + ":" + node).encode()).hexdigest()[:32]
    return "att-" + digest


def _rows(jobs):
    if not jobs.exists():
        return {}
    rows = {}
    for line in jobs.read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            meta = parse_registry_metadata(fields[5])
            if meta.get("attempt_id"):
                rows[meta["attempt_id"]] = (fields[1], meta)
    return rows


def _slot(route, node, rows):
    matches = [aid for aid, (_, meta) in rows.items()
               if ((node == "owner" and meta.get("worker_type") == "owner"
                    and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")})
                   or (node != "owner" and meta.get("route_id") == route["route_id"]
                       and meta.get("route_node") == node and meta.get("worker_type") == "frame"))]
    aid = matches[-1] if matches else attempt_id(route, node)
    if aid in rows:
        if not matches:
            raise DispatchContractError("work-attempt-identity-conflict", aid)
        meta = rows[aid][1]
        parent = default_parent_session_id()
        if not parent or meta.get("parent_sid") != parent:
            raise DispatchContractError("work-parent-recovery-required", aid)
        digest = (meta.get("owner_route_hash") or meta.get("route_hash")) if node == "owner" else meta.get("route_hash")
        if digest != route["route_hash"]:
            raise DispatchContractError("work-attempt-identity-conflict", aid)
    return aid


def _start(route, path, jobs, node, harness, run):
    command = [sys.executable, str(ROOT / "utilities/dispatch-owner.py"), "--start",
               "--route-evidence", str(path), "--jobs", str(jobs),
               "--slug", route["slug"] + "-" + node,
               "--attempt-id", attempt_id(route, node),
               "--prompt-text", route["work_request"]["text"]]
    if node != "owner":
        command += ["--route-node", node]
    if harness:
        command += ["--adapter", harness]
    result = run(command, text=True, capture_output=True, check=False)
    return {"attempt_id": attempt_id(route, node), "exit_code": result.returncode,
            "receipt": result.stdout, "diagnostic": result.stderr}


def _wait_fields(attempts, rows, resume):
    directives = [parent_next(rows[a][1].get("parent_completion_delivery", ""), a, agent_home=ROOT)[0]
                  for a in attempts]
    automatic = bool(directives) and all(d == "end-turn" for d in directives)
    return {"parent_next": "end-turn" if automatic else "bounded-wait",
            "parent_next_command": "" if automatic else resume + " --wait"}


def _outcome(jobs, aid):
    state = current_delivery_state(jobs, aid, parent_attempt_id=aid, advance=False)
    action = delivery_required_action(state)
    return {"attempt_id": aid, "classification": delivery_classification(state),
            "required_action": action, "marker": state.marker,
            "recovery_command": completion_harvest_command(aid, action, jobs=str(jobs),
                surface=str(ROOT / "adapters/codex/bin/preflight.sh"))}


def _advance(route, path, jobs, result, *, wait=False, run=subprocess.run):
    """Advance preparation once; repeating this call creates no duplicate job."""
    request = validate_request(route.get("work_request"))
    path, jobs = Path(path).resolve(), Path(jobs).resolve()
    resume = shlex.join([sys.executable, str(ROOT / "utilities/capability-route.py"),
                         "start", "--route", str(path), "--jobs", str(jobs)])
    result["resume_command"] = resume
    if route["effective_intensity"] == "direct":
        return {**result, "state": "inline", "required_action": "execute-inline",
                "task": request["text"]}
    rows = _rows(jobs)
    existing_owner = _slot(route, "owner", rows)
    frames = ([] if existing_owner in rows else
              [n for n in route["nodes"] if n.get("worker_type") == "frame" and n.get("dispatch_depth") == 1])
    if frames:
        candidates = (route.get("registered_headless_candidates") or []) if route["effective_intensity"] == "quick" else (route.get("dispatch_evidence") or {}).get("tuples", [])
        key = "harness" if route["effective_intensity"] == "quick" else "child_harness"
        harnesses = list(dict.fromkeys(c[key] for c in candidates if c.get("status") == "supported" and c.get(key)))
        if not harnesses:
            return {**result, "state": "needs-attention", "reason": "frame-harness-unavailable"}
        attempts = set()
        # Validate every reused identity before starting any missing sibling.
        slots = [_slot(route, node["id"], rows) for node in frames]
        for index, node in enumerate(frames):
            aid = slots[index]
            if aid not in rows:
                launch = _start(route, path, jobs, node["id"], harnesses[index % len(harnesses)], run)
                result["launches"].append(launch)
                rows = _rows(jobs)
                if aid not in rows:
                    return {**result, "state": "needs-attention", "reason": "frame-launch-not-admitted",
                            "frame_attempts": sorted(attempts),
                            **(_wait_fields(attempts, rows, resume) if attempts else {})}
            attempts.add(aid)
            result["frame_attempts"] = sorted(attempts)
            result.update(_wait_fields(attempts, rows, resume))
        joined = join_selected_attempts(jobs=jobs, expected_attempts=attempts, timeout=600 if wait else 0, recover=True)
        result["observation"] = joined
        if joined["state"] != "ready":
            return {**result, "state": "preparing",
                    "required_action": "wait-for-frame-results"}
        result.pop("parent_next", None)
        result.pop("parent_next_command", None)
        result["frame_results"] = [_outcome(jobs, aid) for aid in sorted(attempts)]
        if any(outcome["classification"] != "success" for outcome in result["frame_results"]):
            return {**result, "state": "needs-attention", "reason": "frame-outcome-needs-inspection"}
        entry = next(n for n in route["nodes"] if {f["id"] for f in frames}.issubset(set(n.get("depends_on", []))))
        completion_marker_gate(str(path), entry["id"], "start", ROOT, jobs, _raising_frame_gate=True)
        try:
            owner_frame_launch_gate(SimpleNamespace(route_file=str(path)), "start", ROOT, jobs)
        except DispatchContractError as exc:
            if exc.reason not in {"human-gate-not-raised", "human-gate-unreleased"}:
                raise
            return {**result, "state": "needs-question", "required_action": "compare-frames-and-ask-user",
                    "frame_markers": [str(jobs.parent / "completion" / route["route_id"] / (n["id"] + ".json")) for n in frames],
                    "gate": "frame-review", "task": request["text"]}
    rows = _rows(jobs)
    aid = _slot(route, "owner", rows)
    if aid not in rows:
        result["launches"].append(_start(route, path, jobs, "owner", request["owner_harness"], run))
        rows = _rows(jobs)
    if aid not in rows:
        return {**result, "state": "needs-attention", "reason": "owner-launch-not-admitted"}
    status, metadata = rows[aid]
    result.update(owner_attempt_id=aid, owner_started=metadata.get("launch_started") == "1")
    joined = join_selected_attempts(jobs=jobs, expected_attempts={aid}, timeout=600 if wait else 0, recover=True)
    if joined["state"] == "ready":
        outcome = _outcome(jobs, aid)
        return {**result, "state": "completed" if outcome["classification"] == "success" else "needs-attention",
                "result": outcome}
    directive, reason, _ = parent_next(metadata.get("parent_completion_delivery", ""), aid, agent_home=ROOT)
    return {**result, "state": "running", "parent_next": directive, "parent_next_reason": reason,
            "parent_next_command": resume + " --wait" if directive == "bounded-wait" else ""}


def start_work(route, path, jobs, *, wait=False, run=subprocess.run):
    result = {"route_file": str(Path(path).resolve()), "route_id": route["route_id"],
              "launches": [], "owner_started": False,
              "resume_command": shlex.join([sys.executable, str(ROOT / "utilities/capability-route.py"),
                  "start", "--route", str(Path(path).resolve()), "--jobs", str(Path(jobs).resolve())])}
    try:
        result = _advance(route, path, jobs, result, wait=wait, run=run)
    except (OSError, ValueError) as exc:
        result = {**result, "state": "needs-attention",
                  "reason": getattr(exc, "reason", type(exc).__name__), "detail": str(exc)}
        # A caller can lose the launch reply after the adapter claimed a row.
        # Preserve those durable obligations in this receipt too; do not spawn
        # a replacement or infer completion from the caller's exception.
        try:
            rows = _rows(Path(jobs))
            parent = default_parent_session_id()
            owned = {aid for aid, (_, meta) in rows.items() if parent and meta.get("parent_sid") == parent
                     and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")}}
            if owned:
                result["registered_attempts"] = sorted(owned)
                result.update(_wait_fields(owned, rows, result["resume_command"]))
        except (OSError, ValueError) as observation_error:
            result["observation_error"] = str(observation_error)
    if result["state"] == "needs-attention":
        result["required_action"] = "inspect-preparation"
        result["resume_command"] = shlex.join([sys.executable, str(ROOT / "utilities/capability-route.py"),
                                                "start", "--route", str(path), "--jobs", str(jobs)])
        result["next_step"] = ("Inspect the exact diagnostic or result recovery_command. Existing workers retain "
            "their runtime watcher and completion delivery. Correct the admission input or resolve the reported "
            "failure, then use resume_command; it does not create a replacement for a failed attempt. "
            "If the correction changes the requested work, ask the user before changing that work.")
    return result
