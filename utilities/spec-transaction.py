#!/usr/bin/env python3
"""Hold the canonical spec lock across one route-declared transaction."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("worker_route_guard",ROOT/"utilities/worker-route-guard.py")
GUARD=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GUARD)
sys.path.insert(0,str(ROOT/"utilities"))
import artifact_producer as PRODUCER  # noqa: E402
import artifact_reader as READER  # noqa: E402


def emit(event, events=None):
    line=json.dumps(event,sort_keys=True)
    print(line,flush=True)
    if events:
        with open(events,"a",encoding="utf-8") as fh: fh.write(line+"\n")


def versions_max(tree: Path) -> int:
    versions=tree/"_internal"/"versions"
    found=[]
    if versions.is_dir():
        for row in versions.iterdir():
            match=re.fullmatch(r"v([0-9]+)",row.name)
            if match and row.is_dir(): found.append(int(match.group(1)))
    return max(found,default=0)


def version_history_trees(spec_root: Path, artifact_root: Path, component: str="") -> list[Path]:
    """Every tree this spec's `_internal/versions/v{N}` chain has lived in.

    The reader's three layouts, in the same enumeration the reader uses: every
    producer cycle's `artifacts/spec` bucket (a sealed cycle keeps its chain
    even when it was never admitted to `shared/`), the immutable `shared/spec`
    revisions, and the legacy read-only bucket.
    """
    artifact_root=Path(artifact_root)
    def under(base: Path) -> Path: return base/component if component else base
    trees=[Path(spec_root),under(artifact_root/"spec")]
    trees.extend(under(bucket) for bucket,_ in READER.cycle_bucket_dirs(artifact_root,"spec"))
    shared=artifact_root/"shared"/"spec"
    if shared.is_dir():
        for ref in sorted(shared.iterdir()):
            revisions=ref/"revisions"
            if not revisions.is_dir(): continue
            trees.extend(under(rev) for rev in sorted(revisions.iterdir()) if rev.is_dir())
    return trees


def next_version(spec_root: Path, artifact_root: Path, component: str="") -> int:
    """The next v{N} on this spec's single canonical chain.

    While the cutover is active `spec_root` is the open cycle's `artifacts/spec`
    bucket, which is empty at the start of every cycle, so counting only that
    bucket restarted the chain at v1 even though the legacy read-only bucket
    already held v1..vN.  The chain therefore spans every tree the spec has
    lived in -- the current root, every producer cycle bucket, the immutable
    `shared/spec` revisions, and the legacy `spec/` fallback.  `component` keeps
    a component spec (`spec/<slug>/`) on its own chain instead of the root one,
    and snapshots still land under the current `spec_root`; only the number is
    continuous.
    """
    trees=version_history_trees(spec_root,artifact_root,component)
    return max((versions_max(tree) for tree in trees),default=0)+1


def read_regular_file(path: Path, *, allow_missing: bool) -> bytes | None:
    if not path.exists():
        if allow_missing:
            return None
        raise ValueError("missing")
    if path.is_symlink() or not path.is_file():
        raise ValueError("not-regular")
    return path.read_bytes()


def persist_snapshot(spec_root: Path, version: int, preimage: bytes) -> tuple[str, Path]:
    """Create the exact pre-image once, or verify an idempotent existing copy."""
    internal=spec_root/"_internal"
    versions=internal/"versions"
    for parent in (internal,versions):
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ValueError("snapshot-parent-not-directory")
        parent.mkdir(exist_ok=True)
    version_dir=versions/f"v{version}"
    if version_dir.exists() and (version_dir.is_symlink() or not version_dir.is_dir()):
        raise ValueError("snapshot-version-not-directory")
    version_dir.mkdir(exist_ok=True)
    snapshot=version_dir/"prd.md"
    if snapshot.exists():
        if snapshot.is_symlink() or not snapshot.is_file():
            raise ValueError("snapshot-not-regular")
        if snapshot.read_bytes()!=preimage:
            raise ValueError("snapshot-mismatch")
        return "matched",snapshot
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):
        flags|=os.O_NOFOLLOW
    fd=os.open(snapshot,flags,0o644)
    try:
        with os.fdopen(fd,"wb",closefd=True) as fh:
            fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    return "created",snapshot


def discard_unused_snapshot(spec_root: Path, version: int, snapshot: Path) -> None:
    snapshot.unlink(missing_ok=True)
    version_dir=spec_root/"_internal"/"versions"/f"v{version}"
    try:
        version_dir.rmdir()
        version_dir.parent.rmdir()
        version_dir.parent.parent.rmdir()
    except OSError:
        pass


def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True); run=sub.add_parser("run")
    run.add_argument("--artifact-root",required=True); run.add_argument("--worktree",required=True)
    run.add_argument("--route",required=True); run.add_argument("--node",required=True)
    run.add_argument("--spec-root",help="component spec root under <artifact-root>/spec")
    run.add_argument("--wait-timeout",type=float,default=600); run.add_argument("--poll",type=float,default=.05)
    run.add_argument("--events")
    run.add_argument("--require-snapshot",action="store_true",help=argparse.SUPPRESS)
    run.add_argument("transaction",nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.transaction[1:] if args.transaction[:1]==["--"] else args.transaction
    if not command: parser.error("transaction command required after --")
    artifact=Path(args.artifact_root).resolve(); worktree=Path(args.worktree)
    # W7C: the spec bucket lives under the open producer cycle once the
    # cutover is active (AGENT_ARTIFACT_CYCLE_DIR); the legacy top-level
    # `spec/` is only reachable during the compatibility window.
    try: spec_base,spec_layout=PRODUCER.resolve_output_dir(artifact,"spec")
    except PRODUCER.ProducerError as exc:
        emit({"status":"blocked","reason":exc.code,"detail":exc.detail,"artifact_root":str(artifact)},args.events); return 65
    spec_base=spec_base.resolve()
    spec_root=(Path(args.spec_root).expanduser() if args.spec_root else spec_base)
    # A relative component root is relative to the resolved spec bucket, which
    # is the open cycle's `artifacts/spec` once the cutover is active (W7D fix:
    # `artifact/<component>` pointed outside the cycle and was always blocked).
    if not spec_root.is_absolute(): spec_root=spec_base/spec_root
    spec_root=spec_root.resolve()
    try: component=spec_root.relative_to(spec_base).as_posix()
    except ValueError:
        emit({"status":"blocked","reason":"spec-root-outside-artifact","spec_root":str(spec_root),"spec_base":str(spec_base),"layout":spec_layout},args.events); return 65
    if component==".": component=""
    try: route,node,_=GUARD.validate_route_contract(args.route,args.node,worktree,artifact)
    except GUARD.WorkerRouteError as exc:
        emit({"status":"blocked","reason":exc.reason,"detail":str(exc),"route_id":exc.route_id,"route_file":args.route},args.events); return 65
    if not route.get("spec_touch"):
        emit({"status":"blocked","reason":"spec-touch-not-declared","route_id":route["route_id"],"route_file":args.route},args.events); return 65
    if not any((scope[:-3] if scope.endswith("/**") else scope)=="spec" or (scope[:-3] if scope.endswith("/**") else scope).startswith("spec/") for scope in node["write_scope"]):
        emit({"status":"blocked","reason":"route-node-scope-mismatch","route_id":route["route_id"],"node_id":node["id"]},args.events); return 65
    lock_path=artifact/".pipeline-lock"; lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open("a+",encoding="utf-8") as lock:
        blocked=False
        waited=False
        try: fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            blocked=True; waited=True; lock.seek(0); owner=lock.read().strip()
            emit({"status":"BLOCKED","action":"wait","route_id":route["route_id"],"owner":owner},args.events)
        deadline=time.monotonic()+max(0,args.wait_timeout)
        while blocked:
            try: fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB); blocked=False
            except BlockingIOError:
                if time.monotonic()>=deadline:
                    emit({"status":"blocked","reason":"spec-lock-timeout","route_id":route["route_id"]},args.events); return 3
                time.sleep(max(.01,args.poll))
        version=next_version(spec_root,artifact,component)
        owner={"route_id":route["route_id"],"node_id":node["id"],"worktree":str(worktree.resolve()),"pid":os.getpid(),"next_version":version}
        lock.seek(0); lock.truncate(); lock.write(json.dumps(owner,sort_keys=True)+"\n"); lock.flush(); os.fsync(lock.fileno())
        emit({"status":"acquired","action":"latest-reread","route_id":route["route_id"],"next_version":version,"waited":waited,"layout":spec_layout,"spec_root":str(spec_root)},args.events)
        prd=spec_root/"prd.md"
        try:
            preimage=read_regular_file(prd,allow_missing=True)
        except ValueError as exc:
            emit({"status":"blocked","reason":"prd-preimage-invalid","detail":str(exc),"route_id":route["route_id"]},args.events)
            lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
            return 65
        prepared_status="not-required-new"
        prepared_path=None
        if preimage is not None:
            try:
                prepared_status,prepared_path=persist_snapshot(spec_root,version,preimage)
            except (OSError,ValueError) as exc:
                reason="version-snapshot-mismatch" if str(exc)=="snapshot-mismatch" else "version-snapshot-invalid"
                emit({"status":"blocked","reason":reason,"detail":str(exc),"route_id":route["route_id"],"version":version},args.events)
                lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
                return 65
            emit({"status":"snapshot-prepared","snapshot":prepared_status,"route_id":route["route_id"],"version":version,"path":str(prepared_path),"preimage_sha256":hashlib.sha256(preimage).hexdigest()},args.events)
        env={**os.environ,"AGENT_SPEC_LOCK_HELD":"1","AGENT_SPEC_NEXT_VERSION":str(version),"AGENT_SPEC_ROOT":str(spec_root),"AGENT_ROUTE_FILE":str(Path(args.route).resolve()),"AGENT_ROUTE_ID":route["route_id"],"AGENT_ROUTE_NODE":node["id"]}
        result=subprocess.run(command,cwd=str(worktree),env=env)
        try:
            postimage=read_regular_file(prd,allow_missing=True)
        except ValueError as exc:
            emit({"status":"blocked","reason":"prd-result-invalid","detail":str(exc),"route_id":route["route_id"]},args.events)
            result=subprocess.CompletedProcess(command,65); postimage=None
        snapshot_status="not-required-new" if preimage is None else "not-required-unchanged"
        if preimage is not None and postimage!=preimage:
            try:
                snapshot_status,snapshot_path=persist_snapshot(spec_root,version,preimage)
            except (OSError,ValueError) as exc:
                reason="version-snapshot-mismatch" if str(exc)=="snapshot-mismatch" else "version-snapshot-invalid"
                emit({"status":"blocked","reason":reason,"detail":str(exc),"route_id":route["route_id"],"version":version},args.events)
                result=subprocess.CompletedProcess(command,65)
            else:
                emit({"status":"snapshot","snapshot":snapshot_status,"route_id":route["route_id"],"version":version,"path":str(snapshot_path),"preimage_sha256":hashlib.sha256(preimage).hexdigest()},args.events)
        elif preimage is not None and prepared_status=="created" and prepared_path is not None:
            discard_unused_snapshot(spec_root,version,prepared_path)
        if preimage is not None and postimage is None and result.returncode==0:
            emit({"status":"blocked","reason":"prd-missing-after-transaction","route_id":route["route_id"],"version":version},args.events)
            result=subprocess.CompletedProcess(command,65)
        emit({"status":"released","route_id":route["route_id"],"version":version,"result":result.returncode,"snapshot":snapshot_status},args.events)
        lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
        return result.returncode


if __name__=="__main__": raise SystemExit(main())
