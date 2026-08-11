"""F-50 (v33) — openai-codex plugin-queue jobs as a read-only additive fleet surface.

The Claude Code `openai-codex` plugin keeps its own background queue under
``$CLAUDE_CONFIG_DIR|~/.claude/plugins/data/codex-openai-codex/state/<workspace>/``:

    state.json    {"version": 1, "config": {...}, "jobs": [ ... ]}   ← authoritative
    broker.json   {"endpoint", "pidFile", "logFile", "sessionDir", "pid"}
    jobs/<id>.json / <id>.log                                        ← enrichment only

A job that the plugin detaches after a foreground timeout is registered NOWHERE in
jobs.log, and its executor (`codex app-server`) is hidden as a companion by F-24. So the
plugin queue is the only place those jobs exist — this collector is their single visible
surface (F-50c), not a second identity for a jobs.log attempt.

Read-only observer invariant (PRD §0.5): nothing here writes to the plugin's state, and no
row carries the (pid, proc_start) identity F-27 needs to signal — a third-party queue's
worker is not fleet's to kill. The observed pid stays visible as evidence instead.
"""
import glob
import json
import os
import time
from datetime import datetime, timezone

from .. import model
from ..model import ContextEvidence, DispatchJob
from ..token_budget import parse_codex_token_count
from . import codex
from .dispatch import DONE_AFTERGLOW_MIN

# The plugin's own layout, relative to the Claude config home.
_STATE_GLOB = os.path.join("plugins", "data", "codex-openai-codex", "state", "*", "state.json")
_SCHEMA_VERSION = 1

# F-50a minimal required set. `workspaceRoot` and `request.cwd` are interchangeable (only
# ~7% of observed records carry a `request` block at all), so one of the two suffices.
_REQUIRED = ("id", "status", "createdAt")

# Terminal words. `completed` reuses the F-46 afterglow window verbatim; the failure class
# keeps the existing dead/killed path but is held for the SAME window before it is dropped —
# a jobs.log failure can afford an immediate drop because a proc row still shows the work,
# while a plugin-queue row has no second surface at all (F-50c).
_TERMINAL = ("completed", "failed", "cancelled")

# comm allowlist for pid verification. The plugin runs both the per-job worker and the
# broker as node scripts; anything else at that pid is a reused pid, not our process.
_PLUGIN_COMMS = ("node",)

# Name-zone cap before render's own clipping — a summary is a paragraph, not a title.
_TITLE_MAX = 120

# `_row` outcome for a terminal record that has aged past the retention window.
_EXPIRED = "expired"


