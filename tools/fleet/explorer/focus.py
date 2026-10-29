"""Single selection-scoped asynchronous collection boundary.

``collect_focus`` reads the selected main session's own records from disk: its registry
rows, the owner routes those rows name, the cycle records those routes' ``parent_cycle_id``
point at, completion-gate markers for the selected request, and the session's unfinished
peer-steward duties. Nothing outside the selected session is read, and no raw attempt
output is opened here (that belongs to the output reader). It never runs on the curses
thread: ``SelectionPump`` runs it through one ``RefreshPump`` (single flight, coalesced
follow-up) and publishes only a result whose focus key and epoch are still current.
"""
from collections import OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import time

from .output import OutputReader, read_generation, utility
from .types import AttemptRow, FocusRequest, FocusResult, Health
from ..refresh import RefreshPump

_CYCLE_ID = re.compile(r"^cyc_[0-9a-f]{32}$")
_ROUTE_ID = re.compile(r"^rt-[0-9a-f]{16}$")
_MAX_RECORD_BYTES = 256 * 1024
_MAX_OBLIGATION_FILES = 256
_MAX_CYCLE_FILES = 2048
_DONE_DUTY_STATES = frozenset(("complete", "cancelled"))


def harness_abbr(value):
    value = str(value or "").lower()
    return {"claude": "CL", "codex": "CX", "opencode": "OC"}.get(
        value, "?" if not value else value[:2].upper())


def _get(value, key, default=None):
    if value is None:
        return default
    found = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    return default if found is None else found


class SourceCache:
    """Bounded (path, mtime_ns, size)-keyed JSON reads; an unchanged file is not read twice."""

    def __init__(self, limit=512):
        self._limit = limit
        self._lock = threading.Lock()
        self._items = OrderedDict()
        self.reads = 0

    def read_json(self, path):
        path = str(path)
        try:
            info = os.stat(path)
        except OSError:
            return None
        if info.st_size > _MAX_RECORD_BYTES:
            return None
        key = (info.st_mtime_ns, info.st_size)
        with self._lock:
            hit = self._items.get(path)
            if hit is not None and hit[0] == key:
                self._items.move_to_end(path)
                return hit[1]
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError, UnicodeError):
            record = None
        with self._lock:
            self.reads += 1
            self._items[path] = (key, record)
            self._items.move_to_end(path)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return record


def _dispatch():
    from ..collectors import dispatch
    return dispatch


def _registry_paths(jobs_path):
    """One registry path, several, or None for the registries Fleet itself observes."""
    if jobs_path is None:
        return tuple(path for path in _dispatch()._candidate_jobs_paths() if path)
    if isinstance(jobs_path, (str, bytes, os.PathLike)):
        return (jobs_path,)
    return tuple(jobs_path)


def session_entries(jobs_path, parent_sid):
    """Every registry line (number, fields, meta) naming ``parent_sid`` as its parent, in file order."""
    dispatch = _dispatch()
    entries = []
    for path in _registry_paths(jobs_path):
        for line_number, fields, meta in dispatch.parsed_registry_rows(path):
            if meta.get("parent_sid") == parent_sid:
                entries.append((line_number, list(fields), dict(meta)))
    return entries


def read_attempts(jobs_path, parent_sid, live_jobs=None, *, entries=None):
    """Latest registry row per attempt for one main session, with Fleet's own decision/liveness."""
    dispatch = _dispatch()
    if entries is None:
        entries = session_entries(jobs_path, parent_sid)
    live = live_jobs or {}
    latest = {}
    for number, fields, meta in entries:
        latest[meta.get("attempt_id") or fields[4]] = (number, fields, meta)
    rows = []
    for key, (number, fields, meta) in latest.items():
        attempt_id = meta.get("attempt_id") or key
        job = live.get(attempt_id)
        decision = _get(job, "attempt_decision") if job is not None else None
        if decision is None:
            decision = dispatch.attempt_decision_payload(
                fields[1], meta, _get(job, "state_evidence") if job is not None else None)
        try:
            depth = int(meta.get("dispatch_depth") or 0)
        except ValueError:
            depth = 0
        rows.append(AttemptRow(
            attempt_id=attempt_id, line=number, status=fields[1], slug=fields[4],
            harness=meta.get("harness") or "", worker_type=meta.get("worker_type") or "",
            depth=depth, route_id=meta.get("route_id") or None,
            owner_route_id=meta.get("owner_route_id") or None,
            route_node=meta.get("route_node") or None, note=meta.get("note") or "",
            timestamp=fields[0], parent_sid=meta.get("parent_sid") or "",
            liveness=(_get(job, "liveness") if job is not None else None) or "unknown",
            decision=decision, meta=meta,
            registered_only=dispatch.row_is_registered_only(fields[1], meta)))
    return sorted(rows, key=lambda row: row.line)


EVENT_LIMIT = 24


def read_events(entries, tag=None, limit=EVENT_LIMIT):
    """The newest registry lines of the focused session as event records (what each line says, nothing more)."""
    records = []
    for number, fields, meta in entries[-limit:]:
        records.append({"time_kst": fields[0], "kind": "registry", "tag": tag,
                        "attempt_id": meta.get("attempt_id") or fields[4], "status": fields[1],
                        "route_node": meta.get("route_node") or None, "harness": meta.get("harness") or None,
                        "worker_type": meta.get("worker_type") or None, "note": meta.get("note") or None,
                        "line": number})
    return tuple(records)


