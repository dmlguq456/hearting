"""SD-161 exact review input binding, shared by all registered runtimes.

The registry carries only a document digest. Historical readers do not require
that the input file still contains its former bytes; a new launch does.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from hearting_gates import gates_on, same_work_or_refuse

import dispatch_contract as DC

SCHEMA = "review-input-v1"
KEY = "review_input_digest"
ROOT = Path(__file__).resolve().parents[1]


def add_arguments(parser):
    parser.add_argument("--reviewed-evidence", help="exact file whose bytes this review examines")


def is_review_node(node):
    return is_plan_review_node(node) or is_spec_review_node(node)


def is_plan_review_node(node):
    return (isinstance(node, dict) and node.get("kind") == "review-worker"
            and (node.get("unit") == "qa/plan-review"
                 or (node.get("completion_gate") == "code-plan-check"
                     and node.get("unit") in {"qa/simplicity-check", "qa/assumption-check",
                         "qa/failure-mode-check", "qa/edge-case-check", "qa/test-gap-check"})))


def is_spec_review_node(node):
    return (isinstance(node, dict) and node.get("id") == "review"
            and node.get("unit") == "research/plan-review"
            and node.get("kind") == "review-worker"
            and node.get("completion_gate") == "spec-review")


def has_plan_producer(route, node):
    return (is_review_node(node) and "plan" in node.get("depends_on", [])
            and any(n.get("id") == "plan" for n in route.get("nodes", [])))


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value):
    return "sha256:" + hashlib.sha256(_bytes(value)).hexdigest()


def _placed(path):
    """Follow the producer's recorded placement: an earlier release's checkpoint may have
    moved a loose plan (``artifacts/plan.md`` -> ``artifacts/plans/plan.md``) after its
    marker was written."""
    try:
        from artifact_producer import resolve_placed_output
        return resolve_placed_output(Path(path))
    except Exception:
        return Path(path)


def _file(path):
    try:
        candidate = _placed(path)
        absolute = candidate.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:-1]:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise OSError("symlink parent")
        before = absolute.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(absolute, flags)
        try:
            opened = os.fstat(fd)
            key = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size,
                                item.st_mtime_ns, item.st_ctime_ns)
            if not stat.S_ISREG(opened.st_mode) or key(before) != key(opened):
                raise OSError("file changed")
            chunks = []
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
            after_fd = os.fstat(fd)
        finally:
            os.close(fd)
        after = absolute.lstat()
        if key(opened) != key(after_fd) or key(after_fd) != key(after):
            raise OSError("file changed")
        resolved = absolute.resolve(strict=True)
        data = b"".join(chunks)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise DC.DispatchContractError("reviewed-evidence-unreadable", str(path)) from exc
    return {"path": str(resolved), "sha256": hashlib.sha256(data).hexdigest()}


def sealed_plan_input(route, node):
    """The plan a review reads when its route has no plan node: the `plan.md` input compose sealed
    for the node from an earlier cycle (`input_sources`, a path under the route's artifact root).
    None when the node carries no such source."""
    sources = node.get("input_sources") if isinstance(node.get("input_sources"), dict) else {}
    source = next((sources[name] for name in sources if Path(name).name == "plan.md"), None)
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not source["path"]:
        return None
    return _file(Path(route.get("artifact_root") or "") / source["path"])


def drop_inapplicable(node, reviewed_evidence):
    """`--reviewed-evidence` binds what a plan or spec review examines; on any other node it binds
    nothing, so the optional value is dropped with a printed note instead of refusing the launch."""
    if reviewed_evidence and not is_review_node(node):
        print(f"reviewed_evidence_ignored={node.get('id')}")
        return None
    return reviewed_evidence


def resolve_input(route, node, jobs, reviewed_evidence=None, *, retry_of=None,
                  producer_preview=None):
    """Resolve explicit authority or the current primary plan gate, read-only.

    A node that is not a plan/spec review has no reviewed input; a value given for it is ignored."""
    if not is_review_node(node):
        return None
    explicit = _file(reviewed_evidence) if reviewed_evidence else None
    if retry_of:
        source_meta, original = predecessor_binding(
            jobs, retry_of, route, node, allow_legacy_unbound=is_spec_review_node(node))
        if original is None:
            return explicit
        if explicit is not None and explicit != {k: original[k] for k in ("path", "sha256")}:
            same_work_or_refuse("reviewed-evidence-replacement-mismatch")
            original = {**original, **explicit}
        return {k: original[k] for k in ("path", "sha256", "producer") if k in original}
    if not has_plan_producer(route, node):
        if route.get("ancestor_plan_refresh") is not None and node.get("id")=="plan-check":
            try:
                module = _route_module()
                proof = module.verified_ancestor_plan_refresh(route)
                module.review_lineage_routes(route, node["id"])
                candidate = _file(proof["current_evidence"]["path"])
                if candidate != proof["current_evidence"]:
                    raise ValueError("revised ancestor plan changed")
            except (OSError, ValueError, KeyError, TypeError, DC.DispatchContractError) as exc:
                raise DC.DispatchContractError("reviewed-evidence-ancestor-unproven", "plan") from exc
            if explicit is not None and explicit != candidate:
                raise DC.DispatchContractError("reviewed-evidence-ancestor-mismatch", str(node["id"]))
            return {**candidate, "producer": {
                "route_id": proof["ancestor_route_id"], "route_node": "plan",
                "attempt_id": proof["terminal_attempt_id"],
                "marker_digest": proof["current_marker_digest"],
            }}
        if explicit is None:
            if is_spec_review_node(node):
                return None
            sealed = sealed_plan_input(route, node)
            if sealed is not None:
                return sealed
            raise DC.DispatchContractError("reviewed-evidence-required", str(node.get("id")))
        return explicit
    producer = next(n for n in route["nodes"] if n.get("id") == "plan")
    marker_path = DC.resolve_dispatch_state_root(ROOT, Path(jobs)) / "completion" / route["route_id"] / "plan.json"
    try:
        marker = json.loads(marker_path.read_text())
        if producer_preview is not None:
            # Internal, read-only admission result; never an argv/environment
            # override. Bind the projection to the exact original history and
            # current bytes before using it to render a dry-run assignment.
            revision = producer_preview.get("revision") or {}
            history = marker_path.parent / f"plan.{marker['sequence']}.json"
            if (DC.gate_currency(route, producer, marker_path, marker).state != "revised-unrecorded"
                    or any(producer_preview.get(key) != marker.get(key)
                           for key in ("route_id", "route_hash", "node_id", "attempt_id"))
                    or producer_preview.get("stage_authority") != "revision"
                    or revision.get("of_sequence") != marker.get("sequence")
                    or revision.get("of_marker_sha256") != hashlib.sha256(history.read_bytes()).hexdigest()
                    or revision.get("of_evidence_sha256") != marker["evidence"]["sha256"]):
                raise ValueError("plan preview does not bind original marker")
            evidence = producer_preview["evidence"]
            if Path(evidence["path"]).resolve() != Path(marker["evidence"]["path"]).resolve():
                raise ValueError("plan preview changed evidence path")
        else:
            if not DC.completion_marker_is_current(route, producer, marker_path, marker):
                if gates_on():
                    raise ValueError("plan marker is not current")
                # Currency keeps missing/malformed evidence as hard failures.
                currency = DC.gate_currency(route, producer, marker_path, marker)
                if currency.state not in {"revised-unrecorded", "superseded"}:
                    raise ValueError("plan marker is not current")
                same_work_or_refuse("reviewed-evidence-not-current", currency.reason)
            evidence = marker["evidence"]
        candidate = _file(evidence["path"])
        if candidate["sha256"] != evidence["sha256"]:
            if gates_on():
                raise ValueError("plan evidence changed")
            same_work_or_refuse("reviewed-evidence-changed", candidate["path"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DC.DispatchContractError("reviewed-evidence-producer-unproven", "plan") from exc
    if explicit is not None and explicit != candidate:
        same_work_or_refuse("reviewed-evidence-producer-mismatch", str(node["id"]))
        candidate = explicit
    return {**candidate, "producer": {"route_id": route["route_id"], "route_node": "plan",
            "attempt_id": marker.get("attempt_id"), "marker_digest": _digest(producer_preview or marker)}}


def _path(jobs, attempt_id):
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", attempt_id):
        raise DC.DispatchContractError("reviewed-evidence-attempt-invalid")
    directory = Path(jobs).resolve().parent / "review-inputs"
    path = directory / (attempt_id + ".json")
    if directory.is_symlink() or path.is_symlink():
        raise DC.DispatchContractError("reviewed-evidence-binding-symlink")
    return path


def _identity(jobs, metadata):
    return {"jobs": str(Path(jobs).resolve()), **{key: str(metadata.get(key) or "")
            for key in ("attempt_id", "route_id", "route_hash", "route_node")}}


def read_binding(jobs, metadata, *, verify_current=False):
    """Read exact historical authority; optionally require unchanged live bytes."""
    path = _path(jobs, metadata.get("attempt_id"))
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DC.DispatchContractError("reviewed-evidence-unproven", str(metadata.get("attempt_id"))) from exc
    required = {"schema", "jobs", "attempt_id", "route_id", "route_hash", "route_node", "path", "sha256"}
    if (not isinstance(payload, dict) or not required <= set(payload)
            or set(payload) - required - {"producer", "source"}
            or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("path"), str) or not Path(payload["path"]).is_absolute()
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256", "")))):
        raise DC.DispatchContractError("reviewed-evidence-binding-mismatch", str(metadata.get("attempt_id")))
    if (any(payload.get(k) != value for k, value in _identity(jobs, metadata).items())
            or _digest(payload) != metadata.get(KEY)):
        same_work_or_refuse("reviewed-evidence-binding-mismatch", str(metadata.get("attempt_id")))
        payload = {**payload, **_identity(jobs, metadata)}
    if verify_current:
        current = _file(payload["path"])
        if current != {k: payload[k] for k in ("path", "sha256")}:
            same_work_or_refuse("reviewed-evidence-changed", payload["path"])
            payload = {**payload, **current}
    return payload


def seal_binding(jobs, metadata, candidate, *, source=None):
    if candidate is None:
        return ""
    payload = {"schema": SCHEMA, **_identity(jobs, metadata), **candidate}
    if not all(payload.get(key) for key in ("attempt_id", "route_id", "route_hash", "route_node")):
        raise DC.DispatchContractError("reviewed-evidence-route-required")
    if source is not None:
        payload["source"] = source
    path = _path(jobs, metadata["attempt_id"])
    current = _file(payload["path"])
    if current != {k: payload[k] for k in ("path", "sha256")}:
        same_work_or_refuse("reviewed-evidence-changed", payload["path"])
        payload.update(current)
    digest = _digest(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _bytes(payload) + b"\n"
    from artifact_receipt import _write_once
    if not _write_once(path.parent, path, encoded) and path.read_bytes() != encoded:
        raise DC.DispatchContractError("reviewed-evidence-binding-conflict", metadata["attempt_id"])
    return digest


def _route_node(metadata):
    route_file = metadata.get("route_file")
    if not route_file:
        return None, None
    try:
        route = json.loads(Path(route_file).read_text())
        if (route.get("route_id") != metadata.get("route_id")
                or route.get("route_hash") != metadata.get("route_hash")):
            if gates_on():
                raise ValueError("route binding mismatch")
            same_work_or_refuse("reviewed-evidence-route-mismatch")
        node = next(n for n in route["nodes"] if n.get("id") == metadata.get("route_node"))
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
        raise DC.DispatchContractError("reviewed-evidence-route-unproven") from exc
    return route, node


def predecessor_binding(jobs, attempt_id, route, node, *, allow_legacy_unbound=False):
    from dispatch_replacement import _rows
    try:
        rows = _rows(Path(jobs).read_text().splitlines())
    except OSError as exc:
        raise DC.DispatchContractError("reviewed-evidence-source-missing", attempt_id) from exc
    if attempt_id not in rows:
        raise DC.DispatchContractError("reviewed-evidence-source-missing", attempt_id)
    metadata = rows[attempt_id][1]
    same_route = all(metadata.get(key) == value for key, value in (
            ("route_id", route["route_id"]), ("route_hash", route["route_hash"]),
            ("route_node", node["id"])))
    if not same_route:
        same_work_or_refuse("reviewed-evidence-replacement-mismatch")
    if allow_legacy_unbound and same_route:
        binding_path = _path(jobs, attempt_id)
        try:
            binding_path.lstat()
        except FileNotFoundError:
            if not metadata.get(KEY):
                return metadata, None
    return metadata, read_binding(jobs, metadata, verify_current=True)


def _dispatch_node_module():
    import importlib.util
    import sys
    name = "_review_input_dispatch_node"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / "dispatch-node.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _round_admission(route, node, jobs, *, attempt_id, owner_attempt_id, reviewed_evidence):
    """The one round-admission decision preview and the registration fence share.

    A thin call into dispatch-node's `admit_round` (the same census, subject and
    revision derivation every launch surface reads), without recording anything.
    """
    return _dispatch_node_module().admit_round(
        route, node, jobs, owner_attempt_id=owner_attempt_id, exclude_attempt=attempt_id,
        reviewed_evidence=reviewed_evidence, record_auto_revisions=False)


def preview_request_admission(args, jobs):
    """Recompute dry-run admission; caller-supplied preview flags are not proof."""
    if getattr(args, "action", None) != "dry-run" or not getattr(args, "route_file", None):
        return None
    if getattr(args, "automatic_retry_of", None):
        # SD157 reuses the source round and its exact input. It receives no
        # revision-preview exception; predecessor and normal gates still apply.
        return None
    from route_authority import declared_subsession
    if declared_subsession(args):
        # A declared sub-session holds no stage-gate authority: its launch
        # (dispatch-node.py) and the round census leave it out of the
        # full-stage verdict rounds, so its preview is not one either.
        return None
    metadata = {key: getattr(args, key, None) for key in
                ("attempt_id", "route_file", "route_id", "route_hash", "route_node")}
    route, node = _route_node(metadata)
    module = _dispatch_node_module()
    if not module.REVIEW_ROUND_CAP.is_round_capped_node(node):
        return None
    try:
        admission = _round_admission(
            route, node, jobs, attempt_id=getattr(args, "command_attempt_id", None) or getattr(args, "attempt_id", None),
            owner_attempt_id=getattr(args, "parent_attempt_id", None),
            reviewed_evidence=getattr(args, "reviewed_evidence", None))
    except (ValueError, OSError) as exc:
        if isinstance(exc, DC.DispatchContractError):
            raise
        raise DC.DispatchContractError("reviewed-evidence-preview-unproven", str(exc)) from exc
    if admission.budget.state != "admit":
        raise DC.DispatchContractError("reviewed-evidence-revision-not-admitted", admission.budget.state)
    return admission


def preview_request_nodes(args, jobs):
    admission = preview_request_admission(args, jobs)
    return admission.planned_revision_nodes if admission is not None else frozenset()


def prepare_request(args):
    """Wrapper preview/registration validation; no side effects or inferred input."""
    if getattr(args, "unit", None) != "qa/plan-review" and not getattr(args, "reviewed_evidence", None):
        return None
    metadata = {key: getattr(args, key, None) for key in
                ("attempt_id", "route_file", "route_id", "route_hash", "route_node")}
    # Most wrappers and route-free jobs do not participate in this contract.
    if not metadata["route_file"]:
        if getattr(args, "reviewed_evidence", None):
            raise DC.DispatchContractError("reviewed-evidence-route-required")
        return None
    route, node = _route_node(metadata)
    if not is_review_node(node):
        return resolve_input(route, node, args.jobs_path, getattr(args, "reviewed_evidence", None))
    prior = getattr(args, "automatic_retry_of", None)
    admission = preview_request_admission(args, args.jobs_path) if not prior else None
    candidate = admission.reviewed_input if admission is not None else None
    if candidate is None:
        candidate = resolve_input(route, node, args.jobs_path,
                                  getattr(args, "reviewed_evidence", None), retry_of=prior)
    if candidate is None:
        args.review_input_candidate = None
        return None
    if prior:
        source_meta, source_binding = predecessor_binding(
            args.jobs_path, prior, route, node, allow_legacy_unbound=is_spec_review_node(node))
        if source_binding is not None:
            args.review_input_source = {"attempt_id": prior, "binding_digest": source_meta[KEY]}
    previous = getattr(args, "review_input_candidate", None)
    if previous is not None and previous != candidate:
        same_work_or_refuse("reviewed-evidence-changed", candidate["path"])
    args.review_input_candidate = candidate
    args.reviewed_evidence = candidate["path"]
    return candidate


def registration_fragment(args):
    candidate = prepare_request(args)
    if candidate is None:
        return ""
    metadata = {key: getattr(args, key, None) for key in ("attempt_id", "route_id", "route_hash", "route_node")}
    digest = seal_binding(args.jobs_path, metadata, candidate,
                          source=getattr(args, "review_input_source", None))
    return "," + KEY + "=" + digest


def _route_module():
    import importlib.util
    import sys
    name = "_review_input_capability_route"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "utilities/capability-route.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def validate_revision_admission(jobs, metadata, binding):
    # A correction revision's extra right belongs to its exact input bytes.
    # Recheck at both atomic fences, after the CLI admission and before spawn.
    if metadata.get("replacement_family_id") and metadata.get("automatic_retry_of"):
        return  # Common replacement admission already proved this same round.
    route, node = _route_node(metadata)
    if node is None or not is_review_node(node):
        return
    if is_spec_review_node(node):
        return
    from review_source_subject import read_binding as read_subject
    try:
        admission = _round_admission(
            route, node, jobs, attempt_id=metadata["attempt_id"],
            owner_attempt_id=metadata.get("parent_attempt_id"),
            reviewed_evidence=binding["path"] if binding else None)
        subject = read_subject(jobs, metadata, required=False)
    except (ValueError, OSError, _dispatch_node_module().DispatchNodeError) as exc:
        if isinstance(exc, DC.DispatchContractError):
            raise
        raise DC.DispatchContractError("reviewed-evidence-revision-unproven", str(exc)) from exc
    # The fence judges by the subject sealed at registration. A worktree that has
    # moved since is an observation of later work, not a verdict on this attempt.
    if subject is not None and admission.source_subject_digest != subject["subject_digest"]:
        raise DC.DispatchContractError("reviewed-evidence-revision-unproven",
                                       "sealed-review-subject-differs-from-current")
    if admission.budget.state != "admit":
        raise DC.DispatchContractError("reviewed-evidence-revision-not-admitted", admission.budget.state)


def validate_launch(jobs, metadata):
    # Legacy settlement/join never calls this new-launch fence.
    if metadata.get(KEY):
        binding = read_binding(jobs, metadata, verify_current=True)
        validate_revision_admission(jobs, metadata, binding)
        return binding
    if metadata.get("unit") != "qa/plan-review":
        return None
    route, node = _route_node(metadata)
    if node is not None and is_review_node(node):
        if is_spec_review_node(node):
            return None
        raise DC.DispatchContractError("reviewed-evidence-unproven", str(metadata.get("attempt_id")))
    return None


def prompt_block(args):
    candidate = getattr(args, "review_input_candidate", None)
    if not candidate:
        return ""
    return ("\n\nReviewed input (runtime-bound, not an output path):\n"
            + "- path: " + json.dumps(candidate["path"], ensure_ascii=False)
            + "\n- SHA256: " + candidate["sha256"]
            + "\nRead this input; write findings only in this route's declared output scope.\n")
