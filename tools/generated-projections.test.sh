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
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
# This suite edits harness-manifest.json and regenerates every projection; do
# it in a private copy so peer suites never read the live checkout mid-edit
# (tools/checkout-copy.sh).
. "$ROOT/tools/checkout-copy.sh"
source_git() { git -c "safe.directory=$ROOT" -C "$ROOT" "$@"; }
SOURCE_GIT_DIR=$(source_git rev-parse --absolute-git-dir)
# git < 2.31 has no --path-format=absolute: ask plainly, absolutize against $ROOT.
source_abs() {
  case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$ROOT/$1" ;; esac
}
SOURCE_COMMON_DIR=$(source_abs "$(source_git rev-parse --git-common-dir)")
SOURCE_INDEX=$(source_abs "$(source_git rev-parse --git-path index)")
SOURCE_HEAD=$(source_git rev-parse HEAD)
SOURCE_REFS=$(source_git show-ref --head | LC_ALL=C sort)
SOURCE_INDEX_DIGEST=$(sha256sum "$SOURCE_INDEX" | cut -d' ' -f1)
mkdir -p "$TMP/decoy-objects"
(
  GIT_DIR="$SOURCE_GIT_DIR" \
  GIT_COMMON_DIR="$SOURCE_COMMON_DIR" \
  GIT_WORK_TREE="$ROOT" \
  GIT_INDEX_FILE="$SOURCE_INDEX" \
  GIT_OBJECT_DIRECTORY="$SOURCE_COMMON_DIR/objects" \
  GIT_ALTERNATE_OBJECT_DIRECTORIES="$TMP/decoy-objects" \
  GIT_NAMESPACE=checkout-copy-fixture \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.bare GIT_CONFIG_VALUE_0=true \
  checkout_copy "$ROOT" "$TMP/repo"
) || { rm -rf "$TMP"; echo "not ok - isolated checkout copy" >&2; exit 1; }
[ "$(source_git rev-parse HEAD)" = "$SOURCE_HEAD" ]
[ "$(source_git show-ref --head | LC_ALL=C sort)" = "$SOURCE_REFS" ]
[ "$(sha256sum "$SOURCE_INDEX" | cut -d' ' -f1)" = "$SOURCE_INDEX_DIGEST" ]
ROOT="$TMP/repo"
MANIFEST="$ROOT/harness-manifest.json"
TARGET="$ROOT/adapters/codex/skills/session-tidy/SKILL.md"
PLUGIN_HOOKS_JSON="$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/hooks/hooks.json"
cp "$MANIFEST" "$TMP/harness-manifest.json"

# A real pre-push hook inherits GIT_DIR.  The hook must clear that local
# environment before mode-map helpers use git -C for worktree discovery.
HOOK_GIT_DIR=$(git -C "$ROOT" rev-parse --absolute-git-dir)
(
  cd "$ROOT"
  GIT_DIR="$HOOK_GIT_DIR" "$ROOT/tools/git-hooks/pre-push" >/dev/null
)

python3 - "$MANIFEST" <<'PY'
import json, sys
path=sys.argv[1]
data=json.load(open(path))
data["capabilities"]["session-tidy"]["summary"] = "GENERATOR_SENTINEL portable metadata"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

python3 "$ROOT/tools/generate.py" >/dev/null
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/claude/skills/session-tidy/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/session-tidy/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/codex/skills/session-tidy/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/codex/plugins/hearting-codex/skills/session-tidy/SKILL.md"
grep -q "GENERATOR_SENTINEL" "$ROOT/adapters/opencode/skills/session-tidy/SKILL.md"
if grep -q '^name:' "$ROOT/adapters/claude/skills/session-tidy/SKILL.md" \
  || grep -q '^name:' "$ROOT/skills/session-tidy/SKILL.md"; then
  echo "not ok - Claude skill names must use their folder while preserving shared identifiers" >&2
  exit 1
fi
grep -q '^name: session-tidy$' "$ROOT/adapters/codex/skills/session-tidy/SKILL.md"
grep -q '^name: session-tidy$' "$ROOT/adapters/opencode/skills/session-tidy/SKILL.md"

# The shared recovery contract and the renamed canonical procedure must both
# reach each native Skill projection.
RECOVERY_CONTRACT="$ROOT/capabilities/code-execute.md"
RECOVERY_PROCEDURE="$ROOT/capabilities/code-execute/references/procedure.md"
cp "$RECOVERY_CONTRACT" "$TMP/code-execute-contract.md"
python3 - "$RECOVERY_CONTRACT" "$RECOVERY_PROCEDURE" <<'PY'
from pathlib import Path
import re
import sys

contract = Path(sys.argv[1])
procedure = Path(sys.argv[2])
procedure_text = procedure.read_text(encoding="utf-8")
required = (
    "## Failure Recovery",
    "clearly attributable edits in reverse order",
    "same file",
    "staged content",
    "untracked files",
    "never replay a failed command automatically",
    "route closure to the runtime",
    "direct/inline and legacy recovery paths",
)
missing = [item for item in required if item not in procedure_text]
if missing:
    raise SystemExit(f"not ok - renamed canonical recovery procedure is incomplete: {missing}")

