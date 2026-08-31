"""Record copy hashes, detect drift, and reapply updates.

Only files copied into a runtime home are tracked. Symlinks are already
canonical, and plugin caches are runtime-owned. The schema shares conventions
with harness-layer-sync.

## Directory layout (under ``<runtime_home>/.harness/``)

- ``manifest.json`` — installed-file hash registry:
  ```json
  {"schema": 1, "runtime": "claude", "scope": "global",
   "version": "<repo git SHA at install>", "timestamp": "<iso8601>",
   "files": {"settings.json": "<sha256hex>", "keybindings.json": "<sha256hex>"}}
  ```
- ``pristine/<relpath>`` — exact canonical bytes at installation time, used as
  the three-way merge base and retained until a successful reapply.
- ``local-patches/<relpath>`` — full backup before replacing a user-edited file.
- ``local-patches/backup-meta.json`` — backup metadata:
  ```json
  {"from_version": "<sha>", "pristine_hashes": {"<relpath>": "<sha256>"}}
  ```

## Cycle 1 scope

The manifest covers copy-once files only, including Claude settings and Windows
copies. OpenCode's merge-managed configuration is tracked separately. SHA-256
hex matches GSD/HLS conventions and shared key names follow
``tools/build-manifest.py``.
"""


import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import paths
import safe_fs


_CHUNK_SIZE = 65536


