#!/usr/bin/env sh
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../../../tools/run-tests.py" --direct-run bash "$0" "$@"
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' 0 HUP INT TERM
FA=$T/source
mkdir -p "$FA/tools" "$FA/adapters/opencode/bin" "$FA/adapters/opencode/utilities" \
  "$FA/core" "$FA/utilities" "$FA/roles" "$FA/capabilities" "$T/home" "$T/xdg"
cp "$ROOT/adapters/opencode/bin/preflight.sh" "$FA/adapters/opencode/bin/preflight.sh"
cp "$ROOT/adapters/opencode/utilities/agent-home.sh" "$FA/adapters/opencode/utilities/agent-home.sh"
cp "$ROOT/utilities/artifact-root.sh" "$FA/utilities/artifact-root.sh"
cp "$ROOT/utilities/preflight-doctor-cause.sh" "$FA/utilities/preflight-doctor-cause.sh"
cp "$ROOT/core/CORE.md" "$FA/core/CORE.md"
cp -R "$ROOT/roles/." "$FA/roles/"; cp -R "$ROOT/capabilities/." "$FA/capabilities/"
chmod +x "$FA/adapters/opencode/bin/preflight.sh" "$FA/adapters/opencode/utilities/agent-home.sh"
cat > "$FA/tools/generate.py" <<'PY'
import os
import sys
with open(os.environ["GENERATE_COUNTER"], "a", encoding="utf-8") as handle: handle.write("x")
sys.stdout.buffer.write(b"ordinary=first\nreason=isolated\x00-native-cause\r\nreason=" + b"x" * 240 + b"\n")
raise SystemExit(7)
PY
cat > "$FA/tools/check-adaptation-boundary.sh" <<'SH'
#!/usr/bin/env sh
printf x >> "${COUNTER:?}"
printf 'boundary-first\nboundary-1\nboundary-2\nboundary-3\nboundary-4\nboundary-last\n'
exit 3
SH
chmod +x "$FA/tools/check-adaptation-boundary.sh"
out=$T/out
if env -u AGENT_DISPATCH_JOBS HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" \
  AGENT_HOME="$FA" TMPDIR="$T" COUNTER="$T/counter" GENERATE_COUNTER="$T/generate-counter" \
  sh "$FA/adapters/opencode/bin/preflight.sh" doctor >"$out" 2>&1; then rc=0; else rc=$?; fi
[ "$rc" -ne 0 ]
grep -q '^check=generated-projections:failed$' "$out"
grep -q '^cause_generated-projections_1=reason=isolated-native-cause$' "$out"
grep -q '^cause_generated-projections_lines=2/3$' "$out"
long_cause=$(sed -n 's/^cause_generated-projections_2=//p' "$out")
[ "$(printf '%s' "$long_cause" | wc -c | tr -d ' ')" -le 200 ]
grep -q '^check=adaptation-boundary:failed$' "$out"
grep -q '^cause_adaptation-boundary_1=boundary-1$' "$out"
grep -q '^cause_adaptation-boundary_5=boundary-last$' "$out"
[ "$(wc -c < "$T/counter" | tr -d ' ')" = 1 ]
[ "$(wc -c < "$T/generate-counter" | tr -d ' ')" = 1 ]
[ "$(grep -c '^cause_adaptation-boundary_lines=' "$out")" -eq 1 ]
grep -q '^cause_adaptation-boundary_lines=5/6$' "$out"
printf 'opencode preflight doctor cause: PASS\n'
