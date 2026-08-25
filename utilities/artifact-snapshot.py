#!/usr/bin/env python3
"""Prepare exact pre-change snapshots for route-owned document artifacts."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path


OWNED_CONTAINERS={"documents","research"}
EXCLUDED_NAMES={"pipeline_summary.md","pipeline_state.yaml"}


class SnapshotError(Exception):
    pass


def emit(payload: dict, *, error: bool=False) -> None:
    print(json.dumps(payload,sort_keys=True),file=sys.stderr if error else sys.stdout,flush=True)


def load_route(path: Path, route_id: str, node_id: str) -> tuple[dict,dict]:
    try:
        route=json.loads(path.read_text(encoding="utf-8"))
        if route.get("route_id")!=route_id:
            raise SnapshotError("route-id-mismatch")
        # An owner binding carries no route node (SD-97, empty node_id by
        # contract). There is no per-node record to look up, so use an empty
        # node instead of letting `next()` raise StopIteration for a node id
        # that was never meant to exist.
        node=next(row for row in route["nodes"] if row["id"]==node_id) if node_id else {}
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError("route-invalid") from exc
    return route,node


def target_parts(artifact_root: Path, target: Path) -> tuple[Path,Path]:
    resolved=target.resolve(strict=False)
    try:
        rel=resolved.relative_to(artifact_root)
    except ValueError as exc:
        raise SnapshotError("target-outside-artifact-root") from exc
    parts=rel.parts
    # W7C cycle layout: campaigns/<camp>/cycles/<cyc>/artifacts/<bucket>/<name>/...
    prefix=()
    if len(parts)>=5 and parts[0]=="campaigns" and parts[2]=="cycles" and parts[4]=="artifacts":
        prefix=parts[:5]; parts=parts[5:]
    elif parts[:1]==("shared",):
        raise SnapshotError("target-shared-immutable")
    if len(parts)<3 or parts[0] not in OWNED_CONTAINERS:
        raise SnapshotError("target-container-unowned")
    if "_internal" in parts or parts[-1] in EXCLUDED_NAMES:
        raise SnapshotError("target-machine-managed")
    return artifact_root.joinpath(*prefix,parts[0],parts[1]),Path(*parts[2:])


def ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise SnapshotError("snapshot-parent-invalid")
        return
    parent=path.parent
    if parent!=path:
        ensure_directory(parent)
    path.mkdir()


def version_rows(versions: Path) -> list[tuple[int,Path]]:
    rows=[]
    if versions.is_dir():
        for row in versions.iterdir():
            match=re.fullmatch(r"v([0-9]+)",row.name)
            if match and row.is_dir() and not row.is_symlink():
                rows.append((int(match.group(1)),row))
    return sorted(rows)


def read_receipt(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SnapshotError("snapshot-receipt-invalid")
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotError("snapshot-receipt-invalid") from exc
    return value if isinstance(value,dict) else None


def exclusive_write(path: Path, payload: bytes) -> str:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise SnapshotError("snapshot-target-invalid")
        if path.read_bytes()!=payload:
            raise SnapshotError("snapshot-preimage-mismatch")
        return "matched"
    ensure_directory(path.parent)
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):
        flags|=os.O_NOFOLLOW
    fd=os.open(path,flags,0o644)
    try:
        with os.fdopen(fd,"wb",closefd=True) as fh:
            fh.write(payload); fh.flush(); os.fsync(fh.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return "created"


def prepare_legacy(target: Path, preimage: bytes, route_id: str) -> int | None:
    expression=re.compile(rf"{re.escape(target.stem)}_v([0-9]+){re.escape(target.suffix)}")
    rows=[]
    for row in target.parent.iterdir():
        match=expression.fullmatch(row.name)
        if match and row.is_file() and not row.is_symlink():
            rows.append((int(match.group(1)),row))
    if not rows:
        return None
    rows.sort()
    version,snapshot=rows[-1]
    if snapshot.read_bytes()==preimage:
        emit({"status":"snapshot","snapshot":"matched-legacy","route_id":route_id,"version":version,"path":str(snapshot),"preimage_sha256":hashlib.sha256(preimage).hexdigest()})
        return 0
    version=rows[-1][0]+1
    snapshot=target.with_name(f"{target.stem}_v{version}{target.suffix}")
    outcome=exclusive_write(snapshot,preimage)
    emit({"status":"snapshot","snapshot":f"{outcome}-legacy","route_id":route_id,"version":version,"path":str(snapshot),"preimage_sha256":hashlib.sha256(preimage).hexdigest()})
    return 0


def prepare(args) -> int:
    artifact_root=Path(args.artifact_root).expanduser().resolve()
    target=Path(args.target).expanduser()
    if not target.is_absolute():
        target=Path.cwd()/target
    route,node=load_route(Path(args.route).expanduser().resolve(),args.route_id,args.node)
    capability=route.get("capability")
    intensity=route.get("effective_intensity")
    if capability=="autopilot-refine" and intensity=="direct":
        emit({"status":"skipped","reason":"minor-direct-edit","target":str(target)})
        return 0
    if capability=="autopilot-refine" and "target-artifact" not in node.get("write_scope",[]):
        emit({"status":"skipped","reason":"node-does-not-mutate-target","target":str(target)})
        return 0
    if capability not in {"autopilot-refine","autopilot-draft"}:
        emit({"status":"skipped","reason":"capability-does-not-own-snapshots","target":str(target)})
        return 0
    try:
        artifact_dir,relative=target_parts(artifact_root,target)
    except SnapshotError as exc:
        if str(exc)=="target-machine-managed":
            emit({"status":"skipped","reason":str(exc),"target":str(target)})
            return 0
        raise
    if not target.exists():
        emit({"status":"skipped","reason":"new-target","target":str(target)})
        return 0
    if target.is_symlink() or not target.is_file():
        raise SnapshotError("target-not-regular")
    preimage=target.read_bytes()
    internal=artifact_dir/"_internal"
    if not internal.exists():
        legacy_result=prepare_legacy(target,preimage,args.route_id)
        if legacy_result is not None:
            return legacy_result
    versions=internal/"versions"
    ensure_directory(internal)
    lock_path=internal/".versions.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        ensure_directory(versions)
        rows=version_rows(versions)
        matches=[]
        for version,row in rows:
            receipt=read_receipt(row/".snapshot-receipt.json")
            if receipt and receipt.get("route_id")==args.route_id and receipt.get("route_hash")==route.get("route_hash"):
                matches.append((version,row))
        if len(matches)>1:
            raise SnapshotError("duplicate-route-snapshot-receipts")
        if matches:
            version,version_dir=matches[0]
        else:
            version=max((number for number,_ in rows),default=0)+1
            version_dir=versions/f"v{version}"
            ensure_directory(version_dir)
            receipt={"artifact":str(artifact_dir.relative_to(artifact_root)),"capability":capability,"route_hash":route.get("route_hash"),"route_id":args.route_id,"version":version}
            exclusive_write(version_dir/".snapshot-receipt.json",(json.dumps(receipt,sort_keys=True)+"\n").encode())
        snapshot=version_dir/relative
        outcome=exclusive_write(snapshot,preimage)
    emit({"status":"snapshot","snapshot":outcome,"route_id":args.route_id,"version":version,"path":str(snapshot),"preimage_sha256":hashlib.sha256(preimage).hexdigest()})
    return 0


def main() -> int:
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="command",required=True)
    prepare_parser=sub.add_parser("prepare")
    prepare_parser.add_argument("--artifact-root",required=True)
    prepare_parser.add_argument("--target",required=True)
    prepare_parser.add_argument("--route",required=True)
    prepare_parser.add_argument("--route-id",required=True)
    prepare_parser.add_argument("--node",required=True)
    args=parser.parse_args()
    try:
        return prepare(args)
    except (OSError,SnapshotError) as exc:
        emit({"status":"blocked","reason":str(exc),"target":getattr(args,"target",None)},error=True)
        return 65


if __name__=="__main__":
    raise SystemExit(main())
