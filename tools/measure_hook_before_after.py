#!/usr/bin/env python3
"""Read-only before/after measurement for the two prompt/Stop hooks (d3 input).

This tool never measures a write-capable hook. Each run happens in a private
copy of one fixed state tree, inside a subprocess whose ``sitecustomize`` audit
shim refuses filesystem writes, socket creation and process spawning. The hook
under test already treats every failure as fail-open, so a refused audit event
is recorded and the run continues read-only.

Reported numbers are a **read-only lower bound**: they exclude delivery effects
(claim, ACK, submission, retries) that a real prompt may additionally perform.
The excluded paths and every audit refusal are listed with the result.

Usage::

    python3 tools/measure_hook_before_after.py \
        --before-code-root <base checkout> --before-state-root <fixed copy> \
        --after-code-root <worktree>   --after-state-root <same fixed copy> \
        --session <target session> --json <result.json>

The ``--*-state-root`` argument is a fixture base from
``utilities/hook_perf_fixture.py`` (a directory holding ``fixture.json``) or a
directory whose own ``hearting/`` tree is the dispatch state root. The tool
copies that tree twice, so both code roots see byte-identical input.

``--chain {claude,codex}:{prompt,tool,stop}`` (repeatable) measures the real hook
chain a harness runs for one prompt, one tool call or one Stop instead
(contract ``python-deny+fingerprint``): Python hooks cannot write outside the
fixture or open a socket, shell hooks are not blocked but their cwd/env point at
the fixture and the code root's git state plus the operational roots' marker
names are compared before and after, failing the run when anything moved::

    python3 tools/measure_hook_before_after.py --chain claude:prompt \\
        --chain codex:stop --state-root <fixture base> --json <result.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HOOK_ENTRY = Path("hooks") / "dispatch-session-sweep.py"
KIND = "claude-parent-runtime"
EXCLUDED_PATHS = (
    "dispatch_pending_delivery.claim / claim_ready_batch / reclaim",
    "dispatch_pending_delivery.ack / ack_received / record_send",
    "dispatch_supervision.continue_for_parent",
    "capacity_auto_resume",
    "codex_queue_delivery.retire_acked_completion",
    "request_obligation_reconciliation (detached runner request)",
)

_SHIM = r'''
"""Audit shim: one read-only hook run. Internal to the measurement tool."""
import atexit
import json
import os
import sys

_LOG = os.environ.get("HOOK_AUDIT_LOG")
_STATE = {"opens": 0, "read_bytes": 0, "blocked": [], "paths": [], "flush": False}
_WRITE_EVENTS = {
    "os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.chmod",
    "os.chown", "os.truncate", "os.link", "os.symlink", "os.utime",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat",
    "socket.__new__", "socket.connect", "socket.bind",
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
}


def _blocked(event):
    if event in _WRITE_EVENTS:
        _STATE["blocked"].append(event)
        raise PermissionError("measurement-read-only: " + event)


def _audit(event, args):
    if _STATE["flush"]:
        return
    if event in ("open", "os.open"):
        path = args[0] if args else ""
        mode = args[1] if len(args) > 1 else ""
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing:
            _STATE["blocked"].append(event + ":write")
            raise PermissionError("measurement-read-only: open-for-write")
        _STATE["opens"] += 1
        if isinstance(path, (str, bytes, os.PathLike)):
            _STATE["paths"].append(os.fspath(path))
        return
    _blocked(event)


sys.addaudithook(_audit)


@atexit.register
def _flush():
    if not _LOG:
        return
    _STATE["flush"] = True
    try:
        with open(_LOG, "w", encoding="utf-8") as handle:
            json.dump({"opens": _STATE["opens"], "blocked": sorted(set(_STATE["blocked"])),
                       "paths": _STATE["paths"][:500]}, handle)
    except OSError:
        pass
'''


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_digest(root: Path) -> dict:
    entries = []
    for path in sorted(root.rglob("*")):
        try:
            info = path.lstat()
        except OSError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        entries.append([str(path.relative_to(root)), info.st_size, info.st_mtime_ns])
    encoded = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    return {"files": len(entries), "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def _fixture_env(state_root: Path) -> dict:
    manifest = state_root / "fixture.json"
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        env = payload.get("env") if isinstance(payload, dict) else None
        if isinstance(env, dict) and env:
            base = str(payload.get("base") or state_root)
            return {key: str(value).replace(base, "{COPY}") for key, value in env.items()}
    hearting = state_root / "hearting"
    return {
        "HARNESS_STATE_ROOT": "{COPY}/hearting",
        "XDG_STATE_HOME": "{COPY}",
        "AGENT_DISPATCH_JOBS": "{COPY}/hearting/dispatch/jobs.log",
        "AGENT_PEER_LEDGER_ROOT": "{COPY}/hearting",
    }


def _copy_state(state_root: Path, destination: Path) -> Path:
    source = Path(state_root)
    if (source / "fixture.json").is_file():
        shutil.copytree(source, destination, symlinks=True)
    elif (source / "hearting").is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        # A bare dispatch root: give it the `hearting/` shape the env expects.
        (destination / "hearting").mkdir(parents=True)
        shutil.copytree(source, destination / "hearting" / "dispatch", symlinks=True)
    return destination


def _shim_dir(base: Path) -> Path:
    directory = base / "shim"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(_SHIM, encoding="utf-8")
    return directory


def _run_once(code_root: Path, state_copy: Path, template_env: dict, session: str) -> dict:
    env = {key: value.replace("{COPY}", str(state_copy)) for key, value in template_env.items()}
    audit_log = state_copy.parent / f"audit-{time.time_ns()}.json"
    env.update({
        "AGENT_HOME": str(code_root),
        "PYTHONPATH": str(_shim_dir(state_copy.parent)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOOK_AUDIT_LOG": str(audit_log),
    })
    payload = json.dumps({"session_id": session, "hook_event_name": "UserPromptSubmit"})
    command = [sys.executable, "-B", str(Path(code_root) / HOOK_ENTRY)]
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic_ns()
    completed = subprocess.run(command, cwd=str(code_root), env=env, input=payload,
                               capture_output=True, text=True, timeout=30)
    elapsed_ns = time.monotonic_ns() - started
    audit = {}
    try:
        audit = json.loads(audit_log.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            audit_log.unlink()
        except OSError:
            pass
    return {
        "command": command,
        "started_at_utc": started_at,
        "elapsed_ns": elapsed_ns,
        "returncode": completed.returncode,
        "stdout_bytes": len(completed.stdout.encode("utf-8")),
        "stderr_tail": completed.stderr[-2000:],
        "opens": audit.get("opens"),
        "blocked": audit.get("blocked", []),
        "opened_paths_sample": audit.get("paths", [])[:20],
    }


def _run_discovery(code_root: Path, state_copy: Path, template_env: dict, session: str) -> dict:
    env = {key: value.replace("{COPY}", str(state_copy)) for key, value in template_env.items()}
    env.update({"AGENT_HOME": str(code_root), "PYTHONPATH": str(_shim_dir(state_copy.parent)),
                "PYTHONDONTWRITEBYTECODE": "1"})
    script = (
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(code_root) / 'utilities')!r})\n"
        "import dispatch_session_sweep as sweep\n"
        "root = Path(os.environ['AGENT_DISPATCH_JOBS']).parent\n"
        "start = time.monotonic_ns()\n"
        f"records, count = sweep.addressed_records(root, {KIND!r}, {session!r})\n"
        "print(json.dumps({'elapsed_ns': time.monotonic_ns() - start, 'count': count, "
        "'records': len(records)}))\n"
    )
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    completed = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(code_root), env=env,
                               capture_output=True, text=True, timeout=30)
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {"error": completed.stderr[-2000:]}
    result.update({"command": [sys.executable, "-B", "-c", "<discovery>"],
                   "returncode": completed.returncode,
                   "started_at_utc": started_at})
    return result


def _prime_copy(code_root: Path, state_copy: Path) -> dict:
    """Re-publish derived observations for the copied state's new inodes.

    A byte copy changes every inode, so an inbox or compact-row file generated
    from the source tree is correctly stale in the copy. The measurement setup
    runs the current priming path once on the private copy (setup, not the
    measured run) so the after code sees its warm contract; the before code
    ignores those files and derives global state as it always did.
    """
    manifest = state_copy / "fixture.json"
    if not manifest.is_file():
        return {"primed": False, "reason": "no-fixture-manifest"}
    script = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(code_root) / 'utilities')!r})\n"
        "import hook_perf_fixture\n"
        "copy = os.environ['FIXTURE_COPY']\n"
        "fixture = json.loads((Path(copy) / 'fixture.json').read_text(encoding='utf-8'))\n"
        "base = str(fixture.get('base') or '')\n"
        "env = {key: str(value).replace(base, copy) for key, value in fixture['env'].items()}\n"
        "os.environ.update(env)\n"
        "for _key in ('base', 'home', 'xdg', 'state', 'dispatch_root', 'jobs',\n"
        "             'sessions', 'peer_root'):\n"
        "    _value = fixture.get(_key)\n"
        "    if isinstance(_value, str):\n"
        "        fixture[_key] = _value.replace(base, copy)\n"
        "fixture['base'] = copy\n"
        "fixture['dispatch_root'] = str(Path(env['AGENT_DISPATCH_JOBS']).parent)\n"
        "fixture['env'] = env\n"
        "hook_perf_fixture.prime_inbox(fixture)\n"
        "print(json.dumps({'primed': bool(fixture.get('inbox_primed')),\n"
        "                  'lineage_rows': fixture.get('lineage_rows')}))\n"
    )
    env = dict(os.environ)
    env.update({"AGENT_HOME": str(code_root), "FIXTURE_COPY": str(state_copy),
                "PYTHONPATH": str(Path(code_root) / "utilities"),
                "PYTHONDONTWRITEBYTECODE": "1"})
    completed = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(code_root), env=env,
                               capture_output=True, text=True, timeout=120)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"primed": False, "reason": "prime-run-failed",
                "stderr": completed.stderr[-2000:]}


def measure(name: str, code_root: Path, state_root: Path, session: str, *, repeat: int,
            scratch: Path, prime_code_root: Path | None = None) -> dict:
    (scratch / name).mkdir(parents=True, exist_ok=True)
    copy = _copy_state(state_root, scratch / name / "state")
    input_digest = _state_digest(copy)
    priming = _prime_copy(prime_code_root or Path(code_root), copy)
    template_env = _fixture_env(state_root)
    first = _run_once(code_root, copy, template_env, session)
    second = _run_once(code_root, copy, template_env, session)
    runs = [first, second]
    for _ in range(max(0, repeat - len(runs))):
        runs.append(_run_once(code_root, copy, template_env, session))
    # Warm copies: the discovery probe runs on a fresh copy so its inbox
    # prefill cost is labeled, not silently reused by the entrypoint runs.
    discovery_copy = _copy_state(state_root, scratch / name / "discovery")
    _prime_copy(prime_code_root or Path(code_root), discovery_copy)
    discovery = _run_discovery(code_root, discovery_copy, template_env, session)
    return {
        "code_root": str(code_root),
        "state_root": str(state_root),
        "state_digest": input_digest,
        "primed_state_digest": _state_digest(copy),
        "priming": priming,
        "entrypoint_runs": runs,
        "entrypoint_median_ns": int(statistics.median(run["elapsed_ns"] for run in runs)),
        "discovery": discovery,
        "discovery_median_ns": discovery.get("elapsed_ns"),
    }


# ---------------------------------------------------------------------------
# --chain: the real hook chain of one harness event (T4)
# ---------------------------------------------------------------------------

CHAIN_CONTRACT = "python-deny+fingerprint"
CHAIN_CONTRACT_NOTE = (
    "Python hooks: writes outside the scratch fixture and socket connects are refused. "
    "Shell hooks cannot be refused: their cwd and env point at the fixture, and the code "
    "root git state plus marker-bearing names under the operational roots are compared "
    "before and after, so a write is noticed (the run fails), not prevented.")
CHAIN_EVENTS = {
    "prompt": ("UserPromptSubmit",),
    "tool": ("PreToolUse", "PostToolUse"),
    "stop": ("Stop",),
}
CHAIN_SETTINGS = {
    "claude": Path("adapters") / "claude" / "settings.json",
    "codex": Path("adapters") / "codex" / "hooks" / "hooks.json",
}
CHAIN_TOOL_NAME = "Bash"
# Executables a restricted PATH leaves out: a hook that looks for one returns early.
PATH_GAP_NAMES = ("herdr", "tmux", "codex", "claude", "opencode")
PATH_KEEP = (
    "python3", "sh", "bash", "node", "git", "sleep", "dirname", "basename", "cat", "grep",
    "sed", "awk", "date", "mkdir", "head", "tail", "tr", "cut", "sort", "uniq", "wc", "env",
    "ls", "stat", "mktemp", "readlink", "realpath", "id", "hostname", "uname", "find",
    "xargs", "tee", "true", "false", "test", "expr", "touch", "cp", "mv", "ln", "rm",
    "chmod", "sha256sum", "md5sum", "jq", "printf", "tput", "cmp", "diff", "egrep", "fgrep",
)
OPS_SCAN_DEPTH = 3

_CHAIN_SHIM = r'''
"""Audit shim: one chain hook run. Internal to the measurement tool."""
import atexit
import json
import os
import sys

_LOG = os.environ.get("HOOK_AUDIT_LOG")
_SCRATCH = os.path.realpath(os.environ.get("CHAIN_SCRATCH") or "/nonexistent-scratch")
_STATE = {"blocked": [], "flush": False}
# event -> ((path index, dir_fd index or None), ...): the arguments that name a write target
_PATH_ARGS = {
    "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.mkdir": ((0, 2),),
    "os.chmod": ((0, 2),), "os.chown": ((0, 3),), "os.truncate": ((0, None),),
    "os.utime": ((0, 3),), "os.rename": ((0, 2), (1, 3)), "os.link": ((0, 2), (1, 3)),
    "os.symlink": ((1, 2),), "shutil.copyfile": ((1, None),),
    "shutil.copymode": ((1, None),), "shutil.copystat": ((1, None),),
    "shutil.rmtree": ((0, 1),),
}
_SOCKET_EVENTS = ("socket.connect", "socket.bind", "socket.sendto")


def _resolve(path, dir_fd=None):
    if isinstance(path, int):
        return None
    try:
        raw = os.fsdecode(path)
    except Exception:
        return None
    if raw.startswith("/dev/"):
        return None
    base = os.getcwd()
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(raw):
        try:
            base = os.readlink("/proc/self/fd/%d" % dir_fd)
        except OSError:
            return raw
    return os.path.realpath(os.path.join(base, raw))


def _outside(path, dir_fd=None):
    real = _resolve(path, dir_fd)
    if real is None:
        return False
    return not (real == _SCRATCH or real.startswith(_SCRATCH + os.sep))


def _deny(event, detail):
    _STATE["blocked"].append(event + ":" + detail)
    raise PermissionError("measurement-scratch-only: " + event)


def _audit(event, args):
    if _STATE["flush"]:
        return
    if event == "open":
        mode = args[1] if len(args) > 1 else ""
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (
            isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and args and _outside(args[0]):
            _deny("open-write", str(_resolve(args[0])))
        return
    if event in _SOCKET_EVENTS:
        _deny(event, "socket")
    for path_index, fd_index in _PATH_ARGS.get(event, ()):
        if len(args) > path_index:
            fd = args[fd_index] if fd_index is not None and len(args) > fd_index else None
            if _outside(args[path_index], fd):
                _deny(event, str(_resolve(args[path_index], fd)))


sys.addaudithook(_audit)


@atexit.register
def _flush():
    if not _LOG or not _STATE["blocked"]:
        return
    _STATE["flush"] = True
    try:
        with open("%s.%d" % (_LOG, os.getpid()), "w", encoding="utf-8") as handle:
            json.dump({"blocked": _STATE["blocked"][:50]}, handle)
    except OSError:
        pass
'''


def matcher_matches(matcher, tool_name: str) -> bool:
    """A harness matcher: empty or ``*`` matches everything, else a regex on the tool name."""
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(str(matcher), tool_name) is not None
    except re.error:
        return False


def chain_commands(settings_path: Path, harness: str, event_key: str,
                   tool_name: str = CHAIN_TOOL_NAME) -> list[dict]:
    """The command hooks one harness event runs, in settings order (matcher applied)."""
    payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    hooks = payload.get("hooks", payload) if isinstance(payload, dict) else {}
    entries: list[dict] = []
    for hook_event in CHAIN_EVENTS[event_key]:
        for group in hooks.get(hook_event, []) or []:
            matcher = group.get("matcher")
            if event_key == "tool" and not matcher_matches(matcher, tool_name):
                continue
            for item in group.get("hooks", []) or []:
                if item.get("type", "command") != "command" or not item.get("command"):
                    continue
                entries.append({
                    "harness": harness, "event": hook_event, "matcher": matcher,
                    "command": item["command"],
                    "async": bool(item.get("async")),
                    "async_rewake": bool(item.get("asyncRewake")),
                })
    return entries


def hook_script(command: str, harness: str) -> str | None:
    """The hook file name behind a ``run-hook.sh`` command, relative to the code root."""
    if harness == "codex":
        found = re.search(r"run-hook\.sh\"?\s+([\w.\-]+\.py)", command)
        return f"adapters/codex/hooks/{found.group(1)}" if found else None
    found = re.search(
        r"run-hook\.sh\"?\s+(?:(?:exec|python3|bash|sh)\s+)?([\w.\-]+\.(?:py|sh))", command)
    return f"hooks/{found.group(1)}" if found else None


def path_gap_names(text: str, *, shell: bool = False) -> list[str]:
    """Names of executables (herdr, tmux, ...) a hook body looks up on PATH or runs by name."""
    found = []
    for name in PATH_GAP_NAMES:
        patterns = [
            rf"""which\(\s*['"]{name}['"]""",
            rf"""command\s+-v\s+{name}\b""",
            rf"""\bwhich\s+{name}\b""",
            rf"""(?:run|Popen|call|check_output|check_call|spawn\w*|execvp?)\(\s*\[\s*['"]{name}['"]""",
        ]
        if shell:
            patterns.append(rf"(?m:(?:^|[;&|(`]|\$\()\s*{name}\s+\S)")
        if any(re.search(pattern, text) for pattern in patterns):
            found.append(name)
    return found


def env_gap_names(text: str) -> list[str]:
    """Pane-identity env vars (``HERDR_*``, ``TMUX*``) a hook reads; the chain env leaves them out."""
    found = []
    if re.search(r"""['"$]\{?HERDR_[A-Z_]+""", text):
        found.append("env:HERDR_*")
    if re.search(r"""['"$]\{?TMUX(?:_PANE)?\b""", text):
        found.append("env:TMUX*")
    return found


def hook_path_gaps(code_root: Path, script_rel: str | None) -> list[str]:
    """Lower-bound causes for a hook: PATH executables and pane env vars it looks for.

    Reads the hook body plus the ``utilities/`` modules it imports (one level).
    """
    if not script_rel:
        return []
    target = Path(code_root) / script_rel
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    names = set(path_gap_names(text, shell=target.suffix == ".sh")) | set(env_gap_names(text))
    for module in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", text, re.M):
        helper = Path(code_root) / "utilities" / f"{module}.py"
        if helper.is_file():
            body = helper.read_text(encoding="utf-8", errors="replace")
            names.update(path_gap_names(body))
            names.update(env_gap_names(body))
    return sorted(names)


def _git_porcelain(code_root: Path, git_config: str = "") -> tuple[str, str]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    if git_config:
        env["GIT_CONFIG_GLOBAL"] = git_config
    completed = subprocess.run(
        ["git", "-C", str(code_root), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--ignored"],
        capture_output=True, env=env, timeout=120)
    if completed.returncode != 0:
        return "", completed.stderr.decode("utf-8", "replace")[-300:] or "git-status-failed"
    return completed.stdout.decode("utf-8", "surrogateescape"), ""


def code_fingerprint(code_root: Path, git_config: str = "") -> dict:
    """Git state of the code root plus (path, size, mtime_ns) of every path it lists.

    The code root must be quiescent: another writer editing it during the run is
    indistinguishable from a hook writing there, and fails the run the same way.
    """
    code_root = Path(code_root)
    raw, error = _git_porcelain(code_root, git_config)
    if error:
        return {"error": error, "files": {}, "status_sha256": ""}
    entries = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            index += 1  # a rename/copy carries its source as the next field
    files: dict[str, list[int]] = {}
    for rel in paths:
        full = code_root / rel
        if rel.endswith("/"):
            for dirpath, _dirs, names in os.walk(full):
                for name in names:
                    child = Path(dirpath) / name
                    try:
                        info = child.lstat()
                    except OSError:
                        continue
                    files[str(child.relative_to(code_root))] = [info.st_size, info.st_mtime_ns]
            continue
        try:
            info = full.lstat()
        except OSError:
            files[rel] = [-1, -1]
            continue
        files[rel] = [info.st_size, info.st_mtime_ns]
    return {"error": "", "files": files,
            "status_sha256": hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()}


def fingerprint_diff(before: dict, after: dict) -> list[str]:
    """Paths whose listing, size or mtime differ between two code fingerprints."""
    changed = {path for path in set(before["files"]) | set(after["files"])
               if before["files"].get(path) != after["files"].get(path)}
    if not changed and before["status_sha256"] != after["status_sha256"]:
        changed.add("<git-status>")
    return sorted(changed)


def ops_marker_names(ops_roots, markers, *, depth: int = OPS_SCAN_DEPTH) -> list[str]:
    """Entry names (never contents) up to ``depth`` below each operational root that hold a marker."""
    markers = [marker for marker in markers if marker]
    found: list[str] = []

    def walk(directory: str, level: int) -> None:
        try:
            with os.scandir(directory) as scan:
                children = list(scan)
        except OSError:
            return
        for child in children:
            if any(marker in child.name for marker in markers):
                found.append(child.path)
            if level < depth:
                try:
                    if child.is_dir(follow_symlinks=False):
                        walk(child.path, level + 1)
                except OSError:
                    continue

    for root in ops_roots:
        walk(str(root), 1)
    return sorted(found)


def default_ops_roots(code_root: Path) -> list[Path]:
    home = Path(os.path.expanduser("~"))
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state") / "hearting"
    roots = [state, home / ".claude", home / ".codex",
             Path(code_root) / ".agent_reports" / ".runtime"]
    return list(dict.fromkeys(roots))


def _refuse_ops(state_root: Path, ops_roots) -> None:
    resolved = Path(state_root).resolve(strict=False)
    for root in ops_roots:
        candidate = Path(root).resolve(strict=False)
        if resolved == candidate or resolved.is_relative_to(candidate):
            raise ValueError(f"chain state root is inside an operational root: {resolved}")


def _chain_workspace(code_root: Path, state_root: Path) -> dict:
    """Scratch pieces a chain run needs, all under the fixture: PATH dir, shim, project, config dirs."""
    base = Path(state_root) / "chain"
    ws = {name: base / name for name in (
        "bin", "shim", "tmp", "project", "claude", "xdg-cache", "xdg-data", "xdg-config")}
    for path in ws.values():
        path.mkdir(parents=True, exist_ok=True)
    hooks_link = ws["claude"] / "hooks"
    if hooks_link.is_symlink() or hooks_link.exists():
        hooks_link.unlink()
    hooks_link.symlink_to(Path(code_root) / "adapters" / "claude" / "hooks")
    (ws["shim"] / "sitecustomize.py").write_text(_CHAIN_SHIM, encoding="utf-8")
    for name in PATH_KEEP:
        target = shutil.which(name)
        if not target:
            continue
        link = ws["bin"] / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(os.path.realpath(target))
    # A fixture on a share can look "dubious" to git; trust is scoped to this scratch config.
    ws["gitconfig"] = base / "gitconfig"
    ws["gitconfig"].write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    if not (ws["project"] / ".git").exists():
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(ws["gitconfig"]), GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q", str(ws["project"])], env=env, check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(ws["project"]), "-c", "user.name=m", "-c",
                        "user.email=m@example.invalid", "commit", "--allow-empty", "-q", "-m",
                        "init"], env=env, check=True, capture_output=True)
    (ws["project"] / ".agent_reports").mkdir(exist_ok=True)
    return ws


