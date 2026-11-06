"""Read the optional, user-owned session-tidy policy without creating files."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Mapping


DEFAULT_IDLE = {
    "enabled": True,
    "wait_seconds": 21600,
    "min_tokens": 500000,
    "cache_margin_seconds": 60,
}
MAX_CONFIG_BYTES = 64 * 1024
_FIELDS = frozenset(DEFAULT_IDLE)


class PolicyError(ValueError):
    """The optional config is malformed or contains an unsafe value."""


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "hearting" / "session-tidy.json"


def _number(value, name: str, *, positive: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PolicyError(f"{name}-must-be-finite-number")
    if positive and value <= 0:
        raise PolicyError(f"{name}-must-be-positive")
    if not positive and value < 0:
        raise PolicyError(f"{name}-must-be-nonnegative")
    return value


def _validate_idle(raw, *, where: str) -> dict:
    if not isinstance(raw, dict) or set(raw) - _FIELDS:
        raise PolicyError(f"{where}-invalid-fields")
    value = dict(DEFAULT_IDLE)
    value.update(raw)
    if not isinstance(value["enabled"], bool):
        raise PolicyError(f"{where}-enabled-must-be-boolean")
    tokens = value["min_tokens"]
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise PolicyError(f"{where}-min_tokens-must-be-positive-integer")
    value["wait_seconds"] = _number(value["wait_seconds"], f"{where}-wait_seconds", positive=True)
    value["cache_margin_seconds"] = _number(
        value["cache_margin_seconds"], f"{where}-cache_margin_seconds", positive=False)
    value["min_tokens"] = tokens
    return value


def validate_document(document) -> dict:
    if not isinstance(document, dict) or set(document) - {"idle", "overrides"}:
        raise PolicyError("invalid-root")
    base = _validate_idle(document.get("idle", {}), where="idle")
    raw_overrides = document.get("overrides", {})
    if not isinstance(raw_overrides, dict):
        raise PolicyError("overrides-must-be-object")
    overrides = {}
    for harness, providers in raw_overrides.items():
        if not isinstance(harness, str) or not harness or not isinstance(providers, dict):
            raise PolicyError("override-harness-must-map-providers")
        overrides[harness] = {}
        for provider, settings in providers.items():
            if not isinstance(provider, str) or not provider:
                raise PolicyError("override-provider-must-be-string")
            validated = _validate_idle(settings, where=f"override-{harness}-{provider}")
            overrides[harness][provider] = {key: validated[key] for key in settings}
    return {"idle": base, "overrides": overrides}


def read_document(path: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> tuple[dict, str]:
    """Return validated config and ``""``; absence is a successful default."""
    target = Path(path) if path is not None else config_path(environ)
    if target.is_symlink():
        return {}, "config-symlink"
    try:
        info = target.stat()
        if not target.is_file() or info.st_size > MAX_CONFIG_BYTES:
            return {}, "config-not-regular-or-too-large"
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"idle": dict(DEFAULT_IDLE), "overrides": {}}, ""
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, "config-unreadable-or-invalid-json"
    try:
        return validate_document(raw), ""
    except PolicyError as exc:
        return {}, str(exc)


def idle_policy(harness: str, provider: str | None = None, *, path: Path | str | None = None,
                environ: Mapping[str, str] | None = None) -> tuple[dict | None, str]:
    document, reason = read_document(path, environ=environ)
    if reason:
        return None, reason
    selected = dict(document["idle"])
    if provider:
        selected.update(document["overrides"].get(harness, {}).get(provider, {}))
    return selected, ""


def validate_user_config(path: Path | str | None = None) -> dict:
    target = Path(path) if path is not None else config_path()
    document, reason = read_document(target)
    if reason:
        return {"status": "invalid", "ok": False, "path": str(target), "detail": reason}
    return {"status": "valid" if target.exists() else "absent", "ok": True,
            "path": str(target),
            "detail": "optional policy; absent uses documented defaults" if not target.exists()
            else f"valid policy; idle.enabled={document['idle']['enabled']}"}
