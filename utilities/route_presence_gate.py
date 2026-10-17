#!/usr/bin/env python3
"""Route presence gate: the one check behind the route-participation invariant.

Before a session's first material action in a folder it asks one question: does this
session already hold any route for that folder? The folder is the canonical artifact
root of the git work tree (``utilities/artifact-root.sh``). The answer comes from the
session's own route-chain ledger (``tools/fleet/route_chain.py``), which
``capability-route.py compose|compile|continuation|start`` already append to. Any line
naming the root passes, open or closed, so a session×folder that once had a route is
never refused again. Nothing about the route itself is checked.

Material actions are an edit tool on a path inside a git work tree, a shell
``git commit`` while the work tree has non-artifact changes, and a long-run launch
(``compute-hosts run``, ``nohup|setsid … python … train*|run.py``). Shell commands are
recognised only in plain forms; anything ambiguous passes.

Always passes: ``HEARTING_ROUTE_GATE=off``, a registered owner or worker, CI, dev
activation (the active harness checkout is the repository being edited), artifact roots,
the temp directory (scratchpad), runtime homes, paths outside git, and any payload or
record this cannot read.

Modes: ``--claude`` (default; exit 2, reason on stderr), ``--codex``
(``{"decision": "block"}`` on stdout), ``--opencode`` (stdin
``{"tool", "args", "sessionID", "cwd"}``; exit 1, reason on stdout).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWITCH = "HEARTING_ROUTE_GATE"
_OFF = {"off", "0", "false", "no"}
_ARTIFACT_DIRS = (".agent_reports", ".claude_reports")
_EDIT_TOOLS = {"Write", "write", "Edit", "edit", "MultiEdit", "multi_edit", "multiedit",
               "NotebookEdit"}
_PATCH_TOOLS = {"apply_patch", "functions.apply_patch", "ApplyPatch", "patch"}
_SHELL_TOOLS = {"Bash", "bash", "Shell", "shell", "exec_command", "functions.exec_command"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
                         re.MULTILINE)
_OPERATORS = set(";&|<>()\n")
_GIT_VALUE_OPTIONS = {"-C", "-c"}
_GIT_FLAG_OPTIONS = {"--no-pager", "-P", "--paginate", "--no-optional-locks", "--bare",
                     "--literal-pathspecs", "--no-replace-objects"}
_GIT_ASSIGN_OPTIONS = ("--git-dir=", "--work-tree=", "--namespace=")
_PREFIX_WORDS = {"env", "command", "time"}
_COMPUTE_HOSTS = {"compute-hosts", "compute-hosts.py"}
_DETACHERS = {"nohup", "setsid"}
_PYTHON = re.compile(r"^python[0-9.]*$")
_TRAIN = re.compile(r"(?:^|[/.])train[\w-]*(?:\.py)?$|(?:^|/)run\.py$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LEDGER_MAX_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------- environment


def _truthy(value) -> bool:
    return str(value or "").strip().lower() not in ("", *_OFF)


def _exempt(env) -> bool:
    if str(env.get(SWITCH, "")).strip().lower() in _OFF:
        return True
    try:
        if int(env.get("AGENT_DISPATCH_DEPTH") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return (str(env.get("AGENT_SESSION_ROLE", "")).lower() == "worker"
            or env.get("AGENT_DISPATCH_CHILD") == "1"
            or env.get("AGENT_DISPATCH_REGISTERED_WORKER") == "1"
            or bool(env.get("OPENCODE_DISPATCH_SLUG"))
            or _truthy(env.get("CI")))


def _real(raw, base: Path) -> Path:
    path = Path(str(raw)).expanduser()
    return Path(os.path.realpath(path if path.is_absolute() else base / path))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _excluded(path: Path, env) -> bool:
    if ".git" in path.parts:
        return True
    home = Path(env.get("HOME") or Path.home())
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    roots = [Path(tempfile.gettempdir()), home / ".claude", home / ".codex",
             home / ".config" / "opencode", data / "hearting"]
    return any(_within(path, Path(os.path.realpath(root))) for root in roots)


# ------------------------------------------------------------------- git layout


def _artifact_path(path: Path, top: Path, root: str = "") -> bool:
    """``path`` lies in the canonical root or in the work tree's own top-level root dirs."""
    roots = [top / name for name in _ARTIFACT_DIRS] + ([Path(root)] if root else [])
    return any(_within(path, Path(os.path.realpath(candidate))) for candidate in roots)


