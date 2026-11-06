"""Dispatch contract: gates and completion-marker currency.

Human-gate and owner gates, frame gates, the completion marker gate, the
auxiliary-arbitration and sibling gates, review self claims, marker and
evidence currency, resource-stage reuse, and completion readiness.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from typing import Mapping, NamedTuple
from hearting_gates import note_same_work_drift
from session_identity import session_label
from route_identity import registered_node_identity
from dispatch_attempt_policy import committed_outcome, verdict_pass, terminal_conflict_pending
from route_authority import depth1_supported_harnesses
from stage_session_contract import load_manifest

from dispatch_contract_base import (
    _route_module, agent_home_equivalent, attempt_row_never_started, ATTEMPT_SCHEMA_VERSION,
    AttemptReadiness, DispatchContractError, owner_closure_shape, parse_registry_metadata,
    route_state_next_action, row_is_subsession,
)

from dispatch_contract_roots import (
    dispatch_state_roots, resolve_dispatch_state_root,
)

from dispatch_contract_policy import (
    validate_attempt_metadata,
)

from dispatch_contract_process import (
    attempt_process_quiescence,
)

from dispatch_contract_launch import (
    _launch_completion_input, _launch_completion_marker_input,
)


# The human gates whose owner contract implements raise + await (SD-129
# §13.41.2-5). See `_human_gate_entry_fence`.
FENCED_HUMAN_GATES = frozenset({"frame-review", "preview-disposition"})


def _human_gate_entry_fence(
    route: dict, node: dict, jobs: Path | None = None, *, release_proof: bool = False,
    only: frozenset | None = None,
) -> None:
    """Refuse to start a node whose entry human gate is not released (SD-129).

    Defect M, measured 2026-09-04 on route rt-da62cded: the owner raised
    `frame-review`, read `BLOCKED_HUMAN_GATE` from `status`, and spawned `plan`
    anyway -- the gate wait was a sentence in `owner-execution.md` and no launch
    surface asked the workflow journal. Every wrapper (the three adapters'
    `dispatch-headless.py`, `dispatch-batch.py`) funnels through this gate, so
    the fence lives here once.

    The answer comes from `workflow_state.human_gate_resolution`, the same rule
    the owner's `await-release` and the carriers read, and from the same ledger
    root the supervisor writes. An explicit wrapper `--jobs` is the authority
    for both sides; ambient roots are used only when no registry was supplied.
    Both "never raised" and "raised, not released" are refusals: a route sealed
    with the binding must pass through the gate. `revise` and `stop` leave the
    gate unreleased too.

    `release_proof` is the question an owner's own operation asks (the owner is
    already running, so no launch ever reaches this fence for that node): a
    `proceed` then also has to be a person's release of the preview that is still
    current, the way a quick one-shot's inline gate already requires. The launch
    callers leave it off and keep their meaning.
    """

    # Two conditions, both required (review rounds 1 and 2, B1):
    #   1. some node of THIS route raises the gate -- the SD-123 mechanism is
    #      `predecessor.continuation = {kind: human-gate, gate}` ->
    #      BLOCKED_HUMAN_GATE -> release; a binding with no raising continuation
    #      is satisfied by the §0.4 card;
    #   2. the gate is in FENCED_HUMAN_GATES -- the two gates whose owner
    #      contracts implement the raise and wait. Older sealed routes can
    #      still carry historical bindings for other gate names; new topology
    #      entries do not declare those three start approvals as gates.
    # A gate joins the set in the same change that teaches its owner to raise
    # it; `dispatch_contract.test.py` pins the set against the real topology.
    # The three direction gates the other autopilot recipes used to declare
    # (`direction-confirmation`, `user-refine-disposition`, `intent-confirmation`)
    # were absorbed into the one universal `frame-review` once
    # `capabilities/autopilot-{design,draft,spec}.md` documented the raise.
    # `preview-disposition` was NOT absorbed (user decision, 2026-09-10): it is
    # an approval before refine's transaction applies an edit, not a direction,
    # and it joins this set in the same change that documents its raise in
    # `capabilities/autopilot-refine.md`. Before that change it was declared but
    # never fenced, so no refine route ever actually waited on it.
    # Routes compiled before that absorption are never retro-fitted: this fence
    # reads only the route object it was handed, so an old-generation route
    # keeps its own generation's node shape, gate names and bindings.
    raised_gates = {
        str((n.get("continuation") or {}).get("gate"))
        for n in (route.get("nodes") or [])
        if isinstance(n, dict) and (n.get("continuation") or {}).get("kind") == "human-gate"
        and (n.get("continuation") or {}).get("gate")
    }
    bindings = [
        row for row in (route.get("human_gate_bindings") or [])
        if isinstance(row, dict) and row.get("node") == node.get("id")
        and (row.get("position") or "entry") == "entry" and row.get("gate")
        and str(row.get("gate")) in raised_gates
        and str(row.get("gate")) in FENCED_HUMAN_GATES
        and (only is None or str(row.get("gate")) in only)
    ]
    if not bindings:
        return
    import workflow_state as WS  # noqa: WPS433 -- workflow_state imports this module

    try:
        ledger = WS.WorkflowLedger(
            str(route["route_id"]), str(route.get("route_hash", "")), jobs=jobs
        )
        entries = ledger.journal()
        _root, source = WS.ledger_root_for(jobs)
    except WS.WorkflowStateError as exc:
        raise DispatchContractError(
            "workflow-ledger-authority-invalid", str(exc)
        ) from exc
    where = (
        f"route {route['route_id']} ledger {ledger.root} "
        f"ledger_root_source={source}"
    )
    for binding in bindings:
        gate = str(binding["gate"])
        resolution = WS.human_gate_resolution(entries, gate)
        status = resolution["status"]
        if status == "proceed":
            if release_proof:
                _release_proof_or_refuse(WS, gate, resolution, where)
            continue
        if status == "not-raised":
            raise DispatchContractError(
                "human-gate-not-raised",
                f"{gate}: node {node.get('id')} enters through human gate {gate!r}, "
                f"which this route has never raised ({where}); the owner raises it with "
                "`workflow-supervisor.py gate --block --artifact <path>` and waits "
                "with `await-release` before starting this node",
            )
        raise DispatchContractError(
            "human-gate-unreleased",
            f"{gate}: latest raise (epoch {resolution['epoch']}) is {status} ({where}); "
            "wait with `workflow-supervisor.py await-release` until a person "
            "releases it with --decision proceed"
            + (" (a revise needs the gate raised again first)" if status == "revise" else ""),
        )



def _release_proof_or_refuse(WS, gate: str, resolution: dict, where: str) -> None:
    """A `proceed` that did not come from a person, or that no longer matches the preview."""
    if resolution.get("actor_kind") != "user":
        raise DispatchContractError(
            "human-gate-unreleased",
            f"{gate}: the release was not recorded by a person ({where})")
    if not resolution.get("artifact_sha256"):
        return  # raised before the preview's bytes were recorded: nothing to compare against
    try:
        WS.require_gate_artifact_current(resolution)
    except (WS.WorkflowStateError, OSError, ValueError) as exc:
        raise DispatchContractError(
            "human-gate-unreleased",
            f"{gate}: the released preview is no longer the current one ({exc}; {where}); raise the gate again") from exc


# The same rule asked for a node the owner executes itself (SD-129 + refine's preview approval).
human_gate_entry_fence = _human_gate_entry_fence

# The gates an owner's own operation answers to. `frame-review` is not one of them: it is
# settled before the owner launches (`owner_frame_launch_gate`), so the operation never re-asks it.
OWNER_OPERATION_GATES = frozenset({"preview-disposition"})


def _owner_executed_node(node: dict) -> bool:
    """`capability-route.owner_executed_terminal`, kept here so a write check never loads the route module."""
    return (node.get("terminal") is True and node.get("kind") == "capability-owner"
            and node.get("unit") == "_kernel/owner" and node.get("dispatch_depth") == 1)


def owner_operation_gates(route: dict) -> list[tuple[dict, str]]:
    """`(node, gate)` for each approval gate sealed on a node the owner executes itself."""
    nodes = {n.get("id"): n for n in route.get("nodes") or [] if isinstance(n, dict)}
    return [(nodes[row["node"]], str(row["gate"])) for row in route.get("human_gate_bindings") or []
            if isinstance(row, dict) and row.get("node") in nodes and (row.get("position") or "entry") == "entry"
            and str(row.get("gate")) in OWNER_OPERATION_GATES and _owner_executed_node(nodes[row["node"]])]


def owner_operation_fence(route: dict, node: dict, jobs: Path | None = None) -> None:
    """Refuse the owner's own operation until a person released the current preview."""
    import route_parent_close
    if route_parent_close.intent(route, jobs):
        raise DispatchContractError("cancelled-by-parent")
    _human_gate_entry_fence(route, node, jobs, release_proof=True, only=OWNER_OPERATION_GATES)


# Set by the runtime's own raise inside the parent's `start` (and nowhere else): the caller is the
# parent session reading the receipt right now, so that receipt is the delivery. See
# `workflow-supervisor.gate_delivered_in_receipt`.
GATE_RECEIPT_DELIVERY_ENV = "AGENT_GATE_RECEIPT_DELIVERY"


def raise_preview_gate_for_node(route_file, route_node, jobs, agent_home, *, in_parent_receipt=False) -> str:
    """Raise the existing `preview-disposition` question from the completed preview node.

    One body for two callers: a refused child start (`recover_preview_gate_after_refusal`) and
    an owner that already applied its transaction without ever raising the gate. Only a
    current completed review artifact can back the gate transaction; this never releases
    anything. Returns the carrier's delivery id (`-` when the receipt itself is the delivery);
    raises ValueError with the reason otherwise.

    `in_parent_receipt` is for the parent's own `start` only: the question reaches the person in
    the receipt that call returns, so a parent kind with no push carrier needs no pending record.
    """
    route = json.loads(Path(route_file).read_text())
    bindings = [b for b in route.get("human_gate_bindings", [])
                if b.get("gate") == "preview-disposition" and b.get("node") == route_node
                and b.get("position", "entry") == "entry"]
    raisers = [n for n in route.get("nodes", [])
               if n.get("continuation") == {"kind": "human-gate", "gate": "preview-disposition"}]
    target = next(n for n in route.get("nodes", []) if n.get("id") == route_node)
    if len(bindings) != 1 or len(raisers) != 1 or raisers[0]["id"] not in target.get("depends_on", []):
        raise ValueError("preview-predecessor-unverified")
    predecessor = raisers[0]
    paths = [r / "completion" / route["route_id"] / (predecessor["id"] + ".json")
             for r in dispatch_state_roots(Path(agent_home), Path(jobs))]
    marker_path = next(p for p in paths if p.is_file())
    marker = json.loads(marker_path.read_text())
    if not completion_marker_is_current(route, predecessor, marker_path, marker, jobs=jobs):
        raise ValueError("preview-marker-unverified")
    ready = completion_attempt_readiness(route, predecessor, marker, Path(jobs))
    if ready.state != "ready":
        raise ValueError("preview-attempt-" + ready.state)
    artifact = Path(marker["evidence"]["path"])
    if not artifact.is_absolute() or not artifact.is_file():
        raise ValueError("preview-artifact-unreadable")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("workflow-supervisor.py")),
         "gate", "--route", str(route_file), "--gate", "preview-disposition",
         "--block", "--jobs", str(jobs), "--artifact", str(artifact)],
        text=True, capture_output=True, timeout=15, check=False,
        env={**os.environ, GATE_RECEIPT_DELIVERY_ENV: "1"} if in_parent_receipt else None)
    if result.returncode:
        raise ValueError("gate-carrier-refused: " + result.stderr.strip()[:240])
    payload = json.loads(result.stdout)
    if payload.get("action") != "blocked" or payload.get("workflow_state") != "BLOCKED_HUMAN_GATE":
        raise ValueError("gate-block-unverified")
    return str(payload.get("delivery") or "-")


def recover_preview_gate_after_refusal(route_file, route_node, action, agent_home, jobs,
                                      error: DispatchContractError) -> str:
    """Outside the claim lock, turn a proved legacy preview into a real question.

    The original start still fails. This never grants approval or changes a route.
    Only a current completed review artifact can back the existing gate transaction.
    """
    if (action != "start" or error.reason != "human-gate-not-raised"
            or not error.detail.startswith("preview-disposition:") or not route_file or not jobs):
        return error.detail
    try:
        delivery = raise_preview_gate_for_node(route_file, route_node, jobs, agent_home)
        return error.detail + "; preview_gate_recovery=blocked delivery=" + delivery
    except (OSError, ValueError, KeyError, StopIteration, DispatchContractError, subprocess.TimeoutExpired) as exc:
        return error.detail + "; preview_gate_recovery=unavailable reason=" + str(exc)[:320]


def owner_frame_launch_gate(binding, action: str, agent_home: Path,
                            jobs: Path | None = None) -> None:
    """The depth-0 frame must finish and be approved before an owner starts."""
    if binding is None or action != "start":
        return
    route = json.loads(Path(binding.route_file).read_text(encoding="utf-8"))
    frames = {n.get("id") for n in route.get("nodes", [])
              if n.get("worker_type") == "frame" and n.get("dispatch_depth") == 1}
    if not frames:
        return  # Earlier route generations keep their original owner contract.
    entries = [n for n in route.get("nodes", [])
               if frames.issubset(set(n.get("depends_on", [])))]
    # A recipe may fan out after the frame pair (autopilot-spec standard+ starts
    # research and research-alternative together); every entry waits on the
    # same approved frames, so each is checked rather than requiring exactly one.
    import route_plan as RP
    if not RP.valid_frame_legs(route, frames) or not entries:
        raise DispatchContractError("frame-owner-entry-invalid", str(route.get("route_id")))
    for entry in entries:
        completion_marker_gate(binding.route_file, entry["id"], action, agent_home, jobs)


