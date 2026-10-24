#!/usr/bin/env python3
"""Render the portable minimal worker bootstrap and deterministic type overlay."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

WORKER_TYPES = ("owner", "stage", "review", "support", "frame")
UNIT_REF_RE = re.compile(r"^[a-z-]+/[a-z-]+$")
RESERVED_UNITS = ("_kernel/owner", "_kernel/resource")
ARTIFACT_PRODUCER_CYCLE_ENV = (
    "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
    "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR", "AGENT_RUNTIME_DIR",
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

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Reader-facing report gates whose format is specified outside their stage contract.
REPORT_CONTRACT_REFERENCES = {
    "lab-report": ("skills/autopilot-lab/references/eval-procedure.md",
                   "skills/autopilot-lab/references/data-contract.md"),
    "research-report": ("skills/autopilot-research/references/report-generation.md",),
    "draft-finalize": ("skills/autopilot-draft/references/pipeline-steps.md",),
    "audit-report": ("skills/audit/references/report-and-autofix.md",),
}

# Vocabulary a unit may declare under its optional `bootstrap:` frontmatter block.
BOOTSTRAP_MEMORY_TOPICS = {
    "report-format": "report 보고서 양식 format template layout style 문체 html 산출물 deliverable "
                     "figure 그림 시각화 table 표",
}
BOOTSTRAP_EXEMPLAR_KINDS = ("deliverable",)
BOOTSTRAP_STAGE_TYPES = ("stage", "review", "support")

# gate -> (HTML patterns, MD patterns) under <cycle>/artifacts, fixed depth, earlier wins.
DELIVERABLE_EXEMPLARS = {
    "lab-report": (
        ("report/index.html", "experiments/*/report/index.html", "experiments/*/report/report.html",
         "experiments/*/report.html", "experiments/*/html_report/index.html"),
        ("report/REPORT.md", "experiments/*/report/REPORT.md", "experiments/*/REPORT.md"),
    ),
    "code-report": ((), ("final_report.md", "plans/*/final_report.md", "plans/final_report.md")),
    "research-report": (("report/*.html", "research/*/report/*.html"),
                        ("report/*.md", "research/*/report/*.md")),
    "design-handoff": (("designs/*/05_handoff/*.html",), ("designs/*/05_handoff/*.md",)),
    "draft-finalize": (("documents/*/*.html",), ("documents/*/*.md",)),
    "audit-report": ((), ("reviews/audit-report.md",)),
}
_EXEMPLAR_MIN_BYTES = 1024
_MEMORY_BLOCK_MAX_BYTES = 6144


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


_BOOTSTRAP_BLOCK_RE = re.compile(r"^bootstrap:[ \t]*(?:#.*)?\n((?:[ \t]+\S.*\n?)+)", re.MULTILINE)


def _declaration_value(raw: str):
    raw = re.sub(r"\s+#.*$", "", raw.strip()).strip()
    if raw.startswith("[") and raw.endswith("]"):
        return tuple(v.strip().strip("\"'") for v in raw[1:-1].split(",") if v.strip())
    return raw.strip("\"'")


@functools.lru_cache(maxsize=64)
def _parse_bootstrap_declaration(path: str, mtime_ns: int) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    front = _FRONTMATTER_RE.match(text)
    block = _BOOTSTRAP_BLOCK_RE.search(front.group(0)) if front else None
    if not block:
        return {}
    found: dict = {}
    for line in block.group(1).splitlines():
        key, sep, value = line.strip().partition(":")
        if not sep or key.startswith("#"):
            continue
        parsed = _declaration_value(value)
        if key in ("memory", "gates"):
            parsed = parsed if isinstance(parsed, tuple) else ((parsed,) if parsed else ())
        elif key == "exemplar":
            parsed = parsed if isinstance(parsed, str) else ""
        else:
            continue
        if parsed:
            found[key] = parsed
    return found


def unit_bootstrap_declaration(root: Path, unit: str | None) -> dict:
    """Read a unit's optional flat ``bootstrap:`` frontmatter block; any failure is ``{}``.

    Only stdlib regex is used (no YAML on the dispatch hot path). Keys:
    ``memory`` (tuple), ``exemplar`` (str), ``gates`` (tuple); absent keys are omitted.
    """
    try:
        path = unit_persona_path(root, unit)
        if path is None:
            return {}
        return dict(_parse_bootstrap_declaration(str(path), path.stat().st_mtime_ns))
    except Exception:
        return {}


def artifact_cycle_environment(environ) -> dict[str, str]:
    """Carry the issued producer context; fill its deterministic output path."""
    values = {key: environ.get(key, "") for key in ARTIFACT_PRODUCER_CYCLE_ENV}
    if values["AGENT_ARTIFACT_CYCLE_DIR"] and not values["AGENT_ARTIFACT_OUTPUT_DIR"]:
        values["AGENT_ARTIFACT_OUTPUT_DIR"] = str(Path(values["AGENT_ARTIFACT_CYCLE_DIR"]) / "artifacts")
    return values


def runtime_dir_candidate(artifact_root, environ=None) -> str:
    """Purely resolve an optional project runtime path; never create it here."""
    import os
    values = os.environ if environ is None else environ
    try:
        root = Path(artifact_root).expanduser().resolve(strict=False)
        raw = values.get("AGENT_RUNTIME_DIR", "")
        if raw:
            supplied = Path(raw).expanduser()
            if not supplied.is_absolute():
                return ""
            candidate = supplied.resolve(strict=False)
            default = (root.parent / ".agent_runtime").resolve(strict=False)
            # Nested dispatch inherits this generated default as an override.
            # Preserve exactly that path while keeping user overrides fenced.
            if candidate == default:
                return str(candidate)
            home = Path.home().resolve(strict=False)
            project = root.parent
            if (candidate == Path("/") or candidate == home or candidate == project
                    or candidate.is_relative_to(root) or candidate.is_relative_to(home)
                    or candidate.is_relative_to(project)):
                return ""
            if "releases" in candidate.parts or ".harness" in candidate.parts:
                return ""
            return str(candidate)
        return str(root.parent / ".agent_runtime")
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def prepare_runtime_dir(candidate: str) -> str:
    """Create only the optional runtime root and its private ignore marker."""
    if not candidate:
        return ""
    try:
        path = Path(candidate).resolve(strict=False)
        if not path.is_absolute() or path == Path("/") or path == Path.home().resolve(strict=False):
            return ""
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return ""
        marker = path / ".gitignore"
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != b"*\n":
                return ""
        else:
            marker.write_bytes(b"*\n")
        return str(path)
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def artifact_context_prompt(environ) -> str:
    values = artifact_cycle_environment(environ)
    output = values["AGENT_ARTIFACT_OUTPUT_DIR"]
    if not output:
        return ""
    runtime = values.get("AGENT_RUNTIME_DIR", "")
    runtime_line = (f"- runtime_dir: {runtime}\n"
                    "- Put environments, caches, external checkouts, and checkpoints under runtime_dir; keep reports and provenance in artifacts.\n") if runtime else ""
    return (f"- artifact_cycle_id: {values['AGENT_ARTIFACT_CYCLE_ID']}\n"
            f"- artifact_output_dir: {output}\n"
            f"{runtime_line}"
            "- Resolve relative artifact paths beneath artifact_output_dir.\n")


class NodeScope(NamedTuple):
    """A route node's declared scope, resolved to one absolute directory (or
    stated as unresolved, never guessed).

    A plain dataclass here breaks under `importlib.util.spec_from_file_location`
    + `exec_module` loading that never registers this module in `sys.modules`:
    `dataclass()` resolves string annotations via `sys.modules[cls.__module__]`
    and crashes with AttributeError when that lookup is None
    (tools/capability_topology.test.py loads this file that way). NamedTuple
    needs no such lookup and stays immutable and attribute-accessed the same way.
    """
    output_dir: str | None
    worktree_dir: str | None
    outputs: tuple[str, ...]
    write_scope: tuple[str, ...]
    source: str  # "env" | "producer-binding" | "unbound"


def resolve_node_scope(
    route, node_id: str | None, environ, *, parent_attempt_id: str | None = None,
    route_file: str | Path | None = None, source_worktree: str | Path | None = None,
    source_worktree_verified: bool = False,
) -> NodeScope:
    """One resolver for "what is this node's write scope, as an absolute path".

    Node ranges are declared cycle-relative (`capabilities/autopilot-spec.md`,
    `hooks/artifact-guard.sh`). A route-bound worker's own cycle environment
    names the open cycle directly; a worker launched without that environment
    (observed 2026-09-08: a depth-2 spec worker with only `artifact_root` in
    its prompt) falls back to the owning attempt's read-only producer binding.
    Neither source available means the caller must say so, not guess a
    root-relative path that the write oracle then refuses.
    """
    # Sealed routes go through the shared resolver. Keep the small legacy
    # projection below for pre-route callers and minimal import fixtures only.
    if route.get("route_id") and route.get("route_hash") and route.get("artifact_root"):
        import artifact_context
        context = artifact_context.resolve_context(
            environ=environ, route=route, node_id=node_id,
            parent_attempt_id=parent_attempt_id,
            route_file=route_file,
            explicit=({"source_worktree": source_worktree,
                       "source_worktree_verified": source_worktree_verified}
                      if source_worktree is not None else None),
        )
        node = context.node
        outputs = tuple((node or {}).get("outputs", []))
        write_scope = tuple((node or {}).get("write_scope", []))
        if (route.get("capability") == "autopilot-code"
                and route.get("effective_intensity") == "quick" and node_id == "one-shot"
                and (node or {}).get("kind") == "capability-owner"
                and (node or {}).get("unit") == "_kernel/owner"
                and set(write_scope) == {"source-scoped", "plans/<cycle>/**"}):
            write_scope = ("source-scoped", "**")
        has_matching_env = bool(context.cycle_record and environ.get("AGENT_ARTIFACT_OUTPUT_DIR")) and all(
            not environ.get(key) or environ.get(key) == expected
            for key, expected in (
                ("AGENT_ARTIFACT_CYCLE_ID", context.cycle_record.get("cycle_id")),
                ("AGENT_ARTIFACT_CAMPAIGN_ID", context.cycle_record.get("campaign_id")),
                ("AGENT_ARTIFACT_PRODUCER_ID", context.cycle_record.get("producer_id")),
            )
        ) and not context.inherited_cycle_mismatch
        source = ("env" if has_matching_env else
                  "producer-binding" if context.output_dir else "unbound")
        return NodeScope(
            str(context.output_dir) if context.output_dir else None,
            str(context.source_worktree) if context.source_worktree else None,
            outputs, write_scope, source,
        )

    node = None
    if node_id:
        node = next((n for n in route.get("nodes", []) if n.get("id") == node_id), None)
    outputs = tuple((node or {}).get("outputs", []))
    write_scope = tuple((node or {}).get("write_scope", []))
    if (route.get("capability") == "autopilot-code"
            and route.get("effective_intensity") == "quick"
            and node_id == "one-shot" and (node or {}).get("kind") == "capability-owner"
            and (node or {}).get("unit") == "_kernel/owner"
            and set(write_scope) == {"source-scoped", "plans/<cycle>/**"}):
        # The one-shot owns the approved deliverables in this cycle; plans is
        # its reporting default. Retain the sealed route's bytes and identity.
        write_scope = ("source-scoped", "**")
    env_output_dir = artifact_cycle_environment(environ)["AGENT_ARTIFACT_OUTPUT_DIR"]
    if env_output_dir:
        return NodeScope(env_output_dir, str(Path(route["cwd"]).resolve()) if route.get("cwd") else None,
                         outputs, write_scope, "env")
    artifact_root = route.get("artifact_root")
    route_id = route.get("route_id")
    if parent_attempt_id and artifact_root and route_id:
        import artifact_producer
        import dispatch_terminal_commit
        try:
            binding = dispatch_terminal_commit.load_producer_binding(
                artifact_root=Path(artifact_root), route_id=route_id,
                owner_attempt_id=parent_attempt_id,
            )
            record = binding.binding or {}
            campaign_id, cycle_id = record.get("campaign_id"), record.get("cycle_id")
            if campaign_id and cycle_id:
                cdir = artifact_producer.cycle_dir(Path(artifact_root), campaign_id, cycle_id)
                return NodeScope(str(cdir / "artifacts"),
                                 str(Path(route["cwd"]).resolve()) if route.get("cwd") else None,
                                 outputs, write_scope, "producer-binding")
        except (dispatch_terminal_commit.TerminalCommitError, artifact_producer.ProducerError):
            pass
    return NodeScope(None, str(Path(route["cwd"]).resolve()) if route.get("cwd") else None,
                     outputs, write_scope, "unbound")


def _resolved_paths(output_dir: str, paths) -> list[str]:
    import artifact_context
    return artifact_context.render_outputs(output_dir, paths)


def route_node_commit_expected(route, node_id: str | None, worker_type: str,
                              *, subsession_id: str | None = None,
                              stage_authority: int = 1) -> bool:
    """Read the sealed route's commit policy without inferring it from depth."""
    from route_authority import subsession_launch
    if worker_type != "stage" or subsession_launch(subsession_id, stage_authority) or not node_id:
        return False
    node = next((n for n in route.get("nodes", []) if n.get("id") == node_id), None)
    return bool(node and node.get("commit_expected") is True)


