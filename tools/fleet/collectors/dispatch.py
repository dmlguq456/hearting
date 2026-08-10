"""Dispatch section — per-project headless jobs, uncapped (PRD §4B, §6).

Two sources, merged:
  (a) process scan: Claude autopilot-*/loops jobs — the statusline job-scan logic ported
      verbatim EXCEPT the top-3 cap and the per-session related() cwd filter are removed
      (this is a global monitor, not a per-session statusline).
  (b) dispatch registries: the current neutral <agent-home> registry plus legacy
      ~/.claude/.dispatch/jobs.log when different. status ∈ {open, running} accepted;
      rows that are malformed (field count ≠ 6) are skipped and counted, never crash
      the reader.

codex/opencode headless dispatch appears ONLY via jobs.log (their argv has no /autopilot-,
01_tap §4d), so jobs.log rows not already covered by a live process are surfaced here.

live_stage() derives the real pipeline stage from plans/*_<slug>/ artifacts (ported from
statusline.sh:131-171) so the label reflects live progress, not the static argv.
"""
import json
import glob
import os
import re
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "core" / "CORE.md").is_file()
    and (parent / "utilities" / "dispatch_contract.py").is_file()
)
sys.path.insert(0, str(_ROOT / "utilities"))
from dispatch_contract import observed_attempt_liveness  # noqa: E402
from codex_dispatch_terminal import terminal_envelope_observed  # noqa: E402

from .. import model
from ..model import ContextEvidence, DispatchJob, etime_to_min
from ..token_budget import parse_codex_token_count, telemetry_from_explicit
from . import procscan

_AUTOPILOT = re.compile(r"/autopilot-([a-z-]+)")
_LOOPS = re.compile(r"loops/(oncall|note|study|drill|runtime-watch)")
_LOOP_KEYS = ("oncall", "note", "study", "drill", "runtime-watch")
_DRILL_LOG_PATH = re.compile(r"(/[^\s'\";]*drill[^\s'\";]*\.log)")
_DRILL_CASE_LINE = re.compile(r"^▶\s+(.+?)\s+\(work=", re.MULTILINE)
_MODE = re.compile(r"(?:^|\s)--mode(?:=|\s+)([a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)?)")
_CAPABILITY_MODE = re.compile(r"(?:^|\s)--capability-mode(?:=|\s+)([a-z][a-z0-9-]*)")
_WORKER_MODE = re.compile(r"(?:^|\s)--worker-mode(?:=|\s+)([a-z][a-z0-9-]*/[a-z][a-z0-9-]*)")
_QA = re.compile(r"--qa ([a-z]+)")
# Valid qa levels — guards argv layer-1 (effective_qa) against contaminated matches:
# `\w+` is Unicode so `--qa (\w+)` would capture Korean/label text that merely mentions
# ``--qa`` inside a task description. Narrowing to [a-z]+ and
# filtering to real levels keeps the argv layer trustworthy, so the R3 layered resolver
# only falls through to jobs.log/plan/default when argv genuinely has no --qa.
_QA_LEVELS = ("quick", "light", "standard", "thorough", "adversarial")
_PIPE = re.compile(r"\s*([A-Za-z][\w-]*)(?::(\w+))?")
_SHELLS = ("zsh", "bash", "sh", "dash")
_PIPE_TOK = re.compile(r"[,\s]+")
_DRILL_SLUG_RE = re.compile(r"^drill-[a-z]+-(.+)-\d{14}-\d+$")   # registry slug → case
_DRILL_CWD_COMP_RE = re.compile(r"^drill-(.+)-[^-]+$")           # /tmp/drill-<case>-<rand> component to case
_MANAGED_SIDECAR_RE = re.compile(
    r"^(.*/\.harness/managed-sessions/[^/]+)/managed-sidecars/[^/]+$"
)
_TERMINAL_REGISTRY_STATUSES = frozenset(("done", "killed", "cancelled"))
# F-46 (v29): a `done` row lingers this long as a display-only afterglow row — the job-row
# mirror of the group cooling window (render `_COOL_WINDOW_MIN`). Rationale: a multi-minute
# quick dispatch used to evaporate the instant it finished, so the user read it as "never
# ran". `killed`/`cancelled` are NOT afterglow (they keep the existing stale/dead path):
# an interrupted attempt is not a completion worth lingering on.
DONE_AFTERGLOW_MIN = 15
_PID_HOST_NAMESPACE_PROOF = "nspid-procfs-root-v1"
_DEGRADATION_CACHE = {}
_DEGRADATION_CACHE_LIMIT = 128
_DEGRADATION_REQUIRED = {"schema_version", "kind", "ts", "route_id", "route_node",
                         "route_hash", "dispatch_depth", "fallback_hop",
                         "execution_surface", "writer"}


def _managed_parent_dir(sidecar_log):
    """Exact managed-session state dir from a registered sidecar path.

    The registry path is attribution evidence only when it is absolute and has the
    launcher-owned ``.harness/managed-sessions/<state>/managed-sidecars/<file>`` shape.
    Normalization removes harmless ``.`` components; malformed or out-of-tree values fail
    closed so render never falls back to cwd guessing.
    """
    if not isinstance(sidecar_log, str) or not os.path.isabs(sidecar_log):
        return None
    normalized = os.path.normpath(sidecar_log)
    match = _MANAGED_SIDECAR_RE.match(normalized)
    return match.group(1) if match else None


def _drill_case_from_slug(slug):
    """Extract a case from registry slug ``drill-<adapter>-<case>-<ts>-<pid>``."""
    m = _DRILL_SLUG_RE.match(slug or "")
    return m.group(1) if m else None


def _drill_case_from_cwd(cwd):
    """Extract a case from a ``/tmp/drill-<case>-<rand>`` cwd component."""
    if not (cwd or "").startswith("/tmp/"):
        return None
    for comp in (cwd or "").split("/"):
        if comp.startswith("drill-"):
            m = _DRILL_CWD_COMP_RE.match(comp)
            if m:
                case = m.group(1)
                return case[len("growing_"):] if case.startswith("growing_") else case
    return None


def _strip_autopilot_prefix(name):
    if name and name.startswith("autopilot-"):
        return name[len("autopilot-"):]
    return name


def _parse_pipe_meta(pipe):
    """Parse jobs.log pipe metadata.

    The registry stays six tab fields for backward compatibility; depth/parent/intensity
    live in this sixth ``pipe`` field as optional ``key=value`` pairs. OLD form
    ``autopilot-code:dev(...)`` still returns name/mode only.
    """
    head = pipe.split("(", 1)[0] if pipe else ""
    eq_pos = head.find("=")
    colon_pos = head.find(":")
    if eq_pos != -1 and (colon_pos == -1 or eq_pos < colon_pos):
        # continuation tokenizer (SD-F4, 2026-07-09 wild fixture): the writer
        # (dispatch-headless.py:260) emits a closed key= vocabulary, but a value can itself
        # contain spaces (e.g. `model_role=deep maker`) — a naive `,`-only or whitespace-only
        # split breaks one of the two forms. Tokenize on `[,\s]+`; a token WITH `=` starts a
        # new (k, v) pair, a token WITHOUT `=` is a continuation that space-joins onto the
        # PREVIOUS pair's value. This assumes every real field is written as `key=value`
        # (never a bare value) — see plan R8/N2 — so a stray `=`-free token can only be a
        # continuation, never a new field.
        fields = {}
        last_key = None
        for tok in _PIPE_TOK.split(head):
            if not tok:
                continue
            if "=" in tok:
                k, v = tok.split("=", 1)
                k = k.strip()
                fields[k] = v.strip()
                last_key = k
            elif last_key is not None:
                fields[last_key] = fields[last_key] + " " + tok
        fields["_name"] = _strip_autopilot_prefix(fields.get("capability"))
        return fields
    m = _PIPE.match(pipe or "")
    if not m:
        return {}
    return {"_name": _strip_autopilot_prefix(m.group(1)), "mode": m.group(2)}


def _parse_pipe(pipe):
    """Parse a jobs.log pipe field, dual-form → (name, mode, qa, profile)."""
    fields = _parse_pipe_meta(pipe)
    return fields.get("_name"), fields.get("mode"), fields.get("qa"), fields.get("profile")


def _dispatch_mode_axes(meta, worker_type=None, unit=None):
    """Project current and legacy mode metadata without laundering conflicts.

    Canonical fields always win. A scalar legacy ``mode`` can backfill only the
    capability axis; a slash form can backfill only a non-owner worker axis.
    The historically invalid owner + stage-mode row stays visible as a conflict
    and is never presented as the owner's capability mode.
    """

    capability_mode = meta.get("capability_mode") or None
    worker_mode = meta.get("worker_mode") or None
    legacy_mode = meta.get("mode") or None
    owner_row = bool(
        worker_type == "owner"
        or unit == "_kernel/owner"
        or (meta.get("worker_role") or "").endswith("orchestrator")
    )
    conflict = False
    if legacy_mode:
        if "/" in legacy_mode:
            if owner_row:
                conflict = True
            elif worker_mode is None:
                worker_mode = legacy_mode
            elif worker_mode != legacy_mode:
                conflict = True
        elif capability_mode is None:
            capability_mode = legacy_mode
        elif capability_mode != legacy_mode:
            conflict = True
    if owner_row and worker_mode:
        conflict = True
    if worker_mode and unit and not unit.startswith("_kernel/") and worker_mode != unit:
        conflict = True
    return capability_mode, worker_mode, conflict


def _pid_namespace_identity(pid="self"):
    try:
        return os.readlink("/proc/%s/ns/pid" % pid)
    except OSError:
        return None


def _authoritative_registry_pid(meta):
    """Resolve the registry identity valid in Fleet's current PID namespace.

    This is intentionally equivalent to
    ``dispatch_contract.authoritative_process_identities``.  Fleet's portable
    copy cannot treat an equal numeric PID as cross-namespace proof: local
    evidence needs the launch observer namespace, while ``pid_host`` needs the
    recorded procfs-root namespace plus the explicit NSpid proof marker.
    """
    current_namespace = _pid_namespace_identity()
    raw_local = meta.get("pid", "")
    local_start = meta.get("pid_start", "") or meta.get("proc_start", "")
    recorded_observer = meta.get("pid_observer_ns", "")
    pid_scope = meta.get("pid_scope", "host-visible")
    local_authoritative = (
        bool(recorded_observer and current_namespace == recorded_observer)
        or (not recorded_observer and pid_scope != "namespace-local")
    )
    if raw_local.isdigit() and local_start and local_authoritative:
        return int(raw_local), local_start, "local"

    raw_host = meta.get("pid_host", "")
    host_start = meta.get("pid_host_start", "") or local_start
    if (
        raw_host.isdigit()
        and host_start
        and meta.get("pid_host_proof") == _PID_HOST_NAMESPACE_PROOF
        and current_namespace
        and current_namespace == meta.get("pid_host_ns", "")
    ):
        return int(raw_host), host_start, "host"
    return None, None, None


def _parse_depth(value):
    try:
        depth = int(value or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, depth)


def _parse_optional_int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


_KNOWN_HARNESSES = {"claude", "codex", "opencode"}
_TRANSPORTS = {"headless", "interactive"}
_EXECUTION_SURFACES = {
    "registered-headless",
    "codex-native-subagent",
    "claude-subagent",
    "claude-agent-team-teammate",
    "inline",
}
_FALLBACK_HOPS = {
    "same-harness-headless",
    "cross-harness-headless",
    "native-subagent",
    "inline",
}


