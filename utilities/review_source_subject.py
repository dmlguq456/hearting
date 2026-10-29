#!/usr/bin/env python3
"""Automatic content identity for implementation-review attempts.

The content digest describes the effective source tree. Attempt, route and
publication identity live in a separate immutable binding beside jobs.log.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import fnmatch
import re
import stat
import subprocess
import time

SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"att-[A-Za-z0-9._-]{1,240}\Z")
_EXCLUDED_PARTS = frozenset({
    ".git", ".agent_reports", ".claude_reports", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv", "dist",
    "build", "coverage", "target", ".next", "dev_logs", "test_logs", "evidence",
    "_internal", "shards", "analysis_project",
})
_ARTIFACT_ROOT_FILES = frozenset({
    "plan.md", "checklist.md", "pipeline_summary.md", "final_report.md",
})
_SEMANTIC_MARKDOWN_ROOTS = frozenset({"core", "capabilities", "roles", "adapters"})
_SOURCE_SCOPE_TOKENS = frozenset({"source/**", "source-alternative/**", "tests/**"})


class SourceSubjectError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason + (":" + detail if detail else ""))
        self.reason = reason
        self.detail = detail


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    env = dict(os.environ)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, check=False)
    if check and result.returncode:
        raise SourceSubjectError("review-source-git-unavailable",
                                 result.stderr.decode("utf-8", "replace")[-400:])
    return result.stdout


def is_implementation_review(route: dict, node: dict) -> bool:
    if node.get("kind") != "review-worker":
        return False
    derived_impl_review = (
        node.get("unit") == "qa/code-review"
        and (node.get("id") == "impl-review"
             or node.get("parallel_anchor") == "impl-review"
             or (isinstance(node.get("plan_stage"), dict)
                 and node["plan_stage"].get("template") == "impl-review"))
    )
    return (node.get("completion_gate") == "code-impl-review"
            or derived_impl_review
            or node.get("source_review_kind") == "implementation")


def is_source_test(node: dict) -> bool:
    """Whether this is the code-test gate, independent of its route-local id."""
    return node.get("completion_gate") == "code-test"


def is_deploy_review(route: dict, node: dict) -> bool:
    """A review the ship deploy node depends on: it judges what the deploy runs."""
    if node.get("kind") != "review-worker" or is_implementation_review(route, node):
        return False
    node_id = str(node.get("id"))
    return any(node_id in _ancestor_ids(route, item) for item in route.get("nodes", [])
               if isinstance(item, dict) and item.get("completion_gate") == "ship-deploy")


def _design_review(node: dict) -> bool:
    return (node.get("completion_gate") in {"design-verify", "design-critic"}
            or node.get("id") in {"visual-verify", "critic-review"})


def _review_input_binding(jobs: str | Path | None, metadata: dict | None) -> dict | None:
    if jobs is None or not isinstance(metadata, dict) or not metadata.get("review_input_digest"):
        return None
    try:
        from review_input import read_binding
        return read_binding(jobs, metadata)
    except Exception as exc:
        raise SourceSubjectError("review-input-binding-unreadable",
                                 str(metadata.get("attempt_id", ""))) from exc


def _subject_family(route: dict, node: dict, reviewed_input: dict | None = None) -> str | None:
    if is_implementation_review(route, node) or is_source_test(node):
        return "source"
    if is_deploy_review(route, node):
        return "source"  # Preserve the existing deploy-request digest contract.
    if _design_review(node):
        return "design-build"
    if isinstance(reviewed_input, dict) and isinstance(reviewed_input.get("sha256"), str):
        try:
            import review_input
            if review_input.is_plan_review_node(node):
                return "document:plan"
            if review_input.is_spec_review_node(node):
                return "document:spec"
        except ImportError:
            return None
    if (isinstance(reviewed_input, dict)
            and isinstance(reviewed_input.get("family"), str)
            and reviewed_input["family"].startswith("artifact-review:")):
        return reviewed_input["family"]
    return None


def _subject_digest(family: str, manifest: list[dict]) -> str:
    if not isinstance(family, str):
        return ""
    identity = {"schema_version": SCHEMA_VERSION, "manifest": manifest}
    if family not in {"source"}:
        identity["family"] = family
    return _sha(_canonical(identity))


def _document_primary_subject(route: dict, node: dict, reviewed_input: dict) -> tuple[str, list[dict], str]:
    family = _subject_family(route, node, reviewed_input) or "document:plan"
    manifest = [{"path": "reviewed-input", "kind": "file",
                 "content_sha256": str(reviewed_input.get("sha256", ""))}]
    return family, manifest, _subject_digest(family, manifest)


def _input_match(path: str, scopes: tuple[str, ...]) -> bool:
    import fnmatch
    for scope in scopes:
        pattern = scope.rstrip("/")
        if fnmatch.fnmatchcase(path, scope) or path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def deploy_request_subject(route: dict, node: dict, jobs: str | Path) -> dict | None:
    """The parent deploy request's hashed inputs inside this review's input scope.

    None while the route has no request yet or the request lists none of the
    review's inputs; that review keeps the plain per-node count.
    """
    request = Path(jobs).resolve().parent / "ship-parent-deploy" / f"{route.get('route_id')}.json"
    try:
        value = json.loads(request.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SourceSubjectError("review-deploy-request-unreadable", str(request)) from exc
    if (not isinstance(value, dict) or value.get("route_id") != route.get("route_id")
            or value.get("route_hash") != route.get("route_hash")):
        return None
    record = value.get("deploy_record")
    inputs = (value.get("command") or {}).get("inputs") if isinstance(value.get("command"), dict) else None
    if not isinstance(record, str) or not isinstance(inputs, dict):
        return None
    artifacts = Path(record).parents[1]  # <cycle>/artifacts/release-config/deploy-record.json
    scopes = tuple(scope for scope in node.get("inputs") or () if isinstance(scope, str) and scope)
    manifest = []
    for name, sha in sorted(inputs.items()):
        try:
            relative = Path(name).relative_to(artifacts).as_posix()
        except ValueError:
            continue
        if _input_match(relative, scopes):
            manifest.append([relative, str(sha)])
    if not manifest:
        return None
    return {"schema_version": SCHEMA_VERSION, "family": "source",
            "digest": _sha(_canonical({"schema_version": SCHEMA_VERSION, "manifest": manifest})),
            "manifest": manifest, "scope": list(scopes),
            "provenance": {"source": "parent-deploy-request", "request_digest": value.get("request_digest")}}


def has_review_subject(route: dict, node: dict, jobs: str | Path | None = None,
                       reviewed_input: dict | None = None) -> bool:
    """Whether launch, round admission, or completion binds to review content."""
    if is_implementation_review(route, node) or is_source_test(node):
        return True
    if is_deploy_review(route, node):
        return jobs is not None and deploy_request_subject(route, node, jobs) is not None
    if _design_review(node):
        try:
            _design_build_node(route, node)
        except SourceSubjectError:
            return False  # No sealed build means no content subject to infer.
    try:
        import review_input
        if review_input.is_review_node(node) and reviewed_input is None:
            return False
    except ImportError:
        pass
    if _subject_family(route, node, reviewed_input) is not None:
        return True
    return _generic_artifact_review_subject(route, node) is not None


def _ancestor_ids(route: dict, node: dict) -> list[str]:
    by_id = {str(item.get("id")): item for item in route.get("nodes", []) if isinstance(item, dict)}
    seen: set[str] = set()
    ordered: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        current = by_id.get(node_id)
        if current is None:
            return
        for parent in current.get("depends_on", ()) or ():
            visit(str(parent))
        if node_id != str(node.get("id")):
            ordered.append(node_id)

    visit(str(node.get("id")))
    return ordered


def source_scopes(route: dict, node: dict) -> tuple[str, ...]:
    """Resolve the source-producing ancestor scopes sealed by the route."""
    by_id = {str(item.get("id")): item for item in route.get("nodes", []) if isinstance(item, dict)}
    ancestors = _ancestor_ids(route, node)
    candidates = [by_id[item] for item in ancestors if item in by_id]
    candidates.append(node)
    if is_implementation_review(route, node):
        candidates.extend(item for item in route.get("nodes", [])
                          if item.get("completion_gate") == "code-execute" or item.get("id") == "execute")
    scopes = sorted({str(scope) for item in candidates for scope in (item.get("write_scope") or ())
                     if isinstance(scope, str) and scope
                     and str(scope) not in _ARTIFACT_ROOT_FILES
                     and PurePosixPath(str(scope).split("*", 1)[0].rstrip("/")).parts[:1]
                     not in (("dev_logs",), ("test_logs",), ("evidence",), ("_internal",), ("shards",))})
    if (is_implementation_review(route, node) or is_source_test(node)) and not scopes:
        # Historical implementation-review gates predate explicit source scopes,
        # and a continuation can resume at test without the execute ancestor.
        return ("source/**",)
    return tuple(scopes)


def has_source_subject(route: dict, node: dict) -> bool:
    if is_implementation_review(route, node):
        return True
    if (node.get("completion_gate") in {"code-execute", "code-test"}
            or node.get("id") == "execute"):
        return True
    return any(scope in _SOURCE_SCOPE_TOKENS
               for scope in node.get("write_scope") or () if isinstance(scope, str))


def _explicit_match(path: str, scopes: tuple[str, ...]) -> bool:
    import fnmatch
    for scope in scopes:
        if scope in _SOURCE_SCOPE_TOKENS:
            return True
        pattern = scope.rstrip("/")
        if fnmatch.fnmatchcase(path, pattern) or path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def _excluded_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in _EXCLUDED_PARTS for part in parts)


def _pure_document(path: str, scopes: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    if name in _ARTIFACT_ROOT_FILES:
        return True
    if _explicit_match(path, tuple(scope for scope in scopes if scope not in _SOURCE_SCOPE_TOKENS)):
        return False
    if parts and parts[0] in _SEMANTIC_MARKDOWN_ROOTS:
        return False
    if len(parts) >= 2 and parts[0] in {"doc", "docs"}:
        return True
    if parts and parts[0] not in _SEMANTIC_MARKDOWN_ROOTS and name.endswith((".md", ".rst")):
        return True
    if name.startswith(("readme", "changelog", "changes")) and name.endswith(".txt"):
        return True
    return False


def _candidate(path: str, scopes: tuple[str, ...]) -> bool:
    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        return False
    if _excluded_path(path) or _pure_document(path, scopes):
        return False
    return _explicit_match(path, scopes) or any(scope in _SOURCE_SCOPE_TOKENS for scope in scopes)


def _index_entries(root: Path) -> tuple[dict[str, tuple[int, str]], set[str]]:
    raw = _git(root, "ls-files", "--stage", "-z")
    staged: dict[str, tuple[int, str]] = {}
    tracked: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        head, sep, raw_path = record.partition(b"\t")
        if not sep:
            raise SourceSubjectError("review-source-index-invalid")
        fields = head.split()
        if len(fields) != 3:
            raise SourceSubjectError("review-source-index-invalid")
        if fields[2] != b"0":
            raise SourceSubjectError("review-source-index-unmerged", os.fsdecode(raw_path))
        path = os.fsdecode(raw_path)
        mode = int(fields[0], 8)
        oid = fields[1].decode("ascii", "strict")
        staged[path] = (mode, oid)
        tracked.add(path)
    untracked = {os.fsdecode(item) for item in _git(
        root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if item}
    return staged, tracked | untracked


def _index_tree_digest(root: Path, staged: dict[str, tuple[int, str]]) -> str:
    """Hash the index tree without `git write-tree` or an index/object write."""
    object_format = _git(root, "rev-parse", "--show-object-format").decode("ascii", "strict").strip()
    if object_format not in {"sha1", "sha256"}:
        raise SourceSubjectError("review-source-index-object-format-unsupported", object_format)
    hash_size = 20 if object_format == "sha1" else 32
    tree: dict = {}
    for relative, (mode, oid) in staged.items():
        parts = PurePosixPath(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise SourceSubjectError("review-source-index-path-invalid", relative)
        branch = tree
        for part in parts[:-1]:
            existing = branch.setdefault(part, {})
            if not isinstance(existing, dict):
                raise SourceSubjectError("review-source-index-path-conflict", relative)
            branch = existing
        if parts[-1] in branch:
            raise SourceSubjectError("review-source-index-path-conflict", relative)
        try:
            object_id = bytes.fromhex(oid)
        except ValueError as exc:
            raise SourceSubjectError("review-source-index-oid-invalid", relative) from exc
        if len(object_id) != hash_size:
            raise SourceSubjectError("review-source-index-oid-invalid", relative)
        branch[parts[-1]] = (mode, object_id)

    def encode_tree(entries: dict) -> bytes:
        ordered = []
        for name, value in entries.items():
            is_directory = isinstance(value, dict)
            if is_directory:
                mode, object_id = 0o40000, bytes.fromhex(encode_tree(value).decode("ascii"))
            else:
                mode, object_id = value
            raw_name = os.fsencode(name)
            ordered.append((raw_name + (b"/" if is_directory else b"\0"),
                            raw_name, mode, object_id))
        body = b"".join(
            format(mode, "o").encode("ascii") + b" " + name + b"\0" + object_id
            for _key, name, mode, object_id in sorted(ordered, key=lambda item: item[0])
        )
        return hashlib.new(object_format,
            b"tree " + str(len(body)).encode("ascii") + b"\0" + body).hexdigest().encode("ascii")

    return encode_tree(tree).decode("ascii")


def _safe_checkout_path(root: Path, relative: str) -> Path:
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise SourceSubjectError("review-source-parent-symlink", relative)
        except FileNotFoundError:
            return path
    return path


def _stat_key(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(path: Path) -> tuple[bytes, tuple[int, ...]]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SourceSubjectError("review-source-file-kind-changed", str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _stat_key(before) != _stat_key(opened):
            raise SourceSubjectError("review-source-file-changed", str(path))
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after_fd = os.fstat(fd)
    finally:
        os.close(fd)
    after = path.lstat()
    if _stat_key(opened) != _stat_key(after_fd) or _stat_key(after_fd) != _stat_key(after):
        raise SourceSubjectError("review-source-file-changed", str(path))
    return b"".join(chunks), _stat_key(after)


def _snapshot_once(root: Path, scopes: tuple[str, ...], *, depth: int = 0,
                   _snapshots: list | None = None) -> tuple[list[dict], dict]:
    if depth > 8:
        raise SourceSubjectError("review-source-submodule-depth")
    snapshots = [] if _snapshots is None else _snapshots
    try:
        head_before = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        staged, paths = _index_entries(root)
        index_before = _index_tree_digest(root, staged)
    except SourceSubjectError:
        raise
    source_paths = {relative for relative in paths if _candidate(relative, scopes)}
    manifest: list[dict] = []
    observations: list[tuple[Path, tuple[int, ...] | None]] = []
    for relative in sorted(source_paths, key=os.fsencode):
        path = _safe_checkout_path(root, relative)
        index_mode, index_oid = staged.get(relative, (0, ""))
        try:
            info = path.lstat()
        except FileNotFoundError:
            manifest.append({"path": relative, "kind": "deleted", "mode": index_mode,
                             "content_sha256": None})
            observations.append((path, None))
            continue
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target_before = os.readlink(path)
            after = path.lstat()
            if _stat_key(info) != _stat_key(after) or target_before != os.readlink(path):
                raise SourceSubjectError("review-source-file-changed", relative)
            data = os.fsencode(target_before)
            manifest.append({"path": relative, "kind": "symlink", "mode": mode,
                             "content_sha256": _sha(data)})
            observations.append((path, _stat_key(after)))
        elif index_mode == 0o160000 or stat.S_ISDIR(info.st_mode):
            if not stat.S_ISDIR(info.st_mode) or not (path / ".git").exists():
                raise SourceSubjectError("review-source-submodule-unreadable", relative)
            nested, provenance = _snapshot_once(
                path, ("source/**",), depth=depth + 1, _snapshots=snapshots)
            nested_digest = _sha(_canonical(nested))
            manifest.append({"path": relative, "kind": "gitlink", "mode": mode,
                             "gitlink_oid": index_oid, "content_sha256": nested_digest})
            observations.append((path, _stat_key(info)))
        elif stat.S_ISREG(info.st_mode):
            data, observed = _read_regular(path)
            manifest.append({"path": relative, "kind": "file", "mode": mode,
                             "content_sha256": _sha(data)})
            observations.append((path, observed))
        else:
            raise SourceSubjectError("review-source-file-kind-unsupported", relative)
    snapshots.append((root, scopes, head_before, index_before, source_paths, observations))
    provenance = {"head": head_before, "index_tree": index_before,
                  "source_paths_sha256": _sha(_canonical(sorted(source_paths, key=os.fsencode)))}
    if _snapshots is not None:
        return manifest, provenance
    # Fence every repository after the entire tree is captured. A parent
    # directory stat cannot detect edits to an existing submodule file.
    # Reuse the inventories instead of adding global status walks that also
    # visit unrelated artifacts outside the source subject.
    for repo, repo_scopes, head, index_tree, expected_paths, _observations in snapshots:
        head_after = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
        staged_after, paths_after = _index_entries(repo)
        index_after = _index_tree_digest(repo, staged_after)
        source_paths_after = {relative for relative in paths_after if _candidate(relative, repo_scopes)}
        if (head, index_tree) != (head_after, index_after) or expected_paths != source_paths_after:
            raise SourceSubjectError("review-source-snapshot-raced")
    # These checks follow all final inventories, including submodules, so
    # source edits made during those reads are detected as well.
    for _repo, _scopes, _head, _index_tree, _paths, observed_files in snapshots:
        for path, expected in observed_files:
            try:
                actual = _stat_key(path.lstat())
            except FileNotFoundError:
                actual = None
            if actual != expected:
                raise SourceSubjectError("review-source-file-changed", str(path))
    return manifest, provenance


def _read_absolute_regular(path: Path) -> tuple[bytes, tuple[int, ...]]:
    absolute = Path(path).absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise SourceSubjectError("review-artifact-input-unavailable", str(path)) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SourceSubjectError("review-artifact-input-kind-invalid", str(path))
    try:
        return _read_regular(absolute)
    except (OSError, SourceSubjectError) as exc:
        if isinstance(exc, SourceSubjectError):
            raise
        raise SourceSubjectError("review-artifact-input-unavailable", str(path)) from exc


def _document_review_subject(route: dict, node: dict, reviewed_input: dict) -> dict:
    path = reviewed_input.get("path")
    expected_sha = reviewed_input.get("sha256")
    if not isinstance(path, str) or not Path(path).is_absolute() or not re.fullmatch(
            r"[0-9a-f]{64}", str(expected_sha or "")):
        raise SourceSubjectError("review-input-binding-invalid", str(node.get("id", "")))
    primary_path = Path(path)
    try:
        primary_bytes, _primary_identity = _read_absolute_regular(primary_path)
        if _sha(primary_bytes) != expected_sha:
            raise SourceSubjectError("review-input-content-changed", str(primary_path))
    except (OSError, SourceSubjectError) as exc:
        if isinstance(exc, SourceSubjectError):
            raise
        raise SourceSubjectError("review-input-unavailable", str(primary_path)) from exc
    family = _subject_family(route, node, reviewed_input) or "document:plan"
    _family, manifest, _primary_digest = _document_primary_subject(route, node, reviewed_input)
    extra = None
    if family == "document:plan":
        producer = next((item for item in route.get("nodes", [])
                         if isinstance(item, dict) and item.get("id") == "plan"), None)
        declares_checklist = bool(producer and any(
            isinstance(scope, str) and (scope == "checklist.md"
                or scope.endswith("/checklist.md") or "checklist.md" in scope)
            for scope in producer.get("write_scope", ())))
        if declares_checklist:
            candidate = primary_path.parent / "checklist.md"
            try:
                candidate.lstat()
            except FileNotFoundError:
                data = None
            except OSError as exc:
                raise SourceSubjectError("review-artifact-input-unavailable", str(candidate)) from exc
            else:
                data, _identity = _read_absolute_regular(candidate)
            if data is not None:
                extra = {"path": "checklist.md", "kind": "file", "content_sha256": _sha(data)}
                manifest.append(extra)
    manifest.sort(key=lambda item: item["path"].encode("utf-8"))
    return {"schema_version": SCHEMA_VERSION, "family": family,
            "digest": _subject_digest(family, manifest), "manifest": manifest,
            "scope": ["reviewed-input", *( ["checklist.md"] if extra else [])],
            "provenance": {"input_producer": reviewed_input.get("producer")}}


def _design_build_node(route: dict, node: dict) -> dict:
    ancestors = set(_ancestor_ids(route, node))
    build = next((item for item in route.get("nodes", [])
                  if isinstance(item, dict) and item.get("id") in ancestors
                  and item.get("completion_gate") == "design-build"), None)
    if build is None:
        raise SourceSubjectError("review-design-build-unavailable", str(node.get("id", "")))
    return build


def _design_review_subject(route: dict, node: dict) -> dict:
    build = _design_build_node(route, node)
    artifact_root = Path(str(route.get("artifact_root") or ""))
    try:
        import artifact_producer
        cycle = artifact_producer.route_cycle_for(artifact_root, route)
        if cycle is None:
            raise SourceSubjectError("review-design-cycle-unavailable", str(node.get("id", "")))
        cycle_dir = artifact_producer.cycle_dir(
            artifact_root, str(cycle["campaign_id"]), str(cycle["cycle_id"]), cycle)
    except SourceSubjectError:
        raise
    except Exception as exc:
        raise SourceSubjectError("review-design-cycle-unavailable", str(node.get("id", ""))) from exc
    output_candidate = cycle_dir / "artifacts"
    try:
        output_info = output_candidate.lstat()
    except OSError as exc:
        raise SourceSubjectError("review-design-output-unavailable", str(output_candidate)) from exc
    if stat.S_ISLNK(output_info.st_mode) or not stat.S_ISDIR(output_info.st_mode):
        raise SourceSubjectError("review-design-output-kind-invalid", str(output_candidate))
    output_root = output_candidate.resolve(strict=True)
    allowed_prefixes = ("designs/02_tokens", "designs/03_components",
                        "designs/<cycle>/02_tokens", "designs/<cycle>/03_components",
                        "spec/design")
    scopes = []
    for scope in build.get("write_scope", ()):
        if not isinstance(scope, str):
            continue
        prefix = scope[:-3] if scope.endswith("/**") else scope
        if prefix in allowed_prefixes or any(prefix.startswith(item + "/") for item in allowed_prefixes):
            scopes.append(scope)
    if not scopes:
        raise SourceSubjectError("review-design-scope-unavailable", str(build.get("id", "")))
    files: dict[str, dict] = {}
    directories: dict[Path, tuple[int, ...]] = {}

    def visit(path: Path, relative: str) -> None:
        try:
            before = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SourceSubjectError("review-design-input-unavailable", relative) from exc
        if stat.S_ISLNK(before.st_mode):
            raise SourceSubjectError("review-design-input-symlink", relative)
        if stat.S_ISDIR(before.st_mode):
            if any(part in {"_internal", "04_review", "05_handoff"}
                   for part in PurePosixPath(relative).parts):
                return
            directories[path] = _stat_key(before)
            try:
                with os.scandir(path) as entries:
                    children = sorted(entries, key=lambda entry: os.fsencode(entry.name))
            except OSError as exc:
                raise SourceSubjectError("review-design-input-unavailable", relative) from exc
            for entry in children:
                child_relative = relative + "/" + entry.name if relative else entry.name
                visit(path / entry.name, child_relative)
            return
        if not stat.S_ISREG(before.st_mode):
            raise SourceSubjectError("review-design-input-kind-invalid", relative)
        data, identity = _read_regular(path)
        files[relative] = {"path": relative, "kind": "file",
                           "mode": stat.S_IMODE(identity[2]),
                           "content_sha256": _sha(data)}

    def visit_scope(relative: str) -> None:
        parts = PurePosixPath(relative).parts
        current = output_root
        for part in parts[:-1]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise SourceSubjectError("review-design-input-unavailable", relative) from exc
            if stat.S_ISLNK(info.st_mode):
                raise SourceSubjectError("review-design-input-symlink", relative)
            if not stat.S_ISDIR(info.st_mode):
                return
        visit(output_root / relative, relative)

    for scope in scopes:
        if scope.endswith("/**"):
            relative = scope[:-3].rstrip("/")
            visit_scope(relative)
        elif "*" not in scope and "?" not in scope:
            visit_scope(scope)
        else:
            raise SourceSubjectError("review-design-scope-invalid", scope)
    for directory, expected in directories.items():
        try:
            actual = _stat_key(directory.lstat())
        except OSError as exc:
            raise SourceSubjectError("review-design-input-changed", str(directory)) from exc
        if actual != expected:
            raise SourceSubjectError("review-design-input-changed", str(directory))
    manifest = [files[key] for key in sorted(files, key=os.fsencode)]
    if not manifest:
        raise SourceSubjectError("review-design-payload-empty", str(build.get("id", "")))
    return {"schema_version": SCHEMA_VERSION, "family": "design-build",
            "digest": _subject_digest("design-build", manifest), "manifest": manifest,
            "scope": scopes, "provenance": {"build_node": build.get("id"),
                "cycle_id": cycle.get("cycle_id"), "route_id": cycle.get("route_id")}}


def _generic_artifact_review_subject(route: dict, node: dict) -> dict | None:
    """Observe only concrete, sealed artifact inputs a capped review consumes."""
    if node.get("kind") != "review-worker":
        return None
    import review_round_cap
    if not review_round_cap.is_round_capped_node(node):
        return None
    try:
        import review_input
        if review_input.is_review_node(node):
            return None
    except ImportError:
        pass
    sources = node.get("input_sources") if isinstance(node.get("input_sources"), dict) else {}
    selectors = [value for value in (node.get("inputs") or ())
                 if isinstance(value, str) and value and "<" not in value and ">" not in value
                 and ("/" in value or PurePosixPath(value).suffix)
                 and value not in {"logs/**", "checkpoints/**", "raw-results/**"}]
    if not sources and not selectors:
        return None
    ancestors = _ancestor_ids(route, node)
    by_id = {str(item.get("id")): item for item in route.get("nodes", [])
             if isinstance(item, dict)}
    producer_nodes = [by_id[item] for item in ancestors if item in by_id]
    producer_scopes = [scope for producer in producer_nodes
                       for scope in (producer.get("write_scope") or ())
                       if isinstance(scope, str)]
    artifact_root = Path(str(route.get("artifact_root") or ""))
    output_root = None
    cycle_id = None
    import artifact_producer
    try:
        cycle = artifact_producer.route_cycle_for(artifact_root, route)
        if cycle is not None:
            cycle_id = cycle.get("cycle_id")
            cycle_dir = artifact_producer.cycle_dir(
                artifact_root, str(cycle["campaign_id"]), str(cycle["cycle_id"]), cycle)
            output_root = cycle_dir / "artifacts"
            info = output_root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SourceSubjectError("review-artifact-output-kind-invalid", str(output_root))
    except SourceSubjectError:
        raise
    except artifact_producer.ProducerError as exc:
        raise SourceSubjectError("review-artifact-cycle-unavailable", exc.code) from exc
    except (OSError, ValueError, KeyError, TypeError):
        output_root = None

    family_anchor = (node.get("parallel_anchor") or node.get("completion_gate")
                     or node.get("id"))
    family = "artifact-review:" + str(family_anchor)
    files: dict[str, dict] = {}
    directories: dict[Path, tuple[int, ...]] = {}
    observed_inputs = []
    missing_exact = []
    missing_globs = []

    def visit(path: Path, relative: str, logical_prefix: str) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SourceSubjectError("review-artifact-input-unavailable", relative) from exc
        if stat.S_ISLNK(info.st_mode):
            raise SourceSubjectError("review-artifact-input-symlink", relative)
        if stat.S_ISDIR(info.st_mode):
            if any(part in {"checkpoints", "raw-results", "logs", "models", "__pycache__"}
                   for part in PurePosixPath(relative).parts):
                return
            directories[path] = _stat_key(info)
            try:
                with os.scandir(path) as entries:
                    children = sorted(entries, key=lambda entry: os.fsencode(entry.name))
            except OSError as exc:
                raise SourceSubjectError("review-artifact-input-unavailable", relative) from exc
            for entry in children:
                child_relative = relative + "/" + entry.name if relative else entry.name
                visit(path / entry.name, child_relative, logical_prefix)
            return
        if not stat.S_ISREG(info.st_mode):
            raise SourceSubjectError("review-artifact-input-kind-invalid", relative)
        data, identity = _read_absolute_regular(path)
        suffix = (relative[len(logical_prefix):].lstrip("/")
                  if relative.startswith(logical_prefix) else relative)
        logical = logical_prefix + ("/" + suffix if suffix else "")
        files[logical] = {"path": logical, "kind": "file",
                          "mode": stat.S_IMODE(identity[2]),
                          "content_sha256": _sha(data)}

    # A sealed input source is exact authority even when its producer belonged
    # to an earlier cycle. The mapping key is its logical input name; physical
    # placement and producer publication identity stay out of the digest.
    sources = node.get("input_sources") if isinstance(node.get("input_sources"), dict) else {}
    for name, source in sorted(sources.items()):
        if not isinstance(name, str) or not isinstance(source, dict):
            continue
        source_path = source.get("path")
        if not isinstance(source_path, str) or not source_path:
            continue
        path = Path(source_path)
        if not path.is_absolute():
            path = artifact_root / path
        try:
            path.resolve(strict=True).relative_to(artifact_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise SourceSubjectError("review-artifact-input-outside-root", name) from exc
        data, identity = _read_absolute_regular(path)
        logical = "sealed-input/" + name
        files[logical] = {"path": logical, "kind": "file",
                          "mode": stat.S_IMODE(identity[2]), "content_sha256": _sha(data)}
        observed_inputs.append(name)

    def normalized_scope(scope: str) -> str:
        return scope[len("artifacts/"):] if scope.startswith("artifacts/") else scope

    def matches_scope(selector: str, scope: str) -> bool:
        selector_glob = selector.endswith("/**")
        scope_glob = scope.endswith("/**")
        selector_base = selector[:-3].rstrip("/") if selector_glob else selector
        scope_base = scope[:-3].rstrip("/") if scope_glob else scope
        if selector_glob:
            return (scope_base == selector_base or scope_base.startswith(selector_base + "/")
                    or selector_base.startswith(scope_base + "/"))
        return (scope_base == selector_base or (scope_glob and selector_base.startswith(scope_base + "/")))

    for selector in node.get("inputs") or ():
        if not isinstance(selector, str) or not selector or "<" in selector or ">" in selector:
            continue  # unresolved symbols/resources keep their existing identity contract
        if selector.startswith("/") or ".." in PurePosixPath(selector).parts or "\\" in selector:
            raise SourceSubjectError("review-artifact-input-path-invalid", selector)
        if selector in {"logs/**", "checkpoints/**", "raw-results/**", "experiment-artifact"}:
            continue
        if not ("/" in selector or PurePosixPath(selector).suffix):
            continue
        matching_scopes = [normalized_scope(scope) for scope in producer_scopes
                           if matches_scope(selector, normalized_scope(scope))]
        if not matching_scopes:
            continue
        selector_glob = selector.endswith("/**")
        selector_base = selector[:-3].rstrip("/") if selector_glob else selector
        candidates = [output_root / selector_base] if output_root is not None else []
        # Root-anchored producer scopes remain valid only at their declared
        # path under this artifact root; cycle output still takes precedence.
        candidates.append(artifact_root / selector_base)
        target = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        if (len(candidates) > 1 and target == candidates[-1]
                and not any(scope == selector or scope.startswith(selector_base + "/")
                            for scope in matching_scopes)):
            continue
        before = len(files)
        visit(target, selector_base, selector_base)
        if len(files) > before:
            observed_inputs.append(selector)
        elif selector_glob:
            missing_globs.append(selector)
        else:
            missing_exact.append(selector)

    for directory, expected in directories.items():
        try:
            actual = _stat_key(directory.lstat())
        except OSError as exc:
            raise SourceSubjectError("review-artifact-input-changed", str(directory)) from exc
        if actual != expected:
            raise SourceSubjectError("review-artifact-input-changed", str(directory))
    # This resolver supplies content identity when a sealed file is present;
    # it is not a second input gate. The stage's existing input contract owns
    # missing-file refusals. An incomplete manifest therefore stays unbound.
    if missing_exact or missing_globs:
        return None
    manifest = [files[key] for key in sorted(files, key=os.fsencode)]
    if not manifest:
        if missing_globs:
            raise SourceSubjectError("review-artifact-input-unavailable", missing_globs[0])
        return None
    return {"schema_version": SCHEMA_VERSION, "family": family,
            "digest": _subject_digest(family, manifest), "manifest": manifest,
            "scope": sorted(set(observed_inputs)),
            "provenance": {"producer_nodes": [str(item.get("id")) for item in producer_nodes],
                           "cycle_id": cycle_id}}


def observe(route: dict, node: dict, *, worktree: str | Path | None = None,
            jobs: str | Path | None = None, reviewed_input: dict | None = None) -> dict:
    """Return a stable review-content manifest and content-only digest."""
    family = _subject_family(route, node, reviewed_input)
    if family == "design-build":
        return _design_review_subject(route, node)
    if family and family.startswith("document:"):
        return _document_review_subject(route, node, reviewed_input)
    if is_deploy_review(route, node):
        if jobs is None:
            raise SourceSubjectError("review-deploy-subject-unavailable", str(node.get("id", "")))
        subject = deploy_request_subject(route, node, jobs)
        if subject is None:
            raise SourceSubjectError("review-deploy-subject-unavailable", str(node.get("id", "")))
        return subject
    if family and family.startswith("artifact-review:") and isinstance(reviewed_input, dict):
        return reviewed_input
    if family is None and node.get("kind") == "review-worker":
        generic = _generic_artifact_review_subject(route, node)
        if generic is not None:
            return generic
    root = Path(worktree or route.get("cwd", "")).resolve()
    if not root.is_dir():
        raise SourceSubjectError("review-source-worktree-unavailable", str(root))
    scopes = source_scopes(route, node)
    if not scopes:
        raise SourceSubjectError("review-source-scope-unavailable", str(node.get("id", "")))
    last_error: SourceSubjectError | None = None
    for attempt in range(3):
        try:
            manifest, provenance = _snapshot_once(root, scopes)
            digest = _sha(_canonical({"schema_version": SCHEMA_VERSION, "manifest": manifest}))
            return {"schema_version": SCHEMA_VERSION, "family": "source",
                    "digest": digest, "manifest": manifest,
                    "scope": list(scopes), "provenance": provenance}
        except SourceSubjectError as exc:
            last_error = exc
            if exc.reason not in {"review-source-file-changed", "review-source-snapshot-raced"}:
                raise
            if attempt < 2:
                time.sleep(0.005)
    raise last_error or SourceSubjectError("review-source-snapshot-raced")


def _subject_directory(jobs: str | Path) -> Path:
    return Path(jobs).resolve().parent / "review-subjects"


def binding_path(jobs: str | Path, attempt_id: str) -> Path:
    if not _SAFE_ID.fullmatch(str(attempt_id)):
        raise SourceSubjectError("review-source-attempt-id-invalid", str(attempt_id))
    return _subject_directory(jobs) / f"{attempt_id}.json"


def _binding_digest(record: dict) -> str:
    return _sha(_canonical({k: v for k, v in record.items() if k != "binding_digest"}))


def read_binding_record(jobs: str | Path, attempt_id: str, *, route_id=None,
                        route_hash=None, node_id=None) -> dict | None:
    """Read a sidecar for exact replacement lineage without inferring legacy rows."""
    path = binding_path(jobs, attempt_id)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceSubjectError("review-source-binding-unreadable", attempt_id) from exc
    if not isinstance(record, dict):
        raise SourceSubjectError("review-source-binding-unreadable", attempt_id)
    family = record.get("family", "source")
    manifest_digest = _subject_digest(family, record.get("manifest"))
    if (record.get("schema_version") != SCHEMA_VERSION
            or ("family" in record and not isinstance(family, str))
            or record.get("attempt_id") != attempt_id
            or (route_id is not None and record.get("route_id") != route_id)
            or (route_hash is not None and record.get("route_hash") != route_hash)
            or (node_id is not None and record.get("node_id") != node_id)
            or record.get("binding_digest") != _binding_digest(record)
            or record.get("subject_digest") != manifest_digest):
        raise SourceSubjectError("review-source-binding-identity-mismatch", attempt_id)
    return record


def _write_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical(payload) + b"\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceSubjectError("review-source-binding-unreadable", str(path)) from exc
        if existing != payload:
            raise SourceSubjectError("review-source-binding-conflict", str(path))
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def seal_attempt(jobs: str | Path, route: dict, node: dict, attempt_id: str, *,
                 worktree: str | Path | None = None, predecessor_attempt_id: str | None = None,
                 expected_subject_digest: str | None = None,
                 attempt_metadata: dict | None = None) -> dict:
    """Bind a new subject-bound review attempt, inheriting exact retry lineage."""
    reviewed_input = _review_input_binding(jobs, attempt_metadata)
    if not has_review_subject(route, node, jobs, reviewed_input):
        return {}
    path = binding_path(jobs, attempt_id)
    if predecessor_attempt_id:
        predecessor = read_binding_record(jobs, predecessor_attempt_id,
            route_id=route["route_id"], route_hash=route["route_hash"], node_id=node["id"])
        if predecessor is None:
            return {}  # A legacy unknown attempt stays unknown through replacement.
        current = observe(route, node, worktree=worktree, jobs=jobs, reviewed_input=reviewed_input)
        if expected_subject_digest and current["digest"] != expected_subject_digest:
            raise SourceSubjectError("review-source-admission-subject-changed", attempt_id)
        expected_epoch = source_epoch(route, node, current["digest"],
                                      Path(jobs).resolve().parent / "completion" / route["route_id"],
                                      family=current.get("family", "source"))
        if (current["digest"] != predecessor["subject_digest"]
                or current.get("family", "source") != predecessor.get("family", "source")
                or expected_epoch != predecessor["source_epoch"]):
            raise SourceSubjectError("review-source-replacement-subject-changed", predecessor_attempt_id)
        record = {k: v for k, v in predecessor.items() if k not in {"attempt_id", "binding_digest"}}
        record.update(attempt_id=attempt_id, predecessor_attempt_id=predecessor_attempt_id)
    else:
        current = observe(route, node, worktree=worktree, jobs=jobs, reviewed_input=reviewed_input)
        if expected_subject_digest and current["digest"] != expected_subject_digest:
            raise SourceSubjectError("review-source-admission-subject-changed", attempt_id)
        completion_dir = Path(jobs).resolve().parent / "completion" / route["route_id"]
        record = {
            "schema_version": SCHEMA_VERSION,
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "node_id": node["id"], "attempt_id": attempt_id,
            "subject_digest": current["digest"],
            "family": current.get("family", "source"),
            "source_epoch": source_epoch(route, node, current["digest"], completion_dir,
                                          family=current.get("family", "source")),
            "scope": current["scope"], "manifest": current["manifest"],
            "provenance": current["provenance"],
        }
    record["binding_digest"] = _binding_digest(record)
    _write_once(path, record)
    return {"review_subject_digest": record["subject_digest"],
            "review_subject_binding_digest": record["binding_digest"]}


def read_binding(jobs: str | Path, metadata: dict, *, required: bool = True) -> dict | None:
    """Read and validate the exact write-once attempt binding; legacy absence is unknown."""
    attempt_id = str(metadata.get("attempt_id", ""))
    digest = metadata.get("review_subject_digest")
    binding_digest = metadata.get("review_subject_binding_digest")
    if not digest and not binding_digest:
        if required:
            raise SourceSubjectError("review-source-binding-missing", attempt_id)
        return None
    if not digest or not binding_digest:
        raise SourceSubjectError("review-source-binding-incomplete", attempt_id)
    record = read_binding_record(jobs, attempt_id, route_id=metadata.get("route_id"),
        route_hash=metadata.get("route_hash"), node_id=metadata.get("route_node"))
    if (record is None or record.get("subject_digest") != digest
            or record.get("binding_digest") != binding_digest):
        raise SourceSubjectError("review-source-binding-identity-mismatch", attempt_id)
    return record


def annotate_rows(jobs: str | Path, rows, *, route: dict | None = None,
                  node: dict | None = None, family: str | None = None):
    """Add verified subject labels to an in-memory round census only."""
    result = []
    for status, metadata in rows:
        binding = read_binding(jobs, metadata, required=False)
        digest = binding["subject_digest"] if binding is not None else None
        if digest is None and family and family.startswith("document:") and route and node:
            reviewed_input = _review_input_binding(jobs, metadata)
            if reviewed_input is not None and _subject_family(route, node, reviewed_input) == family:
                _old_family, _manifest, digest = _document_primary_subject(route, node, reviewed_input)
        result.append((status, metadata if digest is None else {
            **metadata, "_review_subject_digest": digest
        }))
    return result


def validate_attempt(jobs: str | Path, fields: list[str], metadata: dict, *,
                     require_new: bool = False) -> dict | None:
    """Registration/launch fence: exact binding integrity and effective bytes."""
    likely_review = (metadata.get("completion_gate") == "code-impl-review"
                     or metadata.get("completion_gate") == "code-test"
                     or metadata.get("unit") == "qa/code-review"
                     or bool(metadata.get("review_subject_digest")))
    if not likely_review:
        return None
    if (not metadata.get("review_subject_digest")
            and not metadata.get("review_subject_binding_digest")
            and not metadata.get("route_file") and not require_new):
        return None  # A legacy unknown row is never inferred from HEAD or route position.
    route_file = Path(str(metadata.get("route_file", "")))
    if not route_file.is_absolute() or not route_file.is_file():
        raise SourceSubjectError("review-source-route-unavailable", str(route_file))
    try:
        route = json.loads(route_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceSubjectError("review-source-route-unavailable", str(route_file)) from exc
    if (route.get("route_id") != metadata.get("route_id")
            or route.get("route_hash") != metadata.get("route_hash")):
        raise SourceSubjectError("review-source-route-identity-mismatch")
    node = next((item for item in route.get("nodes", [])
                 if item.get("id") == metadata.get("route_node")), None)
    if node is None:
        return None
    record = read_binding(jobs, metadata, required=require_new)
    if record is None:
        return None  # A pre-feature registry row remains unknown, never inferred.
    reviewed_input = _review_input_binding(jobs, metadata)
    if not has_review_subject(route, node, jobs, reviewed_input):
        return None
    current = observe(route, node, worktree=fields[3], jobs=jobs,
                      reviewed_input=reviewed_input)
    current_epoch = source_epoch(route, node, current["digest"],
                                  Path(jobs).resolve().parent / "completion" / route["route_id"],
                                  family=record.get("family", "source"))
    if current["digest"] != record["subject_digest"] or current_epoch != record["source_epoch"]:
        raise SourceSubjectError("review-source-subject-changed-before-launch", metadata.get("attempt_id", ""))
    return record


def _source_generations(route: dict) -> list[dict]:
    """Return the route and its verified continuation lineage."""
    if route.get("continuation_contract_version") != 1:
        return [route]
    from route_lineage import RouteLineageError, verified_route_lineage
    try:
        return verified_route_lineage(route, artifact_root=route.get("artifact_root"))
    except RouteLineageError as exc:
        raise SourceSubjectError("review-source-lineage-unverifiable", exc.code) from exc


def _canonical_head(generation: dict, generation_dir: Path, node_id: str, candidate: dict,
                    *, verify_unknown: bool = True) -> dict | None:
    """Return a canonical marker proven to equal its own numbered history.

    An absent canonical marker is not an error, and a numbered history ahead of
    the canonical head is a valid pending/crash-replay successor, so neither is
    judged here. A present canonical must name this route/node/gate, carry a
    sequence, and have exactly that sequence's immutable history; a missing,
    unreadable or different history is refused rather than skipped. A legacy
    marker without a source subject has no epoch to lose, so epoch callers may
    leave it unverified; fresh evidence binding verifies it and keeps it unknown.
    """
    marker_path = generation_dir / f"{node_id}.json"
    try:
        info = marker_path.lstat()
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(info.st_mode):
            raise SourceSubjectError("review-source-marker-kind-invalid", node_id)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            raise SourceSubjectError("review-source-marker-identity-invalid", node_id)
        if not verify_unknown and not isinstance(marker.get("source_subject"), dict):
            return marker
        if (marker.get("route_id") != generation.get("route_id")
                or marker.get("route_hash") != generation.get("route_hash")
                or marker.get("node_id") != node_id
                or marker.get("completion_gate") != candidate.get("completion_gate")):
            raise SourceSubjectError("review-source-marker-identity-invalid", node_id)
        sequence = marker.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise SourceSubjectError("review-source-marker-sequence-invalid", node_id)
        history_path = generation_dir / f"{node_id}.{sequence}.json"
        try:
            history_info = history_path.lstat()
        except FileNotFoundError as exc:
            raise SourceSubjectError("review-source-marker-history-missing", node_id) from exc
        if not stat.S_ISREG(history_info.st_mode):
            raise SourceSubjectError("review-source-marker-kind-invalid", node_id)
        if json.loads(history_path.read_text(encoding="utf-8")) != marker:
            raise SourceSubjectError("review-source-marker-history-conflict", node_id)
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, SourceSubjectError):
            raise
        raise SourceSubjectError("review-source-marker-unreadable", node_id) from exc
    return marker


def source_epoch(route: dict, node: dict, digest: str, completion_dir: str | Path,
                 *, family: str = "source") -> int:
    """Predict the route-wide source epoch from append-only marker history.

    Epoch changes only when source content changes. Marker sequence, report
    publication and identical-content commits therefore do not create a new
    source generation; returning to earlier bytes after a different source
    does create a new generation and cannot revive an old PASS. The maximum
    epoch across source-consuming markers carries across sibling branches too.
    """
    directory = Path(completion_dir)
    generations = _source_generations(route)
    completion_root = directory.parent
    observations = []
    for generation in generations:
        generation_dir = completion_root / str(generation.get("route_id", ""))
        for candidate in generation.get("nodes", []):
            node_id = str(candidate.get("id", ""))
            if not node_id:
                continue
            try:
                marker = _canonical_head(generation, generation_dir, node_id, candidate,
                                         verify_unknown=False)
                if marker is None:
                    continue
                subject = marker.get("source_subject")
                if not isinstance(subject, dict):
                    continue  # Legacy marker: preserve it as unknown history.
                if subject.get("family", "source") != family:
                    continue
                if (isinstance(subject.get("epoch"), int)
                        and not isinstance(subject.get("epoch"), bool)
                        and isinstance(subject.get("digest"), str)):
                    observations.append((int(subject["epoch"]), subject["digest"]))
                else:
                    raise SourceSubjectError("review-source-marker-subject-invalid", node_id)
            except (OSError, ValueError, TypeError) as exc:
                if isinstance(exc, SourceSubjectError):
                    raise
                raise SourceSubjectError("review-source-marker-unreadable", node_id) from exc
    # A registered subject is an observed generation even when its worker
    # never published a completion marker. This preserves A→B→A currentness
    # without changing any verdict count or inventing an unbound history row.
    jobs_path = completion_root.parent / "jobs.log"
    route_by_id = {str(item.get("route_id")): item for item in generations}
    try:
        lines = jobs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise SourceSubjectError("review-source-registry-unreadable", str(jobs_path)) from exc
    if isinstance(family, str) and family.startswith("document:"):
        latest_digest = None
        latest_epoch = 0
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = {}
            for part in fields[5].split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    metadata[key] = value
            generation = route_by_id.get(metadata.get("route_id", ""))
            if generation is None or metadata.get("route_hash") != generation.get("route_hash"):
                continue
            generation_node = next((item for item in generation.get("nodes", [])
                                    if item.get("id") == metadata.get("route_node")), None)
            if generation_node is None:
                continue
            binding = read_binding(jobs_path, metadata, required=False)
            if binding is not None:
                if binding.get("family", "source") != family:
                    continue
                epoch = binding.get("source_epoch")
                if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
                    raise SourceSubjectError("review-source-binding-identity-mismatch",
                                             metadata.get("attempt_id", ""))
                digest_value = binding["subject_digest"]
            else:
                reviewed = _review_input_binding(jobs_path, metadata)
                if reviewed is None or _subject_family(generation, generation_node, reviewed) != family:
                    continue
                _old_family, _manifest, digest_value = _document_primary_subject(
                    generation, generation_node, reviewed)
                epoch = (0 if latest_digest is None
                         else latest_epoch + (digest_value != latest_digest))
            observations.append((epoch, digest_value))
            latest_digest, latest_epoch = digest_value, epoch
    else:
      for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = {}
        for part in fields[5].split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                metadata[key] = value
        generation = route_by_id.get(metadata.get("route_id", ""))
        if generation is None or not (metadata.get("review_subject_digest")
                                      or metadata.get("review_subject_binding_digest")):
            continue
        if metadata.get("route_hash") != generation.get("route_hash"):
            raise SourceSubjectError("review-source-binding-identity-mismatch",
                                     metadata.get("attempt_id", ""))
        binding = read_binding(jobs_path, metadata, required=True)
        if binding.get("family", "source") != family:
            continue
        epoch = binding.get("source_epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise SourceSubjectError("review-source-binding-identity-mismatch",
                                     metadata.get("attempt_id", ""))
        observations.append((epoch, binding["subject_digest"]))
    if not observations:
        return 0
    maximum = max(epoch for epoch, _subject in observations)
    latest_subjects = {subject for epoch, subject in observations if epoch == maximum}
    if len(latest_subjects) != 1:
        raise SourceSubjectError("review-source-generation-conflict", str(maximum))
    latest = next(iter(latest_subjects))
    return maximum + (digest != latest)


def _inline_evidence_subject_unchanged(route: dict, node: dict, evidence_sha256: str,
                                       digest: str, epoch: int,
                                       completion_dir: str | Path,
                                       attempt_id: str) -> None:
    """Refuse to give previously recorded review bytes a different source subject."""
    completion_root = Path(completion_dir).parent
    unknown = False
    changed = False
    for generation in _source_generations(route):
        candidate = next((item for item in generation.get("nodes", [])
                          if item.get("id") == node.get("id")
                          and is_implementation_review(generation, item)), None)
        if candidate is None:
            continue
        node_id = str(candidate["id"])
        generation_dir = completion_root / str(generation.get("route_id", ""))
        # Fresh evidence binding consumes the existing canonical head and its
        # exact history together, so a missing history cannot hide old evidence.
        _canonical_head(generation, generation_dir, node_id, candidate)
        for history_path in sorted(generation_dir.glob(f"{node_id}.*.json")):
            suffix = history_path.name[len(node_id) + 1:-5]
            if not suffix.isdigit():
                continue
            sequence = int(suffix)
            try:
                info = history_path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise SourceSubjectError("review-source-marker-kind-invalid", node_id)
                marker = json.loads(history_path.read_text(encoding="utf-8"))
                if (not isinstance(marker, dict)
                        or marker.get("route_id") != generation.get("route_id")
                        or marker.get("route_hash") != generation.get("route_hash")
                        or marker.get("node_id") != node_id
                        or marker.get("completion_gate") != candidate.get("completion_gate")
                        or marker.get("sequence") != sequence):
                    raise SourceSubjectError("review-source-marker-identity-invalid", node_id)
                evidence = marker.get("evidence")
                if not isinstance(evidence, dict) or not re.fullmatch(
                        r"[0-9a-f]{64}", str(evidence.get("sha256", ""))):
                    raise SourceSubjectError("review-source-marker-evidence-invalid", node_id)
                if evidence["sha256"] != evidence_sha256:
                    continue
                subject = marker.get("source_subject")
                if (not isinstance(subject, dict)
                        or subject.get("schema_version") != SCHEMA_VERSION
                        or not re.fullmatch(r"[0-9a-f]{64}", str(subject.get("digest", "")))
                        or not isinstance(subject.get("epoch"), int)
                        or isinstance(subject.get("epoch"), bool)
                        or subject["epoch"] < 0):
                    unknown = True
                elif (not isinstance(marker.get("attempt_id"), str)
                      or not _SAFE_ID.fullmatch(marker["attempt_id"])):
                    unknown = True
                else:
                    provenance_valid = True
                    if "inline_provenance" in subject:
                        provenance = subject["inline_provenance"]
                        provenance_valid = not (
                            not isinstance(provenance, dict)
                            or set(provenance) != {"kind", "attempt_id", "manifest_sha256"}
                            or provenance.get("kind") != "owner-inline"
                            or provenance.get("attempt_id") != marker["attempt_id"]
                            or not re.fullmatch(r"[0-9a-f]{64}", str(
                                provenance.get("manifest_sha256", "")))
                            or marker.get("reviewer_kind") != "owner-inline"
                            or marker.get("review_independence") != "degraded"
                            or marker.get("reviewer_identity") != marker["attempt_id"]
                            or marker.get("registered_worker") is not False
                            or marker.get("execution_surface") != "inline"
                        )
                    if not provenance_valid:
                        unknown = True
                    elif subject["digest"] != digest or subject["epoch"] != epoch:
                        changed = True
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError) as exc:
                if isinstance(exc, SourceSubjectError):
                    raise
                raise SourceSubjectError("review-source-marker-unreadable", node_id) from exc
    if unknown:
        raise SourceSubjectError("review-source-binding-missing", attempt_id)
    if changed:
        raise SourceSubjectError("review-source-subject-changed-before-completion", attempt_id)


def marker_subject(route: dict, node: dict, *, attempt_id: str | None = None,
                   attempt_metadata: dict | None = None, jobs: str | Path | None = None,
                   worktree: str | Path | None = None, completion_dir: str | Path,
                   owner_inline: bool = False,
                   evidence_sha256: str | None = None) -> dict | None:
    """Subject fields to put on a newly published completion marker."""
    reviewed_input = _review_input_binding(jobs, attempt_metadata)
    if is_deploy_review(route, node) and not has_review_subject(route, node, jobs):
        return None  # A request without scoped inputs keeps its existing identity.
    family = _subject_family(route, node, reviewed_input)
    if family is None and node.get("kind") == "review-worker":
        candidate = _generic_artifact_review_subject(route, node)
        family = candidate.get("family") if candidate else None
    if family is None and has_source_subject(route, node):
        family = "source"
    if family is None:
        return None
    if has_source_subject(route, node) and not (is_implementation_review(route, node)
                                                or is_source_test(node)):
        current = observe(route, node, worktree=worktree)
        epoch = source_epoch(route, node, current["digest"], completion_dir, family="source")
        return {"schema_version": SCHEMA_VERSION, "digest": current["digest"], "epoch": epoch,
                "manifest_sha256": _sha(_canonical(current["manifest"]))}
    if is_implementation_review(route, node) or is_source_test(node):
        if is_implementation_review(route, node) and owner_inline and attempt_id:
            current = observe(route, node, worktree=worktree)
            epoch = source_epoch(route, node, current["digest"], completion_dir)
            if evidence_sha256 is not None:
                _inline_evidence_subject_unchanged(
                    route, node, evidence_sha256, current["digest"], epoch,
                    completion_dir, attempt_id,
                )
            return {"schema_version": SCHEMA_VERSION, "digest": current["digest"],
                    "epoch": epoch, "inline_provenance": {
                        "kind": "owner-inline", "attempt_id": attempt_id,
                        "manifest_sha256": _sha(_canonical(current["manifest"]))}}
        if not attempt_metadata:
            if is_source_test(node):
                raise SourceSubjectError("review-source-binding-missing", str(attempt_id or ""))
            return None
        registered = attempt_metadata.get("registered_worker") in (True, 1, "1", "true")
        inline = (is_source_test(node) and not registered
                  and attempt_metadata.get("execution_surface") == "inline")
        record = (read_binding(jobs, attempt_metadata, required=is_source_test(node) and not inline)
                  if jobs else None)
        if record is None:
            if not inline:
                if is_source_test(node):
                    raise SourceSubjectError("review-source-binding-missing", str(attempt_id or ""))
                return None
            current = observe(route, node, worktree=worktree)
            epoch = source_epoch(route, node, current["digest"], completion_dir)
            return {"schema_version": SCHEMA_VERSION, "digest": current["digest"],
                    "epoch": epoch, "manifest_sha256": _sha(_canonical(current["manifest"]))}
        current = observe(route, node, worktree=worktree)
        epoch = source_epoch(route, node, current["digest"], completion_dir)
        if current["digest"] != record["subject_digest"] or epoch != record["source_epoch"]:
            raise SourceSubjectError("review-source-subject-changed-before-completion", str(attempt_id or ""))
        return {"schema_version": SCHEMA_VERSION, "digest": record["subject_digest"],
                "epoch": record["source_epoch"], "binding_digest": record["binding_digest"]}
    registered = attempt_metadata.get("registered_worker") in (True, 1, "1", "true") if attempt_metadata else False
    record = read_binding(jobs, attempt_metadata, required=registered) if jobs and attempt_metadata else None
    if record is None:
        if registered:
            raise SourceSubjectError("review-source-binding-missing", str(attempt_id or ""))
        return None  # Owner-closure is a disposition, not an independent subject verdict.
    if record.get("family", "source") != family:
        raise SourceSubjectError("review-source-binding-identity-mismatch", str(attempt_id or ""))
    current = observe(route, node, worktree=worktree, jobs=jobs, reviewed_input=reviewed_input)
    epoch = source_epoch(route, node, current["digest"], completion_dir, family=family)
    if current["digest"] != record["subject_digest"] or epoch != record["source_epoch"]:
        raise SourceSubjectError("review-source-subject-changed-before-completion", str(attempt_id or ""))
    return {"schema_version": SCHEMA_VERSION, "digest": record["subject_digest"],
            "epoch": record["source_epoch"], "family": family,
            "binding_digest": record["binding_digest"]}


def _currency_registry_path(route: dict, completion_dir: str | Path,
                            jobs: str | Path | None) -> Path:
    """Use the consumer's registry, or resolve the route's sealed selection.

    Older markers without a usable route tuple keep their colocated jobs.log
    fallback. When a sealed path has moved, the shared dangling-registry
    resolver handles its migration alias before that legacy fallback.
    """
    if jobs is not None:
        return Path(jobs).expanduser().resolve(strict=False)

    launch = route.get("launch_compatibility_tuple") or {}
    jobs_identity = launch.get("jobs_path") or {}
    sealed = jobs_identity.get("path") if isinstance(jobs_identity, dict) else None
    if isinstance(sealed, str) and sealed and Path(sealed).is_absolute():
        from dispatch_contract import resolve_dangling_registry

        resolution = resolve_dangling_registry(sealed)
        if resolution.status in {"exact", "aliased"} and resolution.jobs_path is not None:
            return resolution.jobs_path

    # The marker directory is already resolved by its consumer. Legacy
    # migration places markers and the canonical jobs.log together there.
    state_root = Path(completion_dir).expanduser().resolve(strict=False).parent.parent
    return state_root / "jobs.log"


def marker_currency(route: dict, node: dict, marker: dict, completion_dir: str | Path,
                    *, jobs: str | Path | None = None) -> tuple[str, str] | None:
    """Current review-content identity for a marker, or None outside this contract."""
    # Other scoped markers contribute source generations to the append-only
    # epoch history. Only implementation review and code-test gates consume
    # source currency as a gate result.
    source_test = is_source_test(node)
    implementation_review = is_implementation_review(route, node)
    subject = marker.get("source_subject")
    subject_family = subject.get("family", "source") if isinstance(subject, dict) else None
    generic_review = isinstance(subject_family, str) and subject_family.startswith("artifact-review:")
    document_review = False
    try:
        import review_input
        from dispatch_contract import DispatchContractError
        if review_input.is_review_node(node):
            if (isinstance(subject_family, str) and subject_family.startswith("document:")):
                document_review = True
            elif (marker.get("review_gate_closure") == "owner-closure"
                  and marker.get("review_independence") == "owner-overridden"):
                document_review = True
            # An available plan today cannot identify what an unbound
            # historical review consumed. Keep its pre-content identity.
    except ImportError:
        document_review = False
    artifact_review = _design_review(node) or document_review or generic_review
    if (not isinstance(subject, dict) and node.get("kind") == "review-worker"
            and not artifact_review and not (implementation_review or source_test
                                             or is_deploy_review(route, node))):
        try:
            artifact_review = _generic_artifact_review_subject(route, node) is not None
        except SourceSubjectError as exc:
            return "completion-evidence-unreadable", exc.reason
    if (not isinstance(subject, dict) and node.get("kind") == "review-worker"
            and marker.get("review_gate_closure") == "owner-closure"
            and marker.get("review_independence") == "owner-overridden"
            and not (implementation_review or source_test or is_deploy_review(route, node))):
        # Owner disposition is not an independent PASS. The shared completion
        # and readiness readers still authenticate its route and exact evidence.
        return None
    if not (implementation_review or source_test or artifact_review):
        return None
    if not isinstance(subject, dict):
        if not artifact_review:
            return "superseded", "completion-source-subject-unknown"
        return "superseded", "completion-review-subject-unknown"
    family = subject.get("family", "source")
    reviewed_input = None
    attempt_id = marker.get("attempt_id")
    binding_digest = subject.get("binding_digest")
    inline_review = implementation_review and "inline_provenance" in subject
    inline_test = (source_test and not binding_digest
                   and marker.get("registered_worker") is False
                   and marker.get("execution_surface") == "inline")
    if implementation_review and not binding_digest and not inline_review:
        return "superseded", "completion-review-source-binding-unknown"
    if source_test and not binding_digest and not inline_test:
        return "superseded", "completion-test-source-binding-unknown"
    record = None
    row = None
    needs_binding = ((isinstance(family, str) and family.startswith("document:"))
                     or family == "design-build"
                     or ((implementation_review or source_test) and not (inline_review or inline_test)))
    if needs_binding:
        jobs_path = _currency_registry_path(route, completion_dir, jobs)
        rows = []
        try:
            for line in jobs_path.read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
                if metadata.get("attempt_id") == attempt_id:
                    rows.append(metadata)
        except OSError:
            prefix = ("completion-test-source" if source_test else
                      "completion-review-source" if implementation_review else
                      "completion-review-subject")
            return "integrity-broken:identity-mismatch", prefix + "-registry-missing"
        if len(rows) != 1:
            prefix = ("completion-test-source" if source_test else
                      "completion-review-source" if implementation_review else
                      "completion-review-subject")
            return "integrity-broken:identity-mismatch", prefix + "-row-not-exact"
        row = rows[0]
        if (row.get("route_id") != route.get("route_id")
                or row.get("route_hash") != route.get("route_hash")
                or row.get("route_node") != node.get("id")
                or row.get("review_subject_digest") != subject.get("digest")
                or row.get("review_subject_binding_digest") != binding_digest):
            prefix = ("completion-test-source" if source_test else
                      "completion-review-source" if implementation_review else
                      "completion-review-subject")
            return "integrity-broken:identity-mismatch", prefix + "-row-binding-mismatch"
        try:
            record = read_binding_record(jobs_path, str(attempt_id), route_id=route.get("route_id"),
                route_hash=route.get("route_hash"), node_id=node.get("id"))
        except SourceSubjectError:
            record = None
        prefix = ("completion-test-source" if source_test else
                  "completion-review-source" if implementation_review else
                  "completion-review-subject")
        if record is None:
            return "integrity-broken:identity-mismatch", prefix + "-binding-missing"
        if (record.get("binding_digest") != binding_digest
                or _binding_digest(record) != binding_digest
                or record.get("subject_digest") != subject.get("digest")
                or record.get("family", "source") != family
                or record.get("attempt_id") != attempt_id
                or record.get("route_id") != route.get("route_id")
                or record.get("route_hash") != route.get("route_hash")
                or record.get("node_id") != node.get("id")):
            return "integrity-broken:identity-mismatch", prefix + "-binding-invalid"
    if isinstance(family, str) and family.startswith("document:"):
        try:
            reviewed_input = _review_input_binding(jobs_path, row)
        except SourceSubjectError as exc:
            return "integrity-broken:identity-mismatch", "completion-review-subject-input-binding-invalid"
        if reviewed_input is None:
            return "integrity-broken:identity-mismatch", "completion-review-input-binding-missing"
    try:
        current = observe(route, node, jobs=jobs, reviewed_input=reviewed_input)
        if current is None:
            return "superseded", "completion-review-subject-input-missing"
        epoch = source_epoch(route, node, current["digest"], completion_dir, family=family)
    except SourceSubjectError as exc:
        return "completion-evidence-unreadable", exc.reason
    if (subject.get("schema_version") != SCHEMA_VERSION
            or family != current.get("family", "source")
            or subject.get("digest") != current["digest"]
            or subject.get("epoch") != epoch):
        reason = ("completion-test-source-stale" if source_test else
                  "completion-review-source-stale" if implementation_review else
                  "completion-review-source-stale" if artifact_review else
                  "completion-source-stale")
        return "superseded", reason
    if inline_review:
        inline = subject["inline_provenance"]
        if (not isinstance(inline, dict)
                or set(inline) != {"kind", "attempt_id", "manifest_sha256"}
                or inline.get("kind") != "owner-inline"
                or not attempt_id or inline.get("attempt_id") != attempt_id
                or inline.get("manifest_sha256") != _sha(_canonical(current["manifest"]))
                or marker.get("reviewer_kind") != "owner-inline"
                or marker.get("review_independence") != "degraded"
                or marker.get("reviewer_identity") != attempt_id
                or marker.get("registered_worker") is not False
                or marker.get("execution_surface") != "inline"
                or binding_digest is not None):
            return "integrity-broken:identity-mismatch", "completion-review-inline-provenance-invalid"
    elif inline_test and subject.get("manifest_sha256") != _sha(_canonical(current["manifest"])):
        return "integrity-broken:identity-mismatch", "completion-test-source-manifest-invalid"
    return None
