"""The cross-root leg of cycle-move: source-last copy with automatic replay.

Historical support is never registered as runnable target routes. Public writer
lookups stay root-local; only the separate historical reader follows this journal.
"""
from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import ctypes
from concurrent.futures import ThreadPoolExecutor
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import artifact_admission as admission
import artifact_campaign as campaigns
import artifact_locator as locator
import artifact_producer as P
import route_authority as authority

JOURNALS = ".runtime/artifact-producer/v1/relocations"


def _error(detail):
    raise P.ProducerError("relocation-conflict", str(detail))


def _safe(root, path):
    campaigns._safe(root, path)
    return Path(path)


def _json(path):
    if path.is_symlink():
        _error(path)
    return json.loads(path.read_bytes())


def _campaign(root, selector, *, read_record=None):
    matches = []
    for path in sorted((root / "campaigns").glob("*/campaign.json")):
        _safe(root, path)
        row = (read_record or _json)(path)
        if selector in {row.get("campaign_id"), row.get("key"), str(path), str(path.relative_to(root))}:
            matches.append((path, campaigns.fold_campaign(root, path, row)))
    active = [item for item in matches if item[1].get("state") == "active"]
    selected = active or matches
    if len(selected) != 1:
        _error("campaign selection is not exact: " + str(selector))
    return selected[0]


def tree(path):
    """Content inventory without dereferencing any symlink (including directories)."""
    rows = {}
    files = []
    def visit(node, rel):
        mode = node.lstat().st_mode
        if stat.S_ISLNK(mode):
            rows[rel] = {"kind": "symlink", "target": os.readlink(node)}
        elif stat.S_ISREG(mode):
            files.append((node, rel))
        elif stat.S_ISDIR(mode):
            rows[rel] = {"kind": "directory"}
            for child in sorted(node.iterdir()):
                visit(child, str(Path(rel) / child.name) if rel else child.name)
        else:
            _error("special payload: " + str(node))
    def read_file(item):
        node, rel = item
        digest = hashlib.sha256()
        before = node.lstat()
        fd = os.open(node, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                _error("payload replaced during read: " + str(node))
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())
        signature = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(node.lstat()) or signature(opened) != signature(after):
            _error("payload changed during read: " + str(node))
        return rel, {"kind": "file", "bytes": after.st_size, "sha256": digest.hexdigest()}
    visit(Path(path), "")
    if len(files) > 1:
        # Bound I/O fan-out; every read retains the same identity/stability checks.
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows.update(pool.map(read_file, files))
    elif files:
        rows.update([read_file(files[0])])
    return rows


def _same_device(source, target):
    return source.stat().st_dev == target.stat().st_dev


def listing(path):
    """Rename inventory: names, kinds and sizes, without reading payload bytes."""
    rows, identities = {}, {}
    def visit(node, rel, info):
        identities[rel] = [info.st_dev, info.st_ino, info.st_mode]
        if stat.S_ISLNK(info.st_mode):
            rows[rel] = {"kind": "symlink", "target": os.readlink(node)}
        elif stat.S_ISREG(info.st_mode):
            rows[rel] = {"kind": "file", "bytes": info.st_size}
        elif stat.S_ISDIR(info.st_mode):
            rows[rel] = {"kind": "directory"}
            with os.scandir(node) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    visit(Path(entry.path), str(Path(rel) / entry.name) if rel else entry.name,
                          entry.stat(follow_symlinks=False))
        else:
            _error("special payload: " + str(node))
    visit(Path(path), "", Path(path).lstat())
    return rows, identities


def _durable(directory):
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in files:
            path = Path(base) / name
            if not path.is_symlink():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        P._fsync_dir(Path(base))


def _save(root, journal):
    path = root / JOURNALS / (journal["operation_id"] + ".json")
    _safe(root, path)
    P._ensure_dir(path.parent)
    P._write_atomic(path, P._json_bytes(journal))


def _fault(phase):
    """Internal injection seam for the isolated transaction tests."""


def _copy_exact(source, target):
    if source.is_symlink():
        _error("symlink control: " + str(source))
    data = source.read_bytes()
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            _error("support identity collision: " + str(target))
        return
    P._ensure_dir(target.parent)
    P._write_exclusive(target, data)
    P._fsync_dir(target.parent)


