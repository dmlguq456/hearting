#!/usr/bin/env bash
# Regression for the native-subagent default model injection hook.
set -u

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SRC="$SRC_DIR/subagent-model-default.sh"
[ -f "$HOOK_SRC" ] || { echo "FAIL: hook missing: $HOOK_SRC"; exit 1; }

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  BAD %s\n' "$1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Fixture layout: the hook resolves <its-realpath-dir>/../config/models.conf, so
# a copied hook next to a fixture conf verifies tier resolution deterministically.
mkdir -p "$TMP/hooks" "$TMP/config" "$TMP/home" "$TMP/noconf/hooks" \
  "$TMP/nopolicy/hooks" "$TMP/nopolicy/config" \
  "$TMP/proj/.claude/agents" "$TMP/user-runtime/agent-config"
cp "$HOOK_SRC" "$TMP/hooks/subagent-model-default.sh"
cp "$HOOK_SRC" "$TMP/noconf/hooks/subagent-model-default.sh"
cp "$HOOK_SRC" "$TMP/nopolicy/hooks/subagent-model-default.sh"
HOOK="$TMP/hooks/subagent-model-default.sh"
cat > "$TMP/config/models.conf" <<'EOF'
# fixture conf
CFG_TIER_LIGHT_MODEL=sonnet
CFG_TIER_LIGHT_EFFORT=high
CFG_NATIVE_SUBAGENT=light   # tier reference, not a concrete ID
CFG_MAIN_SESSION_ONLY_MODELS="fable"
EOF
cat > "$TMP/nopolicy/config/models.conf" <<'EOF'
CFG_TIER_LIGHT_MODEL=sonnet
CFG_NATIVE_SUBAGENT=light
EOF
sed 's/^CFG_TIER_LIGHT_MODEL=.*/CFG_TIER_LIGHT_MODEL=user-light/' \
  "$SRC_DIR/../adapters/claude/config/models.conf" \
  > "$TMP/user-runtime/agent-config/models.conf"
cat > "$TMP/proj/.claude/agents/zz-pinned-team.md" <<'EOF'
---
name: zz-pinned-team
model: haiku
---
Pinned fixture agent.
EOF
cat > "$TMP/proj/.claude/agents/zz-unpinned-team.md" <<'EOF'
---
name: zz-unpinned-team
color: gray
---
Unpinned fixture agent.
EOF
cat > "$TMP/proj/.claude/agents/zz-main-only-team.md" <<'EOF'
---
name: zz-main-only-team
model: claude-fable-5
---
Ineligible pinned fixture agent.
EOF

run_hook() { # run_hook <out> <err> [ENV=VAL ...]
  local out="$1" err="$2"; shift 2
  env -i HOME="$TMP/home" PATH="$PATH" "$@" bash "$HOOK" >"$out" 2>"$err"
}

assert_injected() { # assert_injected <out-file> <expected-model>
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
o = d["hookSpecificOutput"]
assert o["hookEventName"] == "PreToolUse", o
u = o["updatedInput"]
assert u["model"] == sys.argv[2], u
assert u["description"] == "x" and u["prompt"] == "y", u
' "$1" "$2"
}

assert_denied() { # assert_denied <out-file> <expected-reason>
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))["hookSpecificOutput"]
assert d["hookEventName"] == "PreToolUse", d
assert d["permissionDecision"] == "deny", d
assert d["permissionDecisionReason"] == sys.argv[2], d
' "$1" "$2"
}

echo "== subagent-model-default hook =="

# (a) built-in agent type, no model -> inject the conf tier model
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","subagent_type":"general-purpose"}}' \
  | run_hook "$TMP/a.out" "$TMP/a.err"
[ $? = 0 ] && [ ! -s "$TMP/a.err" ] && assert_injected "$TMP/a.out" sonnet \
  && ok "built-in spawn gets conf-tier model injected" \
  || bad "built-in spawn did not get conf-tier injection"

# (a2) Task tool_name takes the same path
printf '%s' '{"tool_name":"Task","tool_input":{"description":"x","prompt":"y"}}' \
  | run_hook "$TMP/a2.out" "$TMP/a2.err"
[ $? = 0 ] && [ ! -s "$TMP/a2.err" ] && assert_injected "$TMP/a2.out" sonnet \
  && ok "Task spawn gets the same injection" \
  || bad "Task spawn did not get injection"

# (a3) the canonical hook selects a complete user-owned config before shipped
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","subagent_type":"general-purpose"}}' \
  | env -i HOME="$TMP/home" PATH="$PATH" CLAUDE_CONFIG_DIR="$TMP/user-runtime" \
      bash "$HOOK_SRC" >"$TMP/a3.out" 2>"$TMP/a3.err"
[ $? = 0 ] && [ ! -s "$TMP/a3.err" ] && assert_injected "$TMP/a3.out" user-light \
  && ok "canonical hook prefers the complete user model config" \
  || bad "canonical hook did not prefer the user model config"

# (b) explicit per-invocation model -> untouched
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","model":"haiku"}}' \
  | run_hook "$TMP/b.out" "$TMP/b.err"
[ $? = 0 ] && [ ! -s "$TMP/b.out" ] && [ ! -s "$TMP/b.err" ] \
  && ok "explicit model param is preserved (no output)" \
  || bad "explicit model param was overridden"

# (c) fork inheritance is ineligible because the main may be Fable
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","subagent_type":"fork"}}' \
  | run_hook "$TMP/c.out" "$TMP/c.err"
