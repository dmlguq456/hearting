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
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = []
    try:
        files = sorted(
            (path for path in (home / ".statusline").glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in files[:12]:
        try:
            if now - path.stat().st_mtime > stale_after:
                break
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        limits = payload.get("rate_limits") or {}
        score = _headroom(
            row.get("used_percentage")
            for row in limits.values()
            if isinstance(row, dict)
        )
        if score is not None:
            return score
    return None


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


def rank_band(candidates, states, counts, declared_order, scores, *, strategy="capacity-aware", usage_gate_used_percent=90):
    """Rank eligible quality peers by headroom, then recent attempts and config order.

    Answers *eligibility*, not ordering: see `ordering_score` for the batch's
    separate ordering-only term over candidates already proven eligible here.
    Do not harmonise the two — an unknown gauge is exclusion here and neutral
    there, deliberately.
    """
    if strategy == "balanced":
        cutoff = 100.0 - float(usage_gate_used_percent)
        candidates = [
            name for name in candidates
            if not _limited(states.get(name, "unknown"))
        ]
        order = {name: index for index, name in enumerate(declared_order)}
        known = [scores.get(name) is not None for name in candidates]
        all_gated = bool(candidates) and all(
            value is not None and float(value) <= cutoff
            for value in (scores.get(name) for name in candidates)
        )
        if all_gated:
            key = lambda name: (-float(scores[name]), int(counts.get(name, 0)), order.get(name, len(order)))
        else:
            key = lambda name: (
                1 if scores.get(name) is not None and float(scores[name]) <= cutoff else 0,
                int(counts.get(name, 0)),
                order.get(name, len(order)),
            )
        ranked = sorted(candidates, key=key)
        bias = os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower()
        if bias in ranked:
            ranked = [bias] + [name for name in ranked if name != bias]
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
    bias = os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower()
    if bias in ranked:
        ranked = [bias] + [name for name in ranked if name != bias]
    return ranked


def select(policy, states, counts, declared_order, scores, *, strategy="capacity-aware", usage_gate_used_percent=90):
    """Select one harness without allowing capacity to erase quality boundaries."""
    ranks = {
        band: rank_band(
            policy.get(band, []), states, counts, declared_order, scores,
            strategy=strategy, usage_gate_used_percent=usage_gate_used_percent,
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
    for band in band_order:
        if ranks[band]:
            return ranks[band][0], band, ranks, promote
    return None, None, ranks, promote


if __name__ == "__main__":
    print(json.dumps(capacity_scores(), sort_keys=True))
