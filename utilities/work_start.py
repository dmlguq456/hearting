"""Start a sealed request through the existing selector, join and human gate.

This is orchestration, not another outcome or retry policy. Stable attempt ids
reuse the adapter's atomic claim; all completion decisions use the shared join.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
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


def _store_once(path, value):
    """A replay reuses the question/answer bytes; it cannot replace them."""
    _store_bytes_once(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())


def _store_bytes_once(path, encoded):
    from artifact_receipt import _write_once
    if path.is_symlink():
        raise ValueError(f"frame-input-symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _write_once(path.parent, path, encoded) and path.read_bytes() != encoded:
        raise ValueError(f"frame-input-conflict: preserve {path}; use the existing gate revision path for changes")


def frame_interview_step(route, path, jobs, *, interview=None, answers=None,
                         decision="proceed", runtime_root=ROOT, run=subprocess.run):
    """Own raise -> actual answers -> intent -> release through existing commands.

    The caller supplies semantic questions/real answers. WorkflowLedger remains
    the only gate authority. runtime_root names the checked execution root;
    an operator can use this orchestration with an already pinned older runtime.
    """
    import frame_interview as FI
    import workflow_state as WS
    from artifact_producer import prepare_route_artifact_env
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    resolution = WS.human_gate_resolution(ledger.journal(), "frame-review")
    supplied = json.loads(Path(interview).read_text()) if interview else None
    if supplied is None and resolution.get("artifact"):
        supplied = json.loads(Path(resolution["artifact"]).read_text())
    if supplied is None:
        if answers:
            raise ValueError("frame-question-required: pass the question already answered with --interview")
        return {"state": "needs-interview", "required_action": "prepare-frame-question",
                "interview_template": {"understanding": "", "brief": {
                    "problem": "", "outcome": "", "affected": "", "constraints": "", "open": ""},
                    "questions": []},
                "next_step": "Compare these exact frame results; fill this semantic interview template and "
                    "rerun resume_command with --interview <file> before displaying the native question. "
                    "The runtime supplies route/cycle fields and registers the gate. If the user already "
                    "answered, supply that interview and --answers <file> together; do not ask again."}
    if not isinstance(supplied, dict):
        raise ValueError("frame-interview-invalid: expected an object")
    if supplied.get("route_id", route["route_id"]) != route["route_id"]:
        raise ValueError("frame-interview-route-mismatch")
    if supplied.get("schema", FI.SCHEMA) != FI.SCHEMA:
        raise ValueError("frame-interview-schema-mismatch")
    if resolution["status"] in {"proceed", "revise", "stop"}:
        # A submitted answer remains readable after its cycle is sealed. Do not
        # reopen the producer or write into a completed cycle on a lost reply.
        recorded_path = Path(resolution["artifact"])
        recorded = json.loads(recorded_path.read_text())
        normalized = {**supplied, **{key: recorded[key] for key in
            ("schema", "route_id", "self_path", "summary", "created")},
            "round": supplied.get("round", recorded["round"])}
        recorded_answer = resolution.get("answers")
        if decision == "stop":
            recorded_answer = json.loads((recorded_path.parent / "answers.json").read_text())
        response = json.loads(Path(answers).read_text()) if answers else recorded_answer
        if normalized != recorded or decision != resolution["status"]:
            raise ValueError("frame-input-conflict: preserve the recorded question and decision")
        if response is not None and FI.validate_answers(recorded, response):
            raise ValueError("frame-input-invalid: answers do not match the recorded interview")
        if response != recorded_answer:
            raise ValueError("frame-input-conflict: preserve the recorded answer")
        return {"state": "released" if decision == "proceed" else "cancelled" if decision == "stop" else "needs-revision",
                "decision": decision, "interview_file": str(recorded_path),
                "answers_file": str(recorded_path.parent / "answers.json"),
                "intent_file": str(recorded_path.parent.parent / "intent.md") if decision == "proceed" else None}
    context = prepare_route_artifact_env(Path(path), start=False, jobs=Path(jobs))
    output = context.get("AGENT_ARTIFACT_OUTPUT_DIR")
    if not output:
        raise ValueError("frame-cycle-required: resume the existing work to prepare its exact cycle")
    frame_dir = Path(output) / "shards/frame"
    round_no = supplied.get("round", resolution.get("epoch") or 1)
    if not isinstance(round_no, int) or isinstance(round_no, bool) or not 1 <= round_no <= FI.MAX_ROUNDS:
        raise ValueError("frame-interview-round-invalid")
    directory = frame_dir / f"round-{round_no}"
    if not directory.resolve().is_relative_to(Path(output).resolve()):
        raise ValueError("frame-output-outside-cycle")
    question_path, answer_path = directory / "interview.json", directory / "answers.json"
    original = json.loads(question_path.read_text()) if question_path.exists() else {}
    question = {**supplied, "schema": FI.SCHEMA, "route_id": route["route_id"], "round": round_no,
                "self_path": str(question_path), "summary": str(directory / "frame-summary.json"),
                "created": original.get("created") or datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    errors = FI.validate(question, intensity=route["effective_intensity"])
    response = json.loads(Path(answers).read_text()) if answers else None
    if response is not None:
        errors += FI.validate_answers(question, response)
    if errors:
        raise ValueError("frame-input-invalid: " + "; ".join(errors[:8]))
    if resolution["status"] != "not-raised" and resolution.get("artifact") != str(question_path):
        raise ValueError("frame-interview-binding-conflict: use the currently registered interview")
    _store_once(question_path, question)
    _store_once(directory / "frame-summary.json", {"route_id": route["route_id"], "frames": [
        {"node": node["id"], "marker": str(Path(jobs).parent / "completion" / route["route_id"] / (node["id"] + ".json"))}
        for node in route["nodes"] if node.get("worker_type") == "frame"]})
    if response is not None:
        _store_once(answer_path, response)

    def command(operation, *options):
        argv = [sys.executable, str(Path(runtime_root) / "utilities/workflow-supervisor.py"), operation,
                "--route", str(path), "--jobs", str(jobs), "--gate", "frame-review", *options]
        completed = run(argv, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise ValueError(f"frame-{operation}-pending: {completed.stderr.strip()} {completed.stdout.strip()}")

    if resolution["status"] == "not-raised":
        command("gate", "--block", "--artifact", str(question_path))
        resolution = WS.human_gate_resolution(ledger.journal(), "frame-review")
    if response is None:
        return {"state": "needs-question", "required_action": "ask-registered-question",
                "interview_file": str(question_path), "interview": question,
                "answers_template": FI.answers_template(question),
                "next_step": "Display the native question and preserve the actual response; then rerun "
                    "resume_command with --answers <file>. The runtime renders intent and releases the gate. "
                    "For a revise/stop decision also pass --decision revise|stop."}
    if resolution["status"] in {"proceed", "revise", "stop"}:
        if resolution["status"] != decision or (decision != "stop" and resolution.get("answers") != response):
            raise ValueError("frame-answer-conflict: the recorded decision cannot be replaced")
    elif resolution["status"] != "blocked":
        raise ValueError("frame-raise-pending: the gate was not durably registered")
    intent = frame_dir / "intent.md"
    if decision == "proceed":
        rendered = FI.render_intent(question, response, now=question["created"])
        saved = directory / "intent.md"
        _store_bytes_once(saved, rendered.encode())
        if not intent.exists() or intent.read_text() != rendered:
            WS._atomic_write(intent, rendered)
    if resolution["status"] == "blocked":
        command("release", "--decision", decision, "--answers", str(answer_path))
    current = WS.human_gate_resolution(ledger.journal(), "frame-review")
    if current["status"] != decision or (decision != "stop" and current.get("answers") != response):
        raise ValueError("frame-release-pending: the exact answer was not committed")
    return {"state": "released" if decision == "proceed" else "cancelled" if decision == "stop" else "needs-revision",
            "decision": decision, "interview_file": str(question_path), "answers_file": str(answer_path),
            "intent_file": str(intent) if decision == "proceed" else None}


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


def _wait_expired(result):
    """A bounded fallback owes a handoff, not another automatic wait loop."""
    result.pop("parent_next", None)
    result.pop("parent_next_command", None)
    return {**result, "state": "needs-attention", "reason": "parent-wait-deadline",
            "required_action": "report-pending-work",
            "next_step": "The bounded wait ended while these exact attempts remain unresolved. "
                "Their runtime watchers retain execution and cleanup responsibility. Report the pending "
                "attempts and observation to the user; use resume_command for a requested follow-up. "
                "This deadline neither fails the workers nor authorizes replacement attempts."}


def _outcome(jobs, aid):
    state = current_delivery_state(jobs, aid, parent_attempt_id=aid, advance=False)
    action = delivery_required_action(state)
    result = {"attempt_id": aid, "classification": delivery_classification(state),
            "required_action": action, "marker": state.marker,
            "recovery_command": completion_harvest_command(aid, action, jobs=str(jobs),
                surface=str(ROOT / "adapters/codex/bin/preflight.sh"))}
    if action == "advance-completed":
        row = _rows(jobs).get(aid)
        if row and row[1].get("workflow_completion") == "runtime-v1":
            from dispatch_terminal_commit import completed_owner_handoff
            result["handoff"] = completed_owner_handoff(jobs, *row)
    return result


def _advance(route, path, jobs, result, *, wait=False, interview=None, answers=None,
             decision="proceed", run=subprocess.run):
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
    frames = ([] if existing_owner in rows and not (interview or answers) else
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
            if wait:
                return _wait_expired(result)
            return {**result, "state": "preparing",
                    "required_action": "wait-for-frame-results"}
        result.pop("parent_next", None)
        result.pop("parent_next_command", None)
        result["frame_results"] = [_outcome(jobs, aid) for aid in sorted(attempts)]
        if any(outcome["classification"] != "success" for outcome in result["frame_results"]):
            return {**result, "state": "needs-attention", "reason": "frame-outcome-needs-inspection"}
        entry = next(n for n in route["nodes"] if {f["id"] for f in frames}.issubset(set(n.get("depends_on", []))))
        completion_marker_gate(str(path), entry["id"], "start", ROOT, jobs, _raising_frame_gate=True)
        gate_pending = bool(interview or answers)
        try:
            if not gate_pending:
                owner_frame_launch_gate(SimpleNamespace(route_file=str(path)), "start", ROOT, jobs)
        except DispatchContractError as exc:
            if exc.reason not in {"human-gate-not-raised", "human-gate-unreleased"}:
                raise
            gate_pending = True
        if gate_pending or interview or answers:
            step = frame_interview_step(route, path, jobs, interview=interview, answers=answers, decision=decision)
            result.update(frame_interview=step, gate="frame-review", task=request["text"])
            if step["state"] != "released":
                return {**result, **step}
            owner_frame_launch_gate(SimpleNamespace(route_file=str(path)), "start", ROOT, jobs)
    rows = _rows(jobs)
    aid = _slot(route, "owner", rows)
    if aid not in rows:
        result["launches"].append(_start(route, path, jobs, "owner", request["owner_harness"], run))
        rows = _rows(jobs)
    if aid not in rows:
        return {**result, "state": "needs-attention", "reason": "owner-launch-not-admitted"}
    status, metadata = rows[aid]
    result.update(owner_attempt_id=aid, owner_started=metadata.get("launch_started") == "1")
    if status == "done" and metadata.get("failure_class") == "pass":
        from dispatch_terminal_commit import owner_workflow_gaps
        missing = owner_workflow_gaps(jobs, metadata, route)
        if missing:
            return {**result, "state": "needs-attention", "reason": "workflow-executor-exited",
                    "missing_terminal_gates": missing, "required_action": "report-unfinished-work",
                    "next_step": "The owner exited before the declared stages completed. Preserve its report "
                        "and committed result, and report these missing stages. Waiting or repeating finalization "
                        "cannot execute them. No automatic retry or replacement is authorized by this observation."}
    joined = join_selected_attempts(jobs=jobs, expected_attempts={aid}, timeout=600 if wait else 0, recover=True)
    if joined["state"] == "ready":
        outcome = _outcome(jobs, aid)
        return {**result, "state": "completed" if outcome["classification"] == "success" else "needs-attention",
                "result": outcome}
    if wait:
        return _wait_expired({**result, "observation": joined})
    directive, reason, _ = parent_next(metadata.get("parent_completion_delivery", ""), aid, agent_home=ROOT)
    return {**result, "state": "running", "parent_next": directive, "parent_next_reason": reason,
            "parent_next_command": resume + " --wait" if directive == "bounded-wait" else ""}


def start_work(route, path, jobs, *, wait=False, interview=None, answers=None,
               decision="proceed", run=subprocess.run):
    result = {"route_file": str(Path(path).resolve()), "route_id": route["route_id"],
              "launches": [], "owner_started": False,
              "resume_command": shlex.join([sys.executable, str(ROOT / "utilities/capability-route.py"),
                  "start", "--route", str(Path(path).resolve()), "--jobs", str(Path(jobs).resolve())])}
    try:
        result = _advance(route, path, jobs, result, wait=wait, interview=interview,
                          answers=answers, decision=decision, run=run)
    except (OSError, ValueError) as exc:
        result = {**result, "state": "needs-attention",
                  "reason": getattr(exc, "reason", type(exc).__name__), "detail": str(exc)}
        # A caller can lose the launch reply after the adapter claimed a row.
        # Preserve those durable obligations in this receipt too; do not spawn
        # a replacement or infer completion from the caller's exception.
        try:
            rows = _rows(Path(jobs))
            parent = default_parent_session_id()
            owned = {aid for aid, (status, meta) in rows.items() if status in {"open", "running"}
                     and parent and meta.get("parent_sid") == parent
                     and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")}}
            if owned:
                result["registered_attempts"] = sorted(owned)
                result.update(_wait_fields(owned, rows, result["resume_command"]))
        except (OSError, ValueError) as observation_error:
            result["observation_error"] = str(observation_error)
    if result["state"] == "needs-attention":
        result.setdefault("required_action", "inspect-preparation")
        result["resume_command"] = shlex.join([sys.executable, str(ROOT / "utilities/capability-route.py"),
                                                "start", "--route", str(path), "--jobs", str(jobs)])
        result.setdefault("next_step", "Inspect the exact diagnostic or result recovery_command. Existing workers retain "
            "their runtime watcher and completion delivery. Correct the admission input or resolve the reported "
            "failure, then use resume_command; it does not create a replacement for a failed attempt. "
            "If the correction changes the requested work, ask the user before changing that work.")
    return result