[ $? = 0 ] && [ ! -s "$TMP/c.err" ] \
  && assert_denied "$TMP/c.out" native-subagent-model-inheritance-ineligible \
  && ok "fork inheritance is denied" \
  || bad "fork inheritance was not denied"

# (d) agent definition with a frontmatter model pin -> untouched
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","subagent_type":"zz-pinned-team"}}' \
  | run_hook "$TMP/d.out" "$TMP/d.err" CLAUDE_PROJECT_DIR="$TMP/proj"
[ $? = 0 ] && [ ! -s "$TMP/d.out" ] && [ ! -s "$TMP/d.err" ] \
  && ok "frontmatter model pin is respected (no output)" \
  || bad "frontmatter model pin was overridden"

# (d2) agent definition without a pin -> injected
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","subagent_type":"zz-unpinned-team"}}' \
  | run_hook "$TMP/d2.out" "$TMP/d2.err" CLAUDE_PROJECT_DIR="$TMP/proj"
[ $? = 0 ] && [ ! -s "$TMP/d2.err" ] && assert_injected "$TMP/d2.out" sonnet \
  && ok "unpinned custom agent gets injection" \
  || bad "unpinned custom agent was not injected"

# (e) malformed JSON -> silent success (fail-open)
printf 'not json' | run_hook "$TMP/e.out" "$TMP/e.err"
[ $? = 0 ] && [ ! -s "$TMP/e.out" ] && [ ! -s "$TMP/e.err" ] \
  && ok "malformed input fails open" \
  || bad "malformed input did not fail open"

# (f) CLAUDE_NATIVE_SUBAGENT_MODEL=inherit -> typed denial
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y"}}' \
  | run_hook "$TMP/f.out" "$TMP/f.err" CLAUDE_NATIVE_SUBAGENT_MODEL=INHERIT
[ $? = 0 ] && [ ! -s "$TMP/f.err" ] \
  && assert_denied "$TMP/f.out" native-subagent-model-inheritance-ineligible \
  && ok "override=inherit is denied" \
  || bad "override=inherit was not denied"

# (g) CLAUDE_NATIVE_SUBAGENT_MODEL=<alias> -> that alias wins over conf
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y"}}' \
  | run_hook "$TMP/g.out" "$TMP/g.err" CLAUDE_NATIVE_SUBAGENT_MODEL=haiku
[ $? = 0 ] && [ ! -s "$TMP/g.err" ] && assert_injected "$TMP/g.out" haiku \
  && ok "override alias is injected instead of conf tier" \
  || bad "override alias was not injected"

# (h) non-matching tool -> untouched
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"true"}}' \
  | run_hook "$TMP/h.out" "$TMP/h.err"
[ $? = 0 ] && [ ! -s "$TMP/h.out" ] && [ ! -s "$TMP/h.err" ] \
  && ok "non-Agent tool is ignored" \
  || bad "non-Agent tool produced output"

# (i) missing models.conf -> valid Agent request fails closed
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y"}}' \
  | env -i HOME="$TMP/home" PATH="$PATH" bash "$TMP/noconf/hooks/subagent-model-default.sh" \
    >"$TMP/i.out" 2>"$TMP/i.err"
[ $? = 0 ] && [ ! -s "$TMP/i.err" ] \
  && assert_denied "$TMP/i.out" native-subagent-model-policy-unavailable \
  && ok "missing conf fails closed" \
  || bad "missing conf did not fail closed"

# (j) explicit main-session-only model -> typed denial
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","model":"claude-fable-5"}}' \
  | run_hook "$TMP/j.out" "$TMP/j.err"
[ $? = 0 ] && [ ! -s "$TMP/j.err" ] \
  && assert_denied "$TMP/j.out" native-subagent-main-session-only-model \
  && ok "explicit main-session-only model is denied" \
  || bad "explicit main-session-only model was not denied"

# (k) frontmatter cannot smuggle a main-session-only model into a worker
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y","subagent_type":"zz-main-only-team"}}' \
  | run_hook "$TMP/k.out" "$TMP/k.err" CLAUDE_PROJECT_DIR="$TMP/proj"
[ $? = 0 ] && [ ! -s "$TMP/k.err" ] \
  && assert_denied "$TMP/k.out" native-subagent-main-session-only-model \
  && ok "frontmatter main-session-only model is denied" \
  || bad "frontmatter main-session-only model was not denied"

# (l) runtime override cannot smuggle a main-session-only model either
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y"}}' \
  | run_hook "$TMP/l.out" "$TMP/l.err" CLAUDE_NATIVE_SUBAGENT_MODEL=claude-fable-5
[ $? = 0 ] && [ ! -s "$TMP/l.err" ] \
  && assert_denied "$TMP/l.out" native-subagent-main-session-only-model \
  && ok "override main-session-only model is denied" \
  || bad "override main-session-only model was not denied"

# (m) an existing config without the eligibility declaration also fails closed
printf '%s' '{"tool_name":"Agent","tool_input":{"description":"x","prompt":"y"}}' \
  | env -i HOME="$TMP/home" PATH="$PATH" bash "$TMP/nopolicy/hooks/subagent-model-default.sh" \
    >"$TMP/m.out" 2>"$TMP/m.err"
[ $? = 0 ] && [ ! -s "$TMP/m.err" ] \
  && assert_denied "$TMP/m.out" native-subagent-model-policy-unavailable \
  && ok "missing eligibility declaration fails closed" \
  || bad "missing eligibility declaration did not fail closed"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
