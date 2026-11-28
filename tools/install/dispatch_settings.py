"""Edit the user-owned dispatch allocation and steward keys in place, validated by dispatch-defaults."""
from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import paths
import safe_fs
from model_settings import _backup

# The allocation keys a user may change here; each keeps the meaning dispatch-defaults gives it.
SCALAR_KEYS = ("strategy", "window", "usage_gate_used_percent", "usage_headroom_exponent",
               "depth_affinity_weight")
NESTED_KEYS = {"depth_affinity": ("owner", "worker"),
               "harness_weights": ("claude", "codex", "opencode")}
UNSET = None   # a nested value of None removes that line (no fixed affinity harness)
# The steward block's user-tunable scalars; the 1f backend owns their meaning and ranges.
STEWARD_KEYS = ("check_interval_seconds", "max_supervisors_per_repo")
SECTIONS = {"allocation": (SCALAR_KEYS, NESTED_KEYS), "steward": (STEWARD_KEYS, {})}


class DispatchSettingsError(RuntimeError):
    pass


class DispatchSettingsUsageError(DispatchSettingsError):
    pass


def _defaults_module():
    path = Path(__file__).resolve().parents[2] / "utilities" / "dispatch-defaults.py"
    spec = importlib.util.spec_from_file_location("hearting_dispatch_defaults_settings", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_path(environ=None) -> Path:
    """The file dispatch reads for this user: the explicit override, else the user-owned policy."""
    environment = os.environ if environ is None else environ
    override = environment.get("DISPATCH_DEFAULTS_CONFIG")
    return Path(override) if override else paths.hearting_config_dir() / "dispatch-defaults.yaml"


def _text(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DispatchSettingsUsageError(f"unsupported value: {value!r}")
    if isinstance(value, float):
        return repr(round(value, 4))
    text = str(value)
    if not text or any(char in text for char in "#:\n[]{}'\"") or text != text.strip():
        raise DispatchSettingsUsageError(f"unsupported value: {value!r}")
    return text


def _split(line: str) -> tuple[int, str, str, str]:
    """``(indent, key, value, trailing comment)`` of one ``key: value`` line."""
    body, hash_mark, comment = line.partition("#")
    indent = len(line) - len(line.lstrip(" "))
    key, _colon, value = body.strip().partition(":")
    return indent, key.strip(), value.strip(), (hash_mark + comment) if hash_mark else ""


def _block(lines: list[str], start: int, indent: int) -> int:
    """Index just past the last content line nested deeper than ``indent`` after ``start``."""
    end = start + 1
    for index in range(start + 1, len(lines)):
        stripped = lines[index].split("#", 1)[0].strip()
        if not stripped:
            continue
        if len(lines[index]) - len(lines[index].lstrip(" ")) <= indent:
            break
        end = index + 1
    return end


def _find(lines: list[str], start: int, end: int, indent: int, key: str):
    for index in range(start, end):
        if lines[index].split("#", 1)[0].strip() and _split(lines[index])[:2] == (indent, key):
            return index
    return None


def _empty_mapping(value: str) -> bool:
    """The parser's `key: {}` test (`parse_yaml_subset`): braces holding only whitespace."""
    return value.startswith("{") and value.endswith("}") and not value[1:-1].strip()


def _set_line(lines, index, indent, key, value):
    _indent, _key, old, comment = _split(lines[index])
    padding = ""
    if comment:
        width = len(lines[index].split("#", 1)[0])
        padding = " " * max(1, width - len(" " * indent + f"{key}: {value}"))
    lines[index] = " " * indent + f"{key}: {value}" + (padding + comment if comment else "")
    return old


def edit_text(text: str, updates: dict, section: str = "allocation") -> str:
    """Rewrite only the requested lines of one section; comments and every other line stay as written."""
    if section not in SECTIONS:
        raise DispatchSettingsUsageError(f"{section} is not an editable section")
    scalar_keys, nested_keys = SECTIONS[section]
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    top = _find(lines, 0, len(lines), 0, section)
    if top is None:
        raise DispatchSettingsError(f"the policy file has no {section} block")
    if _split(lines[top])[2]:
        raise DispatchSettingsError(f"{section} must be a block mapping to be edited")
    for name, value in updates.items():
        end = _block(lines, top, 0)
        if name in scalar_keys:
            if value is UNSET:
                raise DispatchSettingsUsageError(f"{section}.{name} needs a value")
            index = _find(lines, top + 1, end, 2, name)
            if index is None:
                lines.insert(end, f"  {name}: {_text(value)}")
            else:
                _set_line(lines, index, 2, name, _text(value))
            continue
        parent, _dot, child = name.partition(".")
        if child not in nested_keys.get(parent, ()):
            raise DispatchSettingsUsageError(f"{section}.{name} is not an editable key")
        head = _find(lines, top + 1, end, 2, parent)
        if head is not None and _split(lines[head])[2]:
            if not _empty_mapping(_split(lines[head])[2]):
                raise DispatchSettingsError(f"allocation.{parent} must be a block mapping to be edited")
            if value is UNSET:
                continue
            # `parent: {}` is an empty mapping: open it as a block for the child.
            comment = _split(lines[head])[3]
            lines[head] = f"  {parent}:" + (" " + comment if comment else "")
        if head is None:
            if value is UNSET:
                continue
            lines[end:end] = [f"  {parent}:", f"    {child}: {_text(value)}"]
            continue
        inner = _block(lines, head, 2)
        index = _find(lines, head + 1, inner, 4, child)
        if value is UNSET:
            if index is not None:
                del lines[index]
                if _block(lines, head, 2) == head + 1:
                    del lines[head]          # an empty mapping would read as a scalar
        elif index is None:
            lines.insert(inner, f"    {child}: {_text(value)}")
        else:
            _set_line(lines, index, 4, child, _text(value))
    return newline.join(lines)


def _validated(module, text: str):
    try:
        config = module.parse_yaml_subset(text)
        errors = module.validate(config, module.load_topology_capabilities(module.default_topology_path()))
    except module.DefaultsConfigError as exc:
        errors = [str(exc)]
    if errors:
        raise DispatchSettingsUsageError("; ".join(errors))
    return config


def _section_values(module, config, section) -> dict:
    values = config.get(section)
    if not isinstance(values, dict):
        return {}
    if section != "allocation":
        return dict(values)
    # `allocation: {}` or a block missing a required key has no applied values
    # yet; the edit and the validator name what is wrong instead of a KeyError.
    if not {"strategy", "window"} <= set(values):
        return {}
    return module.query_allocation(config)


def _diff(before: dict, after: dict, updates: dict) -> list[str]:
    lines = []
    for name in updates:
        parent, _dot, child = name.partition(".")
        old = (before.get(parent) or {}).get(child) if child else before.get(name)
        new = (after.get(parent) or {}).get(child) if child else after.get(name)
        if old != new:
            lines.append(f"{name}: {'·' if old is None else old} -> {'·' if new is None else new}")
    return lines


def set_allocation(updates: dict, *, config: str | Path | None = None, dry_run: bool = False,
                   section: str = "allocation", _locked: bool = False) -> dict:
    """Plan or apply ``allocation`` (or ``steward``) edits on the user policy, shaped like ``model set``.

    A missing user file is seeded from the shipped policy. The candidate must pass the
    dispatch-defaults validator before anything is written; the old file goes to ``.bak``.
    New dispatch decisions read the result; running and sealed routes keep what they chose.
    """
    if not isinstance(updates, dict) or not updates:
        raise DispatchSettingsUsageError(f"no {section} change was requested")
    module = _defaults_module()
    path = Path(config) if config is not None else config_path()
    missing = not path.exists() and not path.is_symlink()
    if missing:
        shipped = Path(module.SHIPPED_CONFIG_PATH)
        raw = shipped.read_bytes()
        mode = 0o600
        reason = "user-missing; shipped seed"
    else:
        if path.is_symlink() or not path.is_file():
            raise DispatchSettingsError(f"policy file must be a regular non-symlink file: {path}")
        info = os.lstat(path)
        if info.st_nlink != 1:
            raise DispatchSettingsError(f"policy file has a hardlink alias: {path}")
        raw = path.read_bytes()
        mode = stat.S_IMODE(info.st_mode)
        reason = "user"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DispatchSettingsError(f"policy file is not UTF-8: {path}") from exc
    try:
        before = _section_values(module, module.parse_yaml_subset(text), section)
    except module.DefaultsConfigError as exc:
        raise DispatchSettingsError(f"existing policy file is malformed: {exc}") from exc
    candidate_text = edit_text(text, updates, section)
    after_config = _validated(module, candidate_text)
    after = _section_values(module, after_config, section)
    changes = _diff(before, after, updates)
    warnings = ([line for line in module.allocation_warnings(after_config, str(path))]
                if section == "allocation" else [])
    result = {"operation": f"{section}-set", "config_path": str(path), "source_reason": reason,
              "changes": changes, "warnings": warnings, "backup": None, "exit": 0}
    if candidate_text == text or not changes:
        return {**result, "status": "unchanged", "lines": [f"{section}: unchanged"]}
    if dry_run:
        return {**result, "status": "planned",
                "lines": [f"{section}: would set " + "; ".join(changes), f"config: {path}"]}
    if not _locked:
        path.parent.mkdir(parents=True, exist_ok=True)
        with safe_fs.TargetLock(path):
            return set_allocation(updates, config=path, dry_run=False, section=section, _locked=True)
    current = safe_fs.capture_state(path, include_payload=True)
    if missing and current.kind != "missing":
        raise DispatchSettingsError(f"policy file changed during update; preserving successor: {path}")
    if not missing and (current.kind != "file" or current.payload != raw or current.mode != mode):
        raise DispatchSettingsError(f"policy file changed during update; preserving successor: {path}")
    backup = None if missing else _backup(path, raw, mode)
    auth = safe_fs.authority(path, owner="dispatch-settings:set", allowed_paths=(path,),
                             allow_leaf_symlink=False, expected=current)
    safe_fs.atomic_write_bytes(auth, candidate_text.encode("utf-8"), mode, expected=current,
                               create_parents=True)
    return {**result, "status": "changed", "backup": str(backup) if backup else None,
            "lines": [f"{section}: set " + "; ".join(changes), f"config: {path}",
                      f"backup: {backup or 'none (new user policy)'}"]}
