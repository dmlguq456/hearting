"""Read-only projection of unanswered human-gate decisions for Fleet."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "utilities") not in sys.path:
    sys.path.insert(0, str(ROOT / "utilities"))

import frame_interview as FI
import workflow_state as WS

_CACHE_LIMIT = 2048
_JOURNAL_CACHE = OrderedDict()
_JSON_CACHE = OrderedDict()
_OBSERVATION_CACHE = OrderedDict()
_GENERATION_CACHE = OrderedDict()
_ROUTE_OUTCOME_CACHE = OrderedDict()
_GENERATION_LIMIT = 2
_ROUTE_CACHE_LIMIT = 2048
_OBSERVATION_META_KEYS = frozenset((
    "attempt_id", "worker_type", "automatic_retry_of", "replacement_original_attempt_id",
    "prior_attempt_id", "owner_route_file", "owner_route_id", "owner_route_hash",
    "route_file", "route_id", "route_hash", "ended_at", "parent_sid", "parent_harness",
    "harness", "log_file", "note", "failure_class", "classifier_source", "process_exit",
    "reconcile_reason", "terminal_event",
))
_S1_LOG_TAIL_MAX_BYTES = 2 * 1024 * 1024
_S1_LOG_CHUNK_BYTES = 64 * 1024


def _stamp(path):
    """Stable identity for an existing or missing input; never follows symlinks."""
    try:
        meta = Path(path).lstat()
        return (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns, meta.st_size)
    except OSError:
        return None


def _cached(cache, key, loader):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = loader()
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_LIMIT:
        cache.popitem(last=False)
    return value


def _generation_candidates(view):
    """Compile only indexed route/owner candidates once per registry generation."""
    generation_key = (view.path_identity, view.signature)
    cached = _GENERATION_CACHE.get(generation_key)
    if cached is not None:
        _GENERATION_CACHE.move_to_end(generation_key)
        return cached
    owners = {}
    first_order = {}
    successors = set()
    for row in view.answer_wait_owner_candidates():
        parsed = row.completion_metadata()
        meta = {key: parsed[key] for key in _OBSERVATION_META_KEYS if key in parsed}
        attempt = meta.get("attempt_id")
        if not attempt:
            continue
        first_order.setdefault(attempt, row.order)
        # Keep the indexed row so lineage consumers can parse its full metadata
        # only after the cheap owner/route/time filters have selected it.
        owners[attempt] = (row.status, meta, row.fields, row.order, row)
        successors.update(meta[name] for name in
                          ("automatic_retry_of", "replacement_original_attempt_id", "prior_attempt_id")
                          if meta.get(name))
    routes = {}
    for row in view.route_alias_candidates():
        parsed = row.completion_metadata()
        meta = {key: parsed[key] for key in _OBSERVATION_META_KEYS if key in parsed}
        route_file = meta.get("owner_route_file") or meta.get("route_file")
        route_id = meta.get("owner_route_id") or meta.get("route_id")
        digest = meta.get("owner_route_hash") or meta.get("route_hash")
        if not route_file or not route_id or not digest:
            continue
        activity_values = []
        for value in (row.fields[0], meta.get("ended_at")):
            try:
                activity_values.append(float(value))
            except (TypeError, ValueError, OverflowError):
                try:
                    activity_values.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
                except (TypeError, ValueError, OverflowError):
                    pass
        activity = max(activity_values, default=float("nan"))
        route_key = (route_id, digest, route_file)
        previous = routes.get(route_key)
        if previous:
            old_activity = previous[2]
            activity = (old_activity if not math.isfinite(activity) else activity
                        if not math.isfinite(old_activity) else max(activity, old_activity))
        routes[route_key] = (row, meta, activity)
    result = (owners, first_order, successors, routes)
    _GENERATION_CACHE[generation_key] = result
    _GENERATION_CACHE.move_to_end(generation_key)
    while len(_GENERATION_CACHE) > _GENERATION_LIMIT:
        _GENERATION_CACHE.popitem(last=False)
    return result


def _route_outcome(route_module, route_file, route_id, route_digest):
    """Reuse route and closure data while all three source stamps stay unchanged."""
    route_path = Path(route_file)
    outcome_path = route_path.with_name(route_path.stem + ".outcome.json")
    key = (str(route_path.absolute()), route_id, route_digest)
    cached = _ROUTE_OUTCOME_CACHE.get(key)
    route_record = cached[1] if cached else None
    artifact_root = route_record.get("artifact_root") if route_record else None
    finish_path = (Path(artifact_root) / ".runtime" / "inline-finish" / "v1" /
                   str(route_id) / "finish.json") if artifact_root else None
    stamps = (_stamp(route_path), _stamp(outcome_path), _stamp(finish_path) if finish_path else None)
    if cached and cached[0] == stamps:
        _ROUTE_OUTCOME_CACHE.move_to_end(key)
        return cached[1], cached[2]
    route_record = route_module.load(route_file, route_digest, route_id)
    outcome = route_module.load_outcome(route_file, route_id, route_digest) if route_record else None
    if route_record:
        finish_path = (Path(route_record.get("artifact_root", "")) / ".runtime" /
                       "inline-finish" / "v1" / str(route_id) / "finish.json")
        stamps = (_stamp(route_path), _stamp(outcome_path), _stamp(finish_path))
    _ROUTE_OUTCOME_CACHE[key] = (stamps, route_record, outcome)
    _ROUTE_OUTCOME_CACHE.move_to_end(key)
    while len(_ROUTE_OUTCOME_CACHE) > _ROUTE_CACHE_LIMIT:
        _ROUTE_OUTCOME_CACHE.popitem(last=False)
    return route_record, outcome


def _stable_read(path, cache):
    before = _stamp(path)
    if before is None:
        return None
    key = (str(path), before)
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    try:
        value = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    after = _stamp(path)
    if before != after:
        return None
    return _cached(cache, key, lambda: value)


def _journal(ledger):
    stamp = _stamp(ledger.journal_path)
    if stamp is None:
        return []
    key = (str(ledger.journal_path), stamp)
    def parse():
        raw = _stable_read(ledger.journal_path, _JOURNAL_CACHE)
        if raw is None:
            return []
        entries = []
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries
    return _cached(_OBSERVATION_CACHE, ("journal", key), parse)


def _json_file(path):
    stamp = _stamp(path)
    if stamp is None:
        return None
    key = ("json", str(path), stamp)
    def parse():
        raw = _stable_read(path, _JSON_CACHE)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return _cached(_OBSERVATION_CACHE, key, parse)


def _record_epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _tail_json_records(path):
    """Yield JSON object records newest-first from a fixed-size file tail."""
    try:
        size = Path(path).stat().st_size
        lower_bound = max(0, size - _S1_LOG_TAIL_MAX_BYTES)
        position = size
        partial = b""
        with Path(path).open("rb") as handle:
            while position > lower_bound:
                start = max(lower_bound, position - _S1_LOG_CHUNK_BYTES)
                handle.seek(start)
                chunk = handle.read(position - start)
                pieces = (chunk + partial).split(b"\n")
                partial = pieces[0]
                for raw in reversed(pieces[1:]):
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        yield value
                position = start
            if lower_bound == 0 and partial:
                try:
                    value = json.loads(partial)
                except (ValueError, UnicodeError):
                    return
                if isinstance(value, dict):
                    yield value
    except (OSError, ValueError):
        return


def _session_log_path(harness, session_id):
    """Find the exact native log for one sidecar; ambiguity proves nothing."""
    if harness == "codex":
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        pattern = "rollout-*-" + str(session_id) + ".jsonl"
        root = home / "sessions"
    elif harness == "claude":
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        pattern = str(session_id) + ".jsonl"
        root = home / "projects"
    else:
        return None
    try:
        matches = [path for path in root.rglob(pattern) if not path.is_symlink() and path.is_file()]
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _later_session_turn(harness, session_id, waiting_since):
    """Use exact later-turn records only; unknown or capped logs do not close."""
    path = _session_log_path(harness, session_id)
    if path is None:
        return False
    for record in _tail_json_records(path):
        timestamp = _record_epoch(record.get("timestamp"))
        if timestamp is None:
            continue
        if timestamp < waiting_since:
            return False
        if harness == "codex":
            payload = record.get("payload")
            if (record.get("type") == "event_msg" and isinstance(payload, dict)
                    and payload.get("type") == "task_started" and timestamp > waiting_since):
                return True
        elif harness == "claude" and timestamp > waiting_since:
            if record.get("type") != "user" or record.get("isMeta") is True:
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else record.get("content")
            if isinstance(content, str) and content.strip():
                return True
            if isinstance(content, list):
                # A user turn is textual input. Tool results, attachments, and
                # metadata-only blocks are not evidence that a person replied.
                if any(isinstance(block, dict) and block.get("type") == "text"
                       and isinstance(block.get("text"), str) and block["text"].strip()
                       for block in content):
                    return True
    return False

def _inside(path, root):
    try:
        resolved, base = Path(path).resolve(strict=True), Path(root).resolve(strict=True)
        return resolved.is_relative_to(base) and not Path(path).is_symlink()
    except (OSError, ValueError):
        return False


def _valid_frame_answer(interview_path, route, answer_path):
    try:
        interview = _json_file(interview_path)
        answers = _json_file(answer_path)
        if not FI.is_interview(interview):
            return False
        if interview.get("route_id") != route.get("route_id"):
            return False
        if FI.pending_answer_response(interview, answers) or FI.validate_answers(interview, answers):
            return False
        actor = FI.answer_actor_kind(answers)
        return FI.answer_releases_gate(actor, "frame-review")
    except (OSError, ValueError, TypeError):
        return False


def _frame_question_detail(route, jobs, gate_id):
    """Read only the first still-unanswered public question of a sealed interview."""
    try:
        ledger_root, _ = WS.ledger_root_for(jobs)
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], root=ledger_root)
        journal = [row for row in _journal(ledger)
                   if row.get("route_id") == route["route_id"]
                   and row.get("route_hash") == route["route_hash"]]
        resolution = WS.human_gate_resolution(journal, gate_id)
        artifact = resolution.get("artifact")
        if not artifact or not route.get("artifact_root") or not _inside(artifact, route["artifact_root"]):
            return None, [], None
        path = Path(artifact).resolve(strict=True)
        digest = resolution.get("artifact_sha256")
        if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return None, [], None
        interview = _json_file(path)
        if not FI.is_interview(interview):
            return None, [], None
        answers = _json_file(path.parent / "answers.native.json")
        if answers is None:
            answers = _json_file(path.parent / "answers.json")
        given = answers.get("answers") if isinstance(answers, dict) else {}
        given = given if isinstance(given, dict) else {}
        for question in interview.get("questions") or ():
            if not isinstance(question, dict):
                continue
            qid = question.get("id")
            answer = given.get(qid)
            if isinstance(answer, dict) and (answer.get("choice") is not None or answer.get("note")):
                continue
            options = question.get("options") if isinstance(question.get("options"), list) else []
            choices = [str(option.get("label", ""))[:40] for option in options if isinstance(option, dict)]
            recommendation = None
            recommended = question.get("recommended")
            if isinstance(recommended, int) and not isinstance(recommended, bool) \
                    and 0 <= recommended < len(options) and isinstance(options[recommended], dict):
                recommendation = str(options[recommended].get("label", ""))[:40]
            return str(question.get("question", ""))[:160], choices, recommendation
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None, [], None


def pending_human_waits(route, *, jobs, outcome=None):
    """Return unresolved raised gates as private route/gate ids and public-safe fields.

    This reads only the exact route's registry-selected workflow journal and, for
    frame-review, answer files beside the artifact named by the latest raise.
    It never repairs ledger state or changes gate state.
    """
    if not isinstance(route, dict) or not route.get("route_id") or not route.get("route_hash"):
        return []
    try:
        from route_identity import route_hash
        if route_hash(route) != route["route_hash"]:
            return []
        ledger_root, _ = WS.ledger_root_for(jobs)
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], root=ledger_root)
        journal = [row for row in _journal(ledger)
                   if row.get("route_id") == route["route_id"]
                   and row.get("route_hash") == route["route_hash"]]
    except (OSError, ValueError, TypeError, KeyError):
        return []
    terminal = any(row.get("workflow_state") in {"CANCELLED", "COMPLETE", "FAILED_TERMINAL"}
                   for row in journal)
    if outcome and outcome.get("closed_at") and outcome.get("match") is True \
            and not outcome.get("finish_pending"):
        terminal = True
    if terminal:
        return []
    latest_lifecycle = next((row.get("workflow_state") for row in reversed(journal)
                            if row.get("workflow_state")), None)
    if latest_lifecycle in {"CANCELLED", "COMPLETE", "FAILED_TERMINAL"}:
        return []
    gates = []
    raised = []
    for row in journal:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        gate = evidence.get("gate")
        if row.get("workflow_state") == "BLOCKED_HUMAN_GATE" and isinstance(gate, str):
            raised.append(gate)
    for gate in dict.fromkeys(raised):
        resolution = WS.human_gate_resolution(journal, gate)
        if resolution.get("status") != "blocked":
            continue
        raised_at = resolution.get("raised_at")
        try:
            parsed = datetime.fromisoformat(str(raised_at).replace("Z", "+00:00"))
            waiting_since = parsed.timestamp()
        except (ValueError, TypeError, OverflowError):
            continue
        if gate == "frame-review":
            artifact = resolution.get("artifact")
            artifact_root = route.get("artifact_root")
            if artifact and artifact_root and _inside(artifact, artifact_root):
                try:
                    question_path = Path(artifact).resolve(strict=True)
                    digest = resolution.get("artifact_sha256")
                    current = not digest or hashlib.sha256(question_path.read_bytes()).hexdigest() == digest
                    if current and FI.is_interview(_json_file(question_path)):
                        if (_valid_frame_answer(question_path, route, question_path.parent / "answers.native.json")
                                or _valid_frame_answer(question_path, route, question_path.parent / "answers.json")):
                            continue
                except (OSError, ValueError, TypeError):
                    # Invalid/missing answer evidence never clears a wait.
                    pass
        gates.append({"gate_id": gate, "kind": "decision", "source": "hearting-ledger",
                      "waiting_since": waiting_since, "route_id": route["route_id"]})
    return gates


def read_answer_waits(*, jobs_paths, sessions=(), now=None):
    """Project current answer waits from existing sources without changing them.

    A damaged or partial registry is reported as unknown and is never used to
    prove that an owner has no successor. The existing owner-input reader may
    briefly take its advisory lease flock; contention simply omits that source
    for this pass. No answer bodies are returned.
    """
    from collections import defaultdict
    from time import time as _time

    from dispatch_registry_cache import registry_read_scope, registry_snapshot

    instant = _time() if now is None else float(now)
    health = []
    items = []
    by_session = defaultdict(list)
    sessions_by_key = {
        (str(getattr(sess, "harness", "") or ""), str(getattr(sess, "session_id", "") or "")): sess
        for sess in sessions
    }
    seen = set()

    def add(item):
        if item["id"] in seen:
            return
        seen.add(item["id"])
        items.append(item)
        owner = item.get("owner") or {}
        key = (owner.get("harness"), owner.get("session_id"))
        if key[0] and key[1]:
            by_session[key].append(item["id"])

    def epoch(value, fallback):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                return float(fallback)

    # S1 stores only exact identity and timing. Native question text is private
    # to the runtime and is never recovered from a prompt or sidecar.
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction
        root = Path(interaction.state_root())
        for harness in ("claude", "codex", "opencode"):
            directory = root / harness
            try:
                children = tuple(directory.iterdir())
            except FileNotFoundError:
                continue
            except OSError as exc:
                health.append({"source": "S1", "state": "unknown", "reason": type(exc).__name__})
                continue
            for path in children:
                if path.suffix != ".json" or path.is_symlink():
                    continue
                sid = path.stem
                sess = sessions_by_key.get((harness, sid))
                sidecar = interaction.read_wait(sid, harness)
                activity_since = None
                if sidecar and sidecar.get("source") != "codex-appserver":
                    activity_since = (getattr(sess, "_interaction_activity", None)
                                      if sess is not None else None)
                    if (activity_since is None and harness in ("codex", "claude")
                            and _later_session_turn(harness, sid, sidecar["waiting_since"])):
                        activity_since = instant
                record = interaction.pending_wait(
                    sid, harness,
                    session_start=getattr(sess, "started_at", None) if sess is not None else None,
                    activity_since=activity_since,
                    now=instant)
                if not record:
                    continue
                add({"id": "S1:" + harness + ":" + sid + ":" + str(int(record["waiting_since"] * 1000)),
                     "source": "S1",
                     "kind": record["kind"], "question": None, "choices": [],
                     "recommendation": None, "route_id": None, "route_hash": None,
                     "attempt_id": None, "gate_id": None, "since": record["waiting_since"],
                     "owner": {"harness": harness, "session_id": sid},
                     "attribution": "exact"})
    except Exception as exc:
        health.append({"source": "S1", "state": "unknown", "reason": type(exc).__name__})

    # S4's writer owns its private file format; importing is lazy to keep the
    # legacy S1/S2 API independent from the new producer.
    try:
        import human_wait_state
        for record in human_wait_state.read_signals(now=instant):
            add({"id": record["id"], "source": "S4", "kind": "decision",
                 "question": record.get("question"), "choices": [], "recommendation": None,
                 "route_id": None, "route_hash": None, "attempt_id": None, "gate_id": None,
                 "since": record["since"], "owner": {"harness": record.get("harness"),
                 "session_id": record.get("session_id"), "seat_key": record.get("seat_key")},
                 "attribution": "exact", "stale": bool(record.get("stale"))})
    except Exception as exc:
        health.append({"source": "S4", "state": "unknown", "reason": type(exc).__name__})

    for jobs in jobs_paths:
        path = Path(jobs)
        try:
            with registry_read_scope():
                snapshot = registry_snapshot(path)
                if not snapshot.complete:
                    health.append({"source": "registry", "path": str(path),
                                   "state": "unknown", "reason": snapshot.reason})
                    continue
                view = snapshot.view
                try:
                    from dispatch_seat_handover import effective_parent, effective_parent_harness
                    from peer_read_context import PeerReadContext
                    read_context = PeerReadContext()
                except ImportError:
                    effective_parent = effective_parent_harness = None
                    read_context = None
                latest, first_order, successor_sources, route_candidates = _generation_candidates(view)
                import route_authority
                for attempt, (status, meta, fields, row_order, row) in latest.items():
                    if meta.get("worker_type") != "owner" or attempt in successor_sources:
                        continue
                    if route_authority.answerable_owner_end(status, meta) != "BLOCKED":
                        continue
                    route_file = meta.get("owner_route_file") or meta.get("route_file")
                    route_id = meta.get("owner_route_id") or meta.get("route_id")
                    route_digest = meta.get("owner_route_hash") or meta.get("route_hash")
                    if not route_file or not route_id or not route_digest:
                        continue
                    # The same jobs identity and exact route binding must have no
                    # later owner attempt. Children and different route/hash rows do not close it.
                    registered_order = first_order.get(attempt, row_order)
                    later = any(other_id != attempt and first_order.get(other_id, other_order) > registered_order
                                and (other_meta.get("owner_route_id") or other_meta.get("route_id")) == route_id
                                and (other_meta.get("owner_route_hash") or other_meta.get("route_hash")) == route_digest
                                and (other_meta.get("owner_route_file") or other_meta.get("route_file")) == route_file
                                for other_id, (_s, other_meta, _f, other_order, _row) in latest.items())
                    if later:
                        continue
                    try:
                        project_root = str(Path(__file__).resolve().parents[1])
                        if project_root not in sys.path:
                            sys.path.insert(0, project_root)
                        from tools.fleet import route as route_module
                        from dispatch_owner_input import blocked_owner_answers
                        started = epoch(meta.get("ended_at") or fields[0], float("nan"))
                        if not math.isfinite(started):
                            health.append({"source": "S3", "attempt_id": attempt,
                                           "state": "unknown", "reason": "invalid-timestamp"})
                            continue
                        age = instant - started
                        if age < 0 or age > 7 * 86400:
                            continue
                        route_record, outcome = _route_outcome(route_module, route_file,
                                                               route_id, route_digest)
                        if route_record is None:
                            continue
                        started = epoch(meta.get("ended_at") or fields[0], float("nan"))
                        folded = age > 72 * 3600
                        if outcome and outcome.get("closed_at") and outcome.get("match") is True:
                            continue
                        if blocked_owner_answers(path, attempt):
                            continue
                        blocker = "owner가 답을 기다림"
                        log_file = meta.get("log_file")
                        if log_file:
                            try:
                                import codex_dispatch_terminal
                                terminal = codex_dispatch_terminal.inspect_terminal_log(log_file)
                                if terminal:
                                    raw = terminal.get("blocker") or terminal.get("failure_note")
                                    if raw:
                                        blocker = " ".join(str(raw).split())[:160]
                            except Exception:
                                pass
                        try:
                            if effective_parent is None:
                                raise ImportError("lineage reader unavailable")
                            lineage_meta = row.completion_metadata()
                            parent = effective_parent(lineage_meta, path, read_context=read_context)
                            parent_harness = effective_parent_harness(lineage_meta, path,
                                                                      read_context=read_context)
                        except Exception:
                            parent, parent_harness = meta.get("parent_sid"), meta.get("parent_harness")
                        add({"id": "S3:" + str(attempt), "source": "S3",
                             "kind": "decision", "question": blocker, "choices": [],
                             "recommendation": None, "route_id": route_id,
                             "route_hash": route_digest, "attempt_id": attempt,
                             "gate_id": None, "since": started, "stale": folded,
                             "owner": {"harness": parent_harness or None,
                                       "session_id": parent or None,
                                       "seat_key": None},
                             "attribution": "lineage" if parent and parent_harness else "unattributed"})
                    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
                        health.append({"source": "S3", "attempt_id": attempt,
                                       "state": "unknown", "reason": type(exc).__name__})
                # S2 is independent of owner terminal state and uses the same
                # exact route/journal reader as the established Fleet enrichment.
                project_root = str(Path(__file__).resolve().parents[1])
                if project_root not in sys.path:
                    sys.path.insert(0, project_root)
                from tools.fleet import route as route_module
                for (route_id, route_digest, route_file), (row, _candidate_meta, route_activity) in route_candidates.items():
                    if not math.isfinite(route_activity):
                        health.append({"source": "S2", "route_id": route_id,
                                       "state": "unknown", "reason": "invalid-timestamp"})
                        continue
                    if instant - route_activity > 8 * 86400:
                        continue
                    try:
                        # The route row is already a narrow candidate; full lineage
                        # metadata is needed only when it actually has a pending wait.
                        meta = None
                        route_record, outcome = _route_outcome(route_module, route_file,
                                                               route_id, route_digest)
                        if route_record is None:
                            continue
                        for wait in pending_human_waits(route_record, jobs=path, outcome=outcome):
                            if meta is None:
                                meta = row.completion_metadata()
                            parent_sid = meta.get("parent_sid")
                            parent_harness = meta.get("parent_harness")
                            if effective_parent is not None:
                                try:
                                    parent_sid = effective_parent(meta, path, read_context=read_context)
                                    parent_harness = effective_parent_harness(meta, path,
                                                                              read_context=read_context)
                                except Exception:
                                    pass
                            since = float(wait["waiting_since"])
                            age = instant - since
                            if age < 0 or age > 7 * 86400:
                                continue
                            stale = age > 72 * 3600
                            question, choices, recommendation = (
                                _frame_question_detail(route_record, path, wait["gate_id"])
                                if wait["gate_id"] == "frame-review" else (None, [], None))
                            add({"id": "S2:" + route_id + ":" + wait["gate_id"] + ":" +
                                 str(int(wait["waiting_since"] * 1000)),
                                 "source": "S2", "kind": wait["kind"], "question": question,
                                 "choices": choices, "recommendation": recommendation, "route_id": route_id,
                                 "route_hash": route_digest, "attempt_id": meta.get("attempt_id"),
                                 "gate_id": wait["gate_id"], "since": since, "stale": stale,
                                 "owner": {"harness": parent_harness or None,
                                           "session_id": parent_sid or None},
                                 "attribution": "lineage" if parent_sid and parent_harness else "unattributed"})
                    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
                        health.append({"source": "S2", "route_id": route_id,
                                       "state": "unknown", "reason": type(exc).__name__})
        except (OSError, ValueError, UnicodeError) as exc:
            health.append({"source": "registry", "path": str(path),
                           "state": "unknown", "reason": type(exc).__name__})

    current_total = 0
    stale_total = 0
    retained = []
    for item in items:
        try:
            old = instant - float(item.get("since", instant))
            if old < 0 or old > 7 * 86400:
                continue
            item["stale"] = old >= 72 * 3600
        except (TypeError, ValueError, OverflowError):
            continue
        retained.append(item)
    items = retained
    by_session = defaultdict(list)
    for item in items:
        owner = item.get("owner") or {}
        key = (owner.get("harness"), owner.get("session_id"))
        if key[0] and key[1] and not item.get("stale"):
            by_session[key].append(item["id"])
    current_total = 0
    stale_total = 0
    for item in items:
        if item["stale"]:
            stale_total += 1
        else:
            current_total += 1
    current_ids = {item["id"] for item in items if not item.get("stale")}
    by_session = {key: [item_id for item_id in values if item_id in current_ids]
                  for key, values in by_session.items()}
    unattributed = [item["id"] for item in items if item.get("attribution") == "unattributed" and not item.get("stale")]
    return {"items": items, "by_session": dict(by_session), "unattributed": unattributed,
            "total": current_total, "stale_total": stale_total, "health": health, "observed_at": instant,
            "complete": not any(row.get("state") == "unknown" for row in health)}