def _attempt_contract(meta):
    """Project independent attempt axes without mutating historical rows."""
    raw_version = meta.get("attempt_schema_version")
    try:
        version = int(raw_version) if raw_version not in (None, "") else 1
    except (TypeError, ValueError):
        version = None
    if version != 2:
        legacy = version == 1
        return {
            "attempt_schema_version": version,
            "dispatch_depth": None,
            "transport": None,
            "execution_surface": None,
            "registered_worker": None,
            "fallback_hop": None,
            "legacy_read_only": legacy,
            "attempt_contract_status": (
                "legacy-read-only" if legacy else "invalid:schema-version"
            ),
        }

    try:
        dispatch_depth = int(meta.get("dispatch_depth"))
    except (TypeError, ValueError):
        dispatch_depth = None
    transport = meta.get("transport")
    surface = meta.get("execution_surface")
    fallback_hop = meta.get("fallback_hop", "")
    registered_raw = str(meta.get("registered_worker", "")).lower()
    registered = (
        True if registered_raw in {"1", "true"}
        else False if registered_raw in {"0", "false"}
        else None
    )
    violations = []
    if (
        any(key in meta for key in ("depth", "owner_depth", "max_depth"))
        or dispatch_depth not in {0, 1, 2}
    ):
        violations.append("dispatch_depth")
    if transport not in _TRANSPORTS:
        violations.append("transport")
    if surface not in _EXECUTION_SURFACES:
        violations.append("execution_surface")
    if registered is None or registered != (surface == "registered-headless"):
        violations.append("registered_worker")
    if fallback_hop not in _FALLBACK_HOPS and not (
        dispatch_depth == 0 and fallback_hop == ""
    ):
        violations.append("fallback_hop")
    if dispatch_depth == 0 and (
        transport != "interactive"
        or surface != "inline"
        or registered is not False
        or fallback_hop
    ):
        violations.append("direct_axes")
    if surface == "registered-headless" and (
        transport != "headless"
        or fallback_hop not in {
            "same-harness-headless", "cross-harness-headless"
        }
    ):
        violations.append("headless_axes")
    if surface in {"codex-native-subagent", "claude-subagent"} and (
        transport != "headless"
        or registered is not False
        or fallback_hop != "native-subagent"
    ):
        violations.append("native_axes")
    if surface == "claude-agent-team-teammate":
        violations.append("teammate_not_dispatch_attempt")
    if surface == "inline" and dispatch_depth in {1, 2} and fallback_hop != "inline":
        violations.append("inline_axes")
    return {
        "attempt_schema_version": version,
        "dispatch_depth": dispatch_depth,
        "transport": transport,
        "execution_surface": surface,
        "registered_worker": registered,
        "fallback_hop": fallback_hop or None,
        "legacy_read_only": False,
        "attempt_contract_status": (
            "invalid:" + ",".join(dict.fromkeys(violations))
            if violations else "current"
        ),
    }


def _infer_harness(meta, slug=None):
    """Return dispatch runtime from explicit metadata or legacy model fields."""
    h = (meta.get("harness") or "").strip().lower()
    if h in _KNOWN_HARNESSES:
        return h
    if meta.get("reasoning") or meta.get("approval"):
        return "codex"
    if meta.get("variant") or meta.get("agent"):
        return "opencode"
    if meta.get("effort"):
        return "claude"
    s = slug or ""
    for h in _KNOWN_HARNESSES:
        if s.startswith(h + "-") or ("-" + h + "-") in s:
            return h
    return None


def _same_path(a, b):
    if not a or not b:
        return False
    return a == b or os.path.abspath(a) == os.path.abspath(b)


