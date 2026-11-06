"""The journal storage variant of the existing inline finish transaction."""
from __future__ import annotations

from datetime import datetime
import fcntl
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import artifact_journal as journal

AUTO = "<!-- hearting-auto-turns-v1 "


def automatic_evidence(harness, sid, items, text):
    # This is an internal evidence envelope, not a CLI input or authority claim.
    # Existing bounded native/Git observations remain the source of the facts.
    data = {"session": {"harness": harness, "sid": sid}, "items": [
        {k: item.get(k) for k in ("id", "began", "at", "ended", "section", "source", "commits", "top",
                                 "observations", "tail_unknown", "source_count")} for item in items]}
    return AUTO + json.dumps(data, ensure_ascii=False) + " -->\n" + text


def dated(binding, at):
    result = dict(binding)
    result["at"] = at
    result["path"] = str(Path(binding["path"]).parent / (at[:10] + ".md"))
    return result


def record_items(root, binding, session, items, *, summary=None):
    found = []
    for index, item in enumerate(items):
        at = item.get("at") or datetime.fromtimestamp(item["began"]).astimezone().isoformat()
        results = result_versions(item.get("top"), item.get("source") or [], item.get("commits") or [])
        origin = {"route": binding["route_id"], "session": session, "turn": item["id"]}
        day = Path(root) / dated(binding, at)["path"]
        previous = [r for r in journal.read_day(day)["rows"] if r.get("origin") == origin] if day.exists() else []
        if previous and previous[-1]["body"] == journal._body(item.get("section") or "Observation unavailable\n"):
            results = previous[-1]["results"]  # retry never reattributes newer working bytes
        row = journal.append(root, dated(binding, at),
            origin=origin,
            session=session, turn=item["id"], at=at,
            body=item.get("section") or "Observation unavailable\n",
            summary=(summary if index == 0 and summary is not None else
                     previous[-1]["summary"] if previous else f"Automatic record: turn {at}"),
            checks={"state": "observed" if item.get("observations") else "unknown",
                    "source": "native-turn-bounds", "verdict": "not-assessed",
                    "changed_files": item.get("source_count"), "sampled_files": len(item.get("source") or [])},
            status="unknown" if item.get("tail_unknown") else "recorded", results=results,
            correction={"actor": session, "reason": "native-observation-update",
                        "source": {"turn": item["id"], "began": item["began"], "ended": item.get("ended")}})
        found.append(row)
    return found


