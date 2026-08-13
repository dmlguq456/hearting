#!/usr/bin/env bash
# installed-layout-home.test.sh — F-2 (plan §6 Phase 1) 설치 레이아웃 워커 홈 해석 회귀.
#   증명 대상: mem-distill-worker.sh / model-map.sh / worktree-cleanup.sh 가 설치 레이아웃
#   (~/.claude/bin, 고정 3단 hop 이 잘못된 루트를 잡는 위치)에서도 harness root 를 정확히
#   찾는지. 두 픽스처 형태 (a) 디렉토리 심링크, (b) 실파일 복사 를 모두 검증한다.
#   EXPECT_RED=1 이면 수정 전 코드의 실패를 단정하는 red 모드로 동작하고,
#   기본(EXPECT_RED 미설정)이면 수정 후 정상 동작을 단정하는 green 모드로 동작한다.
set -u
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }
fail() { printf 'FAIL - %s\n' "$1"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ -n "$(find "$TMP" -name .git 2>/dev/null)" ]; then
  fail "fixture root unexpectedly contains .git"
fi

# --- bundle (레포 "번들" 사본) ---------------------------------------------
mkdir -p "$TMP/bundle/source/core" "$TMP/bundle/source/utilities" \
         "$TMP/bundle/source/adapters/claude/bin"
: > "$TMP/bundle/source/harness-manifest.json"
: > "$TMP/bundle/source/core/CORE.md"

cat > "$TMP/bundle/source/utilities/model-config.sh" <<'EOF'
#!/usr/bin/env sh
cat <<CFG
CFG_TIER_DEEP_MODEL=BUNDLE-SENTINEL
CFG_TIER_MINI_MODEL=BUNDLE-SENTINEL
CFG_TIER_LIGHT_MODEL=BUNDLE-SENTINEL
CFG_TIER_DEEP_EFFORT=high
CFG_TIER_LIGHT_EFFORT=medium
CFG_LIFECYCLE_NUDGE=light
CFG_LIFECYCLE_CURATE=deep
CFG_ROLES_DEEP='|deep maker|'
CFG_ROLES_LIGHT='|fast implementer|'
CFG
EOF
chmod +x "$TMP/bundle/source/utilities/model-config.sh"

cp "$REPO_ROOT/utilities/agent-home.sh" "$TMP/bundle/source/utilities/agent-home.sh"
chmod +x "$TMP/bundle/source/utilities/agent-home.sh"

cat > "$TMP/bundle/source/utilities/worktree-cleanup.py" <<'EOF'
#!/usr/bin/env python3
print("WT-BUNDLE")
EOF
chmod +x "$TMP/bundle/source/utilities/worktree-cleanup.py"

for f in mem-distill-worker.sh model-map.sh worktree-cleanup.sh; do
  cp "$REPO_ROOT/adapters/claude/bin/$f" "$TMP/bundle/source/adapters/claude/bin/$f"
  chmod +x "$TMP/bundle/source/adapters/claude/bin/$f"
done
# Fossil of the pre-fix code (§6 Phase 1 F-2 "_legacy_probe.sh") — supporting
# evidence only, never the primary red assertion.
cat > "$TMP/bundle/source/adapters/claude/bin/_legacy_probe.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
_mmdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
_root=$(CDPATH= cd -- "$_mmdir/../../.." && pwd)
eval "$("$_root/utilities/model-config.sh" --adapter claude --source-root "$_root")"
printf '%s\n' "$CFG_LIFECYCLE_NUDGE"
EOF
chmod +x "$TMP/bundle/source/adapters/claude/bin/_legacy_probe.sh"
ln -s ../../utilities "$TMP/bundle/source/adapters/claude/utilities"

# --- managed release (agent-home.sh 가 XDG 경유로 찾는 대상) ----------------
mkdir -p "$TMP/release/core" "$TMP/release/utilities"
: > "$TMP/release/harness-manifest.json"
: > "$TMP/release/core/CORE.md"
cat > "$TMP/release/utilities/model-config.sh" <<'EOF'
#!/usr/bin/env sh
cat <<CFG
CFG_TIER_DEEP_MODEL=RELEASE-SENTINEL
CFG_TIER_MINI_MODEL=RELEASE-SENTINEL
CFG_TIER_LIGHT_MODEL=RELEASE-SENTINEL
CFG_TIER_DEEP_EFFORT=high
CFG_TIER_LIGHT_EFFORT=medium
CFG_LIFECYCLE_NUDGE=light
CFG_LIFECYCLE_CURATE=deep
CFG_ROLES_DEEP='|deep maker|'
CFG_ROLES_LIGHT='|fast implementer|'
CFG
EOF
chmod +x "$TMP/release/utilities/model-config.sh"
cat > "$TMP/release/utilities/worktree-cleanup.py" <<'EOF'
#!/usr/bin/env python3
print("WT-RELEASE")
EOF
chmod +x "$TMP/release/utilities/worktree-cleanup.py"
mkdir -p "$TMP/xdg/hearting"
ln -s "$TMP/release" "$TMP/xdg/hearting/current"

