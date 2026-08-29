#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit): enforce the canonical artifact-root write
# boundary and the route-backed spec_touch declaration.
# The canonical artifact root is .agent_reports; .claude_reports is a legacy alias.
#
# Non-blocking by convention: edits to existing artifacts, source code,
# experiments/, user_profile/, README, assets, and _internal.
# WORKFLOW.md §0 is the source of truth.
set -euo pipefail

fp=""
sid=""
cmd=""
route_file="${AGENT_ROUTE_FILE:-}"
route_id="${AGENT_ROUTE_ID:-unknown}"
route_node="${AGENT_ROUTE_NODE:-}"

route_failure(){
  reason=$1
  python3 - "$reason" "$route_id" "$route_file" "${fp:-}" <<'PY' >&2
import json,sys
print(json.dumps({"status":"blocked","reason":sys.argv[1],"route_id":sys.argv[2],"route_file":sys.argv[3],"target":sys.argv[4]},sort_keys=True))
PY
}

if [ "$#" -gt 0 ]; then
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)
        [ "$#" -ge 2 ] || { echo "artifact-guard: --file requires a path" >&2; exit 64; }
        fp="$2"; shift 2 ;;
      --session)
        [ "$#" -ge 2 ] || { echo "artifact-guard: --session requires an id" >&2; exit 64; }
        sid="$2"; shift 2 ;;
      --command)
        [ "$#" -ge 2 ] || { echo "artifact-guard: --command requires a string" >&2; exit 64; }
        cmd="$2"; shift 2 ;;
      --help|-h)
        echo "usage: artifact-guard.sh --file <path> [--session <id>] | --command <shell> [--session <id>]"
        exit 0 ;;
      *)
        echo "artifact-guard: unknown argument: $1" >&2
        exit 64 ;;
    esac
  done
else
  input=$(cat)
  eval "$(printf '%s' "$input" | python3 -c '
import sys, json, shlex
try: d = json.load(sys.stdin)
except Exception: d = {}
ti = d.get("tool_input") or {}
print("FP="+shlex.quote(ti.get("file_path","") or ""))
print("SID="+shlex.quote(d.get("session_id","") or ""))
print("TOOLNAME="+shlex.quote(d.get("tool_name","") or ""))
print("CMD="+shlex.quote(ti.get("command","") or ""))
' 2>/dev/null)"
  fp="${FP:-}"; sid="${SID:-}"; cmd="${CMD:-}"
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARTIFACT_ROOT_RESOLVER="$SCRIPT_DIR/../utilities/artifact-root.sh"

# ---- Bash channel (Tier A/B, C-2b) ----
# `file_path` is absent for Bash tool calls, so `fp` is empty here by
# construction. Only enter Bash mode when a command was actually supplied
# (never both fp and cmd non-empty from a single invocation).
if [ -z "$fp" ] && [ -n "$cmd" ]; then
  parsed=$(python3 "$SCRIPT_DIR/artifact_write_targets.py" --command "$cmd" --cwd "$PWD" 2>/dev/null) || parsed='{"decidable":[],"undecidable":[]}'

  undecidable_count=$(printf '%s' "$parsed" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("undecidable") or []))' 2>/dev/null || echo 0)
  if [ "${undecidable_count:-0}" != "0" ] 2>/dev/null; then
    canonical_for_obs=$("$ARTIFACT_ROOT_RESOLVER" "$PWD" 2>/dev/null) || canonical_for_obs=""
    if [ -n "$canonical_for_obs" ]; then
      obs_dir="$canonical_for_obs/.runtime/observations"
      mkdir -p "$obs_dir" 2>/dev/null || true
      python3 - "$obs_dir/undecidable-write-channel.jsonl" "$route_id" "$route_node" "${sid:-artifact-guard}" "$parsed" <<'PY' 2>/dev/null || true
import hashlib, json, sys, time
out_path, route_id, route_node, session, parsed_json = sys.argv[1:6]
try:
    parsed = json.loads(parsed_json)
except Exception:
    parsed = {"undecidable": []}
lines = []
for entry in parsed.get("undecidable") or []:
    segment = str(entry.get("segment", ""))
    lines.append(json.dumps({
        "ts": time.time(),
        "route_id": route_id,
        "route_node": route_node,
        "session": session,
        "reason": entry.get("reason", "unknown"),
        "segment_digest": hashlib.sha256(segment[:200].encode("utf-8", "replace")).hexdigest(),
    }, sort_keys=True))
