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

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


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
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match or not SAFE_KEY.fullmatch(match.group(1)):
            raise ModelConfigError(f"line {lineno} is not a safe CFG_ assignment")
        key = match.group(1)
        if key in values:
            raise ModelConfigError(f"line {lineno} duplicates {key}")
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
        user_values = parse_config(selected_user, allow_symlink=False)
    except ModelConfigError as exc:
        if not selected_user.exists() and not selected_user.is_symlink():
            reason = "user-missing"
        elif "unreadable" in str(exc):
            reason = "user-unreadable"
        else:
            reason = "user-malformed"
    else:
        if set(shipped_values) - set(user_values):
            reason = "user-incomplete"
        else:
            return user_values, ModelConfigReceipt(
                "hearting.model-config/v1",
                adapter,
                "user",
                "user-valid",
                str(selected_user),
                str(selected_user),
                str(shipped),
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
