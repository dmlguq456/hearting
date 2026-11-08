"""Shared Codex TUI status-line preference merge and feasibility reader."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib


# `thread-id` leads: Codex clips an overflowing footer from the right, so a
# narrow pane loses trailing telemetry instead of the identity Fleet reads.
DEFAULT_STATUS_LINE = (
    "thread-id", "thread-title", "git-branch", "context-used", "model-with-reasoning",
    "five-hour-limit", "weekly-limit",
)
DEFAULT_STATUS_LINE_USE_COLORS = True
BACKUP_NAME = "config.toml.pre-harness-tui"
STARTUP_UPDATE_KEY = "check_for_update_on_startup"
STARTUP_UPDATE_DEFAULT = False
_MISSING = object()
_TABLE_RE = re.compile(r"^\s*(\[\[?[^\]]+\]\]?)\s*(?:#.*)?$")
_TUI_TABLE_RE = re.compile(r"^\s*\[tui\]\s*(?:#.*)?$")
_STATUS_KEY_RE = re.compile(r"^\s*(?:status_line|\"status_line\"|'status_line')\s*=")
_ARRAY_OPEN_RE = re.compile(r"^\s*(?:status_line|\"status_line\"|'status_line')\s*=\s*\[\s*(?:#.*)?$")
_ARRAY_ITEM_RE = re.compile(r"""^(\s*)("(?:[^"\\]|\\.)*"|'[^']*')\s*,?\s*(?:#.*)?$""")
_ARRAY_CLOSE_RE = re.compile(r"^\s*\]\s*(?:#.*)?$")


class CodexTuiConfigError(ValueError):
    """The config cannot be safely merged without changing unrelated state."""


def _parse(text: str, label: str = "Codex config") -> dict:
    try:
        parsed = tomllib.loads(text) if text.strip() else {}
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise CodexTuiConfigError(f"invalid TOML in {label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CodexTuiConfigError(f"invalid TOML in {label}")
    return parsed


def _normalize(items):
    if not isinstance(items, (list, tuple)) or any(not isinstance(item, str) for item in items):
        raise CodexTuiConfigError("tui.status_line must be an array of strings")
    # The single managed identity item goes first; every other item keeps its
    # order. Earlier releases put it last, so the former default and lists that
    # end with it converge here on the next install or refresh.
    return ["thread-id"] + [item for item in items if item != "thread-id"]


def _array_end(lines, start, limit):
    """Find the closing bracket of a validated string array, ignoring strings/comments."""
    depth = 0
    quote = None
    triple = False
    escaped = False
    started = False
    for line_index in range(start, limit):
        line = lines[line_index]
        index = 0
        while index < len(line):
            char = line[index]
            if quote is not None:
                marker = quote * (3 if triple else 1)
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and char == "\\":
                    escaped = True
                elif line.startswith(marker, index):
                    quote = None
                    triple = False
                    index += len(marker) - 1
                index += 1
                continue
            if char == "#":
                break
            if char in ('"', "'"):
                triple = line.startswith(char * 3, index)
                quote = char
                index += 2 if triple else 0
            elif char == "[":
                depth += 1
                started = True
            elif char == "]" and started:
                depth -= 1
                if depth == 0:
                    suffix = line[index + 1:]
                    comment = re.search(r"\s+#.*$", suffix)
                    return line_index, (comment.group(0) if comment else "")
            index += 1
    raise CodexTuiConfigError("could not locate the end of tui.status_line")


def _identity_first_in_place(lines, start, end):
    """Move the managed item of a one-item-per-line array to its top.

    Every other line, including comments inside the array, stays as written.
    Returns None for any other layout; the caller then rewrites the array.
    """
    if (end <= start or not _ARRAY_OPEN_RE.match(lines[start])
            or not _ARRAY_CLOSE_RE.match(lines[end])):
        return None
    body, indent = [], None
    for line in lines[start + 1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            body.append(line)
            continue
        match = _ARRAY_ITEM_RE.match(line)
        if not match:
            return None
        indent = match.group(1) if indent is None else indent
        try:
            value = tomllib.loads("v = " + match.group(2))["v"]
        except tomllib.TOMLDecodeError:
            return None
        if value != "thread-id":
            body.append(line)
    if indent is None:
        indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())] + "  "
    return [lines[start], indent + '"thread-id",', *body, lines[end]]


