#!/usr/bin/env python3
"""Merge byte-identical immutable artifact copies into hard links, reversibly.

One-off maintenance for one artifact root (`.agent_reports`/`.claude_reports`).

  plan    --root R [--journal J]  dry run: reads the store, writes only J
  apply   JOURNAL               link the planned duplicates (explicit command)
  verify  JOURNAL               read-only: applied paths still share the primary
  revert  JOURNAL               give every linked path its own copy again

Only immutable classes are linked:
  * shared-revision      physical files of listed `shared/spec/<ref>/revisions/<rrev>/`
  * cycle-spec-history   `artifacts/spec/**/_internal/versions/vN/prd.md` of a
                          sealed cycle whose routes are all finished
Every other cycle file is a worker write scope (a shell `>` rewrites the inode
in place, which would change every link), so it is counted and left alone.
New cycles keep getting byte copies; to merge the cycles sealed since, run the
same `plan` -> `apply` again with a new journal name.

Candidates come from `revision.json` and cycle `manifest.json` digests; nothing
is linked until both files are compared byte for byte immediately before the
replace. Each duplicate is replaced with `os.link(primary, staging tmp)` and
`os.replace(tmp, path)` (atomic: a reader never sees the path missing); the
temp lives in a staging directory on the same volume because a stray file in a
revision directory fails concurrent revision reads. Each group's result is
appended to the journal with fsync, so an interrupted apply resumes. Revert
checks every planned path, not only the ones the journal records as applied:
a path that still shares the plan-time primary inode gets its own copy back,
and its group stays open (retried by the next revert and counted in
`remaining_groups` and `still_linked`) until none of its paths is linked.

Applying to a real store: apply only when the `plan` counts match what you
expect and `skipped` shows no large `recent-24h`. While apply runs (estimated
17-21 min for hearting, 4-6 min for cairn) start no new work and no spec
transaction on that store; every link changes the primary's ctime, so a reader
of that revision fails its integrity check in that moment. If apply stops
early, rerun the same apply until `remaining_groups: 0` before any revert.
After a reboot or remount apply refuses with `root-volume-changed`; then
`revert` that journal and `plan` again with a new journal. Revert is a separate
decision, never run as part of the apply steps. If revert keeps reporting the
same skipped path, stop and report that path.

Remaining window: the last re-check and `os.replace` are two steps. An atomic
writer that lands between them loses its new bytes, and an in-place writer that
opened a duplicate for writing before the replace keeps writing to the replaced
inode for as long as it holds that fd, so those writes are lost too. Only
classes without writers are linked, and files whose mtime or ctime falls in the
last 24 h are left alone.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

SCHEMA = "hearting-dedup-hardlink/v1"
CHUNK = 1 << 20
RECENT_NS = 24 * 3600 * 10**9
MIN_OTHER_SIZE = 4096  # informational duplicate estimate for excluded cycle files
SEALED = "sealed"
HISTORY = re.compile(r"(?:^|/)_internal/versions/v[0-9]+/prd\.md$")
CYCLE_ID = re.compile(r"cyc_[0-9a-f]{32}")
# Files that never become link candidates, whatever a manifest says. The
# sealed-bytes restore inputs (artifact_restore_sealed.py INPUT_ROLES) and the
# report-bundle verifier both require st_nlink == 1.
CONTROL_NAMES = {".cycle.json", "manifest.json", "revision.json", "reference.json", "campaign.json",
                 "report_manifest.json", "backup-seal.json", "retired-manifest.jsonl",
                 "root-identity.json", "journal.jsonl", "shared-base.json"}
CONTROL_SUFFIXES = (".lock", ".flock", ".tmp", ".part")
DELIVERABLE = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".hwp", ".hwpx", ".xlsx", ".key", ".png", ".jpg",
               ".jpeg", ".svg", ".gif", ".mp4", ".webm", ".wav", ".flac", ".mp3", ".pt", ".ckpt", ".npy",
               ".npz", ".onnx", ".tex", ".bib"}
NOT_SEALED_REASON = "cycle-not-sealed"
# Group skips caused by this run's environment, not by the files: the next apply retries them.
RETRYABLE = {"staging-other-volume", "owner-not-restorable"}


class DedupError(Exception):
    def __init__(self, reason, detail="", code=65):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail, self.code = reason, str(detail), code


# Seams the tests replace; production uses the os functions directly.
_lstat = os.lstat


def _before_replace(path):  # test seam: runs after the final compare, before re-stat
    return None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "rb") as handle:
            value = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _facts(st):
    return {"ino": st.st_ino, "dev": st.st_dev, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "mode": st.st_mode, "uid": st.st_uid, "gid": st.st_gid,
            "nlink": st.st_nlink}


def _same(st, row, keys=("ino", "size", "mtime_ns")):
    return all(getattr(st, "st_" + key) == row[key] for key in keys)


def _sha256_fd(fd):
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, CHUNK)
        if not block:
            return "sha256:" + digest.hexdigest()
        digest.update(block)


def _equal_fds(a, b):
    os.lseek(a, 0, os.SEEK_SET)
    os.lseek(b, 0, os.SEEK_SET)
    while True:
        x, y = os.read(a, CHUNK), os.read(b, CHUNK)
        if x != y:
            return False
        if not x:
            return True


def _error_reason(error):
    """One path's failure as a skip reason instead of ending the whole run."""
    if isinstance(error, DedupError):
        return error.reason
    return "os-error-" + errno.errorcode.get(error.errno, "?")


