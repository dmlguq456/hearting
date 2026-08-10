#!/usr/bin/env python3
"""Require route and recall-opportunity proof for main-session material work.

The default mode consumes Claude hook JSON.  A small CLI is also exposed for
deterministic conformance tests and adapters that can supply the same fields:

  material-route-guard.py bind --route <route.json> --cwd <dir> --session <id>
  material-route-guard.py check --tool <Edit|Write|Bash> [--file <path>] \
      [--command <shell>] --cwd <dir> --session <id>
  material-route-guard.py check --tool ArtifactWrite --file <artifact> \
      --cwd <project> --session <id>
  material-route-guard.py clear --session <id>

Only a verified capability-route record is routing authority. Main interactive
material work also requires a bounded prompt-probe or explicit recall-gate
receipt; registered route-bound workers remain exempt from main memory lifecycle.
Skill invocation and capability-grounding/spec markers are intentionally not read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, NamedTuple


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR_NAME = ".route-grounding"
MARKER_SCHEMA = 1
MAX_MARKERS = 512
MAX_MARKER_AGE_SECONDS = 14 * 24 * 60 * 60
INTENSITIES = {
    "direct",
    "quick",
    "standard",
    "strong",
    "thorough",
    "adversarial",
}
EDIT_TOOLS = {
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "edit",
    "write",
    "multi_edit",
    "multiedit",
    "notebook_edit",
}
CAPABILITY_ARTIFACT_CAPS = {
    "plans": {"autopilot-code", "audit"},
    "research": {"autopilot-research", "autopilot-refine", "audit"},
    "documents": {"autopilot-draft", "autopilot-refine", "audit"},
    "experiments": {"autopilot-lab", "audit"},
    "spec": {"autopilot-spec", "autopilot-design", "autopilot-ship", "audit"},
    "analysis_project": {"analyze-project", "autopilot-code", "audit"},
}
ROUTABLE_CAPABILITIES = set().union(*CAPABILITY_ARTIFACT_CAPS.values())
SOURCE_SUFFIXES = {
    ".asm", ".bash", ".c", ".cc", ".clj", ".cljs", ".cpp", ".cs",
    ".css", ".cu", ".cuh", ".cxx", ".dart", ".elm", ".erl", ".ex",
    ".exs", ".fish", ".fs", ".fsx", ".go", ".groovy", ".h", ".hh",
    ".hpp", ".hrl", ".htm", ".html", ".hxx", ".ipynb", ".java",
    ".jl", ".js", ".jsx", ".kt", ".kts", ".less", ".lua", ".m",
    ".mm", ".mjs", ".php", ".pl", ".pm", ".proto", ".ps1", ".py",
    ".pyi", ".r", ".rb", ".rs", ".sass", ".scala", ".scss", ".sh",
    ".sol", ".sql", ".svelte", ".swift", ".tcl", ".ts", ".tsx",
    ".vue", ".zig", ".zsh",
}
EXCLUDED_PARTS = {
    ".agent_reports",
    ".claude_reports",
    ".config",
    ".git",
    ".idea",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".route-grounding",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "artifacts",
    "build",
    "config",
    "configs",
    "coverage",
    "dist",
    "docs",
    "documentation",
    "node_modules",
    "scratch",
    "tmp",
    "vendor",
}
DENIAL = (
    "material 작업인데 route 미선언 (silent no-route). direct 실행도 선택된 "
    "capability route를 먼저 compile/bind해야 한다."
)
RECALL_RECEIPT_SCHEMA = 1
RECALL_RECEIPT_MAX_AGE_SECONDS = 24 * 60 * 60


class RouteError(RuntimeError):
    """The presented route proof is missing or invalid."""


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )


def resolve_agent_home(explicit: str | None = None) -> Path:
    for candidate in (explicit, os.environ.get("AGENT_HOME")):
        if candidate and (Path(candidate) / "core" / "CORE.md").is_file():
            return Path(candidate).resolve()
    resolver = ROOT / "utilities" / "agent-home.sh"
    try:
        result = _run([str(resolver)])
        candidate = Path(result.stdout.strip())
        if result.returncode == 0 and (candidate / "core" / "CORE.md").is_file():
            return candidate.resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return ROOT


def session_key(session_id: str) -> str:
    if not session_id or len(session_id.encode("utf-8", "replace")) > 1024:
        raise RouteError("session-id-missing")
    return hashlib.sha256(b"material-route-session-v1\0" + session_id.encode()).hexdigest()


def recall_session_key(session_id: str) -> str:
    if not session_id or len(session_id.encode("utf-8", "replace")) > 1024:
        raise RouteError("recall-session-id-missing")
    return hashlib.sha256(
        b"memory-recall-opportunity-v1\0" + session_id.encode("utf-8", "replace")
    ).hexdigest()


def recall_turn_digest(turn_id: str) -> str:
    if not turn_id:
        return ""
    return hashlib.sha256(
        b"memory-recall-turn-v1\0" + turn_id.encode("utf-8", "replace")
    ).hexdigest()


def _is_tool_result_user_row(row: dict[str, Any]) -> bool:
    """Return whether a Claude ``type:user`` row is a tool result, not a prompt."""
    message = row.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    blocks = content if isinstance(content, list) else [content]
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in blocks
    )


def transcript_turn_id(path_value: object) -> str:
    """Derive the current Claude turn from the bounded tail of its transcript."""
    if not isinstance(path_value, str) or not path_value:
        return ""
    path = Path(path_value)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return ""
        with path.open("rb") as handle:
            start = max(0, info.st_size - 1024 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if (not isinstance(row, dict) or row.get("type") != "user"
                or row.get("isSidechain") is True
                or _is_tool_result_user_row(row)):
            continue
        uid = row.get("uuid")
        if isinstance(uid, str) and uid:
            return f"transcript-user:{uid}"
        material = json.dumps(
            [row.get("timestamp"), row.get("message")],
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return "transcript-user-hash:" + hashlib.sha256(material.encode()).hexdigest()
    return ""


def recall_receipt_dir() -> Path:
    explicit = os.environ.get("MEM_RECALL_RECEIPTS")
    if explicit:
        return Path(explicit)
    state = Path(os.environ.get(
        "XDG_STATE_HOME", Path.home() / ".local" / "state"
    ))
    return state / "agent-memory" / "recall-opportunities"


def recall_receipt_path(session_id: str) -> Path:
    return recall_receipt_dir() / f"{recall_session_key(session_id)}.json"


def state_dir(agent_home: Path) -> Path:
    return agent_home / STATE_DIR_NAME


def marker_path(agent_home: Path, session_id: str) -> Path:
    return state_dir(agent_home) / f"{session_key(session_id)}.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    data = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def gc_markers(agent_home: Path, keep: Path | None = None) -> None:
    directory = state_dir(agent_home)
    try:
        entries = [
            item for item in directory.iterdir()
            if item.name.endswith(".json") and not item.is_symlink()
        ]
    except OSError:
        return
    now = time.time()
    ranked: list[tuple[float, Path]] = []
    for item in entries:
        try:
            info = item.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        ranked.append((info.st_mtime, item))
    ranked.sort(reverse=True)
    for index, (mtime, item) in enumerate(ranked):
        if item == keep:
            continue
        if now - mtime > MAX_MARKER_AGE_SECONDS or index >= MAX_MARKERS:
            try:
                item.unlink()
            except OSError:
                pass


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def git_root(path: Path) -> Path | None:
    nearest = _nearest_existing(path)
    probe = nearest if nearest.is_dir() else nearest.parent
    result = _run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"])
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def project_root(cwd: Path, target: Path | None = None) -> Path:
    return git_root(target or cwd) or git_root(cwd) or cwd.resolve()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_material_source(path: Path, repo: Path | None = None) -> bool:
    path = path.resolve(strict=False)
    repo = repo or git_root(path)
    # Scratch and non-project paths are deliberately outside this gate.
    if repo is None or not _within(path, repo):
        return False
    relative = path.relative_to(repo)
    if any(part.lower() in EXCLUDED_PARTS for part in relative.parts[:-1]):
        return False
    if path.suffix.lower() in SOURCE_SUFFIXES:
        return True
    try:
        return path.is_file() and not path.suffix and os.access(path, os.X_OK)
    except OSError:
        return False


def current_commit(root: Path) -> str:
    result = _run(["git", "-C", str(root), "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else "unversioned"


def _first_parent_descendant(root: Path, source_commit: str, head: str) -> bool:
    """SD-67: a moved HEAD is mid-cycle progress, not a stale route, when it is
    a first-parent descendant of the pinned ``source_commit``.

    ``worker-route-guard.py`` accepts exactly this lineage for declared retry
    boundaries; this guard denying it froze every multi-commit route after its
    first commit — each later material edit and ``git commit`` died
    ``route-source-commit-stale`` for the rest of the cycle (observed
    2026-08-07, dispatch-orphan-fixes owner). Rewritten, reset, or divergent
    history is still stale: only the same line of work, advanced, passes.
    """
    if not source_commit or head == "unversioned":
        return False
    result = _run(["git", "-C", str(root), "rev-list", "--first-parent", head])
    return result.returncode == 0 and source_commit in result.stdout.split()


def _load_route(path: Path) -> dict[str, Any]:
    try:
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 2_000_000:
            raise RouteError("route-file-unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RouteError("route-file-unreadable") from exc
    if not isinstance(value, dict):
        raise RouteError("route-record-invalid")
    return value


def _verifier_crashed(result: subprocess.CompletedProcess[str]) -> bool:
    """True when the verifier died instead of reaching a verdict.

    A contract rejection exits with a documented code and prints one
    ``capability-route: <reason>`` line; an interpreter-level failure prints a
    traceback.  Only the latter is worth retrying, and only the latter must not be
    reported to the user as a bad route record.
    """
    return "Traceback (most recent call last)" in (result.stderr or "")


def verify_route(
    route_file: Path,
    expected_root: Path,
    agent_home: Path,
    *,
    expected_route_id: str | None = None,
    expected_node: str | None = None,
    accepted_capabilities: set[str] | None = None,
) -> dict[str, Any]:
    if route_file.is_symlink():
        raise RouteError("route-file-unsafe")
    route_file = route_file.resolve(strict=False)
    route = _load_route(route_file)
    capabilities = accepted_capabilities or {"autopilot-code"}
    if route.get("capability") not in capabilities:
        raise RouteError("route-capability-not-accepted")
    if route.get("effective_intensity") not in INTENSITIES:
        raise RouteError("route-intensity-invalid")
    route_cwd = Path(str(route.get("cwd", ""))).resolve(strict=False)
    if route_cwd != expected_root.resolve():
        raise RouteError("route-cwd-mismatch")
    artifact_root = Path(str(route.get("artifact_root", ""))).resolve(strict=False)
    if not artifact_root.is_absolute() or not _within(route_file, artifact_root):
        raise RouteError("route-file-outside-artifact-root")
    verifier = agent_home / "utilities" / "capability-route.py"
    if not verifier.is_file():
        raise RouteError("route-verifier-unavailable")
    # The verifier is the live repo file, not an installed copy (`~/.claude/utilities`
    # symlinks straight back here), so a parallel session editing the harness can be
    # observed mid-write.  That surfaces as an interpreter-level crash, which is not
    # evidence that this route record is bad — retry once, then say which of the two
    # actually happened instead of blaming the record.
    command = [
        sys.executable,
        str(verifier),
        "verify",
        "--route", str(route_file),
        "--cwd", str(expected_root),
    ]
    result = _run(command)
    if result.returncode and _verifier_crashed(result):
        result = _run(command)
    if result.returncode:
        if _verifier_crashed(result):
            raise RouteError("route-verifier-crashed")
        raise RouteError("route-record-verification-failed")
    head = current_commit(expected_root)
    if route.get("source_commit") != head and not _first_parent_descendant(
        expected_root, str(route.get("source_commit") or ""), head
    ):
        raise RouteError("route-source-commit-stale")
    if expected_route_id and route.get("route_id") != expected_route_id:
        raise RouteError("route-id-mismatch")
    if expected_node:
        nodes = route.get("nodes")
        if not isinstance(nodes, list) or expected_node not in {
            node.get("id") for node in nodes if isinstance(node, dict)
        }:
            raise RouteError("route-node-mismatch")
    return route


def bind_route(
    route_file: Path,
    cwd: Path,
    session_id: str,
    agent_home: Path,
) -> dict[str, Any]:
    root = project_root(cwd)
    route = verify_route(
        route_file,
        root,
        agent_home,
        accepted_capabilities=ROUTABLE_CAPABILITIES,
    )
    path = marker_path(agent_home, session_id)
    marker = {
        "schema_version": MARKER_SCHEMA,
        "session_key": session_key(session_id),
        "route_file": str(route_file.resolve()),
        "route_id": route["route_id"],
        "route_hash": route["route_hash"],
        "cwd": str(root),
        "source_commit": route["source_commit"],
        "created_at_ns": time.time_ns(),
    }
    _atomic_json(path, marker)
    gc_markers(agent_home, keep=path)
    return marker


def clear_route(session_id: str, agent_home: Path) -> None:
    try:
        path = marker_path(agent_home, session_id)
        if not path.is_symlink():
            path.unlink(missing_ok=True)
    except (OSError, RouteError):
        pass
    try:
        receipt = recall_receipt_path(session_id)
        if not receipt.is_symlink():
            receipt.unlink(missing_ok=True)
    except (OSError, RouteError):
        pass
    gc_markers(agent_home)


def session_route(
    session_id: str,
    root: Path,
    agent_home: Path,
    *,
    accepted_capabilities: set[str] | None = None,
) -> dict[str, Any]:
    path = marker_path(agent_home, session_id)
    try:
        if path.is_symlink() or path.stat().st_size > 16_384:
            raise RouteError("session-marker-unsafe")
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RouteError("session-route-missing") from exc
    if not isinstance(marker, dict) or marker.get("schema_version") != MARKER_SCHEMA:
        raise RouteError("session-marker-invalid")
    if marker.get("session_key") != session_key(session_id):
        raise RouteError("session-marker-foreign")
    if Path(str(marker.get("cwd", ""))).resolve(strict=False) != root.resolve():
        raise RouteError("session-marker-cwd-mismatch")
    route_file = Path(str(marker.get("route_file", "")))
    route = verify_route(
        route_file,
        root,
        agent_home,
        expected_route_id=str(marker.get("route_id", "")),
        accepted_capabilities=accepted_capabilities,
    )
    if route.get("route_hash") != marker.get("route_hash"):
        raise RouteError("session-marker-route-hash-mismatch")
    return route


def worker_route(
    root: Path,
    agent_home: Path,
    *,
    accepted_capabilities: set[str] | None = None,
) -> dict[str, Any] | None:
    route_file = os.environ.get("AGENT_ROUTE_FILE", "")
    route_id = os.environ.get("AGENT_ROUTE_ID", "")
    route_node = os.environ.get("AGENT_ROUTE_NODE", "")
    if not route_file and not route_id and not route_node:
        return None
    if not route_file or not route_id:
        raise RouteError("worker-route-binding-incomplete")
    return verify_route(
        Path(route_file),
        root,
        agent_home,
        expected_route_id=route_id,
        expected_node=route_node or None,
        accepted_capabilities=accepted_capabilities,
    )


def active_route(
    session_id: str,
    root: Path,
    agent_home: Path,
    *,
    accepted_capabilities: set[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    worker = worker_route(
        root,
        agent_home,
        accepted_capabilities=accepted_capabilities,
    )
    if worker is not None:
        return worker, True
    return (
        session_route(
            session_id,
            root,
            agent_home,
            accepted_capabilities=accepted_capabilities,
        ),
        False,
    )


def require_route(
    session_id: str,
    root: Path,
    agent_home: Path,
    *,
    accepted_capabilities: set[str] | None = None,
) -> bool:
    _route, is_worker = active_route(
        session_id,
        root,
        agent_home,
        accepted_capabilities=accepted_capabilities,
    )
    return is_worker


def capability_artifact_caps(path: Path) -> set[str] | None:
    """Return capabilities allowed to author one durable artifact bucket."""

    parts = path.resolve(strict=False).parts
    for index, part in enumerate(parts[:-1]):
        if part not in {".agent_reports", ".claude_reports"}:
            continue
        return CAPABILITY_ARTIFACT_CAPS.get(parts[index + 1])
    return None


def require_recall_opportunity(session_id: str, turn_id: str, root: Path) -> None:
    path = recall_receipt_path(session_id)
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            raise RouteError("recall-opportunity-unsafe")
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except RouteError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise RouteError("recall-opportunity-missing") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECALL_RECEIPT_SCHEMA:
        raise RouteError("recall-opportunity-invalid")
    if receipt.get("session_digest") != recall_session_key(session_id):
        raise RouteError("recall-opportunity-foreign")
    if Path(str(receipt.get("cwd", ""))).resolve(strict=False) != root.resolve():
        raise RouteError("recall-opportunity-cwd-mismatch")
    created_at_ns = receipt.get("created_at_ns")
    if not isinstance(created_at_ns, int) or isinstance(created_at_ns, bool):
        raise RouteError("recall-opportunity-time-invalid")
    age_ns = time.time_ns() - created_at_ns
    if age_ns < -300 * 1_000_000_000 or age_ns > RECALL_RECEIPT_MAX_AGE_SECONDS * 1_000_000_000:
        raise RouteError("recall-opportunity-stale")
    source = receipt.get("source")
    if source not in {"candidate-probe", "explicit-recall", "explicit-skip"}:
        raise RouteError("recall-opportunity-source-invalid")
    actual_turn = receipt.get("turn_digest")
    expected_turn = recall_turn_digest(turn_id)
    # Native prompt probes bind to the exact turn. An explicit manual gate may
    # omit a runtime-specific turn id and remains a same-session recovery path.
    if expected_turn and actual_turn and actual_turn != expected_turn:
        raise RouteError("recall-opportunity-turn-mismatch")
    if expected_turn and not actual_turn and source == "candidate-probe":
        raise RouteError("recall-opportunity-turn-missing")
    result_ids = receipt.get("result_ids")
    if (not isinstance(result_ids, list) or len(result_ids) > 3
            or not all(isinstance(item, str) for item in result_ids)):
        raise RouteError("recall-opportunity-results-invalid")
    result_count = receipt.get("result_count")
    if (not isinstance(result_count, int) or isinstance(result_count, bool)
            or result_count != len(result_ids)):
        raise RouteError("recall-opportunity-result-count-invalid")


def _shell_segments(command: str) -> Iterable[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(character in ";&|" for character in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _route_compile_argv(segment: list[str]) -> list[str] | None:
    """Return compile arguments only for an actual router invocation.

    A loose token search lets harmless commands such as
    `echo capability-route.py compile --output old-route.json` bind an old
    record.  Accept only direct execution or a Python interpreter launching
    the router, with optional leading environment assignments/`command`.
    """

    index = 0
    while index < len(segment) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index]
    ):
        index += 1
    if index < len(segment) and segment[index] == "command":
        index += 1
    if index >= len(segment):
        return None
    executable = Path(segment[index]).name
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        index += 1
        if index >= len(segment) or Path(segment[index]).name != "capability-route.py":
            return None
    elif executable != "capability-route.py":
        return None
    index += 1
    if index >= len(segment) or segment[index] != "compile":
        return None
    return segment[index + 1:]


class CompileInvocation(NamedTuple):
    outputs: tuple[Path, ...]
    effective_cwd: Path
    artifact_root: Path | None = None


def _git_common_dir(checkout: Path) -> Path | None:
    try:
        result = _run(["git", "-C", str(checkout), "rev-parse", "--git-common-dir"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    raw = Path(result.stdout.strip())
    return (checkout / raw).resolve(strict=False) if not raw.is_absolute() else raw.resolve(strict=False)


def _trusted_codex_preflight(path: str, command_cwd: Path) -> bool:
    """Accept an exact wrapper in a checkout sharing this harness's Git identity."""
    candidate = Path(os.path.expanduser(path))
    if not candidate.is_absolute():
        candidate = command_cwd / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_file():
        return False
    relative = Path("adapters") / "codex" / "bin" / "preflight.sh"
    try:
        checkout = resolved.parents[3]
        if resolved.relative_to(checkout) != relative:
            return False
    except (IndexError, ValueError):
        return False
    try:
        canonical = (ROOT / relative).resolve(strict=True)
    except OSError:
        canonical = None
    if resolved == canonical:
        return True
    trusted_common = _git_common_dir(ROOT.resolve(strict=True))
    candidate_common = _git_common_dir(checkout)
    return trusted_common is not None and candidate_common == trusted_common


