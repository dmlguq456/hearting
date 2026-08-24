#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# retrieval-eval.test.sh — mini retrieval eval (LongMemEval 축소판)
#
# Fixed query → expected-record contract test against LIVE store content.
# Unlike the fixture suites (mem_retrieval_v14 etc., which build synthetic
# stores), this asserts that real durable records remain reachable through
# the real `mem recall` pipeline.
#
# Read-only guarantee: the live memory.db is opened mode=ro exactly once, for
# a consistent sqlite backup-API snapshot (WAL-safe) into a temp store; every
# mem.py invocation then runs with MEM_STORE pointed at that snapshot, so the
# live DB, its WAL, and live telemetry receive zero writes — even if a
# schema migration were pending, it would only touch the disposable copy.
# Retrieval results are identical to querying the live store: same mem.py,
# same data, same cwd-based project fence (we run from AGENT_HOME).
#
# Tiers:
#   TIER1  (always on)  — English/token queries for stable durable records.
#   TIER2  (gated)      — Korean probes from the retrieval audit. Enable with
#                         RETRIEVAL_EVAL_TIER2=1. NOTE for the finalizer:
#                         flip TIER2 on by default (remove the gate) once the
#                         CJK-bigram repair lands in mem.py (in flight by the
#                         mem.py owner as of 2026-07-22).
#   Nonsense guard      — a garbage query must return zero hits.
#
# Failure taxonomy: `MISSING-RECORD` means the expected record no longer
# exists in the store (fixture must be re-picked — curator may have merged or
# pruned it); `BAD retrieval` means the record exists but recall missed it.
#
# Runtime budget: < 30 s (a snapshot copy plus a handful of CLI calls).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MEM="$HERE/mem.py"
export AGENT_HOME="${AGENT_HOME:-$(cd "$HERE/../.." && pwd)}"
LIVE_DB="$AGENT_HOME/memory/memory.db"
START=$SECONDS

PASS=0 FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok  %s\n' "$*"; }
bad(){ FAIL=$((FAIL+1)); printf '  BAD %s\n' "$*"; }

if [ ! -f "$LIVE_DB" ]; then
  # Linked worktrees / CI checkouts have no live store (memory/ is its own
  # repo and memory.db is a local-only SoT). A live-content eval cannot run
  # there; skip instead of failing unrelated guard sweeps.
  echo "SKIP: live store not found at $LIVE_DB (worktree/CI checkout?)"
  exit 0
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SNAP="$TMP/store"; mkdir -p "$SNAP"

# Consistent point-in-time snapshot of the live DB (backup API includes WAL).
python3 - "$LIVE_DB" "$SNAP/memory.db" <<'PY' || { echo "FATAL: snapshot failed"; exit 1; }
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY

export MEM_STORE="$SNAP"
export MEM_RECALL_EVENTS="$TMP/recall-events.jsonl"
cd "$AGENT_HOME"   # live project fence: current project + global scope

recall(){ python3 "$MEM" recall "$1" --no-touch --limit 20 2>/dev/null; }

# assert_hit <tier-label> <query> <expected-record-id>
assert_hit(){
  local label="$1" query="$2" expect="$3" out
  if ! python3 - "$SNAP/memory.db" "$expect" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = con.execute("select 1 from records where id=?", (sys.argv[2],)).fetchone()
sys.exit(0 if row else 1)
PY
  then
    bad "$label MISSING-RECORD: $expect no longer in store — re-pick this fixture (query: $query)"
    return
  fi
  out="$(recall "$query")"
  if grep -qF "$expect" <<<"$out"; then
    ok "$label \"$query\" → $expect"
  else
    bad "$label retrieval missed: \"$query\" ↛ $expect"
  fi
}

echo "== TIER1: English/token queries (must pass) =="
# Picked 2026-07-22 from live durable content: high-strength records with
# distinctive tokens, fenced to this project (git:.../agent_setting) + global.
assert_hit TIER1 "one-shot headless conductor"        "lesson_lesson-one-shot-headless_0f3dd6"
assert_hit TIER1 "cross-harness dispatch parent_sid"  "fact_fact-cross-harness-dispatch_a38641"
assert_hit TIER1 "jobs.log pipe-delimited"            "dispatch-format_dispatch-format-jobs-log_c1ee28"
assert_hit TIER1 "Pocock predictability"              "decision_decision-pocock-4축-predictability를_8d346e"

# Doc-pointer guard: roles/units/material/figure-gen.md (Script Convention)
# points at this durable output-packaging rule via `mem show <id> --all`;
# keep the pointer target resolvable.
if python3 "$MEM" show "feedback_feedback-figure-자동-제작_6eade0" --all >/dev/null 2>&1; then
  ok "TIER1 doc-pointer: mem show feedback_feedback-figure-자동-제작_6eade0 --all resolves"
else
  bad "TIER1 doc-pointer: figure-gen.md output-rule record unresolvable (update the unit pointer)"
fi

echo "== TIER2: Korean probes (audit set) =="
if [ "${RETRIEVAL_EVAL_TIER2:-1}" = "1" ]; then  # default ON since 2026-07-22 CJK-bigram repair
  # Finalizer: after the CJK-bigram repair lands in mem.py, delete the gate
  # above (run these unconditionally) — they define the repaired contract.
  assert_hit TIER2 "스펙트로그램"  "feedback_feedback-음성-오디오가-포함되는_0abb13"
  assert_hit TIER2 "코딩 컨벤션"   "profile_profile-aspect-coding-convention_e0458f"
else
  echo "  (gated: set RETRIEVAL_EVAL_TIER2=1 — pending CJK-bigram repair in mem.py)"
fi

echo "== Nonsense query must return empty =="
NONSENSE_OUT="$(recall 'zxqvwm flurblegrommet nonesuch')"
if grep -qF '(no store matches)' <<<"$NONSENSE_OUT"; then
  ok "nonsense query returns zero hits"
else
  bad "nonsense query returned hits: $(grep -c '^\s*\[' <<<"$NONSENSE_OUT" || true) lines"
fi

ELAPSED=$((SECONDS - START))
[ "$ELAPSED" -lt 30 ] && ok "runtime ${ELAPSED}s < 30s budget" \
  || bad "runtime ${ELAPSED}s exceeds 30s budget"

printf '\nRESULT: PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