def _supports(root, records, campaign_path, route_ids):
    """Fix the actual related support inventory; never copy the runtime wholesale.

    Route/attempt/group/meta originals live under historical/, not live registries.
    Shared revisions remain at their original root-qualified address.
    """
    ids = {row["cycle_id"] for row in records} | set(route_ids) | { _json(campaign_path)["campaign_id"] }
    selected = {}
    for path in sorted((root / ".runtime").rglob("*")):
        if path.is_symlink():
            # Selected control records cannot be supplied via symlink.
            if any(token in path.name for token in ids):
                _error("symlink support: " + str(path))
            continue
        if not path.is_file() or path.suffix not in {".json", ".jsonl", ".md", ".log", ".txt"}:
            continue
        rel = path.relative_to(root).as_posix()
        if "/relocations/" in rel or "/shared" in rel or path.name in {"LATEST.json", "index.json"}:
            continue
        raw = path.read_bytes()
        if any(token in rel or token.encode() in raw for token in ids):
            selected[rel] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    # Campaign controls are original history, including workflow groups and events.
    excluded = {campaign_path.parent / row["locator"] for row in records}
    for base, dirs, files in os.walk(campaign_path.parent, followlinks=False):
        # These payload subtrees were already excluded from support history;
        # prune before walking them rather than statting every excluded file.
        dirs[:] = sorted(name for name in dirs if Path(base) / name not in excluded)
        for name in sorted(files):
            path = Path(base) / name
            if path.is_file() and not path.is_symlink():
                rel = path.relative_to(root).as_posix()
                raw = path.read_bytes()
                selected[rel] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return selected


def _decision(root, journal):
    paths = [root / item["source"] for item in journal["cycles"] if (root / item["source"]).exists()]
    paths += [Path(item["source"]) for item in journal["attachments"] if Path(item["source"]).exists()]
    selected = set(journal.get("historical_routes", ()))
    for item in journal["cycles"]:
        record = _json(_safe(root, P.cycle_record_path(root, item["cycle_id"])))
        if record.get("route_id"):
            selected.add(record["route_id"])
        selected.update(row["route_id"] for row in record.get("route_bindings", []) if row.get("route_id"))
        current = P._read_manifest_raw(root / item["source"])
        if current:
            selected.update(row["route_id"] for row in current[1].get("routes", []) if row.get("route_id"))
    if selected != set(journal.get("historical_routes", ())):
        _error("source route membership changed")
    result = authority.relocation_admission(root, [item["record"] for item in journal["cycles"]], paths,
                                            selected_route_ids=selected)
    if not result.allowed:
        raise P.ProducerError("relocation-live-work", result.reason or "unknown")
    return result


