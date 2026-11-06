"""Campaign daily records. One append-only Markdown file, shared by all readers.

IDs are allocated, never derived from a locator. Correlation keys only make a
retry idempotent. Historical revisions remain attestable after later appends.
No index or database is maintained beside the daily file.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from artifact_identity import IdAllocator, is_well_formed

START = "<!-- hearting-journal-v1 "
END = "<!-- /hearting-journal-v1 -->\n"
REFERENCE = re.compile(r"^(.*\.md)#(art_[0-9a-f]{32})-(arev_[0-9a-f]{32})$")
READ_BYTES = 16 * 1024 * 1024
READ_ROWS = 2000
WRITE_BYTES = 64 * 1024 * 1024


class JournalError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(row):
    return hashlib.sha256(canonical({k: v for k, v in row.items() if k != "digest"})).hexdigest()


def directory_fd(path, *, create=False):
    """Open an absolute directory one non-following component at a time."""
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(path).absolute().parts[1:]:
            if component in {".", ".."}:
                raise JournalError("journal-path-unsafe")
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd); parent_fd = next_fd
    except BaseException:
        os.close(parent_fd)
        raise
    return parent_fd


def atomic(path, raw):
    path = Path(path)
    parent_fd = directory_fd(path.parent, create=True)
    tmp = ".journal-" + secrets.token_hex(12)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        held, current = os.fstat(parent_fd), path.parent.lstat()
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino) or path.parent.is_symlink():
            raise JournalError("journal-parent-replaced")
        os.replace(tmp, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=parent_fd)
        os.close(parent_fd)


def safe_path(root, path, *, missing=True):
    """Reject every symlink component; a read never follows a replacement."""
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise JournalError("journal-path-outside-root") from exc
    if ".." in parts:
        raise JournalError("journal-path-unsafe")
    current = root
    for part in ("", *parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or (current == path and stat.S_ISREG(mode))):
            raise JournalError("journal-path-unsafe")
    return path


@contextlib.contextmanager
def day_lock(root, path):
    root = Path(root)
    key = hashlib.sha256(str(Path(path).absolute()).encode()).hexdigest()
    lock = safe_path(root, root / ".runtime/journals/locks" / (key + ".lock"))
    parent_fd = directory_fd(lock.parent, create=True)
    try:
        fd = os.open(lock.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        safe_path(root, path)
        yield
    finally:
        os.close(fd)


def _render(row):
    meta = {k: v for k, v in row.items() if k != "body"}
    return (f'<a id="{row["artifact_id"]}-{row["revision_id"]}"></a>\n'
            + START + canonical(meta).decode() + " -->\n" + row["body"]
            + ("\n" if "body_chars" in row else "") + END).encode()


def _body(text):
    return str(text)


def read_day(path, *, limit=READ_ROWS, byte_limit=READ_BYTES, newest=False):
    """Bounded read; malformed/drifted/partial are explicit, never an empty success."""
    path = Path(path)
    result = {"rows": [], "coverage": "complete", "errors": []}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise JournalError("journal-not-regular")
            raw = stream.read(byte_limit + 1)
        if len(raw) > byte_limit:
            result["coverage"] = "partial"
            raw = raw[:byte_limit]
        text = raw.decode("utf-8", errors="replace" if newest and result["coverage"] == "partial" else "strict")
    except (OSError, ValueError, UnicodeError) as exc:
        return {**result, "coverage": "unknown", "errors": [type(exc).__name__]}
    if START not in text and result["coverage"] == "complete" and not re.fullmatch(r"# Work journal \d{4}-\d{2}-\d{2}\s*", text):
        return {**result, "coverage": "unknown", "errors": ["journal-format-unknown"]}
    pos, identities = 0, set()
    while True:
        start = text.find(START, pos)
        if start < 0:
            break
        header = text.find(" -->\n", start)
        end = text.find(END, header + 5) if header >= 0 else -1
        if end < 0:
            result["coverage"] = "partial"
            result["errors"].append("incomplete-entry")
            break
        try:
            row = json.loads(text[start + len(START):header])
            if "body_chars" in row:
                size = row["body_chars"]
                if type(size) is not int or not 0 <= size <= WRITE_BYTES:
                    raise JournalError("entry-body-length-invalid")
                body_end = header + 5 + size
                if not text.startswith("\n" + END, body_end):
                    raise JournalError("entry-body-length-drift")
                row["body"] = text[header + 5:body_end]
                end = body_end + 1
            else:  # the early v1 spelling used escaped reserved comments
                row["body"] = text[header + 5:end]
            if (row.get("schema") != "journal-entry/v1"
                    or not is_well_formed(row.get("artifact_id", ""), "artifact")
                    or not is_well_formed(row.get("revision_id", ""), "artifact_revision")
                    or row.get("digest") != digest(row)
                    or not isinstance(row.get("summary"), str) or not isinstance(row.get("origin"), dict)
                    or not isinstance(row.get("results"), list) or not isinstance(row.get("checks"), dict)):
                raise JournalError("entry-integrity-drift")
            datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
            key = (row["artifact_id"], row["revision_id"])
            if key in identities:
                raise JournalError("entry-identity-duplicate")
            identities.add(key)
            result["rows"].append(row)
        except (ValueError, KeyError, TypeError) as exc:
            result["coverage"] = "partial"
            result["errors"].append(str(exc)[:100])
        pos = end + len(END)
        if len(result["rows"]) >= limit:
            if newest:
                if len(result["rows"]) > limit:
                    result["rows"] = result["rows"][-limit:]
                    result["coverage"] = "partial"
                continue
            if START in text[pos:]:
                result["coverage"] = "partial"
            break
    return result


def reference(path, row):
    return f"{path}#{row['artifact_id']}-{row['revision_id']}"


def split_reference(value):
    match = REFERENCE.fullmatch(str(value))
    return (Path(match[1]), match[2], match[3]) if match else None


def read_reference(value):
    parts = split_reference(value)
    if parts is None:
        raise JournalError("journal-reference-invalid")
    path, aid, rid = parts
    owner = next((p.parent for p in path.parents if p.name == "campaigns"), None)
    if owner is None:
        raise JournalError("journal-reference-outside-campaign")
    safe_path(owner, path, missing=False)
    # Attestation reads the entire bounded write domain, not just a UI page.
    found = read_day(path, limit=100000, byte_limit=WRITE_BYTES)
    rows = [r for r in found["rows"] if r["artifact_id"] == aid and r["revision_id"] == rid]
    if len(rows) != 1:
        raise JournalError("journal-revision-unavailable")
    return rows[0]


def revision_digest(value):
    return read_reference(value)["digest"]


def result_targets(root, entry):
    """Observe link availability within the recorded execution folder.

    A recorded commit/hash is history, not a claim that current bytes still
    have that version. Large lists remain bounded and explicitly unobserved.
    """
    scope = None
    try:
        route_path = safe_path(root, Path(root) / ".runtime/routes" / (entry["route_id"] + ".json"), missing=False)
        if route_path.stat().st_size <= 1024 * 1024:
            scope = Path(json.loads(route_path.read_text())["cwd"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = []
    for index, raw in enumerate(entry.get("results") or []):
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            continue
        row = {**raw, "version": raw.get("version") or {"kind": "unknown"},
               "availability": "unknown", "current_version": "not-observed"}
        path = Path(raw["path"])
        scopes = [scope] if scope else []
        if Path(root).name in {".agent_reports", ".claude_reports"}:
            scopes.append(Path(root).parent)
        if scopes and index < 3 and path.is_absolute():
            try:
                allowed = next((base for base in scopes if path.is_relative_to(base)), None)
                if allowed is None or path.is_relative_to(root):
                    raise JournalError("result-outside-project")
                safe_path(allowed, path, missing=False)
                fd = directory_fd(path.parent)
                try:
                    mode = os.stat(path.name, dir_fd=fd, follow_symlinks=False).st_mode
                    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
                        row.update(availability="available", href=path.as_uri())
                finally:
                    os.close(fd)
            except FileNotFoundError:
                row["availability"] = "missing"
            except (OSError, ValueError):
                row["availability"] = "unavailable"
        result.append(row)
    return result


def _target_path(root, route_id):
    if not re.fullmatch(r"rt-[a-zA-Z0-9-]+", str(route_id)):
        raise JournalError("journal-route-invalid")
    return Path(root) / ".runtime/journals/targets" / (route_id + ".json")


def target(root, route_id):
    path = safe_path(root, _target_path(root, route_id))
    try:
        row = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if row.get("schema") != "journal-target/v1" or row.get("route_id") != route_id:
        raise JournalError("journal-target-invalid")
    safe_path(root, Path(root) / row["path"])
    return row


def prepare_locked(root, route, campaign, campaign_path, *, at=None):
    """Called under the existing admission lock, after campaign selection."""
    old = target(root, route["route_id"])
    if old:
        if old["route_hash"] != route["route_hash"]:
            raise JournalError("journal-target-conflict")
        return old
    at = at or datetime.now().astimezone().isoformat()
    date = datetime.fromisoformat(at.replace("Z", "+00:00")).date().isoformat()
    path = safe_path(root, Path(campaign_path) / "journals" / (date + ".md"))
    row = {"schema": "journal-target/v1", "route_id": route["route_id"],
           "route_hash": route["route_hash"], "campaign_id": campaign["campaign_id"],
           "campaign_key": campaign["key"], "path": path.relative_to(root).as_posix(), "at": at}
    atomic(_target_path(root, route["route_id"]), canonical(row) + b"\n")
    return row


def environment(root, row):
    return {"AGENT_ARTIFACT_ROOT": str(root), "AGENT_ARTIFACT_CAMPAIGN_ID": row["campaign_id"],
            "AGENT_ARTIFACT_JOURNAL": str(Path(root) / row["path"]),
            **{name: "" for name in ("AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
                                    "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR")}}


def append(root, binding, *, origin, body, summary, session=None, turn=None, at=None,
           status="recorded", checks=None, results=None, correction=None):
    """Idempotent append. Caller holds original record lock then admission, if any.

    The same origin with changed bytes needs explicit correction provenance.
    Retrying a successful operation cannot silently create a second artifact.
    """
    path = safe_path(root, Path(root) / binding["path"])
    rel = path.relative_to(root).parts
    if len(rel) != 4 or rel[0] != "campaigns" or rel[2] != "journals":
        raise JournalError("journal-binding-path-invalid")
    campaign_path = safe_path(root, path.parent.parent / "campaign.json", missing=False)
    if json.loads(campaign_path.read_text()).get("campaign_id") != binding["campaign_id"]:
        raise JournalError("journal-campaign-binding-mismatch")
    body = _body(body)
    with day_lock(root, path):
        existing = read_day(path, limit=100000, byte_limit=WRITE_BYTES) if path.exists() else {
            "rows": [], "coverage": "complete"}
        if existing["coverage"] != "complete":
            raise JournalError("journal-incomplete-preserved")
        prior = [r for r in existing["rows"] if r["origin"] == origin]
        if prior and session is not None and prior[-1].get("session") != session:
            raise JournalError("journal-origin-owner-conflict")
        data = {"summary": str(summary), "body": body, "body_chars": len(body), "checks": checks or {"state": "unknown"},
                "results": results or [], "status": status}
        if prior and all(prior[-1].get(k) == v for k, v in data.items()):
            return {**prior[-1], "reference": reference(path, prior[-1]), "replay": True}
        if prior and (not correction or not all(correction.get(k) for k in ("actor", "reason", "source"))):
            raise JournalError("journal-origin-conflict")
        alloc = IdAllocator()
        row = {"schema": "journal-entry/v1", "artifact_id": prior[0]["artifact_id"] if prior else alloc.allocate("artifact"),
               "revision_id": alloc.allocate("artifact_revision"), "event_id": alloc.allocate("event"),
               "origin": origin, "campaign_id": binding["campaign_id"], "route_id": binding["route_id"],
               "route_hash": binding["route_hash"], "at": at or binding["at"],
               "recorded_at": datetime.now().astimezone().isoformat(), "session": session or {},
               "turn": turn, "previous_revision": prior[-1]["revision_id"] if prior else None,
               "correction": correction if prior else None, **data}
        row["digest"] = digest(row)
        raw = path.read_bytes() if path.exists() else (f"# Work journal {binding['at'][:10]}\n\n").encode()
        new = _render(row)
        if len(raw) + len(new) > WRITE_BYTES:
            raise JournalError("journal-write-limit-preserved")
        # Atomic replacement avoids a torn final block after process failure.
        atomic(path, raw + new)
        return {**row, "reference": reference(path, row), "replay": False}


def rows(root, *, session=None, limit=READ_ROWS):
    """Common row source for entrance/Fleet/history/tidy; latest revisions only."""
    from artifact_locator import iter_campaign_dirs
    root = Path(root)
    result = {"rows": [], "coverage": "complete", "errors": []}
    scanned = 0
    try:
        campaigns = list(iter_campaign_dirs(root))
    except (OSError, ValueError) as exc:
        return {"rows": [], "coverage": "unknown", "errors": [type(exc).__name__]}
    for campaign in campaigns:
        folder = campaign / "journals"
        try:
            safe_path(root, folder)
            paths = sorted(folder.glob("????-??-??.md"), reverse=True)
        except (OSError, ValueError) as exc:
            result["coverage"] = "partial"; result["errors"].append(type(exc).__name__)
            continue
        for path in paths:
            scanned += 1
            if scanned > 128:
                result["rows"].sort(key=lambda row: row["at"])
                return {**result, "coverage": "partial", "errors": [*result["errors"], "scan-limit"]}
            try:
                safe_path(root, path, missing=False)
                day = read_day(path, limit=READ_ROWS, newest=True)
            except (OSError, ValueError) as exc:
                day = {"rows": [], "coverage": "unknown", "errors": [type(exc).__name__]}
            if day["coverage"] != "complete":
                result["coverage"] = "partial"
            result["errors"].extend(day["errors"])
            latest = {}
            for entry in day["rows"]:
                latest[entry["artifact_id"]] = entry
            for entry in sorted(latest.values(), key=lambda row: row["at"], reverse=True):
                if session and entry.get("session") != session:
                    continue
                anchor = entry["artifact_id"] + "-" + entry["revision_id"]
                rel = path.relative_to(root).as_posix()
                result["rows"].append({"kind": "journal-entry", "artifact_id": entry["artifact_id"],
                    "revision_id": entry["revision_id"], "digest": entry["digest"], "path": rel,
                    "anchor": anchor, "record_target": rel + "#" + anchor,
                    "result_targets": result_targets(root, entry)[:3], "results_total": len(entry.get("results") or []),
                    "results_coverage": "partial" if len(entry.get("results") or []) > 3 else "complete",
                    "title": entry["summary"][:256],
                    "summary": entry["body"].encode("utf-8")[:6000].decode("utf-8", "ignore"),
                    "campaign_id": entry["campaign_id"], "route_id": entry["route_id"],
                    "session": entry["session"], "turn": entry["turn"], "at": entry["at"],
                    "status": entry["status"], "coverage": day["coverage"], "checks": entry["checks"]})
            if len(result["rows"]) >= limit:
                result["rows"] = result["rows"][:limit]
                result["rows"].sort(key=lambda row: row["at"])
                result["coverage"] = "partial"
                return result
    result["rows"].sort(key=lambda row: row["at"])
    return result


def entrance_snapshot(root):
    snapshot = rows(root, limit=200)
    return {**snapshot, "kind": "journal-entry", "rows": [{**row,
        "date": row["at"][:10], "doc_path": row["path"],
        "summary": "Record saved; test verdict is recorded separately."}
        for row in reversed(snapshot["rows"])]}


def publish_views(root, entries=()):
    """Refresh existing derived views after storage; failures never undo it.

    Called by the existing recorder process, not a new hook, watcher or model.
    No admission/state/day lock is held while rendering projections.
    """
    try:
        import artifact_entrance
        artifact_entrance.refresh(Path(root))
    except Exception:  # noqa: BLE001 -- a derived view is never a completion gate
        pass
    try:
        import status_board
        for row in entries:
            status_board.note_journal_entry(Path(root), row)
    except Exception:  # noqa: BLE001 -- optional existing page, never created
        pass
