#!/usr/bin/env sh
# routing-contract.test.sh — semantic-primary routing / main-session role /
# delegation-surface 계약의 deterministic cross-doc 검사.
# 2026-07-14 사고(재평가+보고서 업데이트가 autopilot-refine primary 로 오라우팅,
# native sub-agent 제한의 headless 확대 해석) 이후 추가. 모델 호출 없이 텍스트
# invariant 만 검사한다. 행동 회귀는 loops/drill/cases_growing/r_route_* ·
# g_subagent_scope_headless · g_eval_stage_dispatch_or_reason 드릴이 담당한다.
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 2
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }

need() { # need <file> <pattern> <label>
  if grep -q "$2" "$1" 2>/dev/null; then ok "$3"; else bad "$3 ($1: '$2' 부재)"; fi
}

# 1. portable core
need core/WORKFLOW.md   '### 0.2. Semantic Primary Routing'                 'WORKFLOW §0.2 semantic primary routing 존재'
need core/WORKFLOW.md   '### 0.3. Pre-Execution Gate for Long-Running Work' 'WORKFLOW §0.3 pre-execution gate 존재'
need core/WORKFLOW.md   'never absorbs a secondary'                         'WORKFLOW §0.2 상호 대체 금지 절'
need core/WORKFLOW.md   'uses that entry as the primary route'              'matching entry-router primary preference 존재'
need core/WORKFLOW.md   'intensity inside the selected entry route'         'direct entry 우회 금지 절'
need core/OPERATIONS.md 'Main-session role contract'                        'OPERATIONS §5.10 main-session role contract 존재'
need core/OPERATIONS.md 'Inline exceptions'                                 'OPERATIONS §5.10 inline exceptions 존재'
need core/OPERATIONS.md 'Delegation surfaces are distinct'                  'OPERATIONS §5.10 delegation surfaces 존재'
need core/CONVENTIONS.md 'WORKFLOW §0.2'                                    'CONVENTIONS §3 invariant 11 semantic routing 참조'

# 2. capability contracts
need capabilities/autopilot-lab.md    'Eval execution topology'  'autopilot-lab eval topology 존재'
need capabilities/autopilot-lab.md    '## Routing Boundary'      'autopilot-lab Routing Boundary 존재'
need capabilities/autopilot-lab.md    'report/logs/'              'autopilot-lab original-log bundle contract 존재'
need capabilities/autopilot-lab.md    "script-src 'none'"        'autopilot-lab scriptless consumer contract 존재'
need capabilities/autopilot-refine.md '## Routing Boundary'      'autopilot-refine Routing Boundary 존재'
need capabilities/autopilot-refine.md 'autopilot-lab'            'autopilot-refine → lab primary 위임 절'
need capabilities/autopilot-spec.md   'never substitutes'        'autopilot-spec spec-sync 비대체 절'
need core/WORKFLOW.md                 '`artifact-sink` extension, always secondary' 'app-neutral artifact-sink extension 존재'
need capabilities/analyze-project.md  'explicit analysis request defaults to persistent output' 'analyze-project 초기 분석 기본값 존재'
need capabilities/analyze-project.md  'Artifact absence alone is not a trigger' '산출물 부재 단독 트리거 금지'

# 3. generated Codex projection 이 topology 를 실어 나르는가 (파리티 갭 회귀)
need adapters/codex/skills/autopilot-lab/SKILL.md 'capabilities/autopilot-lab.md' 'Codex lab projection 에 owner pointer 존재'
need adapters/opencode/skills/autopilot-lab/SKILL.md 'capabilities/autopilot-lab.md' 'OpenCode lab projection 에 owner pointer 존재'
need adapters/codex/skills/autopilot-refine/SKILL.md 'capabilities/autopilot-refine.md' 'Codex refine projection 에 owner pointer 존재'

# 4. adapter bootstraps
need adapters/claude/CLAUDE.md  'core/WORKFLOW.md §0.2'            'Claude bootstrap semantic routing 실현'
need adapters/claude/CLAUDE.md  'headless worker dispatch'         'Claude bootstrap delegation-surface 실현'
need adapters/codex/AGENTS.md   'core/WORKFLOW.md §0.2'            'Codex bootstrap semantic routing 실현'
need adapters/codex/AGENTS.md   'never silently extends'           'Codex bootstrap delegation-surface 실현'
need adapters/opencode/AGENTS.md 'core/WORKFLOW.md §0.2'           'OpenCode bootstrap semantic routing 실현'

# 5. Claude skill realization
need skills/autopilot-lab/SKILL.md 'core/WORKFLOW.md §0.2'         'lab SKILL semantic routing 참조'
need skills/autopilot-lab/references/eval-procedure.md 'pre-execution gate' 'lab eval-procedure gate 참조'
need adapters/claude/skills/autopilot-lab/references/eval-procedure.md 'WAV, MP3, or OGG' 'Claude lab audio parity projection 존재'
need adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/autopilot-lab/references/eval-procedure.md 'WAV, MP3, or OGG' 'Claude plugin lab audio parity projection 존재'

# 6. 행동 드릴 fixture 존재 (Cases A–E)
for c in r_route_lab_eval_primary r_route_refine_doc_only r_route_spec_policy_lab_exec \
         r_route_analyze_project_initial g_subagent_scope_headless \
         g_eval_stage_dispatch_or_reason; do
  d="loops/drill/cases_growing/$c"
  if [ -f "$d/prompt.md" ] && [ -f "$d/fixture.sh" ] && [ -f "$d/assert.sh" ] && [ -f "$d/config" ]; then
    ok "drill fixture $c 완비"
  else
    bad "drill fixture $c 불완전"
  fi
  sh -n "$d/assert.sh" 2>/dev/null && ok "drill $c assert sh-clean" || bad "drill $c assert 문법 오류"
  sh -n "$d/fixture.sh" 2>/dev/null && ok "drill $c fixture sh-clean" || bad "drill $c fixture 문법 오류"
done

if [ "$fails" -gt 0 ]; then
  printf 'routing-contract: %d failure(s)\n' "$fails"
  exit 1
fi
printf 'routing-contract: all checks passed\n'
exit 0
