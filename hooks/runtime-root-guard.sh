#!/bin/sh
# runtime-root-guard.sh — deny Bash calls that invoke a checkout-relative
# runtime-root-sensitive utility while AGENT_HOME points at a different
# active runtime. Mirrors capability-route.py's "launch-runtime-root-mismatch"
# ValueError and the utilities-invocation convention it enforces.
#
# Deny only when ALL of:
#   (a) an installed runtime is active (AGENT_HOME is set)
#   (b) the Bash command invokes one of six checkout-relative utilities
#   (c) that checkout is not the active AGENT_HOME itself (dev activation)
# Everything else — unknown tool, empty command, missing AGENT_HOME, a
# utility outside the six-name allowlist, or any parse failure — fails open.
# POSIX sh, no jq. Dual mode: hook(stdin JSON) + CLI(--tool/--command/--cwd/--session).

tool_arg=""
cmd=""
cwd_arg=""
cwd_json=""
hook_mode=1

if [ "$#" -gt 0 ]; then
  hook_mode=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --tool)    [ "$#" -ge 2 ] || { echo "runtime-root-guard: --tool requires a value" >&2; exit 64; }; tool_arg="$2"; shift 2 ;;
      --command) [ "$#" -ge 2 ] || { echo "runtime-root-guard: --command requires a value" >&2; exit 64; }; cmd="$2"; shift 2 ;;
      --cwd)     [ "$#" -ge 2 ] || { echo "runtime-root-guard: --cwd requires a path" >&2; exit 64; }; cwd_arg="$2"; shift 2 ;;
      --session) [ "$#" -ge 2 ] || { echo "runtime-root-guard: --session requires an id" >&2; exit 64; }; shift 2 ;;
      --help|-h) echo "usage: runtime-root-guard.sh --tool Bash --command <cmd>"; exit 0 ;;
      *) echo "runtime-root-guard: unknown argument: $1" >&2; exit 64 ;;
    esac
  done
  [ "$tool_arg" = "Bash" ] || exit 0
else
  input=$(cat 2>/dev/null)
  [ -z "$input" ] && exit 0
  cmd=$(printf '%s' "$input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"command"[[:space:]]*:[[:space:]]*"//; s/"$//')
  cwd_json=$(printf '%s' "$input" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"cwd"[[:space:]]*:[[:space:]]*"//; s/"$//')
fi

[ -z "$cmd" ] && exit 0

# Only these six utilities carry a runtime-root-mismatch gate. Check
# checkout-relative forms first (bare "utilities/<name>.py" or "./utilities/<name>.py"
# at a word boundary) so a relative call is never mistaken for an absolute one --
# "./utilities/x.py" also satisfies the absolute "*/utilities/x.py*" glob.
tool=""
is_relative=0
for name in capability-route artifact_producer spec-transaction dispatch-owner dispatch-batch dispatch-node; do
  case "$cmd" in
    "utilities/${name}.py"*|*" utilities/${name}.py"*|*'"'"utilities/${name}.py"*|*"'utilities/${name}.py"*|*"./utilities/${name}.py"*)
      tool="$name"; is_relative=1; break ;;
  esac
done
if [ -z "$tool" ]; then
  for name in capability-route artifact_producer spec-transaction dispatch-owner dispatch-batch dispatch-node; do
    case "$cmd" in
      *"/utilities/${name}.py"*) tool="$name"; break ;;
    esac
  done
fi
[ -z "$tool" ] && exit 0

# No active installed runtime pinned: fail open.
agent_home="${AGENT_HOME:-}"
[ -z "$agent_home" ] && exit 0

if [ "$is_relative" -eq 1 ]; then
  # A relative call's checkout is wherever it runs, not something derivable
  # from the command text -- resolve it from the caller-supplied cwd only.
  if [ "$hook_mode" -eq 1 ]; then
    checkout="$cwd_json"
  else
    checkout="$cwd_arg"
  fi
else
  # The checkout root implied by the matched "utilities/<tool>.py" path segment.
  checkout=$(printf '%s' "$cmd" | grep -o '[^ "]*'"/utilities/${tool}.py" | head -1 | sed "s#/utilities/${tool}.py##")
fi
[ -z "$checkout" ] && exit 0

checkout_abs=$(cd "$checkout" 2>/dev/null && pwd -P) || exit 0
agent_home_abs=$(cd "$agent_home" 2>/dev/null && pwd -P) || exit 0

# Dev activation: AGENT_HOME already IS this checkout. Allow.
[ "$checkout_abs" = "$agent_home_abs" ] && exit 0

reason="runtime-root-guard: this Bash command invokes the checkout-relative utilities/${tool}.py at ${checkout_abs}, but AGENT_HOME=${agent_home_abs} names a different active runtime. Run the INSTALLED utility -- python3 \"\$AGENT_HOME/utilities/${tool}.py\" -- or export AGENT_HOME=${checkout_abs} to make this checkout the active runtime."

if [ "$hook_mode" -eq 0 ]; then
  printf '⛔ %s\n' "$reason" >&2
  exit 2
fi
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason"
exit 0
