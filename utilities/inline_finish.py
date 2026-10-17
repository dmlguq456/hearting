"""Retryable direct inline route finish transaction."""
from __future__ import annotations
import fcntl, hashlib, json, os, stat, subprocess, sys, tempfile, time
import fnmatch
from pathlib import Path
from typing import Any, Mapping, Sequence
import artifact_producer, artifact_lifecycle, dispatch_terminal_commit

class InlineFinishError(ValueError):
    pass


_SYNTHETIC_WRITE_SCOPES = frozenset({
    "source-scoped", "source/**", "source-alternative/**", "target-artifact",
})


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _scope_matches(path: str, item: str) -> bool:
    """Whether a tracked path intersects one concrete declared source scope."""
    if not item or item in _SYNTHETIC_WRITE_SCOPES:
        return False
    if item.endswith("/**"):
        base = item[:-3].rstrip("/")
        if any(char in base for char in "*?["):
            return fnmatch.fnmatchcase(path, base + "/*")
        return path == base or path.startswith(base + "/")
    base = item.rstrip("/")
    if path == base or path.startswith(base + "/"):
        return True
    return fnmatch.fnmatchcase(path, item)


def _unscoped_tracked_dirt(cwd: str, scope: Sequence[str]) -> list[str]:
    """Fresh tracked paths outside concrete scope; synthetic authority is not a path."""
    status = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain=v1", "-z", "--untracked-files=no"],
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if status.returncode:
        raise InlineFinishError("finish-git-status-unavailable")
    records = status.stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise InlineFinishError("finish-git-status-unavailable")
        paths.append(os.fsdecode(record[3:]))
        if b"R" in record[:2] or b"C" in record[:2]:
            if index >= len(records) or not records[index]:
                raise InlineFinishError("finish-git-status-unavailable")
            paths.append(os.fsdecode(records[index]))
            index += 1
    paths = sorted(set(paths))
    concrete_scope = [item for item in scope
                      if isinstance(item, str) and item not in _SYNTHETIC_WRITE_SCOPES]
    if any(_scope_matches(path, item) for path in paths for item in concrete_scope):
        raise InlineFinishError("finish-scoped-tracked-dirt")
    return paths


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    fd, tmp = tempfile.mkstemp(prefix=".finish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def _read(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise InlineFinishError("finish-intent-unsafe")
        row = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise InlineFinishError("finish-intent-corrupt") from exc
    if not isinstance(row, dict) or row.get("schema") != "inline_finish_v1":
        raise InlineFinishError("finish-intent-corrupt")
    return row

def _fault(point: str) -> None:
    if os.environ.get("HEARTING_INLINE_FINISH_CRASH_AT") == point:
        raise InlineFinishError("fault-injected-" + point)

def _registry_lines(jobs: Path, inherited: bool) -> list[str]:
    try:
        return jobs.read_text().splitlines()
    except FileNotFoundError:
        if inherited:
            raise
        return []


def _on_a_branch(cwd, commit) -> bool:
    """True when ``commit`` is on a local or remote-tracking branch, not only the checkout's HEAD.

    Work done in a linked worktree and merged through a PR lands on the remote main before
    anyone moves the shared checkout, and moving it under live routes is not the finisher's job.
    """
    found = subprocess.run(["git", "-C", cwd, "for-each-ref", "--count=1", "--contains", commit,
                            "--format=%(refname)", "refs/heads", "refs/remotes"],
                           capture_output=True, text=True)
    return found.returncode == 0 and bool(found.stdout.strip())


def registered_route(route, jobs, inherited):
    from dispatch_contract import parse_registry_metadata
    try:
        return any(route["route_id"] in {meta.get("route_id"), meta.get("owner_route_id")}
            for fields in (line.split("\t") for line in _registry_lines(jobs, inherited))
            for meta in [parse_registry_metadata(fields[5]) if len(fields) > 5 else {}])
    except Exception as exc:
        raise InlineFinishError("finish-registry-unreadable") from exc


def admit(route, *, allow_legacy=False):
    """Collect the current caller and registry; the common policy selects finish."""
    from dispatch_contract import DispatchContractError, resolve_global_registry
    from dispatch_parent_completion import interactive_parent_identity
    from route_authority import decide_continuation
    facts = {"allow_legacy": allow_legacy, "registered_caller": bool(
        os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or os.environ.get("AGENT_DISPATCH_DEPTH", "0") != "0")}
    try:
        _harness, facts["sid"] = interactive_parent_identity(os.environ)
    except DispatchContractError as exc:
        facts["identity_error"] = ("finish-caller-harness-ambiguous" if str(exc) == "caller-harness-ambiguous"
                                   else "finish-caller-harness-invalid")
    inherited = bool(os.environ.get("AGENT_DISPATCH_JOBS"))
    jobs = resolve_global_registry(Path(__file__).resolve().parents[1], None, 0, "read").path
    if inherited and not jobs.is_file():
        raise InlineFinishError("finish-registry-unavailable")
    facts["registered_route"] = registered_route(route, jobs, inherited)
    choice = decide_continuation(route, action="finish", facts=facts)
    if not choice.allowed:
        raise InlineFinishError(choice.reason)
    return choice, jobs, inherited


def finish(args, route: Mapping[str, Any], route_file: Path, api, *, _admission=None) -> dict[str, Any]:
    root = Path(route["artifact_root"]).resolve(strict=True)
    route_file = Path(route_file)
    if route_file.is_symlink():
        raise InlineFinishError("finish-route-unsafe")
    route_file = route_file.resolve(strict=True)
    _choice, jobs, inherited_jobs = _admission or admit(route)
    sid = _choice.census
    nodes = route.get("nodes") or []
    if dispatch_terminal_commit.require_current_cleanup("inline-finish", target=route_file) is not None:
        raise InlineFinishError("finish-foreign-cleanup-scope")
    node = nodes[-1]
    if not isinstance(node, dict) or not node.get("id"):
        raise InlineFinishError("finish-terminal-node-missing")
    base = root / ".runtime" / "inline-finish" / "v1" / route["route_id"]
    state_path, lock_path = base / "finish.json", base / "finish.lock"
    prior_state = _read(state_path)
    from route_authority import decide_continuation
    historical_false = None
    if not prior_state and api.outcome_path(route_file).exists():
        # Autoclose remains terminal. A historical false is consumable only
        # after this exact route now has a current, fully proven terminal gate.
        try:
            closure = json.loads(api.outcome_path(route_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            closure = {}
        choice = decide_continuation(route, action="finish-outcome", facts={"closure": closure})
        if choice.state == "autoclosed":
            print("capability-route: route already closed automatically; nothing left to finish", file=sys.stderr)
            return {"schema": "finish_receipt_v1", "route_id": route["route_id"], "route_hash": route["route_hash"],
                    "state": "already-closed-automatically", "autoclose": closure["autoclose"]}
        if (not choice.allowed
                or closure.get("route_id") != route["route_id"]
                or closure.get("route_hash") != route["route_hash"]
                or closure.get("route_file") != str(Path(route_file).resolve())
                or closure.get("cwd") != route.get("cwd")):
            raise InlineFinishError("finish-route-already-closed")
        # A registered owner's tuple is that owner's: an inline caller (never a registered one) cannot
        # take it over. An inline tuple is compared below with the intent, marker and binding this
        # finish builds now, before anything is claimed.
        if (any(closure.get(key) is not None for key in ("terminal_commit_id", "terminal_owner_attempt_id"))
                or (closure.get("producer_binding_digest") is not None and closure.get("inline_finish_id") is None)):
            raise InlineFinishError("finish-route-outcome-conflict")
        historical_false = closure
        # `finish` itself is the normal completion consumer: it still validates
        # the evidence, caller, route binding, and cleanup before it publishes
        # the terminal marker and current outcome.
    # A finish that already completed is replayed from what it recorded (§45 D-127):
    # the evidence file, the summary, or a manifest revision may have changed since.
    finished = bool(prior_state and prior_state.get("state") == "finished")
    record = artifact_producer.route_cycle_for(root, route)
    if record is None and prior_state:
        record = artifact_producer.read_cycle_record(root, prior_state.get("intent", {}).get("cycle_id", ""))
    if not record:
        raise InlineFinishError("finish-route-cycle-missing")
    admitted = artifact_producer.cycle_route_admission(root, record, route, finalize=True)
    if not admitted.allow:
        raise InlineFinishError("finish-route-cycle-" + admitted.reason)
    choice = decide_continuation(route, action="finish-cycle", facts={"deleted": bool(record.get("deleted_at")),
        "finished": finished, "review_live": artifact_producer._live_review_lease(root, record["cycle_id"]) is not None})
    if choice.state == "deleted":
        # The cycle was deleted while its route ran (§45 D-126): the route ends as a route with no output.
        return _finish_deleted_cycle(root, route, route_file, record, args, api)
    # A campaign deleted since the finish is the campaign it was; a cycle moved to another campaign is
    # still this route's cycle (§45 D-126), so its campaign's key is not the route's to compare.
    campaign = artifact_producer.campaign_or_tombstone(root, record["campaign_id"])
    expected_key = _route_campaign_key(root, route)
    if not campaign or not expected_key or (
            campaign.get("key") != expected_key and not record.get("moved_at") and not finished):
        raise InlineFinishError("finish-campaign-key-mismatch")
    if not choice.allowed:
        error = InlineFinishError(choice.reason)
        error.retry_when = choice.retry_when
        raise error
    output = artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
    evidence = Path(args.evidence).absolute()
    # Nothing moves payloads now; a path an earlier release moved is translated
    # through the move record it left.
    try:
        evidence_rel = evidence.relative_to(output).as_posix()
        placed = artifact_producer._placed_locator(evidence_rel, artifact_producer._output_placements(root, record))
        if placed != evidence_rel:
            evidence = output.parent / placed
    except ValueError:
        pass
    recorded_intent = (prior_state.get("intent") or {}) if finished else {}
    try:
        if evidence.is_symlink() or not stat.S_ISREG(evidence.lstat().st_mode):
            raise InlineFinishError("finish-evidence-not-regular")
        evidence.resolve(strict=True).relative_to(output.resolve(strict=True))
        evidence_raw = evidence.read_bytes()
    except InlineFinishError:
        if not finished: raise
        evidence_raw = None
    except (OSError, ValueError) as exc:
        if not finished: raise InlineFinishError("finish-evidence-outside-cycle-or-unreadable") from exc
        evidence_raw = None
    if evidence_raw is not None and not evidence_raw: raise InlineFinishError("finish-evidence-empty")
    try: summary_raw = Path(args.summary_file).read_bytes()
    except OSError as exc:
        if not finished: raise InlineFinishError("finish-summary-unreadable") from exc
        summary_raw = None
    if summary_raw is not None and (not summary_raw or b"\0" in summary_raw): raise InlineFinishError("finish-summary-invalid")
    summary_text = ""
    if summary_raw is not None:
        try: summary_text = summary_raw.decode("utf-8").strip()
        except UnicodeError as exc: raise InlineFinishError("finish-summary-invalid") from exc
        if not summary_text: raise InlineFinishError("finish-summary-invalid")
    source = str(route.get("source_commit") or "")
    commit = str(args.commit or api._head_commit(route["cwd"]) or "").lower()
    if not api._COMMIT_SHA.fullmatch(source) or not api._COMMIT_SHA.fullmatch(commit):
        raise InlineFinishError("finish-commit-invalid")
    ancestor = subprocess.run(["git", "-C", route["cwd"], "merge-base", "--is-ancestor", source, commit], capture_output=True)
    head = subprocess.run(["git", "-C", route["cwd"], "rev-parse", "HEAD"], capture_output=True, text=True)
    if ancestor.returncode or head.returncode or not _on_a_branch(route["cwd"], commit):
        raise InlineFinishError("finish-commit-not-source-descendant")
    # Tracked edits inside this direct node's declared scope are not admissible.
    scope = (node.get("write_scope") or [])
    _unscoped_tracked_dirt(route["cwd"], scope)
    identity = artifact_lifecycle.read_root_identity(root)
    if not identity: raise InlineFinishError("finish-artifact-root-identity-missing")
    try:
        evidence_rel = evidence.resolve().relative_to(output.resolve()).as_posix()
    except (OSError, ValueError):
        if not finished: raise InlineFinishError("finish-evidence-outside-cycle-or-unreadable")
        evidence_rel = recorded_intent.get("evidence_path")
    intent = {"route_id":route["route_id"], "route_hash":route["route_hash"],
              "artifact_root_id":identity.artifact_root_id, "campaign_key":campaign.get("key"),
              "campaign_id":record["campaign_id"], "cycle_id":record["cycle_id"], "producer_id":record["producer_id"],
              "terminal_node":node["id"], "evidence_path":evidence_rel,
              "evidence_sha256":_digest(evidence_raw) if evidence_raw is not None else recorded_intent.get("evidence_sha256"),
              "summary_sha256":_digest(summary_raw) if summary_raw is not None else recorded_intent.get("summary_sha256"),
              "commit":commit, "cycle_record_digest":dispatch_terminal_commit.cycle_identity_digest(record)}
    if finished and all(recorded_intent.get(key) == value for key, value in intent.items()
                        if key not in {"evidence_sha256", "summary_sha256", "campaign_id", "cycle_record_digest",
                                       "campaign_key"}):
        # The same finish, asked again after its files or its cycle's campaign moved on:
        # what it recorded stands (the receipt is not rewritten, nor is it cancelled).
        intent = dict(recorded_intent)
    intent_id = _digest(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())

    def make_binding(marker_digest):
        return {"kind":"inline_producer_binding_v1", "artifact_root_id":identity.artifact_root_id,
                "campaign_key":campaign.get("key"), "campaign_id":record["campaign_id"],
                "cycle_id":record["cycle_id"], "producer_id":record["producer_id"],
                "route_id":route["route_id"], "route_hash":route["route_hash"],
                "cycle_record_digest":dispatch_terminal_commit.cycle_identity_digest(record),
                "terminal_marker_digest":marker_digest, "evidence_sha256":intent["evidence_sha256"],
                "inline_finish_id":intent_id}

    base.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Re-prove every mutable admission fact while holding the route
            # finish fence.  In particular lstat before reading so a raced
            # symlink replacement cannot redirect evidence bytes.
            try:
                evidence_mode = evidence.lstat().st_mode
                if not stat.S_ISREG(evidence_mode) or evidence.is_symlink():
                    raise InlineFinishError("finish-evidence-not-regular")
                locked_evidence = evidence.read_bytes()
                locked_summary = Path(args.summary_file).read_bytes()
            except OSError as exc:
                if not finished: raise InlineFinishError("finish-evidence-drift") from exc
                locked_evidence, locked_summary = evidence_raw, summary_raw
            if not finished and (locked_evidence != evidence_raw or locked_summary != summary_raw):
                raise InlineFinishError("finish-evidence-drift")
            from route_authority import decide_continuation
            choice = decide_continuation(route, action="finish-registry",
                facts={"registered_route": registered_route(route, jobs, bool(inherited_jobs))})
            if not choice.allowed:
                raise InlineFinishError(choice.reason)
            unscoped_tracked_dirt = _unscoped_tracked_dirt(route["cwd"], scope)
            latest = artifact_producer.read_cycle_record(root, record["cycle_id"])
            if (not latest or latest.get("state") not in {"open", "sealed"}
                    or not dispatch_terminal_commit.cycle_identity_matches(
                        latest, intent["cycle_record_digest"], campaign_id=intent.get("campaign_id"))):
                raise InlineFinishError("finish-cycle-drift")
            current_route = json.loads(route_file.read_text(encoding="utf-8"))
            if (current_route.get("route_id") != intent["route_id"]
                    or current_route.get("route_hash") != intent["route_hash"]
                    or api.route_hash(current_route) != intent["route_hash"]):
                raise InlineFinishError("finish-route-drift")
            if not _on_a_branch(route["cwd"], intent["commit"]):
                raise InlineFinishError("finish-commit-drift")
            choice = decide_continuation(route, action="finish-cycle",
                facts={"review_live": artifact_producer._live_review_lease(root, record["cycle_id"]) is not None})
            if not choice.allowed:
                error = InlineFinishError(choice.reason)
                error.retry_when = choice.retry_when
                raise error
            state = _read(state_path)
            if state and state.get("inline_finish_id") != intent_id:
                prior_intent = state.get("intent") or {}
                if (prior_intent.get("evidence_path") == evidence_rel
                        and prior_intent.get("evidence_sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-evidence-drift")
                raise InlineFinishError("finish-intent-conflict")
            replayed_at_entry = state is not None
            marker_file = api.completion_dir(route["route_id"]) / (node["id"] + ".json")
            if not state:
                if historical_false is not None and any(historical_false.get(key) is not None for key in (
                        "inline_finish_id", "terminal_marker_digest")):
                    # The recorded tuple is compared with what this finish holds now, before a claim: for an
                    # inline record the same intent, summary, commit and binding, and for either the marker
                    # already written (a marker this finish would write now has bytes no record could name).
                    held_marker = marker_file.read_bytes() if marker_file.is_file() else None
                    held_digest = _digest(held_marker) if held_marker is not None else None
                    inline = historical_false.get("inline_finish_id") is not None
                    if (held_digest is None or historical_false.get("terminal_marker_digest") != held_digest
                            or (inline and (
                                historical_false.get("inline_finish_id") != intent_id
                                or historical_false.get("summary_digest") != intent["summary_sha256"]
                                or historical_false.get("head_commit") != commit
                                or historical_false.get("producer_binding_digest") != _digest(json.dumps(
                                    make_binding(held_digest), sort_keys=True, separators=(",", ":")).encode())))):
                        raise InlineFinishError("finish-route-outcome-conflict")
                _fault("before-claim")
                state = {"schema":"inline_finish_v1", "inline_finish_id":intent_id, "intent":intent,
                         "claimant_session":sid, "state":"claimed"}
                _atomic(state_path, state)
                _fault("after-claim")
            if state["state"] == "claimed":
                _fault("before-marker")
                if not marker_file.is_file():
                    cmd = [sys.executable, str(api.ROOT/"utilities/capability-route.py"), "complete",
                           "--route", str(route_file), "--node", node["id"], "--evidence", str(evidence)]
                    child_env=os.environ.copy(); child_env["AGENT_INLINE_FINISH_ID"]=intent_id
                    subprocess.run(cmd, cwd=route["cwd"], env=child_env, check=True, capture_output=True, text=True)
                marker_raw = marker_file.read_bytes()
                marker = json.loads(marker_raw)
                if (marker.get("route_id") != route["route_id"]
                        or marker.get("route_hash") != route["route_hash"]
                        or marker.get("node_id") != node["id"]
                        or marker.get("completion_gate") != node["completion_gate"]
                        or (marker.get("evidence") or {}).get("path") != str(evidence)
                        or (marker.get("evidence") or {}).get("sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-marker-evidence-mismatch")
                marker_digest = _digest(marker_raw)
                _fault("after-marker-write")
                state.update(state="node-completed", terminal_marker_digest=marker_digest); _atomic(state_path, state)
                _fault("after-marker")
            else:
                marker_raw = marker_file.read_bytes()
                try:
                    marker = json.loads(marker_raw)
                except (ValueError, UnicodeError) as exc:
                    raise InlineFinishError("finish-marker-corrupt") from exc
                if (marker.get("route_id") != route["route_id"]
                        or marker.get("route_hash") != route["route_hash"]
                        or marker.get("node_id") != node["id"]
                        or marker.get("completion_gate") != node["completion_gate"]
                        or (marker.get("evidence") or {}).get("path") != str(evidence)
                        or (marker.get("evidence") or {}).get("sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-marker-evidence-mismatch")
                marker_digest = _digest(marker_raw)
                if marker_digest != state.get("terminal_marker_digest"): raise InlineFinishError("finish-marker-drift")
            outcome_path = api.outcome_path(route_file)
            binding = make_binding(marker_digest)
            if state["state"] == "route-closed":
                try:
                    recorded_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                if _digest(json.dumps(recorded_outcome, sort_keys=True).encode()) != state.get("outcome_digest"):
                    raise InlineFinishError("finish-outcome-drift")
                if recorded_outcome.get("terminal_gate_proven") is False:
                    if not api._outcome_replay_matches(
                            recorded_outcome, route_id=route["route_id"], route_hash=route["route_hash"],
                            terminal_marker_digest=marker_digest, inline_finish_id=intent_id,
                            summary_digest=intent["summary_sha256"], inline_commit=commit,
                            producer_binding_digest=_digest(json.dumps(
                                binding, sort_keys=True, separators=(",", ":")).encode())):
                        raise InlineFinishError("finish-outcome-conflict")
                    # Re-enter the existing close step durably before consuming the false record.
                    # If close publishes and then stops, its normal replay updates this same intent.
                    state.update(state="node-completed")
                    _atomic(state_path, state)
            if state["state"] == "node-completed":
                _fault("before-close")
                if not outcome_path.is_file():
                    outcome, _ = api.close_route(route, route_file, commit, summary_text,
                        allow_unproven=False, expected_terminal_marker_digest=marker_digest,
                        inline_finish_id=intent_id, inline_commit=commit,
                        expected_summary_digest=intent["summary_sha256"],
                        expected_producer_binding_digest=_digest(json.dumps(binding,sort_keys=True,separators=(",", ":")).encode()))
                else:
                    outcome = json.loads(outcome_path.read_text())
                    if outcome.get("terminal_gate_proven") is False:
                        outcome, _ = api.close_route(
                            route, route_file, commit, summary_text, allow_unproven=False,
                            expected_terminal_marker_digest=marker_digest,
                            inline_finish_id=intent_id, inline_commit=commit,
                            expected_summary_digest=intent["summary_sha256"],
                            expected_producer_binding_digest=_digest(
                                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()))
                    elif (outcome.get("route_hash") != route["route_hash"]
                            or outcome.get("terminal_marker_digest") != marker_digest
                            or outcome.get("inline_finish_id") != intent_id
                            or outcome.get("summary_digest") != intent["summary_sha256"]
                            or outcome.get("producer_binding_digest") != _digest(json.dumps(binding,sort_keys=True,separators=(",", ":")).encode())
                            or outcome.get("head_commit") != commit):
                        raise InlineFinishError("finish-outcome-conflict")
                _fault("after-close-write")
                state.update(state="route-closed", outcome_digest=_digest(json.dumps(outcome,sort_keys=True).encode()))
                _atomic(state_path, state)
                _fault("after-close")
            if binding["terminal_marker_digest"] != marker_digest:
                raise InlineFinishError("finish-marker-drift")
            if state["state"] in {"route-closed", "producer-sealed", "finished"}:
                try:
                    applied_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                if _digest(json.dumps(applied_outcome, sort_keys=True).encode()) != state.get("outcome_digest"):
                    raise InlineFinishError("finish-outcome-drift")
            if state["state"] == "route-closed":
                _fault("before-finalize")
                crash_manifest = os.environ.get("HEARTING_INLINE_FINISH_CRASH_AT") == "after-manifest"
                artifact_producer.finalize_exact_cycle(root, cycle_id=record["cycle_id"],
                    expected_binding=binding, crash_after_manifest=crash_manifest)
                _fault("after-index")
                verified = artifact_producer.verify_finalized_cycle(root, cycle_id=record["cycle_id"], expected_binding=binding)
                state.update(state="producer-sealed", manifest_digest=verified["manifest_digest"]); _atomic(state_path, state)
                _fault("after-finalize")
            if state["state"] == "producer-sealed":
                _fault("before-receipt")
                # The close this finish made is judged by the revision it recorded; a later
                # document of the cycle (§45 D-124) does not undo it.
                verified = artifact_producer.verify_finalized_cycle(
                    root, cycle_id=record["cycle_id"], expected_binding=binding,
                    expected_manifest_digest=state.get("manifest_digest"))
                if verified.get("manifest_digest") != state.get("manifest_digest"): raise InlineFinishError("finish-seal-drift")
                receipt = {"schema":"finish_receipt_v1", "inline_finish_id":intent_id, "route_id":route["route_id"],
                           "route_hash":route["route_hash"], "cycle_id":record["cycle_id"],
                           "terminal_marker_digest":marker_digest, "manifest_digest":verified["manifest_digest"],
                           "commit":commit, "replay":replayed_at_entry}
                state.update(state="finished", receipt=receipt); _atomic(state_path, state)
                _fault("after-receipt")
            if state.get("state") != "finished": raise InlineFinishError("finish-pending")
            # A durable finished label is only a replay hint. Re-prove the
            # exact public marker, close outcome, and producer seal before
            # returning a successful receipt to any caller.
            if state.get("state") == "finished":
                try:
                    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                expected_binding_digest = _digest(json.dumps(binding, sort_keys=True,
                    separators=(",", ":")).encode())
                if (outcome.get("route_id") != route["route_id"]
                        or outcome.get("route_hash") != route["route_hash"]
                        or outcome.get("inline_finish_id") != intent_id
                        or outcome.get("terminal_marker_digest") != marker_digest
                        or outcome.get("summary_digest") != intent["summary_sha256"]
                        or outcome.get("producer_binding_digest") != expected_binding_digest
                        or outcome.get("head_commit") != commit
                        or outcome.get("terminal_gate_proven") is not True):
                    raise InlineFinishError("finish-outcome-conflict")
                verified = artifact_producer.verify_finalized_cycle(
                    root, cycle_id=record["cycle_id"], expected_binding=binding,
                    expected_manifest_digest=state.get("manifest_digest"))
                if (verified.get("manifest_digest") != state.get("manifest_digest")
                        or state.get("receipt", {}).get("terminal_marker_digest") != marker_digest):
                    raise InlineFinishError("finish-seal-drift")
            result = dict(state["receipt"]); result["replay"] = replayed_at_entry
            result["unscoped_tracked_dirt"] = unscoped_tracked_dirt
            if finished and verified.get("deleted"):
                # Information only: the cycle (or its campaign) was deleted after the finish.
                result["deleted_since"] = True
            if finished and verified.get("updated_since"):
                # Information only: the stored receipt keeps the revision it finished at.
                result["manifest_updated_since"] = verified["current_manifest_digest"]
            if route.get("capability") == "autopilot-spec":
                # Route/cycle/receipt are already committed. A failed shared
                # publication stays retryable beside them, never before them.
                result["shared_publication"] = artifact_producer.completed_spec_publication(
                    root, cycle_id=record["cycle_id"], settle=True)
                _fault("after-admission")
            # Information only, projected fresh on the first finish and on every replay; the stored
            # receipt and its identity are untouched, and nothing here starts the next leg.
            import route_plan
            next_leg = route_plan.next_leg_for_route(route)
            if next_leg is not None:
                result["next_leg"] = next_leg
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def _route_campaign_key(root: Path, route: Mapping[str, Any]) -> str | None:
    """The campaign key the route's own selection names: its key, the reserved `_unassigned`
    container for `--unassigned`, or the parent cycle's campaign for `--parent-cycle`."""
    from route_autoclose import campaign_key_of
    key = campaign_key_of(route)
    if key is None and route.get("parent_cycle_id"):
        parent = artifact_producer.read_cycle_record(root, str(route["parent_cycle_id"]))
        if parent:
            key = (artifact_producer.campaign_or_tombstone(root, parent["campaign_id"]) or {}).get("key")
    return key

def _finish_deleted_cycle(root: Path, route: dict, route_file: Path, record: dict, args, api) -> dict:
    """Close the route of a deleted cycle the way the runtime closes one nobody works on: no proof is
    claimed, and the cycle ends with the existing no-output record (the folder is not made again)."""
    try:
        summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        summary = ""
    commit = str(args.commit or api._head_commit(route["cwd"]) or "").lower() or None
    api.close_route(route, route_file, commit, summary or f"{route.get('capability')} {route.get('slug') or route['route_id']}",
                    allow_unproven=True,
                    autoclose={"reason": "cycle-deleted", "trigger": "finish", "closed_by": "finish",
                               "proof": "not-claimed",
                               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="abandoned",
                               abandon_reason="route-unrecoverable")
    return {"schema": "finish_receipt_v1", "route_id": route["route_id"], "route_hash": route["route_hash"],
            "cycle_id": record["cycle_id"], "state": "cycle-deleted", "terminal_gate_proven": False}


def pending_state(root: Path, route_id: str):
    return _read(Path(root)/".runtime"/"inline-finish"/"v1"/route_id/"finish.json")

def pending_for_cycle(root: Path, cycle_id: str):
    """Find a cycle's unfinished exact finish slot, including a continuation route slot.

    The cycle record retains its begin route, so looking up only that route
    would miss a finish claimed by a verified continuation.
    """
    home = Path(root)/".runtime"/"inline-finish"/"v1"
    if not home.is_dir():
        return None
    matches = []
    for route_home in home.iterdir():
        if route_home.is_symlink() or not route_home.is_dir():
            raise InlineFinishError("finish-intent-unsafe")
        state = _read(route_home/"finish.json")
        if (state and (state.get("intent") or {}).get("cycle_id") == cycle_id
                and state.get("state") != "finished"):
            matches.append(state)
    if len(matches) > 1:
        raise InlineFinishError("finish-cycle-ambiguous")
    return matches[0] if matches else None

def evidence_matches(path: Path, digest: str) -> bool:
    from dispatch_contract import evidence_digest
    return evidence_digest(path) == digest

def assert_not_pending(root: Path, route_id: str) -> None:
    state = pending_state(root, route_id)
    if state and state.get("state") != "finished": raise InlineFinishError("finish-in-progress")