def _codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _codex_transcript_cwd(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"cwd"' not in line:
                    continue
                try:
                    payload = (json.loads(line).get("payload") or {})
                except Exception:
                    continue
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _codex_sessions_dir(profile=None, slug=None):
    if profile and slug:
        return os.path.join(_registry_home(), ".dispatch", "homes", "%s.%s" % (slug, profile), "sessions")
    return os.path.join(_codex_home(), "sessions")


def _codex_sessions_dirs(cwd, profile=None, slug=None):
    """Return every session store that can own this Codex dispatch.

    Nested Codex conductors commonly launch a stage worker with a worktree-local
    CODEX_HOME. The registry records the worktree but not that inherited environment,
    so inspect the deterministic local projection before the Fleet process' own home.
    Profile jobs remain isolated to their explicit profile home.
    """
    if profile and slug:
        return [_codex_sessions_dir(profile, slug)]

    candidates = []
    if cwd:
        candidates.append(os.path.join(cwd, ".dispatch", "codex-home", "sessions"))
    candidates.append(_codex_sessions_dir())

    result = []
    seen = set()
    for path in candidates:
        key = os.path.abspath(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _build_codex_rollout_index(jobs):
    """Build one root-scoped cwd/newest-mtime index for this dispatch collection."""
    roots = []
    seen_roots = set()
    for job in jobs:
        if getattr(job, "harness", None) != "codex" or not getattr(job, "cwd", None):
            continue
        for candidate in _codex_sessions_dirs(
            job.cwd, getattr(job, "profile", None), getattr(job, "slug", None)
        ):
            root = os.path.abspath(candidate)
            if root in seen_roots:
                continue
            seen_roots.add(root)
            roots.append(root)

    result = {}
    parsed_files = {}
    for sessions_root in roots:
        by_cwd = result.setdefault(sessions_root, {})
        try:
            walker = os.walk(sessions_root)
            for root, _dirs, names in walker:
                for name in names:
                    if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                        continue
                    path = os.path.join(root, name)
                    physical = os.path.realpath(path)
                    if physical not in parsed_files:
                        try:
                            cwd = _codex_transcript_cwd(path)
                            mtime = os.path.getmtime(path)
                        except OSError:
                            parsed_files[physical] = None
                        else:
                            parsed_files[physical] = (
                                (os.path.abspath(cwd), mtime)
                                if isinstance(cwd, str) and cwd else None
                            )
                    parsed = parsed_files[physical]
                    if parsed is None:
                        continue
                    cwd, mtime = parsed
                    newest = by_cwd.get(cwd)
                    if newest is None or mtime > newest:
                        by_cwd[cwd] = mtime
        except OSError:
            continue
    return result


def _codex_job_liveness(cwd, now, stale_min=15, profile=None, slug=None,
                        codex_index=None):
    if not cwd:
        return "unknown"
    newest = None
    if isinstance(codex_index, dict):
        wanted_cwd = os.path.abspath(cwd)
        for sessions in _codex_sessions_dirs(cwd, profile, slug):
            mtime = codex_index.get(os.path.abspath(sessions), {}).get(wanted_cwd)
            if mtime is not None and (newest is None or mtime > newest):
                newest = mtime
    else:
        for sessions in _codex_sessions_dirs(cwd, profile, slug):
            try:
                for root, _dirs, names in os.walk(sessions):
                    for name in names:
                        if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                            continue
                        path = os.path.join(root, name)
                        if not _same_path(_codex_transcript_cwd(path) or "", cwd):
                            continue
                        mtime = os.path.getmtime(path)
                        if newest is None or mtime > newest:
                            newest = mtime
            except OSError:
                continue
    if newest is None:
        return "dead"
    return "working" if (now - newest) / 60.0 <= stale_min else "stale"


def _opencode_db():
    explicit = os.environ.get("OPENCODE_DB")
    if explicit:
        return explicit
    data_home = os.environ.get("OPENCODE_DATA_HOME")
    if data_home:
        return os.path.join(data_home, "opencode.db")
    return os.path.expanduser("~/.local/share/opencode/opencode.db")


def _opencode_to_seconds(ts):
    if ts is None:
        return 0.0
    return float(ts) / 1000.0 if ts > 10_000_000_000 else float(ts)


def _opencode_heartbeat_age(slug, now):
    if not slug:
        return None
    path = os.path.join(_registry_home(), ".dispatch", "logs", slug + ".heartbeat")
    try:
        return (now - os.path.getmtime(path)) / 60.0
    except OSError:
        return None


def _opencode_job_liveness(cwd, now, stale_min=15, slug=None):
    if not cwd:
        return "unknown"
    db = _opencode_db()
    if not os.path.exists(db):
        return "unknown"
    con = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
        rows = con.execute(
            """
            SELECT
              s.directory,
              MAX(
                s.time_updated,
                COALESCE((SELECT MAX(time_updated) FROM message WHERE session_id = s.id), 0),
                COALESCE((SELECT MAX(time_updated) FROM part WHERE session_id = s.id), 0),
                COALESCE((SELECT MAX(time_updated) FROM session_message WHERE session_id = s.id), 0),
                COALESCE((SELECT MAX(time_created) FROM session_input WHERE session_id = s.id), 0)
              ) AS last_updated
            FROM session s
            ORDER BY last_updated DESC
            """
        )
        newest = None
        for row in rows:
            if _same_path(row[0], cwd):
                newest = _opencode_to_seconds(row[1])
                break
    except Exception:
        newest = None
    finally:
        if con is not None:
            con.close()
    if newest:
        return "working" if (now - newest) / 60.0 <= stale_min else "stale"
    hb_age = _opencode_heartbeat_age(slug, now)
    if hb_age is not None and hb_age <= stale_min:
        return "working"
    return "dead"


# F-15c: an `open` registry row with no transcript yet, within this startup grace window,
# is genuinely "not started" (queued) rather than dead — past the window with still no
# transcript, it's dead. Canonical value lives in model.JOB_QUEUED_GRACE_MIN (F-25 removed
# the constant duplication); re-exported here for existing callers.
_QUEUED_GRACE_MIN = model.JOB_QUEUED_GRACE_MIN


def _job_transcript_signal(job, now, codex_index=None):
    """tier-3 evidence only: what the transcript/rollout/db mtime says, per harness.
    Returns working | stale | dead | unknown. No judgment — that is classify_job's job."""
    if job.harness == "codex":
        return _codex_job_liveness(
            job.cwd, now, profile=job.profile, slug=job.slug,
            codex_index=codex_index,
        )
    if job.harness == "opencode":
        return _opencode_job_liveness(job.cwd, now, slug=job.slug)
    return _job_liveness(job.cwd, now, profile=job.profile, slug=job.slug)


def _attempt_heartbeat(attempt_id):
    """Read the bounded SD-58 record for one exact attempt, if present."""
    if not attempt_id:
        return None
    path = os.path.join(
        _registry_home(), ".dispatch", "heartbeats",
        str(attempt_id).replace("/", "_") + ".json",
    )
    try:
        if os.path.getsize(path) > 8192:
            return None
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _attempt_terminal_observation(attempt_id, route_id, route_node):
    if not attempt_id or not route_id or not route_node:
        return None
    path = os.path.join(
        _registry_home(), ".dispatch", "watchdog",
        str(attempt_id).replace("/", "_") + ".json",
    )
    try:
        if os.path.getsize(path) > 8192:
            return None
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not value.get("terminal_action"):
        return None
    return {
        **value,
        "attempt_id": attempt_id,
        "route_id": route_id,
        "route_node": route_node,
    }


def _dispatch_liveness(job, now, track=True, codex_index=None):
    """Job → state string. Signature/return preserved; the verdict now comes from the
    single F-25 classifier. Stamps `job.state_evidence` as a side effect.

    track=False skips the cross-tick tracker (and thus hysteresis): used when the call is
    only deriving EVIDENCE for another row, so a row that is about to be dropped never
    leaves a tracker entry behind.
    """
    is_loop = job.source == "proc" and job.key in _LOOP_KEYS
    pid_alive = None
    proc_start_match = None
    actual_proc_start = None
    if job.pid is not None:
        proc_path = "/proc/%s" % job.pid
        visible = os.path.exists(proc_path)
        if not visible:
            pid_alive = False
            proc_start_match = False if job.proc_start else None
        elif job.proc_start:
            observed_start = procscan.read_proc_start(job.pid)
            if observed_start is not None:
                actual_proc_start = str(observed_start)
                pid_alive = True
                proc_start_match = str(observed_start) == str(job.proc_start)
    terminal_observation = _attempt_terminal_observation(
        job.attempt_id, job.route_id, job.route_node
    )
    common_observation = None
    registry_metadata = getattr(job, "_registry_metadata", None)
    if (
        isinstance(registry_metadata, dict)
        and job.attempt_contract_status == "current"
        and job.registered_worker is True
    ):
        common_observation = observed_attempt_liveness(
            job.status,
            registry_metadata,
            terminal_envelope=(
                bool(terminal_observation)
                or terminal_envelope_observed(getattr(job, "_log_file", None))
            ),
        )
    ev_in = {
        "source": job.source,
        "key": job.key,
        "is_loop": is_loop,
        "harness": job.harness,
        "status": job.status,
        "elapsed_min": job.elapsed_min,
        "slug": job.slug,
        "pid": job.pid,
        "proc_start": job.proc_start,
        "pid_scope": job.pid_scope,
        "pid_authoritative": bool(
            job.pid is not None
            and (
                job.source == "proc"
                or job.pid_scope != "namespace-local"
                or bool(job.pid_identity_source)
            )
        ),
        "pid_identity_source": job.pid_identity_source,
        "pid_local": job.pid_local,
        "pid_local_start": job.pid_local_start,
        "pid_host": job.pid_host,
        "pid_host_start": job.pid_host_start,
        "pid_host_ns": job.pid_host_ns,
        "pid_ns": job.pid_ns,
        "pid_observer_ns": job.pid_observer_ns,
        "pid_host_proof": job.pid_host_proof,
        "pgid": job.pgid,
        "actual_proc_start": actual_proc_start,
        "pid_alive": pid_alive,
        "proc_start_match": proc_start_match,
        "attempt_id": job.attempt_id,
        "route_id": job.route_id,
        "route_node": job.route_node,
        "registry_transition": {"status": job.status},
        "heartbeat": _attempt_heartbeat(job.attempt_id),
        "terminal_observation": terminal_observation,
        "observed_liveness": (
            {
                "state": common_observation.state,
                "reason": common_observation.reason,
                "process_state": common_observation.process_state,
                "process_reason": common_observation.process_reason,
            }
            if common_observation
            else None
        ),
        # A loop proc row is decided by tier-2 evidence; skip the mtime probe entirely
        # (it was never consulted on that path pre-F-25 either).
        "transcript": (
            None if is_loop else _job_transcript_signal(
                job, now, codex_index=codex_index
            )
        ),
        "proc_liveness": getattr(job, "_proc_liveness", None),
    }
    state, evidence = model.classify_job(ev_in, now,
                                         key=("j", job.slug) if track else None)
    job.state_evidence = evidence
    if common_observation and common_observation.state == "reconcile-needed":
        job.stage = "reconcile-needed"
        job.note = "reconcile-needed"
    return state


# --- jobs.log path ---
def _registry_home():
    """Canonical dispatch-registry home — reproduces utilities/agent-home.sh resolution
    (AGENT_HOME → CLAUDE_HOME → $HOME/hearting → legacy $HOME/agent_setting → ~/.claude). Holds
    .dispatch/jobs.log and .dispatch/homes/ (profile masked homes). Distinct from the
    runtime telemetry home (_proj_home). See core/OPERATIONS.md §5.10."""
    h = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
    if h:
        return h
    for cand in (os.path.expanduser("~/hearting"), os.path.expanduser("~/agent_setting")):
        if os.path.isdir(cand):
            return cand
    return os.path.expanduser("~/.claude")


def _jobs_path(override=None):
    if override:
        return override
    env = os.environ.get("AGENT_DISPATCH_JOBS")
    if env:
        return env
    home = _registry_home()
    return os.path.join(home, ".dispatch", "jobs.log")


def _opencode_config_home():
    """OpenCode global config home (INSTALL_LAYOUT.md target layout)."""
    return os.path.expanduser("~/.config/opencode")


def _installed_registry_paths():
    """Installed per-runtime-home dispatch registries that exist as files.

    An activated harness install roots its agent home at `<runtime-home>/.harness/`
    (INSTALL_LAYOUT.md activation record), so that install's SD-49 canonical registry
    is `<runtime-home>/.harness/dispatch/jobs.log`, NOT the maintainer checkout's
    `<agent-home>/.dispatch/jobs.log`. A managed Codex parent therefore registers its
    dispatch children under ~/.codex/.harness/; without reading these, fleet reports
    jobs=0 and — because the worker session row is hidden as a dispatch child
    (collectors/__init__.py:_mark_dispatch_child_sessions) — the whole dispatch
    disappears from the screen (user 2026-07-29: "분사 세션이 fleet에 안 보여").
    Read-only and existence-gated: a runtime that was never activated adds nothing.
    """
    out = []
    for home in (_codex_home(), _proj_home(), _opencode_config_home()):
        if not home:
            continue
        path = os.path.join(home, ".harness", "dispatch", "jobs.log")
        if os.path.isfile(path):
            out.append(path)
    return out


def _candidate_jobs_paths(override=None):
    """Dispatch registries to read, in precedence order.

    Explicit override/env means the caller intentionally selected one registry. The default
    path follows the neutral <agent-home> resolution, then adds legacy ~/.claude only when
    it is a distinct existing file, then every installed per-runtime-home registry that
    exists. This keeps old long-running drill/Claude jobs visible during migration and
    surfaces installed-harness dispatch, without duplicating rows for normal projected
    installs.
    """
    if override:
        return [override]
    env = os.environ.get("AGENT_DISPATCH_JOBS")
    if env:
        return [env]
    paths = [_jobs_path()]
    legacy = os.path.expanduser("~/.claude/.dispatch/jobs.log")
    if legacy and not _same_path(legacy, paths[0]) and os.path.exists(legacy):
        paths.append(legacy)
    paths.extend(_installed_registry_paths())
    result = []
    seen = set()
    for path in paths:
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


# --- job liveness = transcript mtime (dispatch-liveness.sh reuse, PRD §7) ---
def _proj_home():
    """Runtime telemetry home (projects/sessions/.statusline) — Claude Code config dir.
    DISTINCT from the registry home (_registry_home): telemetry dirs live only here, not
    under Hearting. CLAUDE_CONFIG_DIR override honored, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


_CLAUDE_STREAM_TAIL_BYTES = 512 * 1024
_CLAUDE_SUBAGENT_SCAN_BYTES = 8 * 1024 * 1024
_CLAUDE_DISPATCH_CONTEXT_WINDOW_DEFAULT = 1_000_000
_CLAUDE_STREAM_CACHE = {}       # path -> (mtime_ns, size, parsed)
_CODEX_ATTEMPT_CACHE = {}       # path -> (mtime_ns, size, parsed)
_OPENCODE_ATTEMPT_CACHE = {}    # path -> (mtime_ns, size, parsed)


def _owned_attempt_log_path(job):
    """Return one exact Claude/Codex/OpenCode attempt log, or fail closed.

    ``--log-dir`` may place registered-worker streams either under the dispatch
    registry or below the canonical artifact root.  ``log_file`` is registry data,
    so accept only a real file below one of those roots whose basename binds both
    the exact attempt id and the declared harness suffix.
    """
    raw = getattr(job, "_log_file", None)
    attempt_id = getattr(job, "attempt_id", None)
    harness = getattr(job, "harness", None)
    if harness not in ("claude", "codex", "opencode") or not raw or not attempt_id:
        return None
    path = os.path.realpath(raw)
    roots = [os.path.realpath(os.path.join(_registry_home(), ".dispatch", "logs"))]
    registry_path = getattr(job, "_registry_path", None)
    if registry_path:
        registry_dir = os.path.dirname(os.path.realpath(registry_path))
        roots.append(os.path.join(registry_dir, "logs"))
        # Installed runtime registries use `<runtime>/.harness/dispatch/jobs.log`,
        # while a wrapper with that agent home still defaults logs to the sibling
        # hidden `.dispatch/logs` directory.
        if os.path.basename(registry_dir) == "dispatch":
            roots.append(os.path.join(os.path.dirname(registry_dir), ".dispatch", "logs"))
    artifact_root = getattr(job, "artifact_root", None)
    if artifact_root:
        roots.append(os.path.realpath(artifact_root))
    allowed = False
    for root in roots:
        try:
            if os.path.commonpath((root, path)) == root:
                allowed = True
                break
        except (TypeError, ValueError):
            continue
    if not allowed:
        return None
    name = os.path.basename(path)
    if not name.endswith(".%s.jsonl" % harness) or ("." + attempt_id + ".") not in name:
        return None
    return path if os.path.isfile(path) else None


def _owned_claude_stream_path(job):
    """Return one current-attempt Claude stream log, or fail closed."""
    if getattr(job, "harness", None) != "claude":
        return None
    return _owned_attempt_log_path(job)


def _attempt_summary_sid(job):
    attempt_id = getattr(job, "attempt_id", None)
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", attempt_id):
        return None
    return "dispatch-" + attempt_id


def _enrich_attempt_summary(job):
    """Attach an attempt-owned summary sidecar produced by dispatch.

    A registered job does not always materialize as a separately collectible
    ``Session``.  The exact attempt log is still a conversational transcript, so
    expose its exact path for attribution and read the dispatch owner's sidecar
    under an attempt-scoped key. Fleet never starts the producer. This never
    guesses by cwd or pid and therefore cannot borrow another child's NOW.
    """
    path = _owned_attempt_log_path(job)
    sid = _attempt_summary_sid(job)
    if path is None or sid is None:
        return
    job._transcript_path = path
    job._summary_sid = sid
    try:
        from .. import titles
        if not getattr(job, "title", None):
            job.title = titles.fresh_title(sid, harness=job.harness)
        if not getattr(job, "summary", None):
            job.summary, job.summary_ts = titles.fresh_summary_with_ts(
                sid, harness=job.harness)
    except Exception:
        pass


def _counter(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return None


def _shell_command_label(command):
    """Return one privacy-minimal executable label, never the full command."""
    if not isinstance(command, str):
        return None
    for line in command.splitlines():
        line = line.strip()
        if (not line or line.startswith("#")
                or re.match(r"^(?:set|export|cd)(?:\s|$)", line)
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line)):
            continue
        try:
            words = shlex.split(line, posix=True)
        except ValueError:
            words = line.split()
        while words and words[0] in ("env", "command", "sudo"):
            words.pop(0)
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)
        if words:
            return os.path.basename(words[0])[:48]
    return None


def _tool_label(name, tool_input=None, command=None):
    if name in ("Bash", "bash", "command_execution", "exec"):
        raw = command
        if raw is None and isinstance(tool_input, dict):
            raw = tool_input.get("command")
        return _shell_command_label(raw) or name
    return str(name)[:48] if isinstance(name, str) and name else None


def _parse_claude_stream_tail(path):
    """Read one bounded exact-attempt tail for identity, context, and open tool."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache_key = (st.st_mtime_ns, st.st_size)
    cached = _CLAUDE_STREAM_CACHE.get(path)
    if cached and cached[:2] == cache_key:
        return cached[2]
    start = max(0, st.st_size - _CLAUDE_STREAM_TAIL_BYTES)
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            raw = stream.read(_CLAUDE_STREAM_TAIL_BYTES)
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    if start and lines:
        lines = lines[1:]  # the first item is a partial JSON line
    session_ids = set()
    latest_usage = None
    latest_model = None
    latest_result_usage = None
    model_windows = {}
    open_tools = {}
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            session_ids.add(sid)
        row_type = payload.get("type")
        message = payload.get("message")
        if row_type == "assistant" and isinstance(message, dict):
            usage = message.get("usage")
            if isinstance(usage, dict):
                latest_usage = usage
            if isinstance(message.get("model"), str):
                latest_model = message["model"]
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    tool_id = item.get("id")
                    label = _tool_label(item.get("name"), item.get("input"))
                    if isinstance(tool_id, str) and label:
                        open_tools[tool_id] = {"name": label}
        elif row_type == "user" and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        open_tools.pop(item.get("tool_use_id"), None)
        elif row_type == "result":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                latest_result_usage = usage
            model_usage = payload.get("modelUsage")
            if isinstance(model_usage, dict):
                for model_id, values in model_usage.items():
                    if not isinstance(model_id, str) or not isinstance(values, dict):
                        continue
                    window = _counter(values.get("contextWindow"))
                    if window:
                        model_windows[model_id] = window
    window = model_windows.get(latest_model)
    if window is None and len(model_windows) == 1:
        window = next(iter(model_windows.values()))
    active = None
    if latest_usage is not None:
        parts = [_counter(latest_usage.get(key)) for key in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
        if any(value is not None for value in parts):
            active = sum(value or 0 for value in parts)
    cumulative = latest_result_usage or {}
    ambiguity = "multiple-stream-session-ids" if len(session_ids) > 1 else None
    parsed = {
        "session_id": next(iter(session_ids)) if len(session_ids) == 1 else None,
        "ambiguity": ambiguity,
        "active_context_tokens": active,
        "context_window_tokens": window,
        "session_input_tokens": _counter(cumulative.get("input_tokens")),
        "session_cached_input_tokens": _counter(cumulative.get("cache_read_input_tokens")),
        "session_output_tokens": _counter(cumulative.get("output_tokens")),
        "exec_tool": next(reversed(open_tools.values())) if open_tools else None,
    }
    _CLAUDE_STREAM_CACHE[path] = (cache_key[0], cache_key[1], parsed)
    if len(_CLAUDE_STREAM_CACHE) > 128:
        _CLAUDE_STREAM_CACHE.pop(next(iter(_CLAUDE_STREAM_CACHE)))
    return parsed


def _enrich_claude_stream_session(job):
    """Attach exact child identity, context/exec, and native sub-agents."""
    path = _owned_claude_stream_path(job)
    if path is None:
        return
    parsed = _parse_claude_stream_tail(path)
    if not parsed:
        return
    if parsed.get("ambiguity"):
        job.association_ambiguity = parsed["ambiguity"]
        return
    job._dispatch_context_owned = True
    active_context_tokens = parsed.get("active_context_tokens")
    context_window_tokens = parsed.get("context_window_tokens")
    # Claude's live JSON stream commonly omits modelUsage/contextWindow until the
    # terminal result. Fleet uses 1M as the live dispatch default; an explicit
    # same-attempt runtime value always takes precedence.
    if context_window_tokens is None and active_context_tokens is not None:
        context_window_tokens = _CLAUDE_DISPATCH_CONTEXT_WINDOW_DEFAULT
    telemetry = telemetry_from_explicit(
        adapter="claude", session_id=parsed.get("session_id"),
        active_context_tokens=active_context_tokens,
        context_window_tokens=context_window_tokens,
        session_input_tokens=parsed.get("session_input_tokens"),
        session_cached_input_tokens=parsed.get("session_cached_input_tokens"),
        session_output_tokens=parsed.get("session_output_tokens"))
    for field_name in (
            "active_context_tokens", "context_window_tokens", "session_input_tokens",
            "session_cached_input_tokens", "session_output_tokens", "session_total_tokens"):
        setattr(job, field_name, getattr(telemetry, field_name))
    job.ctx_pct = telemetry.context_used_pct
    job.exec_tool = parsed.get("exec_tool")
    if job.ctx_pct is not None:
        try:
            st = os.stat(path)
            sequence = (st.st_mtime_ns, st.st_size)
            job._context_evidence = ContextEvidence(
                used_pct=job.ctx_pct, source="claude-attempt-stream",
                sequence=sequence, source_head_sequence=sequence,
                observed_at=st.st_mtime, fresh_until=st.st_mtime + 900)
        except OSError:
            pass
    # The attempt-owned log is an exact source even when the wrapper pid differs from
    # the runtime pid (and even when no persistent Claude transcript was created).
    # Reuse the parent-thread Agent lifecycle parser instead of inventing a second
    # launch/completion state machine. Keep the scan bounded for the 2s Fleet tick.
    try:
        from . import claude as claude_collector
        subagents = claude_collector._tail_subagents(
            path, max_scan=_CLAUDE_SUBAGENT_SCAN_BYTES)
    except Exception:
        subagents = None
    if subagents is not None:
        for subagent in subagents:
            subagent.source = "claude-attempt-stream"
        job.subagents = subagents
    session_id = parsed.get("session_id")
    if not session_id:
        return
    job._runtime_session_id = session_id


def _parse_codex_attempt_tail(path):
    """Read sanitized App Server telemetry and the currently open command item."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache_key = (st.st_mtime_ns, st.st_size)
    cached = _CODEX_ATTEMPT_CACHE.get(path)
    if cached and cached[:2] == cache_key:
        return cached[2]
    start = max(0, st.st_size - _CLAUDE_STREAM_TAIL_BYTES)
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            raw = stream.read(_CLAUDE_STREAM_TAIL_BYTES)
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    if start and lines:
        lines = lines[1:]
    latest_usage = None
    thread_ids = set()
    open_commands = {}
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        row_type = payload.get("type")
        if row_type == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                thread_ids.add(thread_id)
        elif row_type == "dispatch.supervisor.token_usage":
            usage = payload.get("token_usage")
            if isinstance(usage, dict):
                latest_usage = usage
        elif row_type == "item.started":
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            item_id = item.get("id")
            label = _tool_label("command_execution", command=item.get("command"))
            if isinstance(item_id, str) and label:
                open_commands[item_id] = {"name": label}
        elif row_type == "item.completed":
            item = payload.get("item")
            if isinstance(item, dict):
                open_commands.pop(item.get("id"), None)
    parsed = {"token_usage": latest_usage,
              "thread_id": next(iter(thread_ids)) if len(thread_ids) == 1 else None,
              "thread_ambiguity": len(thread_ids) > 1,
              "exec_tool": next(reversed(open_commands.values())) if open_commands else None}
    _CODEX_ATTEMPT_CACHE[path] = (cache_key[0], cache_key[1], parsed)
    if len(_CODEX_ATTEMPT_CACHE) > 128:
        _CODEX_ATTEMPT_CACHE.pop(next(iter(_CODEX_ATTEMPT_CACHE)))
    return parsed


def _codex_attempt_rollout(job, thread_id):
    """Resolve a raw ``codex exec --json`` attempt to one exact projected rollout."""
    try:
        from . import codex as codex_collector
    except Exception:
        return None
    homes = []
    cwd = getattr(job, "cwd", None)
    if isinstance(cwd, str) and cwd:
        dispatch_dir = os.path.join(os.path.realpath(cwd), ".dispatch")
        homes.extend((
            os.path.join(dispatch_dir, "nested-codex-home"),
            os.path.join(dispatch_dir, "codex-home"),
        ))
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        homes.append(env_home)
    homes.append(os.path.expanduser("~/.codex"))
    return codex_collector.exact_rollout_for_session_id(thread_id, homes=homes)


def _enrich_codex_attempt_session(job):
    path = _owned_attempt_log_path(job)
    if getattr(job, "harness", None) != "codex" or path is None:
        return
    parsed = _parse_codex_attempt_tail(path)
    if parsed is None:
        return
    job._dispatch_context_owned = True
    thread_id = parsed.get("thread_id")
    if parsed.get("thread_ambiguity"):
        job.association_ambiguity = "multiple-attempt-thread-ids"
        thread_id = None
    elif thread_id:
        job._runtime_session_id = thread_id
    usage = parsed.get("token_usage") or {}
    last = usage.get("last") if isinstance(usage.get("last"), dict) else {}
    total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
    telemetry = telemetry_from_explicit(
        adapter="codex",
        active_context_tokens=last.get("total_tokens"),
        context_window_tokens=usage.get("model_context_window"),
        session_input_tokens=total.get("input_tokens"),
        session_cached_input_tokens=total.get("cached_input_tokens"),
        session_output_tokens=total.get("output_tokens"),
        session_reasoning_output_tokens=total.get("reasoning_output_tokens"),
        session_total_tokens=total.get("total_tokens"))
    evidence_path = path
    evidence_source = "codex-attempt-app-server"
    if telemetry.context_used_pct is None and thread_id:
        rollout = _codex_attempt_rollout(job, thread_id)
        if rollout:
            try:
                from . import codex as codex_collector
                line = codex_collector._tail_token_count(rollout)
            except Exception:
                line = None
            if line:
                rollout_telemetry = parse_codex_token_count(line, session_id=thread_id)
                if rollout_telemetry.active_context_tokens is not None:
                    telemetry = rollout_telemetry
                    evidence_path = rollout
                    evidence_source = "codex-attempt-rollout"
    for field_name in (
            "active_context_tokens", "context_window_tokens", "session_input_tokens",
            "session_cached_input_tokens", "session_output_tokens",
            "session_reasoning_output_tokens", "session_total_tokens"):
        setattr(job, field_name, getattr(telemetry, field_name))
    job.ctx_pct = telemetry.context_used_pct
    job.exec_tool = parsed.get("exec_tool")
    if job.ctx_pct is not None:
        try:
            st = os.stat(evidence_path)
            sequence = (st.st_mtime_ns, st.st_size)
            job._context_evidence = ContextEvidence(
                used_pct=job.ctx_pct, source=evidence_source,
                sequence=sequence, source_head_sequence=sequence,
                observed_at=st.st_mtime, fresh_until=st.st_mtime + 900)
        except OSError:
            pass


def _parse_opencode_attempt_tail(path):
    """Read the last step's prompt size from an `opencode run --format json` attempt log.

    Every event is an envelope ``{"type":..., "sessionID":..., "part":{...}}``.  Only
    ``step_finish`` carries ``part.tokens``; its ``input + cache.read + cache.write`` is
    the prompt the model actually saw on that request — the same context-side definition
    the session-level opencode collector uses.  ``output``/``reasoning`` are excluded, and
    the per-step numbers are never summed: a bounded tail cannot prove a session total.

    No ``exec_tool`` is derived here, and that is a schema fact rather than an omission:
    opencode publishes a ``tool_use`` event only once the call has already finished.
    Across six real attempt logs every one of 400+ tool events carried a ``state.status``
    of ``completed`` or ``error`` and never an in-flight state, so claude's tool_use ↔
    tool_result pairing has no opencode counterpart.  Reporting the last finished tool as
    ``exec`` would assert it is still running, so the field stays absent instead.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache_key = (st.st_mtime_ns, st.st_size)
    cached = _OPENCODE_ATTEMPT_CACHE.get(path)
    if cached and cached[:2] == cache_key:
        return cached[2]
    start = max(0, st.st_size - _CLAUDE_STREAM_TAIL_BYTES)
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            raw = stream.read(_CLAUDE_STREAM_TAIL_BYTES)
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]                       # a seek mid-file truncates the first line
    session_ids = set()
    active = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        sid = event.get("sessionID")
        if isinstance(sid, str) and sid:
            session_ids.add(sid)
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else None
        if not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        parts = [_counter(tokens.get("input")), _counter(cache.get("read")),
                 _counter(cache.get("write"))]
        if any(value is not None for value in parts):
            active = sum(value or 0 for value in parts)     # last one wins = current context
    parsed = {
        "session_id": next(iter(session_ids)) if len(session_ids) == 1 else None,
        "ambiguity": "multiple-stream-session-ids" if len(session_ids) > 1 else None,
        "active_context_tokens": active,
    }
    _OPENCODE_ATTEMPT_CACHE[path] = (cache_key[0], cache_key[1], parsed)
    if len(_OPENCODE_ATTEMPT_CACHE) > 128:
        _OPENCODE_ATTEMPT_CACHE.pop(next(iter(_OPENCODE_ATTEMPT_CACHE)))
    return parsed


def _opencode_job_context_window(job):
    """Registry ``model`` is ``<providerID>/<model-id>``; resolve its declared window."""
    model = getattr(job, "model", None)
    if not isinstance(model, str) or not model:
        return None
    provider, _, model_id = model.rpartition("/")
    try:
        from . import opencode as opencode_collector
        return opencode_collector._model_ctx_limit(model_id or model, provider or None)
    except Exception:
        return None


def _enrich_opencode_attempt_session(job):
    path = _owned_attempt_log_path(job)
    if getattr(job, "harness", None) != "opencode" or path is None:
        return
    parsed = _parse_opencode_attempt_tail(path)
    if not parsed:
        return
    if parsed.get("ambiguity"):
        job.association_ambiguity = parsed["ambiguity"]
        return
    job._dispatch_context_owned = True
    if parsed.get("session_id"):
        job._runtime_session_id = parsed["session_id"]
    telemetry = telemetry_from_explicit(
        adapter="opencode", session_id=parsed.get("session_id"),
        active_context_tokens=parsed.get("active_context_tokens"),
        context_window_tokens=_opencode_job_context_window(job))
    for field_name in ("active_context_tokens", "context_window_tokens"):
        setattr(job, field_name, getattr(telemetry, field_name))
    job.ctx_pct = telemetry.context_used_pct
    if job.ctx_pct is None:
        return
    try:
        st = os.stat(path)
        sequence = (st.st_mtime_ns, st.st_size)
        job._context_evidence = ContextEvidence(
            used_pct=job.ctx_pct, source="opencode-attempt-stream",
            sequence=sequence, source_head_sequence=sequence,
            observed_at=st.st_mtime, fresh_until=st.st_mtime + 900)
    except OSError:
        pass


def _enc(path):
    return "".join("-" if ch in "/._" else ch for ch in path)


def _model_display(mid):
    """Wire model id → display form: family word + short dotted version, with date and
    context suffixes (-20251001, [1m]) dropped. A vendor id is not spelled out here —
    concrete ids live only in the adapter configs (check-model-config)."""
    parts = mid.split("[", 1)[0].replace("claude-", "").split("-")
    fam = parts[0].capitalize()
    ver = ".".join(p for p in parts[1:] if p.isdigit() and len(p) <= 2)
    return fam + ((" " + ver) if ver else "")


def _claude_job_model(pid_s, jcwd=None):
    """A claude dispatch (claude -p) has its own session — resolve its model via
    sessions/<pid>.json → sessionId → .statusline/<sid>.json (same path as claude.py).
    HEADLESS sessions never render a statusline, so fall back to the transcript's own
    "model" field (assistant entries carry the real model id) — without this a dispatch
    launched with --model opus showed the PARENT's model (user 2026-07-02: main=Fable /
    dispatch-model policy remains observable through fleet)."""
    # Runtime-home only (accepted asymmetry, plan A5/R7): a profile(masked) headless job's
    # own sessions/.statusline live under its masked config home
    # (_registry_home()/.dispatch/homes/...), not here, so this lookup misses and degrades
    # to None while _job_liveness (which DOES branch on profile) stays correct. Deferred
    # fix recorded in R7.
    home = _proj_home()
    try:
        with open(os.path.join(home, "sessions", "%s.json" % pid_s)) as f:
            sid = json.load(f).get("sessionId")
    except Exception:
        return None
    if not sid:
        return None
    try:
        with open(os.path.join(home, ".statusline", "%s.json" % sid)) as f:
            m = json.load(f).get("model") or {}
        return m.get("display_name") or m.get("id")
    except Exception:
        pass
    if not jcwd:
        return None
    path = os.path.join(home, "projects", _enc(jcwd), sid + ".jsonl")
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - 65536))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    ids = re.findall(r'"model":"(claude-[a-z0-9.\-]+)', data)
    return _model_display(ids[-1]) if ids else None


