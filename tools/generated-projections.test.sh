#!/usr/bin/env sh
# Canonical-only change, stale edit rejection, and second-run determinism.
set -eu
export PYTHONDONTWRITEBYTECODE=1

# D-42 hermeticity: the orientation case below asserts what
# `utilities/artifact-root.sh` discovers for a fixture directory, and an
# explicit AGENT_ARTIFACT_ROOT wins over discovery by design. Every registered
# worker exports one, so inheriting it made this suite fail on the live session
# instead of on the code. The fixtures supply their own roots.
unset AGENT_ARTIFACT_ROOT AGENT_ROUTE_FILE AGENT_ROUTE_ID AGENT_ROUTE_NODE

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
MANIFEST="$ROOT/harness-manifest.json"
TARGET="$ROOT/adapters/codex/skills/post-it/SKILL.md"
PLUGIN_HOOKS_JSON="$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/hooks/hooks.json"
cp "$MANIFEST" "$TMP/harness-manifest.json"

# A real pre-push hook inherits GIT_DIR.  The hook must clear that local
# environment before mode-map helpers use git -C for worktree discovery.
HOOK_GIT_DIR=$(git -C "$ROOT" rev-parse --absolute-git-dir)
(
  cd "$ROOT"
  GIT_DIR="$HOOK_GIT_DIR" "$ROOT/tools/git-hooks/pre-push" >/dev/null
)

