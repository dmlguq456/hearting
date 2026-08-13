#!/usr/bin/env bash
# mem-periodic-curate — nightly opt-in project curator (R-3).
#   The single cron firing point for periodic curation. Runs one
#   `mem-distill-dispatch.sh periodic-curate <cwd>` per eligible project,
#   sequentially, inside the existing D-41 slot/budget/lock/GC guards the
#   dispatcher already enforces. No session-event fan-out — this script is the
#   only caller of periodic-curate mode, which is the hard architectural
#   constraint that avoids repeating the v18 216-worker incident (a mass of
#   simultaneous per-session launches with no global bound).
#
#   Opt-in gate: MEM_PERIODIC_CURATE_ENABLE=1, same shape as the existing
#   MEM_DISTILL_ENABLE (v8 precedent). Unset is a complete no-op. This script
#   ships with the feature default OFF; turning it on is the operator's call.
#
#   D-42 worker boundary: cron is not an interactive main session, but nested
#   or double-registered invocation must still be refused the same way a
#   worker session is refused. Check the same markers the dispatcher checks,
#   before any project enumeration or dispatch.
#
#   cron registration is documented only (Phase 3, core/HOOKS.md); this script
#   does not install a crontab entry itself.
set -euo pipefail
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh")}"
DISPATCH="$HOOK_DIR/../hooks/mem-distill-dispatch.sh"
MEM="${MEM_PY:-$AGENT_HOME/tools/memory/mem.py}"

[ "${MEM_PERIODIC_CURATE_ENABLE:-}" = "1" ] || exit 0

# D-42: refuse a worker/registered/dispatch-child context exactly like the
# dispatcher does, so cron cannot be used to route around the main/worker
# boundary via nested invocation.
if [ "${AGENT_SESSION_ROLE:-}" = "worker" ] \
  || [ "${AGENT_DISPATCH_CHILD:-}" = "1" ] \
  || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
  || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
  || [ "${FLEET_TITLE_REFRESH:-}" = "1" ] \
  || [ "${MEM_DISTILL:-}" = "1" ]; then
  exit 0
fi

default_store="$AGENT_HOME/memory"
[ -e "$default_store" ] || [ -L "$default_store" ] \
  || default_store="${XDG_DATA_HOME:-$HOME/.local/share}/hearting/memory"
STORE="${MEM_STORE:-$default_store}"
PROJECTS_ROOT="${MEM_PROJECTS:-$HOME/.claude/projects}"

case "${MEM_PERIODIC_CURATE_MAX_PROJECTS:-8}" in
  ''|*[!0-9]*) _max=8 ;;
  *) _max="${MEM_PERIODIC_CURATE_MAX_PROJECTS:-8}" ;;
esac

# Eligible projects are `PROJECTS_ROOT/<encoded-cwd>/memory` directories — the
# same convention tools/memory/mem.py already uses for the profile projection
# (mem.py:2658). Decode each encoded directory name back to a real, existing
# cwd with the same walk-from-root algorithm mem.py's own `_decode_enc_cwd`
# uses; this is a small, self-contained duplication rather than an import,
# matching this cycle's F-1 precedent of not sharing code across a boundary
# whose reliability is exactly what is in question.
_project_paths() {
  python3 - "$PROJECTS_ROOT" "$_max" <<'PYEOF'
import re
import sys
from pathlib import Path

projects_root = Path(sys.argv[1])
limit = int(sys.argv[2])


def decode(enc):
    def walk(cur, rem, depth):
        if depth > 64:
            return None
        if rem == "":
            return cur if cur.is_dir() else None
        if not rem.startswith("-"):
            return None
        body = rem[1:]
        if body == "":
            return cur if cur.is_dir() else None
        try:
            children = sorted(p.name for p in cur.iterdir())
        except Exception:
            return None
        for name in children:
            e = re.sub(r"[/._]", "-", name)
            if body == e:
                cand = cur / name
                if cand.is_dir():
                    return cand
            elif body.startswith(e + "-"):
                r = walk(cur / name, body[len(e):], depth + 1)
                if r is not None:
                    return r
        return None

    if not enc.startswith("-"):
        return None
    return walk(Path("/"), enc, 0)


if not projects_root.is_dir():
    sys.exit(0)

count = 0
for memory_dir in sorted(projects_root.glob("*/memory")):
    if count >= limit:
        break
    if not memory_dir.is_dir():
        continue
    resolved = decode(memory_dir.parent.name)
    if resolved is None:
        continue
    print(str(resolved))
    count += 1
PYEOF
}

# Sequential for loop only — a background `&` here would recreate the v18
# fan-out shape this design explicitly avoids. Failures are per-project and
# do not stop the remaining projects; the dispatcher's own D-41 guards (kill
# switch, slots, start budget, GC) still bound every individual call. Do not
# set AGENT_SESSION_ROLE=worker here: that is this script's own D-42 boundary
# against nested invocation, not a marker for the dispatcher call below — the
# dispatcher checks the identical marker and would otherwise treat every
# periodic-curate dispatch as a worker call and silently no-op it.
#
# The dispatcher backgrounds its own worker and returns immediately, so a
# bare sequential call here would still let multiple projects' workers run
# concurrently. Compute the same per-project lock name the dispatcher derives
# from cwd and poll for its release before moving to the next project, so
# "sequential" holds for the actual curator runs, not just for this loop.
while IFS= read -r cwd; do
  [ -n "$cwd" ] || continue
  project_key="$(printf '%s' "$cwd" | python3 -c 'import sys, hashlib; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null || true)"
  [ -n "$project_key" ] || continue
  MEM_PY="$MEM" MEM_STORE="$STORE" \
    bash "$DISPATCH" periodic-curate "$cwd" </dev/null >/dev/null 2>&1 || true
  # Wait out the whole curate budget, not an arbitrary 10 seconds. A deep
  # curator legitimately runs up to MEM_DISTILL_TIMEOUT_CURATE (default 600s),
  # so a short poll would return early and let the next project's dispatch
  # overlap the previous one — silently breaking the "sequential" contract this
  # script, core/HOOKS.md, and core/MEMORY.md all state. The dispatcher's own
  # D-41 slots still cap total concurrency; this loop is what makes the
  # stated invariant true rather than approximately true.
  _wait_budget="${MEM_PERIODIC_CURATE_WAIT:-${MEM_DISTILL_TIMEOUT_CURATE:-600}}"
  # +60s of headroom so the lock's own stale cleanup, not this poll, is what
  # releases a wedged run; then give up rather than blocking the night's batch.
  _deadline=$(( $(date +%s) + _wait_budget + 60 ))
  while [ -d "$STORE/.distill-lock-periodic-$project_key" ]; do
    [ "$(date +%s)" -ge "$_deadline" ] && break
    sleep 1
  done
done < <(_project_paths)

exit 0
