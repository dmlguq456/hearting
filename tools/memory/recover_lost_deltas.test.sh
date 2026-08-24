#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Regressions for tools/memory/recover-lost-deltas.py (P3-1).
#
# The tool rewinds distill markers over the 2026-08-10 worker-outage window.
# What matters is that it cannot make the outage worse: a dry run must write
# nothing, --apply must refuse while the worker is still broken, rewinds must
# be backed up, and repeated runs must converge instead of walking backwards.
set -uo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
TOOL="$ROOT/tools/memory/recover-lost-deltas.py"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ❌ $1"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SID="11111111-2222-3333-4444-555555555555"
export MEM_STORE="$TMP/store"
export MEM_DB="$TMP/store/memory.db"
mkdir -p "$MEM_STORE" "$TMP/projects/proj"

# Transcript straddling the outage boundary: two messages before, two after.
cat > "$TMP/projects/proj/$SID.jsonl" <<EOF
{"type":"user","uuid":"uuid-before-1","timestamp":"2026-08-10T19:00:00.000Z","message":{"content":"before one"}}
{"type":"assistant","uuid":"uuid-before-2","timestamp":"2026-08-10T19:30:00.000Z","message":{"content":[{"type":"text","text":"before two"}]}}
{"type":"user","uuid":"uuid-after-1","timestamp":"2026-08-10T21:00:00.000Z","message":{"content":"after one"}}
{"type":"assistant","uuid":"uuid-after-2","timestamp":"2026-08-10T22:00:00.000Z","message":{"content":[{"type":"text","text":"after two"}]}}
EOF

MARKER="$MEM_STORE/.distill-state-$SID"
reset_marker() {
  printf 'uuid-after-2\n' > "$MARKER"
  touch -d '2026-08-11T02:00:00Z' "$MARKER"
  rm -f "$MARKER.pre-recover" 2>/dev/null || true
}

run_tool() {
  MEM_PROJECTS="$TMP/projects" python3 - "$@" <<'PY'
import sys, os
from pathlib import Path
sys.argv = ["recover-lost-deltas.py"] + sys.argv[1:]
sys.path.insert(0, os.path.join(os.environ["ROOT"], "tools", "memory"))
import mem
mem.PROJECTS = Path(os.environ["MEM_PROJECTS"])
src = Path(os.environ["ROOT"]) / "tools" / "memory" / "recover-lost-deltas.py"
code = src.read_text()
ns = {"__name__": "__main__", "__file__": str(src)}
try:
    exec(compile(code, str(src), "exec"), ns)
except SystemExit as e:
    raise SystemExit(e.code)
PY
}
export ROOT

echo "== ① dry run is the default and writes nothing =="
reset_marker
before=$(sha256sum "$MARKER" | cut -d' ' -f1)
out=$(run_tool --since 2026-08-10T20:16:00 2>&1); rc=$?
after=$(sha256sum "$MARKER" | cut -d' ' -f1)
[ "$rc" -eq 0 ] && [ "$before" = "$after" ] \
  && ok "dry run: exit 0 and marker byte-identical" \
  || bad "dry run mutated the marker or failed (rc=$rc)"
grep -q 'dry run: nothing written' <<<"$out" \
  && ok "dry run: says so explicitly" || bad "dry run: missing notice"
[ ! -e "$MARKER.pre-recover" ] \
  && ok "dry run: no backup created" || bad "dry run: created a backup"

echo "== ② the rewind target is the last UUID before the window =="
grep -q 'uuid-bef' <<<"$out" \
  && ok "target resolves to a pre-outage UUID" || bad "target wrong: $out"
grep -qE 'deltas~2' <<<"$out" \
  && ok "counts the 2 post-outage deltas" || bad "delta count wrong: $out"

echo "== ③ --apply is refused while the worker cannot boot =="
reset_marker
before=$(sha256sum "$MARKER" | cut -d' ' -f1)
out=$(MEM_DISTILL_WORKER="$TMP/does-not-exist.sh" AGENT_HOME="$TMP/no-such-home" \
        run_tool --since 2026-08-10T20:16:00 --apply 2>&1); rc=$?
after=$(sha256sum "$MARKER" | cut -d' ' -f1)
[ "$rc" -ne 0 ] && [ "$before" = "$after" ] \
  && ok "missing worker: refused, marker untouched" \
  || bad "missing worker: should refuse (rc=$rc, out=$out)"

cat > "$TMP/broken-worker.sh" <<'EOS'
#!/usr/bin/env bash
echo "line 39: CFG_LIFECYCLE_NUDGE: unbound variable" >&2
exit 1
EOS
chmod +x "$TMP/broken-worker.sh"
reset_marker
before=$(sha256sum "$MARKER" | cut -d' ' -f1)
out=$(MEM_DISTILL_WORKER="$TMP/broken-worker.sh" run_tool --since 2026-08-10T20:16:00 --apply 2>&1); rc=$?
after=$(sha256sum "$MARKER" | cut -d' ' -f1)
[ "$rc" -ne 0 ] && [ "$before" = "$after" ] \
  && ok "dead worker: refused, marker untouched" \
  || bad "dead worker: should refuse (rc=$rc, out=$out)"

echo "== ④ --apply rewinds and backs up when the worker boots =="
cat > "$TMP/good-worker.sh" <<'EOS'
#!/usr/bin/env bash
exit 0
EOS
chmod +x "$TMP/good-worker.sh"
reset_marker
out=$(MEM_DISTILL_WORKER="$TMP/good-worker.sh" run_tool --since 2026-08-10T20:16:00 --apply 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "healthy worker: apply succeeded" || bad "apply failed (rc=$rc): $out"
grep -q 'uuid-before-2' "$MARKER" \
  && ok "marker rewound to the last pre-outage UUID" \
  || bad "marker not rewound: $(cat "$MARKER")"
[ -e "$MARKER.pre-recover" ] && grep -q 'uuid-after-2' "$MARKER.pre-recover" \
  && ok "pre-rewind value backed up" || bad "backup missing or wrong"

echo "== ⑤ idempotent: a second apply does not walk backwards =="
mid=$(sha256sum "$MARKER" | cut -d' ' -f1)
bak=$(sha256sum "$MARKER.pre-recover" | cut -d' ' -f1)
out=$(MEM_DISTILL_WORKER="$TMP/good-worker.sh" run_tool --since 2026-08-10T20:16:00 --apply 2>&1)
[ "$(sha256sum "$MARKER" | cut -d' ' -f1)" = "$mid" ] \
  && ok "second apply: marker unchanged" || bad "second apply moved the marker"
[ "$(sha256sum "$MARKER.pre-recover" | cut -d' ' -f1)" = "$bak" ] \
  && ok "second apply: backup not overwritten" || bad "backup clobbered"

echo "== ⑥ markers outside the window are ignored =="
OLD="$MEM_STORE/.distill-state-99999999-0000-0000-0000-000000000000"
printf 'uuid-old\n' > "$OLD"; touch -d '2026-08-01T00:00:00Z' "$OLD"
out=$(run_tool --since 2026-08-10T20:16:00 2>&1)
grep -q '99999999' <<<"$out" \
  && bad "pre-window marker was selected" || ok "pre-window marker ignored"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