if lines:
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
PY
    fi
  fi

  decidable_targets=$(printf '%s' "$parsed" | python3 -c 'import json,sys
for p in (json.load(sys.stdin).get("decidable") or []):
    print(p)' 2>/dev/null || true)

  if [ -n "$decidable_targets" ]; then
    while IFS= read -r target; do
      [ -z "$target" ] && continue
      # Re-dispatch through the exact same single-target pipeline used for
      # Edit/Write/MultiEdit (canonical-root boundary -> _internal exemption
      # -> spec_touch -> node write_scope). That pipeline already emits its
      # own route_failure JSON and exit 2 on denial; just propagate it.
      "$0" --file "$target" --session "${sid:-artifact-guard}" || exit 2
    done <<EOF
$decidable_targets
EOF
  fi
  exit 0
fi

[ -z "$fp" ] && exit 0

case "$fp" in
  /*) ;;
  *) fp="$PWD/$fp" ;;
esac

# ---- Source-only linked worktree gate ----
# A tracked artifact snapshot can exist in every Git worktree. It is read-only
# shadow state: only the canonical artifact root selected by artifact-root.sh is
# writable. Run this before the _internal snapshot exemption.
case "$fp" in
  */.agent_reports/*) local_project=${fp%%/.agent_reports/*}; local_artifact="$local_project/.agent_reports" ;;
  */.claude_reports/*) local_project=${fp%%/.claude_reports/*}; local_artifact="$local_project/.claude_reports" ;;
  *) local_project=""; local_artifact="" ;;
esac
if [ -n "$local_project" ]; then
  canonical=$("$ARTIFACT_ROOT_RESOLVER" "$local_project" 2>/dev/null) || {
    [ -z "$route_file" ] || route_failure "canonical-artifact-root-unresolved"
    echo "⛔ Cannot resolve the canonical artifact root; artifact writes fail closed: $fp" >&2
    exit 2
  }
  if [ -d "$local_artifact" ]; then
    local_artifact=$(CDPATH= cd -- "$local_artifact" 2>/dev/null && pwd -P) || {
      [ -z "$route_file" ] || route_failure "artifact-target-normalization-failed"
      echo "⛔ Cannot normalize artifact write target: $fp" >&2
      exit 2
    }
  else
    local_parent=$(dirname "$local_artifact")
    local_parent=$(CDPATH= cd -- "$local_parent" 2>/dev/null && pwd -P) || {
      [ -z "$route_file" ] || route_failure "artifact-target-normalization-failed"
      echo "⛔ Cannot normalize artifact write target: $fp" >&2
      exit 2
    }
    local_artifact="$local_parent/$(basename "$local_artifact")"
  fi
  if [ "$local_artifact" != "$canonical" ]; then
    [ -z "$route_file" ] || route_failure "canonical-artifact-root-mismatch"
    printf '⛔ Task worktrees are source-only; agent artifacts must use the canonical root.\n   requested=%s\n   canonical=%s\n' "$fp" "$canonical" >&2
    exit 2
  fi
fi

# ---- Project root containing the canonical artifact root ----
root=$local_project
[ -z "$root" ] && exit 0
cr=$canonical

# ---- W7C write-cutover oracle ----
# `artifact_producer.py check-write` is the single allow/deny oracle for new
# artifact writes: legacy top-level buckets stay writable only while the
# cutover is inactive (compatibility window); once active, every write must
# land under an open cycle's `campaigns/<camp>/cycles/<cyc>/artifacts/`;
# `shared/` revisions are immutable in both states. Fail closed on any error.
if ! cutover_verdict=$(python3 "$SCRIPT_DIR/../utilities/artifact_producer.py"   check-write --artifact-root "$cr" --file "$fp" 2>&1); then
  [ -z "$route_file" ] || route_failure "artifact-write-cutover-denied"
  printf '⛔ Artifact write denied by the W7C write-cutover oracle.\n   target=%s\n   verdict=%s\n' "$fp" "$cutover_verdict" >&2
  exit 2
fi

# Durable capability artifacts are execution, not routing prose. Every public
# or internal write under a capability-owned bucket requires one verified
# current route, including direct intensity. This runs before the `_internal`
# snapshot exemption so scratch output cannot become retroactive authority
# after a standard+ headless route failed.
if ! python3 "$SCRIPT_DIR/material-route-guard.py" \
  check \
  --tool ArtifactWrite --file "$fp" --cwd "$root" \
  --session "${sid:-artifact-guard}"; then
  route_failure "capability-artifact-route-required"
  exit 2
fi

case "$fp" in */_internal/*) exit 0 ;; esac   # Machine-managed canonical snapshot.

# A route-backed spec write must declare spec_touch and assign a spec scope to
# the active node.
case "$fp" in
  "$cr"/spec/*)
    if [ -n "$route_file" ]; then
      # An owner has no route node (SD-97): it can never declare spec_touch
      # scope, so reject explicitly instead of falling into the node lookup
      # below and relying on its blanket `except Exception` to deny it.
      if [ -z "$route_node" ] || [ "$route_node" = "-" ]; then
        route_failure "spec-touch-not-declared-or-outside-node-scope"
        exit 2
      fi
      if ! python3 - "$route_file" "$route_id" "$route_node" <<'PY'
import json,sys
from pathlib import Path
try:
    route=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    node=next(row for row in route["nodes"] if row["id"]==sys.argv[3])
    roots=[scope[:-3] if scope.endswith("/**") else scope for scope in node["write_scope"]]
    ok=route.get("route_id")==sys.argv[2] and route.get("spec_touch") is True and any(root=="spec" or root.startswith("spec/") for root in roots)
except Exception:
    ok=False
raise SystemExit(0 if ok else 1)
PY
      then
        route_failure "spec-touch-not-declared-or-outside-node-scope"
        exit 2
      fi
    fi ;;
esac

# A route-backed artifact write must land inside the active node's declared
# write scope. Declaring a scope and then writing outside it was unchecked at
# every layer: only `spec/` was bound, so a worker could emit any other
# artifact its node never claimed. Node scopes are cycle-relative vocabulary
# (`plan/**`, `dev_logs/**`, `plans/<cycle>/**`) and the route record carries no
# resolved cycle, so a scope matches at any depth beneath the artifact root and
# `<cycle>`/`<topic>` are single-segment wildcards. Only worker-authored regions
# are bound: artifact-root files and dot-prefixed machine state belong to the
# runtime, and `_internal/` already exited above.
case "$fp" in
  "$cr"/*/*)
    if [ -n "$route_file" ]; then
      # An owner has no route node (SD-97, empty AGENT_ROUTE_NODE by
      # contract): node write_scope is a per-node concept, so there is no
      # scope to match against. Skip the node-scope lookup instead of
      # letting it StopIteration through the blanket `except Exception`
      # below into a false "outside node scope" block. The canonical-root
      # boundary and the `_internal` exemption above already ran, so this is
      # not an "owner writes anywhere" carve-out.
      if [ -z "$route_node" ] || [ "$route_node" = "-" ]; then
        :
      elif ! python3 - "$route_file" "$route_id" "$route_node" "$cr" "$fp" <<'PY'
