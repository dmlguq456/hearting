"""Exact native quota evidence, scoped independently of positive usage gauges.

This reader never closes attempts or authorizes retries. Execution/cleanup and
the sealed selection policy retain those decisions. No credential bytes leave
scope hashing; legacy evidence without a launch scope remains diagnostic only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

HARNESSES = ("claude", "codex", "opencode")
WINDOWS = {"five_hour": (6 * 3600, "all"), "seven_day": (8 * 86400, "all"),
           "seven_day_opus": (8 * 86400, "opus"), "seven_day_sonnet": (8 * 86400, "sonnet")}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def launch_scope(harness, env=None):
    """Seal the selected authentication scope before model start, without a probe.

    Native weekly feedback is currently supplied by Claude subscription events.
    Other runtimes retain their existing typed capacity handling, not a guessed
    weekly window derived from HTTP 429 or a provider name.
    """
    env = os.environ if env is None else env
    if harness != "claude":
        return {}
    home = Path(env.get("HOME") or Path.home())
    runtime = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser().resolve()
    if any(env.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        return {}  # API/provider throttling is not subscription quota.
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    config = runtime / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    account = _json(config).get("oauthAccount") or {}
    if not isinstance(account, dict):
        account = {}
    identity = {k: account.get(k) for k in ("accountUuid", "organizationUuid")}
    if token:
        identity = {"oauth_token_digest": hashlib.sha256(token.encode()).hexdigest()}
    elif not all(isinstance(v, str) and v for v in identity.values()):
        return {}
    return {"quota_scope": digest(["claude-subscription-v1", str(runtime), identity]),
            "quota_scope_kind": "claude-subscription-v1"}


def native_quota(rows, *, observed_at, now=None):
    """Accept only an exact failed native session's structured rejected window."""
    now = time.time() if now is None else now
    terminal_index = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("type") == "result"), None)
    terminal = rows[terminal_index] if terminal_index is not None else None
    if not terminal or terminal.get("runtime") == "opencode" or terminal.get("is_error") is not True:
        return None
    if str(terminal.get("api_error_status", "")) != "429":
        return None
    sid = terminal.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    previous_terminal = next((i for i in range(terminal_index - 1, -1, -1)
                              if rows[i].get("type") == "result"), -1)
    rows = rows[previous_terminal + 1:terminal_index]
    # A newer accepted event for this session supersedes a previous rejection.
    event = next((r for r in reversed(rows) if r.get("type") == "rate_limit_event"
                  and r.get("session_id") == sid), None)
    info = (event or {}).get("rate_limit_info") or {}
    if not isinstance(info, dict):
        return None
    window = info.get("rateLimitType")
    if info.get("status") != "rejected" or not isinstance(window, str) or window not in WINDOWS or info.get("isUsingOverage") is True:
        return None
    reset = info.get("resetsAt")
    maximum, model_scope = WINDOWS[window]
    if (isinstance(reset, bool) or not isinstance(reset, (int, float)) or not math.isfinite(reset)
            or not observed_at <= now + 60 or not observed_at < reset <= observed_at + maximum):
        return None
    return {"window": window, "reset_epoch": int(reset), "model_scope": model_scope,
            "native_session_id": sid, "observed_at": observed_at,
            "evidence_digest": digest([event, terminal]), "expired": now >= reset}


def _native_rows(path):
    try:
        with Path(path).open("rb") as f:
            start = max(0, f.seek(0, 2) - 1024 * 1024)
            f.seek(start)
            lines = f.read().splitlines()
        if start:
            lines = lines[1:]
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except (ValueError, UnicodeError):
            pass
    return result


