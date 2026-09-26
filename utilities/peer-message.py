#!/usr/bin/env python3
"""SD-122 peer-session steward ledger: record | list | status | release | prune-steward-markers.

Body text is never persisted. Callers pass the body via --body-file or
stdin; only its sha256 and a hard-truncated first-line summary are written.
"""
import argparse
import functools
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

_UTILITIES_DIR = str(Path(__file__).resolve().parent)
if _UTILITIES_DIR not in sys.path:
    sys.path.insert(0, _UTILITIES_DIR)

from dispatch_contract import resolve_dispatch_state_root, stable_state_root  # noqa: E402

_KINDS = ("watch", "steer", "handoff", "gate-relay", "notice")
_SURFACES = (
    "claude-native", "herdr", "codex-queue", "codex-gateway-steer",
    "opencode-unknown", "manual-paste",
)
_STATUSES = ("sent", "delivered", "received", "failed", "unknown")
_SUMMARY_MAX = 200
# F-100c-2 (2026-09-06, fleet-steward-role) — the steward flag (depth −1) is a ROLE, not a
# side effect of talking. A marker target counts as steward evidence only when its
# `source` is one of `_STEWARD_SOURCES`: `explicit` (`peer-steward.py steward on`),
# `watch` (`peer-steward.py wait`/`watch` — written by that surface AFTER herdr answered
# about a real target) or `start` (`peer-steward.py start` received an error-free agent
# block). **No `record` path raises the flag, whatever the kind**: a SendMessage with
# `notify_when_idle` is recorded as `kind=watch` by the Claude hook, and a worker's
# `[handoff]` to its steward is a message — under the old rule ("SENT steer/handoff/
# gate-relay/watch = steward") every worker that talked looked like a steward (13 markers
# measured on the real root, most of them handoff-only). Send counts stay in F-98a
# `peer_sent_1h`. The role claim lives only in `peer-steward.py`, at the points where
# something was actually observed or launched (review round 1, #1/#2).
_STEWARD_SOURCES = ("explicit", "watch", "start")
# Legacy entries (written before `source` existed) are judged by their kind alone:
# `wait`/`watch` wrote kind=watch, and so did the old `steward on` placeholder. The old
# `start` was recorded as a `steer` row and is intentionally not evidence (a restart
# raises it again with source=start).
_STEWARD_LEGACY_KINDS = ("watch",)
_STEWARD_TARGETS_MAX = 32

# F-100c — the harness-neutral sender trailer a steward appends to a herdr prompt so the
# RECEIVER (Claude UserPromptSubmit hook, Codex userprompt hook, OpenCode plugin) can write
# its own `notice` record with an exact sender. New trailers display [tag] and
# an opaque transfer ref; legacy full-session-id trailers remain receive-only.
# Native Claude SendMessage already wraps its delivery in <cross-session-message from=…>;
# a herdr prompt has no envelope, so the trailer is the envelope.
_PEER_TRAILER_RE = re.compile(
    r"\(peer-from:\s*(?P<harness>[A-Za-z0-9_-]+)\s+(?P<sid>[^\s)]+)(?:\s+(?P<name>[^)]*?))?\s*\)")


def _session_handle():
    """``fleet.session_handle``, the one home of the shared identity rules, or ``None``.

    Imported lazily and by path: this tool ships next to `tools/` in both the checkout and
    an installed release, but it is a standalone script and never runs as part of that
    package.
    """
    try:
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet import session_handle
        return session_handle
    except Exception:
        return None


def peer_alias(harness, session_id):
    """Fleet's display tag only; never resolve a tag/name/pane back to an id.

    The badge rule itself lives in ``fleet.session_handle.resolve_tag`` — the trailer and
    the board have to agree on what `[46]` means, so there is one definition, not a copy
    here that can drift away from the one Fleet draws.
    """
    if not usable_session_id(session_id):
        return "[?]"
    handle = _session_handle()
    if handle is None:
        return "[?]"
    try:
        tag = handle.resolve_tag(harness, session_id)
    except Exception:
        return "[?]"
    return f"[{tag}]" if isinstance(tag, str) and re.fullmatch(r"[0-9a-f]{2}", tag) else "[?]"


