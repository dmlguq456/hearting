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

# Assert $1 is valid JSON with a deny decision whose reason names both
# runtime-root-guard and AGENT_HOME. Prints OK or a failure tag on stdout.
assert_deny_json() {
  python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception as e:
    print("PARSE_FAIL:" + str(e)); sys.exit(1)
h = d.get("hookSpecificOutput", {})
if h.get("permissionDecision") != "deny":
    print("NOT_DENY"); sys.exit(1)
reason = h.get("permissionDecisionReason", "")
if "runtime-root-guard" not in reason or "AGENT_HOME" not in reason:
    print("MISSING_HINTS"); sys.exit(1)
print("OK")
' "$1"
}

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

# --- deny: checkout-relative call, payload cwd == checkout, different AGENT_HOME ---
json_input=$(printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"python3 utilities/capability-route.py compile"}}' "$CHECKOUT")
out=$(printf '%s' "$json_input" | AGENT_HOME="$OTHER_HOME" "$GUARD")
rc=$?
verdict=$(assert_deny_json "$out")
if [ "$rc" -eq 0 ] && [ "$verdict" = "OK" ]; then
  ok "denies relative utilities/capability-route.py with payload cwd=checkout under a different AGENT_HOME"
else
  bad "denies relative utilities/capability-route.py with payload cwd=checkout under a different AGENT_HOME (rc=$rc verdict=$verdict out=$out)"
fi

# --- deny: "./utilities/<name>.py" relative form ---
json_input=$(printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"./utilities/dispatch-batch.py foo"}}' "$CHECKOUT")
out=$(printf '%s' "$json_input" | AGENT_HOME="$OTHER_HOME" "$GUARD")
rc=$?
verdict=$(assert_deny_json "$out")
if [ "$rc" -eq 0 ] && [ "$verdict" = "OK" ]; then
  ok "denies ./utilities/dispatch-batch.py with payload cwd=checkout under a different AGENT_HOME"
else
  bad "denies ./utilities/dispatch-batch.py with payload cwd=checkout under a different AGENT_HOME (rc=$rc verdict=$verdict out=$out)"
fi

# --- deny: active AGENT_HOME path itself contains a double quote (JSON-escaping
# regression -- AGENT_HOME is read straight from the env, not JSON-decoded, so
# the quote lands in `reason` unescaped unless the guard escapes it) ---
QUOTED_HOME="$TMP/quo\"te-home"
mkdir -p "$QUOTED_HOME"
json_input=$(printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"python3 utilities/capability-route.py compile"}}' "$CHECKOUT")
out=$(printf '%s' "$json_input" | AGENT_HOME="$QUOTED_HOME" "$GUARD")
rc=$?
verdict=$(assert_deny_json "$out")
if [ "$rc" -eq 0 ] && [ "$verdict" = "OK" ]; then
  ok "denies with valid JSON when the active AGENT_HOME path itself contains a double quote"
else
  bad "denies with valid JSON when the active AGENT_HOME path itself contains a double quote (rc=$rc verdict=$verdict out=$out)"
fi

# --- allow: relative call, payload cwd == AGENT_HOME (dev activation) ---
json_input=$(printf '{"cwd":"%s","tool_name":"Bash","tool_input":{"command":"python3 utilities/capability-route.py compile"}}' "$CHECKOUT")
out=$(printf '%s' "$json_input" | AGENT_HOME="$CHECKOUT" "$GUARD")
rc=$?
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
  ok "allows relative utilities/capability-route.py when payload cwd equals AGENT_HOME (dev)"
else
  bad "allows relative utilities/capability-route.py when payload cwd equals AGENT_HOME (dev) (rc=$rc out=$out)"
fi

# --- allow: relative call, payload has no cwd field at all (fail-open) ---
json_input='{"tool_name":"Bash","tool_input":{"command":"python3 utilities/capability-route.py compile"}}'
out=$(printf '%s' "$json_input" | AGENT_HOME="$OTHER_HOME" "$GUARD")
rc=$?
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
  ok "allows relative utilities/capability-route.py when payload has no cwd (fail-open)"
else
  bad "allows relative utilities/capability-route.py when payload has no cwd (fail-open) (rc=$rc out=$out)"
fi

# --- hook-mode (stdin JSON) smoke: deny surfaces as JSON deny, exit 0 ---
json_input=$(printf '{"tool_name":"Bash","tool_input":{"command":"python3 %s/utilities/capability-route.py compile"}}' "$CHECKOUT")
out=$(printf '%s' "$json_input" | AGENT_HOME="$OTHER_HOME" "$GUARD")
rc=$?
verdict=$(assert_deny_json "$out")
if [ "$rc" -eq 0 ] && [ "$verdict" = "OK" ]; then
  ok "hook-mode stdin JSON denies with structured JSON and exit 0"
else
  bad "hook-mode stdin JSON denies with structured JSON and exit 0 (rc=$rc verdict=$verdict out=$out)"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
