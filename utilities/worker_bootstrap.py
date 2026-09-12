#!/usr/bin/env python3
"""Render the portable minimal worker bootstrap and deterministic type overlay."""

from __future__ import annotations

import json
import re
from pathlib import Path

WORKER_TYPES = ("owner", "stage", "review", "support", "frame")
UNIT_REF_RE = re.compile(r"^[a-z-]+/[a-z-]+$")
RESERVED_UNITS = ("_kernel/owner", "_kernel/resource")
ARTIFACT_PRODUCER_CYCLE_ENV = (
    "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
    "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR",
)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
WORKER_KIND_TYPES = {
    "capability-owner": "owner",
    "pipeline-stage": "stage",
    "review-worker": "review",
    "map-worker": "support",
}
REVIEW_MARKERS = (
    "review",
    "reviewer",
    "verify",
    "verifier",
    "audit",
    "adversary",
    "perspective",
    "plan-check",
)
STAGE_NODE_CONTRACT = {
    "plan": "code-plan",
    "planning": "code-plan",
    "execute": "code-execute",
    "implementation": "code-execute",
    "test": "code-test",
    "verification": "code-test",
    "report": "code-report",
    "reporting": "code-report",
}


def profile_worker_type(root: Path, profile: str | None) -> str | None:
    """Read the single scalar needed from a profile without loading its full schema."""
    if not profile:
        return None
    path = root / "profiles" / f"{profile}.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^worker_type:\s*([a-z-]+)\s*$", text, re.MULTILINE)
    return match.group(1) if match and match.group(1) in WORKER_TYPES else None


def resolve_worker_type(
    *,
    explicit: str | None,
    dispatch_depth: int,
    worker_role: str | None = None,
    route_node: str | None = None,
    profile_type: str | None = None,
) -> str:
    """Resolve one bootstrap type.

    Canonical route writers pass ``explicit`` from the topology node kind.
    ``worker_role`` remains a final legacy-reader fallback only; it is not a
    portable session-bootstrap field.
    """
    for candidate in (explicit, profile_type):
        if candidate:
            if candidate not in WORKER_TYPES:
                raise ValueError(f"invalid worker type: {candidate}")
            return candidate
    if dispatch_depth == 1:
        return "owner"
    signal = (route_node or "").lower()
    if any(marker in signal for marker in REVIEW_MARKERS):
        return "review"
    if signal:
        return "stage"
    # Compatibility for pre-worker_type commands and registry fixtures. New
    # writers must not use this branch.
    legacy_signal = (worker_role or "").lower()
    if any(marker in legacy_signal for marker in REVIEW_MARKERS):
        return "review"
    if legacy_signal:
        return "stage"
    return "support"