def stage_commit_enabled(args) -> bool:
    """Normalize the shared commit policy before adapters project permissions.

    Route-bound launches use the sealed node, never a trailing legacy value.
    Route-free fixtures retain their legacy input. Slices have no commit
    authority on either path; adapters do not interpret sealed node fields.
    """
    worker_type = getattr(args, "worker_type", None)
    subsession_id = getattr(args, "subsession_id", None)
    authority = getattr(args, "stage_authority", 1)
    from route_authority import subsession_launch
    if worker_type != "stage" or subsession_launch(subsession_id, authority):
        return False
    route_path = getattr(args, "route_file", None)
    node_id = getattr(args, "route_node", None)
    if route_path and node_id:
        try:
            route = json.loads(Path(route_path).read_text(encoding="utf-8"))
            return route_node_commit_expected(route, node_id, worker_type,
                subsession_id=subsession_id, stage_authority=authority)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    return bool(getattr(args, "commit_expected", False))


def _resolved_write_scopes(scope: NodeScope) -> tuple[list[str], list[str]]:
    """Resolve repository mutation vocabulary separately from artifact scopes."""
    import artifact_context
    artifact, source = [], []
    for value in scope.write_scope:
        path = Path(value)
        if path.is_absolute():
            (source if scope.worktree_dir and path.is_relative_to(scope.worktree_dir) else artifact).append(str(path))
        elif artifact_context.classify_scope(value) == "source" and value in (
                "source-scoped", "source/**", "source-alternative/**"):
            source.append(str(Path(scope.worktree_dir) / "**") if scope.worktree_dir else value)
        elif artifact_context.classify_scope(value) == "source":
            source.append(str(Path(scope.worktree_dir) / value) if scope.worktree_dir else value)
        elif artifact_context.classify_scope(value) == "target":
            artifact.append(value)
        elif scope.output_dir:
            artifact.append(str(Path(scope.output_dir) / value))
        else:
            artifact.append(value)
    return artifact, source


