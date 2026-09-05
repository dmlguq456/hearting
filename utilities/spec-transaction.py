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
import artifact_cutover as CUTOVER  # noqa: E402


def emit(event, events=None):
    line=json.dumps(event,sort_keys=True)
    print(line,flush=True)
    if events:
        with open(events,"a",encoding="utf-8") as fh: fh.write(line+"\n")


def seed_cycle_spec(spec_base: Path, artifact: Path):
    """W7C cycle layout: a fresh cycle's `artifacts/spec` is empty, so the
    transaction would see no pre-image and write no `_internal/versions/vN`
    snapshot -- which is why operators copied the previous version by hand
    (cairn v169/v170, defect K). Seed the whole latest shared/spec revision
    (every component, D-87) plus the `_internal/versions/v*` history of every
    earlier revision of that reference, so the pre-image, the version counter
    and the snapshot all come from the tool. Returns an event dict."""
    if any(p.is_file() for p in spec_base.rglob("*")):
        has_prd=(spec_base/"prd.md").is_file() or any(p.name=="prd.md" and p.parent.parent==spec_base for p in spec_base.rglob("prd.md"))
        return {"status":"seed-skipped","reason":"spec-base-not-empty","prd_present":has_prd,"spec_base":str(spec_base)}
    revision=CUTOVER.latest_shared_revision(artifact,"spec")
    if revision is None:
        return {"status":"seed-skipped","reason":"no-shared-revision","spec_base":str(spec_base)}
    copied=0
    for src in sorted(revision.rglob("*")):
        if not src.is_file() or src.is_symlink():
            continue
        rel=src.relative_to(revision)
        if rel.as_posix()=="revision.json":
            continue
        dst=spec_base/rel; dst.parent.mkdir(parents=True,exist_ok=True)
        dst.write_bytes(src.read_bytes()); copied+=1
    history=0
    revisions_dir=revision.parent
    if revisions_dir.name=="revisions":
        for other in sorted(revisions_dir.iterdir()):
            if other==revision or not other.is_dir():
                continue
            for src in sorted(other.rglob("prd.md")):
                rel=src.relative_to(other)
                parts=rel.parts
                # <component?>/_internal/versions/vN/prd.md -- immutable history rows only
                if len(parts)<4 or parts[-4]!="_internal" or parts[-3]!="versions" or not re.fullmatch(r"v[0-9]+",parts[-2]):
                    continue
                dst=spec_base/rel
                if dst.exists() or src.is_symlink() or not src.is_file():
                    continue
                dst.parent.mkdir(parents=True,exist_ok=True); dst.write_bytes(src.read_bytes()); history+=1
    versions=[p for p in spec_base.rglob("prd.md") if len(p.relative_to(spec_base).parts)>=4 and p.relative_to(spec_base).parts[-4]=="_internal"]
    return {"status":"seeded","source":str(revision),"files":copied,"history_versions":history,
            "version_history_present":bool(versions),"spec_base":str(spec_base)}


def legacy_spec_state(artifact: Path):
    legacy=artifact/"spec"
    state={}
    if not legacy.is_dir():
        return state
    for path in legacy.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.name!=".pipeline-lock":
            st=path.stat(); state[path.relative_to(legacy).as_posix()]=(st.st_size,st.st_mtime_ns)
    return state


def next_version(spec_root: Path) -> int:
    versions=spec_root/"_internal"/"versions"
    found=[]
    if versions.is_dir():
        for row in versions.iterdir():
            match=re.fullmatch(r"v([0-9]+)",row.name)
            if match and row.is_dir(): found.append(int(match.group(1)))
    return max(found,default=0)+1


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
    try: spec_root.relative_to(spec_base)
    except ValueError:
        emit({"status":"blocked","reason":"spec-root-outside-artifact","spec_root":str(spec_root),"spec_base":str(spec_base),"layout":spec_layout},args.events); return 65
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
        if spec_layout=="cycle":
            # Seed only once the route contract passed and the spec lock is ours:
            # a refused run must leave no admittable content behind, and two
            # transactions on one cycle must not interleave a half copy.
            seeded=seed_cycle_spec(spec_base,artifact)
            emit({**seeded,"route_id":route["route_id"]},args.events)
            if seeded["status"]=="seeded" and not seeded["version_history_present"]:
                emit({"status":"version-history-absent","route_id":route["route_id"],"detail":"seeded revision carries no _internal/versions history; the counter restarts at 1"},args.events)
        version=next_version(spec_root)
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
        legacy_before=legacy_spec_state(artifact) if spec_layout=="cycle" else {}
        result=subprocess.run(command,cwd=str(worktree),env=env)
        if spec_layout=="cycle":
            # Defect K: the transaction protected an empty cycle directory while
            # the child wrote the legacy `spec/` tree and reported success. A
            # cycle-layout transaction whose window changed the legacy bucket is
            # a typed failure, whatever the child's exit code said.
            legacy_after=legacy_spec_state(artifact)
            changed=sorted(set(k for k in set(legacy_before)|set(legacy_after) if legacy_before.get(k)!=legacy_after.get(k)))
            if changed:
                emit({"status":"blocked","reason":"legacy-spec-written","route_id":route["route_id"],"changed":changed[:20],"spec_root":str(spec_root)},args.events)
                result=subprocess.CompletedProcess(command,65)
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