text = contract.read_text(encoding="utf-8")
heading = "## Portable Procedure\n"
if heading not in text:
    raise SystemExit("not ok - code-execute Portable Procedure is missing")
sentinel = "\nProjection fixture: PORTABLE_RECOVERY_PROCEDURE_SENTINEL.\n"
start = text.index(heading) + len(heading)
next_heading = re.search(r"^## ", text[start:], re.MULTILINE)
end = start + next_heading.start() if next_heading else len(text)
contract.write_text(text[:end].rstrip() + sentinel + text[end:], encoding="utf-8")
PY
python3 "$ROOT/tools/generate.py" >/dev/null
python3 - \
  "$RECOVERY_CONTRACT" \
  "$ROOT/adapters/codex/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/codex/plugins/hearting-codex/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/opencode/skills/code-execute/SKILL.md" <<'PY'
from pathlib import Path
import re
import sys

def portable_section(path):
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r"(?ms)^## Portable Procedure\n(.*?)(?=^## |\Z)", text)
    if not match:
        raise SystemExit(f"not ok - Portable Procedure missing from {path}")
    return match.group(1).strip()

expected = portable_section(sys.argv[1])
if "PORTABLE_RECOVERY_PROCEDURE_SENTINEL" not in expected:
    raise SystemExit("not ok - recovery projection fixture was not inserted")
for path in sys.argv[2:]:
    if portable_section(path) != expected:
        raise SystemExit(f"not ok - Portable Procedure differs in {path}")
PY
grep -Fq 'capabilities/code-execute.md#portable-procedure' "$RECOVERY_PROCEDURE" || {
  echo "not ok - renamed canonical procedure omits the shared recovery contract" >&2
  exit 1
}
for skill_copy in \
  "$ROOT/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/claude/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/codex/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/codex/plugins/hearting-codex/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/opencode/skills/code-execute/SKILL.md"; do
  grep -Fq 'capabilities/code-execute/references/procedure.md' "$skill_copy" || {
    echo "not ok - renamed canonical procedure reference missing from $skill_copy" >&2
    exit 1
  }
done
cmp -s "$ROOT/skills/code-execute/SKILL.md" "$ROOT/adapters/claude/skills/code-execute/SKILL.md"
cmp -s "$ROOT/skills/code-execute/SKILL.md" \
  "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/code-execute/SKILL.md"
cp "$TMP/code-execute-contract.md" "$RECOVERY_CONTRACT"
python3 "$ROOT/tools/generate.py" >/dev/null

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
has_first_close_content() {
  case "$1" in
    *'first close of material work'*'what changed and why'*'before/after'*'checks actually run'*'commit/pr/deployment/serving state'*'limits or work that remains'*'preserve explicit user-requested content and output formats'*) return 0 ;;
    *) return 1 ;;
  esac
}
workflow_flat=$(tr '\n' ' ' < "$ROOT/core/WORKFLOW.md" | tr -s ' ' | tr 'A-Z' 'a-z')
has_first_close_content "$workflow_flat" || {
  echo "not ok - shared first-close content contract is incomplete" >&2
  exit 1
}
case "$workflow_flat" in
  *'must not present partial, failed, or blocked work as completed'*) ;;
  *) echo "not ok - honest completion status contract regressed" >&2; exit 1 ;;
esac
case "$workflow_flat" in
  *'neither the card nor its field order is required'*) ;;
  *) echo "not ok - completion card is still mandatory" >&2; exit 1 ;;
esac
case "$workflow_flat" in
  *'fully populated five-field report card'*|*'keep the five fields and their order'*)
    echo "not ok - shared first report still mandates the card or its order" >&2
    exit 1
    ;;
esac
without_requested_output=$(printf '%s' "$workflow_flat" | \
  sed 's/preserve explicit user-requested content and output formats/remove/')
if has_first_close_content "$without_requested_output"; then
  echo "not ok - first-close check accepts a missing explicit-output priority" >&2
  exit 1
fi
grep -Fq 'A process exit or intermediate stage verdict alone is not task completion.' "$ROOT/core/WORKFLOW.md" || {
  echo "not ok - process/stage completion boundary missing from core/WORKFLOW.md" >&2
  exit 1
}
response_policy_flat=$(tr '\n' ' ' < "$ROOT/roles/response-policy.md" | tr -s ' ' | tr 'A-Z' 'a-z')
case "$response_policy_flat" in
  *'first close of material work follows the content contract in `core/workflow.md §0.5`'*'card is an optional presentation'*'required facts and explicit user-requested output'*) ;;
  *) echo "not ok - response policy must preserve first-close content without requiring a card" >&2; exit 1 ;;
esac
case "$response_policy_flat" in
  *'continuation keeps the original accepted user task'*'every requested output constraint intact'*'receipt is supplemental context'*'not a replacement or a reason to narrow the task'*'do not assume the original user message was physically deleted'*) ;;
  *) echo "not ok - shared continuation policy can narrow or replace the original user task" >&2; exit 1 ;;