def _frame_capacity_failures(route: dict, lines: list[str], harnesses: set[str]) -> dict[str, str]:
    """Exact failed frame attempts can disprove admission-time availability.

    Capacity estimates and a generic exit code are not that proof. Retain the
    latest attempt per unavailable harness, its native quota result and cleanup.
    """
    from codex_dispatch_terminal import inspect_terminal_attempt
    latest = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        if (meta.get("route_id") == route["route_id"] and meta.get("route_hash") == route["route_hash"]
                and meta.get("worker_type") == "frame" and meta.get("dispatch_depth") == "1"
                and meta.get("harness") in harnesses):
            latest[meta["harness"]] = (fields, meta)
    unavailable = {}
    for harness, (fields, meta) in latest.items():
        if fields[1] != "done" or terminal_conflict_pending(meta) or verdict_pass(meta):
            continue
        if attempt_process_quiescence(meta, terminal_receipt=True).state != "quiescent":
            continue
        terminal = inspect_terminal_attempt(meta.get("log_file"), worktree=fields[3],
            artifact_root_metadata=meta.get("artifact_root"), artifact_binding=meta)
        if terminal.get("failure_class") == "capacity":
            unavailable[harness] = meta["attempt_id"]
    return unavailable


def frame_harness_admission(route: dict, jobs: Path, lines: list[str],
                            harnesses: list[str], profiles: list[str | None]) -> list[str]:
    """Validate effective SD-160 frame personas, preserving sealed route bytes.

    Historical frame nodes used the two canonical prompt roles without an
    explicit perspective field. Their node IDs retain that meaning; an explicit
    field must be nonempty and distinct, regardless of the selected harnesses.
    The empty proof list is retained for callers of the old admission API.
    """
    import route_plan as RP
    frames = [node for node in route.get("nodes", [])
              if node.get("id") in {"frame", "frame-alternative"}]
    if not RP.valid_frame_legs(route, [node.get("id") for node in frames]) or len(frames) != len(
            {node.get("id") for node in frames}):
        raise DispatchContractError("frame-pair-incomplete", str(route.get("route_id")))
    perspectives = [node.get("perspective", {"frame": "primary-frame",
                                           "frame-alternative": "alternative-frame"}[node["id"]])
                    for node in frames]
    if (any(not isinstance(value, str) or not value.strip() for value in perspectives)
            or len({value.strip() for value in perspectives}) != len(frames)):
        raise DispatchContractError("frame-perspective-duplicate", str(perspectives))
    supported = depth1_supported_harnesses(route, worker_type="frame")
    if len(harnesses) != len(frames) or not supported or not set(harnesses).issubset(supported):
        raise DispatchContractError("frame-harness-unsupported", str(harnesses))
    return []


def record_frame_launch_degradation(route: dict, jobs: Path, proof_ids: list[str],
                                    first_attempt: str, harness: str,
                                    reason: str = "frame-single-available-harness",
                                    detail: str | None = None) -> None:
    """Record launch-time frame degradation through the existing contract writer.

    SD-93d makes this observation best effort; admission remains authoritative.
    """
    from dispatch_degradation import record_degradation
    record_degradation(route_id=route["route_id"], route_hash=route["route_hash"],
        route_node="frame-alternative", dispatch_depth=1, writer="dispatch_contract.py",
        jobs=jobs, fallback_hop="same-harness-headless",
        execution_surface="registered-headless", reason=reason,
        prior_attempt_ids=proof_ids, attempt_trace=[first_attempt], harness=harness,
        detail=detail or json.dumps({"proof_ids": proof_ids}))


def _frame_pair_attempt_gate(route: dict, node: dict, markers: dict,
                             jobs: Path, registry_lines: list[str] | None) -> None:
    """Compare the exact completed attempts, never a declared diversity label."""
    frames = [n for n in route.get("nodes", [])
              if n.get("id") in node.get("depends_on", [])
              and n.get("worker_type") == "frame" and n.get("dispatch_depth") == 1]
    if not frames:
        return
    import route_plan as RP
    if not RP.valid_frame_legs(route, [n.get("id") for n in frames]):
        raise DispatchContractError("frame-pair-incomplete", str(node.get("id")))
    try:
        lines = registry_lines if registry_lines is not None else jobs.read_text().splitlines()
    except OSError as exc:
        raise DispatchContractError("frame-attempt-unverifiable", "registry-unreadable") from exc
    attempts, harnesses = [], []
    for frame in frames:
        attempt = markers[frame["id"]].get("attempt_id")
        rows = []
        for line in lines:
            fields = line.split("\t")
            if len(fields) == 6:
                metadata = parse_registry_metadata(fields[5])
                if metadata.get("attempt_id") == attempt:
                    rows.append((fields, metadata))
        if len(rows) != 1:
            raise DispatchContractError("frame-attempt-unverifiable", str(attempt))
        fields, metadata = rows[0]
        try:
            identity = registered_node_identity(metadata, frame)
        except ValueError as exc:
            raise DispatchContractError("frame-attempt-unverifiable", str(attempt)) from exc
        if (fields[1] != "done" or metadata.get("note") != "completed-marker"
                or identity !=
                   (route.get("route_id"), route.get("route_hash") or "", frame["id"])
                or metadata.get("worker_type") != "frame"
                or metadata.get("harness") not in {"codex", "claude", "opencode"}):
            raise DispatchContractError("frame-attempt-unverifiable", str(attempt))
        attempts.append(attempt)
        harnesses.append(metadata["harness"])
    if len(set(attempts)) != len(frames):
        raise DispatchContractError("frame-attempt-duplicate", str(attempts))
    frame_harness_admission(route, jobs, lines, harnesses,
                            [f.get("model_profile") for f in frames])


def evidence_digest(evidence):
    """One sha256 over an artifact that may be a file OR a directory of files.

    A worker's artifact is legitimately either shape, and the envelope inspector
    accepts both, so the marker has to be able to name either. A directory is
    digested over its sorted root-relative paths and contents, so the value is
    stable across runs and changes when any member does. Symlinks are recorded by
    their target text and never followed: a marker must describe the tree it was
    given, not wherever that tree points today.

    Anything that cannot be attested raises rather than returning a digest. In
    particular a path that is neither a file nor a directory raises instead of
    yielding the empty-directory constant — otherwise "deleted before the gate"
    and "empty at completion" would verify as the same artifact. A non-regular
    member (FIFO, socket, device) is refused rather than read: reading a FIFO
    blocks forever, and a completion digest must not be able to wedge.

    SD-154 A-SD154-8: this is the ONE place non-writer code recomputes a
    completion marker's evidence sha256 -- `gate_currency` calls it once per
    currency judgement, and every reader that used to keep its own
    `hashlib.sha256(...read_bytes())` copy (`_marker_identity_row`) now reads
    `gate_currency`'s result instead. The writer allow-list
    (`_publish_completion_locked`, `publish_revision_locked`,
    `write_completion_marker`, `_existing_marker_replay`,
    `_completion_marker_replay`, `source_evidence_digest`) still calls this
    directly because a writer computing the digest it is about to seal is not
    the "recompute to re-verify" pattern the census closes.
    """
    from artifact_journal import split_reference, revision_digest
    if split_reference(evidence):
        return revision_digest(evidence)
    path=Path(evidence)
    if not os.path.lexists(path):
        try:
            from artifact_producer import resolve_placed_output
            path = resolve_placed_output(path)
        except (ImportError, OSError, ValueError):
            pass
    if path.is_symlink():
        raise ValueError(f"evidence-symlink-not-attestable:{path}")
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        raise ValueError(f"evidence-not-a-file-or-directory:{path}")
    # Lazy: codex_dispatch_terminal's own import chain reaches back into this
    # module (opencode_session_runtime -> dispatch_contract), so a top-level
    # import here would be circular.
    from codex_dispatch_terminal import directory_artifact_reason
    # The SAME rule the envelope inspector applies, imported rather than
    # restated: an empty or oversized directory must be refused at every door,
    # including the documented manual `complete --evidence` one.
    refusal=directory_artifact_reason(path)
    if refusal:
        raise ValueError(f"evidence-{refusal.removeprefix('artifact-')}:{path}")
    digest=hashlib.sha256(b"artifact-directory-v1\0")
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).parts):
        # `os.fsencode` and not `.encode("utf-8")`: a filename is bytes, and a
        # non-UTF-8 name arrives surrogate-escaped and would raise on encode.
        relative=os.fsencode(child.relative_to(path))
        if child.is_symlink():
            digest.update(b"L\0"+relative+b"\0"+os.fsencode(os.readlink(child)))
        elif child.is_dir():
            digest.update(b"D\0"+relative+b"\0")
        elif child.is_file():
            digest.update(b"F\0"+relative+b"\0")
            try:
                digest.update(hashlib.sha256(child.read_bytes()).digest())
            except OSError as exc:
                # Typed, never a traceback: this runs inside completion.
                raise ValueError(f"evidence-member-unreadable:{child}") from exc
        else:
            raise ValueError(f"evidence-member-not-regular:{child}")
    return digest.hexdigest()


def completion_marker_gate(
    route_file: str | None,
    route_node: str | None,
    action: str,
    agent_home: Path,
    jobs: Path | None = None,
    *,
    registry_lines: list[str] | None = None,
    attempt_id: str | None = None,
    saved_resource_admissions: dict | None = None,
    _raising_frame_gate: bool = False,
) -> None:
    """SD-56 decision gate for start and its read-only dry-run preview.

    Neither may admit a node whose ``depends_on`` predecessors lack current,
    linked completion evidence or whose own previous attempt has not stopped.

    ``agent_home`` is an explicit argument, not re-read from the environment,
    so the writer (capability-route.py complete) and every reader (this gate,
    called once per wrapper) are structurally forced to agree on one root.

    ``attempt_id`` is the identity about to launch. It is what makes "sibling"
    mean something: without it the caller's own freshly claimed row is the most
    recent row for this node and would block every launch.
    """

    if not route_file:
        return
    route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    contract_version = route.get("dispatch_contract_version") or route.get("broker_contract_version")
    contract_version = contract_version or 1
    if action in {"register", "start"} and contract_version != 3:
        raise DispatchContractError(
            "legacy-broker-route-read-only",
            f"dispatch contract v{contract_version} cannot register or start workers",
        )
    if action not in {"start", "dry-run"} or contract_version != 3:
        return
    node = next((row for row in route.get("nodes", []) if row.get("id") == route_node), None)
    if node is None:
        return
    _launch_completion_input(route, node)
    missing = []
    resource_actions: dict[str, str] = {}
    markers = {}
    proceeding = []
    blocked: list[tuple[str, AttemptReadiness]] = []
    for dep in node.get("depends_on", []):
        marker_path = next(
            (
                candidate
                for candidate in (
                    root / "completion" / route["route_id"] / f"{dep}.json"
                    for root in dispatch_state_roots(agent_home, jobs)
                )
                if candidate.is_file()
            ),
            None,
        )
        if marker_path is None:
            saved = (saved_resource_admissions or {}).get(dep)
            if isinstance(saved, dict):
                import resource_failure_resolution as FAILURE_RESOLUTION
                current = FAILURE_RESOLUTION.saved_output_admission(
                    route, route_node, saved.get("jobs"),
                    saved.get("owner_attempt_id"), saved.get("owner_session_id"))
                if current != saved:
                    raise DispatchContractError("saved-resource-output-changed", dep)
                continue
            missing.append(dep)
            _resource_predecessor_action(route, dep, resource_actions)
            continue
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DispatchContractError(
                "completion-marker-integrity-broken",
                f"{dep}:completion-marker-unreadable",
            )
        dep_node = next((row for row in route.get("nodes", []) if row.get("id") == dep), None)
        if dep_node is None:
            missing.append(dep)
            continue
        # `completion_marker_is_current` stays the boolean call site (several
        # tests mock it directly); `gate_currency` only runs -- and only
        # distinguishes "revised-unrecorded" from a harder failure -- once
        # that boolean says not-current.
        if not completion_marker_is_current(route, dep_node, marker_path, marker, jobs=jobs):
            currency = gate_currency(route, dep_node, marker_path, marker, jobs=jobs)
            if currency.state == "revised-unrecorded":
                # SD-154 rule 6: a route-state refusal, not "missing" -- the
                # dependency's gate evidence changed and needs `revise`, not
                # another launch of a predecessor that already ran.
                raise DispatchContractError(
                    "completion-evidence-revised-unrecorded", dep,
                    next_action=currency.next_action,
                )
            if completion_superseded(currency):
                # Execution may continue, but the old marker is omitted from the
                # admitted observations so no consumer can treat its superseded
                # PASS as current.
                continue
            if currency.state.startswith("integrity-broken:") or currency.state == "completion-evidence-unreadable":
                raise DispatchContractError(
                    "completion-marker-integrity-broken",
                    f"{dep}:{currency.state}:{currency.reason}",
                )
            missing.append(dep)
            if currency.state == "resource-stage-not-current":
                resource_actions[dep] = resource_stage_next_action(currency, dep)
            else:
                _resource_predecessor_action(route, dep, resource_actions)
            continue
        markers[dep] = marker
        proceeding.append((dep_node, marker_path, marker))
        readiness = completion_attempt_readiness(
            route,
            dep_node,
            marker,
            jobs or (resolve_dispatch_state_root(agent_home) / "jobs.log"),
            registry_lines=registry_lines,
        )
        if readiness.state != "ready":
            blocked.append((dep, readiness))
    if missing:
        # A marker beside a resource stage that is not current names its own step (never `revise`).
        raise DispatchContractError(
            "completion-marker-missing", ",".join(missing),
            next_action="; ".join(resource_actions.get(dep) or f"wait for node {dep} to publish a completion marker"
                                  for dep in missing) if resource_actions else None)
    if not blocked:
        _frame_pair_attempt_gate(route, node, markers,
                                 jobs or (resolve_dispatch_state_root(agent_home) / "jobs.log"),
                                 registry_lines)
    if _raising_frame_gate:
        import route_plan as RP
        if not RP.valid_frame_legs(route, node.get("depends_on", [])):
            raise DispatchContractError("frame-owner-entry-invalid", str(node.get("id")))
    else:
        _human_gate_entry_fence(route, node, jobs)
    _auxiliary_arbitration_gate(route, node, agent_home, jobs)
    if blocked:
        reason = (
            "predecessor-process-draining"
            if any(item.state == "draining" for _, item in blocked)
            else "predecessor-process-unverifiable"
        )
        detail = ",".join(
            f"{dep}:{item.attempt_id or '-'}:{item.reason}" for dep, item in blocked
        )
        raise DispatchContractError(reason, detail)
    _sibling_attempt_gate(
        route,
        route_node,
        jobs or (resolve_dispatch_state_root(agent_home) / "jobs.log"),
        registry_lines=registry_lines,
        attempt_id=attempt_id,
    )
    if action == "start":
        # The start proceeds on these predecessors: keep any gates-off edit of
        # their evidence as history. The dry-run preview writes nothing.
        for dep_node, marker_path, marker in proceeding:
            note_evidence_change(route, dep_node, marker_path, marker)


