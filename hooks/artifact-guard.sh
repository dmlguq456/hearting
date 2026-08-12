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
      --help|-h)
        echo "usage: artifact-guard.sh --file <path> [--session <id>]"
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
' 2>/dev/null)"
  fp="${FP:-}"; sid="${SID:-}"
fi

[ -z "$fp" ] && exit 0

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARTIFACT_ROOT_RESOLVER="$SCRIPT_DIR/../utilities/artifact-root.sh"

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
import fnmatch,json,sys
from pathlib import Path
WORKTREE_ONLY={"source-scoped"}
def patterns(scope):
    if scope=="target-artifact":
        return ["^documents/*/*","^research/*/*"]
    root=scope[:-3] if scope.endswith("/**") else scope
    if scope in WORKTREE_ONLY or root=="source" or root.startswith("source/"): return []
    return [scope.replace("<cycle>","*").replace("<topic>","*").replace("/**","/*")]
def bound(rel,pat):
    if pat.startswith("^"):
        return fnmatch.fnmatch(rel,pat[1:])
    segments=rel.split("/")
    return any(fnmatch.fnmatch("/".join(segments[i:]),pat) for i in range(len(segments)))
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