def _plan(source, target, *, operation_id, cycle_id, source_campaign, campaign, attachments, now):
    if source == target:
        _error("campaign batching and attachments require distinct roots; use local cycle-move for a local move")
    if source in target.parents or target in source.parents:
        _error("nested artifact roots")
    if not source.is_dir() or not target.is_dir():
        _error("both artifact roots must exist")
    _safe(source, source / ".runtime")
    _safe(target, target / ".runtime")
    if bool(cycle_id) == bool(source_campaign):
        _error("select one cycle or source campaign")
    if source_campaign:
        source_path, source_row = _campaign(source, source_campaign)
        ids = source_row.get("cycles", [])
    else:
        record = _json(P.cycle_record_path(source, cycle_id))
        source_path, source_row = _campaign(source, record["campaign_id"])
        ids = [cycle_id]
    target_path, target_row = _campaign(target, campaign or source_row["key"])
    if target_row.get("state") != "active":
        _error("target campaign must be active")
    transfer = "rename" if _same_device(source, target) else "copy"
    cycles, records, used = [], [], set()
    for cid in ids:
        record_path = P.cycle_record_path(source, cid)
        _safe(source, record_path)
        record = _json(record_path)
        if record.get("campaign_id") != source_row["campaign_id"] or record.get("relocation"):
            _error("source membership mismatch: " + cid)
        directory = _safe(source, source_path.parent / record["locator"])
        binding = locator.read_cycle_binding(directory)
        if not binding or binding.get("cycle_id") != cid or binding.get("campaign_id") != source_row["campaign_id"]:
            _error("source binding mismatch: " + cid)
        if P.cycle_record_path(target, cid).exists():
            _error("cycle ID already exists at target: " + cid)
        name = record["locator"]
        base = locator.locator_base(record["started_on"], record.get("slug") or "")
        suffix = 2
        while os.path.lexists(target_path.parent / name) or name in used:
            name = base + "-" + str(suffix)
            suffix += 1
        used.add(name)
        destination = _safe(target, target_path.parent / name)
        inventory, identities = listing(directory) if transfer == "rename" else (tree(directory), _identities(directory))
        cycles.append({"cycle_id": cid, "record": record, "source": str(directory.relative_to(source)),
                       "target": str(destination.relative_to(target)), "inventory": inventory,
                       "source_identities": identities, "transfer": transfer, "state": "planned",
                       "original_binding": (directory / locator.CYCLE_BINDING).read_bytes().hex(),
                       "original_manifest": (directory / "manifest.json").read_bytes().hex()
                           if (directory / "manifest.json").is_file() else None})
        records.append(record)
    attach = []
    used_names = set()
    for address, cid in attachments:
        directory = Path(address).absolute()
        _safe(source, directory)
        logs = directory / "dev_logs"
        if directory.is_dir() and {p.name for p in directory.iterdir()} == {"artifacts"}:
            logs = directory / "artifacts/dev_logs"
            if {p.name for p in (directory / "artifacts").iterdir()} != {"dev_logs"}:
                _error("attachment artifacts must contain only dev_logs: " + str(directory))
        _safe(source, logs)
        if not directory.is_dir() or not logs.is_dir() or {p.name for p in directory.iterdir()} != {logs.relative_to(directory).parts[0]}:
            _error("attachment must contain only unregistered dev_logs: " + str(directory))
        item = next((item for item in cycles if item["cycle_id"] == cid), None)
        if item is None:
            _error("attachment cycle is not selected: " + cid)
        if any(directory == source / c["source"] or directory in (source / c["source"]).parents
               or (source / c["source"]) in directory.parents for c in cycles):
            _error("attachment overlaps a cycle")
        name = directory.name
        dest = target / item["target"] / "artifacts/dev_logs/relocated" / name
        index = 2
        while str(dest) in used_names or os.path.lexists(dest) or os.path.lexists(source / item["source"] / dest.relative_to(target / item["target"])):
            dest = dest.parent / (name + "-" + str(index)); index += 1
        used_names.add(str(dest))
        inventory, identities = listing(logs) if transfer == "rename" else (tree(logs), _identities(logs))
        attach.append({"source": str(directory), "logs_source": str(logs), "target": str(dest.relative_to(target)), "cycle_id": cid,
                       "inventory": inventory, "source_identities": identities, "transfer": transfer, "state": "planned"})
    journal = {"schema": "artifact-cross-root-move/v1", "operation_id": operation_id, "state": "planned",
               "source_root": str(source), "target_root": str(target), "source_campaign": source_row["campaign_id"],
               "target_campaign": target_row["campaign_id"], "source_campaign_path": str(source_path.relative_to(source)),
               "target_campaign_path": str(target_path.relative_to(target)), "merge": bool(source_campaign),
               "cycles": cycles, "attachments": attach, "at": P._rfc3339(now),
               "history_actor": P._history_actor("human")}
    owned_routes = set()
    for item in cycles:
        record = item["record"]
        if record.get("route_id"):
            owned_routes.add(record["route_id"])
        owned_routes.update(row["route_id"] for row in record.get("route_bindings", []) if row.get("route_id"))
        current = P._read_manifest_raw(source / item["source"])
        if current:
            owned_routes.update(row["route_id"] for row in current[1].get("routes", []) if row.get("route_id"))
    journal["historical_routes"] = sorted(owned_routes)
    result = _decision(source, journal)
    journal["route_ids"] = list(result.route_ids)
    journal["support_inventory"] = _supports(source, records, source_path, result.route_ids)
    jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    rows = []
    if jobs and Path(jobs).is_file():
        for raw in Path(jobs).read_bytes().splitlines(keepends=True):
            if any(route_id.encode() in raw for route_id in result.route_ids):
                rows.append(raw.decode("utf-8"))
    journal["registry_support"] = {"original_path": jobs, "rows": rows,
        "sha256": hashlib.sha256("".join(rows).encode()).hexdigest(), "count": len(rows)}
    controls = {}
    history = Path(JOURNALS) / operation_id / "historical"
    for rel, expected in journal["support_inventory"].items():
        raw = (source / rel).read_bytes()
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            _error("support changed while planning: " + rel)
        controls[str(history / rel)] = raw.hex()
        prefix = ".runtime/artifact-producer/v1/"
        remainder = rel.removeprefix(prefix)
        # Campaign-wide support is archived intact, but only the selected
        # cycles' controls are reissued into the target's active namespaces.
        selected_control = any(item["cycle_id"] in rel for item in cycles)
        if not selected_control:
            continue
        if rel.startswith(prefix) and remainder.startswith(("manifests/", "history/")):
            controls[rel] = raw.hex()
        elif rel.startswith(prefix) and remainder.startswith((P.OPEN_MANIFEST_DIR + "/", P.CHECKPOINT_DIR + "/")) and rel.endswith(".json"):
            data = _retarget(json.loads(raw), source, target, journal)
            if data.get("manifest_kind"):
                data = relocate_document(target, data, {"operation_id": operation_id, "original_root": str(source)})
            controls[rel] = P._json_bytes(data).hex()
    if rows:
        controls[str(history / "dispatch/selected-jobs.log")] = "".join(rows).encode().hex()
    journal["target_controls"] = controls
    journal["controls_policy"] = 2
    return journal


def _upgrade_controls(journal):
    """Resume older journals without importing another root's shared cursors."""
    if journal.get("controls_policy", 1) >= 2:
        return
    prefix = ".runtime/artifact-producer/v1/"
    ids = [item["cycle_id"] for item in journal["cycles"]]
    journal["target_controls"] = {rel: raw for rel, raw in journal["target_controls"].items()
        if not rel.startswith(prefix) or "/historical/" in rel or any(cid in rel for cid in ids)}
    journal["controls_policy"] = 2