def peer_trailer(harness, session_id, name=None, *, transfer_ref=None):
    """Human display; old receivers reject bracketed tags as session identities."""
    parts = [str(harness or "unknown"), peer_alias(harness, session_id)]
    clean = " ".join(str(name or "").replace(")", " ").replace(";", " ").split())
    if session_id:
        clean = clean.replace(str(session_id), parts[1])
    if clean:
        parts.append(clean)
    suffix = f" ; ref={transfer_ref}" if transfer_ref else ""
    return "(peer-from: %s%s)" % (" ".join(parts), suffix)


def _transfer_path(ref):
    # No caller-controlled paths, legacy ids, or arbitrary-length lookup keys.
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{32}", ref):
        raise ValueError("invalid-peer-transfer-ref")
    return peer_state_root() / "peer-messages" / "transfers" / f"{ref}.json"


def _read_transfer_record(ref):
    """Read Fleet's trusted ledger roots; a message never supplies a path.

    Each writer keeps its canonical jobs root. Multiple records for one ref
    are ambiguous, even when one of them would match the recipient.
    """
    own_path = _transfer_path(ref)  # Validate before querying any roots.
    roots, _source = steward_marker_roots()
    paths = [own_path, *(Path(root) / "peer-messages" / "transfers" / own_path.name for root in roots)]
    record, found, seen = None, False, set()
    for path in paths:
        root = path.parent.parent.parent.resolve()
        path = root / "peer-messages" / "transfers" / own_path.name
        if path in seen:
            continue
        seen.add(path)
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError("peer-transfer-symlink")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            data = fh.read(65537)
            if len(data) > 65536 or found:
                raise ValueError("peer-transfer-ambiguous-or-oversized")
            record, found = json.loads(data), True
    return record


def _valid_transfer_endpoint(endpoint):
    if not isinstance(endpoint, dict) or endpoint.get("harness") not in ("claude", "codex", "opencode"):
        return False
    sid, name = endpoint.get("session_id"), endpoint.get("name")
    return (isinstance(sid, str) and bool(sid) and usable_session_id(sid) == sid
            and (name is None or isinstance(name, str)))