def _codex_route_compile_argv(segment: list[str], command_cwd: Path) -> list[str] | None:
    """Recognize the exact local `preflight.sh route --capability` form."""
    index = 0
    while index < len(segment) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index]
    ):
        index += 1
    if index >= len(segment) or not _trusted_codex_preflight(segment[index], command_cwd):
        return None
    if segment[index + 1:index + 3] != ["route", "--capability"]:
        return None
    return segment[index + 3:]


def route_compile_invocations(command: str, cwd: Path) -> list[CompileInvocation]:
    invocations: list[CompileInvocation] = []
    command_cwd = cwd.resolve(strict=False)
    for segment in _shell_segments(command):
        if segment and segment[0] == "cd" and len(segment) == 2 and not segment[1].startswith("-"):
            command_cwd = _resolve_path(command_cwd, segment[1])
            continue
        tail = _route_compile_argv(segment)
        if tail is None:
            tail = _codex_route_compile_argv(segment, command_cwd)
        if tail is None:
            continue
        outputs: list[Path] = []
        artifact_root: Path | None = None
        for offset, value in enumerate(tail):
            raw = ""
            if value == "--output" and offset + 1 < len(tail):
                raw = tail[offset + 1]
            elif value.startswith("--output="):
                raw = value.split("=", 1)[1]
            if raw:
                path = Path(os.path.expanduser(raw))
                outputs.append(
                    (command_cwd / path).resolve() if not path.is_absolute() else path.resolve()
                )
            root_raw = ""
            if value == "--artifact-root" and offset + 1 < len(tail):
                root_raw = tail[offset + 1]
            elif value.startswith("--artifact-root="):
                root_raw = value.split("=", 1)[1]
            if root_raw:
                root_path = Path(os.path.expanduser(root_raw))
                artifact_root = (
                    (command_cwd / root_path).resolve()
                    if not root_path.is_absolute()
                    else root_path.resolve()
                )
        unique = []
        for path in outputs:
            if path not in unique:
                unique.append(path)
        if unique:
            invocations.append(CompileInvocation(tuple(unique), command_cwd, artifact_root))
        elif artifact_root is not None:
            # SD-2.5: `--output` omitted means compile writes to the canonical
            # default (`<artifact-root>/.runtime/routes/<route_id>.json`), whose
            # route_id is only known after the command runs. PostToolUse resolves
            # it from `tool_response` stdout; this zero-output invocation is the
            # marker that a resolution attempt should happen.
            invocations.append(CompileInvocation((), command_cwd, artifact_root))
    return invocations


