#!/usr/bin/env python3
"""Trigger one interactive-session summary refresh outside Fleet."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

HARNESSES = {"claude", "codex", "opencode"}
PHASES = {"initial", "periodic", "final"}
ANCHOR_TEXT_CAP = 2000


def _bounded_anchor(value: str | None) -> str:
    return value[:ANCHOR_TEXT_CAP] if isinstance(value, str) else ""


def _codex_transcript(sid: str) -> Path | None:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    root = home / "sessions"
    if not root.is_dir() or not sid:
        return None
    suffix = "-" + sid + ".jsonl"
    candidates = [path for path in root.rglob("rollout-*.jsonl") if path.name.endswith(suffix)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _opencode_db() -> Path:
    explicit = os.environ.get("OPENCODE_DB")
    if explicit:
        return Path(explicit).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / "opencode" / "opencode.db"
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def trigger(harness: str, sid: str, phase: str, transcript: str | None = None,
            wait_seconds: float = 0.0, after_mtime_ns: int | None = None,
            anchor_text: str | None = None) -> bool:
    if harness not in HARNESSES or phase not in PHASES or not sid:
        return False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    source: Path | None = Path(transcript).expanduser() if transcript else None
    while True:
        if harness == "codex" and source is None:
            source = _codex_transcript(sid)
        if harness == "opencode":
            database = _opencode_db()
            if database.is_file():
                from fleet import refresh_title

                return bool(refresh_title.maybe_spawn(
                    harness=harness,
                    sid=sid,
                    debounce=0 if phase in {"initial", "final"} else 90,
                    priority=phase in {"initial", "final"},
                    quota_class=phase if phase in {"initial", "final"} else None,
                    refresh_source={"kind": "opencode-db", "db_path": str(database)},
                    anchor_text=_bounded_anchor(anchor_text),
                ))
        elif source is not None and source.is_file():
            if after_mtime_ns is not None:
                try:
                    source_ready = source.stat().st_mtime_ns > after_mtime_ns
                except OSError:
                    source_ready = False
                if not source_ready:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.1)
                    continue
            from fleet import refresh_title

            return bool(refresh_title.maybe_spawn(
                harness=harness,
                sid=sid,
                transcript=str(source),
                debounce=0 if phase in {"initial", "final"} else 90,
                priority=phase in {"initial", "final"},
                quota_class=phase if phase in {"initial", "final"} else None,
                anchor_text=_bounded_anchor(anchor_text),
            ))
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def launch_trigger(harness: str, sid: str, phase: str, transcript: str | None = None,
                   anchor_text: str | None = None) -> bool:
    """Launch the bounded trigger without adding latency to a runtime hook."""
    if harness not in HARNESSES or phase not in PHASES or not sid:
        return False
    # Codex UserPromptSubmit runs before the new user message is durable in the
    # rollout.  Gate the detached initial refresh on a post-hook source mtime so
    # it cannot summarize the preceding turn under the current session card.
    after_mtime_ns = time.time_ns() if harness == "codex" and phase == "initial" else None
    argv = [
        sys.executable, str(Path(__file__).resolve()),
        "--harness", harness, "--sid", sid, "--phase", phase,
        "--wait", "5" if phase == "initial" else "1",
    ]
    if after_mtime_ns is not None:
        argv.extend(["--after-mtime-ns", str(after_mtime_ns)])
    if transcript:
        argv.extend(["--transcript", transcript])
    bounded_anchor = _bounded_anchor(anchor_text)
    if bounded_anchor:
        argv.append("--anchor-stdin")
    env = dict(os.environ)
    env["AGENT_SESSION_ROLE"] = "worker"
    try:
        process = subprocess.Popen(
            argv, cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE if bounded_anchor else subprocess.DEVNULL,
            text=bool(bounded_anchor),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    if bounded_anchor:
        try:
            process.stdin.write(bounded_anchor)
            process.stdin.close()
        except (AttributeError, BrokenPipeError, OSError):
            # The detached trigger still has the transcript fallback.
            pass
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    parser.add_argument("--sid", required=True)
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--transcript")
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--after-mtime-ns", type=int)
    parser.add_argument("--anchor-stdin", action="store_true")
    args = parser.parse_args(argv)
    anchor_text = sys.stdin.read(ANCHOR_TEXT_CAP) if args.anchor_stdin else None
    trigger(
        args.harness, args.sid, args.phase, args.transcript,
        wait_seconds=args.wait, after_mtime_ns=args.after_mtime_ns,
        anchor_text=anchor_text,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
