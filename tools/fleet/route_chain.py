"""Session-anchored route chain ledger + reader (F-<next>, fleet-route-chain-r2 plan §3 Phase A).

Each depth-0 session appends one line per `compose|compile|continuation|start` event to its
own append-only ledger. Fleet reassembles the calling session's most recent same-campaign
run of routes into one displayed chain (`경로 research ✓ › draft ● solo › apply ○`) without
ever listing the route directory or opening jobs.log.

State lives below ``FLEET_ROUTE_CHAIN_DIR`` when set, else
``${XDG_STATE_HOME:-~/.local/state}/agent-fleet/route-chains`` — the same state-root shape
as ``tools/fleet/interaction.py`` and ``tools/fleet/session_registry.py``.

Import contract: this module must stay importable with only the repository's ``tools``
directory on ``sys.path`` (``from fleet import route_chain``, mirroring
``session_registry.py``'s contract). It never imports ``render``, ``model``, or
``collectors`` at module scope; sibling ``fleet`` modules (``route``, ``session_registry``)
are imported lazily, inside the functions that need them.
"""
import json
import contextlib
import os
from collections import OrderedDict
from pathlib import Path
import re
import stat
import sys
import time

SCHEMA = 1
HARNESSES = ("claude", "codex", "opencode")
# Writer coverage per harness. "env" means the harness exports its own native session id to
# the process, so a ledger line can be anchored without any separate identity store. All three
# do: `interactive_parent_identity()` (dispatch_parent_completion.py) reads CLAUDE_SESSION_ID,
# CODEX_THREAD_ID/CODEX_SESSION_ID and OPENCODE_SESSION_ID, and the opencode collector already
# sets `sess.session_id` from its own session row. Measured 2026-09-29 in a live opencode
# session: that env id and the rendered row's `session_id` are the same string, so the write
# anchor and the read anchor already agree. Herdr is deliberately NOT the anchor here — it
# reports no session id for opencode — and none is required.
WRITER_SUPPORT = {"claude": "env", "codex": "env", "opencode": "env"}
TAIL_BYTES = 64 * 1024
MAX_LINE_BYTES = 4096
MAX_PLAN = 12
MAX_NODES = 16
SWEEP_MAX_AGE = 14 * 86400
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_LEDGER_EVENTS = frozenset(("compose", "compile", "continuation", "start"))
_FULL_HISTORY_CACHE = OrderedDict()
_FULL_HISTORY_CACHE_LIMIT = 256
# The events that say "this session composed the route" (a `start` line only says it started one).
COMPOSING_EVENTS = frozenset(("compose", "compile", "continuation"))
# Every event that puts a session in charge of a route at its `ts` (a start hands it over).
OWNING_EVENTS = COMPOSING_EVENTS | {"start"}


def _session_identity(env):
    """`utilities/session_identity.identity`, imported lazily (this module needs only `tools`)."""
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from session_identity import identity
    return identity(env)


