#!/usr/bin/env python3
"""Resolve, seal, verify, and package experiment configuration provenance."""
import argparse, hashlib, json, os, re, subprocess, sys, tarfile, zipfile
from pathlib import Path

ERROR = 65

DEFAULT_ROOTS = {"default": "configs", "exp": "configs_exp", "legacy": "configs_legacy"}
ROOT_NAMESPACE_PREFIX = {"default": "config", "exp": "exp", "legacy": "legacy"}

SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RETRY = re.compile(r"^(?P<base>.+)__a(?P<n>[1-9][0-9]*)$")
CONFIG_REF_RE = re.compile(r"^(config|exp|legacy|path):[^\x00-\x1f\x7f]+$")

SOURCE_GIT_STATES = (
    "clean", "staged", "unstaged", "staged+unstaged", "untracked", "ignored", "unknown-no-git",
)

# The 11-key manifest contract. This constant, the cmd_verify() key-set check,
# and capabilities/lab-config-manifest.schema.json's `required` array are a
# three-way anchor -- they must all agree, and a drift test proves it.
MANIFEST_KEYS = frozenset({
    "schema_version", "config_ref", "run_id", "source_path", "source_sha256",
    "source_commit", "source_dirty", "source_git_state", "snapshot_path",
    "snapshot_sha256", "config_layout",
})

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def fail(message):
    print("lab-config-provenance:", message, file=sys.stderr)
    raise SystemExit(ERROR)

def safe_slug(value):
    return bool(SAFE_SLUG.match(value))

def safe_run_id(value):
    return bool(SAFE_RUN_ID.match(value))

def _config_ref_conforms(value):
    if not isinstance(value, str) or not CONFIG_REF_RE.match(value):
        return False
    _, _, rest = value.partition(":")
    if ".." in Path(rest).parts or Path(rest).is_absolute() or rest.endswith("/"):
        return False
    return True

