#!/usr/bin/env python3
"""Create and verify content-bound smoke attestations before expensive lab runs."""
import argparse, hashlib, json, os, re, subprocess, sys
from pathlib import Path

UTILITIES = Path(__file__).resolve().parents[1] / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))
import resource_execution_environment as RESOURCE_ENV

def digest(path):
    path=Path(path)
    if path.is_file(): return hashlib.sha256(path.read_bytes()).hexdigest()
    rows=[]
    for item in sorted(x for x in path.rglob("*") if x.is_file()):
        rows.append((str(item.relative_to(path)),hashlib.sha256(item.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(rows,separators=(",",":")).encode()).hexdigest()

def _verify_config_manifest(path):
    tool = Path(__file__).with_name("lab-config-provenance.py")
    result = subprocess.run([sys.executable, str(tool), "verify", "--manifest", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("config manifest failed verification: " + result.stderr.strip())

def payload(paths, command, cwd, config_manifest=None):
    execution_environment = RESOURCE_ENV.execution_identity(command, cwd, environ=os.environ)
    if not RESOURCE_ENV.valid_execution_identity(execution_environment):
        raise ValueError("smoke execution environment is not parseable")
    data = {"schema_version":1,"cwd":str(Path(cwd).resolve(strict=True)),"command":command,
            "execution_environment":execution_environment}
    input_rows = [{"path":str(Path(p).resolve(strict=True)),"sha256":digest(p)} for p in paths]
    if config_manifest:
        # Full rule 1-8 contract first (schema, hashes, in-situ location); this
        # only allows source *absence*, so attest-time presence (A5) is a
        # separate, stricter check below.
        _verify_config_manifest(config_manifest)
        manifest = json.loads(Path(config_manifest).read_text())
        snapshot = Path(manifest["snapshot_path"]).resolve(strict=True)
        source_raw = Path(manifest["source_path"])
        if not source_raw.exists():
            raise ValueError("config manifest source is missing: " + str(source_raw))
        source = source_raw.resolve(strict=True)
        source_digest = digest(source)
        if source_digest != manifest["source_sha256"]:
            raise ValueError("config manifest source is stale: " + str(source))
        input_rows.append({"path": str(snapshot), "sha256": manifest["snapshot_sha256"]})
        input_rows.append({"path": str(source), "sha256": source_digest})
        data["config_sha256"] = manifest["snapshot_sha256"]
        data["config_source_sha256"] = manifest["source_sha256"]
        data["config_source_path"] = str(source)
    dedup = {}
    for row in input_rows:
        dedup[row["path"]] = row
    data["inputs"] = [dedup[key] for key in sorted(dedup)]
    return data

CONFIG_KEYS = ("config_sha256", "config_source_sha256", "config_source_path")

def verify(data):
    claimed=data.get("attestation_hash")
    if not isinstance(claimed,str) or not claimed:
        raise ValueError("attestation hash is required")
    bare={k:v for k,v in data.items() if k!="attestation_hash"}
    actual="sha256:"+hashlib.sha256(json.dumps(bare,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if claimed!=actual: raise ValueError("attestation hash mismatch")
    if ("execution_environment" in data
            and not RESOURCE_ENV.valid_execution_identity(data["execution_environment"])):
        raise ValueError("smoke execution environment is invalid")
    for row in data["inputs"]:
        if not Path(row["path"]).exists() or digest(row["path"])!=row["sha256"]: raise ValueError("stale smoke input: "+row["path"])
    present = [k for k in CONFIG_KEYS if k in data]
    if present and len(present) != 3:
        raise ValueError(
            "config provenance must be absent as a whole or contain all of "
            + ", ".join(CONFIG_KEYS) + "; missing: "
            + ", ".join(k for k in CONFIG_KEYS if k not in data))
    if present:
        for key in ("config_sha256", "config_source_sha256"):
            if not isinstance(data[key], str) or not re.fullmatch(r"[0-9a-f]{64}", data[key]):
                raise ValueError(f"{key} must be a 64-character lowercase hex digest")
        if not isinstance(data["config_source_path"], str) or not data["config_source_path"]:
            raise ValueError("config_source_path must be a non-empty string")
        if not any(row["sha256"] == data["config_sha256"] for row in data["inputs"]):
            raise ValueError("config hash is not bound to an input")
        # Hash-only binding would be vacuous when source_sha256 == snapshot_sha256
        # (the snapshot row alone would satisfy it); a path match is required too.
        match = next((row for row in data["inputs"] if row["path"] == data["config_source_path"]), None)
        if not match or match["sha256"] != data["config_source_sha256"]:
            raise ValueError("config source is not bound to an input")
        # The snapshot row must be proven by a distinct input row carrying the
        # config hash, UNLESS the source's own real bytes already are the
        # snapshot bytes (config_source_sha256 == config_sha256) -- the only
        # legitimate degenerate case, since source-path binding above already
        # forces config_source_sha256 to be the file's real digest. A
        # filename/stem heuristic here is forgeable (round 5 plan-check
        # blocking finding): a file merely *named* like the hash, with
        # unrelated real content, must not satisfy this.
        snapshot_rows = [row for row in data["inputs"] if row["sha256"] == data["config_sha256"]]
        source_is_snapshot = data["config_source_sha256"] == data["config_sha256"]
        if len(snapshot_rows) < 2 and not source_is_snapshot:
            raise ValueError(
                "config snapshot row is not bound to an input: expected a "
                "distinct input row carrying the config hash beside the "
                "source row")
    if data.get("status")!="passed" or data.get("exit_code")!=0: raise ValueError("smoke did not pass")
    return True


def verify_execution_environment(data, command, cwd, *, environ=None, remote=None):
    """Require a new launch to use the environment identity captured by smoke."""
    recorded = data.get("execution_environment") if isinstance(data, dict) else None
    if not RESOURCE_ENV.valid_execution_identity(recorded):
        raise ValueError("smoke execution environment is unbound")
    current = RESOURCE_ENV.execution_identity(command, cwd, environ=environ, remote=remote)
    if not RESOURCE_ENV.valid_execution_identity(current) or current != recorded:
        raise ValueError("smoke execution environment mismatch")
    return True

def _route_defaults(argv):
    values = list(argv)
    if not values or values[0] != "attest":
        return values
    from pathlib import Path
    import os, sys
    utilities = Path(__file__).resolve().parents[1] / "utilities"
    if str(utilities) not in sys.path:
        sys.path.insert(0, str(utilities))
    from artifact_context import ArtifactContextError, resolve_context
    context = resolve_context(environ=os.environ)
    if not context.route or not context.node:
        return values
    def option_value(name):
        try:
            index = values.index(name)
            return values[index + 1]
        except (ValueError, IndexError):
            found = next((part[len(name) + 1:] for part in values if part.startswith(name + "=")), None)
            return found
    divider = values.index("--") if "--" in values else len(values)
    if option_value("--cwd") is None:
        if not context.source_worktree:
            raise ArtifactContextError("source-worktree-unavailable")
        values[1:1] = ["--cwd", str(context.source_worktree)]
        divider += 2
    else:
        supplied_cwd = Path(option_value("--cwd")).expanduser().resolve(strict=False)
        if context.source_worktree and supplied_cwd != context.source_worktree:
            raise ArtifactContextError("source-worktree-route-mismatch", str(supplied_cwd))
    output = option_value("--output")
    try:
        binding = context.resolve_output("smoke-attestation", explicit=output)
    except ArtifactContextError:
        raise
    if binding is None or binding.path is None:
        raise ArtifactContextError("artifact-output-role-missing", "smoke-attestation")
    if output is None:
        values[divider:divider] = ["--output", str(binding.path)]
    return values


def main(argv=None):
    p=argparse.ArgumentParser(); s=p.add_subparsers(dest="cmd",required=True)
    a=s.add_parser("attest"); a.add_argument("--input",action="append",required=True); a.add_argument("--config-manifest"); a.add_argument("--cwd",required=True); a.add_argument("--output",required=True); a.add_argument("command",nargs=argparse.REMAINDER)
    v=s.add_parser("verify"); v.add_argument("--attestation",required=True)
    try:
        import sys
        utilities = Path(__file__).resolve().parents[1] / "utilities"
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        from artifact_context import ArtifactContextError
        x=p.parse_args(_route_defaults(sys.argv[1:] if argv is None else argv))
    except ArtifactContextError as e:
        raise ValueError(str(e)) from e
    if x.cmd=="verify": verify(json.loads(Path(x.attestation).read_text())); print("smoke_attestation=valid"); return
    command=x.command[1:] if x.command[:1]==["--"] else x.command
    if not command: raise ValueError("smoke command required")
    data=payload(x.input,command,x.cwd,x.config_manifest); result=subprocess.run(command,cwd=data["cwd"])
    data.update(exit_code=result.returncode,status="passed" if result.returncode==0 else "failed")
    data["attestation_hash"]="sha256:"+hashlib.sha256(json.dumps(data,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    Path(x.output).parent.mkdir(parents=True,exist_ok=True); Path(x.output).write_text(json.dumps(data,indent=2)+"\n")
    raise SystemExit(result.returncode)
if __name__=="__main__":
 try: main()
 except ValueError as e: print("smoke-attestation:",e,file=sys.stderr); raise SystemExit(65)
