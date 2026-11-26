#!/usr/bin/env sh
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../../../tools/run-tests.py" --direct-run bash "$0" "$@"
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' 0 HUP INT TERM
FA=$T/agent-home; mkdir -p "$FA/tools/install"
for e in core roles capabilities adapters utilities hooks skills scaffolds codex_setting; do [ -e "$ROOT/$e" ] && ln -s "$ROOT/$e" "$FA/$e"; done
for e in "$ROOT"/tools/*; do b=$(basename "$e"); [ "$b" = install ] || ln -s "$e" "$FA/tools/$b"; done
for e in "$ROOT"/tools/install/*; do b=$(basename "$e"); [ "$b" = harness.sh ] || ln -s "$e" "$FA/tools/install/$b"; done
cat > "$FA/tools/install/harness.sh" <<'STUB'
#!/usr/bin/env sh
printf '%s' "${STUB_DOCTOR_BODY:-}"
exit "${STUB_DOCTOR_EXIT:-0}"
STUB
chmod +x "$FA/tools/install/harness.sh"
CH=$T/codex; mkdir -p "$CH/.harness" "$CH/skills" "$CH/agents" "$T/home" "$T/xdg" "$T/claude"
ln -s "$FA" "$CH/hearting"
printf '{"runtime":"codex","source_root":"%s","active_root":"%s","activated_projection_digest":"x","mode":"linked"}\n' "$FA" "$FA" > "$CH/.harness/activation.json"
run_case() {
  name=$1 body=$2 code=$3; out=$T/$name.out
  if env -u AGENT_DISPATCH_JOBS HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" CODEX_HOME="$CH" CLAUDE_CONFIG_DIR="$T/claude" TMPDIR="$T" AGENT_HOME="$FA" CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY=1 STUB_DOCTOR_BODY="$body" STUB_DOCTOR_EXIT="$code" sh "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$out" 2>/dev/null; then rc=0; else rc=$?; fi
  printf '%s=%s\n' "$name" "$rc"
  last_rc=$rc
}
run_case T1 '' 0; grep -q 'detail=no-output' "$T/T1.out"
run_case T2 '{"runtime":"codex","ok":true,"freshness":"fresh_1","surface_skew":{"ok":true,"compared":["a"]}}' 23; [ "$last_rc" -eq 1 ]
grep -q '^check=runtime-activation:ok reason=strict-doctor freshness=fresh_1$' "$T/T2.out"; grep -q '^check=installation-surface-skew:ok compared=1$' "$T/T2.out"
run_case T3 '{"runtime":"codex","ok":true}' 0; ! grep -q 'runtime-activation:ok\|installation-surface-skew:ok' "$T/T3.out"
run_case T3b '{"runtime":"codex","ok":true,"surface_skew":{"ok":false,"skewed":[{"subtree":"core"}],"compared":[]}}' 7; grep -q 'subtrees=core' "$T/T3b.out"
run_case T3c not-json 3; grep -q 'detail=invalid-json' "$T/T3c.out"
run_case T4 '{"runtime":"codex","ok":false,"freshness":"f","next_action":"repair_now","surface_skew":{"ok":true}}' 1; grep -q 'next_action=repair_now' "$T/T4.out"
mkdir -p "$T/bin"; printf '#!/usr/bin/env sh\nexit 23\n' > "$T/bin/codex"; chmod +x "$T/bin/codex"
if env -u AGENT_DISPATCH_JOBS PATH="$T/bin:$PATH" HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" CODEX_HOME="$CH" CLAUDE_CONFIG_DIR="$T/claude" TMPDIR="$T" AGENT_HOME="$FA" STUB_DOCTOR_BODY='{"runtime":"codex","ok":true,"surface_skew":{"ok":true}}' sh "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$T/T5.out" 2>/dev/null; then rc=0; else rc=$?; fi
[ "$rc" -eq 1 ]; grep -q 'check=plugin:unknown reason=codex-plugin-list-failed exit=23 skill_discovery=native' "$T/T5.out"; ! grep -q '^plugin_install_' "$T/T5.out"

cat > "$T/bin/python3" <<'STUB'
#!/usr/bin/env sh
set -eu
report=$(/usr/bin/python3 "$@")
printf '%s\n' "$report"
if [ "$#" -eq 3 ] && [ "${1:-}" = - ]; then printf 'fails_delta=1\n'; fi
STUB
chmod +x "$T/bin/python3"
if env -u AGENT_DISPATCH_JOBS PATH="$T/bin:$PATH" HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" CODEX_HOME="$CH" CLAUDE_CONFIG_DIR="$T/claude" TMPDIR="$T" AGENT_HOME="$FA" STUB_DOCTOR_BODY='{"runtime":"codex","ok":true,"surface_skew":{"ok":true}}' sh "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$T/T6.out" 2>/dev/null; then rc=0; else rc=$?; fi
[ "$rc" -eq 1 ]; grep -q '^check=runtime-activation:failed reason=doctor-report-unavailable exit=0 detail=parser-unavailable$' "$T/T6.out"; grep -q '^check=installation-surface-skew:failed reason=surface-skew-unreported detail=parser-unavailable$' "$T/T6.out"; grep -q '^status=failed ' "$T/T6.out"
printf 'check-runtime-projection: PASS\n'
