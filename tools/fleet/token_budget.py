"""Portable token/context telemetry primitives.

This module keeps *active context* separate from cumulative session counters.
The distinction matters because legacy ``Session.tokens`` has different meanings
across collectors and is therefore not a valid policy signal.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CODEX_BASELINE_TOKENS = 12_000
DEFAULT_TIGHT_PCT = 70
DEFAULT_CRITICAL_PCT = 85
DEFAULT_MAX_AGE_SECONDS = 86_400

DIRECTIVE_IDS = {
    "tight": "tight-v1",
    "critical": "critical-v1",
}
DIRECTIVE_TEXTS = {
    "tight-v1": (
        "TOKEN_BUDGET=tight: concise output; defer optional extras only. "
        "Keep required work, intensity/dispatch, tools, tests, safety, and input context unchanged."
    ),
    "critical-v1": (
        "TOKEN_BUDGET=critical: minimal concise output; defer optional extras only. "
        "Keep required work, intensity/dispatch, tools, tests, safety, and input context unchanged."
    ),
}

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


def _counter(value) -> Optional[int]:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def _policy_tokens(value):
    """Native policy counts are exact non-negative integers; bool/float are not evidence."""
    return value if type(value) is int and value >= 0 else None


def context_policy_snapshot(*, source, runtime_version=None, configured_request_tokens=None,
                            configured_request=None,
                            effective_window_tokens=None, applied_trigger_tokens=None,
                            output_allowance_tokens=None, compaction_reserve_tokens=None,
                            disabled=None, enabled=None, enforced=None, session_id=None,
                            attempt_id=None, raw_percentage=None, reason=None):
    """Keep a runtime's request, resolved limit, applied trigger, and reserve distinct."""
    requested = _policy_tokens(configured_request_tokens)
    effective = _policy_tokens(effective_window_tokens)
    trigger = _policy_tokens(applied_trigger_tokens)
    output = _policy_tokens(output_allowance_tokens)
    reserve = _policy_tokens(compaction_reserve_tokens)
    if disabled is True or enabled is False or enforced is False:
        state = "disabled"
    elif enabled is True and enforced is True and trigger is not None:
        state = "applied"
    else:
        state = "unknown"
        if reason is None:
            reason = "native-policy-not-observed"
    if state == "applied" and trigger is None:
        state = "unknown"
        reason = reason or "invalid-applied-trigger"
    return {
        "state": state,
        "source": str(source or "unknown"),
        "runtime_version": runtime_version if isinstance(runtime_version, str) else None,
        "configured_request": (configured_request[:128] if isinstance(configured_request, str)
                               else None),
        "configured_request_tokens": requested,
        "effective_window_tokens": effective,
        "applied_trigger_tokens": trigger,
        "output_allowance_tokens": output,
        "compaction_reserve_tokens": reserve,
        "disabled": True if state == "disabled" else False if state == "applied" else None,
        "enabled": enabled if type(enabled) is bool else None,
        "enforced": enforced if type(enforced) is bool else None,
        "raw_percentage": (raw_percentage if isinstance(raw_percentage, (int, float))
                           and not isinstance(raw_percentage, bool)
                           and 0 <= raw_percentage <= 100 else None),
        "session_id": session_id if isinstance(session_id, str) and session_id else None,
        "attempt_id": attempt_id if isinstance(attempt_id, str) and attempt_id else None,
        "reason": reason if state != "applied" else None,
    }


