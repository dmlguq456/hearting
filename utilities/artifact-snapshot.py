#!/usr/bin/env python3
"""Prepare exact pre-change snapshots for route-owned document artifacts."""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from artifact_context import scope_matches


OWNED_CONTAINERS={"documents","research"}
EXCLUDED_NAMES={"pipeline_summary.md","pipeline_state.yaml"}
# Only these buckets hold pre-change target documents. A draft/refine route's
# other declared scopes (frame shards, strategy, draft stage output, reviews,
# final) are support artifacts: they never had a target document to preserve,
# so they are not snapshot targets.
WORKTREE_ONLY={"source-scoped"}


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
        if not node_id:
            # The owner executes the transaction itself, so it names no node. Resolve the
            # route's one declared owner operation that mutates the target.
            owned=[row for row in route["nodes"] if row.get("kind")=="capability-owner"
                   and row.get("unit")=="_kernel/owner" and row.get("dispatch_depth")==1
                   and "target-artifact" in (row.get("write_scope") or [])]
            node=owned[0] if len(owned)==1 else {}
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
    # Current and historical cycle layouts both retain the bucket-relative
    # ownership rules below.
    prefix=()
    if len(parts)>=5 and parts[0]=="campaigns" and parts[2]=="cycles" and parts[4]=="artifacts":
        prefix=parts[:5]; parts=parts[5:]
    elif len(parts)>=4 and parts[0]=="campaigns" and parts[3]=="artifacts":
        prefix=parts[:4]; parts=parts[4:]
    elif parts[:1]==("shared",):
        raise SnapshotError("target-shared-immutable")
    if len(parts)<3 or parts[0] not in OWNED_CONTAINERS:
        raise SnapshotError("target-container-unowned")
    if "_internal" in parts or parts[-1] in EXCLUDED_NAMES:
        raise SnapshotError("target-machine-managed")
    return artifact_root.joinpath(*prefix,parts[0],parts[1]),Path(*parts[2:])


def worktree_target(artifact_root: Path, route: dict, target: Path) -> tuple[Path,Path] | None:
    """A document in the route's project, including a bound unversioned folder.

    Its pre-change copy is kept in the route's cycle, under the cycle's `artifacts/` as
    `_internal/versions/vN/<repository path>`; nothing is written into the repository. None when
    the target lies outside that repository (or in its `.git`) or the route has no cycle yet.
    """
    cwd=route.get("cwd")
    if not isinstance(cwd,str) or not cwd:
        return None
    try:
        top=subprocess.run(["git","-C",cwd,"rev-parse","--show-toplevel"],capture_output=True,text=True,timeout=10)
        if top.returncode==0 and top.stdout.strip():
            repository=Path(top.stdout.strip()).resolve()
        else:
            from dispatch_contract import diagnose_repository_start
            if (route.get("source_commit")!="unversioned"
                    or diagnose_repository_start(cwd).get("state")!="non-git"):
                return None
            repository=Path(cwd).resolve()
        relative=target.resolve(strict=False).relative_to(repository)
    except (OSError,ValueError,subprocess.SubprocessError):
        return None
    if not relative.parts or relative.parts[0]==".git":
        return None
    try:
        import artifact_producer
        record=artifact_producer.route_cycle_for(artifact_root,route)
        artifacts=artifact_producer._cycle_artifacts_dir(artifact_root,record["cycle_id"]) if record else None
    except Exception:
        return None
    return (artifacts,relative) if artifacts else None


def bucket_relative_parts(artifact_root: Path, target: Path) -> tuple[str,...]:
    """The target's path below the artifact root, with the cycle prefix removed."""
    resolved=target.resolve(strict=False)
    try:
        rel=resolved.relative_to(artifact_root)
    except ValueError:
        return ()
    parts=rel.parts
    if len(parts)>=5 and parts[0]=="campaigns" and parts[2]=="cycles" and parts[4]=="artifacts":
        return parts[5:]
    if len(parts)>=4 and parts[0]=="campaigns" and parts[3]=="artifacts":
        return parts[4:]
    return parts