def _job_liveness(path, now, stale_min=15, profile=None, slug=None):
    """working (transcript ≤15min) / stale (hung) / dead (no transcript) / unknown (no path).

    profile-aware (isomorphic to dispatch-liveness.sh, spec §7): when `profile` is set
    (and `slug` available), the job's transcript is isolated under its masked config home
    (`.dispatch/homes/<slug>.<profile>/projects/<enc>`) under the REGISTRY home
    (`_registry_home()` — masked homes live under hearting/.dispatch/homes/, never
    under the runtime telemetry home), rather than the RUNTIME home's `projects/<enc>`
    (`_proj_home()`) used by the non-profile branch. Resolving the profile branch against
    the wrong root would always false-DEAD a profile job. profile None (the pre-existing,
    profile-less job case) → unchanged runtime-home path."""
    if not path:
        return "unknown"
    if profile and slug:
        proj = os.path.join(_registry_home(), ".dispatch", "homes", "%s.%s" % (slug, profile),
                             "projects", _enc(path))
    else:
        proj = os.path.join(_proj_home(), "projects", _enc(path))
    newest = None
    try:
        for n in os.listdir(proj):
            if n.endswith(".jsonl"):
                m = os.path.getmtime(os.path.join(proj, n))
                if newest is None or m > newest:
                    newest = m
    except OSError:
        return "dead"
    if newest is None:
        return "dead"
    return "working" if (now - newest) / 60.0 <= stale_min else "stale"


