#!/usr/bin/env python3
"""Deterministic capacity signals and quality-band selection.

Capacity never changes the quality policy: it only orders peers inside a band,
or promotes an explicitly declared relief band below its configured threshold.
OpenCode intentionally has no proactive gauge until its runtime exposes one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
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


def _claude_score(now: float, stale_after: int) -> float | None:
    """Account-wide headroom: merge every non-stale session's windows.

    A single session file only ever reports the windows it happened to touch,
    so picking one "winning" file (the old behaviour) could report a 51%
    headroom window as the account's answer while a different, equally fresh
    session file showed the same window at 1%. Instead, take each window
    key's most-recently-observed `used_percentage` across all non-stale
    files, then combine those per-window values into one headroom the same
    way a single file always did.
    """
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = []
    try:
        files = sorted(
            (path for path in (home / ".statusline").glob("*.json") if path.is_file()),
            key=lambda path: (-path.stat().st_mtime, path.name),
        )
    except OSError:
        return None
    latest_used: dict[str, float] = {}
    latest_mtime: dict[str, float] = {}
    for path in files[:12]:
        try:
            mtime = path.stat().st_mtime
            if now - mtime > stale_after:
                break
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        limits = payload.get("rate_limits") or {}
        for key, row in limits.items():
            if not isinstance(row, dict):
                continue
            used = row.get("used_percentage")
            if not isinstance(used, (int, float)):
                continue
            if key not in latest_mtime or mtime > latest_mtime[key]:
                latest_mtime[key] = mtime
                latest_used[key] = used
    if not latest_used:
        return None
    return _headroom(latest_used.values())


def _codex_api_score() -> float | None:
    """Live account headroom via the codex TUI's own `/wham/usage` endpoint.

    Rollout samples update only when a codex session runs, so a rollout-only
    reader self-reinforces starvation: an idle codex degrades to unknown,
    capacity-aware placement stops selecting it, and the gauge never refreshes
    (observed 2026-08-13). The fleet collector already proved this active probe
    (`tools/fleet/collectors/codex.py account_usage`); the two readers must not
    drift apart again. A missing/unreadable `auth.json` returns None before any
    network I/O, which keeps hermetic fixtures (temp `CODEX_HOME`) offline.
    """
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        tokens = json.loads((home / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}
    except (OSError, ValueError, AttributeError):
        return None
    token = tokens.get("access_token")
    if not token:
        return None
    request = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={
            "Authorization": "Bearer " + token,
            "chatgpt-account-id": tokens.get("account_id") or "",
            "User-Agent": "codex-cli",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
    except Exception:
        return None
    limits = (payload if isinstance(payload, dict) else {}).get("rate_limit") or {}
    return _headroom(
        (limits.get(window) or {}).get("used_percent")
        for window in ("primary_window", "secondary_window")
        if isinstance(limits.get(window), dict)
    )


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


def capacity_scores(*, stale_after: int = 3600, now: float | None = None) -> dict[str, float | None]:
    """Return headroom percentages; unknown is ``None`` and never invented."""
    now = time.time() if now is None else now
    manual = _manual_scores()
    if "codex" in manual:
        codex = manual["codex"]
    else:
        # Active probe first: the rollout gauge only refreshes while codex runs,
        # so on its own it starves an idle harness into permanent unknown.
        codex = _codex_api_score()
        if codex is None:
            codex = _codex_score(now, stale_after)
    return {
        "claude": manual.get("claude", _claude_score(now, stale_after)),
        "codex": codex,
        # No supported proactive API. Exhaustion still arrives through usage-check.
        "opencode": manual.get("opencode"),
    }


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
                       preferred=None, affinity_weight=0.5, headroom_exponent=1):
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
    without moving any ordering boundary.
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
        ** headroom_exponent * multipliers[name]
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


def rank_band(candidates, states, counts, declared_order, scores, *, strategy="capacity-aware", usage_gate_used_percent=90,
              preferred=None, affinity_weight=0.5, headroom_exponent=1):
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
                                         headroom_exponent=headroom_exponent)
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
           preferred=None, affinity_weight=0.5, headroom_exponent=1):
    """Select one harness without allowing capacity to erase quality boundaries."""
    ranks = {
        band: rank_band(
            policy.get(band, []), states, counts, declared_order, scores,
            strategy=strategy, usage_gate_used_percent=usage_gate_used_percent,
            preferred=preferred, affinity_weight=affinity_weight,
            headroom_exponent=headroom_exponent,
        )
        for band in ("primary", "relief", "last_resort")
    }
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
    print(json.dumps(capacity_scores(), sort_keys=True))
