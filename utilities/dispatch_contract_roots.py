"""Dispatch contract: state roots and the global registry.

Dispatch state roots, the global registry and its file modes, the migration
journal, completed aliases, dangling registries, and nested-dispatch
eligibility.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import NamedTuple

from dispatch_contract_base import (
    CANONICAL_PARENT_TRANSPORTS, DispatchContractError, ELIGIBILITY, LAUNCH_AUTHORITIES,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH, RegistrySelection,
)


def _absolute(path: str | Path, field: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise DispatchContractError(f"{field}-must-be-absolute", str(value))
    return value.resolve(strict=False)


def _versioned_source_layout(path: str | Path) -> tuple[str, Path | None]:
    """Classify installed immutable source trees without trusting symlink spelling.

    A Codex activation has an unambiguous mutable runtime home immediately before
    ``.harness/bundles/<id>/source``.  A shared Hearting release does not encode
    which runtime owns the launch, so selecting a registry from it must fail
    closed instead of guessing.
    """

    candidate = Path(path).expanduser().resolve(strict=False)
    parts = candidate.parts
    for index, part in enumerate(parts):
        if (
            part == ".harness"
            and index + 3 < len(parts)
            and parts[index + 1] == "bundles"
            and parts[index + 3] == "source"
        ):
            return "bundle", Path(*parts[:index])
        if (
            part == "hearting"
            and index + 2 < len(parts)
            and parts[index + 1] == "releases"
        ):
            return "shared-release", None
    return "mutable-or-checkout", None


def _validated_registry_path(path: str | Path, field: str) -> Path:
    candidate = _absolute(path, field)
    layout, _runtime_home = _versioned_source_layout(candidate)
    if layout == "bundle":
        raise DispatchContractError(
            "versioned-source-registry-fallback",
            f"{field}={candidate}; set AGENT_DISPATCH_JOBS to activation-owned mutable state",
        )
    return candidate


def state_root(environ: dict[str, str] | os._Environ[str]) -> Path:
    """The one per-user state root: `HARNESS_STATE_ROOT` (installer-owned) or
    `$XDG_STATE_HOME/hearting` or `$HOME/.local/state/hearting`, read only from
    the passed mapping so an isolation test's `environ={}` never touches the
    live process environment or creates a real user home (SD-112 §13.33.2-(1))."""

    harness_state_root = environ.get("HARNESS_STATE_ROOT")
    if harness_state_root:
        return _absolute(harness_state_root, "harness-state-root")
    xdg_state_home = environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return _absolute(xdg_state_home, "xdg-state-home") / "hearting"
    home = environ.get("HOME")
    if not home:
        raise DispatchContractError(
            "dispatch-state-root-unresolved",
            "none of HARNESS_STATE_ROOT, XDG_STATE_HOME, HOME are set",
        )
    return _absolute(home, "home") / ".local" / "state" / "hearting"


def stable_state_root(environ: dict[str, str] | os._Environ[str]) -> Path:
    """The canonical release-independent dispatch state root (SD-112
    §13.33.2-(1)): `state_root(environ) / "dispatch"`."""

    return state_root(environ) / "dispatch"


LAUNCH_MISMATCH_VALUE_MAX = 180

# `\t` delimits row fields and `,`/`=` delimit metadata pairs. The rest is every
# character `str.splitlines()` treats as a line break -- what every registry reader
# and writer splits rows on. Linux permits all of them in a path, and stripping only
# `\t\n\r=,` left eight that still split a row (VT, FF, FS, GS, RS, NEL, LS, PS).
_REGISTRY_UNSAFE = frozenset(
    ",=\t\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
)


def _registry_safe(value: str, limit: int = LAUNCH_MISMATCH_VALUE_MAX) -> str:
    """One registry metadata value: no separator may survive inside it.

    Rows are tab-delimited with a comma-separated `k=v` metadata field, so a
    value carrying a comma, tab, newline or `=` would be parsed back as extra
    keys. Paths are the realistic offender.
    """

    cleaned = "".join(
        "_" if character in _REGISTRY_UNSAFE else character for character in str(value)
    )
    return cleaned[:limit]


def launch_mismatch_annotation(detail: str) -> dict[str, str]:
    """Turn a launch-fence `launch-runtime-root-mismatch` detail into row fields.

    The fence already reports which sealed root disagreed with the live one
    (`revalidate_launch_compatibility` returns a per-root `expected`/`actual`/
    `fields` map, and `launch-fence.py` serializes it), but the launcher recorded
    only the bare reason on the row. Three codex owners died on 2026-09-04
    (att-4dbfe919, att-7629175b, att-ce81b143) and the row said
    `dead-launch-runtime-root-mismatch` with no way to tell whether the offending
    root was `runtime_root`, `grounding_roots.cwd` or `jobs_path` -- the cause had
    to be inferred. Carry the answer on the row instead.

    Returns `{}` for any detail this cannot read: a diagnostic must never be the
    reason a failure path fails.
    """

    _, _, payload = detail.partition(" ")
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    mismatches = parsed.get("mismatches") if isinstance(parsed, dict) else None
    if not isinstance(mismatches, dict) or not mismatches:
        return {}
    roots = sorted(str(name) for name in mismatches)
    annotation = {"launch_mismatch_roots": _registry_safe("|".join(roots))}
    # The detail paths can only describe ONE root, so name it: an alphabetical
    # first pick made a row list three roots while its paths silently described
    # `grounding_roots.cwd`, which is the inference this record exists to remove.
    # `runtime_root` is the one that produced defect Q, so it is preferred.
    subject = "runtime_root" if "runtime_root" in mismatches else roots[0]
    detail_of = mismatches.get(subject)
    if isinstance(detail_of, dict):
        fields = detail_of.get("fields")
        if isinstance(fields, list) and fields:
            annotation["launch_mismatch_fields"] = _registry_safe(
                subject + ":" + "|".join(str(item) for item in fields)
            )
        for key, source in (("expected", "launch_mismatch_expected"),
                            ("actual", "launch_mismatch_observed")):
            side = detail_of.get(key)
            path = side.get("path") if isinstance(side, dict) else side
            if path:
                # `:` qualifies, never `=` -- that one separates metadata pairs and
                # `_registry_safe` strips it, which would silently mangle the value.
                annotation[source] = _registry_safe(subject) + ":" + _registry_safe(path)
    return annotation


def git_optional_locks_env() -> dict[str, str]:
    """Disable Git's optional index refreshes in the dispatched worker."""
    return {"GIT_OPTIONAL_LOCKS": "0"}


