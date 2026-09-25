"""Campaign satisfaction: verified native acceptance -> immutable event -> projection.

Cycle/route success is not inferred from campaign satisfaction. Native session
stores are the existing local trust boundary, not cryptographic proof against
a process allowed to forge those stores. No caller-supplied user actor is used.

A row's provisional keys (`route_closed`, `terminal_gate_proven`,
`terminal_gate_reasons`, `route_outcome_digest`) appear only on a provisional
member and hold the route outcome sidecar's values as of the close that
produced them; they are never re-observed later.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import artifact_admission as admission
import artifact_identity as identity
import artifact_index as index_module
import artifact_lifecycle as lifecycle
import artifact_locator as locator
import artifact_manifest as manifest

CONTRACT = "artifact-campaign-closure/v1"
EVENT_NAME = "campaign.satisfied.json"
MAX_JSON = 16 * 1024 * 1024

# A cycle sealed `state: active` (route open at seal time, D-6) whose route has
# since closed. Never written to a record or event; computed for display only,
# so relabeling it later costs nothing against a signed statement.
PROVISIONAL_DISPOSITION = "sealed-unproven"


class CampaignError(Exception):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, str(detail)
        super().__init__(code + (": " + self.detail if self.detail else ""))


def disposition(row):
    if row.get("route_closed") is True and row.get("state") == "active":
        return PROVISIONAL_DISPOSITION
    return row["state"]


# One definition of campaign membership for producer cycle records.
# `artifact_producer._remove_empty_cycle` drops an output-less cycle from
# `campaign.cycles` but keeps its record (state `abandoned` or `no-lineage`)
# so the sealed abandon reason stays auditable.  Such a record is detached,
# not a member: counting it against `campaign.cycles` reported a permanent
# `campaign-membership-drift` (TF-Rehancer root, 2026-09-15, three abandoned
# records with no directory).  Writer and verifier both read this set.
DETACHED_STATES = frozenset({"abandoned", "no-lineage"})


def is_member_record(record):
    return isinstance(record, dict) and record.get("state") not in DETACHED_STATES


def campaign_records(root, campaign_id):
    """Split this campaign's producer records into (members, detached)."""
    records = Path(root) / ".runtime/artifact-producer/v1/cycles"
    members, detached = [], []
    for entry in sorted(records.glob("*.json")):
        record, _ = read_json(root, entry)
        if record.get("campaign_id") != campaign_id:
            continue
        (members if is_member_record(record) else detached).append(record)
    return members, detached