def _work_tree(path: Path):
    """The directory holding ``.git`` for ``path`` (which may not exist yet), or None."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    for directory in (probe, *probe.parents):
        marker = directory / ".git"
        if (marker / "HEAD").is_file():
            return directory
        if marker.is_file():
            try:
                if marker.read_text(encoding="utf-8", errors="replace").startswith("gitdir:"):
                    return directory
            except OSError:
                return None
    return None


def _primary(top: Path):
    """The main worktree of ``top`` when the layout is the standard one, else None."""
    marker = top / ".git"
    if marker.is_dir():
        return Path(os.path.realpath(top))
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = _real(text.partition(":")[2].strip(), top)
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        return None
    return Path(os.path.realpath(gitdir.parent.parent.parent))


def artifact_root(top: Path, env) -> str:
    """Canonical artifact root for the work tree ``top``.

    Computed in-process only where that is exactly what ``artifact-root.sh`` answers (no
    ``AGENT_ARTIFACT_ROOT``, standard ``.git`` layout); otherwise the resolver runs.
    """
    if not env.get("AGENT_ARTIFACT_ROOT"):
        primary = _primary(top)
        if primary is not None:
            for name in _ARTIFACT_DIRS:
                if (primary / name).is_dir():
                    return os.path.realpath(primary / name)
            return str(primary / ".agent_reports")
    result = subprocess.run(["sh", str(ROOT / "utilities" / "artifact-root.sh"), str(top)],
                            capture_output=True, text=True, timeout=20, check=False,
                            env=dict(env))
    lines = (result.stdout or "").strip().splitlines()
    if result.returncode != 0 or not lines:
        return ""
    return os.path.realpath(lines[-1])


def _has_source_change(top: Path, root: str, env) -> bool:
    result = subprocess.run(["git", "-C", str(top), "status", "--porcelain", "-z"],
                            capture_output=True, text=True, timeout=20, check=False,
                            env={**env, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode != 0:
        return False
    entries = [entry for entry in result.stdout.split("\0") if entry]
    for entry in entries:
        if len(entry) < 4 or entry[2] != " ":
            continue  # rename source half of a -z pair
        path = _real(entry[3:], top)
        if _excluded(path, env) or _artifact_path(path, top, root):
            continue
        return True
    return False


# --------------------------------------------------------------------- payload


def _mapping(payload: dict, *keys: str) -> dict:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _string(payload: dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool(payload: dict) -> str:
    tool = _string(payload, "tool_name", "toolName")
    if not tool and isinstance(payload.get("tool"), dict):
        tool = _string(payload["tool"], "name")
    elif not tool:
        tool = _string(payload, "tool")
    return tool


def _args(payload: dict) -> dict:
    return _mapping(payload, "tool_input", "toolInput", "input", "arguments", "args", "params")


def _session_id(payload: dict) -> str:
    sid = _string(payload, "session_id", "sessionID", "sessionId", "thread_id", "threadID")
    if not sid:
        sid = _string(_mapping(payload, "session"), "id")
    return sid


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


def _edit_targets(tool: str, args: dict, payload: dict) -> list[str]:
    if tool in _EDIT_TOOLS:
        raw = _string(args, "file_path", "filePath", "notebook_path", "path")
        return [raw] if raw else []
    if tool in _PATCH_TOOLS or "apply_patch" in tool:
        return [(match.group(1) or match.group(2) or "").strip()
                for match in _PATCH_FILE.finditer(_patch_text(payload))]
    return []


# ----------------------------------------------------------------------- shell


def _segments(command: str) -> list[list[str]]:
    """Simple command word lists; stops at a here-doc, returns [] when unparsable."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments, current = [], []
    for token in tokens:
        if token and set(token) <= _OPERATORS:
            if token.startswith("<<"):
                break
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_prefix(words: list[str]) -> list[str]:
    index = 0
    while index < len(words) and (_ASSIGNMENT.match(words[index])
                                  or words[index] in _PREFIX_WORDS):
        index += 1
    return words[index:]