restore() {
  cp "$TMP/harness-manifest.json" "$MANIFEST"
  python3 "$ROOT/tools/generate.py" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap restore EXIT HUP INT TERM

python3 - "$MANIFEST" <<'PY'
import json, sys
path=sys.argv[1]
data=json.load(open(path))
data["capabilities"]["post-it"]["summary"] = "GENERATOR_SENTINEL portable metadata"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

python3 "$ROOT/tools/generate.py" >/dev/null
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/claude/skills/post-it/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/post-it/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/codex/skills/post-it/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/codex/plugins/hearting-codex/skills/post-it/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/opencode/skills/post-it/SKILL.md"

python3 - "$MANIFEST" <<'PY'
import json, sys
path=sys.argv[1]
data=json.load(open(path))
data["capabilities"]["autopilot-code"]["invocation"]["use_when"] = (
    "Use when GENERATOR_ROUTE_SENTINEL source code must change."
)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

python3 "$ROOT/tools/generate.py" >/dev/null
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/manifest.json"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/tools/skill-conformance/invocation-policy.tsv"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/skills/autopilot-code/SKILL.md"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/adapters/claude/skills/autopilot-code/SKILL.md"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/autopilot-code/SKILL.md"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/adapters/codex/skills/autopilot-code/SKILL.md"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/adapters/codex/plugins/hearting-codex/skills/autopilot-code/SKILL.md"
grep -q "GENERATOR_ROUTE_SENTINEL" "$ROOT/adapters/opencode/skills/autopilot-code/SKILL.md"

cp "$TMP/harness-manifest.json" "$MANIFEST"
chmod 0755 "$PLUGIN_HOOKS_JSON"
python3 "$ROOT/tools/generate.py" >/dev/null
[ "$(stat -c '%a' "$PLUGIN_HOOKS_JSON")" = "644" ] || {
  echo "not ok - generated plugin hooks.json mode was not repaired to 0644" >&2
  exit 1
}
python3 "$ROOT/tools/generate.py" --check >/dev/null
python3 "$ROOT/tools/entry-skill-layer.test.py" >/dev/null

# Whole-layer collapse: a new utilities/* file must resolve for Claude through
# the adapters/claude/utilities dir-level symlink with zero per-file mirror
# work. Remove the probe before asserting so set -e cannot leak it.
PROBE="$ROOT/utilities/.zz_projection_probe.py"
printf '#!/usr/bin/env python3\n' > "$PROBE"
probe_resolved=false
[ -f "$ROOT/adapters/claude/utilities/.zz_projection_probe.py" ] && probe_resolved=true
rm -f "$PROBE"
[ "$probe_resolved" = true ]

find "$ROOT/capabilities" "$ROOT/adapters/claude/skills" "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude" "$ROOT/adapters/codex/skills" "$ROOT/adapters/codex/agents" "$ROOT/adapters/codex/modes" "$ROOT/adapters/codex/plugins/hearting-codex" "$ROOT/adapters/opencode/skills" "$ROOT/adapters/opencode/commands" "$ROOT/adapters/opencode/agents" -type f -print | sort | xargs sha256sum > "$TMP/first.sha"
sha256sum "$ROOT/tools/skill-conformance/invocation-policy.tsv" >> "$TMP/first.sha"
python3 "$ROOT/tools/generate.py" >/dev/null
find "$ROOT/capabilities" "$ROOT/adapters/claude/skills" "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude" "$ROOT/adapters/codex/skills" "$ROOT/adapters/codex/agents" "$ROOT/adapters/codex/modes" "$ROOT/adapters/codex/plugins/hearting-codex" "$ROOT/adapters/opencode/skills" "$ROOT/adapters/opencode/commands" "$ROOT/adapters/opencode/agents" -type f -print | sort | xargs sha256sum > "$TMP/second.sha"
sha256sum "$ROOT/tools/skill-conformance/invocation-policy.tsv" >> "$TMP/second.sha"
cmp "$TMP/first.sha" "$TMP/second.sha"

cp "$TARGET" "$TMP/generated-skill"
printf '\nmanual generated edit\n' >> "$TARGET"
if python3 "$ROOT/tools/generate.py" --check >/dev/null 2>&1; then
  echo "not ok - manual generated edit was accepted" >&2
  exit 1
fi
cp "$TMP/generated-skill" "$TARGET"
python3 "$ROOT/tools/generate.py" --check >/dev/null

ORIENTATION_FIXTURE="$TMP/orientation-fixture"
mkdir -p \
  "$ORIENTATION_FIXTURE/.claude_reports/analysis_project/code" \
  "$ORIENTATION_FIXTURE/.claude_reports/experiments/example"
printf '%s\n' 'existing analysis' > "$ORIENTATION_FIXTURE/.claude_reports/analysis_project/code/summary.md"
printf '%s\n' 'run log' > "$ORIENTATION_FIXTURE/.claude_reports/experiments/_RUNLOG.md"
printf '%s\n' 'story' > "$ORIENTATION_FIXTURE/.claude_reports/experiments/example/STORY.md"

resolved=$("$ROOT/utilities/artifact-root.sh" "$ORIENTATION_FIXTURE")
[ "$resolved" = "$ORIENTATION_FIXTURE/.claude_reports" ] || {
  echo "not ok - legacy artifact root was not selected for orientation" >&2
  exit 1
}
mkdir -p "$ORIENTATION_FIXTURE/.agent_reports"
resolved=$("$ROOT/utilities/artifact-root.sh" "$ORIENTATION_FIXTURE")
[ "$resolved" = "$ORIENTATION_FIXTURE/.agent_reports" ] || {
  echo "not ok - canonical artifact root did not take precedence" >&2
  exit 1
}

grep -Fq '### 0.1. Read-Only Orientation Before Capability Routing' "$ROOT/core/WORKFLOW.md"
grep -Fq 'Read-only orientation invokes no capability and writes no artifact' "$ROOT/core/WORKFLOW.md"
recall_line=$(grep -n -m1 'current main-session prompt received its bounded capsule candidate probe' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
artifact_line=$(grep -n -m1 'Use the adapter status surface' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
[ "$recall_line" -lt "$artifact_line" ] || {
  echo "not ok - targeted recall must precede artifact discovery" >&2
  exit 1
}
grep -Fq 'latest specification or user-confirmed decision' "$ROOT/core/WORKFLOW.md"
grep -Fq 'durable project fact' "$ROOT/core/WORKFLOW.md"
grep -Fq 'latest experiment contract' "$ROOT/core/WORKFLOW.md"
grep -Fq 'A shortened or ellipsized snippet is never final evidence' "$ROOT/core/MEMORY.md"
grep -Fq 'read the full body by record ID' "$ROOT/core/WORKFLOW.md"
grep -Fq '## Routing Boundary' "$ROOT/capabilities/analyze-project.md"
grep -Fq 'orientation and is not an `analyze-project` trigger by itself' "$ROOT/capabilities/analyze-project.md"
grep -Fq 'Pointer follow-through' "$ROOT/core/MEMORY.md"
grep -Fq '### 0.4. Primary Entry Confirmation' "$ROOT/core/WORKFLOW.md"
grep -Fq '[실행 확인]' "$ROOT/core/WORKFLOW.md"
grep -Fq '작업: <무엇을 어떤 결과로 만들지>' "$ROOT/core/WORKFLOW.md"
grep -Fq '→ 진행 / 수정: <틀린 부분> / 중단' "$ROOT/core/WORKFLOW.md"
grep -Fq '### 0.5. Post-Execution Completion Report' "$ROOT/core/WORKFLOW.md"
grep -Fq '[완료 보고]' "$ROOT/core/WORKFLOW.md"
completion_task_line=$(grep -n -m1 '작업: <수행한 작업>' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
completion_result_line=$(grep -n -m1 '결과: <완료 | 부분 완료 | 실패 | 차단 — 실제 결과>' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
completion_verify_line=$(grep -n -m1 '검증: <실행한 검증과 결과>' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
completion_artifact_line=$(grep -n -m1 '산출물: <사용자가 확인할 경로 또는 없음>' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
completion_remaining_line=$(grep -n -m1 '남은 사항: <미완료 항목, 위험, 차단 요인 또는 없음>' "$ROOT/core/WORKFLOW.md" | cut -d: -f1)
[ "$completion_task_line" -lt "$completion_result_line" ] \
  && [ "$completion_result_line" -lt "$completion_verify_line" ] \
  && [ "$completion_verify_line" -lt "$completion_artifact_line" ] \
  && [ "$completion_artifact_line" -lt "$completion_remaining_line" ] || {
  echo "not ok - completion report fields must keep canonical order" >&2
  exit 1
}
grep -Fq 'A worker handoff, background-process exit, or stage verdict alone is not task' "$ROOT/core/WORKFLOW.md"
grep -Fq 'material work with the canonical five-field post-execution card' "$ROOT/roles/response-policy.md"
grep -Fq 'auto-proceed without another confirmation, then use the applicable concise' "$ROOT/roles/response-policy.md"
if grep -Fq 'auto-proceed and report in one line' "$ROOT/roles/response-policy.md"; then
  echo "not ok - one-line follow-up close contradicts the completion report card" >&2
  exit 1
fi
for bootstrap in \
  "$ROOT/adapters/claude/CLAUDE.md" \
  "$ROOT/adapters/codex/AGENTS.md" \
  "$ROOT/adapters/opencode/AGENTS.md"; do
  grep -Fq 'five-field card in §0.4' "$bootstrap" || {
    echo "not ok - entry confirmation pointer missing from $bootstrap" >&2
    exit 1
  }
  # Bootstrap prose is hard-wrapped, so match against a whitespace-normalized
  # copy: a restored clause must survive an unrelated reflow.
  bootstrap_flat=$(tr '\n' ' ' < "$bootstrap" | tr -s ' ' | tr 'A-Z' 'a-z')
  while IFS='|' read -r needle label; do
    [ -n "$needle" ] || continue
    case "$bootstrap_flat" in
      *"$needle"*) ;;
      *)
        echo "not ok - $label missing from $bootstrap" >&2
        exit 1
        ;;
    esac
  done <<'BOOTSTRAP_PARITY_EOF'
five-field completion card in §0.5|WORKFLOW §0.5 completion report pointer
§5.11|OPERATIONS §5.11 same-turn commit/push policy
dispatch depth 3 is forbidden|dispatch-depth-3 prohibition
genuinely non-obvious|genuinely-non-obvious ask clause
continue low-risk reversible work autonomously|structured-input autonomy threshold
if structured input is unavailable|structured-input fallback clause
legacy `.claude_reports/` is only a fallback|legacy artifact-root fallback qualifier
depth, tests, safety, and validation on fallback|fallback preservation guarantee
before committing|expose-change-before-committing clause
BOOTSTRAP_PARITY_EOF
done
if grep -Fq '## Projected Portable Details' "$ROOT/adapters/codex/skills/autopilot-code/SKILL.md"; then
  echo "not ok - entry-router Codex Skill preloads projected details" >&2
  exit 1
fi
grep -Fq '## Projected Portable Details' "$ROOT/adapters/codex/skills/code-plan/SKILL.md"
if grep -Fq '## Portable Contract' "$ROOT/adapters/opencode/skills/autopilot-code/SKILL.md"; then
  echo "not ok - entry-router OpenCode Skill preloads portable details" >&2
  exit 1
fi
grep -Fq '## Portable Contract' "$ROOT/adapters/opencode/skills/code-plan/SKILL.md"

for projected in \
  "$ROOT/skills/analyze-project/SKILL.md" \
  "$ROOT/adapters/claude/skills/analyze-project/SKILL.md" \
  "$ROOT/adapters/codex/skills/analyze-project/SKILL.md" \
  "$ROOT/adapters/opencode/skills/analyze-project/SKILL.md"; do
  grep -Fq 'user asks to analyze existing code' "$projected" || {
    echo "not ok - initial-analysis positive trigger missing from $projected" >&2
    exit 1
  }
  grep -Fq 'Not for conversational, read-only, or no-file analysis' "$projected" || {
    echo "not ok - orientation exclusion missing from $projected" >&2
    exit 1
  }
done
grep -Fq 'agent-chosen memory recall' "$ROOT/capabilities/analyze-project.md"
for owner in \
  "$ROOT/skills/analyze-project/references/owner-execution.md" \
  "$ROOT/adapters/claude/skills/analyze-project/references/owner-execution.md" \
  "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/analyze-project/references/owner-execution.md"; do
  grep -Fq 'agent-chosen memory recall' "$owner" || {
    echo "not ok - post-approval owner contract missing from $owner" >&2
    exit 1
  }
done
grep -Fq 'five-field confirmation card' "$ROOT/adapters/codex/skills/analyze-project/SKILL.md"
grep -Fq 'five-field confirmation card' "$ROOT/adapters/opencode/skills/analyze-project/SKILL.md"

for figure_contract in \
  "$ROOT/roles/units/material/figure-gen.md" \
  "$ROOT/adapters/codex/modes/material/figure-gen.md"; do
  grep -Fq 'METRIC_BAND_HZ' "$figure_contract" || {
    echo "not ok - metric/display separation missing from $figure_contract" >&2
    exit 1
  }
  grep -Fq '24000' "$figure_contract" || {
    echo "not ok - full-band maximum missing from $figure_contract" >&2
    exit 1
  }
done

grep -Fq -- '--verify-report' "$ROOT/adapters/codex/tools/material/figure-gen.sh"
grep -Fq -- '--verify-report' "$ROOT/adapters/opencode/tools/material/figure-gen.sh"
grep -Fq -- '--verify-report semantic gate' "$ROOT/adapters/codex/bin/mode-map.sh"
grep -Fq -- '--verify-report semantic gate' "$ROOT/adapters/opencode/bin/mode-map.sh"
codex_mode=$($ROOT/adapters/codex/bin/preflight.sh mode-info material/figure-gen)
opencode_mode=$($ROOT/adapters/opencode/bin/preflight.sh mode-info material/figure-gen)
printf '%s\n' "$codex_mode" | grep -Fq 'report_tool_contract_check=adapters/codex/bin/preflight.sh figure-gen --verify-report <manifest.json> <report.md>'
printf '%s\n' "$opencode_mode" | grep -Fq 'report_tool_contract_check=adapters/opencode/bin/preflight.sh figure-gen --verify-report <manifest.json> <report.md>'
test -x "$ROOT/tools/figure-semantic-verify.py"
test -f "$ROOT/tools/figure-semantic-manifest.schema.json"
test -L "$ROOT/adapters/claude/tools/figure-semantic-verify.py"
grep -Fq '### Step 4c: Report figure semantic gate' "$ROOT/skills/autopilot-draft/references/pipeline-steps.md"
grep -Fq 'PNG existence, dimensions, count, and links alone are not a pass' "$ROOT/skills/code-test/SKILL.md"
cmp -s "$ROOT/adapters/codex/skills/analyze-project/SKILL.md" \
  "$ROOT/adapters/codex/plugins/hearting-codex/skills/analyze-project/SKILL.md"
cmp -s "$ROOT/adapters/claude/skills/code-test/SKILL.md" \
  "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/code-test/SKILL.md"
cmp -s "$ROOT/utilities/artifact-root.sh" \
  "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/utilities/artifact-root.sh"
python3 "$ROOT/tools/figure-semantic-verify.test.py" >/dev/null

WRAPPER_FIXTURE="$TMP/figure-wrapper"
mkdir -p "$WRAPPER_FIXTURE"
python3 - "$ROOT" "$WRAPPER_FIXTURE" <<'PY'
import importlib.util, json, sys
from pathlib import Path

root, target = map(Path, sys.argv[1:])
source = root / "tools/figure-semantic-verify.test.py"
spec = importlib.util.spec_from_file_location("figure_fixture", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
png = target / "spectrogram.png"
module.write_png(png)
report = target / "report.md"
report.write_text(
    "# Result\n\n![Spectrogram](spectrogram.png)\n\n이 그림은 **전 대역**\n에너지를 보여준다.\n",
    encoding="utf-8",
)
valid = module.valid_manifest(png)
(target / "valid.json").write_text(json.dumps(valid, ensure_ascii=False), encoding="utf-8")
invalid = json.loads(json.dumps(valid))
invalid["figure_groups"][0]["max_hz"] = 1000
(target / "bad-max.json").write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
PY

for adapter in codex opencode; do
  "$ROOT/adapters/$adapter/bin/preflight.sh" figure-gen --verify-report \
    "$WRAPPER_FIXTURE/valid.json" "$WRAPPER_FIXTURE/report.md" >/dev/null
  if "$ROOT/adapters/$adapter/bin/preflight.sh" figure-gen --verify-report \
      "$WRAPPER_FIXTURE/bad-max.json" "$WRAPPER_FIXTURE/report.md" >/dev/null 2>&1; then
    echo "not ok - $adapter report wrapper accepted max_hz=1000" >&2
    exit 1
  else
    rc=$?
    [ "$rc" -eq 2 ] || {
      echo "not ok - $adapter report wrapper returned $rc instead of 2" >&2
      exit 1
    }
  fi
done

echo "generated-projections: PASS"