def _sha256(path):
    """Return the SHA-256 hex digest while streaming the file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(runtime, scope):
    return paths.harness_state_dir(runtime, scope) / "manifest.json"


def _pristine_path(runtime, scope, relpath):
    return paths.harness_state_dir(runtime, scope) / "pristine" / relpath


def _backup_path(runtime, scope, relpath):
    return paths.harness_state_dir(runtime, scope) / "local-patches" / relpath


def _safe_relpath(rel):
    """Validate a manifest relative path before disk access.

    Reject absolute paths, parent traversal, NUL bytes, and zip-slip escapes.
    """
    if "\x00" in rel:
        raise ValueError(f"relpath contains a NUL byte: {rel!r}")

    p = Path(rel)
    if p.is_absolute():
        raise ValueError(f"relpath must not be absolute: {rel!r}")

    if any(part == ".." for part in p.parts):
        raise ValueError(f"relpath contains '..' traversal: {rel!r}")

    base = Path("/__manifest_safe_base__").resolve()
    resolved = (base / p).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(f"relpath escapes the base path: {rel!r}")

    return rel


def _load_manifest(path):
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_ownership_manifest(path, runtime, scope):
    """Load and seal the exact manifest object used as uninstall authority.

    A parsed dictionary by itself is not ownership evidence: the pathname can
    be replaced between parsing and deletion.  Return the exact file state so
    uninstall can compare it again after acquiring all target locks.
    """

    path = Path(path)
    try:
        state = safe_fs.capture_state(path, include_payload=True)
    except safe_fs.SafetyError as exc:
        raise ValueError(f"ownership-unproved: unsafe manifest: {exc}") from exc
    if state.kind == "missing":
        return None, state
    if state.kind != "file" or state.payload is None:
        raise ValueError(f"ownership-unproved: manifest is not a regular file: {path}")
    try:
        data = json.loads(state.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"ownership-unproved: corrupt manifest: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"ownership-unproved: manifest is not an object: {path}")
    if (
        data.get("schema") != 1
        or data.get("runtime") != runtime
        or data.get("scope") != scope
    ):
        raise ValueError(
            f"ownership-unproved: manifest identity does not match {runtime}/{scope}: {path}"
        )
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"ownership-unproved: manifest files map is invalid: {path}")
    for relpath, digest in files.items():
        if not isinstance(relpath, str):
            raise ValueError(f"ownership-unproved: manifest path is not text: {path}")
        _safe_relpath(relpath)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                f"ownership-unproved: manifest digest is invalid for {relpath}: {path}"
            )
    return data, state


def _write_manifest(path, data):
    path = Path(path)
    payload = json.dumps(
        data, sort_keys=True, ensure_ascii=False, indent=2
    ).encode("utf-8")
    current = safe_fs.capture_state(path)
    auth = safe_fs.authority(
        path,
        owner="manifest:ownership-registry",
        allowed_paths=(path,),
        allow_leaf_symlink=False,
        expected=current,
    )
    safe_fs.atomic_write_bytes(auth, payload, 0o600, create_parents=True)


def _atomic_write_bytes(path, data):
    """Write bytes through a temporary file and atomic rename."""
    path = Path(path)
    current = safe_fs.capture_state(path)
    auth = safe_fs.authority(
        path,
        owner="manifest:pristine-or-local-patch",
        allowed_paths=(path,),
        allow_leaf_symlink=False,
        expected=current,
    )
    safe_fs.atomic_write_bytes(auth, data, 0o600, create_parents=True)


def _git_head_or_unknown():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(paths.agent_home()),
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except Exception:
        pass
    return "unknown"


def _three_way_merge(ours, base, theirs):
    """Run ``git merge-file -p --diff3 ours base theirs``.

    Return ``(ok, merged_bytes, had_conflict_markers, tool_missing)``.
    """
    try:
        result = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", str(ours), str(base), str(theirs)],
            capture_output=True,
        )
    except FileNotFoundError:
        return (False, None, False, True)

    merged_bytes = result.stdout
    conflict_markers = any(
        marker in merged_bytes for marker in (b"<<<<<<<", b"=======", b">>>>>>>")
    )
    ok = result.returncode == 0 and not conflict_markers
    return (ok, merged_bytes, conflict_markers, False)


def record(runtime, files, scope="global", version=None):
    """Record each copied file's hash after installation.

    ``files`` contains projector ``copy_once`` actions. Create pristine
    snapshots only when absent; update them only after a verified reapply.
    """
    manifest_path = _manifest_path(runtime, scope)
    existing = _load_manifest(manifest_path) or {}
    files_map = dict(existing.get("files", {}))

    for entry in files:
        relpath = _safe_relpath(entry["relpath"])
        source_abs = Path(entry["source_abs"])
        dest_abs = Path(entry["dest_abs"])

        pristine_path = _pristine_path(runtime, scope, relpath)
        if not pristine_path.exists():
            _atomic_write_bytes(pristine_path, source_abs.read_bytes())

        files_map[relpath] = _sha256(dest_abs)

    if version is None:
        version = _git_head_or_unknown()

    manifest = {
        "schema": 1,
        "runtime": runtime,
        "scope": scope,
        "version": version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": files_map,
    }
    _write_manifest(manifest_path, manifest)
    return manifest


def check_drift(runtimes, scope="global"):
    """Compare registered hashes with current files and return user drift.

    Missing manifests mean the runtime was never installed and are skipped.
    """
    drift = []
    for rt in runtimes:
        manifest = _load_manifest(_manifest_path(rt, scope))
        if manifest is None:
            continue

        files_map = manifest.get("files", {})
        for relpath in sorted(files_map.keys()):
            relpath = _safe_relpath(relpath)
            recorded_hash = files_map[relpath]
            dest_abs = paths.runtime_home(rt, scope) / relpath

            if not dest_abs.exists():
                drift.append({"runtime": rt, "path": relpath, "detail": "file missing"})
                continue

            current_hash = _sha256(dest_abs)
            if current_hash != recorded_hash:
                drift.append({"runtime": rt, "path": relpath, "detail": "hash mismatch"})

    return drift


def reapply(runtimes, scope="global", sources=None):
    """Reapply new files while preserving local-patch backups.

    ``sources`` maps runtimes and relative paths to current canonical sources.
    Report three-way conflicts instead of forcing a merge.
    """
    sources = sources or {}

    result = {"reapplied": [], "conflicts": [], "verify_failed": [], "missing": []}
    touched_runtimes = set()

    for rt in runtimes:
        rt_sources = sources.get(rt, {})
        rt_drift = check_drift([rt], scope=scope)

        manifest_path = _manifest_path(rt, scope)
        manifest = _load_manifest(manifest_path)
        if manifest is None:
            continue

        for entry in rt_drift:
            relpath = _safe_relpath(entry["path"])
            detail = entry["detail"]

            if detail == "file missing":
                result["missing"].append({"runtime": rt, "path": relpath})
                continue

            dest_abs = paths.runtime_home(rt, scope) / relpath
            pristine_path = _pristine_path(rt, scope, relpath)
            backup_path = _backup_path(rt, scope, relpath)

            # Step 1 — pre-wipe backup.
            _atomic_write_bytes(backup_path, dest_abs.read_bytes())

            backup_meta_path = _backup_path(rt, scope, "backup-meta.json").parent / "backup-meta.json"
            backup_meta = _load_manifest(backup_meta_path) or {
                "from_version": manifest.get("version"),
                "pristine_hashes": {},
            }
            backup_meta["from_version"] = manifest.get("version")
            if pristine_path.exists():
                backup_meta["pristine_hashes"][relpath] = _sha256(pristine_path)
            _write_manifest(backup_meta_path, backup_meta)

            source_abs = rt_sources.get(relpath)
            if source_abs is None:
                result["verify_failed"].append(
                    {"runtime": rt, "path": relpath, "status": "no canonical source provided"}
                )
                continue

            if not pristine_path.exists():
                result["verify_failed"].append(
                    {"runtime": rt, "path": relpath, "status": "no pristine snapshot"}
                )
                continue

            # Step 2 — 3-way merge.
            ok, merged_bytes, had_conflict, tool_missing = _three_way_merge(
                dest_abs, pristine_path, source_abs
            )

            if tool_missing:
                result["conflicts"].append(
                    {"runtime": rt, "path": relpath, "status": "no-git-merge-file"}
                )
                continue

            if not ok:
                result["conflicts"].append({"runtime": rt, "path": relpath, "status": "conflict"})
                continue

            # Step 3 — deterministic post-merge verifier.
            backup_lines = [
                line for line in backup_path.read_bytes().splitlines() if line.strip()
            ]
            verify_ok = all(line in merged_bytes for line in backup_lines)

            if not verify_ok:
                result["verify_failed"].append(
                    {"runtime": rt, "path": relpath, "status": "verify_failed"}
                )
                continue

            # Step 4 — write merged output, refresh pristine, update manifest hash.
            _atomic_write_bytes(dest_abs, merged_bytes)
            _atomic_write_bytes(pristine_path, Path(source_abs).read_bytes())
            manifest.setdefault("files", {})[relpath] = _sha256(dest_abs)
            touched_runtimes.add(rt)

            result["reapplied"].append({"runtime": rt, "path": relpath})

        if rt in touched_runtimes:
            manifest["version"] = _git_head_or_unknown()
            manifest["timestamp"] = datetime.now(timezone.utc).isoformat()
            _write_manifest(manifest_path, manifest)

    return result