def worker_type_for_kind(kind: str) -> str:
    """Map portable topology kind to the one worker bootstrap overlay."""
    try:
        return WORKER_KIND_TYPES[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported headless worker kind: {kind}") from exc


def unit_persona_path(root: Path, unit: str | None) -> Path | None:
    """Resolve a route node's unit ref to its catalog persona file.

    Reserved kernel refs (`_kernel/owner`, `_kernel/resource`) carry no catalog
    persona — the owner overlay / detached lifecycle is the contract — and an
    absent unit means a pre-unit route node. Both return None. A malformed or
    dangling catalog ref fails loud: silently dropping an assigned persona
    would dispatch a bare kernel worker.
    """
    if not unit or unit in RESERVED_UNITS:
        return None
    if not UNIT_REF_RE.match(unit):
        raise ValueError(f"invalid unit ref: {unit!r}")
    path = root / "roles" / "units" / f"{unit}.md"
    if not path.is_file():
        raise ValueError(f"unknown unit: {unit} (no roles/units/{unit}.md)")
    return path


def unit_persona_body(root: Path, unit: str | None) -> str | None:
    """Return the unit BODY as plain markdown (frontmatter stripped), or None."""
    path = unit_persona_path(root, unit)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def artifact_cycle_environment(environ) -> dict[str, str]:
    """Carry the issued producer context; fill its deterministic output path."""
    values = {key: environ.get(key, "") for key in ARTIFACT_PRODUCER_CYCLE_ENV}
    if values["AGENT_ARTIFACT_CYCLE_DIR"] and not values["AGENT_ARTIFACT_OUTPUT_DIR"]:
        values["AGENT_ARTIFACT_OUTPUT_DIR"] = str(Path(values["AGENT_ARTIFACT_CYCLE_DIR"]) / "artifacts")
    return values


def artifact_context_prompt(environ) -> str:
    values = artifact_cycle_environment(environ)
    output = values["AGENT_ARTIFACT_OUTPUT_DIR"]
    if not output:
        return ""
    return (f"- artifact_cycle_id: {values['AGENT_ARTIFACT_CYCLE_ID']}\n"
            f"- artifact_output_dir: {output}\n"
            "- Resolve relative artifact paths beneath artifact_output_dir.\n")


def released_task_prompt(args) -> str:
    """Carry the same released task across owner/stage and runtime boundaries.

    This consumes the existing gate journal, not an owner's copied prompt or
    a directory picked by recency. Launch authority remains with the gate.
    Rendering the recorded answers also works when no plan/intent file was
    passed by the conductor. Explicit per-stage assignments remain separate.
    """
    if getattr(args, "worker_type", None) == "frame":
        return ""
    binding = getattr(args, "owner_route_binding", None)
    route_id = getattr(args, "route_id", None) or getattr(binding, "route_id", None)
    if not route_id:
        return ""
    import frame_interview as interview
    import workflow_state as workflow

    jobs = getattr(args, "jobs", None)
    if jobs is not None and not Path(jobs).expanduser().exists():
        return ""  # A fresh registry preview has no recorded release to consume.
    ledger = workflow.WorkflowLedger(route_id, jobs=jobs)
    resolution = workflow.human_gate_resolution(ledger.journal(), "frame-review")
    if resolution["status"] != "proceed" or not resolution.get("interview"):
        return ""  # Frame preparation and legacy routes retain their own inputs.
    artifact = str(resolution.get("artifact") or "")
    try:
        source = Path(artifact)
        if not source.is_absolute():
            raise ValueError("recorded interview path is not absolute")
        value = json.loads(source.read_text(encoding="utf-8"))
        if value.get("route_id") != route_id:
            raise ValueError("recorded interview belongs to a different route")
        answers = resolution.get("answers")
        errors = interview.validate_answers(value, answers)
        if errors:
            raise ValueError("; ".join(errors[:3]))
        value = {**value, "self_path": artifact}
        intent = interview.render_intent(
            value, answers, now=str(resolution.get("resolved_at") or "")[:10],
        )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(
            f"Released task input unavailable for {route_id}: {exc}. "
            f"Restore the recorded interview at {artifact} and its recorded answers; "
            "do not infer a replacement task from Git history."
        ) from exc
    return (
        "Released task context (frame-review):\n"
        "The recorded user scope and decisions below govern this work. Apply the "
        "assigned stage within that scope; role defaults do not expand it. Cite "
        "applicable decision ids in the output.\n\n"
        f"{intent}\n"
    )


def runtime_progress_prompt() -> str:
    return ("The runtime observes tool progress and publishes completion. "
            "No per-tool heartbeat command is required. Complete the assigned work "
            "and return its final artifact and verdict.\n\n")


def assignment_prompt(args, task: str, environ) -> str:
    """Project the route's input/output boundary, rather than ask a caller to copy it.

    A frame consumes the requested work as analysis input. In particular the
    final task's report filename is not that frame's output filename. Route
    validation and the write oracle retain authority over paths.
    """
    if getattr(args, "worker_type", None) != "frame":
        return f"Assignment:\n{task.rstrip()}\n\n"
    outputs = []
    route_file = getattr(args, "route_file", None)
    output_root = artifact_cycle_environment(environ)["AGENT_ARTIFACT_OUTPUT_DIR"]
    if route_file:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        node = next(n for n in route["nodes"] if n["id"] == args.route_node)
        outputs = [str(Path(output_root) / path) if output_root else path for path in node.get("outputs", [])]
    return (
        "User goal to analyze (the later owner's task):\n"
        f"{task.rstrip()}\n\n"
        "Current assignment: produce your frame direction brief, with options and a direction verdict. "
        "Keep the analysis within the user's scope. Output filenames in the user goal describe the later "
        "task; this frame produces only its own declared brief.\n"
        + ("This frame's declared output: " + json.dumps(outputs, ensure_ascii=False) + "\n" if outputs else "")
        + "\n"
    )


def supervised_owner_prompt() -> str:
    return (
        "Runtime-owned completion join: launch the current batch through its checked dispatch surface. "
        "A start receipt proves launch with registered=1, started=1, child_spawned=1. "
        "Yield with `runtime_wait: registered-children`; the runtime waits and resumes this owner "
        "with an exact receipt. It also acknowledges delivery and retains unresolved cleanup. "
        "Use the result to continue authorized work within the existing gates. "
        "Inspection commands are available when needed; they are not a delivery acknowledgement.\n\n"
    )


def render_worker_bootstrap(root: Path, worker_type: str, unit: str | None = None) -> str:
    """Return exactly one canonical kernel plus one type fragment.

    When the assigned route node carries a catalog ``unit``, the unit BODY from
    ``roles/units/<unit>.md`` is appended as the worker persona (kernel +
    worker-type overlay + unit body); kernel and overlay mechanics are unchanged.
    """
    if worker_type not in WORKER_TYPES:
        raise ValueError(f"invalid worker type: {worker_type}")
    if worker_type == "frame" and not unit:
        unit = "plan/frame"
    paths = (
        root / "roles" / "worker-bootstrap.md",
        root / "roles" / "worker-types" / f"{worker_type}.md",
    )
    fragments = [path.read_text(encoding="utf-8").strip() for path in paths]
    persona = unit_persona_body(root, unit)
    if persona:
        fragments.append(persona)
    return "\n\n".join(fragments) + "\n"


def assigned_contract(
    *,
    capability: str,
    worker_type: str,
    route_node: str | None,
    completion_gate: str | None = None,
    explicit: str | None = None,
    root: Path | None = None,
    unit: str | None = None,
) -> str:
    """Resolve the assigned portable contract without consulting worker role.

    A completion gate names the stage contract when that contract exists in
    the portable catalog. Otherwise the entry capability remains the readable
    contract and the immutable route node supplies the narrower assignment.
    """
    if worker_type == "frame":
        return unit or "plan/frame"  # The injected unit is the contract, not the owner recipe.
    if explicit:
        return explicit
    if worker_type in {"stage", "review", "support"}:
        if completion_gate and root and (root / "capabilities" / f"{completion_gate}.md").is_file():
            return completion_gate
        if route_node and route_node.lower() in STAGE_NODE_CONTRACT:
            return STAGE_NODE_CONTRACT[route_node.lower()]
    return capability


def contract_read_prompt(args, harness: str) -> str:
    """One contract-loading instruction; frame units are already injected."""
    if args.worker_type == "frame":
        return ("- Your frame unit contract is already included above. Read its named inputs within "
                "the requested scope; no owner capability Skill or full harness bootstrap is needed.\n")
    if harness == "codex":
        return (f"- Read only $AGENT_HOME/adapters/codex/skills/{args.assigned_contract}/SKILL.md; "
                "the typed bootstrap above already contains the exact portable unit persona.\n")
    if harness == "claude":
        return (f"- Read only the exposed {args.assigned_contract} Skill, named artifacts, and selected specialization. "
                "General Claude custom subagents may still inherit project CLAUDE.md; do not manually load a full harness bootstrap.\n")
    return (f"- Read only the assigned {args.assigned_contract} Skill/mode and named artifact inputs. "
            "Project instruction auto-load is not treated as physically masked; do not manually load a full harness bootstrap.\n")


def handoff_template() -> str:
    return (
        "artifact: <canonical path | ->\n"
        "verdict: PASS | FAIL | BLOCKED\n"
        "blocker: none | <one line>"
    )
