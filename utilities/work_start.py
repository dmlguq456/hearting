"""Start a sealed request through the existing selector, join and human gate.

This is orchestration, not another outcome or retry policy. Stable attempt ids
reuse the adapter's atomic claim; all completion decisions use the shared join.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

from dispatch_contract import (
    DispatchContractError, completion_marker_gate, owner_frame_launch_gate,
    parse_registry_metadata, verdict_pass,
)
from dispatch_completion_join import (
    join_selected_attempts, current_delivery_state, delivery_classification,
    delivery_required_action, completion_harvest_command,
)
from route_authority import caller_identity as interactive_parent_identity, default_parent_session_id
import route_authority
from codex_managed_dispatch import ManagedDispatchError, probe_managed_codex_parent
from parent_next_directive import correction_command, entrypoint, parent_next, resume_command
from execution_access import ExecutionAccessError, prepare_task_request
import owner_write_advisory as OWNER_WRITE_ADVISORY
import route_plan as RP
import resource_resume as RESOURCE_RESUME

ROOT = Path(__file__).resolve().parents[1]
_ROUTE_MODULE = None
START_WINDOW_SECONDS = 600
# A concurrency-cap refusal (class-cap/global-cap) carries no timestamp
# evidence to size a wait from -- this is the one bounded guess `--wait`
# spends on it, matching the join timeout's own bound.
CAPACITY_PROBE_SECONDS = 60
_REFUSAL_RECEIPT_KEYS = {
    "reason", "child_spawned", "retryable", "refusal", "worker_class",
    "retry_after_seconds", "frees_at",
}


def _route_module():
    """`capability-route.py` as a module, for the catalogue and the first leg's compile.

    Loaded on first use: the compiler imports this module lazily, so the dependency stays
    one-way at import time (the same idiom `dispatch_contract._route_module` uses).
    """
    global _ROUTE_MODULE
    if _ROUTE_MODULE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("capability_route_framed", ROOT / "utilities/capability-route.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ROUTE_MODULE = module
    return _ROUTE_MODULE


def _decision_home(root):
    """Runtime-owned files of framed decisions: the frame prompt, the first leg's prompt and snapshot."""
    return Path(root) / ".runtime" / "framed-decision"