def _compiled_route_id(tool_response: object) -> str | None:
    """Read `route_id` from a compile invocation's stdout (the route JSON)."""
    if not isinstance(tool_response, dict):
        return None
    stdout = tool_response.get("stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        route = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    route_id = route.get("route_id") if isinstance(route, dict) else None
    return route_id if isinstance(route_id, str) and route_id else None


def route_compile_outputs(command: str, cwd: Path) -> list[Path]:
    outputs: list[Path] = []
    for invocation in route_compile_invocations(command, cwd):
        outputs.extend(invocation.outputs)
    return outputs


def _resolve_path(base: Path, raw: str) -> Path:
    path = Path(os.path.expanduser(raw))
    return (base / path).resolve(strict=False) if not path.is_absolute() else path.resolve(strict=False)


def _git_commit_segments(
    command: str,
    base: Path,
    *,
    depth: int = 0,
) -> list[tuple[Path, bool, str, list[str]]]:
    if depth > 4:
        return []
    found: list[tuple[Path, bool, str, list[str]]] = []
    current_cwd = base.resolve()
    for segment in _shell_segments(command):
        if not segment:
            continue
        if Path(segment[0]).name in {"sh", "bash", "zsh", "dash", "ksh"}:
            try:
                shell_index = segment.index("-c")
            except ValueError:
                shell_index = -1
            if shell_index >= 0 and shell_index + 1 < len(segment):
                found.extend(
                    _git_commit_segments(
                        segment[shell_index + 1], current_cwd, depth=depth + 1
                    )
                )
                continue
        if segment[0] == "cd" and len(segment) == 2 and not segment[1].startswith("-"):
            current_cwd = _resolve_path(current_cwd, segment[1])
            continue
        git_index = next(
            (index for index, value in enumerate(segment) if Path(value).name == "git"),
            None,
        )
        if git_index is None:
            continue
        index = git_index + 1
        command_cwd = current_cwd
        while index < len(segment):
            token = segment[index]
            if token == "-C" and index + 1 < len(segment):
                command_cwd = _resolve_path(command_cwd, segment[index + 1])
                index += 2
                continue
            if token.startswith("-C") and len(token) > 2:
                command_cwd = _resolve_path(command_cwd, token[2:])
                index += 1
                continue
            if token == "--work-tree" and index + 1 < len(segment):
                command_cwd = _resolve_path(command_cwd, segment[index + 1])
                index += 2
                continue
            if token.startswith("--work-tree="):
                command_cwd = _resolve_path(command_cwd, token.split("=", 1)[1])
                index += 1
                continue
            if token in {"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree"}:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        if index >= len(segment) or segment[index] != "commit":
            continue
        args = segment[index + 1:]
        all_tracked = False
        path_mode = "default"
        paths: list[str] = []
        value_options = {
            "-C", "--reuse-message", "-c", "--reedit-message", "-F", "--file",
            "-m", "--message", "--author", "--date", "--fixup", "--squash",
            "--cleanup", "--trailer", "-S", "--gpg-sign",
        }
        positional = False
        offset = 0
        while offset < len(args):
            token = args[offset]
            if positional:
                paths.append(token)
                offset += 1
                continue
            if token == "--":
                positional = True
                offset += 1
                continue
            if token in {"-a", "--all"} or (
                token.startswith("-") and not token.startswith("--") and "a" in token[1:]
            ):
                all_tracked = True
            if token in {"-i", "--include"}:
                path_mode = "include"
            if token in {"-o", "--only"}:
                path_mode = "only"
            if token.startswith(("--include=", "--only=")):
                path_mode = "include" if token.startswith("--include=") else "only"
                paths.append(token.split("=", 1)[1])
                offset += 1
                continue
            if re.match(r"^-[io].+", token):
                path_mode = "include" if token.startswith("-i") else "only"
                paths.append(token[2:])
                offset += 1
                continue
            if token == "--pathspec-from-file":
                # The referenced path list is intentionally not opened by a
                # hook. Conservatively inspect all staged and tracked changes.
                all_tracked = True
                offset += 2
                continue
            if token.startswith("--pathspec-from-file="):
                all_tracked = True
                offset += 1
                continue
            if token in value_options:
                offset += 2
                continue
            if any(token.startswith(option + "=") for option in value_options if option.startswith("--")):
                offset += 1
                continue
            if token.startswith("-") and not token.startswith("--"):
                # Short options may be bundled (`-am message`).  A value-taking
                # flag at the end consumes the following token; without this,
                # the commit message is misclassified as a pathspec and a real
                # `git commit -am` source change can evade the staged-scope read.
                short = token[1:]
                consumes_next = any(
                    short.endswith(flag) for flag in ("m", "F", "C", "c")
                )
                offset += 2 if consumes_next and offset + 1 < len(args) else 1
                continue
            if token.startswith("-"):
                offset += 1
                continue
            paths.append(token)
            offset += 1
        found.append((command_cwd, all_tracked, path_mode, paths))
    return found


def _diff_entries(repo: Path, *, cached: bool, paths: list[str] | None = None) -> list[tuple[str, list[str]]]:
    command = ["git", "-C", str(repo), "diff"]
    if cached:
        command.append("--cached")
    command += ["--name-status", "-z", "--find-renames=100%"]
    if _run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"]).returncode == 0:
        command.append("HEAD")
    if paths:
        command += ["--", *paths]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
    if result.returncode:
        return []
    fields = result.stdout.split(b"\0")
    entries: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(fields) and fields[index]:
        status_text = fields[index].decode("utf-8", "surrogateescape")
        index += 1
        count = 2 if status_text.startswith(("R", "C")) else 1
        names = [
            fields[index + item].decode("utf-8", "surrogateescape")
            for item in range(count)
            if index + item < len(fields) and fields[index + item]
        ]
        index += count
        entries.append((status_text, names))
    return entries


def commit_has_material(
    repo: Path,
    all_tracked: bool,
    path_mode: str,
    paths: list[str],
) -> bool:
    root = git_root(repo)
    if root is None:
        return False
    if paths and path_mode in {"default", "only"}:
        # Plain pathspecs and --only commit those paths without including an
        # unrelated staged change. Read both index and worktree so an unborn
        # branch cannot hide a staged initial source file.
        entries = _diff_entries(root, cached=True, paths=paths)
        entries += _diff_entries(root, cached=False, paths=paths)
    else:
        entries = _diff_entries(root, cached=True)
        if all_tracked or path_mode == "include":
            entries += _diff_entries(root, cached=False, paths=paths or None)
    for status_text, names in entries:
        if status_text == "R100":
            continue
        if any(is_material_source(root / name, root) for name in names):
            return True
    return False


def check_action(
    tool: str,
    cwd: Path,
    session_id: str,
    agent_home: Path,
    *,
    file_path: str = "",
    command: str = "",
    turn_id: str = "",
) -> None:
    if tool == "ArtifactWrite":
        if not file_path:
            return
        target = _resolve_path(cwd, file_path)
        accepted = capability_artifact_caps(target)
        if not accepted:
            return
        root = project_root(cwd)
        route, is_worker = active_route(
            session_id,
            root,
            agent_home,
            accepted_capabilities=accepted,
        )
        artifact_root = Path(str(route.get("artifact_root", ""))).resolve(
            strict=False
        )
        if not _within(target.resolve(strict=False), artifact_root):
            raise RouteError("route-artifact-root-mismatch")
        if not is_worker:
            require_recall_opportunity(session_id, turn_id, root)
        return
    if tool in EDIT_TOOLS:
        if not file_path:
            return
        target = _resolve_path(cwd, file_path)
        repo = git_root(target)
        if not is_material_source(target, repo):
            return
        root = project_root(cwd, target)
        is_worker = require_route(
            session_id,
            root,
            agent_home,
            accepted_capabilities={"autopilot-code"},
        )
        if not is_worker:
            require_recall_opportunity(session_id, turn_id, root)
        return
    if tool not in {"Bash", "bash", "Shell", "shell"} or not command:
        return
    for command_cwd, all_tracked, path_mode, paths in _git_commit_segments(command, cwd):
        repo = git_root(command_cwd)
        if repo is None or not commit_has_material(repo, all_tracked, path_mode, paths):
            continue
        is_worker = require_route(
            session_id,
            repo,
            agent_home,
            accepted_capabilities={"autopilot-code"},
        )
        if not is_worker:
            require_recall_opportunity(session_id, turn_id, repo)


def deny_json(reason: str) -> None:
    recovery = ""
    if reason.startswith("recall-opportunity"):
        recovery = (
            " 현재 turn의 memory candidate probe가 없거나 오래됐습니다. "
            "prompt hook을 다시 거치거나 mem recall-gate로 recall/skip을 명시하세요."
        )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"{DENIAL}{recovery} [reason={reason}]",
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def hook_main(payload: dict[str, Any], agent_home: Path) -> int:
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve(strict=False)
    session_id = str(payload.get("session_id") or "")
    turn_id = str(payload.get("turn_id") or payload.get("turnID") or "")
    if not turn_id:
        turn_id = transcript_turn_id(
            payload.get("transcript_path") or payload.get("transcriptPath")
        )
    if event == "SessionEnd":
        if session_id:
            clear_route(session_id, agent_home)
        return 0
    if event == "PostToolUse" and (
        tool in {"Bash", "bash", "Shell", "shell", "exec_command", "functions.exec_command"}
        or tool.endswith(".exec_command")
    ):
        invocations = route_compile_invocations(str(tool_input.get("command") or ""), cwd)
        outputs = [path for invocation in invocations for path in invocation.outputs]
        if session_id and len(outputs) == 1 and len(invocations) == 1:
            try:
                bind_route(outputs[0], invocations[0].effective_cwd, session_id, agent_home)
            except (RouteError, OSError, subprocess.SubprocessError):
                pass
        elif (
            session_id
            and len(invocations) == 1
            and not invocations[0].outputs
            and invocations[0].artifact_root is not None
        ):
            # `--output` was omitted, so compile wrote its canonical default. The
            # route_id is only known from the compiled route JSON on stdout; if
            # that is not readable, bind nothing rather than guess (no silent
            # over-binding).
            route_id = _compiled_route_id(payload.get("tool_response"))
            if route_id:
                canonical = (
                    invocations[0].artifact_root / ".runtime" / "routes" / f"{route_id}.json"
                )
                try:
                    bind_route(canonical, invocations[0].effective_cwd, session_id, agent_home)
                except (RouteError, OSError, subprocess.SubprocessError):
                    pass
        return 0
    if event != "PreToolUse":
        return 0
    try:
        check_action(
            tool,
            cwd,
            session_id,
            agent_home,
            file_path=str(
                tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or tool_input.get("path")
                or ""
            ),
            command=str(tool_input.get("command") or ""),
            turn_id=turn_id,
        )
    except RouteError as exc:
        deny_json(str(exc))
    except (OSError, subprocess.SubprocessError):
        deny_json("route-guard-check-failed")
    return 0


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-home")
    sub = parser.add_subparsers(dest="action", required=True)
    bind = sub.add_parser("bind")
    bind.add_argument("--route", required=True)
    bind.add_argument("--cwd", required=True)
    bind.add_argument("--session", required=True)
    check = sub.add_parser("check")
    check.add_argument("--tool", required=True)
    check.add_argument("--file", default="")
    check.add_argument("--command", default="")
    check.add_argument("--cwd", required=True)
    check.add_argument("--session", required=True)
    check.add_argument("--turn", default="")
    clear = sub.add_parser("clear")
    clear.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    agent_home = resolve_agent_home(args.agent_home)
    try:
        if args.action == "bind":
            bind_route(Path(args.route), Path(args.cwd), args.session, agent_home)
        elif args.action == "check":
            check_action(
                args.tool,
                Path(args.cwd),
                args.session,
                agent_home,
                file_path=args.file,
                command=args.command,
                turn_id=args.turn,
            )
        else:
            clear_route(args.session, agent_home)
    except RouteError as exc:
        print(f"{DENIAL} [reason={exc}]", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print(f"{DENIAL} [reason=route-guard-check-failed]", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    return hook_main(payload, resolve_agent_home())


if __name__ == "__main__":
    raise SystemExit(main())