def derive_artifact_root(rows):
    """The artifact root the selected session's routes live under: the root its route files name, else the env's."""
    counts = {}
    for row in rows:
        for key in ("owner_route_file", "route_file"):
            path = row.meta.get(key)
            if not path:
                continue
            parts = Path(path).parts
            if len(parts) >= 4 and parts[-2] == "routes" and parts[-3] == ".runtime":
                root = str(Path(*parts[:-3]))
                counts[root] = counts.get(root, 0) + 1
        explicit = row.meta.get("artifact_root")
        if explicit and Path(explicit, ".runtime").is_dir():
            counts[explicit] = counts.get(explicit, 0) + 1
    if counts:
        return max(counts, key=lambda root: counts[root])
    return os.environ.get("AGENT_ARTIFACT_ROOT") or ""


def _epoch(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _runtime(artifact_root, *parts):
    return Path(artifact_root, ".runtime", *parts)


def _load_routes(rows, artifact_root, route_loader, selected_rows=(), have=()):
    """Owner routes (needed for the cycle join) plus the selected request's other routes."""
    wanted = {}
    for row in rows:
        if row.worker_type == "owner":
            route_id = row.owner_route_id or row.route_id
            if route_id:
                wanted.setdefault(route_id, row)
    for row in selected_rows:
        if row.route_id:
            wanted.setdefault(row.route_id, row)
    records = {}
    for route_id, row in wanted.items():
        if route_id in have or not _ROUTE_ID.match(route_id):
            continue
        candidates = [_runtime(artifact_root, "routes", route_id + ".json")]
        for key in ("owner_route_file", "route_file"):
            if row.meta.get(key):
                candidates.append(Path(row.meta[key]))
        for path in dict.fromkeys(candidates):
            record = route_loader(str(path), expect_id=route_id)
            if record is not None:
                records[route_id] = record
                break
    return records


def _load_cycles(rows, routes, artifact_root, cache):
    """Cycle records named by owner routes; frame-only requests are found by their frame route."""
    cycles = {}
    cycle_dir = _runtime(artifact_root, "artifact-producer", "v1", "cycles")
    for route in routes.values():
        cycle_id = _get(route, "parent_cycle_id")
        if isinstance(cycle_id, str) and _CYCLE_ID.match(cycle_id) and cycle_id not in cycles:
            record = cache.read_json(cycle_dir / (cycle_id + ".json"))
            if isinstance(record, dict):
                cycles[cycle_id] = record
    proven = {_get(c, "route_id") for c in cycles.values()}
    pending = {row.route_id for row in rows
               if row.worker_type == "frame" and row.route_id and row.route_id not in proven}
    if pending:
        stamps = [e for e in (_epoch(row.timestamp) for row in rows) if e is not None]
        since = (min(stamps) - 86400) if stamps else 0
        try:
            entries = [entry for entry in os.scandir(cycle_dir) if entry.name.endswith(".json")]
        except OSError:
            entries = []
        dated = []
        for entry in entries:
            try:
                dated.append((entry.stat().st_mtime, entry.path))
            except OSError:
                continue
        for count, (mtime, path) in enumerate(sorted(dated, reverse=True)):
            if mtime < since or count >= _MAX_CYCLE_FILES or not pending:
                break
            record = cache.read_json(path)
            if isinstance(record, dict) and _get(record, "route_id") in pending:
                cycles[_get(record, "cycle_id")] = record
                pending.discard(_get(record, "route_id"))
    return cycles


def _gate(record, route_id, node, state_roots, route_module):
    if route_module.gate_mark(record, node, state_roots=state_roots):
        return "passed"
    if isinstance(record, dict) and record.get("schema_version") == 2:
        for root in state_roots:
            if os.path.exists(os.path.join(root, "completion", route_id, node + ".json")):
                return "hold"                  # a marker exists but does not prove the gate
    return None


def _normalize_duty(record, session_id, now):
    if not isinstance(record, dict) or record.get("state") in _DONE_DUTY_STATES:
        return None
    intent = record.get("intent") or {}
    identity = intent.get("identity") or record.get("identity") or {}
    if intent.get("kind") != "registered-batch" or identity.get("session_id") != session_id:
        return None
    stamp = record.get("updated_at") or record.get("accepted_at")
    try:
        age = max(0, int((now - float(stamp)) // 60))
    except (TypeError, ValueError):
        age = None
    return {"id": record.get("id"), "kind": "registered-batch",
            "attempt_id": identity.get("attempt_id"), "harness": identity.get("harness"),
            "state": record.get("state"), "cleanup": record.get("cleanup"),
            "delivery": record.get("delivery"), "age_min": age,
            "_order": float(stamp) if isinstance(stamp, (int, float)) else 0.0}


def read_obligations(state_roots, session_id, now, cache):
    """Unfinished registered-batch duties of one main session, oldest first (bounded scan)."""
    items, seen = [], set()
    for root in state_roots:
        folder = Path(root, "peer-steward", "obligations")
        try:
            entries = [e for e in os.scandir(folder)
                       if e.name.startswith("registered-batch-") and e.name.endswith(".json")
                       and not e.is_symlink()]
        except OSError:
            continue
        dated = []
        for entry in entries:
            try:
                dated.append((entry.stat().st_mtime, entry.path))
            except OSError:
                continue
        for _mtime, path in sorted(dated, reverse=True)[:_MAX_OBLIGATION_FILES]:
            duty = _normalize_duty(cache.read_json(path), session_id, now)
            if duty is not None and duty["id"] not in seen:
                seen.add(duty["id"])
                items.append(duty)
    items.sort(key=lambda item: item["_order"])
    for item in items:
        del item["_order"]
    return tuple(items)


LINEAGE_LIMIT = 12


def _card_title(module, card):
    """First line of the earlier generation's card, or ''."""
    path = (card or {}).get("path")
    if not path or (card or {}).get("status") != "available":
        return ""
    data = module.read_json(Path(path))
    body = data.get("body") if isinstance(data, dict) else None
    return _first_body_line(body)


def read_lineage(session, harness, sid):
    """``(entries, Health)``: the earlier generations of a main session's seat (read only).

    The seat is made the way ``session_tidy`` makes it (harness, sid, cwd and, when the session
    reports one, its pane). ``entries`` is None when the lineage could not be read, never a guess.
    """
    from . import nav
    try:
        module = utility("session_tidy")
        env = {}
        pane = nav._get(session, "herdr_pane", "pane", "pane_id")
        if pane:
            env["HERDR_PANE_ID"] = str(pane)
        seat = module.resolve_seat(harness, cwd=nav._get(session, "cwd", "project"), env=env, sid=sid)
    except Exception as exc:
        return None, Health("failed", None, f"seat 없음 · {type(exc).__name__}")
    try:
        rows = module.seat_lineage(seat, sid=sid)
    except Exception as exc:
        return None, Health("failed", None, type(exc).__name__)
    entries = []
    for row in rows:
        if row.get("harness") == harness and row.get("sid") == sid:
            continue
        entries.append({"harness": row.get("harness"), "sid": row.get("sid"),
                        "source": row.get("source"), "relation": row.get("relation"),
                        "title": _card_title(module, row.get("card")),
                        "card_status": (row.get("card") or {}).get("status"),
                        "transcript": dict(row.get("transcript") or {})})
        if len(entries) >= LINEAGE_LIMIT:
            break
    return tuple(entries), Health("success", time.time())



# -- seat history, cards, dialogue, pane (bounded reads for what is open) ----------------------

SEAT_SESSION_LIMIT = 12
CARD_LINES_MAX = 600
CARD_TITLE_MAX = 400
DIALOGUE_LINES_MAX = 400
PANE_ROWS = 14
_ROLE_LINE = re.compile(r"\[([A-Za-z_ ]+)\] ")


_TAG_ONLY = re.compile(r"^</?[A-Za-z_][\w-]*>$")


def _first_body_line(body):
    """What the card says first (see ``nav.card_title``); a bare ``<analysis>``-style tag line is not a title."""
    from . import nav
    lines = [line for line in str(body or "").splitlines() if not _TAG_ONLY.match(line.strip())]
    return nav.card_title(lines)[:CARD_TITLE_MAX]


def _read_card(module, card, *, full):
    """One card of the seat: its generation, when it was written, its first line and (``full``) its body."""
    entry = {"generation": card.get("generation"), "status": card.get("status")}
    path = card.get("path")
    if not path or card.get("status") != "available":
        return entry
    data = module.read_json(Path(path))
    if not isinstance(data, dict):
        return entry
    body = str(data.get("body") or "")
    at = data.get("authored_at_epoch")
    entry.update(at=float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
                 title=_first_body_line(body), chars=len(body))
    if full:
        entry["lines"] = body.splitlines()[:CARD_LINES_MAX]
    return entry


def read_seat_history(session, harness, sid, *, cards=False, pane=None):
    """``(entries, pane, Health)``: every session of the seat, oldest first and the current one last.

    Each entry carries its start source, its handover relation, where its transcript is and its
    cards. Only the newest card of the whole seat is opened unless ``cards`` is set (the opened
    seat), in which case every card is read with its body. ``entries`` is None when the seat could
    not be read, never a guess.
    """
    from . import nav
    try:
        module = utility("session_tidy")
        pane = pane or nav._get(session, "herdr_pane", "pane", "pane_id")
        if pane:
            # The viewer is not the pane's runtime, so a verified-caller seat cannot be resolved; the
            # seat a session's own hooks wrote under is exactly this (endpoint scope, pane id).
            endpoint = module._endpoint_scope()
            key = module._digest("pane", endpoint, str(pane)) if endpoint else module._digest("pane", str(pane))
            seat = module.Seat("pane", key, str(pane), harness or "", "", endpoint=endpoint)
        else:
            seat = module.resolve_seat(harness, cwd=nav._get(session, "cwd", "project"), env={}, sid=sid)
    except Exception as exc:
        return None, "", Health("failed", None, f"seat 없음 · {type(exc).__name__}")
    try:
        rows = module.seat_lineage(seat, sid=sid)
    except Exception as exc:
        return None, "", Health("failed", None, type(exc).__name__)
    rows = [row for row in rows[:SEAT_SESSION_LIMIT + 1]]
    current = [row for row in rows if row.get("harness") == harness and row.get("sid") == sid]
    earlier = [row for row in rows if row not in current]
    ordered = list(reversed(earlier)) + current
    newest_card_owner = None
    for row in reversed(ordered):
        if row.get("cards"):
            newest_card_owner = id(row)
            break
    entries = []
    for row in ordered:
        row_cards = list(row.get("cards") or ())
        read = []
        for index, card in enumerate(row_cards):
            last = id(row) == newest_card_owner and index == len(row_cards) - 1
            read.append(_read_card(module, card, full=cards) if (cards or last)
                        else {"generation": card.get("generation"), "status": card.get("status")})
        entries.append({"harness": row.get("harness"), "sid": row.get("sid"),
                        "started_at": row.get("started_at"), "source": row.get("source"),
                        "relation": row.get("relation"),
                        "transcript": dict(row.get("transcript") or {}),
                        "current": bool(row in current), "cards": read})
    return tuple(entries), (seat.pane if seat.kind == "pane" else ""), Health("success", time.time())


def lineage_from_seat(entries):
    """The older ``lineage`` shape (earlier sessions, newest first) from a seat history."""
    items = []
    for entry in reversed(tuple(entries or ())):
        if entry.get("current"):
            continue
        cards = entry.get("cards") or ()
        titled = next((card.get("title") for card in reversed(cards) if card.get("title")), "")
        items.append({"harness": entry.get("harness"), "sid": entry.get("sid"), "source": entry.get("source"),
                      "relation": entry.get("relation"), "title": titled,
                      "card_status": (cards[-1].get("status") if cards else None),
                      "transcript": dict(entry.get("transcript") or {})})
    return tuple(items[:LINEAGE_LIMIT])


def read_dialogue(entry):
    """The last conversation rows of one generation's transcript as ``role: text`` lines."""
    where = (entry or {}).get("transcript") or {}
    path, status = where.get("path"), where.get("status")
    harness, sid = str(entry.get("harness") or ""), str(entry.get("sid") or "")
    if not path or status != "available":
        return None
    try:
        module = utility("tidy_transcripts")
        from .output import TAIL_BYTES
        if harness == "opencode":
            cursor, texts = 0, []
            for _ in range(64):
                chunk = module.read_chunk(harness, path, cursor, sid=sid, limit_bytes=TAIL_BYTES)
                if chunk.error:
                    return None
                if chunk.text:
                    texts.append(chunk.text)
                texts = texts[-2:]
                if chunk.eof or chunk.cursor_to <= cursor:
                    break
                cursor = chunk.cursor_to
            text = "\n".join(texts)
        else:
            size = os.stat(path).st_size
            text = ""
            for window in (TAIL_BYTES, 8 * TAIL_BYTES, 32 * TAIL_BYTES):
                # whole rows only: a window that starts inside one very long row yields nothing, so widen it
                chunk = module.read_chunk(harness, path, max(0, size - window), sid=sid, limit_bytes=window)
                if chunk.error:
                    return None
                text = chunk.text
                if text.strip() or size <= window:
                    break
    except Exception:
        return None
    lines = []
    for raw in str(text).split("\n"):
        match = _ROLE_LINE.match(raw)
        lines.append(f"{match.group(1).lower()}: {raw[match.end():]}" if match else raw)
    return lines[-DIALOGUE_LINES_MAX:]


def _herdr_argv(*args):
    argv = ["herdr"]
    try:
        endpoint = utility("herdr_observation").endpoint_name()
    except Exception:
        endpoint = "default"
    if endpoint and endpoint != "default":
        argv += ["--session", str(endpoint)]
    return argv + list(args)


def read_pane_map(timeout=3.0):
    """``{session id: pane id}`` from one ``herdr pane list``; empty when herdr is not reachable."""
    import subprocess
    try:
        proc = subprocess.run(_herdr_argv("pane", "list"), capture_output=True, text=True, timeout=timeout)
        if proc.returncode:
            return {}
        panes = ((json.loads(proc.stdout or "{}").get("result") or {}).get("panes")) or ()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    found = {}
    for pane in panes:
        owner = (pane.get("agent_session") or {}).get("value") if isinstance(pane, dict) else None
        if isinstance(owner, str) and owner and isinstance(pane.get("pane_id"), str):
            found.setdefault(owner, pane["pane_id"])
    return found


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def read_pane_visible(pane, *, rows=PANE_ROWS, timeout=2.5):
    """The open session's visible pane text: its last non-blank rows, nothing else of the screen."""
    import subprocess
    if not isinstance(pane, str) or not pane:
        return None
    try:
        proc = subprocess.run(_herdr_argv("pane", "read", pane, "--source", "visible", "--format", "text"),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stdout if isinstance(proc.stdout, str) else ""
    if proc.returncode or not text or len(text.encode("utf-8")) > 262144:
        return None
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
            if payload.get("error"):
                return None
            text = str(((payload.get("result") or {}).get("read") or {}).get("text") or "")
        except ValueError:
            pass
    lines = [_CONTROL.sub("", line).rstrip() for line in text.splitlines()]
    lines = [line for line in lines if line.strip()]
    return lines[-rows:]


def read_owner_raw(route_id, attempt_id, rows, live, reader):
    """Raw owner output of a route (``attempt_view`` lines through the shared reader), or None.

    The owner attempt is the live job the snapshot names (it carries its log file and registry), else
    the registry row of the focused session that is that route's owner."""
    from .output import SourcePlan, _abbr
    if reader is None:
        return None
    job = live.get(attempt_id) if attempt_id else None
    if job is not None:
        harness = str(_get(job, "harness") or "")
        meta = tuple((name, str(_get(job, attr))) for name, attr in (
            ("registry_path", "_registry_path"), ("artifact_root", "artifact_root"),
            ("launch_home", "_launch_home")) if _get(job, attr))
        plan = SourcePlan(attempt_id, "owner", harness, _abbr(harness), "owner",
                          log_file=str(_get(job, "_log_file") or ""), meta=meta)
    else:
        owner = next((row for row in rows if row.worker_type == "owner"
                      and (row.owner_route_id or row.route_id) == route_id), None)
        if owner is None:
            return None
        harness = owner.harness or ""
        plan = SourcePlan(owner.attempt_id, "owner", harness, _abbr(harness), "owner",
                          meta=tuple((k, v) for k, v in owner.meta.items()
                                     if k in ("registry_path", "artifact_root", "launch_home")))
    view = reader.read(("route-raw", route_id), "owner raw", [plan])
    return {"att": plan.attempt_id, "route": route_id,
            "lines": [event.text for event in getattr(view, "events", ()) if event.text][-300:]}


def nav_tag(session):
    tag = _get(session, "session_tag")
    return str(tag) if tag else None


def _as_mapping(value):
    value = value() if callable(value) else value
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {_get(job, "attempt_id"): job for job in value if _get(job, "attempt_id")}


def collect_focus(request, *, jobs_path=None, artifact_root=None, route_loader=None, now=None,
                  live_jobs=None, snapshot=None, state_roots=None, cache=None, output_reader=None,
                  earlier=False):
    """Read only the focused session's bounded source facts and optional earlier transcript."""
    from .. import route as route_module
    route_loader = route_loader or route_module.load
    cache = cache or SourceCache()
    wall = time.time() if now is None else float(now)
    if state_roots is None:
        try:
            state_roots = list(route_module._dispatch_state_roots())
        except Exception:
            state_roots = []
    live = _as_mapping(live_jobs)
    key = request.key
    entries = session_entries(jobs_path, key.sid)
    rows = read_attempts(jobs_path, key.sid, live, entries=entries)
    if not artifact_root:
        artifact_root = derive_artifact_root(rows)
    errors = []

    owner_routes = _load_routes(rows, artifact_root, route_loader)
    cycles = _load_cycles(rows, owner_routes, artifact_root, cache)
    records = dict(owner_routes)
    gates = {}

    sources = []
    output, lineage = {}, {}
    session = None
    if snapshot is not None:
        from . import nav
        session = nav.find_session(snapshot, key.sid)
    focus_ref = f"{key.harness}:{key.sid}"
    from . import nav
    wants = tuple(getattr(request, "wants", ()) or ())
    seat_entries, pane_id, extra = None, "", {}
    panes = read_pane_map() if (session is not None and wants) else {}
    session_ids = (key.sid, *(_get(session, "session_aliases") or ())) if session is not None else ()
    pane_id = next((panes[i] for i in session_ids if i in panes), "")
    if session is not None and any(w in ("seat", "cards") or w.startswith("dialogue:") for w in wants):
        seat_entries, seat_pane, seat_health = read_seat_history(
            session, key.harness, key.sid, cards="cards" in wants, pane=pane_id or None)
        pane_id = pane_id or seat_pane
        if seat_entries is None:
            errors.append("seat: " + str(seat_health.error))
        else:
            extra["seat"] = seat_entries
    if seat_entries is not None:
        lineage = {"lineage": lineage_from_seat(seat_entries), "lineage_health": seat_health}
        for want in wants:
            if want.startswith("dialogue:") and (want[9:].isdigit() or want[9:] == "last"):
                chain = nav.generation_chain(seat_entries, key.harness, key.sid)
                index = nav.default_generation(chain) if want[9:] == "last" else int(want[9:])
                if 0 <= index < len(chain) and chain[index].get("entry") is not None:
                    lines = read_dialogue(chain[index]["entry"])
                    if lines is not None:
                        extra["dialogue"] = {"index": index, "harness": chain[index]["harness"],
                                             "sid": chain[index]["sid"], "lines": lines}
    elif focus_ref in tuple(getattr(request, "expanded_sessions", ()) or ()) and session is not None:
        try:
            earlier_sessions, health = read_lineage(session, key.harness, key.sid)
        except Exception as exc:                 # a lineage fault is the entry row's ``?``, nothing else
            earlier_sessions, health = None, Health("failed", None, type(exc).__name__)
        lineage = {"lineage": earlier_sessions, "lineage_health": health}
    generation = None
    if key.generation and lineage.get("lineage"):
        generation = next((item for item in lineage["lineage"]
                           if f"{item['harness']}:{item['sid']}" == key.generation), None)
    if output_reader is not None and generation is not None:
        title = ""
        try:
            view = read_generation(output_reader, generation)
            output = {"output": view.rows, "output_events": view.events,
                      "output_title": view.title, "output_notes": view.notes,
                      "output_health": view.health, "output_more": view.more}
            sources.append(("output", view.health))
        except Exception as exc:
            errors.append(f"output: {type(exc).__name__}")
            sources.append(("output", Health("failed", None, type(exc).__name__)))
            output = {"output_title": title, "output_health": Health("failed", None, type(exc).__name__)}
    try:
        obligations = read_obligations(state_roots, key.sid, wall, cache)
        sources.append(("obligations", Health("success", wall)))
    except Exception as exc:
        obligations = None
        errors.append(f"obligations: {type(exc).__name__}")
        sources.append(("obligations", Health("failed", None, type(exc).__name__)))
    events = read_events(entries, nav_tag(session))

    pane = None
    if session is not None:
        for field_name in ("herdr_pane", "pane", "pane_id"):
            pane = _get(session, field_name)
            if pane is not None:
                break
    if session is not None and "pane" in wants:
        lines = read_pane_visible(pane_id) if pane_id else None
        if lines is not None:
            extra["pane_read"] = {"pane": pane_id, "at": wall, "lines": lines}
    raw_want = next((w[4:] for w in wants if w.startswith("raw:")), "")
    if raw_want and output_reader is not None:
        raw_route, _, raw_attempt = raw_want.partition(":")
        try:
            raw = read_owner_raw(raw_route, raw_attempt, rows, live, output_reader)
        except Exception as exc:
            raw = None
            errors.append(f"raw: {type(exc).__name__}")
        if raw is not None:
            extra["raw"] = raw
    source_records = {
        **extra,
        "registry_entries": tuple(entries),
        "attempts": tuple(rows),
        "routes": tuple(sorted(records.items())),
        "cycles": tuple(sorted(cycles.items())),
        "gates": tuple((route_id, node_id, value)
                       for (route_id, node_id), value in sorted(gates.items())),
        "lineage": lineage.get("lineage"),
        "pane": pane,
        "obligations": obligations,
        "output_events": tuple(output.get("output_events", ())),
        "warnings": tuple(errors),
    }
    result = FocusResult(
        key=key, epoch=request.epoch, snapshot_generation=request.snapshot_generation,
        obligations=obligations, events=events, sources=tuple(sources),
        health=Health("partial" if errors else "success", wall, "; ".join(errors) or None),
        **output, **lineage, source_records=source_records)
    return result



# -- models / dispatch configuration (cached observation) and the list-view reads -----------------

_PROFILES = ("top", "deep", "balanced-deep", "balanced", "light", "mini")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CONTEXT_SOURCE = {"claude": "→ CLAUDE_CODE_AUTO_COMPACT_WINDOW", "codex": "→ model_context_window",
                   "opencode": "→ limit.context capped at start"}
_TOKEN_UNITS = {"400k": 400000, "600k": 600000, "800k": 800000, "1M": 1000000, "300K": 300000,
                "400K": 400000, "500K": 500000, "600K": 600000, "650K": 650000, "700K": 700000,
                "750K": 750000}
_EFFORT_KEYS = {"low": "dim", "medium": "eff_medium", "high": "soft", "xhigh": "eff_xhigh", "max": "dead"}


def _profile_cell(values, profile):
    """``[model, effort]`` a harness's models.conf gives one profile, or None (unset or collapsed)."""
    name = profile.upper().replace("-", "_")
    if str(values.get("CFG_MODEL_PROFILE_GRANULARITY_" + name, "")).startswith("collapsed"):
        return None
    raw = values.get("CFG_MODEL_PROFILE_" + name)
    if not raw or ":" not in raw:
        return None
    target, effort = raw.rsplit(":", 1)
    if target.startswith("model/"):
        model = target[len("model/"):]
    else:
        tier = target.upper().replace("-", "_")
        model = values.get(f"CFG_TIER_{tier}_MODEL")
    if not model:
        return None
    return [model, effort]


def _allocation_fields(policy):
    """The DISPATCH ALLOCATION lines as ``[label, [[text, colour key], ...]]``."""
    allocation = policy.get("allocation") or {}
    fields = []
    if allocation:
        fields.append(["strategy:", [[str(allocation.get("strategy", "·")), "soft"],
                                     ["   usage gate ", "label"],
                                     [f"{allocation.get('usage_gate_used_percent', '·')}%", "lvl_y"],
                                     ["   window ", "label"], [f"{allocation.get('window', '·')} attempts", "soft"],
                                     ["   headroom exponent ", "label"],
                                     [str(allocation.get("usage_headroom_exponent", "·")), "soft"]]])
        weights = allocation.get("harness_weights") or {}
        if allocation.get("strategy") == "balanced":
            parts = []
            for harness in ("codex", "opencode", "claude"):
                weight = float(weights.get(harness, 1.0))
                parts += [[harness, "hb_" + harness], [" ", "dim"],
                          ["▮" * max(1, round(10 * weight)), "hb_" + harness], [f" {weight:.1f}    ", "dim"]]
            fields.append(["weights:", parts])
        affinity = allocation.get("depth_affinity") or {}
        parts = []
        if affinity.get("worker"):
            parts += [["worker → ", "dim"], [str(affinity["worker"]), "hb_" + str(affinity["worker"])],
                      [f" ({allocation.get('depth_affinity_weight', '·')})", "dim"]]
        if affinity.get("owner"):
            parts += [["   owner → ", "dim"], [str(affinity["owner"]), "hb_" + str(affinity["owner"])]]
        else:
            parts += [["   owner → ", "dim"], ["usage-weighted, no fixed harness", "soft"]]
        fields.append(["affinity:", parts])
    for capability, stages in (policy.get("capabilities") or {}).items():
        values = sorted({str(value) for value in stages.values()})
        if len(values) == 1:                       # one choice for every stage reads as one
            fields.append(["per capability:", [[f"{capability}  ", "soft"], ["all stages", "dim"],
                                               ["  →  ", "line"], [values[0], "soft"]]])
            continue
        fields.append(["per capability:", [[f"{capability}  ", "soft"],
                                           [" · ".join(f"{name} {value}" for name, value in stages.items()), "dim"]]])
    confirmation = (policy.get("confirmation") or {}).get("mode")
    permission = (policy.get("headless") or {}).get("claude_permission_mode")
    if confirmation or permission:
        fields.append(["confirmation:", [[str(confirmation or "·"), "soft"], ["   permissions ", "label"],
                                         [str(permission or "·"), "soft"]]])
    return fields


_ALLOCATION_OPTIONAL = ("usage_gate_used_percent", "depth_affinity", "depth_affinity_weight",
                        "usage_headroom_exponent", "harness_weights")


def _allocation_editable(module, policy):
    """Current allocation values and, per strategy, the keys it leaves without effect (and why)."""
    values = module.query_allocation(policy)
    if not values:
        return {}
    strategies = sorted(module.ALLOCATION_STRATEGIES)
    probe = {key: None for key in _ALLOCATION_OPTIONAL}
    inert = {name: dict(module.inert_allocation_keys(dict(probe, strategy=name))) for name in strategies}
    enabled = [name for name in ("claude", "codex", "opencode")
               if name in ((policy.get("harnesses") or {}).get("enabled") or ("claude", "codex", "opencode"))]
    path = module.default_config_path()
    return {"values": {"strategy": values.get("strategy"), "window": values.get("window"),
                       "usage_gate_used_percent": values.get("usage_gate_used_percent"),
                       "usage_headroom_exponent": values.get("usage_headroom_exponent"),
                       "depth_affinity_weight": values.get("depth_affinity_weight"),
                       "depth_affinity": dict(values.get("depth_affinity") or {}),
                       "harness_weights": dict(values.get("harness_weights") or {})},
            "strategies": strategies, "inert": inert, "harnesses": enabled,
            "user_file": os.path.realpath(path) != os.path.realpath(module.SHIPPED_CONFIG_PATH)}


_STEWARD_KEYS = ("check_interval_seconds", "max_supervisors_per_repo")


def _supervisors():
    """This repository first, then every other one holding a supervisor, from the steward
    collector's last pass (``None`` before it ran). Nothing here counts on its own."""
    from ..collectors import steward
    census = steward.last_supervision()
    if census is None:
        return None
    here = steward._repository_key(os.getcwd())
    rows = [census[key] for key in sorted(census, key=lambda key: (key != here, key))]
    if here and here not in census:
        rows.insert(0, {"repository": Path(here).parent.name, "repository_key": here,
                        "holders": [], "counted": 0, "limit": None, "over_limit": False})
    return rows


def _steward_editable(module, policy):
    """The steward check interval and per-repo supervisor limit through the shared policy reader."""
    current = module.query_steward_policy(policy)
    values = {key: current[key] for key in _STEWARD_KEYS}
    try:
        supervisors = _supervisors()
    except Exception:
        supervisors = None
    return {"values": values, "supervisors": supervisors}


def _read_tidy_points():
    """Read the context alert thresholds; unavailable or malformed source stays unknown."""
    try:
        points = utility("context_alert").THRESHOLDS
    except (ImportError, AttributeError, OSError):
        return None
    if not isinstance(points, (tuple, list)) or len(points) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in points):
        return None
    return tuple(points)


def read_model_config():
    """MODELS view data from the harnesses' models.conf and the dispatch defaults (read once, cached)."""
    import importlib.util
    model_config = utility("model_config")
    profile_map, choices, main_only, context = {}, {}, {}, {}
    for adapter in ("claude", "codex", "opencode"):
        try:
            values, _receipt = model_config.resolve_config(adapter)
        except Exception:
            continue
        profile_map[adapter] = {profile: _profile_cell(values, profile) for profile in _PROFILES}
        if adapter == "opencode":
            try:
                # Use the selected runtime config's normal resolver: an omitted
                # TOP uses the installed collapse, without rewriting the file.
                top = utility("model_profile").resolve_profile_values(adapter, values, "top")
                profile_map[adapter]["top"] = [top["model"], top["budget"]]
            except Exception:
                pass
        picked = []
        for cell in profile_map[adapter].values():
            if cell and cell[0] not in picked:
                picked.append(cell[0])
        choices[adapter] = picked
        hidden = [item for item in str(values.get("CFG_MAIN_SESSION_ONLY_MODELS", "")).split() if item]
        if hidden:
            main_only[adapter] = hidden
        window = values.get("CFG_MODEL_CONTEXT_WINDOW")
        if str(window).isdigit():
            context[adapter] = int(window)
    catalog_sources = {}
    try:
        from .model_catalog import read_catalogs
        catalogs = read_catalogs()
        for adapter, catalog in catalogs.items():
            catalog_sources[adapter] = {"state": catalog.state, "source": catalog.source,
                                        "observed_at": catalog.observed_at}
            if catalog.models or catalog.state in ("available", "empty"):
                choices[adapter] = list(catalog.models)
    except Exception:
        catalogs = {}
    if not profile_map and not any(catalog.models for catalog in catalogs.values()):
        return None
    pools, allocation = {}, {}
    try:
        path = Path(__file__).resolve().parents[3] / "utilities" / "dispatch-defaults.py"
        spec = importlib.util.spec_from_file_location("fleet_dispatch_defaults", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy, _warnings = module.load_validated(module.default_config_path(), module.default_topology_path())
        for profile, bands in (policy.get("profiles") or {}).items():
            note = ""
            if bands.get("relief"):
                note = "relief " + " ".join(bands["relief"]) + f" below {bands.get('promote_relief_below')}%"
            elif bands.get("last_resort"):
                note = "last resort " + " ".join(bands["last_resort"])
            pools[profile] = [[list(bands.get("primary") or ())], note]
        pools.setdefault("top", None)
        allocation = {"fields": _allocation_fields(policy),
                      "note": "new dispatch decisions only · running and sealed routes keep their choice"}
        allocation.update(_allocation_editable(module, policy))
        steward = _steward_editable(module, policy)
    except Exception:
        pools, allocation, steward = {}, {}, {}
    try:
        from .. import render
        shown = {}
        for models in choices.values():
            for model in models:
                shown[model] = render._clean_model(render._dispatch_display_model(model)) or model
        for models in main_only.values():
            for model in models:
                shown[model] = render._clean_model(render._dispatch_display_model(model)) or model
    except Exception:
        shown = {}
    return {"profiles": [p for p in _PROFILES if any(m.get(p) for m in profile_map.values())],
            "profile_map": profile_map, "model_choices": choices, "main_only": main_only,
            "efforts": list(_EFFORTS), "pools": pools, "effort_keys": dict(_EFFORT_KEYS), "shown": shown,
            "context": context, "context_source": dict(_CONTEXT_SOURCE), "catalog_sources": catalog_sources,
            "tidy": _read_tidy_points(),
            "token_units": dict(_TOKEN_UNITS), "allocation": allocation, "steward": steward}


def collect_aux(request, *, snapshot=None, now=None):
    """The reads that belong to no single session: panes of every session, the seats of the list
    views, the model configuration. The request's key is ``FocusKey("*", <kind>)``."""
    from . import nav
    kind = request.key.sid
    wall = time.time() if now is None else float(now)
    records, errors = {}, []
    if kind == "panes":
        records["panes"] = read_pane_map()
    elif kind == "seats":
        seats, panes = {}, read_pane_map()
        for ref in request.expanded_sessions:
            harness, _, sid = ref.partition(":")
            session = nav.find_session(snapshot, sid)
            if session is None:
                continue
            pane = next((panes[i] for i in (sid, *(nav._get(session, "session_aliases") or ())) if i in panes), None)
            entries, _pane, health = read_seat_history(session, harness, sid, cards=False, pane=pane)
            if entries is None:
                errors.append(f"{ref}: {health.error}")
            else:
                seats[ref] = entries
        records["seats"] = seats
    elif kind == "models":
        config = read_model_config()
        if config is None:
            errors.append("models: no configuration")
        records["models"] = config
    return FocusResult(request.key, request.epoch, request.snapshot_generation,
                       health=Health("partial" if errors else "success", wall, "; ".join(errors) or None),
                       source_records=records)


class FocusReader:
    """``SelectionReader`` provider with one source cache for the process lifetime."""

    def __init__(self, jobs_path=None, artifact_root=None, *, route_loader=None, live_jobs=None,
                 snapshot=None, state_roots=None, now=None, output_reader=None):
        self.jobs_path = jobs_path
        self.artifact_root = artifact_root
        self.route_loader = route_loader
        self.live_jobs = live_jobs
        self.snapshot = snapshot
        self.state_roots = state_roots
        self.now = now
        self.cache = SourceCache()
        self.output_reader = OutputReader() if output_reader is None else (output_reader or None)
        self._lock = threading.Lock()
        self._earlier = set()

    def load_earlier(self, key):
        """Ask for the records before the oldest one shown for ``key`` (read on the next pass)."""
        with self._lock:
            self._earlier.add(key)

    def __call__(self, request):
        if request.key.harness == "*":
            return collect_aux(request, snapshot=self.snapshot() if callable(self.snapshot) else self.snapshot,
                               now=self.now() if callable(self.now) else self.now)
        with self._lock:
            earlier = request.key in self._earlier
            self._earlier.discard(request.key)
        return collect_focus(
            request, jobs_path=self.jobs_path, artifact_root=self.artifact_root,
            route_loader=self.route_loader, now=self.now() if callable(self.now) else self.now,
            live_jobs=self.live_jobs,
            snapshot=self.snapshot() if callable(self.snapshot) else self.snapshot,
            state_roots=self.state_roots, cache=self.cache, output_reader=self.output_reader,
            earlier=earlier)


class SelectionReader:
    def __init__(self, providers):
        self.providers = providers

    def read(self, request):
        result = self.providers(request)
        if isinstance(result, FocusResult):
            return result
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           output=tuple(result.get("output", ())),
                           events=tuple(result.get("events", ())),
                           source_records=result.get("source_records", {}),
                           health=Health("success", time.time()))


def status_text(health, now=None):
    """The one line shown for a region fed by the pump: reading, no success yet, or last-good age."""
    now = time.time() if now is None else now
    if health.state == "loading":
        return "읽는 중"
    if health.state == "failed":
        return "성공 기록 없음" + (f" · {health.error}" if health.error else "")
    if health.state == "stale":
        age = int(max(0, now - health.last_success_at)) if health.last_success_at else 0
        return f"직전 값 {age}초 전" + (f" · {health.error}" if health.error else "")
    return ""


class SelectionPump:
    """RefreshPump-backed single flight; publish only the current focus identity."""

    _GOOD_LIMIT = 8

    def __init__(self, reader, interval=1.0, clock=time.monotonic):
        self.reader = reader if isinstance(reader, SelectionReader) else SelectionReader(reader)
        self._lock = threading.Lock()
        self._request = None
        self._epoch = 0
        self._generation = 0
        self._good = OrderedDict()                 # key -> last result published for it
        self._pump = RefreshPump(self._produce, interval, clock=clock, name="fleet-selection")

    def select(self, key, snapshot_generation=0, expanded_sessions=(), wants=()):
        with self._lock:
            self._epoch += 1
            self._request = FocusRequest(key, self._epoch, snapshot_generation, tuple(expanded_sessions),
                                         tuple(wants))
            return self._request

    def _produce(self):
        with self._lock:
            request = self._request
        if request is None:
            return None
        return self.reader.read(request)

    def request(self, force=True):
        return self._pump.request(force=force)

    def load_earlier(self):
        """Read further back in the raw output of the current selection (only when the person asks)."""
        with self._lock:
            current = self._request
        loader = getattr(self.reader.providers, "load_earlier", None)
        if current is None or loader is None:
            return False
        loader(current.key)
        self._pump.request(force=True)
        return True

    def request_due(self, now=None):
        return self._pump.request_due(now)

    def poll(self):
        """The newest result for the current focus, or None; a late result for any older
        selection (different key or epoch) is dropped, never shown."""
        update = self._pump.poll(self._generation)
        with self._lock:
            current = self._request
            if update is not None:
                self._generation, value = update
                if (value is not None and current is not None and
                        (value.key, value.epoch, value.snapshot_generation) ==
                        (current.key, current.epoch, current.snapshot_generation)):
                    self._good[current.key] = value
                    self._good.move_to_end(current.key)
                    while len(self._good) > self._GOOD_LIMIT:
                        self._good.popitem(last=False)
            return self._good.get(current.key) if current is not None else None

    def health(self, now=None):
        status = self._pump.health(now)
        return Health(status["state"], status.get("last_success_at"), status.get("last_error"))

    def status(self, now=None):
        """Per-focus state: loading (no result yet), failed (no success ever), stale, or success."""
        with self._lock:
            current = self._request
            good = self._good.get(current.key) if current is not None else None
        if current is None:
            return Health("idle")
        pump = self._pump.health(now)
        if good is not None and good.epoch == current.epoch and pump["state"] != "failed":
            return Health("success", good.health.last_success_at)
        if good is not None:
            return Health("stale", good.health.last_success_at, pump.get("last_error"))
        if pump["state"] == "failed":
            return Health("failed", None, pump.get("last_error"))
        return Health("loading")

    def stop(self, join_timeout=1.0):
        self._pump.stop(join_timeout=join_timeout)