def _table_insert_index(lines, start, end):
    # Trailing comments may introduce the next managed table. Keep its block intact.
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return end


def merge_status_line_text(text: str, defaults=DEFAULT_STATUS_LINE,
                           default_colors=DEFAULT_STATUS_LINE_USE_COLORS) -> str:
    """Return valid TOML with one thread-id item first; leave all other values intact."""
    current = _parse(text)
    tui = current.get("tui", {})
    if not isinstance(tui, dict):
        raise CodexTuiConfigError("[tui] must be a table")
    if (not isinstance(defaults, (list, tuple)) or any(not isinstance(item, str) for item in defaults)
            or "thread-id" not in defaults or type(default_colors) is not bool):
        raise CodexTuiConfigError("invalid harness TUI defaults")
    default_items = _normalize(defaults)
    raw_items = tui.get("status_line", _MISSING)
    items = default_items if raw_items is _MISSING else _normalize(raw_items)
    raw_colors = tui.get("status_line_use_colors", _MISSING)
    if raw_colors is not _MISSING and type(raw_colors) is not bool:
        raise CodexTuiConfigError("tui.status_line_use_colors must be a boolean")
    colors = default_colors if raw_colors is _MISSING else raw_colors

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    tui_start = next((i for i, line in enumerate(lines) if _TUI_TABLE_RE.match(line)), None)
    if tui_start is None:
        if "tui" in current:
            raise CodexTuiConfigError("unsupported inline or dotted [tui] config")
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("[tui]", "status_line = " + json.dumps(items, ensure_ascii=False),
                      "status_line_use_colors = " + str(colors).lower()))
    else:
        table_end = next((i for i in range(tui_start + 1, len(lines))
                          if _TABLE_RE.match(lines[i])), len(lines))
        status_index = next((i for i in range(tui_start + 1, table_end)
                             if _STATUS_KEY_RE.match(lines[i])), None)
        if raw_items is not _MISSING and status_index is None:
            raise CodexTuiConfigError("could not locate the parsed tui.status_line assignment")
        if status_index is None:
            lines.insert(_table_insert_index(lines, tui_start, table_end),
                         "status_line = " + json.dumps(items, ensure_ascii=False))
            table_end += 1
        elif raw_items != items:
            end_index, suffix = _array_end(lines, status_index, table_end)
            indent = lines[status_index][:len(lines[status_index]) - len(lines[status_index].lstrip())]
            span = [indent + "status_line = " + json.dumps(items, ensure_ascii=False) + suffix]
            in_place = _identity_first_in_place(lines, status_index, end_index)
            if in_place is not None:
                candidate = lines[:status_index] + in_place + lines[end_index + 1:]
                try:
                    kept = _parse(newline.join(candidate)).get("tui", {}).get("status_line")
                except CodexTuiConfigError:
                    kept = None
                span = in_place if kept == items else span
            lines[status_index:end_index + 1] = span
            table_end += len(span) - (end_index - status_index + 1)
        has_colors = any(re.match(r"^\s*(?:status_line_use_colors|\"status_line_use_colors\"|'status_line_use_colors')\s*=",
                                  lines[i]) for i in range(tui_start + 1, table_end))
        if not has_colors:
            lines.insert(_table_insert_index(lines, tui_start, table_end),
                         "status_line_use_colors = " + str(colors).lower())

    result = newline.join(lines) + (newline if lines else "")
    _parse(result, "merged Codex config")
    return result


def status_line_capability(text: str) -> str:
    """Return capable, incapable, or unknown for an effective status_line setting."""
    try:
        current = _parse(text)
    except CodexTuiConfigError:
        return "unknown"
    tui = current.get("tui", {})
    if not isinstance(tui, dict):
        return "unknown"
    items = tui.get("status_line")
    if items is None:
        return "incapable"
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        return "unknown"
    # First is the current order; last is how installs before the reorder still
    # read until their next refresh. Both are a whole field the footer reader proves.
    return "capable" if items and "thread-id" in (items[0], items[-1]) else "incapable"