def _commit_dir(words: list[str], cwd: Path):
    """The directory of a plain ``git [global options] commit``, else None."""
    if not words or Path(words[0]).name != "git":
        return None
    index, directory = 1, cwd
    while index < len(words):
        word = words[index]
        if word in _GIT_VALUE_OPTIONS and index + 1 < len(words):
            if word == "-C":
                directory = _real(words[index + 1], directory)
            index += 2
        elif word in _GIT_FLAG_OPTIONS or word.startswith(_GIT_ASSIGN_OPTIONS):
            index += 1
        else:
            return directory if word == "commit" else None
    return None


def _long_run(words: list[str]) -> bool:
    if not words:
        return False
    head = Path(words[0]).name
    if head in _DETACHERS:
        rest = words[1:]
        for index, word in enumerate(rest):
            if _PYTHON.match(Path(word).name):
                return any(_TRAIN.search(later) for later in rest[index + 1:])
        return False
    names = [Path(word).name for word in words[:2]]
    for index, name in enumerate(names):
        if name in _COMPUTE_HOSTS and (index == 0 or _PYTHON.match(names[0])):
            return "run" in words[index + 1:]
    return False


def shell_triggers(command: str, cwd: Path) -> list[tuple[str, Path]]:
    """``[(kind, directory)]`` for each plain commit or long-run segment."""
    found, directory = [], cwd
    for raw in _segments(command):
        words = _strip_prefix(raw)
        if not words:
            continue
        if words[0] == "cd" and len(words) == 2:
            directory = _real(words[1], directory)
            continue
        commit = _commit_dir(words, directory)
        if commit is not None:
            found.append(("commit", commit))
        elif _long_run(words):
            found.append(("run", directory))
    return found


# ---------------------------------------------------------------------- ledger


def _route_chain():
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet import route_chain  # noqa: WPS433 -- lazy, like capability-route.py

    return route_chain


def _ledger_roots(chain, identities) -> set[str]:
    roots = set()
    for harness, session_id in identities:
        try:
            path = chain.ledger_path(harness, session_id)
        except ValueError:
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read(_LEDGER_MAX_BYTES)
        except FileNotFoundError:
            continue
        for line in data.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            root = record.get("artifact_root") if isinstance(record, dict) else None
            if isinstance(root, str) and root:
                roots.add(os.path.realpath(root))
    return roots


def _identities(chain, harness: str, session_id: str, env) -> list[tuple[str, str]]:
    found = [(harness, session_id)] if session_id else []
    writer = chain.writer_identity(env)
    if writer and tuple(writer) not in found:
        found.append(tuple(writer))
    return found


def _dev_activation(top: Path, env) -> bool:
    target = _primary(top)
    for candidate in (env.get("AGENT_HOME"), str(ROOT)):
        if not candidate:
            continue
        home = Path(os.path.realpath(candidate))
        if not (home / ".git").exists():
            continue
        if home == Path(os.path.realpath(top)) or (target and _primary(home) == target):
            return True
    return False


# ---------------------------------------------------------------------- refusal