def _auxiliary_arbitration_gate(
    route: dict[str, object],
    node: dict[str, object],
    agent_home: Path,
    jobs: Path | None,
) -> None:
    """G1/AC 5: no node starts over an unarbitrated owner-merge group.

    A predecessor that is a member of an auxiliary-bearing group whose arbiter
    is the owner's merge record carries a second obligation beyond its own
    completion marker: the owner must have registered the merge record with
    `capability-route.py arbitrate` after the group joined. Without this the
    auxiliary findings are advisory output nobody is required to have read, and
    `autopilot-code`'s `execute` would start on an unmerged `plan-check`.
    """
    dependencies = [dep for dep in node.get("depends_on", [])]
    if not dependencies:
        return
    route_module = _route_module()
    owner_merge = route_module.owner_merge_auxiliary_groups(route)
    if not owner_merge:
        return
    dependency_groups = {
        row.get("parallel_group")
        for row in route.get("nodes", [])
        if isinstance(row, dict)
        and row.get("id") in dependencies
        and row.get("parallel_group")
    }
    unarbitrated = []
    unresolved = []
    for group_id, error in sorted(owner_merge.items()):
        if group_id not in dependency_groups:
            continue
        if error is not None:
            # M3: a route-integrity failure is a different event from "the owner
            # has not merged yet", and `arbitrate` cannot resolve it -- it raises
            # at the same point with the same error. Naming it
            # `auxiliary-arbitration-missing` sent the operator to a command that
            # cannot help. `terminal_gate_observation` already calls this state
            # `auxiliary-arbiter-unresolved`; the two consumers now agree.
            unresolved.append(f"{group_id}:{error}")
            continue
        basename = Path(
            str(route_module.arbitration_path(route["route_id"], group_id))
        ).name
        found = next(
            (
                candidate
                for candidate in (
                    root / "completion" / route["route_id"] / basename
                    for root in dispatch_state_roots(agent_home, jobs)
                )
                if candidate.is_file()
            ),
            None,
        )
        if found is None:
            # Absent under the handed root means refused. `_arbitration_observation`
            # falls back to `arbitration_path()`, which re-resolves the state root
            # from the environment, so passing `path=None` here would open the
            # spawn on a record this gate was never handed -- fail-open, and the
            # exact opposite of the one-root discipline this call site exists to
            # keep. `agent_home`/`jobs` are explicit arguments precisely so the
            # writer and every reader are structurally forced to agree on one
            # root, and `dispatch_state_root_rotation` makes rotation real rather
            # than theoretical.
            unarbitrated.append(group_id)
            continue
        row = route_module._arbitration_observation(
            route, group_id, error, path=found
        )
        if not row["passed"]:
            unarbitrated.append(group_id)
    if unresolved:
        raise DispatchContractError(
            "auxiliary-arbiter-unresolved", ",".join(unresolved)
        )
    if unarbitrated:
        raise DispatchContractError(
            "auxiliary-arbitration-missing", ",".join(unarbitrated)
        )


def _sibling_attempt_gate(
    route: dict[str, object],
    route_node: str | None,
    jobs: Path,
    *,
    registry_lines: list[str] | None = None,
    attempt_id: str | None = None,
) -> None:
    """Check the latest node execution using the shared lifecycle decision.

    This preflight is advisory to the atomic claim boundary. It cannot grant
    retry permission or override a committed result; it explains outstanding
    cleanup before the claimant reaches the jobs lock.
    """

    if registry_lines is None:
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
    else:
        lines = registry_lines
    sibling: tuple[list[str], dict[str, str]] | None = None
    # Parallel slices of one chain run side by side; only another chain's
    # (or a non-slice) attempt of this node can be a leaked prior execution.
    own_chain = ""
    if attempt_id:
        for line in lines:
            fields = line.split("\t")
            if len(fields) == 6:
                own = parse_registry_metadata(fields[5])
                if own.get("attempt_id") == attempt_id and own.get("subsession_mode") == "parallel":
                    own_chain = own.get("session_chain_id", "")
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if (
            metadata.get("route_id") != route.get("route_id")
            or metadata.get("route_node") != route_node
        ):
            continue
        candidate = metadata.get("attempt_id", "")
        if not candidate or candidate == (attempt_id or ""):
            continue
        if own_chain and metadata.get("session_chain_id") == own_chain:
            continue
        # Only the existing durable never-started fence can skip a predecessor.
        # Missing PID on a claimed/started launch is an observation gap.
        if attempt_row_never_started(fields):
            continue
        # Only the most recent sibling by registry order is authoritative; older
        # rows are its lineage, not independent claimants.
        sibling = (fields, metadata)
    if sibling is None:
        return
    sibling_fields, sibling_metadata = sibling
    sibling_status = sibling_fields[1]
    from recovery_evidence import observe_attempt
    evidence = observe_attempt(sibling_status, sibling_metadata, registry_rows=lines,
                               repo=sibling_fields[2], worktree=sibling_fields[3])
    process, decision = evidence.process, evidence.decision
    if decision.action not in {"wait", "recover"}:
        return
    reason = (
        "prior-attempt-still-live"
        if process.state == "live"
        else "prior-attempt-unverifiable"
    )
    raise DispatchContractError(
        reason,
        f"{route_node}:{sibling_metadata.get('attempt_id', '-')}:{decision.reason}",
    )


