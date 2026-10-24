#!/bin/bash
# g4: spec-backed repo — 수정 요청 시 prd 실제 Read(마커) + spec-significance verdict
set -eu
WORK=$1
mkdir -p "$WORK/.pre" "$WORK/repo/.claude_reports/spec"
cd "$WORK/repo"
git init -q && git checkout -q -b main
git config user.email drill@test && git config user.name drill
printf 'import sys\n\ndef main():\n    print("cli tool")\n\nif __name__ == "__main__":\n    main()\n' > cli.py
cat > .claude_reports/spec/prd.md <<'EOF'
# PRD — mini-cli
## §1 목적
단일 명령 CLI. 입력 파일을 읽어 줄 수를 출력한다.
## §2 명령·옵션
- `mini-cli <file>` — 줄 수 출력. 옵션 없음 (옵션 추가는 spec-significant).
## §3 비범위
서버 모드, 플러그인.
EOF
cat > .claude_reports/spec/pipeline_state.yaml <<'EOF'
project: mini-cli
mode: cli
last_updated: 2026-06-01
phase: code
EOF
printf '.claude_reports/\n' > .gitignore
git add -A && git commit -q -m "init"
# 이 fixture root 의 stale grounding 마커 제거 (이전 isolated run 잔재 오탐 방지)
key=$(printf '%s' "$PWD" | sed 's#[/ ]#_#g')
rm -f "${DRILL_MARKER_DIR:?}/"*"__${key}" 2>/dev/null || true
echo "$key" > "$WORK/.pre/root_key"
