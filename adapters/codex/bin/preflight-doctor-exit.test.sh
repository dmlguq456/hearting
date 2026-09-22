#!/usr/bin/env sh
# A passing adaptation-boundary check must not erase an earlier doctor failure.
# `doctor_boundary` runs in the caller's shell, so a bare `rc=$?` there shared
# the accumulator `doctor()` uses and reported status=ok with exit 0.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' 0 HUP INT TERM
FA=$T/source
mkdir -p "$FA/tools" "$FA/adapters/codex/bin" "$FA/adapters/codex/utilities" "$FA/core" \
         "$FA/utilities" "$FA/roles" "$FA/capabilities" "$FA/hooks" "$T/home" "$T/xdg" "$T/claude"
cp "$ROOT/adapters/codex/bin/preflight.sh" "$FA/adapters/codex/bin/preflight.sh"
cp "$ROOT/core/CORE.md" "$FA/core/CORE.md"
cp "$ROOT/core/ADAPTATION.md" "$FA/core/ADAPTATION.md"
cp "$ROOT/adapters/codex/utilities/agent-home.sh" "$FA/adapters/codex/utilities/agent-home.sh"
cp "$ROOT/utilities/artifact-root.sh" "$FA/utilities/artifact-root.sh"
cp -R "$ROOT/roles/." "$FA/roles/"
cp -R "$ROOT/capabilities/." "$FA/capabilities/"
cp -R "$ROOT/hooks/." "$FA/hooks/"
chmod +x "$FA/adapters/codex/utilities/agent-home.sh" "$FA/hooks/core-first-guard.sh"

# One earlier check fails; the adaptation boundary that runs after it succeeds.
printf '#!/usr/bin/env sh\nexit 1\n' > "$FA/tools/generate.py"
printf '#!/usr/bin/env sh\nexit 0\n' > "$FA/tools/check-adaptation-boundary.sh"
chmod +x "$FA/tools/generate.py" "$FA/tools/check-adaptation-boundary.sh"

out=$T/out
if env -u AGENT_DISPATCH_JOBS HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" CODEX_HOME="$T/codex" \
       CLAUDE_CONFIG_DIR="$T/claude" TMPDIR="$T" AGENT_HOME="$FA" \
       sh "$FA/adapters/codex/bin/preflight.sh" doctor >"$out" 2>&1; then rc=0; else rc=$?; fi

grep -q '^check=generated-projections:failed$' "$out" || { echo "FAIL: fixture did not fail the first check"; cat "$out"; exit 1; }
grep -q '^check=adaptation-boundary:ok$' "$out" || { echo "FAIL: fixture did not pass the boundary check"; cat "$out"; exit 1; }
grep -q '^status=failed$' "$out" || { echo "FAIL: doctor reported ok despite an earlier failure"; cat "$out"; exit 1; }
[ "$rc" -ne 0 ] || { echo "FAIL: doctor exited 0 despite an earlier failure"; cat "$out"; exit 1; }
printf 'preflight-doctor-exit: PASS\n'