def repo_path(repo, raw):
    root = Path(repo).resolve(strict=True)
    candidate = (root / raw).resolve(strict=False) if not Path(raw).is_absolute() else Path(raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        fail("path escapes repository")
    if candidate.exists():
        try:
            candidate.resolve(strict=True).relative_to(root)
        except ValueError:
            fail("symlink escapes repository")
    return candidate

def _validate_root_value(value, decl_path):
    if not isinstance(value, str) or not value:
        fail(f"invalid layout declaration ({decl_path}): root value must be a non-empty string")
    if "\\" in value:
        fail(f"invalid layout declaration ({decl_path}): root value must use POSIX separators")
    if value.startswith("/"):
        fail(f"invalid layout declaration ({decl_path}): root value must not be absolute")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        fail(f"invalid layout declaration ({decl_path}): root value contains control characters")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail(f"invalid layout declaration ({decl_path}): root value must be a normalized relative path")
    return value

def _check_roots_distinct(repo, roots, decl_path):
    lexical = {}
    for key, value in roots.items():
        lexical.setdefault(value, []).append(key)
    if any(len(keys) > 1 for keys in lexical.values()):
        fail(f"invalid layout declaration ({decl_path}): roots must be pairwise distinct")
    # Fourth guard (A1/A2): roots must also stay pairwise distinct once
    # resolved, since canonical_ref() attributes by resolved path -- a
    # symlink making one root's real directory equal another's would
    # otherwise defeat the lexical-distinct check above.
    resolved = {}
    for key, value in roots.items():
        rp = (repo / value).resolve(strict=False)
        resolved.setdefault(rp, []).append(key)
    if any(len(keys) > 1 for keys in resolved.values()):
        fail(f"invalid layout declaration ({decl_path}): roots must be pairwise distinct after resolution")

def layout_spec(repo):
    json_decl = repo / ".lab-config-layout.json"
    if json_decl.is_file():
        try:
            data = json.loads(json_decl.read_text())
        except json.JSONDecodeError as e:
            fail(f"invalid layout declaration ({json_decl}): {e}")
        if not isinstance(data, dict):
            fail(f"invalid layout declaration ({json_decl}): must be a JSON object")
        allowed_top = {"schema_version", "layout", "roots"}
        extra = set(data) - allowed_top
        if extra:
            fail(f"invalid layout declaration ({json_decl}): unexpected top-level keys {sorted(extra)}")
        if data.get("schema_version") != 1:
            fail(f"invalid layout declaration ({json_decl}): schema_version must be 1")
        roots_raw = data.get("roots")
        if not isinstance(roots_raw, dict) or set(roots_raw) != {"default", "exp", "legacy"}:
            fail(f"invalid layout declaration ({json_decl}): roots must have exactly default/exp/legacy keys")
        roots = {key: _validate_root_value(value, json_decl) for key, value in roots_raw.items()}
        _check_roots_distinct(repo, roots, json_decl)
        layout_name = data.get("layout")
        if layout_name is not None and (not isinstance(layout_name, str) or not layout_name.strip()):
            fail(f"invalid layout declaration ({json_decl}): layout must be a non-empty string")
        label = f"declared/{layout_name}" if layout_name else "declared/custom"
        return label, roots, "json-roots"
    plain_decl = repo / ".lab-config-layout"
    if plain_decl.is_file():
        return f"declared/{plain_decl.read_text().strip()}", dict(DEFAULT_ROOTS), "label-only"
    conventions = repo / "analysis_project/code/experiment_conventions.md"
    if conventions.is_file():
        for line in conventions.read_text().splitlines():
            if line.lower().startswith("lab-config-layout:"):
                name = line.split(":", 1)[1].strip()
                return f"declared/{name}", dict(DEFAULT_ROOTS), "conventions-label"
    if (repo / DEFAULT_ROOTS["default"]).is_dir():
        return "structured", dict(DEFAULT_ROOTS), "none"
    return "legacy/unstructured", dict(DEFAULT_ROOTS), "none"

def _under(root, root_rel, rest):
    if ".." in Path(rest).parts: fail("path traversal is not allowed")
    base = (Path(root) / root_rel).resolve(strict=False)
    path = repo_path(root, str(Path(root_rel, rest)))
    try:
        path.relative_to(base)
    except ValueError:
        fail("reference escapes its lifecycle root")
    return path

def canonical_ref(repo, resolved_path, roots):
    resolved_path = Path(resolved_path).resolve(strict=False)
    candidates = []
    for key in ("default", "exp", "legacy"):
        root_resolved = (repo / roots[key]).resolve(strict=False)
        try:
            rel = resolved_path.relative_to(root_resolved)
        except ValueError:
            continue
        candidates.append((len(root_resolved.parts), key, rel))
    if candidates:
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, key, rel = candidates[0]
        return f"{ROOT_NAMESPACE_PREFIX[key]}:{rel.as_posix()}", key
    rel = resolved_path.relative_to(repo)
    return f"path:{rel.as_posix()}", "path"

def resolve_ref(repo, ref, requested=None):
    root = Path(repo).resolve(strict=True)
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in ref):
        fail("control characters are not allowed in a config reference")
    if ".." in Path(ref).parts: fail("path traversal is not allowed")
    label, roots, declaration_kind = layout_spec(root)
    requested_ns = None
    if ref.startswith("exp:"):
        rest = ref[4:]
        if not rest: fail("experiment reference requires a path")
        path = _under(root, roots["exp"], rest)
        requested_ns = "exp"
    elif ref.startswith("legacy:"):
        rest = ref[7:]
        if not rest: fail("legacy reference requires a path")
        path = _under(root, roots["legacy"], rest)
        requested_ns = "legacy"
    elif ref.startswith("config:"):
        rest = ref[7:]
        if not rest: fail("config reference requires a path")
        path = _under(root, roots["default"], rest)
        requested_ns = "default"
    elif Path(ref).is_absolute() or "/" in ref or "\\" in ref:
        path = repo_path(root, ref)
    else:
        if not (root / roots["default"]).is_dir():
            fail("bare config is unavailable outside a structured layout")
        path = _under(root, roots["default"], ref)
    if not path.is_file(): fail("config does not exist: " + str(path))
    config_ref, namespace = canonical_ref(root, path, roots)
    if requested_ns is not None and namespace != requested_ns:
        fail(f"reference explicitly requested the {ROOT_NAMESPACE_PREFIX[requested_ns]} "
             f"namespace but resolves under {ROOT_NAMESPACE_PREFIX.get(namespace, namespace)}; "
             "use an explicit physical path to reach a nested root")
    if requested is not None and requested != requested_ns:
        fail("requested namespace does not match the reference prefix")
    return {
        "layout": label,
        "layout_declaration": declaration_kind,
        "roots": roots,
        "root": str(root),
        "namespace": namespace,
        "config_ref": config_ref,
        "input_ref": ref,
        "path": str(path),
    }