def _rename_locked(source, target):
    """Publish a directory while the caller holds the root admission locks.

    NFSv3 can reject RENAME_NOREPLACE although ordinary rename is supported.
    Use producer admission's existing absence-check/rename protocol there.
    """
    if os.path.lexists(target):
        _error("destination appeared before publication: " + str(target))
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        code = errno.ENOSYS
    else:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return
        code = ctypes.get_errno()
    if code in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        # Recheck after the unsupported syscall too: never adopt a foreign path.
        if os.path.lexists(target):
            _error("destination appeared before publication: " + str(target))
        os.rename(source, target)
        return
    if code == errno.EEXIST:
        _error("destination appeared before publication: " + str(target))
    raise OSError(code, os.strerror(code), str(target))


def _publish_tree(source, target, stage, inventory, *, binding=None, manifest_raw=None, prepare=None, roots=()):
    _safe(target.parent, target)
    published_inventory = dict(inventory)
    if binding is not None:
        published_inventory[locator.CYCLE_BINDING] = {"kind": "file", "bytes": len(binding),
                                                    "sha256": hashlib.sha256(binding).hexdigest()}
    if manifest_raw is not None:
        published_inventory["manifest.json"] = {"kind": "file", "bytes": len(manifest_raw),
                                                 "sha256": hashlib.sha256(manifest_raw).hexdigest()}
    if os.path.lexists(target):
        if tree(target) != published_inventory:
            _error("destination changed: " + str(target))
        return
    if not source.is_dir() or tree(source) != inventory:
        _error("source changed before copy: " + str(source))
    if stage.exists():
        shutil.rmtree(stage)
    P._ensure_dir(stage.parent)
    shutil.copytree(source, stage, symlinks=True)
    _durable(stage)
    if tree(stage) != inventory or tree(source) != inventory:
        _error("payload changed during copy: " + str(source))
    _fault("stage")
    if binding is not None:
        P._write_atomic(stage / locator.CYCLE_BINDING, binding)
    if tree(source) != inventory:
        _error("source changed before publication: " + str(source))
    with admission.lock_roots(roots):
        if prepare:
            prepare(stage)
        # Only manifest/control CAS and the atomic rename hold admission locks.
        if os.path.lexists(target):
            _error("destination appeared during copy: " + str(target))
        P._ensure_dir(target.parent)
        _rename_locked(stage, target)
        P._fsync_dir(target.parent)
    _fault("publish")


def _rename_tree(source, target, item, *, binding=None, manifest_raw=None, prepare=None, roots=(), admit=None):
    """Locked rename; exact inode ownership reconciles a lost rename response."""
    if not os.path.lexists(target):
        inventory, identities = listing(source)
        if inventory != item["inventory"] or identities != item["source_identities"]:
            _error("source changed before rename: " + str(source))
        with admission.lock_roots(roots):
            if admit:
                admit()
            info = source.lstat()
            if [info.st_dev, info.st_ino, info.st_mode] != item["source_identities"][""]:
                _error("source replaced before rename: " + str(source))
            P._ensure_dir(target.parent)
            _rename_locked(source, target)
            P._fsync_dir(source.parent)
            P._fsync_dir(target.parent)
        _fault("rename")
    landed, identities = listing(target)
    if identities.get("") != item["source_identities"][""]:
        _error("foreign rename destination: " + str(target))
    expected = dict(item["inventory"])
    for name, raw in ((locator.CYCLE_BINDING, binding), ("manifest.json", manifest_raw)):
        if raw is None:
            continue
        original = item.get("original_binding" if name == locator.CYCLE_BINDING else "original_manifest")
        path = target / name
        if not path.is_file() or path.is_symlink() or path.read_bytes() not in (bytes.fromhex(original) if original else None, raw):
            _error("renamed control changed: " + str(path))
        landed.pop(name, None)
        expected.pop(name, None)
        identities.pop(name, None)
    original_identities = {name: value for name, value in item["source_identities"].items()
                           if name in identities}
    if landed != expected or identities != original_identities:
        _error("renamed payload changed: " + str(target))
    with admission.lock_roots(roots):
        if binding is not None:
            P._write_atomic(target / locator.CYCLE_BINDING, binding)
        if prepare:
            prepare(target)
    _fault("publish")


def _retarget(value, source, target, journal):
    """Reissue only active cycle-local control addresses; originals are historical."""
    if isinstance(value, dict):
        return {key: item if key in {"shared_references", "shared_reference_revisions"} else
                _retarget(item, source, target, journal) for key, item in value.items()}
    if isinstance(value, list):
        return [_retarget(item, source, target, journal) for item in value]
    if isinstance(value, str):
        for item in journal["cycles"]:
            old = str(source / item["source"])
            if value == old or value.startswith(old + "/"):
                return str(target / item["target"]) + value[len(old):]
        if value == str(source):
            return str(target)
        if value == journal["source_campaign"]:
            return journal["target_campaign"]
    return value


