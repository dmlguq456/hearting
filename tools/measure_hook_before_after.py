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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before-code-root", required=True)
    parser.add_argument("--before-state-root", required=True)
    parser.add_argument("--after-code-root", required=True)
    parser.add_argument("--after-state-root", required=True)
    parser.add_argument("--session", default="perf-target")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--json", default="")
    parser.add_argument("--keep-scratch", action="store_true")
    args = parser.parse_args(argv)

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
