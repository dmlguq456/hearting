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
    return {
        "claude": manual.get("claude", _claude_score(now, stale_after)),
        "codex": manual.get("codex", _codex_score(now, stale_after)),
        # No supported proactive API. Exhaustion still arrives through usage-check.
        "opencode": manual.get("opencode"),
    }


def rank_band(candidates, states, counts, declared_order, scores):
    """Rank eligible quality peers by headroom, then recent attempts and config order."""
    candidates = [name for name in candidates if not _limited(states.get(name, "unknown"))]
    order = {name: index for index, name in enumerate(declared_order)}
    known = [scores[name] for name in candidates if scores.get(name) is not None]
    neutral = sum(known) / len(known) if known else 50.0
    ranked = sorted(
        candidates,
        key=lambda name: (
            -float(scores.get(name) if scores.get(name) is not None else neutral),
            int(counts.get(name, 0)),
            order.get(name, len(order)),
        ),
    )
    bias = os.environ.get("HARNESS_CAPACITY_BIAS", "").strip().lower()
    if bias in ranked:
        ranked = [bias] + [name for name in ranked if name != bias]
    return ranked


def select(policy, states, counts, declared_order, scores):
    """Select one harness without allowing capacity to erase quality boundaries."""
    ranks = {
        band: rank_band(policy.get(band, []), states, counts, declared_order, scores)
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
