"""Bounded, dependency-free readers for existing detached-run metric files.

This module only decodes bytes at the fixed locations documented by the lab
runtime. It does not choose a run, decide whether to notify, or write state.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import struct

MAX_JSONL_BYTES_PER_PASS = 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_EPOCH_FILE_BYTES = 64 * 1024
MAX_DIRECTORY_ENTRIES = 4096
MAX_TFRECORD_BYTES = 1024 * 1024
_EPOCH_FILE = re.compile(r"^(valid|train)-epoch-(\d{4,})\.json$")
_VALID_NUMBER = (int, float)


def _read_file(path: Path, root: Path, limit: int) -> tuple[bytes, os.stat_result]:
    root = root.resolve(strict=True)
    absolute = Path(os.path.abspath(path))
    if not absolute.is_relative_to(root) or ".." in absolute.parts:
        raise ValueError("metric-path-outside-root")
    current = root
    for part in absolute.relative_to(root).parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("metric-path-symlink")
    fd = os.open(absolute, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("metric-file-unbounded")
        raw = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if (len(raw) > limit or (before.st_dev, before.st_ino, before.st_size,
                            before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError("metric-file-changed")
    current = absolute.lstat()
    if (stat.S_ISLNK(current.st_mode)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
        raise ValueError("metric-path-replaced")
    return raw, after


def _check_parents(path: Path, root: Path):
    root = root.resolve(strict=True)
    absolute = Path(os.path.abspath(path))
    if not absolute.is_relative_to(root):
        raise ValueError("metric-path-outside-root")
    current = root
    for part in absolute.relative_to(root).parts[:-1]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("metric-parent-invalid")


def _number(value):
    return (isinstance(value, _VALID_NUMBER) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _metric_map(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if isinstance(key, str) and key and _number(item):
            result[key] = float(item)
        elif isinstance(key, str) and key and isinstance(item, float) and not math.isfinite(item):
            result[key] = item
    return result


def _split_epoch_object(value, split, path, filename_epoch=None):
    if not isinstance(value, dict):
        return None
    epoch = value.get("epoch", filename_epoch)
    if (type(epoch) is not int or epoch < 0
            or (filename_epoch is not None and epoch != filename_epoch)):
        return None
    metrics = {}
    for key in ("losses", "metrics"):
        metrics.update(_metric_map(value.get(key)))
    for key, item in value.items():
        if key not in {"epoch", "losses", "metrics", "seconds", "elapsed_seconds",
                       "total_epochs", "split", "step", "ts", "updated_at"}:
            if _number(item) or (isinstance(item, float) and not math.isfinite(item)):
                metrics[key] = item
    if not metrics:
        return None
    elapsed = value.get("elapsed_seconds", value.get("seconds"))
    total = value.get("total_epochs")
    return {"epoch": epoch, "unit": "epoch", "split": split,
            "metrics": metrics,
            "elapsed_seconds": float(elapsed) if _number(elapsed) and elapsed >= 0 else None,
            "total_epochs": total if type(total) is int and total > 0 else None,
            "source_path": str(path), "format": "epoch-json-files"}


def decode_jsonl(root: str | Path, cursor: dict | None = None):
    """Read a bounded complete-line suffix from ``logs/epochs.jsonl``."""
    root = Path(root).resolve(strict=True)
    path = root / "logs" / "epochs.jsonl"
    warnings, samples = [], []
    try:
        _check_parents(path, root)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("metric-path-not-regular")
        key = {"device": info.st_dev, "inode": info.st_ino}
        offset = 0
        if isinstance(cursor, dict) and cursor.get("device") == info.st_dev \
                and cursor.get("inode") == info.st_ino and type(cursor.get("offset")) is int \
                and 0 <= cursor["offset"] <= info.st_size:
            offset = cursor["offset"]
            anchor_size = cursor.get("anchor_size", 0)
            anchor_hash = cursor.get("anchor_sha256")
            if type(anchor_size) is int and 0 < anchor_size <= min(128, offset) \
                    and isinstance(anchor_hash, str):
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as source:
                    source.seek(offset - anchor_size)
                    anchor = source.read(anchor_size)
                if hashlib.sha256(anchor).hexdigest() != anchor_hash:
                    warnings.append("epochs-jsonl-generation-changed")
                    offset = 0
        elif isinstance(cursor, dict) and cursor:
            warnings.append("epochs-jsonl-generation-changed")
        if info.st_size - offset > MAX_JSONL_BYTES_PER_PASS:
            warnings.append("epochs-jsonl-partial-history")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            source.seek(offset)
            before = os.fstat(source.fileno())
            chunk = source.read(MAX_JSONL_BYTES_PER_PASS)
            after = os.fstat(source.fileno())
            end = source.tell()
        path_info = path.lstat()
        if ((before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                or (path_info.st_dev, path_info.st_ino) != (before.st_dev, before.st_ino)
                or stat.S_ISLNK(path_info.st_mode) or path_info.st_size < end):
            raise ValueError("metric-path-replaced")
        complete_end = chunk.rfind(b"\n") + 1
        consumed = chunk[:complete_end]
        anchor_size, anchor_hash = 0, ""
        if consumed:
            anchor = consumed[-128:]
            anchor_size = len(anchor)
            anchor_hash = hashlib.sha256(anchor).hexdigest()
        elif isinstance(cursor, dict) and offset == cursor.get("offset"):
            anchor_size = cursor.get("anchor_size", 0)
            anchor_hash = cursor.get("anchor_sha256", "")
        cursor_out = {**key, "offset": offset + complete_end,
                      "anchor_size": anchor_size, "anchor_sha256": anchor_hash}
        if complete_end != len(chunk):
            warnings.append("epochs-jsonl-partial-record")
        for raw in chunk[:complete_end].splitlines():
            if len(raw) > MAX_RECORD_BYTES:
                warnings.append("epochs-jsonl-record-oversized")
                continue
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                warnings.append("epochs-jsonl-record-invalid")
                continue
            if not isinstance(value, dict):
                warnings.append("epochs-jsonl-record-invalid")
                continue
            if type(value.get("epoch")) is int:
                nested_rows = []
                for split_key in ("train", "valid", "val", "validation", "test"):
                    nested = value.get(split_key)
                    if not isinstance(nested, dict):
                        continue
                    split = "valid" if split_key in {"val", "validation"} else split_key
                    row = _split_epoch_object({"epoch": value["epoch"], **nested}, split, path)
                    if row:
                        nested_rows.append({**row, "format": "epochs-jsonl"})
                if nested_rows:
                    samples.extend(nested_rows)
                else:
                    row = _split_epoch_object(value, str(value.get("split") or "unknown"), path)
                    if row:
                        samples.append({**row, "format": "epochs-jsonl"})
                continue
            step = value.get("step")
            if type(step) is not int or step < 0:
                warnings.append("epochs-jsonl-step-missing")
                continue
            split = value.get("split")
            name, scalar = value.get("name"), value.get("value")
            if isinstance(split, str) and isinstance(name, str) and name and \
                    (_number(scalar) or (isinstance(scalar, float) and not math.isfinite(scalar))):
                samples.append({"epoch": step, "unit": "step", "split": split,
                    "metrics": {name: float(scalar)}, "elapsed_seconds": None,
                    "total_epochs": None, "source_path": str(path), "format": "epochs-jsonl"})
                continue
            for split_key in ("train", "valid", "val", "validation", "test"):
                nested = value.get(split_key)
                mapped = _metric_map(nested)
                if mapped:
                    samples.append({"epoch": step, "unit": "step",
                        "split": "valid" if split_key in {"val", "validation"} else split_key,
                        "metrics": mapped, "elapsed_seconds": None,
                        "total_epochs": None, "source_path": str(path), "format": "epochs-jsonl"})
    except FileNotFoundError:
        return [], None, warnings, []
    except (OSError, ValueError, TypeError) as exc:
        return [], None, [*warnings, "epochs-jsonl-unreadable:" + str(exc)], []
    return samples, cursor_out, warnings, [str(path)]


def _json_object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-metric-key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def decode_epoch_files(root: str | Path):
    """List only the run root once and read recognized epoch JSON files."""
    root = Path(root).resolve(strict=True)
    warnings, samples, paths, entries = [], [], [], 0
    progress = None
    try:
        with os.scandir(root) as listing:
            for entry in listing:
                entries += 1
                if entries > MAX_DIRECTORY_ENTRIES:
                    warnings.append("run-root-entry-limit")
                    break
                if entry.name == "progress.json" and entry.is_file(follow_symlinks=False):
                    try:
                        raw, progress_info = _read_file(Path(entry.path), root, MAX_EPOCH_FILE_BYTES)
                        value = _json_object(raw)
                        reached = value.get("reached_epoch")
                        total = value.get("total_epochs")
                        if (type(reached) is int and reached >= 0
                                and (total is None or type(total) is int and total >= reached)):
                            progress = {"reached_epoch": reached, "total_epochs": total,
                                "best_updated": value.get("best_updated") is True,
                                "updated_at": progress_info.st_mtime,
                                "source_path": entry.path}
                    except (OSError, ValueError, UnicodeError):
                        warnings.append("progress-json-unreadable")
                match = _EPOCH_FILE.fullmatch(entry.name)
                if not match or not entry.is_file(follow_symlinks=False):
                    continue
                epoch = int(match.group(2))
                try:
                    raw, _ = _read_file(Path(entry.path), root, MAX_EPOCH_FILE_BYTES)
                    value = _json_object(raw)
                    sample = _split_epoch_object(value, match.group(1), Path(entry.path), epoch)
                    if sample:
                        samples.append(sample)
                        paths.append(entry.path)
                    else:
                        warnings.append("epoch-json-invalid:" + entry.name)
                except (OSError, ValueError, UnicodeError):
                    warnings.append("epoch-json-unreadable:" + entry.name)
    except OSError as exc:
        warnings.append("run-root-unreadable:" + str(exc))
    return samples, progress, warnings, sorted(set(paths))


def _varint(data, offset):
    value = shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("protobuf-varint-invalid")


def _fields(data):
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("protobuf-field-zero")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 1:
            if offset + 8 > len(data): raise ValueError("protobuf-fixed64-truncated")
            value, offset = data[offset:offset + 8], offset + 8
        elif wire == 2:
            size, offset = _varint(data, offset)
            if size > len(data) - offset: raise ValueError("protobuf-bytes-truncated")
            value, offset = data[offset:offset + size], offset + size
        elif wire == 5:
            if offset + 4 > len(data): raise ValueError("protobuf-fixed32-truncated")
            value, offset = data[offset:offset + 4], offset + 4
        elif wire == 3:
            start = offset
            depth = 1
            while offset < len(data) and depth:
                nested_tag, offset = _varint(data, offset)
                nested_wire = nested_tag & 7
                nested_number = nested_tag >> 3
                if nested_wire == 3:
                    depth += 1
                elif nested_wire == 4:
                    depth -= 1
                    if not depth and nested_number != number:
                        raise ValueError("protobuf-group-mismatch")
                elif nested_wire == 0:
                    _unused, offset = _varint(data, offset)
                elif nested_wire == 1:
                    offset += 8
                elif nested_wire == 2:
                    size, offset = _varint(data, offset)
                    offset += size
                elif nested_wire == 5:
                    offset += 4
                else:
                    raise ValueError("protobuf-group-wire-invalid")
                if offset > len(data):
                    raise ValueError("protobuf-group-truncated")
            if depth:
                raise ValueError("protobuf-group-truncated")
            value, wire = data[start:offset], 2
        elif wire == 4:
            raise ValueError("protobuf-unexpected-end-group")
        else:
            raise ValueError("protobuf-wire-invalid")
        yield number, wire, value


def _crc32c(data):
    crc = 0xffffffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82f63b78 if crc & 1 else 0)
    return crc ^ 0xffffffff


def _masked_crc(data):
    crc = _crc32c(data)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def _shape_is_scalar(raw):
    dims = []
    for number, wire, value in _fields(raw):
        if number == 2 and wire == 2:
            size = None
            for dim_no, dim_wire, dim_value in _fields(value):
                if dim_no == 1 and dim_wire == 0:
                    size = dim_value
            if size is None or size < 0:
                return False
            dims.append(size)
    return not dims or math.prod(dims) == 1


def _tensor_scalar(raw):
    dtype, shape, content, repeated = None, None, None, {}
    for number, wire, value in _fields(raw):
        if number == 1 and wire == 0: dtype = value
        elif number == 2 and wire == 2: shape = value
        elif number == 4 and wire == 2: content = value
        elif number in {5, 6, 7, 10}:
            repeated.setdefault(number, []).append((wire, value))
    if shape is not None and not _shape_is_scalar(shape):
        return None
    formats = {1: (5, "f"), 2: (6, "d"), 3: (7, "i"), 9: (10, "q")}
    if dtype not in formats:
        return None
    field, code = formats[dtype]
    size = struct.calcsize("<" + code)
    if content is not None:
        if len(content) != size:
            return None
        return struct.unpack("<" + code, content)[0]
    values = repeated.get(field, [])
    if len(values) != 1:
        return None
    wire, value = values[0]
    if wire == (5 if dtype == 1 else 1 if dtype == 2 else 0):
        if isinstance(value, bytes):
            if len(value) != size:
                return None
            return struct.unpack("<" + code, value)[0]
        return int(value)
    if wire == 2:
        if len(value) != size:
            return None
        return struct.unpack("<" + code, value)[0]
    if dtype in {1, 2}:
        return None
    value = int(value)
    if dtype == 9 and value >= 2**63:
        value -= 2**64
    elif dtype == 3 and value >= 2**31:
        value -= 2**32
    return value


def _tag_split(tag):
    lowered = tag.lower()
    for prefix in ("validation/", "validation_", "valid/", "valid_", "val/", "val_",
                   "train/", "train_", "test/", "test_"):
        if lowered.startswith(prefix):
            return ("valid" if prefix.startswith(("validation", "valid", "val"))
                    else "train" if prefix.startswith("train") else "test",
                    tag[len(prefix):])
    return "", tag


def _event_scalars(payload, path):
    wall, step, result = None, None, []
    for number, wire, value in _fields(payload):
        if number == 1 and wire == 1: wall = struct.unpack("<d", value)[0]
        elif number == 2 and wire == 0: step = value
        elif number == 5 and wire == 2:
            for summary_no, summary_wire, summary_value in _fields(value):
                if summary_no != 1 or summary_wire != 2: continue
                tag, scalar = None, None
                for field_no, field_wire, field_value in _fields(summary_value):
                    if field_no == 1 and field_wire == 2:
                        tag = field_value.decode("utf-8", "replace")
                    elif field_no == 2 and field_wire == 5:
                        scalar = struct.unpack("<f", field_value)[0]
                    elif field_no == 8 and field_wire == 2:
                        scalar = _tensor_scalar(field_value)
                if isinstance(tag, str) and tag and _number(scalar) or \
                        isinstance(tag, str) and tag and isinstance(scalar, float) and not math.isfinite(scalar):
                    split, name = _tag_split(tag)
                    result.append({"epoch": step, "unit": "step", "split": split,
                        "metrics": {name: float(scalar)},
                        "elapsed_seconds": None, "total_epochs": None,
                        "wall_time": wall, "source_path": str(path), "format": "tfevents"})
    return result


def _decode_tfrecord(raw, path, *, with_offset=False):
    samples, warnings, offset = [], [], 0
    while offset < len(raw):
        if len(raw) - offset < 12:
            warnings.append("tfevents-partial-record")
            break
        start = offset
        length_bytes = raw[offset:offset + 8]
        length = struct.unpack("<Q", length_bytes)[0]
        length_crc = struct.unpack("<I", raw[offset + 8:offset + 12])[0]
        offset += 12
        if length > MAX_TFRECORD_BYTES:
            warnings.append("tfevents-record-oversized")
            break
        if length_crc != _masked_crc(length_bytes):
            warnings.append("tfevents-length-crc-invalid")
            break
        if len(raw) - offset < length + 4:
            warnings.append("tfevents-partial-record")
            break
        payload = raw[offset:offset + length]
        data_crc = struct.unpack("<I", raw[offset + length:offset + length + 4])[0]
        if data_crc != _masked_crc(payload):
            warnings.append("tfevents-data-crc-invalid")
            offset += length + 4
            continue
        try:
            samples.extend(_event_scalars(payload, path))
        except (ValueError, UnicodeError, struct.error):
            warnings.append("tfevents-event-invalid")
        offset += length + 4
        if offset <= start:
            break
    return (samples, warnings, offset) if with_offset else (samples, warnings)


def encode_tfrecord(payload):
    """Small fixture helper, also useful to producers testing this reader."""
    size = struct.pack("<Q", len(payload))
    return size + struct.pack("<I", _masked_crc(size)) + payload + \
        struct.pack("<I", _masked_crc(payload))


def decode_tfevents(root: str | Path, cursor=None):
    """Read event files from one ``tensorboard/`` listing, without TensorBoard."""
    root = Path(root).resolve(strict=True)
    directory = root / "tensorboard"
    warnings, samples, paths, entries = [], [], [], 0
    cursor = cursor if isinstance(cursor, dict) else {}
    cursor_out = dict(cursor)
    remaining = MAX_JSONL_BYTES_PER_PASS
    try:
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("tfevents-directory-invalid")
        with os.scandir(directory) as listing:
            for entry in listing:
                entries += 1
                if entries > MAX_DIRECTORY_ENTRIES:
                    warnings.append("tfevents-entry-limit")
                    break
                if not entry.is_file(follow_symlinks=False) or "tfevents" not in entry.name:
                    continue
                path = Path(entry.path)
                try:
                    _check_parents(path, root)
                    before = path.lstat()
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("tfevents-not-regular")
                    prior = cursor.get(entry.name)
                    offset = 0
                    if (isinstance(prior, dict) and prior.get("device") == before.st_dev
                            and prior.get("inode") == before.st_ino
                            and type(prior.get("offset")) is int
                            and 0 <= prior["offset"] <= before.st_size):
                        offset = prior["offset"]
                        anchor_size = prior.get("anchor_size", 0)
                        anchor_hash = prior.get("anchor_sha256")
                        if type(anchor_size) is int and 0 < anchor_size <= min(128, offset) \
                                and isinstance(anchor_hash, str):
                            anchor_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                            with os.fdopen(anchor_fd, "rb") as anchor_stream:
                                anchor_stream.seek(offset - anchor_size)
                                anchor = anchor_stream.read(anchor_size)
                            if hashlib.sha256(anchor).hexdigest() != anchor_hash:
                                warnings.append("tfevents-generation-changed:" + entry.name)
                                offset = 0
                    elif prior:
                        warnings.append("tfevents-generation-changed:" + entry.name)
                    if remaining <= 0:
                        warnings.append("tfevents-pass-byte-limit")
                        break
                    amount = min(remaining, MAX_JSONL_BYTES_PER_PASS)
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as source:
                        first = os.fstat(source.fileno())
                        source.seek(offset)
                        raw = source.read(amount)
                        after = os.fstat(source.fileno())
                    current = path.lstat()
                    if ((first.st_dev, first.st_ino) != (before.st_dev, before.st_ino)
                            or (after.st_dev, after.st_ino) != (first.st_dev, first.st_ino)
                            or (current.st_dev, current.st_ino) != (first.st_dev, first.st_ino)
                            or stat.S_ISLNK(current.st_mode)):
                        raise ValueError("tfevents-path-replaced")
                    values, issues, consumed = _decode_tfrecord(raw, path, with_offset=True)
                    samples.extend(values)
                    warnings.extend(issues)
                    consumed_raw = raw[:consumed]
                    if consumed_raw:
                        anchor = consumed_raw[-128:]
                        anchor_size = len(anchor)
                        anchor_hash = hashlib.sha256(anchor).hexdigest()
                    elif isinstance(prior, dict) and offset == prior.get("offset"):
                        anchor_size = prior.get("anchor_size", 0)
                        anchor_hash = prior.get("anchor_sha256", "")
                    else:
                        anchor_size, anchor_hash, anchor = 0, "", b""
                    cursor_out[entry.name] = {"device": after.st_dev, "inode": after.st_ino,
                        "offset": offset + consumed,
                        "anchor_size": anchor_size, "anchor_sha256": anchor_hash}
                    remaining -= len(raw)
                    paths.append(str(path))
                except (OSError, ValueError):
                    warnings.append("tfevents-unreadable:" + entry.name)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        warnings.append("tfevents-directory-unreadable:" + str(exc))
    return samples, warnings, sorted(paths), cursor_out


def read_sources(root: str | Path, *, cursor=None):
    """Read all fixed sources once; return normalized samples and provenance."""
    root = Path(root).resolve(strict=True)
    cursor = cursor if isinstance(cursor, dict) else {}
    jsonl_cursor = cursor.get("jsonl", cursor)
    events_cursor = cursor.get("tfevents") if isinstance(cursor.get("tfevents"), dict) else {}
    jsonl, next_jsonl_cursor, warnings, jsonl_paths = decode_jsonl(root, jsonl_cursor)
    epoch_samples, progress, issues, epoch_paths = decode_epoch_files(root)
    warnings.extend(issues)
    event_samples, issues, event_paths, next_events_cursor = decode_tfevents(root, events_cursor)
    warnings.extend(issues)
    sources = []
    if jsonl_paths:
        keys = sorted({key for row in jsonl for key in row["metrics"]})
        sources.append({"format": "epochs-jsonl", "path": jsonl_paths[0], "keys": keys})
    if epoch_paths:
        keys = sorted({key for row in epoch_samples for key in row["metrics"]})
        sources.append({"format": "epoch-json-files", "path": epoch_paths, "keys": keys})
    if event_paths:
        keys = sorted({key for row in event_samples for key in row["metrics"]})
        sources.append({"format": "tfevents", "path": event_paths, "keys": keys})
    samples = [*jsonl, *epoch_samples, *event_samples]
    precedence = {"override": 0, "epoch-json-files": 1, "epochs-jsonl": 2, "tfevents": 3}
    selected, conflicts = {}, []
    for row in samples:
        key = (row.get("unit"), row.get("split"), row.get("epoch"))
        for metric, value in row["metrics"].items():
            exact = (*key, metric)
            old = selected.get(exact)
            if old is not None and old["metrics"][metric] != value:
                conflicts.append({"key": exact, "sources": [old["format"], row["format"]]})
            if old is None or precedence.get(row["format"], 99) < precedence.get(old["format"], 99):
                selected[exact] = {**row, "metrics": {metric: value}}
    grouped = {}
    for row in selected.values():
        identity = (row.get("unit"), row.get("split"), row.get("epoch"), row.get("format"))
        current = grouped.get(identity)
        if current is None:
            grouped[identity] = dict(row, metrics=dict(row["metrics"]))
        else:
            current["metrics"].update(row["metrics"])
    return {"samples": list(grouped.values()), "sources": sources,
            "progress": progress, "cursor": {"jsonl": next_jsonl_cursor,
                                                  "tfevents": next_events_cursor},
            "warnings": sorted(set(warnings)), "conflicts": conflicts,
            "coverage": "partial" if "epochs-jsonl-partial-history" in warnings else "observed"}
