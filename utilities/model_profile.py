#!/usr/bin/env python3
"""Resolve route-sealed portable execution profiles through adapter config."""

from __future__ import annotations

import re
from pathlib import Path


PORTABLE_PROFILES = ("deep", "balanced-deep", "light", "mini")
SUBSTANTIVE_WORKER_TYPES = frozenset({"owner", "stage", "review"})
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/ |,-]+$")
SAFE_KEY = re.compile(r"^CFG_[A-Z0-9_]+$")


class ModelProfileError(ValueError):
    pass


def load_config(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ModelProfileError(f"model profile config unreadable: {exc}") from exc
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if line.startswith("CFG_"):
                raise ModelProfileError(
                    f"model profile config line {lineno} is a malformed CFG_ declaration"
                )
            continue
        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        key = key.strip()
        if not key.startswith("CFG_"):
            continue
        if not SAFE_KEY.fullmatch(key):
            raise ModelProfileError(
                f"model profile config line {lineno} has an invalid CFG_ key: {key!r}"
            )
        if not value or not SAFE_VALUE.fullmatch(value):
            raise ModelProfileError(
                f"model profile config line {lineno} has an invalid value for {key}"
            )
        values[key] = value
    return values


def resolve_profile(adapter: str, config_path: str | Path, profile: str) -> dict[str, str]:
    if profile not in PORTABLE_PROFILES:
        raise ModelProfileError(f"unknown portable model profile: {profile!r}")
    config = load_config(config_path)
    profile_key = "CFG_MODEL_PROFILE_" + profile.upper().replace("-", "_")
    spec = config.get(profile_key)
    if not spec or spec.count(":") != 1:
        raise ModelProfileError(f"{profile_key} must declare tier:effort-or-variant")
    tier, budget = spec.split(":", 1)
    tier_key = tier.upper().replace("-", "_")
    model = config.get(f"CFG_TIER_{tier_key}_MODEL")
    budget_suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
    declared_default = config.get(f"CFG_TIER_{tier_key}_{budget_suffix}")
    profile_granularity_key = "CFG_MODEL_PROFILE_GRANULARITY_" + profile.upper().replace("-", "_")
    granularity = config.get(profile_granularity_key) or config.get(
        "CFG_MODEL_PROFILE_GRANULARITY", "unknown"
    )
    if adapter not in {"claude", "codex", "opencode"}:
        raise ModelProfileError(f"unknown adapter: {adapter!r}")
    if not model or not declared_default:
        raise ModelProfileError(f"profile tier {tier!r} lacks model/{budget_suffix.lower()}")
    if not budget:
        raise ModelProfileError(f"profile {profile!r} has an empty execution budget")
    return {
        "profile": profile,
        "tier": tier,
        "model": model,
        "budget": budget,
        "budget_kind": budget_suffix.lower(),
        "granularity": granularity,
    }


def validate_registered_profile(
    profile: str | None,
    *,
    registered_worker: bool,
    dispatch_depth: int,
    worker_type: str | None,
) -> None:
    if profile is None:
        return
    if profile not in PORTABLE_PROFILES:
        raise ModelProfileError(f"unknown portable model profile: {profile!r}")
    if (
        profile == "mini"
        and registered_worker
        and dispatch_depth in {1, 2}
        and worker_type in SUBSTANTIVE_WORKER_TYPES
    ):
        raise ModelProfileError(
            "mini is reserved for lifecycle or explicitly micro-semantic helpers"
        )