def _marker_schema_identity_ok(
    route: dict[str, object], node: dict[str, object], marker: dict[str, object],
    marker_path: Path, *, observe: bool = False,
) -> tuple[bool, Path | None]:
    """Schema/identity/history-byte checks shared by every marker generation
    (an ordinary marker, and -- recursively, SD-154 I-4 -- the marker a
    `stage_authority=revision` marker's `revision.of_sequence` points at).
    """
    if not isinstance(marker, dict) or marker.get("schema_version") != 2:
        return False, None
    # Recursive revision/source markers consume their own completion lineage,
    # even when the outer marker has no registered worker of its own.
    _launch_completion_input(route, node, marker)
    node_id = str(node["id"])
    sequence = int(marker.get("sequence", 0))
    if sequence < 1:
        return False, None
    expected = {
        "route_id": route.get("route_id"),
        "route_hash": route.get("route_hash"),
        "registry_digest": route.get("registry_digest"),
        "node_id": node_id,
        "completion_gate": node.get("completion_gate"),
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        if observe or any(key not in marker for key in expected):
            return False, None
        note_same_work_drift("completion-marker-identity-mismatch", node_id)
    history_path = marker_path.parent / f"{node_id}.{sequence}.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if history != marker:
        if observe or not isinstance(history, dict):
            return False, None
        note_same_work_drift("completion-marker-history-changed", node_id)
    return True, history_path


def original_review_self_claim(route, node, origin, *, jobs, metadata, require_receipt=False):
    """Read the claim made by the original publication, never the replay claim.

    New publications retain their input in history. The legacy Codex command
    receipt is usable only when its successful output contains this exact
    original marker and its command binds every completion input. Do not read
    arbitrary transcripts or infer a claim from degraded reviewer_identity.
    """
    claim = {"kind": "registered-worker", "attempt_id": origin.get("attempt_id")}
    if origin.get("reviewer_claim") == claim and not require_receipt:
        return {"source": "original-marker"}
    if "reviewer_claim" in origin and origin.get("reviewer_claim") != claim:
        return None
    log = metadata.get("log_file")
    if not log:
        return None
    matches = []
    for line in Path(log).read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            item = event.get("item") or {}
            if (event.get("type") != "item.completed"
                    or item.get("type") != "command_execution"
                    or item.get("status") != "completed" or item.get("exit_code") != 0):
                continue
            tokens = shlex.split(item.get("command") or "")
            # Native Codex command receipts wrap the command in a shell -lc.
            if len(tokens) == 3 and tokens[1] in {"-lc", "-c"}:
                tokens = shlex.split(tokens[2])
            if len(tokens) < 3 or Path(tokens[1]).name != "capability-route.py" or tokens[2] != "complete":
                continue
            arguments = tokens[3:]
            if len(arguments) % 2 or len(set(arguments[::2])) != len(arguments[::2]):
                continue
            args = dict(zip(arguments[::2], arguments[1::2]))
            required = {"--route", "--node", "--jobs", "--attempt-id", "--reviewer-attempt", "--evidence"}
            axis_flags = {"--dispatch-depth": "dispatch_depth", "--transport": "transport",
                          "--execution-surface": "execution_surface", "--registered-worker": "registered_worker",
                          "--fallback-hop": "fallback_hop"}
            if not required <= set(args) or set(args) - required - set(axis_flags):
                continue
            if any(str(args[flag]).lower() != str(origin.get(key)).lower()
                   and not (key == "registered_worker" and args[flag] == "1" and origin.get(key) is True)
                   for flag, key in axis_flags.items() if flag in args):
                continue
            if (args["--node"] != node["id"]
                    or args["--attempt-id"] != origin.get("attempt_id")
                    or args["--reviewer-attempt"] != origin.get("attempt_id")
                    or Path(args["--jobs"]).resolve() != Path(jobs).resolve()
                    or Path(args["--evidence"]).resolve() != Path(origin["evidence"]["path"]).resolve()):
                continue
            sealed = json.loads(Path(args["--route"]).read_text(encoding="utf-8"))
            sealed = _route_module().verify_route(sealed)
            if (sealed["route_id"], sealed["route_hash"]) != (route["route_id"], route["route_hash"]):
                continue
            output_records = []
            for output in item.get("aggregated_output", "").splitlines():
                try:
                    output_records.append(json.loads(output))
                except ValueError:
                    pass
            if (origin not in output_records or
                    {"attempt_id": origin["attempt_id"], "status": "closed"} not in output_records):
                continue
            matches.append({"source": "runtime-command", "log_file": str(Path(log)),
                            "item_id": item.get("id"),
                            "receipt_sha256": hashlib.sha256(line.encode()).hexdigest()})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    # Started/completed are distinct; multiple successful original publications
    # are ambiguous rather than a license to select a convenient late claim.
    return matches[0] if len(matches) == 1 else None


REVIEW_SELF_PROVENANCE_BLOCKER = (
    "v3.28.6 closed the completion but downgraded reviewer independence because "
    "the attempt had no terminal verdict at registration; an independent stage marker was not established."
)


def _review_report_hash_receipt(command, report, evidence):
    """Recognize a public hash read, without executing or retaining shell text.

    Support a literal sha256sum argv and the historical report-append command:
    literal F assignment, installed write preflight, one heredoc append, then
    sha256sum. No general shell expansion, arbitrary stdout or script execution
    can stand in for a registered byte receipt.
    """
    tokens = shlex.split(command)
    if len(tokens) == 3 and tokens[1] in {"-lc", "-c"}:
        if Path(tokens[0]).name not in {"sh", "bash", "zsh"}:
            return False
        command = tokens[2]
    lines = command.splitlines()
    variable = None
    if len(lines) > 1:
        # The actual append receipt uses an unquoted EOF body; inspect only
        # its command structure. Never evaluate the body or copy its preimage.
        lexer = shlex.shlex(lines[0], posix=True, punctuation_chars=";<>")
        lexer.whitespace_split = True
        first = list(lexer)
        if (len(first) != 12 or not first[0].startswith("F=")
                or first[1] != ";" or not first[2].endswith("/adapters/codex/bin/preflight.sh")
                or first[3:5] != ["write", "$F"] or first[6:] != [";", "cat", ">>", "$F", "<<", "EOF"]):
            return False
        variable = first[0][2:]
        if (not variable or any(c in variable for c in "$`\n")
                or Path(variable).resolve() != report.resolve()):
            return False
        # Only a single heredoc followed by one hash command is supported.
        if lines[-2] != "EOF" or "EOF" in lines[1:-2]:
            return False
        command = lines[-1]
    argv = shlex.split(command)
    if len(argv) != 3 or argv[0] != "sha256sum":
        return False
    args = [variable if arg == "$F" and variable else arg for arg in argv[1:]]
    return (all(Path(arg).is_absolute() and not any(c in arg for c in "$`\n") for arg in args)
            and [Path(arg).resolve() for arg in args] == [report.resolve(), evidence.resolve()])


def review_self_blocked_basis(route, node, prior, metadata, terminal, *, jobs):
    """One historical provenance-only BLOCKED basis for writer and readers."""
    from codex_dispatch_terminal import inspect_terminal_log
    view = inspect_terminal_log(metadata.get("log_file"))
    if (not view or view["blocker"] != REVIEW_SELF_PROVENANCE_BLOCKER
            or terminal.get("review_artifact_verdict") in {"FAIL", "BLOCKED"}):
        raise ValueError("review-self-disposition-terminal-unverified:blocked-not-provenance")
    claim = original_review_self_claim(route, node, prior, jobs=jobs, metadata=metadata, require_receipt=True)
    # This historical exception needs the actual successful normal command,
    # not a newly recorded self claim or the registry's success note alone.
    if not claim or claim.get("source") != "runtime-command" or not claim.get("item_id"):
        raise ValueError("review-self-disposition-blocked-receipt-unverified")
    evidence = Path(prior["evidence"]["path"])
    content = json.loads(evidence.read_text())
    root = Path(route["artifact_root"]).resolve()
    output = next((p for p in evidence.parents if p.name == "artifacts" and p.is_relative_to(root)), None)
    if output is None:
        raise ValueError("review-self-disposition-report-binding-invalid")
    cycle = json.loads((output.parent / ".cycle.json").read_text())
    import artifact_producer
    import artifact_locator
    cycle_record = artifact_producer.read_cycle_record(root, cycle.get("cycle_id")) or {}
    locator = content.get("review_artifact")
    if not isinstance(locator, str) or not locator or any(c in locator for c in "\n\r\x00"):
        raise ValueError("review-self-disposition-report-binding-invalid")
    report = (output / locator).resolve()
    encoded = str(terminal.get("artifact_path_b64") or "")
    primary = Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
    census = prior.get("round_census") or {}
    if (not report.is_relative_to(output.resolve()) or report == evidence.resolve()
            or not agent_home_equivalent(primary, report)
            or content.get("guard_session_id") != prior["attempt_id"]
            or content.get("cycle_id") != cycle.get("cycle_id")
            or content.get("route_cycle_id") != cycle.get("cycle_id")
            or content.get("stage") != node["completion_gate"]
            or content.get("completion_gate") != node["completion_gate"]
            or content.get("route_id") != route["route_id"]
            or any(content.get(k, v) != v for k, v in
                   (("route_hash", route["route_hash"]), ("node_id", node["id"])))
            or type(content.get("round")) is not int
            or content["round"] != census.get("verdict_rounds", 0) + census.get("verdictless_rounds", 0)
            or content.get("verdict_sequence") != census.get("verdict_rounds")
            or content.get("verdict_ceiling") != census.get("cap", 0) + 1
            or census.get("closure_class") != "closure-check"
            or content.get("blocker") != "none"):
        raise ValueError("review-self-disposition-report-binding-invalid")
    located = artifact_locator.locate(root, cycle["cycle_id"], candidates=lambda: [output.parent], heal=False)
    if ((cycle_record.get("route_id"), cycle_record.get("route_hash")) != (route["route_id"], route["route_hash"])
            or located is None or located.resolve() != output.parent.resolve()):
        raise ValueError("review-self-disposition-cycle-binding-invalid")
    # The unchanged prepared request is independently bound by its normal
    # strict reader; never trust a prose or boolean claim instead of its pins.
    from ship_parent_deploy import load_request
    request, _ = load_request(Path(content.get("request_path", "")))
    if (request.get("route_id") != route["route_id"]
            or request.get("route_hash") != route["route_hash"]
            or prior["attempt_id"] in request.get("prior_review_attempts", [])
            or datetime.fromisoformat(request["prepared_at"]) > datetime.fromisoformat(prior["completed_at"].replace("Z", "+00:00"))
            or content.get("request_digest") != request.get("request_digest")
            or content.get("request_prepared_at") != request.get("prepared_at")
            or content.get("request_digest_verified") is not True
            or content.get("prepare_receipt_matches") is not True
            or content.get("bound_input_mismatches") != []
            or content.get("bound_inputs_verified") != len(request["command"]["inputs"])
            or Path(request.get("jobs", "")).resolve() != Path(jobs).resolve()
            or any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha
                   for path, sha in request["command"]["inputs"].items())):
        raise ValueError("review-self-disposition-request-binding-invalid")
    records = []
    for line in report.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    closed = {"attempt_id": prior["attempt_id"], "status": "closed"}
    if (records.count(prior) != 1 or records.count(closed) != 1
            or any(isinstance(record, dict) and (
                record.get("verdict") in {"FAIL", "BLOCKED"} or record.get("gate_met") is False
                or (("completion_gate" in record or record.get("status") == "closed") and record not in (prior, closed)))
                   for record in records)):
        raise ValueError("review-self-disposition-report-receipt-invalid")
    lines = Path(metadata["log_file"]).read_text().splitlines()
    events = []
    for line in lines:
        try:
            events.append((line, json.loads(line)))
        except ValueError:
            continue
    publication = next((i for i, (line, _) in enumerate(events)
                        if hashlib.sha256(line.encode()).hexdigest() == claim["receipt_sha256"]), None)
    # Exact terminal parsing above already proves the envelope. Bind its public
    # event/message bytes as well as chronology, without copying private text.
    ends = [i for i, (_, e) in enumerate(events) if e.get("type") in {"turn.completed", "result", "step_finish"}]
    end = ends[-1] if ends else -1
    if publication is None or end <= publication:
        raise ValueError("review-self-disposition-terminal-chronology-invalid")
    message = end
    if terminal["source"] == "exact-turn-completed":
        message = next((i for i in range(end - 1, publication, -1)
                        if events[i][1].get("type") == "item.completed"
                        and (events[i][1].get("item") or {}).get("type") == "agent_message"), -1)
    elif terminal["source"] == "exact-step-finish-stop":
        message = next((i for i in range(end - 1, publication, -1)
                        if events[i][1].get("type") == "text"), -1)
    if message <= publication:
        raise ValueError("review-self-disposition-terminal-chronology-invalid")
    report_digest = hashlib.sha256(report.read_bytes()).hexdigest()
    expected = {str(report): report_digest, str(evidence.resolve()): prior["evidence"]["sha256"]}
    receipts = []
    for index, (line, event) in enumerate(events):
        item = event.get("item") or {}
        if (not publication < index < message or event.get("type") != "item.completed"
                or item.get("type") != "command_execution" or item.get("status") != "completed"
                or item.get("exit_code") != 0 or not item.get("id")):
            continue
        try:
            if not _review_report_hash_receipt(item.get("command") or "", report, evidence):
                continue
            digests = {}
            for output_line in (item.get("aggregated_output") or "").splitlines():
                digest, path = output_line.split("  ", 1)
                resolved_path = str(Path(path).resolve())
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or resolved_path in digests:
                    raise ValueError("invalid digest output")
                digests[resolved_path] = digest
            receipts.append((digests, {"item_id": item.get("id"), "receipt_sha256": hashlib.sha256(line.encode()).hexdigest()}))
        except (TypeError, ValueError):
            continue
    if len(receipts) != 1 or receipts[0][0] != expected:
        raise ValueError("review-self-disposition-report-bytes-unverified")
    claim["log_file"] = str(Path(claim["log_file"]).resolve())
    view["log_file"] = str(Path(view["log_file"]).resolve())
    return {"report_path": str(report), "report_sha256": report_digest,
            "publication_receipt": claim,
            "request_path": str(Path(content["request_path"]).resolve()),
            "cycle_record_path": str(artifact_producer.cycle_record_path(root, cycle["cycle_id"])),
            "cycle_binding_path": str(output.parent / ".cycle.json"),
            "hash_receipt": receipts[0][1], "terminal_verdict": "BLOCKED",
            "terminal_source": terminal["source"],
            "terminal_sha256": hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest(),
            "terminal_message_sha256": hashlib.sha256(events[message][0].encode()).hexdigest(),
            "terminal_event_sha256": hashlib.sha256(events[end][0].encode()).hexdigest()}


def review_self_disposition_subject(route, node, prior, marker_path, *, jobs, authority_basis=None):
    """Read the exact settled judgment; never let a disposition select its log."""
    module = _route_module()
    jobs = Path(jobs)
    canonical = marker_path.parent / f"{node['id']}.json"
    if (jobs.name != "jobs.log" or not jobs.is_file()
            or not agent_home_equivalent(module.completion_dir(route['route_id'], jobs=jobs), marker_path.parent)):
        raise ValueError("review-self-disposition-jobs-invalid")
    lines = jobs.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) == 6:
            meta = parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == prior["attempt_id"]:
                rows.append((fields, meta))
    if len(rows) != 1:
        raise ValueError("review-self-disposition-row-not-exact")
    fields, meta = rows[0]
    axes = module._marker_attempt_axes(node, prior["attempt_id"], meta)
    if (fields[1] != "done" or meta.get("worker_type") != "review"
            or row_is_subsession(meta) or not verdict_pass(meta) or terminal_conflict_pending(meta)
            or registered_node_identity(meta, node) != (route["route_id"], route["route_hash"], node["id"])
            or any(prior.get(k) != v for k, v in axes.items())
            or Path(fields[3]).resolve() != Path(route["cwd"]).resolve()
            or not agent_home_equivalent(meta.get("completion_marker", ""), canonical)
            or not agent_home_equivalent(meta.get("completion_marker_history", ""),
                     marker_path.parent / f"{node['id']}.{prior['sequence']}.json")):
        raise ValueError("review-self-disposition-row-binding-invalid")
    readiness = completion_attempt_readiness(route, node, prior, jobs, registry_lines=lines, observe=True)
    if readiness.state != "ready":
        raise ValueError("review-self-disposition-not-settled:" + readiness.reason)
    if module._registered_marker_fence(route, node, prior, lines) is not None:
        raise ValueError("review-self-disposition-subject-superseded")
    from codex_dispatch_terminal import inspect_terminal_attempt
    terminal = inspect_terminal_attempt(meta.get("log_file"), worktree=fields[3],
                    artifact_root_metadata=meta.get("artifact_root"), worker_type="review",
                    artifact_binding=meta)
    if (terminal.get("state") != "valid" or terminal.get("verdict") not in {"PASS", "BLOCKED"}
            or terminal.get("artifact_state") != "readable" or terminal.get("review_verdict_conflict")):
        raise ValueError("review-self-disposition-terminal-unverified")
    encoded = str(terminal.get("artifact_path_b64") or "")
    artifact = Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
    evidence = Path(prior["evidence"]["path"])
    if ((terminal.get("verdict") == "PASS" and not agent_home_equivalent(artifact, evidence))
            or evidence_currency(route, node, marker_path, prior, observe=True).state != "current"):
        raise ValueError("review-self-disposition-terminal-artifact-mismatch")
    content = json.loads(evidence.read_text(encoding="utf-8"))
    if (not isinstance(content, dict) or content.get("verdict") != "PASS" or content.get("findings") != [] or content.get("gate_met") is not True
            or content.get("route_id", route["route_id"]) != route["route_id"]
            or content.get("completion_gate", node["completion_gate"]) != node["completion_gate"]):
        raise ValueError("review-self-disposition-judgment-invalid")
    if terminal.get("verdict") == "BLOCKED":
        basis = review_self_blocked_basis(route, node, prior, meta, terminal, jobs=jobs)
        if authority_basis is not None:
            authority_basis.update(basis)
    return fields, meta


def review_self_disposition_lineage(route, jobs, metadata):
    """Verified historical owners followed by the current owner, without liveness policy."""
    from dispatch_replacement import effective_attempts
    actor = metadata.get("parent_attempt_id")
    lineage = []
    for _ in range(32):
        if not actor or actor in lineage:
            raise ValueError("review-self-disposition-parent-lineage-invalid")
        rows = []
        for line in Path(jobs).read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 6:
                meta = parse_registry_metadata(fields[5])
                if meta.get("attempt_id") == actor:
                    rows.append(meta)
        if len(rows) != 1:
            raise ValueError("review-self-disposition-actor-not-exact")
        owner = rows[0]
        if (owner.get("worker_type") != "owner" or owner.get("dispatch_depth") != "1"
                or owner.get("registered_worker") != "1"
                or (owner.get("owner_route_id"), owner.get("owner_route_hash")) !=
                   (route["route_id"], route["route_hash"])):
            raise ValueError("review-self-disposition-actor-route-invalid")
        sealed = _route_module().verify_route(json.loads(Path(owner["owner_route_file"]).read_text()))
        if (sealed["route_id"], sealed["route_hash"]) != (route["route_id"], route["route_hash"]):
            raise ValueError("review-self-disposition-actor-route-invalid")
        lineage.append(actor)
        effective, mapping = effective_attempts(jobs, {actor})
        if not mapping:
            return lineage
        if len(effective) != 1:
            raise ValueError("review-self-disposition-parent-lineage-invalid")
        actor = next(iter(effective))
    raise ValueError("review-self-disposition-parent-lineage-invalid")


