#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
jobs="$tmp/jobs.log"; : > "$jobs"
before=$(sha256sum "$jobs")
route() { AGENT_HOME="$tmp" "$root/utilities/dispatch-route.sh" --jobs "$jobs" "$@"; }
assert() { printf '%s\n' "$1" | grep -qx "$2"; }
assert_not() { printf '%s\n' "$1" | grep -qx "$2" && { echo "unexpected match: $2" >&2; exit 1; }; return 0; }

# Fixture config is exported before ANY route() call so no block consumes the
# shipped profiles/dispatch-defaults.yaml (suite isolation, SD-66).
cfg="$tmp/dispatch-defaults.yaml"
cat > "$cfg" <<'EOF'
schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    execute: codex
    test: diverse
    report: claude
  autopilot-apply:
    apply: opencode
EOF
export DISPATCH_DEFAULTS_CONFIG="$cfg"

# ---- role-only, argument/conflict, explicit OpenCode, usage/eligibility,
#      exact model mapping, and trace behavior (unchanged by SD-66; these calls
#      pass no --capability, so the fixture config supplies no affinity here) ----
out=$(route --stage plan); assert "$out" 'adapter=codex'; assert "$out" 'role=deep maker'
out=$(route --stage plan --adapter claude); assert "$out" 'adapter=claude'
out=$(route --stage report); assert "$out" 'adapter=claude'
out=$(HARNESS_CAPACITY_BIAS=codex "$root/utilities/dispatch-route.sh" --jobs "$jobs" --stage report); assert "$out" 'adapter=codex'
out=$(route --stage code-review --maker-family claude); assert "$out" 'adapter=codex'
out=$(route --stage code-review --maker-family gpt); assert "$out" 'adapter=claude'
printf '%s\tdone\tx\tx\tx\tharness=claude,note=dead-session-limit\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$jobs"
out=$(route --stage code-review --adapter claude); assert "$out" 'adapter=codex'; printf '%s\n' "$out" | grep -q '^rejected.1=claude:usage-limited'; assert "$out" 'fallback.1=codex:known-limit-on-claude'
out=$(route --stage test --maker-family unknown); assert "$out" 'trace.2=affinity=diverse;maker_family=unknown;required=none;bias=claude'
out=$(route --stage plan --adapter opencode); assert "$out" 'status=unknown'; assert "$out" 'family=unknown'
out2=$(route --stage test --maker-family gpt); [ "$out" != "$out2" ]
after=$(sha256sum "$jobs"); [ "$before" != "$after" ] # fixture write only
before=$(sha256sum "$jobs"); route --stage plan >/dev/null; after=$(sha256sum "$jobs"); [ "$before" = "$after" ]
rm "$jobs"
out=$(route --stage report); assert "$out" 'trace.1=explicit=none;family=none;eligibility=usage-unknown'
! route --stage plan --adapter claude --family gpt >/dev/null 2>&1
: > "$jobs"

# ---- SD-66: profiles/dispatch-defaults.yaml fixture-config coverage ----
# (fixture created and exported at the top of this suite)

# Configured value is honored for an addressable, populated cell.
out=$(route --capability autopilot-code --stage execute); assert "$out" 'adapter=codex'

# Omitted cell (plan is not in the fixture) stays neutral/discretionary and
# falls through to the existing stage-name heuristic.
out=$(route --capability autopilot-code --stage plan); assert "$out" 'adapter=codex'; assert "$out" 'role=deep maker'

# Configured "diverse" resolves against maker family, same as the built-in
# diverse heuristic.
out=$(route --capability autopilot-code --stage test --maker-family claude); assert "$out" 'adapter=codex'
out=$(route --capability autopilot-code --stage test --maker-family gpt); assert "$out" 'adapter=claude'

# Configured value wins over capacity bias (cascade: adapter > family >
# config > bias).
out=$(HARNESS_CAPACITY_BIAS=codex DISPATCH_DEFAULTS_CONFIG="$cfg" AGENT_HOME="$tmp" "$root/utilities/dispatch-route.sh" --jobs "$jobs" --capability autopilot-code --stage report)
assert "$out" 'adapter=claude'