def relocate_document(target, document, provenance):
    """Current membership uses target identities; route rows remain origin-qualified.

    Earlier manifest bytes and IDs stay in snapshots. This only makes a new
    current revision; it grants no execution authority to its historical routes.
    """
    identity = P.artifact_lifecycle.read_root_identity(target)
    if not identity:
        _error("target root identity missing")
    if document["artifact_root_id"] == identity.artifact_root_id and document["repository_id"] == identity.repository_id:
        return document
    original = document.get("relocation") or {}
    document["relocation"] = {"operation_id": provenance["operation_id"], "source_root": provenance["original_root"],
        "source_repository_id": document["repository_id"], "source_artifact_root_id": document["artifact_root_id"],
        "historical_root_ids": list(dict.fromkeys([document["artifact_root_id"], *original.get("historical_root_ids", [])]))}
    document["artifact_root_id"] = identity.artifact_root_id
    document["repository_id"] = identity.repository_id
    return document


def _verify_controls(target, journal):
    for rel, encoded in journal["target_controls"].items():
        path = _safe(target, target / rel)
        if not path.is_file() or path.is_symlink() or path.read_bytes() != bytes.fromhex(encoded):
            _error("target control changed; source retained: " + rel)


def _support_copy(source, target, journal):
    # Expected archive, snapshot and reissued reservation bytes are fixed before
    # any target publication. No existing destination is silently adopted.
    for rel, encoded in journal["target_controls"].items():
        path = _safe(target, target / rel)
        raw = bytes.fromhex(encoded)
        with admission.lock_roots([source, target]):
            if os.path.lexists(path):
                if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                    _error("target control collision: " + rel)
            elif journal.get("controls_published"):
                _error("target control lost; source retained: " + rel)
            else:
                P._ensure_dir(path.parent)
                P._write_exclusive(path, raw)
                P._fsync_dir(path.parent)
    journal["controls_published"] = True
    _save(source, journal)


@contextmanager
def _operation_lock(source, op):
    # Serialize only exact-operation replay. Unrelated producers never acquire
    # this lock. Process death releases it without a recovery command.
    path = _safe(source, source / JOURNALS / (op + ".lock"))
    P._ensure_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _identities(path):
    result = {}
    def visit(node, rel):
        info = node.lstat()
        result[rel] = [info.st_dev, info.st_ino, info.st_mode]
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(node.iterdir()):
                visit(child, str(Path(rel) / child.name) if rel else child.name)
    visit(path, "")
    return result


def _source_survivors(old, item):
    if not os.path.lexists(old):
        if item["state"] not in {"cleaning", "cleaned"}:
            _error("source disappeared before cleanup: " + str(old))
        return
    survivors = tree(old)
    if item["state"] not in {"cleaning", "cleaned"} and survivors != item["inventory"]:
        _error("source changed before cleanup: " + str(old))
    identities = _identities(old)
    for rel, expected in survivors.items():
        if item["inventory"].get(rel) != expected or item["source_identities"].get(rel) != identities[rel]:
            _error("foreign source survivor; copies retained: " + str(old / rel))


def _cleanup(source, target, journal, old, item):
    _verify_controls(target, journal)
    _verify_landed(target, journal, item)
    _source_survivors(old, item)
    item["state"] = "cleaning"
    _save(source, journal)  # durable before the first unlink, not after rmtree
    # Delete only original inventory entries. New foreign children make rmdir
    # fail and survive; replay also refuses any changed/replaced survivor.
    for rel in sorted(item["inventory"], key=lambda r: (len(Path(r).parts), r), reverse=True):
        node = old / rel
        if not os.path.lexists(node):
            continue
        info = node.lstat()
        if item["source_identities"][rel] != [info.st_dev, info.st_ino, info.st_mode]:
            _error("source identity changed during cleanup: " + str(node))
        if item["inventory"][rel]["kind"] == "directory":
            node.rmdir()
        else:
            if tree(node)[""] != item["inventory"][rel]:
                _error("source bytes changed during cleanup: " + str(node))
            node.unlink()
    P._fsync_dir(old.parent)
    item["state"] = "cleaned"
    _save(source, journal)


def _verify_landed(target, journal, item):
    directory = _safe(target, target / item["target"])
    if not os.path.lexists(directory):
        _error("target payload missing; source retained: " + item["target"])
    renamed = item.get("transfer") == "rename"
    landed = listing(directory)[0] if renamed else tree(directory)
    if "logs_source" in item:
        if landed != item["inventory"]:
            _error("target logs changed; source retained: " + item["target"])
        return
    expected_inventory = dict(item["inventory"])
    def file_row(raw):
        row = {"kind": "file", "bytes": len(raw)}
        if not renamed:
            row["sha256"] = hashlib.sha256(raw).hexdigest()
        return row
    binding = locator.cycle_binding_bytes(journal["target_campaign"], item["cycle_id"],
                                           started_on=item["record"].get("started_on"))
    expected_inventory[locator.CYCLE_BINDING] = file_row(binding)
    if (directory / locator.CYCLE_BINDING).read_bytes() != binding:
        _error("target binding changed; data retained: " + item["cycle_id"])
    if item.get("prepared_manifest"):
        raw = P.artifact_manifest.canonical_bytes(item["prepared_manifest"])
        expected_inventory["manifest.json"] = file_row(raw)
        snapshot = _safe(target, P.artifact_lifecycle.manifest_snapshot_path(target, item["cycle_id"],
                                                  item["prepared_manifest"]["manifest_revision_id"]))
        if not snapshot.is_file() or snapshot.is_symlink() or snapshot.read_bytes() != raw:
            _error("required target snapshot changed; source retained: " + item["cycle_id"])
    for rel, expected in expected_inventory.items():
        if landed.get(rel) != expected:
            _error("target payload changed; source retained: " + item["cycle_id"] + "/" + rel)