def input_sources_prompt(route, node_id) -> str:
    """The inputs compose filled from an earlier cycle (`input_sources`), as absolute paths to read."""
    node = next((n for n in route.get("nodes") or [] if n.get("id") == node_id), None) if node_id else None
    sources = (node or {}).get("input_sources")
    if not isinstance(sources, dict) or not sources or not route.get("artifact_root"):
        return ""
    rows = [f"{name}={Path(route['artifact_root']) / source['path']}" for name, source in sources.items()
            if isinstance(source, dict) and isinstance(source.get("path"), str)]
    return ("Inputs from an earlier cycle (read them; they are not this cycle's outputs): "
            + ", ".join(rows) + "\n") if rows else ""


def node_scope_prompt(scope: "NodeScope") -> str:
    artifact_scope, source_scope = _resolved_write_scopes(scope)
    if scope.output_dir:
        outputs = _resolved_paths(scope.output_dir, scope.outputs)
    else:
        outputs = list(scope.outputs)
    lines = ["This node's declared scope:",
             f"outputs={json.dumps(outputs, ensure_ascii=False)}",
             f"artifact_write_scope={json.dumps(artifact_scope, ensure_ascii=False)}"]
    if "**" in scope.write_scope:
        lines.append("Cycle-wide artifact scope covers only the approved task's deliverables, not other work or cycles.")
    if source_scope:
        lines.append(f"worktree_source_scope={json.dumps(source_scope, ensure_ascii=False)}")
    if not scope.output_dir:
        lines.append("Cycle is unbound (no open cycle is bound); do not write durable artifacts under the artifact root.")
    if not scope.worktree_dir and ("source/**" in scope.write_scope or "source-alternative/**" in scope.write_scope
                                   or any(path.startswith("tests/") for path in scope.write_scope)):
        lines.append("The worktree source root is unresolved; do not guess its path.")
    return " ".join(lines) + "\n"


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
        answers = interview.recorded_answer_context(resolution.get("answers"), resolution.get("actor_kind"))
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
        "The recorded scope and decisions below govern this work. Apply the "
        "assigned stage within that scope; role defaults do not expand it. Cite "
        "applicable decision ids in the output.\n\n"
        f"{intent}\n"
    )