# --- live_stage (ported statusline.sh:131-171) ---
def _has_entries(p):
    try:
        with os.scandir(p) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def _scan_degradations(route_ids, agent_home=None, tail=64):
    """Read only resolved route shards; malformed evidence is non-fatal and skipped."""
    root = os.path.join(agent_home or _registry_home(), ".dispatch", "degradations")
    out, seen = {}, set()
    paths = [os.path.join(root, str(rid) + ".jsonl") for rid in (route_ids or ())]
    paths.append(os.path.join(root, "_unattributed.jsonl"))
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (st.st_mtime_ns, st.st_size)
        cached = _DEGRADATION_CACHE.get(path)
        if cached and cached[:2] == key:
            rows = cached[2]
        else:
            try:
                with open(path, "rb") as stream:
                    stream.seek(max(0, st.st_size - 256 * tail))
                    raw = stream.read().splitlines()[-tail:]
                rows = []
                for line in raw:
                    try:
                        row = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(row, dict) or not _DEGRADATION_REQUIRED.issubset(row):
                        continue
                    if row.get("schema_version") != 1 or row.get("kind") not in {"degradation", "chain-exhausted", "leg-failure"}:
                        continue
                    if path.endswith("_unattributed.jsonl"):
                        row = dict(row)
                        row["_unattributed"] = True
                    rows.append(row)
            except OSError:
                continue
            _DEGRADATION_CACHE[path] = (key[0], key[1], rows)
            if len(_DEGRADATION_CACHE) > _DEGRADATION_CACHE_LIMIT:
                for old in sorted(_DEGRADATION_CACHE, key=lambda item: _DEGRADATION_CACHE[item][:2])[:-_DEGRADATION_CACHE_LIMIT]:
                    _DEGRADATION_CACHE.pop(old, None)
        for row in rows:
            event = row.get("event_id")
            if event and event in seen:
                continue
            if event:
                seen.add(event)
            rid = row.get("route_id")
            if row.get("_unattributed") or not rid:
                out.setdefault("_unattributed", []).append(row)
            elif rid in route_ids:
                out.setdefault(rid, []).append(row)
    return out


def _is_code_job(key=None, capability=None, worker_role=None):
    return (key or "").startswith("code") or (capability or "") == "autopilot-code" or (worker_role or "").startswith("code-")


def _find_plan_dir(jcwd, slug, key=None, capability=None, worker_role=None,
                   artifact_root=None):
    """Locate the plans/*_<slug>/ folder for (jcwd, slug): exact `_<slug>` suffix match,
    else the folder with max hyphen-token overlap (skipping done folders). abs path or None.
    Extracted from live_stage (REFACTOR, behavior-preserving — see plan Step 1.3).

    `artifact_root` (registry meta) wins over the cwd heuristic: a SOURCE-ONLY worktree
    (OPERATIONS §5.10) has no reports dir of its own — its plans/ live in the primary
    checkout's root the wrapper recorded. Without this, an inline conductor's breadcrumb
    never left `pre` (user 2026-07-20: "fleet이 여전히 pre에만 깜빡이는거야?")."""
    if not _is_code_job(key, capability, worker_role) or not jcwd or not slug:
        return None
    if artifact_root and os.path.isdir(os.path.join(artifact_root, "plans")):
        base = os.path.join(artifact_root, "plans")
    else:
        ar = ".agent_reports" if os.path.isdir(os.path.join(jcwd, ".agent_reports")) else ".claude_reports"
        base = os.path.join(jcwd, ar, "plans")
    try:
        cand = sorted(d for d in os.listdir(base) if d.endswith("_" + slug))
    except OSError:
        cand = []
    if not cand:
        # slug mismatch fallback: pick the plan folder with max hyphen-token overlap
        stoks = set(t for t in slug.split("-") if t)
        try:
            dirs = [d for d in os.listdir(base)
                    if not d.startswith(".") and os.path.isdir(os.path.join(base, d))]
        except OSError:
            dirs = []
        best, bestn, bestm = None, 0, -1.0
        for d in dirs:
            if os.path.exists(os.path.join(base, d, "pipeline_summary.md")):
                continue                      # skip done folders (avoid generic-token false "done")
            dslug = d.split("_", 1)[-1] if "_" in d else d
            n = len(stoks & set(t for t in dslug.split("-") if t))
            try:
                mt = os.path.getmtime(os.path.join(base, d))
            except OSError:
                mt = 0.0
            if n > bestn or (n == bestn and n > 0 and mt > bestm):
                best, bestn, bestm = d, n, mt
        if not best or bestn == 0:
            return None
        cand = [best]
    return os.path.join(base, cand[-1])


def live_stage(jcwd, slug, fallback, capability=None, worker_role=None, artifact_root=None):
    """Derive plan→exec→test→done from plans/*_<slug>/ artifacts; fallback = argv key."""
    if not jcwd or not slug:
        return fallback
    pd = _find_plan_dir(jcwd, slug, fallback, capability, worker_role,
                        artifact_root=artifact_root)
    if not pd:
        return fallback
    if os.path.exists(os.path.join(pd, "pipeline_summary.md")):
        return "done"
    if _has_entries(os.path.join(pd, "test_logs")):
        return "test"
    if _has_entries(os.path.join(pd, "dev_logs")):
        return "exec"
    try:
        with open(os.path.join(pd, "plan", "checklist.md")) as f:
            if "[x]" in f.read().lower():
                return "exec"
    except OSError:
        pass
    if os.path.exists(os.path.join(pd, "plan", "plan.md")):
        return "plan"
    return "plan"


def resolve_plan_qa_artifact(job):
    """Read QA from one exact plan directory for QA only.

    Stage authority lives in WorkProjection. This resolver is intentionally
    exact-cardinality and never uses the old token-overlap/mtime selection.
    """
    def value(name, default=None):
        if isinstance(job, dict):
            return job.get(name, default)
        return getattr(job, name, default)

    slug = value("slug") or value("key")
    if not slug:
        return None
    roots = []
    for root in (value("artifact_root"), value("cwd")):
        if not root:
            continue
        root = os.path.realpath(os.path.expanduser(str(root)))
        roots.extend((root, os.path.join(root, ".agent_reports"),
                      os.path.join(root, ".claude_reports")))
    candidates = set()
    for root in roots:
        for path in glob.glob(os.path.join(root, "plans", "*_%s" % slug)):
            if os.path.isdir(path):
                candidates.add(os.path.realpath(path))
    if len(candidates) != 1:
        return None
    pd = next(iter(candidates))
    for relpath in ("pipeline_state.yaml", os.path.join("plan", "plan.md")):
        try:
            with open(os.path.join(pd, relpath), encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("qa_level:"):
                        return s.split(":", 1)[1].strip()
        except OSError:
            continue
    return None


def _plan_qa(jcwd, slug, key=None, capability=None, worker_role=None):
    """Compatibility wrapper; QA resolution is now exact and separately named."""
    return resolve_plan_qa_artifact({"cwd": jcwd, "slug": slug, "key": key})


_QA_DEFAULT = {
    "code": "thorough",
    "spec": "thorough",
    "research": "thorough",
    "draft": "thorough",
    "refine": "thorough",
    "lab": "light",
    "note": "light",
}


def effective_qa(argv_qa, pipe_qa, jcwd, slug, key, capability=None, worker_role=None,
                 artifact_root=None):
    """Layered qa resolver, first-hit precedence: argv > jobslog(pipe) > plan artifact >
    CONVENTIONS default. Returns (qa, source) — source in argv|jobslog|plan|default|None."""
    if argv_qa:
        return argv_qa, "argv"
    if pipe_qa:
        return pipe_qa, "jobslog"
    v = resolve_plan_qa_artifact({"cwd": jcwd, "slug": slug, "key": key,
                                  "artifact_root": artifact_root,
                                  "capability": capability, "worker_role": worker_role})
    if v:
        return v, "plan"
    v = _QA_DEFAULT.get(key)
    if v:
        return v, "default"
    return None, None


_STAGE_SUFFIX_RE = re.compile(r"-(?:code-)?(?:plan|exec|execute|test|report)$")


def _slug_stem(slug):
    """Strip a trailing dispatch-depth-2 stage-role suffix (F-15c dedup key): 'fleet-ui-v2-execute'
    -> 'fleet-ui-v2', 'x-code-plan' -> 'x', 'already' unchanged. Display/matching helper
    only — never mutates DispatchJob.slug itself."""
    if not slug:
        return slug
    return _STAGE_SUFFIX_RE.sub("", slug)


def _norm_cwd(p):
    """Normalize a cwd for cross-source string-equality matching (B1/B2, R6): the jobs.log
    `worktree` field is writer-verbatim (dispatch-headless.py:append_job — no
    canonicalization), while a process cwd resolved via `/proc/<pid>/cwd` is already
    symlink-canonical. Both match sides must pass through this SAME helper (realpath) so a
    symlinked or trailing-slash worktree still reconciles against the live process cwd."""
    return os.path.realpath(p)


def _live_claude_cwds(exclude_pids):
    """{normalized_cwd: pid} for live `claude -p` processes not already argv-matched.

    Targets tokenless headless dispatch (stdin-piped `claude -p < promptfile`, and
    session-limit `-p -c` resume — plan §5): argv carries no /autopilot- token, so
    `_scan_processes()` can never surface these as proc jobs. Gate: `comm == "claude"` AND
    an EXACT `-p` token in argv — token-equality via `args.split()` (`"-p" in args.split()`),
    never a substring test, so a `--print` long-form flag or an incidental "-p" inside the
    prompt body is rejected while interactive `claude --resume` sessions stay excluded too
    (R2). cwd is resolved via `os.readlink("/proc/<pid>/cwd")`, falling back to
    `sessions/<pid>.json`'s `cwd` field under the RUNTIME home (`_proj_home()`) if that
    fails (`_ps_lines()` carries no cwd column — procscan.py:53). Keys are normalized with
    `_norm_cwd` (os.path.realpath) so a symlinked/trailing-slash worktree still matches the
    jobs.log row (R6). Two live processes sharing one cwd → lowest pid wins, deterministic
    "earliest wins" (R3). Any per-process OS error is swallowed (skip that process); total
    scan failure (`_ps_lines()` → []) returns {}."""
    result = {}
    for line in procscan._ps_lines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        pid_s, comm = parts[0], parts[1]
        args = parts[3] if len(parts) > 3 else ""
        if comm != "claude":
            continue
        if "-p" not in args.split():
            continue
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if pid in exclude_pids:
            continue
        jcwd = None
        try:
            jcwd = os.readlink("/proc/%s/cwd" % pid_s)
            if jcwd.endswith(" (deleted)"):
                jcwd = jcwd[: -len(" (deleted)")]
        except OSError:
            try:
                with open(os.path.join(_proj_home(), "sessions", "%s.json" % pid_s)) as f:
                    jcwd = json.load(f).get("cwd")
            except Exception:
                jcwd = None
        if not jcwd:
            continue
        key = _norm_cwd(jcwd)
        if key not in result or pid < result[key]:
            result[key] = pid
    return result


def _drill_current_case_from_log(path):
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - 65536))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    matches = _DRILL_CASE_LINE.findall(data)
    if not matches:
        return None
    return matches[-1].strip().replace("growing:", "")