def _automatic(route):
    """`utilities/turn_change_record.is_automatic_route`, imported lazily (this module needs only `tools`)."""
    try:
        utilities = str(Path(__file__).resolve().parents[2] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from turn_change_record import is_automatic_route
        return bool(is_automatic_route(route))
    except Exception:
        return False


def writer_identity(environ=None):
    """Return the depth-0 ledger anchor as ``(harness, session_id)`` when known.

    This is the identity used by route-chain writers. Nested stage workers are
    deliberately not treated as the interactive composing session.
    """
    env = os.environ if environ is None else environ
    try:
        depth = int(env.get("AGENT_DISPATCH_DEPTH") or 0)
    except (TypeError, ValueError):
        return None
    if depth != 0:
        return None
    found = _session_identity(env)
    if not found.known:
        return None
    harness, session_id = found.harness, found.session_id
    if harness not in HARNESSES or WRITER_SUPPORT.get(harness) != "env":
        return None
    try:
        ledger_path(harness, session_id)
    except ValueError:
        return None
    return harness, session_id


# Without the route's creation time the lookup keeps to this many of the newest ledgers.
ANCHOR_SCAN_FILES = 64
# A ledger that composed a route was appended to at or after the route was written; this absorbs
# the timestamp granularity and small clock differences between the route and the state volumes.
ANCHOR_MTIME_SLACK = 5.0


def _composes(harness, session_id, route_id):
    return any(line.get("route_id") == route_id and line.get("event") in COMPOSING_EVENTS
               for line in read_tail(harness, session_id, max_bytes=1024 * 1024))


def composing_anchor(route_id, environ=None, not_before=None):
    """``(harness, session_id)`` of the depth-0 session whose ledger says it composed ``route_id``.

    The answer comes from ledger evidence, never from the asking process: a worker, a
    supervisor, or a session that only started the route is not the composing session. Its
    own ledger is tried first; otherwise exactly one other ledger must carry a composing line
    for the route. No evidence, or more than one session, is None.

    ``not_before`` is the route's creation time (its file's mtime): a ledger last modified
    before it cannot hold the route's compose line, so every later ledger is scanned. Without
    it only the ``ANCHOR_SCAN_FILES`` most recently modified ledgers are.
    """
    if not isinstance(route_id, str) or not route_id:
        return None
    own = writer_identity(environ)
    if own and _composes(own[0], own[1], route_id):
        return own
    recent = []
    for harness in HARNESSES:
        directory = os.path.join(state_root(), harness)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            try:
                recent.append((os.stat(os.path.join(directory, name)).st_mtime, harness, name[:-len(".jsonl")]))
            except OSError:
                continue
    if isinstance(not_before, (int, float)) and not isinstance(not_before, bool):
        candidates = [row for row in recent if row[0] >= not_before - ANCHOR_MTIME_SLACK]
    else:
        candidates = sorted(recent, reverse=True)[:ANCHOR_SCAN_FILES]
    found = set()
    for _mtime, harness, session_id in candidates:
        if (harness, session_id) != own and _composes(harness, session_id, route_id):
            found.add((harness, session_id))
    return next(iter(found)) if len(found) == 1 else None


def state_root():
    explicit = os.environ.get("FLEET_ROUTE_CHAIN_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "route-chains")


def capability_grounding_dir():
    explicit = os.environ.get("FLEET_CAPABILITY_GROUNDING_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "capability-grounding")


def ledger_path(harness, session_id):
    if harness not in HARNESSES:
        raise ValueError("route-chain-unknown-harness")
    if not isinstance(session_id, str) or not _SAFE_KEY_RE.fullmatch(session_id):
        raise ValueError("route-chain-unsafe-session-id")
    return os.path.join(state_root(), harness, session_id + ".jsonl")


def chain_key(values):
    """Rule 1: campaign_key, else `parent-cycle:<id>`, else `_unassigned`."""
    values = values or {}
    campaign_key = values.get("campaign_key")
    if isinstance(campaign_key, str) and campaign_key:
        return campaign_key
    parent_cycle_id = values.get("parent_cycle_id")
    if isinstance(parent_cycle_id, str) and parent_cycle_id:
        return "parent-cycle:" + parent_cycle_id
    return "_unassigned"


def build_line(route, *, event, harness, session_id, route_file, plan=None, plan_source=None,
               dispatch_depth=0, by_attempt=None, now=None):
    """PURE — one ledger line dict. Never carries `work_request` or other large route values."""
    route = route or {}
    selection = route.get("selection") if isinstance(route.get("selection"), dict) else {}
    slug = route.get("slug")
    if isinstance(slug, str) and len(slug) > 80:
        slug = slug[:80]
    plan_list = list(plan)[:MAX_PLAN] if plan else None
    campaign_unassigned = route.get("campaign_unassigned")
    line = {
        "v": SCHEMA,
        "ts": time.time() if now is None else now,
        "event": event,
        "harness": harness,
        "session_id": session_id,
        "route_id": route.get("route_id"),
        "route_hash": route.get("route_hash"),
        "route_file": str(route_file),
        "artifact_root": route.get("artifact_root"),
        "capability": route.get("capability"),
        "capability_mode": route.get("capability_mode"),
        "shape": selection.get("shape") if selection.get("route_origin") == "compose" else None,
        "intensity": route.get("effective_intensity"),
        "campaign_key": route.get("campaign_key"),
        "campaign_unassigned": bool(campaign_unassigned) if campaign_unassigned is not None else None,
        "parent_cycle_id": route.get("parent_cycle_id"),
        "slug": slug,
        "source_route_id": route.get("source_route_id"),
        "plan": plan_list,
        "plan_source": plan_source if plan_list else None,
        "dispatch_depth": dispatch_depth,
        "by_attempt": by_attempt,
    }
    if _automatic(route):
        # An automatic change record (utilities/turn_change_record.py) never stands in for the
        # session's own routes: `own_lines` leaves it out while the session has any.
        line["automatic"] = True
    payload = json.dumps(line, ensure_ascii=False, sort_keys=True)
    if len(payload.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("route-chain-line-too-large")
    return line


def _prepare_directory(directory):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    meta = os.lstat(directory)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise OSError(f"route-chain directory must be an owner directory: {directory} uid={meta.st_uid} expected={os.getuid()}")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def append(harness, session_id, line, *, on_error=None):
    """Single `os.write` under O_APPEND — ext4-local atomic per-line write (K-11). Never raises."""
    try:
        path = ledger_path(harness, session_id)
        payload = (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(payload) > MAX_LINE_BYTES + 1:
            raise ValueError("route-chain-line-too-large")
        _prepare_directory(os.path.dirname(path))
        is_new = not os.path.exists(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
                raise OSError(f"route-chain file must be an owner regular file: {path}")
            if os.write(fd, payload) != len(payload):
                raise OSError(f"route-chain short write: {path}")
        finally:
            os.close(fd)
        if is_new:
            try:
                sweep()
            except Exception:
                pass
        return True
    except (OSError, ValueError, TypeError) as exc:
        if on_error is not None:
            try:
                on_error(f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        return False


def _sweep_dir(directory, now, max_age, skip_dotfiles=False):
    removed = 0
    try:
        dir_meta = os.lstat(directory)
    except OSError:
        return 0
    if not stat.S_ISDIR(dir_meta.st_mode) or dir_meta.st_uid != os.getuid():
        return 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if skip_dotfiles and name.startswith("."):
            continue
        path = os.path.join(directory, name)
        try:
            meta = os.lstat(path)
            if (stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid()
                    and now - meta.st_mtime > max_age):
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


def sweep(now=None, max_age=SWEEP_MAX_AGE):
    """Remove only old, self-owned, regular files. Never raises."""
    current = time.time() if now is None else now
    removed = 0
    try:
        for harness in os.listdir(state_root()):
            removed += _sweep_dir(os.path.join(state_root(), harness), current, max_age)
    except OSError:
        pass
    removed += _sweep_dir(capability_grounding_dir(), current, max_age, skip_dotfiles=True)
    return removed


def short_capability(name):
    """`autopilot-code` -> `code` — the same short form `parse_plan` accepts and Fleet
    displays, so a route's own node and a declared plan step are the same token."""
    if not isinstance(name, str):
        return name
    return name[len("autopilot-"):] if name.startswith("autopilot-") else name


def parse_plan(text, known):
    """Short capability names (`autopilot-` prefix stripped), or raise `ValueError`."""
    if text is None:
        return None
    items = [item.strip() for item in text.split(",")]
    if not items or any(not item for item in items):
        raise ValueError("compose-plan-invalid:empty-item")
    if len(items) > MAX_PLAN:
        raise ValueError("compose-plan-invalid:too-many")
    known_short = {
        (name[len("autopilot-"):] if name.startswith("autopilot-") else name)
        for name in (known or ())
    }
    result = []
    previous = None
    for item in items:
        short = item[len("autopilot-"):] if item.startswith("autopilot-") else item
        if short not in known_short:
            raise ValueError("compose-plan-invalid:unknown:%s" % short)
        if short == previous:
            raise ValueError("compose-plan-invalid:duplicate-consecutive")
        result.append(short)
        previous = short
    return result


def inherited_plan(lines):
    """`(plan, "inherited")` from the latest line in `lines` that carries one; else `([], None)`."""
    for line in reversed(lines or ()):
        plan = line.get("plan")
        if plan:
            return list(plan), "inherited"
    return [], None


def _is_number(value):
    """A present ledger `ts` must be a real number; a bool, string or null makes the whole line
    invalid, so no later sort or comparison can raise on it (one malformed line must not blank
    a tick). An absent `ts` stays valid — every reader already defaults it to 0."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_tail(harness, session_id, max_bytes=TAIL_BYTES):
    """Validated ledger lines from one session ledger; ``None`` reads its full history."""
    try:
        path = ledger_path(harness, session_id)
    except ValueError:
        return []
    try:
        meta = os.lstat(path)
    except OSError:
        return []
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
        return []
    stamp = (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns, meta.st_size)
    cache_key = (path, stamp)
    if max_bytes is None and cache_key in _FULL_HISTORY_CACHE:
        _FULL_HISTORY_CACHE.move_to_end(cache_key)
        return list(_FULL_HISTORY_CACHE[cache_key])
    try:
        with open(path, "rb") as handle:
            offset = max(0, meta.st_size - max_bytes) if max_bytes is not None else 0
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    try:
        after = os.lstat(path)
        after_stamp = (after.st_dev, after.st_ino, after.st_mtime_ns,
                       after.st_ctime_ns, after.st_size)
    except OSError:
        return []
    if stamp != after_stamp:
        return []
    raw_lines = data.decode("utf-8", errors="replace").split("\n")
    if offset > 0 and raw_lines:
        raw_lines = raw_lines[1:]   # partial/foreign first line dropped (mid-file start)
    result = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if (not isinstance(obj, dict) or obj.get("v") != SCHEMA
                or ("ts" in obj and not _is_number(obj["ts"]))
                or obj.get("harness") != harness or obj.get("session_id") != session_id
                or not isinstance(obj.get("route_id"), str) or not obj.get("route_id")
                or not isinstance(obj.get("route_file"), str) or not obj.get("route_file")):
            continue
        result.append(obj)
    if max_bytes is None:
        _FULL_HISTORY_CACHE[cache_key] = tuple(result)
        _FULL_HISTORY_CACHE.move_to_end(cache_key)
        while len(_FULL_HISTORY_CACHE) > _FULL_HISTORY_CACHE_LIMIT:
            _FULL_HISTORY_CACHE.popitem(last=False)
    return result


def session_lines(session, *, full_history=False):
    """Every ledger line reachable from `session`'s own id and its resume aliases, ts-merged."""
    from . import session_registry
    merged = []
    for harness, sid in session_registry.session_join_keys(session):
        merged.extend(read_tail(harness, sid, max_bytes=None if full_history else TAIL_BYTES))
    merged.sort(key=lambda item: item.get("ts") or 0)
    return merged


def own_lines(lines):
    """`lines` without automatic change records, unless those are all the session has."""
    lines = list(lines or [])
    own = [line for line in lines if not line.get("automatic")]
    return own or lines


def current_segment(lines):
    """The continuous tail run of `lines` sharing the last line's chain key (U1). An automatic
    change record does not end or replace the session's own chain (`own_lines`)."""
    lines = own_lines(lines)
    if not lines:
        return []
    key = chain_key(lines[-1])
    segment = []
    for line in reversed(lines):
        if chain_key(line) != key:
            break
        segment.append(line)
    segment.reverse()
    return segment


def assemble(lines, *, load_record, load_outcome, jobs=(), node_evidence=None):
    """PURE (loaders injected) — one displayed chain from a session's ledger lines."""
    from . import route as _route

    segment = current_segment(lines)
    plan, plan_source = inherited_plan(segment)
    key = chain_key(segment[-1]) if segment else "_unassigned"
    if not segment:
        return {"v": 1, "key": key, "visible": False, "plan": plan or [],
                "plan_source": plan_source, "nodes": [], "current": None}

    ordered = []
    seen = set()
    for line in segment:
        rid = line.get("route_id")
        if rid in seen:
            continue
        seen.add(rid)
        ordered.append(line)

    records_by_rid = {}
    nodes = []
    for line in ordered:
        rid = line["route_id"]
        route_file = line.get("route_file")
        route_hash = line.get("route_hash")
        try:
            record = load_record(route_file, route_hash, rid)
        except Exception:
            record = None
        records_by_rid[rid] = record
        ambiguity = []
        result = None
        if record is None:
            capability = line.get("capability")
            capability_mode = line.get("capability_mode")
            shape = None
            intensity = line.get("intensity")
            state = "unknown"
            ambiguity.append("route-record-mismatch")
        else:
            capability = record.get("capability")
            capability_mode = record.get("capability_mode")
            selection = record.get("selection") if isinstance(record.get("selection"), dict) else {}
            shape = selection.get("shape") if selection.get("route_origin") == "compose" else None
            intensity = record.get("effective_intensity")
            try:
                outcome = load_outcome(route_file, rid, route_hash)
            except Exception:
                outcome = None
            result = _route.result_projection(record, outcome)
            if not isinstance(outcome, dict) or not outcome.get("present"):
                state = "open"
            elif outcome.get("match") is False:
                state = "unknown"
                ambiguity.append("outcome-mismatch")
            else:
                tgp = outcome.get("terminal_gate_proven")
                if outcome.get("finish_pending"):
                    state = "finish-pending"
                elif tgp is True:
                    state = "done"
                elif tgp is False:
                    state = "unknown"
                    ambiguity.append("terminal-result-unproven")
                else:
                    state = "unknown"
        nodes.append({
            "label": "fix record" if line.get("automatic") else short_capability(capability), "capability": capability,
            "capability_mode": capability_mode, "automatic": bool(line.get("automatic")),
            "route_id": rid, "route_file": route_file, "state": state, "shape": shape,
            "intensity": intensity, "round": 1, "handoff_to": None, "handoff_from": None,
            "ambiguity": ambiguity, "source_route_id": line.get("source_route_id"),
            "ts": line.get("ts"), "_record": record,
            "result": result,
        })

    # Step 4 — continuation folding (rule 3-i): verified hops collapse onto the earliest slot.
    slots = []
    slot_of = {}
    for node in nodes:
        rid = node["route_id"]
        src = node.get("source_route_id")
        folded = False
        if src and src in slot_of:
            if node["_record"] is not None:
                lineage = _route.continuation_lineage(
                    node["_record"], records_by_rid, node_evidence, jobs,
                )
            else:
                lineage = {"status": "unverified", "reason": "record-malformed"}
            if lineage.get("status") == "verified":
                idx = slot_of[src]
                slots[idx] = node
                slot_of[rid] = idx
                folded = True
            else:
                node["ambiguity"] = list(node.get("ambiguity") or []) + [
                    "continuation-unverified:%s" % lineage.get("reason")]
        if not folded:
            slots.append(node)
            slot_of[rid] = len(slots) - 1

    # Step 5 — a `failed` node immediately followed by the same capability folds to R<n>
    # (rule 3-ii/iii); the last unfolded `failed` node stays as-is.
    folded_nodes = []
    for node in slots:
        if (folded_nodes and folded_nodes[-1]["state"] == "failed"
                and folded_nodes[-1]["label"] == node["label"]):
            node["round"] = folded_nodes[-1]["round"] + 1
            folded_nodes[-1] = node
        else:
            folded_nodes.append(node)

    # Step 6 — plan merge (rule 4, U2, D-3): actual nodes consume declared steps in order;
    # skipped declarations are dropped; unmatched trailing declarations become `planned`.
    result_nodes = []
    pi = 0
    plan = plan or []
    for node in folded_nodes:
        pos = None
        for j in range(pi, len(plan)):
            if plan[j] == node["label"]:
                pos = j
                break
        if pos is not None:
            pi = pos + 1
        result_nodes.append(node)
    for label in plan[pi:]:
        result_nodes.append({
            "label": label, "capability": label, "capability_mode": None, "route_id": None,
            "route_file": None, "state": "planned", "shape": None, "intensity": None,
            "round": None, "handoff_to": None, "handoff_from": None, "ambiguity": [],
            "source_route_id": None, "ts": None, "_record": None,
        })

    # Step 7 — shape is a dial state, shown only on the last actual (non-planned) node.
    last_actual_idx = None
    for idx, node in enumerate(result_nodes):
        if node["state"] != "planned":
            last_actual_idx = idx
    for idx, node in enumerate(result_nodes):
        if idx != last_actual_idx:
            node["shape"] = None

    # Step 8 — cap displayed nodes; fold the front into a count.
    folded_done = 0
    if len(result_nodes) > MAX_NODES:
        folded_done = len(result_nodes) - MAX_NODES
        result_nodes = result_nodes[folded_done:]
        if last_actual_idx is not None:
            last_actual_idx -= folded_done

    for node in result_nodes:
        node.pop("_record", None)

    current = None
    if last_actual_idx is not None and 0 <= last_actual_idx < len(result_nodes):
        current = result_nodes[last_actual_idx]
    chain = {
        "v": 1, "key": key, "visible": len(result_nodes) >= 2 or bool(plan),
        "plan": plan, "plan_source": plan_source, "nodes": result_nodes, "current": current,
    }
    if folded_done:
        chain["folded_done"] = folded_done
    return chain


def current_capability(chain, marker_fields, *, session_start, slack):
    """Rule 5: an open route fresher than `session_start - slack` beats the capability marker."""
    current = (chain or {}).get("current")
    if (isinstance(current, dict) and not current.get("automatic") and current.get("state") == "open"
            and isinstance(current.get("ts"), (int, float))
            and isinstance(session_start, (int, float))
            and current["ts"] >= session_start - slack):
        result = {"capability": current.get("capability"), "intensity": current.get("intensity"),
                  "source": "route-chain", "route_id": current.get("route_id")}
        mode = current.get("capability_mode")
        if mode and mode != "default":
            result["mode"] = mode
        return result
    return marker_fields


def enrich(sessions, jobs=(), node_evidence=None, now=None, fast_first=False,
           only_sessions=None):
    """Attach `session.route_chain` to every eligible session (env-writer harness, not a
    dispatch child, not an app-server row, with a session id). `only_sessions` limits writes
    while all eligible sessions remain available as handoff context. Exceptions are per-session."""
    if fast_first:
        return  # route/outcome and ledger reads belong to the next detail tick
    try:
        from artifact_journal import scan_scope
    except ImportError:
        scan_scope = contextlib.nullcontext
    # Sessions of one project read the same journal folder; scan it once.
    with scan_scope():
        return _enrich(sessions, jobs, node_evidence, now, only_sessions)


def _enrich(sessions, jobs, node_evidence, now, only_sessions):
    from . import route as _route

    node_evidence = node_evidence or {}
    jobs = jobs or ()
    update_ids = None if only_sessions is None else {id(sess) for sess in only_sessions}
    eligible = []
    for sess in sessions or ():
        try:
            harness = str(getattr(sess, "harness", "") or "").lower()
            if (WRITER_SUPPORT.get(harness) != "env"
                    or getattr(sess, "is_child", False)
                    or getattr(sess, "app_server", False)
                    or not getattr(sess, "session_id", None)):
                continue
            eligible.append(sess)
        except Exception:
            continue

    per_session_lines = {}
    per_session_segment = {}
    for sess in eligible:
        try:
            lines = session_lines(sess)
        except Exception:
            lines = []
        per_session_lines[id(sess)] = lines
        try:
            per_session_segment[id(sess)] = current_segment(lines)
        except Exception:
            per_session_segment[id(sess)] = []

    def _identity(sess):
        return (str(getattr(sess, "harness", "") or "").lower(), getattr(sess, "session_id", None))

    origin_of = {}
    receiver_of = {}
    for sess in eligible:
        try:
            for line in per_session_segment.get(id(sess), ()):
                rid = line.get("route_id")
                event = line.get("event")
                if event in ("compose", "compile", "continuation"):
                    bucket, cmp_key = origin_of, rid
                elif event == "start":
                    bucket, cmp_key = receiver_of, rid
                else:
                    continue
                prev = bucket.get(cmp_key)
                if prev is None or line.get("ts", 0) > prev[1].get("ts", 0):
                    bucket[cmp_key] = (sess, line)
        except Exception:
            continue   # one session's bad segment drops only its own handoff marks

    handoffs = {}
    for rid, (origin_sess, origin_line) in origin_of.items():
        receiver = receiver_of.get(rid)
        if receiver is None:
            continue
        receiver_sess, receiver_line = receiver
        if receiver_sess is origin_sess or receiver_line.get("ts", 0) <= origin_line.get("ts", 0):
            continue
        handoffs[rid] = {"origin": _identity(origin_sess), "receiver": _identity(receiver_sess)}

    for sess in eligible:
        if update_ids is not None and id(sess) not in update_ids:
            continue
        try:
            chain = assemble(
                per_session_lines.get(id(sess), []), load_record=_route.load,
                load_outcome=_route.load_outcome, jobs=jobs, node_evidence=node_evidence,
            )
            identity = _identity(sess)
            for node in chain.get("nodes") or []:
                handoff = handoffs.get(node.get("route_id"))
                if not handoff:
                    continue
                if handoff["origin"] == identity:
                    node["handoff_to"] = {"harness": handoff["receiver"][0],
                                           "session_id": handoff["receiver"][1]}
                elif handoff["receiver"] == identity:
                    node["handoff_from"] = {"harness": handoff["origin"][0],
                                             "session_id": handoff["origin"][1]}
            try:
                from turn_change_record import supplementary_records
                from . import session_registry
                chain['automatic_records'] = [row for harness, sid in session_registry.session_join_keys(sess)
                    for row in supplementary_records(harness, sid, getattr(sess, 'cwd', '') or '')]
                chain['visible'] = chain.get('visible', False) or bool(chain['automatic_records'])
            except Exception:
                chain['automatic_records'] = []
            sess.route_chain = chain
        except Exception:
            try:
                sess.route_chain = None
            except Exception:
                pass