def _pointer(source, destination, operation_id):
    pointer = source.with_name(source.name + ".RELOCATED.json")
    value = {"schema": "fleet-payload-pointer.v1", "new_path": str(destination), "operation_id": operation_id}
    if pointer.exists() and _json(pointer) != value:
        _error("historical pointer collision: " + str(pointer))
    P._write_atomic(pointer, P._json_bytes(value))


def _receipt(journal):
    return {"status": "moved" if journal["state"] == "committed" else "planned",
            "operation_id": journal["operation_id"], "source_root": journal["source_root"],
            "target_root": journal["target_root"], "campaign_id": journal["target_campaign"],
            "source_campaign_id": journal["source_campaign"], "cycle_ids": [c["cycle_id"] for c in journal["cycles"]],
            "historical_routes": journal["historical_routes"], "support_route_ids": journal["route_ids"],
            "support_inventory": journal["support_inventory"],
            "registry_support": journal["registry_support"],
            "active_cycle_controls": [rel for rel in journal["target_controls"]
                                      if rel.startswith(".runtime/artifact-producer/v1/")
                                      and "/historical/" not in rel],
            "cycles": [dict({key: item[key] for key in ("cycle_id", "source", "target")},
                            transfer=item.get("transfer", "copy")) for item in journal["cycles"]],
            "attachments": journal["attachments"]}


def _local_preview(root, cycle_id, campaign, parent, no_parent):
    if parent is not None and no_parent:
        _error("--parent and --no-parent cannot be combined")
    record = _json(_safe(root, P.cycle_record_path(root, cycle_id)))
    if record.get("relocation", {}).get("artifact_root"):
        raise P.ProducerError("cycle-relocated", str(record["relocation"]))
    original_path, original = _campaign(root, record["campaign_id"])
    target_path, target = _campaign(root, campaign or record["campaign_id"])
    destination = original_path.parent / record["locator"]
    if target["campaign_id"] != original["campaign_id"]:
        name = record["locator"]
        suffix = 2
        while os.path.lexists(target_path.parent / name):
            name = locator.locator_base(record["started_on"], record.get("slug") or "") + "-" + str(suffix)
            suffix += 1
        destination = target_path.parent / name
    if parent is not None:
        parent_path = _safe(root, P.cycle_record_path(root, parent))
        if not parent_path.exists() or parent == cycle_id or P._parent_chain_has(root, parent, cycle_id):
            _error("parent cycle is not joinable")
    return {"status": "planned", "dry_run": True, "cycle_id": cycle_id,
            "campaign_id": target["campaign_id"], "cycle_dir": str(destination),
            "parent_cycle_id": None if no_parent else parent or record.get("parent_cycle_id")}


