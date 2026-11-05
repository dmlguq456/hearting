"""Project the shared context budget into runtime-owned native settings."""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utilities"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_config
import safe_fs


class NativeContextError(ValueError):
    pass


_CONTEXT_KEY = "model_context_window"
_ROOT_KEY = re.compile(r"""[ \t]*(?:model_context_window|"model_context_window"|'model_context_window')[ \t]*=[ \t]*""")
_INTEGER = re.compile(r"[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|[0-9_]+)")
_HEADER = re.compile(r"[ \t]*\[")


def _native_key_record(previous, adapter: str, current_present: bool, current,
                       desired, *, extra=None, key: str | None = None) -> dict:
    """Carry a valid activation baseline only while the projected value still matches."""
    managed = previous.get("managed_config", {}) if isinstance(previous, dict) else {}
    native = managed.get("native_context", {}) if isinstance(managed, dict) else {}
    runtime_record = native.get(adapter, {}) if isinstance(native, dict) else {}
    keys = runtime_record.get("keys", {}) if isinstance(runtime_record, dict) else {}
    key = key or (_CONTEXT_KEY if adapter == "codex" else "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    old = keys.get(key) if isinstance(keys, dict) else None
    value_type = bool if key == "idleCompaction" else int if adapter == "codex" else str
    valid_old = (isinstance(old, dict) and isinstance(old.get("baseline"), dict)
                 and type(old["baseline"].get("present")) is bool
                 and (not old["baseline"]["present"] or "value" in old["baseline"])
                 and type(old.get("projected")) is value_type
                 and (adapter != "codex" or old["projected"] > 0))
    if valid_old and adapter == "codex" and old["baseline"]["present"]:
        valid_old = type(old["baseline"].get("value")) is int and old["baseline"]["value"] > 0
    if valid_old and current_present and type(current) is value_type and current == old["projected"]:
        baseline = dict(old["baseline"])
        carry_extra = dict(old)
    else:
        baseline = {"present": bool(current_present)}
        if current_present:
            baseline["value"] = current
        carry_extra = {}
    record = {**carry_extra, "baseline": baseline, "projected": desired}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key == "env_present" and "env_present" in carry_extra:
                continue
            record[key] = value
    return record


def _native_restore_action(record, current_present: bool, current, *, adapter: str, key: str | None = None):
    """Return (action, baseline value) only for a well-formed exact-match receipt."""
    value_type = bool if key == "idleCompaction" else int if adapter == "codex" else str
    if not isinstance(record, dict) or type(record.get("projected")) is not value_type:
        return "keep", None
    if adapter == "codex" and record["projected"] < 1:
        return "keep", None
    baseline = record.get("baseline")
    if (not isinstance(baseline, dict) or type(baseline.get("present")) is not bool
            or (baseline["present"] and "value" not in baseline)):
        return "keep", None
    if not current_present or type(current) is not value_type or current != record["projected"]:
        return "keep", None
    if baseline["present"]:
        value = baseline.get("value")
        if adapter == "codex" and (type(value) is not int or value < 1):
            return "keep", None
        return "restore", value
    return "delete", None


def _remove_codex_root(raw: bytes) -> bytes:
    text, parsed = _parse_toml(raw)
    span, _ = _root_scan(text)
    if not span:
        return raw
    line_start = text.rfind("\n", 0, span[0]) + 1
    line_end = text.find("\n", span[1])
    if line_end < 0:
        line_end = len(text)
        newline = ""
    else:
        newline = "\r\n" if text[line_end - 1] == "\r" else "\n"
    line = text[line_start:line_end]
    comment = line.find("#", span[1] - line_start)
    replacement = ""
    if comment >= 0:
        replacement = "#" + line[comment + 1:] + newline
    candidate = (text[:line_start] + replacement + text[line_end + (1 if newline else 0):]).encode("utf-8")
    try:
        after = tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"Codex context restoration is invalid: {exc}") from exc
    if _CONTEXT_KEY in after or {k:v for k,v in parsed.items() if k != _CONTEXT_KEY} != after:
        raise NativeContextError("Codex context restoration changed unrelated settings")
    return candidate


def restore_codex_context(raw: bytes, record: dict) -> tuple[bytes, bool]:
    """Restore one root key while preserving every other TOML byte and parsed value."""
    text, parsed = _parse_toml(raw)
    current = parsed.get(_CONTEXT_KEY)
    action, baseline = _native_restore_action(
        record, _CONTEXT_KEY in parsed, current, adapter="codex")
    if action == "keep":
        return raw, False
    if action == "restore":
        return _edit_codex(raw, baseline), True
    return _remove_codex_root(raw), True


def _parse_toml(raw: bytes) -> tuple[str, dict]:
    try:
        text = raw.decode("utf-8")
        return text, tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"invalid Codex config TOML: {exc}") from exc


