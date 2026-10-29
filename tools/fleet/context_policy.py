"""Read exact, process/attempt-scoped native context-policy observations."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat

SCHEMA = "hearting-context-policy-v1"
MAX_BYTES = 16 * 1024


def policy_directory() -> Path:
    """Use the existing Hearting runtime state root, beside session registry data."""
    from . import session_registry
    return Path(session_registry.state_root()).parent / "context-policy"


def policy_path(harness, *, pid=None, proc_start=None, attempt_id=None) -> Path | None:
    if harness not in {"claude", "codex", "opencode"}:
        return None
    if attempt_id:
        if (not isinstance(attempt_id, str) or len(attempt_id) > 160
                or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in attempt_id)):
            return None
        return policy_directory() / harness / ("attempt-" + attempt_id + ".json")
    if (type(pid) is not int or pid <= 1 or not isinstance(proc_start, str)
            or not proc_start.isdigit()):
        return None
    return policy_directory() / harness / (f"{pid}-{proc_start}.json")


def read_policy(*, harness, session_id, pid=None, proc_start=None, attempt_id=None,
                model=None, environ=None):
    """Return one exact policy record only when process/attempt, SID and model agree."""
    path = policy_path(harness, pid=pid, proc_start=proc_start, attempt_id=attempt_id)
    if path is None or not isinstance(session_id, str) or not session_id:
        return None
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_size > MAX_BYTES):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            initial = os.fstat(fd)
            if (not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.getuid()
                    or initial.st_size > MAX_BYTES):
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_BYTES + 1)
            latest = os.fstat(fd)
        finally:
            os.close(fd)
        current = os.lstat(path)
        generation = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if (len(raw) > MAX_BYTES or generation(initial) != generation(latest)
                or generation(initial) != generation(current) or generation(before) != generation(initial)):
            return None
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(record, dict) or record.get("schema") != SCHEMA
            or record.get("harness") != harness or record.get("session_id") != session_id
            or record.get("attempt_id") != (attempt_id or None)):
        return None
    if not attempt_id and (type(record.get("pid")) is not int or record["pid"] != pid
                           or str(record.get("proc_start")) != str(proc_start)):
        return None
    if model and record.get("model") != model:
        return None
    policy = record.get("policy")
    if not isinstance(policy, dict) or policy.get("state") not in {"applied", "disabled", "unknown"}:
        return None
    if (policy.get("session_id") not in (None, session_id)
            or policy.get("attempt_id") not in (None, attempt_id or None)):
        return None
    return record


# ---------------------------------------------------------------------------
# Native compaction consumers, verified per runtime version.
#
# Claude Code 2.1.299 (bundled consumer: resolveAutoCompactWindow -> `Jw`, trigger
# `f5(_3())`, autocompact_state schema): with CLAUDE_CODE_AUTO_COMPACT_WINDOW valid the
# window is min(model window, max(100k, min(value, 1M))); the effective window is that
# minus min(maxOutputTokens, 20000); threshold-triggered compaction fires at effective -
# 13000, or min(floor(effective * pct/100), effective - 13000) under
# CLAUDE_AUTOCOMPACT_PCT_OVERRIDE. Without an env/settings window the source is "auto",
# which locally defers to the API's prompt-too-long path and has no enforced trigger.
#
# Codex 0.162.1 (openai/codex tag rust-v0.162.1, ModelInfo::auto_compact_token_limit and
# session/context_window.rs): the usable window in `token_count.model_context_window` is
# context_window * effective_context_window_percent / 100; with default (Total) scope the
# trigger is min(configured-or-catalogue limit, context_window * 9 / 10) compared with
# `last_token_usage.total_tokens`.
#
# A version outside this table keeps raw telemetry but gets no applied trigger.
# ---------------------------------------------------------------------------
VERIFIED_VERSIONS = {
    "claude": ("2.1.299",),
    "codex": ("0.162.1",),
}
_CLAUDE_MIN_WINDOW = 100_000
_CLAUDE_MAX_WINDOW = 1_000_000
_CLAUDE_OUTPUT_RESERVE_CAP = 20_000
_CLAUDE_COMPACT_BUFFER = 13_000
_CLAUDE_FAMILY = re.compile(r"^claude-(?:opus|sonnet|haiku|fable)-(\d+)(?:[-.\[]|$)")


def _digits(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _truthy(value):
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _claude_model_output_floor(model_id):
    """Claude 4+ models all declare at least 32k output tokens; older ids stay unknown."""
    match = _CLAUDE_FAMILY.match(model_id or "")
    return 32_000 if match and int(match.group(1)) >= 4 else None


def claude_applied_policy(*, runtime_version, model_id, raw_window_tokens, request,
                          session_id=None, attempt_id=None, max_output_tokens=None):
    """Applied Claude auto-compaction trigger for one exact execution, or an honest unknown.

    ``request`` is the launch/callback-time env of that same process: ``window``,
    ``pct_override``, ``disable_auto_compact_env``, ``disable_compact_env``,
    ``max_output_env`` and ``auto_compact_enabled`` (False when a settings file said so).
    """
    from .token_budget import context_policy_snapshot
    request = request if isinstance(request, dict) else {}
    window_text = request.get("window")
    base = dict(
        source=f"claude-code-{runtime_version or 'unknown'}-autocompact-consumer",
        runtime_version=runtime_version if isinstance(runtime_version, str) else None,
        configured_request=window_text if isinstance(window_text, str) else None,
        configured_request_tokens=_digits(window_text),
        session_id=session_id, attempt_id=attempt_id)

    def unknown(reason):
        return context_policy_snapshot(**base, reason=reason)

    if runtime_version not in VERIFIED_VERSIONS["claude"]:
        return unknown("native-consumer-version-unverified")
    if (_truthy(request.get("disable_compact_env"))
            or _truthy(request.get("disable_auto_compact_env"))
            or request.get("auto_compact_enabled") is False):
        return context_policy_snapshot(**base, disabled=True, enabled=False,
                                       reason="native-auto-compaction-disabled")
    requested = _digits(window_text)
    if requested is None or requested == 0:
        return unknown("native-window-source-not-env")
    configured = max(_CLAUDE_MIN_WINDOW, min(requested, _CLAUDE_MAX_WINDOW))
    raw = _digits(raw_window_tokens)
    floor = _claude_model_output_floor(model_id)
    if raw is not None and raw > 0:
        window = min(raw, configured)
    elif configured <= 200_000 and floor is not None:
        window = configured          # every Claude 4+ model window is at least 200k
    else:
        return unknown("raw-model-window-not-observed")
    output = _digits(max_output_tokens)
    if output is None or output == 0:
        output = floor
    max_env = request.get("max_output_env")
    if isinstance(max_env, str) and max_env.strip():
        env_value = _digits(max_env.strip())
        if env_value is None:
            return unknown("max-output-env-unparseable")
        output = env_value if output is None else min(output, env_value)
    if output is None:
        return unknown("model-max-output-not-verified")
    reserve = min(output, _CLAUDE_OUTPUT_RESERVE_CAP)
    effective = window - reserve
    if effective <= _CLAUDE_COMPACT_BUFFER:
        return unknown("native-window-below-compaction-buffer")
    trigger = effective - _CLAUDE_COMPACT_BUFFER
    pct_text = request.get("pct_override")
    if isinstance(pct_text, str):
        match = re.match(r"\s*[+-]?(?:\d+\.?\d*|\.\d+)", pct_text)
        if match:
            pct = float(match.group(0))
            if 0 < pct <= 100:
                trigger = min(math.floor(effective * (pct / 100)), trigger)
    return context_policy_snapshot(
        **base, effective_window_tokens=window, applied_trigger_tokens=trigger,
        output_allowance_tokens=reserve, compaction_reserve_tokens=_CLAUDE_COMPACT_BUFFER,
        enabled=True, enforced=True)


def codex_catalog_entry(model, runtime_version, *, home=None):
    """Catalogue fields in this execution's own home; another home proves nothing."""
    if not isinstance(model, str) or not model or not home:
        return None
    for directory in (home,):
        path = os.path.join(directory, "models_cache.json")
        try:
            if os.path.getsize(path) > 16 * 1024 * 1024:
                continue
            with open(path, encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or document.get("client_version") != runtime_version:
            continue
        for entry in document.get("models") or []:
            if isinstance(entry, dict) and entry.get("slug") == model:
                return {key: entry.get(key) for key in (
                    "context_window", "max_context_window",
                    "effective_context_window_percent", "auto_compact_token_limit")}
    return None


def codex_user_overrides(*, home=None):
    """Compaction-affecting keys in the Codex user config, or ``None`` when unreadable.

    ``ambiguous`` is True for a profile table, token-budget table or non-default scope:
    those can change the trigger in ways this reader does not interpret.
    """
    home = home or os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")
    path = os.path.join(home, "config.toml")
    try:
        import tomllib
        if os.path.getsize(path) > 1024 * 1024:
            return None
        with open(path, "rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return None  # absence now cannot prove absence when an older process started
    except Exception:
        return None
    limit = config.get("model_auto_compact_token_limit")
    scope = config.get("model_auto_compact_token_limit_scope")
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    profiles = config.get("profiles") if isinstance(config.get("profiles"), dict) else {}
    ambiguous = bool(
        (scope is not None and str(scope).lower() not in {"total"})
        or "token_budget" in features
        or any(isinstance(profile, dict) and any(
            key.startswith("model_auto_compact") for key in profile)
            for profile in profiles.values()))
    if limit is not None and _digits(limit) is None:
        ambiguous = True
    return {"limit": _digits(limit), "ambiguous": ambiguous}


_CODEX_EXECUTION_INPUTS = {}


def _codex_execution_inputs(home, model, version, started_at, execution_key,
                            native_startup_at=None):
    """Retain native inputs that were present before this exact execution started.

    A collector restart cannot reconstruct deleted or subsequently changed settings.
    Such evidence stays unknown. A captured snapshot survives later file changes.
    """
    if (not home or not execution_key or isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or not math.isfinite(started_at) or started_at <= 0):
        return None
    key = (os.path.abspath(home), model, version, started_at, execution_key)
    if key in _CODEX_EXECUTION_INPUTS:
        return _CODEX_EXECUTION_INPUTS[key]
    paths = [os.path.join(home, name) for name in ("models_cache.json", "config.toml")]
    def generation(path):
        st = os.stat(path)
        private_catalogue = (path.endswith("models_cache.json")
                             and os.path.dirname(os.path.realpath(path))
                             == os.path.realpath(home))
        cutoff = (native_startup_at if private_catalogue
                  and native_startup_at is not None else started_at)
        if max(st.st_mtime, st.st_ctime) > cutoff:
            raise ValueError("native input changed after startup")
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
    try:
        before = [generation(path) for path in paths]
        entry = codex_catalog_entry(model, version, home=home)
        overrides = codex_user_overrides(home=home)
        if before != [generation(path) for path in paths]:
            return None
    except (OSError, ValueError):
        return None
    if entry is None or overrides is None:
        return None
    if len(_CODEX_EXECUTION_INPUTS) >= 512:
        _CODEX_EXECUTION_INPUTS.pop(next(iter(_CODEX_EXECUTION_INPUTS)))
    _CODEX_EXECUTION_INPUTS[key] = (entry, overrides)
    return entry, overrides


def codex_applied_policy(*, runtime_version, model, usable_window_tokens, session_id=None,
                         attempt_id=None, home=None, cli_override_present=False,
                         execution_started_at=None, execution_key=None,
                         session_created_at=None, private_runtime_home=False):
    """Applied Codex auto-compaction trigger for the thread that reported ``usable_window``."""
    from .token_budget import context_policy_snapshot
    usable = _digits(usable_window_tokens)
    base = dict(
        source=f"codex-{runtime_version or 'unknown'}-auto-compact-consumer",
        runtime_version=runtime_version if isinstance(runtime_version, str) else None,
        session_id=session_id, attempt_id=attempt_id)

    def unknown(reason, **extra):
        return context_policy_snapshot(**base, **extra, reason=reason)

    if runtime_version not in VERIFIED_VERSIONS["codex"]:
        return unknown("native-consumer-version-unverified")
    if usable is None or usable == 0:
        return unknown("codex-usable-window-not-observed")
    if cli_override_present:
        return unknown("codex-cli-compaction-override-present")
    if cli_override_present is None:
        return unknown("codex-cli-compaction-inputs-not-observed")
    if (type(session_created_at) not in (int, float)
            or type(execution_started_at) not in (int, float)
            or not math.isfinite(session_created_at)
            or not math.isfinite(execution_started_at)
            or session_created_at < execution_started_at):
        return unknown("codex-runtime-version-not-bound-to-execution")
    inputs = _codex_execution_inputs(home, model, runtime_version,
                                    execution_started_at, execution_key,
                                    session_created_at if attempt_id and private_runtime_home
                                    else None)
    if inputs is None:
        return unknown("codex-policy-inputs-not-bound-to-execution")
    entry, overrides = inputs
    percent = _digits(entry.get("effective_context_window_percent")) if entry else None
    if not percent or percent > 100:
        return unknown("codex-model-catalogue-percent-not-verified")
    # The native usable window is floored, so its inverse can name several
    # original windows. Only an invariant final trigger is exact evidence.
    lower = -(-usable * 100 // percent)
    upper = -(-(usable + 1) * 100 // percent) - 1
    if lower > upper or lower * percent // 100 != usable:
        return unknown("codex-usable-window-not-derivable")
    window = lower if lower == upper else None
    if overrides["ambiguous"]:
        return unknown("codex-compaction-config-not-interpretable", effective_window_tokens=window)
    # A configured `model_auto_compact_token_limit` replaces the catalogue's; either is
    # then clamped to 90% of the resolved window (ModelInfo::auto_compact_token_limit).
    limit = overrides["limit"]
    if limit is None:
        limit = _digits(entry.get("auto_compact_token_limit"))
    trigger = lower * 9 // 10
    upper_trigger = upper * 9 // 10
    if limit is not None:
        trigger = min(trigger, limit)
        upper_trigger = min(upper_trigger, limit)
    # Both operations are monotone; equal endpoints prove the whole interval.
    if trigger != upper_trigger:
        return unknown("codex-applied-trigger-not-uniquely-determined",
                       effective_window_tokens=window)
    return context_policy_snapshot(
        **base, effective_window_tokens=window, applied_trigger_tokens=trigger,
        enabled=True, enforced=True)