def normalize(value):
    value = re.sub(r"[^A-Za-z0-9._-]", "-", value)
    return re.sub(r"-+", "-", value)

def _ref_stem(config_ref):
    _, _, rest = config_ref.partition(":")
    return normalize(Path(rest).name.rsplit(".", 1)[0])

def run_id(slug, config_ref, source_sha256, attempt=None):
    stem = _ref_stem(config_ref)
    material = "\n".join([slug, config_ref, source_sha256 or ""]).encode()
    result = f"{normalize(slug)}__{stem}__{hashlib.sha256(material).hexdigest()[:12]}"
    return result + (f"__a{attempt}" if attempt is not None else "")

def _iter_porcelain_records(raw):
    fields = [f for f in raw.split(b"\0") if f]
    records, i = [], 0
    while i < len(fields):
        chunk = fields[i]; i += 1
        code = chunk[:2].decode()
        path = chunk[3:].decode()
        old = None
        # -z rename/copy records are two NUL-terminated fields ("XY new" then
        # "old"); consuming the extra field here keeps the next record from
        # being misparsed as part of this one.
        if code[0] in "RC" and i < len(fields):
            old = fields[i].decode(); i += 1
        records.append((code, path, old))
    return records

def git_state(repo, rel_path):
    root = Path(repo)
    check = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        return "unknown-no-git", "unknown"
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    commit = head.stdout.strip() if head.returncode == 0 else "unknown"
    rel_str = str(rel_path)
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--ignored=matching", "--", rel_str],
        capture_output=True,
    )
    if status.returncode != 0:
        return "unknown-no-git", commit
    # NOTE: files marked assume-unchanged/skip-worktree read as "clean" here;
    # that is git's own semantics for those flags and is out of scope.
    records = _iter_porcelain_records(status.stdout)
    match = next((r for r in records if r[1] == rel_str or r[2] == rel_str), None)
    if match is None:
        return "clean", commit
    code = match[0]
    if code == "!!":
        return "ignored", commit
    if code == "??":
        return "untracked", commit
    x, y = code[0], code[1]
    staged = x not in (" ", "?")
    unstaged = y != " "
    if staged and unstaged:
        return "staged+unstaged", commit
    if staged:
        return "staged", commit
    if unstaged:
        return "unstaged", commit
    return "clean", commit

def _producer():
    import importlib
    utilities = Path(__file__).resolve().parents[1] / "utilities"
    if str(utilities) not in sys.path:
        sys.path.insert(0, str(utilities))
    return importlib.import_module("artifact_producer")

def _assert_manifest_location(manifest_path, snapshot_path):
    m = Path(manifest_path).resolve(strict=False)
    s = Path(snapshot_path).resolve(strict=False)
    if m.parent != s.parent:
        fail("manifest and snapshot must be co-located")

def _manifest_chain_slug(manifest_path):
    # Two candidates: a lexical (symlink-unresolved) form and a fully resolved
    # form. Either satisfying the experiments/<slug>/_internal/configs shape
    # is accepted -- cmd_seal's `resolve(strict=True)` on the output directory
    # (see cmd_seal below) can collapse a symlinked `experiments` segment in
    # the recorded paths, so the lexical candidate recovers the slug in that
    # layout while the resolved candidate covers the reverse (logical-path
    # side symlinked) layout.
    manifest_path = Path(manifest_path)
    candidates = (Path(os.path.abspath(str(manifest_path))), manifest_path.resolve(strict=False))
    for cand in candidates:
        if (cand.parent.name == "configs" and cand.parent.parent.name == "_internal"
                and cand.parent.parent.parent.parent.name == "experiments"):
            # Recomputation below assumes this recovered slug is byte-identical
            # to the original `a.slug` cmd_seal used -- true only because
            # cmd_seal names the directory with the raw, unnormalized slug.
            slug = cand.parent.parent.parent.name
            if not safe_slug(slug):
                fail("inferred experiment slug is not a safe slug")
            return slug
    fail("manifest must live in <artifact-root>/experiments/<slug>/_internal/configs")

def cmd_resolve(a): print(json.dumps(resolve_ref(a.repo, a.ref), sort_keys=True))