def _open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise DedupError("not-regular", path)
    return fd


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class _Counter:
    def __init__(self):
        self.rows = {}

    def add(self, reason, size, n=1):
        row = self.rows.setdefault(reason, {"files": 0, "bytes": 0})
        row["files"] += n
        row["bytes"] += size

    def as_dict(self):
        return {k: self.rows[k] for k in sorted(self.rows)}


class _Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds if seconds else None

    def check(self, phase):
        if self.end is not None and time.monotonic() > self.end:
            raise DedupError("deadline-exceeded", phase, code=75)

    def expired(self):
        return self.end is not None and time.monotonic() > self.end


# ---------------------------------------------------------------------------
# plan: exclusions
# ---------------------------------------------------------------------------

def _default_jobs():
    explicit = os.environ.get("AGENT_DISPATCH_JOBS")
    if explicit:
        return Path(explicit)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
    try:
        import dispatch_contract_roots  # noqa: E402
        return dispatch_contract_roots.stable_state_root(os.environ) / "jobs.log"
    except Exception:
        return None


def open_registry_routes(jobs):
    """Route ids that an open/running registry attempt still writes for."""
    if jobs is None:
        return set(), "none"
    jobs = Path(jobs)
    if not jobs.is_file():
        raise DedupError("registry-unavailable", f"{jobs} (pass --jobs PATH or --jobs none)", code=66)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
    from dispatch_registry_cache import registry_view  # noqa: E402
    routes = set()
    for record in registry_view(jobs).records:
        if record.status not in {"open", "running"}:
            continue
        meta = record.completion_metadata()
        for key in ("route_id", "owner_route_id", "parent_close_route_id"):
            if meta.get(key):
                routes.add(meta[key])
    return routes, str(jobs)


def _cycle_routes(record):
    routes = {record.get("route_id")}
    for binding in record.get("route_bindings") or []:
        if isinstance(binding, dict):
            routes.update({binding.get("route_id"), binding.get("continuation_id"), binding.get("source_route_id")})
    return {r for r in routes if isinstance(r, str) and r}


