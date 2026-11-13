"""Read-only projection of unanswered human-gate decisions for Fleet."""
from __future__ import annotations

import contextlib
import contextvars
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
        meta = os.lstat(path)
        return (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns, meta.st_size)
    except (OSError, TypeError, ValueError):
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


# Partial results of ``_generation_candidates``, one per registry segment, keyed by the identity of
# the segment's ``records`` tuple. A segment's owner and route-alias rows are a pure function of
# its records (their fields, their orders), and a segment that a rewrite recognised again shares
# its predecessor's ``records`` object (``_RegistrySegment.resynced``), so the same object means the
# same bytes at the same orders. Each held entry keeps its ``records`` alive, so the id cannot be
# reused for another tuple. Per path only the current generation's segments are held.
_SEGMENT_PARTS = {}
_METRICS = {"generation_segment_builds": 0, "generation_segment_reuses": 0}


def metrics_snapshot():
    return dict(_METRICS)


def _route_key_of(meta):
    return (meta.get("owner_route_id") or meta.get("route_id"),
            meta.get("owner_route_hash") or meta.get("route_hash"),
            meta.get("owner_route_file") or meta.get("route_file"))


def _merged_activity(old, new):
    """The later of two activity times, ignoring a time that is not finite (nan when neither is)."""
    return (old if not math.isfinite(new) else new
            if not math.isfinite(old) else max(new, old))


def _segment_candidates(segment):
    """Owner and route-alias candidates of one registry segment: ``(owners, first_order, successors, routes)``."""
    owners = {}
    first_order = {}
    successors = set()
    for row in segment.observation_owners:
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
    selected = {}
    for rows in segment.completion_by_route_alias.values():
        for row in rows:
            selected[row.order] = row
    for order in sorted(selected):
        row = selected[order]
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
            activity = _merged_activity(previous[2], activity)
        routes[route_key] = (row, meta, activity)
    return owners, first_order, successors, routes


def _generation_candidates(view):
    """Compile only indexed route/owner candidates once per registry generation.

    A new generation merges its segments' partial results in file order, and builds only the
    segments it has not seen: the later segment wins for an attempt or a route, an attempt's first
    order is its earliest, a route's activity is the latest finite one (``_merged_activity`` is
    associative, so folding segment by segment equals folding row by row). The fifth item answers
    the S3 "no later owner on the same route" question in one lookup: per route triple, the
    greatest first order of any owner attempt on it. Order is the line number of a registry row,
    unique within one generation, so "another owner began after this one" is "the greatest first
    order on the route exceeds this owner's own".
    """
    generation_key = (view.path_identity, view.signature)
    cached = _GENERATION_CACHE.get(generation_key)
    if cached is not None:
        _GENERATION_CACHE.move_to_end(generation_key)
        return cached
    held = _SEGMENT_PARTS.get(view.path_identity) or {}
    current = {}
    owners = {}
    first_order = {}
    successors = set()
    routes = {}
    for segment in view._segments:
        key = id(segment.records)
        entry = held.get(key) or current.get(key)
        if entry is None or entry[0] is not segment.records:
            entry = (segment.records, _segment_candidates(segment))
            _METRICS["generation_segment_builds"] += 1
        else:
            _METRICS["generation_segment_reuses"] += 1
        current[key] = entry
        part_owners, part_first, part_successors, part_routes = entry[1]
        owners.update(part_owners)
        for attempt, order in part_first.items():
            first_order.setdefault(attempt, order)
        successors.update(part_successors)
        for route_key, (row, meta, activity) in part_routes.items():
            previous = routes.get(route_key)
            routes[route_key] = (row, meta, _merged_activity(previous[2], activity) if previous else activity)
    _SEGMENT_PARTS[view.path_identity] = current
    latest_start = {}
    for attempt, (_status, meta, _fields, _order, _row) in owners.items():
        route_key = _route_key_of(meta)
        started = first_order[attempt]
        if started > latest_start.get(route_key, -1):
            latest_start[route_key] = started
    result = (owners, first_order, successors, routes, latest_start)
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


def _stable_read(path, cache=None):
    """One stable read. Callers cache the parsed value under the same stamp, so the
    raw text is not retained a second time (``cache`` is accepted for old callers)."""
    before = _stamp(path)
    if before is None:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    after = _stamp(path)
    if before != after:
        return None
    return value


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


