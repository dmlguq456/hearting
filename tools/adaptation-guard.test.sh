#!/usr/bin/env bash
# adaptation-guard.test.sh — harness-layer-sync Phase 2 음성(negative) 테스트.
#   증명 대상 (spec §10 항목 6~8 + 옵션):
#     1) delta 예외가 canonical baseline 밖으로 어긋나면(HLS-3) 가드 red.
#     2) delta baseline 이 미설정/무효면 가드 red.
#     3) adapter bootstrap 이 byte-budget 상한(HLS-9)을 넘으면 가드 red.
#     4) 파생 codex-hook 집합 대비 ADAPTATION_INVENTORY ledger drift(HLS-7) 시 가드 red.
#     5) parity-loss(HLS-8) 명시 unsupported 토큰이 사라지면 가드 red (silent skip 금지).
#     6) build-manifest REPO_ROOT realpath — 어댑터 심링크 경로 직접 실행 시 이중경로 없이 정상.
#   전략: 실트리를 임시 변형 → 가드 실행 → 메시지 assert → 무조건 원복(trap). 종료 시 트리 clean 확인.
set -uo pipefail

if ! ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  ROOT=$(cd "$(dirname -- "$0")/.." && pwd)
fi
cd "$ROOT"
GUARD="tools/check-adaptation-boundary.sh"
BM="tools/build-manifest.py"
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }

before_manifest=$(sha256sum manifest.json | awk '{print $1}')
if python3 "$BM" --help >/dev/null \
  && [ "$before_manifest" = "$(sha256sum manifest.json | awk '{print $1}')" ]; then
  ok "build-manifest --help is read-only"
else
  bad "build-manifest --help must not write manifest.json"
fi