def context_policy_used_pct(active_context_tokens, policy):
    """Percent of the exact applied trigger; unknown/disabled and zero trigger stay undefined."""
    active = _policy_tokens(active_context_tokens)
    trigger = _policy_tokens(policy.get("applied_trigger_tokens")) if isinstance(policy, dict) else None
    if (active is None or trigger is None or trigger == 0
            or policy.get("state") != "applied"):
        return None
    if active >= trigger:
        return 100
    return min(99, (200 * active + trigger) // (2 * trigger))


@dataclass
class TokenTelemetry:
    """Normalized read-only session telemetry; ``None`` means not exposed."""

    adapter: str = "portable"
    status: str = "unknown"
    reason: Optional[str] = None
    session_id: Optional[str] = None
    signal_source: Optional[str] = None
    signal_age_seconds: Optional[int] = None
    active_context_tokens: Optional[int] = None
    compaction_pressure: Optional[dict] = None
    context_window_tokens: Optional[int] = None
    context_used_pct: Optional[int] = None
    session_input_tokens: Optional[int] = None
    session_cached_input_tokens: Optional[int] = None
    session_output_tokens: Optional[int] = None
    session_reasoning_output_tokens: Optional[int] = None
    session_total_tokens: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def codex_context_used_pct(active_context_tokens, context_window_tokens) -> Optional[int]:
    """Mirror Codex's 12k reserve formula used by the native TUI."""

    active = _counter(active_context_tokens)
    window = _counter(context_window_tokens)
    if active is None or window is None or window <= CODEX_BASELINE_TOKENS:
        return None
    effective_window = window - CODEX_BASELINE_TOKENS
    used = max(0, active - CODEX_BASELINE_TOKENS)
    return min(99, round(100.0 * used / effective_window))


def policy_band(context_used_pct, tight_pct=DEFAULT_TIGHT_PCT,
                critical_pct=DEFAULT_CRITICAL_PCT) -> str:
    """Return ``unknown|normal|tight|critical`` for a normalized context %."""

    if not isinstance(tight_pct, int) or not isinstance(critical_pct, int):
        raise ValueError("thresholds must be integers")
    if not 0 < tight_pct < critical_pct <= 100:
        raise ValueError("thresholds must satisfy 0 < tight < critical <= 100")
    if not isinstance(context_used_pct, (int, float)):
        return "unknown"
    if context_used_pct >= critical_pct:
        return "critical"
    if context_used_pct >= tight_pct:
        return "tight"
    return "normal"


def directive_for_band(band: str) -> tuple[Optional[str], str]:
    """Return the frozen directive id/text pair for a pressure band."""

    directive_id = DIRECTIVE_IDS.get(band)
    return directive_id, DIRECTIVE_TEXTS.get(directive_id, "")


def telemetry_from_explicit(*, adapter="portable", session_id=None,
                            active_context_tokens=None, context_window_tokens=None,
                            session_input_tokens=None, session_cached_input_tokens=None,
                            session_output_tokens=None,
                            session_reasoning_output_tokens=None,
                            session_total_tokens=None) -> TokenTelemetry:
    active = _counter(active_context_tokens)
    window = _counter(context_window_tokens)
    telemetry = TokenTelemetry(
        adapter=adapter,
        status="observed" if active is not None and window is not None else "unknown",
        reason=None if active is not None and window is not None else "missing-context-signal",
        session_id=session_id,
        signal_source="explicit",
        active_context_tokens=active,
        context_window_tokens=window,
        session_input_tokens=_counter(session_input_tokens),
        session_cached_input_tokens=_counter(session_cached_input_tokens),
        session_output_tokens=_counter(session_output_tokens),
        session_reasoning_output_tokens=_counter(session_reasoning_output_tokens),
        session_total_tokens=_counter(session_total_tokens),
    )
    if adapter == "codex":
        telemetry.context_used_pct = codex_context_used_pct(active, window)
    elif active is not None and window:
        telemetry.context_used_pct = min(99, round(100.0 * active / window))
    if telemetry.context_used_pct is None:
        telemetry.status = "unknown"
        telemetry.reason = "invalid-context-window"
    return telemetry


def parse_codex_token_count(line: str, *, session_id=None,
                            signal_age_seconds=None) -> TokenTelemetry:
    """Parse one Codex ``token_count`` JSONL event without exposing content."""

    unknown = TokenTelemetry(adapter="codex", session_id=session_id,
                             signal_source="codex-rollout")
    try:
        event = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        unknown.reason = "malformed-token-count"
        return unknown
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        unknown.reason = "not-token-count"
        return unknown
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        unknown.reason = "missing-token-info"
        return unknown
    last = info.get("last_token_usage")
    total = info.get("total_token_usage")
    if last is not None and not isinstance(last, dict):
        unknown.reason = "malformed-token-usage"
        return unknown
    if total is not None and not isinstance(total, dict):
        unknown.reason = "malformed-token-usage"
        return unknown
    last = last if isinstance(last, dict) else {}
    total = total if isinstance(total, dict) else {}
    telemetry = telemetry_from_explicit(
        adapter="codex",
        session_id=session_id,
        active_context_tokens=last.get("total_tokens"),
        context_window_tokens=info.get("model_context_window"),
        session_input_tokens=total.get("input_tokens"),
        session_cached_input_tokens=total.get("cached_input_tokens"),
        session_output_tokens=total.get("output_tokens"),
        session_reasoning_output_tokens=total.get("reasoning_output_tokens"),
        session_total_tokens=total.get("total_tokens"),
    )
    telemetry.signal_source = "codex-rollout"
    telemetry.signal_age_seconds = _counter(signal_age_seconds)
    return telemetry


def tail_codex_token_count(path: Path, chunk=262_144) -> Optional[str]:
    """Return the last token-count event from a bounded rollout tail."""

    try:
        size = path.stat().st_size
        start = max(0, size - chunk)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = data.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if (isinstance(payload, dict) and payload.get("type") == "token_count"
                and isinstance(payload.get("info"), dict)):
            return line
    return None


def codex_event_epoch(line: str) -> Optional[float]:
    """Return a token-count event timestamp, or ``None`` for legacy rows."""

    try:
        event = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    value = event.get("timestamp") if isinstance(event, dict) else None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def find_codex_rollout(codex_home: Path, session_id: str) -> tuple[Optional[Path], str]:
    """Find exactly one rollout whose filename ends with the exact session id."""

    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None, "invalid-session-id"
    root = codex_home / "sessions"
    try:
        candidates = [
            path for path in root.rglob("rollout-*.jsonl")
            if path.name.endswith("-" + session_id + ".jsonl")
        ]
    except OSError:
        return None, "session-store-unavailable"
    if not candidates:
        return None, "session-not-found"
    if len(candidates) != 1:
        return None, "ambiguous-session"
    return candidates[0], "ok"


def telemetry_from_codex_session(session_id: str, *, codex_home=None,
                                 max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
                                 now=None) -> TokenTelemetry:
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path, reason = find_codex_rollout(home, session_id)
    if path is None:
        return TokenTelemetry(adapter="codex", session_id=session_id,
                              signal_source="codex-rollout", reason=reason)
    now = time.time() if now is None else now
    try:
        file_age = max(0, int(now - path.stat().st_mtime))
    except OSError:
        return TokenTelemetry(adapter="codex", session_id=session_id,
                              signal_source="codex-rollout", reason="rollout-unreadable")
    line = tail_codex_token_count(path)
    if line is None:
        return TokenTelemetry(adapter="codex", session_id=session_id,
                              signal_source="codex-rollout", signal_age_seconds=file_age,
                              reason="token-count-not-found")
    event_epoch = codex_event_epoch(line)
    age = max(0, int(now - event_epoch)) if event_epoch is not None else file_age
    if max_age_seconds is not None and age > max_age_seconds:
        return TokenTelemetry(adapter="codex", session_id=session_id,
                              signal_source="codex-rollout", signal_age_seconds=age,
                              reason="stale-signal")
    telemetry = parse_codex_token_count(line, session_id=session_id,
                                       signal_age_seconds=age)
    telemetry.compaction_pressure = codex_compaction_pressure(
        session_id, path, line, home=home, now=now)
    return telemetry

def _unknown_compaction_pressure(session_id, reason):
    return {"state": "unknown", "source": "codex-internal-log-v2",
            "session_id": session_id, "reason": reason,
            "scope_tokens": None, "limit_tokens": None, "scope": None,
            "observed_at": None, "usage_at": None, "compact_at": None,
            "token_limit_reached": None, "fresh_until": None}


def current_compaction_pressure(pressure, *, now=None):
    """A display clock can expire a sample without collecting or changing policy."""
    now = time.time() if now is None else now
    if not isinstance(pressure, Mapping) or pressure.get("state") != "observed":
        return False
    if any(type(pressure.get(key)) not in (int, float) or not math.isfinite(pressure[key])
           for key in ("usage_at", "observed_at", "fresh_until")):
        return False
    return (type(pressure.get("scope_tokens")) is int and pressure["scope_tokens"] >= 0
            and type(pressure.get("limit_tokens")) is int and pressure["limit_tokens"] > 0
            and pressure.get("scope") in {"Total", "BodyAfterPrefix"}
            and type(pressure.get("token_limit_reached")) is bool
            and pressure["usage_at"] <= pressure["observed_at"] <= pressure["usage_at"] + 2
            and pressure["usage_at"] <= now + 2 < pressure["fresh_until"])


def compaction_imminent(pressure, *, now=None):
    """Only a complete current native judgment warrants the short warning."""
    return current_compaction_pressure(pressure, now=now) and pressure.get("token_limit_reached") is True


def context_display_detail(entity):
    """One numeric display record; never a policy or input authorization."""
    get = entity.get if isinstance(entity, Mapping) else lambda key: getattr(entity, key, None)
    pressure = get("compaction_pressure")
    if get("liveness") in {"dead", "stale"}:
        return None
    if not isinstance(pressure, Mapping):
        return None
    return {"server_total_tokens": get("active_context_tokens"),
            "server_window_tokens": get("context_window_tokens"),
            "pressure": dict(pressure)}


def codex_pressure_paths(home):
    """Existing event sources only; no new collector or persistent record."""
    database = Path(home) / "logs_2.sqlite"
    return database, Path(str(database) + "-wal")


def codex_compaction_pressure(session_id, rollout, token_line, *, home=None,
                             now=None, max_age_seconds=900):
    """Bound an internal diagnostic to this SID, usage point and compact interval.

    Codex 0.163's public ThreadTokenUsage reports last/total/window, not the
    auto-compaction estimate. This private fallback never changes that usage,
    infers missing values, or supplies any identity/readiness authority.
    """
    import sqlite3
    import stat

    unknown = lambda reason: _unknown_compaction_pressure(session_id, reason)
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return unknown("invalid-session-id")
    usage_at = codex_event_epoch(token_line)
    if usage_at is None or not math.isfinite(usage_at):
        return unknown("missing-usage-time")
    now = time.time() if now is None else now
    if not math.isfinite(now) or usage_at > now + 2 or now - usage_at > max_age_seconds:
        return unknown("stale-usage-time")
    if not isinstance(rollout, (str, os.PathLike)):
        return unknown("rollout-unavailable")
    path = Path(rollout)
    home = Path(home) if home else next((p.parent for p in path.parents if p.name == "sessions"), None)
    if home is None:
        return unknown("runtime-home-unavailable")
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    try:
        initial = path.stat()
        with path.open("rb") as handle:
            head = handle.read(64 * 1024).splitlines()
            start = max(0, initial.st_size - 262_144)
            handle.seek(start)
            if start:
                handle.readline()
            tail = handle.read(262_144).splitlines()
        if signature(initial) != signature(path.stat()):
            return unknown("rollout-changed")
        meta = None
        for raw in head:
            if b'"session_meta"' not in raw:
                continue
            event = json.loads(raw)
            if not isinstance(event, dict):
                return unknown("malformed-session-meta")
            if event.get("type") == "session_meta":
                meta = event.get("payload")
                break
        if not isinstance(meta, dict) or meta.get("id") != session_id:
            return unknown("session-mismatch")
        # Internal log semantics are verified for this release, not a public API.
        if meta.get("cli_version") != "0.163.0":
            return unknown("unsupported-log-version")
        latest, compact_at = None, None
        for raw in reversed(tail):
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                if b'"token_count"' in raw or b'"compacted"' in raw:
                    return unknown("malformed-usage-boundary")
                continue
            if not isinstance(event, dict):
                if b'"token_count"' in raw or b'"compacted"' in raw:
                    return unknown("malformed-usage-boundary")
                continue
            if event.get("type") == "compacted":
                compact_at = codex_event_epoch(raw)
                # A boundary without a time cannot certify its side of usage.
                if compact_at is None or not math.isfinite(compact_at):
                    return unknown("unknown-compact-time")
                break
            payload = event.get("payload")
            if latest is None and isinstance(payload, dict) and payload.get("type") == "token_count":
                latest = event
        supplied = json.loads(token_line)
        if latest != supplied:
            return unknown("usage-not-current")
        info = (latest.get("payload") or {}).get("info")
        usage = info.get("last_token_usage") if isinstance(info, dict) else None
        if not isinstance(usage, dict) or _policy_tokens(usage.get("total_tokens")) is None:
            return unknown("missing-server-usage")
        if compact_at is not None and compact_at >= usage_at:
            return unknown("usage-before-compact")
    except (OSError, ValueError, TypeError, StopIteration):
        return unknown("rollout-unavailable")
    database, _wal = codex_pressure_paths(home)
    connection = None
    try:
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return unknown("logs-unavailable")
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=.03)
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + .03
        steps = 0
        def stop():
            nonlocal steps
            steps += 1
            return int(steps >= 100 or time.monotonic() >= deadline)
        connection.set_progress_handler(stop, 1000)
        # The existing thread/time index bounds work. Limit rows and each body;
        # only this request's post-sampling judgment may match, never an older one.
        rows = connection.execute(
            "SELECT ts, ts_nanos, length(CAST(feedback_log_body AS BLOB)), "
            "substr(CAST(feedback_log_body AS BLOB),1,8193) FROM logs "
            "WHERE thread_id=? AND ts>=? AND ts<=? AND target=? "
            "AND feedback_log_body LIKE '%auto_compact_scope_tokens=%' "
            "ORDER BY ts DESC,ts_nanos DESC LIMIT 8",
            (session_id, int(usage_at), int(usage_at + 2), "codex_core::session::turn")).fetchall()
        if signature(initial) != signature(path.stat()):
            return unknown("rollout-changed")
        candidates = []
        for ts, ns, body_size, body in rows:
            if type(ts) is not int or type(ns) is not int or not 0 <= ns < 1_000_000_000:
                return unknown("unsupported-log-format")
            point = ts + ns / 1_000_000_000
            if not usage_at <= point <= min(now + 2, usage_at + 2):
                continue
            # A readable prefix is not a complete diagnostic. The extra byte and
            # original byte length detect truncation, including multibyte text.
            if (type(body_size) is not int or body_size > 8192
                    or not isinstance(body, bytes) or len(body) != body_size):
                return unknown("partial-log-body")
            candidates.append((point, body.decode("utf-8")))
        if len(candidates) != 1:
            return unknown("missing-or-ambiguous-judgment")
        observed_at, body = candidates[0]
        fields = {}
        for key in ("auto_compact_scope_tokens", "auto_compact_scope_limit",
                    "auto_compact_limit_scope", "token_limit_reached"):
            matches = re.findall(r"\b" + key + r"=([^\s]+)", body or "")
            if len(matches) != 1:
                return unknown("unsupported-log-format")
            fields[key] = matches[0]
        tokens = fields["auto_compact_scope_tokens"]
        limit = re.fullmatch(r"Some\(([0-9]+)\)", fields["auto_compact_scope_limit"])
        scope = fields["auto_compact_limit_scope"]
        reached = fields["token_limit_reached"]
        if (not tokens.isascii() or not tokens.isdigit() or len(tokens) > 18
                or not limit or len(limit[1]) > 18 or int(limit[1]) <= 0
                or scope not in {"Total", "BodyAfterPrefix"} or reached not in {"true", "false"}
                or reached == "false" and int(tokens) >= int(limit[1])):
            return unknown("unsupported-log-format")
        return {"state": "observed", "source": "codex-internal-log-v2", "session_id": session_id,
                "reason": None, "scope_tokens": int(tokens), "limit_tokens": int(limit[1]),
                "scope": scope, "observed_at": observed_at, "usage_at": usage_at,
                "compact_at": compact_at, "token_limit_reached": reached == "true",
                "fresh_until": usage_at + max_age_seconds}
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return unknown("logs-unavailable")
    finally:
        if connection is not None:
            connection.close()