def detached_rows(detached):
    return [{"cycle_id": record.get("cycle_id"), "state": record.get("state"),
             "abandon_reason": record.get("abandon_reason"), "sealed_on": record.get("sealed_on"),
             "locator": record.get("locator")} for record in detached]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _safe(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise CampaignError("campaign-path-outside-root", path) from exc
    if ".." in parts:
        raise CampaignError("campaign-path-outside-root", path)
    current = root
    for part in ("", *parts):
        current = current / part
        if current.is_symlink():
            raise CampaignError("campaign-symlink", current)
    return path


def read_json(root, path):
    path = _safe(root, path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
                raise CampaignError("campaign-input-kind-or-size", path)
            raw = stream.read(MAX_JSON + 1)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value, raw
    except (OSError, ValueError, UnicodeError) as exc:
        raise CampaignError("campaign-input-invalid", path) from exc


def campaign_path(root, selection):
    root = Path(root).resolve()
    if identity.is_well_formed(str(selection), "campaign"):
        directory = locator.find_path_by_id(root, str(selection))
        if directory is None:
            raise CampaignError("campaign-unknown", selection)
        path = directory / "campaign.json"
    else:
        path = Path(selection)
        if not path.is_absolute():
            path = root / path
        if path.name != "campaign.json":
            path = path / "campaign.json"
    _safe(root / "campaigns", path)
    if path.parent.parent != root / "campaigns":
        raise CampaignError("campaign-path-invalid", path)
    return path


def _event_path(path):
    return path.parent / EVENT_NAME


def _load_event(root, path):
    event_path = _event_path(path)
    if not event_path.exists() and not event_path.is_symlink():
        return None
    event, raw = read_json(root, event_path)
    violations = []
    manifest._v_event_row(event, "$", violations)
    if violations:
        raise CampaignError("campaign-event-invalid", violations[:3])
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise CampaignError("campaign-event-invalid", event_path)
    snapshot = payload.get("snapshot", {})
    approved = payload.get("approval", {})
    if not all(isinstance(value, dict) for value in (snapshot, approved)) or not isinstance(snapshot.get("campaign"), dict):
        raise CampaignError("campaign-event-invalid", event_path)
    campaign = snapshot.get("campaign", {})
    expected = approval_text(campaign.get("campaign_id", ""), digest(snapshot))
    actor_id = "native-user:" + hashlib.sha256(
        (str(approved.get("harness")) + ":" + str(approved.get("session_id"))).encode()).hexdigest()
    if (raw != canonical(event) + b"\n" or event.get("event_type") != "campaign.satisfied"
            or payload.get("contract") != CONTRACT or event.get("target_id") != campaign.get("campaign_id")
            or campaign.get("state") != "active" or approved.get("statement") != expected
            or approved.get("decision") != "accepted"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(approved.get("native_message_digest")))
            or approved.get("harness") not in {"claude", "codex", "opencode"}
            or approved.get("actor_id") != actor_id
            or event.get("actor") != {"kind": "user", "id": approved.get("actor_id")}
            or event.get("stream_sequence") != 1
            or event.get("event_id") != _id("evt", {k: v for k, v in event.items() if k != "event_id"})
            or payload.get("root") != str(Path(root).resolve())):
        raise CampaignError("campaign-event-invalid", event_path)
    return event


def _id(prefix, value):
    return prefix + "_" + hashlib.sha256(canonical(value)).hexdigest()[:32]


def _projection(event):
    value = dict(event["payload"]["snapshot"]["campaign"])
    value.update(state="satisfied", satisfied_on=event["recorded_at"],
                 satisfaction_event_id=event["event_id"])
    return value


def fold_campaign(root, path, campaign):
    """Pure read: a committed event fences begin even before cache recovery."""
    event = _load_event(root, path)
    if event is None:
        return campaign
    before = event["payload"]["snapshot"]["campaign"]
    after = _projection(event)
    if campaign not in (before, after):
        raise CampaignError("campaign-projection-conflict", path)
    return after


def check_campaign_write(root, path, proposed):
    event = _load_event(root, path)
    if event is not None:
        current, _ = read_json(root, path)
        fold_campaign(root, path, current)
        if proposed != _projection(event):
            raise CampaignError("campaign-terminal-write-conflict", path)


def _validate_runlog(root, path, campaign, ids):
    value = campaign.get("runlog")
    if value is None:
        return
    keys = {"contract", "path", "sha256", "source_cycle_id", "source_locator"}
    if not isinstance(value, dict) or set(value) != keys:
        raise CampaignError("campaign-runlog-invalid", "shape")
    source_cycle_id = value.get("source_cycle_id")
    if (value.get("contract") != "campaign-runlog/v1" or value.get("path") != "RUNLOG.md"
            or value.get("source_locator") != "experiments/_RUNLOG.md"
            or not identity.is_well_formed(source_cycle_id, "cycle")
            or source_cycle_id in ids
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("sha256")))):
        raise CampaignError("campaign-runlog-invalid", "contract")
    runlog = _safe(path.parent, path.parent / "RUNLOG.md")
    try:
        fd = os.open(runlog, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CampaignError("campaign-runlog-invalid", "kind")
            actual = "sha256:" + hashlib.sha256(stream.read()).hexdigest()
    except OSError as exc:
        raise CampaignError("campaign-runlog-missing", runlog) from exc
    if actual != value["sha256"]:
        raise CampaignError("campaign-runlog-digest-mismatch", runlog)


def _route_outcome(root, record, document):
    """`None` when the cycle's route is still open; else `(outcome, raw)`.

    Route-closed is judged by the canonical outcome sidecar's existence, not
    by `route_file` (relocation-tolerant) or `schema_version`. A sidecar that
    exists but whose identity does not match the record and the manifest's
    `routes[]` row is a typed integrity error, not "open".
    """
    try:
        outcome_path = lifecycle.canonical_outcome_path(root, record["route_id"])
    except lifecycle.LifecycleError as exc:
        raise CampaignError("campaign-cycle-route-outcome-mismatch", record.get("cycle_id")) from exc
    if not os.path.lexists(outcome_path):
        return None
    outcome, raw = read_json(root, outcome_path)
    routes = document.get("routes")
    matching = ([row for row in routes if isinstance(row, dict) and row.get("route_id") == record["route_id"]]
                if isinstance(routes, list) else [])
    if (outcome.get("route_id") != record.get("route_id")
            or outcome.get("route_hash") != record.get("route_hash")
            or len(matching) != 1
            or matching[0].get("route_hash") != outcome.get("route_hash")
            or matching[0].get("terminal_marker") != "pending"):
        raise CampaignError("campaign-cycle-route-outcome-mismatch", record.get("cycle_id"))
    return outcome, raw


def _open_refusal_detail(pending):
    entries = [f"cycle={cid} route={rid} route_state=open" for cid, rid in pending]
    shown = "; ".join(entries[:10])
    if len(entries) > 10:
        shown += f" +{len(entries) - 10} more"
    return (f"open={len(pending)} {shown} "
            "next=capability-route.py complete --route <route_file> --node <terminal node> "
            "then close --route <route_file>; or close --route <route_file> --allow-unproven "
            "(disclosed as unproven); then rerun campaign-status")


def _cycle_rows(root, path, campaign):
    ids = campaign.get("cycles")
    if (not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids)
            or len(set(ids)) != len(ids)):
        raise CampaignError("campaign-membership-invalid")
    _validate_runlog(root, path, campaign, ids)
    records = root / ".runtime/artifact-producer/v1/cycles"
    root_id = lifecycle.read_root_identity(root)
    if root_id is None:
        raise CampaignError("root-identity-missing")
    index = admission.load_index(root)
    if index.artifact_root_id != root_id.artifact_root_id:
        raise CampaignError("campaign-index-root-mismatch")
    # A campaign list alone cannot hide an open member or an unregistered tree.
    # Detached records (see `is_member_record`) are reported, never counted.
    member_records, detached = campaign_records(root, campaign["campaign_id"])
    members = {record.get("cycle_id") for record in member_records}
    if members != set(ids):
        raise CampaignError("campaign-membership-drift")
    directories = {}
    for entry, layout in locator.iter_cycle_dirs(path.parent):
        _safe(path.parent, entry)
        if (entry / ".cycle.json").exists():
            binding, _ = read_json(root, entry / ".cycle.json")
        elif (entry / "manifest.json").exists():
            # Historical sealed cycles predate .cycle.json. Their immutable
            # manifest plus producer record and index still prove the binding.
            document, _ = read_json(root, entry / "manifest.json")
            binding = document.get("cycle", {})
        elif layout == "legacy-id" and entry.name in ids:
            binding = {"cycle_id": entry.name, "campaign_id": campaign["campaign_id"]}
        else:
            # Undeclared material is not silently made a cycle or a new
            # residual-zero requirement. Declared members still must resolve.
            continue
        if not isinstance(binding, dict) or binding.get("campaign_id") != campaign["campaign_id"]:
            raise CampaignError("campaign-cycle-binding-mismatch", entry)
        cid = binding.get("cycle_id")
        if not isinstance(cid, str) or cid in directories:
            raise CampaignError("campaign-cycle-binding-duplicate", entry)
        directories[cid] = entry
    if set(directories) != set(ids):
        raise CampaignError("campaign-cycle-bindings-incomplete")
    rows = []
    pending_open = []
    for cid in sorted(ids):
        if not identity.is_well_formed(cid, "cycle"):
            raise CampaignError("campaign-cycle-id-invalid", cid)
        record, _ = read_json(root, records / (cid + ".json"))
        if record.get("state") not in {"sealed", "superseded"} or not record.get("sealed_on"):
            raise CampaignError("campaign-cycle-not-sealed", cid)
        directory = directories[cid]
        _safe(path.parent, directory)
        expected_directory = (path.parent / str(record["locator"]) if record.get("locator")
                              else path.parent / "cycles" / cid)
        if directory != expected_directory:
            raise CampaignError("campaign-cycle-locator-invalid", cid)
        document, raw = read_json(root, directory / "manifest.json")
        report = manifest.validate(document)
        if not report.ok:
            raise CampaignError("campaign-manifest-invalid", cid + ": " + str(report.violations[:3]))
        # The stored/indexed seal binds bytes. Legacy approved merges retained
        # noncanonical JSON formatting; never rewrite them to today's encoder.
        mdigest = "sha256:" + hashlib.sha256(raw).hexdigest()
        cycle = document["cycle"]
        if (record.get("manifest_digest") != mdigest
                or cycle.get("cycle_id") != cid or cycle.get("campaign_id") != campaign["campaign_id"]
                or document["campaign"]["campaign_id"] != campaign["campaign_id"]
                or document["artifact_root_id"] != root_id.artifact_root_id
                or document["producer"]["producer_id"] != record.get("producer_id")
                or cycle["state"] not in {"completed", "abandoned", "active"}
                or cycle["state"] != record.get("cycle_state")
                or (cycle["state"] == "active" and record.get("state") != "sealed")):
            raise CampaignError("campaign-seal-mismatch", cid)
        expected = index_module.apply(index_module.empty(root_id.artifact_root_id), document,
                                      cycle_path=str(directory.relative_to(root)),
                                      manifest_digest=manifest.manifest_digest(document), idempotency_key=cid)
        if (index.manifests.get(cid) != expected.manifests[cid]
                or index.cycles.get(cid) != expected.cycles[cid]):
            raise CampaignError("campaign-index-mismatch", cid)
        for revision in document["artifact_revisions"]:
            _safe(directory, directory / revision["locator"]["path"])
        failures = lifecycle.verify_artifact_revisions(document, directory)
        if failures:
            raise CampaignError("campaign-artifact-mismatch", cid + ": " + ";".join(failures[:5]))
        row = {"cycle_id": cid, "state": cycle["state"], "manifest_digest": mdigest,
               "index_digest": manifest.manifest_digest(document),
               "route_id": record["route_id"],
               "manifest_id": document["manifest_id"],
               "manifest_revision_id": document["manifest_revision_id"]}
        if cycle["state"] == "active":
            outcome = _route_outcome(root, record, document)
            if outcome is None:
                pending_open.append((cid, record["route_id"]))
                continue
            outcome_doc, outcome_raw = outcome
            gates = outcome_doc.get("terminal_gates")
            reasons = []
            if isinstance(gates, dict):
                for node_id, gate_row in gates.items():
                    if not isinstance(gate_row, dict) or gate_row.get("passed") is not True:
                        reason = gate_row.get("reason") if isinstance(gate_row, dict) else None
                        reasons.append(f"{node_id}:{reason or 'unknown'}")
                reasons.sort()
            row["route_closed"] = True
            row["terminal_gate_proven"] = outcome_doc.get("terminal_gate_proven")
            row["terminal_gate_reasons"] = reasons
            row["route_outcome_digest"] = "sha256:" + hashlib.sha256(outcome_raw).hexdigest()
        rows.append(row)
    if pending_open:
        raise CampaignError("campaign-cycle-provisional-active", _open_refusal_detail(pending_open))
    return root_id, rows