def restore_target_cycles(root):
    """Cycles named by any sealed-bytes restore request (their files must keep nlink 1)."""
    found = set()
    for request in root.glob("campaigns/*/*/artifacts/plans/sealed-bytes-restore/_internal/requests/*/*.json"):
        try:
            found.update(CYCLE_ID.findall(request.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return found


def cycle_directories(root):
    """cycle_id -> directory, from on-disk `.cycle.json` bindings (pure read)."""
    found, duplicate = {}, set()
    for pattern in ("campaigns/*/*/.cycle.json", "campaigns/*/cycles/*/.cycle.json"):
        for binding in root.glob(pattern):
            value = _read_json(binding)
            cycle = value.get("cycle_id") if value else None
            if not isinstance(cycle, str):
                continue
            if cycle in found and found[cycle] != binding.parent:
                duplicate.add(cycle)
            found[cycle] = binding.parent
    for cycle in duplicate:
        found.pop(cycle, None)
    return found, duplicate


# ---------------------------------------------------------------------------
# plan: candidate rows
# ---------------------------------------------------------------------------

def _row(path, digest, size, cls, reason=None):
    return {"path": path, "digest": digest, "size": size, "class": cls, "reason": reason}


def shared_rows(root, counter):
    rows = []
    base = root / "shared" / "spec"
    for ref in sorted(base.glob("ref_*")) if base.is_dir() else []:
        reference = _read_json(ref / "reference.json")
        if reference is None:
            counter.add("reference-unreadable", 0)
            continue
        for rid in reference.get("revisions") or []:
            record = _read_json(ref / "revisions" / str(rid) / "revision.json")
            if record is None or record.get("shared_reference_revision_id") != rid:
                counter.add("revision-unreadable", 0)
                continue
            stored = record.get("stored_in") or {}
            for item in record.get("files") or []:
                name, digest, size = item.get("path"), item.get("sha256"), item.get("byte_size")
                if not isinstance(name, str) or name in stored or not isinstance(size, int):
                    continue
                rows.append(_row(f"shared/spec/{ref.name}/revisions/{rid}/{name}", digest, size,
                                 "shared-revision", "git" if ".git" in name.split("/") else None))
    return rows


def _path_reason(rel_in_cycle):
    parts = rel_in_cycle.split("/")
    name = parts[-1]
    if ".git" in parts:
        return "git"
    if name in CONTROL_NAMES or name.endswith(CONTROL_SUFFIXES):
        return "control-document"
    if "sealed-bytes-restore" in parts:
        return "restore-control"
    if parts[:2] == ["artifacts", "report"]:
        return "report-bundle"
    if rel_in_cycle in ("artifacts/status.md", "artifacts/현황.md"):
        return "status-page"
    if os.path.splitext(name)[1].lower() in DELIVERABLE:
        return "deliverable-format"
    return None


def cycle_rows(root, cycles, excluded, counter, deadline, threads=24):
    directories, duplicate = cycle_directories(root)
    for cycle in duplicate:
        counter.add("cycle-dir-ambiguous", 0)
    jobs = []
    for record in cycles:
        cycle = record.get("cycle_id")
        directory = directories.get(cycle)
        if directory is None:
            counter.add("cycle-dir-unresolved", 0)
            continue
        jobs.append((record, directory))

    def load(item):
        record, directory = item
        manifest = _read_json(directory / "manifest.json")
        return record, directory, manifest

    rows = []
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        for record, directory, manifest in pool.map(load, jobs):
            deadline.check("cycle-manifests")
            if manifest is None:
                counter.add("no-manifest", 0)
                continue
            latest = {}
            for item in manifest.get("artifact_revisions") or []:
                if not isinstance(item, dict):
                    continue
                key = item.get("artifact_id")
                if key not in latest or (item.get("revision_sequence") or 0) >= (latest[key].get("revision_sequence") or 0):
                    latest[key] = item
            reason_cycle = excluded.get(record.get("cycle_id"))
            prefix = directory.relative_to(root).as_posix()
            for item in latest.values():
                locator = item.get("locator") or {}
                rel, digest, size = locator.get("path"), item.get("content_digest"), item.get("byte_size")
                if locator.get("kind") != "cycle-relative" or not isinstance(rel, str) or not isinstance(size, int):
                    continue
                if rel.startswith("/") or ".." in rel.split("/"):
                    counter.add("locator-outside-cycle", size)  # could name another (open) cycle's file
                    continue
                spec = rel.startswith("artifacts/spec/")
                history = spec and HISTORY.search(rel[len("artifacts/spec/"):]) is not None
                cls = "cycle-spec-history" if history else ("cycle-spec-live" if spec else "cycle-other")
                reason = reason_cycle or _path_reason(rel)
                if reason is None and cls != "cycle-spec-history":
                    reason = "live-cycle-file"
                rows.append(_row(f"{prefix}/{rel}", digest, size, cls, reason))
    return rows


def classify_cycles(root, open_routes, restore_targets):
    """cycle_id -> exclusion reason (absent when eligible), and the records."""
    records = []
    directory = root / ".runtime" / "artifact-producer" / "v1" / "cycles"
    for entry in sorted(directory.glob("cyc_*.json")) if directory.is_dir() else []:
        value = _read_json(entry)
        if value and isinstance(value.get("cycle_id"), str):
            records.append(value)
    routes_dir = root / ".runtime" / "routes"
    excluded = {}
    for record in records:
        cycle = record["cycle_id"]
        routes = _cycle_routes(record)
        if record.get("state") == "open":
            excluded[cycle] = "cycle-open"
        elif record.get("state") != SEALED:
            excluded[cycle] = NOT_SEALED_REASON
        elif routes & open_routes:
            excluded[cycle] = "route-open-attempt"
        elif any(not (routes_dir / f"{r}.outcome.json").is_file() for r in routes) or not routes:
            excluded[cycle] = "route-unfinished"
        elif cycle in restore_targets:
            excluded[cycle] = "restore-target"
    return records, excluded


# ---------------------------------------------------------------------------
# plan: stat and grouping
# ---------------------------------------------------------------------------

class _ParentCheck:
    """True when no directory between root and the file is a symlink."""

    def __init__(self, root):
        self.root = str(root)
        self.cache = {self.root: True}

    def ok(self, directory):
        if directory in self.cache:
            return self.cache[directory]
        parent = os.path.dirname(directory)
        if not directory.startswith(self.root + "/") or parent == directory:
            value = False
        else:
            try:
                value = self.ok(parent) and stat.S_ISDIR(_lstat(directory).st_mode)
            except OSError:
                value = False
        self.cache[directory] = value
        return value


def stat_rows(root, rows, counter, deadline, now_ns, threads=32):
    parents = _ParentCheck(root)
    live = [r for r in rows if r["reason"] is None]
    for row in rows:
        if row["reason"] is not None:
            counter.add(row["reason"], row["size"] or 0)
    # Prime the directory cache serially by depth (cheap, avoids races on the dict).
    for directory in sorted({os.path.dirname(str(root / r["path"])) for r in live}, key=len):
        parents.ok(directory)
    deadline.check("parent-check")

    def one(row):
        path = str(root / row["path"])
        if not parents.cache.get(os.path.dirname(path)):
            return row, None, "symlinked-parent"
        try:
            st = _lstat(path)
        except FileNotFoundError:
            return row, None, "missing"
        except OSError:
            return row, None, "unreadable"
        if stat.S_ISLNK(st.st_mode):
            return row, None, "symlink"
        if not stat.S_ISREG(st.st_mode):
            return row, None, "special-file"
        if st.st_size != row["size"]:
            return row, None, "size-differs-from-record"
        if st.st_size == 0:
            return row, None, "empty"
        if now_ns - max(st.st_mtime_ns, st.st_ctime_ns) < RECENT_NS:  # ctime: `cp -p` keeps an old mtime
            return row, None, "recent-24h"
        return row, st, None

    kept = []
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        for index, (row, st, reason) in enumerate(pool.map(one, live)):
            if index % 2048 == 0:
                deadline.check("stat")
            if reason:
                counter.add(reason, row["size"] or 0)
            else:
                row["stat"] = _facts(st)
                kept.append(row)
    return kept


def _primary_key(rows_by_ino):
    def key(item):
        ino, rows = item
        return (-len(rows), not any(r["class"] == "shared-revision" for r in rows),
                min(r["stat"]["mtime_ns"] for r in rows), min(r["path"] for r in rows))
    return key


def build_groups(rows, counter):
    by_content = {}
    for row in rows:
        by_content.setdefault((row["digest"], row["size"]), []).append(row)
    groups = []
    for (digest, size), members in sorted(by_content.items(), key=lambda kv: kv[0]):
        if len(members) < 2:
            continue
        subsets = {}
        for row in members:
            s = row["stat"]
            subsets.setdefault((s["dev"], s["uid"], s["gid"], s["mode"]), []).append(row)
        largest = max(subsets, key=lambda k: len(subsets[k]))
        for key, subset in subsets.items():
            if len(subset) < 2:
                lone = subset[0]["stat"]
                ref = largest if key != largest else None
                if ref is not None:
                    reason = ("other-volume" if lone["dev"] != ref[0] else
                              "owner-differs" if (lone["uid"], lone["gid"]) != ref[1:3] else "mode-differs")
                    counter.add(reason, size)
                continue
            by_ino = {}
            for row in subset:
                by_ino.setdefault(row["stat"]["ino"], []).append(row)
            clean = {}
            for ino, linked in by_ino.items():
                if linked[0]["stat"]["nlink"] > len(linked):
                    counter.add("external-hardlink", size, len(linked))
                else:
                    clean[ino] = linked
            if len(clean) < 2:
                if len(clean) == 1:
                    only = next(iter(clean.values()))
                    if len(only) > 1:
                        counter.add("already-linked", 0, len(only) - 1)
                continue
            ordered = sorted(clean.items(), key=_primary_key(clean))
            primary_ino, primary_rows = ordered[0]
            primary_rows = sorted(primary_rows, key=lambda r: (r["class"] != "shared-revision", r["path"]))
            primary = primary_rows[0]
            if len(primary_rows) > 1:
                counter.add("already-linked", 0, len(primary_rows) - 1)
            links = [r for _, linked in ordered[1:] for r in sorted(linked, key=lambda r: r["path"])]
            groups.append({
                "kind": "group", "digest": digest, "size": size,
                "dev": key[0], "uid": key[1], "gid": key[2], "mode": key[3],
                "primary": {"path": primary["path"], "class": primary["class"], **primary["stat"]},
                "links": [{"path": r["path"], "class": r["class"], **r["stat"]} for r in links],
                "freed_bytes": size * (len(ordered) - 1),
            })
    for index, group in enumerate(groups, 1):
        group["id"] = f"g{index:06d}"
    return groups


def _duplicate_estimate(rows):
    """Manifest-level duplicate bytes per class, ignoring stat (research comparison)."""
    by_content = {}
    for row in rows:
        if row["digest"] and row["size"]:
            by_content.setdefault((row["digest"], row["size"]), []).append(row)
    out = {}
    for (_, size), members in by_content.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda r: (r["class"] != "shared-revision", r["path"]))
        for row in members[1:]:
            if row["class"] == "cycle-other" and size < MIN_OTHER_SIZE:
                continue
            slot = out.setdefault(row["class"], {"files": 0, "bytes": 0, "excluded_files": 0, "excluded_bytes": 0})
            slot["files"] += 1
            slot["bytes"] += size
            if row["reason"] is not None:
                slot["excluded_files"] += 1
                slot["excluded_bytes"] += size
    return {k: out[k] for k in sorted(out)}


def plan(root, journal, *, jobs, max_seconds, now_ns=None, threads=32):
    started = time.monotonic()
    root = Path(os.path.realpath(root))
    if not root.is_dir():
        raise DedupError("root-not-directory", root, code=66)
    deadline = _Deadline(max_seconds)
    now_ns = now_ns if now_ns is not None else time.time_ns()
    counter = _Counter()
    open_routes, registry = open_registry_routes(jobs)
    restore = restore_target_cycles(root)
    records, excluded = classify_cycles(root, open_routes, restore)
    timings = {"exclusions": round(time.monotonic() - started, 1)}
    rows = shared_rows(root, counter)
    deadline.check("shared")
    timings["shared"] = round(time.monotonic() - started, 1)
    rows += cycle_rows(root, records, excluded, counter, deadline, threads=max(4, threads * 3 // 4))
    timings["cycles"] = round(time.monotonic() - started, 1)
    estimate = _duplicate_estimate(rows)
    kept = stat_rows(root, rows, counter, deadline, now_ns, threads=threads)
    timings["stat"] = round(time.monotonic() - started, 1)
    groups = build_groups(kept, counter)
    cycle_reasons = {}
    for reason in excluded.values():
        cycle_reasons[reason] = cycle_reasons.get(reason, 0) + 1
    summary = {
        "kind": "plan-summary", "groups": len(groups),
        "link_files": sum(len(g["links"]) for g in groups),
        "freed_bytes": sum(g["freed_bytes"] for g in groups),
        "by_class": {}, "skipped": counter.as_dict(),
        "cycles": {"records": len(records), "eligible": len(records) - len(excluded),
                   "excluded_by_reason": dict(sorted(cycle_reasons.items()))},
        "candidate_rows": len(rows), "statted_rows": len(kept),
        "manifest_duplicate_estimate": estimate,
        "seconds": round(time.monotonic() - started, 1), "timings": timings,
    }
    for group in groups:
        for link in group["links"]:
            slot = summary["by_class"].setdefault(link["class"], {"files": 0, "bytes": 0})
            slot["files"] += 1
            slot["bytes"] += group["size"]
    header = {"kind": "header", "schema": SCHEMA, "root": str(root),
              "root_dev": os.stat(root).st_dev, "planned_ns": now_ns,
              "registry": registry, "open_routes": len(open_routes),
              "restore_target_cycles": sorted(restore)}
    if journal is not None:
        _write_plan(Path(journal), header, groups, summary)
    return header, groups, summary


def _write_plan(journal, header, groups, summary):
    journal.parent.mkdir(parents=True, exist_ok=True)
    if journal.exists():
        raise DedupError("journal-exists", journal, code=73)
    tmp = journal.with_name(f".{journal.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        for line in [header, *groups, summary]:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.link(tmp, journal)  # no-replace publish
    os.unlink(tmp)
    _fsync_dir(str(journal.parent))


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------

class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.header, self.groups, self.summary = None, [], None
        self.applied, self.reverted = {}, {}
        self.torn = 0
        with open(self.path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    value = json.loads(line) if line.endswith("\n") else None
                except ValueError:
                    value = None
                if value is None:
                    if self.summary is None:
                        raise DedupError("journal-corrupt", f"{self.path}:{number}")
                    # A killed run's partial event: its group is simply processed again.
                    self.torn += 1
                    continue
                kind = value.get("kind")
                if number == 1:
                    if kind != "header" or value.get("schema") != SCHEMA:
                        raise DedupError("journal-schema", self.path)
                    self.header = value
                elif kind == "group":
                    self.groups.append(value)
                elif kind == "plan-summary":
                    self.summary = value
                elif kind == "applied":
                    self.applied[value["group"]] = value
                elif kind == "reverted":
                    self.reverted[value["group"]] = value
        if self.header is None or self.summary is None:
            raise DedupError("journal-incomplete", self.path)
        self.root = Path(self.header["root"])
        self.fd = None

    def lock(self):
        self.fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise DedupError("journal-busy", self.path, code=75)
        # A killed run can leave a partial last line; start the next append on a new line.
        with open(self.path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    os.write(self.fd, b"\n")

    def append(self, value):
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(self.fd, view):]
            os.fsync(self.fd)
        except OSError as error:  # the group stays unrecorded; revert still finds its paths
            raise DedupError("journal-write-failed", f"{self.path}: {error}", code=74) from error

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Staging:
    """Same-volume private temp directory; also proves who owns new files."""

    def __init__(self, path, journal, parent=None):
        self.path = Path(path)
        self.parent = parent  # the default `<root>/.runtime/dedup-hardlink`, removed again when empty
        self.path.mkdir(parents=True, exist_ok=True)
        self.prefix = f".dedup-{os.getpid()}-"
        self.count = 0
        for stale in self.path.glob(".dedup-*.tmp"):
            try:
                stale.unlink()  # our own leftover temp links from an interrupted run
            except OSError:
                pass
        probe = self.name()
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        st = os.fstat(fd)
        os.close(fd)
        os.unlink(probe)
        self.dev, self.owner = os.stat(self.path).st_dev, (st.st_uid, st.st_gid)

    def name(self):
        self.count += 1
        return str(self.path / f"{self.prefix}{self.count}.tmp")

    def remove_if_empty(self):
        for directory in (self.path, self.parent):
            if directory is None:
                continue
            try:
                directory.rmdir()
            except OSError:
                return


def _staging(journal, staging):
    if staging:
        return Staging(staging, journal)
    parent = journal.root / ".runtime" / "dedup-hardlink"
    return Staging(parent / journal.path.stem, journal, parent=parent)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _apply_link(root, group, link, primary_path, st_p, pfd, staging):
    """Link one duplicate; returns "linked", "already", or the reason it was skipped."""
    primary = group["primary"]
    path = str(root / link["path"])
    try:
        st_d = _lstat(path)
    except FileNotFoundError:
        return "missing"
    if st_d.st_ino == st_p.st_ino and st_d.st_dev == st_p.st_dev:
        return "already"  # replaced before an interruption
    if not stat.S_ISREG(st_d.st_mode) or not _same(st_d, link):
        return "changed-since-plan"
    if (st_d.st_dev, st_d.st_uid, st_d.st_gid, st_d.st_mode) != (group["dev"], group["uid"], group["gid"], group["mode"]):
        return "changed-since-plan"
    if st_d.st_nlink > link["nlink"]:  # only our own replacements may lower it
        return "external-hardlink"
    dfd = _open_regular(path)
    try:
        before = os.fstat(dfd)
        if before.st_ino != st_d.st_ino or not _equal_fds(pfd, dfd):
            return "content-differs"
    finally:
        os.close(dfd)
    tmp = staging.name()
    try:
        os.link(primary_path, tmp, follow_symlinks=False)
    except OSError as error:
        return "link-failed-" + errno.errorcode.get(error.errno, "?")
    try:
        if _lstat(tmp).st_ino != st_p.st_ino:
            return "changed-during-apply"
        _before_replace(path)
        now_d, now_p = _lstat(path), _lstat(primary_path)
        if ((now_d.st_ino, now_d.st_size, now_d.st_mtime_ns, now_d.st_ctime_ns) !=
                (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                or now_p.st_ino != st_p.st_ino or not _same(now_p, primary)):
            return "changed-during-apply"
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    _fsync_dir(os.path.dirname(path))
    return "linked"


def _apply_group(root, group, staging, done):
    primary_path = str(root / group["primary"]["path"])
    primary = group["primary"]
    result = {"kind": "applied", "group": group["id"], "linked": [], "already": [], "skipped": [],
              "group_skipped": None}
    try:
        st_p = _lstat(primary_path)
    except OSError:
        result["group_skipped"] = "primary-missing"
        return result
    if not stat.S_ISREG(st_p.st_mode) or not _same(st_p, primary):
        result["group_skipped"] = "primary-changed"
        return result
    if (st_p.st_dev, st_p.st_uid, st_p.st_gid, st_p.st_mode) != (group["dev"], group["uid"], group["gid"], group["mode"]):
        result["group_skipped"] = "primary-changed"
        return result
    if st_p.st_dev != staging.dev:
        result["group_skipped"] = "staging-other-volume"
        return result
    if (group["uid"], group["gid"]) != staging.owner:
        result["group_skipped"] = "owner-not-restorable"
        return result
    try:
        pfd = _open_regular(primary_path)
    except (OSError, DedupError) as error:
        result["group_skipped"] = "primary-unreadable-" + _error_reason(error)
        return result
    try:
        try:
            if not _same(os.fstat(pfd), primary):
                result["group_skipped"] = "primary-changed"
                return result
            if _sha256_fd(pfd) != group["digest"]:
                result["group_skipped"] = "primary-digest-mismatch"
                return result
        except OSError as error:
            result["group_skipped"] = "primary-unreadable-" + _error_reason(error)
            return result
        for link in group["links"]:
            try:
                outcome = _apply_link(root, group, link, primary_path, st_p, pfd, staging)
            except (OSError, DedupError) as error:
                outcome = _error_reason(error)
            if outcome in ("linked", "already"):
                result[outcome].append(link["path"])
            else:
                result["skipped"].append({"path": link["path"], "reason": outcome})
    finally:
        os.close(pfd)
    return result


def apply(journal_path, *, staging=None, max_seconds=None, out=None):
    journal = Journal(journal_path)
    root = journal.root
    if os.stat(root).st_dev != journal.header["root_dev"]:
        raise DedupError("root-volume-changed", root)
    journal.lock()
    stage = _staging(journal, staging)
    deadline = _Deadline(max_seconds)
    totals = {"groups_done": 0, "linked": 0, "already": 0, "skipped": {}, "group_skipped": {},
              "freed_bytes": 0, "stopped_by_deadline": False}
    try:
        for group in journal.groups:
            prior = journal.applied.get(group["id"])
            if group["id"] in journal.reverted or (prior is not None and not (
                    prior["group_skipped"] in RETRYABLE and not prior["linked"] and not prior["already"])):
                continue
            if deadline.expired():
                totals["stopped_by_deadline"] = True
                break
            result = _apply_group(root, group, stage, journal.applied)
            result["at_ns"] = time.time_ns()
            journal.append(result)
            journal.applied[group["id"]] = result
            totals["groups_done"] += 1
            totals["linked"] += len(result["linked"])
            totals["already"] += len(result["already"])
            if result["group_skipped"]:
                totals["group_skipped"][result["group_skipped"]] = totals["group_skipped"].get(result["group_skipped"], 0) + 1
            for skip in result["skipped"]:
                totals["skipped"][skip["reason"]] = totals["skipped"].get(skip["reason"], 0) + 1
            if result["linked"] or result["already"]:
                # bytes are freed only when no path of the old inode remains
                old = {}
                for link in group["links"]:
                    old.setdefault(link["ino"], []).append(link["path"])
                moved = set(result["linked"]) | set(result["already"])
                totals["freed_bytes"] += group["size"] * sum(1 for paths in old.values() if set(paths) <= moved)
    finally:
        journal.close()
        stage.remove_if_empty()
    totals["remaining_groups"] = sum(1 for g in journal.groups if g["id"] not in journal.applied
                                     or journal.applied[g["id"]]["group_skipped"] in RETRYABLE)
    return totals


# ---------------------------------------------------------------------------
# verify / revert
# ---------------------------------------------------------------------------

# A revert event closes its group only when none of its planned paths was left on
# the plan-time primary inode; any other skip keeps the group open for the next revert.
REVERT_DONE = {"not-linked-now"}
LINKED, OTHER, MISSING = "linked", "other", "missing"


def _revert_closed(event):
    return event is not None and all(skip.get("reason") in REVERT_DONE for skip in event.get("skipped") or [])


def _dev_now(journal):
    """Recorded root volume number -> its number now: a reboot or remount renumbers an NFS
    volume's st_dev while the inode numbers stay, so links are found by today's number."""
    return {journal.header["root_dev"]: os.stat(journal.root).st_dev}


def _link_state(root, group, link, dev_now):
    """Whether a planned path shares the plan-time primary inode (what apply links to) now."""
    try:
        st = _lstat(str(root / link["path"]))
    except FileNotFoundError:
        return MISSING
    except OSError as error:
        return _error_reason(error)
    dev = dev_now.get(group["primary"]["dev"], group["primary"]["dev"])
    if (st.st_dev, st.st_ino) == (dev, group["primary"]["ino"]) and stat.S_ISREG(st.st_mode):
        return LINKED
    return OTHER


def _link_states(root, groups, dev_now, threads=16):
    pairs = [(group, link) for group in groups for link in group["links"]]
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        states = list(pool.map(lambda pair: _link_state(root, *pair, dev_now), pairs))
    return {(group["id"], link["path"]): state for (group, link), state in zip(pairs, states)}


def verify(journal_path, *, hash_primaries=True):
    journal = Journal(journal_path)
    root = journal.root
    report = {"groups": 0, "linked_ok": 0, "diverged": 0, "missing": 0, "primary_bad": 0, "reverted_groups": 0,
              "still_linked": 0, "unrecorded_links": 0, "unreadable": 0,
              "diverged_paths": [], "bad_primaries": [], "unrecorded_paths": []}
    states = _link_states(root, journal.groups, _dev_now(journal))
    for group in journal.groups:
        applied = journal.applied.get(group["id"])
        recorded = set(applied["linked"]) | set(applied["already"]) if applied else set()
        for link in group["links"]:
            state = states[(group["id"], link["path"])]
            if state == LINKED:
                report["still_linked"] += 1  # after a finished revert this is 0
                if link["path"] not in recorded:  # an apply stopped before it journaled the group
                    report["unrecorded_links"] += 1
                    if len(report["unrecorded_paths"]) < 50:
                        report["unrecorded_paths"].append(link["path"])
            elif state not in (OTHER, MISSING):
                report["unreadable"] += 1
        if _revert_closed(journal.reverted.get(group["id"])):
            report["reverted_groups"] += 1
            continue
        if not applied or group["id"] in journal.reverted:
            continue  # never applied, or a revert is still open: `still_linked` covers it
        report["groups"] += 1
        primary = str(root / group["primary"]["path"])
        try:
            _lstat(primary)
            if hash_primaries:
                fd = _open_regular(primary)
                try:
                    good = _sha256_fd(fd) == group["digest"]
                finally:
                    os.close(fd)
            else:
                good = True
        except (OSError, DedupError):
            good = False
        if not good:
            report["primary_bad"] += 1
            report["bad_primaries"].append(group["primary"]["path"])
        for rel in applied["linked"] + applied["already"]:
            state = states.get((group["id"], rel))
            if state == LINKED:
                report["linked_ok"] += 1
            elif state == MISSING:
                report["missing"] += 1
            elif state == OTHER:
                report["diverged"] += 1  # rewritten since apply (atomic writer): no longer shared
                if len(report["diverged_paths"]) < 50:
                    report["diverged_paths"].append(rel)
    return report


def _revert_path(root, group, link, stage):
    primary_ino = group["primary"]["ino"]
    path = str(root / link["path"])
    st = _lstat(path)
    if st.st_ino != primary_ino or not stat.S_ISREG(st.st_mode):
        return "changed-during-revert"  # linked a moment ago; the next revert looks again
    src = _open_regular(path)
    tmp = stage.name()
    try:
        dst = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(link["mode"]))
        try:
            os.lseek(src, 0, os.SEEK_SET)
            while True:
                block = os.read(src, CHUNK)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    view = view[os.write(dst, view):]
            os.fsync(dst)
            os.fchmod(dst, stat.S_IMODE(link["mode"]))
            written = os.fstat(dst)
            if written.st_gid != link["gid"]:
                try:
                    os.fchown(dst, -1, link["gid"])
                except OSError:
                    pass
        finally:
            os.close(dst)
        os.utime(tmp, ns=(st.st_atime_ns, link["mtime_ns"]))
        check = _open_regular(tmp)
        try:
            if _sha256_fd(check) != group["digest"]:
                return "copy-digest-mismatch"
        finally:
            os.close(check)
        now = _lstat(path)
        if now.st_ino != primary_ino:
            return "changed-during-revert"
        os.replace(tmp, path)
        tmp = None
    finally:
        os.close(src)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    _fsync_dir(os.path.dirname(path))
    after = _lstat(path)
    fd = _open_regular(path)
    try:
        ok = (_sha256_fd(fd) == group["digest"] and after.st_mtime_ns == link["mtime_ns"]
              and after.st_mode == link["mode"] and after.st_ino != primary_ino)
    finally:
        os.close(fd)
    return None if ok else "restored-facts-differ"


def revert(journal_path, *, staging=None, max_seconds=None):
    """Give every planned path that still shares the plan-time primary inode its own copy.

    Every group is checked, whether or not an `applied` event records it: an apply
    stopped mid-group has replaced paths without journaling them. A group whose
    revert left a linked path (or could not tell) stays open for the next revert.
    """
    journal = Journal(journal_path)
    root = journal.root
    dev_now = _dev_now(journal)
    journal.lock()
    stage = _staging(journal, staging)
    deadline = _Deadline(max_seconds)
    totals = {"groups_done": 0, "restored": 0, "skipped": {}, "stopped_by_deadline": False}
    try:
        for group in reversed(journal.groups):
            prior = journal.reverted.get(group["id"])
            if _revert_closed(prior):
                continue
            if deadline.expired():
                totals["stopped_by_deadline"] = True
                break
            applied = journal.applied.get(group["id"]) or {}
            recorded = set(applied.get("linked") or []) | set(applied.get("already") or [])
            event = {"kind": "reverted", "group": group["id"], "restored": [], "skipped": []}
            for link in group["links"]:
                state = _link_state(root, group, link, dev_now)
                if state == LINKED:
                    try:
                        reason = _revert_path(root, group, link, stage)
                    except (OSError, DedupError) as error:
                        reason = _error_reason(error)
                elif state in (OTHER, MISSING):
                    if prior is not None or link["path"] not in recorded:
                        continue  # never linked, or restored by an earlier revert
                    reason = "not-linked-now"  # rewritten since apply: left as it is
                else:
                    reason = state  # cannot tell; the group stays open
                if reason is None:
                    event["restored"].append(link["path"])
                else:
                    event["skipped"].append({"path": link["path"], "reason": reason})
                    totals["skipped"][reason] = totals["skipped"].get(reason, 0) + 1
            event["at_ns"] = time.time_ns()
            journal.append(event)
            journal.reverted[group["id"]] = event
            totals["groups_done"] += 1
            totals["restored"] += len(event["restored"])
    finally:
        journal.close()
        stage.remove_if_empty()
    open_groups = [g for g in journal.groups if not _revert_closed(journal.reverted.get(g["id"]))]
    totals["remaining_groups"] = len(open_groups)
    totals["still_linked"] = sum(1 for state in _link_states(root, open_groups, dev_now).values() if state == LINKED)
    return totals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", allow_abbrev=False, help="read-only: write the merge journal and print counts")
    p.add_argument("--root", required=True)
    p.add_argument("--journal", help="new journal file to write (omit for counts only)")
    p.add_argument("--jobs", help="dispatch registry (default: the canonical one); 'none' skips it")
    p.add_argument("--max-seconds", type=float, default=840.0, help="stop a slow scan (default 840)")
    for name, text in (("apply", "link the planned duplicates"), ("revert", "restore own copies")):
        q = sub.add_parser(name, allow_abbrev=False, help=text)
        q.add_argument("journal")
        q.add_argument("--staging", help="same-volume temp dir (default <root>/.runtime/dedup-hardlink/<journal>)")
        q.add_argument("--max-seconds", type=float, help="stop between groups after this long; rerun resumes")
    v = sub.add_parser("verify", allow_abbrev=False, help="read-only check of an applied journal")
    v.add_argument("journal")
    v.add_argument("--stat-only", action="store_true", help="skip hashing primaries")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            jobs = None if args.jobs == "none" else (Path(args.jobs) if args.jobs else _default_jobs())
            if args.jobs is None and jobs is None:
                raise DedupError("registry-unavailable", "pass --jobs PATH or --jobs none", code=66)
            header, _, summary = plan(args.root, args.journal, jobs=jobs, max_seconds=args.max_seconds)
            _print({"root": header["root"], "journal": args.journal, "registry": header["registry"],
                    "open_routes": header["open_routes"], **{k: v for k, v in summary.items() if k != "kind"}})
        elif args.command == "apply":
            _print(apply(args.journal, staging=args.staging, max_seconds=args.max_seconds))
        elif args.command == "revert":
            _print(revert(args.journal, staging=args.staging, max_seconds=args.max_seconds))
        elif args.command == "verify":
            report = verify(args.journal, hash_primaries=not args.stat_only)
            _print(report)
            return 1 if report["primary_bad"] else 0
    except DedupError as error:
        print(json.dumps({"error": error.reason, "detail": error.detail}), file=sys.stderr)
        return error.code
    except OSError as error:  # e.g. the staging directory cannot be created
        print(json.dumps({"error": _error_reason(error), "detail": str(error)}), file=sys.stderr)
        return 74
    return 0


if __name__ == "__main__":
    sys.exit(main())
