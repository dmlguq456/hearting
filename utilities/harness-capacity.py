#!/usr/bin/env python3
"""Deterministic capacity signals and quality-band selection.

Capacity never changes the quality policy: it only orders peers inside a band,
or promotes an explicitly declared relief band below its configured threshold.
Every networked gauge shares Fleet's last-good usage cache (write-through on
success, read-back on failure) so a transient probe failure keeps the last true
reading instead of becoming unknown.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
import os
from pathlib import Path
import sys
import time
import urllib.request


HARNESSES = ("claude", "codex", "opencode")


def _limited(state: str) -> bool:
    return state == "limited" or state.startswith("limited(")


def _manual_scores() -> dict[str, float]:
    raw = os.environ.get("HARNESS_CAPACITY_SCORES", "")
    scores = {}
    for cell in raw.split(","):
        name, sep, value = cell.strip().partition(":")
        if not sep or name not in HARNESSES:
            continue
        try:
            score = float(value)
        except ValueError:
            continue
        if 0 <= score <= 100:
            scores[name] = score
    return scores


def _headroom(values) -> float | None:
    used = [float(value) for value in values if isinstance(value, (int, float))]
    if not used:
        return None
    return max(0.0, min(100.0, 100.0 - max(used)))


def _claude_capacity_reading(now: float, stale_after: int) -> tuple[float | None, dict | None]:
    """Read Claude headroom and conservative file-freshness context.

    A single session file only ever reports the windows it happened to touch,
    so picking one "winning" file (the old behaviour) could report a 51%
    headroom window as the account's answer while a different, equally fresh
    session file showed the same window at 1%. Instead, take each window
    key's readings across non-stale files. A tap write is not a new usage
    observation: within one unexpired reset window the highest reading remains
    authoritative over a lower value written again. Legacy taps without a reset
    retain their historical mtime ordering; they prove no window transition.
    """
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = []
    try:
        files = sorted(
            (path for path in (home / ".statusline").glob("*.json") if path.is_file()),
            key=lambda path: (-path.stat().st_mtime, path.name),
        )
    except OSError:
        return None, None
    latest_used: dict[str, float] = {}
    latest_mtime: dict[str, float] = {}
    window_used: dict[tuple[str, float], float] = {}
    contributing_mtimes: list[float] = []
    from dispatch_capacity_evidence import _scope_candidates
    current_scopes = _scope_candidates("claude")
    for path in files[:12]:
        try:
            mtime = path.stat().st_mtime
            if now - mtime > stale_after:
                break
            if mtime > now:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        bound = payload.get("quota_scope")
        if bound is not None and bound != current_scopes.get(payload.get("quota_scope_kind")):
            continue
        limits = payload.get("rate_limits") or {}
        if not isinstance(limits, dict):
            continue
        for key, row in limits.items():
            if not isinstance(row, dict):
                continue
            used = row.get("used_percentage")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                continue
            reset = row.get("resets_at")
            if reset is not None:
                try:
                    if isinstance(reset, bool):
                        continue
                    expires = (float(reset) if isinstance(reset, (int, float)) else
                               datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
                    if not math.isfinite(expires) or expires <= now:
                        continue
                except (TypeError, ValueError, AttributeError, OverflowError):
                    continue
                window = (key, expires)
                window_used[window] = max(window_used.get(window, used), used)
                contributing_mtimes.append(mtime)
                continue
            if key not in latest_mtime or mtime > latest_mtime[key]:
                latest_mtime[key] = mtime
                latest_used[key] = used
                contributing_mtimes.append(mtime)
    if not latest_used and not window_used:
        return None, None
    score = _headroom([*latest_used.values(), *window_used.values()])
    if score is None or not contributing_mtimes:
        return score, None
    return score, {
        "basis": "file-mtime",
        "age_seconds": round(max(0.0, now - min(contributing_mtimes)), 1),
        "stale_after_seconds": stale_after,
        "observation_time": "unknown",
    }


def _claude_score(now: float, stale_after: int) -> float | None:
    """Return Claude headroom while preserving the existing score API."""
    return _claude_capacity_reading(now, stale_after)[0]


# Why the last live probe produced no reading, per harness; `_codex_probe` /
# `_opencode_probe` turn it into the `unknown:<reason>` source a receipt prints.
_LAST_PROBE_REASON: dict[str, str] = {}


def _fleet_collectors():
    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))


def _usage_cache():
    """Fleet's last-good usage cache (`~/.local/state/agent-fleet/usage`), the one
    store every reader shares: Fleet ticks, the statusline's stale-driven refresh,
    and each dispatch-time probe all write it; placement reads it when the live
    probe fails. None when the Fleet package is unavailable."""
    _fleet_collectors()
    try:
        from fleet.collectors import usage_cache
    except Exception:
        return None
    return usage_cache


def _fleet_windows_headroom(payload) -> float | None:
    windows = (payload or {}).get("windows") if isinstance(payload, dict) else None
    if not isinstance(windows, list):
        return None
    return _headroom(row[1] for row in windows if isinstance(row, (list, tuple)) and len(row) >= 2)


def _codex_api_score() -> float | None:
    """Live account headroom via the codex TUI's own `/wham/usage` endpoint.

    Rollout samples update only when a codex session runs, so a rollout-only
    reader self-reinforces starvation: an idle codex degrades to unknown,
    capacity-aware placement stops selecting it, and the gauge never refreshes
    (observed 2026-08-13). One HTTP client, imported from
    `tools/fleet/collectors/codex.py` rather than reimplemented (the two readers
    drifted apart once). A missing/unreadable `auth.json` returns None before
    any network I/O, which keeps hermetic fixtures (temp `CODEX_HOME`) offline.
    A successful reading is written through to the shared usage cache.
    """
    _fleet_collectors()
    try:
        from fleet.collectors import codex as fleet_codex
    except Exception as exc:
        _LAST_PROBE_REASON["codex"] = f"collector-unavailable:{type(exc).__name__}"
        return None
    payload, reason = fleet_codex._api_usage_detail()
    if payload is None:
        _LAST_PROBE_REASON["codex"] = reason or "probe-failed"
        return None
    cache = _usage_cache()
    if cache is not None:
        cache.record("codex", payload)
    _LAST_PROBE_REASON.pop("codex", None)
    return _fleet_windows_headroom(payload)


def _cached_score(harness: str, now: float, stale_after: int) -> tuple[float | None, str | None]:
    """Last-good reading from the shared cache, if younger than `stale_after`."""
    cache = _usage_cache()
    if cache is None:
        return None, None
    snapshot = cache.read(harness, now=now, stale_max=stale_after)
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or not payload:
        return None, None
    if harness == "opencode":
        used = [row[1] for row in (payload.get("rl_windows") or [])
                if isinstance(row, (list, tuple)) and len(row) >= 2]
        score = _headroom(used)
    else:
        score = _fleet_windows_headroom(payload)
    if score is None:
        return None, None
    age = int(max(0.0, now - float(snapshot.get("observed_at") or now)))
    return score, f"cache:{age}s"


def _codex_probe(now: float, stale_after: int) -> tuple[float | None, str]:
    """(headroom, source). Order: live probe → shared cache (≤ stale_after) →
    own rollout samples (≤ stale_after) → unknown with the live probe's reason.
    A transient backend failure at compose time therefore reuses the most recent
    true reading instead of turning into `unknown` (2026-09-16 10:36)."""
    score = _codex_api_score()
    if score is not None:
        return score, "live"
    reason = _LAST_PROBE_REASON.get("codex", "probe-failed")
    score, source = _cached_score("codex", now, stale_after)
    if score is not None:
        return score, f"{source};live={reason}"
    score = _codex_score(now, stale_after)
    if score is not None:
        return score, f"rollout;live={reason}"
    return None, f"unknown:{reason}"


def _codex_score(now: float, stale_after: int) -> float | None:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        files = sorted(
            (path for path in (home / "sessions").rglob("*.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in files[:12]:
        try:
            if now - path.stat().st_mtime > stale_after:
                break
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 262_144))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                payload = json.loads(line).get("payload") or {}
            except (ValueError, AttributeError):
                continue
            limits = payload.get("rate_limits") or {}
            score = _headroom(
                row.get("used_percent")
                for row in limits.values()
                if isinstance(row, dict)
            )
            if score is not None:
                return score
    return None


def _opencode_probe(now: float, stale_after: int) -> tuple[float | None, str]:
    score = _opencode_api_score()
    if score is not None:
        return score, "live"
    reason = _LAST_PROBE_REASON.get("opencode", "probe-failed")
    score, source = _cached_score("opencode", now, stale_after)
    if score is not None:
        return score, f"{source};live={reason}"
    return None, f"unknown:{reason}"


def _opencode_api_score() -> float | None:
    """Live Go-plan headroom via the same reader the Fleet gauge uses.

    Until 2026-09-10 this slot was hardcoded to `None` behind the comment "no
    supported proactive API". That stopped being true on 2026-09-04, when the
    public `GET /zen/go/v1/usage` endpoint shipped and
    `tools/fleet/collectors/zen_go_usage.py` started reading it with the
    user's own inference key; the capacity probe simply never caught up, so
    opencode stayed permanently unknown to placement while the dashboard two
    panes over showed its rolling window at 100%.

    Imported, not reimplemented: a second HTTP client for the same number is
    how the two codex readers drifted apart before. A missing key, a missing
    collector, or any transient failure returns None (unknown), never a
    guess. The collector owns its own 3-second timeout and TTL cache.
    """

    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        from fleet.collectors import zen_go_usage
    except Exception as exc:
        _LAST_PROBE_REASON["opencode"] = f"collector-unavailable:{type(exc).__name__}"
        return None
    try:
        usage = zen_go_usage.account_usage()
    except Exception as exc:
        _LAST_PROBE_REASON["opencode"] = f"probe-raised:{type(exc).__name__}"
        return None
    if not isinstance(usage, dict) or "error" in usage:
        _LAST_PROBE_REASON["opencode"] = (
            f"probe-error:{usage.get('error')}" if isinstance(usage, dict) else "probe-failed")
        return None
    cache = _usage_cache()
    if cache is not None:
        cache.record("opencode", usage)
    _LAST_PROBE_REASON.pop("opencode", None)
    # Every window the account exposes counts, exactly as the Claude reader
    # merges its own: the tightest one decides the headroom. `weekly` here is
    # a Monday-anchored window, not a rolling 7d, but for "how much room is
    # left right now" that distinction does not change the answer.
    used = [row[1] for row in (usage.get("rl_windows") or [])
            if isinstance(row, (list, tuple)) and len(row) >= 2]
    return _headroom(used)


def capacity_report(*, stale_after: int = 3600, now: float | None = None) -> dict[str, dict]:
    """Headroom percentages plus where each one came from.

    ``scores``: unknown is ``None`` and never invented. ``sources``: ``manual``,
    ``live``, ``cache:<age>s;live=<reason>``, ``rollout;live=<reason>``,
    ``taps``, ``active-limit`` or ``unknown:<reason>`` — the receipt prints it so
    an unknown gauge says why (timeout, http-503, no-auth) instead of nothing.
    ``freshness`` optionally describes admitted Claude tap file mtimes; an mtime
    does not establish when usage was observed.
    """
    now = time.time() if now is None else now
    manual = _manual_scores()
    scores: dict[str, float | None] = {}
    sources: dict[str, str] = {}
    freshness: dict[str, dict] = {}
    if "codex" in manual:
        scores["codex"], sources["codex"] = manual["codex"], "manual"
    else:
        # Active probe first: the rollout gauge only refreshes while codex runs,
        # so on its own it starves an idle harness into permanent unknown.
        scores["codex"], sources["codex"] = _codex_probe(now, stale_after)
    if "claude" in manual:
        scores["claude"], sources["claude"] = manual["claude"], "manual"
    else:
        claude, claude_freshness = _claude_capacity_reading(now, stale_after)
        scores["claude"], sources["claude"] = claude, ("taps" if claude is not None else "unknown:no-fresh-taps")
        if claude is not None and claude_freshness is not None:
            freshness["claude"] = claude_freshness
    if "opencode" in manual:
        scores["opencode"], sources["opencode"] = manual["opencode"], "manual"
    else:
        scores["opencode"], sources["opencode"] = _opencode_probe(now, stale_after)

    from dispatch_capacity_evidence import active_limits
    jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    if jobs:
        for harness in active_limits(jobs, now=now):
            scores[harness] = 0.0
            sources[harness] = "active-limit"
            freshness.pop(harness, None)
    return {"scores": scores, "sources": sources, "freshness": freshness}


def capacity_scores(*, stale_after: int = 3600, now: float | None = None) -> dict[str, float | None]:
    """Return headroom percentages; unknown is ``None`` and never invented."""
    return capacity_report(stale_after=stale_after, now=now)["scores"]


def refresh_gauges(now: float | None = None) -> dict[str, dict]:
    """Probe every networked gauge once and leave the result in the shared
    cache: a hit is written through by the probe itself, a miss bumps
    `attempted_at` so stale-driven refreshers (the Claude statusline tick, Fleet)
    back off instead of retrying every tick. Local-only readers (claude taps)
    are reported, not probed."""
    report = capacity_report(now=now)
    cache = _usage_cache()
    if cache is not None:
        for harness in ("codex", "opencode"):
            if report["scores"].get(harness) is None or not report["sources"][harness].startswith(("live", "manual")):
                cache.record_attempt(harness, now=now)
    return report


ORDERING_NEUTRAL_SCORE = 50.0


def preferred_for_depth(allocation, depth):
    """Configured depth preference, or None when an explicit env bias overrides it."""
    bias = os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower()
    if bias in HARNESSES:
        return None
    affinity = allocation.get("depth_affinity") or {}
    return affinity.get("owner" if depth == 1 else "worker")


def affinity_margin(affinity_weight=0.5):
    """Headroom points a preferred harness may trail the best candidate by.

    `(weight - 0.5) * 200`: 0 at the neutral 0.5, 30 at the shipped 0.65, 100 at
    a pin-like 1.0. A weight below 0.5 yields a negative margin and therefore
    never reorders; it does not invert into a demotion.
    """
    return round((float(affinity_weight) - 0.5) * 200.0, 9)


def within_affinity_margin(scores, preferred, candidates, *, affinity_weight=0.5):
    """The single `capacity-aware` affinity oracle, shared by every consumer.

    True when `preferred` may be hoisted inside its own already-eligible class.
    Neutral weight, no preference, and a preferred harness outside `candidates`
    are all False. An unknown or non-positive gauge is False as well, which is
    `rank_band`'s capacity-aware exclusion, not a new rule: a harness that branch
    would never rank cannot be hoisted by a preference either.

    `dispatch-batch.py` scores whole combinations rather than a ranked band, so
    it cannot reuse `rank_band`'s reorder; it asks this predicate instead, and
    both consumers therefore share one margin definition.
    """
    if preferred is None or float(affinity_weight) == 0.5 or preferred not in candidates:
        return False
    value = scores.get(preferred)
    if value is None or float(value) <= 0:
        return False
    known = [
        float(scores[name]) for name in candidates
        if scores.get(name) is not None and float(scores[name]) > 0
    ]
    if not known:
        return False
    return round(max(known) - float(value), 9) <= affinity_margin(affinity_weight)


def ordering_score(scores, harness, neutral=ORDERING_NEUTRAL_SCORE):
    """Order candidates that checked evidence has ALREADY proven eligible.

    This is deliberately not `rank_band`'s question and must never become a
    gate. `rank_band` answers *eligibility*: an absent or stale gauge is
    excluded, hardened after the 2026-08-10 incident in which treating unknown
    as a neutral 50 redirected owners onto a user-exhausted harness.
    `ordering_score` answers *ordering among candidates a checked dispatch tuple
    already authorizes*, so an unknown gauge is neutral rather than
    disqualifying: OpenCode exposes no proactive gauge by design (see
    `capacity_scores`) and both primary gauges can be stale, so gating here
    would fail whole batches instead of ordering them.

    The numeric behaviour is exactly the value this replaces in
    dispatch-batch.py's assignment score; changing it is a separate,
    evidence-requiring decision and is out of scope for this repair.
    """
    value = scores.get(harness)
    return float(neutral) if value is None else float(value)


def allocation_deficit(scores, counts, candidates, *, neutral=ORDERING_NEUTRAL_SCORE,
                       preferred=None, affinity_weight=0.5, headroom_exponent=1,
                       harness_weights=None):
    """Blend remaining headroom and round-robin balance into one continuous key.

    Headroom sets each candidate's *target share* of the next attempt; the
    recent-attempt count says how much of that share it already consumed.
    Equal headroom collapses the formula to exact round-robin, so the
    2026-08-13 balanced-first policy survives as the equal-gauge special case
    rather than being replaced. A widening gauge gap moves the ranking
    continuously — deliberately no second threshold, because the defect this
    repairs (2026-08-20: 58%-headroom claude beating 99%-headroom codex) was
    caused by leaving ordering to a single step decision.

    An unknown gauge takes the same neutral share `ordering_score` uses: it is
    not exclusion here (see `rank_band`'s capacity-aware branch for the
    exclusion semantics that must NOT be harmonised with this).

    `preferred`/`affinity_weight` apply the configured depth affinity as a
    multiplier on the target share (`weight` for the preferred candidate,
    `(1-weight)/(n-1)` for each other); a neutral weight, an absent preference
    or a single candidate leaves every multiplier at 1.0, so equal headroom
    still reduces to exact round-robin. `headroom_exponent` sharpens the share
    without moving any ordering boundary. `harness_weights` (optional
    `allocation.harness_weights`, e.g. a slow harness at 0.3) multiplies each
    named candidate's target share; an absent or empty mapping leaves the
    formula exactly as it was.
    """
    n = len(candidates)
    neutral_affinity = (
        preferred is None or preferred not in candidates
        or affinity_weight == 0.5 or n == 1
    )
    multipliers = {
        name: (
            1.0 if neutral_affinity else
            affinity_weight if name == preferred else (1.0 - affinity_weight) / (n - 1)
        ) for name in candidates
    }
    shares = {
        name: (float(neutral) if scores.get(name) is None else max(0.0, float(scores[name])))
        ** headroom_exponent * multipliers[name] * float((harness_weights or {}).get(name, 1.0))
        for name in candidates
    }
    total = sum(shares.values())
    consumed = {name: int(counts.get(name, 0)) for name in candidates}
    if total <= 0:
        # Every gauge reads exactly zero: no share information survives, so
        # fall back to pure round-robin instead of dividing by zero.
        return {name: -float(value) for name, value in consumed.items()}
    horizon = sum(consumed.values()) + 1
    return {
        name: shares[name] / total * horizon - consumed[name]
        for name in candidates
    }


def gate_cutoff(usage_gate_used_percent=90):
    """Headroom cutoff below which a `balanced` candidate is gated.

    Scores are headroom (`100 - used`), so the cutoff is the complement of the
    configured used-percent threshold.
    """
    return 100.0 - float(usage_gate_used_percent)


def is_gated(scores, harness, *, usage_gate_used_percent=90):
    """Is `harness` gated under the `balanced` cross-band usage gate?

    A known score at or below `gate_cutoff` is gated. An absent/unknown score
    is optimistically ungated — the same unknown-is-not-exclusion reading
    `ordering_score` and `allocation_deficit` already use for `balanced`
    ordering, deliberately distinct from `rank_band`'s capacity-aware
    eligibility branch, which excludes unknown gauges instead.
    """
    value = scores.get(harness)
    return value is not None and float(value) <= gate_cutoff(usage_gate_used_percent)


def window_release_epoch(harness, *, usage_gate_used_percent=100, now=None, stale_after=3600):
    """The last reset among `harness`'s cached windows at or above the gate, else None.

    The cache-reading half of `gate_release_epoch`, exported so the hard
    eligibility reader judges the same rows at 100% instead of parsing them a
    second time. Codex `windows` and opencode `rl_windows` carry
    `[label, used, reset_epoch]`; a window at the gate without a future reset
    makes the answer unknown (a time is never guessed), and a cache older than
    `stale_after` carries no answer at all.
    """
    cache = _usage_cache()
    if cache is None:
        return None
    now = time.time() if now is None else now
    payload = cache.read(harness, now=now, stale_max=stale_after).get("payload")
    return _payload_release_epoch(payload, harness, usage_gate_used_percent, now)


def _payload_release_epoch(payload, harness, usage_gate_used_percent, now):
    """Judge windows from one snapshot without reading the cache again."""
    rows = payload.get("rl_windows" if harness == "opencode" else "windows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    release = None
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        used, reset = row[1], row[2]
        if isinstance(used, bool) or not isinstance(used, (int, float)) or used < float(usage_gate_used_percent):
            continue
        if (isinstance(reset, bool) or not isinstance(reset, (int, float))
                or not math.isfinite(reset) or reset <= now):
            return None
        release = reset if release is None else max(release, reset)
    return release


def gauge_limit_epoch(harness, *, now=None, stale_after=3600):
    """When the shared gauge itself says `harness` is out of room, else None.

    Hard-eligibility counterpart of `gate_release_epoch`: the same cached
    windows, at 100% used instead of the soft gate, with a future reset. A
    manual `HARNESS_CAPACITY_SCORES` entry replaces the gauge exactly as it does
    in `capacity_report`, and Claude has no gauge windows here (its limits stay
    native). No window, a past reset, or a cache older than `stale_after`
    stays unknown. Codex usable credits in that same fresh snapshot lift only
    this gauge hold; headroom and the soft gate still use the original windows.
    """
    if harness not in ("codex", "opencode") or harness in _manual_scores():
        return None
    cache = _usage_cache()
    if cache is None:
        return None
    now = time.time() if now is None else now
    payload = cache.read(harness, now=now, stale_max=stale_after).get("payload")
    if harness == "codex":
        from fleet.collectors.codex import credits_usable
        if credits_usable(payload):
            return None
    return _payload_release_epoch(payload, harness, 100, now)


def gate_release_epoch(harness, source, *, usage_gate_used_percent=90, now=None, stale_after=3600):
    """When `harness` leaves the `balanced` usage gate, if its gauge says so; else None.

    The gate holds while a usage window is at or above the gate and lifts when the
    last such window resets. Only a score read from the shared usage cache (`live`
    writes through to it, `cache:<age>s` reads it) has those windows: codex
    `windows` and opencode `rl_windows`, rows `[label, used, reset_epoch]`. A
    window at the gate without a future reset makes the answer unknown; a time is
    never guessed.
    """
    if not isinstance(source, str) or not source.startswith(("live", "cache:")):
        return None
    return window_release_epoch(harness, usage_gate_used_percent=usage_gate_used_percent,
                                now=now, stale_after=stale_after)


def rank_band(candidates, states, counts, declared_order, scores, *, strategy="capacity-aware", usage_gate_used_percent=90,
              preferred=None, affinity_weight=0.5, headroom_exponent=1, harness_weights=None):
    """Rank eligible quality peers by headroom, then recent attempts and config order.

    Answers *eligibility*, not ordering: see `ordering_score` for the batch's
    separate ordering-only term over candidates already proven eligible here.
    Do not harmonise the two — an unknown gauge is exclusion here and neutral
    there, deliberately.

    In capacity-aware ordering, the preferred harness may move to the front
    only within a headroom margin of `(affinity_weight - 0.5) * 200`; a neutral
    weight performs no reorder.

    Within `balanced`, the general (not-all-gated) branch is the one exception:
    it already sorts by `counts`/`declared_order`, i.e. it lives on the
    ordering side of that divide, not the eligibility side. `allocation_deficit`
    blends fresh headroom into that existing ordering key rather than opening a
    new gate — a widening headroom gap must move the ranking continuously
    instead of only at the `usage_gate_used_percent` cutoff. The `all_gated`
    branch keeps its own documented contract (max fresh headroom breaks the
    tie when every candidate is gated — `core/OPERATIONS.md` SD-16) untouched,
    and `capacity-aware` below keeps unknown-gauge exclusion untouched too.
    """
    if strategy == "balanced":
        candidates = [
            name for name in candidates
            if not _limited(states.get(name, "unknown"))
        ]
        order = {name: index for index, name in enumerate(declared_order)}
        all_gated = bool(candidates) and all(
            is_gated(scores, name, usage_gate_used_percent=usage_gate_used_percent)
            for name in candidates
        )
        if all_gated:
            key = lambda name: (-float(scores[name]), int(counts.get(name, 0)), order.get(name, len(order)))
        else:
            deficit = allocation_deficit(scores, counts, candidates, preferred=preferred,
                                         affinity_weight=affinity_weight,
                                         headroom_exponent=headroom_exponent,
                                         harness_weights=harness_weights)
            key = lambda name: (
                1 if is_gated(scores, name, usage_gate_used_percent=usage_gate_used_percent) else 0,
                # Rounded so two identical inputs can never diverge on float
                # noise; declared_order stays the deterministic final tiebreak.
                round(-deficit[name], 9),
                order.get(name, len(order)),
            )
        ranked = sorted(candidates, key=key)
        bias = os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower()
        if bias in ranked:
            if all_gated:
                # Single gate class: no boundary to protect, free reorder as before.
                ranked = [bias] + [name for name in ranked if name != bias]
            else:
                # Balanced-only constraint: bias may reorder within its own gate
                # class but must never lift a gated harness above an ungated one
                # (or vice versa) — that would defeat the usage gate.
                gated_of = lambda name: is_gated(
                    scores, name, usage_gate_used_percent=usage_gate_used_percent
                )
                bias_gated = gated_of(bias)
                bias_group = [name for name in ranked if gated_of(name) == bias_gated]
                other_group = [name for name in ranked if gated_of(name) != bias_gated]
                reordered_bias_group = [bias] + [name for name in bias_group if name != bias]
                ranked = (
                    (other_group + reordered_bias_group)
                    if bias_gated
                    else (reordered_bias_group + other_group)
                )
        return ranked

    # Automatic recovery needs positive evidence of fresh headroom.  Treating
    # an absent/stale gauge as a neutral 50 silently redirected owners to a
    # user-exhausted harness during the 2026-08-10 incident.
    candidates = [
        name
        for name in candidates
        if not _limited(states.get(name, "unknown"))
        and scores.get(name) is not None
        and float(scores[name]) > 0
    ]
    order = {name: index for index, name in enumerate(declared_order)}
    ranked = sorted(
        candidates,
        key=lambda name: (
            -float(scores[name]),
            int(counts.get(name, 0)),
            order.get(name, len(order)),
        ),
    )
    # Configured depth affinity, confined to this band and this eligibility
    # class. The margin is the only condition, deliberately: DP-24 forbids a new
    # soft threshold, and `counts` already breaks the ordinary sort above.
    if within_affinity_margin(scores, preferred, ranked, affinity_weight=affinity_weight):
        ranked = [preferred] + [name for name in ranked if name != preferred]
    bias = os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower()
    if bias in ranked:
        ranked = [bias] + [name for name in ranked if name != bias]
    return ranked


def ordered_candidates(ranks, band_order, scores, *, strategy="capacity-aware", usage_gate_used_percent=90):
    """Flatten `ranks` across `band_order` into the one gate-first candidate order.

    This is the single realization of the B-1 cross-band gate (policy rule 3):
    while any ungated candidate exists, no gated candidate may precede it,
    regardless of quality band. Non-`balanced` strategies pass through
    unchanged — `rank_band` never gates them.

    Gated candidates are demoted, never dropped, so they remain reachable as
    fallback hops. When every candidate is gated, maximum headroom is compared
    across all bands; Python's stable sort preserves the existing band/rank
    order for equal-headroom ties.
    """
    flat = [(band, name) for band in band_order for name in ranks[band]]
    if strategy != "balanced":
        return flat
    ungated = [pair for pair in flat if not is_gated(scores, pair[1], usage_gate_used_percent=usage_gate_used_percent)]
    gated = [pair for pair in flat if is_gated(scores, pair[1], usage_gate_used_percent=usage_gate_used_percent)]
    if flat and len(gated) == len(flat):
        return sorted(flat, key=lambda pair: -float(scores[pair[1]]))
    return ungated + gated


def select(policy, states, counts, declared_order, scores, *, strategy="capacity-aware", usage_gate_used_percent=90,
           preferred=None, affinity_weight=0.5, headroom_exponent=1, harness_weights=None,
           preference_order=None):
    """Select one harness without allowing capacity to erase quality boundaries."""
    ranks = {
        band: rank_band(
            policy.get(band, []), states, counts, declared_order, scores,
            strategy=strategy, usage_gate_used_percent=usage_gate_used_percent,
            preferred=preferred, affinity_weight=affinity_weight,
            headroom_exponent=headroom_exponent, harness_weights=harness_weights,
        )
        for band in ("primary", "relief", "last_resort")
    }
    # A caller may supply an owner-only order. Eligibility was already checked
    # above; the existing cross-band ordering below still owns usage gates,
    # quality bands and all-gated recovery. An explicit env bias retains priority.
    if preference_order and os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower() not in HARNESSES:
        order = {name: i for i, name in enumerate(preference_order)}
        for band, rows in ranks.items():
            ranks[band] = sorted(rows, key=lambda name: order.get(name, len(order)))
    threshold = int(policy.get("promote_relief_below", 0))
    primary_headroom = [scores.get(name) for name in ranks["primary"]]
    primary_headroom = [value for value in primary_headroom if value is not None]
    promote = bool(
        ranks["relief"]
        and threshold > 0
        and primary_headroom
        and len(primary_headroom) == len(ranks["primary"])
        and max(primary_headroom) <= threshold
    )
    band_order = ("relief", "primary", "last_resort") if promote else (
        "primary", "relief", "last_resort"
    )
    flat = ordered_candidates(
        ranks, band_order, scores, strategy=strategy, usage_gate_used_percent=usage_gate_used_percent,
    )
    if flat:
        band, name = flat[0]
        return name, band, ranks, promote
    return None, None, ranks, promote


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="harness capacity gauge")
    parser.add_argument("--refresh", action="store_true",
                        help="probe the networked gauges once and update the shared usage cache")
    parser.add_argument("--json", action="store_true", help="print scores and sources")
    args = parser.parse_args()
    if args.refresh:
        report = refresh_gauges()
        print(json.dumps(report, sort_keys=True))
    elif args.json:
        print(json.dumps(capacity_report(), sort_keys=True))
    else:
        print(json.dumps(capacity_scores(), sort_keys=True))
