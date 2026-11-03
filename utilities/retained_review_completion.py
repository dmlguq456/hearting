"""Reuse an unchanged independent judgment after a rejected, non-scientific correction.

The ordinary terminal-conflict command owns publication. Historical payloads,
terminal facts and attempt links are never rewritten. Readers share this proof.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from dispatch_attempt_policy import RETAINED_REVIEW_RESOLUTION as KIND


def _fail(reason):
    from dispatch_contract import DispatchContractError
    raise DispatchContractError("retained-review-" + reason)


def _json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _fail("file-unverifiable")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        _fail("object-required")
    return value, raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _rows(jobs):
    from dispatch_contract import parse_registry_metadata
    result = {}
    for raw in Path(jobs).read_text().splitlines():
        fields = raw.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        aid = meta.get("attempt_id")
        if aid:
            if aid in result:
                _fail("attempt-not-unique")
            result[aid] = (fields, meta, raw)
    return result


def _bound_file(record, root):
    path = Path(str(record.get("path") or record.get("preserved") or ""))
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(root)):
        _fail("evidence-outside-root")
    raw = path.read_bytes()
    if (record.get("sha256") != _sha(raw)
            or "bytes" in record and record["bytes"] != len(raw)):
        _fail("evidence-changed")
    return path, raw


def candidate(jobs, attempt_id):
    """Whether keeping this committed success would accept explicit non-PASS evidence."""
    from codex_dispatch_terminal import artifact_declared_verdict
    rows = _rows(jobs)
    if attempt_id not in rows:
        return False
    fields, meta, _ = rows[attempt_id]
    if fields[1] != "done" or meta.get("note") != "completed-marker" or meta.get("worker_type") != "review":
        return False
    if not meta.get("completion_marker"):
        return False
    marker, _ = _json(meta["completion_marker"])
    if "retained_review" in marker:
        return marker["retained_review"].get("rejected_attempt_id") == attempt_id
    return (marker.get("attempt_id") == attempt_id
            and artifact_declared_verdict(Path(marker.get("evidence", {}).get("path", ""))) in {"BLOCKED", "FAIL"})


def _actor(route, jobs, rows, parent_id):
    import route_authority as authority
    import dispatch_contract as D
    parent_id = D.review_self_disposition_lineage(route, jobs, {"parent_attempt_id": parent_id})[-1]
    if D._route_module().outcome_path(Path(rows[parent_id][1]["owner_route_file"])).exists():
        _fail("route-already-closed")
    row = rows.get(parent_id)
    if not row:
        _fail("owner-missing")
    fields, owner, _ = row
    if (owner.get("worker_type") != "owner" or owner.get("dispatch_depth") != "1"
            or owner.get("owner_route_id") != route["route_id"]
            or owner.get("owner_route_hash") != route["route_hash"]):
        _fail("owner-binding-invalid")
    import route_parent_close
    if owner.get("parent_close_requested") == "1" or route_parent_close.intent(route, jobs):
        _fail("parent-close-requested")
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller:
        if caller != parent_id:
            _fail("actor-not-owner")
        authority.review_owner_authority(route, jobs, parent_id)
        return {"owner_attempt_id": parent_id, "attempt_id": caller}
    sid = authority.caller_identity()[1]
    authority.require_owner(owner, sid, jobs, "retained-review-actor-not-current-parent")
    return {"owner_attempt_id": parent_id, "session_id": sid}


def plan(jobs, attempt_id, *, rejected_marker=None):
    """Read-only proof; review/actor authorization belongs to the apply boundary."""
    import dispatch_contract as D
    import dispatch_replacement as replacement
    from codex_dispatch_terminal import inspect_terminal_attempt
    jobs = Path(jobs).resolve()
    rows = _rows(jobs)
    if attempt_id not in rows:
        _fail("attempt-missing")
    fields, bad, _ = rows[attempt_id]
    route, route_bytes = _json(bad.get("route_file", ""))
    from route_lineage import verified_route_lineage
    verified_route_lineage(route, observe=True)
    # Validation may add execution defaults. They are not historical sealed
    # work and must not replace the route whose bytes this proof authenticates.
    D._route_module().verify_route(copy.deepcopy(route), allow_stale_registry=True)
    nodes = [n for n in route["nodes"] if n.get("id") == bad.get("route_node")]
    if len(nodes) != 1 or nodes[0].get("kind") != "review-worker":
        _fail("node-not-review")
    node = nodes[0]
    root = Path(route["artifact_root"]).resolve()
    canonical = jobs.parent / "completion" / route["route_id"] / (node["id"] + ".json")
    recorded = Path(bad.get("completion_marker", ""))
    if (not recorded.is_absolute() or recorded.resolve() != canonical.resolve()
            or not canonical.resolve().is_relative_to(jobs.parent)):
        _fail("completion-location-invalid")
    current, current_bytes = _json(canonical)
    marker = rejected_marker or current
    if "retained_review" in marker:
        marker, _ = _json(marker["retained_review"]["proof"]["rejected_marker"]["path"])
    history = canonical.parent / f"{node['id']}.{marker.get('sequence')}.json"
    historic, historic_bytes = _json(history)
    if marker != historic:
        _fail("marker-history-mismatch")
    if current != marker and (current.get("sequence") != marker.get("sequence", 0) + 1
            or current.get("retained_review", {}).get("rejected_attempt_id") != attempt_id
            or any(k in current for k in ("state", "stage_authority", "review_gate_closure", "reviewer_disposition"))):
        _fail("newer-completion-marker")
    identity = (route["route_id"], route["route_hash"], node["id"])
    if (fields[1] != "done" or bad.get("note") != "completed-marker"
            or bad.get("failure_class") != "pass" or bad.get("worker_type") != "review"
            or D.registered_node_identity(bad, node) != identity
            or tuple(marker.get(k) for k in ("route_id", "route_hash", "node_id")) != identity
            or marker.get("attempt_id") != attempt_id
            or marker.get("registered_worker") is not True
            or marker.get("review_independence") != "independent"
            or marker.get("reviewer_kind") != "registered-worker"
            or any(k in marker for k in ("state", "stage_authority", "review_gate_closure", "reviewer_disposition"))):
        _fail("rejected-binding-invalid")
    if not D._marker_link_current(route, node, marker, history):
        _fail("rejected-link-invalid")
    conflicts = D.terminal_conflicts(bad)
    if (len(conflicts) != 1 or any(e.get("note") != "dead-worker-blocked"
            or e.get("failure_class") != "blocked"
            or e.get("classifier_source") != "completion-join-late-terminal-v1" for e in conflicts.values())):
        _fail("conflict-not-operational-blocked")
    if any(e.get("review_sha256") for e in conflicts.values()):
        _fail("conflict-already-reviewed")
    relevant = [(f, m) for f, m, _ in rows.values()
                if m.get("route_id") == route["route_id"] and m.get("route_node") == node["id"]]
    if not relevant or relevant[-1][1].get("attempt_id") != attempt_id or any(f[1] in {"open", "running"} for f, _ in relevant):
        _fail("newer-or-live-review")
    blocked_path, blocked_bytes = _bound_file(marker["evidence"], root)
    blocked = json.loads(blocked_bytes)
    zeros = lambda data, keys: isinstance(data, dict) and all(type(data.get(k)) is int and data[k] == 0 for k in keys)
    if (blocked.get("verdict") != "BLOCKED" or blocked.get("io", {}).get("verdict") != "BLOCKED"
            or blocked.get("route_node") != node["id"]
            or blocked.get("reviewer_attempt") != attempt_id or blocked.get("guard_session_id") != attempt_id
            or blocked.get("source_changed") is not False or blocked.get("prd_changed") is not False
            or blocked.get("other_peer_files_read") is not False
            or blocked.get("new_inference_or_resource") is not False
            or blocked.get("blocking_findings", []) != []
            or blocked.get("delta_correctness_defects") != []
            or not zeros(blocked.get("completed_counts"), ("raw_records", "raw_shards", "scientific_levels"))
            or not zeros(blocked.get("failed_counts"), ("observed_scientific_mismatches",))
            or not zeros(blocked.get("prior_red_status"), ("closed", "open", "regressed"))
            or blocked.get("prior_verdict", {}).get("verdict") != "PASS"
            or blocked.get("prior_verdict", {}).get("blocking_findings_count") != 0):
        _fail("blocked-not-preserved-judgment")
    preserved = blocked["prior_verdict"].get("preserved")
    if not isinstance(preserved, list) or not preserved:
        _fail("preserved-evidence-missing")
    snapshots, candidates = [], []
    for entry in preserved:
        path, raw = _bound_file({**entry, "path": entry.get("preserved")}, root)
        snapshots.append({"path": str(path), "sha256": _sha(raw)})
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("verdict") == "PASS" and value.get("route_node") == node["id"]:
            candidates.append((path, raw, value))
    if len(candidates) != 1:
        _fail("prior-pass-not-unique")
    prior_path, prior_bytes, prior = candidates[0]
    prior_id = prior.get("guard_session_id")
    if (not prior_id or prior_id == attempt_id or prior.get("reviewer_attempt_id") != prior_id
            or prior.get("blocking_findings") != [] or prior.get("io", {}).get("verdict") != "PASS"):
        _fail("prior-pass-invalid")
    origins = []
    if type(marker.get("sequence")) is not int or marker["sequence"] < 2:
        _fail("marker-sequence-invalid")
    for path in canonical.parent.glob(node["id"] + ".[0-9]*.json"):
        suffix = path.name[len(node["id"]) + 1:-5]
        if not suffix.isdecimal() or int(suffix) >= marker["sequence"]:
            continue
        origin, raw = _json(path)
        if origin.get("attempt_id") == prior_id and origin.get("evidence", {}).get("sha256") == _sha(prior_bytes):
            origins.append((path, raw, origin))
    if len(origins) != 1 or prior_id not in rows:
        _fail("prior-history-not-unique")
    origin_path, origin_bytes, origin = origins[0]
    prior_fields, prior_meta, _ = rows[prior_id]
    if (prior_fields[1] != "done" or prior_meta.get("note") != "completed-marker"
            or prior_meta.get("worker_type") != "review" or prior_meta.get("registered_worker") != "1"
            or not D.verdict_pass(prior_meta) or D.terminal_conflicts(prior_meta)
            or D.registered_node_identity(prior_meta, node) != identity
            or prior_meta.get("parent_attempt_id") != bad.get("parent_attempt_id")
            or origin.get("reviewer_kind") != "registered-worker"
            or origin.get("review_independence") != "independent"
            or any(k in origin for k in ("state", "stage_authority", "review_gate_closure", "reviewer_disposition"))
            or not D._marker_link_current(route, node, origin, origin_path)):
        _fail("prior-binding-invalid")
    dependencies = [{"path": bad["route_file"], "sha256": _sha(route_bytes)},
                    {"path": str(history), "sha256": _sha(historic_bytes)},
                    {"path": str(origin_path), "sha256": _sha(origin_bytes)},
                    {"path": str(blocked_path), "sha256": _sha(blocked_bytes)}, *snapshots]
    for row_fields, meta in ((fields, bad), (prior_fields, prior_meta)):
        D.validate_attempt_metadata(meta)
        if (row_fields[3] != route["cwd"] or meta.get("artifact_root") != route["artifact_root"]
                or D.attempt_process_quiescence(meta, terminal_receipt=True).state != "quiescent"):
            _fail("attempt-not-settled")
        launch = replacement.launch_input(jobs, meta["attempt_id"], meta)
        if (launch.get("worktree") != route["cwd"] or launch.get("harness") != meta.get("harness")
                or launch.get("route_id") != route["route_id"] or launch.get("route_node") != node["id"]):
            _fail("assignment-binding-invalid")
        launch_path = replacement._directory(jobs) / "inputs" / (meta["attempt_id"] + ".json")
        dependencies.append({"path": str(launch_path), "sha256": _sha(launch_path.read_bytes())})
        terminal = inspect_terminal_attempt(meta.get("log_file"), worktree=row_fields[3],
                    artifact_root_metadata=meta.get("artifact_root"), worker_type="review", artifact_binding=meta)
        expected = "BLOCKED" if meta is bad else "PASS"
        if terminal.get("state") != "valid" or terminal.get("verdict") != expected or terminal.get("artifact_state") != "readable":
            _fail("terminal-unverified")
        dependencies.append({"path": meta["log_file"], "sha256": _sha(Path(meta["log_file"]).read_bytes())})
    subject = prior.get("report_manifest_sha256")
    manifest = prior.get("reviewed_report_manifest") or {}
    if not subject or blocked.get("report_manifest_sha256") != subject or manifest.get("sha256") != subject:
        _fail("subject-changed")
    inputs = prior.get("input_hashes")
    if not isinstance(inputs, list) or not inputs or len({e.get("path") for e in inputs}) != len(inputs):
        _fail("input-census-invalid")
    if not any(e.get("path") == manifest.get("path") and e.get("sha256") == subject for e in inputs):
        _fail("manifest-not-bound")
    for entry in [*inputs, *([prior["governing_prd"]] if "governing_prd" in prior else [])]:
        path, raw = _bound_file(entry, root)
        dependencies.append({"path": str(path), "sha256": _sha(raw)})
    # The reviewed manifest binds the published bundle, including glob inputs.
    # Check its members as well as the manifest bytes; unchanged index bytes
    # alone must not hide a changed figure or report.
    output = next((p for p in blocked_path.parents if p.name == "artifacts" and p.is_relative_to(root)), root)
    manifest_path = Path(str(manifest.get("path", "")))
    if manifest_path != output / "report/report_manifest.json":
        _fail("manifest-location-invalid")
    bundle, _ = _json(manifest_path)
    members = bundle.get("files")
    if not isinstance(members, list) or not members:
        _fail("bundle-census-missing")
    bundle_dependencies = []
    member_paths = set()
    for entry in [*members, *bundle.get("parent_inputs", []), *bundle.get("governing_inputs", [])]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            _fail("bundle-member-invalid")
        path = (manifest_path.parent / entry["path"]).resolve()
        if entry.get("absolute_path") and Path(entry["absolute_path"]).resolve() != path:
            _fail("bundle-member-binding-invalid")
        checked, raw = _bound_file({**entry, "path": str(path)}, root)
        bundle_dependencies.append({"path": str(checked), "sha256": _sha(raw)})
        member_paths.add(str(checked))
    literal_inputs = {str((output / item).resolve()) for item in node.get("inputs", [])
                      if not any(c in item for c in "*?[")}
    declared = {str(Path(e["path"]).resolve()) for e in inputs}
    if not literal_inputs.issubset(declared) or not member_paths:
        _fail("review-input-census-incomplete")
    import review_source_subject as subjects
    observed = subjects.observe(route, node, jobs=jobs)
    if not observed or not observed.get("family", "").startswith("artifact-review:"):
        _fail("fixed-report-subject-required")
    bound_paths = {str(Path(e["path"]).resolve()): e["sha256"] for e in [*dependencies, *bundle_dependencies]}
    for entry in observed["manifest"]:
        path = str((output / entry["path"]).resolve())
        if bound_paths.get(path) != entry.get("content_sha256"):
            _fail("unreviewed-input-member")
    subject = {"schema_version": subjects.SCHEMA_VERSION, "family": observed["family"],
               "digest": observed["digest"],
               "epoch": subjects.source_epoch(route, node, observed["digest"], canonical.parent,
                                               family=observed["family"])}
    census = marker.get("round_census")
    if (not isinstance(census, dict) or census.get("verdict_rounds") != 2 or census.get("cap") != 2
            or census.get("closure_class") != "registered-verdict"):
        _fail("round-census-unverified")
    all_dependencies = {}
    for entry in [*dependencies, *bundle_dependencies]:
        if entry["path"] in all_dependencies and all_dependencies[entry["path"]] != entry:
            _fail("input-changed-during-proof")
        all_dependencies[entry["path"]] = entry
    return {"route": route, "node": node, "rows": rows, "canonical": canonical,
            "current": current, "current_bytes": current_bytes, "rejected": marker,
            "origin": origin, "prior_path": prior_path, "prior_sha256": _sha(prior_bytes), "subject": subject,
            "dependencies": list(all_dependencies.values()),
            "proof": {"kind": KIND, "jobs": str(jobs), "rejected_attempt_id": attempt_id,
                      "rejected_marker": {"path": str(history), "sha256": _sha(historic_bytes)},
                      "origin_marker": {"path": str(origin_path), "sha256": _sha(origin_bytes)},
                      "dependencies": sorted(dependencies, key=lambda e: e["path"]),
                      "parent_attempt_id": bad["parent_attempt_id"], "source_subject": subject}}


def preview(jobs, attempt_id):
    if not candidate(jobs, attempt_id):
        return None
    try:
        import dispatch_contract as D
        value = plan(jobs, attempt_id)
        ready, reason = True, ""
        try:
            _actor(value["route"], jobs, value["rows"], value["proof"]["parent_attempt_id"])
        except ValueError as exc:
            ready, reason = False, str(exc)
        state = "prepared"
        if "retained_review" in value["current"]:
            verify_marker(value["route"], value["node"], value["current"], value["canonical"])
            state = "applied"
        elif any(e.get("resolution") == KIND for e in D.terminal_conflicts(value["rows"][attempt_id][1]).values()):
            state = "publication-pending"
        return {"disposition": KIND, "prior_attempt_id": value["origin"]["attempt_id"],
                "recovery_state": state, "can_reuse_prior_review": True,
                "authority_ready": ready, "authority_reason": reason}
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return {"disposition": "nonpass-completion-unresolved", "can_reuse_prior_review": False,
                "recovery_reason": str(exc)}


def _record_shape(record):
    if (not isinstance(record, dict) or set(record) != {"kind", "jobs", "rejected_attempt_id", "proof", "actor", "review", "recorded_at"}
            or record.get("kind") != KIND or not isinstance(record.get("proof"), dict)
            or not isinstance(record.get("actor"), dict) or not isinstance(record.get("review"), dict)
            or set(record["review"]) != {"path", "sha256"}):
        _fail("record-invalid")
    digest = record["review"].get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(record["review"].get("path"), str)
            or not Path(record["review"]["path"]).is_absolute()):
        _fail("record-review-invalid")
    actor = record["actor"]
    if (set(actor) not in ({"owner_attempt_id", "attempt_id"}, {"owner_attempt_id", "session_id"})
            or not all(isinstance(v, str) and v for v in actor.values())
            or "attempt_id" in actor and actor["attempt_id"] != actor["owner_attempt_id"]):
        _fail("record-actor-invalid")
    if datetime.fromisoformat(record["recorded_at"]).tzinfo is None:
        _fail("record-time-invalid")


def _historical_marker(route, node, marker, marker_path):
    """Authenticate a committed history edge without rereading revised payloads."""
    import dispatch_contract as D
    record = marker["retained_review"]
    proof = record["proof"]
    jobs = Path(record["jobs"])
    directory = jobs.parent / "completion" / route["route_id"]
    if not jobs.is_absolute() or Path(marker_path).parent.resolve() != directory.resolve():
        _fail("record-location-invalid")
    history = directory / f"{node['id']}.{marker['sequence']}.json"
    stored, raw = _json(history)
    if stored != marker:
        _fail("recovery-history-changed")
    rows = _rows(jobs)
    rejected = rows.get(record["rejected_attempt_id"])
    if not rejected:
        _fail("rejected-row-missing")
    entries = D.terminal_conflicts(rejected[1])
    if (len(entries) != 1 or any(e.get("resolution") != KIND
            or e.get("resolved_failure_class") != "blocked"
            or e.get("resolution_review_sha256") != record["review"]["sha256"]
            or e.get("recovery_marker_sha256") != _sha(raw) for e in entries.values())):
        _fail("record-not-committed")
    markers = []
    for key in ("origin_marker", "rejected_marker"):
        path = Path(proof[key]["path"])
        if path.parent.resolve() != directory.resolve():
            _fail("record-location-invalid")
        value, content = _json(path)
        if proof[key]["sha256"] != _sha(content):
            _fail("record-history-changed")
        if tuple(value.get(k) for k in ("route_id", "route_hash", "node_id")) != (route["route_id"], route["route_hash"], node["id"]):
            _fail("record-binding-invalid")
        row = rows.get(value.get("attempt_id"))
        if (not row or row[0][1] != "done" or row[1].get("worker_type") != "review"
                or D.registered_node_identity(row[1], node) != (route["route_id"], route["route_hash"], node["id"])
                or not D._marker_link_current(route, node, value, path)):
            _fail("record-binding-invalid")
        markers.append(value)
    origin, rejected_marker = markers
    evidence = marker.get("evidence")
    if (not isinstance(evidence, dict) or evidence.get("sha256") != origin["evidence"]["sha256"]
            or not any(e == evidence for e in proof["dependencies"])):
        _fail("record-evidence-invalid")
    expected = copy.deepcopy(origin)
    expected.update(sequence=rejected_marker["sequence"] + 1, evidence=evidence,
                    round_census=rejected_marker["round_census"], source_subject=proof["source_subject"],
                    retained_review=record)
    if (expected != marker or record["rejected_attempt_id"] != rejected_marker["attempt_id"]
            or proof["rejected_attempt_id"] != record["rejected_attempt_id"]
            or proof["kind"] != KIND or proof["jobs"] != str(jobs)):
        _fail("record-binding-invalid")
    return {"origin": origin, "rejected": rejected_marker}


def verify_marker(route, node, marker, marker_path, *, allow_pending=False, historical=False):
    import dispatch_contract as D
    record = marker.get("retained_review")
    _record_shape(record)
    if historical:
        return _historical_marker(route, node, marker, marker_path)
    rejected, _ = _json(record["proof"]["rejected_marker"]["path"])
    value = plan(record["jobs"], record["rejected_attempt_id"], rejected_marker=rejected)
    expected = copy.deepcopy(value["origin"])
    expected.update(sequence=value["rejected"]["sequence"] + 1,
                    evidence={"path": str(value["prior_path"]), "sha256": value["prior_sha256"]},
                    round_census=value["rejected"]["round_census"], source_subject=value["subject"], retained_review=record)
    if (expected != marker or record.get("proof") != value["proof"]
            or route["route_hash"] != value["route"]["route_hash"] or node != value["node"]):
        _fail("record-binding-invalid")
    review_sha256 = record["review"]["sha256"]
    owner = value["rows"].get(record["actor"].get("owner_attempt_id"))
    if not owner or owner[1].get("owner_route_id") != route["route_id"]:
        _fail("record-actor-invalid")
    row = value["rows"][record["rejected_attempt_id"]][1]
    conflicts = D.terminal_conflicts(row)
    if not allow_pending and any(e.get("resolution") != KIND or e.get("resolution_review_sha256") != review_sha256
            or e.get("resolved_failure_class") != "blocked" for e in conflicts.values()):
        _fail("record-not-committed")
    history = Path(marker_path).parent / f"{node['id']}.{marker['sequence']}.json"
    if not allow_pending and any(e.get("recovery_marker_sha256") != _sha(history.read_bytes()) for e in conflicts.values()):
        _fail("record-digest-invalid")
    if history.is_file() and json.loads(history.read_bytes()) != marker:
        _fail("recovery-history-changed")
    return value


def apply(jobs, attempt_id, *, expected_row_sha256, review):
    """Existing completion -> registry lock order; interrupted publication rolls forward."""
    import dispatch_contract as D
    R = D._route_module()
    jobs, review = Path(jobs).resolve(), Path(review)
    initial = plan(jobs, attempt_id)
    _actor(initial["route"], jobs, initial["rows"], initial["proof"]["parent_attempt_id"])
    if not review.is_absolute() or review.is_symlink() or not review.is_file():
        _fail("review-required")
    raw_review = review.read_bytes()
    if not raw_review.strip():
        _fail("review-required")
    D.ensure_global_registry_writable(jobs)
    with R._exclusive_lock(initial["canonical"].parent / f".{initial['node']['id']}.completion.lock"):
        with R._exclusive_lock(Path(str(jobs) + ".lock")):
            value = plan(jobs, attempt_id)
            actor = _actor(value["route"], jobs, value["rows"], value["proof"]["parent_attempt_id"])
            fields, meta, raw = value["rows"][attempt_id]
            if _sha(raw.encode()) != expected_row_sha256:
                _fail("row-changed")
            sequence = value["rejected"]["sequence"] + 1
            history = value["canonical"].parent / f"{value['node']['id']}.{sequence}.json"
            if history.exists():
                marker, _ = _json(history)
                verify_marker(value["route"], value["node"], marker, history, allow_pending=True)
                if (marker["retained_review"]["review"] != {"path": str(review), "sha256": _sha(raw_review)}
                        or marker["retained_review"]["actor"] != actor):
                    _fail("review-changed")
            else:
                record = {"kind": KIND, "jobs": str(jobs), "rejected_attempt_id": attempt_id,
                          "proof": value["proof"], "actor": actor,
                          "review": {"path": str(review), "sha256": _sha(raw_review)},
                          "recorded_at": datetime.now(timezone.utc).isoformat()}
                marker = copy.deepcopy(value["origin"])
                marker.update(sequence=sequence,
                    evidence={"path": str(value["prior_path"]), "sha256": value["prior_sha256"]},
                    round_census=value["rejected"]["round_census"], source_subject=value["subject"], retained_review=record)
                R.write_once(history, marker)
            verify_marker(value["route"], value["node"], marker, history, allow_pending=True)
            _actor(value["route"], jobs, value["rows"], value["proof"]["parent_attempt_id"])
            conflicts = D.terminal_conflicts(meta)
            for entry in conflicts.values():
                entry.update(resolution_review_sha256=_sha(raw_review),
                    review_path_b64=base64.b64encode(str(review).encode()).decode(),
                    resolution=KIND, resolved_failure_class="blocked",
                    recovery_marker_sha256=_sha(history.read_bytes()))
            values = {"terminal_conflicts_b64": base64.b64encode(json.dumps(conflicts, sort_keys=True).encode()).decode()}
            lines = jobs.read_text().splitlines()
            replacement = D._updated_attempt_metadata(fields[5], values, terminal=True)
            changed = fields[:5] + [replacement]
            lines[lines.index(raw)] = "\t".join(changed)
            D._atomic_registry_replace(jobs, lines)
            # If interrupted here, the original non-PASS head remains blocked.
            # The same command resumes this exact history, with a fresh row CAS.
            verify_marker(value["route"], value["node"], marker, history)
            if value["canonical"].read_bytes() != value["current_bytes"]:
                _fail("canonical-changed")
            R.atomic_write(value["canonical"], marker)
            return values