def _keep_first(path, encoded):
    """Write `encoded` unless the file exists; either way return the bytes that are there."""
    from artifact_receipt import _write_once
    if path.is_symlink():
        raise ValueError(f"frame-input-symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_once(path.parent, path, encoded)
    return path.read_bytes()


def frame_task_text(route):
    """What every frame leg of one route receives: the request, the hints, the full part catalogue.

    The catalogue is `stages_block` for every capability -- the exact source `capability-route.py
    stages` prints -- rendered as JSON, never parsed back from CLI text. Both legs get the same
    text and nothing of each other.
    """
    module = _route_module()
    registry = module.TOPO.load_registry()
    blocks = [module.stages_block(registry, recipe) for recipe in registry["recipes"]
              if recipe["capability"] != module.ROUTE_FRAME_CAPABILITY]
    request = route["work_request"]
    lines = [request["text"].rstrip(), "", "## Routing hints (suggestions, never orders)", ""]
    hints = request.get("routing_hints") or {}
    lines += [f"- {key}: {value}" for key, value in sorted(hints.items())] or ["- none"]
    lines += ["", "## Part catalogue", "",
              "Every capability, its modes and its stage parts (`id`, unit and unit choices, inputs and outputs,",
              "`shareable`, `start_approval`, human gates, and the parts another capability may borrow as",
              "`capability:stage`). Assemble section 8 of your brief from this catalogue alone.", "",
              "```json", json.dumps(blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False), "```", "",
              "## Independence", "",
              "You are one of two frame legs working blind to each other: do not read another leg's brief.", ""]
    return "\n".join(lines)


def _frame_prompt_file(route):
    path = _decision_home(route["artifact_root"]) / f"{route['route_id']}.frame-prompt.md"
    _keep_first(path, frame_task_text(route).encode("utf-8"))
    return path


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


def _frame_decision_result(decision, question, question_path, answer_path):
    result = {"state": "released" if decision == "proceed" else "cancelled" if decision == "stop" else "needs-revision",
              "decision": decision, "interview_file": str(question_path),
              "answers_file": str(answer_path) if answer_path.exists() else None,
              "intent_file": str(question_path.parent.parent / "intent.md") if decision == "proceed" else None}
    if decision == "revise":
        from frame_interview import MAX_ROUNDS
        if question["round"] >= MAX_ROUNDS:
            return {**result, "state": "needs-attention", "reason": "frame-revision-round-limit",
                    "required_action": "report-unresolved-frame",
                    "next_step": "The recorded revision remains unresolved at the interview round limit. "
                        "Report the user's feedback and the remaining decision; do not promise an invalid next round or start the owner."}
        result.update(required_action="revise-frame-question", interview_template={
            key: value for key, value in question.items() if key in {"understanding", "brief", "questions"}},
            next_step="Revise this template from the user's feedback and submit it with --interview. "
                "The runtime registers the next round; old questions and answers remain preserved.")
        result["interview_template"]["round"] = question["round"] + 1
    return result


def record_native_answer(route, jobs, asked):
    """Keep the person's native reply to this route's registered frame question.

    When the frame-review gate waits on a registered question with no answer yet and `asked`
    (`frame_interview.answers_from_native`) is a valid answer to it, the reply is written once
    beside the question as `answers.native.json`, and the next `start` takes it as the person's
    answer. Returns that file, or None (no question waiting, or the reply is not its answer)."""
    import frame_interview as FI
    import workflow_state as WS
    resolution = WS.human_gate_resolution(
        WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).journal(), "frame-review")
    question_path = Path(resolution.get("artifact") or "")
    if resolution.get("status") != "blocked" or not question_path.is_file() \
            or (question_path.parent / "answers.json").exists():
        return None
    question = json.loads(question_path.read_text(encoding="utf-8"))
    answers = FI.answers_from_native(question, asked)
    if answers is None or FI.validate_answers(question, answers):
        return None
    target = question_path.parent / FI.NATIVE_ANSWERS_NAME
    target.write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def frame_interview_step(route, path, jobs, *, interview=None, answers=None,
                         decision="proceed", runtime_root=ROOT, run=subprocess.run):
    """Own raise -> actual answers -> intent -> release through existing commands.

    The caller supplies semantic questions/real answers. WorkflowLedger remains
    the only gate authority. runtime_root names the checked execution root;
    an operator can use this orchestration with an already pinned older runtime.
    """
    import frame_interview as FI
    import workflow_state as WS
    from artifact_producer import ProducerError, prepare_route_artifact_env
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    resolution = WS.human_gate_resolution(ledger.journal(), "frame-review")
    supplied = json.loads(Path(interview).read_text()) if interview else None
    if supplied is None and resolution.get("artifact"):
        supplied = json.loads(Path(resolution["artifact"]).read_text())
    if supplied is None:
        if answers:
            raise ValueError("frame-question-required: pass the question already answered with --interview")
        understanding, brief = "", {field: "" for field in FI.BRIEF_FIELDS}
        try:
            # A draft from the request and the anchor frame's brief; the owner rewrites it for the person.
            understanding = FI.understanding_draft((route.get("work_request") or {}).get("text") or "")
            output = prepare_route_artifact_env(Path(path), start=False, jobs=Path(jobs)).get("AGENT_ARTIFACT_OUTPUT_DIR")
            if output:
                brief = FI.brief_draft((Path(output) / "shards/frame/direction-brief.md").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, ProducerError):
            pass
        return {"state": "needs-interview", "required_action": "prepare-frame-question",
                "interview_template": {"understanding": understanding, "brief": brief, "questions": []},
                "question_example": FI.QUESTION_EXAMPLE,
                "next_step": "Compare these exact frame results; fill this semantic interview template (its "
                    "understanding and brief are drafts from the request and the frame brief: rewrite them in the "
                    "person's words) and "
                    "rerun resume_command with --interview <file> before displaying the native question. "
                    "question_example shows one complete question; write yours in the person's language "
                    "(code names in backticks are fine). "
                    "The runtime supplies route/cycle fields and registers the gate. If the user already "
                    "answered, supply that interview and --answers <file> together; do not ask again. "
                    + FI.ANSWERS_SHAPE}
    if not isinstance(supplied, dict):
        raise ValueError("frame-interview-invalid: expected an object")
    if supplied.get("route_id", route["route_id"]) != route["route_id"]:
        raise ValueError("frame-interview-route-mismatch")
    if supplied.get("schema", FI.SCHEMA) != FI.SCHEMA:
        raise ValueError("frame-interview-schema-mismatch")
    next_round = bool(interview and resolution["status"] == "revise"
                      and supplied.get("round") == resolution["epoch"] + 1)
    if not answers and not next_round and decision == "proceed" and resolution["status"] in {"revise", "stop"}:
        decision = resolution["status"]  # A plain resume cannot replace the person's decision.
    if resolution["status"] in {"proceed", "revise", "stop"} and not next_round:
        # A submitted answer remains readable after its cycle is sealed. Do not
        # reopen the producer or write into a completed cycle on a lost reply.
        recorded_path = Path(resolution["artifact"])
        recorded = json.loads(recorded_path.read_text())
        if "route_proposals" in recorded and "route_proposals" not in supplied:
            # The recorded question carries the mapping the runtime built from these marks.
            supplied = {**supplied, "route_proposals": recorded["route_proposals"]}
        normalized = {**supplied, **{key: recorded[key] for key in
            ("schema", "route_id", "self_path", "summary", "created")},
            "round": supplied.get("round", recorded["round"])}
        recorded_answer = resolution.get("answers")
        if decision == "stop":
            saved_answer = recorded_path.parent / "answers.json"
            recorded_answer = json.loads(saved_answer.read_text()) if saved_answer.exists() else None
        response = json.loads(Path(answers).read_text()) if answers else recorded_answer
        if normalized != recorded or decision != resolution["status"]:
            raise ValueError("frame-input-conflict: preserve the recorded question and decision")
        if response is not None and FI.validate_answers(recorded, response):
            raise ValueError("frame-input-invalid: answers do not match the recorded interview")
        if response != recorded_answer:
            raise ValueError("frame-input-conflict: preserve the recorded answer")
        return _frame_decision_result(decision, recorded, recorded_path, recorded_path.parent / "answers.json")
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
    supplied, mark_errors = _marked_route_proposals(route, jobs, supplied)
    question = {**supplied, "schema": FI.SCHEMA, "route_id": route["route_id"], "round": round_no,
                "self_path": str(question_path), "summary": str(directory / "frame-summary.json"),
                "created": original.get("created") or datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    errors = mark_errors + FI.validate(question, intensity=route["effective_intensity"])
    native = directory / FI.NATIVE_ANSWERS_NAME
    if not answers and not next_round and resolution["status"] == "blocked" and native.is_file():
        answers = native                    # the person's native reply, recorded when it arrived
    response = json.loads(Path(answers).read_text()) if answers else None
    # A native timeout/acknowledgement is not an answer file. Keep the same
    # registered question available for a later ordinary conversation reply.
    unanswered = FI.pending_answer_response(question, response)
    if unanswered:
        response = None
    if response is not None:
        errors += FI.validate_answers(question, response)
    if errors:
        raise ValueError("frame-input-invalid: " + "; ".join(errors))
    if response is not None:
        # Check provenance before write-once answers/intent can occupy the
        # question. A refused machine reply must leave room for the real reply.
        registered_worker = os.environ.get("AGENT_DISPATCH_REGISTERED_WORKER") == "1"
        actor_kind = FI.answer_actor_kind(response, registered_worker=registered_worker)
        binding = next((row for row in route.get("human_gate_bindings", [])
                        if row.get("gate") == "frame-review"), {})
        authority = (resolution.get("release_authority") or
                     ("depth-0" if resolution.get("interview") else None) or
                     binding.get("release_authority") or "depth-0")
        if authority == "depth-0" and (registered_worker or not FI.answer_releases_gate(actor_kind, "frame-review")):
            raise ValueError("gate-release-authority-refused: frame-review takes the person's actual answer, "
                             "or a supervisor's answer on the person's behalf")
        response = {**response, "actor_kind": actor_kind}
    if not next_round and resolution["status"] != "not-raised" and resolution.get("artifact") != str(question_path):
        raise ValueError("frame-interview-binding-conflict: use the currently registered interview")
    _store_once(question_path, question)
    _store_once(directory / "frame-summary.json", {"route_id": route["route_id"], "frames": [
        {"node": node["id"], "marker": str(Path(jobs).parent / "completion" / route["route_id"] / (node["id"] + ".json"))}
        for node in route["nodes"] if node.get("worker_type") == "frame"]})
    if response is not None:
        _store_once(answer_path, response)
        _checkpoint("after-answer-save")

    def command(operation, *options):
        argv = [sys.executable, str(Path(runtime_root) / "utilities/workflow-supervisor.py"), operation,
                "--route", str(path), "--jobs", str(jobs), "--gate", "frame-review", *options]
        completed = run(argv, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise ValueError(f"frame-{operation}-pending: {completed.stderr.strip()} {completed.stdout.strip()}")

    ask_now = (resolution["status"] == "not-raised" or next_round) and not answers
    if resolution["status"] == "not-raised" or next_round:
        command("gate", "--block", "--artifact", str(question_path))
        resolution = WS.human_gate_resolution(ledger.journal(), "frame-review")
    if response is None and decision == "proceed":
        return {"state": "needs-question", "required_action": "ask-registered-question" if ask_now else "wait-for-user-answer",
                "interview_file": str(question_path), "interview": question,
                "answers_template": FI.answers_template(question),
                "human_wait": {"state": "pending", "fallback": "ordinary-conversation",
                               "question_block": FI.pending_question_block(question)},
                "parent_next": "end-turn",
                "next_step": ("Present the registered question once. " if ask_now else
                              "Keep waiting for the actual answer. If this question has not yet been presented, "
                              "present human_wait.question_block now; registration alone does not prove presentation. "
                              "Do not automatically open another native question. ") +
                    "Ask the person to confirm or correct the understanding in their language, and present the "
                    "registered question and choices without changing their words. "
                    "Leave human_wait.question_block in the final conversation reply if the native box closes. "
                    "When the native question tool returns the reply, the runtime keeps it beside the question "
                    "(answers.native.json, recorded by the harness hook): then just rerun resume_command. Otherwise "
                    "preserve only the person's actual structured or typed reply in answers_template and set "
                    "actor_kind=user for that reply. Keep supervisor/automatic/unknown sources as such; "
                    "an acknowledgement or template is never a user decision. Then rerun "
                    "resume_command with --answers <file>. The runtime renders intent and releases the gate. "
                    "For a revise/stop decision also pass --decision revise|stop. " + FI.PENDING_ANSWER_RULE}
    if resolution["status"] in {"proceed", "revise", "stop"}:
        if resolution["status"] != decision or (decision != "stop" and resolution.get("answers") != response):
            raise ValueError("frame-answer-conflict: the recorded decision cannot be replaced")
    elif resolution["status"] != "blocked":
        raise ValueError("frame-raise-pending: the gate was not durably registered")
    intent = frame_dir / "intent.md"
    if decision == "proceed":
        rendered = FI.render_intent(
            question, response, now=question["created"],
            **({"approval_scope": _approval_scope(question, response)} if "route_proposals" in question else {}))
        saved = directory / "intent.md"
        _store_bytes_once(saved, rendered.encode())
        _checkpoint("after-intent-render")
        if not intent.exists() or intent.read_text() != rendered:
            WS._atomic_write(intent, rendered)
    if resolution["status"] == "blocked":
        command("release", "--decision", decision, *(["--answers", str(answer_path)] if response is not None else []))
    current = WS.human_gate_resolution(ledger.journal(), "frame-review")
    if current["status"] != decision or (decision != "stop" and current.get("answers") != response):
        raise ValueError("frame-release-pending: the exact answer was not committed")
    return _frame_decision_result(decision, question, question_path, answer_path)


def validate_request(value):
    if (not isinstance(value, dict) or not {"text", "owner_harness"} <= set(value)
            or set(value) - {"text", "owner_harness", "workflow_group_context", "routing_hints"}):
        raise ValueError("work-request-invalid")
    if not isinstance(value["text"], str) or not value["text"].strip():
        raise ValueError("work-request-empty")
    if value["owner_harness"] not in {None, "claude", "codex", "opencode"}:
        raise ValueError("work-request-owner-invalid")
    if "routing_hints" in value:
        # What a framed compose was given for the ordinary shapes: recorded only, never sealed.
        hints = value["routing_hints"]
        if (not isinstance(hints, dict) or not hints
                or set(hints) - {"capability", "capability_mode", "graph", "profile"}
                or any(not isinstance(item, str) or not item.strip() or len(item) > 512
                       for item in hints.values())):
            raise ValueError("work-request-routing-hints-invalid")
    if "workflow_group_context" in value:
        import artifact_identity
        from artifact_workflow_groups import GROUP_ID
        context = value["workflow_group_context"]
        if (not isinstance(context, dict) or set(context) != {"campaign_id", "group_id"}
                or not artifact_identity.is_well_formed(context.get("campaign_id"), "campaign")
                or not isinstance(context.get("group_id"), str)
                or not GROUP_ID.fullmatch(context["group_id"])):
            raise ValueError("work-request-group-context-invalid")
    return value


def continuation_work_request(route, jobs):
    """Read the original request for a legacy continuation, without rewriting it.

    New continuations already inherit work_request. BC's older continuation
    lacked it, so the same normal start could not automatically attach an owner.
    """
    current, seen = route, set()
    while current.get("work_request") is None:
        source_id, source_hash = current.get("source_route_id"), current.get("source_route_hash")
        if not source_id or not source_hash or source_id in seen:
            return validate_request(None)
        seen.add(source_id)
        module = _route_module()
        source_path = module.resolve_route_argument(source_id, jobs)
        current = module.verify_route(json.loads(Path(source_path).read_text()))
        if (current.get("route_id"), current.get("route_hash")) != (source_id, source_hash):
            return validate_request(None)
    return validate_request(current["work_request"])


def group_context_matches_campaign(route, context):
    """Check an explicit context against selection; never infer one from a parent."""
    import artifact_producer as producer
    root = Path(route["artifact_root"])
    campaign = producer.read_campaign(root, context["campaign_id"])
    if campaign is None or route.get("campaign_unassigned"):
        return False
    key, parent_id = route.get("campaign_key"), route.get("parent_cycle_id")
    if key is not None and key != campaign.get("key"):
        return False
    if parent_id:
        parent = producer.read_cycle_record(root, parent_id)
        if parent is None or parent.get("campaign_id") != context["campaign_id"]:
            return False
    return bool(key or parent_id)


def capture_request_context(value, route):
    """Seal only the already explicit same-campaign context for later start."""
    request = dict(validate_request(value))
    if "workflow_group_context" not in request:
        campaign = os.environ.get("AGENT_ARTIFACT_CAMPAIGN_ID")
        group = os.environ.get("AGENT_ARTIFACT_WORKFLOW_GROUP_ID")
        if campaign and group:
            context = {"campaign_id": campaign, "group_id": group}
            if group_context_matches_campaign(route, context):
                request["workflow_group_context"] = context
    return validate_request(request)


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


def _current_parent_session_id():
    """The calling native session owns delivery, never a witnessed sibling."""
    return default_parent_session_id()


def _owns(meta, parent, jobs):
    """The launching session, or its confirmed same-seat successor after a /clear (seat handover)."""
    return route_authority.owns(meta, parent, jobs)


def _slot(route, node, rows, jobs=None):
    matches = [aid for aid, (_, meta) in rows.items()
               if ((node == "owner" and meta.get("worker_type") == "owner"
                    and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")})
                   or (node != "owner" and meta.get("route_id") == route["route_id"]
                       and meta.get("route_node") == node and meta.get("worker_type") == "frame"))]
    aid = matches[-1] if matches else attempt_id(route, node)
    if aid in rows:
        if not matches:
            raise DispatchContractError("work-attempt-identity-conflict", aid)
        status, meta = rows[aid]
        # A live attempt delivers to the session that launched it, so another
        # session cannot adopt it. A finished one only has a result to read:
        # the session a supervisor handed the route to may harvest it.
        if status != "done":
            parent = _current_parent_session_id()
            if not _owns(meta, parent, jobs):
                raise DispatchContractError("work-parent-recovery-required", aid)
        digest = (meta.get("owner_route_hash") or meta.get("route_hash")) if node == "owner" else meta.get("route_hash")
        if digest != route["route_hash"]:
            raise DispatchContractError("work-attempt-identity-conflict", aid)
    return aid


def _start(route, path, jobs, node, harness, run):
    command = [sys.executable, str(ROOT / "utilities/dispatch-owner.py"), "--start",
               "--route-evidence", str(path), "--jobs", str(jobs),
               "--slug", route["slug"] + "-" + node,
               "--attempt-id", attempt_id(route, node)]
    if node == "owner" or not route.get("artifact_root"):
        # A route with no artifact root has no runtime home for a prompt file; every sealed route has one.
        task = (route["work_request"] if "work_request" in route
                else continuation_work_request(route, jobs))["text"]
        if RESOURCE_RESUME.route_selected(route):
            task += RESOURCE_RESUME.verification_prompt(route, jobs)
        command += ["--prompt-text", task]
    else:
        command += ["--prompt-file", str(_frame_prompt_file(route))]
    if node != "owner":
        command += ["--route-node", node]
    if harness:
        command += ["--adapter", harness]
    access_diagnostic = ""
    # Explicit requests keep precedence; lab owners combine that validated
    # input with their inventory run storage through the same request format.
    # Otherwise the request carries what the approved task names (its target
    # table and the roots derived from its text); start/resume share this path.
    lab_owner = node == "owner" and route.get("capability") == "autopilot-lab"
    if lab_owner or not os.environ.get("AGENT_DISPATCH_EXECUTION_ACCESS_FILE"):
        try:
            prepared = prepare_task_request(route, jobs, node=node)
        except ExecutionAccessError as exc:
            access_diagnostic = f"{exc.reason}: {exc.detail}"
            # An explicit target input was recognized but could not be safely
            # prepared. Preserve the existing typed execution-access reason
            # and stop before dispatch-owner can take its default grant path.
            return {
                "attempt_id": attempt_id(route, node),
                "exit_code": 69,
                "receipt": f"check=failed\nreason={exc.reason}\ndetail={exc.detail}\nchild_spawned=0\n",
                "diagnostic": "",
                "execution_access_diagnostic": access_diagnostic,
            }
        if prepared is not None:
            command += ["--execution-access-file", str(prepared)]
    result = run(command, text=True, capture_output=True, check=False)
    receipt = {"attempt_id": attempt_id(route, node), "exit_code": result.returncode,
               "receipt": result.stdout, "diagnostic": result.stderr}
    if access_diagnostic:
        receipt["execution_access_diagnostic"] = access_diagnostic
    return receipt


def _never_started(status, meta):
    """An owner row its launcher closed before spawning: nothing ran and nothing failed."""
    return (status == "done" and meta.get("launch_outcome") == "never-launched"
            and meta.get("launch_claimed") == "0" and meta.get("launch_started") != "1"
            and not meta.get("pid"))


def _launch_failure_reason(launch):
    """The `reason=` of the launcher receipt's last `check=failed` block, or `-`."""
    lines = (launch.get("receipt") or "").splitlines()
    starts = [index for index, line in enumerate(lines) if line == "check=failed"]
    for line in lines[starts[-1] + 1:] if starts else []:
        key, sep, value = line.partition("=")
        if sep and key == "reason":
            return value
    return "-"


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


def _capacity_refusal(launch):
    """A typed, retryable governor admission refusal from one launch's receipt.

    `dispatch-owner.py` inherits the adapter wrapper's stdout without
    capturing it (the wrapper's `check=failed` block flows straight through),
    but also prints its own `check=failed` blocks for failures that never
    reach the wrapper at all (e.g. `wrapper-unavailable`) -- so a receipt can
    carry more than one such block. Only the *last* one reflects this
    launch's actual terminal outcome. Only known keys are read, and only
    their first appearance in that block is kept, so an embedded newline
    inside `detail=`'s own text can never forge a later field; the caller's
    registry check (row absent), not this parse, remains the classification
    authority.
    """
    lines = (launch.get("receipt") or "").splitlines()
    starts = [index for index, line in enumerate(lines) if line == "check=failed"]
    if not starts:
        return None
    fields: dict[str, str] = {}
    for line in lines[starts[-1] + 1:]:
        key, sep, value = line.partition("=")
        if sep and key in _REFUSAL_RECEIPT_KEYS and key not in fields:
            fields[key] = value
    if (fields.get("reason") != "model-worker-governor-denied"
            or fields.get("child_spawned") != "0"
            or fields.get("retryable") != "1"):
        return None

    def optional_int(value):
        if value in (None, "-"):
            return None
        try:
            return int(value)
        except ValueError:
            return None

    return {
        "refusal": fields.get("refusal", "-"),
        "worker_class": fields.get("worker_class", "-"),
        "retry_after_seconds": optional_int(fields.get("retry_after_seconds")),
        "frees_at": optional_int(fields.get("frees_at")),
    }


def _launch_admitted(route, path, jobs, node, harness, run, result, *, wait, sleep, clock):
    """Start one node, retrying a capacity refusal exactly once inside one wait.

    A registered row is the sole authority that a launch was admitted (plan
    §3.0 e); a typed, retryable governor refusal with no row is not a failed
    attempt. `wait` sleeps out one bounded delay and relaunches the same
    attempt id exactly once, re-checking the registry immediately before that
    retry so a capacity-freeing event another resume already used never
    causes a duplicate launch.
    """
    launch = _start(route, path, jobs, node, harness, run)
    result["launches"].append(launch)
    rows = _rows(jobs)
    aid = launch["attempt_id"]
    if aid in rows:
        return rows, None
    refusal = _capacity_refusal(launch)
    if not refusal or not wait or result.get("capacity_waited_seconds", 0) != 0:
        return rows, refusal
    delay = min(START_WINDOW_SECONDS, refusal["retry_after_seconds"] or CAPACITY_PROBE_SECONDS)
    sleep(delay)
    result["capacity_waited_seconds"] = delay
    rows = _rows(jobs)
    if aid in rows:
        return rows, None
    launch = _start(route, path, jobs, node, harness, run)
    result["launches"].append(launch)
    rows = _rows(jobs)
    if aid in rows:
        return rows, None
    return rows, _capacity_refusal(launch) or refusal


def _wait_budget(result):
    """What one `start --wait` call still has of its single start window.

    A replacement re-enters the same call, so its join spends only what the earlier capacity
    sleep and joins left instead of opening a new window (OpenCode r4: one call ran past its
    caller's own timeout and returned nothing).
    """
    return max(0, START_WINDOW_SECONDS - result.get("capacity_waited_seconds", 0)
               - result.get("join_waited_seconds", 0))


def _timed_join(result, clock, *, observe_only=False, **kwargs):
    began = clock()
    joined = join_selected_attempts(**kwargs)
    result["join_waited_seconds"] = round(result.get("join_waited_seconds", 0) + max(0, clock() - began), 1)
    if observe_only and kwargs.get("timeout") == 0 and joined["state"] == "timeout":
        # The shared join calls a zero-time snapshot a timeout. No wait expired
        # in this start receipt; preserve the exact children and diagnostics.
        joined = {**joined, "state": "pending"}
    return joined


def _capacity_wait(result, aid, node, refusal, resume, clock):
    """§3.0(e): a typed, retryable capacity refusal with no admitted row.

    This attempt was never created and never failed -- the registry holds no
    row for it -- so it must never be reported or treated as a failed launch.
    A second refusal after the one bounded wait hands back without arming
    another automatic wait (no infinite retry loop).
    """
    frees_at = refusal.get("frees_at")
    retry_epoch = frees_at if frees_at is not None else (
        clock() + (refusal.get("retry_after_seconds") or CAPACITY_PROBE_SECONDS))
    retry_at = datetime.fromtimestamp(retry_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    already_waited = result.get("capacity_waited_seconds", 0) > 0
    waiting = {
        **result,
        "state": "waiting-capacity",
        "reason": "launch-capacity-wait",
        "refused_attempt_id": aid,
        "refused_node": node,
        "spawned": False,
        "refusal": refusal.get("refusal", "-"),
        "worker_class": refusal.get("worker_class", "-"),
        "retry_after_seconds": refusal.get("retry_after_seconds"),
        "retry_at": retry_at,
        "capacity_waited_seconds": result.get("capacity_waited_seconds", 0),
    }
    waiting.pop("parent_next", None)
    waiting.pop("parent_next_command", None)
    if already_waited:
        waiting.update(required_action="report-capacity-wait",
            next_step="A second capacity refusal followed the one bounded wait for this attempt. "
                "No attempt was created or failed here; report the pending capacity to the user "
                "instead of waiting again or starting a replacement.")
    else:
        waiting.update(required_action="resume-after-capacity",
            parent_next="bounded-wait", parent_next_command=resume + " --wait",
            next_step="No attempt was created or failed: the rolling per-class start budget refused "
                "admission. resume_command --wait waits until retry_at (at most one start window) and "
                "launches the same attempt id once; a second refusal returns without another wait.")
    return waiting


def _capacity_pause(result, attention, resume):
    """A usage-limit stop is a pause, not a failure: nothing was started, one `start` resumes it."""
    waiting = {**result, "state": "waiting-capacity", "reason": "owner-capacity-wait",
               "required_action": "resume-after-capacity", "harness": attention.get("harness", ""),
               "source_attempt_id": attention.get("source_attempt_id", ""),
               "next_step": "The harness is at a usage limit; nothing failed and nothing was started. "
                   "After retry_at run resume_command once from the session that owns the route; "
                   "completed stages are kept."}
    if attention.get("retry_at"):
        waiting["retry_at"] = attention["retry_at"]
    if attention.get("usage_state"):
        waiting["usage_state"] = attention["usage_state"]
    if attention.get("usage_state") == "allocation-usage-gate":
        # The route's balanced allocation, not a usage limit, holds this harness.
        harness, gate = attention.get("harness", ""), attention.get("usage_gate_used_percent")
        when = ("After retry_at, when the last usage window at the gate resets, run resume_command once"
                if attention.get("retry_at") else
                f"No reset time is known: run resume_command once after {harness} usage drops below the {gate}% gate")
        waiting["next_step"] = (f"The route's balanced allocation holds {harness}: its usage is at or above the "
            f"{gate}% gate, not at a usage limit. Nothing failed and nothing was started. {when}, "
            "from the session that owns the route; completed stages are kept.")
    waiting.pop("parent_next", None)
    waiting.pop("parent_next_command", None)
    return waiting


def _outcome(jobs, aid):
    state = current_delivery_state(jobs, aid, parent_attempt_id=aid, advance=False)
    if state.cancellation_requested:
        return {"attempt_id": aid, "classification": "success" if state.cancelled else "pending",
                "required_action": "advance-completed" if state.cancelled else "",
                "cancellation_requested": True, "state": "cancelled" if state.cancelled else "termination-pending",
                "reason": "cancelled-by-parent"}
    action = delivery_required_action(state)
    result = {"attempt_id": aid, "classification": delivery_classification(state),
            "required_action": action, "marker": state.marker,
            "recovery_command": completion_harvest_command(aid, action, jobs=str(jobs),
                surface=str(ROOT / "adapters/codex/bin/preflight.sh"))}
    row = _rows(jobs).get(aid)
    if row and _never_started(*row):
        # No log or result exists to harvest; the way on is to start the same work again.
        route_file = row[1].get("owner_route_file") or row[1].get("route_file")
        if route_file:
            result["recovery_command"] = resume_command(route_file, jobs, agent_home=ROOT)
    if action == "advance-completed":
        if row and row[1].get("workflow_completion") == "runtime-v1":
            from dispatch_terminal_commit import completed_owner_handoff, completed_owner_publication
            result["handoff"] = completed_owner_handoff(jobs, *row)
            publication = completed_owner_publication(jobs, *row)
            if publication is not None:
                result["shared_publication"] = publication
    return result


def _route_cli(jobs, *argv):
    """One `capability-route.py` command under this start's registry; a failure keeps its text."""
    env = {**os.environ, "AGENT_DISPATCH_JOBS": str(jobs)}
    done = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), *argv],
                          text=True, capture_output=True, check=False, env=env)
    if done.returncode:
        raise ValueError(f"framed-{argv[0]}-pending: {done.stderr.strip()} {done.stdout.strip()}")
    return done.stdout


def _framed_cycle(route):
    """The one producer cycle this framed route began, open or sealed."""
    import artifact_producer as producer
    root = Path(route["artifact_root"]).resolve()
    records = [row for row in producer.list_cycle_records(root) if row.get("route_id") == route["route_id"]]
    if len(records) != 1:
        raise ValueError("frame-cycle-required: the framed route has no single producer cycle")
    record = records[0]
    return root, record, producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"


def _framed_facts(root, output, legs=RP.FRAME_PAIR):
    """The brief row of each frame leg and the intent row the decision binds, or None when a file is missing."""
    rows = []
    for node in legs:
        brief = output / "shards" / node / "direction-brief.md"
        if brief.is_symlink() or not brief.is_file():
            return None
        rows.append({"node": node, "path": brief.relative_to(root).as_posix(), "sha256": RP.file_digest(brief)})
    intent = output / "shards/frame/intent.md"
    if intent.is_symlink() or not intent.is_file():
        return None
    return rows, {"path": intent.relative_to(root).as_posix(), "sha256": RP.file_digest(intent)}


# Test-only seam: a callable `hook(name)` a test sets to stop the transaction at a named boundary
# (raise, or exit the process). It is never set by the runtime and there is no flag for it.
FAULT_HOOK = None


def _checkpoint(name):
    return FAULT_HOOK(name) if FAULT_HOOK is not None else None


def _proposal_rows(route, jobs, root, record, output):
    """Each brief's proposal row, every leg validated by a memory compile (nothing written or started)."""
    module = _route_module()
    readiness = {}

    def probe(cwd=None):
        key = cwd or route["cwd"]
        if key not in readiness:
            readiness[key] = module.proposal_readiness({**route, "cwd": key}, jobs)
        return readiness[key]

    def compile_leg(leg, index):
        return module.compile_proposal_leg(leg, index, frame_route=route, frame_cycle_id=record["cycle_id"],
                                           readiness=probe)

    return [RP.evaluate_brief(output / "shards" / node / "direction-brief.md", root=root, node=node,
                              compile_leg=compile_leg, start_approvals=module.route_start_approvals)
            for node in RP.frame_legs(route)]


def _grouped_approvals(row):
    groups = {}
    for item in (row.get("facts") or {}).get("start_approvals", []):
        groups.setdefault((item["leg"], item["start_approval"]), []).append(item["part"])
    return [{"leg": leg, "key": key, "parts": sorted(parts)} for (leg, key), parts in sorted(groups.items())]


def _frame_downgrade_summary(route, jobs):
    """The frame legs that ran one profile lower because the first attempt stopped at a usage limit:
    `[{node, original_profile, actual_profile, cause, attempt_id, original_attempt_id}]`, or None.

    Only a replacement row the one transition reader (`dispatch_replacement.read_profile_transition`)
    verifies is listed; the sealed route still says `top`, and this is where the person sees what
    actually ran."""
    import dispatch_replacement
    found = []
    for aid, (_, meta) in sorted(_rows(jobs).items()):
        if (meta.get("route_id") != route["route_id"] or meta.get("worker_type") != "frame"
                or not meta.get("replacement_original_attempt_id")):
            continue
        try:
            transition = dispatch_replacement.read_profile_transition(
                jobs, route=route, node=meta.get("route_node"), attempt_id=aid)
        except (DispatchContractError, OSError, ValueError):
            continue
        if transition:
            found.append({"node": meta["route_node"], "original_profile": transition["from"],
                          "actual_profile": meta.get("model_profile") or transition["to"],
                          "cause": transition["reason"], "attempt_id": aid,
                          "original_attempt_id": meta["replacement_original_attempt_id"]})
    return found or None


def _start_approval_keys(row):
    """The sorted `(leg, key)` start approvals a validated proposal row carries."""
    return sorted({(item["leg"], item["start_approval"]) for item in row["facts"]["start_approvals"]})


def _interview_questions(rows):
    """The route question and one yes/no question per start approval, built from the validated
    proposals. Ids, kinds and marks are filled; the wording is left to the session that asks the
    person, in their language."""
    import frame_interview as FI
    valid = [row for row in rows if row["proposal"] is not None]
    if not valid:
        return []

    def blank(qid, kind, options):
        return {"id": qid, "topic": "", "question": "", "kind": kind, "options": options,
                "recommended": 0, "why": ""}
    option = {"label": "", "means": ""}
    if len(valid) == 1 or RP.proposals_equal(rows):
        choice = blank(FI.ROUTE_QUESTION_ID, "yes-no", [{**option, FI.PROPOSAL_MARK: valid[0]["node"]}, dict(option)])
    else:
        choice = blank(FI.ROUTE_QUESTION_ID, "choice", [{**option, FI.PROPOSAL_MARK: row["node"]} for row in valid])
    approvals = sorted({pair for row in valid for pair in _start_approval_keys(row)})
    return [choice] + [blank(FI.approval_question_id(leg, key), "yes-no", [{**option, "approves": True}, dict(option)])
                      for leg, key in approvals]


def _marked_route_proposals(route, jobs, interview):
    """`(interview, errors)`: a framed interview's `route_proposals`, built from its `proposal` marks.

    Each marked option selects the validated proposal of the frame brief it names, with one start
    approval row per approval question of that proposal (a later leg's row only when its question is
    asked; an unasked one keeps that leg's gate). An interview that carries `route_proposals` itself,
    carries no marks, or belongs to another route shape is returned unchanged."""
    import frame_interview as FI
    if not RP.is_framed_route(route) or not isinstance(interview, dict) or "route_proposals" in interview:
        return interview, []
    questions = [q for q in interview.get("questions") or [] if isinstance(q, dict)]
    marked = [q for q in questions if any(isinstance(o, dict) and FI.PROPOSAL_MARK in o for o in q.get("options") or [])]
    if not marked:
        return interview, []
    if len(marked) > 1:
        return interview, ["questions: proposal marks belong to one route question"]
    root, record, output = _framed_cycle(route)
    rows = {row["node"]: row for row in _proposal_rows(route, jobs, root, record, output) if row["proposal"] is not None}
    asked = {q.get("id") for q in questions}
    question, by_option, errors = marked[0], {}, []
    for index, option in enumerate(question.get("options") or []):
        if not isinstance(option, dict) or FI.PROPOSAL_MARK not in option:
            continue
        row = rows.get(option[FI.PROPOSAL_MARK])
        if row is None:
            errors.append(f"{question.get('id')}.options[{index}].{FI.PROPOSAL_MARK}: "
                          f"no valid proposal from {option[FI.PROPOSAL_MARK]!r}")
            continue
        approvals = [{"key": key, "leg": leg, "question": FI.approval_question_id(leg, key)}
                     for leg, key in _start_approval_keys(row)
                     if leg == 0 or FI.approval_question_id(leg, key) in asked]
        by_option[str(option.get("label") or "")] = {**row["proposal"], "entry_approvals": approvals}
    if errors:
        return interview, errors
    return {**interview, "route_proposals": {"question": question.get("id"), "by_option": by_option}}, []


def _proposal_review(route, path, jobs, rows=None):
    """Information for the session that writes the interview: both validated proposals (or none with
    the reason), whether they are equal, the start-approval parts each leg would carry, and the
    frame downgrade summary. Nothing here is a decision."""
    if rows is None:
        root, record, output = _framed_cycle(route)
        rows = _proposal_rows(route, jobs, root, record, output)
    return {
        "proposals": [{
            **{key: row[key] for key in ("node", "proposal", "reason", "brief_path", "sha256")},
            "display": RP.none_text(row["reason"]) if row["proposal"] is None else "proposal",
            "legs": (row.get("facts") or {}).get("legs", []),
            "start_approvals": _grouped_approvals(row),
            **({"read_notes": row["read_notes"]} if row.get("read_notes") else {}),
            **({"question_renames": renames} if (renames := RP.question_renames(row.get("source") or "")) else {})}
           for row in rows],
        "equal": RP.proposals_equal(rows),
        "wording_differs": RP.wording_differs(rows),
        "frame_downgrade": _frame_downgrade_summary(route, jobs),
    }


def _approval_scope(question, response):
    """`{(key, leg): [part ids]}` for the approvals of the route the answers selected, from the
    part catalogue alone, so the intent renders the same on every replay."""
    import frame_interview as FI
    choice = FI.route_choice(question, response)
    if not choice or choice["proposal"] is None:
        return {}
    module = _route_module()
    registry = module.TOPO.load_registry()
    scope = {}
    for approval in choice["proposal"].get("entry_approvals") or []:
        leg = choice["proposal"]["legs"][approval["leg"]]
        scope[(approval["key"], approval["leg"])] = sorted(
            {part for key, part in module.declared_start_approvals(leg, registry) if key == approval["key"]})
    return scope


def _recorded_interview(route, jobs):
    """The interview and answers the frame-review gate recorded for this route."""
    import workflow_state as WS
    resolution = WS.human_gate_resolution(
        WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).journal(), "frame-review")
    artifact = resolution.get("artifact")
    if not artifact or not Path(artifact).is_file():
        return None, None
    import frame_interview as FI
    answers = FI.recorded_answer_context(resolution.get("answers"),
                                         resolution.get("actor_kind") if resolution.get("status") == "proceed" else None)
    return json.loads(Path(artifact).read_text()), answers


