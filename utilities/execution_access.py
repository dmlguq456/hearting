#!/usr/bin/env python3
"""Strict, runtime-neutral execution access request validation.

The module owns request meaning and receipt vocabulary.  Adapters own only the
spelling of their runtime flags (for example Codex ``--add-dir`` versus App
Server ``--writable-root``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePath
import re
import stat
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_ROOT_INVENTORY_BYTES = 64 * 1024
MAX_ROOTS = 16
MAX_READ_ROOTS = 256
READ_INPUTS_VERSION = 1
HARNESS_SOURCE_DIRECTORIES = (
    "core", "capabilities", "roles", "utilities", "adapters", "docs",
    "hooks", "profiles", "tools",
)
MAX_PATH_LENGTH = 4096
MAX_HOSTS = 32
MAX_TEXT_LENGTH = 500
MAX_JSON_DEPTH = 64
MAX_TASK_TARGET_SCRIPT_BYTES = 1024 * 1024
MAX_DERIVATION_SKIPPED = 48
COMPUTE_NETWORK_REASON = "Compute-hosts SSH for GPU lab execution"
COMPUTE_INVENTORY_REASON = "User compute-hosts inventory for GPU lab execution"

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "writable_roots",
        "read_roots",
        "network",
        "enforcement_required",
        "justification",
    }
)
_NETWORK_FIELDS = frozenset({"required", "reason", "hosts"})
_RUNTIMES = frozenset(
    {"codex-exec", "codex-app-server", "claude-cli", "claude-supervisor", "opencode"}
)


def _runtime_harness(runtime: str) -> str:
    return str(runtime).split("-", 1)[0]


def _os_sandboxed(runtime: str) -> bool:
    """Whether the runtime's declared access enforcement is an OS sandbox
    (`harness_capabilities` `access.enforcement`); an unknown runtime has none."""
    from harness_capabilities import HARNESSES, access
    harness = _runtime_harness(runtime)
    return harness in HARNESSES and access(harness)["enforcement"] == "os-sandbox"
_PATH_BAD = re.compile(r"[\x00-\x1f\x7f,=]|[*?\[\]{}]")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?\Z"
)
_PIPE_VALUE = re.compile(r"^[A-Za-z0-9._:;/-]+$")


class ExecutionAccessError(ValueError):
    """A typed refusal safe for a pre-launch CLI boundary."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class AccessContext:
    worktree: Path
    artifact_root: Path
    dispatch_state_root: Path
    agent_home: Path
    home: Path
    config_roots: tuple[Path, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        worktree: str | Path,
        artifact_root: str | Path,
        dispatch_state_root: str | Path,
        agent_home: str | Path,
        environ: Mapping[str, str] | None = None,
    ) -> "AccessContext":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME") or str(Path.home())).resolve(strict=False)
        roots: list[Path] = []
        for value in (
            env.get("CODEX_HOME") or str(home / ".codex"),
            env.get("CLAUDE_CONFIG_DIR") or str(home / ".claude"),
            env.get("XDG_CONFIG_HOME") or str(home / ".config"),
        ):
            path = Path(value).expanduser()
            if path.is_absolute():
                roots.append(path.resolve(strict=False))
        return cls(
            worktree=Path(worktree).resolve(strict=False),
            artifact_root=Path(artifact_root).resolve(strict=False),
            dispatch_state_root=Path(dispatch_state_root).resolve(strict=False),
            agent_home=Path(agent_home).resolve(strict=False),
            home=home,
            config_roots=tuple(_unique_paths(roots)),
        )


@dataclass(frozen=True)
class ExecutionAccessRequest:
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    network_required: bool
    network_reason: str
    network_hosts: tuple[str, ...]
    enforcement_required: str
    justification: tuple[tuple[str, str], ...]
    request_sha256: str
    source_path: Path


@dataclass(frozen=True)
class ParentGrant:
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...] = ()
    network_allowed: bool = False
    boundary: str = "parent-effective-grant"
    attempt_id: str = ""
    runtime: str = ""
    sandbox: str = ""
    file_enforcement: str = "none"
    network_enforcement: str = "none"


@dataclass(frozen=True)
class ResolvedTaskTargets:
    manifest_path: Path
    manifest_sha256: str
    selected_names: tuple[str, ...]
    writable_roots: tuple[Path, ...]


def _read_bounded_regular_file(path: Path, limit: int) -> bytes:
    """Read a small input without following a symlink or accepting a special file."""

    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError("input must be a non-symlink regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(descriptor)
        if (not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise OSError("input changed while opening")
        if after.st_size > limit:
            raise OSError(f"input exceeds {limit} bytes")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise OSError(f"input exceeds {limit} bytes")
        return raw
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid",
            f"target manifest is not safely readable: {exc}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _task_target_reference(route: Mapping[str, object]) -> tuple[tuple[str, ...], Path] | None:
    """Resolve only the explicit `루트 목록과 경로` input/table pair."""

    work_request = route.get("work_request")
    cwd_value = route.get("cwd")
    if not isinstance(work_request, dict) or not isinstance(cwd_value, str):
        return None
    text = work_request.get("text")
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "## 입력"), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].startswith("## ")), len(lines))
    input_lines = lines[start + 1:end]
    target_rows = [line for line in input_lines if line.startswith("- 루트 목록과 경로: ")]
    if not target_rows:
        return None
    if len(target_rows) != 1:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target reference is ambiguous"
        )
    preview_rows = [line for line in input_lines if line.startswith("- 미리보기(사용자가 본 것): ")]
    if len(preview_rows) != 1:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target requires one paired preview reference"
        )
    target = target_rows[0]
    match = re.fullmatch(
        r"- 루트 목록과 경로: ([A-Za-z0-9_./-]+) 의 ROOTS 표\(([^()]*)\)", target
    )
    if not match:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target reference has an unsupported shape"
    )
    manifest_reference, names_text = match.groups()
    if manifest_reference != "previews/run_all.sh":
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS input is not the supported direct preview table"
        )
    names = tuple(part.strip() for part in names_text.split(","))
    if (not names or len(names) > MAX_ROOTS or any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in names)
            or len(names) != len(set(names))):
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target names are ambiguous"
        )
    preview_match = re.fullmatch(
        r"- 미리보기\(사용자가 본 것\): ([A-Za-z0-9_./-]+)/previews/<루트>\.md",
        preview_rows[0],
    )
    if not preview_match:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "preview and ROOTS paths are not a supported pair"
        )
    preview_base = Path(preview_match.group(1))
    if preview_base.is_absolute() or ".." in preview_base.parts:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "preview reference must stay relative to its source root"
        )
    if preview_base.parts[:1] == (".agent_reports",):
        try:
            route_root_value = Path(str(route.get("artifact_root") or ""))
            if not route_root_value.is_absolute():
                raise ValueError("route artifact root must be absolute")
            canonical_root = route_root_value.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid",
                f"route artifact root is unavailable: {type(exc).__name__}",
            ) from exc
        relative_base = Path(*preview_base.parts[1:])
        manifest = canonical_root / relative_base / manifest_reference
        try:
            manifest.resolve(strict=False).relative_to(canonical_root)
        except ValueError as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", "ROOTS input escapes the canonical artifact root"
            ) from exc
    else:
        if preview_base.parts[:1] and preview_base.parts[0].startswith(".agent_reports"):
            raise ExecutionAccessError(
                "execution-access-target-input-invalid",
                "artifact-relative references must use the canonical .agent_reports prefix",
            )
        source_root = Path(cwd_value).expanduser().resolve(strict=False)
        manifest = source_root / preview_base / manifest_reference
        try:
            manifest.resolve(strict=False).relative_to(source_root)
        except ValueError as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", "ROOTS input escapes the route source directory"
            ) from exc
    return names, manifest


def read_roots_data(path: str | Path, selected_names: Iterable[str]) -> ResolvedTaskTargets:
    """Read only the delimited ROOTS data block; never execute its shell script."""

    manifest = Path(path).expanduser()
    if not manifest.is_absolute():
        manifest = manifest.absolute()
    try:
        resolved_manifest = manifest.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", f"ROOTS input path is invalid: {type(exc).__name__}"
        ) from exc
    raw = _read_bounded_regular_file(manifest, MAX_TASK_TARGET_SCRIPT_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS data is not UTF-8"
        ) from exc
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line == "done <<'ROOTS'"]
    if len(starts) != 1:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "expected one exact ROOTS data block"
        )
    start = starts[0] + 1
    try:
        end = lines.index("ROOTS", start)
    except ValueError as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS data block is unterminated"
        ) from exc
    rows: dict[str, Path] = {}
    for line in lines[start:end]:
        if not line or line.startswith("#"):
            continue
        name, separator, root_text = line.partition("|")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not root_text:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", "ROOTS row has an invalid shape"
            )
        if name in rows:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", f"duplicate ROOTS row: {name}"
            )
        try:
            literal = Path(root_text)
            if not literal.is_absolute() or ".." in literal.parts:
                raise ValueError("root must be an exact absolute path")
            root = literal.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", f"invalid root for {name}: {exc}"
            ) from exc
        rows[name] = root
    names = tuple(selected_names)
    if len(names) != len(set(names)) or any(name not in rows for name in names):
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "a selected ROOTS name is missing or duplicated"
        )
    return ResolvedTaskTargets(
        manifest_path=resolved_manifest,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        selected_names=names,
        writable_roots=tuple(_unique_paths(rows[name] for name in names)),
    )


