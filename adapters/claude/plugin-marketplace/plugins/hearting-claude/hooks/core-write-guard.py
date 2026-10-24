#!/usr/bin/env python3
"""The two write gates kept after the 2026-09-28 gate removal.

Refuses a file-editing tool call only when it would

1. edit an installed release copy (``<data>/hearting/releases/...`` or a runtime's
   ``.harness/bundles/...``) — the source checkout is where changes belong; or
2. edit the shared primary checkout from a session working in one of its linked
   worktrees — several live sessions share that checkout. Its artifact root
   (``.agent_reports/``, legacy ``.claude_reports/``) stays writable, because linked
   worktrees record artifacts there by design.

Each refusal is one line naming the path to use instead. Anything this cannot
judge — an unparsable payload, a path outside both rules, an unexpected error — is
allowed. Shell commands are not inspected.

Modes: no argument = Claude PreToolUse (exit 2, reason on stderr);
``--codex`` = Codex PreToolUse (``{"decision": "block"}`` on stdout);
``--check FILE [--cwd DIR]`` = one path (exit 1, reason on stdout; OpenCode).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_ARTIFACT_ROOTS = (".agent_reports", ".claude_reports")
_EDIT_TOOLS = {"Write", "write", "Edit", "edit", "MultiEdit", "multi_edit", "multiedit",
               "NotebookEdit"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
                         re.MULTILINE)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _runtime_override_allowed(candidate: Path, primary: Path) -> bool:
    """Keep explicit runtime overrides outside shared, home, and release trees."""
    return (candidate != primary and not _within(candidate, primary)
            and not _within(candidate, Path.home())
            and not any(_within(candidate, release) for release in _release_roots()))


def _real(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return Path(os.path.realpath(path if path.is_absolute() else base / path))


def _release_roots() -> list[Path]:
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    roots = [data / "hearting" / "releases"]
    roots += [home / runtime / ".harness" / "bundles"
              for runtime in (".claude", ".codex", ".config/opencode")]
    return [Path(os.path.realpath(root)) for root in roots]


def _linked_worktree(cwd: Path):
    """``(worktree, primary checkout)`` when ``cwd`` is inside a linked worktree."""
    for directory in (cwd, *cwd.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return None
        if not marker.is_file():
            continue
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = _real(text.partition(":")[2].strip(), directory)
        if gitdir.parent.name != "worktrees":
            return None
        common = gitdir.parent.parent
        if common.name != ".git":
            return None
        return Path(os.path.realpath(directory)), common.parent
    return None


def violation(target: str, cwd: str) -> str:
    """The one-line refusal for editing ``target`` from ``cwd``, or ``""``."""
    try:
        base = Path(os.path.realpath(cwd or os.getcwd()))
        path = _real(target, base)
        for root in _release_roots():
            if _within(path, root):
                return (f"hearting: {path} is an installed release copy; edit the source "
                        f"checkout and reinstall instead.")
        route_file = os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_OWNER_ROUTE_FILE")
        registered = os.environ.get("AGENT_DISPATCH_REGISTERED_WORKER", "").lower() in {"1", "true"}
        if route_file and registered:
            route_record = None
            try:
                agent_home = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[1])
                sys.path.insert(0, str(agent_home / "utilities"))
                from dispatch_contract import validated_unversioned_route
                route_record = json.loads(Path(route_file).expanduser().read_text(encoding="utf-8"))
                route_cwd = route_record.get("cwd") if isinstance(route_record, dict) else None
                if not isinstance(route_cwd, str) or not route_cwd:
                    raise ValueError("bound route has no cwd")
                route = validated_unversioned_route(route_cwd)
            except Exception:
                route = None
                if any(os.environ.get(key) for key in (
                        "AGENT_ROUTE_ID", "AGENT_OWNER_ROUTE_ID", "AGENT_ROUTE_HASH",
                        "AGENT_OWNER_ROUTE_HASH")):
                    return "hearting: 등록된 route를 검증할 수 없어 원본 편집을 막았습니다. " \
                           "현재 cycle 출력 경로를 확인하고 다시 시작하세요."
            if (route is None and isinstance(route_record, dict)
                    and route_record.get("source_commit") == "unversioned"
                    and any(os.environ.get(key) for key in (
                        "AGENT_ROUTE_ID", "AGENT_OWNER_ROUTE_ID", "AGENT_ROUTE_HASH",
                        "AGENT_OWNER_ROUTE_HASH"))):
                return "hearting: 등록된 route를 검증할 수 없어 원본 편집을 막았습니다. " \
                       "현재 cycle 출력 경로를 확인하고 다시 시작하세요."
            if route is not None:
                route_cwd = Path(os.path.realpath(route.get("cwd", "")))
                artifact = Path(os.path.realpath(route.get("artifact_root", "")))
                lexical = Path(os.path.abspath(
                    Path(target).expanduser() if Path(target).expanduser().is_absolute()
                    else base / Path(target).expanduser()
                ))
                lexical_in_artifact = _within(lexical, artifact)
                resolved_in_artifact = _within(path, artifact)
                if ((lexical_in_artifact != resolved_in_artifact)
                        or ((_within(lexical, route_cwd) or _within(path, route_cwd))
                            and not (lexical_in_artifact and resolved_in_artifact))):
                    output = (os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR")
                              or os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
                              or str(artifact))
                    return (f"hearting: non-Git 기록형 route에서는 원본을 편집할 수 없습니다: {path}. "
                            f"이번 cycle 출력에 쓰세요: {output}")
        linked = _linked_worktree(base)
        if not linked:
            return ""
        worktree, primary = linked
        if _within(path, worktree) or not _within(path, primary):
            return ""
        relative = path.relative_to(primary)
        if relative.parts and relative.parts[0] in _ARTIFACT_ROOTS:
            return ""
        # The project's default runtime sibling is the one narrow additional
        # primary-checkout write surface. realpath resolution above ensures a
        # symlink inside it cannot redirect writes into source or a release.
        runtime = Path(os.path.realpath(primary / ".agent_runtime"))
        if _within(path, runtime):
            return ""
        override = os.environ.get("AGENT_RUNTIME_DIR", "")
        if override:
            try:
                candidate = Path(os.path.realpath(Path(override).expanduser()))
                if (Path(override).is_absolute() and _runtime_override_allowed(candidate, primary)
                        and _within(path, candidate)):
                    return ""
            except (OSError, RuntimeError, ValueError):
                pass
        return (f"hearting: this session works in the worktree {worktree}; edit "
                f"{worktree / relative} instead of the shared checkout {primary}.")
    except Exception:
        return ""


def _mapping(payload: dict, *keys: str) -> dict:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _patch_text(payload) -> str:
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and "*** Begin Patch" in value:
            return value
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return ""


def _targets(payload: dict) -> list[str]:
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    if isinstance(payload.get("tool"), dict):
        tool = tool or payload["tool"].get("name", "")
    args = _mapping(payload, "tool_input", "toolInput", "input", "arguments", "args")
    if tool in _EDIT_TOOLS:
        raw = next((args.get(key) for key in ("file_path", "filePath", "notebook_path", "path")
                    if isinstance(args.get(key), str) and args.get(key)), "")
        return [raw] if raw else []
    if "apply_patch" in str(tool) or tool in {"ApplyPatch", "patch"}:
        text = _patch_text(payload)
        return [(match.group(1) or match.group(2) or "").strip()
                for match in _PATCH_FILE.finditer(text)]
    return []


def _first_violation(payload: dict) -> str:
    cwd = payload.get("cwd") or payload.get("working_directory") or os.getcwd()
    for target in _targets(payload):
        reason = violation(target, str(cwd))
        if reason:
            return reason
    return ""


def main(argv: list[str]) -> int:
    if argv[:1] == ["--check"] and len(argv) >= 2:
        cwd = argv[argv.index("--cwd") + 1] if "--cwd" in argv[:-1] else os.getcwd()
        reason = violation(argv[1], cwd)
        if reason:
            print(reason)
            return 1
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    reason = _first_violation(payload) if isinstance(payload, dict) else ""
    if not reason:
        return 0
    if argv[:1] == ["--codex"]:
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
