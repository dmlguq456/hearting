"""One session's story for the NOW popup, and the reads behind each of its rows.

The person's prompts, the NOW values and the session's other events (route start and end,
questions, messages with other sessions) in time order, oldest first (``rows``); the
conversation a prompt or NOW row stands for (``passage``); the folded view of a list taller
than its room (``fold``); and how far a harness's own view must step to reach a prompt
(``scroll_steps``). The popup draws only what these return.

Every source is an existing record, read the way the panel and Fleet already read it:

* prompts and NOW values: the session's timeline file (``titles.read_timeline``);
* clears, compactions and handovers: the session's own tidy ledger (``session_tidy``), whose
  handover/succession edge (``confirmed_predecessor_identity``) links an earlier generation;
* routes: the session's route-chain ledger (``route_chain.read_tail``) with each route's record
  and outcome sidecar (``route.load``, ``route.load_outcome``, ``route_chain.outcome_state``);
* questions: the answer-wait board (``human_wait_state.cached_view``, ``questions.describe``),
  the session's S4 signal (an answered one stays on disk) and the board's closed S1 ids;
* messages: the sender ledger shard this session writes and its retained received inputs
  (``session_tidy.prompt_display``), worded by the panel's ``peer_message_rows``;
* a passage: ``tidy_transcripts.read_chunk`` (its OpenCode reader for seq/rowid spans) drawn
  by Fleet's ``transcript.dialogue_rows``.

Nothing here writes a record, draws, sends a key to a harness or asks a model.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from .. import titles

HARNESSES = ("claude", "codex", "opencode")
GENERATIONS = 8               # earlier generations followed through the handover/succession edge
TEXT_CHARS = 384              # session_tidy.PROMPT_LINE_CHARS: one row's text at most
NOW_WINDOW = 64 * 1024        # refresh_title.DELTA_CAP: the bytes a NOW's model read at most
PASSAGE_HEAD = 128 * 1024     # a long passage reads its first 128 KiB ...
PASSAGE_TAIL = 256 * 1024     # ... and its last 256 KiB, with `…` between
PASTE_LINES = 6               # lines of a pasted block a passage shows
RECOVER_BYTES = 64 * 1024     # a prompt's own row is looked for this far before its cursor
SENT_READ_MAX = 1024 * 1024   # tail of one monthly sender shard
SENT_MONTHS = 3
MESSAGES_MAX = 400
DIVIDER_MERGE_SEC = 120       # SessionStart(compact) and its compact event are one compaction
SCAN_BLOCK = 4 * 1024 * 1024

# What each harness's own view counts when it steps through prompts (11-16 study,
# DESIGN-NOW-REAL-SCROLL-1116; read from installed releases and sources, never run). Each value
# is one assumption nobody has checked against a running harness; flipping it here is the whole fix.
SCROLL_RULES = {
    # Claude 2.1.325 skips a prompt whose text (after leading <system-reminder> blocks) starts with
    # `<`. A prompt that is one pasted block is assumed stored as `<pasted_content …>`, so skipped.
    "claude_counts_paste_only": False,
    # A prompt typed while Claude worked (a `queued_command` attachment, not a task notification)
    # is assumed to be one of the prompts `{` stops at.
    "claude_counts_queued": True,
    # A Claude row from another session (origin peer with a sender task, or observer) is not counted.
    "claude_skips_peer_origin": True,
    # Codex counts every user message after its session head, Hearting's own deliveries included.
    "codex_counts_injected": True,
    # OpenCode's timeline lists every `user` message, Hearting's deliveries and empty ones included,
    # and leaves out synthetic-only rows and subagent/shell notifications.
    "opencode_counts_injected": True,
}

_RANK = {"date": 0, "divider": 1, "prompt": 2, "question": 3, "route": 4, "message": 5, "now": 6}
_ROLE_LINE = re.compile(r"\[([A-Za-z_ ]+)\] ")       # tidy_transcripts dialogue text (Fleet focus)
_PASTE = re.compile(r'<pasted_content id="(?P<id>[^"\r\n]+)">\n?(?P<body>.*?)\n?'
                    r'</pasted_content(?: id="(?P=id)")?>', re.S)
_REMINDER = re.compile(r"\A\s*<system-reminder>.*?</system-reminder>\s*", re.S)
_DIM = ("d", 0, 1)
_SAFE_SID = re.compile(r"[A-Za-z0-9._-]{1,128}")


_UTILITIES = str(Path(os.path.realpath(__file__)).parents[3] / "utilities")
_MODULES: dict = {}
_LINES: dict = {}             # masked lines already made: a list read again costs no second masking
_LINES_MAX = 16384


def _utility(name):
    """A shared ``utilities/`` module, imported once (the same loader Fleet's explorer uses)."""
    module = _MODULES.get(name)
    if module is None:
        if _UTILITIES not in sys.path:
            sys.path.insert(0, _UTILITIES)
        module = _MODULES[name] = importlib.import_module(name)
    return module


def _peer_message():
    """``utilities/peer-message.py``, under the name the panel's backend already loads it by."""
    name = "hearting_shell_peer_message"
    if name not in sys.modules:
        import importlib.util
        path = Path(_UTILITIES) / "peer-message.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _cursor(value):
    return value if type(value) is int and value >= 0 else None


def _line(text, limit=TEXT_CHARS):
    """One printable line: the first non-empty one, secrets masked, at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    key = (text, limit)
    held = _LINES.get(key)
    if held is None:
        if len(_LINES) >= _LINES_MAX:
            _LINES.clear()
        held = _LINES[key] = _masked_line(text, limit)
    return held


def _masked_line(text, limit):
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
    try:
        line = _utility("work_intent").redact_secrets(line)
    except Exception:
        line = ""
    line = "".join(char for char in line if char.isprintable())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _safe(sid):
    return isinstance(sid, str) and _SAFE_SID.fullmatch(sid) is not None


def _seat(tidy, harness, sid):
    """The exact native session's seat: its ledger and prompt display depend on (harness, sid) only."""
    key = tidy._native_session_key(harness, sid)
    return tidy.session_seat(tidy.Seat("native", key, harness=harness), harness, sid)


# ---------------------------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------------------------

def rows(harness, sid, *, now=None, generations=GENERATIONS, events=True):
    """The session's story, oldest first. Never raises; a source that cannot be read adds nothing.

    Every row is a dict with ``key`` (stable across reads), ``at`` (epoch seconds), ``kind``,
    ``text`` (one line, masked and bounded), ``harness``, ``sid``, ``generation`` (0 is this
    session, 1 the one it continued, …) and ``open`` (what Enter opens, or None):

    * ``date``: ``date`` (``YYYY-MM-DD``), ``today``; the first row of each local day.
    * ``divider``: ``sub`` ``compact`` | ``clear`` | ``handover``.
    * ``prompt``: ``answer`` (it answered a question), ``lines``, ``paste``, ``seq``.
    * ``now``: ``prompt`` (the key of the prompt it follows), ``current`` (the latest NOW).
    * ``route``: ``sub`` ``start`` | ``end``, ``route_id``, ``capability``, ``intensity``,
      ``state``; an end row also has ``result`` (``passed`` | ``unproven``) and ``elapsed``.
    * ``question``: ``sub`` ``asked`` | ``answered`` | ``closed``, ``id``, ``source`` (S1-S4).
    * ``message``: ``sub`` ``received`` | ``sent``, ``who``, ``message_kind``, ``delivery``
      (the panel's word for a sent one's state, else None), ``id``.

    ``open`` is ``{"kind": "passage", …}`` for a prompt or NOW (pass it to ``passage``) and
    ``{"kind": "popup", "popup": "routes" | "questions" | "messages", "id": …}`` for an event.
    """
    if harness not in HARNESSES or not _safe(sid):
        return []
    now = time.time() if now is None else float(now)
    found = []
    try:
        tidy = _utility("session_tidy")
    except Exception:
        tidy = None
    with contextlib.ExitStack() as stack:
        if tidy is not None:
            # One parse of each ledger for the whole read, without defensive copies.
            stack.enter_context(tidy.seat_ledger_read_scope())
            stack.enter_context(tidy.seat_ledger_readonly_scope())
        chain = _chain(tidy, harness, sid, generations)
        for generation, (gsid, ledger) in enumerate(chain):
            story = _guard(_story_rows, harness, gsid, generation)
            found.extend(story)
            found.extend(_guard(_divider_rows, harness, gsid, generation, ledger))
            if events:
                found.extend(_guard(_route_rows, harness, gsid, generation))
                found.extend(_guard(_message_rows, tidy, harness, gsid, generation, story, now))
        if events:
            sids = {gsid: generation for generation, (gsid, _ledger) in enumerate(chain)}
            found.extend(_guard(_question_rows, harness, sids, now))
    return _finish(found, now)


def _guard(source, *args):
    try:
        return list(source(*args))
    except Exception:
        return []


def _chain(tidy, harness, sid, limit):
    """``[(sid, ledger rows)]`` from this session back through its confirmed predecessors."""
    chain, seen, current = [], set(), sid
    while current and current not in seen and len(chain) < max(1, int(limit)):
        seen.add(current)
        ledger, prior = [], None
        if tidy is not None:
            try:
                seat = _seat(tidy, harness, current)
                ledger = list(tidy._read_ledger_lines(seat, include_physical_relations=False))
                prior = tidy.confirmed_predecessor_identity(seat, harness, current)
            except Exception:
                ledger, prior = ledger, None
        chain.append((current, ledger))
        current = (prior.get("sid") if isinstance(prior, dict) and prior.get("harness") == harness
                   and _safe(prior.get("sid")) else None)
    return chain


def _story_rows(harness, sid, generation):
    """The timeline file's prompt and NOW rows, each with the span its passage reads."""
    story = []
    for order, item in enumerate(titles.read_timeline(sid, harness=harness)):
        text = _line(item.get("text"))
        row = {"at": float(item["at"]), "kind": item["kind"], "text": text, "harness": harness,
               "sid": sid, "generation": generation, "_order": order}
        if item["kind"] == "prompt":
            lines = item.get("lines")
            row.update(answer=item.get("answer") is True, paste=item.get("paste") is True,
                       lines=lines if type(lines) is int and lines > 0 else 1,
                       seq=item.get("seq") if type(item.get("seq")) is int else None,
                       _from=_cursor(item.get("from")))
        else:
            unit = item.get("unit") if item.get("unit") in ("bytes", "seq", "rowid") else ""
            row.update(_unit=unit, _cursor_kind=item.get("cursor_kind") if isinstance(item.get("cursor_kind"), str) else "",
                       _from=_cursor(item.get("from")), _to=_cursor(item.get("to")),
                       _read_from=_cursor(item.get("read_from")))
        story.append(row)
    _spans(story)
    return story


def _spans(story):
    """Each prompt and NOW row's ``open`` passage (follow-up 1 of the PR #1661 review).

    A NOW's passage is the window its model really read: ``read_from`` when recorded, else the last
    ``NOW_WINDOW`` bytes of its span (the reader's cap), and for a span that went backwards (the
    conversation file was replaced) the last ``NOW_WINDOW`` bytes before its end, as the reader
    did. A gap before the next NOW (a refresh that repeated this NOW or skipped a short peer turn
    moved the cursor without a row) belongs to this NOW. A prompt's passage runs to the next prompt.
    """
    prompts = [row for row in story if row["kind"] == "prompt"]
    nows = [row for row in story if row["kind"] == "now"]
    owner, marks, current = {}, {}, None     # each NOW's prompt, each prompt's NOWs (file order)
    for row in story:
        if row["kind"] == "prompt":
            current = row
            marks[id(row)] = []
        elif current is not None:
            owner[id(row)] = current
            marks[id(current)].append(row)
    for index, row in enumerate(nows):
        start, end, unit, note = row["_from"], row["_to"], row["_unit"], ""
        if start is not None and end is not None:
            if unit == "bytes":
                if row["_read_from"] is not None and row["_read_from"] <= end:
                    start, note = row["_read_from"], "reversed" if start > end else ""
                elif start > end:
                    start, note = max(0, end - NOW_WINDOW), "reversed"
                else:
                    start = max(start, end - NOW_WINDOW)
            nxt = nows[index + 1] if index + 1 < len(nows) else None
            if (nxt and nxt["_unit"] == unit and nxt["_cursor_kind"] == row["_cursor_kind"]
                    and nxt["_from"] is not None and nxt["_to"] is not None
                    and end < nxt["_from"] <= nxt["_to"] and start <= end):
                end, note = nxt["_from"], note or "gap"
        prompt = owner.get(id(row))
        row["open"] = {"kind": "passage", "row": "now", "harness": row["harness"], "sid": row["sid"],
                       "unit": unit, "from": start, "to": end, "at": row["at"], "text": row["text"],
                       "prompt": prompt["text"] if prompt else "", "note": note}
        row["_prompt_row"] = prompt
    for index, row in enumerate(prompts):
        nxt = prompts[index + 1] if index + 1 < len(prompts) else None
        row["open"] = {"kind": "passage", "row": "prompt", "harness": row["harness"], "sid": row["sid"],
                       "unit": "bytes" if row["harness"] in ("claude", "codex") else "",
                       "from": row["_from"], "to": nxt["_from"] if nxt else None,
                       "at": row["at"], "until": nxt["at"] if nxt else None, "text": row["text"],
                       "marks": [{"at": now["at"], "text": now["text"], "cursor": now["_to"], "unit": now["_unit"]}
                                 for now in marks[id(row)]]}


def _divider_rows(harness, sid, generation, ledger):
    """Clears, compactions and handovers this session's own ledger recorded."""
    out, last = [], {}
    for row in ledger:
        if not isinstance(row, dict) or (row.get("harness"), row.get("sid")) != (harness, sid):
            continue
        at, event, source = _number(row.get("ts")), row.get("event"), row.get("source")
        if at is None:
            continue
        if event == "compact" or event == "start" and source == "compact":
            sub = "compact"
        elif event == "succession" and row.get("from"):
            sub = "clear"
        elif event == "handover" and row.get("from"):
            sub = "handover"
        elif event == "start" and source == "clear":
            sub = "clear"
        else:
            continue
        group = "compact" if sub == "compact" else "generation"
        if group in last and abs(at - last[group]) <= DIVIDER_MERGE_SEC:
            continue
        last[group] = at
        out.append({"at": at, "kind": "divider", "sub": sub, "text": "", "harness": harness,
                    "sid": sid, "generation": generation, "open": None})
    return out


def _route_rows(harness, sid, generation):
    """Each route this session started or took over: a start row, and an end row once it closed."""
    from .. import route, route_chain
    from ..work_titles import readable
    first = {}
    for line in route_chain.own_lines(route_chain.read_tail(harness, sid, max_bytes=None)):
        if line.get("event") not in route_chain.OWNING_EVENTS or _number(line.get("ts")) is None:
            continue
        held = first.get(line["route_id"])
        if held is None or line["ts"] < held["ts"]:
            first[line["route_id"]] = line
    out = []
    for rid, line in first.items():
        record = route.load(line["route_file"], line.get("route_hash"), rid) or {}
        outcome = route.load_outcome(line["route_file"], rid, line.get("route_hash")) if record else None
        state = route_chain.outcome_state(outcome)[0] if record else "unknown"
        title = (readable(record.get("display_title")) or readable(record.get("title"))
                 or readable(record.get("slug")) or readable(line.get("slug")) or rid)
        capability = route_chain.short_capability(record.get("capability") or line.get("capability"))
        intensity = record.get("effective_intensity") or line.get("intensity")
        base = {"kind": "route", "harness": harness, "sid": sid, "generation": generation,
                "route_id": rid, "text": _line(title, 160),
                "capability": capability if isinstance(capability, str) else "",
                "intensity": intensity if isinstance(intensity, str) else "", "state": state,
                "open": {"kind": "popup", "popup": "routes", "id": rid}}
        out.append({**base, "at": float(line["ts"]), "sub": "start"})
        closed = _epoch((outcome or {}).get("closed_at")) if isinstance(outcome, dict) else None
        if closed is not None and state in ("done", "unknown") and outcome.get("match") is not False:
            out.append({**base, "at": closed, "sub": "end", "result": "passed" if state == "done" else "unproven",
                        "elapsed": max(0.0, closed - float(line["ts"]))})
    return out


def _epoch(value):
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _question_rows(harness, sids, now):
    """Questions these sessions asked: the board's waits, the S4 signal kept on disk (one per
    session; an answered one stays) and the board's closed S1 ids (no text is kept for those)."""
    hws, questions = _utility("human_wait_state"), _utility("questions")
    view = hws.cached_view(request=False) or {}
    out, seen = [], set()

    def add(sid, at, sub, qid, source, text, state=""):
        if sid not in sids or _number(at) is None or (qid, sub) in seen:
            return
        seen.add((qid, sub))
        out.append({"at": float(at), "kind": "question", "sub": sub, "id": qid, "source": source,
                    "text": _line(text, 240), "state": state, "harness": harness, "sid": sid,
                    "generation": sids[sid], "open": {"kind": "popup", "popup": "questions", "id": qid}})

    for item in view.get("items", ()) if isinstance(view.get("items"), list) else ():
        owner = item.get("owner") if isinstance(item, dict) else None
        if (not isinstance(owner, dict) or owner.get("harness") != harness
                or owner.get("session_id") not in sids or item.get("stale")):
            continue
        described = questions.describe(item, now=now)
        first = next((q.get("question") for q in described.get("questions") or () if isinstance(q, dict)), "")
        add(owner["session_id"], described.get("since"), "asked", str(described.get("id") or ""),
            str(described.get("source") or ""), first or "", described.get("request_state") or "open")
    try:
        paths = list(hws.state_root().glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        if not re.fullmatch(r"[0-9a-f]{64}", path.stem):
            continue
        signal = hws._read(path)
        if not signal or signal.get("harness") != harness or signal.get("session_id") not in sids:
            continue
        add(signal["session_id"], signal.get("since"), "asked", signal["id"], "S4", signal.get("question"),
            signal.get("lifecycle") or "")
        completion = signal.get("completion") if isinstance(signal.get("completion"), dict) else {}
        # An answer typed as the next prompt is that prompt's row (`answer`); another one is its own row.
        if signal.get("lifecycle") == "answered" and completion.get("actor") != "direct":
            add(signal["session_id"], completion.get("at"), "answered", signal["id"], "S4",
                signal.get("question"), "answered")
    # The body-free closed history the board's cache file keeps (``closed_question`` reads it the
    # same way; ``cached_view`` does not carry it).
    projection = hws._read_cache_file() or {}
    closed = projection.get("closed_questions") if isinstance(projection.get("closed_questions"), dict) else {}
    for qid, entry in closed.items():
        parts = qid.split(":") if isinstance(qid, str) else []
        if len(parts) != 4 or parts[0] != "S1" or parts[1] != harness or not parts[3].isdigit():
            continue
        if not isinstance(entry, dict):
            continue
        add(parts[2], int(parts[3]) / 1000, "asked", qid, "S1", "", entry.get("status") or "")
        add(parts[2], entry.get("at"), "answered" if entry.get("status") == "answered" else "closed",
            qid, "S1", "", entry.get("status") or "")
    return out


def _message_rows(tidy, harness, sid, generation, story, now):
    """Messages with other sessions: the received inputs this session retained and the records
    of its own sender shard, worded by the panel's own ``peer_message_rows``."""
    from ..session_line import _text, peer_message_rows
    from ..session_tags import assigned_tag
    received = tidy.prompt_display(_seat(tidy, harness, sid), harness, sid).get("received") or []
    oldest = min((row["at"] for row in story), default=now)
    sent = _sent_records(sid, oldest, now)
    # 11-17: a sent message that has not gone in reads `awaiting delivery` and why, as the panel's MESSAGES do.
    worded = peer_message_rows(received, sent, assigned_tag, limit=MESSAGES_MAX, now=now,
                               pending_of=lambda ref: _peer_message()._read_pending(ref))
    out = []
    for row in worded:
        if row["way"] == "←":
            match = next((item for item in received if isinstance(item, dict) and item.get("source") == "peer"
                          and (item.get("at") or 0) == row["at"] and _text(item.get("text"), "") == row["text"]), {})
            ident = match.get("ref") if isinstance(match.get("ref"), str) else ""
        else:
            match = next((item for item in sent if (item.get("at") or 0) == row["at"]
                          and _text(item.get("summary"), "") == row["text"]), {})
            ident = match.get("message_id") or ""
        out.append({"at": float(row["at"]), "kind": "message", "sub": "received" if row["way"] == "←" else "sent",
                    "who": row.get("who") or "", "message_kind": row.get("kind") or "",
                    "delivery": (row.get("delivery") or [None])[0], "id": ident,
                    "text": _line(row.get("text"), 240), "harness": harness, "sid": sid,
                    "generation": generation, "open": {"kind": "popup", "popup": "messages", "id": ident}})
    return out


def _sent_records(sid, oldest, now):
    """This session's sent peer records from its monthly sender shards (the panel's reader
    reads the same shard's last 32 KiB; this reads up to ``SENT_READ_MAX`` of each month)."""
    peer = _peer_message()
    months, stamp = [], time.gmtime(now)
    year, month = stamp.tm_year, stamp.tm_mon
    first = time.gmtime(max(0.0, oldest))
    for _ in range(SENT_MONTHS):
        months.append((year, month))
        if (year, month) <= (first.tm_year, first.tm_mon):
            break
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    rows = []
    for year, month in reversed(months):
        path = peer._ledger_path(sid, time.struct_time((year, month, 1, 0, 0, 0, 0, 1, 0)))
        try:
            with open(path, "rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                stream.seek(max(0, size - SENT_READ_MAX))
                raw = stream.read(SENT_READ_MAX).decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in raw[1:] if size > SENT_READ_MAX else raw:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            source = record.get("from") if isinstance(record, dict) else None
            if (not isinstance(source, dict) or source.get("session_id") != sid
                    or not isinstance(record.get("summary"), str) or not isinstance(record.get("ts"), str)):
                continue
            at = _epoch(record["ts"])
            if at is None:
                continue
            delivery = record.get("delivery") if isinstance(record.get("delivery"), dict) else {}
            status = delivery.get("status") if isinstance(delivery.get("status"), str) else None
            message_id = record.get("message_id") if isinstance(record.get("message_id"), str) else None
            earlier = next((row for row in rows if message_id and row.get("message_id") == message_id), None)
            if earlier is not None:
                earlier["status"] = status or earlier.get("status")
                continue
            rows.append({"to": record.get("to") if isinstance(record.get("to"), dict) else {},
                         "summary": record["summary"][:400], "at": at, "message_id": message_id,
                         "kind": record.get("kind") if isinstance(record.get("kind"), str) else None,
                         "status": status})
    return rows


def _finish(found, now):
    """Order the rows (the same order for the same time, every read), date rows, keys, ``current``."""
    found = [row for row in found if _number(row.get("at")) is not None and row.get("kind") in _RANK]
    found.sort(key=lambda row: (row["at"], _RANK[row["kind"]], -row.get("generation", 0),
                                row.get("_order", 0), row.get("sub", ""), row.get("text", "")))
    out, day, keys = [], None, set()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    for row in found:
        date = time.strftime("%Y-%m-%d", time.localtime(row["at"]))
        if date != day:
            day = date
            out.append({"key": "date:" + date, "at": row["at"], "kind": "date", "date": date,
                        "text": date[5:], "today": date == today, "harness": row["harness"],
                        "sid": row["sid"], "generation": row.get("generation", 0), "open": None})
        out.append(row)
    for row in out:
        key = row["key"] if row["kind"] == "date" else _key(row)
        base, count = key, 1
        while key in keys:
            count += 1
            key = "%s#%d" % (base, count)
        keys.add(key)
        row["key"] = key
    current = next((row for row in reversed(out) if row["kind"] == "now" and row.get("generation") == 0), None)
    for row in out:
        if row["kind"] == "now":
            prompt = row.get("_prompt_row")
            row["prompt"] = prompt.get("key") if prompt else None
            row["current"] = row is current
    for row in out:
        for name in [name for name in row if name.startswith("_")]:
            del row[name]
    return out


def _key(row):
    kind, sid = row["kind"], row["sid"]
    stamp = "%d" % round(row["at"] * 1000)
    if kind == "prompt":
        return "prompt:%s:%s" % (sid, row["seq"] if row.get("seq") is not None else stamp)
    if kind == "now":
        return "now:%s:%s" % (sid, stamp)
    if kind == "route":
        return "route:%s:%s" % (row["route_id"], row["sub"])
    if kind in ("question", "message"):
        return "%s:%s:%s:%s" % (kind, row.get("id") or stamp, row["sub"], stamp if not row.get("id") else "")
    return "%s:%s:%s:%s" % (kind, sid, stamp, row.get("sub", ""))


# ---------------------------------------------------------------------------------------------
# The folded view
# ---------------------------------------------------------------------------------------------

def fold(story, room, selected=None):
    """The rows a list of ``room`` lines shows: ``[{"index": i, "folded": n}]`` in order.

    A list that fits is shown whole. Otherwise each prompt keeps its own line and its last NOW,
    whose ``folded`` says how many of the prompt's other rows it stands for (``+N``); the prompt
    that holds ``selected`` (an index into ``story``) stays open, and date and divider rows are
    never folded. A group with no NOW keeps its prompt, which then carries the count.
    """
    story = list(story or ())
    if len(story) <= max(0, int(room)):
        return [{"index": index, "folded": 0} for index in range(len(story))]
    groups, current = [], None
    for index, row in enumerate(story):
        kind = row.get("kind") if isinstance(row, dict) else None
        if kind in ("date", "divider"):
            groups.append({"head": index, "members": [], "fixed": True})
            current = None
        elif kind == "prompt":
            current = {"head": index, "members": [], "fixed": False}
            groups.append(current)
        else:
            if current is None:
                current = {"head": None, "members": [], "fixed": False}
                groups.append(current)
            current["members"].append(index)
    shown = []
    for group in groups:
        indices = ([group["head"]] if group["head"] is not None else []) + group["members"]
        if group["fixed"] or selected in indices or len(group["members"]) <= 1:
            shown.extend({"index": index, "folded": 0} for index in indices)
            continue
        last_now = next((index for index in reversed(group["members"]) if story[index].get("kind") == "now"), None)
        hidden = len(group["members"]) - (1 if last_now is not None else 0)
        if group["head"] is not None:
            shown.append({"index": group["head"], "folded": 0 if last_now is not None else hidden})
        if last_now is not None:
            shown.append({"index": last_now, "folded": hidden})
        elif group["head"] is None:
            shown.append({"index": group["members"][-1], "folded": hidden - 1})
    return shown


# ---------------------------------------------------------------------------------------------
# Reading a passage
# ---------------------------------------------------------------------------------------------

def passage(target, width):
    """The conversation one prompt or NOW row stands for, drawn as the harness draws it.

    ``target`` is the row's ``open`` value. Returns ``{"ok", "reason", "rows", "marks",
    "clipped", "span", "head"}``: ``rows`` are ``transcript.dialogue_rows`` rows (and a dim line
    for each NOW inside a prompt's passage, whose indices are ``marks``); ``head`` is the prompt
    a NOW follows. A span longer than ``PASSAGE_HEAD + PASSAGE_TAIL`` reads its head and tail
    with a ``…`` row between (``clipped``); a pasted block shows ``PASTE_LINES`` lines. A span
    that cannot be read gives no rows and a reason (``invalid-target``, ``no-transcript``,
    ``span-reversed``, ``transcript-shorter``, ``no-cursor``, ``prompt-not-found``,
    ``cursor-unit-changed``, ``unreadable``); this never raises.
    """
    result = {"ok": False, "reason": "invalid-target", "rows": [], "marks": [], "clipped": False,
              "span": None, "head": ""}
    if (not isinstance(target, dict) or target.get("kind") != "passage"
            or target.get("harness") not in HARNESSES or not _safe(target.get("sid"))
            or type(width) is not int or width < 8):
        return result
    result["head"] = (target.get("prompt") or "") if target.get("row") == "now" else ""
    try:
        if target["harness"] == "opencode":
            return _opencode_passage(target, width, result)
        return _jsonl_passage(target, width, result)
    except Exception as exc:  # noqa: BLE001 - a reader of display text never fails its caller
        result.update(ok=False, reason="unreadable", rows=[], marks=[], detail=type(exc).__name__)
        return result


def _tidy_transcripts():
    return _utility("tidy_transcripts")


def _jsonl_passage(target, width, result):
    tt = _tidy_transcripts()
    harness, sid = target["harness"], target["sid"]
    path = tt.locate_transcript(harness, sid)
    if path is None:
        return {**result, "reason": "no-transcript"}
    size = os.stat(path).st_size
    start, end = _cursor(target.get("from")), _cursor(target.get("to"))
    if target.get("row") == "prompt":
        if start is None:
            start = _offset_at(path, target.get("at"))
            if start is None:
                return {**result, "reason": "no-cursor"}
        found, _matched = _prompt_offset(path, harness, start, target.get("text") or "", counted=False)
        start = found if found is not None else start
        if end is None and _number(target.get("until")) is not None:
            end = _offset_at(path, target["until"])
        end = size if end is None else end
    elif start is None or end is None:
        return {**result, "reason": "no-cursor"}
    if start > end:
        return {**result, "reason": "span-reversed", "span": {"from": start, "to": end, "unit": "bytes"}}
    if end > size:
        return {**result, "reason": "transcript-shorter", "span": {"from": start, "to": end, "unit": "bytes"}}
    ranges, clipped = _ranges(start, end)
    marks = [mark for mark in target.get("marks") or () if isinstance(mark, dict)
             and mark.get("unit") == "bytes" and _cursor(mark.get("cursor")) is not None
             and start < mark["cursor"] <= end]
    pieces = []
    for number, (low, high) in enumerate(ranges):
        if number:
            pieces.append(("gap", None))
        cuts = sorted({low, high, *(mark["cursor"] for mark in marks if low < mark["cursor"] < high)})
        for left, right in zip(cuts, cuts[1:]):
            chunk = tt.read_chunk(harness, str(path), left, sid=sid, limit_bytes=max(1, right - left))
            if chunk.error:
                return {**result, "reason": "unreadable", "detail": chunk.error}
            pieces.append(("text", chunk.text))
            for mark in marks:
                if mark["cursor"] == right:
                    pieces.append(("mark", mark))
    for mark in marks:                      # a NOW right at a range edge outside the cuts
        if mark["cursor"] == end and ("mark", mark) not in pieces:
            pieces.append(("mark", mark))
    return _draw(pieces, width, harness, {**result, "ok": True, "reason": "", "clipped": clipped,
                                          "span": {"from": start, "to": end, "unit": "bytes"}})


def _ranges(start, end):
    if end - start <= PASSAGE_HEAD + PASSAGE_TAIL:
        return [(start, end)], False
    return [(start, start + PASSAGE_HEAD), (end - PASSAGE_TAIL, end)], True


def _draw(pieces, width, harness, result):
    """Rows of the read pieces: dialogue rows, a `…` row between head and tail, a dim NOW line."""
    from .transcript import dialogue_rows
    work_intent = _utility("work_intent")
    rows, marks = [], []
    for kind, value in pieces:
        if kind == "gap":
            rows.append(([("…", _DIM)], None))
        elif kind == "mark":
            marks.append(len(rows))
            rows.append(([("· ", _DIM), (time.strftime("%H:%M", time.localtime(value["at"])), _DIM),
                          ("  ", None), (value.get("text") or "", _DIM)], None))
        elif value:
            lines = _role_lines(work_intent.redact_secrets(_fold_pastes(value)))
            drawn = dialogue_rows(lines, width, harness)
            if drawn and rows:
                rows.append(([], None))
            rows.extend(drawn)
    return {**result, "rows": rows, "marks": marks}


def _fold_pastes(text):
    def short(match):
        lines = match.group("body").splitlines()
        kept = "\n".join(lines[:PASTE_LINES])
        return kept + ("\n… +%d lines" % (len(lines) - PASTE_LINES) if len(lines) > PASTE_LINES else "")
    return _PASTE.sub(short, text)


def _role_lines(text):
    """``tidy_transcripts`` dialogue text as ``role: text`` lines (Fleet ``focus.read_dialogue``)."""
    out = []
    for raw in str(text).split("\n"):
        match = _ROLE_LINE.match(raw)
        out.append(f"{match.group(1).lower()}: {raw[match.end():]}" if match else raw)
    return out


def _row_time(data):
    stamp = data.get("timestamp") if isinstance(data, dict) else None
    return _epoch(stamp) if isinstance(stamp, str) else None


def _offset_at(path, at):
    """The offset of the first row not older than ``at`` (rows that carry a ``timestamp``), by
    bisection over row boundaries; the file size when every row is older."""
    if _number(at) is None:
        return None

    def first_stamp(handle, position, limit):
        """``(row start, stamp)`` of the first stamped row at or after ``position`` before ``limit``."""
        handle.seek(position)
        if position:
            handle.readline()
        while handle.tell() < limit:
            start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            try:
                stamp = _row_time(json.loads(raw))
            except ValueError:
                stamp = None
            if stamp is not None:
                return start, stamp
        return None, None

    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        low, high = 0, size
        while high - low > 64 * 1024:
            middle = (low + high) // 2
            start, stamp = first_stamp(handle, middle, high)
            if start is None or stamp >= at:
                high = middle
            else:
                low = start
        handle.seek(low)
        if low:
            handle.readline()
        while True:
            start = handle.tell()
            raw = handle.readline()
            if not raw:
                return size
            try:
                stamp = _row_time(json.loads(raw))
            except ValueError:
                stamp = None
            if stamp is not None and stamp >= at:
                return start


def _prompt_offset(path, harness, cursor, text, *, counted=True):
    """The prompt row's own offset near ``cursor`` (follow-up 8 of the PR #1661 review).

    The hook's ``from`` is the file size when it saw the prompt; a harness that wrote the prompt
    first leaves it after the row. The row is looked for in the ``RECOVER_BYTES`` before
    ``cursor`` and the first rows after it: the nearest user row whose first line is the row's
    text, else the nearest user row after ``cursor``. Returns ``(offset or None, matched)``;
    ``counted=False`` also looks at user rows the harness's view does not stop at.
    """
    low = max(0, cursor - RECOVER_BYTES)
    with open(path, "rb") as handle:
        handle.seek(low)
        data = handle.read(cursor - low + RECOVER_BYTES)
    position, best, after = low, None, None
    lines = data.split(b"\n")
    if low:
        position += len(lines[0]) + 1
        lines = lines[1:]
    wanted = _match_key(text)
    for raw in lines:
        offset, position = position, position + len(raw) + 1
        user = _user_text(harness, raw) if counted else _any_user_text(harness, raw)
        if user is None:
            continue
        if wanted and _match_key(user) == wanted and (best is None or abs(offset - cursor) < abs(best - cursor)):
            best = offset
        if after is None and offset >= cursor:
            after = offset
    return (best, True) if best is not None else (after, False)


def _any_user_text(harness, raw):
    """A user row's text whether or not the harness's view stops at it (tool results are not)."""
    if harness == "codex":
        return _user_text(harness, raw)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "user":
        return None
    content = (data.get("message") or {}).get("content") if isinstance(data.get("message"), dict) else None
    if isinstance(content, str):
        return content
    first = content[0] if isinstance(content, list) and content else None
    return first.get("text") if isinstance(first, dict) and first.get("type") == "text" else None


def _match_key(text):
    first = _first_line_plain(text).rstrip("…").strip()
    return first[:60]


def _first_line_plain(text):
    try:
        return _utility("session_tidy").prompt_first_line(text)
    except Exception:
        return _line(text)


def _user_text(harness, raw):
    """A user prompt's text in one transcript row, else None (tool results and runtime rows are not)."""
    if harness == "codex":
        if b'"user_message"' not in raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "user_message":
            return None
        message = payload.get("message") if isinstance(payload.get("message"), str) else ""
        if not SCROLL_RULES["codex_counts_injected"] and (
                "HEARTING_RECEIPT_V1" in message
                or _utility("session_tidy").runtime_turn_source(message) in ("peer", "runtime")):
            return None
        return message
    if b'"user"' not in raw and b"queued_command" not in raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return _claude_counted(data)


def _claude_counted(data):
    """The text of a row Claude's transcript view stops at with `{`, else None (``SCROLL_RULES``)."""
    if not isinstance(data, dict):
        return None
    if data.get("type") == "attachment":
        attachment = data.get("attachment") if isinstance(data.get("attachment"), dict) else {}
        prompt = attachment.get("prompt")
        if (SCROLL_RULES["claude_counts_queued"] and attachment.get("type") == "queued_command"
                and isinstance(prompt, str) and not prompt.lstrip().startswith("<task-notification")):
            return prompt
        return None
    if (data.get("type") != "user" or data.get("isMeta") or data.get("isVisibleInTranscriptOnly")
            or data.get("isSidechain")):
        return None
    origin = data.get("origin")
    if SCROLL_RULES["claude_skips_peer_origin"] and isinstance(origin, dict):
        kind = origin.get("kind") or origin.get("type")
        if kind == "observer" or kind == "peer" and origin.get("senderTaskId"):
            return None
    content = (data.get("message") or {}).get("content") if isinstance(data.get("message"), dict) else None
    if isinstance(content, list):
        first = content[0] if content else None
        if not isinstance(first, dict) or first.get("type") != "text" or not isinstance(first.get("text"), str):
            return None
        text = first["text"]
    elif isinstance(content, str):
        text = content
    else:
        return None
    rest = text
    while True:
        stripped = _REMINDER.sub("", rest, count=1)
        if stripped == rest:
            break
        rest = stripped
    if rest.lstrip().startswith("<"):
        if SCROLL_RULES["claude_counts_paste_only"] and rest.lstrip().startswith("<pasted_content"):
            return text
        return None
    return text


def _opencode_passage(target, width, result):
    tt = _tidy_transcripts()
    sid = target["sid"]
    db = tt.locate_transcript("opencode", sid)
    if db is None:
        return {**result, "reason": "no-transcript"}
    unit = target.get("unit") or ""
    start, end = _cursor(target.get("from")), _cursor(target.get("to"))
    if target.get("row") == "prompt":
        found = _opencode_prompt(db, sid, target.get("at"), target.get("text") or "")
        if found is None:
            return {**result, "reason": "prompt-not-found"}
        unit, start = found["unit"], found["cursor"] - 1
        nxt = (_opencode_prompt(db, sid, target.get("until"), "", after=found["cursor"])
               if _number(target.get("until")) is not None else None)
        end = nxt["cursor"] - 1 if nxt else None
    elif start is None or end is None:
        return {**result, "reason": "no-cursor"}
    if end is not None and start > end:
        return {**result, "reason": "span-reversed", "span": {"from": start, "to": end, "unit": unit}}
    taken, cursor, page = [], start, 256
    while True:
        page_unit, part, error = tt._opencode_session(Path(db), sid, cursor, page)
        if error:
            return {**result, "reason": "unreadable", "detail": error}
        if unit and part and page_unit != unit:
            return {**result, "reason": "cursor-unit-changed"}
        stop = False
        for row in part:
            if end is not None and row[0] > end:
                stop = True
                break
            taken.append(row)
        if stop or len(part) < page:
            break
        cursor = part[-1][0]
    if end is None:
        end = taken[-1][0] if taken else start
    sizes = [tt._row_size(row) for row in taken]
    clipped = sum(sizes) > PASSAGE_HEAD + PASSAGE_TAIL
    groups = [taken]
    if clipped:
        head, used = [], 0
        for row, size in zip(taken, sizes):
            if used and used + size > PASSAGE_HEAD:
                break
            head.append(row)
            used += size
        tail, used = [], 0
        for row, size in zip(reversed(taken[len(head):]), reversed(sizes[len(head):])):
            if used and used + size > PASSAGE_TAIL:
                break
            tail.insert(0, row)
            used += size
        groups = [head, tail]
    marks = [mark for mark in target.get("marks") or () if isinstance(mark, dict)
             and mark.get("unit") == unit and _cursor(mark.get("cursor")) is not None]
    pieces = []
    for number, group in enumerate(groups):
        if number:
            pieces.append(("gap", None))
        segment = []
        for row in group:
            segment.append(row)
            hit = [mark for mark in marks if mark["cursor"] == row[0]]
            if hit:
                pieces.append(("text", _opencode_text(tt, db, segment)))
                pieces.extend(("mark", mark) for mark in hit)
                segment = []
        if segment:
            pieces.append(("text", _opencode_text(tt, db, segment)))
    return _draw(pieces, width, "opencode", {**result, "ok": True, "reason": "", "clipped": clipped,
                                             "span": {"from": start, "to": end, "unit": unit}})


def _opencode_text(tt, db, rows):
    chunk = tt.Chunk("opencode", str(db), 0, 0)
    tt._digest_opencode(rows, chunk)
    return chunk.text


def _opencode_messages(db, sid, *, page=200, pages=50):
    """User/assistant messages of one OpenCode session, newest first, through the shared reader
    (``opencode_runtime.read_session_parts``): ``{"cursor", "role", "at", "text", "parts"}``.
    A V1 message is its parts grouped by ``messageID``; its cursor is its first part's rowid."""
    runtime = _utility("opencode_runtime")
    before, generation, yielded = None, None, set()
    for _ in range(pages):
        snapshot = runtime.read_session_parts(sid, db_path=db, limit=page, newest=True, before_cursor=before)
        if snapshot.get("state") != "observed":
            return
        generation = generation or snapshot.get("generation")
        parts = snapshot.get("parts") or []
        if not parts:
            return
        if generation == "v2":
            for row in reversed(parts):
                data = row.get("data") if isinstance(row.get("data"), dict) else {}
                yield {"cursor": int(row["cursor"]), "role": row.get("role") or "", "unit": "seq",
                       "at": (row.get("updated") or 0) / 1000,
                       "text": data.get("text") if isinstance(data.get("text"), str) else "",
                       "synthetic": data.get("synthetic") is True, "parts": [data]}
        else:
            grouped = {}
            for row in parts:
                data = row.get("data") if isinstance(row.get("data"), dict) else {}
                key = data.get("messageID") or ("part", row["cursor"])
                entry = grouped.setdefault(key, {"cursor": int(row["cursor"]), "role": row.get("role") or "",
                                                 "unit": "rowid", "at": 0, "parts": []})
                entry["cursor"] = min(entry["cursor"], int(row["cursor"]))
                entry["at"] = max(entry["at"], (row.get("updated") or 0) / 1000)
                entry["parts"].append(data)
            for key, entry in sorted(grouped.items(), key=lambda item: -item[1]["cursor"]):
                if key in yielded:
                    continue                # the rest of a message the page before already gave
                yielded.add(key)
                texts = [part.get("text") for part in entry["parts"]
                         if part.get("type") == "text" and isinstance(part.get("text"), str)]
                entry["text"] = "\n".join(texts)
                entry["synthetic"] = bool(texts) and all(
                    part.get("synthetic") for part in entry["parts"] if part.get("type") == "text")
                yield entry
        before = min(int(row["cursor"]) for row in parts)
        if len(parts) < page:
            return


def _opencode_prompt(db, sid, at, text, *, after=None, window=600):
    """The user message a prompt row was written for: the nearest one in time whose first line is
    the row's text (``text`` empty: the first user message after cursor ``after``)."""
    if _number(at) is None:
        return None
    wanted, best = _match_key(text), None
    for message in _opencode_messages(db, sid):
        if message["at"] and message["at"] < at - window:
            break
        if message["role"] != "user" or (after is not None and message["cursor"] <= after):
            continue
        if wanted and _match_key(message["text"]) != wanted:
            continue
        if abs((message["at"] or at) - at) > window:
            continue
        if best is None or abs(message["at"] - at) <= abs(best["at"] - at):
            best = message
    return best


# ---------------------------------------------------------------------------------------------
# Steps a harness's own view needs to reach a prompt (computed only; no key is sent)
# ---------------------------------------------------------------------------------------------

def scroll_steps(story, index):
    """How far the harness's own conversation view must step to stand on a row's prompt.

    ``story`` is ``rows``' list and ``index`` the pressed row (a prompt, or a NOW whose prompt is
    used). Returns ``{"ok": True, "harness", "sid", "count", "steps", "texts"}``: ``count`` is the
    number of prompts the harness counts from that prompt to the newest, the prompt included;
    ``steps`` the keys its view takes (Claude: ``{`` presses from the bottom; Codex: ``←`` after its
    backtrack preview highlights the newest; OpenCode: ``↓`` in its timeline dialog, newest first);
    ``texts`` each counted prompt's first 20 cells, oldest first, to check on screen. When the
    move cannot happen it returns ``{"ok": False, "reason": …}`` with ``earlier-generation``,
    ``no-prompt``, ``invalid-target`` (the row's sid is not a session id), ``no-transcript``,
    ``prompt-not-found``, ``not-counted`` (the harness does not
    stop at this prompt) or ``compacted-reopened`` (Claude reopened after a compaction that came
    after the prompt, so its view starts below it). This never raises.
    """
    try:
        row = story[index]
    except (IndexError, TypeError, KeyError):
        return {"ok": False, "reason": "no-prompt"}
    if not isinstance(row, dict):
        return {"ok": False, "reason": "no-prompt"}
    if row.get("kind") == "now":
        key = row.get("prompt")
        row = next((item for item in story if isinstance(item, dict) and item.get("key") == key), None)
    if not isinstance(row, dict) or row.get("kind") != "prompt":
        return {"ok": False, "reason": "no-prompt"}
    if row.get("generation", 0) != 0:
        return {"ok": False, "reason": "earlier-generation"}
    harness, sid = row.get("harness"), row.get("sid")
    if harness in HARNESSES and not _safe(sid):
        return {"ok": False, "reason": "invalid-target", "harness": harness, "sid": sid}
    try:
        if harness == "opencode":
            found = _opencode_steps(sid, row)
        elif harness in ("claude", "codex"):
            found = _jsonl_steps(harness, sid, row, story)
        else:
            found = {"ok": False, "reason": "no-prompt"}
    except Exception as exc:  # noqa: BLE001
        found = {"ok": False, "reason": "unreadable", "detail": type(exc).__name__}
    return {**found, "harness": harness, "sid": sid}


def _cells(text, width=20):
    """The first ``width`` cells of a prompt's first line as the person sees it (runtime blocks
    in front of it left out, secrets masked)."""
    from .output import clip
    return clip(_first_line_plain(text), width)


def _jsonl_steps(harness, sid, row, story):
    if harness == "claude" and _compacted_reopened(story, row):
        return {"ok": False, "reason": "compacted-reopened"}
    path = _tidy_transcripts().locate_transcript(harness, sid)
    if path is None:
        return {"ok": False, "reason": "no-transcript"}
    start = (row.get("open") or {}).get("from")
    start = start if _cursor(start) is not None else _offset_at(path, row.get("at"))
    if start is None:
        return {"ok": False, "reason": "prompt-not-found"}
    found, matched = _prompt_offset(path, harness, start, row.get("text") or "")
    if not matched:
        seen, raw_match = _prompt_offset(path, harness, start, row.get("text") or "", counted=False)
        return {"ok": False, "reason": "not-counted" if raw_match else "prompt-not-found"}
    texts = [_cells(text) for text in _scan_user_texts(path, harness, found)]
    count = len(texts)
    return {"ok": True, "count": count, "steps": count if harness == "claude" else count - 1, "texts": texts}


_MARKERS = {"claude": (b'"type":"user"', b'"type": "user"', b"queued_command"),
            "codex": (b'"user_message"',)}


def _scan_user_texts(path, harness, start):
    """The counted prompt texts from ``start`` to the end of a JSONL file. Only lines holding a
    user row's marker are parsed (a quoted marker inside a string is escaped, so never matches),
    and a Claude tool result is passed over without parsing."""
    markers, texts = _MARKERS[harness], []
    with open(path, "rb") as handle:
        handle.seek(start)
        rest = b""
        while True:
            block = handle.read(SCAN_BLOCK)
            data = rest + block
            cut = data.rfind(b"\n") + 1 if block else len(data)
            data, rest = data[:cut], data[cut:]
            nexts, done = [data.find(marker) for marker in markers], 0
            while True:
                live = [at for at in nexts if at >= 0]
                if not live:
                    break
                at = min(live)
                which = nexts.index(at)
                if at < done:
                    nexts[which] = data.find(markers[which], done)
                    continue
                left = data.rfind(b"\n", 0, at) + 1
                right = data.find(b"\n", at)
                right = len(data) if right < 0 else right
                done = right
                nexts[which] = data.find(markers[which], done)
                if harness == "claude" and (data.find(b'"type":"tool_result"', left, right) >= 0
                                            or data.find(b'"type": "tool_result"', left, right) >= 0):
                    continue
                text = _user_text(harness, data[left:right])
                if text is not None:
                    texts.append(text)
            if not block:
                return texts


def _compacted_reopened(story, row):
    """A compaction after the prompt followed by a reopening of the same session (Claude's view then
    starts at its compaction line), as this session's ledger recorded it."""
    try:
        tidy = _utility("session_tidy")
        ledger = tidy._read_ledger_lines(_seat(tidy, row["harness"], row["sid"]), include_physical_relations=False)
    except Exception:
        return False
    compact = None
    for item in ledger:
        if not isinstance(item, dict) or (item.get("harness"), item.get("sid")) != (row["harness"], row["sid"]):
            continue
        at = _number(item.get("ts"))
        if at is None or at <= row["at"]:
            continue
        if item.get("event") == "compact" or item.get("event") == "start" and item.get("source") == "compact":
            compact = at if compact is None else min(compact, at)
        elif (compact is not None and item.get("event") == "start"
              and item.get("source") in ("resume", "startup") and at > compact + DIVIDER_MERGE_SEC):
            return True
    return False


def _opencode_counted(message):
    if message["role"] != "user":
        return False
    if message.get("synthetic"):
        return False
    text = message.get("text") or ""
    try:
        source = _utility("session_tidy").runtime_turn_source(text)
    except Exception:
        source = ""
    if source == "notification":
        return False
    if not SCROLL_RULES["opencode_counts_injected"] and source in ("peer", "runtime"):
        return False
    return True


def _opencode_steps(sid, row):
    db = _tidy_transcripts().locate_transcript("opencode", sid)
    if db is None:
        return {"ok": False, "reason": "no-transcript"}
    target = _opencode_prompt(db, sid, row.get("at"), row.get("text") or "")
    if target is None:
        return {"ok": False, "reason": "prompt-not-found"}
    if not _opencode_counted(target):
        return {"ok": False, "reason": "not-counted"}
    newer = []
    for message in _opencode_messages(db, sid):
        if message["cursor"] <= target["cursor"]:
            break
        if _opencode_counted(message):
            newer.append(_cells(message["text"]))
    texts = [_cells(target["text"])] + list(reversed(newer))
    return {"ok": True, "count": len(texts), "steps": len(texts) - 1, "texts": texts}