def approval_text(campaign_id, snapshot_digest):
    return f"campaign-satisfy {campaign_id} {snapshot_digest}"


def _snapshot(root, path):
    campaign, _ = read_json(root, path)
    effective = fold_campaign(root, path, campaign)
    if effective.get("state") != "active":
        raise CampaignError("campaign-not-active", effective.get("state"))
    criterion = campaign.get("completion_criterion", {}).get("statement")
    if (not isinstance(criterion, str) or not criterion.strip()
            or not isinstance(campaign.get("goal"), str) or not campaign["goal"].strip()
            or not identity.is_well_formed(campaign.get("campaign_id"), "campaign")):
        raise CampaignError("campaign-criterion-missing")
    root_id, rows = _cycle_rows(root, path, campaign)
    return {"artifact_root_id": root_id.artifact_root_id, "root": str(root),
            "campaign": campaign, "cycles": rows}


def status(root, selection):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
    raw, _ = read_json(root, path)
    effective = fold_campaign(root, path, raw)
    event = _load_event(root, path)
    if event:
        return {"status": "satisfied", "campaign_id": effective["campaign_id"],
                "projection_pending": raw != effective, "event_id": event["event_id"],
                "recovery_command": ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                                     "campaign-recover", "--artifact-root", str(root), "--campaign", str(path)]}
    snapshot = _snapshot(root, path)
    _members, detached = campaign_records(root, raw["campaign_id"])
    cycles = [{**row, "disposition": disposition(row)} for row in snapshot["cycles"]]
    unproven = [row for row in cycles if row["disposition"] == PROVISIONAL_DISPOSITION]
    instruction = ("Show the user the goal, criterion, cycle outcomes and this exact statement, and end "
                   "your turn there. The user approves by typing the statement, or (Claude sessions) by "
                   "answering with a short closing reply such as '응 닫아', '닫아' or '승인' as their next "
                   "and last input before close; a bare 'ok'/'응' is not approval. The agent must not "
                   "submit either.")
    result = {"status": "awaiting-user-acceptance", "campaign_id": raw["campaign_id"],
              "goal": raw["goal"], "completion_criterion": raw["completion_criterion"],
              "cycles": cycles, "detached_cycles": detached_rows(detached),
              "snapshot_digest": digest(snapshot),
              "approval_statement": approval_text(raw["campaign_id"], digest(snapshot)),
              "instruction": instruction}
    if unproven:
        without_proof = sum(1 for row in unproven if row.get("terminal_gate_proven") is not True)
        result["unproven_cycles"] = {"count": len(unproven), "without_terminal_proof": without_proof}
        result["instruction"] = instruction + (
            f" {len(unproven)} cycle(s) are sealed-unproven: sealed provisionally active before their "
            f"route closed ({without_proof} without terminal proof); accepting closes the campaign with "
            "them recorded as sealed, not completed.")
    return result


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") in {"text", "input_text"}
                                         and isinstance(p.get("text"), str) for p in content):
        return "\n".join(p.get("text", "") for p in content)
    return ""