def _status_line_override(argv) -> bool:
    """Whether argv makes the disk status-line setting unknown."""
    argv = list(argv or ())
    index = 0
    while index < len(argv):
        word = str(argv[index])
        if word in ("--profile", "-p"):
            return True
        if word in ("--config", "-c"):
            if index + 1 >= len(argv):
                return True
            setting = str(argv[index + 1]).split("=", 1)[0].strip()
            if setting == "tui" or setting == "tui.status_line" or setting.startswith("tui.status_line."):
                return True
            index += 2
            continue
        if word.startswith("--config=") or word.startswith("-c="):
            setting = word.split("=", 2)[1].strip() if word.count("=") > 1 else ""
            if setting == "tui" or setting == "tui.status_line" or setting.startswith("tui.status_line."):
                return True
        index += 1
    return False


def status_line_capability_for_home(home, *, argv=()) -> str:
    """Read one known Codex home; unreadable config and relevant CLI overrides stay unknown."""
    if _status_line_override(argv):
        return "unknown"
    path = Path(home).expanduser() / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "incapable"
    except (OSError, UnicodeError):
        return "unknown"
    return status_line_capability(text)


def managed_status_line_observation(home, *, argv=()):
    """One unchanged default-format observation; custom/unknown layers stay visible."""
    if _status_line_override(argv):
        return None
    path = Path(home).expanduser() / "config.toml"
    def generation(info):
        return [str(value) for value in (info.st_dev, info.st_ino, info.st_size,
                                        info.st_mtime_ns, info.st_ctime_ns)]
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o022 or before.st_size > 1024 * 1024):
            return None
        parsed = _parse(path.read_text(encoding="utf-8"))
        if (parsed.get("profile") or (parsed.get("tui") or {}).get("status_line")
                != list(DEFAULT_STATUS_LINE)):
            return None
        if generation(path.lstat()) != generation(before):
            return None
        return {"path": str(path), "stamp": generation(before),
                "items": list(DEFAULT_STATUS_LINE)}
    except (OSError, UnicodeError, CodexTuiConfigError, TypeError, AttributeError):
        return None


def merge_status_line_from_fragment(text: str, fragment_path) -> str:
    fragment_path = Path(fragment_path)
    fragment = _parse(fragment_path.read_text(encoding="utf-8"), str(fragment_path))
    tui = fragment.get("tui")
    if not isinstance(tui, dict):
        raise CodexTuiConfigError(f"invalid TUI defaults in {fragment_path}")
    defaults = tui.get("status_line")
    colors = tui.get("status_line_use_colors", _MISSING)
    if colors is _MISSING:
        colors = DEFAULT_STATUS_LINE_USE_COLORS
    return merge_status_line_text(text, defaults, colors)


def _root_table_index(lines):
    """Find the first real TOML table header, ignoring quoted multiline content."""
    multiline = None
    for line_index, line in enumerate(lines):
        if multiline is None and _TABLE_RE.match(line.rstrip("\r\n")):
            return line_index
        index = 0
        while index < len(line):
            char = line[index]
            if multiline is not None:
                if multiline == '"' and char == "\\":
                    index += 2
                elif line.startswith(multiline * 3, index):
                    multiline = None
                    index += 3
                else:
                    index += 1
                continue
            if char == "#":
                break
            if char in ('"', "'"):
                if line.startswith(char * 3, index):
                    multiline = char
                    index += 3
                    continue
                index += 1
                while index < len(line):
                    if char == '"' and line[index] == "\\":
                        index += 2
                    elif line[index] == char:
                        index += 1
                        break
                    else:
                        index += 1
                continue
            index += 1
    return None


def _merge_startup_update_default(text: str) -> tuple[str, str]:
    parsed = _parse(text)
    current = parsed.get(STARTUP_UPDATE_KEY, _MISSING)
    if current is not _MISSING:
        if type(current) is not bool:
            raise CodexTuiConfigError(f"{STARTUP_UPDATE_KEY} must be a boolean")
        return text, ""

    newline = "\r\n" if "\r\n" in text else "\n"
    assignment = f"{STARTUP_UPDATE_KEY} = false{newline}"
    # Put the root setting before all user tables and managed blocks. Inserting
    # directly before a table can split a comment-delimited block such as the
    # runtime-activation identity table that precedes its own header.
    inserted = assignment
    updated = inserted + text
    _parse(updated, "merged Codex startup config")
    return updated, inserted


def merge_startup_update_default_text(text: str) -> str:
    """Insert the startup-update default at TOML root, preserving explicit booleans."""
    return _merge_startup_update_default(text)[0]