def move(source, target, *, cycle_id, source_campaign, campaign, attach_logs=(), dry_run=False,
         parent=None, no_parent=False, reason=None, now=None):
    if source == target and dry_run and cycle_id and not source_campaign and not attach_logs:
        return _local_preview(source, cycle_id, campaign, parent, no_parent)
    if parent is not None or no_parent:
        _error("cross-root moves preserve parent links; use cycle-move at target to change a parent")
    attachments = []
    for value in attach_logs:
        address, sep, cid = value.rpartition("=")
        if not sep:
            _error("attachment requires directory=cycle-id")
        attachments.append((str(Path(address).absolute()), cid))
    # Identity precedes discovery: a fully removed source remains replayable.
    identity = [str(source), str(target), cycle_id, source_campaign, campaign, sorted(attachments)]
    op = "move_" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    path = source / JOURNALS / (op + ".json")
    _safe(source, path)
    journal = _json(path) if path.exists() else None
    if journal and journal["state"] == "committed":
        return _receipt(journal)
    if dry_run:
        journal = journal or _plan(source, target, operation_id=op, cycle_id=cycle_id,
            source_campaign=source_campaign, campaign=campaign, attachments=attachments, now=now)
        _upgrade_controls(journal)
        return dict(_receipt(journal), dry_run=True)
    with _operation_lock(source, op):
        journal = _json(path) if path.exists() else _plan(source, target, operation_id=op, cycle_id=cycle_id,
            source_campaign=source_campaign, campaign=campaign, attachments=attachments, now=now)
        if journal["state"] == "committed":
            return _receipt(journal)
        _upgrade_controls(journal)
        now = datetime.fromisoformat(journal["at"].replace("Z", "+00:00")).timestamp()
        _decision(source, journal)
        _save(source, journal)
        _support_copy(source, target, journal)
        staging = _safe(target, target / JOURNALS / op / "staging")
        with admission.lock_roots([source, target], now=now):
            # Seed selected records before manifest publication for internal parent references.
            for item in journal["cycles"]:
                cid = item["cycle_id"]
                target_record_path = P.cycle_record_path(target, cid)
                _safe(target, target_record_path)
                existing = P.read_cycle_record(target, cid)
                if existing and (existing.get("relocation") or {}).get("operation_id") != op:
                    _error("target cycle identity collision: " + cid)
                if not existing:
                    record = dict(item["record"], campaign_id=journal["target_campaign"],
                        locator=Path(item["target"]).name, relocation={"operation_id": op, "original_root": str(source),
                        "original_campaign_id": journal["source_campaign"], "original_locator": item["source"],
                        "original_repository_id": P.artifact_lifecycle.read_root_identity(source).repository_id,
                        "external_parent_root": str(source) if item["record"].get("parent_cycle_id") not in
                            {c["cycle_id"] for c in journal["cycles"]} else None})
                    P._write_cycle_record(target, record, exclusive=True)
                P._edit_campaign_members(target, (target / journal["target_campaign_path"]).parent, cid, joining=True)
        for item in journal["cycles"]:
            if item["state"] == "planned":
                _safe(source, source / item["source"])
                _safe(target, target / item["target"])
                _safe(target, staging / item["cycle_id"])
                prepared = item.get("prepared_manifest")
                original_manifest = P._read_manifest_raw(source / item["source"])
                if original_manifest is not None and prepared is None:
                    prepared = P._next_document(original_manifest[1], P.artifact_identity.IdAllocator())
                    prepared = relocate_document(target, prepared, P.read_cycle_record(target, item["cycle_id"])["relocation"])
                    target_campaign = _json(target / journal["target_campaign_path"])
                    prepared["campaign"] = dict(prepared["campaign"], campaign_id=journal["target_campaign"],
                        goal=target_campaign.get("goal", ""), title=target_campaign.get("title", ""),
                        completion_criterion={"statement": (target_campaign.get("completion_criterion") or {}).get("statement", "")})
                    prepared["cycle"] = dict(prepared["cycle"], campaign_id=journal["target_campaign"])
                    item["prepared_manifest"] = prepared
                    _save(source, journal)
                raw = P.artifact_manifest.canonical_bytes(prepared) if prepared else None
                def prepare(stage):
                    if prepared is None:
                        return
                    digest = P.artifact_manifest.manifest_digest(prepared)
                    index = admission.load_index(target)
                    if (index.manifests.get(item["cycle_id"]) or {}).get("manifest_digest") == digest:
                        P._write_atomic(stage / "manifest.json", raw)
                        return
                    original = P._read_manifest_raw(stage)
                    P._publish_document_locked(target, P.read_cycle_record(target, item["cycle_id"]), stage,
                        original[0], original[1], prepared, [], now=now, moved_fields=("campaign_id",),
                        cycle_path=item["target"], index=index)
                binding = locator.cycle_binding_bytes(journal["target_campaign"], item["cycle_id"],
                                                       started_on=item["record"].get("started_on"))
                if item.get("transfer") == "rename":
                    _rename_tree(source / item["source"], target / item["target"], item,
                        binding=binding, manifest_raw=raw, prepare=prepare, roots=[source, target],
                        admit=lambda: _decision(source, journal))
                else:
                    _publish_tree(source / item["source"], target / item["target"], staging / item["cycle_id"], item["inventory"],
                        binding=binding, manifest_raw=raw, prepare=prepare, roots=[source, target])
                item["state"] = "published"; _save(source, journal)
        for index, item in enumerate(journal["attachments"]):
            if item["state"] == "planned":
                _safe(source, Path(item["logs_source"]))
                _safe(target, target / item["target"])
                _safe(target, staging / ("logs-" + str(index)))
                if item.get("transfer") == "rename":
                    _rename_tree(Path(item["logs_source"]), target / item["target"], item,
                        roots=[source, target], admit=lambda: _decision(source, journal))
                else:
                    _publish_tree(Path(item["logs_source"]), target / item["target"], staging / ("logs-" + str(index)), item["inventory"], roots=[source, target])
                item["state"] = "published"; _save(source, journal)
        _decision(source, journal)
        with admission.lock_roots([source, target], now=now):
            for item in journal["cycles"]:
                cid = item["cycle_id"]
                if item["state"] == "published":
                    record = P.read_cycle_record(target, cid)
                    # A crash after publication can replay with an already adopted record.
                    record, _ = P._adopt_location_locked(target, record, target / item["target"],
                        command="cycle-move", stamp=op, reason=reason, now=now, by="human")
                    P._edit_campaign_members(target, (target / journal["target_campaign_path"]).parent, cid, joining=True)
                    item["state"] = "adopted"; _save(source, journal)
            locator.update_indexes(target, [journal["target_campaign"]])
        _fault("metadata")
        canonical = {"artifact_root": str(target), "campaign_id": journal["target_campaign"], "operation_id": op}
        source_campaign_path = source / journal["source_campaign_path"]
        with admission.lock_roots([source, target], now=now):
            if journal["merge"]:
                row = campaigns.fold_campaign(source, source_campaign_path, _json(source_campaign_path))
                row["relocation"] = canonical
                P._write_campaign(source, row, exclusive=False)
                campaigns.supersede_locked(source, source_campaign_path, operation_id=op,
                    target_root=target, target_campaign=journal["target_campaign"])
        _fault("closure")
        # Re-observe liveness and source content before the source-last destructive step.
        _decision(source, journal)
        for item in journal["cycles"]:
            _verify_landed(target, journal, item)
            if item.get("transfer") != "rename":
                _source_survivors(source / item["source"], item)
        for item in journal["attachments"]:
            _verify_landed(target, journal, item)
            if item.get("transfer") != "rename":
                _source_survivors(Path(item["logs_source"]), item)
        _verify_controls(target, journal)
        for item in journal["cycles"]:
            cid = item["cycle_id"]
            old, new = source / item["source"], target / item["target"]
            _safe(source, old); _safe(target, new)
            with admission.lock_roots([source], now=now):
                record = P.read_cycle_record(source, cid)
                record["relocation"] = dict(canonical, cycle_id=cid, locator=item["target"])
                P._write_cycle_record(source, record, exclusive=False)
                _pointer(old, new, op)
                P._edit_campaign_members(source, source_campaign_path.parent, cid, joining=False)
                if item.get("transfer") == "rename":
                    item["state"] = "cleaned"; _save(source, journal)
                else:
                    _cleanup(source, target, journal, old, item)
            _fault("source-cleanup")
        for item in journal["attachments"]:
            old, new = Path(item["source"]), target / item["target"]
            _safe(source, old); _safe(target, new)
            logs = Path(item["logs_source"])
            with admission.lock_roots([source], now=now):
                _pointer(logs, new, op)
                _pointer(old, new, op)
                if item.get("transfer") == "rename":
                    item["state"] = "cleaned"; _save(source, journal)
                else:
                    _cleanup(source, target, journal, logs, item)
        with admission.lock_roots([source, target], now=now):
            P._retire_rows(source, [item["cycle_id"] for item in journal["cycles"]])
            locator.update_indexes(source, [journal["source_campaign"]])
            for item in journal["cycles"]:
                cid = item["cycle_id"]
                line = P._command_line(command="cycle-move", stamp=op, target_type="cycle", target_id=cid,
                    target_path=item["target"], operation="move", field="path",
                    before={"value": {"root": str(source), "path": item["source"]}},
                    after={"value": {"root": str(target), "path": item["target"]}}, reason=reason, now=now, by="human")
                for key in ("actor", "actor_by", "session", "harness", "route", "attempt"):
                    line.pop(key, None)
                line.update(journal["history_actor"])
                record = P.read_cycle_record(target, cid)
                P._write_cycle_record(target, P._with_cycle_lines(record, [line]), exclusive=False)
                P._flush_cycle_pending_locked(target, cid)
            for item in journal["attachments"]:
                cid = item["cycle_id"]
                payload_digest = hashlib.sha256(P._json_bytes(item["inventory"])).hexdigest()
                line = P._command_line(command="cycle-move", stamp=op + item["source"], target_type="cycle", target_id=cid,
                    target_path=item["target"], operation="move", field="path",
                    before={"value": {"root": str(source), "path": str(Path(item["source"]).relative_to(source)),
                                       "payload_sha256": payload_digest}},
                    after={"value": {"root": str(target), "path": item["target"], "cycle_id": cid}},
                    reason="attach relocated logs", now=now, by="human")
                for key in ("actor", "actor_by", "session", "harness", "route", "attempt"):
                    line.pop(key, None)
                line.update(journal["history_actor"])
                record = P.read_cycle_record(target, cid)
                P._write_cycle_record(target, P._with_cycle_lines(record, [line]), exclusive=False)
                P._flush_cycle_pending_locked(target, cid)
        _fault("history")
        journal["state"] = "committed"
        _save(source, journal)
        _save(target, journal)
        return _receipt(journal)


def historical_route_ids(root):
    """Committed source routes are history, not candidates for automatic closure."""
    ids = set()
    for path in (Path(root) / JOURNALS).glob("*.json"):
        _safe(root, path)
        row = _json(path)
        if row.get("state") == "committed" and row.get("source_root") == str(Path(root).resolve()):
            ids.update(row.get("historical_routes", []))
    return ids