def observations(jobs, *, now=None, env=None, registry_lines=None):
    now = time.time() if now is None else now
    scope = launch_scope("claude", env)
    try:
        lines = registry_lines if registry_lines is not None else Path(jobs).read_text().splitlines()
    except (OSError, UnicodeError):
        return []
    found = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6 or fields[1] != "done":
            continue
        meta = dict(cell.split("=", 1) for cell in fields[5].split(",") if "=" in cell)
        if meta.get("harness") != "claude" or meta.get("failure_class") == "pass" or not meta.get("note", "").startswith("dead-"):
            continue
        attempt = meta.get("attempt_id", "")
        log = meta.get("log_file", "")
        if not re.fullmatch(r"att-[a-zA-Z0-9-]+", attempt) or attempt not in Path(log).name:
            continue
        try:
            observed = datetime.fromisoformat(fields[0].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if now - observed > 8 * 86400:
            continue
        quota = native_quota(_native_rows(log), observed_at=observed, now=now)
        if not quota:
            continue
        bound_scope = meta.get("quota_scope")
        matches = bool(bound_scope and bound_scope == scope.get("quota_scope"))
        found.append({**quota, "harness": "claude", "attempt_id": attempt,
                      "quota_scope": bound_scope, "scope_authority": "launch-bound" if bound_scope else "unbound",
                      "scope_matches": matches,
                      "row_digest": digest(line)})
    return found


def active_limits(jobs, *, profile=None, models=None, now=None, env=None, registry_lines=None):
    models = dict(models or {})
    if profile and "claude" not in models:
        from model_profile import resolve_runtime_profile, ModelProfileError
        try:
            models["claude"] = resolve_runtime_profile("claude", profile, environ=env)[0]["model"]
        except (ModelProfileError, KeyError):
            pass
    result = {}
    for observation in observations(jobs, now=now, env=env, registry_lines=registry_lines):
        if observation["expired"] or not observation["scope_matches"]:
            continue
        model_scope = observation["model_scope"]
        if model_scope != "all" and model_scope not in models.get("claude", "").lower():
            continue
        previous = result.get("claude")
        if not previous or previous["reset_epoch"] < observation["reset_epoch"]:
            result["claude"] = observation
    return result


def apply_limits(states, limits):
    states = dict(states)
    for harness, limit in limits.items():
        reset = datetime.fromtimestamp(limit["reset_epoch"], timezone.utc).isoformat().replace("+00:00", "Z")
        states[harness] = f"limited({reset})"
    return states


def usage_states(jobs, *, profile=None, models=None, unknown_window_min=60, now=None, env=None):
    """One read contract for scoped native proof and legacy text compatibility."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    try:
        lines = Path(jobs).read_text().splitlines()
    except (OSError, UnicodeError):
        return dict.fromkeys(HARNESSES, "unknown")
    states = dict.fromkeys(HARNESSES, "ok")
    scope = launch_scope("claude", env).get("quota_scope")
    native = observations(jobs, now=now, env=env, registry_lines=lines)
    native_ids = {r["attempt_id"] for r in native}
    legacy = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = dict(cell.split("=", 1) for cell in fields[5].split(",") if "=" in cell)
        harness = meta.get("harness") or meta.get("owner_harness")
        if harness not in HARNESSES or not re.match(r"dead-[a-z-]*limit", meta.get("note", "")):
            continue
        # A native event owns its account/model/window even after reset or
        # account change. A lossy text marker cannot widen it back to global.
        if meta.get("attempt_id") in native_ids:
            continue
        if meta.get("quota_scope") and (harness != "claude" or meta["quota_scope"] != scope):
            continue
        if harness not in legacy or fields[0] > legacy[harness][0]:
            legacy[harness] = (fields[0], meta.get("reset", "-"))
    for harness, (stamp, reset) in legacy.items():
        try:
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        expires = None
        if reset not in {"", "-", "unknown", "unknown-reset"}:
            normalized = re.sub("noon", "12pm", reset, flags=re.I)
            normalized = re.sub("midnight", "12am", normalized, flags=re.I)
            try:
                clock = bool(re.fullmatch(r"[0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?", normalized, re.I))
                if clock:
                    day = subprocess.run(["date", "-d", f"@{int(observed)}", "+%Y-%m-%d"], capture_output=True, text=True, timeout=2, env=env)
                    normalized = day.stdout.strip() + " " + normalized
                elif not re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", normalized):
                    raise ValueError("unanchored-reset")
                parsed = subprocess.run(["date", "-d", normalized, "+%s"], capture_output=True, text=True, timeout=2, env=env)
                expires = int(parsed.stdout.strip()) if parsed.returncode == 0 else None
                if expires is not None and expires < observed and clock:
                    expires += 86400
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if expires is not None:
            if now < expires:
                states[harness] = f"limited({reset})"
        elif 0 <= now - observed < unknown_window_min * 60:
            states[harness] = "limited(unknown-reset)"
    return apply_limits(states, active_limits(jobs, profile=profile, models=models, now=now, env=env, registry_lines=lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "states"))
    parser.add_argument("--jobs")
    parser.add_argument("--harness", choices=(*HARNESSES, "all"), default="all")
    parser.add_argument("--unknown-window-min", "--window-min", type=int, default=int(os.environ.get("UNKNOWN_WINDOW_MIN", "60")))
    parser.add_argument("--model-profile")
    args = parser.parse_args()
    if not args.jobs:
        args.jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    if not args.jobs:
        root = subprocess.run([str(Path(__file__).with_name("dispatch-state-root.sh"))], capture_output=True, text=True, check=True).stdout.strip()
        args.jobs = str(Path(root) / "jobs.log")
    if args.action == "inspect":
        print(json.dumps(observations(args.jobs), sort_keys=True))
    else:
        states = usage_states(args.jobs, profile=args.model_profile, unknown_window_min=args.unknown_window_min)
        for harness in HARNESSES if args.harness == "all" else [args.harness]:
            print(harness, states[harness])
        print("bias", os.environ.get("HARNESS_CAPACITY_BIAS", "auto"))


if __name__ == "__main__":
    main()
