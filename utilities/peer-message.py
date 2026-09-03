#!/usr/bin/env python3
"""SD-122 peer-session steward ledger: record | list | status.

Body text is never persisted. Callers pass the body via --body-file or
stdin; only its sha256 and a hard-truncated first-line summary are written.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

_UTILITIES_DIR = str(Path(__file__).resolve().parent)
if _UTILITIES_DIR not in sys.path:
    sys.path.insert(0, _UTILITIES_DIR)

from dispatch_contract import resolve_dispatch_state_root  # noqa: E402

_KINDS = ("watch", "steer", "handoff", "gate-relay", "notice")
_SURFACES = (
    "claude-native", "herdr", "codex-queue", "codex-gateway-steer",
    "opencode-unknown", "manual-paste",
)
_STATUSES = ("sent", "delivered", "received", "failed", "unknown")
_SUMMARY_MAX = 200
# F-100c — kinds whose SENT record marks the sender as a steward (depth −1) of the target.
_STEWARD_KINDS = ("steer", "handoff", "gate-relay", "watch")
_STEWARD_TARGETS_MAX = 32

# F-100c — the harness-neutral sender trailer a steward appends to a herdr prompt so the
# RECEIVER (Claude UserPromptSubmit hook, Codex userprompt hook, OpenCode plugin) can write
# its own `notice` record with an exact sender: `(peer-from: <harness> <session_id> <name>)`.
# Native Claude SendMessage already wraps its delivery in <cross-session-message from=…>;
# a herdr prompt has no envelope, so the trailer is the envelope.
_PEER_TRAILER_RE = re.compile(
    r"\(peer-from:\s*(?P<harness>[A-Za-z0-9_-]+)\s+(?P<sid>[^\s)]+)(?:\s+(?P<name>[^)]*?))?\s*\)")


def peer_trailer(harness, session_id, name=None):
    """The trailer line a steward appends to a herdr prompt body."""
    parts = [str(harness or "unknown"), str(session_id or "-")]
    clean = " ".join(str(name or "").replace(")", " ").split())
    if clean:
        parts.append(clean)
    return "(peer-from: %s)" % " ".join(parts)


def parse_peer_trailer(text):
    """→ {harness, session_id, name} for the LAST trailer in ``text``, else ``None``."""
    if not isinstance(text, str) or "peer-from:" not in text:
        return None
    match = None
    for match in _PEER_TRAILER_RE.finditer(text):
        pass
    if match is None:
        return None
    return {
        "harness": match.group("harness").lower(),
        "session_id": match.group("sid"),
        "name": (match.group("name") or "").strip() or None,
    }


def claude_session_name(session_id, config_dir=None):
    """The Claude Code registry ``name`` (``hearting-46``) for ``session_id`` from
    ``<CLAUDE_CONFIG_DIR|~/.claude>/sessions/*.json``; ``None`` when unknown. Stable
    across title changes, which is why the ledger carries it and not the AI title."""
    if not session_id:
        return None
    home = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        entries = os.listdir(os.path.join(home, "sessions"))
    except OSError:
        return None
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        try:
            with open(os.path.join(home, "sessions", entry), encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("sessionId") == session_id:
            name = rec.get("name")
            return name if isinstance(name, str) and name else None
    return None


def _agent_home():
    home = os.environ.get("AGENT_HOME")
    return Path(home) if home else Path.cwd()


def _ledger_root():
    return resolve_dispatch_state_root(_agent_home(), explicit_jobs=None, environ=os.environ)


def _ledger_path(from_session_id, when=None):
    when = when or time.gmtime()
    month = time.strftime("%Y-%m", when)
    root = _ledger_root() / "peer-messages" / month
    return root / f"{from_session_id}.jsonl"


def steward_marker_path(harness, session_id):
    """F-100c steward flag: ``<dispatch-state-root>/peer-steward/<harness>/<sid>.json``.
    Written by every SENT steer/handoff/gate-relay/watch record (so the wrapper, the
    Claude SendMessage hook and a manual `record` all raise the same flag), read by the
    Fleet steward collector, cleared by `peer-message release`."""
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(session_id or ""))
    return _ledger_root() / "peer-steward" / str(harness or "unknown") / f"{safe or 'unknown'}.json"


def mark_steward(harness, session_id, to, kind, ts):
    if not session_id:
        return False
    path = steward_marker_path(harness, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = {"schema_version": 1, "harness": harness, "session_id": session_id,
                    "since": ts, "targets": {}}
        targets = data.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        key = to.get("session_id") or to.get("name") or "-"
        targets[key] = {"harness": to.get("harness"), "session_id": to.get("session_id"),
                        "name": to.get("name"), "kind": kind, "ts": ts}
        if len(targets) > _STEWARD_TARGETS_MAX:
            oldest = sorted(targets, key=lambda k: targets[k].get("ts") or "")
            for k in oldest[: len(targets) - _STEWARD_TARGETS_MAX]:
                targets.pop(k, None)
        data["targets"] = targets
        data["updated"] = ts
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def read_steward_markers(roots=None):
    """``{(harness, session_id): marker_dict}`` over every marker under the ledger
    root(s). ``roots`` = iterable of dispatch state roots (F-100c: Fleet passes every
    installed runtime's own root as well); default = this process's own root."""
    out = {}
    root_list = [Path(r) for r in roots] if roots else [_ledger_root()]
    for base in root_list:
        root = base / "peer-steward"
        try:
            harness_dirs = list(root.iterdir())
        except OSError:
            continue
        for hdir in harness_dirs:
            if not hdir.is_dir():
                continue
            for f in hdir.glob("*.json"):
                try:
                    with open(f, encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("session_id"):
                    key = (hdir.name, str(data["session_id"]))
                    if key not in out or (data.get("updated") or "") > (out[key].get("updated") or ""):
                        out[key] = data
    return out


def _message_id(from_sid, to, ts, summary):
    # `ts` is second-resolution (its documented output shape is fixed), so two
    # records for the same sender/target/summary in the same second would
    # otherwise collide. `time.time_ns()` gives each call its own
    # sub-second/entropy component in the digest input without touching the
    # `ts` field itself.
    to_key = to.get("session_id") or to.get("name") or ""
    raw = f"{from_sid}|{to_key}|{ts}|{summary}|{time.time_ns()}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _read_body(args):
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8", errors="replace")
    if args.body_stdin:
        return sys.stdin.read()
    return ""


def cmd_record(args):
    if args.kind not in _KINDS:
        print("peer-message: invalid-kind", file=sys.stderr)
        return 1
    if args.surface not in _SURFACES:
        print("peer-message: invalid-surface", file=sys.stderr)
        return 1
    if args.status not in _STATUSES:
        print("peer-message: invalid-status", file=sys.stderr)
        return 1
    try:
        body = _read_body(args)
        first_line = body.splitlines()[0] if body else ""
        summary = first_line[:_SUMMARY_MAX]
        body_sha256 = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        to = {"harness": args.to_harness}
        if args.to_session_id:
            to["session_id"] = args.to_session_id
        if args.to_name:
            to["name"] = args.to_name
        from_block = {
            "harness": args.from_harness,
            "session_id": args.from_session_id,
            "project": args.from_project,
        }
        from_name = getattr(args, "from_name", None)
        if from_name:
            from_block["name"] = str(from_name)
        kind = args.kind
        rec = {
            "schema_version": 1,
            "message_id": _message_id(args.from_session_id, to, ts, summary),
            "ts": ts,
            "from": from_block,
            "to": to,
            "kind": kind,
            "summary": summary,
            "body_sha256": body_sha256,
            "delivery": {
                "surface": args.surface,
                "status": args.status,
                "receipt": args.receipt,
            },
            "refs": list(args.ref or []),
        }
        path = _ledger_path(args.from_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        if args.status == "sent" and kind in _STEWARD_KINDS:
            mark_steward(args.from_harness, args.from_session_id, to, kind, ts)
        return 0
    except Exception as exc:
        print(f"peer-message record failed: {exc}", file=sys.stderr)
        return 1


def cmd_release(args):
    """Clear the steward flag for one session (the ledger itself is append-only)."""
    path = steward_marker_path(args.harness, args.session_id)
    try:
        path.unlink()
        print("released")
    except FileNotFoundError:
        print("not-marked")
    except Exception as exc:
        print(f"peer-message release failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _iter_records(since_hours=None):
    root = _ledger_root() / "peer-messages"
    if not root.is_dir():
        return
    cutoff = None
    if since_hours is not None:
        cutoff = time.time() - since_hours * 3600
    for month_dir in sorted(root.glob("*")):
        if not month_dir.is_dir():
            continue
        for f in sorted(month_dir.glob("*.jsonl")):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if cutoff is not None:
                            try:
                                ts = time.strptime(rec.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ")
                                if time.mktime(ts) - time.timezone < cutoff:
                                    continue
                            except Exception:
                                pass
                        yield rec
            except Exception:
                continue


def cmd_list(args):
    try:
        recs = list(_iter_records(since_hours=args.since_hours))
        if args.limit:
            recs = recs[-args.limit:]
        for rec in recs:
            print(json.dumps(rec, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"peer-message list failed: {exc}", file=sys.stderr)
        return 1


def cmd_status(args):
    try:
        recs = list(_iter_records(since_hours=args.since_hours))
        print(json.dumps({"count": len(recs)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"peer-message status failed: {exc}", file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="peer-message")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_record = sub.add_parser("record")
    p_record.add_argument("--from-harness", required=True)
    p_record.add_argument("--from-session-id", required=True)
    p_record.add_argument("--from-project", default="")
    p_record.add_argument("--from-name", default=None)
    p_record.add_argument("--to-harness", required=True)
    p_record.add_argument("--to-session-id", default=None)
    p_record.add_argument("--to-name", default=None)
    p_record.add_argument("--kind", default="steer")
    p_record.add_argument("--surface", required=True)
    p_record.add_argument("--status", default="sent")
    p_record.add_argument("--receipt", default=None)
    p_record.add_argument("--ref", action="append", default=[])
    p_record.add_argument("--body-file", default=None)
    p_record.add_argument("--body-stdin", action="store_true")
    p_record.set_defaults(func=cmd_record)

    p_list = sub.add_parser("list")
    p_list.add_argument("--since-hours", type=float, default=None)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status")
    p_status.add_argument("--since-hours", type=float, default=1.0)
    p_status.set_defaults(func=cmd_status)

    p_release = sub.add_parser("release")
    p_release.add_argument("--harness", required=True)
    p_release.add_argument("--session-id", required=True)
    p_release.set_defaults(func=cmd_release)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
