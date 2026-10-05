#!/usr/bin/env python3
"""Launch-time hard exclusion for user-prohibited harnesses (no-Claude, etc.).

A user prohibition ("no-Claude", "no-Codex") is stronger than any routing
signal (core/OPERATIONS.md: "A user prohibition is stronger than any
signal"). The sealed route already excludes a harness probed with
``--disable-harness`` (nested-eligibility ``user-disabled`` → ``unsupported``),
but a prohibition that arrives *after* the route was sealed still left the
old candidates supported. A launch-time fallback then silently violated it:
``--dry-run`` reported the Codex head as ``exit 0`` while ``--start`` spent
the Codex attempt (``exit 1``) and automatically fell back to a freshly
launched Claude worker (home-os ``owner-handoff.md`` 2026-10-04).

Official path: ``AGENT_DISPATCH_EXCLUDED_HARNESSES`` (alias
``AGENT_DISPATCH_DISABLED_HARNESSES`` for the ``--disable-harness``
vocabulary), comma- and/or whitespace-separated, case-insensitive. Both
``dispatch-owner.py`` (depth-1 owner/frame selection, including its
eligibility-fallback pool) and ``stage-dispatch-fallback.py`` (depth-2
same-/cross-harness hops, including the native-subagent hop) read it on
every invocation — ``--dry-run``, ``--register`` and ``--start`` share the
same filter, so the dry-run receipt predicts the start fallback chain.
Excluded harnesses are never selected, never used as automatic or explicit
fallback, and an explicit ``--adapter <excluded>`` is refused before any
wrapper runs. When nothing non-excluded remains the launch fails closed
instead of falling through to the prohibited harness.
"""
from __future__ import annotations

import os
import re

KNOWN_HARNESSES = frozenset({"claude", "codex", "opencode"})

# Primary name first; the alias keeps the `--disable-harness` vocabulary.
ENV_VARS = ("AGENT_DISPATCH_EXCLUDED_HARNESSES", "AGENT_DISPATCH_DISABLED_HARNESSES")

_SPLIT = re.compile(r"[,\s]+")


def parse_excluded(raw: str | None) -> frozenset[str]:
    """Parse one raw env value into a validated harness set.

    Empty/None → empty set. Unknown names raise ValueError so a typo
    (``Claud``, ``gpt``) fails closed instead of silently allowing the
    prohibited harness through.
    """
    if not raw:
        return frozenset()
    names = {part.strip().lower() for part in _SPLIT.split(raw.strip()) if part.strip()}
    unknown = sorted(names - KNOWN_HARNESSES)
    if unknown:
        raise ValueError("excluded-harness-unknown:" + ",".join(unknown))
    return frozenset(names)


def excluded_harnesses(env=None) -> frozenset[str]:
    """Union of every exclusion env var (validated)."""
    source = os.environ if env is None else env
    out: set[str] = set()
    for key in ENV_VARS:
        out |= set(parse_excluded(source.get(key)))
    return frozenset(out)


def format_excluded(excluded) -> str:
    """Stable receipt rendering: sorted csv, or ``none``."""
    if not excluded:
        return "none"
    return ",".join(sorted(excluded))