: > "$TMP/prompt.txt"

# --- 형태 (a): 디렉토리 심링크 (현 실배포) ----------------------------------
mkdir -p "$TMP/homeA/.claude"
ln -s "$TMP/bundle/source/adapters/claude/bin" "$TMP/homeA/.claude/bin"
ln -s "$TMP/bundle/source/adapters/claude/utilities" "$TMP/homeA/.claude/utilities"

# --- 형태 (b): 실파일 복사 (Windows/copy 브랜치) ----------------------------
mkdir -p "$TMP/homeB/.claude/bin" "$TMP/homeB/.claude/utilities"
for f in "$TMP/bundle/source/adapters/claude/bin"/*.sh; do
  cp "$f" "$TMP/homeB/.claude/bin/"
done
cp "$TMP/bundle/source/utilities/model-config.sh" "$TMP/homeB/.claude/utilities/"
cp "$TMP/bundle/source/utilities/agent-home.sh" "$TMP/homeB/.claude/utilities/"
cp "$TMP/bundle/source/utilities/worktree-cleanup.py" "$TMP/homeB/.claude/utilities/"

if [ -n "$(find "$TMP" -name .git 2>/dev/null)" ]; then
  fail "fixture unexpectedly contains .git after population"
fi

runenv() { # $1=HOME ... command
  _h=$1; shift
  env -u AGENT_HOME -u CLAUDE_HOME HOME="$_h" XDG_DATA_HOME="$TMP/xdg" \
    PATH="/usr/bin:/bin" "$@"
}

# red 판정 = 근본 원인(잘못된 루트 아래 파일을 집는다) 공통 1차 단정 + 스크립트별 2차 시그니처.
# 세 스크립트의 실패 문자열은 서로 다르므로(§3.1) 단일 공통 정규식은 쓰지 않는다(r3 정정).
assert_red() {   # $1=label $2=home $3=sig1(ERE) $4=sig2(ERE, may be empty) $5...=command
  _label=$1; _home=$2; _sig1=$3; _sig2=$4; shift 4
  _err=$(runenv "$_home" "$@" 2>&1 >/dev/null)
  _rc=$?
  if [ "$_rc" -eq 0 ]; then
    bad "$_label: expected nonzero exit, got 0"
    return
  fi
  # 1차: 비영 exit AND stderr 에 번들 루트가 아닌 <WRONG>/utilities/{model-config.sh|worktree-cleanup.py}
  if ! printf '%s' "$_err" | grep -Eq "$TMP/utilities/(model-config\.sh|worktree-cleanup\.py)"; then
    bad "$_label: no wrong-root path in stderr; got: $_err"
    return
  fi
  if printf '%s' "$_err" | grep -Fq "$TMP/bundle/source/utilities/"; then
    bad "$_label: resolved the bundle root — not red; got: $_err"
    return
  fi
  # 2차: 스크립트별 시그니처
  if ! printf '%s' "$_err" | grep -Eq "$_sig1"; then
    bad "$_label: missing signature /$_sig1/; got: $_err"
    return
  fi
  if [ -n "$_sig2" ] && ! printf '%s' "$_err" | grep -Eq "$_sig2"; then
    bad "$_label: missing signature /$_sig2/; got: $_err"
    return
  fi
  ok "$_label: red reproduced (rc=$_rc, wrong root + signature)"
}

assert_green_call() {  # $1=label $2=home $3...=command
  _label=$1; _home=$2; shift 2
  _err=$(runenv "$_home" "$@" 2>&1 >/dev/null)
  _rc=$?
  if [ "$_rc" -ne 0 ]; then
    bad "$_label: expected exit 0, got $_rc; stderr: $_err"
    return
  fi
  if printf '%s' "$_err" | grep -Eq 'unbound variable|No such file or directory'; then
    bad "$_label: unexpected error text in stderr: $_err"
    return
  fi
  ok "$_label: exit 0, clean stderr"
}

RED=${EXPECT_RED:-}

if [ -n "$RED" ]; then
  echo "== red mode (EXPECT_RED=1) — 실스크립트 3종 x 픽스처 2형태 = 6 단정 =="
  assert_red "(a) mem-distill-worker.sh" "$TMP/homeA" 'CFG_LIFECYCLE_NUDGE' '' "$TMP/homeA/.claude/bin/mem-distill-worker.sh" increment fast-distiller "$TMP/prompt.txt"
  assert_red "(a) model-map.sh"          "$TMP/homeA" 'CFG_ROLES_DEEP' '' "$TMP/homeA/.claude/bin/model-map.sh" 'deep maker'
  assert_red "(a) worktree-cleanup.sh"   "$TMP/homeA" 'worktree-cleanup\.py' "can't open file|No such file" "$TMP/homeA/.claude/bin/worktree-cleanup.sh" --help
  assert_red "(b) mem-distill-worker.sh" "$TMP/homeB" 'CFG_LIFECYCLE_NUDGE' '' "$TMP/homeB/.claude/bin/mem-distill-worker.sh" increment fast-distiller "$TMP/prompt.txt"
  assert_red "(b) model-map.sh"          "$TMP/homeB" 'CFG_ROLES_DEEP' '' "$TMP/homeB/.claude/bin/model-map.sh" 'deep maker'
  assert_red "(b) worktree-cleanup.sh"   "$TMP/homeB" 'worktree-cleanup\.py' "can't open file|No such file" "$TMP/homeB/.claude/bin/worktree-cleanup.sh" --help

  echo "== _legacy_probe.sh 보조 증거 (두 형태) =="
  assert_red "(a) _legacy_probe.sh" "$TMP/homeA" 'CFG_LIFECYCLE_NUDGE' '' "$TMP/homeA/.claude/bin/_legacy_probe.sh"
  assert_red "(b) _legacy_probe.sh" "$TMP/homeB" 'CFG_LIFECYCLE_NUDGE' '' "$TMP/homeB/.claude/bin/_legacy_probe.sh"
else
  echo "== green mode — 실스크립트 3종 x 픽스처 2형태 = 6 단정 + 센티널 판별 =="
  assert_green_call "(a) mem-distill-worker.sh" "$TMP/homeA" "$TMP/homeA/.claude/bin/mem-distill-worker.sh" increment fast-distiller "$TMP/prompt.txt"
  assert_green_call "(a) model-map.sh"          "$TMP/homeA" "$TMP/homeA/.claude/bin/model-map.sh" 'deep maker'
  assert_green_call "(a) worktree-cleanup.sh"   "$TMP/homeA" "$TMP/homeA/.claude/bin/worktree-cleanup.sh" --help
  assert_green_call "(b) mem-distill-worker.sh" "$TMP/homeB" "$TMP/homeB/.claude/bin/mem-distill-worker.sh" increment fast-distiller "$TMP/prompt.txt"
  assert_green_call "(b) model-map.sh"          "$TMP/homeB" "$TMP/homeB/.claude/bin/model-map.sh" 'deep maker'
  assert_green_call "(b) worktree-cleanup.sh"   "$TMP/homeB" "$TMP/homeB/.claude/bin/worktree-cleanup.sh" --help

  _outA=$(runenv "$TMP/homeA" "$TMP/homeA/.claude/bin/model-map.sh" 'deep maker' 2>/dev/null)
  case "$_outA" in
    *BUNDLE-SENTINEL*) case "$_outA" in *RELEASE-SENTINEL*) bad "(a) model-map.sh: unexpectedly saw RELEASE-SENTINEL too" ;; *) ok "(a) model-map.sh: BUNDLE-SENTINEL wins (step 2 before step 3)" ;; esac ;;
    *) bad "(a) model-map.sh: expected BUNDLE-SENTINEL; got: $_outA" ;;
  esac
  _outB=$(runenv "$TMP/homeB" "$TMP/homeB/.claude/bin/model-map.sh" 'deep maker' 2>/dev/null)
  case "$_outB" in
    *RELEASE-SENTINEL*) ok "(b) model-map.sh: RELEASE-SENTINEL (copy-layout falls to managed release, intended)" ;;
    *) bad "(b) model-map.sh: expected RELEASE-SENTINEL; got: $_outB" ;;
  esac

  _wtA=$(runenv "$TMP/homeA" "$TMP/homeA/.claude/bin/worktree-cleanup.sh" --help 2>/dev/null)
  case "$_wtA" in
    *WT-BUNDLE*) ok "(a) worktree-cleanup.sh: WT-BUNDLE (resolved bundle root)" ;;
    *) bad "(a) worktree-cleanup.sh: expected WT-BUNDLE; got: $_wtA" ;;
  esac

  for f in mem-distill-worker.sh model-map.sh worktree-cleanup.sh; do
    if grep -q '_harness_root' "$REPO_ROOT/adapters/claude/bin/$f"; then
      ok "$f: _harness_root resolver present (drift guard)"
    else
      bad "$f: _harness_root resolver missing"
    fi
  done

  echo "== _legacy_probe.sh 보조 증거 (두 형태, green 모드에서도 red 재현) =="
  assert_red "(a) _legacy_probe.sh" "$TMP/homeA" 'CFG_LIFECYCLE_NUDGE' '' "$TMP/homeA/.claude/bin/_legacy_probe.sh"
  assert_red "(b) _legacy_probe.sh" "$TMP/homeB" 'CFG_LIFECYCLE_NUDGE' '' "$TMP/homeB/.claude/bin/_legacy_probe.sh"
fi

echo "---"
if [ "$fails" -eq 0 ]; then
  echo "installed-layout-home.test.sh: PASS"
else
  echo "installed-layout-home.test.sh: FAIL ($fails)"
fi
exit "$fails"