def bytecode_cache_env(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> dict[str, str]:
    """`PYTHONPYCACHEPREFIX` pointing at the state root, for a harness child.

    A managed release must stay byte-identical to what it was built from, but
    Python writes `__pycache__` beside every module it imports, so any process
    running with `AGENT_HOME` at a release drops bytecode into it (91 files were
    already there before this was wired). That was tolerable while each runtime
    held its own bundle copy; once packaged bundles link the release, all three
    runtimes write into the one shared tree.

    A prefix keeps the caching (the point of it) and moves the artifacts to
    state, where every other mutable byte the harness produces already lives.
    Digests are unaffected either way -- `__pycache__` is in `_IGNORE_NAMES` --
    so this is about the immutability invariant, not about a checksum.

    Returns an empty mapping when the state root cannot be resolved: bytecode
    placement must never be the reason a launch fails.
    """

    source = os.environ if environ is None else environ
    if source.get("PYTHONPYCACHEPREFIX") or source.get("PYTHONDONTWRITEBYTECODE"):
        return {}
    try:
        root = state_root(source)
    except (DispatchContractError, OSError):
        return {}
    return {"PYTHONPYCACHEPREFIX": str(root / "pycache")}


def _fallback_registry(
    agent_home: Path, environ: dict[str, str] | os._Environ[str]
) -> Path:
    home = Path(agent_home).expanduser()
    resolved_home = home.resolve(strict=False)
    layout, runtime_home = _versioned_source_layout(resolved_home)
    if layout == "bundle":
        assert runtime_home is not None
        return (runtime_home / ".harness" / "dispatch" / "jobs.log").resolve(
            strict=False
        )
    # State-root chain (3) supersession (SD-112 §13.33.2-(8)): a shared managed
    # release and a maintainer checkout both default to the stable per-user
    # root now. Release succession moves file *contents*, not the `jobs_path`
    # *identity* that `revalidate_launch_compatibility` seals, which is exactly
    # the failure mode this migration closes. A checkout-local `.dispatch` is
    # reachable only through an explicit chain ①/② override (`explicit_jobs` or
    # `AGENT_DISPATCH_JOBS`), resolved by callers before they ever reach this
    # fallback, or as a legacy read candidate in `dispatch_state_roots()`.
    return stable_state_root(environ) / "jobs.log"


def resolve_global_registry(
    agent_home: Path,
    explicit_jobs: str | None,
    dispatch_depth: int,
    action: str,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> RegistrySelection:
    """Resolve the one authoritative registry and reject nested overrides.

    Dispatch-depth-0/root dispatch may select an explicit registry once. The wrapper then
    exports it through AGENT_DISPATCH_JOBS. A real nested start must inherit that
    path; argv may repeat it, but cannot replace it.
    """

    env = os.environ if environ is None else environ
    inherited_raw = env.get("AGENT_DISPATCH_JOBS")
    explicit = _validated_registry_path(explicit_jobs, "jobs") if explicit_jobs else None
    inherited = (
        _validated_registry_path(inherited_raw, "agent-dispatch-jobs")
        if inherited_raw
        else None
    )

    managed_parent = (
        env.get("AGENT_CODEX_MANAGED_GATEWAY") == "1"
        and env.get("AGENT_CODEX_MANAGED_PARENT_RUNTIME") == "codex"
    )
    if managed_parent and inherited and explicit and inherited != explicit:
        raise DispatchContractError(
            "managed-parent-registry-immutable",
            f"explicit={explicit} inherited={inherited}",
        )

    if dispatch_depth > 1 and inherited and explicit and inherited != explicit:
        raise DispatchContractError(
            "noncanonical-nested-jobs",
            f"explicit={explicit} inherited={inherited}",
        )

    nested_start = dispatch_depth > 1 and action == "start"
    if nested_start and inherited is None:
        raise DispatchContractError(
            "global-registry-unset",
            "nested --start requires inherited AGENT_DISPATCH_JOBS",
        )

    # An ordinary dispatch-depth-1 invocation is the root dispatch boundary and may
    # choose a new registry over unrelated ambient shell state.  A managed interactive
    # parent is different: its launcher enrolled one canonical registry, so the check
    # above makes that inherited path immutable for the entire session.
    if dispatch_depth <= 1 and explicit:
        return RegistrySelection(explicit, "root-explicit", False)
    if inherited:
        return RegistrySelection(inherited, "inherited-env", True)
    if explicit:
        return RegistrySelection(explicit, "root-explicit", False)
    fallback = _fallback_registry(agent_home, env).resolve(strict=False)
    source = "agent-home"
    if fallback.parent.name == "dispatch":
        try:
            is_stable = fallback.parent == stable_state_root(env)
        except DispatchContractError:
            is_stable = False
        source = "stable-state-root" if is_stable else "activation-runtime"
    return RegistrySelection(fallback, source, False)


def dispatch_state_root(jobs: str | Path) -> Path:
    """The one derivation: dispatch state lives beside its canonical registry."""

    return Path(jobs).expanduser().resolve(strict=False).parent


def validate_dispatch_log_dir(
    jobs: str | Path, log_dir: str | Path | None
) -> Path:
    """Resolve a launch log directory inside the registry-owned state root."""

    state_root = dispatch_state_root(jobs)
    candidate = (
        state_root / "logs"
        if log_dir is None
        else Path(log_dir).expanduser().resolve(strict=False)
    )
    try:
        candidate.relative_to(state_root)
    except ValueError as exc:
        raise DispatchContractError(
            "log-dir-outside-dispatch-state-root", str(candidate)
        ) from exc
    if candidate == state_root or candidate.is_symlink() or candidate.parent.is_symlink():
        raise DispatchContractError(
            "log-dir-outside-dispatch-state-root", str(candidate)
        )
    return candidate


def resolve_dispatch_state_root(
    agent_home: Path,
    explicit_jobs: str | Path | None = None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    """Resolve the one canonical dispatch state root.

    Chain: ① `explicit_jobs` (a `RegistrySelection.path` the caller already
    holds) -> ② inherited `AGENT_DISPATCH_JOBS` -> ③ a checked fallback.
    Maintainer checkouts retain `agent_home/.dispatch`; Codex bundle sources
    derive activation-owned mutable state, and ambiguous shared releases fail
    closed. No new env
    var -- the only override surface remains `AGENT_DISPATCH_JOBS`, so marker
    root and registry root cannot structurally diverge. A caller that already
    has a `RegistrySelection` must pass `explicit_jobs=selection.path` so
    marker root and registry root are pinned to the same value at the call
    site, not re-derived independently.
    """

    if explicit_jobs is not None:
        return dispatch_state_root(_validated_registry_path(explicit_jobs, "jobs"))
    env = os.environ if environ is None else environ
    inherited = env.get("AGENT_DISPATCH_JOBS")
    if inherited:
        return dispatch_state_root(
            _validated_registry_path(inherited, "agent-dispatch-jobs")
        )
    return _fallback_registry(agent_home, env).parent


def inherited_jobs_argument() -> dict[str, object]:
    """`add_argument` options for a `--jobs` the inherited `AGENT_DISPATCH_JOBS`
    answers: its value is the default when set; otherwise `--jobs` stays
    required exactly as before."""

    inherited = os.environ.get("AGENT_DISPATCH_JOBS") or None
    return {"default": inherited, "required": inherited is None,
            "help": "default: AGENT_DISPATCH_JOBS"}


def dispatch_state_roots(
    agent_home: Path,
    jobs: str | Path | None = None,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[Path, ...]:
    """Read order for dispatch state (SD-112 §13.33.2-(6)): the canonical
    write root the resolver actually chose, then the stable per-user root (a
    no-op once the two coincide, which is the common case now that chain-3
    defaults to stable), then the legacy agent-home/active-release-relative
    tree. Deduplicated, read-only past index 0 -- the writer uses only
    `dispatch_state_roots(...)[0]`, which is always
    `resolve_dispatch_state_root()`'s result and never a hardcoded stable
    literal; stable-first is a *consequence* of that resolution, not an
    override applied here.
    """

    env = os.environ if environ is None else environ
    canonical = resolve_dispatch_state_root(agent_home, explicit_jobs=jobs, environ=env)
    candidates = [canonical]
    try:
        stable = stable_state_root(env)
    except DispatchContractError:
        stable = None
    if stable is not None and stable not in candidates:
        candidates.append(stable)
    legacy = Path(agent_home).expanduser().resolve(strict=False) / ".dispatch"
    if legacy not in candidates:
        candidates.append(legacy)
    return tuple(candidates)


def _mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _ensure_new_directory_mode(path: Path, mode: int) -> None:
    """Force `mode` only when `path` doesn't exist yet. An existing symlink,
    non-directory, or mode wider than `mode` is a typed refusal -- never a
    chmod of something this process didn't just create (SD-112 §13.33.2-(7)).
    """

    if path.is_symlink():
        raise DispatchContractError(
            "dispatch-state-root-unwritable", f"{path}: refusing symlink"
        )
    if path.exists():
        if not path.is_dir():
            raise DispatchContractError(
                "dispatch-state-root-unwritable", f"{path}: not a directory"
            )
        existing = _mode_bits(path)
        if existing != mode:
            raise DispatchContractError(
                "dispatch-state-root-mode-violation",
                f"{path}: mode={oct(existing)} expected={oct(mode)}",
            )
        return
    path.mkdir(parents=True, exist_ok=False)
    os.chmod(path, mode)  # mkdir's `mode=` is masked by umask; force exact bits.


def _is_stable_dispatch_root(dispatch_root: Path) -> bool:
    """Only the stable per-user dispatch root is mode-enforced by
    `ensure_global_registry_writable` (SD-112 §13.33.2-(7)); every other
    dispatch root keeps its historical, mode-agnostic creation path. Compared
    lexically (`.absolute()`, not `.resolve()`): a symlinked stable root must
    still compare equal so the symlink refusal in `_ensure_new_directory_mode`
    actually fires instead of silently falling through as "not stable"."""

    try:
        stable = stable_state_root(os.environ)
    except DispatchContractError:
        return False
    return Path(dispatch_root).expanduser().absolute() == stable


def ensure_global_registry_writable(path: Path) -> None:
    """Open the global registry and its lock before any child spawn.

    Mode enforcement (`0700` on first creation, typed refusal -- never a
    chmod -- if the root already exists with a different mode, is a symlink,
    or is not a directory) applies only when `path.parent` *is* the stable
    per-user dispatch root (SD-112 §13.33.2-(7)). Every other dispatch root --
    Codex bundle, shared-release/checkout legacy, or any fixture-owned tree --
    keeps today's plain `mkdir(parents=True, exist_ok=True)` with no mode
    check: countless existing fixtures across this suite create or copy those
    directories with an ordinary umask-derived mode (commonly `0o775`, e.g.
    release-succession's `shutil.copytree`), and retrofitting strict mode
    equality onto every one of those call sites is out of this slice's fence.
    `jobs.log` itself stays append-only and mode-agnostic for the same reason.
    `state_root()`'s own ancestry (e.g. `~/.local/state`,
    `~/.local/state/hearting`) is never touched by this function.
    """

    try:
        dispatch_root = path.parent
        dispatch_root.parent.mkdir(parents=True, exist_ok=True)
        if _is_stable_dispatch_root(dispatch_root):
            _ensure_new_directory_mode(dispatch_root, 0o700)
        else:
            dispatch_root.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{path}.lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with path.open("a", encoding="utf-8") as registry:
                registry.flush()
                os.fsync(registry.fileno())
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise DispatchContractError("global-registry-unwritable", f"{path}: {exc}") from exc


def _ensure_new_file_mode(path: Path, mode: int) -> None:
    """Force `mode` only when `path` doesn't exist yet, mirroring
    `_ensure_new_directory_mode` for the migration journal and other stable
    per-user files (SD-112 §13.33.2-(7)). Never chmods an existing file."""

    if path.is_symlink():
        raise DispatchContractError(
            "dispatch-state-root-unwritable", f"{path}: refusing symlink"
        )
    if path.exists():
        if not path.is_file():
            raise DispatchContractError(
                "dispatch-state-root-unwritable", f"{path}: not a regular file"
            )
        existing = _mode_bits(path)
        if existing != mode:
            raise DispatchContractError(
                "dispatch-state-root-mode-violation",
                f"{path}: mode={oct(existing)} expected={oct(mode)}",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(fd)


MIGRATION_ALIAS_RECORD_VERSION = 1
MIGRATION_JOURNAL_FILENAME = "migration-journal.jsonl"


def read_migration_journal(stable_root: Path) -> list[dict]:
    """Best-effort parse of the append-only migration-alias journal. A
    malformed line is skipped, not raised -- the journal is evidence consumed
    by a fail-closed validator, not a strict schema gate on its own."""

    path = Path(stable_root) / MIGRATION_JOURNAL_FILENAME
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


MIGRATION_ALIAS_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def _alias_digest_well_formed(value: object) -> bool:
    """`sha256:` + 64 lowercase hex, nothing else.

    Presence alone is not evidence: a record carrying `"content_digest": "x"`
    is as structurally "complete" as a real one, so a truthiness check lets a
    hand-written journal line relieve a sealed `jobs_path` mismatch. The
    recorded digests cannot be recomputed later -- the legacy registry is
    pruned by the migration that wrote the record, and the stable registry
    keeps appending rows afterwards, so its content digest legitimately
    diverges the moment the next attempt registers. Shape is therefore the
    strongest check available on the digest fields themselves; liveness of the
    target is checked separately, against the filesystem, in
    `resolve_dangling_registry`.
    """

    return isinstance(value, str) and MIGRATION_ALIAS_DIGEST_PATTERN.fullmatch(value) is not None


def _alias_record_valid(record: dict) -> bool:
    """Structural validity of one `completed` migration-alias record.
    Forged or partially-written records (missing, malformed, or
    non-absolute identities) never pass, so `resolve_completed_alias` stays
    fail-closed by construction."""

    if record.get("record_version") != MIGRATION_ALIAS_RECORD_VERSION:
        return False
    if record.get("status") != "completed":
        return False
    legacy = record.get("legacy_jobs_identity")
    target = record.get("stable_jobs_identity")
    if not isinstance(legacy, dict) or not isinstance(target, dict):
        return False
    for identity in (legacy, target):
        path = identity.get("path")
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            return False
        if not _alias_digest_well_formed(identity.get("content_digest")):
            return False
    if not _alias_digest_well_formed(record.get("source_digest")):
        return False
    if not _alias_digest_well_formed(record.get("target_digest")):
        return False
    # Optional by contract (SD-112 §13.33.2-(3): verified only "when present"),
    # but a present-and-malformed value is a broken record, not an absent one --
    # accepting it would let a forgery opt out of the extra verification simply
    # by writing garbage into the field.
    route_hash = record.get("route_hash")
    if route_hash is not None and not _alias_digest_well_formed(route_hash):
        return False
    return True


def resolve_completed_alias(stable_root: Path, legacy_jobs_path: str | Path) -> dict | None:
    """The most recent structurally-valid `completed` alias record whose
    `legacy_jobs_identity.path` matches `legacy_jobs_path`, or `None`. Route
    hash / attempt link, when present on the record, are returned for the
    caller to verify too (decision 1); this function only proves the record
    is a well-formed completed alias, not that any particular route may use
    it."""

    target_path = str(Path(legacy_jobs_path).expanduser().resolve(strict=False))
    match = None
    for record in read_migration_journal(stable_root):
        if not _alias_record_valid(record):
            continue
        if record["legacy_jobs_identity"]["path"] != target_path:
            continue
        match = record  # append-only journal; the latest completed record wins
    return match


class DanglingRegistryResolution(NamedTuple):
    status: str  # "exact" | "aliased" | "compat-window" | "unresolved"
    jobs_path: Path | None
    alias_record: dict | None


def resolve_dangling_registry(
    sealed_jobs: str | Path,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    window_open: bool = True,
) -> DanglingRegistryResolution:
    """Classify one absolute sealed `jobs.log` path against current
    filesystem/journal state (SD-112 §13.33.2-(3)/(6), decision 4).
    `capability-route.py`'s continuation resolver and (Slice 2) Fleet's row
    mapping share this one judgement instead of deriving their own.

    exact          sealed_jobs's parent directory still exists -- use it
                    directly, no alias/window involved.
    aliased        parent is gone, but the stable migration journal has a
                    structurally-valid `completed` record mapping sealed_jobs
                    to a live stable jobs.log. Checked before compat-window
                    (decision 1): an unvalidated path substitution must never
                    look like the normal path.
    compat-window  parent is gone, no alias, and the legacy read window is
                    still open. This cycle never closes the window, so
                    `window_open` defaults `True`; a future cycle can pass
                    the real judgement once `legacy_read_window_may_close()`
                    is wired to an authoritative count.
    unresolved     parent is gone, no alias, and the window is closed.
    """

    sealed = Path(sealed_jobs).expanduser().resolve(strict=False)
    if sealed.parent.is_dir():
        return DanglingRegistryResolution("exact", sealed, None)
    env = os.environ if environ is None else environ
    try:
        stable_root = stable_state_root(env)
    except DispatchContractError:
        stable_root = None
    if stable_root is not None:
        record = resolve_completed_alias(stable_root, sealed)
        if record is not None:
            target = Path(record["stable_jobs_identity"]["path"])
            # The docstring's promise is a *live* stable jobs.log, so require
            # the file itself. A present parent directory proves nothing about
            # the alias: a stale or forged record naming any existing
            # directory would otherwise resurrect a registry that is not
            # there, and the caller would bind a route to it. Without the file
            # this is not an alias -- fall through to the compat window.
            if target.is_file():
                return DanglingRegistryResolution("aliased", target, record)
    if window_open:
        return DanglingRegistryResolution("compat-window", None, None)
    return DanglingRegistryResolution("unresolved", None, None)


LEGACY_READ_WINDOW_MIN_SUPPORTED_RELEASES = 2


def legacy_read_window_may_close(
    *,
    supported_releases_elapsed: int,
    legacy_bound_open_writers: int,
    delta: int,
    legacy_read_hits: int,
) -> bool:
    """Pure four-condition truth table (SD-112 §13.33.2-(6), B-13a). Never
    touches the filesystem and never calls `_succeed_dispatch_state()`; actual
    post-promotion delta reachability is Slice 3's B-13b, not this slice's."""

    return (
        supported_releases_elapsed >= LEGACY_READ_WINDOW_MIN_SUPPORTED_RELEASES
        and legacy_bound_open_writers == 0
        and delta == 0
        and legacy_read_hits == 0
    )


def validate_nested_eligibility(
    *,
    dispatch_depth: int,
    action: str,
    parent_harness: str,
    parent_transport: str,
    parent_sandbox: str,
    child_harness: str,
    launch_authority: str,
    status: str,
    source: str,
) -> None:
    if dispatch_depth < 2:
        return
    if launch_authority not in LAUNCH_AUTHORITIES:
        raise DispatchContractError("invalid-launch-authority", launch_authority)
    if status not in ELIGIBILITY:
        raise DispatchContractError("invalid-nested-eligibility", status)
    if parent_transport not in CANONICAL_PARENT_TRANSPORTS and parent_transport != "unknown":
        raise DispatchContractError(
            "invalid-parent-transport",
            f"{parent_transport}; expected one of {sorted(CANONICAL_PARENT_TRANSPORTS)}",
        )
    # Canonical vocabulary is not enough: this call site is already inside a
    # dispatch-depth-2 launch, whose parent is by construction the depth-1
    # registered-headless owner. `interactive` is a well-formed word for the
    # depth-0 session and a launch-time contradiction here.
    expected_parent_transport = PARENT_TRANSPORT_BY_DISPATCH_DEPTH[1]
    if parent_transport not in (expected_parent_transport, "unknown"):
        raise DispatchContractError(
            "parent-transport-not-registered-headless",
            f"dispatch_depth={dispatch_depth} sealed parent_transport={parent_transport};"
            f" a dispatch-depth-2 parent is the {expected_parent_transport} depth-1 owner",
        )
    missing = [
        name
        for name, value in (
            ("parent_harness", parent_harness),
            ("parent_transport", parent_transport),
            ("parent_sandbox", parent_sandbox),
            ("child_harness", child_harness),
            ("eligibility_source", source),
        )
        if not value or value == "unknown"
    ]
    if action == "start" and missing:
        raise DispatchContractError("nested-eligibility-evidence-missing", ",".join(missing))
    if action == "start" and status != "supported":
        raise DispatchContractError(f"nested-child-spawn-{status}", source or "no checked evidence")