# Explicit --adapter / --family still outrank a configured affinity.
out=$(route --capability autopilot-code --stage execute --adapter claude); assert "$out" 'adapter=claude'
out=$(route --capability autopilot-code --stage execute --family claude); assert "$out" 'adapter=claude'

# Hard eligibility (usage limit) still rejects/falls back even when config
# picked the now-limited harness.
printf '%s\tdone\tx\tx\tx\tharness=codex,note=dead-session-limit\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$jobs"
out=$(route --capability autopilot-code --stage execute); assert "$out" 'adapter=claude'; assert "$out" 'fallback.1=claude:known-limit-on-codex'
rm "$jobs"; : > "$jobs"

# An explicit "opencode" config cell routes like an explicit --adapter
# opencode would, but only when the caller left --adapter/--family unset.
out=$(route --capability autopilot-apply --stage apply); assert "$out" 'status=unknown'; assert "$out" 'adapter=opencode'
out=$(route --capability autopilot-apply --stage apply --adapter claude); assert "$out" 'adapter=claude'

# Schema-v1 compatibility keeps OpenCode out of automatic neutral/diverse
# selection: an unconfigured cell never resolves to opencode.
out=$(route --capability autopilot-apply --stage verify --maker-family claude); assert_not "$out" 'adapter=opencode'
out=$(route --capability autopilot-apply --stage verify --maker-family gpt); assert_not "$out" 'adapter=opencode'

# depth1_owner policy query: concrete-harness allowed set, [claude, codex].
owners=$(python3 "$root/utilities/dispatch-defaults.py" owners --config "$cfg")
[ "$owners" = "claude,codex" ]

# opencode relief-only policy query.
policy=$(python3 "$root/utilities/dispatch-defaults.py" opencode-policy --config "$cfg")
[ "$policy" = "relief-only" ]

# ---- SD-66 schema v2: three normal harnesses + recent-attempt balance ----
cfg_v2="$tmp/dispatch-defaults-v2.yaml"
cat > "$cfg_v2" <<'EOF'
schema_version: 2
depth1_owner: [claude, codex, opencode]
opencode:
  relief_only: false
allocation:
  strategy: least-recent-attempts
  window: 30
capabilities:
  autopilot-code:
    execute: diverse
EOF
export DISPATCH_DEFAULTS_CONFIG="$cfg_v2"
: > "$jobs"

# With equal usage the declared order is the deterministic tie-break.
out=$(route --capability autopilot-code --stage execute)
assert "$out" 'adapter=claude'; assert "$out" 'allocation_rank=claude,codex,opencode'

# Only exact registered attempt rows count; once Claude and Codex each have
# one recent attempt, the same route conditions select OpenCode.
printf '%s\tdone\tx\tx\tx\tharness=claude,attempt_id=att-c,attempt_schema_version=2,registered_worker=1\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$jobs"
printf '%s\tdone\tx\tx\tx\tharness=codex,attempt_id=att-g,attempt_schema_version=2,registered_worker=1\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$jobs"
printf '%s\tdone\tx\tx\tx\tharness=opencode,attempt_id=ignored,attempt_schema_version=1,registered_worker=1\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$jobs"
out=$(route --capability autopilot-code --stage execute)
assert "$out" 'status=unknown'; assert "$out" 'adapter=opencode'
assert "$out" 'attempt_count.claude=1'; assert "$out" 'attempt_count.codex=1'; assert "$out" 'attempt_count.opencode=0'
assert "$out" 'allocation_rank=opencode,claude,codex'

# A current hard usage marker removes the least-used harness and balance
# continues over the remaining normal pool.
printf '%s\tdone\tx\tx\tx\tharness=opencode,note=dead-session-limit\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$jobs"
out=$(route --capability autopilot-code --stage execute)
assert "$out" 'adapter=claude'; assert_not "$out" 'adapter=opencode'