def _note_gate_reads(sink, question_path, artifact_root=None):
    """Add a frame-review artifact, its two answer files and (when given) the root it must lie
    in to ``sink`` with their identities, each once; a missing one is kept as missing."""
    if sink is None:
        return
    known = {read for read, _identity in sink}
    paths = [question_path, question_path.parent / "answers.native.json",
             question_path.parent / "answers.json"]
    if artifact_root is not None:
        paths.append(Path(artifact_root))
    for read in paths:
        if read not in known:
            known.add(read)
            sink.append((read, _stamp(read)))


def pending_human_waits(route, *, jobs, outcome=None):
    """Return unresolved raised gates as private route/gate ids and public-safe fields.

    This reads only the exact route's registry-selected workflow journal and, for
    frame-review, answer files beside the artifact named by the latest raise.
    It never repairs ledger state or changes gate state. A caller that keeps the result can
    name what it depends on: ``_WAIT_READS`` collects ``(path, identity)`` of every file besides
    the journal this call looked at (a frame-review artifact and its two answer files, as named
    and as resolved, and the artifact root), including one that was missing or refused.
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
            sink = _WAIT_READS.get()
            if artifact and artifact_root:
                # Identified before the path check, so a gate file that is missing or refused now
                # is an input too: its appearance (an archived interview restored) is seen next time.
                _note_gate_reads(sink, Path(artifact), artifact_root)
            if artifact and artifact_root and _inside(artifact, artifact_root):
                try:
                    question_path = Path(artifact).resolve(strict=True)
                    # Identified before they are read, so a change after the read is seen next time.
                    _note_gate_reads(sink, question_path)
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


# The S2 judgment of one route (``pending_human_waits``) is a pure function of its sealed record, the
# route's outcome sidecars, its workflow journal and, for a raised frame-review gate, the artifact and
# the two answer files. Every one of those is a file read through its exact identity (``_stamp``: device,
# inode, mtime, ctime, size), so a result is kept together with those identities and reused while all of
# them are found again, and computed anew as soon as one differs. The record itself is not kept (it is
# covered by its file's identity, and ``_ROUTE_OUTCOME_CACHE`` holds it for the few routes that need it),
# so the number of routes a pass can answer from memory does not depend on that cache's size.
# Time, process liveness and lineage are not inputs; the caller still computes them every pass.
# One entry per route triple; entries the latest full pass did not see are dropped.
_WAIT_MEMO = {}
# The route files the latest *full* pass read for the routes it saw, for waking only: the producer
# watches them (a NAS file by stat) so a route write starts the next pass early. It is never used
# to skip a judgment. ``_WAIT_MEMO`` is not this: it can hold routes the pass did not see (an
# ``only_id`` pass, or a pass that ended before its cleanup keeps older entries), so it is rebuilt
# from ``seen_routes`` at the cleanup point and emptied by a full pass that did not reach it.
_LAST_PASS_ROUTE_PATHS = ()
_WAIT_READS = contextvars.ContextVar("human_gate_wait_reads", default=None)
_METRICS["route_wait_recomputes"] = 0
_METRICS["route_wait_reuses"] = 0


def _route_file_paths(route_file, record, route_id):
    """The files ``_route_outcome`` keys on, as plain strings: record, outcome sidecar, inline-finish file."""
    route_path = Path(route_file)
    artifact_root = record.get("artifact_root") if record else None
    finish = (str(Path(artifact_root) / ".runtime" / "inline-finish" / "v1" / str(route_id) / "finish.json")
              if artifact_root else None)
    return (str(route_path), str(route_path.with_name(route_path.stem + ".outcome.json")), finish)


def last_pass_route_paths():
    """Sorted route record/outcome/finish/journal paths the latest full pass read (wake only)."""
    return _LAST_PASS_ROUTE_PATHS


def _seen_route_paths(seen_routes):
    paths = set()
    for key in seen_routes:
        if key[2]:
            paths.add(str(key[2]))
        entry = _WAIT_MEMO.get(key)
        if entry is not None:
            paths.update(str(item) for item in entry[1] if item)
            if entry[3]:
                paths.add(str(entry[3]))
            paths.update(str(read[0]) for read in entry[5])
    return paths


def _journal_file(route_key, ledger_root):
    """The workflow journal this pass's registry selects for the route (None without a ledger root)."""
    return str(Path(ledger_root) / str(route_key[0]) / "journal.jsonl") if ledger_root is not None else None