def cmd_run_id(a):
    if not safe_slug(a.slug): fail("invalid --slug")
    if not _config_ref_conforms(a.config_ref):
        fail("--config-ref does not match the expected grammar")
    if not isinstance(a.config_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", a.config_sha256):
        fail("--config-sha256 must be 64 lowercase hex characters")
    if a.attempt is not None and a.attempt < 1:
        fail("--attempt must be a positive integer")
    print(run_id(a.slug, a.config_ref, a.config_sha256, a.attempt))

def cmd_seal(a):
    if not safe_slug(a.slug): fail("invalid --slug")
    resolved = resolve_ref(a.repo, a.config)
    if a.config_ref is not None and a.config_ref != resolved["config_ref"]:
        fail("--config-ref does not match the resolved canonical reference")
    source = Path(resolved["path"]); digest = sha(source)
    artifact_root = Path(a.artifact_root)
    if not artifact_root.is_absolute() or not artifact_root.is_dir():
        fail("--artifact-root must be an existing absolute directory")
    artifact_root = artifact_root.resolve(strict=True)
    # _manifest_chain_slug() recomputes the sealed run id from a slug it
    # recovers from this directory name -- that recomputation is only valid
    # because this uses the raw, unnormalized `a.slug` (not normalize(slug)).
    # W7C: `experiments/` resolves under the open producer cycle once the
    # write-cutover is active (AGENT_ARTIFACT_CYCLE_DIR); the legacy top-level
    # bucket is only reachable during the compatibility window.
    try:
        experiments_dir, _layout = _producer().resolve_output_dir(artifact_root, "experiments")
    except Exception as exc:  # ProducerError or import failure: fail closed
        fail(f"experiments bucket unavailable: {exc}")
    out = experiments_dir / a.slug / "_internal" / "configs"
    try:
        out.resolve(strict=False).relative_to(artifact_root)
    except ValueError:
        fail("derived output path escapes the artifact root")
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve(strict=True)
    snapshot = out / f"{digest}{source.suffix}"
    if snapshot.exists() and sha(snapshot) != digest: fail("snapshot filename/content mismatch")
    if not snapshot.exists(): snapshot.write_bytes(source.read_bytes())
    rel_source = source.relative_to(Path(resolved["root"]))
    state, commit = git_state(resolved["root"], rel_source)
    computed_run_id = run_id(a.slug, resolved["config_ref"], digest)
    if a.run_id is not None:
        if not safe_run_id(a.run_id): fail("invalid --run-id")
        if a.run_id != computed_run_id: fail("--run-id does not match the computed run id")
        run_id_value = a.run_id
    else:
        run_id_value = computed_run_id
    manifest = {
        "schema_version": 2,
        "config_ref": resolved["config_ref"],
        "run_id": run_id_value,
        "source_path": str(source),
        "source_sha256": digest,
        "source_commit": commit,
        "source_dirty": state != "clean",
        "source_git_state": state,
        "snapshot_path": str(snapshot),
        "snapshot_sha256": sha(snapshot),
        "config_layout": resolved["layout"],
    }
    target = out / f"{run_id_value}.manifest.json"
    _assert_manifest_location(target, snapshot)
    if target.exists() and json.loads(target.read_text()) != manifest: fail("manifest already exists with different inputs")
    if not target.exists(): target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, sort_keys=True))