owners=$(python3 "$root/utilities/dispatch-defaults.py" owners --config "$cfg_v2")
[ "$owners" = "claude,codex,opencode" ]
policy=$(python3 "$root/utilities/dispatch-defaults.py" opencode-policy --config "$cfg_v2")
[ "$policy" = "normal" ]
: > "$jobs"
export DISPATCH_DEFAULTS_CONFIG="$cfg"

# ---- malformed config fails loud with useful stderr, for every class ----
bad_model_like="$tmp/bad-model-like.yaml"
cat > "$bad_model_like" <<'EOF'
schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    execute: gpt-5.4-mini
EOF

bad_unknown_capability="$tmp/bad-unknown-capability.yaml"
cat > "$bad_unknown_capability" <<'EOF'
schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-nonexistent:
    foo: claude
EOF

bad_unknown_stage="$tmp/bad-unknown-stage.yaml"
cat > "$bad_unknown_stage" <<'EOF'
schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    exec: codex
EOF

bad_owner_set="$tmp/bad-owner-set.yaml"
cat > "$bad_owner_set" <<'EOF'
schema_version: 1
depth1_owner: [claude, claude, diverse]
opencode:
  relief_only: true
capabilities:
EOF

bad_relief_policy="$tmp/bad-relief-policy.yaml"
cat > "$bad_relief_policy" <<'EOF'
schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: false
capabilities:
EOF

for bad in "$bad_model_like" "$bad_unknown_capability" "$bad_unknown_stage" "$bad_owner_set" "$bad_relief_policy"; do
  err=$(DISPATCH_DEFAULTS_CONFIG="$bad" AGENT_HOME="$tmp" "$root/utilities/dispatch-route.sh" --jobs "$jobs" --stage plan 2>&1 1>/dev/null) && { echo "expected failure for $bad" >&2; exit 1; }
  status=$?
  [ "$status" -ne 0 ] || { echo "expected nonzero exit for $bad" >&2; exit 1; }
  [ -n "$err" ] || { echo "expected stderr diagnostic for $bad" >&2; exit 1; }
  err=$(python3 "$root/utilities/dispatch-defaults.py" validate --config "$bad" 2>&1 1>/dev/null) && { echo "expected loader failure for $bad" >&2; exit 1; }
  [ -n "$err" ] || { echo "expected loader stderr diagnostic for $bad" >&2; exit 1; }
done

unset DISPATCH_DEFAULTS_CONFIG

# ---- jobs-log non-mutation with an active config fixture ----
export DISPATCH_DEFAULTS_CONFIG="$cfg"
before=$(sha256sum "$jobs"); route --capability autopilot-code --stage execute >/dev/null; after=$(sha256sum "$jobs"); [ "$before" = "$after" ]
unset DISPATCH_DEFAULTS_CONFIG

# ---- selector-paths: adapter-projection surface resolves helpers ----
# Invoking through each adapters/<h>/utilities/ symlink must resolve the
# internal helpers (dispatch-defaults.py, usage-check.sh, model-map.sh) the
# same as the real utilities/ path. 'adapter=' is only emitted after model-map
# resolves, so asserting it proves the repo-root climb (L103-104) is fixed.
# Fixture isolation preserved via an explicit DISPATCH_DEFAULTS_CONFIG.
: > "$jobs"
for h in claude codex opencode; do
  proj="$root/adapters/$h/utilities/dispatch-route.sh"
  out=$(DISPATCH_DEFAULTS_CONFIG="$cfg" AGENT_HOME="$tmp" "$proj" --jobs "$jobs" \
        --stage test --capability autopilot-code --maker-family gpt)
  assert "$out" 'status=eligible'
  printf '%s\n' "$out" | grep -q '^adapter=' \
    || { echo "projection $h did not reach adapter= output" >&2; exit 1; }
done
# plan stage once through a projection exercises the codex model-map branch.
out=$(DISPATCH_DEFAULTS_CONFIG="$cfg" AGENT_HOME="$tmp" \
      "$root/adapters/claude/utilities/dispatch-route.sh" --jobs "$jobs" --stage plan)
assert "$out" 'adapter=codex'; assert "$out" 'role=deep maker'

echo 'dispatch-route: PASS'