def _leg_task_text(route, root, output, briefs, intent, approvals, adopted=None):
    """The work request of every leg of an approved route: the original request, the agreed intent,
    the brief paths with their digests (the one whose route the person chose marked), and the
    approvals actually given."""
    lines = [route["work_request"]["text"].rstrip(), "", "## Agreed intent", "",
             (output / "shards/frame/intent.md").read_text(encoding="utf-8").rstrip(), "",
             "## Frame briefs (read-only input)", ""]
    lines += [f"- {root / row['path']} (sha256 {row['sha256']})"
              + (" — adopted direction: the person chose this brief's route" if row["path"] == adopted else "")
              for row in briefs]
    lines += ["", "## Execution scope", "", approvals.get("execution_scope", "complete"),
              "", "## Start approvals given", ""]
    lines += [f"- {row['key']} for leg {row['leg']} ({', '.join(row['parts']) or 'steps named in the question'}): "
              + ("approved" if row["accepted"] else "held for the person at its own gate" if row.get("held_for_person")
                 else "not approved") for row in approvals["given"]] or ["- none"]
    return "\n".join(lines) + "\n"


def _decide(route, jobs, root, record, output, briefs, intent):
    """The decision part for this frame route: the approved first leg, or `selected: none` with the reason."""
    import frame_interview as FI
    module = _route_module()
    frame_route = {"route_id": route["route_id"], "route_hash": route["route_hash"], "cycle_id": record["cycle_id"]}

    def ended(reason, rows=None):
        if rows is None:
            return RP.none_decision(frame_route=frame_route, briefs=briefs, intent=intent, reason=reason,
                                    legs=RP.frame_legs(route))
        return RP.build_decision(frame_route=frame_route, selected=RP.NONE, reason=reason, briefs=briefs,
                                 intent=intent, proposals=rows)

    interview, answers = _recorded_interview(route, jobs)
    if not isinstance(interview, dict) or not isinstance(answers, dict) or "route_proposals" not in interview:
        return ended(RP.NO_PROPOSAL_READ)
    rows = _proposal_rows(route, jobs, root, record, output)
    shown = [{key: row[key] for key in ("node", "proposal", "reason", "brief_path", "sha256", "source", "read_notes")
              if key in row} for row in rows]
    choice = FI.route_choice(interview, answers)
    if choice is None:
        return ended(RP.NO_PROPOSAL_READ)
    if choice["state"] != "selected":
        return ended({"declined": "route-declined", "off-menu": "route-off-menu"}.get(choice["state"], "route-unanswered"), shown)
    valid = [row for row in rows if row["proposal"] is not None]
    match = (next((row for row in valid if RP.same_proposal(row["proposal"], choice["proposal"])), None)
             or next((row for row in valid if RP.same_proposal(row["proposal"], choice["proposal"],
                                                                resolved=row["facts"]["legs"])), None))
    if match is None:
        return ended("proposal-not-verified", shown)
    given = []
    for row in FI.approvals_given(interview, answers, choice["proposal"]):
        parts = sorted({item["part"] for item in match["facts"]["start_approvals"]
                        if item["leg"] == row["leg"] and item["start_approval"] == row["key"]})
        given.append({**row, "parts": parts})
    for item in match["facts"]["start_approvals"]:
        # A part held for the person starts with its leg and keeps its own gate.
        if item["leg"] == 0 and not any((row["accepted"] or row.get("held_for_person")) and row["leg"] == 0
                                        and row["key"] == item["start_approval"] for row in given):
            return ended(f"approval-missing:{item['start_approval']}", shown)
    execution_scope = choice.get("execution_scope") or choice["proposal"].get("execution_scope", "complete")
    if execution_scope not in ("complete", "report"):
        return ended("proposal-not-verified", shown)
    approvals = {"given": given, "execution_scope": execution_scope}
    prompt = _decision_home(root) / f"{route['route_id']}.leg-task.md"
    task = _keep_first(prompt, _leg_task_text(route, root, output, briefs, intent, approvals,
                                              adopted=match.get("brief_path")).encode("utf-8"))
    leg = match["proposal"]["legs"][0]
    projected_leg = module.project_entry_execution_scope(leg, execution_scope)
    execution_graph = list(projected_leg.get("graph") or [])
    source = ((route.get("tracked_gate_evidence") or {}).get("spec_read") or {}).get("source") or "auto"
    record_rel = (output / RP.RECORD_RELATIVE).relative_to(root).as_posix()
    compose = {
        "leg": 0, **{key: leg[key] for key in ("capability", "mode", "shape", "graph", "intensity")},
        **({"cwd": leg["cwd"]} if leg.get("cwd") else {}),
        "route_plan": record_rel + "#0",
        "context": {"cwd": route["cwd"], "artifact_root": str(root), "slug": route.get("slug") or "framed",
                    "campaign_key": route.get("campaign_key"), "parent_cycle": record["cycle_id"],
                    "prompt_file": str(prompt), "prompt_sha256": hashlib.sha256(task).hexdigest(),
                    "spec_read": "auto" if str(source).startswith("compose-auto:") else source,
                    "owner": (route.get("work_request") or {}).get("owner_harness")}}
    if execution_scope == "report" and leg.get("shape") == "staged":
        compose["graph"] = execution_graph
    return RP.build_decision(frame_route=frame_route, selected=choice["label"], reason="", briefs=briefs,
                             intent=intent, proposal=match["proposal"], proposals=shown, approvals=approvals,
                             first_leg_compose=compose)


