#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
GUARD="$ROOT/hooks/runtime-root-guard.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

CHECKOUT="$TMP/checkout"
OTHER_HOME="$TMP/other-home"
mkdir -p "$CHECKOUT/utilities" "$OTHER_HOME"

# --- deny: installed runtime active, checkout-relative six-utility call ---
if AGENT_HOME="$OTHER_HOME" "$GUARD" --tool Bash \
    --command "python3 \"$CHECKOUT/utilities/capability-route.py\" compile" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  bad "denies checkout-relative capability-route.py under a different AGENT_HOME"
else
  [ "$?" -eq 2 ] || true
  grep -q "runtime-root-guard" /tmp/rrg.err && ok "denies checkout-relative capability-route.py under a different AGENT_HOME" \
    || bad "denies checkout-relative capability-route.py under a different AGENT_HOME (no reason)"
fi

# --- deny for each of the six utilities ---
for name in artifact_producer spec-transaction dispatch-owner dispatch-batch dispatch-node; do
  mkdir -p "$CHECKOUT/utilities"
  if AGENT_HOME="$OTHER_HOME" "$GUARD" --tool Bash \
      --command "python3 \"$CHECKOUT/utilities/${name}.py\" foo" \
      >/tmp/rrg.out 2>/tmp/rrg.err; then
    bad "denies checkout-relative ${name}.py"
  else
    ok "denies checkout-relative ${name}.py"
  fi
done

# --- allow: dev activation (AGENT_HOME == checkout) ---
if AGENT_HOME="$CHECKOUT" "$GUARD" --tool Bash \
    --command "python3 \"$CHECKOUT/utilities/capability-route.py\" compile" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  ok "allows dev activation where AGENT_HOME is the checkout itself"
else
  bad "allows dev activation where AGENT_HOME is the checkout itself"
fi

# --- allow: no AGENT_HOME set (fail open) ---
if env -u AGENT_HOME "$GUARD" --tool Bash \
    --command "python3 \"$CHECKOUT/utilities/capability-route.py\" compile" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  ok "allows when AGENT_HOME is unset (fail open)"
else
  bad "allows when AGENT_HOME is unset (fail open)"
fi

# --- allow: non-Bash tool ---
if AGENT_HOME="$OTHER_HOME" "$GUARD" --tool Read \
    --command "python3 \"$CHECKOUT/utilities/capability-route.py\" compile" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  ok "allows non-Bash tool"
else
  bad "allows non-Bash tool"
fi

# --- allow: unrelated utility not in the six-name allowlist ---
if AGENT_HOME="$OTHER_HOME" "$GUARD" --tool Bash \
    --command "python3 \"$CHECKOUT/utilities/peer-message.py\" record" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  ok "allows a utility outside the six-name allowlist"
else
  bad "allows a utility outside the six-name allowlist"
fi

# --- allow: unrelated command entirely ---
if AGENT_HOME="$OTHER_HOME" "$GUARD" --tool Bash \
    --command "ls -la" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  ok "allows an unrelated command"
else
  bad "allows an unrelated command"
fi

# --- allow: already-installed AGENT_HOME path invocation (not checkout-relative) ---
if AGENT_HOME="$OTHER_HOME" "$GUARD" --tool Bash \
    --command "python3 \"$OTHER_HOME/utilities/capability-route.py\" compile" \
    >/tmp/rrg.out 2>/tmp/rrg.err; then
  ok "allows an invocation against the active AGENT_HOME's own utilities"
else
  bad "allows an invocation against the active AGENT_HOME's own utilities"
fi

# --- hook-mode (stdin JSON) smoke: deny surfaces as JSON deny, exit 0 ---
json_input=$(printf '{"tool_name":"Bash","tool_input":{"command":"python3 %s/utilities/capability-route.py compile"}}' "$CHECKOUT")
out=$(printf '%s' "$json_input" | AGENT_HOME="$OTHER_HOME" "$GUARD")
rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '"permissionDecision":"deny"'; then
  ok "hook-mode stdin JSON denies with structured JSON and exit 0"
else
  bad "hook-mode stdin JSON denies with structured JSON and exit 0 (rc=$rc out=$out)"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