def prepare_peer_message(body, sender, recipient):
    """Seal a transmission intent before delivery (the receiver may run immediately).

    This immutable record shares the ledger's local write trust boundary; it is
    not an authorization token. The final ledger row refers to this message_id.
    Full identities live here, never in the generated display trailer.
    """
    ref = secrets.token_hex(16)
    text = body.rstrip("\n") + "\n\n" + peer_trailer(
        sender.get("harness"), sender.get("session_id"), sender.get("name"), transfer_ref=ref)
    record = {"schema_version": 1, "message_id": ref, "from": dict(sender),
              "to": dict(recipient), "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    path = _transfer_path(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents rebinding an existing transfer. Readers hold a
    # shared lock, so a partially written intent never becomes attribution.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        json.dump(record, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    return text, ref


def usable_session_id(value):
    """The session id if it can serve as an identity key, else ``None``.

    F-101i makes `(harness, session_id)` the identity key, so a value that cannot
    identify anything must not enter the ledger as if it could. Two shapes reach here
    in practice: the `-` this module itself writes for "no id", and an unsubstituted
    template like `<sid>` from a sender that emitted the trailer's documentation form
    verbatim (measured 2026-09-03 — four such records, and a `<sid>.jsonl` ledger shard
    beside the real ones). Both are treated as absent, which is the same fail-soft the
    Claude native path already has: `✉` counters and `notice` keep working, the exact
    link does not appear, and nothing claims an endpoint it cannot prove.

    Also the path guard for `_ledger_path`: the id becomes a filename there, so a value
    carrying `/` or `..` must never reach it. `steward_marker_path` already sanitizes;
    this keeps the two writers consistent.
    """
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    if not all(ch.isalnum() or ch in "._-" for ch in text):
        return None
    if text.strip(".") == "":
        return None
    return text


def parse_peer_trailer(text, recipient=None):
    """→ {harness, session_id, name} for the LAST trailer in ``text``, else ``None``.

    New aliases require a sealed transfer and the actual recipient tuple. A
    missing/mismatched record yields session_id=None. Legacy full-id trailers
    retain the existing receive semantics, never a fallback for a bad alias.
    """
    if not isinstance(text, str) or "peer-from:" not in text:
        return None
    match = None
    for match in _PEER_TRAILER_RE.finditer(text):
        pass
    if match is None:
        return None
    result = {
        "harness": match.group("harness").lower(),
        "session_id": usable_session_id(match.group("sid")),
        "name": (match.group("name") or "").strip() or None,
    }
    if match.group("sid").startswith("["):
        display_name, sep, ref = (match.group("name") or "").rpartition("; ref=")
        if sep:
            result["name"] = display_name.strip() or None
        # A missing display tag does not decide identity. Malformed metadata,
        # wrong actual recipient and changed bodies remain unattributed.
        if not _valid_transfer_endpoint(recipient) or not re.fullmatch(r"\[(?:[0-9a-f]{2}|\?)\]", match.group("sid")):
            return result
        try:
            record = _read_transfer_record(ref)
            sender, target = record["from"], record["to"]
            if (not _valid_transfer_endpoint(sender) or not _valid_transfer_endpoint(target)
                    or type(record.get("schema_version")) is not int or record["schema_version"] != 1
                    or record.get("message_id") != ref
                    or record.get("body_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()
                    or sender.get("harness") != result["harness"]
                    or any(target.get(k) != recipient.get(k) for k in ("harness", "session_id"))):
                return result
            result.update(session_id=sender["session_id"], name=sender.get("name"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return result


def cmd_receive(args):
    """Canonical receive boundary for adapters without an in-process parser."""
    text = sys.stdin.read()
    trailer = parse_peer_trailer(text, {"harness": args.to_harness, "session_id": args.to_session_id})
    if not trailer or not usable_session_id(args.to_session_id):
        return 0
    return cmd_record(argparse.Namespace(
        from_harness=trailer["harness"], from_session_id=trailer["session_id"],
        from_name=trailer["name"], from_project=args.from_project,
        to_harness=args.to_harness, to_session_id=args.to_session_id, to_name=None,
        kind="notice", surface="herdr", status="received", receipt=None, ref=[],
        body_file=None, body_stdin=False))


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


def claude_session_id_for_address(address, config_dir=None):
    """A peer socket address → that session's id; the rule lives in
    ``fleet.session_handle.session_id_for_address`` so the ledger writer and the board
    cannot disagree about who a socket belongs to."""
    handle = _session_handle()
    if handle is None:
        return None
    try:
        return usable_session_id(handle.session_id_for_address(address, config_dir=config_dir))
    except Exception:
        return None


def _agent_home():
    home = os.environ.get("AGENT_HOME")
    return Path(home) if home else Path.cwd()


def _ledger_root():
    return resolve_dispatch_state_root(_agent_home(), explicit_jobs=None, environ=os.environ)


def peer_state_root():
    """The one canonical root for the SD-122 peer-message ledger (plan.md C-1).

    Kept separate from the dispatch registry root on purpose: a Codex managed
    session inherits ``AGENT_DISPATCH_JOBS`` pointing at its own tree
    (``~/.codex/.harness/dispatch``), and ``_ledger_root()`` follows that same
    chain — so anchoring the ledger to it forks the ledger into two roots
    (plan.md §2.2). Chain: ``AGENT_PEER_LEDGER_ROOT`` (test/ops override) ->
    ``stable_state_root(os.environ)`` (the per-user root, independent of any
    dispatch registry selection) -> ``_ledger_root()`` (only if both above
    fail). Reads stay tolerant of the old root (`steward_marker_roots`,
    `_iter_records`) — this function decides only where NEW records land.
    """
    override = os.environ.get("AGENT_PEER_LEDGER_ROOT")
    if override:
        return Path(override)
    try:
        return stable_state_root(os.environ)
    except Exception:
        return _ledger_root()


def _ledger_path(from_session_id, when=None):
    when = when or time.gmtime()
    month = time.strftime("%Y-%m", when)
    root = peer_state_root() / "peer-messages" / month
    # The id is a filename here. An unusable one shards to `unknown` instead of minting
    # a junk sibling of the real per-session shards (or, with a separator in it, a path
    # outside the month directory at all).
    return root / f"{usable_session_id(from_session_id) or 'unknown'}.jsonl"


def steward_marker_path(harness, session_id):
    """F-100c steward flag: ``<dispatch-state-root>/peer-steward/<harness>/<sid>.json``.
    Raised only by `peer-steward.py` (`wait`/`watch` after observing a real target,
    `start` after an error-free launch, `steward on`; see `_STEWARD_SOURCES`), never by
    `record`; read by the Fleet steward collector through `steward_evidence_targets`,
    cleared by `peer-message release`, and swept of evidence-less leftovers by
    `peer-message prune-steward-markers`."""
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(session_id or ""))
    return peer_state_root() / "peer-steward" / str(harness or "unknown") / f"{safe or 'unknown'}.json"


def mark_steward(harness, session_id, to, kind, ts, source=None):
    """Raise (or extend) the session's steward marker with one evidence entry.

    ``source`` is required and says why the entry counts (`explicit` | `watch` |
    `start`); anything else — including a missing source — is refused: returns False
    and writes nothing, so no send path can raise the flag by accident again. Every
    entry carries its ``source`` so the reader can show the grounds."""
    if not session_id or source not in _STEWARD_SOURCES:
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
                        "name": to.get("name"), "kind": kind, "ts": ts, "source": source}
        if len(targets) > _STEWARD_TARGETS_MAX:
            oldest = sorted(targets, key=lambda k: targets[k].get("ts") or "")
            for k in oldest[: len(targets) - _STEWARD_TARGETS_MAX]:
                targets.pop(k, None)
        data["targets"] = targets
        data["updated"] = ts
        # schema_version 2 = every entry carries `source`. A legacy file that keeps a
        # source-less entry stays at its own version (the rule is per entry anyway).
        if all(isinstance(e, dict) and e.get("source") is not None for e in targets.values()):
            data["schema_version"] = 2
        else:
            data["schema_version"] = data.get("schema_version") or 1
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        # destructive-ok: reason=atomic publish of the rewritten steward marker (tmp → final); boundary=<dispatch-state-root>/peer-steward/<harness>/<sid>.json
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def steward_evidence_targets(marker):
    """The target entries of ``marker`` that prove the steward role, oldest first.

    An entry counts when its ``source`` is in `_STEWARD_SOURCES`; a legacy entry with no
    ``source`` counts only when its ``kind`` is in `_STEWARD_LEGACY_KINDS`. An empty
    list means the marker is not steward evidence — typically a leftover the old rule
    raised on a handoff/steer send — and every reader treats it as absent, so old
    markers become harmless without a migration. This is the ONE definition of the
    rule; the Fleet collector calls it rather than restating it."""
    if not isinstance(marker, dict):
        return []
    targets = marker.get("targets")
    if not isinstance(targets, dict):
        return []
    out = []
    for entry in targets.values():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if source is not None:
            if source in _STEWARD_SOURCES:
                out.append(entry)
        elif entry.get("kind") in _STEWARD_LEGACY_KINDS:
            out.append(entry)
    out.sort(key=lambda t: str(t.get("ts") or ""))
    return out


def is_steward_marker(marker):
    return bool(steward_evidence_targets(marker))


def _iter_marker_files(base):
    """Yield ``(harness, path, data)`` for every ``peer-steward/<harness>/*.json`` under
    the dispatch state root ``base``; ``data`` is None when the file is unreadable."""
    root = Path(base) / "peer-steward"
    try:
        harness_dirs = sorted(root.iterdir())
    except OSError:
        return
    for hdir in harness_dirs:
        if not hdir.is_dir():
            continue
        for f in sorted(hdir.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                data = None
            yield hdir.name, f, data if isinstance(data, dict) else None


def steward_marker_roots():
    """``(roots, source)`` — the dispatch state roots a marker sweep must cover, which is
    the SAME set the Fleet steward collector reads (`fleet.collectors.peer_messages
    ._state_roots`: the writer's resolver chain plus every installed runtime's own
    root). Two markers under `~/.codex/.harness/dispatch` were unreachable from the
    single-root default (review round 1, #3). Falls back to the resolver chain alone,
    then to this process's own root; ``source`` names which one applied so the sweep's
    summary can say it."""
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    try:
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet.collectors import peer_messages as _fleet_pm  # noqa: WPS433
        roots = [Path(r) for r in (_fleet_pm._state_roots() or ())]
        if roots:
            return roots, "fleet-reader"
    except Exception:
        pass
    try:
        from dispatch_contract import dispatch_state_roots  # noqa: WPS433
        roots = [Path(r) for r in dispatch_state_roots(
            _agent_home(), jobs=os.environ.get("AGENT_DISPATCH_JOBS"), environ=os.environ)]
        if roots:
            return roots, "dispatch-chain"
    except Exception:
        pass
    return [peer_state_root()], "own-root"


def read_steward_markers(roots=None):
    """``{(harness, session_id): marker_dict}`` over every marker under the ledger
    root(s). ``roots`` = iterable of dispatch state roots (F-100c: Fleet passes every
    installed runtime's own root as well); default = this process's own root.

    A marker with no steward evidence (`steward_evidence_targets` empty) is left out,
    so every consumer is defended against leftovers of the old send-marks rule. The
    prune surface walks the files itself (`_iter_marker_files`) because it needs paths."""
    out = {}
    root_list = [Path(r) for r in roots] if roots else [peer_state_root()]
    for base in root_list:
        for harness, _path, data in _iter_marker_files(base):
            if data is None or not data.get("session_id"):
                continue
            if not is_steward_marker(data):
                continue
            key = (harness, str(data["session_id"]))
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
        # Normalize both endpoints at the one writer every harness goes through, so a
        # placeholder never becomes an identity key downstream (F-101i).
        args.from_session_id = usable_session_id(args.from_session_id) or ""
        to = {"harness": args.to_harness}
        if usable_session_id(args.to_session_id):
            to["session_id"] = usable_session_id(args.to_session_id)
        if args.to_name:
            to["name"] = args.to_name
        to_pane = getattr(args, "to_pane", None)
        if to_pane:
            # SD-122 (11): the exact herdr pane the text was typed into -- the
            # one field the herdr server log itself does not keep.
            to["pane"] = str(to_pane)
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
        transfer_ref = getattr(args, "transfer_ref", None)
        if transfer_ref:
            _transfer_path(transfer_ref)
            rec["message_id"] = transfer_ref
            rec["transfer_ref"] = transfer_ref
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
        # No send path raises the steward flag (F-100c-2): `peer-steward.py` marks after
        # it observed or launched something. A `kind=watch` row here may be a plain
        # SendMessage with `notify_when_idle`, which is not a role.
        return 0
    except Exception as exc:
        print(f"peer-message record failed: {exc}", file=sys.stderr)
        return 1


def cmd_release(args):
    """Clear the steward flag for one session (the ledger itself is append-only)."""
    path = steward_marker_path(args.harness, args.session_id)
    try:
        # destructive-ok: reason=caller-requested release of its own steward flag (steward off); boundary=<dispatch-state-root>/peer-steward/<harness>/<sid>.json
        path.unlink()
        print("released")
    except FileNotFoundError:
        print("not-marked")
    except Exception as exc:
        print(f"peer-message release failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_prune_steward_markers(args):
    """List (default, dry-run) or delete (`--apply`) markers that hold no steward
    evidence — the leftovers an earlier version raised on every SENT steer/handoff/
    gate-relay. Roots default to the Fleet reader's own set (`steward_marker_roots`)
    and are printed one per line. Unreadable markers are counted and never touched;
    each candidate is re-read right before its unlink so a flag raised meanwhile
    survives. Exit: dry-run 2 when candidates exist (0 when clean); apply 0 when every
    candidate was removed or kept-now-evidenced, 1 when an unlink failed."""
    explicit = [Path(r) for r in (getattr(args, "root", None) or [])]
    roots, roots_source = (explicit, "explicit") if explicit else steward_marker_roots()
    mode = "apply" if args.apply else "dry-run"
    total = unreadable = removed = 0
    stale = []
    for base in roots:
        print(f"root={base}")
        for harness, path, data in _iter_marker_files(base):
            total += 1
            if data is None:
                unreadable += 1
                continue
            if is_steward_marker(data):
                continue
            entries = data.get("targets") if isinstance(data.get("targets"), dict) else {}
            kinds = sorted({str(e.get("kind")) for e in entries.values() if isinstance(e, dict)})
            stale.append((harness, str(data.get("session_id") or path.stem), len(entries), kinds, path))
    rc = 0
    for harness, sid, count, kinds, path in stale:
        verdict = "candidate"
        if args.apply:
            verdict = _unlink_unevidenced_marker(path)
            if verdict == "removed":
                removed += 1
            elif verdict.startswith("failed"):
                rc = 1
        print(f"{verdict} {harness} {sid} entries={count} kinds={','.join(kinds) or '-'} path={path}")
    print(f"prune-steward-markers mode={mode} roots={len(roots)} roots_source={roots_source} "
          f"markers={total} unevidenced={len(stale)} removed={removed} unreadable={unreadable}")
    if not args.apply and stale:
        return 2
    return rc


def _unlink_unevidenced_marker(path):
    """Re-read ``path`` and unlink it only if it STILL holds no steward evidence
    (a `steward on`/`wait` may have landed between the scan and this call)."""
    try:
        with open(path, encoding="utf-8") as fh:
            fresh = json.load(fh)
    except FileNotFoundError:
        return "gone"
    except Exception:
        fresh = None
    if isinstance(fresh, dict) and is_steward_marker(fresh):
        return "kept:now-evidenced"
    try:
        # destructive-ok: reason=evidence-less steward marker re-checked immediately before removal, opt-in --apply; boundary=<dispatch-state-root>/peer-steward/<harness>/<sid>.json
        path.unlink()
    except FileNotFoundError:
        return "gone"
    except OSError as exc:
        return f"failed:{exc.errno}"
    return "removed"


def _iter_records(since_hours=None):
    """C-9 — reads follow the same "migration-free, tolerant" contract as every
    other peer-ledger reader: a single root (`peer_state_root()`) is where NEW
    records land (C-1), but records written before that cutover still live under
    the dispatch-registry chain, so `cmd_list`/`cmd_status` must see both. Walks
    the same root set `steward_marker_roots()` gives every other consumer
    (peer 정본 root first, then the dispatch resolver chain, then every
    installed runtime's own root), de-duplicated by `Path.resolve()` — not by
    file, since two different roots' same-named shards are genuinely different
    records. A root that fails to resolve is skipped; the rest are still read.
    """
    roots, _source = steward_marker_roots()
    if not roots:
        roots = [peer_state_root()]
    cutoff = None
    if since_hours is not None:
        cutoff = time.time() - since_hours * 3600
    seen = set()
    for base in roots:
        try:
            resolved = Path(base).resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        root = Path(base) / "peer-messages"
        if not root.is_dir():
            continue
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
    parser = argparse.ArgumentParser(prog="peer-message", allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True,
                                parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))

    p_receive = sub.add_parser("receive")
    p_receive.add_argument("--to-harness", required=True)
    p_receive.add_argument("--to-session-id", required=True)
    p_receive.add_argument("--from-project", default="")
    p_receive.set_defaults(func=cmd_receive)

    p_record = sub.add_parser("record")
    p_record.add_argument("--from-harness", required=True)
    p_record.add_argument("--from-session-id", required=True)
    p_record.add_argument("--from-project", default="")
    p_record.add_argument("--from-name", default=None)
    p_record.add_argument("--to-harness", required=True)
    p_record.add_argument("--to-session-id", default=None)
    p_record.add_argument("--to-name", default=None)
    p_record.add_argument("--to-pane", default=None)
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

    p_prune = sub.add_parser("prune-steward-markers",
                             help="list (default) or delete (--apply) markers with no steward evidence")
    p_prune.add_argument("--apply", action="store_true")
    p_prune.add_argument("--root", action="append", default=[],
                         help="dispatch state root to scan (repeatable; default: the same root set the Fleet steward collector reads)")
    p_prune.set_defaults(func=cmd_prune_steward_markers)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