def _kept_waits(route_key, path, ledger_root):
    """The kept judgment of this route when every input is the file it was made from, else None."""
    entry = _WAIT_MEMO.get(route_key)
    if entry is None or ledger_root is None or entry[0] != str(path):
        return None
    _jobs_key, files, stamps, journal, journal_stamp, reads, waits = entry
    # The same registry path can select another ledger (its directory now links elsewhere): the
    # journal the judgment was made from must still be the one this pass selects.
    if journal != _journal_file(route_key, ledger_root):
        return None
    if (_stamp(files[0]) != stamps[0] or _stamp(files[1]) != stamps[1]
            or (files[2] is not None and _stamp(files[2]) != stamps[2])
            or _stamp(journal) != journal_stamp
            or any(_stamp(read) != stamp for read, stamp in reads)):
        return None
    _METRICS["route_wait_reuses"] += 1
    return [dict(wait) for wait in waits]


def _compute_waits(route_key, raw_record, route_record, outcome, path, ledger_root):
    """``pending_human_waits`` for one route, kept with the identity of every file it read."""
    _METRICS["route_wait_recomputes"] += 1
    journal = _journal_file(route_key, ledger_root)
    files = _route_file_paths(route_key[2], raw_record, route_key[0])
    # The identities the record and outcome were loaded under (taken before they were read), so a
    # change after that load is seen on the next pass; stamped now only when they are not at hand.
    held = _ROUTE_OUTCOME_CACHE.get((str(Path(route_key[2]).absolute()), route_key[0], route_key[1]))
    if held is not None and held[1] is raw_record and held[2] is outcome and len(held[0]) == 3:
        stamps = tuple(held[0])
    else:
        stamps = (_stamp(files[0]), _stamp(files[1]), _stamp(files[2]) if files[2] is not None else None)
    journal_stamp = _stamp(journal) if journal is not None else None
    reads = []
    token = _WAIT_READS.set(reads)
    try:
        waits = pending_human_waits(route_record, jobs=path, outcome=outcome)
    finally:
        _WAIT_READS.reset(token)
    if journal is not None:
        _WAIT_MEMO[route_key] = (str(path), files, stamps, journal, journal_stamp, tuple(reads),
                                 tuple(dict(wait) for wait in waits))
    return waits


def _waits_for(route_key, raw_record, route_record, outcome, path, ledger_root):
    """The judgment of one route: the kept one while its inputs hold, else computed (and kept)."""
    kept = _kept_waits(route_key, path, ledger_root)
    if kept is not None:
        return kept
    return _compute_waits(route_key, raw_record, route_record, outcome, path, ledger_root)


def _session_liveness_probe():
    """One pass's owner observation: ``live`` / ``ended`` / ``unknown`` per session.

    Reuses Fleet's shared session-end proof (``collectors.steward.session_liveness``),
    and only from the initial PID namespace, where an absent process is really absent;
    elsewhere, and for an unreadable table, the owner stays ``unknown`` (LOOP §4)."""
    seen = {}
    state = {}

    def probe(harness, session_id):
        key = (harness, session_id)
        if key not in seen:
            try:
                project_root = str(Path(__file__).resolve().parents[1])
                if project_root not in sys.path:
                    sys.path.insert(0, project_root)
                from tools.fleet.collectors import procscan, steward
                if "observation" not in state:
                    # Its registry fallback probes a recorded pid with a signal;
                    # from another PID namespace an absent pid proves nothing.
                    state["host"] = procscan.initial_pid_namespace()
                    state["observation"] = steward.end_observation()
                if not state["host"]:
                    seen[key] = "unknown"
                    return seen[key]
                seen[key] = steward.session_liveness(harness, session_id, agents=(),
                                                     observation=state["observation"])
            except Exception:
                seen[key] = "unknown"
        return seen[key]
    return probe


def _owner_state(item, liveness):
    """``live`` / ``successor`` / ``none`` / ``unknown`` for the session that answers now.

    S2/S3 owners are already the end of the recorded handover lineage
    (``effective_parent``); ``none`` therefore means the proven-ended owner has no
    successor. S1/S4 belong to the exact native session that asked."""
    if item.get("owner_state") in ("live", "successor", "none", "unknown"):
        return item["owner_state"]
    owner = item.get("owner") or {}
    harness, session_id = owner.get("harness"), owner.get("session_id")
    if not harness or not session_id:
        return "unknown"
    observed = liveness(harness, session_id)
    if observed == "ended":
        return "none"
    if observed == "live":
        return "successor" if item.get("registered_owner") else "live"
    return "unknown"