def review_self_disposition_origin(route, node, marker, marker_path):
    """Validate the sole origin-link exception for a named-self disposition.

    Returns the original marker/path. Every other field is compared as a full
    copy, preserving judgment, evidence, request bindings and round census.
    Readers and both publication entrances share this decision.
    """
    disposition = marker.get("reviewer_disposition")
    if not isinstance(disposition, dict) or disposition.get("kind") != "registered-named-self":
        raise ValueError("review-self-disposition-invalid")
    sequence = disposition.get("of_sequence")
    if (type(sequence) is not int or sequence < 1
            or type(marker.get("sequence")) is not int or sequence >= marker["sequence"]):
        raise ValueError("review-self-disposition-sequence-invalid")
    prior_path = marker_path.parent / f"{node['id']}.{sequence}.json"
    prior_bytes = prior_path.read_bytes()
    if hashlib.sha256(prior_bytes).hexdigest() != disposition.get("of_marker_sha256"):
        raise ValueError("review-self-disposition-origin-changed")
    prior = json.loads(prior_bytes)
    ok, _ = _marker_schema_identity_ok(route, node, prior, prior_path, observe=True)
    if (not ok or node.get("kind") != "review-worker"
            or prior.get("reviewer_kind") != "owner-inline"
            or prior.get("review_independence") != "degraded"
            or prior.get("reviewer_downgrade_reason") != "reviewer-attempt-no-terminal-verdict"
            or not prior.get("attempt_id") or prior.get("reviewer_identity") != prior["attempt_id"]
            or prior.get("registered_worker") is not True
            or any(k in prior for k in ("stage_authority", "state", "review_gate_closure", "reviewer_disposition"))):
        raise ValueError("review-self-disposition-origin-invalid")
    link_path = marker_path.parent / f"{node['id']}.{prior['attempt_id']}.attempt.json"
    if (hashlib.sha256(link_path.read_bytes()).hexdigest() != disposition.get("origin_link_sha256")
            or not _marker_link_current(route, node, prior, prior_path)):
        raise ValueError("review-self-disposition-link-invalid")
    expected = dict(prior, sequence=marker["sequence"], reviewer_kind="registered-worker",
                    review_independence="independent", reviewer_disposition=disposition)
    expected.pop("reviewer_downgrade_reason", None)
    if expected != marker:
        raise ValueError("review-self-disposition-content-changed")
    keys = {"kind", "of_sequence", "of_marker_sha256", "origin_link_sha256",
            "original_claim", "jobs", "actor", "settled_basis", "recorded_at"}
    blocked = disposition.get("settled_basis") == "historical-provenance-blocked"
    if blocked:
        keys.add("authority_basis")
    if (set(disposition) != keys
            or any(not isinstance(disposition.get(k), str) or not disposition[k]
                   for k in ("actor", "recorded_at", "jobs"))
            or disposition.get("settled_basis") not in {"successful-quiescent-terminal", "historical-provenance-blocked"}
            or not isinstance(disposition.get("original_claim"), dict)):
        raise ValueError("review-self-disposition-basis-invalid")
    recorded = datetime.fromisoformat(disposition["recorded_at"].replace("Z", "+00:00"))
    completed = datetime.fromisoformat(prior["completed_at"].replace("Z", "+00:00"))
    if recorded.tzinfo is None or not completed <= recorded <= datetime.now(timezone.utc):
        raise ValueError("review-self-disposition-time-invalid")
    authority_basis = {}
    _, metadata = review_self_disposition_subject(route, node, prior, prior_path, jobs=disposition["jobs"],
                                                authority_basis=authority_basis)
    if ((blocked and (not authority_basis or disposition["authority_basis"] != authority_basis))
            or (not blocked and authority_basis)):
        raise ValueError("review-self-disposition-basis-invalid")
    lineage = review_self_disposition_lineage(route, disposition["jobs"], metadata)
    if disposition["actor"] not in lineage:
        raise ValueError("review-self-disposition-actor-invalid")
    actor_index = lineage.index(disposition["actor"])
    times = {}
    for line in Path(disposition["jobs"]).read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            aid = parse_registry_metadata(fields[5]).get("attempt_id")
            if aid in lineage:
                times[aid] = datetime.fromisoformat(fields[0].replace("Z", "+00:00"))
    if (times[lineage[actor_index]] > recorded
            or (actor_index + 1 < len(lineage) and times[lineage[actor_index + 1]] <= recorded)):
        raise ValueError("review-self-disposition-actor-time-invalid")
    basis = disposition["original_claim"]
    observed = original_review_self_claim(route, node, prior, jobs=disposition["jobs"], metadata=metadata)
    # Supported path aliases do not change which registered log supplied the receipt.
    if observed and observed.get("source") == "runtime-command" and basis.get("source") == "runtime-command":
        if agent_home_equivalent(observed["log_file"], basis.get("log_file", "")):
            observed["log_file"] = basis["log_file"]
    if observed != basis:
        raise ValueError("review-self-disposition-claim-invalid")
    return prior, prior_path


def _marker_link_current(
    route: dict[str, object], node: dict[str, object], marker: dict[str, object],
    marker_path: Path,
) -> bool:
    """Owner-closure/resource-runner/owner-chain/attempt-link shape checks.

    Reads only the marker's OWN recorded evidence sha256 (never the live
    evidence file), so this is safe to call on a historical marker whose
    evidence path may since have been overwritten by a later revision --
    exactly the recursive "marker k" check SD-154 I-4 needs from
    `gate_currency`.
    """
    node_id = str(node["id"])
    evidence_record = marker.get("evidence") or {}

    if "retained_review" in marker:
        from retained_review_completion import verify_marker
        verify_marker(route, node, marker, marker_path,
                      historical=marker_path.name != f"{node['id']}.json")
        return True

    if "reviewer_disposition" in marker:
        review_self_disposition_origin(route, node, marker, marker_path)
        return True

    if marker.get("stage_authority") == "owner-closure":
        _route_module().validate_continuation_owner_closure(route, node, marker)
        if not (
            marker.get("dispatch_depth") == node.get("dispatch_depth")
            and marker.get("transport") == "headless"
            and marker.get("execution_surface") == "inline"
            and marker.get("fallback_hop") == "inline"
        ):
            return False
        # Continue through the shared immutable attempt-link check below.
        # A crash after marker publication but before its link is a hold.

    if node.get("kind") == "resource-runner":
        return (
            marker.get("attempt_id") is None
            and marker.get("dispatch_depth") is None
            and marker.get("transport") is None
            and marker.get("execution_surface") is None
            and marker.get("registered_worker") is False
            and marker.get("fallback_hop") is None
        )

    if marker.get("stage_authority") == "owner-chain":
        manifest_path = Path(str(marker.get("subsession_manifest", "")))
        if (
            not manifest_path.is_absolute()
            or not manifest_path.is_file()
            or hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            != marker.get("subsession_manifest_sha256")
        ):
            return False
        manifest = load_manifest(manifest_path, node=node)
        return (
            manifest.get("route_id") == route.get("route_id")
            and manifest.get("route_hash") == route.get("route_hash")
            and manifest.get("chain_id") == marker.get("session_chain_id")
            and marker.get("attempt_id")
            == "att-stage-" + str(marker.get("subsession_manifest_sha256"))[:32]
            and marker.get("dispatch_depth") == node.get("dispatch_depth")
            and marker.get("transport") == "headless"
            and marker.get("execution_surface") == "inline"
            and marker.get("registered_worker") is False
            and marker.get("fallback_hop") == "inline"
        )

    attempt_id = marker.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return False
    axes = {
        "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
        "dispatch_depth": marker.get("dispatch_depth"),
        "transport": marker.get("transport"),
        "execution_surface": marker.get("execution_surface"),
        "registered_worker": marker.get("registered_worker"),
        "fallback_hop": marker.get("fallback_hop") or "",
    }
    validate_attempt_metadata(axes)
    if int(axes["dispatch_depth"]) != node.get("dispatch_depth"):
        return False
    safe_attempt = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in attempt_id
    )
    link_path = marker_path.parent / f"{node_id}.{safe_attempt}.attempt.json"
    link = json.loads(link_path.read_text(encoding="utf-8"))
    link_expected = {
        "schema_version": 2,
        "route_id": route.get("route_id"),
        "node_id": node_id,
        "attempt_id": attempt_id,
        "dispatch_depth": marker.get("dispatch_depth"),
        "transport": marker.get("transport"),
        "execution_surface": marker.get("execution_surface"),
        "registered_worker": marker.get("registered_worker"),
        "fallback_hop": marker.get("fallback_hop"),
        "evidence_sha256": evidence_record.get("sha256"),
    }
    if marker.get("stage_authority") == "owner-closure":
        link_expected.update(stage_authority="owner-closure", owner_closure_proof=marker["owner_closure_proof"])
    if not all(link.get(key) == value for key, value in link_expected.items()):
        return False
    # The link records its own absolute location as written by the marker
    # writer, whose env may spell the same directory in pointer form while
    # this reader resolved it (or vice versa). Identity, not spelling, is
    # the contract, so compare through agent_home_equivalent -- the
    # comparison-site normalizer this module defines for exactly this.
    sequence = int(marker.get("sequence", 0))
    history_path = marker_path.parent / f"{node_id}.{sequence}.json"
    canonical_path = marker_path.parent / f"{node_id}.json"
    for key, expected_path in (
        ("completion_marker", canonical_path),
        ("completion_marker_history", history_path),
    ):
        recorded = link.get(key)
        if not isinstance(recorded, str) or not agent_home_equivalent(
            recorded, expected_path
        ):
            return False
    return True


class GateCurrency(NamedTuple):
    """SD-154 rule 5: the one currency judgement every reader/consumer reads.

    ``state`` is one of ``current`` | ``superseded`` | ``revised-unrecorded``
    | ``integrity-broken:<reason>`` | ``completion-evidence-unreadable``.
    ``next_action`` is set for route-state evidence refusals.
    """

    state: str
    reason: str
    next_action: str | None = None
    evidence_digest: str | None = None


# Currency states an approved fix's answered verdict may show when it is reopened: the
# verdict is not reused, so an evidence or input change needs no revision first. Integrity and
# resource-stage refusals keep their own handling.
ANSWERED_CURRENCY_STATES = frozenset({"current", "superseded", "revised-unrecorded",
                                      "completion-evidence-unreadable"})


def completion_superseded(currency: GateCurrency, *, answered: bool = False) -> bool:
    """Whether a completion is no longer the current result of its node (U8c, the one answer).

    A source- or input-bound PASS that is stale or unbound, one whose upstream completed again
    after it, and a marker an upstream revision tombstoned are all superseded. ``answered``
    adds the verdict an approved fix answered, which is reopened whatever evidence change it
    shows. An integrity break is never superseded: it keeps its refusal. Every consumer drops
    such a marker from its observations and reopens the node; none reuses it as current.
    """
    if currency.state.startswith("integrity-broken:"):
        return False
    if currency.state == "superseded":
        return True
    return answered and currency.state in ANSWERED_CURRENCY_STATES


# `resource-stage-not-current` reasons. Unsettled is an observation: the readable ledger records
# the supervised stage in a state other than STAGE_SUCCEEDED (or its success was retracted).
# Unobservable is a record that exists but could not be read right now. Everything else (a settled
# stage whose proof does not match, a missing or malformed record) keeps `completion-marker-missing`.
RESOURCE_STAGE_UNSETTLED = "resource-stage-unsettled"
RESOURCE_STAGE_UNOBSERVABLE = "resource-stage-unobservable"


def _resource_predecessor_action(route, dep: str, actions: dict) -> None:
    """A resource predecessor with no current marker is continued, never waited on."""
    node = next((row for row in route.get("nodes", []) if row.get("id") == dep), None)
    if node is not None and node.get("kind") == "resource-runner":
        actions[dep] = resource_stage_next_action(
            GateCurrency("resource-stage-not-current", RESOURCE_STAGE_UNSETTLED), dep)


def resource_stage_unsettled(currency: GateCurrency) -> bool:
    """Whether a marker stands beside a resource stage observed not to have settled as succeeded.

    Such a marker is not the stage's result. The launch gate reads it as missing, as it reads every
    not-current resource marker, and a replacement owner continues that stage. Nothing else is read
    this way: a proof mismatch or an unreadable record stays unknown and keeps its refusal.
    """
    return (currency.state == "resource-stage-not-current"
            and currency.reason == RESOURCE_STAGE_UNSETTLED)


def resource_stage_unobservable(currency: GateCurrency) -> bool:
    """Whether a resource stage's records could not be read right now: unknown, never unsettled."""
    return (currency.state == "resource-stage-not-current"
            and currency.reason == RESOURCE_STAGE_UNOBSERVABLE)


def resource_stage_next_action(currency: GateCurrency, node_id: str) -> str:
    """The supported step for a marker beside a resource stage that is not current.

    `revise` refuses such a marker, so it is never the step.
    """
    if resource_stage_unsettled(currency):
        return (f"continue resource stage {node_id}: its runs have not settled as succeeded, so run its "
                "next registered resource; the next start of its successor after a successful run settles it")
    if resource_stage_unobservable(currency):
        return f"retry the same command when the run records of resource stage {node_id} can be read again"
    return (f"inspect the run records and completion marker of resource stage {node_id}: its settled "
            "run's proof did not match, or a run record is missing or malformed")