import fnmatch,json,re,sys
from pathlib import Path
WORKTREE_ONLY={"source-scoped"}
def patterns(scope):
    if scope=="target-artifact":
        return ["^documents/*/*","^research/*/*"]
    root=scope[:-3] if scope.endswith("/**") else scope
    if scope in WORKTREE_ONLY or root=="source" or root.startswith("source/"): return []
    # Substitution vocabulary truth lives in capabilities/topologies.json;
    # closed-vocabulary verification is owned by tools/check-scope-placeholders.py.
    return [re.sub(r"<[a-z_]+>", "*", scope)]
def component_match(value, pattern):
    values=value.split("/") if value else []
    parts=pattern.split("/") if pattern else []
    recursive=bool(parts and parts[-1]=="**")
    if recursive:
        parts=parts[:-1]
        if len(values)<len(parts): return False
    elif len(values)!=len(parts):
        return False
    return all(fnmatch.fnmatchcase(value_part, pattern_part)
               for value_part,pattern_part in zip(values,parts))
def bound(rel,pat):
    if pat.startswith("^"):
        return component_match(rel,pat[1:])
    segments=rel.split("/")
    return any(component_match("/".join(segments[i:]),pat) for i in range(len(segments)))
try:
    route=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if route.get("route_id")!=sys.argv[2]: raise ValueError("route id mismatch")
    node=next(row for row in route["nodes"] if row["id"]==sys.argv[3])
    rel=Path(sys.argv[5]).relative_to(Path(sys.argv[4])).as_posix()
    pats=[pat for scope in node["write_scope"] for pat in patterns(scope)]
    ok=any(part.startswith(".") for part in rel.split("/")) or any(bound(rel,pat) for pat in pats)
except Exception:
    ok=False
raise SystemExit(0 if ok else 1)
PY
      then
        route_failure "artifact-write-outside-node-scope"
        exit 2
      fi
      if ! python3 "$SCRIPT_DIR/../utilities/artifact-snapshot.py" prepare \
        --artifact-root "$cr" --target "$fp" --route "$route_file" \
        --route-id "$route_id" --node "$route_node" >/dev/null
      then
        route_failure "artifact-snapshot-failed"
        exit 2
      fi
    fi ;;
esac

# Source-code and artifact edits are not blocked here. UserPromptSubmit
# routing and convention steer them through autopilot-code.
exit 0