def component_match(values: tuple[str,...], pattern: str) -> bool:
    parts=pattern.split("/") if pattern else []
    recursive=bool(parts and parts[-1]=="**")
    if recursive:
        parts=parts[:-1]
        if len(values)<len(parts):
            return False
    elif len(values)!=len(parts):
        return False
    return all(fnmatch.fnmatchcase(value_part,pattern_part)
               for value_part,pattern_part in zip(values,parts))


def declared_support_target(node: dict, relative: tuple[str,...]) -> bool:
    """Whether the target sits in a declared, non-document node write scope.

    A draft/refine node's document targets are ``documents/**``,
    ``research/**`` or the ``target-artifact`` alias for them. Everything else
    the node declares (frame shards, strategy, stage output, reviews, final)
    is a support artifact with no pre-change document to preserve. This keeps
    the refusal for an owned-bucket path that is merely malformed, because
    such a path matches no declared support scope here.
    """
    if not relative:
        return False
    for scope in node.get("write_scope") or []:
        if not isinstance(scope,str) or not scope or scope=="target-artifact":
            continue
        root=scope[:-3] if scope.endswith("/**") else scope
        first=root.split("/",1)[0]
        if first in OWNED_CONTAINERS or first in WORKTREE_ONLY or first=="source":
            continue
        if scope_matches("/".join(relative), (scope,)):
            return True
    return False


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
    if (route.get("artifact_root") and route.get("route_hash")
            and (os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_ROUTE_ID"))):
        from artifact_context import resolve_context
        context = resolve_context(
            environ=os.environ, route=args.route, node_id=args.node,
            explicit={"artifact_root": artifact_root},
        )
        route, node = context.route, context.node
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
    if capability == "autopilot-refine" and intensity == "quick":
        from artifact_producer import _quick_refine_write_gate, ProducerError
        try:
            _quick_refine_write_gate(artifact_root, target, route)
        except ProducerError as exc:
            raise SnapshotError(exc.code + ": " + exc.detail) from exc
        # The preview is output of this same conductor, before approval. It is
        # not a target document and must not enter the target snapshot path.
        try:
            rel_parts = target.resolve().relative_to(artifact_root).parts
        except ValueError:
            rel_parts = ()     # a repository document, handled below
        if rel_parts[:1] == ("campaigns",):
            index = 4 if len(rel_parts) > 2 and rel_parts[2] == "cycles" else 3
            rel_parts = rel_parts[index + 1:]
        if rel_parts[:2] == ("reviews", "refine") and "reviews/refine/**" in node.get("write_scope", []):
            emit({"status": "skipped", "reason": "quick-preview-artifact", "target": str(target)})
            return 0
    in_repository=False
    try:
        artifact_dir,relative=target_parts(artifact_root,target)
    except SnapshotError as exc:
        reason=str(exc)
        if reason=="target-machine-managed":
            emit({"status":"skipped","reason":reason,"target":str(target)})
            return 0
        # A draft/refine route's declared support scopes are not target
        # documents. Skip only a path that a node actually declared as
        # support; a malformed owned path or an undeclared container still
        # fails closed below.
        if reason=="target-container-unowned" and declared_support_target(
            node,bucket_relative_parts(artifact_root,target)
        ):
            emit({"status":"skipped","reason":"support-artifact","target":str(target)})
            return 0
        found=worktree_target(artifact_root,route,target) if reason=="target-outside-artifact-root" else None
        if found is None:
            raise
        (artifact_dir,relative),in_repository=found,True
    if capability == "autopilot-refine" and intensity != "quick":
        # The owner's own `transaction` changes the target after the person released the preview it
        # raised: the fence the entry gate holds for a child launch, asked only once the path is known
        # to be the bound target, for the step that precedes the edit.
        import dispatch_contract
        if node.get("id") in {gated.get("id") for gated, _gate in dispatch_contract.owner_operation_gates(route)}:
            try:
                dispatch_contract.owner_operation_fence(route, node, jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None)
            except dispatch_contract.DispatchContractError as exc:
                raise SnapshotError(exc.reason + ": " + exc.detail) from exc
    if not target.exists():
        emit({"status":"skipped","reason":"new-target","target":str(target)})
        return 0
    if target.is_symlink() or not target.is_file():
        raise SnapshotError("target-not-regular")
    preimage=target.read_bytes()
    internal=artifact_dir/"_internal"
    if not internal.exists() and not in_repository:
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
                matches.append((version,row,receipt))
        if len(matches)>1:
            raise SnapshotError("duplicate-route-snapshot-receipts")
        if matches:
            version,version_dir,receipt=matches[0]
        else:
            version=max((number for number,_ in rows),default=0)+1
            version_dir=versions/f"v{version}"
            ensure_directory(version_dir)
            receipt={"artifact":str(artifact_dir.relative_to(artifact_root)),"capability":capability,"route_hash":route.get("route_hash"),"route_id":args.route_id,"version":version}
            exclusive_write(version_dir/".snapshot-receipt.json",(json.dumps(receipt,sort_keys=True)+"\n").encode())
        snapshot=version_dir/relative
        outcome=exclusive_write(snapshot,preimage)
        receipt.setdefault("targets",{})[relative.as_posix()]={"path":str(target.resolve()),
            "preimage_sha256":hashlib.sha256(preimage).hexdigest()}
        from artifact_producer_store import _write_atomic
        _write_atomic(version_dir/".snapshot-receipt.json",(json.dumps(receipt,sort_keys=True)+"\n").encode())
    emit({"status":"snapshot","snapshot":outcome,"route_id":args.route_id,"version":version,"path":str(snapshot),"target":str(target.resolve()),"preimage_sha256":hashlib.sha256(preimage).hexdigest()})
    return 0


def _route_defaults(argv):
    values = list(argv)
    if not values or values[0] != "prepare":
        return values
    flags = ("--artifact-root", "--route", "--route-id", "--node")
    if all(any(item == flag or item.startswith(flag + "=") for item in values) for flag in flags):
        return values
    from artifact_context import resolve_context
    explicit_root = None
    for index, item in enumerate(values):
        if item == "--artifact-root" and index + 1 < len(values):
            explicit_root = values[index + 1]
        elif item.startswith("--artifact-root="):
            explicit_root = item.split("=", 1)[1]
    context = resolve_context(environ=os.environ,
        explicit={"artifact_root": explicit_root} if explicit_root else None)
    if not context.route or not context.node or not context.artifact_root:
        return values
    defaults = {
        "--artifact-root": str(context.artifact_root),
        "--route": str(context.artifact_root / ".runtime" / "routes" / f"{context.route['route_id']}.json"),
        "--route-id": str(context.route["route_id"]),
        "--node": str(context.node_id),
    }
    insertion = 1
    for flag, value in defaults.items():
        if not any(item == flag or item.startswith(flag + "=") for item in values):
            values[insertion:insertion] = [flag, value]
            insertion += 2
    return values


def main() -> int:
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="command",required=True)
    prepare_parser=sub.add_parser("prepare")
    prepare_parser.add_argument("--artifact-root",required=True)
    prepare_parser.add_argument("--target",required=True)
    prepare_parser.add_argument("--route",required=True)
    prepare_parser.add_argument("--route-id",required=True)
    prepare_parser.add_argument("--node",required=True)
    args=parser.parse_args(_route_defaults(sys.argv[1:]))
    try:
        return prepare(args)
    except (OSError,SnapshotError,ValueError) as exc:
        emit({"status":"blocked","reason":str(exc),"target":getattr(args,"target",None)},error=True)
        return 65


if __name__=="__main__":
    raise SystemExit(main())