def _valid_startup_update_record(record) -> bool:
    if not isinstance(record, dict) or record.get("schema") != 1 \
            or record.get("baseline") != {"present": False} \
            or type(record.get("projected")) is not bool or record["projected"] is not False:
        return False
    inserted = record.get("inserted")
    if not isinstance(inserted, str) or not re.fullmatch(
            r"(?:\r?\n)?check_for_update_on_startup = false(?:\r?\n)", inserted):
        return False
    try:
        return _parse(inserted, "startup default receipt") == {STARTUP_UPDATE_KEY: False}
    except CodexTuiConfigError:
        return False


def _startup_update_record_matches(text: str, record) -> bool:
    if not _valid_startup_update_record(record):
        return False
    try:
        parsed = _parse(text)
    except CodexTuiConfigError:
        return False
    inserted = record["inserted"]
    if parsed.get(STARTUP_UPDATE_KEY, _MISSING) is not False or text.count(inserted) != 1:
        return False
    lines = text.splitlines(keepends=True)
    table_index = _root_table_index(lines)
    root_end = sum(len(line) for line in lines[:table_index]) if table_index is not None else len(text)
    start = text.find(inserted)
    key_start = start + (1 if inserted.startswith(("\n", "\r\n")) else 0)
    return start >= 0 and key_start < root_end


def startup_update_default_record(before: str, after: str, previous=None) -> dict | None:
    """Record only our exact root insertion, retaining its ownership across refresh."""
    parsed_before = _parse(before)
    current = parsed_before.get(STARTUP_UPDATE_KEY, _MISSING)
    if current is not _MISSING:
        if type(current) is not bool:
            raise CodexTuiConfigError(f"{STARTUP_UPDATE_KEY} must be a boolean")
        return previous if current is False and _startup_update_record_matches(before, previous) else None
    expected, inserted = _merge_startup_update_default(before)
    if not inserted or expected != after:
        return None
    record = {"schema": 1, "baseline": {"present": False},
              "projected": STARTUP_UPDATE_DEFAULT, "inserted": inserted}
    return record if _valid_startup_update_record(record) else None


def restore_startup_update_default_text(text: str, record) -> str:
    """Remove only the exact inserted root line while it still has the projected value."""
    if not _startup_update_record_matches(text, record):
        return text
    updated = text.replace(record["inserted"], "", 1)
    try:
        parsed = _parse(updated, "restored Codex startup config")
    except CodexTuiConfigError:
        return text
    return text if STARTUP_UPDATE_KEY in parsed else updated


def _tui_bounds(lines):
    start = next((i for i, line in enumerate(lines) if _TUI_TABLE_RE.match(line)), None)
    if start is None:
        return None, None
    end = next((i for i in range(start + 1, len(lines)) if _TABLE_RE.match(lines[i])), len(lines))
    return start, end


def _setting_span(lines, key):
    start, end = _tui_bounds(lines)
    if start is None:
        return None
    pattern = re.compile(r"^\s*(?:" + key + r"|\"" + key + r"\"|'" + key + r"')\s*=")
    index = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
    if index is None:
        return None
    last = _array_end(lines, index, end)[0] if key == "status_line" else index
    return index, last + 1


def _valid_setting_record(item, key):
    if not isinstance(item, dict) or not isinstance(item.get("baseline"), dict):
        return False
    baseline = item["baseline"]
    if type(baseline.get("present")) is not bool or not isinstance(baseline.get("source"), str):
        return False
    values = [item.get("projected")]
    if baseline["present"]:
        values.append(baseline.get("value"))
    if key == "status_line":
        if any(not isinstance(value, list) or any(not isinstance(entry, str) for entry in value)
               for value in values):
            return False
    elif any(type(value) is not bool for value in values):
        return False
    try:
        expected = {"tui": {key: baseline["value"]}} if baseline["present"] else {"tui": {}}
        return _parse("[tui]\n" + baseline["source"]) == expected
    except CodexTuiConfigError:
        return False