def _loop_current_case(args):
    for path in reversed(_DRILL_LOG_PATH.findall(args or "")):
        case_id = _drill_current_case_from_log(path)
        if case_id:
            return case_id
    return None


def _iso_to_epoch(ts):
    """ISO8601 jobs.log timestamp -> epoch seconds (float) | None. Sibling of
    `_iso_elapsed_min` (:806) but returns the raw instant instead of a pre-computed elapsed —
    route.py's `build_views` takes `now` as an argument (purity, §3.3), so the elapsed-minutes
    math for a route-node's `done` row happens there, not here."""
    try:
        dt = datetime.fromisoformat((ts or "").strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _scan_registry_evidence(paths):
    """Return terminal-surviving route evidence and an exact-attempt drain index.

    Both projections come from one registry snapshot. Route nodes keep their existing
    last-occurrence-wins behavior, while attempts are keyed by ``attempt_id`` rather than
    slug/cwd/PID. The second result contains only current, terminal registered stages;
    a still-live proc row for one of those exact attempts is a runtime drain, not a new
    stage row.

    The return shape is ``(route_nodes, terminal_registered_attempts)``.
    """
    result = {}
    latest_attempts = {}
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    for path in paths:
        path_result = {}
        path_attempts = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                rows = f.read().splitlines()
        except OSError:
            continue
        for registry_order, line in enumerate(rows):
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            ts, status, _repo, _worktree, slug, pipe = fields
            meta = _parse_pipe_meta(pipe or "")
            attempt_contract = _attempt_contract(meta)
            route_id = meta.get("route_id")
            route_node = meta.get("route_node")
            attempt_id = meta.get("attempt_id")
            if attempt_id:
                # Latest row for this exact attempt wins inside one registry, even if
                # the later row is non-terminal. This prevents an earlier terminal row
                # from hiding a subsequently reopened/malformed attempt.
                path_attempts[attempt_id] = {
                    "attempt_id": attempt_id,
                    "status": status,
                    "route_id": route_id,
                    "route_node": route_node,
                    "harness": _infer_harness(meta, slug),
                    "slug": slug,
                    "registry_order": registry_order,
                    **attempt_contract,
                }
            if not route_id or not route_node:
                continue
            pid_s = meta.get("pid")
            node_evidence = path_result.setdefault(route_id, {}).setdefault(
                route_node, {}
            )
            node_evidence.setdefault("attempt_history", []).append({
                "attempt_id": attempt_id,
                "status": status,
                "dispatch_depth": attempt_contract["dispatch_depth"],
                "transport": attempt_contract["transport"],
                "execution_surface": attempt_contract["execution_surface"],
                "registered_worker": attempt_contract["registered_worker"],
                "fallback_hop": attempt_contract["fallback_hop"],
                "contract_status": attempt_contract["attempt_contract_status"],
            })
            # route_file/route_hash/parent name WHERE the sealed record lives and who the
            # conductor is — raw registry fields, independent of whether this particular
            # attempt row's OWN dispatch-contract metadata validates as "current". Gating
            # them behind attempt-contract validity silently defeated the code-test
            # verification.md §10 fix for any legacy-schema terminal row: route.
            # resolve_records() found no route_file at all, so a fully-finished route with a
            # perfectly valid sealed record still degraded to an unresolved view.
            node_evidence.update({
                "route_file": meta.get("route_file"),
                "route_hash": meta.get("route_hash"),
                "parent": meta.get("parent") or meta.get("parent_slug"),
            })
            if attempt_contract["attempt_contract_status"] != "current":
                continue
            node_evidence.update({
                "status": status, "slug": slug, "ts": _iso_to_epoch(ts),
                "pid": int(pid_s) if (pid_s or "").isdigit() else None,
                "harness": _infer_harness(meta, slug),
                "model": meta.get("model"),
                "effort": meta.get("effort") or meta.get("reasoning"),
                "completion_gate": meta.get("completion_gate"),
                "note": meta.get("note"),
            })
        # Candidate paths are precedence-ordered (canonical first). Preserve
        # last-occurrence-wins inside each file, but never let a later legacy
        # registry overwrite canonical terminal evidence or attempt identity.
        for route_id, nodes in path_result.items():
            target = result.setdefault(route_id, {})
            for route_node, evidence in nodes.items():
                target.setdefault(route_node, evidence)
        for attempt_id, evidence in path_attempts.items():
            latest_attempts.setdefault(attempt_id, evidence)

    terminal_attempts = {
        attempt_id: evidence
        for attempt_id, evidence in latest_attempts.items()
        if evidence["status"] in _TERMINAL_REGISTRY_STATUSES
        and evidence["attempt_contract_status"] == "current"
        and evidence["transport"] == "headless"
        and evidence["execution_surface"] == "registered-headless"
        and evidence["registered_worker"] is True
        and evidence["dispatch_depth"] in (1, 2)
        and evidence["route_id"]
        and evidence["route_node"]
    }
    return result, terminal_attempts


def _scan_route_nodes(paths):
    """{route_id: {node_id: {...}}} — F-28a (§3.3): unlike `_scan_jobs_log`, this pass keeps
    TERMINAL rows (done/killed/cancelled). A route node that just finished has no live job row
    left (`_scan_jobs_log` drops terminal rows before classification, dispatch.py:845-846), so
    without this separate pass a completed node could never render `✓` (plan §3.3's "설계상 가장
    놓치기 쉬운 지점"). Rereads the same jobs.log files (§3.2.3 — `_jobs_log_fields` precedent,
    not a new I/O pattern); last-occurrence-wins per (route_id, route_node), same reconciliation
    idiom as `_scan_jobs_log`'s per-slug dedup."""
    return _scan_registry_evidence(paths)[0]


def _suppress_terminal_attempt_proc_jobs(proc_jobs, terminal_attempts):
    """Drop only duplicate proc jobs for exact terminal registered stages.

    The semantic stage remains in ``last_route_nodes``. Sessions are deliberately not
    accepted by this helper, so native subagents and unrelated same-cwd harness sessions
    cannot disappear through registry reconciliation.
    """
    kept = []
    for job in proc_jobs:
        evidence = terminal_attempts.get(getattr(job, "attempt_id", None))
        exact_stage = (
            getattr(job, "source", None) == "proc"
            and evidence is not None
            and getattr(job, "route_id", None) == evidence.get("route_id")
            and getattr(job, "route_node", None) == evidence.get("route_node")
            and (
                not evidence.get("harness")
                or not getattr(job, "harness", None)
                or job.harness == evidence["harness"]
            )
        )
        if not exact_stage:
            kept.append(job)
    return kept


# --- source (a): process scan (uncapped, no related() filter) ---
def _scan_processes():
    jobs = []
    seen = set()
    for line in procscan._ps_lines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        pid_s, _comm, etime = parts[0], parts[1], parts[2]
        args = parts[3] if len(parts) > 3 else ""
        ms = _AUTOPILOT.findall(args)
        loop = _LOOPS.search(args)
        env = procscan.read_environ(pid_s) if (ms or loop) else {}
        # Session-end distillers and Fleet title refreshers can inherit dispatch
        # metadata and even contain `/autopilot-*` as prompt data. They are support
        # workers, not dispatch jobs; procscan exposes them only through the dedicated
        # mem-worker path when `a` is enabled.
        if env.get("MEM_DISTILL") == "1" or env.get("FLEET_TITLE_REFRESH") == "1":
            continue
        if ms and "claude" in args:
            if os.path.basename(args.split(None, 1)[0]) in _SHELLS:
                continue                      # launcher shell wrapper, not the claude process
            try:
                jcwd = os.readlink("/proc/%s/cwd" % pid_s)
            except OSError:
                jcwd = ""
            if jcwd.endswith(" (deleted)"):
                jcwd = jcwd[: -len(" (deleted)")]
            key = ms[-1]
            env_capability_mode = env.get("AGENT_DISPATCH_CAPABILITY_MODE") or None
            env_worker_mode = env.get("AGENT_DISPATCH_WORKER_MODE") or None
            mode = (
                None
                if env_capability_mode or env_worker_mode
                else (_MODE.findall(args) or [None])[-1]
            )
            worker_type = env.get("AGENT_DISPATCH_WORKER_TYPE")
            unit = env.get("AGENT_DISPATCH_UNIT") or None
            capability_mode, worker_mode, mode_axis_conflict = _dispatch_mode_axes(
                {
                    "capability_mode": env_capability_mode
                    or (_CAPABILITY_MODE.findall(args) or [None])[-1],
                    "worker_mode": env_worker_mode
                    or (_WORKER_MODE.findall(args) or [None])[-1],
                    "mode": mode,
                    "worker_role": env.get("AGENT_DISPATCH_WORKER_ROLE"),
                },
                worker_type=worker_type,
                unit=unit,
            )
            qa_hits = [q for q in _QA.findall(args) if q in _QA_LEVELS]
            qa = qa_hits[-1] if qa_hits else None
            slug = os.path.basename(jcwd.rstrip("/")) if jcwd else ""
            dkey = "%s:%s" % (key, slug)
            if dkey in seen:
                continue
            seen.add(dkey)
            parent_sid = env.get("AGENT_DISPATCH_PARENT_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID")
            parent_slug = env.get("AGENT_DISPATCH_PARENT_SLUG")
            depth = _parse_depth(env.get("AGENT_DISPATCH_DEPTH"))
            attempt_contract = _attempt_contract({
                "attempt_schema_version": env.get(
                    "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION"
                ),
                "dispatch_depth": env.get("AGENT_DISPATCH_DEPTH"),
                "transport": env.get("AGENT_DISPATCH_TRANSPORT"),
                "execution_surface": env.get("AGENT_DISPATCH_EXECUTION_SURFACE"),
                "registered_worker": env.get("AGENT_DISPATCH_REGISTERED_WORKER"),
                "fallback_hop": env.get("AGENT_DISPATCH_FALLBACK_HOP"),
            })
            is_child = env.get("AGENT_SESSION_ROLE", "").lower() == "worker" or env.get("CLAUDE_CODE_CHILD_SESSION") == "1" or bool(parent_slug or parent_sid)
            q, qsrc = effective_qa(qa, None, jcwd, slug, key,
                                   artifact_root=env.get("AGENT_ARTIFACT_ROOT"))
            jobs.append(DispatchJob(
                key=key, stage=None, mode=mode,
                capability_mode=capability_mode, worker_mode=worker_mode,
                mode_axis_conflict=mode_axis_conflict, qa=q,
                elapsed_min=etime_to_min(etime), slug=slug, cwd=jcwd,
                parent_sid=parent_sid, parent_slug=parent_slug, is_child=is_child,
                qa_source=qsrc, source="proc", harness="claude",
                pid=int(pid_s) if pid_s.isdigit() else None,
                proc_start=procscan.read_proc_start(pid_s) if pid_s.isdigit() else None,
                model=_claude_job_model(pid_s, jcwd), depth=depth,
                **attempt_contract,
                intensity=env.get("AGENT_DISPATCH_INTENSITY"),
                worker_type=worker_type,
                assigned_contract=env.get("AGENT_DISPATCH_ASSIGNED_CONTRACT"),
                unit=unit,
                worker_role=env.get("AGENT_DISPATCH_WORKER_ROLE"),
                capability_owner=env.get("AGENT_DISPATCH_OWNER"),
                effort=env.get("AGENT_DISPATCH_EFFORT"),
                model_role=env.get("AGENT_DISPATCH_MODEL_ROLE"),
                route_file=env.get("AGENT_ROUTE_FILE") or None,
                route_id=env.get("AGENT_ROUTE_ID") or None,
                route_node=env.get("AGENT_ROUTE_NODE") or None,
                attempt_id=env.get("AGENT_DISPATCH_ATTEMPT_ID") or None,
                # AGENT_ROUTE_HASH is not exported by the headless launcher (§3.2.2) — a proc
                # job's route_hash stays None; integrity still rests on the record's own
                # recomputed hash (route.py P1), so this is not a weaker check.
            ))
        elif loop:
            key = loop.group(1)
            current_case = _loop_current_case(args)
            if current_case:
                dkey = "%s:%s" % (key, current_case)
            else:
                if any(k.startswith(key + ":") for k in seen):
                    continue
                dkey = key
            if dkey in seen:
                continue
            seen.add(dkey)
            try:
                jcwd = os.readlink("/proc/%s/cwd" % pid_s)
            except OSError:
                jcwd = ""
            if jcwd.endswith(" (deleted)"):
                jcwd = jcwd[: -len(" (deleted)")]
            parent_sid = env.get("AGENT_DISPATCH_PARENT_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID")
            parent_slug = env.get("AGENT_DISPATCH_PARENT_SLUG")
            parent_cwd = env.get("AGENT_DISPATCH_PARENT_CWD") or (env.get("PWD") if parent_sid else None)
            is_child = env.get("AGENT_SESSION_ROLE", "").lower() == "worker" or env.get("CLAUDE_CODE_CHILD_SESSION") == "1" or bool(parent_slug or parent_sid)
            jobs.append(DispatchJob(
                key=key, stage="running", mode="loop/%s" % key,
                elapsed_min=etime_to_min(etime), slug=current_case or key, cwd=jcwd,
                parent_sid=parent_sid, parent_cwd=parent_cwd, parent_slug=parent_slug,
                is_child=is_child, source="proc", harness="claude" if env.get("CLAUDECODE") or "claude" in args else None,
                pid=int(pid_s) if pid_s.isdigit() else None,
                proc_start=procscan.read_proc_start(pid_s) if pid_s.isdigit() else None,
                worker_role=current_case,
                capability_owner=key,
                effort=env.get("AGENT_DISPATCH_EFFORT"),
                model_role=env.get("AGENT_DISPATCH_MODEL_ROLE"),
                attempt_id=env.get("AGENT_DISPATCH_ATTEMPT_ID") or None,
            ))
    return jobs


# --- source (b): jobs.log tolerant merge ---
def _iso_elapsed_min(ts):
    try:
        dt = datetime.fromisoformat(ts.strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))