def _skip_multiline(text: str, index: int, quote: str) -> int:
    """Return the offset after a multiline string whose body starts at ``index``."""
    while index < len(text):
        if quote == '"' and text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote * 3, index):
            end = index + 3
            # Up to two quotes may sit directly before the closing delimiter.
            while end < len(text) and text[end] == quote and end - index < 5:
                end += 1
            return end
        index += 1
    return index


def _skip_string(text: str, index: int, quote: str) -> int:
    """Return the offset after a one-line string whose body starts at ``index``."""
    while index < len(text) and text[index] != "\n":
        if quote == '"' and text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return index


def _root_scan(text: str) -> tuple[tuple[int, int] | None, int]:
    """Find the root key's value span and the first table header, outside strings and comments."""
    found = None
    index, line_start, depth = 0, True, 0
    while index < len(text):
        if line_start:
            line_start = False
            if depth == 0 and _HEADER.match(text, index):
                return found, index
            key = _ROOT_KEY.match(text, index) if depth == 0 and found is None else None
            if key:
                number = _INTEGER.match(text, key.end())
                if not number:
                    raise NativeContextError("Codex root model_context_window is not a plain integer")
                found = (key.end(), number.end())
        char = text[index]
        if char == "\n":
            line_start = True
            index += 1
        elif char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        elif text.startswith('"""', index) or text.startswith("'''", index):
            index = _skip_multiline(text, index + 3, char)
        elif char in "\"'":
            index = _skip_string(text, index + 1, char)
        else:
            # A multi-line array or inline table may hold lines that look like headers.
            depth += 1 if char in "[{" else -1 if char in "]}" else 0
            index += 1
    return found, len(text)


def _verify_codex(original: dict, candidate: bytes, desired: int) -> None:
    """Only the root key may differ; anything else means the edit landed in the wrong place."""
    try:
        parsed = tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"Codex context projection is invalid: {exc}") from exc
    if parsed.get(_CONTEXT_KEY) != desired:
        raise NativeContextError("Codex context projection did not set the root model_context_window")
    before = {key: value for key, value in original.items() if key != _CONTEXT_KEY}
    after = {key: value for key, value in parsed.items() if key != _CONTEXT_KEY}
    if before != after and repr(before) != repr(after):
        raise NativeContextError("Codex context projection would change other settings; config left unchanged")


def _edit_codex(raw: bytes, desired: int) -> bytes:
    text, parsed = _parse_toml(raw)
    value = parsed.get(_CONTEXT_KEY)
    if value is not None and (type(value) is not int or value < 1):
        raise NativeContextError("Codex root model_context_window must be a positive integer")
    span, header = _root_scan(text)
    if span:
        candidate = (text[:span[0]] + str(desired) + text[span[1]:]).encode("utf-8")
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        prefix, suffix = text[:header], text[header:]
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        candidate = (prefix + f"{_CONTEXT_KEY} = {desired}{newline}" + suffix).encode("utf-8")
    _verify_codex(parsed, candidate, desired)
    return candidate


def _publish(path: Path, candidate: bytes, *, owner: str, source: bytes,
             already_locked=False) -> bool:
    current = safe_fs.capture_state(path, include_payload=True)
    if current.kind == "missing":
        before = b""
        mode = 0o600
    elif current.kind == "file":
        if current.mode is None or current.payload is None:
            raise NativeContextError(f"cannot safely read native config: {path}")
        if os.lstat(path).st_nlink != 1:
            raise NativeContextError(f"native config has a hardlink alias: {path}")
        before, mode = current.payload, current.mode
    else:
        raise NativeContextError(f"native config must be a regular file: {path}")
    if before == candidate:
        return False
    try:
        with (nullcontext() if already_locked else safe_fs.TargetLock(path)):
            current = safe_fs.capture_state(path, include_payload=True)
            if current.kind == "missing":
                if source:
                    raise NativeContextError(f"native config changed concurrently: {path}")
                before, mode = b"", 0o600
            elif current.kind == "file" and current.payload is not None:
                if current.payload != source:
                    raise NativeContextError(f"native config changed concurrently: {path}")
                if os.lstat(path).st_nlink != 1:
                    raise NativeContextError(f"native config has a hardlink alias: {path}")
                before, mode = current.payload, current.mode or 0o600
            else:
                raise NativeContextError(f"native config must be a regular file: {path}")
            if before == candidate:
                return False
            authority = safe_fs.authority(path, owner=owner, allowed_paths=(path,),
                                          allow_leaf_symlink=False, expected=current)
            safe_fs.atomic_write_bytes(authority, candidate, mode, expected=current,
                                       create_parents=True)
    except safe_fs.SafetyError as exc:
        raise NativeContextError(str(exc)) from exc
    return True