def resolve_task_targets(route: Mapping[str, object]) -> ResolvedTaskTargets | None:
    """Resolve targets named directly by a route's structured input section."""

    reference = _task_target_reference(route)
    if reference is None:
        return None
    names, manifest = reference
    return read_roots_data(manifest, names)


# Access the sealed task names on its own (no hand-written request needed).
# Read roots come from any absolute path in the task text. Write roots come only
# from the approved scope: the start card's `범위:` (`Scope:`) field as sealed in
# the task, and in it only the paths outside a read-only or excluded clause.
_SCOPE_FIELD = re.compile(r"^[\s*-]*(?:\*\*)?(?:범위|[Ss]cope)(?:\*\*)?\s*[:：]\s*(?:\*\*)?(.*)$")
_TASK_PATH = re.compile(r"(?<![\w.~/\\-])/[^\s`'\"<>()\[\]{}|,;*?=]+")
_SCOPE_CLAUSE = re.compile(r"[^,;·、，()]+")
_SCOPE_EXCLUDED = re.compile(
    r"제외|금지|않|말고|빼고|건드리지|손대지|보호|지\s*말|지\s*마(?:라|세요|십시오|$|[\s.,)])"
    r"|\b(?:exclud\w*|except|never|not|without|untouched|off[- ]limits|protect\w*)\b", re.I)
_SCOPE_READ_ONLY = re.compile(
    r"읽|참조|참고|조회|입력|보기|확인|보존|불변|유지|그대로|조사|분석|검토|비교"
    r"|\b(?:read\w*|referenc\w*|input\w*|inspect\w*|view\w*|preserv\w*|keep|analy[sz]\w*|review\w*)\b",
    re.I)
# A negated action keeps a path read only ("수정 안 함", "쓰기 불가", "don't modify").
_SCOPE_NEGATED = re.compile(
    r"안\s*(?:함|한다|하|됨|된다|되)|없음|없다|없이|불가|못\s*(?:함|한다|하)"
    r"|\b(?:no|cannot|none|avoid\w*|forbid\w*|prohibit\w*|disallow\w*)\b|n't\b", re.I)
# Only a clause that says it writes there, and nothing else, makes a write root.
_SCOPE_WRITES = re.compile(
    r"쓰기|쓴다|써서|저장|수정|생성|작성|갱신|적용|만들|추가|변경|편집|삭제|이동|복사|옮기|출력|기록"
    r"|\b(?:writ\w*|save\w*|updat\w*|modif\w*|creat\w*|appl(?:y|ies)|edit\w*|generat\w*|stor(?:e|es)"
    r"|append\w*|delet\w*|output\w*|copy|move)\b", re.I)
_PATH_PARTICLES = ("에서는", "에서도", "으로는", "에서", "에게", "에는", "에도", "으로", "까지", "부터",
                   "처럼", "이나", "안에", "아래", "폴더", "경로", "로", "에", "의", "을", "를", "은",
                   "는", "이", "가", "와", "과", "도", "만", "나", "안")
# Never derived: credentials, keys, user and runtime settings, system areas.
_SENSITIVE_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".pgpass", ".my.cnf",
    ".git-credentials", ".npmrc", ".pypirc", ".env", ".password-store"})
_SENSITIVE_PREFIXES = ("credential", "secret", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".gpg", ".env")
_SENSITIVE_TOP = frozenset({"etc", "root", "proc", "sys", "dev", "run", "boot"})


@dataclass(frozen=True)
class DerivedAccess:
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    justification: tuple[tuple[str, str], ...]
    record: dict


def _strip_particles(token: str) -> str:
    """`/data/out에` names `/data/out`; a name that merely contains Hangul stays."""

    token = token.rstrip(".:!?。")
    match = re.fullmatch(r"(.*[A-Za-z0-9_./-])([가-힣]+)", token)
    if match:
        rest = match.group(2)
        while rest:
            particle = next((p for p in _PATH_PARTICLES if rest.endswith(p)), None)
            if particle is None:
                return token
            rest = rest[: -len(particle)]
        token = match.group(1).rstrip(".:!?。")
    return token


_KOREAN_WRITE_TAIL = re.compile(
    r"(?:을|를|은|는|이|가|도|만|및)?\s*(?:가능|하다|한다|함|하기|하고|합니다|해요|하여|해서|해|할|됨|된다)?")


def _affirmed_write(clause: str, *, alone: bool = False) -> str | None:
    """The write word a clause affirms, by where it stands rather than which words
    surround it: a Korean one followed by nothing but a particle, `가능` or a plain
    ending (`…/out에 결과 저장`, `쓰기 가능`), an English one with no other word
    before it (`write …/out`) and, once its path came first or in a parenthesis
    right after a path, no word after it either (`…/out (write)`). With `alone` no
    other word may stand before a Korean one either (`(덮어쓰기 가능)`). A question
    mark or anything else (`수정 대상 아님`, `nothing written to`, `writes disabled`)
    affirms nothing and stays read."""

    paths = [match.span() for match in _TASK_PATH.finditer(clause)]
    for found in _SCOPE_WRITES.finditer(clause):
        if any(left < found.end() and found.start() < right for left, right in paths):
            continue                                     # a path's own letters say nothing
        start = found.start()
        while start > 0 and (clause[start - 1].isalnum() or clause[start - 1] == "_"):
            start -= 1                                   # the whole word the match sits in
        before = re.findall(r"[\w가-힣]+", _TASK_PATH.sub(" ", clause[:start]))
        rest = _TASK_PATH.sub(" ", clause[found.end():])
        if "?" in rest:
            continue
        tail = re.sub(r"^[\W_]+|[\W_]+$", "", rest)
        if re.search(r"[가-힣]", found.group(0)):
            if _KOREAN_WRITE_TAIL.fullmatch(tail) and not (alone and before):
                return found.group(0)
        elif not before and not ((alone or any(right <= start for _left, right in paths)) and tail):
            return found.group(0)
    return None


def _scope_access(clauses: list[str], parenthesized: list[bool] | None = None) -> list[tuple[str, str]]:
    """`(access, why)` for each clause of the scope field: `write` only where the
    clause says it writes there and says nothing that reads, negates or excludes,
    `excluded` from the first excluding clause on, otherwise `read`. A clause that
    names no path qualifies the nearest path clause (before it, or the first one after
    a leading clause) with what it reads, negates or excludes (`…/raw (제외)`, `…/raw,
    읽기만`); its write words pass only as a parenthesis right after the path
    (`…/out (write)`), never from a clause about something else. Any doubt falls
    toward less access, never toward write."""

    parenthesized = parenthesized or [False] * len(clauses)
    marks: list[dict[str, str]] = [{} for _ in clauses]
    owner = None
    leading: dict[str, str] = {}
    excluded_from = None
    for index, clause in enumerate(clauses):
        has_path = bool(_TASK_PATH.search(clause))
        if has_path:
            owner = index
            for kind, word in leading.items():   # a path-less clause before the first path
                marks[owner].setdefault(kind, word)
            leading = {}
        words = _TASK_PATH.sub(" ", clause)        # a path's own letters say nothing
        for kind, pattern in (("exclude", _SCOPE_EXCLUDED), ("read", _SCOPE_READ_ONLY),
                              ("negated", _SCOPE_NEGATED)):
            found = pattern.search(words)
            if found:
                (marks[index] if has_path else marks[owner] if owner is not None else leading
                 ).setdefault(kind, found.group(0))
        if has_path:
            written = _affirmed_write(clause)
            if written:
                marks[index].setdefault("write", written)
        elif owner == index - 1 and parenthesized[index]:
            written = _affirmed_write(clause, alone=True)
            if written:
                marks[owner].setdefault("write", written)
        if excluded_from is None and owner is not None and "exclude" in marks[owner]:
            excluded_from = owner
    result = []
    for index, mark in enumerate(marks):
        if excluded_from is not None and index >= excluded_from:
            word = mark.get("exclude") or marks[excluded_from].get("exclude", "")
            result.append(("excluded", f"excluded:{word}"))
        elif "write" in mark and "negated" in mark:
            result.append(("read", f"negated:{mark['negated']}/{mark['write']}"))
        elif "write" in mark and "read" not in mark:
            result.append(("write", f"writes:{mark['write']}"))
        elif "write" in mark:
            result.append(("read", f"both read and write:{mark['read']}/{mark['write']}"))
        elif "read" in mark or "negated" in mark:
            result.append(("read", f"reads:{mark.get('read') or mark['negated']}"))
        else:
            result.append(("read", "no affirmed write word"))
    return result