_CODEX_USER_SOURCES = frozenset(("cli", "vscode"))
_CODEX_USER_ORIGINATORS = frozenset(("codex-tui", "codex_vscode", "Codex Desktop", "codex_cli_rs"))
# Rows Claude Code writes with a user shape that are not the user speaking.
_NOT_USER_PREFIXES = ("<task-notification", "<cross-session-message", "<command-name>", "<local-command",
                      "[Request interrupted")


def _loose_text(content):
    """Assistant turns mix text with tool/thinking parts; keep only the text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in {"text", "output_text", "input_text"}
                         and isinstance(p.get("text"), str))
    return ""


def _native_turns(harness, session):
    """Read the native store, never a caller-created export or approval file.

    Yields ``(role, text, evidence)`` in store order. ``role`` is
    ``assistant`` or ``user``; a user turn's evidence carries ``input_kind``
    (``typed`` / ``queued`` / ``queued-command`` / ``enqueue`` / ``legacy``) and,
    when the store proves it, ``typed_at`` (epoch seconds of the keystroke
    submit). Only Claude stores carry assistant text, timestamps and input
    kinds; Codex and OpenCode yield plain ``legacy`` user turns."""
    if harness == "opencode":
        if not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
            raise CampaignError("approval-session-invalid")
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(["opencode", "export", session], stdout=output,
                                        stderr=subprocess.DEVNULL, timeout=30, check=False)
                if result.returncode or output.tell() > MAX_JSON:
                    raise CampaignError("approval-native-export-unavailable")
                output.seek(0); data = json.load(output)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise CampaignError("approval-native-export-unavailable") from exc
        if not isinstance(data, dict) or not isinstance(data.get("info"), dict) or data["info"].get("id") != session:
            raise CampaignError("approval-session-mismatch")
        if data["info"].get("parentID"):
            return  # a sub-agent session's "user" turns are its parent agent's prompts
        for row in data.get("messages", []):
            if not isinstance(row, dict) or not isinstance(row.get("info"), dict):
                raise CampaignError("approval-native-record-invalid")
            info = row.get("info", {})
            if info.get("sessionID") == session and info.get("role") == "user":
                parts = row.get("parts", [])
                if not isinstance(parts, list) or any(not isinstance(p, dict) or p.get("synthetic") or p.get("ignored") for p in parts):
                    continue
                yield "user", _text(parts), {"source": "opencode-export", "message_id": info.get("id"),
                                             "native_message_digest": digest(row), "input_kind": "legacy"}
            elif info.get("sessionID") == session and info.get("role") == "assistant":
                yield "assistant", _loose_text(row.get("parts", [])), {
                    "source": "opencode-export", "message_id": info.get("id"),
                    "native_message_digest": digest(row)}
        return
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
        raise CampaignError("approval-session-invalid")
    home = Path.home()
    if harness == "codex":
        base = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions"
        candidates = list(base.glob(f"*/*/*/rollout-*-{session}.jsonl"))
    elif harness == "claude":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "projects"
        candidates = list(base.glob(f"*/{session}.jsonl"))
    else:
        raise CampaignError("approval-harness-unsupported", harness)
    if len(candidates) != 1:
        raise CampaignError("approval-native-session-unavailable", session)
    path = _safe(base, candidates[0])
    witnessed = harness == "claude"
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if len(line) > MAX_JSON:
                raise CampaignError("approval-native-record-oversized")
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise CampaignError("approval-native-record-invalid", number) from exc
            if not isinstance(row, dict):
                raise CampaignError("approval-native-record-invalid", number)
            if harness == "codex":
                payload = row.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if row.get("type") == "session_meta":
                    # `codex exec` and spawned sub-agents are programmatic: their
                    # "user" turns are whatever the launching process wrote.
                    # So is a session another program drives through the app
                    # server (the Claude Code codex plugin records originator
                    # "Claude Code", source "vscode"): only interactive clients
                    # are the user. Records predating these fields stay legacy.
                    source = payload.get("source")
                    originator = payload.get("originator")
                    witnessed = (payload.get("id") == session
                                 and (source is None or source in _CODEX_USER_SOURCES)
                                 and (originator is None or originator in _CODEX_USER_ORIGINATORS))
                if (not witnessed or row.get("type") != "response_item" or payload.get("type") != "message"
                        or payload.get("role") not in {"user", "assistant"}):
                    continue
                role = payload["role"]
                text = (_text if role == "user" else _loose_text)(payload.get("content"))
            else:
                role, text, extra = _claude_turn(row, session)
                if role is None:
                    continue
                yield role, text, {"source": str(path), "line": number,
                                   "native_message_digest": "sha256:" + hashlib.sha256(line).hexdigest(),
                                   **extra}
                continue
            yield role, text, {"source": str(path), "line": number,
                               "native_message_digest": "sha256:" + hashlib.sha256(line).hexdigest(),
                               **({"input_kind": "legacy"} if role == "user" else {})}


def _when(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _claude_turn(row, session):
    """One Claude Code store row -> (role, text, extra), or (None, "", {}).

    Human input is recognised only by what Claude Code records about it:
    ``origin.kind == "human"`` with ``promptSource`` typed/queued, a
    ``queued_command`` attachment typed while the agent was busy (2026-09-18,
    session 5ed5b30b line 154), or the ``queue-operation`` enqueue row written
    at the keystroke. Headless (``sdk``), suggestion, task-notification and
    tool-result rows are not human input. Rows written before Claude Code
    recorded origin/promptSource count as ``legacy`` (exact statement only).
    ``typed_at`` is the submit time: the row's own timestamp for a typed
    prompt, the enqueue time for a queued one."""
    if row.get("isMeta") or row.get("isSidechain"):
        return None, "", {}
    kind = row.get("type")
    row_at = _when(row.get("timestamp"))
    if kind == "queue-operation":
        content = row.get("content")
        if (row.get("operation") == "enqueue" and isinstance(content, str)
                and row.get("sessionId") == session and not content.lstrip().startswith(_NOT_USER_PREFIXES)):
            return "user", content, {"input_kind": "enqueue", "typed_at": row_at, "row_at": row_at}
        return None, "", {}
    message = row.get("message", {})
    if kind == "assistant":
        if not isinstance(message, dict) or row.get("sessionId") != session or message.get("role") != "assistant":
            return None, "", {}
        return "assistant", _loose_text(message.get("content")), {"at": _when(row.get("timestamp"))}
    if kind == "user":
        if not isinstance(message, dict) or row.get("sessionId") != session or message.get("role") != "user":
            return None, "", {}
        text = _text(message.get("content"))
        if not text or text.lstrip().startswith(_NOT_USER_PREFIXES):
            return None, "", {}  # tool results, notifications, slash commands, interrupts
        origin = row.get("origin")
        source = row.get("promptSource")
        if origin is None and source is None:
            return "user", text, {"input_kind": "legacy", "typed_at": row_at, "row_at": row_at}
        if not (isinstance(origin, dict) and origin.get("kind") == "human" and source in {"typed", "queued"}):
            return None, "", {}
        return "user", text, {"input_kind": source, "typed_at": row_at if source == "typed" else None,
                              "row_at": row_at}
    if kind == "attachment":
        attachment = row.get("attachment")
        origin = attachment.get("origin") if isinstance(attachment, dict) else None
        if (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
                and isinstance(origin, dict) and origin.get("kind") == "human"
                and isinstance(attachment.get("prompt"), str)
                and session in {row.get("sessionId"), row.get("session_id")}):
            return "user", attachment["prompt"], {"input_kind": "queued-command",
                                                  "typed_at": _when(attachment.get("timestamp")),
                                                  "row_at": row_at}
    return None, "", {}


# Natural-language consent (user instruction 2026-09-18: "이건 무슨 이상한 규정이야?
# 하팅에 수정하라고 해" -- retyping a 90-character digest line is not a
# reasonable ask). A short consent counts only when (1) it is a whole-reply
# closing phrase from the allowlist below, (2) it is the user's first human
# input after the assistant text that showed the exact statement, judged by
# the time the user submitted it, and (3) no other session injected it. Any
# short negation afterwards -- including one still queued while the agent is
# running campaign-close -- withdraws it.
_CONSENT_MAX_CHARS = 60
_CONSENT_PREFIX = r"(?:응|웅|ㅇㅇ|ㅇㅋ|네|넵|넹|예|그래|좋아|좋아요|좋습니다|오케이|ok|okay|yes|yep|sure)"
_CONSENT_VERB = (r"(?:닫아|닫아요|닫아 ?줘|닫아 ?줘요|닫아 ?주세요|닫아도 ?(?:돼|돼요|좋아|좋아요|됩니다)|"
                 r"닫자|닫으세요|닫읍시다|닫습니다|"
                 r"승인|승인이요|승인해|승인해요|승인할게|승인할게요|승인합니다|승인해 ?줘|승인해 ?줘요|승인해 ?주세요|"
                 r"종료해|종료해요|종료해 ?줘|종료해 ?주세요|close|close it|approve|approved|lgtm)")
_CONSENT_RE = re.compile(r"^(?:" + _CONSENT_PREFIX + r"[ ,.!~]*)?" + _CONSENT_VERB + r"$")
_NEGATION_RE = re.compile(
    r"아니|아뇨|않|못|불가|싫|말자|마라|말고|말아|지 ?마|ㄴㄴ|노노|취소|보류|잠깐|기다|멈춰|그만|거절|거부|반려|"
    r"나중|아직|안됨|안 ?(?:닫|해|하|돼|되|승|종|할|함)|"
    r"(?<![a-z])(?:no|nope|nah|not|never|later|don't|dont|cancel|cancell?ed|reject|rejected|deny|denied|"
    r"abort|wait|hold|stop)(?![a-z])")
_QUESTION_RE = re.compile(r"[?？]|어떻게|어때|왜|뭐|무엇|(?:까|나|니|냐|는지|는가|을까|ㄹ까)$")
_PEER_TRAILER = re.compile(r"\(peer-from:\s")


def _normalize_reply(text):
    value = text.strip().lower().replace("\u2019", "'").replace("\u2018", "'")
    value = re.sub(r"\s+", " ", value)
    return re.sub(r"^[\s.,!~…·ㅎㅋ]+|[\s.,!~…·ㅎㅋ]+$", "", value)


def consent_verdict(text):
    """'accept' | 'reject' | None for one short human reply."""
    value = _normalize_reply(text)
    if not value:
        return None
    if _NEGATION_RE.search(value):
        return "reject"  # refusing is the safe direction, so length never hides it
    if len(value) > _CONSENT_MAX_CHARS:
        return None
    if _QUESTION_RE.search(value):
        return None
    return "accept" if _CONSENT_RE.match(value) else None


class _PeerLedger:
    """What other sessions typed into this one, as far as the peer ledger knows.

    Pane prompts are stored exactly like typed human input, so a body digest
    (any recipient; several whitespace forms) or a recent ledger row whose
    summary starts the text (recipient this session or unknown) marks a row
    injected. ``readable`` is False when the ledger could not be read at all;
    short consent is then off, never assumed clean."""

    def __init__(self, session):
        self.session = session
        self.digests = set()
        self.rows = []
        self.readable = True
        try:
            for record in self._records():
                if not isinstance(record, dict):
                    continue
                if record.get("body_sha256"):
                    self.digests.add(str(record["body_sha256"]))
                target = record.get("to") if isinstance(record.get("to"), dict) else {}
                if target.get("session_id") in (None, "", session):
                    summary = _normalize_reply(str(record.get("summary") or ""))
                    at = _when(record.get("ts"))
                    if summary and at is not None:
                        self.rows.append((at, summary))
        except Exception:
            self.readable = False

    @staticmethod
    def _records():
        override = os.environ.get("AGENT_PEER_LEDGER_ROOT")
        if override:
            for shard in sorted(Path(override, "peer-messages").glob("*/*.jsonl")):
                with shard.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("peer_message_for_campaign",
                                                      Path(__file__).with_name("peer-message.py"))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        yield from module._iter_records()

    def injected(self, text, typed_at):
        if _PEER_TRAILER.search(text):
            return True
        forms = {text, text.strip(), text + "\n", text.strip() + "\n", text + " ", text.rstrip("\n")}
        if any(hashlib.sha256(form.encode("utf-8")).hexdigest() in self.digests for form in forms):
            return True
        if typed_at is None:
            return False
        value = _normalize_reply(text)
        for at, summary in self.rows:
            if at - 180 <= typed_at <= at + 10:
                if value == summary:
                    return True  # a short injected reply: its summary is the whole body
                head = min(len(summary), len(value), 40)
                if head >= 12 and value[:head] == summary[:head]:
                    return True
        return False


def verify_approval(harness, session, statement):
    """Accept the exact statement, or a short closing consent to it (Claude only).

    Inputs carrying the peer trailer are another session speaking and are
    dropped. Inputs the peer ledger attributes to another session can never
    accept, but still count when they refuse: refusing is the safe direction.
    A short consent must be the user's last input before close; anything the
    user types after it voids it, and any refusal withdraws either kind of
    acceptance (a long message merely containing a negation does not undo a
    typed statement)."""
    rejection = statement.replace("campaign-satisfy ", "campaign-reject ", 1)
    ledger = _PeerLedger(session)
    shown = []    # (at, contains_statement, evidence, order) for Claude assistant text
    inputs = []
    waiting = {}  # enqueue text -> unconsumed enqueue entries, paired with the row that delivers them
    order = 0
    for role, text, evidence in _native_turns(harness, session):
        order += 1
        if role == "assistant":
            if text.strip():
                shown.append((evidence.get("at"), statement in text, evidence, order))
            continue
        kind = evidence.get("input_kind")
        entry = {"text": text, "typed_at": evidence.get("typed_at"), "row_at": evidence.get("row_at"),
                 "order": order, "reached_model": kind != "enqueue", "evidence": evidence, "kind": kind}
        if kind == "enqueue":
            inputs.append(entry); waiting.setdefault(text, []).append(entry)
            continue
        if kind in {"queued", "queued-command"} and waiting.get(text):
            typed = waiting[text].pop(0)
            typed.update(reached_model=True, evidence=evidence, kind=kind)
            continue
        inputs.append(entry)

    def when(entry):
        value = entry["typed_at"] if entry["typed_at"] is not None else entry["row_at"]
        return (value if value is not None else float("inf"), entry["order"])

    ordered = [e for e in sorted(inputs, key=when) if not _PEER_TRAILER.search(e["text"])]
    decision = None
    for index, entry in enumerate(ordered):
        stripped = entry["text"].strip()
        attributed = ledger.injected(entry["text"], entry["typed_at"])
        if decision is not None and decision[:2] == ("accepted", "presented-consent"):
            decision = ("rejected", "consent-superseded", entry, None)
        if stripped == rejection:
            decision = ("rejected", "exact-statement", entry, None)
            continue
        if stripped == statement:
            if entry["reached_model"] and not attributed:
                decision = ("accepted", "exact-statement", entry, None)
            continue
        verdict = consent_verdict(stripped)
        if verdict == "reject":
            typed_statement = decision is not None and decision[:2] == ("accepted", "exact-statement")
            if not (typed_statement and len(_normalize_reply(stripped)) > _CONSENT_MAX_CHARS):
                decision = ("rejected", "refusal", entry, None)
            continue
        if (verdict != "accept" or attributed or harness != "claude" or not ledger.readable
                or not entry["reached_model"] or entry["kind"] not in {"typed", "queued", "queued-command"}
                or entry["typed_at"] is None):
            continue
        before = [item for item in shown if item[0] is not None and item[0] < entry["typed_at"]]
        if not before:
            continue
        at, contains, presentation, _ = max(before, key=lambda item: (item[0], item[3]))
        if not contains or any(at < when(other)[0] < entry["typed_at"] for other in ordered[:index]):
            continue  # not an answer to the statement, or not the first input after it
        decision = ("accepted", "presented-consent", entry, presentation)
    if decision is None or decision[0] != "accepted":
        raise CampaignError("campaign-user-acceptance-required", statement)
    _, mode, entry, presentation = decision
    evidence = {k: v for k, v in entry["evidence"].items() if k in {"source", "line", "message_id", "native_message_digest"}}
    result = {**evidence, "harness": harness, "session_id": session,
              "actor_id": "native-user:" + hashlib.sha256((harness + ":" + session).encode()).hexdigest(),
              "statement": statement, "decision": "accepted", "acceptance_mode": mode}
    if presentation is not None:
        result["reply_text"] = _normalize_reply(entry["text"])[:_CONSENT_MAX_CHARS]
        result["presentation"] = {k: presentation[k] for k in ("line", "message_id", "native_message_digest")
                                  if k in presentation}
    return result


def _publish_event(root, path, event):
    """No-replace atomic publication; interrupted staging is never authority."""
    target = _event_path(path)
    _safe(root, target)
    target.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".campaign-close-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(event) + b"\n"); stream.flush(); os.fsync(stream.fileno())
        os.link(tmp, target, follow_symlinks=False)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(tmp)


def _index_update(fn, root, campaign_id):
    """`close`'s own errors are `CampaignError`; the locator layer cannot import
    this module (its own lazy-import convention), so it reports the very same
    `fold_campaign` conflict as a `LocatorError`. Re-wrap at this boundary so a
    caller catching `CampaignError` still sees every failure this call can
    produce, including one `scan_campaign` surfaces through `_campaign_view`."""
    try:
        fn(root, [campaign_id])
    except locator.LocatorError as exc:
        raise CampaignError(exc.code, exc.detail) from exc


def _materialize(root, path, event):
    import artifact_producer as producer
    current, _ = read_json(root, path)
    projected = fold_campaign(root, path, current)
    if projected != _projection(event):
        raise CampaignError("campaign-projection-conflict")
    if current != projected:
        producer._write_campaign(root, projected, exclusive=False)
    return {"status": "satisfied", "campaign_id": event["target_id"],
            "event_id": event["event_id"], "projection_pending": False}


def close(root, selection, *, harness=None, session=None, recover=False):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
    # Validate before creating lock/staging or mutating any campaign state.
    event = _load_event(root, path)
    if event is None:
        if recover:
            raise CampaignError("campaign-no-committed-close")
        snapshot = _snapshot(root, path)
        statement = approval_text(snapshot["campaign"]["campaign_id"], digest(snapshot))
        if not harness or not session:
            raise CampaignError("campaign-user-acceptance-required", statement)
        approval = verify_approval(harness, session, statement)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        committed = _load_event(root, path)
        if committed:
            campaign_id = committed["target_id"]
            _index_update(locator.prepare_index_update, root, campaign_id)
            result = _materialize(root, path, committed)
            _index_update(locator.update_indexes, root, campaign_id)
            return result
        if event is not None:
            raise CampaignError("campaign-committed-event-disappeared")
        latest = _snapshot(root, path)
        if latest != snapshot:
            raise CampaignError("campaign-approval-snapshot-changed")
        # A rejection recorded while acquiring the lock must not be ignored.
        approval = verify_approval(harness, session, statement)
        first = snapshot["cycles"][0]
        when = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = {"stream_id": _id("strm", {"root": str(root), "campaign": snapshot["campaign"]["campaign_id"]}),
                 "stream_sequence": 1, "event_type": "campaign.satisfied",
                 "target_id": snapshot["campaign"]["campaign_id"],
                 "actor": {"kind": "user", "id": approval["actor_id"]}, "recorded_at": when,
                 "provenance": {"source_manifest_id": first["manifest_id"],
                                "source_revision_id": first["manifest_revision_id"],
                                "producer_route_id": first["route_id"], "schema_version": 1,
                                "algorithm_version": CONTRACT, "source_digest": digest(snapshot)},
                 "evidence_ids": [], "payload": {"contract": CONTRACT, "root": str(root),
                                                   "snapshot": snapshot, "approval": approval}}
        event["event_id"] = _id("evt", event)
        violations = []
        manifest._v_event_row(event, "$", violations)
        if violations:
            raise CampaignError("campaign-event-invalid", violations[:3])
        campaign_id = event["target_id"]
        _index_update(locator.prepare_index_update, root, campaign_id)
        try:
            _publish_event(root, path, event)
            result = _materialize(root, path, event)
            _index_update(locator.update_indexes, root, campaign_id)
            return result
        except OSError as exc:
            if _load_event(root, path) is not None:
                recovery = ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                            "campaign-recover", "--artifact-root", str(root), "--campaign", str(path)]
                raise CampaignError("campaign-close-committed-recovery-required",
                                    json.dumps({"recovery_command": recovery, "error": str(exc)})) from exc
            raise CampaignError("campaign-close-not-committed", str(exc)) from exc
    finally:
        admission._release_lock(root, lock)