esac
grep -Fq 'auto-proceed without another confirmation, then use the applicable concise' "$ROOT/roles/response-policy.md"
if grep -Fq 'auto-proceed and report in one line' "$ROOT/roles/response-policy.md"; then
  echo "not ok - one-line follow-up close contradicts the completion report card" >&2
  exit 1
fi
followup_flat=$(tr '\n' ' ' < "$ROOT/utilities/dispatch_completion_join.py" | \
  sed -E 's/"[[:space:]]*"/ /g' | tr -s ' ' | tr 'A-Z' 'a-z')
has_original_task_followthrough() {
  case "$1" in
    *'continue and complete the original user task itself'*'preserving every explicit content and output-format requirement'*'this receipt supplements the task'*'does not replace it with a receipt-only response'*) return 0 ;;
    *) return 1 ;;
  esac
}
has_original_task_followthrough "$followup_flat" || {
  echo "not ok - shared continuation text omits the original task or its requested output format" >&2
  exit 1
}
if has_original_task_followthrough 'Receipt received. Queue consumption and ACK were observed.'; then
  echo "not ok - generic receipt-only text satisfies original-task follow-through" >&2
  exit 1
fi
grep -Fq 'completion_followup_text(' "$ROOT/utilities/claude-session-supervisor.py"
grep -Fq 'completion_followup_text(' "$ROOT/utilities/codex-managed-completion.py"
grep -Fq 'completion_followup_text(' "$ROOT/utilities/dispatch_session_sweep.py"
grep -Fq 'dispatch_session_sweep.py' "$ROOT/adapters/opencode/plugins/hearting-guards.js"
has_entry_confirmation() {
  case "$1" in
    *'present the five-field card in §0.4 before'*|*'present the §0.4 five-field card before'*|*'else the §0.4 card unless approved'*|*'for unapproved material work, use the §0.4 card'*) return 0 ;;
    *) return 1 ;;
  esac
}
has_first_report_reference() {
  case "$1" in
    *'first-report content required by §0.5; its card is optional'*) return 0 ;;
    *) return 1 ;;
  esac
}
for bootstrap in \
  "$ROOT/adapters/claude/CLAUDE.md" \
  "$ROOT/adapters/codex/AGENTS.md" \
  "$ROOT/adapters/opencode/AGENTS.md"; do
  # Bootstrap prose is hard-wrapped, so match against a whitespace-normalized
  # copy: a restored clause must survive an unrelated reflow.
  bootstrap_flat=$(tr '\n' ' ' < "$bootstrap" | tr -s ' ' | tr 'A-Z' 'a-z')
  grep -Fq 'Portable behavior contract = `roles/response-policy.md`' "$bootstrap" || {
    echo "not ok - shared response policy is detached from $bootstrap" >&2
    exit 1
  }
  has_entry_confirmation "$bootstrap_flat" || {
    echo "not ok - entry confirmation pointer missing from $bootstrap" >&2
    exit 1
  }
  # Removing the entry action must fail even with the evidence-check exemption
  # elsewhere in each real bootstrap. Preserve the rest of the text verbatim.
  without_entry=$(printf '%s' "$bootstrap_flat" | sed -E \
    's/present the (five-field card in §0\.4|§0\.4 five-field card) before//g; s/else the §0\.4 card unless approved//g; s/for unapproved material work, use the §0\.4 card//g')
  if has_entry_confirmation "$without_entry"; then
    echo "not ok - entry confirmation check accepts a missing action in $bootstrap" >&2
    exit 1
  fi
  has_first_report_reference "$bootstrap_flat" || {
    echo "not ok - first-report content pointer missing from $bootstrap" >&2
    exit 1
  }
  case "$bootstrap_flat" in
    *'close with the §0.5 card'*|*'five-field completion card in §0.5'*|*'close with §0.5'*)
      echo "not ok - adapter bootstrap still requires a completion card" >&2
      exit 1
      ;;
  esac
  without_first_report=$(printf '%s' "$bootstrap_flat" | sed \
    's/first-report content required by §0\.5; its card is optional//g')
  if has_first_report_reference "$without_first_report"; then
    echo "not ok - adapter check accepts a missing first-report reference in $bootstrap" >&2
    exit 1
  fi
  case "$bootstrap_flat" in
    *'continue low-risk reversible work autonomously'*|*'continue reversible in-flow work'*) ;;
    *)
      echo "not ok - structured-input autonomy threshold missing from $bootstrap" >&2
      exit 1
      ;;
  esac
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
§5.11|OPERATIONS §5.11 same-turn commit/push policy
dispatch depth 3 is forbidden|dispatch-depth-3 prohibition
genuinely non-obvious|genuinely-non-obvious ask clause
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
  "$ROOT/capabilities/analyze-project/references/owner-execution.md" \
  "$ROOT/adapters/claude/plugin-marketplace/plugins/hearting-claude/capabilities/analyze-project/references/owner-execution.md"; do
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
grep -Fq '### Step 4c: Report figure semantic gate' "$ROOT/capabilities/autopilot-draft/references/pipeline-steps.md"
grep -Fq 'PNG existence, dimensions, count, and links alone are not a pass' "$ROOT/capabilities/code-test/references/procedure.md"
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