def chain_env(ws: dict, state_root: Path, fixture_env: dict) -> dict:
    """Environment for chain hooks: fixture-only locations, no ``AGENT_HOME``, no live harness vars."""
    home = fixture_env.get("HOME") or str(Path(state_root) / "home")
    env = {
        "PATH": str(ws["bin"]),
        "HOME": home,
        "LANG": "C.UTF-8",
        "TMPDIR": str(ws["tmp"]),
        "XDG_STATE_HOME": fixture_env.get("XDG_STATE_HOME") or str(Path(state_root) / "xdg"),
        "XDG_CACHE_HOME": str(ws["xdg-cache"]),
        "XDG_DATA_HOME": str(ws["xdg-data"]),
        "XDG_CONFIG_HOME": str(ws["xdg-config"]),
        "CLAUDE_CONFIG_DIR": str(ws["claude"]),
        "CODEX_HOME": str(Path(home) / ".codex"),
        "PYTHONPATH": str(ws["shim"]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CHAIN_SCRATCH": str(Path(state_root).resolve()),
        "GIT_CONFIG_GLOBAL": str(ws["gitconfig"]),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for key in ("HARNESS_STATE_ROOT", "AGENT_DISPATCH_JOBS", "AGENT_PEER_LEDGER_ROOT"):
        if fixture_env.get(key):
            env[key] = fixture_env[key]
    return env


def _link_codex_root(env: dict, code_root: Path) -> None:
    link = Path(env["HOME"]) / ".codex" / "hearting"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path(code_root))


def chain_payload(harness: str, hook_event: str, *, session: str, cwd: str, transcript: str,
                  tool_name: str = CHAIN_TOOL_NAME) -> dict:
    payload = {"session_id": session, "transcript_path": transcript, "cwd": cwd,
               "hook_event_name": hook_event, "permission_mode": "default"}
    if harness == "codex":
        payload.update({"turn_id": "turn-chain-measure", "model": "chain-measure"})
    if hook_event == "UserPromptSubmit":
        payload["prompt"] = "latency measurement prompt: list the files"
    elif hook_event in ("PreToolUse", "PostToolUse"):
        payload.update({"tool_name": tool_name, "tool_input": {"command": "true"},
                        "tool_use_id": "toolu_chain_measure"})
        if hook_event == "PostToolUse":
            payload["tool_response"] = {"stdout": "", "stderr": "", "interrupted": False}
    elif hook_event == "Stop":
        payload["stop_hook_active"] = False
    return payload


def _gather_audit(audit_base: str) -> list[str]:
    blocked: list[str] = []
    directory, prefix = os.path.split(audit_base)
    for name in sorted(os.listdir(directory or ".")):
        if not name.startswith(prefix + "."):
            continue
        path = os.path.join(directory, name)
        try:
            blocked.extend(json.loads(Path(path).read_text(encoding="utf-8")).get("blocked", []))
        except (OSError, ValueError):
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
    return blocked


def run_chain_hook(entry: dict, env: dict, cwd: str, payload: dict, audit_base: str,
                   *, timeout: float = 30.0) -> dict:
    """Run one hook command the way the harness does: ``sh -c`` with the payload on stdin."""
    run_env = dict(env, HOOK_AUDIT_LOG=audit_base)
    started = time.monotonic_ns()
    returncode = None
    stderr = ""
    try:
        completed = subprocess.run(["/bin/sh", "-c", entry["command"]], cwd=cwd, env=run_env,
                                   input=json.dumps(payload), capture_output=True, text=True,
                                   timeout=timeout)
        returncode, stderr = completed.returncode, completed.stderr[-400:]
    except subprocess.TimeoutExpired:
        stderr = "timeout"
    elapsed_ns = time.monotonic_ns() - started
    return {"elapsed_ns": elapsed_ns, "returncode": returncode, "stderr_tail": stderr,
            "denied": _gather_audit(audit_base)}


def _snapshot(code_root: Path, ops_roots, markers, git_config: str = "") -> dict:
    return {"code": code_fingerprint(code_root, git_config),
            "ops": set(ops_marker_names(ops_roots, markers))}


def _snapshot_violation(label: dict, before: dict, after: dict) -> dict | None:
    if before["code"]["error"] or after["code"]["error"]:
        return dict(label, code_root_changed=[],
                    fingerprint_error=before["code"]["error"] or after["code"]["error"],
                    operational_new_names=[])
    code = fingerprint_diff(before["code"], after["code"])
    new = sorted(after["ops"] - before["ops"])
    if not code and not new:
        return None
    return dict(label, code_root_changed=code, operational_new_names=new)


def fixture_manifest(state_root: Path) -> dict:
    manifest = Path(state_root) / "fixture.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except ValueError:
            return {}
    return {}


def measure_chain(specs: list[str], code_root: Path, state_root: Path, *, repeat: int = 2,
                  ops_roots=None, fixture_env: dict | None = None, marker: str = "",
                  session: str = "", settle_s: float = 0.5,
                  tool_name: str = CHAIN_TOOL_NAME) -> dict:
    """Measure each ``harness:event`` chain; fail the run if any hook wrote outside the fixture."""
    code_root, state_root = Path(code_root).resolve(), Path(state_root).resolve()
    ops_roots = [Path(root) for root in (ops_roots if ops_roots is not None
                                          else default_ops_roots(code_root))]
    _refuse_ops(state_root, ops_roots)
    parsed = []
    for spec in specs:
        harness, _, event = spec.partition(":")
        if harness not in CHAIN_SETTINGS or event not in CHAIN_EVENTS:
            raise ValueError(f"bad --chain spec {spec!r}: want {{claude,codex}}:{{prompt,tool,stop}}")
        parsed.append((spec, harness, event))
    manifest = fixture_manifest(state_root)
    if fixture_env:
        env_base = dict(fixture_env)
    elif isinstance(manifest.get("env"), dict) and manifest["env"]:
        old_base = str(manifest.get("base") or state_root)
        env_base = {key: str(value).replace(old_base, str(state_root))
                    for key, value in manifest["env"].items()}
    else:
        env_base = {key: value.replace("{COPY}", str(state_root))
                    for key, value in _fixture_env(state_root).items()}
    session = session or str(manifest.get("target_session") or "perf-target")
    marker = marker or state_root.name
    markers = [marker, session]
    ws = _chain_workspace(code_root, state_root)
    git_config = str(ws["gitconfig"])
    env = chain_env(ws, state_root, env_base)
    _link_codex_root(env, code_root)
    transcript = str(manifest.get("transcript") or ws["tmp"] / "transcript.jsonl")
    if not Path(transcript).exists():
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text("", encoding="utf-8")
    cwd = str(ws["project"])
    audit_base = str(ws["tmp"] / "chain-audit")

    violations: list[dict] = []
    chains: dict[str, dict] = {}
    notes: list[str] = []
    current = _snapshot(code_root, ops_roots, markers, git_config)
    for spec, harness, event in parsed:
        entries = chain_commands(code_root / CHAIN_SETTINGS[harness], harness, event, tool_name)
        runs: list[list[dict]] = [[] for _ in entries]
        for rep in range(max(1, repeat)):
            for position, entry in enumerate(entries):
                payload = chain_payload(harness, entry["event"], session=session, cwd=cwd,
                                        transcript=transcript, tool_name=tool_name)
                result = run_chain_hook(entry, env, cwd, payload, audit_base)
                runs[position].append(result)
                if rep == 0:
                    after = _snapshot(code_root, ops_roots, markers, git_config)
                    found = _snapshot_violation({"chain": spec, "hook": hook_script(
                        entry["command"], harness) or entry["command"][:60]}, current, after)
                    if found:
                        violations.append(found)
                    current = after
            if rep > 0:
                after = _snapshot(code_root, ops_roots, markers, git_config)
                found = _snapshot_violation({"chain": spec, "hook": f"<repeat {rep + 1}>"},
                                            current, after)
                if found:
                    violations.append(found)
                current = after
        hooks = []
        gap_notes: list[str] = []
        for position, entry in enumerate(entries):
            script = hook_script(entry["command"], harness)
            gaps = hook_path_gaps(code_root, script)
            ms = [round(run["elapsed_ns"] / 1e6, 3) for run in runs[position]]
            denied = sorted({item for run in runs[position] for item in run["denied"]})
            blocking = not (entry["async"] or entry["async_rewake"])
            hooks.append({
                "hook": script or entry["command"][:60], "event": entry["event"],
                "matcher": entry["matcher"], "blocking": blocking,
                "async": entry["async"], "async_rewake": entry["async_rewake"],
                "runs_ms": ms, "first_ms": ms[0], "median_ms": round(statistics.median(ms), 3),
                "returncode": runs[position][-1]["returncode"],
                "denied": denied, "path_gaps": gaps,
                "stderr_tail": runs[position][-1]["stderr_tail"],
            })
            for name in gaps:
                what = f"env {name[4:]}" if name.startswith("env:") else f"PATH에 {name}"
                gap_notes.append(f"{what} 없음 → {script}는 하한")
            for item in denied[:3]:
                gap_notes.append(f"쓰기·socket 거부({item}) → {script}는 하한")
        blocking_ms = [item["median_ms"] for item in hooks if item["blocking"]]
        chains[spec] = {
            "hooks": hooks, "n_hooks": len(hooks), "n_blocking": len(blocking_ms),
            "sum_s": round(sum(blocking_ms) / 1000, 6),
            "max_s": round(max(blocking_ms, default=0.0) / 1000, 6),
            "sum_s_incl_async": round(sum(item["median_ms"] for item in hooks) / 1000, 6),
            "sum_s_lower_bound_hooks": round(sum(
                item["median_ms"] for item in hooks
                if item["path_gaps"] or item["denied"]) / 1000, 6),
        }
        notes.extend(sorted(set(gap_notes)))
    if settle_s:
        time.sleep(settle_s)
    after = _snapshot(code_root, ops_roots, markers, git_config)
    found = _snapshot_violation({"chain": "*", "hook": "<after-settle>"}, current, after)
    if found:
        violations.append(found)
    notes.append("sum_s = blocking hooks run one after another (upper bound); max_s = they run "
                 "in parallel (lower bound); async hooks are listed but excluded from both")
    notes.append("each hook is `sh -c <settings command>` through run-hook.sh with the audit "
                 "shim on Python children, so the shim's interpreter-start cost is included")
    return {
        "schema": 1, "mode": "chain", "contract": CHAIN_CONTRACT,
        "contract_note": CHAIN_CONTRACT_NOTE, "code_root": str(code_root),
        "state_root": str(state_root), "session": session, "repeat": max(1, repeat),
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "violations": violations, "discarded": bool(violations),
        "chains": {} if violations else chains, "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before-code-root")
    parser.add_argument("--before-state-root")
    parser.add_argument("--after-code-root")
    parser.add_argument("--after-state-root")
    parser.add_argument("--session", default="perf-target")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--json", default="")
    parser.add_argument("--keep-scratch", action="store_true")
    parser.add_argument("--chain", action="append", default=[], metavar="HARNESS:EVENT",
                        help="measure a real hook chain: {claude,codex}:{prompt,tool,stop}")
    parser.add_argument("--code-root", default=str(ROOT), help="--chain: code root (read only)")
    parser.add_argument("--state-root", help="--chain: fixture base (disposable; hooks run in place)")
    parser.add_argument("--marker", default="", help="--chain: marker in the fixture path")
    parser.add_argument("--ops-root", action="append", default=None,
                        help="--chain: operational root to compare by name (repeatable)")
    args = parser.parse_args(argv)

    if args.chain:
        if not args.state_root:
            parser.error("--chain requires --state-root")
        try:
            result = measure_chain(args.chain, Path(args.code_root), Path(args.state_root),
                                   repeat=args.repeat, ops_roots=args.ops_root,
                                   marker=args.marker, session=args.session)
        except ValueError as error:
            parser.error(str(error))
        text = json.dumps(result, indent=2, sort_keys=True)
        if args.json:
            Path(args.json).write_text(text + "\n", encoding="utf-8")
        print(text)
        return 1 if result["violations"] else 0

    missing = [flag for flag, value in (
        ("--before-code-root", args.before_code_root),
        ("--before-state-root", args.before_state_root),
        ("--after-code-root", args.after_code_root),
        ("--after-state-root", args.after_state_root)) if not value]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    scratch = Path(tempfile.mkdtemp(prefix="hook-measure-"))
    try:
        result = {
            "schema": 1,
            "read_only_lower_bound": True,
            "excluded_paths": list(EXCLUDED_PATHS),
            "session": args.session,
            "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "before": measure("before", Path(args.before_code_root),
                              Path(args.before_state_root), args.session,
                              repeat=args.repeat, scratch=scratch,
                              prime_code_root=Path(args.after_code_root)),
            "after": measure("after", Path(args.after_code_root),
                             Path(args.after_state_root), args.session,
                             repeat=args.repeat, scratch=scratch,
                             prime_code_root=Path(args.after_code_root)),
        }
        result["input_digests_match"] = (
            result["before"]["state_digest"] == result["after"]["state_digest"])
        result["same_input_state"] = args.before_state_root == args.after_state_root
        text = json.dumps(result, indent=2, sort_keys=True)
        if args.json:
            Path(args.json).write_text(text + "\n", encoding="utf-8")
        print(text)
    finally:
        if not args.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