def _completion_instant(marker: Mapping[str, object]):
    # An unreadable, timezone-less or otherwise unorderable value cannot be placed against
    # another completion, so it is no instant (never a reason to call a result older).
    try:
        instant = datetime.fromisoformat(str(marker["completed_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return None
    return instant if instant.tzinfo is not None and instant.utcoffset() is not None else None


def _upstream_completed_later(route, node, marker, marker_path):
    """The upstream node that completed again after this marker was written, or None.

    A node's completion is the result of the nodes it depends on (directly or through others).
    When one of them has a later completion (a new attempt's new sequence), this one is the
    result of an older upstream, however unchanged its own evidence is. Not a new completion:
    an upstream's first marker (a resource stage's owner settlement writes it after the
    dependents that already consumed its accepted result), an upstream revision (its
    dependents carry their own tombstone or restored marker), a tombstone, or a marker that
    cannot be read or dated. Only markers beside this one are read.
    A result bound to a source or input subject is judged by that subject (`marker_currency`),
    not by when its upstream last ran.
    """
    if isinstance(marker.get("source_subject"), dict):
        return None
    mine = _completion_instant(marker)
    if mine is None:
        return None
    by_id = {str(row.get("id")): row for row in route.get("nodes", []) if isinstance(row, dict)}
    pending = [str(dep) for dep in node.get("depends_on", [])]
    seen = set()
    while pending:
        upstream = pending.pop()
        if upstream in seen or upstream not in by_id:
            continue
        seen.add(upstream)
        pending.extend(str(dep) for dep in by_id[upstream].get("depends_on", []))
        try:
            later = json.loads((marker_path.parent / f"{upstream}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (not isinstance(later, dict) or later.get("node_id") != upstream
                or type(later.get("sequence")) is not int or later["sequence"] < 2
                or later.get("state") == "superseded-by-upstream-revision"
                or later.get("stage_authority") == "revision"):
            continue
        completed = _completion_instant(later)
        if completed is not None and completed > mine:
            return upstream, later.get("sequence")
    return None


def _older_than_upstream(route, node, marker, marker_path):
    """``superseded`` when an upstream node completed again after this marker, else None.

    An older result of a node whose upstream has completed again is not current in any gate
    mode: the node is reopened, not read as done.
    """
    newer = _upstream_completed_later(route, node, marker, marker_path)
    if newer is None:
        return None
    action = route_state_next_action(
        "completion-evidence-superseded", str(node.get("id")), None, node,
    ) + f" (upstream {newer[0]}@{newer[1]} completed again)"
    return GateCurrency("superseded", "completion-evidence-superseded", action)


def reopened_output_path(route, node, jobs, output_dir) -> str | None:
    """Where a reopened node writes its new result so it does not overwrite the old one (C2).

    A node run again because its result became historical (`completion_superseded`), or run on a
    continuation of a route that completed it, whose sealed output is one exact file that still holds
    an earlier result of that node, gets the output's next revision path (`<stem>.r<N><suffix>`, N one
    past the node's highest completion sequence along the route's verified lineage). The path is
    derived from the node's own sealed output, so it is inside the approved scope (`artifact_context`),
    and the earlier result stays readable. Any other node, or a current result, gets None.
    """
    outputs = [value for value in node.get("outputs") or () if isinstance(value, str)]
    if (len(outputs) != 1 or not output_dir or re.search(r"[*?\[<]", outputs[0])
            or not Path(outputs[0]).suffix):
        return None
    held = Path(output_dir) / outputs[0]
    node_id = str(node["id"])
    try:
        if not held.is_file():
            return None
        module = _route_module()
        own = module.completion_dir(route["route_id"], jobs=jobs)
        try:
            marker_path = own / f"{node_id}.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            marker = None
        if marker is not None and not completion_superseded(
                gate_currency(route, node, marker_path, marker, observe=True, jobs=jobs)):
            return None
        try:
            from route_lineage import RouteLineageError, verified_route_lineage
            lineage = verified_route_lineage(route, observe=True)
        except (RouteLineageError, OSError, ValueError, KeyError):
            lineage = [route]
        recorded, highest = set(), 0
        for item in lineage:
            directory = module.completion_dir(item["route_id"], jobs=jobs)
            for path in directory.glob(f"{node_id}.*.json"):
                middle = path.name[len(node_id) + 1:-5]
                if not middle.isdigit():
                    continue
                highest = max(highest, int(middle))
                evidence = json.loads(path.read_text(encoding="utf-8")).get("evidence") or {}
                recorded.add(Path(str(evidence.get("path") or "")).resolve(strict=False))
        if held.resolve(strict=False) not in recorded:
            return None
        sequence = max(module._next_marker_sequence(own, node_id), highest + 1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, DispatchContractError):
        return None
    from artifact_context import revision_output
    return revision_output(held, sequence)


def _resource_stage_marker_current(route, node, marker, marker_path, jobs):
    """Bind supervised-owner marker use to the journaled owner admission.

    Legacy markers keep their existing interpretation unless an exact
    same-route reconciliation records that a poll-only marker was unconsumed.
    New supervised-owner markers carry the resource identity that the owner
    admitted at the existing successor-start boundary.
    """
    if (node.get("kind") != "resource-runner"
            or (node.get("continuation") or {}).get("kind") != "supervised"):
        return None
    jobs_path = Path(jobs) if jobs else None
    try:
        import workflow_state as WS
        ledger = WS.WorkflowLedger(route["route_id"], route.get("route_hash", ""), jobs=jobs_path)
        state = ledger.read_only_state()
        if not state.get("ledger_dir_exists"):
            return (GateCurrency("resource-stage-not-current", "completion-marker-missing")
                    if marker.get("resource_execution_identity") is not None else None)
        if state.get("journal_unreadable"):
            return GateCurrency("resource-stage-not-current", "completion-marker-missing")
        armed_path = ledger.root / "armed" / f"{node['id']}.json"
        armed = json.loads(armed_path.read_text(encoding="utf-8"))
        if armed.get("predecessor_kind") != "resource":
            return None
        registry = json.loads(Path(armed["resource_registry"]).read_text(encoding="utf-8"))
        row = registry["runs"][armed["predecessor_id"]]
        if row.get("resource_policy") != "supervised-owner":
            return None
        raw = marker_path.read_bytes()
        marker_sha = hashlib.sha256(raw).hexdigest()
        marker_sequence = marker.get("sequence")
        stage = state.get("nodes", {}).get(node["id"], {})
        # Only a recorded stage state is an observation: no entry for the node is not "unsettled".
        observed = stage.get("state")
        not_current = GateCurrency("resource-stage-not-current",
                                   RESOURCE_STAGE_UNSETTLED if observed and observed != "STAGE_SUCCEEDED"
                                   else "completion-marker-missing")
        for entry in ledger.journal():
            evidence = entry.get("evidence") or {}
            if (entry.get("actor") == "resource-false-success-reconciliation"
                    and evidence.get("marker_sha256") == marker_sha
                    and evidence.get("marker_sequence") == marker_sequence):
                return not_current
        if observed != "STAGE_SUCCEEDED":
            return not_current
        resource_identity = marker.get("resource_execution_identity")
        if resource_identity is None:
            return None
        evidence = stage.get("evidence") or {}
        try:
            import resource_resume as RESOURCE_RESUME
            current_digest = RESOURCE_RESUME.row_digest(row)
        except (ImportError, KeyError, TypeError, ValueError):
            return GateCurrency("resource-stage-not-current", "completion-marker-missing")
        owner_wait = row.get("owner_wait") or {}
        output = __import__("resource_run_evidence").paths(armed).get("runtime_output")
        responsibility = __import__("dispatch_resource_wait").current_responsibility(
            row, route=route, owner_attempt_id=evidence.get("owner_attempt_id"),
            session_id=evidence.get("owner_session_id") or owner_wait.get("session_id"),
            jobs=str(jobs_path) if jobs_path else str(owner_wait.get("jobs") or ""),
            historical=True)
        if (stage.get("state") != "STAGE_SUCCEEDED"
                or responsibility is None
                or evidence.get("resource_execution_identity") != resource_identity
                or evidence.get("identity") != resource_identity
                or evidence.get("resource_sha256") != current_digest
                or not (evidence.get("owner_successor_start") is True
                        or evidence.get("owner_terminal_settlement") is True)
                or marker.get("evidence", {}).get("path") != output):
            return GateCurrency("resource-stage-not-current", "completion-marker-missing")
        return None
    except FileNotFoundError:
        return (GateCurrency("resource-stage-not-current", "completion-marker-missing")
                if marker.get("resource_execution_identity") is not None else None)
    except OSError:
        # A record that exists but cannot be read right now is not observed yet, whatever the
        # marker carries: a hand marker without an identity is not current on an unread ledger.
        return GateCurrency("resource-stage-not-current", RESOURCE_STAGE_UNOBSERVABLE)
    except (ValueError, KeyError, TypeError, AttributeError):
        if marker.get("resource_execution_identity") is not None:
            return GateCurrency("resource-stage-not-current", "completion-marker-missing")
        return None


def resource_completion_reuse_proof(route, node, marker_path, marker, jobs):
    """Expose an already admitted resource completion without a model sidecar."""
    identity = marker.get("resource_execution_identity")
    if node.get("kind") != "resource-runner" or identity is None:
        return None
    if (not isinstance(identity, str) or not identity
            or marker.get("registered_worker") is not False
            or marker.get("attempt_id") is not None
            or (node.get("continuation") or {}).get("kind") != "supervised"):
        raise DispatchContractError("resource-completion-identity-unverified", "resource completion identity is not exact")
    currency = gate_currency(route, node, marker_path, marker, jobs=jobs)
    readiness = completion_attempt_readiness(route, node, marker, Path(jobs))
    if currency.state != "current" or readiness.state != "ready":
        raise DispatchContractError("resource-completion-unverified", "resource completion is not current and ready")
    try:
        import workflow_state
        ledger = workflow_state.WorkflowLedger(route["route_id"], route.get("route_hash", ""), jobs=Path(jobs))
        stage = ledger.read_only_state().get("nodes", {}).get(node["id"], {})
        evidence = stage.get("evidence") or {}
        if (stage.get("state") != "STAGE_SUCCEEDED"
                or evidence.get("identity") != identity
                or evidence.get("resource_execution_identity") != identity
                or evidence.get("quiescent") is not True or evidence.get("terminal") is not True
                or not (evidence.get("owner_successor_start") is True
                        or evidence.get("owner_terminal_settlement") is True)
                or not isinstance(evidence.get("resource_sha256"), str)
                or not isinstance(evidence.get("owner_attempt_id"), str)):
            raise ValueError("resource completion has no admitted quiescence")
        return {"resource_execution_identity": identity,
                "resource_sha256": evidence["resource_sha256"],
                "owner_attempt_id": evidence["owner_attempt_id"],
                "owner_successor_start": evidence.get("owner_successor_start") is True,
                "owner_terminal_settlement": evidence.get("owner_terminal_settlement") is True,
                "quiescent": True, "terminal": True}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DispatchContractError("resource-completion-quiescence-unverified", "resource quiescence is not admitted") from exc


def evidence_change_history_path(marker_path: Path, node_id: str) -> Path:
    """The append-only history of a node's evidence edits after its marker."""
    return marker_path.parent / f"{node_id}.evidence-changes.jsonl"


def record_evidence_change(
    route: Mapping[str, object],
    node: Mapping[str, object],
    marker_path: Path,
    marker: Mapping[str, object],
    digest: str,
    *,
    change_kind: str = "edited",
) -> None:
    """Record one evidence edit after its completion marker; never refuse it.

    An artifact stays editable after its stage
    completed (user decision 2026-10-01): the edit is kept as one history line
    -- when, by whom, why, and the digest it replaced -- instead of a refusal.
    The same change is written once however often it is read, and a failed
    write never stops the work it describes.
    """
    node_id = str(node.get("id"))
    evidence = marker.get("evidence") if isinstance(marker.get("evidence"), dict) else {}
    path = evidence_change_history_path(marker_path, node_id)
    try:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            last = None
            for line in handle.read().splitlines():
                if line.strip():
                    last = line
            previous = None
            if last is not None:
                try:
                    previous = json.loads(last)
                except ValueError:
                    previous = None
            if (isinstance(previous, dict) and previous.get("sha256") == digest
                    and previous.get("marker_sequence") == marker.get("sequence")
                    and previous.get("change_kind", "edited") == change_kind):
                return
            changed_at = _evidence_mtime_text(Path(str(evidence.get("path") or "")))
            command = [Path(sys.argv[0]).name] if sys.argv and sys.argv[0] else []
            command += [arg for arg in sys.argv[1:] if not arg.startswith("-")][:1]
            record = {
                "schema_version": 1,
                "route_id": route.get("route_id"),
                "node_id": node_id,
                "marker_sequence": marker.get("sequence"),
                "evidence_path": evidence.get("path"),
                "marker_sha256": evidence.get("sha256"),
                "previous_sha256": (previous.get("sha256") if isinstance(previous, dict)
                                    and previous.get("marker_sequence") == marker.get("sequence")
                                    else evidence.get("sha256")),
                "sha256": digest,
                "change_kind": change_kind,
                "reason": ("evidence-missing-after-completion" if change_kind == "missing"
                           else "evidence-changed-after-completion"),
                "evidence_changed_at": changed_at,
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "observed_by": (os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
                                or session_label()),
                "observed_in": " ".join(command) or None,
            }
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
    except OSError:
        return


def note_evidence_change(
    route: Mapping[str, object],
    node: Mapping[str, object],
    marker_path: Path,
    marker: Mapping[str, object] | None = None,
) -> None:
    """Keep an evidence edit as history at an operation that proceeds on the marker.

    Called by continuation, a stage start, `complete` and a replacement owner's
    claim -- never by a read (dry-run, status, Fleet), which stays write-free.
    """
    try:
        marker = marker if marker is not None else json.loads(marker_path.read_text(encoding="utf-8"))
        currency = evidence_currency(route, dict(node), marker_path, dict(marker))
    except (OSError, ValueError, TypeError, DispatchContractError):
        return
    evidence = marker.get("evidence") if isinstance(marker.get("evidence"), dict) else {}
    if currency.state == "current" and currency.reason == "completion-evidence-recorded-missing":
        record_evidence_change(route, node, marker_path, marker, str(evidence.get("sha256") or ""),
                               change_kind="missing")
    elif (currency.state == "current" and currency.evidence_digest
          and currency.evidence_digest != evidence.get("sha256")):
        record_evidence_change(route, node, marker_path, marker, currency.evidence_digest)
    elif (currency.state == "current" and currency.evidence_digest
          and currency.evidence_digest == evidence.get("sha256")):
        history = evidence_change_history(marker_path, str(node.get("id")))
        if (history and history[-1].get("marker_sequence") == marker.get("sequence")
                and history[-1].get("sha256") != evidence.get("sha256")):
            record_evidence_change(route, node, marker_path, marker, str(evidence.get("sha256")),
                                   change_kind="reverted")


def _evidence_mtime_text(path: Path) -> str | None:
    """Return evidence mtime in the exact UTC format used by evidence history."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, ValueError):
        return None


def evidence_change_history(marker_path: Path, node_id: str) -> list[dict[str, object]]:
    """Every recorded evidence edit of one node, oldest first (read-only)."""
    try:
        lines = evidence_change_history_path(marker_path, node_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def evidence_currency(
    route: dict[str, object],
    node: dict[str, object],
    marker_path: Path,
    marker: dict[str, object] | None = None,
    *, observe: bool = False, check_source: bool = True,
    jobs: str | Path | None = None,
) -> GateCurrency:
    """The schema/identity/tombstone/evidence-digest half of `gate_currency`.

    ``observe=True`` reads integrity without gates-off execution relaxation.
    It never changes gate settings or adds execution requirements.

    Deliberately stops short of the attempt-link/shape proof
    (`_marker_link_current`) -- `_marker_identity_row`'s own `passed` field
    has always been a WEAKER claim than `completion_marker_is_current`'s (a
    join can pass the identity row without a link file yet existing;
    `test_ac5_owner_merge_arbitration_transaction` fixes this apart on
    purpose), so it reads only this half. `gate_currency` calls this first
    and adds the link proof on top for its own, stronger "current".
    """
    try:
        marker = marker or json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GateCurrency("integrity-broken:history-conflict", "completion-marker-unreadable")
    try:
        schema_ok, _history_path = _marker_schema_identity_ok(route, node, marker, marker_path, observe=observe)
    except (KeyError, TypeError, ValueError):
        schema_ok = False
    if not schema_ok:
        return GateCurrency("integrity-broken:identity-mismatch", "completion-marker-identity-mismatch")
    if marker.get("state") == "superseded-by-upstream-revision":
        superseded_by = marker.get("superseded_by") or {}
        upstream = superseded_by.get("node") if isinstance(superseded_by, dict) else None
        sequence = superseded_by.get("sequence") if isinstance(superseded_by, dict) else None
        detail = f"{upstream}@{sequence}" if upstream and sequence else node.get("id")
        action = route_state_next_action(
            "completion-evidence-superseded", str(node.get("id")), None, node,
        )
        if detail != node.get("id"):
            action += f" (upstream revision {detail})"
        if observe:
            return GateCurrency("superseded", "completion-evidence-superseded", action)
        note_same_work_drift("completion-evidence-superseded", str(detail))
    if check_source:
        from review_source_subject import marker_currency as source_marker_currency
        source_currency = source_marker_currency(
            route, node, marker, marker_path.parent, jobs=jobs,
        )
        if source_currency is not None:
            state, reason = source_currency
            if state != "current":
                return GateCurrency(state, reason,
                                    route_state_next_action(reason, str(node.get("id")), None, node)
                                    if state == "superseded" else None)
        older = _older_than_upstream(route, node, marker, marker_path)
        if older is not None:
            return older
    evidence_record = marker.get("evidence")
    from codex_dispatch_terminal import artifact_declared_verdict
    if (isinstance(evidence_record, dict) and not owner_closure_shape(marker)
            and artifact_declared_verdict(Path(str(evidence_record.get("path", "")))) in {"BLOCKED", "FAIL"}):
        return GateCurrency("integrity-broken:identity-mismatch", "completion-evidence-verdict-not-pass")
    if not isinstance(evidence_record, dict):
        return GateCurrency("integrity-broken:identity-mismatch", "completion-marker-evidence-missing")
    evidence_path_str = str(evidence_record.get("path") or "")
    from artifact_journal import split_reference, revision_digest
    if split_reference(evidence_path_str):
        try:
            digest = revision_digest(evidence_path_str)
        except (OSError, ValueError):
            return GateCurrency("completion-evidence-unreadable", "completion-evidence-unreadable")
        if digest != evidence_record.get("sha256"):
            return GateCurrency("revised-unrecorded", "completion-evidence-revised-unrecorded")
        return GateCurrency("current", "completion-marker-verified", evidence_digest=digest)
    evidence_path = Path(evidence_path_str) if evidence_path_str else None
    if evidence_path is None or not evidence_path.is_absolute():
        return GateCurrency("completion-evidence-unreadable", "completion-evidence-unreadable")
    if not os.path.lexists(evidence_path):
        # Keep the same recorded-move resolution as evidence_digest before
        # classifying an absent original locator as deleted evidence.
        try:
            from artifact_producer import resolve_placed_output
            evidence_path = resolve_placed_output(evidence_path)
        except (ImportError, OSError, ValueError):
            pass
    try:
        evidence_path.lstat()
    except FileNotFoundError:
        # evidence_digest intentionally normalizes a missing path to a typed
        # ValueError; inspect the directory entry first so only true absence
        # can take the recorded-history branch.
        try:
            marker_stat = marker_path.lstat()
            recorded_sha = evidence_record.get("sha256")
            if (not observe and stat.S_ISREG(marker_stat.st_mode)
                    and isinstance(recorded_sha, str)
                    and re.fullmatch(r"[0-9a-f]{64}", recorded_sha)):
                return GateCurrency("current", "completion-evidence-recorded-missing",
                                    evidence_digest=recorded_sha)
        except OSError:
            pass
        return GateCurrency("completion-evidence-unreadable", "completion-evidence-unreadable")
    except OSError:
        return GateCurrency("completion-evidence-unreadable", "completion-evidence-unreadable")
    try:
        digest = evidence_digest(evidence_path)
    except (OSError, ValueError):
        return GateCurrency("completion-evidence-unreadable", "completion-evidence-unreadable")
    if digest != evidence_record.get("sha256"):
        node_id = str(node.get("id"))
        if not observe:
            # Gates off an edit is not a refusal (nor a warning line). This read
            # stays a read: the operations that proceed on the marker keep the
            # edit as history (`note_evidence_change`).
            return GateCurrency("current", "completion-marker-verified", evidence_digest=digest)
        return GateCurrency(
            "revised-unrecorded", "completion-evidence-revised-unrecorded",
            route_state_next_action("completion-evidence-revised-unrecorded", node_id, None, node),
            evidence_digest=digest,
        )
    return GateCurrency("current", "completion-marker-verified", evidence_digest=digest)


def gate_currency(
    route: dict[str, object],
    node: dict[str, object],
    marker_path: Path,
    marker: dict[str, object] | None = None,
    *, observe: bool = False, allow_source_transition: bool = False,
    jobs: str | Path | None = None,
) -> GateCurrency:
    """Prove one completion marker current, superseded, or needing `revise`.

    SD-154 rule 5: this replaces the three evidence-sha recompute copies
    (`_marker_identity_row`, `_arbitration_observation`,
    `completion_marker_is_current`) and the consumers that reclassified
    `completion-evidence-hash-mismatch` by hand. Judgement order:
    `evidence_currency` (schema/identity/history-byte -> tombstone -> live
    evidence digest) -> shape/attempt-link. A live digest mismatch alone does
    not authorize revise: revised-unrecorded also passes the full provenance
    proof below before it is returned. Revision provenance then walks a
    bounded, strictly decreasing chain of immutable historical markers. It
    does not compare historical evidence paths to live bytes: official revise
    updates that file by design.
    """
    try:
        marker = marker or json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GateCurrency("integrity-broken:history-conflict", "completion-marker-unreadable")
    base = evidence_currency(
        route, node, marker_path, marker, observe=observe, check_source=False,
        jobs=jobs,
    )
    provenance = _marker_provenance_currency(route, node, marker_path, marker, base, observe=observe)
    if (not allow_source_transition and provenance.state in {"current", "revised-unrecorded"}):
        from review_source_subject import marker_currency as source_marker_currency
        source_currency = source_marker_currency(
            route, node, marker, marker_path.parent, jobs=jobs,
        )
        if source_currency is not None:
            state, reason = source_currency
            if state != "current":
                return GateCurrency(state, reason,
                    route_state_next_action(reason, str(node.get("id")), None, node)
                    if state == "superseded" else None,
                    provenance.evidence_digest)
        older = _older_than_upstream(route, node, marker, marker_path)
        if older is not None:
            return older._replace(evidence_digest=provenance.evidence_digest)
    if provenance.state in {"current", "revised-unrecorded"}:
        resource_currency = _resource_stage_marker_current(route, node, marker, marker_path, jobs)
        if resource_currency is not None:
            return resource_currency
    return provenance


def _marker_provenance_currency(route, node, marker_path, marker, base, *, observe=False):
    """Prove recorded marker lineage without rereading historical payloads."""
    if base.state not in {"current", "revised-unrecorded", "superseded"}:
        return base
    try:
        schema_ok, _ = _marker_schema_identity_ok(route, node, marker, marker_path, observe=observe)
    except (KeyError, OSError, TypeError, ValueError):
        schema_ok = False
    if not schema_ok:
        return GateCurrency("integrity-broken:identity-mismatch", "revision-predecessor-identity-invalid")
    # Legacy tombstones kept the original sequence. Run their ordinary
    # provenance walk, including sequence and revision-edge validation,
    # rather than treating an exact attempt link as the whole proof.
    legacy_supersession = (marker.get("state") == "superseded-by-upstream-revision"
                           and not marker.get("superseded_by"))
    if marker.get("state") == "superseded-by-upstream-revision" and not legacy_supersession:
        # Revise writes a new history row for the supersession, without
        # changing the original worker's attempt link. Prove that exact
        # predecessor rather than expecting its link to name the new row.
        try:
            prior_path, prior = _superseded_marker_predecessor(route, node, marker_path, marker)
            prior_currency = _marker_provenance_currency(
                route, node, prior_path, prior,
                GateCurrency("current", "completion-marker-verified"), observe=True)
        except (DispatchContractError, KeyError, OSError, TypeError, ValueError):
            return GateCurrency("integrity-broken:identity-mismatch", "supersession-provenance-invalid")
        if prior_currency.state not in {"current", "revised-unrecorded"}:
            return prior_currency
        return base
    if base.state not in {"current", "revised-unrecorded"} and not legacy_supersession:
        return base
    node_id = str(node.get("id"))
    current = marker
    current_path = marker_path
    initial_sequence = marker.get("sequence")
    if not isinstance(initial_sequence, int) or isinstance(initial_sequence, bool) or initial_sequence < 1:
        return GateCurrency("integrity-broken:identity-mismatch", "revision-provenance-invalid")
    # A valid chain can contain every sequence from the canonical marker down
    # to 1. Bound traversal by that sequence and the available predecessor
    # history, without imposing an arbitrary revision-count ceiling.
    available_predecessors = 0
    sequence_prefix = f"{node_id}."
    for child in marker_path.parent.iterdir():
        if not child.is_file() or not child.name.startswith(sequence_prefix) or child.suffix != ".json":
            continue
        suffix = child.name[len(sequence_prefix):-len(".json")]
        if suffix.isdecimal() and 1 <= int(suffix) < initial_sequence:
            available_predecessors += 1
    max_steps = min(initial_sequence, available_predecessors + 1)
    seen: set[int] = set()
    for _depth in range(max_steps):
        sequence = current.get("sequence")
        if (current.get("state") == "superseded-by-upstream-revision"
                and not (legacy_supersession and current is marker)):
            historical = _marker_provenance_currency(
                route, node, current_path, current,
                GateCurrency("current", "completion-marker-verified"), observe=True)
            return base if historical.state == "current" else historical
        if current.get("stage_authority") != "revision":
            try:
                link_ok = _marker_link_current(route, node, current, current_path)
            except (DispatchContractError, KeyError, OSError, TypeError, ValueError):
                link_ok = False
            if not link_ok:
                return GateCurrency("integrity-broken:identity-mismatch", "revision-predecessor-link-invalid")
            return base
        revision = current.get("revision") or {}
        of_sequence = revision.get("of_sequence")
        of_marker_sha256 = revision.get("of_marker_sha256")
        recorded_evidence = (current.get("evidence") or {}).get("sha256")
        if (
            not isinstance(sequence, int) or isinstance(sequence, bool)
            or not isinstance(of_sequence, int) or isinstance(of_sequence, bool)
            or of_sequence < 1 or of_sequence >= sequence or of_sequence in seen
            or not isinstance(of_marker_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", of_marker_sha256)
            or revision.get("evidence_sha256") != recorded_evidence
        ):
            return GateCurrency("integrity-broken:identity-mismatch", "revision-provenance-invalid")
        seen.add(sequence)
        prior_path = marker_path.parent / f"{node_id}.{of_sequence}.json"
        try:
            prior_bytes = prior_path.read_bytes()
        except OSError:
            return GateCurrency("integrity-broken:history-conflict", "canonical completion marker history conflict")
        if hashlib.sha256(prior_bytes).hexdigest() != of_marker_sha256:
            return GateCurrency("integrity-broken:history-conflict", "canonical completion marker history conflict")
        try:
            prior_marker = json.loads(prior_bytes)
            prior_schema_ok, _ = _marker_schema_identity_ok(route, node, prior_marker, prior_path, observe=observe)
        except (DispatchContractError, KeyError, OSError, TypeError, ValueError):
            prior_schema_ok = False
            prior_marker = {}
        if not prior_schema_ok or prior_marker.get("sequence") != of_sequence:
            return GateCurrency("integrity-broken:identity-mismatch", "revision-predecessor-identity-invalid")
        current, current_path = prior_marker, prior_path
    return GateCurrency("integrity-broken:identity-mismatch", "revision-provenance-invalid")


def _superseded_marker_predecessor(route, node, marker_path, marker):
    """Read the predecessor copied by the existing revision writer."""
    node_id = str(node["id"])
    sequence = marker.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 2:
        raise ValueError("supersession-sequence-invalid")
    predecessors = []
    prefix = f"{node_id}."
    for path in marker_path.parent.glob(f"{node_id}.*.json"):
        suffix = path.name[len(prefix):-len(".json")]
        if suffix.isdecimal() and 1 <= int(suffix) < sequence:
            predecessors.append((int(suffix), path))
    prior_sequence, prior_path = max(predecessors)
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    expected = dict(prior, sequence=sequence, state="superseded-by-upstream-revision",
                    superseded_by=marker.get("superseded_by"))
    if expected != marker and "reviewer_disposition" in prior:
        # History allocation can precede canonical commitment. A revision
        # copies the actual head, leaving that valid pending disposition in
        # history; follow its already verified origin, never delete the row.
        origin, origin_path = review_self_disposition_origin(route, node, prior, prior_path)
        if any(origin['sequence'] < seq < prior_sequence for seq, _ in predecessors):
            raise ValueError("supersession-predecessor-ambiguous")
        prior, prior_path, prior_sequence = origin, origin_path, origin['sequence']
    if (not isinstance(prior, dict) or prior.get("sequence") != prior_sequence
            or prior.get("state") == "superseded-by-upstream-revision"):
        raise ValueError("supersession-predecessor-invalid")
    expected = dict(prior, sequence=sequence, state="superseded-by-upstream-revision",
                    superseded_by=marker.get("superseded_by"))
    if expected != marker:
        raise ValueError("supersession-predecessor-mismatch")
    superseding = marker.get("superseded_by") or {}
    if not isinstance(superseding, dict):
        raise ValueError("supersession-upstream-invalid")
    upstream_id, upstream_sequence = superseding.get("node"), superseding.get("sequence")
    upstream_node = next((n for n in route["nodes"] if n["id"] == upstream_id), None)
    if (upstream_node is None
            or not isinstance(upstream_sequence, int) or isinstance(upstream_sequence, bool)
            or upstream_sequence < 2
            or node_id not in _route_module()._downstream_node_ids(route, upstream_id)):
        raise ValueError("supersession-upstream-invalid")
    upstream_path = marker_path.parent / f"{upstream_id}.{upstream_sequence}.json"
    upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
    if (not isinstance(upstream, dict) or upstream.get("stage_authority") != "revision"
            or upstream.get("sequence") != upstream_sequence):
        raise ValueError("supersession-upstream-not-revision")
    upstream_currency = _marker_provenance_currency(
        route, upstream_node, upstream_path, upstream,
        GateCurrency("current", "completion-marker-verified"), observe=True)
    if upstream_currency.state not in {"current", "revised-unrecorded"}:
        raise ValueError("supersession-upstream-unproven")
    return prior_path, prior


def observe_terminal_review_failure(
    route: dict[str, object], node: dict[str, object], attempt_id: str,
    evidence_path: Path, evidence_sha256: str, registry_lines: list[str],
) -> dict[str, object]:
    """Read an exact finished FAIL without requiring a PASS stage marker.

    Input digests remain the subject reader's responsibility. This observation
    binds the governed attempt, terminal log and readable evidence bytes; it
    grants neither execution nor stage completion authority.
    """
    from codex_dispatch_terminal import inspect_terminal_attempt

    exact = []
    for line in registry_lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            exact.append((fields, metadata))
        elif fields[1] in {"open", "running"}:
            try:
                identity = registered_node_identity(metadata, node)
            except ValueError:
                # A node-less owner belongs to its terminal owner node, not
                # this review. Match readiness's handling of unrelated rows.
                continue
            if identity == (route.get("route_id"), route.get("route_hash"), node.get("id")):
                raise ValueError("conflicting-active-retry")
    if not attempt_id or len(exact) != 1:
        raise ValueError("terminal-review-attempt-not-unique")
    fields, metadata = exact[0]
    if (registered_node_identity(metadata, node) !=
            (route.get("route_id"), route.get("route_hash"), node.get("id"))
            or node.get("kind") != "review-worker"
            or committed_outcome(fields[1], metadata) != "review-blocked"
            or metadata.get("worker_type") != "review"
            or metadata.get("artifact_root") != route.get("artifact_root")
            or fields[3] != route.get("cwd")
            or verdict_pass(metadata) or terminal_conflict_pending(metadata)):
        raise ValueError("terminal-review-binding-conflict")
    validate_attempt_metadata(metadata)
    if attempt_process_quiescence(metadata, terminal_receipt=True).state != "quiescent":
        raise ValueError("terminal-review-not-quiescent")
    terminal = inspect_terminal_attempt(
        metadata.get("log_file"), worktree=fields[3],
        artifact_root_metadata=metadata.get("artifact_root"), worker_type="review",
        artifact_binding=metadata)
    if (terminal.get("state") != "valid" or terminal.get("verdict") != "FAIL"
            or terminal.get("artifact_state") != "readable"
            or terminal.get("failure_note") != "completed-review-blocking"
            or terminal.get("review_verdict_conflict")):
        raise ValueError("terminal-review-failure-unverified")
    encoded = str(terminal.get("artifact_path_b64", ""))
    path = Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
    if (path.resolve() != evidence_path.resolve()
            or evidence_digest(path) != evidence_sha256):
        raise ValueError("terminal-review-evidence-conflict")
    return terminal


def completion_marker_is_current(
    route: dict[str, object],
    node: dict[str, object],
    marker_path: Path,
    marker: dict[str, object] | None = None,
    *, jobs: str | Path | None = None,
) -> bool:
    """Prove one schema-v2 marker and its immutable history/attempt linkage.

    One-line delegate (SD-154 rule 5): `gate_currency` is the only place that
    recomputes an evidence sha256 or walks the revision chain. Callers that
    already resolved a registry can pin source currency to that exact path;
    callers using the legacy shape remain compatible.
    """
    return gate_currency(route, node, marker_path, marker, jobs=jobs).state == "current"


def completion_conflict_attempt(marker: Mapping[str, object], registry_lines: list[str]) -> str:
    """Find a recorded consumption hold without reclassifying an old marker.

    Semantic history may outlive its process registry. Actual launch still
    requires completion_attempt_readiness and its current execution proof.
    """
    _launch_completion_marker_input(marker)
    for line in registry_lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        same_chain = (marker.get("stage_authority") == "owner-chain"
                      and marker.get("session_chain_id")
                      and meta.get("session_chain_id") == marker["session_chain_id"])
        same_attempt = marker.get("attempt_id") and meta.get("attempt_id") == marker["attempt_id"]
        if (same_chain or same_attempt) and terminal_conflict_pending(meta):
            return meta.get("attempt_id", "")
    return ""


def completed_marker_verdict_contradicts(metadata: Mapping[str, object]) -> bool:
    """Detect a ``completed-marker`` row whose own verdict was not a pass.

    ``verdict_pass`` already carries the completion-deferred exception (a
    transient-classifier row with a completion marker still passes). This is
    the only other axis: an explicit non-pass classification on a row that
    otherwise claims completion. A row sealed under owner-closure is exempt —
    that closure path deliberately keeps its review verdict on the FAIL axis
    (`capability-route.py`'s ``_complete_node_locked``) while still being the
    terminal record of a successful owner sweep, so it is not a contradiction.
    """
    if metadata.get("note") != "completed-marker":
        return False
    if metadata.get("gate_closure") == "owner-closure":
        return False
    if not (metadata.get("failure_class") or "").strip("-"):
        return False
    return not verdict_pass(metadata)


def completion_attempt_readiness(
    route: dict[str, object],
    node: dict[str, object],
    marker: dict[str, object],
    jobs: Path,
    *,
    registry_lines: list[str] | None = None,
    observe: bool = False,
) -> AttemptReadiness:
    """Combine a current semantic marker with its exact governed process state.

    ``observe=True`` retains route hash conflicts independently of execution
    compatibility; it does not change launch or completion policy.
    """

    _launch_completion_input(route, node, marker)
    if owner_closure_shape(marker) == "continuation":
        try:
            _route_module().validate_continuation_owner_closure(
                route, node, marker, jobs=jobs, lines=registry_lines, check_process=True,
            )
        except (DispatchContractError, ValueError, OSError, KeyError, TypeError) as exc:
            return AttemptReadiness("unverifiable", str(exc), str(marker.get("attempt_id") or ""))
        return AttemptReadiness("ready", "owner-closure-source-rounds-quiescent", marker["attempt_id"])
    if marker.get("stage_authority") == "owner-chain":
        try:
            chain_rows = registry_lines if registry_lines is not None else jobs.read_text(encoding="utf-8").splitlines()
        except OSError:
            return AttemptReadiness("unverifiable", "registry-unreadable")
        conflict = completion_conflict_attempt(marker, chain_rows)
        if conflict:
            return AttemptReadiness("unverifiable", "terminal-evidence-conflict", conflict)
        return AttemptReadiness("ready", "subsession-chain-quiescence-verified-at-stage-gate")
    if node.get("kind") == "resource-runner" or marker.get("registered_worker") is False:
        return AttemptReadiness("ready", "semantic-terminal-no-registered-process")
    attempt_id = marker.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return AttemptReadiness("unverifiable", "marker-attempt-id-missing")
    if registry_lines is None:
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return AttemptReadiness("unverifiable", "registry-unreadable", attempt_id)
    else:
        lines = registry_lines

    exact: list[tuple[list[str], dict[str, str]]] = []
    conflicting_active: list[str] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        try:
            row_route, row_hash, row_node = registered_node_identity(metadata, node)
        except ValueError as exc:
            if metadata.get("attempt_id") == attempt_id:
                return AttemptReadiness("unverifiable", str(exc), attempt_id)
            continue
        if row_route != route.get("route_id") or row_node != node.get("id"):
            continue
        # Only a hash-less route and hash-less legacy row share the old
        # read window. A modern route always requires its exact hash, at every
        # depth, just as registered complete does.
        if row_hash != str(route.get("route_hash") or ""):
            if metadata.get("attempt_id") == attempt_id:
                if observe:
                    return AttemptReadiness("unverifiable", "attempt-route-hash-mismatch", attempt_id)
                note_same_work_drift("attempt-route-hash-mismatch", attempt_id)
            else:
                continue
        if metadata.get("attempt_id") == attempt_id:
            exact.append((fields, metadata))
        elif fields[1] in {"open", "running"} and metadata.get("attempt_id"):
            conflicting_active.append(metadata["attempt_id"])
    if len(exact) != 1:
        return AttemptReadiness(
            "unverifiable", f"marker-attempt-row-count-{len(exact)}", attempt_id
        )
    fields, metadata = exact[0]
    if terminal_conflict_pending(metadata):
        return AttemptReadiness("unverifiable", "terminal-evidence-conflict", attempt_id)
    try:
        validate_attempt_metadata(metadata)
    except DispatchContractError as exc:
        return AttemptReadiness("unverifiable", exc.reason, attempt_id)
    if fields[1] != "done" or metadata.get("note") != "completed-marker":
        return AttemptReadiness("unverifiable", "marker-attempt-not-terminal", attempt_id)
    if completed_marker_verdict_contradicts(metadata):
        return AttemptReadiness("unverifiable", "marker-attempt-failure-class-not-pass", attempt_id)
    if conflicting_active:
        return AttemptReadiness("draining", "conflicting-active-retry", attempt_id)
    process = attempt_process_quiescence(metadata, terminal_receipt=True)
    if process.state == "quiescent":
        return AttemptReadiness("ready", process.reason, attempt_id)
    if process.state == "live":
        return AttemptReadiness("draining", process.reason, attempt_id)
    return AttemptReadiness("unverifiable", process.reason, attempt_id)