def _task_paths(text: str) -> list[tuple[int, str, str, str, str, str]]:
    """`(line, path, access, source, quote, why)` for each absolute path the task
    names: `source` is `scope` on the approved scope field, else `task`; `access`
    is `write`, `read` or `excluded` and `why` the words that decided it
    (`_scope_access`); `quote` is the scope clause or the words around the path."""

    found: list[tuple[int, str, str, str, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        scope = _SCOPE_FIELD.match(line)
        if scope is None:
            clauses = [(line, ("read", "task text"))]
        else:
            value = scope.group(1)
            spans = list(_SCOPE_CLAUSE.finditer(value))
            parts = [m.group(0) for m in spans]
            opened = [m.start() > 0 and value[m.start() - 1] == "(" for m in spans]
            clauses = list(zip(parts, _scope_access(parts, opened)))
        for clause, (access, why) in clauses:
            for match in _TASK_PATH.finditer(clause):
                if not match.group(0).startswith("//"):
                    quote = (clause if scope is not None
                             else clause[max(0, match.start() - 60):match.end() + 40])
                    found.append((number, _strip_particles(match.group(0)), access,
                                  "task" if scope is None else "scope", quote, why))
    return found


def harness_source_read_roots(agent_home: Path) -> tuple[Path, ...]:
    """Source contracts only, including the alias native tools may use."""
    canonical_home = agent_home.resolve()
    roots = []
    for name in HARNESS_SOURCE_DIRECTORIES:
        lexical = agent_home / name
        try:
            canonical = lexical.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not _is_within(canonical, canonical_home):
            continue
        parts = canonical.relative_to(canonical_home).parts
        if not parts or parts[0] not in HARNESS_SOURCE_DIRECTORIES:
            continue
        if any(part.lower() in _SENSITIVE_NAMES or part.lower().startswith(_SENSITIVE_PREFIXES)
               or part.lower().endswith(_SENSITIVE_SUFFIXES) for part in parts):
            continue
        roots.extend((lexical, canonical))
    return tuple(dict.fromkeys(roots))


def _sensitive(path: Path, context: AccessContext, *, source_read: bool = False) -> bool:
    parts = path.parts
    if len(parts) > 1 and parts[1] in _SENSITIVE_TOP:
        return True
    for name in parts[1:]:
        lowered = name.lower()
        if (lowered in _SENSITIVE_NAMES or lowered.startswith(_SENSITIVE_PREFIXES)
                or lowered.endswith(_SENSITIVE_SUFFIXES)):
            return True
    source = source_read and any(_is_within(path, root) for root in
                                harness_source_read_roots(context.agent_home))
    data_roots = (context.home / ".local/share",)
    if os.environ.get("XDG_DATA_HOME"):
        data_roots += (Path(os.environ["XDG_DATA_HOME"]),)
    inventory = source_read and "ops" in path.parts and any(
        root.is_absolute() and _is_within(path, root) for root in data_roots)
    if _is_within(path, context.home) and path != context.home and not (source or inventory):
        if path.relative_to(context.home).parts[0].startswith("."):
            return True
    return any(_is_within(path, root) for root in (
        *context.config_roots, context.dispatch_state_root)) or (
            _is_within(path, context.agent_home) and not source)


def _excerpt(quote: str) -> str:
    text = " ".join(quote.split())
    return text if len(text) <= 200 else text[:197] + "..."


def _derivable(literal: Path, resolved: Path, access: str, entry: dict, context: AccessContext,
               granted: tuple[Path, ...], excluded: list[Path]) -> tuple[str, Path, Path, str]:
    """`(skip reason or "", root, resolved root, access)` for one named path; a
    file names its folder, and a write target that is a file is read only."""

    if access == "excluded":
        return "excluded-by-scope", literal, resolved, access
    if _sensitive(literal, context, source_read=access == "read") or _sensitive(
            resolved, context, source_read=access == "read"):
        return "sensitive", literal, resolved, access
    if not resolved.is_dir():
        if not resolved.exists():
            return "missing", literal, resolved, access
        if access == "write":
            access, entry["note"] = "read", "file-target-read-only"
        literal, resolved = literal.parent, resolved.parent
    if _sensitive(literal, context, source_read=access == "read") or _sensitive(
            resolved, context, source_read=access == "read"):
        return "sensitive", literal, resolved, access
    if any(_is_within(resolved, area) for area in granted):
        return "already-granted", literal, resolved, access
    if _broad_root(literal, context) or _broad_root(resolved, context):
        return "too-broad", literal, resolved, access
    if any(_is_within(resolved, x) for x in excluded):
        return "excluded-by-scope", literal, resolved, access
    if any(_is_within(x, resolved) for x in excluded):
        return "holds-excluded-path", literal, resolved, access
    return "", literal, resolved, access


def _root_inventory(path: Path, context: AccessContext) -> tuple[str, ...]:
    """One task-named data file, never executable code or recursive references."""
    if path.suffix.lower() != ".json" or _sensitive(path, context, source_read=True):
        return ()
    try:
        raw = _read_bounded_regular_file(path, MAX_ROOT_INVENTORY_BYTES)
        if not _json_depth_is_bounded(raw):
            return ()
        data = json.loads(raw, object_pairs_hook=_pairs_no_duplicates)
    except (ExecutionAccessError, OSError, ValueError):
        return ()
    rows = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_READ_ROOTS:
        return ()
    return tuple(value for row in rows
                 for value in (row.get("artifact_root_path") if isinstance(row, dict) else row,)
                 if isinstance(value, str) and value.startswith("/"))


def derive_task_access(
    route: Mapping[str, object], context: AccessContext, *, writable: Iterable[Path] = (),
    write: bool = True, read_limit: int = MAX_READ_ROOTS,
) -> DerivedAccess:
    """The access a sealed task names, with the line each root came from.

    A path is skipped (and recorded with its reason) when it is sensitive, missing,
    already inside the worktree, artifact root or another granted root, too broad,
    or excluded by the scope field or holding a path it excludes. A file grants its
    folder for reading; a write target that is a file, or that overlaps a path the
    scope keeps read-only, is granted read only. Nothing here refuses: a path that
    cannot be granted is simply not derived. With `write=False` (a node other than
    the owner) every derived root is read-only."""

    work_request = route.get("work_request")
    text = work_request.get("text") if isinstance(work_request, dict) else None
    granted: list[dict] = []
    skipped: list[dict] = []
    if not isinstance(text, str):
        return DerivedAccess((), (), (), {"granted": granted, "skipped": skipped})
    occurrences = []
    for number, token, access, source, quote, why in _task_paths(text):
        try:
            _validate_path_text(token)
            literal = Path(token)
            resolved = literal.resolve(strict=False)
        except (ExecutionAccessError, OSError, RuntimeError, ValueError):
            skipped.append({"path": token[:200], "line": number, "source": source, "reason": "not-a-path"})
            continue
        occurrences.append((number, literal, resolved, access, source, quote, why))
    excluded = [resolved for _, _, resolved, access, _, _, _ in occurrences if access == "excluded"]
    # Inventory membership is approved read input, never a write instruction.
    # Inspect only directly named files, before an in-worktree file is absorbed.
    for number, literal, resolved, access, source, quote, why in list(occurrences):
        if access == "excluded" or any(_is_within(resolved, x) for x in excluded):
            continue
        for token in _root_inventory(resolved, context):
            try:
                _validate_path_text(token)
                target = Path(token)
                canonical = _resolve_request_path(target)
            except (ExecutionAccessError, OSError, RuntimeError, ValueError):
                skipped.append({"path": token[:200], "source": "inventory", "reason": "not-a-path"})
                continue
            occurrences.append((number, target, canonical, "read", "inventory",
                                str(literal), "task-named root inventory"))
    kept_read_only = [resolved for _, _, resolved, access, source, _, _ in occurrences
                      if access == "read" and source == "scope"]
    defaults = (context.worktree, context.artifact_root,
                *(Path(p).resolve(strict=False) for p in writable))
    candidates: list[tuple[Path, str, dict]] = []
    for number, literal, resolved, access, source, quote, why in occurrences:
        entry = {"line": number, "source": source, "text": _excerpt(quote), "why": why}
        try:
            reason, literal, resolved, access = _derivable(
                literal, resolved, access, entry, context, defaults, excluded)
        except (OSError, RuntimeError, ValueError):
            reason = "unreadable"
        if reason:
            skipped.append({"path": str(literal), **entry, "reason": reason})
            continue
        if access == "write" and any(_is_within(x, resolved) or _is_within(resolved, x)
                                     for x in kept_read_only):
            access, entry["note"] = "read", "scope-keeps-part-read-only"
        elif access == "write" and not write:
            access, entry["note"] = "read", "node-reads-only"
        candidates.append((resolved, access, entry))
    write_roots = _unique_paths(r for r, a, _ in candidates if a == "write")
    write_roots = [r for r in write_roots if not any(_proper_ancestor(o, r) for o in write_roots)]
    read_roots = _unique_paths(r for r, a, _ in candidates if a == "read")
    read_roots = [r for r in read_roots
                  if not any(_is_within(r, w) for w in write_roots)
                  and not any(_proper_ancestor(o, r) for o in read_roots)]
    chosen: list[tuple[Path, str]] = []
    for root in (*write_roots, *read_roots):
        access = "write" if root in write_roots else "read"
        entry = next(e for r, a, e in candidates if r == root and a == access)
        limit = MAX_ROOTS - len(_unique_paths(writable)) if access == "write" else read_limit
        if sum(a == access for _, a in chosen) >= limit:
            skipped.append({"path": str(root), **entry, "reason": "root-limit"})
            continue
        chosen.append((root, access))
        granted.append({"path": str(root), "access": access, **entry})
    distinct = {(row["path"], row["reason"]): row for row in reversed(skipped)}
    skipped = [row for row in skipped if distinct.get((row["path"], row["reason"])) is row]
    return _derived_from_record({"granted": granted, "skipped": skipped[:MAX_DERIVATION_SKIPPED]})


def _derived_from_record(record: object) -> DerivedAccess | None:
    """The derived roots and their justification, read back from a derivation record."""

    if not isinstance(record, dict):
        return None
    granted = [row for row in record.get("granted") or () if isinstance(row, dict)]
    justification = tuple(
        (str(row.get("path")), (f"Derived {row.get('access')} root from the "
                                f"{'approved scope field' if row.get('source') == 'scope' else 'task text'} "
                                f"(line {row.get('line')}, {row.get('why') or 'task text'}): "
                                f"{row.get('text')}")[:MAX_TEXT_LENGTH])
        for row in granted)
    return DerivedAccess(
        writable_roots=tuple(Path(str(row.get("path"))) for row in granted if row.get("access") == "write"),
        read_roots=tuple(Path(str(row.get("path"))) for row in granted if row.get("access") == "read"),
        justification=justification,
        record=dict(record),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ExecutionAccessError("execution-access-cache-conflict", "cache path is a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExecutionAccessError(
            "execution-access-cache-unavailable", f"cannot persist prepared request: {exc}"
        ) from exc


def _lab_run_root(route: Mapping[str, object], node: str) -> Path | None:
    """Read normal lab run storage from the existing inventory loader only."""

    if not compute_execution_scope(route, node=node, owner=(node == "owner")):
        return None
    path = Path(__file__).resolve().parent / "compute-hosts.py"
    spec = importlib.util.spec_from_file_location("_execution_compute_hosts", path)
    if spec is None or spec.loader is None:
        raise ExecutionAccessError("execution-access-compute-inventory-invalid", "inventory loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.load_config()["run_root"]
    except module.ConfigError as exc:
        if exc.status in {"missing", "template"}:
            return None
        raise ExecutionAccessError("execution-access-compute-inventory-invalid", str(exc)) from exc


_TOPOLOGY_REGISTRY_CACHE: dict[str, tuple[object, object]] = {}


def _topology_registry():
    topology_path = (Path(__file__).resolve().parents[1] / "tools" / "capability_topology.py").resolve()
    key = str(topology_path)
    cached = _TOPOLOGY_REGISTRY_CACHE.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_execution_capability_topology", topology_path)
    if spec is None or spec.loader is None:
        return None
    topology = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(topology)
    registry = topology.load_registry()
    cached = (topology, registry)
    _TOPOLOGY_REGISTRY_CACHE[key] = cached
    return cached


def compute_execution_scope(route: Mapping[str, object], *, node: str | None = None,
                            owner: bool = True) -> bool:
    """Resolve compute access from each sealed node's registered origin stage."""
    try:
        loaded = _topology_registry()
        if loaded is None:
            return False
        topology, registry = loaded
    except (OSError, ValueError, ImportError):
        return False

    selected = []
    nodes = route.get("nodes") if isinstance(route.get("nodes"), list) else []
    if node not in (None, "owner"):
        selected = [item for item in nodes if isinstance(item, dict) and item.get("id") == node]
    elif owner:
        selected = [item for item in nodes if isinstance(item, dict)]
    else:
        selected = [item for item in nodes if isinstance(item, dict) and item.get("id") == node]

    for sealed in selected:
        part = sealed.get("part")
        if isinstance(part, str):
            identity = part
        else:
            stage = sealed.get("parallel_anchor") or sealed.get("id")
            capability = route.get("capability")
            if not isinstance(capability, str) or not isinstance(stage, str):
                continue
            identity = f"{capability}:{stage}"
        try:
            found = topology.part_row(registry, identity)
        except (KeyError, TypeError, ValueError):
            found = None
        if found and "compute-hosts" in (found.get("execution_needs") or []):
            return True
    return False


def _route_identity(route: Mapping[str, object]) -> tuple[str, str] | None:
    route_id, route_hash = route.get("route_id"), route.get("route_hash")
    if (isinstance(route_id, str) and re.fullmatch(r"[A-Za-z0-9._-]+", route_id)
            and isinstance(route_hash, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", route_hash)):
        return route_id, route_hash
    return None


def _prepared_binding(path: Path) -> dict | None:
    try:
        value = json.loads(_read_bounded_regular_file(path, MAX_REQUEST_BYTES)) if path.exists() else None
    except (ExecutionAccessError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _given_access(route: Mapping[str, object], jobs: str | Path) -> Path | None:
    """The request the route's parent handed its next owner (`route_authority.access_in_force`),
    as a write-once file named by its digest; None while the first prepared request stands."""

    import route_authority

    change = route_authority.access_in_force(route)
    identity = _route_identity(route)
    if change is None or change.get("source") == "derived" or identity is None:
        return None
    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    path = (state_root / "execution-access" / "routes" / identity[0] / "access"
            / change["request_sha256"][:16] / "given.json")
    data = _canonical_json_bytes(change["request"])
    try:
        existing = _read_bounded_regular_file(path, MAX_REQUEST_BYTES) if path.exists() else None
    except OSError as exc:
        raise ExecutionAccessError("execution-access-cache-conflict", "given request is unreadable") from exc
    if existing is None:
        _atomic_write(path, data)
    elif existing != data:
        raise ExecutionAccessError("execution-access-cache-conflict", "given request changed after it was recorded")
    return path


def prepare_task_request(
    route: Mapping[str, object], jobs: str | Path, *, node: str = "owner", environment: bool = True,
) -> Path | None:
    """Prepare named targets, lab run storage and the access the task text names
    (`derive_task_access`) with the existing request schema.

    An explicit request file wins over derivation; a lab owner still adds its run
    storage to it. Without one in the environment (or with `environment=False`), the
    request the route's parent last handed its next owner is that explicit request
    (`route_authority.access_in_force`). Each route node keeps its own prepared file, and a
    node derives once, at its first preparation: a later start or resume reuses that
    result. An older snapshot adds only its missing read inputs once."""

    # The explicit typed request keeps precedence over preview-table input.
    compute_owner = node == "owner" and compute_execution_scope(route, owner=True)
    explicit = request_path(None) if environment else None
    given = _given_access(route, jobs) if explicit is None else None
    if given is not None and not compute_owner:
        return given
    explicit = explicit if given is None else given
    targets = None if compute_owner and explicit is not None else resolve_task_targets(route)
    run_root = _lab_run_root(route, node)
    compute_scope = compute_owner and run_root is not None
    inventory_root = None
    if compute_scope:
        inventory = os.environ.get("COMPUTE_HOSTS_CONFIG") or (
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "hearting/compute-hosts.yaml")
        inventory_root = Path(inventory).expanduser().absolute().parent
    if given is not None and run_root is None:
        return given
    if targets is None and run_root is None and (
            explicit is not None or _route_identity(route) is None
            or not re.fullmatch(r"[A-Za-z0-9._-]+", node)):
        return None
    route_id = route.get("route_id")
    route_hash = route.get("route_hash")
    if not isinstance(route_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", route_id):
        raise ExecutionAccessError("execution-access-route-invalid", "route id is missing or invalid")
    if not isinstance(route_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", route_hash):
        raise ExecutionAccessError("execution-access-route-invalid", "route hash is missing or invalid")
    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    directory = state_root / "execution-access" / "routes" / route_id
    if given is not None:
        directory = given.parent
    if node != "owner" and re.fullmatch(r"[A-Za-z0-9._-]+", node):
        directory = directory / "nodes" / node
    request_path_value = directory / "request.json"
    binding_path = directory / "binding.json"
    previous_binding = _prepared_binding(binding_path)
    if compute_scope:
        # Old prepared requests remain history. A normal start/correction on
        # an upgraded release prepares these runtime defaults alongside them,
        # retaining the task roots already derived by the original owner.
        directory = directory / "compute"
        request_path_value = directory / "request.json"
        binding_path = directory / "binding.json"
    prior = _prepared_binding(binding_path) or previous_binding
    upgrade_reads = explicit is None and prior is not None and prior.get("read_inputs_version") != READ_INPUTS_VERSION
    if upgrade_reads:
        # Keep the historical request and its write decisions. Only the missing
        # read inputs are filled on the first normal start after upgrading.
        directory = directory / "read-inputs"
        request_path_value = directory / "request.json"
        binding_path = directory / "binding.json"
        upgraded = _prepared_binding(binding_path)
        if upgraded is not None:
            prior, upgrade_reads = upgraded, False
    context = AccessContext.build(
        worktree=str(route.get("cwd") or ""),
        artifact_root=str(route.get("artifact_root") or ""),
        dispatch_state_root=state_root,
        agent_home=Path(__file__).resolve().parents[1],
    )
    if inventory_root is not None and _broad_root(inventory_root, context):
        # An explicitly placed inventory may sit beside the worktree or at a
        # broad root. Keep the utility's exact file input without granting its
        # surrounding tree or adding a new refusal to an otherwise valid run.
        inventory_root = None
    supplied = load_request(explicit, context=context) if run_root is not None and explicit is not None else None
    named = list(targets.writable_roots if targets else ())
    if run_root is not None:
        named.append(run_root)
    if supplied is not None:
        named.extend(supplied.writable_roots)
    if explicit is not None:
        derived = None
    elif prior is not None:
        derived = _derived_from_record(prior.get("derivation"))
        if upgrade_reads:
            reads = derive_task_access(route, context,
                writable=(*named, *(derived.writable_roots if derived else ())), write=False)
            previous = derived.record if derived else {}
            rows = {row["path"]: row for row in previous.get("granted", [])}
            read_roots = {row["path"] for row in rows.values() if row.get("access") == "read"}
            if inventory_root is not None:
                read_roots.add(str(inventory_root))
            skipped = [*previous.get("skipped", []), *reads.record.get("skipped", [])]
            for row in reads.record.get("granted", []):
                if row["path"] in rows:
                    continue
                if row["path"] not in read_roots and len(read_roots) >= MAX_READ_ROOTS:
                    skipped.append({**row, "reason": "root-limit"})
                    continue
                rows[row["path"]] = row
                read_roots.add(row["path"])
            derived = _derived_from_record({"granted": list(rows.values()),
                                           "skipped": skipped[:MAX_DERIVATION_SKIPPED]})
    else:
        derived = derive_task_access(route, context, writable=named, write=node == "owner",
                                    read_limit=MAX_READ_ROOTS - int(inventory_root is not None))

    def assemble(derived: DerivedAccess | None) -> dict:
        roots = [str(path) for path in _unique_paths(named)]
        justification = {root: "Directly named approved task target" for root in roots}
        if run_root is not None:
            justification[str(run_root)] = "Compute-hosts inventory run_root for lab resource work"
        if supplied is not None:
            justification.update(dict(supplied.justification))
        read = [str(path) for path in supplied.read_roots] if supplied else []
        if inventory_root is not None:
            read.append(str(inventory_root))
            justification[str(inventory_root)] = COMPUTE_INVENTORY_REASON
        if derived is not None:
            roots = [str(path) for path in _unique_paths((*named, *derived.writable_roots))]
            read = [str(path) for path in _unique_paths((*map(Path, read), *derived.read_roots))]
            for root, text in derived.justification:
                justification.setdefault(root, text)
        return {
            "schema_version": SCHEMA_VERSION,
            "writable_roots": roots,
            "read_roots": read,
            "network": {
                "required": supplied.network_required if supplied else bool(compute_scope),
                "reason": supplied.network_reason if supplied else (
                    COMPUTE_NETWORK_REASON if compute_scope else ""),
                "hosts": list(supplied.network_hosts) if supplied else [],
            },
            "enforcement_required": supplied.enforcement_required if supplied else "any",
            "justification": justification,
        }

    request = assemble(derived)
    # Use the same validator as load_request before publishing a prepared file.
    # Inventory defaults never bypass broad-root, symlink or request limits.
    try:
        validated = _validate_request(request, source=request_path_value, context=context)
    except ExecutionAccessError as exc:
        if derived is None or not (derived.writable_roots or derived.read_roots):
            raise
        # A derived root never adds a refusal: keep what was named explicitly.
        retained = (prior.get("derivation") or {}).get("granted", []) if upgrade_reads else []
        derived = _derived_from_record({**derived.record, "granted": retained, "dropped": exc.reason})
        request = assemble(derived)
        validated = _validate_request(request, source=request_path_value, context=context)
    if targets is None and run_root is None and not (request["writable_roots"] or request["read_roots"]):
        return None
    request_bytes = _canonical_json_bytes(request)
    request_digest = validated.request_sha256
    binding = {
        "schema_version": 1,
        "read_inputs_version": READ_INPUTS_VERSION,
        "route_id": route_id,
        "route_hash": route_hash,
        "artifact_root": str(Path(str(route.get("artifact_root") or "")).resolve(strict=False)),
        "manifest_path": str(targets.manifest_path) if targets else None,
        "manifest_sha256": targets.manifest_sha256 if targets else None,
        "selected_names": list(targets.selected_names) if targets else [],
        "writable_roots": request["writable_roots"],
        "request_sha256": request_digest,
    }
    if derived is not None and any(derived.record.get(key) for key in ("granted", "skipped", "dropped")):
        binding["derivation"] = derived.record
    binding_bytes = (json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if max(len(request_bytes), len(binding_bytes)) > MAX_REQUEST_BYTES:
        raise ExecutionAccessError("execution-access-unreadable", "prepared access exceeds the bounded cache size")
    try:
        existing_request = _read_bounded_regular_file(request_path_value, MAX_REQUEST_BYTES) if request_path_value.exists() else None
        existing_binding = _read_bounded_regular_file(binding_path, MAX_REQUEST_BYTES) if binding_path.exists() else None
    except OSError as exc:
        raise ExecutionAccessError("execution-access-cache-conflict", "prepared request cache is unreadable") from exc
    if existing_request is not None or existing_binding is not None:
        if existing_request != request_bytes or existing_binding != binding_bytes:
            raise ExecutionAccessError(
                "execution-access-cache-conflict",
                "the route binding or execution access input changed after request preparation",
            )
        return request_path_value
    _atomic_write(request_path_value, request_bytes)
    _atomic_write(binding_path, binding_bytes)
    if node == "owner" and derived is not None and derived.record.get("granted"):
        # The same record a parent's later request joins, with where these roots came from.
        import route_authority
        try:
            harness, session = route_authority.caller_identity()
            route_authority.record_access_change(
                route, request=normalized_request(validated), request_sha256=request_digest,
                by={"harness": harness, "session_id": session}, source="derived")
        except (OSError, ValueError):
            pass   # the prepared binding keeps the evidence either way
    return request_path_value


@dataclass(frozen=True)
class ExecutionAccessGrant:
    request_sha256: str
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    additional_writable_roots: tuple[Path, ...]
    absorbed_writable_roots: tuple[Path, ...]
    network: str
    file_enforcement: str
    network_enforcement: str
    unmet: tuple[str, ...]
    source_path: Path | None = None
    enclosing_parent_attempt_id: str | None = None
    enclosing_parent_network_allowed: bool = False
    # How strongly the read-only roots stay unwritten: the OS sandbox, the tool
    # permission rules that carry them, or nothing; and the read-only roots that
    # lie outside every writable area, the ones an adapter keeps unwritten.
    read_enforcement: str = "none"
    unwritable_read_roots: tuple[Path, ...] = ()


def request_path(
    cli_value: str | None, environ: Mapping[str, str] | None = None
) -> Path | None:
    """Resolve the sole request surface without touching the file."""

    env = os.environ if environ is None else environ
    value = (
        cli_value
        if cli_value is not None
        else env.get("AGENT_DISPATCH_EXECUTION_ACCESS_FILE")
    )
    return Path(value) if value is not None else None


def _safe_subject(value: object) -> str:
    raw = str(value)
    if raw and len(raw) <= 160 and _PIPE_VALUE.fullmatch(raw):
        return raw
    return "sha256-" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _reject(prefix: str, subject: object, detail: str) -> None:
    raise ExecutionAccessError(f"{prefix}:{_safe_subject(subject)}", detail)


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionAccessError(
                "execution-access-invalid-json", f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _json_depth_is_bounded(raw: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in (ord("["), ord("{")):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif byte in (ord("]"), ord("}")):
            depth = max(0, depth - 1)
    return True


def _read_request(path: Path) -> object:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError("request must be a non-symlink regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise OSError("request must be a non-symlink regular file")
        if info.st_size > MAX_REQUEST_BYTES:
            raise OSError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_REQUEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_REQUEST_BYTES:
            raise OSError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-unreadable", f"request file is not safely readable: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not _json_depth_is_bounded(raw):
        raise ExecutionAccessError(
            "execution-access-invalid-json",
            f"request JSON exceeds maximum nesting depth {MAX_JSON_DEPTH}",
        )
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except ExecutionAccessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExecutionAccessError(
            "execution-access-invalid-json", "request file is not valid UTF-8 JSON"
        ) from exc


def _field_error(field: str, detail: str) -> None:
    raise ExecutionAccessError(
        f"execution-access-field-invalid:{_safe_subject(field)}", detail
    )


def _one_line(value: object, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        _field_error(field, f"{field} must be a string")
    if (not allow_empty and not value) or len(value) > MAX_TEXT_LENGTH:
        _field_error(field, f"{field} has invalid length")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _field_error(field, f"{field} must be one line")
    return value


def _validate_path_text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        _reject("execution-access-path-invalid", value, "path must be a bounded string")
    if _URI.match(value) or value.startswith("~") or _PATH_BAD.search(value):
        _reject("execution-access-path-invalid", value, "path syntax is not allowed")
    pure = PurePath(value)
    if not pure.is_absolute() or ".." in pure.parts:
        _reject("execution-access-path-invalid", value, "path must be exact and absolute")
    return value


def _raw_path_list(value: object, field: str) -> list[Path]:
    if not isinstance(value, list):
        _field_error(field, f"{field} must be a list")
    if any(not isinstance(item, str) for item in value):
        _field_error(field, f"{field} items must be strings")
    return [Path(item) for item in value]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _proper_ancestor(candidate: Path, target: Path) -> bool:
    return candidate != target and _is_within(target, candidate)


def _is_top_level(path: Path) -> bool:
    return path == Path("/") or len(path.parts) == 2


def _broad_root(path: Path, context: AccessContext) -> bool:
    candidate = path.resolve(strict=False)
    if _is_top_level(candidate) or candidate == context.home:
        return True
    exact_forbidden = (
        context.dispatch_state_root,
        context.agent_home,
        *context.config_roots,
    )
    if candidate in exact_forbidden:
        return True
    sensitive = (
        context.home,
        context.dispatch_state_root,
        context.agent_home,
        context.worktree,
        context.artifact_root,
        *context.config_roots,
    )
    return any(_proper_ancestor(candidate, target) for target in sensitive)


def _resolve_request_path(path: Path) -> Path:
    """``Path.resolve(strict=False)`` that still fails on a symlink loop.

    Python 3.13 stopped raising ``RuntimeError`` for a loop in non-strict mode
    and returns the unresolved path instead, so a loop would pass as an exact
    future leaf.  A fully resolved path can never fail with ``ELOOP``.
    """
    resolved = path.resolve(strict=False)
    try:
        os.stat(resolved)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"symlink loop from {str(path)!r}") from exc
    return resolved


def _resolve_paths(raw: list[Path]) -> list[tuple[Path, Path]]:
    resolved_by_literal: list[tuple[Path, Path]] = []
    for literal in raw:
        # Literal and resolved paths are independently subject to the format
        # and broad-root checks.  ``strict=False`` deliberately catches links
        # through existing prefixes while permitting an exact future leaf.
        _validate_path_text(str(literal))
        try:
            resolved = _resolve_request_path(literal)
        except (OSError, RuntimeError, ValueError) as exc:
            _reject(
                "execution-access-path-invalid",
                literal,
                f"path cannot be resolved safely: {type(exc).__name__}",
            )
        _validate_path_text(str(resolved))
        resolved_by_literal.append((literal, resolved))

    return resolved_by_literal


def _validate_path_boundaries(
    resolved_by_literal: list[tuple[Path, Path]], context: AccessContext
) -> None:
    """Apply phase 6 symlink checks before phase 7 broad-root checks."""

    for literal, resolved in resolved_by_literal:
        for other_literal, other_resolved in resolved_by_literal:
            if literal == other_literal or not _proper_ancestor(other_literal, literal):
                continue
            if not _is_within(resolved, other_resolved):
                _reject(
                    "execution-access-path-symlink-escape",
                    literal,
                    "a declared descendant resolves outside its declared ancestor",
                )

    for literal, resolved in resolved_by_literal:
        if _broad_root(literal, context):
            _reject("execution-access-root-too-broad", literal, "literal root is too broad")
        if _broad_root(resolved, context):
            _reject("execution-access-root-too-broad", literal, "resolved root is too broad")


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    return [Path(value) for value in sorted({str(Path(path)) for path in paths})]


def _normalize_host(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 260:
        _field_error("network.hosts", "host must be a bounded string")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value) or "," in value or "=" in value:
        _field_error("network.hosts", "host contains an unsafe delimiter")
    host = value
    port_text: str | None = None
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9A-Fa-f:]+)\](?::([0-9]{1,5}))?", value)
        if not match:
            _field_error("network.hosts", "invalid bracketed IPv6 host")
        try:
            host = f"[{ipaddress.IPv6Address(match.group(1)).compressed}]"
        except ipaddress.AddressValueError:
            _field_error("network.hosts", "invalid bracketed IPv6 host")
        port_text = match.group(2)
    elif value.count(":") <= 1:
        host, separator, candidate_port = value.partition(":")
        port_text = candidate_port if separator else None
        dotted = re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", host)
        if dotted:
            try:
                host = str(ipaddress.IPv4Address(host))
            except ipaddress.AddressValueError:
                _field_error("network.hosts", "invalid IPv4 host")
        elif not _HOSTNAME.fullmatch(host):
            _field_error("network.hosts", "invalid host")
        host = host.lower().rstrip(".")
    else:
        _field_error("network.hosts", "IPv6 hosts must be bracketed")
    canonical_port = ""
    if port_text is not None:
        if not re.fullmatch(r"[0-9]{1,5}", port_text):
            _field_error("network.hosts", "port must use ASCII decimal digits")
        port_number = int(port_text, 10)
        if not 1 <= port_number <= 65535:
            _field_error("network.hosts", "invalid port")
        canonical_port = str(port_number)
    return host + (f":{canonical_port}" if canonical_port else "")


def load_request(path: str | Path, *, context: AccessContext) -> ExecutionAccessRequest:
    """Read and validate one ``execution_access_v1`` request in spec order."""

    source = Path(path)
    data = _read_request(source)
    return _validate_request(data, source=source, context=context)


def normalized_request(request: ExecutionAccessRequest) -> dict:
    """The canonical request a validated one digests to (`request_sha256`)."""

    return {
        "schema_version": SCHEMA_VERSION,
        "writable_roots": [str(path) for path in request.writable_roots],
        "read_roots": [str(path) for path in request.read_roots],
        "network": {"required": request.network_required, "reason": request.network_reason,
                    "hosts": list(request.network_hosts)},
        "enforcement_required": request.enforcement_required,
        "justification": dict(request.justification),
    }


def _validate_request(
    data: object, *, source: Path, context: AccessContext
) -> ExecutionAccessRequest:
    """Shared validation for explicit files and normally prepared lab requests."""

    if not isinstance(data, dict):
        _field_error("request", "top-level JSON must be an object")

    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        rendered = version if type(version) is int else "unknown"
        raise ExecutionAccessError(
            f"execution-access-schema-unsupported:v{rendered}",
            "only execution_access_v1 schema_version=1 is supported",
        )

    unknown = next((key for key in data if key not in _TOP_LEVEL_FIELDS), None)
    if unknown is not None:
        _field_error(str(unknown), "unknown top-level field")
    for required in ("writable_roots", "read_roots", "network"):
        if required not in data:
            _field_error(required, "required field is missing")

    writable_raw = _raw_path_list(data["writable_roots"], "writable_roots")
    read_raw = _raw_path_list(data["read_roots"], "read_roots")

    network = data["network"]
    if not isinstance(network, dict):
        _field_error("network", "network must be an object")
    unknown_network = next((key for key in network if key not in _NETWORK_FIELDS), None)
    if unknown_network is not None:
        _field_error(f"network.{unknown_network}", "unknown network field")
    required = network.get("required", False)
    if type(required) is not bool:
        _field_error("network.required", "network.required must be boolean")
    reason = _one_line(network.get("reason", ""), "network.reason")
    hosts_value = network.get("hosts", [])
    if not isinstance(hosts_value, list) or len(hosts_value) > MAX_HOSTS:
        _field_error("network.hosts", f"network.hosts must contain at most {MAX_HOSTS} items")
    hosts = tuple(sorted(set(_normalize_host(host) for host in hosts_value)))
    if not required and (reason or hosts):
        _field_error("network", "reason/hosts require network.required=true")
    if required and not reason:
        _field_error("network.reason", "required network access needs a one-line reason")

    enforcement = data.get("enforcement_required", "any")
    if not isinstance(enforcement, str) or enforcement not in {"any", "os-sandbox"}:
        _field_error("enforcement_required", "expected any or os-sandbox")

    justification_value = data.get("justification", {})
    if not isinstance(justification_value, dict):
        _field_error("justification", "justification must be an object")
    justification_raw: list[tuple[Path, str]] = []
    for key, value in justification_value.items():
        if not isinstance(key, str):
            _field_error("justification", "justification keys must be paths")
        justification_raw.append(
            (Path(key), _one_line(value, f"justification.{key}", allow_empty=False))
        )

    # Phase ⑤ begins only after every unknown-key and type check in phase ④.
    if len(writable_raw) > MAX_ROOTS or len(read_raw) > MAX_READ_ROOTS:
        _reject(
            "execution-access-path-invalid",
            "root-count",
            f"at most {MAX_ROOTS} writable and {MAX_READ_ROOTS} read roots are supported",
        )
    for raw_path in (*writable_raw, *read_raw):
        _validate_path_text(str(raw_path))
    for raw_path, _ in justification_raw:
        _validate_path_text(str(raw_path))

    # Resolution and broad-root checks happen only after all phase ⑤ syntax
    # checks have passed, preserving first-failure semantics and partial grant 0.
    resolved_paths = _resolve_paths([*writable_raw, *read_raw])
    _validate_path_boundaries(resolved_paths, context)
    writable_count = len(writable_raw)
    writable = tuple(
        _unique_paths(resolved for _, resolved in resolved_paths[:writable_count])
    )
    read = tuple(
        _unique_paths(resolved for _, resolved in resolved_paths[writable_count:])
    )
    declared = set(writable) | set(read)
    justification: dict[str, str] = {}
    for raw_path, text in justification_raw:
        try:
            resolved = _resolve_request_path(raw_path)
        except (OSError, RuntimeError, ValueError) as exc:
            _reject(
                "execution-access-path-invalid",
                raw_path,
                f"justification path cannot be resolved safely: {type(exc).__name__}",
            )
        if resolved not in declared:
            _field_error("justification", "justification key is not a declared root")
        justification[str(resolved)] = text

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "writable_roots": [str(path) for path in writable],
        "read_roots": [str(path) for path in read],
        "network": {"required": required, "reason": reason, "hosts": list(hosts)},
        "enforcement_required": enforcement,
        "justification": dict(sorted(justification.items())),
    }
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    try:
        source_path = source.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-unreadable",
            f"request path cannot be resolved safely: {type(exc).__name__}",
        ) from exc
    return ExecutionAccessRequest(
        writable_roots=writable,
        read_roots=read,
        network_required=required,
        network_reason=reason,
        network_hosts=hosts,
        enforcement_required=enforcement,
        justification=tuple(sorted(justification.items())),
        request_sha256=digest,
        source_path=source_path,
    )


def _covered(path: Path, roots: Iterable[Path]) -> bool:
    return any(_is_within(path, root.resolve(strict=False)) for root in roots)


def assert_within_parent(
    request: ExecutionAccessRequest,
    parent: ParentGrant | None,
    *,
    is_child: bool,
) -> None:
    """Enforce child ⊆ parent without interpreting missing context as unlimited."""

    if not is_child:
        return
    if parent is None:
        raise ExecutionAccessError(
            "execution-access-exceeds-parent:parent-grant-unknown",
            "parent effective grant is unavailable; restart with the request at the top-level launch",
        )
    for root in request.writable_roots:
        if not _covered(root, parent.writable_roots):
            _reject(
                "execution-access-exceeds-parent",
                root,
                f"writable root exceeds {parent.boundary}; change the top-level launch request",
            )
    for root in request.read_roots:
        if not _covered(root, (*parent.read_roots, *parent.writable_roots)):
            _reject(
                "execution-access-exceeds-parent",
                root,
                f"read root exceeds {parent.boundary}; change the top-level launch request",
            )
    if request.network_required and not parent.network_allowed:
        raise ExecutionAccessError(
            "execution-access-exceeds-parent:network",
            f"network exceeds {parent.boundary}; change the top-level launch request",
        )


def build_grant(
    request: ExecutionAccessRequest,
    *,
    runtime: str,
    default_writable_roots: Iterable[str | Path] = (),
    network_available: bool = False,
    effective_sandbox: str = "workspace-write",
    gpu_resource_scope: bool = False,
    enclosing_parent: ParentGrant | None = None,
) -> ExecutionAccessGrant:
    """Compute the effective explicit grant; never create runtime argv.

    How a runtime realizes the request is its adapter's declaration
    (`harness_capabilities` `access`); nothing here branches on a harness name."""

    if runtime not in _RUNTIMES:
        raise ExecutionAccessError(
            f"execution-access-enforcement-unavailable:{_safe_subject(runtime)}",
            "runtime has no execution access projection",
        )
    harness = _runtime_harness(runtime)
    sandboxed = _os_sandboxed(runtime)
    defaults = tuple(Path(path).resolve(strict=False) for path in default_writable_roots)
    absorbed = tuple(root for root in request.writable_roots if _covered(root, defaults))
    additional = tuple(root for root in request.writable_roots if not _covered(root, defaults))

    if enclosing_parent is not None:
        if (not sandboxed or effective_sandbox != "danger-full-access"
                or not _os_sandboxed(enclosing_parent.runtime)
                or enclosing_parent.sandbox != "workspace-write"
                or enclosing_parent.file_enforcement != "os-sandbox"
                or enclosing_parent.network_enforcement not in ("os-sandbox", "none")
                or not re.fullmatch(r"[A-Za-z0-9._-]+", enclosing_parent.attempt_id)):
            raise ExecutionAccessError(
                f"execution-access-enforcement-unavailable:{harness}-parent-sandbox",
                "the enclosing parent has no checked workspace-write OS boundary",
            )
        assert_within_parent(request, enclosing_parent, is_child=True)
        for root in defaults:
            if not _covered(root, enclosing_parent.writable_roots):
                _reject("execution-access-exceeds-parent", root,
                        "default writable root exceeds the enclosing parent grant")
        # The outer sandbox already projects these roots. Inner --add-dir does
        # not establish a new grant when the inner mount sandbox is disabled.
        absorbed, additional = request.writable_roots, ()
        network_available = enclosing_parent.network_allowed

    if sandboxed:
        file_grade = (enclosing_parent.file_enforcement if enclosing_parent else
                      "os-sandbox" if effective_sandbox == "workspace-write" else "none")
        network_grade = (enclosing_parent.network_enforcement if enclosing_parent else
                         "os-sandbox" if effective_sandbox == "workspace-write" else "none")
        # The sandbox reads everywhere; read-only roots stay unwritten while it confines writes.
        read_grade = (enclosing_parent.file_enforcement if enclosing_parent else
                      "os-sandbox" if effective_sandbox in ("workspace-write", "read-only") else "none")
    else:
        file_grade = read_grade = "tool-permission"
        network_grade = "none"

    unmet: list[str] = []
    # A read-only root that holds or lies inside a writable area stays writable there.
    writable_areas = (*defaults, *request.writable_roots,
                      *(enclosing_parent.writable_roots if enclosing_parent else ()))
    unwritable = tuple(root for root in request.read_roots
                       if not any(_covered(root, (area,)) or _covered(area, (root,)) for area in writable_areas))
    if request.read_roots and read_grade == "none":
        unmet.append("read-only-unenforced")
    elif len(unwritable) != len(request.read_roots):
        unmet.append("read-only-root-writable")
        read_grade = "none"
    gpu_logical = (gpu_resource_scope and sandboxed
                   and effective_sandbox == "danger-full-access"
                   and request.enforcement_required == "any")
    if request.writable_roots and sandboxed and file_grade == "none" and not gpu_logical:
        sandbox_subject = (
            f"{harness}-read-only"
            if effective_sandbox == "read-only"
            else f"{harness}-file-sandbox"
        )
        raise ExecutionAccessError(
            f"execution-access-enforcement-unavailable:{sandbox_subject}",
            "the effective sandbox cannot project requested writable roots",
        )
    if (request.writable_roots or gpu_logical) and file_grade == "none":
        unmet.append("file-enforcement-none")
    if gpu_logical:
        unmet.append("network-enforcement-none")

    if request.network_required:
        if sandboxed:
            if not network_available or (network_grade != "os-sandbox" and not gpu_logical):
                raise ExecutionAccessError(
                    f"execution-access-enforcement-unavailable:{harness}-network-role-gated",
                    "network is outside the current launch policy; change the top-level launch request/role",
                )
            if gpu_logical:
                network = "granted-unenforced"
                unmet.append(f"network-unenforced-{harness}")
                if request.network_hosts:
                    unmet.append("network-hosts-unenforced")
            elif request.network_hosts:
                if request.enforcement_required == "os-sandbox":
                    raise ExecutionAccessError(
                        f"execution-access-enforcement-unavailable:{harness}-network-hosts",
                        "a boolean sandbox network switch cannot enforce the requested host allowlist",
                    )
                network = "granted-unenforced"
                unmet.append("network-hosts-unenforced")
            else:
                network = "enforced"
        else:
            network = "granted-unenforced"
            unmet.append(f"network-unenforced-{harness}")
    else:
        network = "not-requested"

    relevant_grades = []
    if request.writable_roots:
        relevant_grades.append(file_grade)
    if request.read_roots:
        relevant_grades.append(read_grade)
    if request.network_required:
        relevant_grades.append(network_grade)
    if request.enforcement_required == "os-sandbox":
        if any(grade != "os-sandbox" for grade in relevant_grades):
            raise ExecutionAccessError(
                f"execution-access-enforcement-unavailable:{runtime}",
                "the requested axes are not enforced by an OS sandbox on this runtime",
            )

    return ExecutionAccessGrant(
        request_sha256=request.request_sha256,
        writable_roots=request.writable_roots,
        read_roots=request.read_roots,
        additional_writable_roots=additional,
        absorbed_writable_roots=absorbed,
        network=network,
        file_enforcement=file_grade,
        network_enforcement=network_grade,
        unmet=tuple(sorted(set(unmet))),
        source_path=request.source_path,
        enclosing_parent_attempt_id=enclosing_parent.attempt_id if enclosing_parent else None,
        enclosing_parent_network_allowed=enclosing_parent.network_allowed if enclosing_parent else False,
        read_enforcement=read_grade if request.read_roots else "none",
        unwritable_read_roots=unwritable,
    )


def bind_request(
    cli_value: str | None,
    *,
    environ: Mapping[str, str] | None,
    context: AccessContext,
    is_child: bool,
    parent: ParentGrant | None,
    runtime: str,
    default_writable_roots: Iterable[str | Path] = (),
    network_available: bool = False,
    effective_sandbox: str = "workspace-write",
    gpu_resource_scope: bool = False,
    inherit_parent_sandbox: bool = False,
    compute_execution_scope: bool | None = None,
) -> ExecutionAccessGrant | None:
    """Resolve, validate, constrain, and grade an explicit request.

    The ``None`` fast path is intentionally first and side-effect free so an
    absent request cannot alter legacy adapter assembly.
    """

    source = request_path(cli_value, environ)
    if source is None:
        return None
    request = load_request(source, context=context)
    if compute_execution_scope is False and request.network_reason == COMPUTE_NETWORK_REASON:
        # A non-execution child may inherit the owner's input file, but not its
        # automatic compute defaults. Explicit operator requests retain their
        # existing behavior; this is only the runtime's own default request.
        inventory_roots = {Path(root) for root, reason in request.justification
                           if reason == COMPUTE_INVENTORY_REASON}
        request = replace(request, network_required=False, network_reason="", network_hosts=(),
                          read_roots=tuple(root for root in request.read_roots if root not in inventory_roots))
    assert_within_parent(request, parent, is_child=is_child)
    if inherit_parent_sandbox and (not is_child or parent is None):
        raise ExecutionAccessError("execution-access-exceeds-parent:parent-grant-unknown")
    return build_grant(
        request,
        runtime=runtime,
        default_writable_roots=default_writable_roots,
        network_available=network_available,
        effective_sandbox=effective_sandbox,
        gpu_resource_scope=gpu_resource_scope,
        enclosing_parent=parent if inherit_parent_sandbox else None,
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def publish_effective_grant(
    *,
    jobs: str | Path,
    attempt_id: str,
    route_id: str,
    route_hash: str,
    runtime: str,
    sandbox: str,
    grant: ExecutionAccessGrant | None,
    default_writable_roots: Iterable[str | Path],
    network_allowed: bool,
    execution_selection: Mapping[str, object] | None = None,
) -> tuple[Path, str]:
    """Publish the exact filesystem/network effect used by one attempt."""

    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    if (not re.fullmatch(r"[A-Za-z0-9._-]+", attempt_id)
            or not re.fullmatch(r"[A-Za-z0-9._-]+", route_id)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", route_hash)):
        raise ExecutionAccessError("execution-access-attempt-invalid", "attempt route identity is incomplete")
    defaults = [Path(root).expanduser().resolve(strict=False) for root in default_writable_roots]
    requested = list(grant.writable_roots) if grant is not None else []
    writable = [str(path) for path in _unique_paths((*defaults, *requested))]
    read = [str(path) for path in _unique_paths(grant.read_roots if grant else ())]
    request_file = grant.source_path if grant is not None else None
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "route_id": route_id,
        "route_hash": route_hash,
        "runtime": runtime,
        "sandbox": sandbox,
        "request_path": str(request_file) if request_file else None,
        "request_sha256": grant.request_sha256 if grant else None,
        "writable_roots": writable,
        "read_roots": read,
        "network_allowed": bool(network_allowed or (grant is not None and grant.network == "granted-unenforced")),
        "file_enforcement": grant.file_enforcement if grant else (
            ("os-sandbox" if sandbox == "workspace-write" else "none") if _os_sandboxed(runtime)
            else "tool-permission" if runtime in _RUNTIMES else "none"
        ),
        "network_enforcement": grant.network_enforcement if grant else (
            "os-sandbox" if _os_sandboxed(runtime) and sandbox == "workspace-write" else "none"
        ),
    }
    if grant is not None and grant.read_roots:
        record["read_enforcement"] = grant.read_enforcement
    if grant is not None and grant.enclosing_parent_attempt_id is not None:
        record.update({"boundary": "parent-os-sandbox",
                       "enclosing_parent_attempt_id": grant.enclosing_parent_attempt_id,
                       "network_allowed": grant.enclosing_parent_network_allowed,
                       "os_filesystem_enforced": grant.file_enforcement == "os-sandbox",
                       "os_network_enforced": grant.network_enforcement == "os-sandbox"})
    if (grant is not None and _os_sandboxed(runtime)
            and sandbox == "danger-full-access" and grant.file_enforcement == "none"):
        record.update({"boundary": "logical-request", "unmet": list(grant.unmet),
                       "os_filesystem_enforced": False, "os_network_enforced": False})
        record["network_allowed"] = bool(network_allowed or grant.network == "granted-unenforced")
    if execution_selection is not None and execution_selection.get("gpu_scope") is True:
        record["execution_sandbox_selection"] = dict(execution_selection)
        if _os_sandboxed(runtime) and sandbox == "danger-full-access":
            record.update({"boundary": "logical-request" if grant else "logical-defaults",
                           "os_filesystem_enforced": False, "os_network_enforced": False})
    raw = _canonical_json_bytes(record)
    digest = hashlib.sha256(raw).hexdigest()
    path = state_root / "execution-access" / "attempts" / attempt_id / "effective.json"
    if not _is_within(path.resolve(strict=False), state_root):
        raise ExecutionAccessError("execution-access-record-outside-state", str(path))
    if path.exists():
        try:
            prior = _read_bounded_regular_file(path, MAX_REQUEST_BYTES)
        except ExecutionAccessError:
            raise
        if prior != raw:
            raise ExecutionAccessError(
                "execution-access-record-conflict", "attempt already has a different effective grant"
            )
    else:
        _atomic_write(path, raw)
    return path, digest


def _exact_attempt_metadata(jobs: str | Path, attempt_id: str) -> dict[str, str]:
    matches: list[dict[str, str]] = []
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ExecutionAccessError("execution-access-parent-row-unreadable", str(exc)) from exc
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6 or fields[1] not in {"open", "running"}:
            continue
        try:
            metadata = _pairs_no_duplicates([
                (part.split("=", 1)[0], part.split("=", 1)[1])
                for part in fields[5].split(",") if "=" in part
            ])
        except (ExecutionAccessError, IndexError):
            continue
        if metadata.get("attempt_id") == attempt_id:
            matches.append({str(key): str(value) for key, value in metadata.items()})
    if len(matches) != 1:
        raise ExecutionAccessError(
            "execution-access-parent-row-invalid", f"expected one live attempt row, found {len(matches)}"
        )
    return matches[0]


def load_parent_effective_grant(
    *,
    jobs: str | Path,
    parent_attempt_id: str,
    context: AccessContext,
) -> ParentGrant:
    """Load the effective record published by the exact live parent row."""

    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    row = _exact_attempt_metadata(jobs, parent_attempt_id)
    path_value = row.get("execution_access_effective_file", "")
    digest_value = row.get("execution_access_effective_sha256", "")
    expected_path = state_root / "execution-access" / "attempts" / parent_attempt_id / "effective.json"
    if path_value != str(expected_path) or not re.fullmatch(r"[0-9a-f]{64}", digest_value):
        raise ExecutionAccessError(
            "execution-access-parent-record-missing", "live parent row has no canonical effective record"
        )
    try:
        raw = _read_bounded_regular_file(expected_path, MAX_REQUEST_BYTES)
    except ExecutionAccessError as exc:
        raise ExecutionAccessError("execution-access-parent-record-invalid", exc.detail) from exc
    if hashlib.sha256(raw).hexdigest() != digest_value:
        raise ExecutionAccessError(
            "execution-access-parent-record-digest-mismatch", "effective grant digest does not match live row"
        )
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ExecutionAccessError) as exc:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "effective record is invalid JSON") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "effective record schema is unsupported")
    route_prefix = ""
    if (not row.get("route_id") and not row.get("route_hash")
            and row.get("dispatch_depth") == "1" and row.get("worker_type") == "owner"
            and row.get("unit") == "_kernel/owner" and row.get("owner_route_file")):
        route_prefix = "owner_"
    for key, row_key in (("attempt_id", "attempt_id"),
                         ("route_id", route_prefix + "route_id"),
                         ("route_hash", route_prefix + "route_hash")):
        if not isinstance(record.get(key), str) or not record.get(key) or record[key] != row.get(row_key):
            raise ExecutionAccessError("execution-access-parent-record-identity-mismatch", key)
    if record.get("runtime") not in _RUNTIMES or record.get("sandbox") != row.get("runtime_sandbox"):
        raise ExecutionAccessError("execution-access-parent-record-identity-mismatch", "runtime/sandbox")
    for key in ("writable_roots", "read_roots"):
        roots = record.get(key)
        if (not isinstance(roots, list) or any(not isinstance(root, str) for root in roots)
                or any(not Path(root).is_absolute() or str(Path(root).resolve(strict=False)) != root for root in roots)):
            raise ExecutionAccessError("execution-access-parent-record-invalid", f"invalid {key}")
    if type(record.get("network_allowed")) is not bool:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "network_allowed must be boolean")
    request_path_value = record.get("request_path")
    request_digest = record.get("request_sha256")
    if request_path_value is not None:
        request = load_request(request_path_value, context=context)
        if request.request_sha256 != request_digest:
            raise ExecutionAccessError("execution-access-parent-request-changed", "request digest changed")
    elif request_digest is not None:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "request digest has no request path")
    return ParentGrant(
        writable_roots=tuple(Path(root) for root in record["writable_roots"]),
        read_roots=tuple(Path(root) for root in record["read_roots"]),
        network_allowed=record["network_allowed"],
        attempt_id=record["attempt_id"],
        runtime=record["runtime"],
        sandbox=record["sandbox"],
        file_enforcement=record.get("file_enforcement", "none"),
        network_enforcement=record.get("network_enforcement", "none"),
    )


def receipt_fields(grant: ExecutionAccessGrant) -> dict[str, str]:
    """Return exactly the five pipe-safe registry facts required by SD-141."""

    enforcement = grant.file_enforcement
    if not grant.writable_roots and grant.read_roots:
        enforcement = grant.read_enforcement
    if not grant.writable_roots and not grant.read_roots and grant.network != "not-requested":
        enforcement = grant.network_enforcement
    fields = {
        "execution_access_request": grant.request_sha256[:16],
        "execution_access_roots": str(len(grant.writable_roots)),
        "execution_access_network": grant.network,
        "execution_access_enforcement": enforcement,
        "execution_access_unmet": ";".join(grant.unmet) if grant.unmet else "none",
    }
    if grant.enclosing_parent_attempt_id is not None:
        fields["execution_access_boundary"] = "parent-os-sandbox"
        fields["execution_access_parent_attempt"] = grant.enclosing_parent_attempt_id
    for key, value in fields.items():
        if not _PIPE_VALUE.fullmatch(value):
            raise ExecutionAccessError(
                "execution-access-receipt-unsafe", f"unsafe receipt value for {key}"
            )
    return fields


def receipt_fragment(grant: ExecutionAccessGrant | None) -> str:
    """Render the optional canonical registry suffix."""

    if grant is None:
        return ""
    return "".join(f",{key}={value}" for key, value in receipt_fields(grant).items())


def adapter_default_roots(args: object, *roots: Iterable[str | Path]) -> tuple[Path, ...]:
    """Flatten adapter-computed defaults for absorption tests/builders."""

    values: list[Path] = []
    worktree = getattr(args, "worktree", None)
    if worktree:
        values.append(Path(worktree).resolve(strict=False))
    for group in roots:
        values.extend(Path(value).resolve(strict=False) for value in group)
    artifact = getattr(args, "artifact_root", None)
    if artifact:
        values.append(Path(artifact).resolve(strict=False))
    report = getattr(args, "report_bundle_root", None)
    if report:
        values.append(Path(report).resolve(strict=False))
    runtime = getattr(args, "runtime_dir", None)
    if runtime:
        values.append(Path(runtime).resolve(strict=False))
    return tuple(_unique_paths(values))
