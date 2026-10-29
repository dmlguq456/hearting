#!/usr/bin/env python3
"""Select one complete Hearting model configuration for a runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


ADAPTERS = ("claude", "codex", "opencode")
SAFE_KEY = re.compile(r"^CFG_[A-Z0-9_]+$")
SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9._:/ |,-]+$")
ASSIGNMENT = re.compile(r"^(CFG_[A-Z0-9_]+)=(.*)$")
CONTEXT_WINDOW_KEY = "CFG_MODEL_CONTEXT_WINDOW"
CONTEXT_WINDOW_DEFAULT = 800000
CONTEXT_WINDOW_MIN = 100000
CONTEXT_WINDOW_MAX = 1000000


class ModelConfigError(ValueError):
    """A candidate configuration is not safe or complete."""


class ShippedConfigError(ModelConfigError):
    """The shipped fallback itself cannot provide a safe configuration."""


@dataclass(frozen=True)
class ModelConfigReceipt:
    schema: str
    adapter: str
    source: str
    reason: str
    selected_path: str
    user_path: str
    shipped_path: str
    balanced_provenance: str = "explicit"
    # Shipped tier keys the user file lacks but never references (2026-09-08,
    # SD-145): a complete legacy copy stays selected whole-file instead of being
    # silently replaced by the shipped policy when a release adds a tier.
    unreferenced_tier_keys: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


TIER_KEY = re.compile(r"^CFG_TIER_([A-Z0-9_]+)_(MODEL|EFFORT|VARIANT)$")
# Tiers each adapter's own wrappers read by literal key name, whatever the
# profiles say: the role mappers (`adapters/<adapter>/bin/model-map.sh`,
# `role-map.sh`) and the distill workers match a role in `CFG_ROLES_*` and then
# read `CFG_TIER_DEEP_MODEL` and friends directly, so those keys are required
# even when every profile routes through `model/<id>:effort` (review R2-B1).
# This is a declared contract, not a runtime source scan: selecting the required
# tiers reads no wrapper file (the config files themselves are still read on every
# resolve). `model_config.test.py` guards it by scanning each adapter's `bin/*.sh`
# and `*.py` for fully written tier keys, so a consumer that assembles the key at
# runtime, uses another extension, or lives outside `bin/` must be added here by
# hand (reviews R3-B1/R3-M1/R4-M1/R4-M2).
WRAPPER_REQUIRED_TIERS: dict[str, frozenset[str]] = {
    "claude": frozenset({"DEEP", "LIGHT", "MINI"}),
    "codex": frozenset({"DEEP", "LIGHT", "MINI"}),
    "opencode": frozenset({"BALANCED_DEEP", "LIGHT", "MINI"}),
}
# Optional wrapper tiers must name a required fallback. Absence never makes a
# legacy user copy incomplete; the literal-read drift guard verifies that every
# optional read uses that fallback rather than an unguarded shell variable.
WRAPPER_FALLBACK_TIERS: dict[str, dict[str, str]] = {
    "opencode": {"DEEP": "BALANCED_DEEP"},
}


TIER_REFERENCE_KEYS = ("CFG_TIER_DEEP_FAILOVER", "CFG_NATIVE_SUBAGENT", "CFG_LIFECYCLE_NUDGE", "CFG_LIFECYCLE_CURATE")
# Profile keys a complete user copy may omit. `balanced` is derived from light
# in memory (below); an absent `top` does not replace the whole selected file.
# OpenCode resolves it through its balanced-deep collapse; other runtimes require
# opt-in. Unreferenced TOP tier keys are optional (SD-145). Nothing writes to the file.
# `CFG_MODEL_PROFILE_GRANULARITY_DEEP` (2026-09-30) is receipt metadata only: the
# resolver falls back to the global granularity when it is absent, so a copy seeded
# before the shipped OpenCode file started naming its deep collapse stays selected.
OPTIONAL_PROFILE_KEYS = frozenset({
    "CFG_MODEL_PROFILE_BALANCED", "CFG_MODEL_PROFILE_GRANULARITY_BALANCED",
    "CFG_MODEL_PROFILE_TOP", "CFG_MODEL_PROFILE_GRANULARITY_TOP",
    "CFG_MODEL_PROFILE_GRANULARITY_DEEP",
})
# New scalar policy is optional independently of profile completeness: old
# complete user copies stay selected and receive the in-memory default.
OPTIONAL_SCALAR_KEYS = frozenset({CONTEXT_WINDOW_KEY, "CFG_V2_AUTO_FALLBACKS"})
# Policy keys a release added after a user copy was seeded. A complete user
# copy without one stays selected whole-file, and the consumer sees the key
# as absent (top review B2, 2026-09-10: the codex main-session-only key made
# every earlier codex copy `user-incomplete`, replacing the user's whole
# policy with the shipped file while the docs promised "no restriction").
# An absent main-session-only key means no restriction on every adapter
# (`headless_model_refusal` below). Claude's key stays required instead: a
# Claude copy without it is replaced by the shipped file, which keeps the
# shipped restriction rather than lifting it (combined review m1).
OPTIONAL_POLICY_KEYS: dict[str, frozenset[str]] = {
    "claude": frozenset({"CFG_CAPACITY_PROBE_MODELS"}),
    "codex": frozenset({"CFG_MAIN_SESSION_ONLY_MODELS"}),
}
# Tiers a user copy may reference without declaring: the exception tier's
# resolver refuses typed (`profile-top-undeclared`) instead of the whole copy
# falling back to the shipped file.
OPTIONAL_TIERS = frozenset({"TOP"})


def restricted_model(model: str, restricted: list[str] | tuple[str, ...] | str) -> bool:
    """Whether `model` names a CFG_MAIN_SESSION_ONLY_MODELS entry.

    An entry matches as a whole identifier (a hyphenated vendor id) or, when it
    is a bare alphanumeric alias, as one token of the model id (the alias inside
    a versioned full id).
    One definition for the Claude and Codex wrappers and the capacity
    cascade, so a hyphenated top model id cannot pass one gate and fail
    another. `hooks/subagent-model-default.sh` (embedded Python, no imports)
    carries a hand-mirrored copy that must be kept identical."""

    entries = restricted.split() if isinstance(restricted, str) else list(restricted)
    tokens = set(re.split(r"[^a-z0-9]+", model.lower()))
    lowered = model.lower()
    for entry in entries:
        alias = entry.lower()
        if not alias:
            continue
        if alias == lowered or (re.fullmatch(r"[a-z0-9]+", alias) and alias in tokens):
            return True
    return False


MAIN_SESSION_ONLY_KEY = "CFG_MAIN_SESSION_ONLY_MODELS"


def main_session_only_models(policy: Mapping[str, str]) -> tuple[str, ...]:
    """The models a registered headless launch may not select; none when the key is absent."""
    return tuple(policy.get(MAIN_SESSION_ONLY_KEY, "").split())


def main_session_only_state(policy: Mapping[str, str]) -> str:
    """`declared` or `absent`, for the launch receipt (review R1 M3)."""
    return "declared" if MAIN_SESSION_ONLY_KEY in policy else "absent"


def headless_model_refusal(policy: Mapping[str, str], model: str, source: str) -> tuple[str, str] | None:
    """`(reason, message)` when a headless launch selected a main-session-only model.

    One rule for every adapter wrapper (audit §4 #8, A8); the OpenCode wrapper
    used to skip this check and the Claude one refused when the key was absent.
    """
    if restricted_model(model, main_session_only_models(policy)):
        return ("headless-main-session-only-model",
                f"model selected by {source} is interactive dispatch-depth-0 main-session only")
    return None


def inheritance_refusal(policy: Mapping[str, str], harness: str) -> tuple[str, str] | None:
    """`(reason, message)` when `--inherit-model-settings` may not run headless.

    A headless launch cannot prove which model the inherited interactive
    settings name, so inheritance is refused exactly when the adapter declares
    a main-session-only model it could leak (top review M1, combined review
    B1). An adapter that declares none has nothing to leak.
    """
    restricted = main_session_only_models(policy)
    if not restricted:
        return None
    return ("headless-model-inheritance-ineligible",
            f"registered headless {harness} dispatch cannot prove that inherited interactive settings "
            f"exclude a main-session-only model ({' '.join(restricted)}); select a model profile, a model role, "
            "or an explicit model")


def _referenced_tiers(values: Mapping[str, str]) -> set[str]:
    """Tier ids a config file actually points at (profile `tier:budget` values and
    the scalar tier selectors). Tier ids are normalized like model_profile does
    (`balanced-deep` -> `BALANCED_DEEP`)."""
    tiers: set[str] = set()
    for key, value in values.items():
        if key.startswith("CFG_MODEL_PROFILE_") and ":" in value and not value.startswith("model/"):
            tiers.add(value.split(":", 1)[0].strip().upper().replace("-", "_"))
        elif key in TIER_REFERENCE_KEYS:
            tiers.add(value.strip().upper().replace("-", "_"))
    return tiers


def _unreferenced_tier_keys(missing: set[str], user_values: Mapping[str, str], adapter: str) -> set[str]:
    """The subset of `missing` shipped keys that are tier keys of a tier nothing
    in this configuration needs. A release that adds a tier (e.g. `balanced-deep`)
    must not turn an older complete user copy into `user-incomplete` — that would
    silently replace the user's explicit policy (main-only list, model tiers)
    with the shipped one. A tier stays required when the user file references it
    (a profile's `tier:budget`, or a scalar tier selector) or when this adapter's
    wrappers read its keys by name (`WRAPPER_REQUIRED_TIERS`)."""
    # `OPTIONAL_TIERS` excuses a tier the *user file* points at (the
    # exception tier's resolver refuses typed instead), never one this
    # adapter's wrappers read by name -- those must stay declared or the
    # wrapper reads `None` (combined review m2). Subtracting before the
    # union is a pre-emptive correction, not a fix for an observed escape:
    # it changes nothing while `OPTIONAL_TIERS` and every adapter's
    # `WRAPPER_REQUIRED_TIERS` stay disjoint, and holds the invariant the
    # first overlap would otherwise break (guard review m4).
    required = (_referenced_tiers(user_values) - OPTIONAL_TIERS) | WRAPPER_REQUIRED_TIERS.get(adapter, frozenset())
    optional: set[str] = set()
    for key in missing:
        match = TIER_KEY.fullmatch(key)
        if match and match.group(1) not in required:
            optional.add(key)
    return optional


def _derive_balanced_values(adapter: str, values: dict[str, str]) -> dict[str, str]:
    """Derive only the optional balanced extension in memory.

    The user file remains the selected whole file; this is deliberately not a
    merge with shipped values and never writes back to the runtime home.
    """
    if "CFG_MODEL_PROFILE_BALANCED" in values:
        return values
    light = values.get("CFG_MODEL_PROFILE_LIGHT")
    if not light or ":" not in light:
        raise ModelConfigError("light profile is required to derive balanced")
    light_tier, light_budget = light.split(":", 1)
    from model_profile import resolve_profile_values
    try:
        resolve_profile_values(adapter, values, "light")
    except ValueError as exc:
        raise ModelConfigError("light tier is incomplete for balanced derivation") from exc
    values = dict(values)
    values["CFG_MODEL_PROFILE_BALANCED"] = f"{light_tier}:" + (light_budget if adapter == "opencode" else "high")
    values["CFG_MODEL_PROFILE_GRANULARITY_BALANCED"] = (
        "collapsed-balanced-to-light" if adapter == "opencode" else "full"
    )
    return values


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _check_adapter(adapter: str) -> None:
    if adapter not in ADAPTERS:
        raise ModelConfigError(f"unknown adapter: {adapter!r}")


def _absolute(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ModelConfigError(f"{label} must be an absolute path")
    return candidate


def shipped_path(adapter: str, *, source_root: str | Path | None = None) -> Path:
    _check_adapter(adapter)
    root = _absolute(source_root, "source root") if source_root is not None else repository_root()
    return root / "adapters" / adapter / "config" / "models.conf"


def runtime_home(
    adapter: str,
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    _check_adapter(adapter)
    env = os.environ if environ is None else environ
    if explicit is not None:
        return _absolute(explicit, "runtime home")
    # A registered worker may mask its native home after launch. The worker
    # home builder pins the original per-harness config directory here so
    # descendants keep resolving the user's model policy through the mask.
    if env.get("HEARTING_MODEL_CONFIG_HOME"):
        return _absolute(env["HEARTING_MODEL_CONFIG_HOME"], "HEARTING_MODEL_CONFIG_HOME")
    home = _absolute(env.get("HOME") or str(Path.home()), "HOME")
    if adapter == "claude":
        return _absolute(env.get("CLAUDE_CONFIG_DIR") or home / ".claude", "CLAUDE_CONFIG_DIR")
    if adapter == "codex":
        return _absolute(env.get("CODEX_HOME") or home / ".codex", "CODEX_HOME")
    config_home = _absolute(env.get("XDG_CONFIG_HOME") or home / ".config", "XDG_CONFIG_HOME")
    return config_home / "opencode"


def user_path(
    adapter: str,
    *,
    runtime: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    return runtime_home(adapter, runtime, environ) / "agent-config" / "models.conf"


def _strip_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#":
            return value[:index].rstrip()
    if quote:
        raise ModelConfigError("unterminated quoted value")
    return value.rstrip()


def _parse_value(raw: str, lineno: int) -> str:
    value = _strip_comment(raw).strip()
    if not value:
        raise ModelConfigError(f"line {lineno} has an empty value")
    if value[0] in "'\"":
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ModelConfigError(f"line {lineno} has an unterminated quoted value")
        value = value[1:-1]
        if quote == '"':
            value = re.sub(r"\\([\\\"#])", r"\1", value)
        if not value:
            raise ModelConfigError(f"line {lineno} has an empty value")
        return value
    if not SAFE_UNQUOTED.fullmatch(value):
        raise ModelConfigError(f"line {lineno} has an unsafe unquoted value")
    return value


def parse_context_window(value: object) -> str:
    """Validate and canonicalize the common requested context budget."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ModelConfigError(
            f"{CONTEXT_WINDOW_KEY} must be decimal ASCII digits"
        )
    number = int(value, 10)
    if not CONTEXT_WINDOW_MIN <= number <= CONTEXT_WINDOW_MAX:
        raise ModelConfigError(
            f"{CONTEXT_WINDOW_KEY} must be between {CONTEXT_WINDOW_MIN} and {CONTEXT_WINDOW_MAX}"
        )
    return str(number)


def resolve_context_window(values: Mapping[str, str]) -> tuple[str, str]:
    """Return (normalized requested tokens, configured/default provenance)."""
    if CONTEXT_WINDOW_KEY not in values:
        return str(CONTEXT_WINDOW_DEFAULT), "default"
    return parse_context_window(values[CONTEXT_WINDOW_KEY]), "configured"


def parse_config(path: str | Path, *, allow_symlink: bool = True) -> dict[str, str]:
    """Parse a flat CFG file without evaluating shell syntax."""
    candidate = Path(path)
    try:
        if not allow_symlink and candidate.is_symlink():
            raise OSError("symlinks are not accepted")
        mode = candidate.stat().st_mode
        if not stat.S_ISREG(mode) or not mode & 0o444:
            raise OSError("not a readable regular file")
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ModelConfigError(f"configuration unreadable: {exc}") from exc
    values: dict[str, str] = {}
    for lineno, raw in enumerate(lines, 1):
        raw_assignment = raw.lstrip()
        line = raw_assignment.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match or not SAFE_KEY.fullmatch(match.group(1)):
            raise ModelConfigError(f"line {lineno} is not a safe CFG_ assignment")
        key = match.group(1)
        if key in values:
            raise ModelConfigError(f"line {lineno} duplicates {key}")
        if key == CONTEXT_WINDOW_KEY:
            raw_match = ASSIGNMENT.fullmatch(raw_assignment)
            raw_value = (raw_match.group(2) if raw_match and raw_match.group(1) == key
                         else match.group(2))
            # Keep malformed scalar text inert until the typed resolver can
            # report the specific user-invalid-context-window reason. Do not
            # normalize whitespace around an unquoted scalar into validity.
            if "#" not in raw_value and raw_value != raw_value.strip():
                values[key] = raw_value
                continue
            try:
                values[key] = _parse_value(raw_value, lineno)
            except ModelConfigError:
                values[key] = raw_value
        else:
            values[key] = _parse_value(match.group(2), lineno)
    if not values:
        raise ModelConfigError("configuration has no CFG_ declarations")
    return values


def resolve_config(
    adapter: str,
    *,
    runtime: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    source_root: str | Path | None = None,
) -> tuple[dict[str, str], ModelConfigReceipt]:
    _check_adapter(adapter)
    shipped = shipped_path(adapter, source_root=source_root)
    selected_user = user_path(adapter, runtime=runtime, environ=environ)
    try:
        shipped_values = parse_config(shipped)
    except ModelConfigError as exc:
        raise ShippedConfigError(f"shipped configuration unusable: {exc}") from exc
    try:
        resolve_context_window(shipped_values)
    except ModelConfigError as exc:
        raise ShippedConfigError(f"shipped configuration has invalid context window: {exc}") from exc

    try:
        user_values = parse_config(selected_user, allow_symlink=False)
    except ModelConfigError as exc:
        if not selected_user.exists() and not selected_user.is_symlink():
            reason = "user-missing"
        elif "unreadable" in str(exc):
            reason = "user-unreadable"
        else:
            reason = "user-malformed"
    else:
        try:
            resolve_context_window(user_values)
        except ModelConfigError:
            reason = "user-invalid-context-window"
        else:
            optional_balanced = (set(OPTIONAL_PROFILE_KEYS) | set(OPTIONAL_POLICY_KEYS.get(adapter, frozenset()))
                                 | set(OPTIONAL_SCALAR_KEYS))
            missing = set(shipped_values) - set(user_values)
            unreferenced_tier = _unreferenced_tier_keys(missing - optional_balanced, user_values, adapter)
            if missing - optional_balanced - unreferenced_tier:
                reason = "user-incomplete"
            else:
                deriving = ("CFG_MODEL_PROFILE_BALANCED" in shipped_values
                            and "CFG_MODEL_PROFILE_BALANCED" not in user_values)
                try:
                    selected = _derive_balanced_values(adapter, user_values) if deriving else user_values
                    # Validate the extension when present; old unrelated config validation stays unchanged.
                    if "CFG_MODEL_PROFILE_BALANCED" in selected:
                        from model_profile import resolve_profile_values
                        resolve_profile_values(adapter, selected, "balanced")
                except ValueError:
                    reason = "user-incomplete"
                else:
                    return selected, ModelConfigReceipt(
                        "hearting.model-config/v1", adapter, "user",
                        "user-valid-derived-balanced" if deriving else "user-valid",
                        str(selected_user), str(selected_user), str(shipped),
                        "derived-from-user-light" if deriving else "explicit",
                        ",".join(sorted(unreferenced_tier)),
                    )

    return shipped_values, ModelConfigReceipt(
        "hearting.model-config/v1",
        adapter,
        "shipped",
        reason,
        str(shipped),
        str(selected_user),
        str(shipped),
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def assignments(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={_shell_quote(value)}\n" for key, value in sorted(values.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True, choices=ADAPTERS)
    parser.add_argument("--runtime-home")
    parser.add_argument("--source-root")
    parser.add_argument("--receipt-fd", type=int)
    try:
        args = parser.parse_args(argv)
        if args.receipt_fd is not None and args.receipt_fd < 0:
            return 64
        values, receipt = resolve_config(
            args.adapter, runtime=args.runtime_home, source_root=args.source_root
        )
        if args.receipt_fd is not None:
            payload = json.dumps(receipt.as_dict(), separators=(",", ":")) + "\n"
            os.write(args.receipt_fd, payload.encode())
        sys.stdout.write(assignments(values))
        return 0
    except ShippedConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 65
    except (ModelConfigError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