def _slug_word(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "work"


def _campaign_key(root: str, top: Path) -> str:
    newest, key = -1.0, ""
    try:
        entries = list(os.scandir(Path(root) / "campaigns"))
    except OSError:
        entries = []
    for entry in entries:
        match = re.match(r"^\d{4}-\d{2}-\d{2}_(.+)$", entry.name)
        if not match or not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest, key = mtime, match.group(1)
    return key or _slug_word((_primary(top) or top).name)


def _agent_home(env) -> Path:
    home = env.get("AGENT_HOME")
    if home and (Path(home) / "utilities" / "capability-route.py").is_file():
        return Path(home)
    data = Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME") or Path.home()) / ".local" / "share")
    current = data / "hearting" / "current"
    if (current / "utilities" / "capability-route.py").is_file():
        return current
    return ROOT


def refusal(root: str, top: Path, kind: str, target: str, env) -> str:
    # The cycle folder already carries the date, so the slug is the name alone; a
    # managed release is printed as its `current` pointer like every other printed command.
    from parent_next_directive import entrypoint

    stem = Path(target).stem if kind == "edit" and target else kind
    command_parts = [
        "python3", shlex.quote(entrypoint(_agent_home(env), "utilities/capability-route.py")),
        "compose"]
    if kind == "edit":
        command_parts.extend(["--start", "--shape", "direct", "--prompt-file", shlex.quote("<task>")])
    else:
        command_parts.extend(["--shape", "direct"])
    command_parts.extend([
        "--campaign-key", shlex.quote(_campaign_key(root, top)),
        "--slug", shlex.quote(_slug_word(stem)), "--cwd", shlex.quote(str(top))])
    command = " ".join(command_parts)
    task_guidance = ("It prepares an isolated worktree; make the edit in the worktree it prints."
                     if kind == "edit" else "It records the route for changes already made in place.")
    return (f"hearting: this session has no route for {root} yet (core/WORKFLOW.md §0.4). "
            f"For one atomic, reversible inline change, run this once, then retry:\n{command}\n"
            f"{task_guidance} "
            "For other new work, omit --shape and use compose --start --prompt-file <task>; the frame decides the route. "
            "Follow an already-decided frame or person-specified route under core/WORKFLOW.md §0.2.1. "
            "To continue a route you already have: capability-route.py start --route <route-file>.")


# ---------------------------------------------------------------------- judge


def _triggers(payload: dict, env) -> list[tuple[str, Path, str]]:
    tool = _tool(payload)
    args = _args(payload)
    cwd = _real(_string(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd(),
                Path("/"))
    found = []
    for raw in _edit_targets(tool, args, payload):
        path = _real(raw, cwd)
        if _excluded(path, env):
            continue
        top = _work_tree(path)
        if top is not None and not _artifact_path(path, top):
            found.append(("edit", top, str(path)))
    if tool in _SHELL_TOOLS or tool.endswith(".exec_command"):
        command = _string(args, "command", "cmd", "script") or _string(payload, "command", "cmd")
        workdir = _string(args, "workdir", "workDir", "cwd")
        base = _real(workdir, cwd) if workdir else cwd
        for kind, directory in shell_triggers(command, base) if command else []:
            if _excluded(directory, env):
                continue
            top = _work_tree(directory)
            if top is not None:
                found.append((kind, top, ""))
    return found


def judge(harness: str, payload: dict, env=None) -> str:
    """The one-time refusal for this payload, or ``""`` (pass). Never raises."""
    env = os.environ if env is None else env
    try:
        if _exempt(env) or not isinstance(payload, dict):
            return ""
        triggers = _triggers(payload, env)
        if not triggers:
            return ""
        chain = _route_chain()
        identities = _identities(chain, harness, _session_id(payload), env)
        if not identities:
            return ""
        roots = _ledger_roots(chain, identities)
        for kind, top, target in triggers:
            if _dev_activation(top, env):
                continue
            root = artifact_root(top, env)
            if not root or root in roots:
                continue
            if target and _artifact_path(Path(target), top, root):
                continue
            if kind == "commit" and not _has_source_change(top, root, env):
                continue
            return refusal(root, top, kind, target, env)
    except Exception:  # noqa: BLE001 -- any judgement failure passes
        return ""
    return ""


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "--claude"
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    if mode == "--opencode":
        if not isinstance(payload, dict):
            return 0
        payload = {"tool_name": payload.get("tool") or "", "tool_input": payload.get("args") or {},
                   "session_id": payload.get("sessionID") or "", "cwd": payload.get("cwd") or ""}
        reason = judge("opencode", payload)
        if reason:
            print(reason)
            return 1
        return 0
    harness = "codex" if mode == "--codex" else "claude"
    reason = judge(harness, payload)
    if not reason:
        return 0
    if harness == "codex":
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