TMP=$(mktemp -d)
CREATED=""      # 정리할 임시 생성 파일들
# 변형 전 baseline: 미커밋 Phase 2 편집·untracked 파일은 정상이므로 기준선에 포함해 비교한다.
BASELINE_STATUS=$(git status --porcelain -- hooks/ utilities/ tools/ adapters/ core/ 2>/dev/null || true)
restore_all() {
  # 백업된 원본 복원
  for b in "$TMP"/*.bak; do
    [ -e "$b" ] || continue
    orig=$(cat "$b.path")
    cp -p "$b" "$orig"
  done
  # 임시 생성 파일 제거
  for f in $CREATED; do rm -f "$f"; done
  rm -rf "$TMP"
}
trap restore_all EXIT

backup() {  # $1 = repo-relative path
  key=$(printf '%s' "$1" | sed 's#[/.]#-#g')
  cp -p "$1" "$TMP/$key.bak"
  printf '%s' "$1" > "$TMP/$key.bak.path"
}

run_guard() { sh "$GUARD" 2>&1; }

# --- Case 1: delta baseline drift → red ---
backup hooks/mem-distill-dispatch.sh
printf '\n# negative-test canonical drift marker\n' >> hooks/mem-distill-dispatch.sh
out=$(run_guard); rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'delta baseline DRIFT'; then
  ok "delta baseline drift → guard red"
else
  bad "expected delta-baseline-DRIFT red; rc=$rc"
fi
cp -p "$TMP/hooks-mem-distill-dispatch-sh.bak" hooks/mem-distill-dispatch.sh

# --- Case 2: delta baseline unset/invalid → red ---
backup tools/adaptation-exemptions.tsv
# 4번째 필드를 '-' 로 되돌려 미설정 상태 재현
awk -F'\t' 'BEGIN{OFS="\t"} $0!~/^#/ && $2=="delta"{$4="-"} {print}' \
  "$TMP/tools-adaptation-exemptions-tsv.bak" > tools/adaptation-exemptions.tsv
out=$(run_guard); rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'no valid canonical baseline hash'; then
  ok "delta baseline unset → guard red"
else
  bad "expected unset-baseline red; rc=$rc"
fi
cp -p "$TMP/tools-adaptation-exemptions-tsv.bak" tools/adaptation-exemptions.tsv

# --- Case 3: bootstrap byte-budget over ceiling → red ---
backup adapters/claude/CLAUDE.md
# Size the padding from the current bootstrap so prose compaction cannot make
# this negative fixture silently fall below the 16,384-byte ceiling.
bootstrap_size=$(wc -c < "$TMP/adapters-claude-CLAUDE-md.bak" | tr -d ' ')
padding_size=$((16384 - bootstrap_size + 1))
[ "$padding_size" -gt 0 ] || padding_size=1
{ cat "$TMP/adapters-claude-CLAUDE-md.bak"; head -c "$padding_size" /dev/zero | tr '\0' 'x'; } > adapters/claude/CLAUDE.md
out=$(run_guard); rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'bootstrap byte-budget'; then
  ok "bootstrap over byte-budget → guard red"
else
  bad "expected byte-budget red; rc=$rc"
fi
cp -p "$TMP/adapters-claude-CLAUDE-md.bak" adapters/claude/CLAUDE.md

# --- Case 4: ledger drift vs derived codex-hook set → red ---
tmp_hook="adapters/codex/hooks/_zz-negtest-lifecycle.py"
CREATED="$CREATED $tmp_hook"
printf '#!/usr/bin/env python3\n' > "$tmp_hook"
chmod +x "$tmp_hook"
out=$(run_guard); rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "ledger drifted from filesystem-derived set"; then
  ok "derived codex-hook not in ledger → guard red (ledger drift)"
else
  bad "expected ledger-drift red; rc=$rc"
fi
rm -f "$tmp_hook"; CREATED=$(printf '%s' "$CREATED" | sed "s#$tmp_hook##")

# --- Case 5: parity-loss token removed → red ---
backup adapters/codex/bin/preflight.sh
sed 's/claude_headless=unsupported/claude_headless=SILENTLYDROPPED/g' \
  "$TMP/adapters-codex-bin-preflight-sh.bak" > adapters/codex/bin/preflight.sh
out=$(run_guard); rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'parity-loss for'; then
  ok "parity-loss unsupported token removed → guard red"
else
  bad "expected parity-loss red; rc=$rc"
fi
cp -p "$TMP/adapters-codex-bin-preflight-sh.bak" adapters/codex/bin/preflight.sh

# --- Case 6: REPO_ROOT realpath — 어댑터 심링크 경로 직접 실행 정상 ---
if [ -L adapters/claude/tools/build-manifest.py ]; then
  out=$(python3 adapters/claude/tools/build-manifest.py --check 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'up-to-date'; then
    ok "build-manifest via adapter symlink resolves repo root (realpath)"
  else
    bad "expected symlink-run --check to pass; rc=$rc out=[$out]"
  fi
else
  ok "build-manifest adapter path is concrete (skip symlink realpath case)"
fi

# --- Case 7: 하위 디렉토리 비-symlink 실파일 심으면 → red (재귀 census, HLS-5/7) ---
# Phase 1 census 가 놓친 "하위 디렉토리 물리 복사본 생존" 클래스를 기계가 잡는지 증명.
tmp_census="adapters/claude/tools/memory/_zz-negtest-census.py"
CREATED="$CREATED $tmp_census"
printf '#!/usr/bin/env python3\n# negative-test nested real file\n' > "$tmp_census"
out=$(run_guard); rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'recursive census'; then
  ok "nested non-symlink real file under shared layer → guard red (recursive census)"
else
  bad "expected recursive-census red; rc=$rc"
fi
rm -f "$tmp_census"; CREATED=$(printf '%s' "$CREATED" | sed "s#$tmp_census##")

# --- 종료: 음성 변형이 baseline(변형 전 상태)으로 원복됐는지 확인 ---
now_status=$(git status --porcelain -- hooks/ utilities/ tools/ adapters/ core/ 2>/dev/null || true)
if [ "$now_status" = "$BASELINE_STATUS" ]; then
  ok "working tree restored to pre-test baseline after negative mutations"
else
  bad "working tree diverged from baseline: $(diff <(printf '%s' "$BASELINE_STATUS") <(printf '%s' "$now_status") | tr '\n' ' ')"
fi

# --- 양성: 원복 후 가드가 다시 green 인지 확인 ---
if sh "$GUARD" >/dev/null 2>&1; then
  ok "guard green again after restore"
else
  bad "guard not green after restore"
fi

if [ "$fails" -eq 0 ]; then
  printf '\nPASS: all %s\n' "adaptation-guard negative tests"
  exit 0
fi
printf '\nFAILED: %d case(s)\n' "$fails"
exit 1