def status_line_record(before: str, after: str, previous=None) -> dict:
    """Remember only the two managed keys, retaining their baseline through refresh."""
    old_tui = _parse(before).get("tui", {})
    new_tui = _parse(after).get("tui", {})
    previous = previous if isinstance(previous, dict) else {}
    prior_keys = previous.get("keys", {})
    prior_keys = prior_keys if isinstance(prior_keys, dict) else {}
    lines = before.splitlines(keepends=True)
    record = {"table_created": previous.get("table_created") is True or "tui" not in _parse(before),
              "keys": {}}
    for key in ("status_line", "status_line_use_colors"):
        prior = prior_keys.get(key)
        span = _setting_span(lines, key)
        baseline = {"present": key in old_tui,
                    "source": "".join(lines[slice(*span)]) if span else ""}
        if key in old_tui:
            baseline["value"] = old_tui[key]
        if _valid_setting_record(prior, key):
            if old_tui.get(key, _MISSING) == prior.get("projected", _MISSING):
                baseline = dict(prior["baseline"])
            elif (key == "status_line" and isinstance(old_tui.get(key), list)
                  and "thread-id" not in prior["baseline"].get("value", [])
                  and "thread-id" in prior.get("projected", [])):
                # An edited list owns its new items, but not our added identity item.
                baseline["value"] = [item for item in old_tui[key] if item != "thread-id"]
                baseline["source"] = "status_line = " + json.dumps(baseline["value"]) + "\n"
        record["keys"][key] = {"baseline": baseline, "projected": new_tui[key]}
    return record


def restore_status_line_text(text: str, record) -> str:
    """Remove recorded additions without restoring over subsequent user settings."""
    if not isinstance(record, dict) or not isinstance(record.get("keys"), dict):
        return text
    parsed = _parse(text)
    tui = parsed.get("tui", {})
    if not isinstance(tui, dict):
        return text
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    for key in ("status_line", "status_line_use_colors"):
        item = record["keys"].get(key)
        if not _valid_setting_record(item, key):
            continue
        baseline = item["baseline"]
        span = _setting_span(lines, key)
        if span is None:
            continue
        current = tui.get(key, _MISSING)
        if current == item.get("projected", _MISSING):
            replacement = baseline.get("source") if baseline["present"] else ""
            if not isinstance(replacement, str):
                continue
        elif (key == "status_line" and isinstance(current, list)
              and "thread-id" not in baseline.get("value", [])
              and "thread-id" in item.get("projected", [])):
            remaining = [entry for entry in current if entry != "thread-id"]
            if remaining == current:
                continue
            suffix = _array_end(lines, span[0], span[1])[1].rstrip("\r\n")
            replacement = "status_line = " + json.dumps(remaining, ensure_ascii=False) + suffix + newline
        else:
            continue
        lines[span[0]:span[1]] = [replacement] if replacement else []
    start, end = _tui_bounds(lines)
    if (record.get("table_created") is True and start is not None
            and all(_valid_setting_record(record["keys"].get(key), key)
                    for key in ("status_line", "status_line_use_colors"))
            and not "".join(lines[start + 1:end]).strip()):
        # merge_status_line_text added one separator before its new table.
        if start and not lines[start - 1].strip():
            start -= 1
        del lines[start:end]
    result = "".join(lines)
    _parse(result, "restored Codex config")
    return result


def apply_status_line(config_path, fragment_path, *, dry_run=False) -> dict:
    """Apply shared Codex defaults with the shell installer's backup contract."""
    config_path, fragment_path = Path(config_path), Path(fragment_path)
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated = merge_status_line_from_fragment(original, fragment_path)
    statusline_updated = updated
    startup_error = None
    try:
        updated = merge_startup_update_default_text(updated)
    except CodexTuiConfigError as exc:
        # A malformed startup key must not discard the independent status-line merge.
        startup_error = str(exc)
    changed = updated != original
    if changed and not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_path = config_path.resolve(strict=False) if config_path.is_symlink() else config_path
        if config_path.exists():
            backup = config_path.with_name(BACKUP_NAME)
            if not backup.exists():
                backup.write_bytes(config_path.read_bytes())
            mode = config_path.stat().st_mode & 0o777
        else:
            mode = 0o600
        descriptor, temporary = tempfile.mkstemp(dir=str(write_path.parent),
                                                   prefix=f".{write_path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, write_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return {"path": str(config_path), "fragment": str(fragment_path),
            "changed": changed, "startup_changed": updated != statusline_updated,
            "startup_error": startup_error,
            "status": "planned" if dry_run and changed else "ok"}