def _registered_owner(meta, harness, session_id):
    """The registered parent when lineage moved the answering session, else None."""
    registered = meta.get("parent_sid")
    if not registered or not session_id or registered == session_id:
        return None
    return {"harness": meta.get("parent_harness") or harness, "session_id": registered}


def _codex_rollout_waits():
    """``{"complete", "pending": {sid: waiting_since}}`` from live Codex rollouts, or None.

    A Codex thread started without the interaction hook leaves its unanswered
    ``request_user_input`` only in its rollout; Fleet's own rollout cursor reads it."""
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from tools.fleet.collectors import codex as codex_rollouts
    return codex_rollouts.open_rollout_questions()


class _SkipSource(Exception):
    """A narrowed read does not observe this source."""


def read_answer_waits(*, jobs_paths, sessions=(), now=None, owner_liveness=None, only_id=None):
    """Project current and folded older answer waits without changing sources.

    ``only_id`` names one S2/S3 wait: the registry is read the same way, but
    only that wait's route and owner are observed, so opening one question
    does not re-observe every older wait on the board.

    A damaged or partial registry is reported as unknown and is never used to
    prove that an owner has no successor. The existing owner-input reader may
    briefly take its advisory lease flock; contention simply omits that source
    for this pass. No answer bodies are returned. Age only folds a wait
    (``human_wait_state.wait_fold``); a proven-ended owner with no successor folds
    it as ownerless. ``owner_liveness(harness, sid)`` defaults to the shared
    session-end proof.
    """
    from collections import defaultdict
    from time import time as _time

    from dispatch_registry_cache import registry_read_scope, registry_snapshot
    import human_wait_state

    instant = _time() if now is None else float(now)
    liveness = owner_liveness or _session_liveness_probe()
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

    only_source = only_id.split(":", 1)[0] if isinstance(only_id, str) else None
    # S1 stores exact identity, timing, and optional native request refs only.
    # Native question text is private to the runtime and never recovered from prose.
    try:
        if only_source in ("S2", "S3"):
            raise _SkipSource
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
                if sidecar is None:
                    # A damaged observation cannot prove that this source has
                    # no current request. Keep health body-free and advisory.
                    if path.exists():
                        health.append({"source": "S1", "state": "unknown",
                                       "reason": "invalid-sidecar"})
                    continue
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
                item = {"id": "S1:" + harness + ":" + sid + ":" + str(int(record["waiting_since"] * 1000)),
                        "source": "S1",
                        "kind": record["kind"], "question": None, "choices": [],
                        "recommendation": None, "route_id": None, "route_hash": None,
                        "attempt_id": None, "gate_id": None, "since": record["waiting_since"],
                        "owner": {"harness": harness, "session_id": sid},
                        "attribution": "exact"}
                item.update({key: record[key] for key in ("request_id", "turn_id", "item_id")
                             if key in record})
                add(item)
    except _SkipSource:
        pass
    except Exception as exc:
        health.append({"source": "S1", "state": "unknown", "reason": type(exc).__name__})

    # S1 also keeps a Codex question that exists only in its live rollout (no hook
    # sidecar). The sidecar for the same session wins; the id has the sidecar shape.
    try:
        if only_source in ("S2", "S3"):
            raise _SkipSource
        observed = _codex_rollout_waits()
        if observed is None:
            health.append({"source": "S1", "state": "unknown", "reason": "codex-rollouts-unreadable"})
        else:
            if not observed["complete"]:
                health.append({"source": "S1", "state": "unknown",
                               "reason": "codex-rollouts-partial"})
            rollout_waits = observed["pending"]
            with_sidecar = {(item.get("owner") or {}).get("session_id") for item in items
                            if item.get("source") == "S1"
                            and (item.get("owner") or {}).get("harness") == "codex"}
            for sid, since in sorted(rollout_waits.items()):
                if sid in with_sidecar:
                    continue
                since = float(since)
                if not math.isfinite(since):
                    continue
                add({"id": "S1:codex:" + sid + ":" + str(int(since * 1000)), "source": "S1",
                     "kind": "decision", "question": None, "choices": [],
                     "recommendation": None, "route_id": None, "route_hash": None,
                     "attempt_id": None, "gate_id": None, "since": since,
                     "owner": {"harness": "codex", "session_id": sid},
                     # A live Codex process holds this rollout open.
                     "owner_state": "live", "attribution": "exact"})
    except _SkipSource:
        pass
    except Exception as exc:
        health.append({"source": "S1", "state": "unknown", "reason": type(exc).__name__})

    # S4's writer owns its private file format; importing is lazy to keep the
    # legacy S1/S2 API independent from the new producer.
    try:
        if only_source in ("S2", "S3"):
            raise _SkipSource
        import human_wait_state
        for record in human_wait_state.read_signals(now=instant):
            message = record.get("mode") == "message"
            add({"id": record["id"], "source": "S4", "kind": "message" if message else "decision",
                 "question": None if message else record.get("question"), "choices": [], "recommendation": None,
                 "route_id": None, "route_hash": None, "attempt_id": None, "gate_id": None,
                 "since": record["since"], "owner": {"harness": record.get("harness"),
                 "session_id": record.get("session_id"), "seat_key": record.get("seat_key")},
                 "attribution": "exact", "stale": bool(record.get("stale"))})
    except _SkipSource:
        pass
    except Exception as exc:
        health.append({"source": "S4", "state": "unknown", "reason": type(exc).__name__})

    pass_paths, pass_total, pass_reached = set(), 0, 0
    for jobs in jobs_paths:
        path = Path(jobs)
        pass_total += 1
        try:
            with registry_read_scope(), contextlib.ExitStack() as lineage_pin:
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
                    # One observation of the lineage per pass: every wait below resolves its
                    # owner against the same one (a writer that lands meanwhile is seen by the
                    # next pass), instead of listing and stating every ledger once per wait.
                    lineage_pin.enter_context(read_context.pinned_lineage())
                except ImportError:
                    effective_parent = effective_parent_harness = None
                    read_context = None
                latest, first_order, successor_sources, route_candidates, latest_start = _generation_candidates(view)
                import route_authority
                for attempt, (status, meta, fields, row_order, row) in latest.items():
                    if meta.get("worker_type") != "owner" or attempt in successor_sources:
                        continue
                    if only_id is not None and only_id != "S3:" + str(attempt):
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
                    if latest_start.get((route_id, route_digest, route_file), -1) > registered_order:
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
                        # Age folds a blocked owner (shared rule below); it never
                        # drops one. Only answer, closure or a successor does.
                        if instant - started < 0:
                            continue
                        route_record, outcome = _route_outcome(route_module, route_file,
                                                               route_id, route_digest)
                        if route_record is None:
                            continue
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
                        lineage_read = True
                        try:
                            if effective_parent is None:
                                raise ImportError("lineage reader unavailable")
                            lineage_meta = row.completion_metadata()
                            parent = effective_parent(lineage_meta, path, read_context=read_context)
                            parent_harness = effective_parent_harness(lineage_meta, path,
                                                                      read_context=read_context)
                        except Exception:
                            # A failed lineage read cannot prove the owner has no
                            # successor: keep the registered parent, owner unknown.
                            lineage_read = False
                            parent, parent_harness = meta.get("parent_sid"), meta.get("parent_harness")
                        add({"id": "S3:" + str(attempt), "source": "S3",
                             "kind": "decision", "question": blocker, "choices": [],
                             "recommendation": None, "route_id": route_id,
                             "route_hash": route_digest, "attempt_id": attempt,
                             "gate_id": None, "since": started,
                             "owner": {"harness": parent_harness or None,
                                       "session_id": parent or None,
                                       "seat_key": None},
                             "registered_owner": _registered_owner(meta, parent_harness, parent),
                             "cwd": route_record.get("cwd"),
                             **({} if lineage_read else {"owner_state": "unknown"}),
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
                try:
                    ledger_root = WS.ledger_root_for(path)[0]
                except (OSError, ValueError, TypeError, KeyError):
                    ledger_root = None
                seen_routes = set()
                for (route_id, route_digest, route_file), (row, _candidate_meta, route_activity) in route_candidates.items():
                    if only_id is not None and not only_id.startswith("S2:" + str(route_id) + ":"):
                        continue
                    if not math.isfinite(route_activity):
                        health.append({"source": "S2", "route_id": route_id,
                                       "state": "unknown", "reason": "invalid-timestamp"})
                        continue
                    if route_activity > instant:
                        continue
                    try:
                        # The route row is already a narrow candidate; full lineage
                        # metadata is needed only when it actually has a pending wait.
                        meta = None
                        route_key = (route_id, route_digest, route_file)
                        # A route none of whose input files changed keeps its judgment, and
                        # its record is not even loaded unless a wait has to be shown.
                        waits = _kept_waits(route_key, path, ledger_root)
                        route_record = None
                        if waits is None or waits:
                            route_record, outcome = _route_outcome(route_module, route_file,
                                                                   route_id, route_digest)
                            if route_record is None:
                                continue
                            # The loader's private schema tag is not covered by the
                            # sealed route hash; pending_human_waits must see sealed bytes.
                            raw_record = route_record
                            route_record = {name: value for name, value in route_record.items()
                                            if name != "_fleet_schema_status"}
                            if waits is None:
                                waits = _compute_waits(route_key, raw_record, route_record, outcome,
                                                       path, ledger_root)
                        seen_routes.add(route_key)
                        for wait in waits:
                            if meta is None:
                                meta = row.completion_metadata()
                            parent_sid = meta.get("parent_sid")
                            parent_harness = meta.get("parent_harness")
                            lineage_read = effective_parent is not None
                            if lineage_read:
                                try:
                                    parent_sid = effective_parent(meta, path, read_context=read_context)
                                    parent_harness = effective_parent_harness(meta, path,
                                                                              read_context=read_context)
                                except Exception:
                                    lineage_read = False
                            since = float(wait["waiting_since"])
                            if instant - since < 0:
                                continue
                            question, choices, recommendation = (
                                _frame_question_detail(route_record, path, wait["gate_id"])
                                if wait["gate_id"] == "frame-review" else (None, [], None))
                            add({"id": "S2:" + route_id + ":" + wait["gate_id"] + ":" +
                                 str(int(wait["waiting_since"] * 1000)),
                                 "source": "S2", "kind": wait["kind"], "question": question,
                                 "choices": choices, "recommendation": recommendation, "route_id": route_id,
                                 "route_hash": route_digest, "attempt_id": meta.get("attempt_id"),
                                 "gate_id": wait["gate_id"], "since": since,
                                 "owner": {"harness": parent_harness or None,
                                           "session_id": parent_sid or None},
                                 "registered_owner": _registered_owner(meta, parent_harness, parent_sid),
                                 "cwd": route_record.get("cwd"),
                                 **({} if lineage_read else {"owner_state": "unknown"}),
                                 "attribution": "lineage" if parent_sid and parent_harness else "unattributed"})
                    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
                        health.append({"source": "S2", "route_id": route_id,
                                       "state": "unknown", "reason": type(exc).__name__})
                if only_id is None:
                    for stale in [key for key in _WAIT_MEMO if key not in seen_routes]:
                        del _WAIT_MEMO[stale]
                    pass_paths.update(_seen_route_paths(seen_routes))
                    pass_reached += 1
        except (OSError, ValueError, UnicodeError) as exc:
            health.append({"source": "registry", "path": str(path),
                           "state": "unknown", "reason": type(exc).__name__})

    if only_id is None:
        global _LAST_PASS_ROUTE_PATHS
        _LAST_PASS_ROUTE_PATHS = (tuple(sorted(pass_paths))
                                  if pass_total and pass_reached == pass_total else ())

    retained = []
    for item in items:
        try:
            if instant - float(item.get("since", instant)) < 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        item["owner_state"] = _owner_state(item, liveness)
        item["fold"] = human_wait_state.wait_fold(item["since"], instant,
                                                  owner_state=item["owner_state"])
        item["stale"] = item["fold"] is not None
        if not item.get("registered_owner"):
            item.pop("registered_owner", None)
        retained.append(item)
    items = retained
    by_session = defaultdict(list)
    for item in items:
        owner = item.get("owner") or {}
        key = (owner.get("harness"), owner.get("session_id"))
        if key[0] and key[1] and not item.get("stale"):
            by_session[key].append(item["id"])
    counts = human_wait_state.wait_counts(items)
    unattributed = [item["id"] for item in items if item.get("attribution") == "unattributed" and not item.get("stale")]
    return {"items": items, "by_session": dict(by_session), "unattributed": unattributed,
            "total": counts["total"], "stale_total": counts["older"],
            "ownerless": counts["ownerless"], "health": health, "observed_at": instant,
            "complete": not any(row.get("state") == "unknown" for row in health)}
