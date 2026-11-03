#!/usr/bin/env python3
"""Reattach bounded observation to existing runs; never launch their payloads."""
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from resource_run_registry import proc_identity


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def watch_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def _matching_watchers(command):
    """Exact observer argv, never a workflow supervisor or payload ancestor."""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            argv = (entry / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            argv = [os.fsdecode(part) for part in argv]
            if (len(argv) == len(command) and Path(argv[1]).name == Path(command[1]).name
                    and argv[2:] == command[2:]):
                identity = proc_identity(int(entry.name))
                if identity and identity.get('state') != 'Z':
                    yield argv, identity
        except (OSError, ValueError, IndexError):
            continue


def _retire_watch(argv, identity):
    """Pin the process before rechecking its identity; only an exact watcher dies."""
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        return False
    try:
        fd = os.pidfd_open(identity['pid'])
    except OSError:
        return False
    try:
        current = proc_identity(identity['pid'])
        actual = (Path('/proc') / str(identity['pid']) / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        if current != identity or [os.fsdecode(part) for part in actual] != argv:
            return False
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        return bool(select.select([fd], [], [], 2)[0])
    except (OSError, ProcessLookupError):
        return False
    finally:
        os.close(fd)


def ensure_watch(command, lock_path, *, environ=None, spawn=subprocess.Popen):
    """Reuse this release's observer, replace an exact old observer, serialize starts."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.with_suffix('.launch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = None
        retained = []
        for argv, identity in list(_matching_watchers(command)):
            if Path(argv[1]).resolve() == Path(command[1]).resolve():
                current = identity
            elif not _retire_watch(argv, identity):
                # Older kernels may lack pidfds. Attach observation anyway;
                # the existing exact notice keys deduplicate terminal delivery.
                retained.append(identity['pid'])
        if current:
            return {'started': False, 'reused': True, 'pid': current['pid']}
        env = dict(os.environ if environ is None else environ)
        env['AGENT_HOME'] = str(Path(__file__).resolve().parents[1])
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        proc = spawn(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=env, start_new_session=True, close_fds=True)
        return {'started': True, 'pid': getattr(proc, 'pid', None), 'legacy_retained': retained}


def retire_watch(command, lock_path):
    """Retire older-release observers of a run that needs none; never start one.

    This release's own observer leaves by itself on its next poll.
    """
    lock_path = Path(lock_path)
    with lock_path.with_suffix('.launch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return sum(1 for argv, identity in list(_matching_watchers(command))
                   if Path(argv[1]).resolve() != Path(command[1]).resolve()
                   and _retire_watch(argv, identity))


def resource_lock(registry, run_id):
    leaf = hashlib.sha256(str(run_id).encode()).hexdigest()[:24]
    return Path(registry).parent / '.resource-observers' / (leaf + '.lock')


def ensure_resource(registry, run_id):
    return ensure_watch([sys.executable, str(Path(__file__).resolve()), 'watch',
                         str(Path(registry).resolve()), run_id], resource_lock(registry, run_id))


def watch_resource(registry, run_id, *, sleep=time.sleep):
    registry = Path(registry)
    runner = load_tool('resource-runner')
    import resource_run_registry as RR
    with watch_lock(resource_lock(registry, run_id)) as acquired:
        if not acquired:
            return 0
        while registry.is_file():
            row = json.loads(registry.read_text()).get('runs', {}).get(run_id)
            if not isinstance(row, dict):
                return 0
            liveness = RR.classify_identity(row)[0]
            if liveness == 'exited' and runner.read_sentinel(row.get('sentinel')) is not None:
                row, _ = runner.settle(registry, run_id, row)
            try:
                RR.observe_run_if_due(run_id, row, registry)
            except Exception:  # optional observation retries without losing terminal delivery
                sleep(60)
                continue
            if RR.resource_never_started(row) or (liveness == 'exited' and row.get('ended_at') is not None):
                return 0
            sleep(60)
    return 0


def reconcile():
    results = []
    compute = load_tool('compute-hosts')
    try:
        config = compute.load_config()
        compute._refuse_namespace_self(config, 'resource-observers')
        root = config['run_root']
        entries = root.iterdir() if root.is_dir() else []
        now = time.time()
        for directory in entries:
            if directory.name.startswith('.') or directory.is_symlink() or not directory.is_dir():
                continue
            if not (directory / 'meta.json').is_file():
                continue
            if (directory / 'exit_code').exists():
                # Only a run whose watching stopped as unknown is reported late.
                if not (directory / compute.QUIET_RUN_MARKER).exists():
                    continue
            else:
                quiet_since = compute._run_quiet_since(directory, now)
                # A quiet run is let go only once its host reports the session
                # gone (recorded beside it, so it is asked once); a live or
                # unreadable session keeps its watcher.
                if quiet_since is not None and (
                        (directory / compute.QUIET_RUN_MARKER).exists()
                        or compute._run_session_alive(directory) is False):
                    compute._record_quiet_run(directory, quiet_since, now)
                    retired = retire_watch(
                        [sys.executable, str(Path(compute.__file__).resolve()), 'watch-run', str(directory)],
                        directory / '.observation-watch.lock')
                    results.append({'kind': 'compute', 'run_id': directory.name, 'watching': False,
                                    'state': 'unknown', 'retired': retired})
                    continue
            try:
                meta = json.loads((directory / 'meta.json').read_text())
                started = compute._spawn_completion_watch(directory, meta,
                    environ={**os.environ, 'AGENT_DISPATCH_DEPTH': '0'})
                results.append({'kind': 'compute', 'run_id': directory.name, 'watching': started})
            except (OSError, ValueError, TypeError) as exc:
                results.append({'kind': 'compute', 'run_id': directory.name, 'error': str(exc)})
    except (OSError, ValueError, compute.ConfigError) as exc:
        results.append({'kind': 'compute', 'error': str(exc)})
    import resource_run_registry as RR
    registries, warnings = RR.indexed_paths()
    for registry in registries:
        try:
            _source, raw = RR.read_registry_source(registry)
            data = RR.strict_json_loads(raw) if raw is not None else {}
            for run_id, row in data.get('runs', {}).items():
                if isinstance(row, dict) and row.get('status') in {'running', 'launching'} and row.get('pid'):
                    results.append({'kind': 'resource', 'run_id': run_id, **ensure_resource(registry, run_id)})
        except (OSError, ValueError, TypeError) as exc:
            results.append({'kind': 'resource', 'registry': str(registry), 'error': str(exc)})
    return {'results': results, 'warnings': warnings}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == 'watch':
        return watch_resource(argv[1], argv[2])
    if argv:
        raise SystemExit('usage: hearting run resource-observers')
    print(json.dumps(reconcile(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
