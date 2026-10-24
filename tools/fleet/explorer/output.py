"""Raw output of the selected request: bounded formatting and the single-reader state behind it.

The native records are opened, cut and masked by the shared ``utilities/attempt_view.py``
(``resolve_sources`` / ``read_entries`` / ``format_stream``); nothing here parses a transcript. This
module turns the shared ``Line`` values into one physical row each (``OutputLine``), keeps the rows
of the scope being read, and reports the health of every source. ``OutputReader`` is used from the
selection pump's worker only; ``render_output`` and the row helpers are pure and run on the curses
thread.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys
import time
import unicodedata
from typing import Iterable, Optional, Sequence, Tuple

from .types import Health

TAIL_BYTES = 64 * 1024
TAIL_RECORDS = 200
MAX_STAGE_SOURCES = 6              # a request without a chosen stage mixes at most this many stage logs
MAX_ROWS = 5000                    # rows kept for the scope being read (the oldest leave first)
RESOLVE_RETRY_SEC = 5.0            # a source that could not be opened is looked for again this often
SUPERVISOR_ONLY = "감독 기록만 남음 · 원천 정리됨"
RESET_NOTE = "원천이 잘리거나 교체되어 처음부터 다시 읽음"
RESET_NOTICE_SEC = 30.0
CLEANED = "원천 정리됨"


@dataclass(frozen=True)
class OutputLine:
    text: str
    omitted_lines: int = 0


@dataclass(frozen=True)
class OutputEvent:
    """One formatted source event, retaining its complete (possibly multiline) body."""
    kind: str
    text: str
    label: str = ""
    abbr: str = ""
    stamp: str = ""


def cell_width(text: str) -> int:
    width = 0
    for char in text:
        code = ord(char)
        if (unicodedata.combining(char) or code in (0xFE0E, 0xFE0F, 0x200B, 0x200C, 0x200D, 0x2060)):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def clip(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    available = max(0, width - cell_width(ellipsis))
    out = []
    used = 0
    for char in text:
        cells = cell_width(char)
        if used + cells > available:
            break
        out.append(char)
        used += cells
    return "".join(out) + (ellipsis if width >= cell_width(ellipsis) else "")


def fit_row(text: str, omitted: int, width: int) -> str:
    """One physical row: the first line, ``(+k줄)`` for the lines left out, a right ``…`` when it does not fit.

    The marker is always kept: the first line is cut to leave room for it, then ``…`` follows it.
    """
    text = text.replace("\n", " ")
    if not omitted:
        return clip(text, width)
    marker = f" (+{omitted}줄)"
    if cell_width(text) + cell_width(marker) <= width:
        return text + marker
    # One cell stays free after the ``…``: in a terminal that draws it two cells wide it still fits.
    room = width - cell_width(marker) - cell_width("…") - 1
    if room <= 0:
        return clip(text + marker, width)
    out, used = [], 0
    for char in text:
        cells = cell_width(char)
        if used + cells > room:
            break
        out.append(char)
        used += cells
    return "".join(out) + marker + "…"


def _record(record):
    if isinstance(record, OutputLine):
        return record.text, record.omitted_lines
    if isinstance(record, str):
        return record, 0
    return str(getattr(record, "text", record)), int(getattr(record, "omitted_lines", 0) or 0)


def render_output(records: Iterable[object], width: int, height: int, *, scroll: Optional[int] = 0) -> Tuple[str, ...]:
    """Records as one physical row each, then a viewport. ``scroll`` is the first row shown; ``None``
    (never scrolled) follows the newest rows."""
    rows = [fit_row(*_record(record), width) for record in records]
    height = max(0, height)
    last = max(0, len(rows) - height)
    start = last if scroll is None else max(0, min(int(scroll), last))
    return tuple(rows[start:start + height])


def attempt_summary_lines(job):
    """Keep the existing dispatch summary fields available in PR-1 output."""
    def value(name):
        return job.get(name) if isinstance(job, dict) else getattr(job, name, None)
    fields = []
    for name, label in (("title", "작업"), ("summary", "요약"), ("stage", "단계"),
                        ("status", "상태"), ("liveness", "생존"),
                        ("summary_ts", "요약 시각")):
        item = value(name)
        if item not in (None, ""):
            fields.append(f"{label}: {item}")
    decision = value("attempt_decision")
    if isinstance(decision, dict):
        for key, label in (("outcome", "결과"), ("action", "다음"), ("responsible", "담당"), ("reason", "근거")):
            item = decision.get(key)
            if item:
                fields.append(f"{label}: {item}")
    return tuple(fields)


# -- native records -> rows -----------------------------------------------------------------------

_UTILITIES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))), "utilities")


def utility(name):
    """A shared ``utilities/`` module (imported on first use; the curses thread never calls this)."""
    if _UTILITIES not in sys.path:
        sys.path.insert(0, _UTILITIES)
    import importlib
    return importlib.import_module(name)


def attempt_view():
    """The shared viewer module (the one place that opens, cuts, formats and masks native records)."""
    return utility("attempt_view")


_STRIP = {
    "message": ("assistant: ", "text: ", "message: ", "agent: "),
    "prompt": ("user: ", "userMessage: ", "text: "),
    "output": ("tool output: ", "output: "),
    "thinking": ("thinking: ", "reasoning: "),
    "notice": ("notice: ",),
    "attachment": (),
}
_GLYPH = {"message": "»", "prompt": "«", "output": ">", "thinking": "~", "notice": "!",
          "attachment": "=", "unknown": "", "raw": ""}
_SHELLS = ("bash", "shell", "sh", "zsh")
_TOOL_HEAD = re.compile(r"tool (\S+):(?:\n(.*))?\Z", re.S)
_EXIT = re.compile(r"exit: (\S+)")


def _without(text, labels):
    for label in labels:
        if text.startswith(label):
            return text[len(label):]
    return text


def line_row(line, label, abbr, stamp="") -> Optional[OutputLine]:
    """One shared ``Line`` as a row: ``[label] CX $ command`` / ``> output`` / ``exit 0`` / ``» message``."""
    kind, text = line.kind, str(line.text)
    if kind == "supervisor":
        return None                                # lifecycle bookkeeping, not output
    head = f"{stamp}[{label}] {abbr} "
    if kind == "result":
        exit_code = _EXIT.fullmatch(text.strip())
        if exit_code:
            return OutputLine(f"{head}exit {exit_code.group(1)}")
        glyph, body = "=", text
    elif kind == "tool":
        shell = _TOOL_HEAD.fullmatch(text)
        if text.startswith("command: "):
            glyph, body = "$", text[len("command: "):]
        elif shell and shell.group(1).casefold() in _SHELLS and shell.group(2):
            glyph, body = "$", shell.group(2)
        elif shell:
            glyph, body = "◆", shell.group(1) + (" " + shell.group(2) if shell.group(2) else "")
        else:
            glyph, body = "·", text
    else:
        glyph, body = _GLYPH.get(kind, "·"), _without(text, _STRIP.get(kind, ()))
    lines = body.splitlines() or [""]
    omitted = len(lines) - 1
    # Tool output is a compact native block; keep its continuation count on the
    # event row while retaining the full body in OutputEvent for the transcript.
    if kind == "output" and omitted:
        return OutputLine(head + (glyph + " " if glyph else "") +
                          lines[0] + f" (+{omitted}줄)", 0)
    return OutputLine(head + (glyph + " " if glyph else "") + lines[0], omitted)


def record_time(record) -> Optional[float]:
    """Epoch seconds of a native file record (``timestamp`` on the record or its item), else None."""
    try:
        data = json.loads(record) if isinstance(record, str) else record
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    item = data.get("item") if isinstance(data.get("item"), dict) else {}
    stamp = data.get("timestamp") or item.get("timestamp")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return float(stamp) / (1000.0 if stamp > 1e11 else 1.0)
    if isinstance(stamp, str):
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def clock_text(epoch, tz=None) -> str:
    return datetime.fromtimestamp(epoch, tz).strftime("%H:%M:%S ")


# -- the sources of one scope ---------------------------------------------------------------------

@dataclass(frozen=True)
class SourcePlan:
    """One attempt whose output is read: ``label`` is the stage node (or ``owner``)."""
    attempt_id: str
    label: str
    harness: str
    abbr: str
    role: str = "stage"                          # owner | stage | generation
    log_file: str = ""
    meta: Tuple[Tuple[str, str], ...] = ()
    direct: str = ""                             # an attempt log path to read as it is (no registry lookup)


@dataclass(frozen=True)
class OutputView:
    """What the pump hands to the screen for one scope."""
    title: str = ""
    rows: Tuple[OutputLine, ...] = ()
    notes: Tuple[str, ...] = ()
    health: Health = Health()
    more: bool = False                           # records exist before the oldest row shown
    events: Tuple[OutputEvent, ...] = ()


class _Src:
    def __init__(self, plan, source):
        self.plan, self.source = plan, source
        self.cursor = attempt_view().Cursor(source.path)
        self.format_state = {}
        self.rows = {}                           # entry key -> ((time, key), [OutputLine, ...])
        self.events = {}                         # entry key -> ((time, key), [OutputEvent, ...])
        self.last_time = 0.0
        self.state, self.reason = "ok", ""
        self.reset_at = None                     # when the source was last found truncated or replaced

    @property
    def identity(self):
        return (str(self.source.path), self.source.kind, self.source.session_id)

    @property
    def first(self):
        if self.source.kind == "opencode-db":
            return not self.cursor.reader_state.get("tailed")
        return not self.cursor.tailed


class _Slot:
    """The sources of one plan item: resolved once, looked for again while a native one may still appear."""

    def __init__(self, plan):
        self.plan = plan
        self.sources = []
        self.state, self.reason = "pending", ""
        self.tried = None
        self.supervisor_only = False


class OutputScope:
    def __init__(self):
        self.slots = {}                          # attempt_id -> _Slot
        self.last_success = None


class OutputReader:
    """Reads the selected scope's sources with the shared reader and keeps their rows.

    Called from the selection pump's worker only (single flight); a scope read before is continued
    from its cursors, so each pass costs one bounded read per source. ``resolver`` replaces
    ``attempt_view.resolve_sources`` (tests); ``fallback_home`` is the registry home used when an
    attempt cannot be found by its registry row but that row names a log path.
    """

    SCOPE_LIMIT = 4

    def __init__(self, resolver=None, fallback_home=None, tz=None, clock=time.time, monotonic=time.monotonic):
        self.resolver = resolver
        self.fallback_home = fallback_home
        self.tz = tz
        self.clock = clock
        self.monotonic = monotonic
        self.scopes = OrderedDict()
        self.resolved = 0                        # resolve_sources calls (a hidden tab must not add to it)
        self.reads = 0                           # read_entries / read_earlier calls

    # -- resolving ------------------------------------------------------------------------------
    def _resolve(self, plan):
        module = attempt_view()
        if plan.direct:
            return [module.Source(module.Path(plan.direct), plan.harness, "log")], "ok", ""
        resolve = self.resolver or module.resolve_sources
        self.resolved += 1
        try:
            sources, error = resolve(plan.attempt_id)
        except Exception as exc:
            return [], "unreadable", type(exc).__name__
        if error and not sources:
            if "not found" in error and plan.log_file:
                fallback = self._fallback_source(plan)
                if fallback is not None:
                    return [fallback], "ok", ""
            return [], ("missing" if "missing" in error else "unreadable"), error
        return list(sources), "ok", ""

    def _fallback_source(self, plan):
        from types import SimpleNamespace
        module = attempt_view()
        meta = dict(plan.meta)
        job = SimpleNamespace(_log_file=plan.log_file, attempt_id=plan.attempt_id, harness=plan.harness,
                              _registry_path=meta.get("registry_path"), artifact_root=meta.get("artifact_root"),
                              _launch_home=meta.get("launch_home"))
        path = module.owned_attempt_log_path(job, fallback_home=self.fallback_home)
        return module.Source(module.Path(path), plan.harness, "log") if path else None

    def _open(self, slot, now):
        """Resolve the slot's sources; a Claude/OpenCode attempt that has only its supervisor log is
        looked for again (its native session may be announced later)."""
        wants_native = (slot.plan.harness in ("claude", "opencode") and slot.sources
                        and all(src.source.kind == "log" for src in slot.sources))
        wait = RESOLVE_RETRY_SEC * (6 if slot.supervisor_only else 1)
        if slot.tried is not None and (now - slot.tried < wait or (slot.sources and not wants_native)):
            return
        slot.tried = now
        sources, state, reason = self._resolve(slot.plan)
        known = {src.identity: src for src in slot.sources}
        slot.sources = [known.get((str(item.path), item.kind, item.session_id)) or _Src(slot.plan, item)
                        for item in sources]
        slot.state, slot.reason = state, reason
        slot.supervisor_only = False
        if (len(slot.sources) == 1 and slot.plan.harness in ("claude", "opencode")
                and slot.sources[0].source.kind == "log"):
            try:
                module = attempt_view()
                slot.supervisor_only = bool(module.announced_sessions(slot.sources[0].source.path,
                                                                     slot.plan.attempt_id))
            except Exception:
                slot.supervisor_only = False

    # -- reading --------------------------------------------------------------------------------
    def _absorb(self, src, entries, stamped, older=False):
        module = attempt_view()
        previous = src.last_time
        first_known = next((t for t in (record_time(e.record) for e in entries) if t), 0.0)
        for entry in entries:
            stamp = record_time(entry.record)
            if stamp is None:
                stamp = first_known if older else previous
            if not older and stamp:
                previous = stamp
            lines = module.format_stream(src.source.harness, [entry.record], src.format_state)
            prefix = clock_text(stamp, self.tz) if stamped and stamp else ""
            src.events[entry.key] = ((stamp, entry.key), [
                OutputEvent(line.kind, str(line.text), src.plan.label, src.plan.abbr, prefix)
                for line in lines if line.kind != "supervisor"
            ])
            rows = [row for row in (line_row(line, src.plan.label, src.plan.abbr, prefix) for line in lines)
                    if row is not None]
            src.rows[entry.key] = ((stamp, entry.key), rows)
        if not older:
            src.last_time = previous

    def _read_src(self, src, *, earlier, stamped):
        module = attempt_view()
        self.reads += 1
        if earlier:
            result = module.read_earlier(src.source, src.cursor, max_bytes=TAIL_BYTES, max_records=TAIL_RECORDS)
        else:
            result = module.read_entries(src.source, src.cursor, max_bytes=TAIL_BYTES,
                                         max_records=TAIL_RECORDS, tail=src.first)
        if result.state != "ok":
            src.state, src.reason = result.state, result.reason
            return False
        src.state, src.reason = "ok", ""
        if result.reset:
            src.rows.clear()
            src.events.clear()
            src.format_state.clear()
            src.reset_at = self.clock()
        self._absorb(src, result.entries, stamped, older=earlier)
        return True

    # -- the scope ------------------------------------------------------------------------------
    def read(self, scope_key, title, plans, *, stamped=False, earlier=False, now=None) -> OutputView:
        """Read (and, when ``earlier``, first read further back) the sources of ``plans``; return the view."""
        now = self.monotonic() if now is None else now
        scope = self.scopes.get(scope_key)
        if scope is None:
            scope = self.scopes[scope_key] = OutputScope()
        self.scopes.move_to_end(scope_key)
        while len(self.scopes) > self.SCOPE_LIMIT:
            self.scopes.popitem(last=False)
        wanted = {plan.attempt_id for plan in plans}
        for gone in [key for key in scope.slots if key not in wanted]:
            del scope.slots[gone]
        problems = []
        for plan in plans:
            slot = scope.slots.setdefault(plan.attempt_id, _Slot(plan))
            self._open(slot, now)
            if not slot.sources:
                problems.append((plan.label, slot.state, slot.reason))
                continue
            for src in slot.sources:
                if earlier and not src.first:
                    self._read_src(src, earlier=True, stamped=stamped)
                if not self._read_src(src, earlier=False, stamped=stamped):
                    problems.append((plan.label, src.state, src.reason))
            if slot.supervisor_only:
                problems.append((plan.label, "cleaned", SUPERVISOR_ONLY))
            if any(src.reset_at is not None and self.clock() - src.reset_at < RESET_NOTICE_SEC for src in slot.sources):
                problems.append((plan.label, "reset", RESET_NOTE))
        return self._view(scope, title, plans, problems)

    def _view(self, scope, title, plans, problems):
        module = attempt_view()
        held, held_events, more = [], [], False
        for order, plan in enumerate(plans):
            slot = scope.slots.get(plan.attempt_id)
            for number, src in enumerate(slot.sources if slot else ()):
                for (stamp, key), rows in src.rows.values():
                    for index, row in enumerate(rows):
                        held.append(((stamp or 0.0, order, number, key, index), row))
                for (stamp, key), events in src.events.values():
                    for index, event in enumerate(events):
                        held_events.append(((stamp or 0.0, order, number, key, index), event))
                more = more or module.has_earlier(src.source, src.cursor)
        held.sort(key=lambda item: item[0])
        held_events.sort(key=lambda item: item[0])
        rows = tuple(row for _, row in held)[-MAX_ROWS:]
        events = tuple(event for _, event in held_events)[-MAX_ROWS:]
        unreadable = [item for item in problems if item[1] == "unreadable"]
        if not unreadable:
            scope.last_success = self.clock()
        notes = []
        for label, state, reason in problems:
            text = SUPERVISOR_ONLY if state == "cleaned" else RESET_NOTE if state == "reset" else (
                CLEANED if state == "missing" else (f"읽지 못함 · {reason}" if reason else "읽지 못함"))
            notes.append(f"[{label}] {text}" if len(plans) > 1 else text)
        if unreadable:
            progressed = len(unreadable) < sum(len(scope.slots[p.attempt_id].sources) or 1
                                               for p in plans if p.attempt_id in scope.slots)
            health = Health("partial" if progressed else ("stale" if scope.last_success else "failed"),
                            scope.last_success, unreadable[0][2] or None)
        else:
            health = Health("success", scope.last_success)
        shown = tuple(notes[:3]) + ((f"+{len(notes) - 3}",) if len(notes) > 3 else ())
        return OutputView(title, rows, shown, health, more, events)


# -- an earlier generation of the focused main session ----------------------------------------------

_DIALOGUE = re.compile(r"\[([A-Za-z]+)\] ")
_ROLE_GLYPH = {"user": "«", "assistant": "»"}


def _dialogue_rows(text, label, abbr):
    """Rows of ``tidy_transcripts`` dialogue text: ``[role] body`` starts a record, other lines continue it."""
    records = []
    for line in text.split("\n"):
        match = _DIALOGUE.match(line)
        if match:
            records.append([match.group(1), [line[match.end():]]])
        elif records:
            records[-1][1].append(line)
        elif line.strip():
            records.append(["", [line]])
    rows = []
    for role, lines in records:
        glyph = _ROLE_GLYPH.get(role, "·")
        rows.append(OutputLine(f"[{label}] {abbr} {glyph} {lines[0]}", len(lines) - 1))
    return rows


def _is_attempt_log(path, harness):
    base = os.path.basename(path)
    return ".att-" in base and base.endswith(f".{harness}.jsonl")


def _generation_state(reader, key):
    return reader.scopes.setdefault(("generation-cache",), OrderedDict())


def _read_transcript(harness, path, sid, label, abbr):
    """The last 64KiB of a session transcript through ``tidy_transcripts.read_chunk`` (whole rows only)."""
    module = utility("tidy_transcripts")
    if harness == "opencode":
        cursor, texts = 0, []
        for _ in range(64):
            chunk = module.read_chunk(harness, path, cursor, sid=sid, limit_bytes=TAIL_BYTES)
            if chunk.error:
                return None, chunk.error
            if chunk.text:
                texts.append(chunk.text)
            texts = texts[-2:]
            if chunk.eof or chunk.cursor_to <= cursor:
                break
            cursor = chunk.cursor_to
        text = "\n".join(texts)
    else:
        size = os.stat(path).st_size
        chunk = module.read_chunk(harness, path, max(0, size - TAIL_BYTES), sid=sid, limit_bytes=TAIL_BYTES)
        if chunk.error:
            return None, chunk.error
        text = chunk.text
    return _dialogue_rows(text, label, abbr)[-TAIL_RECORDS:], ""


def read_generation(reader, entry, now=None) -> OutputView:
    """The raw-output view of one earlier generation (``entry`` is a lineage entry)."""
    harness, sid = str(entry.get("harness") or ""), str(entry.get("sid") or "")
    abbr = _abbr(harness)
    label = "이전 " + sid[:8]
    title = f"이전 대화 · {abbr} {sid[:8]} · 원래 출력"
    where = entry.get("transcript") or {}
    path, status = where.get("path"), where.get("status")
    if not path or status == "missing":
        return OutputView(title, (), (CLEANED,), Health("success", reader.clock()), False)
    if status != "available":
        return OutputView(title, (), (f"읽지 못함 · {status or '원문 위치 확인 불가'}",),
                          Health("failed", None, status or "unknown"), False)
    if _is_attempt_log(path, harness):
        plan = SourcePlan(f"generation:{harness}:{sid}", label, harness, abbr, "generation", direct=path)
        return reader.read(("generation", harness, sid), title, [plan], now=now)
    try:
        info = os.stat(path)
        stamp = (path, info.st_mtime_ns, info.st_size)
        cache = reader.scopes.setdefault(("generation-cache",), OrderedDict())
        hit = cache.get((harness, sid))
        if hit is not None and hit[0] == stamp:
            return hit[1]
        rows, error = _read_transcript(harness, path, sid, label, abbr)
    except OSError as exc:
        rows, error = None, type(exc).__name__
    if rows is None:
        return OutputView(title, (), (f"읽지 못함 · {error}",), Health("failed", None, error), False)
    view = OutputView(title, tuple(rows), (), Health("success", reader.clock()), False)
    cache[(harness, sid)] = (stamp, view)
    while len(cache) > 4:
        cache.popitem(last=False)
    return view


def _abbr(harness):
    value = str(harness or "").lower()
    return {"claude": "CL", "codex": "CX", "opencode": "OC"}.get(
        value, "?" if not harness else value[:2].upper())