def _scan_jobs_log(path, seen_slugs, seen_keys=None, registry_priority=0):
    jobs = []
    malformed = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rows = f.read().splitlines()
    except OSError:
        return jobs, 0
    # Reconcile each job to its LATEST row before deciding live-ness. Identity key = slug,
    # NOT the worktree path: a terminal (done/killed/cancelled) row drops the worktree to '-'
    # after harvest, so a path key would never match the earlier running row and the job would
    # zombie forever at its running timestamp. A slug appears running→done chronologically
    # (append order), so last-occurrence wins. (Bug: an `open/running`-first filter let a later
    # `done` never cancel the running row — 290h phantom jobs. User report 2026-07-01.)
    latest = {}
    order = []
    for registry_order, line in enumerate(rows):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 6:
            malformed += 1
            continue
        slug = fields[4]
        if slug not in latest:
            order.append(slug)
        latest[slug] = (registry_order, fields)  # last occurrence = newest status for this slug
    for slug in order:
        registry_order, fields = latest[slug]
        ts, status, repo, worktree, _slug, pipe = fields
        row_age_min = _iso_elapsed_min(ts)
        afterglow = False
        if status not in ("open", "running"):
            # F-46: `done` within the afterglow window survives as a display-only row
            # (status stays the verbatim registry word `done`; `afterglow=True` is the
            # additive display marker — `--json` consumers see no changed key meaning).
            # Every other terminal word — and any `done` older than the window — is dropped
            # exactly as before, so the row self-clears on the next tick past the window.
            if status != "done" or row_age_min is None or row_age_min > DONE_AFTERGLOW_MIN:
                continue                      # newest state is terminal → not live
            afterglow = True
        cwd = worktree if worktree not in ("-", "(main-tree)") else ""
        if slug in seen_slugs:
            continue                          # already shown as a live process job
        # F-15c: (normalized cwd, slug-stem) dedup — a stage-worker registry row
        # (fleet-ui-v2-execute) reconciles against its already-shown proc job
        # (fleet-ui-v2) ONLY when both cwd and stem match; a different worktree (conductor
        # vs its own dispatch-depth-2 child, OPERATIONS §5.10) never collapses, so both stay visible.
        if cwd and seen_keys and (_norm_cwd(cwd), _slug_stem(slug)) in seen_keys:
            continue
        seen_slugs.add(slug)
        meta = _parse_pipe_meta(pipe or "")
        attempt_contract = _attempt_contract(meta)
        pname = meta.get("_name") or repo or "job"
        # _parse_pipe_meta already strips any `autopilot-` prefix on a successful parse; this
        # covers the fallback-name path (parse failure) where pname = repo or "job".
        if pname.startswith("autopilot-"):
            pname = pname[len("autopilot-"):]   # normalize to proc key form (code/spec/…)
        capability = meta.get("capability")
        worker_role = meta.get("worker_role")
        q, qsrc = effective_qa(None, meta.get("qa"), cwd, slug, pname, capability, worker_role,
                               artifact_root=meta.get("artifact_root"))
        parent_slug = meta.get("parent") or meta.get("parent_slug") or None
        parent_sid = meta.get("parent_sid") or meta.get("parent_session_id") or None
        parent_cwd = meta.get("parent_cwd") or meta.get("parent_worktree") or None
        parent_managed_dir = _managed_parent_dir(meta.get("managed_sidecar_log"))
        harness = _infer_harness(meta, slug)
        worker_type = meta.get("worker_type")
        unit = meta.get("unit") or None
        capability_mode, worker_mode, mode_axis_conflict = _dispatch_mode_axes(
            meta, worker_type=worker_type, unit=unit
        )
        pid_s = meta.get("pid", "")
        pid_local = int(pid_s) if pid_s.isdigit() else None
        pid_local_start = meta.get("pid_start") or meta.get("proc_start")
        registry_pid, registry_start, identity_source = _authoritative_registry_pid(meta)
        pid_host_s = meta.get("pid_host", "")
        pgid_s = meta.get("pgid", "")
        job = DispatchJob(
            key=pname, stage=status, mode=meta.get("mode"),
            capability_mode=capability_mode, worker_mode=worker_mode,
            mode_axis_conflict=mode_axis_conflict, qa=q,
            elapsed_min=row_age_min, slug=slug or worktree or repo,
            cwd=cwd, parent_sid=parent_sid, parent_cwd=parent_cwd,
            parent_managed_dir=parent_managed_dir,
            parent_slug=parent_slug,
            is_child=bool(parent_slug or parent_sid or parent_cwd or parent_managed_dir),
            qa_source=qsrc, source="jobs", status=status,
            harness=harness, model=meta.get("model"),
            pid=registry_pid, proc_start=registry_start,
            pid_scope=meta.get("pid_scope"),
            pid_local=pid_local, pid_local_start=pid_local_start,
            pid_host=int(pid_host_s) if pid_host_s.isdigit() else None,
            pid_host_start=meta.get("pid_host_start"),
            pid_host_ns=meta.get("pid_host_ns"), pid_ns=meta.get("pid_ns"),
            pid_observer_ns=meta.get("pid_observer_ns"),
            pid_host_proof=meta.get("pid_host_proof"),
            pgid=int(pgid_s) if pgid_s.isdigit() else None,
            pid_identity_source=identity_source,
            profile=meta.get("profile"),
            depth=_parse_depth(meta.get("dispatch_depth", meta.get("depth"))),
            **attempt_contract,
            intensity=meta.get("intensity"),
            worker_type=worker_type,
            assigned_contract=meta.get("assigned_contract"),
            unit=unit,
            worker_role=worker_role,
            capability_owner=meta.get("owner") or meta.get("capability_owner"),
            effort=meta.get("effort") or meta.get("reasoning") or meta.get("variant"),
            model_role=meta.get("model_role"),
            model_profile=meta.get("model_profile"), model_tier=meta.get("model_tier"),
            profile_granularity=meta.get("profile_granularity"),
            parallel_group=meta.get("parallel_group") or meta.get("replica_group"),
            replica_group=meta.get("replica_group"),
            perspective=meta.get("perspective") or meta.get("batch_perspective"),
            parallel_leg_index=_parse_optional_int(meta.get("batch_parallel_leg_index")),
            parallel_leg_count=_parse_optional_int(meta.get("batch_declared_size")),
            route_file=meta.get("route_file"), route_id=meta.get("route_id"),
            route_hash=meta.get("route_hash"), route_node=meta.get("route_node"),
            attempt_id=meta.get("attempt_id"),
            artifact_root=meta.get("artifact_root"),
            registry_order=registry_order,
            registry_priority=registry_priority,
            afterglow=afterglow,
        )
        job._log_file = meta.get("log_file")
        job._registry_path = path
        job._registry_metadata = dict(meta)
        jobs.append(job)
    quick_groups = {}
    for job in jobs:
        # F-46: an afterglow row is a finished attempt, not a live one — it must never make a
        # legitimate successor look like `quick-multiple-live`, and its own contract shape is
        # already settled history.
        if job.afterglow:
            continue
        if job.intensity != "quick" or not job.route_id or not job.route_node:
            continue
        key = (job.route_id, job.route_node)
        quick_groups.setdefault(key, []).append(job)
        violations = []
        if job.dispatch_depth != 1 or job.parent_slug:
            violations.append("quick-shape")
        if (
            job.execution_surface != "registered-headless"
            or job.registered_worker is not True
        ):
            violations.append("quick-surface")
        if job.fallback_hop != "same-harness-headless":
            violations.append("quick-fallback")
        if violations:
            job.attempt_contract_status = "invalid:" + ",".join(violations)
    for group in quick_groups.values():
        if len(group) > 1:
            for job in group:
                job.attempt_contract_status = "invalid:quick-multiple-live"
    return jobs, malformed