def _home():
    """Claude Code config home — same resolution the claude collector uses."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _iso_epoch(ts):
    """ISO8601 (the plugin writes `...Z`) → epoch seconds; None when unparseable."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _age_min(epoch, now):
    if epoch is None:
        return None
    return max(0, int((now - epoch) // 60))


def _first_iso(record, *names):
    for name in names:
        epoch = _iso_epoch(record.get(name))
        if epoch is not None:
            return epoch
    return None


def _pid_alive(pid, job_id=None):
    """True when `pid` is a live plugin process (and, with `job_id`, that exact job's worker).

    Two-step so a recycled pid can never resurrect a finished job: /proc existence, then
    comm in the plugin's allowlist, then — when the caller supplies a job id — the
    `--job-id <id>` argument the companion's task-worker is launched with.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not os.path.exists("/proc/%d" % pid):
        return False
    try:
        with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as f:
            comm = f.read().strip()
    except OSError:
        return False
    if comm not in _PLUGIN_COMMS:
        return False
    if job_id is None:
        return True
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return False
    return job_id in argv


def _broker_pid(state_dir):
    """The queue broker's pid from broker.json — enrichment, so any failure is just None."""
    try:
        with open(os.path.join(state_dir, "broker.json"), encoding="utf-8", errors="replace") as f:
            broker = json.load(f)
    except (OSError, ValueError):
        return None
    pid = broker.get("pid") if isinstance(broker, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _title(record):
    """F-50d — the plugin's own `summary` head as the name-zone identity.

    The prompt body is never rendered; `summary` is what the plugin already distilled for
    display. Collapsed to one line and cut from the TAIL so the head survives (F-9).
    """
    for name in ("summary", "title"):
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            head = " ".join(value.split())
            return head[:_TITLE_MAX] if len(head) > _TITLE_MAX else head
    return None


def _public_record(record):
    """The observed record verbatim (F-50d `--json`), minus the prompt body (privacy)."""
    public = dict(record)
    request = public.get("request")
    if isinstance(request, dict) and "prompt" in request:
        request = dict(request)
        request.pop("prompt")
        request["prompt_omitted"] = True
        public["request"] = request
    return public


def _rollout_for_thread(thread_id, home=None):
    """F-50f — the Codex rollout whose filename sid EXACTLY equals `threadId`, else None.

    Exact-1 or nothing: zero matches, two or more matches, or an id that is not a rollout sid
    at all all leave the telemetry an honest gap. The filename grammar keeps its single owner
    (`codex._sid`) — this join never re-implements it, and glob metacharacters cannot reach
    the pattern because the id has to parse as a sid first.

    Only the two layouts Codex actually writes are searched (`sessions/YYYY/MM/DD/` and a flat
    `sessions/`); a recursive walk of every rollout would cost a full tree scan per tick for a
    lookup that is already exact.
    """
    return codex.exact_rollout_for_session_id(
        thread_id, homes=[home or codex._home()])


def _telemetry(record, home=None):
    """F-50f — (telemetry dict, ContextEvidence) for the joined rollout, or (None, None).

    Display-layer additive ONLY: this runs after `classify_job` and feeds nothing back into
    the F-50b lifecycle judgement — a job's state is decided by the plugin's own status word
    and the pid evidence, never by how full its context window is.
    """
    path = _rollout_for_thread(record.get("threadId"), home=home)
    if not path:
        return None, None
    line = codex._tail_token_count(path)     # bounded 64 KB tail, same as session enrichment
    if not line:
        return None, None
    tel = parse_codex_token_count(line, session_id=record.get("threadId"))
    if tel.context_used_pct is None and tel.active_context_tokens is None:
        return None, None                    # parsed but empty → still an honest gap
    payload = {
        "source": "codex-rollout",
        "thread_id": record.get("threadId"),
        "rollout": path,
        "context_used_pct": tel.context_used_pct,
        "active_context_tokens": tel.active_context_tokens,
        "context_window_tokens": tel.context_window_tokens,
        "session_total_tokens": tel.session_total_tokens,
    }
    evidence = None
    if tel.context_used_pct is not None:
        try:
            st = os.stat(path)
        except OSError:
            return payload, None
        sequence = (st.st_mtime_ns, st.st_size)
        evidence = ContextEvidence(
            used_pct=tel.context_used_pct, source="codex-rollout",
            sequence=sequence, source_head_sequence=sequence,
            observed_at=st.st_mtime, fresh_until=st.st_mtime + 900)
    return payload, evidence


def _row(record, state_dir, now, codex_home=None):
    """One record → DispatchJob, `_EXPIRED` when it is finished history, None when malformed.

    The three outcomes are kept distinct on purpose: a terminal row that has aged out of the
    retention window is the NORMAL steady state (the queue keeps every job it ever ran
    forever), and counting it as malformed would make the dim skip signal meaningless.
    """
    if not isinstance(record, dict):
        return None
    if any(not record.get(name) for name in _REQUIRED):
        return None
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    cwd = request.get("cwd") or record.get("workspaceRoot") or ""
    if not cwd:
        return None
    job_id = str(record["id"])
    status = record["status"]

    started = _first_iso(record, "startedAt", "createdAt")
    if status in _TERMINAL:
        # Terminal rows measure elapsed from COMPLETION — the F-46 display contract
        # ("✓ done <since it finished>"), and the same clock the retention window uses.
        finished = _first_iso(record, "completedAt", "updatedAt") or started
        age_min = _age_min(finished, now)
        if age_min is None or age_min > DONE_AFTERGLOW_MIN:
            return _EXPIRED                  # past the window → the row self-clears
        elapsed_min = age_min
    else:
        elapsed_min = _age_min(started, now)

    parent_sid = record.get("sessionId") or None
    job = DispatchJob(
        key=record.get("kindLabel") or "codex-task",
        slug=job_id,
        cwd=cwd,
        elapsed_min=elapsed_min,
        status=status,                       # verbatim plugin word; fleet never rewrites it
        source="plugin-queue",
        surface_kind="plugin-agent",        # F-73: not a registered dispatch identity
        harness="codex",
        # F-50c: `sessionId` is the spawning CLAUDE session. It only ever nests on an exact
        # session-id match; `parent_cwd` is recorded as observed metadata, never as a
        # second, weaker attribution path (see render's plugin-queue guard).
        parent_sid=parent_sid,
        parent_cwd=record.get("workspaceRoot") or None,
        is_child=bool(parent_sid),
        title=_title(record),
        afterglow=(status == "completed"),
        plugin_job=_public_record(record),
    )

    pid = record.get("pid")
    pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
    verified = None
    if status == "running":
        if _pid_alive(pid, job_id=job_id):
            verified = "job"
        elif _pid_alive(_broker_pid(state_dir)):
            verified = "broker"
    ev_in = {
        "source": "plugin-queue",
        "key": job.key,
        "harness": "codex",
        "status": status,
        "elapsed_min": elapsed_min,
        "slug": job_id,
        "plugin_pid": pid,                   # evidence only — never job.pid (no kill target)
        "broker_pid": _broker_pid(state_dir) if status == "running" else None,
        "pid_verified": verified,
        "phase": record.get("phase"),
    }
    job.liveness, job.state_evidence = model.classify_job(
        ev_in, now, key=("j", "plugin-queue:" + job_id))
    # F-50f — additive display telemetry, attached AFTER the state verdict is settled so the
    # join can never move a row between states.
    job.plugin_telemetry, job._context_evidence = _telemetry(record, home=codex_home)
    return job


def _scan_state_file(path, now, codex_home=None):
    """(rows, malformed) for one state.json. Tolerant: a broken file is a skip, not a raise."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return [], 1
    if not isinstance(state, dict) or state.get("version") != _SCHEMA_VERSION:
        # Schema guard (F-50a): an unknown layout is skipped whole — reading it would be
        # inventing meaning for fields we have never observed.
        return [], 1
    records = state.get("jobs")
    if not isinstance(records, list):
        return [], 1
    state_dir = os.path.dirname(path)
    rows = []
    malformed = 0
    for record in records:
        try:
            job = _row(record, state_dir, now, codex_home=codex_home)
        except Exception:
            job = None                       # a single odd record is a skip, never a raise
        if job is _EXPIRED:
            continue                         # finished history, not a defect
        if job is None:
            malformed += 1
            continue
        rows.append(job)
    return rows, malformed


def collect(home=None, now=None, codex_home=None):
    """Return [DispatchJob] for every live/afterglow plugin-queue job.

    `collect.last_malformed` mirrors the jobs.log idiom: skipped files + skipped records,
    surfaced as a dim signal rather than a hidden failure.
    """
    now = time.time() if now is None else now
    root = home or _home()
    jobs = []
    malformed = 0
    for path in sorted(glob.glob(os.path.join(root, _STATE_GLOB))):
        rows, skipped = _scan_state_file(path, now, codex_home=codex_home)
        jobs.extend(rows)
        malformed += skipped
    collect.last_malformed = malformed
    return jobs


collect.last_malformed = 0