def result_versions(top, paths, commits):
    """Result bytes stay in the project. A hash identifies dirt, not a commit."""
    result = []
    for rel in paths[:200]:
        path = Path(top or "/") / rel
        row = {"path": str(path), "version": {"kind": "unknown"}}
        try:
            # No symlink traversal or large result read on the Stop path.
            journal.safe_path(Path(top or "/"), path, missing=False)
            parent = journal.directory_fd(path.parent)
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            finally:
                os.close(parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if stat.S_ISREG(info.st_mode) and info.st_size <= 1024 * 1024:
                    raw = stream.read(1024 * 1024 + 1)
                    after = os.fstat(stream.fileno())
                    if (len(raw) == info.st_size and len(raw) <= 1024 * 1024
                            and (info.st_mtime_ns, info.st_ctime_ns, info.st_ino) ==
                                (after.st_mtime_ns, after.st_ctime_ns, after.st_ino)):
                        row["version"] = {"kind": "sha256", "value": hashlib.sha256(raw).hexdigest(),
                                          "source": "record-time-read"}
        except (OSError, ValueError):
            pass
        row["observed_commits"] = commits
        result.append(row)
    return result


def finish(args, route, route_file, api, admission, binding):
    import inline_finish as inline
    import artifact_producer as producer
    from capability_route_markers import write_completion_marker
    from route_authority import decide_continuation
    import artifact_lifecycle
    from dispatch_parent_completion import interactive_parent_identity
    root = Path(route["artifact_root"])
    root_identity = artifact_lifecycle.read_root_identity(root)
    if root_identity is None:
        raise inline.InlineFinishError("finish-artifact-root-identity-missing")
    node = route["nodes"][-1]
    sid = admission[0].census
    harness, caller_sid = interactive_parent_identity(os.environ)
    caller = {"harness": harness, "sid": caller_sid}
    if caller_sid != sid:
        raise inline.InlineFinishError("finish-caller-harness-invalid")
    base = root / ".runtime/inline-finish/v1" / route["route_id"]
    state_path, lock_path = base / "finish.json", base / "finish.lock"
    base.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = inline._read(state_path)
        replay = prior is not None
        fresh = json.loads(Path(route_file).read_text())
        if fresh.get("route_hash") != route["route_hash"] or api.route_hash(fresh) != route["route_hash"]:
            raise inline.InlineFinishError("finish-route-drift")
        choice = decide_continuation(route, action="finish-registry",
            facts={"registered_route": inline.registered_route(route, admission[1], bool(admission[2]))})
        if not choice.allowed:
            raise inline.InlineFinishError(choice.reason)
        commit, cwd, observation = inline.select_result_commit(route, args.commit, api)
        source = str(route.get("source_commit") or "")
        source_sha = bool(api._COMMIT_SHA.fullmatch(source))
        if source_sha:
            if (not api._COMMIT_SHA.fullmatch(commit or "")
                    or not inline._shares_source_history(cwd, source, commit)
                    or not inline._on_a_branch(cwd, commit)):
                raise inline.InlineFinishError("finish-commit-not-source-descendant")
            dirt = inline._finish_tracked_dirt(route, cwd, node.get("write_scope") or [], observation)
        else:
            commit, dirt = None, []
        if prior and prior.get("schema") != "inline_finish_v2":
            raise inline.InlineFinishError("finish-intent-conflict")
        if prior:
            intent = prior["intent"]
            if (intent["route_hash"] != route["route_hash"] or prior["claimant_session"] != sid
                    or intent["artifact_root_id"] != root_identity.artifact_root_id
                    or intent.get("claimant_identity") != caller):
                raise inline.InlineFinishError("finish-intent-conflict")
            if prior["state"] != "finished":
                # A pending finish must still have the exact supplied bytes.
                evidence = (str(args.evidence) + "\n").encode() if inline._evidence_url(str(args.evidence)) else inline._read_evidence_file(Path(args.evidence))
                summary = Path(args.summary_file).read_bytes()
                if (inline._digest(evidence) != intent["evidence_sha256"]
                        or inline._digest(summary) != intent["summary_sha256"] or commit != intent["commit"]
                        or str(args.evidence) != intent["evidence_source"]):
                    raise inline.InlineFinishError("finish-evidence-drift")
            records = [journal.read_reference(ref["path"]) for ref in intent["entries"]]
            if any(r["digest"] != ref["digest"] for r, ref in zip(records, intent["entries"])):
                raise inline.InlineFinishError("finish-journal-drift")
        else:
            if api.outcome_path(route_file).exists():
                raise inline.InlineFinishError("finish-route-already-closed")
            evidence = (str(args.evidence) + "\n").encode() if inline._evidence_url(str(args.evidence)) else inline._read_evidence_file(Path(args.evidence))
            summary = Path(args.summary_file).read_bytes()
            if not evidence or not summary or b"\0" in summary:
                raise inline.InlineFinishError("finish-summary-invalid")
            summary_text = summary.decode("utf-8").strip()
            if not summary_text:
                raise inline.InlineFinishError("finish-summary-invalid")
            try:
                text = evidence.decode("utf-8")
                if "\0" in text:
                    raise UnicodeError("binary evidence")
            except UnicodeError:
                text = "## Binary evidence\n\n```base64\n" + base64.b64encode(evidence).decode() + "\n```\n"
            if source_sha:
                dirt = inline._finish_tracked_dirt(route, cwd, node.get("write_scope") or [], observation)
                if not inline._on_a_branch(cwd, commit):
                    raise inline.InlineFinishError("finish-commit-drift")
            from turn_change_record import is_automatic_route
            from codex_dispatch_terminal import artifact_declared_verdict
            if (not is_automatic_route(route) and not inline._evidence_url(str(args.evidence))
                    and artifact_declared_verdict(Path(args.evidence)) in {"FAIL", "BLOCKED"}):
                raise inline.InlineFinishError("completion-evidence-verdict-not-pass")
            if is_automatic_route(route) and text.startswith(AUTO):
                end = text.index(" -->\n")
                auto = json.loads(text[len(AUTO):end])
                if auto["session"] != caller:
                    raise inline.InlineFinishError("finish-intent-conflict")
                records = record_items(root, binding, auto["session"], auto["items"], summary=summary_text)
            else:
                committed_result = bool(commit and not dirt and
                    "unchanged-starting-tree" not in (observation or {}).get("warnings", []))
                results = [{"path": str(cwd or route.get("cwd")),
                            "version": {"kind": "commit", "value": commit} if committed_result else {"kind": "unknown"},
                            "observed_commit": commit, "source": "finish-result-selection"}]
                records = [journal.append(root, binding, origin={"route": route["route_id"], "node": node["id"]},
                    session=caller, summary=summary_text, body=text, results=results,
                    correction={"actor": caller, "source": str(args.evidence), "reason": "unclaimed-evidence-update"})]
            if not records:
                raise inline.InlineFinishError("finish-evidence-empty")
            current = json.loads(Path(route_file).read_text())
            if api.route_hash(current) != route["route_hash"] or journal.target(root, route["route_id"]) != binding:
                raise inline.InlineFinishError("finish-route-drift")
            intent = {"storage_kind": "journal", "route_id": route["route_id"], "route_hash": route["route_hash"],
                      "artifact_root_id": root_identity.artifact_root_id, "evidence_source": str(args.evidence),
                      "claimant_identity": caller,
                      "campaign_id": binding["campaign_id"], "terminal_node": node["id"],
                      "entries": [{"path": r["reference"], "digest": r["digest"]} for r in records],
                      "evidence_sha256": inline._digest(evidence), "summary_sha256": inline._digest(summary),
                      "summary": summary_text[:1000], "summary_in_entry": len(summary_text) > 1000,
                      "commit": commit}
            prior = {"schema": "inline_finish_v2", "inline_finish_id": inline._digest(journal.canonical(intent)),
                     "claimant_session": sid, "intent": intent, "state": "claimed"}
            inline._fault("before-claim"); inline._atomic(state_path, prior); inline._fault("after-claim")
        ident = prior["inline_finish_id"]
        marker_path = api.completion_dir(route["route_id"]) / (node["id"] + ".json")
        ref = intent["entries"][0]
        if prior["state"] == "claimed":
            inline._fault("before-marker")
            write_completion_marker(route, node, node["id"], Path(ref["path"]))
            marker_digest = inline._digest(marker_path.read_bytes())
            prior.update(state="node-completed", terminal_marker_digest=marker_digest)
            inline._atomic(state_path, prior); inline._fault("after-marker")
        marker_digest = inline._digest(marker_path.read_bytes())
        marker = json.loads(marker_path.read_bytes())
        if (marker_digest != prior["terminal_marker_digest"] or marker.get("route_hash") != route["route_hash"]
                or marker.get("evidence") != {"path": ref["path"], "sha256": ref["digest"]}):
            raise inline.InlineFinishError("finish-marker-drift")
        producer_binding = {"kind": "inline_journal_binding_v2", "entries": intent["entries"],
                            "inline_finish_id": ident, "terminal_marker_digest": marker_digest}
        binding_digest = inline._digest(journal.canonical(producer_binding))
        expected = dict(expected_terminal_marker_digest=marker_digest, inline_finish_id=ident,
                        inline_commit=intent["commit"], expected_summary_digest=intent["summary_sha256"],
                        expected_producer_binding_digest=binding_digest)
        if prior["state"] == "node-completed":
            inline._fault("before-close")
            close_summary = records[0]["summary"] if intent.get("summary_in_entry") else intent["summary"]
            api.close_route(route, route_file, intent["commit"], close_summary, allow_unproven=False, **expected)
            inline._fault("after-close-write")
            prior.update(state="route-closed"); inline._atomic(state_path, prior); inline._fault("after-close")
        outcome = json.loads(api.outcome_path(route_file).read_bytes())
        if (outcome.get("inline_finish_id") != ident or outcome.get("terminal_marker_digest") != marker_digest
                or outcome.get("producer_binding_digest") != binding_digest
                or outcome.get("summary_digest") != intent["summary_sha256"]
                or outcome.get("head_commit") != intent["commit"] or not outcome.get("terminal_gate_proven")):
            raise inline.InlineFinishError("finish-outcome-conflict")
        if prior["state"] == "route-closed":
            inline._fault("before-receipt")
            prior.update(state="finished", receipt={"schema": "finish_receipt_v2", "storage_kind": "journal",
                "inline_finish_id": ident, "route_id": route["route_id"], "route_hash": route["route_hash"],
                "entries": intent["entries"], "terminal_marker_digest": marker_digest, "commit": intent["commit"]})
            inline._atomic(state_path, prior); inline._fault("after-receipt")
        if commit != inline.recorded_head(prior)[0]:
            if not inline._follow_up(cwd, prior, commit, sid):
                raise inline.InlineFinishError("finish-intent-conflict")
            summary = Path(args.summary_file).read_bytes()
            update = {"commit": commit, "previous_commit": inline.recorded_head(prior)[0],
                      "summary_sha256": inline._digest(summary), "session": sid,
                      "at": datetime.now().astimezone().isoformat()}
            # Follow-up evidence stays in the same journal, never a sidecar.
            journal.append(root, binding, origin={"route": route["route_id"], "follow_up": commit},
                summary=summary.decode(), body=summary.decode(), session=caller,
                results=[{"path": str(cwd), "version": {"kind": "commit", "value": commit}}])
            prior.setdefault("updates", []).append(update); inline._atomic(state_path, prior)
        journal.publish_views(root)
        return {**prior["receipt"], "commit": inline.recorded_head(prior)[0], "replay": replay, "unscoped_tracked_dirt": dirt,
                "commit_observation": observation, "updates": prior.get("updates") or []}