def runtime_progress_prompt(args=None, *, now=None) -> str:
    text = ("The runtime observes tool progress and publishes completion. "
            "No per-tool heartbeat command is required. Complete the assigned work "
            "and return its final artifact and verdict.")
    if args is not None and getattr(args, "worker_type", None) == "stage":
        try:
            seconds = float(args.foreground_timeout)
            if seconds > 0 and seconds < float("inf"):
                origin = now or datetime.now(timezone.utc)
                if origin.tzinfo is None:
                    origin = origin.replace(tzinfo=timezone.utc)
                deadline = origin.astimezone(timezone.utc) + timedelta(seconds=seconds)
                text += (f"\nThis attempt's execution budget is {seconds:g} seconds "
                         f"({seconds / 60:g} minutes); approximate deadline: "
                         f"{deadline.strftime('%Y-%m-%d %H:%M:%S UTC')}. "
                         "Long waits, including waiting with sleep, use this budget.")
        except (AttributeError, TypeError, ValueError, OverflowError):
            text += "\nThe attempt budget/deadline is unavailable in this prompt."
    return text + "\n\n"


def owner_gate_prompt(args) -> str:
    """Name the approval gate sealed on a node the owner executes itself, with the existing commands.

    Text only; the runtime enforces the gate whatever the owner does. Without it the owner reaches
    its own operation with no launch to be refused at, and the wait is a sentence it never read.
    """
    if getattr(args, "worker_type", None) != "owner":
        return ""
    route_file = getattr(args, "route_file", None) or getattr(getattr(args, "owner_route_binding", None), "route_file", None)
    if not route_file:
        return ""
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        import dispatch_contract
        held = dispatch_contract.owner_operation_gates(route)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    lines = []
    for node, gate in held:
        jobs = getattr(args, "jobs", None)
        command = (f"python3 {Path(__file__).resolve().with_name('workflow-supervisor.py')} gate --route {route_file} "
                   f"--gate {gate} --block --artifact <preview>" + (f" --jobs {jobs}" if jobs else ""))
        before = _owner_stage_predecessors(route, node)
        first = (f"first finish stage {', '.join(before)} (dispatch it, or run it yourself and publish its marker "
                 "as below) -- the gate is raised on a reviewed preview -- then " if before else "")
        lines.append(
            f"Human gate {gate} holds your own node {node.get('id')}: {first}write the preview, then run "
            f"`{command}` and end your turn; do not apply the edit before a person releases it. "
            "If the gate command is refused, do not apply either: finish with verdict BLOCKED and the reason.\n")
    return "".join(lines) + ("\n" if lines else "")


def _owner_stage_predecessors(route, node):
    """Declared stages before `node` that a worker, not the owner, executes (settlement needs their markers)."""
    import dispatch_contract
    nodes = {n.get("id"): n for n in route.get("nodes") or [] if isinstance(n, dict)}
    seen, pending, found = set(), list(node.get("depends_on") or []), []
    while pending:
        nid = pending.pop()
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        pending.extend(nodes[nid].get("depends_on") or [])
        if not dispatch_contract._owner_executed_node(nodes[nid]):
            found.append(nid)
    return sorted(found)