def _replace_record(record_path, new_record):
    """Monotonic rewrite of the record: the decision part and its digest must be unchanged."""
    current = RP.read_record(record_path)
    if current["decision"] != new_record["decision"] or current["digest"] != new_record["digest"]:
        raise ValueError("route-decision-conflict: the decision part is immutable")
    from workflow_state import _atomic_write
    _atomic_write(record_path, RP.render(new_record).decode("utf-8"))


def _bind(record_path, **first_leg):
    """Add first-leg facts (or confirm the same ones again) and rewrite the record."""
    record = RP.read_record(record_path)
    bound = RP.bind_first_leg(record, first_leg)
    if bound != record:
        _replace_record(record_path, bound)
    return bound


def _first_leg_started(leg_result, jobs, route):
    """The start receipt is complete: a direct leg answered `execute-inline`; a registered owner
    is registered and started (its launch receipt said registered=1 started=1 child_spawned=1)."""
    if route["effective_intensity"] == "direct":
        return leg_result.get("state") == "inline" and leg_result.get("required_action") == "execute-inline"
    aid = leg_result.get("owner_attempt_id")
    if not aid or not leg_result.get("owner_started") or aid not in _rows(jobs):
        return False
    tokens = {token for launch in leg_result.get("launches", []) for token in (launch.get("receipt") or "").split()}
    return not tokens or {"registered=1", "started=1", "child_spawned=1"} <= tokens


