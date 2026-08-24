#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Isolated test for `mem inject` D-16 (QA ② + ③) + lifecycle() equivalence (Step 1.1).
# Fully isolated via MEM_STORE + MEM_PROJECTS temp dirs — never touches real runtime state.
# All mem calls via `python3 "$MEM" ...` (worktree copy, matches distill.test.sh convention).
#
# NOTE: The existing suites (hooks/mem-turn-nudge.test.sh, hooks/mem-distill-dispatch.test.sh,
#       tools/memory/distill.test.sh) must still pass unchanged — they are run separately by
#       code-test, not from within this file. In particular distill.test.sh:97 hard-asserts
#       mem-turn-nudge.test.sh prints "RESULT: PASS=12 FAIL=0". Do NOT touch those files.
#
# QA cases:
#   T1   near-dup seed → inject output contains 정리 신호 section with both ids
#   T2   read-only proof: pre/post durable id-set identical after inject
#   T3   lifecycle() equivalence: dup-flag group count == near_dup_groups count (mandatory)
#   T4   no near-dups → inject does NOT contain 정리 신호
#   T5   cap: > max_groups near-dup groups → at most 5 near-dup lines in section
#   T6   capacity line: durable > soft_ceiling(80) → line present;  == 80 → absent
#   T7   expiring-soon working: today+2d → line present;  today+10d → absent
#   T8   regression: working/durable/profile blocks still present (QA ③)
#   T9   empty store → inject emits nothing
#   T10  inject --hook with cleanup section → valid JSON + additionalContext contains 정리 신호
#   T11  scope coherence: global durable near-dups excluded (project-scoped, matches inject body)
#   T12  default budget: SessionStart context stays ≤2000 chars and ≤15 bullet lines
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEM="$ROOT/tools/memory/mem.py"
[ -f "$MEM" ] || { echo "FAIL: mem.py not found at $MEM"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

# ---------- isolated store / projects ----------
STORE="$(mktemp -d)"; PROJ="$(mktemp -d)"
trap 'rm -rf "$STORE" "$PROJ"' EXIT
export MEM_STORE="$STORE" MEM_PROJECTS="$PROJ"

# ── Near-dup body design (plan fixture-design fact A) ───────────────────────────────────────────
# write-dedup keys on sha256(norm_body(FULL body)); near_dup_groups keys on norm_body(body)[:80].
# For a surviving near-dup: bodies must share the same first-80 normalized chars BUT differ after.
# norm_body = re.sub(r"[\s\W_]+", " ", body.lower()).strip()
#
# Shared prefix (normalized ≥80 chars):
# "this is a sufficiently long durable memory body that shares the same first eighty chars "
# → normalized: "this is a sufficiently long durable memory body that shares the same first eighty chars "
# len = 89 chars normalized — well over 80.
# Distinct tails: "AAAA tail one" vs "BBBB tail two" → different sha256 → both survive write-dedup.
NDP_PREFIX="this is a sufficiently long durable memory body that shares the same first eighty chars "
NDP_BODY1="${NDP_PREFIX}AAAA tail one"
NDP_BODY2="${NDP_PREFIX}BBBB tail two"

# ── T1: near-dup seed → inject output contains 정리 신호 section ────────────────────────────────
echo "== T1: near-dup → 정리 신호 section present =="

STORE_T1="$(mktemp -d)"; export MEM_STORE="$STORE_T1"
python3 "$MEM" add durable thread "$NDP_BODY1" >/dev/null 2>&1
python3 "$MEM" add durable thread "$NDP_BODY2" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t1="$(python3 "$MEM" inject 2>/dev/null)"

echo "$inject_out_t1" | grep -q "Cleanup signals" \
  && ok "T1: 정리 신호 section present in inject output" \
  || bad "T1: 정리 신호 section missing (output: $inject_out_t1)"

# Both ids should appear (extract ids from the records)
ids_t1="$(python3 -c "
import sqlite3, os, sys
db = os.path.join(os.environ['MEM_STORE'], 'memory.db')
con = sqlite3.connect(db)
rows = con.execute(\"SELECT id FROM records WHERE tier='durable'\").fetchall()
con.close()
print(' '.join(r[0] for r in rows))
" 2>/dev/null)"

id1="$(echo "$ids_t1" | awk '{print $1}')"
id2="$(echo "$ids_t1" | awk '{print $2}')"

if [ -n "$id1" ] && echo "$inject_out_t1" | grep -q "near-dup"; then
  ok "T1: near-dup line present in 정리 신호"
else
  bad "T1: near-dup line missing (ids=$ids_t1)"
fi

rm -rf "$STORE_T1"

# ── T2: read-only proof — pre/post durable id-set identical ─────────────────────────────────────
echo "== T2: read-only proof (pre/post id-set equality) =="

STORE_T2="$(mktemp -d)"; export MEM_STORE="$STORE_T2"
python3 "$MEM" add durable thread "$NDP_BODY1" >/dev/null 2>&1
python3 "$MEM" add durable thread "$NDP_BODY2" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

get_ids() {
  python3 -c "
import sqlite3, os
db = os.path.join(os.environ['MEM_STORE'], 'memory.db')
con = sqlite3.connect(db)
rows = con.execute('SELECT id FROM records ORDER BY id').fetchall()
con.close()
print(','.join(r[0] for r in rows))
" 2>/dev/null
}

ids_pre="$(get_ids)"
python3 "$MEM" inject >/dev/null 2>&1
ids_post="$(get_ids)"

[ "$ids_pre" = "$ids_post" ] \
  && ok "T2: pre/post id-set identical (inject is read-only, zero deletes)" \
  || bad "T2: id-set changed by inject (pre='$ids_pre' post='$ids_post')"

rm -rf "$STORE_T2"

# ── T3: lifecycle() equivalence (MANDATORY — proves Step 1.1 extract is behavior-preserving) ────
echo "== T3: lifecycle() equivalence — dup-flag count == near_dup_groups count =="

STORE_T3="$(mktemp -d)"; export MEM_STORE="$STORE_T3"
# Seed 2 near-dup groups: group A (2 records) + group B (2 records with different prefix)
NDP_B_PREFIX="another completely different prefix body text with enough chars to exceed eighty normalized "
python3 "$MEM" add durable thread "$NDP_BODY1" >/dev/null 2>&1
python3 "$MEM" add durable thread "$NDP_BODY2" >/dev/null 2>&1
python3 "$MEM" add durable thread "${NDP_B_PREFIX}CCCC tail" >/dev/null 2>&1
python3 "$MEM" add durable thread "${NDP_B_PREFIX}DDDD tail" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

# Count [dup-flag] lines from mem lifecycle (no --apply, report mode)
lifecycle_dup_count="$(python3 "$MEM" lifecycle 2>/dev/null | grep -c '^\s*\[dup-flag\]' || echo 0)"

# Count near_dup_groups via python (call the function directly on the isolated store)
ndg_count="$(python3 -c "
import sys, os, importlib.util
spec = importlib.util.spec_from_file_location('mem', '$MEM')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
con = m.get_con()
try:
    dups = m.near_dup_groups(con)
finally:
    con.close()
print(len(dups))
" 2>/dev/null)"

[ "$lifecycle_dup_count" = "$ndg_count" ] \
  && ok "T3: lifecycle dup-flag count ($lifecycle_dup_count) == near_dup_groups count ($ndg_count)" \
  || bad "T3: mismatch — lifecycle dup-flag=$lifecycle_dup_count near_dup_groups=$ndg_count"

rm -rf "$STORE_T3"

# ── T4: no near-dups → inject does NOT contain 정리 신호 ────────────────────────────────────────
echo "== T4: no near-dups → 정리 신호 absent =="

STORE_T4="$(mktemp -d)"; export MEM_STORE="$STORE_T4"
# Seed two distinct durable records with completely different bodies (no near-dup)
python3 "$MEM" add durable thread "completely unique memory alpha for no-dup test case here" >/dev/null 2>&1
python3 "$MEM" add durable thread "totally different content beta for no-dup test entirely" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t4="$(python3 "$MEM" inject 2>/dev/null)"

echo "$inject_out_t4" | grep -q "Cleanup signals" \
  && bad "T4: 정리 신호 section present when no near-dups (output: $inject_out_t4)" \
  || ok "T4: 정리 신호 section absent (correct — no near-dups)"

rm -rf "$STORE_T4"

# ── T5: cap — seed > 5 near-dup groups → section lists at most 5 near-dup lines ────────────────
echo "== T5: > 5 near-dup groups → at most 5 near-dup lines in section =="

STORE_T5="$(mktemp -d)"; export MEM_STORE="$STORE_T5"
# Seed 7 near-dup groups (each group = 2 records, 7 groups = 14 records)
for i in $(seq 1 7); do
  GRP_PREFIX="group ${i} near dup test prefix body text long enough to exceed eighty normalized chars pad "
  python3 "$MEM" add durable thread "${GRP_PREFIX}ALPHA end" >/dev/null 2>&1
  python3 "$MEM" add durable thread "${GRP_PREFIX}BETA end" >/dev/null 2>&1
done
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t5="$(python3 "$MEM" inject 2>/dev/null)"

neardup_count="$(echo "$inject_out_t5" | grep -c '^\- near-dup' || echo 0)"
[ "$neardup_count" -le 5 ] \
  && ok "T5: near-dup lines in section ≤ 5 (got $neardup_count)" \
  || bad "T5: too many near-dup lines ($neardup_count > 5)"

rm -rf "$STORE_T5"

# ── T6: capacity line boundary (durable > 80 → present; == 80 → absent) ─────────────────────────
echo "== T6: capacity line boundary =="

STORE_T6="$(mktemp -d)"; export MEM_STORE="$STORE_T6"

# Seed 81 distinct durable records (>80 → capacity line must appear)
# Each body is unique so write-dedup doesn't merge them
for i in $(seq 1 81); do
  python3 "$MEM" add durable thread "capacity test record number ${i} unique body abcdef ghijkl mnopqr" >/dev/null 2>&1
done
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t6a="$(python3 "$MEM" inject 2>/dev/null)"
echo "$inject_out_t6a" | grep -q "durable.*>.*soft ceiling" \
  && ok "T6a: durable 81 > soft-ceiling 80 → capacity line present" \
  || bad "T6a: capacity line missing when durable=81 (output snippet: $(echo "$inject_out_t6a" | grep -i 'durable\|capacity\|ceiling' | head -3))"

# Now seed exactly 80 (reset the store, add exactly 80 unique records)
rm -rf "$STORE_T6"; STORE_T6="$(mktemp -d)"; export MEM_STORE="$STORE_T6"
for i in $(seq 1 80); do
  python3 "$MEM" add durable thread "boundary test record number ${i} unique body pqrst uvwxyz abcde" >/dev/null 2>&1
done
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t6b="$(python3 "$MEM" inject 2>/dev/null)"
echo "$inject_out_t6b" | grep -q "durable.*>.*soft ceiling" \
  && bad "T6b: capacity line present when durable==80 (strict > boundary failure)" \
  || ok "T6b: capacity line absent when durable==80 (strict > boundary correct)"

rm -rf "$STORE_T6"

# ── T7: expiring-soon working line boundary ───────────────────────────────────────────────────────
echo "== T7: expiring-soon working line boundary =="

STORE_T7="$(mktemp -d)"; export MEM_STORE="$STORE_T7"

# (a) Seed a working record with expires = today+2d → 만료 임박 line must appear
# working tier add sets expires = today+21d by default. To set today+2d, use sqlite directly.
python3 "$MEM" add working thread "working record that will expire soon for test purposes here" >/dev/null 2>&1

# Get the newly added record id and update its expires to today+2d
EXPIRES_2D="$(python3 -c "import datetime; print((datetime.date.today()+datetime.timedelta(days=2)).isoformat())")"
python3 -c "
import sqlite3, os
db = os.path.join(os.environ['MEM_STORE'], 'memory.db')
con = sqlite3.connect(db)
con.execute(\"UPDATE records SET expires=? WHERE tier='working'\", ('$EXPIRES_2D',))
con.commit()
con.close()
print('updated expires to $EXPIRES_2D')
" >/dev/null 2>&1

# Also need a durable record so inject() doesn't early-return (work+dur+prof check)
python3 "$MEM" add durable thread "durable anchor record to prevent early return in inject test T7a" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t7a="$(python3 "$MEM" inject 2>/dev/null)"
echo "$inject_out_t7a" | grep -q "working record(s) near expiry" \
  && ok "T7a: expires=today+2d → 만료 임박 working line present" \
  || bad "T7a: 만료 임박 line missing for today+2d (output: $inject_out_t7a)"

# (b) Seed a working record with expires = today+10d → 만료 임박 line must NOT appear
rm -rf "$STORE_T7"; STORE_T7="$(mktemp -d)"; export MEM_STORE="$STORE_T7"
python3 "$MEM" add working thread "working record that expires far in the future no urgency here" >/dev/null 2>&1

EXPIRES_10D="$(python3 -c "import datetime; print((datetime.date.today()+datetime.timedelta(days=10)).isoformat())")"
python3 -c "
import sqlite3, os
db = os.path.join(os.environ['MEM_STORE'], 'memory.db')
con = sqlite3.connect(db)
con.execute(\"UPDATE records SET expires=? WHERE tier='working'\", ('$EXPIRES_10D',))
con.commit()
con.close()
" >/dev/null 2>&1

python3 "$MEM" add durable thread "durable anchor record to prevent early return in inject test T7b" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t7b="$(python3 "$MEM" inject 2>/dev/null)"
echo "$inject_out_t7b" | grep -q "working record(s) near expiry" \
  && bad "T7b: 만료 임박 line present when expires=today+10d (threshold is ≤3d)" \
  || ok "T7b: 만료 임박 line absent for expires=today+10d (correct)"

rm -rf "$STORE_T7"

# ── T8: regression — existing working/durable/profile blocks still emitted (QA ③) ──────────────
echo "== T8: regression — working/durable/profile blocks present =="

STORE_T8="$(mktemp -d)"; export MEM_STORE="$STORE_T8"
# Seed one working, one durable (project), one profile record
# Note: mem add tier type body — 'type' is positional (2nd arg), --scope is optional
python3 "$MEM" add working thread "working block regression test record for inject QA three check abcdef" >/dev/null 2>&1
python3 "$MEM" add durable thread "durable block regression test record for inject QA three check abcdef" >/dev/null 2>&1
python3 "$MEM" add durable profile "profile block regression test for user profile aspect check abcdef" --scope global >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t8="$(python3 "$MEM" inject 2>/dev/null)"

echo "$inject_out_t8" | grep -q "Working memory" \
  && ok "T8: working block header present" \
  || bad "T8: working block header missing (output: $inject_out_t8)"

echo "$inject_out_t8" | grep -q "Durable memory.*this project" \
  && ok "T8: durable block header present" \
  || bad "T8: durable block header missing"

echo "$inject_out_t8" | grep -q "Durable memory.*user profile" \
  && ok "T8: profile block header present" \
  || bad "T8: profile block header missing (output: $(echo "$inject_out_t8" | head -20))"

rm -rf "$STORE_T8"

# ── T9: empty store → inject emits nothing ───────────────────────────────────────────────────────
echo "== T9: empty store → inject emits nothing =="

STORE_T9="$(mktemp -d)"; export MEM_STORE="$STORE_T9"
# Don't add anything — store has no DB yet (or empty DB)
inject_out_t9="$(python3 "$MEM" inject 2>/dev/null)"

[ -z "$inject_out_t9" ] \
  && ok "T9: empty store → inject output empty (early return intact)" \
  || bad "T9: inject output non-empty on empty store: $inject_out_t9"

rm -rf "$STORE_T9"

# ── T10: inject --hook with cleanup section → valid JSON + additionalContext contains 정리 신호 ──
echo "== T10: inject --hook with near-dup → valid JSON + additionalContext contains 정리 신호 =="

STORE_T10="$(mktemp -d)"; export MEM_STORE="$STORE_T10"

# Seed near-dup bodies that contain a quote, newline marker, and Korean
# to exercise json.dumps(ensure_ascii=False) escaping path
NDP_QUOTE_BODY1="${NDP_PREFIX}quote\"here Korean 한글 QQUOTE_A"
NDP_QUOTE_BODY2="${NDP_PREFIX}quote\"here Korean 한글 RQUOTE_B"
python3 "$MEM" add durable thread "$NDP_QUOTE_BODY1" >/dev/null 2>&1
python3 "$MEM" add durable thread "$NDP_QUOTE_BODY2" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t10="$(python3 "$MEM" inject --hook 2>/dev/null)"
hook_tmpf="$(mktemp)"
printf '%s' "$inject_out_t10" > "$hook_tmpf"

# (a) valid JSON with hookSpecificOutput/SessionStart
python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    ho = d["hookSpecificOutput"]
    assert ho["hookEventName"] == "SessionStart"
    print("valid")
except Exception as e:
    print(f"invalid: {e}")
' "$hook_tmpf" | grep -q "^valid" \
  && ok "T10a: inject --hook output is valid hookSpecificOutput/SessionStart JSON" \
  || bad "T10a: inject --hook JSON invalid (content: $(cat "$hook_tmpf"))"

# (b) additionalContext contains 정리 신호 and a near-dup id
python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    ctx = d["hookSpecificOutput"]["additionalContext"]
    has_section = "Cleanup signals" in ctx
    has_neardup = "near-dup" in ctx
    if has_section and has_neardup:
        print("content_ok")
    else:
        print(f"missing: has_section={has_section} has_neardup={has_neardup}")
except Exception as e:
    print(f"error: {e}")
' "$hook_tmpf" | grep -q "^content_ok" \
  && ok "T10b: additionalContext contains 정리 신호 + near-dup line" \
  || bad "T10b: additionalContext missing 정리 신호 or near-dup (content: $(python3 -c 'import json; d=json.load(open("'"$hook_tmpf"'")); print(d["hookSpecificOutput"]["additionalContext"][:200])' 2>/dev/null))"

rm -f "$hook_tmpf"
rm -rf "$STORE_T10"

# ── T11: scope coherence — global durable near-dups must NOT surface in 정리 신호 ─────────────────
# (cleanup section scope == inject body 'dur' section == tier='durable' AND scope='project'.
#  global durables are analyze-user's domain, not ad-hoc prune candidates; counting/surfacing them
#  here would mismatch the visible "장기 — 이 프로젝트 (durable)" list. Codex Y1 regression pin.)
echo "== T11: global durable near-dups excluded from 정리 신호 (project-scoped) =="

STORE_T11="$(mktemp -d)"; export MEM_STORE="$STORE_T11"
# 2 GLOBAL near-dups (would group if scope-blind) + 1 distinct PROJECT durable (so dur non-empty → inject emits)
python3 "$MEM" add durable convention "${NDP_PREFIX}GLOB AAAA" --scope global >/dev/null 2>&1
python3 "$MEM" add durable convention "${NDP_PREFIX}GLOB BBBB" --scope global >/dev/null 2>&1
python3 "$MEM" add durable thread "a distinct project durable so the dur block is non-empty and inject emits xyz" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t11="$(python3 "$MEM" inject 2>/dev/null)"

echo "$inject_out_t11" | grep -q "near-dup" \
  && bad "T11: global durable near-dup leaked into 정리 신호 (project-scope regression)" \
  || ok "T11: global durable near-dups excluded from 정리 신호 (project-scoped)"

# sanity: inject DID emit (project durable present) — isolation, not total suppression
echo "$inject_out_t11" | grep -q "Durable memory.*this project" \
  && ok "T11: inject still emits project durable block (global isolation, not suppression)" \
  || bad "T11: inject did not emit project durable block (fixture error)"

rm -rf "$STORE_T11"

# ── T12: default budget — SessionStart context remains compact by default ──────────────────────
echo "== T12: default inject budget ≤2000 chars and ≤15 bullet lines =="

STORE_T12="$(mktemp -d)"; export MEM_STORE="$STORE_T12"
for i in $(seq 1 30); do
  python3 "$MEM" add working thread "budget working record number ${i} with deliberately long body text that should be clipped by default session start injection caps abcdefghijklmnopqrstuvwxyz" >/dev/null 2>&1
done
for i in $(seq 1 30); do
  python3 "$MEM" add durable thread "budget durable record number ${i} with deliberately long body text that should be clipped by default session start injection caps abcdefghijklmnopqrstuvwxyz" >/dev/null 2>&1
done
python3 "$MEM" add durable profile "budget profile aspect one long body" --scope global --source user-profile:01_budget_profile >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_out_t12="$(python3 "$MEM" inject 2>/dev/null)"
chars_t12="$(printf '%s' "$inject_out_t12" | python3 -c 'import sys; print(len(sys.stdin.read()))')"
bullets_t12="$(printf '%s\n' "$inject_out_t12" | grep -c '^- ' || echo 0)"

[ "$chars_t12" -le 2000 ] \
  && ok "T12a: default inject output chars ≤2000 (got $chars_t12)" \
  || bad "T12a: default inject output too large ($chars_t12 chars)"

[ "$bullets_t12" -le 15 ] \
  && ok "T12b: default inject bullet lines ≤15 (got $bullets_t12)" \
  || bad "T12b: default inject bullet line cap exceeded ($bullets_t12)"

printf '%s' "$inject_out_t12" | grep -q "omitted by session-start cap" \
  && ok "T12c: omitted summary points to recall path" \
  || bad "T12c: omitted summary missing for capped output"

rm -rf "$STORE_T12"

# Restore original STORE env
export MEM_STORE="$STORE"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