def cmd_verify(a):
    manifest_path = Path(a.manifest)
    try:
        m = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        fail(f"manifest is not valid JSON: {e}")
    if not isinstance(m, dict): fail("manifest must be a JSON object")
    if set(m) != MANIFEST_KEYS: fail("manifest key set does not match the schema")
    if m.get("schema_version") != 2: fail("unsupported schema_version")
    for key in ("source_sha256", "snapshot_sha256"):
        if not isinstance(m.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", m[key]):
            fail(f"{key} is not a valid sha256 digest")
    if not isinstance(m.get("source_dirty"), bool): fail("source_dirty must be a boolean")
    if m.get("source_git_state") not in SOURCE_GIT_STATES: fail("source_git_state is not a recognized value")
    for key in ("config_ref", "run_id", "source_path", "snapshot_path", "source_commit", "config_layout"):
        if not isinstance(m.get(key), str) or not m[key]: fail(f"{key} must be a non-empty string")
    for key in ("source_path", "snapshot_path"):
        if not Path(m[key]).is_absolute(): fail(f"{key} must be an absolute path")
    if m["source_dirty"] != (m["source_git_state"] != "clean"):
        fail("source_dirty disagrees with source_git_state")
    if not _config_ref_conforms(m["config_ref"]): fail("config_ref does not match the expected grammar")
    if not safe_run_id(m["run_id"]): fail("run_id is not a safe run id")
    if m["source_sha256"] != m["snapshot_sha256"]: fail("source and snapshot digests disagree")
    snapshot = Path(m["snapshot_path"])
    if not snapshot.is_file(): fail("snapshot is missing")
    if sha(snapshot) != m["snapshot_sha256"]: fail("snapshot hash mismatch")
    if snapshot.stem != m["snapshot_sha256"]: fail("snapshot filename/hash mismatch")
    _assert_manifest_location(manifest_path, snapshot)
    if manifest_path.name != f'{m["run_id"]}.manifest.json':
        fail("manifest filename must be <run_id>.manifest.json")
    slug = _manifest_chain_slug(manifest_path)
    if m["run_id"] != run_id(slug, m["config_ref"], m["source_sha256"]):
        fail("run_id does not match the sealed identity recomputed from slug, config_ref, and source_sha256")
    source = Path(m["source_path"]); source_present = source.exists()
    if source_present and sha(source) != m["source_sha256"]: fail("source hash mismatch")
    print(f"lab_config_manifest=valid source_present={'true' if source_present else 'false'}")

_ZIP_EXT = (".whl", ".zip")
_TAR_EXT = (".tar.gz", ".tgz", ".tar")

def _archive_members(path):
    p = Path(path); name = p.name.lower()
    if name.endswith(_ZIP_EXT):
        with zipfile.ZipFile(p) as zf:
            return [(info.filename, not info.filename.endswith("/")) for info in zf.infolist()]
    if any(name.endswith(ext) for ext in _TAR_EXT):
        with tarfile.open(p) as tf:
            return [(member.name, member.isfile() or member.islnk() or member.issym()) for member in tf.getmembers()]
    fail(f"unsupported archive format: {p}")

def _normalize_member_name(name):
    name = name.replace("\\", "/")
    if name.startswith("./"): name = name[2:]
    return name

def _root_present(members, root_rel):
    segs = [s for s in root_rel.split("/") if s]; n = len(segs); matches = []
    for name, is_file in members:
        if not is_file: continue
        parts = _normalize_member_name(name).split("/")
        for i in range(len(parts) - n + 1):
            if parts[i:i + n] == segs and i + n < len(parts):
                matches.append("/".join(parts)); break
    return matches

def cmd_package(a):
    root = Path(a.repo).resolve(strict=True)
    _, roots, _ = layout_spec(root)
    if a.archive:
        members = _archive_members(a.archive)
        matched, missing = {}, []
        for key, root_rel in roots.items():
            found = _root_present(members, root_rel)
            matched[key] = found[0] if found else None
            if not found: missing.append(root_rel)
        result = {
            "check": "archive", "verified": not missing, "missing": missing,
            "archive": str(Path(a.archive).resolve(strict=True)), "roots": roots, "matched": matched,
        }
    else:
        text = "\n".join(
            (root / n).read_text() for n in ("pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in")
            if (root / n).is_file()
        )
        missing = [
            name for name in roots.values()
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)
        ]
        result = {"check": "static-declaration", "declared": not missing, "missing": missing, "roots": roots}
    print(json.dumps(result, sort_keys=True))
    if missing: raise SystemExit(ERROR)

def main():
    p = argparse.ArgumentParser(); s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("resolve"); x.add_argument("--repo", required=True); x.add_argument("--ref", required=True); x.set_defaults(fn=cmd_resolve)
    x = s.add_parser("run-id"); x.add_argument("--slug", required=True); x.add_argument("--config-ref", required=True); x.add_argument("--config-sha256", required=True); x.add_argument("--attempt", type=int); x.set_defaults(fn=cmd_run_id)
    x = s.add_parser("seal"); x.add_argument("--repo", required=True); x.add_argument("--config", required=True); x.add_argument("--slug", required=True); x.add_argument("--run-id"); x.add_argument("--artifact-root", required=True); x.add_argument("--config-ref"); x.set_defaults(fn=cmd_seal)
    x = s.add_parser("verify"); x.add_argument("--manifest", required=True); x.set_defaults(fn=cmd_verify)
    x = s.add_parser("package-data"); x.add_argument("--repo", required=True); x.add_argument("--archive"); x.set_defaults(fn=cmd_package)
    a = p.parse_args()
    try: a.fn(a)
    except (KeyError, json.JSONDecodeError, OSError, ValueError) as e: fail(str(e))
if __name__ == "__main__": main()