def _jobs_log_fields(paths):
    """{slug: metadata} from the latest jobs.log row per slug (last-occurrence-wins,
    mirrors the reconciliation in _scan_jobs_log). Tolerant: missing file / malformed rows
    (field count != 6) never raise — worst case an empty or partial map."""
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    fields_by_slug = {}
    for path in paths:
        path_fields = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                rows = f.read().splitlines()
        except OSError:
            continue
        for line in rows:
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            slug = fields[4]
            value = _parse_pipe_meta(fields[5] or "")
            value["_registry_path"] = path
            path_fields[slug] = value
        for slug, value in path_fields.items():
            if slug not in fields_by_slug:
                fields_by_slug[slug] = value         # first registry wins across files
    return fields_by_slug


def _reconcile_drill_rows(jobs, now=None, codex_index=None):
    """Merge duplicate registry and process rows for the same drill run.

    Keep the registry row, absorb the process PID and its liveness as tier-2 EVIDENCE,
    and never write the registry.

    F-25: this used to overwrite `r.liveness` directly, which made it a second, competing
    classifier. It now stashes the proc row's state as `_proc_liveness` evidence and the
    single classifier (model.classify_job) decides — same outcome, one decision point.
    """
    # Index registry drill rows by validated case.
    reg_by_case = {}
    reg_by_runner_pid = {}
    for r in jobs:
        if r.source != "jobs":
            continue
        case = _drill_case_from_slug(r.slug)
        if not case:
            continue
        if _drill_case_from_cwd(r.cwd) != case:      # Validate the cwd case component.
            continue
        reg_by_case.setdefault(case, r)              # First registry row is canonical.
        pid_match = re.search(r"-(\d+)$", r.slug or "")
        if pid_match:
            reg_by_runner_pid.setdefault(int(pid_match.group(1)), r)
    if not reg_by_case:
        return jobs
    drop = set()
    for p in jobs:
        if p.source != "proc" or p.key != "drill":
            continue
        case = _drill_case_from_cwd(p.cwd) or p.worker_role or (p.slug if p.slug != "drill" else None)
        r = reg_by_case.get(case)
        if r is None and p.pid is not None:
            # run.sh itself runs from the harness worktree, not the fixture cwd,
            # so it may have no case signal. Registry slugs end in run.sh's $$;
            # use that exact PID to collapse the proc/control row deterministically.
            r = reg_by_runner_pid.get(p.pid)
        if r is None:
            continue
        # Absorb process PID and liveness into the canonical registry row as evidence.
        if r.pid is None and r.pid_scope != "namespace-local":
            r.pid = p.pid
            r.proc_start = p.proc_start      # pid and its start-time travel together, always
        if r.elapsed_min is None:
            r.elapsed_min = p.elapsed_min
        # Reconciliation runs BEFORE the classify loop (so there is exactly one place a
        # liveness is assigned), which means a proc row normally has no state yet — derive
        # it here. A caller that already classified (or a test that pins one) is honored.
        pl = p.liveness
        if pl in (None, "unknown"):
            # track=False: this row is about to be dropped, so it must not occupy a
            # tracker slot (and its verdict is evidence, not a rendered state).
            pl = _dispatch_liveness(
                p, time.time() if now is None else now, track=False,
                codex_index=codex_index,
            )
        if pl in ("working", "idle"):
            r._proc_liveness = pl                    # tier-2 evidence; classify_job weighs it
        drop.add(id(p))
    if not drop:
        return jobs
    return [j for j in jobs if id(j) not in drop]


_ORPHAN_REGISTRY_MOD = None


def _orphan_registry_module():
    """Lazily load utilities/dispatch-registry.py in-process (SD-64/71).

    Reuses the exact same classify()/route_incomplete()/resume_boundary()
    primitives dispatch-liveness.sh and preflight.sh status call out to, so
    Fleet never re-derives the orphan classification on its own. In-process
    import (not a subprocess) since collect() runs on every Fleet tick.
    """
    global _ORPHAN_REGISTRY_MOD
    if _ORPHAN_REGISTRY_MOD is None:
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "utilities", "dispatch-registry.py")
        try:
            spec = importlib.util.spec_from_file_location("_fleet_dispatch_registry", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            mod = False
        _ORPHAN_REGISTRY_MOD = mod
    return _ORPHAN_REGISTRY_MOD or None


def _annotate_orphan_conductors(jobs, now, jobs_path=None):
    """SD-64/71: stamp note/resume_boundary on a dead dispatch-depth-1 owner row that is a
    detected orphan (route incomplete + a registered open child or a ready
    un-started successor). Read-only — never mutates the registry."""
    dead_owners = [j for j in jobs if j.liveness == "dead" and int(getattr(j, "depth", 1) or 1) == 1
                   and not getattr(j, "route_node", None)
                   and getattr(j, "attempt_id", None)]
    if not dead_owners:
        return
    registry = _orphan_registry_module()
    if registry is None:
        return
    paths = _candidate_jobs_paths(jobs_path)
    jobs_path = next((p for p in paths if p and os.path.isfile(p)), None)
    if not jobs_path:
        return
    try:
        from pathlib import Path as _Path
        rows = registry.read_rows(_Path(jobs_path))
    except Exception:
        return
    newest = {}
    for row in rows:
        key = (row["meta"].get("route_id"), row["meta"].get("route_node"))
        if all(key):
            newest[key] = row["order"]

    class _Args:
        pass

    args = _Args()
    args.agent_home = _Path(_registry_home())
    args.now = now
    for j in dead_owners:
        row = next((r for r in rows if r["meta"].get("attempt_id") == j.attempt_id), None)
        if row is None:
            continue
        try:
            _, _, note = registry.classify(row, args, newest, rows)
        except Exception:
            continue
        if note == "dead-parent-orphaned":
            try:
                _, route_file, _ = registry.resolve_owner_route(row, rows)
                incomplete, _ = registry.route_incomplete(row, args.agent_home, rows)
                boundary = registry.resume_boundary(route_file, incomplete)
            except Exception:
                boundary = None
            j.note = "dead-parent-orphaned"
            j.resume_boundary = boundary or "-"


def collect(jobs_path=None, harness_filter=None):
    """Return merged [DispatchJob]. harness_filter does not restrict dispatch — the section
    is cross-harness by design (jobs, not sessions)."""
    proc_jobs = _scan_processes()
    paths = _candidate_jobs_paths(jobs_path)
    try:
        route_nodes, terminal_attempts = _scan_registry_evidence(paths)
    except Exception:
        # Registry evidence is an enrichment surface. On read/parse failure keep the
        # process row visible rather than hiding it without exact terminal proof.
        route_nodes, terminal_attempts = {}, {}
    proc_jobs = _suppress_terminal_attempt_proc_jobs(proc_jobs, terminal_attempts)
    seen = set(j.slug for j in proc_jobs if j.slug)
    seen_keys = set((_norm_cwd(j.cwd), _slug_stem(j.slug)) for j in proc_jobs if j.cwd and j.slug)
    log_jobs = []
    malformed = 0
    for registry_priority, path in enumerate(paths):
        path_jobs, path_malformed = _scan_jobs_log(
            path, seen, seen_keys, registry_priority=registry_priority
        )
        log_jobs.extend(path_jobs)
        malformed += path_malformed
    jobs = proc_jobs + log_jobs
    # Typed-mode+profile backfill for proc jobs whose argv/env omitted metadata.
    # Legacy mode backfill is read-only; profile=None backfill IS spec §7-mandated —
    # a proc-scanned profile job has no argv signal for --profile at all).
    if any(
        j.mode is None or j.capability_mode is None
        or j.worker_mode is None or j.profile is None
        or (j.attempt_id and not getattr(j, "_log_file", None))
        for j in proc_jobs
    ):
        log_fields = _jobs_log_fields(paths)
        for j in proc_jobs:
            if j.slug:
                metadata = log_fields.get(j.slug, {})
                if j.mode is None:
                    j.mode = metadata.get("mode")
                if j.capability_mode is None:
                    j.capability_mode = metadata.get("capability_mode")
                if j.worker_mode is None:
                    j.worker_mode = metadata.get("worker_mode")
                if j.profile is None:
                    j.profile = metadata.get("profile")
                if (j.attempt_id and metadata.get("attempt_id") == j.attempt_id
                        and not getattr(j, "_log_file", None)):
                    j._log_file = metadata.get("log_file")
                    j._registry_path = metadata.get("_registry_path")
                    if not j.artifact_root:
                        j.artifact_root = metadata.get("artifact_root")
                cap_mode, worker_mode, conflict = _dispatch_mode_axes(
                    {
                        "capability_mode": j.capability_mode,
                        "worker_mode": j.worker_mode,
                        "mode": j.mode,
                        "worker_role": j.worker_role,
                    },
                    worker_type=j.worker_type,
                    unit=j.unit,
                )
                j.capability_mode = cap_mode
                j.worker_mode = worker_mode
                j.mode_axis_conflict = j.mode_axis_conflict or conflict
    # cwd-fallback enrichment for tokenless headless dispatch (stdin-piped `claude -p`,
    # `-p -c` resume — plan Phase B): these jobs.log rows have harness=None because their
    # argv carries no /autopilot- token, so _scan_processes() never argv-matched them.
    # Additive only — enriches log_jobs whose harness is still None (disjoint from the
    # mode+profile backfill above, which touches proc_jobs), so already-argv-matched proc
    # jobs are never affected. Order-independent w.r.t. the liveness loop below (j.cwd /
    # j.profile, which liveness reads, are unchanged by this block).
    # Guard the extra `ps` spawn: only scan for live processes when there is at least one
    # unenriched log job to match (fleet re-collects every ~2s, so skipping the second `ps`
    # when nothing needs it saves a subprocess per tick in the common all-argv-matched case).
    candidates = [
        j for j in log_jobs
        if j.harness is None and j.cwd and not j.attempt_id
    ]
    if candidates:
        exclude = {j.pid for j in proc_jobs if j.pid}
        cwd_index = _live_claude_cwds(exclude)
        consumed = set()
        for j in candidates:
            pid = cwd_index.get(_norm_cwd(j.cwd))
            if pid and pid not in consumed:
                j.harness = "claude"
                j.pid = pid
                j.proc_start = procscan.read_proc_start(pid)   # identity, not just a number
                consumed.add(pid)
                j.model = _claude_job_model(str(pid), j.cwd)
                j.stage = None
    now = time.time()
    try:
        codex_index = _build_codex_rollout_index(jobs)
    except Exception:
        codex_index = None
    # F-18a correlation merges proc evidence onto canonical registry rows BEFORE
    # classification, so every row is decided exactly once, by the single classifier.
    jobs = _reconcile_drill_rows(jobs, now, codex_index=codex_index)
    for j in jobs:
        _enrich_claude_stream_session(j)
        _enrich_codex_attempt_session(j)
        _enrich_opencode_attempt_session(j)
        _enrich_attempt_summary(j)
    for j in jobs:
        j.liveness = _dispatch_liveness(j, now, codex_index=codex_index)
    _annotate_orphan_conductors(jobs, now, jobs_path=jobs_path)
    # F-15c(a): a registry-only row (source="jobs") that turns out to be genuinely working
    # re-derives its breadcrumb from the real plan artifacts instead of the raw jobs.log
    # status word ("open"/"running") — otherwise a live job with real progress shows a
    # Avoid leaving a static queued/running placeholder forever.
    for j in jobs:
        if j.source == "jobs" and j.cwd and j.liveness == "working":
            j.stage = None
    # stash malformed count on the module for the render header (optional signal)
    collect.last_malformed = malformed
    # F-28a (§3.3) — terminal route-node evidence, stashed the same way `last_malformed` is
    # (module attribute, not a return-signature change — every existing caller stays untouched).
    collect.last_route_nodes = route_nodes
    collect.last_terminal_attempts = terminal_attempts
    collect.last_degradations = _scan_degradations(set(route_nodes) | {getattr(j, "route_id", None) for j in jobs if getattr(j, "route_id", None)})
    return jobs


collect.last_malformed = 0
collect.last_route_nodes = {}
collect.last_terminal_attempts = {}
collect.last_degradations = {}
