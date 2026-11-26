#!/usr/bin/env sh
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../../../tools/run-tests.py" --direct-run bash "$0" "$@"
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' 0 HUP INT TERM
FA=$T/source; mkdir -p "$FA/tools" "$FA/adapters/codex/bin" "$FA/core" "$FA/adapters/codex/utilities" "$FA/utilities" "$FA/roles" "$FA/capabilities" "$FA/hooks" "$T/home" "$T/xdg" "$T/claude"
cp "$ROOT/adapters/codex/bin/preflight.sh" "$FA/adapters/codex/bin/preflight.sh"; cp "$ROOT/core/CORE.md" "$FA/core/CORE.md"; cp "$ROOT/core/ADAPTATION.md" "$FA/core/ADAPTATION.md"
cp "$ROOT/utilities/preflight-doctor-cause.sh" "$FA/utilities/preflight-doctor-cause.sh"
cp "$ROOT/adapters/codex/utilities/agent-home.sh" "$FA/adapters/codex/utilities/agent-home.sh"; cp "$ROOT/utilities/artifact-root.sh" "$FA/utilities/artifact-root.sh"
cp -R "$ROOT/roles/." "$FA/roles/"; cp -R "$ROOT/capabilities/." "$FA/capabilities/"; cp -R "$ROOT/hooks/." "$FA/hooks/"
chmod +x "$FA/adapters/codex/utilities/agent-home.sh"
printf 'import sys\nprint()\nprint("status=failed")\nprint()\nprint("reason=selected-signal")\nfor i in range(10): print("diff-line-" + str(i))\nsys.exit(1)\n' > "$FA/tools/generate.py"
cat > "$FA/adapters/codex/bin/check-runtime-projection.sh" <<'STUB'
#!/usr/bin/env sh
printf x >> "${COUNTER:?}"
printf 'check=hooks-json:failed reason=not-harness-hook-projection\nstatus=failed fails=1\n'
exit 1
STUB
chmod +x "$FA/adapters/codex/bin/check-runtime-projection.sh"
out=$T/out
if env -u AGENT_DISPATCH_JOBS HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" CODEX_HOME="$T/codex" CLAUDE_CONFIG_DIR="$T/claude" TMPDIR="$T" AGENT_HOME="$FA" COUNTER="$T/counter" sh "$FA/adapters/codex/bin/preflight.sh" doctor --runtime >"$out" 2>&1; then rc=0; else rc=$?; fi
[ "$rc" -ne 0 ]; grep -q '^check=generated-projections:failed$' "$out"; grep -q '^cause_generated-projections_1=status=failed$' "$out"; grep -q '^cause_generated-projections_2=reason=selected-signal$' "$out"; grep -q '^cause_generated-projections_lines=2/12$' "$out"; [ "$(grep -c '^cause_generated-projections_lines=' "$out")" -eq 1 ]
empty=$(printf '' | "$FA/utilities/preflight-doctor-cause.sh" empty)
[ "$empty" = "$(printf 'cause_empty_1=no-output-captured\ncause_empty_lines=0/0')" ]
printf 'preflight-doctor-cause: PASS\n'