def owner_inline_marker_prompt(args) -> str:
    """Name the existing inline-stage completion command for a route owner. Text only; settlement still
    refuses a declared stage with no marker whatever the owner does."""
    if getattr(args, "worker_type", None) != "owner":
        return ""
    route_file = getattr(args, "route_file", None) or getattr(getattr(args, "owner_route_binding", None), "route_file", None)
    if not route_file:
        return ""
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        import dispatch_contract
        stages = [n for n in route.get("nodes") or [] if isinstance(n, dict)
                  and isinstance(n.get("dispatch_depth"), int) and n["dispatch_depth"] >= 2
                  and not dispatch_contract._owner_executed_node(n)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    if not stages:
        return ""
    command = (f"python3 {Path(__file__).resolve().with_name('capability-route.py')} complete --inline "
               f"--node <node> --reason \"<one line>\"")
    return (f"A declared stage ({', '.join(str(n['id']) for n in stages)}) you run yourself instead of dispatching "
            f"still publishes its completion marker: `{command}` (route and attempt axes are derived; pass --evidence with a cycle-local file when the stage produced one or the route has no bound cycle). Without it the route never settles.\n\n")


def plan_leg_prompt(route, node_id=None) -> str:
    """The approved plan for this leg, read from the sealed decision: what finishes it, what
    verification looks at, what it hands over, and the direction the person chose. Empty for a
    route made from no plan. A verification stage is also told where to record each item."""
    import route_plan as RP
    plan = RP.leg_plan(route)
    leg = (plan or {}).get("leg") or {}
    lines = []
    if leg.get("done_when"):
        lines.append("Done when (sealed ids, in this order):")
        lines += [f"- {item['id']}: {item['text']}" + (f" (check: `{item['check']}`)" if item.get("check") else "")
                  for item in leg["done_when"]]
    if leg.get("verify"):
        lines.append(f"Verify: {leg['verify']}")
    if leg.get("hands_over"):
        lines.append("Hands over to the next leg: " + "; ".join(leg["hands_over"]))
    for row in (plan or {}).get("handed_over") or []:
        lines.append(f"Handed over by leg {row['leg']}: " + "; ".join(row["items"]))
    if leg.get("parallel"):
        lines.append("Parallel slices the plan suggests: " + "; ".join(leg["parallel"]))
    added = {(n.get("plan_stage") or {}).get("id"): n["id"] for n in route.get("nodes") or [] if n.get("plan_stage")}
    for stage in leg.get("extra_stages") or []:
        if stage["id"] in added:
            lines.append(f"Stage the plan added: {added[stage['id']]} after {stage.get('after')}"
                         + (f" (verify: {stage['verify']})" if stage.get("verify") else ""))
        else:
            lines.append("Extra stage the plan suggests: " + ", ".join(f"{k} {v}" for k, v in stage.items()))
    if plan and plan.get("adopted_brief"):
        lines.append(f"Adopted direction: the brief {plan['adopted_brief']} (the person chose its route).")
    if not lines:
        return ""
    node = next((n for n in route.get("nodes") or [] if n.get("id") == node_id), {}) if node_id else {}
    if leg.get("done_when") and str(node.get("unit") or "").startswith("qa/"):
        lines.append(
            "Record each done-when item beside your artifact as `<artifact>.items.json`: "
            '{"schema": "leg_items_v1", "route_id": "' + str(route.get("route_id")) + '", "node": "'
            + str(node_id) + '", "attempt_id": "<your attempt id>", "items": [{"id": "d1", "state": '
            '"met|unmet|unknown", "note": "one line"}]} -- every sealed id once, no other id.')
    return "Plan for this leg (approved in the frame interview; base the verdict on it):\n" + "\n".join(lines) + "\n\n"


def auxiliary_arbitration_prompt(route, node_id, artifact="artifact") -> str:
    """Explain the existing gate using only the sealed realized groups and legs."""
    import dispatch_contract
    resolver = dispatch_contract._route_module()
    groups, required = resolver._auxiliary_groups_arbitrated_by(route, node_id)
    if not groups:
        return ""
    legs = [leg for group in groups for leg in resolver._realized_auxiliary_nodes(route, group)]
    lines = [
        f"Auxiliary arbitration for groups {', '.join(groups)}: {required} realized auxiliary leg(s) "
        f"({', '.join(leg['id'] for leg in legs)}). The {artifact} must carry "
        f"auxiliary_findings_considered with exactly {required} entries, one per auxiliary leg, "
        "stating how its findings affected the conclusion. Use Markdown frontmatter as follows:",
        "---", "auxiliary_findings_considered:",
        *(f'  - "{leg["id"]}: <finding and disposition>"' for leg in legs), "---",
    ]
    return "\n".join(lines) + "\n"


def automatic_consumer_prompt(route, node_id) -> str:
    additions = ((route.get("composed_recipe") or {}).get("compose") or {}).get("auto_completed_nodes", [])
    addition = next((row for row in additions if row["id"] == node_id
                     and row["reason"] != "terminal-anchor"), None)
    if addition is None:
        return ""
    node = next(node for node in route["nodes"] if node["id"] == node_id)
    return (
        f"Your automatic registry consumer {node_id}: read every predecessor result "
        f"({', '.join(node.get('depends_on') or [])}) and complete the existing "
        f"{node['completion_gate']} gate using only your sealed inputs, outputs and write scope.\n"
        + auxiliary_arbitration_prompt(route, node_id) + "\n")


def owner_close_prompt(args) -> str:
    """Project the automatic closing node and its existing arbitration obligation."""
    if getattr(args, "worker_type", None) != "owner":
        return ""
    route_file = getattr(args, "route_file", None) or getattr(getattr(args, "owner_route_binding", None), "route_file", None)
    if not route_file:
        return ""
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""  # Preview-only bindings retain the existing prompt fallback.
    lines = []
    for node in route.get("nodes") or []:
        if node.get("completion_gate") != "compose-owner-close":
            continue
        lines.append(
            f"Your automatic closing node {node['id']}: finish every predecessor "
            f"({', '.join(node.get('depends_on') or [])}), read all their results, reconcile findings, "
            f"and write only {', '.join(node['outputs'])}. No publication, synchronization, or edits to prior artifacts.")
        arbitration = auxiliary_arbitration_prompt(route, node['id'], "closing summary")
        if arbitration:
            lines.append(arbitration.rstrip())
    return "\n".join(lines) + ("\n\n" if lines else "")


def _child_scope_environment(args, route, environ, source_worktree=None):
    """Project validated launch identity over the caller's context for scope lookup.

    Prompt construction runs before adapters build the child environment. Keep
    inherited producer-cycle and review bindings intact, while replacing the
    parent-specific route node/worktree with the child values already checked
    by the wrapper. The linked-slice worktree is supplied only by
    ``_linked_slice_source_worktree`` after its parent and repository checks.
    """
    child = dict(environ or {})
    route_file = getattr(args, "route_file", None)
    route_id = getattr(args, "route_id", None) or route.get("route_id")
    artifact_root = getattr(args, "artifact_root", None) or route.get("artifact_root")
    node_id = getattr(args, "route_node", None)
    binding = getattr(args, "review_output_binding", None)
    worktree = (source_worktree
                or (binding or {}).get("worktree")
                or getattr(args, "worktree", None)
                or route.get("cwd"))

    if route_file:
        child["AGENT_ROUTE_FILE"] = str(route_file)
    if route_id:
        child["AGENT_ROUTE_ID"] = str(route_id)
    child["AGENT_ROUTE_NODE"] = str(node_id or "")
    if artifact_root:
        child["AGENT_ARTIFACT_ROOT"] = str(artifact_root)
    if worktree:
        child["AGENT_DISPATCH_WORKTREE"] = str(worktree)
    parent_attempt_id = getattr(args, "parent_attempt_id", None)
    if parent_attempt_id:
        child["AGENT_DISPATCH_PARENT_ATTEMPT_ID"] = str(parent_attempt_id)
    attempt_id = getattr(args, "attempt_id", None)
    if attempt_id:
        child["AGENT_DISPATCH_ATTEMPT_ID"] = str(attempt_id)
    return child


def assignment_prompt(args, task: str, environ) -> str:
    """Project the route's input/output boundary, rather than ask a caller to copy it.

    A frame consumes the requested work as analysis input. In particular the
    final task's report filename is not that frame's output filename. Route
    validation and the write oracle retain authority over paths.
    """
    if getattr(args, "worker_type", None) != "frame":
        route_file = getattr(args, "route_file", None)
        if not route_file:
            return f"Assignment:\n{task.rstrip()}\n\n{owner_gate_prompt(args)}{owner_inline_marker_prompt(args)}{owner_close_prompt(args)}"
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        source_worktree = _linked_slice_source_worktree(args, route)
        scope_environment = _child_scope_environment(args, route, environ, source_worktree)
        scope = resolve_node_scope(
            route, getattr(args, "route_node", None), scope_environment,
            parent_attempt_id=getattr(args, "parent_attempt_id", None),
            route_file=route_file,
            source_worktree=source_worktree,
            source_worktree_verified=source_worktree is not None,
        )
        return (f"Assignment:\n{task.rstrip()}\n\n{plan_leg_prompt(route, getattr(args, 'route_node', None))}"
                f"{node_scope_prompt(scope)}{input_sources_prompt(route, getattr(args, 'route_node', None))}\n"
                f"{automatic_consumer_prompt(route, getattr(args, 'route_node', None))}"
                f"{owner_gate_prompt(args)}{owner_inline_marker_prompt(args)}{owner_close_prompt(args)}")
    outputs = []
    route_file = getattr(args, "route_file", None)
    if route_file:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        source_worktree = _linked_slice_source_worktree(args, route)
        scope_environment = _child_scope_environment(args, route, environ, source_worktree)
        scope = resolve_node_scope(
            route, getattr(args, "route_node", None), scope_environment,
            parent_attempt_id=getattr(args, "parent_attempt_id", None),
            route_file=route_file,
            source_worktree=source_worktree,
            source_worktree_verified=source_worktree is not None,
        )
        outputs = (
            _resolved_paths(scope.output_dir, scope.outputs) if scope.output_dir else list(scope.outputs)
        )
    return (
        "User goal to analyze (the later owner's task):\n"
        f"{task.rstrip()}\n\n"
        "Current assignment: produce your frame direction brief, with options and a direction verdict. "
        "Keep the analysis within the user's scope. Output filenames in the user goal describe the later "
        "task; this frame produces only its own declared brief.\n"
        + ("This frame's declared output: " + json.dumps(outputs, ensure_ascii=False) + "\n" if outputs else "")
        + "\n"
    )


def _linked_slice_source_worktree(args, route) -> Path | None:
    """Reuse the wrapper's exact live-parent validation for a declared slice.

    Only a stage-authority-zero sub-session with the exact resolved parent may
    use its linked execution worktree for source provenance. Ordinary children,
    incomplete bindings, and review-bound launches keep the route/environment
    worktree check unchanged.
    """
    candidate = getattr(args, "worktree", None)
    parent = getattr(args, "parent_binding", None)
    parent_attempt_id = getattr(args, "parent_attempt_id", None)
    if (not candidate or not parent or not parent_attempt_id
            or getattr(parent, "attempt_id", None) != parent_attempt_id
            or getattr(args, "review_output_binding", None)):
        return None
    metadata = {
        "subsession_id": getattr(args, "subsession_id", None),
        "stage_authority": str(getattr(args, "stage_authority", 1)),
    }
    import dispatch_contract
    if not dispatch_contract.is_linked_worktree_slice(metadata):
        return None
    sealed = route.get("cwd")
    parent_worktree = getattr(parent, "worktree", None)
    if not sealed or not parent_worktree:
        return None
    sealed_path = Path(str(sealed)).expanduser().resolve(strict=False)
    parent_path = Path(str(parent_worktree)).expanduser().resolve(strict=False)
    source_path = Path(str(candidate)).expanduser().resolve(strict=False)
    if parent_path != sealed_path or source_path == sealed_path:
        return None
    if (dispatch_contract.canonical_repository_identity(source_path)
            != dispatch_contract.canonical_repository_identity(sealed_path)):
        return None
    return source_path


def worker_ending_prompt(*, supervised_owner: bool = False) -> str:
    """One ending rule for every adapter; supervised wait turns are nonterminal."""
    wait = (
        "End an intermediate child-registration or resource-wait turn only with the "
        "standalone line `runtime_wait: registered-children`, without a three-line handoff. "
        if supervised_owner else ""
    )
    # State both ends: "nothing after it" alone allowed a summary before the
    # envelope in two 2026-07-28 pipelines, losing their terminal handoff.
    return (
        wait + "For the terminal final response, end with the kernel's exact three-line "
        "handoff as the entire message — no summary sentence before it, nothing after it.\n"
    )


def supervised_owner_prompt() -> str:
    return (
        "Runtime-owned completion join: launch the current batch through its checked dispatch surface. "
        "A start receipt proves launch with registered=1, started=1, child_spawned=1. "
        "Yield with `runtime_wait: registered-children`; the runtime waits and resumes this owner "
        "after model children or the exact route-bound resource. For a resource, use the normal "
        "runner receipt confirming payload_spawned and supervisor_alive, then yield instead of "
        "waiting for training inside a Bash turn. Resource exit is not verification PASS. "
        "with an exact receipt. It also acknowledges delivery and retains unresolved cleanup. "
        "Use the result to continue authorized work within the existing gates. "
        "Inspection commands are available when needed; they are not a delivery acknowledgement.\n\n"
    )


_TERMINAL_CONTRACT_START = "<!-- worker-terminal-contract:start -->"
_TERMINAL_CONTRACT_END = "<!-- worker-terminal-contract:end -->"


def worker_terminal_contract(root: Path) -> str:
    """The kernel's short completion contract, reusable after native compaction."""
    kernel = (root / "roles" / "worker-bootstrap.md").read_text(encoding="utf-8")
    return kernel.split(_TERMINAL_CONTRACT_START, 1)[1].split(_TERMINAL_CONTRACT_END, 1)[0].strip()


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
    # The section markers locate the recurring contract; workers need only its text.
    fragments[0] = "\n".join(
        line for line in fragments[0].splitlines()
        if line not in {_TERMINAL_CONTRACT_START, _TERMINAL_CONTRACT_END})
    persona = unit_persona_body(root, unit)
    if persona:
        fragments.append(persona)
    return "\n\n".join(fragments) + "\n"


@functools.lru_cache(maxsize=8)
def _gate_contracts_cached(path: str, mtime_ns: int) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    table = data.get("completion_gate_contracts")
    return table if isinstance(table, dict) else {}


def _gate_contracts(root: Path | None) -> dict:
    """The portable ``completion_gate_contracts`` table; ``{}`` when unreadable."""
    try:
        path = Path(root or _REPO_ROOT) / "capabilities" / "topologies.json"
        return _gate_contracts_cached(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return {}


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

    The gate table is the source: a ``capability-doc`` gate names its contract
    file stem, any other gate kind is read through the entry capability. The
    node-id fallback (plan/execute/test/report -> code-*) is autopilot-code
    legacy only, so another capability's ``report`` node is not read as code-report.
    """
    if worker_type == "frame":
        return unit or "plan/frame"  # The injected unit is the contract, not the owner recipe.
    if explicit:
        return explicit
    if worker_type in {"stage", "review", "support"}:
        entry = _gate_contracts(root).get(completion_gate) if completion_gate else None
        if isinstance(entry, dict):
            if entry.get("kind") == "capability-doc" and entry.get("contract"):
                return Path(str(entry["contract"])).stem
            return capability
        if completion_gate and root and (Path(root) / "capabilities" / f"{completion_gate}.md").is_file():
            return completion_gate
        if capability == "autopilot-code" and route_node and route_node.lower() in STAGE_NODE_CONTRACT:
            return STAGE_NODE_CONTRACT[route_node.lower()]
    return capability


def _report_reference_line(args) -> str:
    if getattr(args, "worker_type", None) != "stage":
        return ""
    refs = REPORT_CONTRACT_REFERENCES.get(getattr(args, "completion_gate", None) or "", ())
    existing = [str(_REPO_ROOT / rel) for rel in refs if (_REPO_ROOT / rel).is_file()]
    if not existing:
        return ""
    return ("- This node's report format is specified in: " + ", ".join(existing)
            + " — read their report/bundle sections before writing.\n")


def contract_read_prompt(args, harness: str) -> str:
    """One contract-loading instruction; frame units are already injected."""
    if args.worker_type == "frame":
        return ("- Your frame unit contract is already included above. Read its named inputs within "
                "the requested scope; no owner capability Skill or full harness bootstrap is needed.\n")
    if harness == "codex":
        line = (f"- Read only $AGENT_HOME/adapters/codex/skills/{args.assigned_contract}/SKILL.md; "
                "the typed bootstrap above already contains the exact portable unit persona.\n")
    elif harness == "claude":
        line = (f"- Read only the exposed {args.assigned_contract} Skill, named artifacts, and selected specialization. "
                "General Claude custom subagents may still inherit project CLAUDE.md; do not manually load a full harness bootstrap.\n")
    else:
        line = (f"- Read only the assigned {args.assigned_contract} Skill/mode and named artifact inputs. "
                "Project instruction auto-load is not treated as physically masked; do not manually load a full harness bootstrap.\n")
    return line + _report_reference_line(args)


def _cycle_candidates(roots: list[Path], current_cycle: Path | None, deadline: float):
    """Yield cycle dirs: the current campaign first, then the whole project, newest name first."""
    def children(path: Path) -> list[Path]:
        try:
            with os.scandir(path) as it:
                return sorted((Path(e.path) for e in it if e.is_dir(follow_symlinks=False)),
                              key=lambda q: q.name, reverse=True)
        except OSError:
            return []

    seen: set[Path] = set()
    if current_cycle is not None:
        for cycle in children(current_cycle.parent):
            seen.add(cycle.resolve())
            yield cycle
    rest: list[Path] = []
    for root in roots:
        for campaign in children(root / "campaigns"):
            if time.monotonic() >= deadline:
                break
            rest.extend(children(campaign))
    for cycle in sorted(rest, key=lambda q: q.name, reverse=True):
        try:
            key = cycle.resolve()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            yield cycle


def _first_valid(artifacts: Path, patterns) -> Path | None:
    for pattern in patterns:
        try:
            for match in sorted(artifacts.glob(pattern)):
                rel = match.relative_to(artifacts)
                if "_internal" in rel.parts or match.is_symlink() or not match.is_file():
                    continue
                if match.stat().st_size >= _EXEMPLAR_MIN_BYTES:
                    return match
        except OSError:
            continue
    return None


def _deliverable_exemplars(gate, artifact_root, environ, *, deadline_s: float = 1.5,
                           max_cycles: int = 60) -> list[Path]:
    """Newest sealed cycle's completed deliverable(s) for ``gate``: first MD and first HTML."""
    patterns = DELIVERABLE_EXEMPLARS.get(gate or "")
    if not patterns or not artifact_root:
        return []
    deadline = time.monotonic() + deadline_s
    base = Path(artifact_root)
    roots = [base]
    sibling = {".agent_reports": ".claude_reports", ".claude_reports": ".agent_reports"}.get(base.name)
    if sibling and (base.parent / sibling).is_dir():
        roots.append(base.parent / sibling)
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    cycle_env = artifact_cycle_environment(environ)
    cycle_dir = cycle_env["AGENT_ARTIFACT_CYCLE_DIR"] or (
        str(Path(cycle_env["AGENT_ARTIFACT_OUTPUT_DIR"]).parent) if cycle_env["AGENT_ARTIFACT_OUTPUT_DIR"] else "")
    current = Path(cycle_dir).resolve() if cycle_dir else None
    html_patterns, md_patterns = patterns
    inspected = 0
    for cycle in _cycle_candidates(unique, current, deadline):
        if inspected >= max_cycles or time.monotonic() >= deadline:
            break
        try:
            if current is not None and cycle.resolve() == current:
                continue
            inspected += 1
            if not (cycle / "manifest.json").is_file():
                continue
            artifacts = cycle / "artifacts"
            found = [p for p in (_first_valid(artifacts, md_patterns), _first_valid(artifacts, html_patterns)) if p]
        except OSError:
            continue
        if found:
            return found
    return []


def _query_tokens(task: str, worktree: str | None) -> str:
    """Project-name tokens plus up to 24 task words; path/URL-like and numeric tokens are dropped."""
    def usable(token: str) -> bool:
        return len(token) >= 2 and not token.isdigit() and not any(c in token for c in "/=:")

    picked = [t for t in (re.split(r"[-_.]", Path(worktree).name) if worktree else []) if usable(t)]
    task_words: list[str] = []
    for raw in (task or "")[:1200].split():
        token = raw.strip(".,;:()[]{}\"'`*#<>")
        if usable(token) and token not in task_words:
            task_words.append(token)
    return " ".join(dict.fromkeys(picked + task_words[:24]))


def _bootstrap_memory_block(topics, task, worktree, *, root=None, timeout_s: float = 3.0,
                            environ=None) -> str:
    """Bounded read-only preference bodies through ``mem bootstrap-context``; failure is ``""``."""
    q1 = " ".join(BOOTSTRAP_MEMORY_TOPICS[t] for t in topics if t in BOOTSTRAP_MEMORY_TOPICS)
    if not q1 or not worktree:
        return ""
    mem = Path(root or _REPO_ROOT) / "tools" / "memory" / "mem.py"
    cmd = [sys.executable, str(mem), "bootstrap-context", "--cwd", str(worktree), "--query", q1]
    q2 = _query_tokens(task, worktree)
    if q2:
        cmd += ["--query", q2]
    cmd += ["--limit", "4", "--max-bytes", str(_MEMORY_BLOCK_MAX_BYTES)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(worktree),
                              env=dict(environ) if environ is not None else os.environ.copy())
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if done.returncode != 0:
        return ""
    out = done.stdout.encode("utf-8")[:_MEMORY_BLOCK_MAX_BYTES].decode("utf-8", "ignore").strip()
    return out


def unit_bootstrap_prompt(args, task: str, environ, *, root: Path | None = None) -> str:
    """Material a unit declared in its ``bootstrap:`` frontmatter, or ``""``.

    Runtime-supplied and read-only. A unit without a declaration receives nothing
    (no subprocess, no scan); any failure drops the material, never the launch.
    """
    try:
        root = Path(root or _REPO_ROOT)
        decl = unit_bootstrap_declaration(root, getattr(args, "unit", None))
        if not decl or getattr(args, "worker_type", None) not in BOOTSTRAP_STAGE_TYPES:
            return ""
        gate = getattr(args, "completion_gate", None)
        if decl.get("gates") and gate not in decl["gates"]:
            return ""
        lines = []
        if decl.get("exemplar") in BOOTSTRAP_EXEMPLAR_KINDS:
            found = _deliverable_exemplars(gate, getattr(args, "artifact_root", None), environ)
            if found:
                shown = "; ".join(f"{p} ({max(1, round(p.stat().st_size / 1024))} KB)" for p in found)
                lines.append(
                    f"- Format exemplar — a completed {gate} deliverable from this project: {shown}\n"
                    "  Follow its section order, layout, tone, and visualization style. "
                    "Take every fact only from your assigned inputs.\n"
                    "  For HTML, skim the structure; do not load embedded media.\n")
        memory = _bootstrap_memory_block(decl.get("memory", ()), task, getattr(args, "worktree", None),
                                         root=root, environ=environ)
        if memory:
            lines.append("- Saved preferences from memory (reference only; apply only what fits your assignment; "
                         "do not write, curate, or sync memory):\n" + memory + "\n")
        if not lines:
            return ""
        return ("Unit bootstrap material (runtime-supplied, read-only reference):\n" + "".join(lines)
                + "- If format guidance and your inputs disagree: content follows the inputs, "
                  "format follows these conventions.\n\n")
    except Exception:
        return ""


def handoff_template() -> str:
    return (
        "artifact: <canonical path | ->\n"
        "verdict: PASS | FAIL | BLOCKED\n"
        "blocker: none | <one line>"
    )