def project_context_window(adapter: str, *, runtime, source_root, previous=None,
                           scope="global", claude_merge: Callable | None = None,
                           context_only=False, native_locked=False):
    """Resolve once and project the selected scalar to the explicit runtime home."""
    if adapter not in model_config.ADAPTERS:
        raise NativeContextError(f"unknown harness: {adapter}")
    runtime_home = Path(runtime).expanduser().absolute()
    try:
        values, receipt = model_config.resolve_config(adapter, runtime=runtime_home,
                                                     source_root=source_root)
        desired, provenance = model_config.resolve_context_window(values)
    except model_config.ModelConfigError as exc:
        raise NativeContextError(str(exc)) from exc
    number = int(desired)
    if adapter == "opencode":
        return {"kind": "context-window-projected", "runtime": adapter,
                "path": None, "previous": None, "desired": desired,
                "changed": False, "source": receipt.source,
                "report_lines": [f"context window: OpenCode reads {desired} at runtime startup"]}
    if adapter == "claude":
        if claude_merge is None:
            raise NativeContextError("Claude context projection requires the existing settings merger")
        result = claude_merge(context_window=desired, context_only=context_only,
                              native_locked=native_locked)
        return {"kind": "context-window-projected", "runtime": adapter,
                "path": result.get("path"), "previous": result.get("previous_context_window"),
                "desired": desired, "changed": bool(result.get("context_changed")),
                "source": receipt.source, "merge": result,
                "native_context": result.get("native_context"),
                "report_lines": list(result.get("report_lines", []))}
    path = runtime_home / "config.toml"
    try:
        write_path = path.resolve(strict=False) if path.is_symlink() else path
    except (OSError, RuntimeError) as exc:
        raise NativeContextError(f"cannot resolve Codex config target: {exc}") from exc
    config_existed = path.exists()
    try:
        original = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        raise NativeContextError(f"cannot read Codex config: {exc}") from exc
    old = None
    if original:
        try:
            parsed = tomllib.loads(original.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise NativeContextError(f"invalid Codex config TOML: {exc}") from exc
        root = parsed.get("model_context_window")
        if root is not None and type(root) is not int:
            raise NativeContextError("Codex root model_context_window must be an integer")
        old = str(root) if root is not None else None
    parsed_before = tomllib.loads(original.decode("utf-8")) if original else {}
    old_present = _CONTEXT_KEY in parsed_before
    record = _native_key_record(previous, adapter, old_present,
                                parsed_before.get(_CONTEXT_KEY), number,
                                extra=None)
    if isinstance(previous, dict):
        prior_managed = previous.get("managed_config", {})
        prior_native = prior_managed.get("native_context", {}) if isinstance(prior_managed, dict) else {}
        prior_codex = prior_native.get("codex", {}) if isinstance(prior_native, dict) else {}
        prior_keys = prior_codex.get("keys", {}) if isinstance(prior_codex, dict) else {}
        prior_key = prior_keys.get(_CONTEXT_KEY) if isinstance(prior_keys, dict) else None
        prior_baseline = prior_key.get("baseline") if isinstance(prior_key, dict) else None
        prior_record_valid = (
            isinstance(prior_baseline, dict)
            and type(prior_baseline.get("present")) is bool
            and (not prior_baseline["present"] or "value" in prior_baseline)
            and type(prior_key.get("projected")) is int
            and prior_key["projected"] > 0
            and (not prior_baseline["present"]
                 or (type(prior_baseline.get("value")) is int and prior_baseline["value"] > 0))
        )
        if (prior_record_valid and isinstance(prior_codex, dict)
                and isinstance(prior_codex.get("file_created"), bool)):
            record["file_created"] = prior_codex["file_created"]
        elif prior_key is None:
            identity = prior_managed.get("codex_identity") if isinstance(prior_managed, dict) else None
            backup = path.with_name("config.toml.pre-harness-identity")
            record["file_created"] = bool(isinstance(identity, dict)
                                          and isinstance(identity.get("inserted"), str)
                                          and identity["inserted"] and not backup.exists())
        else:
            record["file_created"] = False
    else:
        record["file_created"] = not config_existed
    candidate = _edit_codex(original, number)
    changed = _publish(write_path, candidate, source=original,
                       owner="native-context:codex-root-window",
                       already_locked=native_locked)
    lines = [] if old == desired and not changed else [
        f"context window: codex {old or 'unset'} -> {desired} tokens", f"config: {path}"]
    # A profile-level override wins at runtime; expose it without changing it.
    parsed = tomllib.loads(candidate.decode("utf-8"))
    profile = parsed.get("profile")
    profile_table = parsed.get("profiles", {}).get(profile, {}) if isinstance(profile, str) else {}
    effective = profile_table.get("model_context_window") if isinstance(profile_table, dict) else None
    if effective is not None and effective != number:
        lines.append(f"effective Codex profile model_context_window={effective} overrides root={desired}")
    return {"kind": "context-window-projected", "runtime": adapter,
            "path": str(path), "previous": old, "desired": desired,
            "changed": changed, "source": receipt.source, "provenance": provenance,
            "native_context": {"codex": {"file_created": record.pop("file_created"),
                                             "keys": {_CONTEXT_KEY: record}}},
            "pre_projection_exists": config_existed, "pre_projection_bytes": original,
            "report_lines": lines}
