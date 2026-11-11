#!/usr/bin/env python3
"""Token telemetry plus XDG-stateful, transition-only response policy output."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.token_budget import (  # noqa: E402
    DEFAULT_CRITICAL_PCT,
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_TIGHT_PCT,
    TokenTelemetry,
    directive_for_band,
    policy_band,
    telemetry_from_codex_session,
    telemetry_from_explicit,
    telemetry_from_opencode_message,
)
from fleet.token_accounting import (  # noqa: E402
    read_accounting,
    record_accounting,
    session_digest,
)


DEGRADED_REASONS = {
    "stale-signal",
    "malformed-token-count",
    "ambiguous-session",
    "session-counter-decreased",
    "rollout-unreadable",
}


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adapter", choices=("portable", "codex", "opencode"), default="portable")
    ap.add_argument("--session-id")
    ap.add_argument("--active-context-tokens", type=int)
    ap.add_argument("--context-window", dest="context_window_tokens", type=int)
    ap.add_argument("--session-input-tokens", type=int)
    ap.add_argument("--session-cached-input-tokens", type=int)
    ap.add_argument("--session-output-tokens", type=int)
    ap.add_argument("--session-reasoning-output-tokens", type=int)
    ap.add_argument("--session-total-tokens", type=int)
    ap.add_argument("--previous-session-total-tokens", type=int)
    ap.add_argument("--tight-pct", type=int,
                    default=env_int("AGENT_TOKEN_BUDGET_TIGHT_PCT", DEFAULT_TIGHT_PCT))
    ap.add_argument("--critical-pct", type=int,
                    default=env_int("AGENT_TOKEN_BUDGET_CRITICAL_PCT", DEFAULT_CRITICAL_PCT))
    ap.add_argument("--max-age-seconds", type=int,
                    default=env_int("AGENT_TOKEN_BUDGET_MAX_AGE_SECONDS",
                                    DEFAULT_MAX_AGE_SECONDS))
    ap.add_argument("--codex-home")
    ap.add_argument("--state-dir")
    ap.add_argument("--native-active", action="store_true",
                    help="validated native rollout-budget opt-in; suppress fallback policy")
    ap.add_argument("--format", choices=("kv", "json", "hook"), default="kv")
    ap.add_argument("--message-stdin", action="store_true",
                    help="OpenCode plugin: read {tokens, context_window} of the latest "
                         "assistant request from stdin")
    ap.add_argument("--record-accounting", action="store_true",
                    help="hook format: record this invocation's outcome in the bounded "
                         "aggregate (an adapter whose parent is not Python)")
    return ap


def opencode_message(stream) -> dict:
    try:
        value = json.loads(stream.read(65536) or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def collect(args) -> TokenTelemetry:
    if args.adapter == "opencode" and args.message_stdin:
        message = opencode_message(sys.stdin)
        return telemetry_from_opencode_message(
            session_id=args.session_id, tokens=message.get("tokens"),
            context_window_tokens=message.get("context_window"),
            session_total_tokens=args.session_total_tokens)
    explicit = args.active_context_tokens is not None or args.context_window_tokens is not None
    if explicit:
        return telemetry_from_explicit(
            adapter=args.adapter,
            session_id=args.session_id,
            active_context_tokens=args.active_context_tokens,
            context_window_tokens=args.context_window_tokens,
            session_input_tokens=args.session_input_tokens,
            session_cached_input_tokens=args.session_cached_input_tokens,
            session_output_tokens=args.session_output_tokens,
            session_reasoning_output_tokens=args.session_reasoning_output_tokens,
            session_total_tokens=args.session_total_tokens,
        )
    if args.adapter == "codex" and args.session_id:
        return telemetry_from_codex_session(
            args.session_id,
            codex_home=args.codex_home,
            max_age_seconds=args.max_age_seconds,
        )
    return TokenTelemetry(adapter=args.adapter, session_id=args.session_id,
                          reason="missing-context-signal")


def state_path(args) -> Path | None:
    if not args.session_id:
        return None
    base = Path(args.state_dir) if args.state_dir else Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    ) / "hearting" / "token-budget"
    digest = session_digest(args.session_id)
    return base / (digest + ".json")


def read_state(path: Path | None) -> dict | None:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".token-budget-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


class TransitionLock:
    """Cross-platform bounded lock using atomic directory creation."""

    def __init__(self, path: Path, timeout_seconds=0.5):
        self.path = path.with_suffix(".lock")
        self.timeout_seconds = timeout_seconds
        self.acquired = False

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.path.mkdir()
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > 30
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.path.rmdir()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("token-budget transition lock timeout")
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, traceback):
        if self.acquired:
            try:
                self.path.rmdir()
            except OSError:
                pass


def policy_result(args, telemetry: TokenTelemetry) -> dict:
    native = args.native_active or env_truthy("AGENT_TOKEN_BUDGET_NATIVE_VALIDATED")
    if native:
        state, source = "native", "validated-native"
    elif telemetry.status != "observed":
        state, source = "unknown", "none"
    else:
        state = policy_band(telemetry.context_used_pct, args.tight_pct, args.critical_pct)
        source = "observed-context"
    previous = args.previous_session_total_tokens
    if previous is None:
        stored = read_state(state_path(args))
        if stored is None:
            telemetry.status = "degraded"
            telemetry.reason = "malformed-transition-state"
            state, source = "unknown", "none"
        else:
            stored_total = stored.get("session_total_tokens")
            previous = stored_total if isinstance(stored_total, int) else None
    current = telemetry.session_total_tokens
    if previous is not None and current is not None and current < previous:
        telemetry.status = "degraded"
        telemetry.reason = "session-counter-decreased"
        state, source = "unknown", "none"
    result = telemetry.to_dict()
    result.update(policy_state=state, policy_source=source,
                  tight_pct=args.tight_pct, critical_pct=args.critical_pct)
    return result


def zero_reason(result: dict) -> str:
    if result.get("policy_state") == "native":
        return "native"
    if result.get("status") == "degraded" or result.get("reason") in DEGRADED_REASONS:
        return "degraded"
    if result.get("policy_state") == "unknown":
        return "unknown"
    if result.get("policy_state") == "normal":
        return "normal"
    return "same_band"


def receipt(result: dict, *, directive_id=None, directive="", reason=None) -> dict:
    return {
        "receipt_version": 1,
        "outcome": "emission" if directive else "zero",
        "zero_reason": None if directive else reason,
        "directive_id": directive_id if directive else None,
        "directive_utf8_bytes": len(directive.encode("utf-8")),
        "session_total_tokens": (
            result.get("session_total_tokens")
            if isinstance(result.get("session_total_tokens"), int) else None
        ),
    }


def hook_output(args, result: dict) -> tuple[str, dict]:
    path = state_path(args)
    if path is None or result["policy_state"] == "unknown":
        return "", receipt(result, reason=zero_reason(result))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_context = TransitionLock(path)
        lock_context.__enter__()
    except (OSError, TimeoutError):
        return "", receipt(result, reason="degraded")
    try:
        previous = read_state(path)
        if previous is None:
            return "", receipt(result, reason="degraded")
        current_total = result.get("session_total_tokens")
        previous_total = previous.get("session_total_tokens")
        if (isinstance(current_total, int) and isinstance(previous_total, int)
                and current_total < previous_total):
            return "", receipt(result, reason="degraded")
        band = result["policy_state"]
        previous_band = previous.get("band")
        state = {"band": band}
        if isinstance(current_total, int):
            state["session_total_tokens"] = current_total
        write_state(path, state)
        directive_id, directive = directive_for_band(band)
        if directive and band != previous_band:
            return directive, receipt(result, directive_id=directive_id, directive=directive)
        return "", receipt(result, reason=zero_reason(result))
    except OSError:
        return "", receipt(result, reason="degraded")
    finally:
        lock_context.__exit__(None, None, None)


def write_receipt(path_value: str, value: dict) -> bool:
    path = Path(path_value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".token-budget-result-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            os.replace(name, path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def print_kv(result: dict, prefix="") -> None:
    for key, value in result.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            print_kv(value, name)
            continue
        if value is None:
            value = "unknown"
        elif isinstance(value, bool):
            value = "1" if value else "0"
        print(f"{name}={value}")


def main() -> int:
    args = parser().parse_args()
    if args.max_age_seconds < 0:
        parser().error("--max-age-seconds must be >= 0")
    telemetry = collect(args)
    try:
        result = policy_result(args, telemetry)
    except ValueError as exc:
        parser().error(str(exc))
    if args.format == "hook":
        directive, private_receipt = hook_output(args, result)
        if args.record_accounting and args.session_id:
            try:
                record_accounting(args.session_id, private_receipt, adapter=args.adapter,
                                  state_dir=args.state_dir)
            except Exception:
                pass  # Accounting is an observation and never owns the output.
        receipt_path = os.environ.get("AGENT_TOKEN_BUDGET_RESULT_PATH")
        if receipt_path:
            write_receipt(receipt_path, private_receipt)
        if directive:
            print(directive)
        return 0
    # L2 diagnostics use the raw id only as a private lookup key. Never echo it.
    result.pop("session_id", None)
    if args.session_id:
        accounting = read_accounting(
            args.session_id, adapter=args.adapter, state_dir=args.state_dir)
        if accounting is not None:
            result["accounting"] = accounting
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print_kv(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
