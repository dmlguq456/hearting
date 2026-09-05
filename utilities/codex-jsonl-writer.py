#!/usr/bin/env python3
"""Run a child and append timestamped JSONL output without hiding its status."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path


def _timestamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _usage(event: object) -> dict[str, int] | None:
    if not isinstance(event, dict):
        return None
    usage = event.get("usage")
    if not isinstance(usage, dict):
        token = event.get("token_usage")
        usage = token.get("token_usage") if isinstance(token, dict) else None
    if not isinstance(usage, dict):
        return None
    aliases = {
        "input": ("input", "input_tokens"),
        "cached_input": ("cached-input", "cached_input", "cached_input_tokens"),
        "output": ("output", "output_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
        "total": ("total", "total_tokens"),
    }
    result: dict[str, int] = {}
    for name, keys in aliases.items():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[name] = value
                break
    return result or None


def _usage_path(log: Path, attempt: str) -> Path | None:
    if not attempt:
        return None
    # The codex log convention is "<slug>.<attempt>.codex.jsonl"; decompose that
    # exact suffix so the sidecar lands at "<slug>.<attempt>.codex.usage.json"
    # instead of duplicating the attempt segment via log.stem (which already
    # contains it).  Any other log name falls back to appending the attempt.
    known_suffix = f".{attempt}.codex.jsonl"
    if log.name.endswith(known_suffix):
        return log.with_name(log.name[: -len(".jsonl")] + ".usage.json")
    return log.with_name(f"{log.stem}.{attempt}.usage.json")


def run(log: Path, attempt: str, argv: list[str]) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    usage_path = _usage_path(log, attempt)
    child = subprocess.Popen(argv, stdin=sys.stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    found_usage: dict[str, int] = {}
    # Defect L: this log is a live feed, not just a post-mortem record. Fleet's
    # `_parse_codex_attempt_tail` reads it while the child runs to recover the
    # thread id (and from there the rollout that carries the context window).
    # Python's default 8 KiB block buffer kept a real attempt log at 0 bytes for
    # 19 minutes while the child streamed one long response, so Fleet found no
    # `thread.started`, never reached the rollout fallback, and rendered no ctx.
    #
    # Buffered plus an explicit per-line flush, NOT `buffering=0`: the unbuffered
    # form returns a raw `FileIO` whose `write()` may legally short-write, and the
    # return value here is ignored. This log lives on an NFS mount, where that
    # guarantee is weakest. Keeping the BufferedWriter keeps its short-write loop;
    # the flush gives the same visibility.
    with log.open("ab") as out:
        assert child.stdout is not None
        for raw in iter(child.stdout.readline, b""):
            line = raw.rstrip(b"\r\n")
            ending = raw[len(line):]
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                out.write(raw)
                out.flush()
                continue
            if not isinstance(event, dict):
                out.write(raw)
                out.flush()
                continue
            if "timestamp" not in event:
                event["timestamp"] = _timestamp()
            usage = _usage(event)
            if usage:
                found_usage.update(usage)
            out.write((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + ending.decode("ascii", "ignore")).encode("utf-8"))
            out.flush()
        child.wait()
    if usage_path is not None and found_usage:
        tmp = usage_path.with_suffix(usage_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"attempt_id": attempt, "usage": found_usage}, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, usage_path)
    return child.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--attempt", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command or args.command[0] != "--":
        parser.error("a child command after -- is required")
    return run(args.log, args.attempt, args.command[1:])


if __name__ == "__main__":
    raise SystemExit(main())