def _first_leg(route, path, jobs, result, root, record, output, record_path, decision_record, *,
               wait, run, sleep, clock):
    """Compile, publish and start the approved first leg; bind each fact into the record before the
    next step. Returns `(record, None)` once its start receipt is stored, or `(None, response)` to
    hand the caller the first leg's own state while the frame route stays open."""
    module = _route_module()
    decision = decision_record["decision"]
    compose = decision["first_leg_compose"]
    context = compose["context"]
    first = decision_record.get("first_leg") or {}
    if "snapshot" not in first:
        binding = RP.read_route_plan(f"{record_path}#0", root)
        readiness = {}

        def probe(cwd=None):
            key = cwd or route["cwd"]
            if key not in readiness:
                readiness[key] = module.proposal_readiness({**route, "cwd": key}, jobs)
            return readiness[key]
        leg = {**binding["leg"], **{key: compose[key] for key in ("capability", "mode", "shape", "graph", "intensity")}}
        try:
            leg_route = module.compile_first_leg(
                leg, frame_route=route, frame_cycle_id=record["cycle_id"], context=context, binding=binding,
                work_request={"text": Path(context["prompt_file"]).read_text(encoding="utf-8"),
                              "owner_harness": context.get("owner")}, readiness=probe)
        except ValueError as exc:
            return None, {**result, "state": "needs-attention", "reason": "first-leg-compose-refused",
                          "detail": str(exc), "required_action": "inspect-preparation",
                          "record_file": str(record_path),
                          "next_step": "The decision record is kept. Correct what the compose error names, then run "
                              "resume_command again, or compose the route yourself from the record's proposal."}
        # Key order is part of the route's bytes (the validators read some tables in declared order),
        # so the snapshot keeps it; `route_hash` is order-independent.
        raw = (json.dumps(leg_route, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = _decision_home(root) / f"{route['route_id']}.leg0.{digest[:16]}.route.json"
        _keep_first(snapshot, raw)
        decision_record = _bind(record_path, snapshot={
            "path": snapshot.relative_to(root).as_posix(), "sha256": digest,
            "route_id": leg_route["route_id"], "route_hash": leg_route["route_hash"]})
        first = decision_record["first_leg"]
        _checkpoint("after-compiled-snapshot")
    snap = first["snapshot"]
    snapshot_path = root / snap["path"]
    if RP.file_digest(snapshot_path) != snap["sha256"]:
        raise ValueError("route-decision-conflict: the first leg's snapshot changed")
    leg_path = module.canonical_route_path(str(root), snap["route_id"])
    if not Path(leg_path).exists():
        module.publish_composed_route(json.loads(snapshot_path.read_text(encoding="utf-8")), str(root),
                                      plan=RP.display_plan(decision["proposal"]["legs"]))
    leg_path = Path(leg_path)
    published = json.loads(leg_path.read_text(encoding="utf-8"))
    if (published.get("route_id"), published.get("route_hash")) != (snap["route_id"], snap["route_hash"]):
        raise ValueError("route-decision-conflict: the published first leg differs from its snapshot")
    _checkpoint("after-route-publish")
    if "route" not in first:
        decision_record = _bind(record_path, route={
            "route_id": snap["route_id"], "route_hash": snap["route_hash"],
            "route_file": leg_path.relative_to(root).as_posix()})
        first = decision_record["first_leg"]
    _checkpoint("after-route-bind")
    if "start_receipt" in first:
        return decision_record, None
    leg_route = module.verify_route(published)
    leg_result = start_work(leg_route, leg_path, jobs, wait=wait, run=run, sleep=sleep, clock=clock)
    if not _first_leg_started(leg_result, jobs, leg_route):
        return None, leg_result
    print(module.compose_card(leg_route, RP.display_plan(decision["proposal"]["legs"]),
                              owner_harness=context.get("owner")), file=sys.stderr)
    _checkpoint("after-first-start")
    encoded = json.dumps(leg_result, sort_keys=True, ensure_ascii=False)
    decision_record = _bind(record_path, start_receipt=json.loads(encoded),
                            receipt_digest="sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest())
    _checkpoint("after-first-start-receipt")
    return decision_record, None


def _first_leg_state(root, decision_record, jobs, receipt):
    """The plan's current leg state, for a frame route read again after its first start.

    Once a later leg of the same decision has started, the furthest one answers through its own
    `start` (`route_plan.latest_leg_route`); until then the first leg answers as below.

    The stored `start_receipt` is the launch's own history, so it answers only while the leg is
    still what it describes. A closed leg, or one with an inline finish pending, is answered by
    `start_work` (those branches only read); a registered owner that has exited is answered from
    the registry. Nothing here launches, replaces, settles or closes anything; the one write is the
    existing approval question an exited owner never raised (`_owner_gate_response`).
    """
    first = decision_record["first_leg"]
    bound = first["route"]
    leg_path = Path(_route_module().canonical_route_path(str(root), bound["route_id"]))
    if leg_path != root / bound["route_file"]:
        raise ValueError("route-decision-conflict: the first leg's route file is not where the record bound it")
    published = json.loads(leg_path.read_text(encoding="utf-8"))
    if ((published.get("route_id"), published.get("route_hash")) != (bound["route_id"], bound["route_hash"])
            or _route_module().route_hash(published) != bound["route_hash"]):
        raise ValueError("route-decision-conflict: the published first leg differs from the one the record bound")
    leg_route = _route_module().verify_route(published)
    latest = RP.latest_leg_route(leg_route)
    if latest.get("route_id") != leg_route["route_id"]:
        # A later leg of the same decision has started: answer for the furthest one, as its own
        # `start` would (a closed last leg is completed with no next leg).
        latest_path = Path(_route_module().canonical_route_path(str(root), latest["route_id"]))
        return start_work(_route_module().verify_route(latest), latest_path, jobs)
    from dispatch_notice_state import closed_outcome
    import inline_finish
    pending = inline_finish.pending_state(root, leg_route["route_id"])
    if closed_outcome(leg_path, leg_route) or (pending and pending.get("state") != "finished"):
        return start_work(leg_route, leg_path, jobs)
    aid = receipt.get("owner_attempt_id")
    row = _rows(jobs).get(aid) if aid else None
    if row is None or row[0] != "done":
        return receipt
    resume = resume_command(leg_path, Path(jobs).resolve(), agent_home=ROOT)
    result = {**receipt, "resume_command": resume}
    for key in ("parent_next", "parent_next_reason", "parent_next_command"):
        result.pop(key, None)
    try:
        return (_owner_gate_response(leg_route, leg_path, jobs, aid, row[1], result)
                or _exited_owner_response(leg_route, result, jobs, aid))
    except RuntimeError as exc:         # the registry row could not be proved; say so instead of the stale receipt
        return {**result, "state": "needs-attention", "reason": getattr(exc, "reason", type(exc).__name__),
                "detail": str(exc), "required_action": "inspect-preparation"}


def _framed_settle(route, path, jobs, result, *, closed=None, wait=False, run=subprocess.run,
                   sleep=time.sleep, clock=time.time):
    """Fix the decision, start its first leg, complete the model-less terminal, close the route and
    finalize the cycle.

    Every step reads what is already on disk and does only what is missing, so repeating `start`
    after any interruption finishes the same single decision, route and attempt. A decision that
    selects no proposal ends as `selected: none`: nothing starts and the main session composes next.
    """
    import artifact_producer
    import dispatch_terminal_commit
    producer = artifact_producer
    root, record, output = _framed_cycle(route)
    lock_path = root / ".runtime" / "framed-decision" / (route["route_id"] + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if closed is None:
                # A start that waited on the lock may find the decision already ended.
                from dispatch_notice_state import closed_outcome
                closed = closed_outcome(Path(path), route)
                if closed and closed.get("terminal_gate_proven") is not True:
                    return {**result, "state": "needs-attention", "reason": "route-closed-unproven",
                            "required_action": "inspect-closed-route", "outcome": closed}
            record_path = output / RP.RECORD_RELATIVE
            if record_path.exists():
                decision_record = RP.read_record(record_path)
            elif closed:
                raise ValueError("route-decision-missing: the route closed without its decision record")
            else:
                facts = _framed_facts(root, output, RP.frame_legs(route))
                if facts is None:
                    return {**result, "state": "needs-attention", "reason": "frame-outcome-needs-inspection"}
                briefs, intent = facts
                _checkpoint("after-intent-save")
                decision_record = RP.build_record(_decide(route, jobs, root, record, output, briefs, intent))
                _store_bytes_once(record_path, RP.render(decision_record))
                _checkpoint("after-decision-write")
            decision = decision_record["decision"]
            started = decision["selected"] != RP.NONE
            replayed = started and "start_receipt" in (decision_record.get("first_leg") or {})
            if started:
                decision_record, response = _first_leg(
                    route, path, jobs, result, root, record, output, record_path, decision_record,
                    wait=wait, run=run, sleep=sleep, clock=clock)
                if response is not None:
                    return response
            if closed is None:
                _route_cli(jobs, "complete", "--route", str(path), "--node", RP.TERMINAL_NODE,
                           "--evidence", str(record_path))
                _checkpoint("after-complete")
                _route_cli(jobs, "close", "--route", str(path), "--summary",
                           ("framed route started its first leg " + decision_record["first_leg"]["route"]["route_id"])
                           if started else "framed route ended without a proposal: " + decision["reason"])
                _checkpoint("after-close")
            producer.finalize_exact_cycle(root, cycle_id=record["cycle_id"], expected_binding={
                "kind": "runtime_producer_binding_v1", "campaign_id": record["campaign_id"],
                "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
                "cycle_record_digest": dispatch_terminal_commit.cycle_identity_digest(record)},
                crash_after_manifest=_checkpoint("finalize-crash-after-manifest") is True)
            _checkpoint("after-finalize")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if started:
        receipt = dict(decision_record["first_leg"]["start_receipt"])
        if replayed:
            receipt = dict(_first_leg_state(root, decision_record, jobs, receipt))
        receipt["route_decision"] = {"record_file": str(record_path), "selected": decision["selected"],
                                     "frame_route_id": route["route_id"]}
        return receipt
    intent_file = root / decision["intent"]["path"]
    return {**result, "state": "completed", "reason": "route-decision-none", "required_action": "compose-route",
            "selected": decision["selected"], "decision_reason": decision["reason"],
            "record_file": str(record_path), "intent_file": str(intent_file),
            "brief_files": [str(root / row["path"]) for row in decision["briefs"]],
            "next_step": "No route was proposed, so nothing starts automatically. Read intent_file and "
                "brief_files, decide the next route, and compose it yourself."}


def _owner_report(route, metadata):
    """The file the exited owner's own terminal result names, or None when it names no readable one."""
    try:
        from codex_dispatch_terminal import inspect_terminal_attempt
        terminal = inspect_terminal_attempt(metadata.get("log_file"), worktree=route.get("cwd"),
                                            artifact_root_metadata=route.get("artifact_root"), worker_type="owner")
        if (terminal.get("state") != "valid" or terminal.get("artifact_state") != "readable"
                or not terminal.get("artifact_path_b64")):
            return None
        encoded = str(terminal["artifact_path_b64"])
        return str(Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _raise_owner_entry_gate(route, path, jobs, aid, metadata, result):
    """An owner that exited without ever raising the approval gate sealed on its own operation.

    Its PASS is a proposal; a BLOCKED owner is one that obeyed its prompt line (the gate command was
    refused, so it did not apply). Settlement already refuses to complete the route (the entry gate is
    unreleased); this turns that into the one existing question: raise `preview-disposition` from
    the completed preview, exactly as a refused child start does. The caller is the parent's own
    `start` and returns the receipt, so that receipt is the delivery and no parent kind is left without
    one (`in_parent_receipt`). Raised once -- a gate that is already raised, released or stopped is left
    as it is. When the raise itself is refused, `unraised_gate` records why and the caller reports
    `human-gate-not-raised`. A missing earlier stage keeps its own existing receipt, so it is not
    examined here."""
    if not (metadata.get("workflow_completion") == "runtime-v1"
            and (verdict_pass(metadata) or metadata.get("note") == "dead-worker-blocked")):
        return
    from dispatch_contract import owner_operation_gates, raise_preview_gate_for_node
    from dispatch_terminal_commit import owner_workflow_gaps
    import workflow_state as WS
    gates = owner_operation_gates(route)
    if not gates or owner_workflow_gaps(jobs, metadata, route):
        return
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    for node, gate in gates:
        if WS.human_gate_resolution(ledger.journal(), gate)["status"] != "not-raised":
            continue
        try:
            raise_preview_gate_for_node(path, node["id"], jobs, ROOT, in_parent_receipt=True)
        except (OSError, ValueError, KeyError, StopIteration, DispatchContractError, subprocess.TimeoutExpired) as exc:
            result["unraised_gate"] = {"gate": gate, "node": node["id"], "detail": str(exc)[:320]}


def _owner_gate_response(route, path, jobs, aid, metadata, result):
    """The receipt for an exited owner whose own operation waits on a human gate, or None.

    Asks the existing question when the owner never raised it (`_raise_owner_entry_gate`), then reads
    the park. The one case left at `needs-attention` is a raise the runtime could not make."""
    _raise_owner_entry_gate(route, path, jobs, aid, metadata, result)
    parked_response = _owner_parked_response(result, jobs, aid)
    if parked_response is not None:
        return parked_response
    unraised = result.get("unraised_gate")
    if not unraised:
        return None
    report = _owner_report(route, metadata)
    return {**result, "state": "needs-attention", "reason": "human-gate-not-raised",
            "required_action": "report-unfinished-work", "gate": unraised["gate"],
            **({"owner_report": report} if report else {}),
            "next_step": f"The owner exited on {unraised['node']} without ever raising human gate "
                f"{unraised['gate']}, and the runtime could not raise it from a completed preview "
                f"({unraised['detail']}). Its result is not accepted as approved work: nothing is "
                "settled and no next leg is offered. Tell the person what the owner says it changed "
                "(owner_report) and ask how to proceed; do not release the gate on their behalf."}


def _owner_parked_response(result, jobs, aid):
    """The receipt for an owner that exited at a human gate, or None when it is not parked there
    (or the gate was answered `proceed`, which the continuation path handles)."""
    import dispatch_replacement
    import frame_interview as FI
    parked = dispatch_replacement.owner_parked_gate(jobs, aid)
    if not parked:
        return None
    result["parked_gate"] = parked
    gate = parked["gate"]
    try:
        owner_meta = _rows(jobs)[aid][1]
        report = _owner_report(json.loads(Path(parked["route_file"]).read_text()), owner_meta)
    except (OSError, ValueError, KeyError):
        owner_meta, report = {}, None
    # A passed owner already finished: its proceed lets the runtime settle that work. A blocked owner
    # did not apply: its proceed starts the existing continuation owner.
    after_proceed = ("A proceed settles the owner's finished work automatically."
                     if verdict_pass(owner_meta) else "A proceed starts the continuation automatically.")
    extra = {"owner_report": report} if report else {}
    release = shlex.join([sys.executable, entrypoint(ROOT, "utilities/workflow-supervisor.py"), "release",
                          "--route", parked["route_file"], "--jobs", str(jobs), "--gate", gate,
                          "--decision", "proceed"])
    if parked["status"] == "blocked":
        return {**result, **extra, "state": "waiting-human-gate", "reason": "owner-parked-at-human-gate",
                "required_action": "answer-human-gate", "gate": gate,
                "gate_artifact": parked["artifact"], "release_command": release,
                "next_step": f"The owner paused at human gate {gate} and exited; this is not a failure. "
                    "Show the person the gate artifact and ask for a decision. Record it with "
                    "release_command (replace proceed with revise or stop when chosen). "
                    + after_proceed
                    + ((" owner_report is the owner's own account of what it already changed; show it "
                        "with the preview, because the edit was made before this answer."
                        if verdict_pass(owner_meta) else
                        " owner_report is the owner's own account; show it with the preview.") if report else "")
                    + " " + FI.PENDING_ANSWER_RULE}
    if parked["status"] == "stop":
        return {**result, **extra, "state": "stopped", "reason": "human-gate-stop", "gate": gate,
                "next_step": f"The person stopped this work at gate {gate}. Report that; "
                    "nothing is running and no replacement starts."}
    if parked["status"] == "revise":
        return {**result, **extra, "state": "needs-attention", "reason": "human-gate-revise-owner-parked",
                "required_action": "report-gate-revision", "gate": gate,
                "next_step": f"The person asked for a revision at gate {gate} while no owner is running. "
                    "Report the feedback and ask how to proceed; no automatic continuation starts "
                    "for a revise."}
    return None


def _exited_owner_response(route, result, jobs, aid, **extra):
    """The receipt for an owner whose row is `done`: completed on a success outcome, else the
    settlement still owed. Reads only; it never launches, replaces or settles anything."""
    from dispatch_terminal_commit import inspect_owner_completion
    status, metadata = _rows(jobs)[aid]
    outcome = _outcome(jobs, aid)
    if outcome.get("cancellation_requested"):
        return {"route_id": route["route_id"], "state": outcome["state"],
                "reason": "cancelled-by-parent", "launches": [], "owner_started": False, "result": outcome}
    if outcome["classification"] == "success":
        return _with_next_leg(route, {**result, "state": "completed", "result": outcome})
    return {**result, "state": "needs-attention", "reason": "owner-settlement-pending",
            "required_action": outcome["required_action"], "result": outcome,
            "closure": inspect_owner_completion(jobs, status, metadata), **extra,
            "next_step": "The owner has exited. Preserve its result and inspect the exact closure "
                "obligation; waiting for a model turn or starting a replacement cannot finish it."}


PIN_HANDOFF_SUMMARY = "owner-pin-handoff"


def _pin_handoff_close_command(path, jobs):
    # close already owns cancellation and resource preservation on every harness.
    # It reads the registry from the environment rather than a --jobs option.
    return shlex.join(["env", f"AGENT_DISPATCH_JOBS={Path(jobs).resolve()}", sys.executable,
                       entrypoint(ROOT, "utilities/capability-route.py"), "close", "--route", str(path),
                       "--summary", PIN_HANDOFF_SUMMARY])


def _owner_pin_handoff(route, path, jobs, result, rows=None):
    rows = _rows(jobs) if rows is None else rows
    aid = _slot(route, "owner", rows, jobs)
    if aid not in rows:
        return None
    status, metadata = rows[aid]
    if (status not in {"open", "running"}
            or not (metadata.get("launch_claimed") == "1" or metadata.get("launch_started") == "1")):
        return None
    pinned = route_authority.sealed_pin_harness(route, worker_type="owner")
    moved = route_authority.moved_owner_harness(route, metadata.get("harness"))
    if (not pinned or not metadata.get("harness") or metadata["harness"] == pinned
            or not (moved or metadata.get("replacement_original_attempt_id"))):
        return None
    return {**result, "state": "needs-attention", "reason": PIN_HANDOFF_SUMMARY,
            "owner_attempt_id": aid, "owner_started": metadata.get("launch_started") == "1",
            "harness": metadata["harness"], "requested_harness": pinned,
            "required_action": "close-owner-for-handoff",
            "recovery_command": _pin_handoff_close_command(path, jobs),
            "next_step": "Run recovery_command to settle this owner through the existing close. "
                "Resource runs are preserved. The close receipt then gives the start command "
                "for the unfinished stages on the current pin."}


def pin_handoff_continuation(route, path, jobs, outcome):
    """After the ordinary close settled, prepare its ordinary unfinished suffix.

    No live identity changes and no owner is spawned here. Each receipt carries
    one existing command: close while unsettled, then start the prepared suffix.
    Replays reuse the same source evidence and immutable continuation file.
    """
    if outcome.get("summary") != PIN_HANDOFF_SUMMARY:
        return outcome
    if outcome.get("terminal_gate_proven") is True:
        return outcome  # normal completion won the close race; no unfinished handoff
    if outcome.get("state") != "cancelled":
        return {**outcome, "recovery_command": _pin_handoff_close_command(path, jobs)}
    metadata = (_rows(jobs).get(outcome.get("owner_attempt_id")) or (None, {}))[1]
    if not _owns(metadata, _current_parent_session_id(), jobs):
        return {**outcome, "handoff_reason": "work-parent-recovery-required"}
    pinned = route_authority.sealed_pin_harness(route, worker_type="owner")
    if not pinned:
        return outcome
    import route_parent_close
    import workflow_state as WS
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    with ledger.lock():
        pins = route_authority.selection_pin_rows(route)
        recorded = route_parent_close.settled_result(route, ledger) or outcome
        if recorded.get("successor_route") and recorded.get("handoff_pins") == pins:
            return recorded  # follow the published suffix, even after runtime/source changes
        module = _route_module()
        try:
            for node in route.get("nodes", []):
                try:
                    module._continuation_reused_evidence(route, node)
                except ValueError:
                    boundary = node["id"]
                    break
            else:
                return outcome  # all content settled before close; nothing to replay
            evidence = route_authority.route_in_force(route).get("dispatch_evidence")
            successor = module.build_continuation_route(
                route, resume_from_node=boundary, requested_boundary=boundary,
                reason=f"{PIN_HANDOFF_SUMMARY}:{metadata['attempt_id']}:{pinned}",
                artifact_root=route["artifact_root"],
                dispatch_evidence=evidence if evidence != route.get("dispatch_evidence") else None)
            target = module.canonical_route_path(route["artifact_root"], successor["route_id"])
            module.publish_continuation_route(successor, route, target)
            module.verify_route(successor)
            module._record_route_chain(successor, str(target), "continuation")
            prepared = {**outcome, "successor_route": str(target),
                    "handoff_pins": pins,
                    "recovery_command": resume_command(target, jobs, agent_home=ROOT),
                    "next_step": "The previous owner is settled and resource runs are preserved. "
                        "Run recovery_command to start the unfinished stages on the current pin; "
                        "completed stages remain reused."}
            ledger._append({"at": WS.now_iso(), "route_id": route["route_id"],
                            "route_hash": route["route_hash"], "actor": "parent-close",
                            "evidence": {"parent_close_result": prepared}})
            return prepared
        except (OSError, ValueError) as exc:
            return {**outcome, "handoff_reason": str(exc),
                    "recovery_command": resume_command(path, jobs, agent_home=ROOT),
                    "next_step": "Owner cleanup settled; continuation preparation did not. "
                        "Inspect handoff_reason, then run recovery_command to retry preparation."}


def _advance(route, path, jobs, result, *, wait=False, interview=None, answers=None,
             decision="proceed", run=subprocess.run, sleep=time.sleep, clock=time.time):
    """Advance preparation once; repeating this call creates no duplicate job."""
    request = continuation_work_request(route, jobs)
    path, jobs = Path(path).resolve(), Path(jobs).resolve()
    resume = resume_command(path, jobs, agent_home=ROOT)
    result["resume_command"] = resume
    from dispatch_notice_state import closed_outcome
    import inline_finish
    pending = inline_finish.pending_state(Path(route.get("artifact_root", "")), route["route_id"])
    if pending and pending.get("state") != "finished":
        return {**result, "state":"needs-attention", "reason":"finish-pending",
                "required_action":"resume-inline-finish", "finish_state":pending.get("state")}
    closed = closed_outcome(path, route)
    if closed and closed.get("autoclose"):
        # The runtime closed this route after it sat unused (route_autoclose.py).
        # Nothing to repair: the same work starts again as a new route.
        command = _compose_again(route)
        return {**result, "state": "autoclosed", "reason": "route-closed-automatically",
                "required_action": "compose-again", "outcome": closed, "resume_command": command,
                "parent_next": "compose", "parent_next_command": command,
                "next_step": "This route was closed automatically after it sat unused; run parent_next_command "
                             "to compose the same work again."}
    if closed:
        if closed.get("finish_pending"):
            return {**result, "state": "needs-attention", "reason": "finish-pending",
                    "required_action": "resume-inline-finish", "outcome": closed}
        # Replaying a finished request cannot prepare frames or reopen a cycle.
        # A closure with an explicitly unproven gate is not successful work.
        if closed.get("terminal_gate_proven") is not True:
            return {**result, "state": "needs-attention", "reason": "route-closed-unproven",
                    "required_action": "inspect-closed-route", "outcome": closed}
        if RP.is_framed_route(route):
            return _framed_settle(route, path, jobs, result, closed=closed, wait=wait, run=run,
                                  sleep=sleep, clock=clock)
        owner = closed.get("terminal_owner_attempt_id")
        rows = _rows(jobs)
        if owner and owner not in rows:
            raise ValueError("closed-route-owner-missing")
        publication = None
        delivery = []
        for aid, (status, meta) in rows.items():
            if meta.get("workflow_completion") == "runtime-v1" and (
                    aid == owner or route["route_id"] in {meta.get("route_id"), meta.get("owner_route_id")}):
                # An owner with nothing to settle (`not-applicable`: replaced, stopped at a gate or not
                # passed) does not hold the closed route back; every other unfinished settlement does.
                from dispatch_terminal_commit import owner_completion_state, settle_owner_completion
                completion = owner_completion_state(jobs, status, meta)
                if completion.reason == "shared-spec-publication-pending":
                    # Only the post-seal publication obligation uses this normal
                    # retry. All earlier identity/gate/closure refusals stay put.
                    retry = settle_owner_completion(jobs, status, meta)
                    publication = retry.shared_publication if retry is not None else None
                    completion = owner_completion_state(jobs, status, meta)
                if completion.state not in {"complete", "not-applicable"}:
                    return {**result, "state": "needs-attention", "reason": "workflow-completion-pending",
                            "required_action": "inspect-recovery", "outcome": closed,
                            **({"shared_publication": publication} if publication is not None else {})}
                if completion.state == "complete" and meta.get("parent_completion_reason"):
                    delivery.append({"attempt_id": aid,
                                     "carrier": meta.get("parent_completion_delivery"),
                                     "reason": meta["parent_completion_reason"]})
                if route.get("capability") == "autopilot-spec" and completion.state == "complete" and publication is None:
                    from dispatch_terminal_commit import completed_owner_publication
                    publication = completed_owner_publication(jobs, status, meta)
        if route.get("capability") == "autopilot-spec" and publication is None:
            import artifact_producer
            root_value = route.get("artifact_root")
            if not isinstance(root_value, str) or not root_value.strip():
                # A historical closed route may lack the compiled root. Keep
                # its sealed result and report the publication gap without
                # guessing a root from the caller's cwd or preparing new work.
                publication = {"status": "pending", "reason": "spec-artifact-root-unavailable"}
            else:
                root = Path(root_value).resolve()
                cycle = artifact_producer.route_cycle_for(root, route)
                publication = (artifact_producer.completed_spec_publication(root, cycle_id=cycle["cycle_id"], settle=True)
                               if cycle is not None else {"status": "pending", "reason": "spec-cycle-unavailable"})
            if publication["status"] == "pending":
                return {**result, "state": "needs-attention", "reason": "shared-spec-publication-pending",
                        "required_action": "inspect-recovery", "outcome": closed, "shared_publication": publication,
                        "next_step": "Preserve the completed work and inspect the publication reason. "
                                     "The same normal completion retry keeps the original source/base; no model restarts."}
        return _with_next_leg(route, {**result, "state": "completed", "required_action": "advance-completed",
                                      "outcome": closed,
                                      **({"completion_delivery": delivery} if delivery else {}),
                                      **({"shared_publication": publication} if publication is not None else {})})
    handoff = _owner_pin_handoff(route, path, jobs, result)
    if handoff is not None:
        return handoff
    import dispatch_resource_wait as OWNER_RESOURCE
    watches = (OWNER_RESOURCE.supervisor().recover_resource_watches(route, jobs)
               if any(n.get("kind") == "resource-runner" for n in route.get("nodes", [])) else [])
    if watches:
        return {**result, "state": "resource-watching", "required_action": "end-turn",
                "resource_watches": watches, "parent_next": "end-turn",
                "next_step": "The exact resource retains its watch and declared continuation; end this turn."}
    if RESOURCE_RESUME.route_selected(route):
        resource = RESOURCE_RESUME.observation(route, jobs)
        if resource["state"] != "resource-succeeded":
            from artifact_producer import prepare_route_artifact_env
            supervised = RESOURCE_RESUME.supervisor_alive(resource.get("supervision"))
            artifacts = prepare_route_artifact_env(path, start=True, jobs=jobs)
            output = Path(artifacts["AGENT_ARTIFACT_OUTPUT_DIR"])
            runner_command = shlex.join([sys.executable, entrypoint(ROOT, "utilities/resource-runner.py"),
                "--registry", str(output / "resource-runs.json"), "start", "--run-id", route["route_id"],
                "--cwd", route["cwd"], "--log", str(output / "logs/resume-run.log"),
                "--route", str(path), "--node", "resume-run", "--jobs", str(jobs), "--"])
            if resource["state"] == "resource-running" and not supervised:
                resource = {**resource, "state": "needs-attention", "reason": "resource-watch-unavailable"}
            return {**result, **resource, "required_action": "start-resource" if resource["state"] == "resource-ready"
                    else "wait-for-resource" if supervised else "inspect-resource-continuation",
                    "artifact_env": artifacts, "resource_runner_command": runner_command,
                    "parent_next": "end-turn" if supervised else "inspect-receipt",
                    "parent_next_command": "",
                    "next_step": "Run the already approved payload once through resource-runner start with this "
                        "route and node resume-run. It owns the shared exit watch and post-run verifier. "
                        "No payload rerun is authorized by a missing supervisor or exit proof."}
    if route["effective_intensity"] == "direct":
        from artifact_producer import prepare_route_artifact_env
        return {**result, "state": "inline", "required_action": "execute-inline",
                "task": request["text"],
                # The inline session is the producer (capabilities/*.md lifecycle step 1):
                # an inactive root with legacy content keeps its legacy-compat window.
                "artifact_env": prepare_route_artifact_env(path, start=True, jobs=jobs, require_cycle=False)}
    rows = _rows(jobs)
    existing_owner = _slot(route, "owner", rows, jobs)
    frames = ([] if existing_owner in rows and not (interview or answers) else
              [n for n in route["nodes"] if n.get("worker_type") == "frame" and n.get("dispatch_depth") == 1])
    if frames:
        if RP.is_framed_route(route) and not RP.yaml_available():
            # Only frame/proposal processing needs PyYAML. A registered owner above
            # skips this branch and can still be observed or resumed without it.
            python = shlex.quote(sys.executable)
            return {**result, "state": "needs-attention", "reason": "yaml-unavailable",
                    "required_action": "install-pyyaml",
                    "next_step": f"A framed route reads its frame proposals with PyYAML, which {python} cannot "
                        "import, so this start launched no frame leg and read no proposal. Install it for that "
                        f"Python (e.g. `{python} -m pip install --user pyyaml`) and run resume_command, or "
                        "compose the work again with a non-framed shape (--shape direct, solo or staged)."}
        in_force = route_authority.route_in_force(route)   # with the parent's later pin changes
        candidates = (in_force.get("registered_headless_candidates") or []) if route["effective_intensity"] == "quick" else (in_force.get("dispatch_evidence") or {}).get("tuples", [])
        key = "harness" if route["effective_intensity"] == "quick" else "child_harness"
        harnesses = list(dict.fromkeys(c[key] for c in candidates if c.get("status") == "supported" and c.get(key)))
        if not harnesses:
            return {**result, "state": "needs-attention", "reason": "frame-harness-unavailable"}
        # A tool the caller pinned (`compose --pin frame=H`, else the owner pin,
        # which `--owner H` also seals) is the user's explicit choice, so it is
        # passed on; with no pin the frame keeps the automatic selection. A
        # pinned tool the route's evidence does not support is not forced: the
        # legs fall back to automatic selection and the result says so.
        pins = in_force.get("selection_pins") or {}
        frame_pin = ((pins.get("frame") or pins.get("owner") or {}).get("harness"))
        if frame_pin and frame_pin not in harnesses:
            result["frame_explicit_harness"] = f"unavailable:{frame_pin}"
            frame_pin = None
        attempts = set()
        # Validate every reused identity before starting any missing sibling.
        slots = [_slot(route, node["id"], rows, jobs) for node in frames]
        for node, aid in zip(frames, slots):
            if aid not in rows:
                # Readiness proves runtime support, not remaining usage. Passing
                # a round-robin candidate as --adapter turned an automatic
                # choice into a user override and bypassed the capacity gate.
                # The selector rechecks live usage inside the sealed pool for
                # each frame, just as it does for an automatic owner.
                rows, refusal = _launch_admitted(route, path, jobs, node["id"], frame_pin, run, result,
                                                  wait=wait, sleep=sleep, clock=clock)
                if aid not in rows:
                    result["frame_attempts"] = sorted(attempts)
                    if refusal:
                        return _capacity_wait(result, aid, node["id"], refusal, resume, clock)
                    receipt_lines = result["launches"][-1]["receipt"].splitlines()
                    if "reason=frame-harness-unavailable" in receipt_lines and "child_spawned=0" in receipt_lines:
                        return {**result, "state": "needs-attention", "reason": "frame-harness-unavailable",
                                "frame_attempts": sorted(attempts)}
                    launch_reason = _launch_failure_reason(result["launches"][-1])
                    if launch_reason.startswith("execution-access-"):
                        return {**result, "state": "needs-attention", "reason": launch_reason,
                                "frame_attempts": sorted(attempts)}
                    if (node["id"] == "frame-alternative" and result["launches"][-1]["exit_code"] == 75
                            and "check=deferred" in receipt_lines
                            and "reason=frame-first-attempt-pending" in receipt_lines
                            and "child_spawned=0" in receipt_lines):
                        return {**result, "state": "preparing", "reason": "frame-first-attempt-pending",
                                "required_action": "wait-for-first-frame-attempt",
                                "frame_attempts": sorted(attempts),
                                **_wait_fields(attempts, rows, resume)}
                    return {**result, "state": "needs-attention", "reason": "frame-launch-not-admitted",
                            "frame_attempts": sorted(attempts),
                            **(_wait_fields(attempts, rows, resume) if attempts else {})}
            attempts.add(aid)
            result["frame_attempts"] = sorted(attempts)
            result.update(_wait_fields(attempts, rows, resume))
        joined = _timed_join(
            result, clock, jobs=jobs, expected_attempts=attempts,
            timeout=_wait_budget(result) if wait else 0, recover=True, observe_only=not wait)
        result["observation"] = joined
        from dispatch_replacement import advance_batch
        effective, lineage, attention = advance_batch(jobs, attempts, run=run)
        if lineage and effective != attempts:
            result["replacement_lineage"] = lineage
            return _advance(route, path, jobs, result, wait=wait and _wait_budget(result) > 0, interview=interview,
                            answers=answers, decision=decision, run=run, sleep=sleep, clock=clock)
        if attention:
            result["replacement_attention"] = attention
            if joined["state"] == "ready":
                if attention[0]["reason"] == "replacement-capacity-wait":
                    return _capacity_pause(result, attention[0], resume)
                return {**result, "state": "needs-attention", "reason": attention[0]["reason"],
                        "node": attention[0].get("node", "")}
        if joined["state"] != "ready":
            if wait:
                return _wait_expired(result)
            children = joined.get("children") or []
            pending = [child for child in children if child.get("readiness") == "pending"]
            if (pending and all(child.get("status") in {"done", "killed", "cancelled"}
                                for child in children)
                    and all(child.get("reason") == "process-unverifiable" for child in pending)):
                result.pop("parent_next", None)
                result.pop("parent_next_command", None)
                return {**result, "state": "needs-attention", "reason": "frame-cleanup-unverifiable",
                        "required_action": "report-pending-work",
                        "next_step": "Completed frame results are preserved, but cleanup could not be verified. "
                            "Report the exact pending attempts and observation; existing supervision retains "
                            "cleanup responsibility. This receipt does not promise automatic delivery or "
                            "authorize replacement attempts. Use resume_command for a requested follow-up."}
            return {**result, "state": "preparing",
                    "required_action": "wait-for-frame-results"}
        result.pop("parent_next", None)
        result.pop("parent_next_command", None)
        result["frame_results"] = [_outcome(jobs, aid) for aid in sorted(attempts)]
        downgrade = _frame_downgrade_summary(route, jobs)
        if downgrade:
            result["frame_downgrade"] = downgrade
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
            if RP.is_framed_route(route) and step["state"] == "needs-interview":
                root, record, output = _framed_cycle(route)
                rows = _proposal_rows(route, jobs, root, record, output)
                template = {**(step.get("interview_template") or {}), "questions": _interview_questions(rows)}
                step = {**step, "interview_template": template,
                        "route_proposal_review": _proposal_review(route, path, jobs, rows),
                        "next_step": step.get("next_step", "") + " route_proposal_review holds each brief's validated route "
                            "proposal (or proposal:none(reason)), whether the two are equal, and the start-approval parts "
                            "in scope. interview_template already holds the route question and one yes/no question per "
                            "start approval: keep their ids, kinds and the \"proposal\" and \"approves\" marks, and write "
                            "topic, question, option label and means, and why in the person's language. The runtime maps "
                            "each marked option to its proposal when the interview is submitted; add the questions only "
                            "the person can answer beside them. An answer outside the options selects no route."}
            result.update(frame_interview=step, gate="frame-review", task=request["text"])
            if step["state"] != "released":
                return {**result, **step}
            owner_frame_launch_gate(SimpleNamespace(route_file=str(path)), "start", ROOT, jobs)
    if RP.is_framed_route(route):
        return _framed_settle(route, path, jobs, result, wait=wait, run=run, sleep=sleep, clock=clock)
    rows = _rows(jobs)
    aid = _slot(route, "owner", rows, jobs)
    refusal = None
    launched_now = aid not in rows
    if aid not in rows:
        owner_pin = route_authority.sealed_pin_harness(route, worker_type="owner")
        view = route_authority.route_in_force(route)
        if owner_pin and view.get("registered_headless_candidates") is None and not any(
                row.get("parent_harness") == owner_pin and row.get("status") == "supported"
                and row.get("launch_authority") == "conductor"
                for row in (view.get("dispatch_evidence") or {}).get("tuples", [])):
            # Older composes probed the caller instead of the fixed owner.
            # Keep that route and its approval; ordinary start checks the owner
            # and records the missing evidence with the existing pin writer.
            module = _route_module()
            result["pin_evidence"] = module._change_pins(route, jobs, RP.pin_tokens(
                {"owner": (view.get("selection_pins") or {})["owner"]}))
        rows, refusal = _launch_admitted(route, path, jobs, "owner", owner_pin or request["owner_harness"], run, result,
                                          wait=wait, sleep=sleep, clock=clock)
    if aid not in rows:
        if refusal:
            return _capacity_wait(result, aid, "owner", refusal, resume, clock)
        launch_reason = _launch_failure_reason(result["launches"][-1]) if result["launches"] else "-"
        if launch_reason.startswith("execution-access-"):
            return {**result, "state": "needs-attention", "reason": launch_reason,
                    "launch_reason": launch_reason}
        if result["launches"] and _launch_failure_reason(result["launches"][-1]).partition(":")[0] == "admission-busy":
            # The launcher's preparation timed out on a held admission lock before any row existed.
            return {**result, "state": "needs-attention", "reason": "owner-launch-not-admitted",
                    "required_action": "resume-later", "launch_reason": "admission-busy",
                    "recovery_command": resume,
                    "next_step": "The owner did not start: the artifact admission lock stayed busy. Nothing ran; "
                        "run resume_command again later (after about a minute) and it starts the owner again."}
        return {**result, "state": "needs-attention", "reason": "owner-launch-not-admitted"}
    status, metadata = rows[aid]
    if launched_now and _never_started(status, metadata):
        # One launch per start: the next start launches this same work again, so no wait is armed here.
        reason = _launch_failure_reason(result["launches"][-1])
        return {**result, "state": "needs-attention", "reason": "owner-launch-not-started",
                "required_action": "resume-later", "owner_attempt_id": aid, "launch_reason": reason,
                "recovery_command": resume,
                "next_step": f"The owner did not start ({reason}); nothing ran and nothing failed. "
                    "Run resume_command again later (for a busy admission lock, after about a minute); "
                    "it starts the owner again."}
    result.update(owner_attempt_id=aid, owner_started=metadata.get("launch_started") == "1")
    result["correction_command"] = correction_command(aid, jobs, agent_home=ROOT)
    handoff = _owner_pin_handoff(route, path, jobs, result, rows)
    if handoff is not None:
        return handoff
    if status == "done":
        gate_response = _owner_gate_response(route, path, jobs, aid, metadata, result)
        if gate_response is not None:
            return gate_response
    if status == "done" and verdict_pass(metadata):
        from dispatch_terminal_commit import owner_workflow_gaps
        missing = owner_workflow_gaps(jobs, metadata, route)
        if missing:
            return {**result, "state": "needs-attention", "reason": "workflow-executor-exited",
                    "missing_terminal_gates": missing, "required_action": "report-unfinished-work",
                    "next_step": "The owner exited before the declared stages completed. Preserve its report "
                        "and committed result, and report these missing stages. Waiting or repeating finalization "
                        "cannot execute them. No automatic retry or replacement is authorized by this observation."}
    joined = _timed_join(
        result, clock, jobs=jobs, expected_attempts={aid},
        timeout=_wait_budget(result) if wait else 0, recover=True, observe_only=not wait)
    if status != "done" and _rows(jobs).get(aid, (status,))[0] == "done":
        # The owner exited while this call waited: its receipt asks the same question the next start would.
        gate_response = _owner_gate_response(route, path, jobs, aid, _rows(jobs)[aid][1], result)
        if gate_response is not None:
            return gate_response
    from dispatch_replacement import advance, effective_attempts
    # Only an explicit start resumes an owner that stopped at a usage limit.
    replacement = advance(jobs, aid, run=run, resume_capacity=True)
    if replacement.get("state") in {"running", "reused"}:
        result["replacement_lineage"] = effective_attempts(jobs, {aid})[1]
        current_path = Path(replacement["record"]["route_file"])
        current_route = json.loads(current_path.read_text())
        return _advance(current_route, current_path, jobs, result, wait=wait and _wait_budget(result) > 0,
                        interview=interview, answers=answers, decision=decision, run=run, sleep=sleep,
                        clock=clock)
    status, metadata = _rows(jobs).get(aid, (status, metadata))
    owner_stop = route_authority.answerable_owner_end(status, metadata)
    if (owner_stop
            and not replacement.get("parked_gate")
            and (replacement.get("state") == "not-applicable"
                 or replacement.get("reason") == "automatic-replacement-exhausted")):
        # It stopped for an answer no declared gate carries. The answer, sent to it, is the way on
        # (an answered stop is a pause, so a spent replacement budget does not end it).
        report = _owner_report(route, metadata)
        return {**result, "state": "needs-attention", "reason": "owner-" + ("blocked" if owner_stop == "BLOCKED" else "failed"),
                "required_action": "answer-blocked-owner" if owner_stop == "BLOCKED" else "answer-failed-owner",
                "result": _outcome(jobs, aid),
                **({"owner_report": report} if report else {}),
                "next_step": f"The owner stopped {owner_stop} and is waiting for an answer; owner_report says what it "
                    "needs. Get the person's answer (or fix what it names), then run correction_command with "
                    "--message-file <answer file>: that continues this route at once in a replacement owner that "
                    "receives the answer. Do not close or recompose the route."}
    if replacement.get("state") == "needs-attention":
        result["replacement_attention"] = [replacement]
        if replacement["reason"] == "replacement-capacity-wait":
            return _capacity_pause(result, replacement, resume)
        if joined["state"] == "ready":
            return {**result, "state": "needs-attention", "reason": replacement["reason"],
                    "node": replacement.get("node", "")}
    if joined["state"] == "ready":
        outcome = _outcome(jobs, aid)
        return _with_next_leg(route, {
            **result, "state": "completed" if outcome["classification"] == "success" else "needs-attention",
            "result": outcome})
    status, metadata = _rows(jobs).get(aid, (status, metadata))
    if status == "done":
        return _exited_owner_response(route, result, jobs, aid, observation=joined)
    if metadata.get("pid") and metadata.get("pid_start") and metadata.get("launch_started") == "1":
        from dispatch_contract import attempt_process_quiescence
        process = attempt_process_quiescence(metadata, terminal_receipt=True)
        if process.state != "live":
            return {**result, "state": "needs-attention",
                    "reason": "owner-settlement-pending" if process.state == "quiescent"
                              else "owner-process-unverifiable",
                    "owner_process_state": process.state, "owner_process_reason": process.reason,
                    "observation": joined,
                    "next_step": "The owner is exited or unobservable. Its open row retains settlement; "
                        "the ordinary start or correction retries it. Preserve existing results."}
    if wait:
        return _wait_expired({**result, "observation": joined})
    directive, reason, _ = parent_next(metadata.get("parent_completion_delivery", ""), aid, agent_home=ROOT)
    return {**result, "state": "running", "parent_next": directive, "parent_next_reason": reason,
            "parent_next_command": resume + " --wait" if directive == "bounded-wait" else "",
            "next_step": "You are the parent session; the owner runs as a separate attempt. Do not kill, "
                "replace, or redo its work inline. Follow parent_next: end-turn means yield; "
                "bounded-wait means run parent_next_command once."}


def _with_next_leg(route, result):
    """Attach the route plan's next leg to a `completed` receipt; `start_work` then starts it
    (`_advance_plan`). It is never written into `parent_next` or `required_action`."""
    if result.get("state") != "completed" or route.get("route_plan") is None:
        return result
    leg = RP.next_leg_for_route(route)
    return {**result, "next_leg": leg} if leg else result


def _plan_leg(route, jobs, index):
    """`(route, path)` of leg `index` of the approved plan `route` belongs to: the one already
    recorded in the plan cursor, or compiled now from the previous leg and recorded.

    The plan's own arguments, `--route-plan <record>#<index>` and the finished leg's sealed cycle as
    parent: exactly what the printed `next_leg.compose_command` would seal. One lock per decision
    keeps two starts from compiling the same leg twice."""
    module = _route_module()
    sealed = RP.validate_sealed(route["route_plan"])
    root = Path(route["artifact_root"]).resolve()
    binding = RP.read_route_plan(f"{root / sealed['decision']}#{index}", root)
    if binding["digest"] != sealed["digest"]:
        raise ValueError("route-plan-digest-changed")
    decision = binding["record"]["decision"]
    frame_route_id = decision["frame_route"]["route_id"]
    lock_path = RP.plan_cursor_path(root, frame_route_id)
    if lock_path is None:
        raise ValueError("plan-cursor-unlocated")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path.with_name(lock_path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for row in RP.plan_cursor(root, frame_route_id, binding["digest"]):
            if row["index"] == index:
                leg_path = root / row["route_file"]
                return module.verify_route(json.loads(leg_path.read_text(encoding="utf-8"))), leg_path
        cycle = RP.completed_cycle(route)
        if cycle is None:
            raise ValueError("route-plan-previous-leg-not-sealed")
        context = decision["first_leg_compose"]["context"]
        prompt = Path(context["prompt_file"])
        readiness = {}

        def probe(cwd=None):
            key = cwd or route["cwd"]
            if key not in readiness:
                readiness[key] = module.proposal_readiness({**route, "cwd": key}, jobs)
            return readiness[key]
        leg_route = module.compile_first_leg(
            binding["leg"], frame_route=route, frame_cycle_id=cycle["cycle_id"], context=context,
            binding=binding, index=index, readiness=probe,
            work_request={"text": prompt.read_text(encoding="utf-8"), "owner_harness": context.get("owner")})
        leg_path = Path(module.canonical_route_path(str(root), leg_route["route_id"]))
        if not leg_path.exists():
            module.publish_composed_route(leg_route, str(root), plan=RP.display_plan(decision["proposal"]["legs"]))
        RP.append_plan_cursor(root, frame_route_id, {
            "digest": binding["digest"], "index": index, "route_id": leg_route["route_id"],
            "route_hash": leg_route["route_hash"], "route_file": leg_path.relative_to(root).as_posix(),
            "after_route_id": route["route_id"], "by": {"session": _current_parent_session_id() or None},
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return module.verify_route(json.loads(leg_path.read_text(encoding="utf-8"))), leg_path


def _advance_plan(route, jobs, result, *, run, sleep, clock):
    """A finished leg of an approved plan starts the plan's next leg (the plan cursor).

    The person approved every leg in the one frame interview, so the next leg starts without
    another question; the receipt returned is the new leg's own. A plan approved for a report
    (`execution_scope: report`) stops after its leg with `next_leg` as information, as before."""
    following = result.get("next_leg")
    if result.get("state") != "completed" or not isinstance(following, dict):
        return result
    try:
        sealed = RP.validate_sealed(route["route_plan"])
        root = Path(route["artifact_root"]).resolve()
        record = RP.read_route_plan(f"{root / sealed['decision']}#{sealed['index']}", root)["record"]
        if (record["decision"].get("approvals") or {}).get("execution_scope") == "report":
            return result
        leg_route, leg_path = _plan_leg(route, jobs, following["index"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "plan_advance": {"state": "not-started", "leg": following.get("index"),
                                           "reason": str(exc)[:240]},
                "next_step": "This leg finished. The plan's next leg did not start: " + str(exc)[:160]
                    + ". Run next_leg.compose_command to start it, or report the reason."}
    started = start_work(leg_route, leg_path, jobs, run=run, sleep=sleep, clock=clock)
    return {**started, "plan_advanced": {"leg": following["index"], "after_route_id": route["route_id"],
                                         "route_file": str(leg_path)}}


def _compose_again(route) -> str:
    """The compose command that starts an automatically closed route's work again: the same shape,
    stages, intensity, pins and plan reference the closed route was sealed with."""
    task = Path(route["artifact_root"]) / ".runtime" / "route-autoclose" / f"{route['route_id']}.task.md"
    if not task.is_file():
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(str((route.get("work_request") or {}).get("text") or ""), encoding="utf-8")
    selection = route.get("selection") if isinstance(route.get("selection"), dict) else {}
    shape = selection.get("shape") or ("direct" if route.get("effective_intensity") == "direct" else "staged")
    # A framed compose names no capability: the shape is the route.
    named = [] if shape == "framed" else ["--capability", route["capability"],
                                          "--capability-mode", str(route.get("capability_mode") or "default")]
    argv = [sys.executable, entrypoint(ROOT, "utilities/capability-route.py"), "compose",
            "--slug", str(route.get("slug") or route["route_id"]), *named, "--shape", shape,
            "--cwd", route["cwd"], "--artifact-root", route["artifact_root"],
            "--prompt-file", str(task), "--start"]
    if RESOURCE_RESUME.route_selected(route):
        argv += ["--graph", "resume-run,run-verify"]
    elif shape == "staged" and ((route.get("composed_recipe") or {}).get("compose") or {}).get("graph"):
        argv += ["--graph", ",".join(route["composed_recipe"]["compose"]["graph"])]
    if shape == "staged" and route.get("effective_intensity"):
        argv += ["--intensity", route["effective_intensity"]]
    elif shape == "framed" and len(RP.frame_legs(route)) == 2:
        argv += ["--intensity", "strong"]           # both frame legs again
    for token in RP.pin_tokens(route_authority.route_in_force(route).get("selection_pins")):
        argv += ["--pin", token]
    if route.get("route_plan") is not None:
        sealed = route["route_plan"]
        argv += ["--route-plan", f"{Path(route['artifact_root']).resolve() / sealed['decision']}#{sealed['index']}"]
    if route.get("campaign_key"):
        argv += ["--campaign-key", route["campaign_key"]]
    elif route.get("parent_cycle_id"):
        argv += ["--parent-cycle", route["parent_cycle_id"]]
    else:
        argv += ["--unassigned"]
    return shlex.join(argv)


def start_work(route, path, jobs, *, wait=False, interview=None, answers=None,
               decision="proceed", run=subprocess.run, sleep=time.sleep, clock=time.time):
    import route_parent_close
    closing = route_parent_close.intent(route, jobs)
    if not closing:
        for _, metadata in _rows(jobs).values():
            if (route["route_id"] in {metadata.get("owner_route_id"), metadata.get("route_id")}
                    and route_parent_close.requested(metadata)):
                closing = route_parent_close.intent({
                    "route_id": metadata.get("parent_close_route_id"),
                    "route_hash": metadata.get("parent_close_route_hash")}, jobs)
                if closing:
                    break
    if closing:
        outcome = route_parent_close.continue_close(closing, jobs=jobs)
        outcome = pin_handoff_continuation(route, path, jobs, {**outcome, "summary": closing.get("summary")})
        return {"route_id": route["route_id"], "route_file": str(path), "launches": [],
                "owner_started": False, "reason": route_parent_close.NOTE, **outcome}
    result = {"route_file": str(Path(path).resolve()), "route_id": route["route_id"],
              "launches": [], "owner_started": False,
              "advisories": OWNER_WRITE_ADVISORY.advisories(route),
              "resume_command": resume_command(Path(path).resolve(), Path(jobs).resolve(), agent_home=ROOT)}
    try:
        result = _advance(route, path, jobs, result, wait=wait, interview=interview,
                          answers=answers, decision=decision, run=run, sleep=sleep, clock=clock)
    except (OSError, ValueError) as exc:
        result = {**result, "state": "needs-attention",
                  "reason": getattr(exc, "reason", type(exc).__name__), "detail": str(exc)}
        # A caller can lose the launch reply after the adapter claimed a row.
        # Preserve those durable obligations in this receipt too; do not spawn
        # a replacement or infer completion from the caller's exception.
        try:
            rows = _rows(Path(jobs))
            parent = _current_parent_session_id()
            owned = {aid for aid, (status, meta) in rows.items() if status in {"open", "running"}
                     and _owns(meta, parent, jobs)
                     and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")}}
            if owned:
                result["registered_attempts"] = sorted(owned)
                result.update(_wait_fields(owned, rows, result["resume_command"]))
        except (OSError, ValueError) as observation_error:
            result["observation_error"] = str(observation_error)
    if result["state"] == "needs-attention":
        result.setdefault("required_action", "inspect-preparation")
        result["resume_command"] = resume_command(path, jobs, agent_home=ROOT)
        result.setdefault("next_step", "Inspect the exact diagnostic or result recovery_command. Existing workers retain "
            "their runtime watcher and completion delivery. Correct the admission input or resolve the reported "
            "failure, then use resume_command; a usage-limit stop resumes after the limit resets, and a silent death "
            "is replaced once per logical node. "
            "If the correction changes the requested work, ask the user before changing that work.")
    for launch in result.get("launches", []):
        for advisory in OWNER_WRITE_ADVISORY.receipt_advisories(launch.get("receipt", "")):
            if advisory not in result["advisories"]:
                result["advisories"].append(advisory)
    advanced = _advance_plan(route, jobs, result, run=run, sleep=sleep, clock=clock)
    if advanced is not result:
        return advanced          # the next leg's own start already armed its own resume
    from start_receipt import save
    return save(_arm_capacity_resume(result, path, jobs), jobs)


def _arm_capacity_resume(result, path, jobs):
    """A usage-limit pause with a known reset time, or an owner launch that did not start,
    resumes itself once (audit §4 #17)."""
    import capacity_auto_resume as capacity_resume
    try:
        armed = capacity_resume.arm(result, path, jobs)
    except (OSError, ValueError):
        armed = None
    if not armed:
        return result
    if armed.get("cause") == "launch-not-started":
        return {**result, "auto_resume": armed, "required_action": "wait-for-auto-resume",
                "parent_next": "end-turn", "parent_next_reason": "launch-auto-resume",
                "next_step": f"The owner did not start and nothing ran. The runtime runs resume_command once "
                    f"at {armed['resume_at']} and then leaves this session one notice of the result. "
                    "resume_command stays valid to try earlier."}
    return {**result, "auto_resume": armed, "required_action": "wait-for-auto-resume",
            "parent_next": "end-turn", "parent_next_reason": "capacity-auto-resume",
            "next_step": f"The runtime runs resume_command once at {armed['resume_at']} and then leaves "
                "this session one notice of the result; tell the user the work is paused until then. "
                "Nothing failed. resume_command stays valid if the user wants to resume earlier."}
