#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v git >/dev/null 2>&1 && ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null); then
  :
else
  ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
fi
CATALOG="$ROOT/capabilities/README.md"

usage() {
  cat <<'EOF'
usage: capability-map.sh <capability>

Prints how the Codex adapter realizes a portable capability.
EOF
}

[ "${1:-}" != "-h" ] && [ "${1:-}" != "--help" ] || { usage; exit 0; }
[ "$#" -eq 1 ] || { usage >&2; exit 64; }

cap=$1

if [ ! -f "$CATALOG" ]; then
  echo "codex capability-map: missing capabilities catalog" >&2
  exit 69
fi

if ! grep -Fq "| \`$cap\` |" "$CATALOG"; then
  echo "codex capability-map: unknown capability: $cap" >&2
  exit 64
fi

if [ -f "$ROOT/capabilities/$cap.md" ]; then
  portable_source="capabilities/$cap.md"
else
  portable_source="capabilities/README.md"
fi
status="instruction-only"
realization="portable-instructions"
tool_contract=""
pipeline_contract=""
optional_pipeline_step=""
artifact_contract=""
role_contract=""
dispatch_contract=""
stage_graph_contract=""
plan_policy=""
note="Codex has no native skill/plugin realization for this capability yet; read the portable catalog and task-relevant docs, then use preflight guards. Legacy compatibility references are not native input."
native_skill_path="adapters/codex/skills/$cap/SKILL.md"
native_plugin_skill_path="adapters/codex/plugins/hearting-codex/skills/$cap/SKILL.md"
if [ -f "$ROOT/$native_skill_path" ]; then
  native_skill=1
  realization="codex-native-skill"
  note="Codex has an adapter-owned native Skill projection generated from the portable capability spec. Use it with explicit preflight guards; legacy compatibility references are not native input."
else
  native_skill=0
  native_skill_path=""
fi
if [ -f "$ROOT/$native_plugin_skill_path" ]; then
  native_plugin=1
  note="$note An optional marketplace bundle copy also exists, but it is not part of core activation or verification."
else
  native_plugin=0
  native_plugin_skill_path=""
fi

case "$cap" in
  autopilot-code)
    pipeline_contract="code-plan>code-execute>code-test>code-report"
    optional_pipeline_step="code-refine"
    artifact_contract="plans/<date>_<slug>:plan.md,checklist.md,pipeline_summary.md,dev_logs/,test_logs/"
    role_contract="planning=plan/plan-author,plan-check=qa/plan-review,implementation=dev/*,impl-review=qa/code-review,verification=qa/test,report=editorial/report"
    dispatch_contract="preflight.sh dispatch --capability autopilot-code --capability-mode <mode> [--worker-mode <family/mode>] --qa <level> --intensity <level> --dispatch-depth 1|2 [--parent <slug>]"
    stage_graph_contract="core/CONVENTIONS.md#pipeline-intensity-stage-graph-and-assurance"
    plan_policy="direct=no-plan;quick=registered-headless-dispatch-depth-1-one-shot-micro-plan+plan-check-lite;standard+=durable-plan"
    note="$note Follow the reported pipeline_contract, artifact_contract, and intensity/dispatch-depth contract before claiming the autopilot-code cycle is complete."
    ;;
  code-test)
    status="tool-contract"
    tool_contract="verification-runner"
    artifact_contract="plans/<date>_<slug>:test_logs/,_internal/test_reviews/;handoff=code-report"
    role_contract="verification=qa/test,review=qa/code-review"
    note="$note Run mode-info qa/test and the verification-runner contract before claiming code-test results."
    ;;
  autopilot-design|design-*)
    status="tool-contract"
    tool_contract="visual-harness"
    if [ "$native_plugin" -eq 1 ]; then
      note="Codex has native Skill and plugin projections for guidance and an adapter-owned visual harness contract; run the harness for concrete design outputs before claiming full support."
    elif [ "$native_skill" -eq 1 ]; then
      note="Codex has a native Skill projection for guidance and an adapter-owned visual harness contract; run the harness for concrete design outputs before claiming full support."
    else
      realization="portable-instructions"
      note="Codex has an adapter-owned visual harness contract; run the harness for concrete design outputs before claiming full support."
    fi
    ;;
esac

printf 'capability=%s\n' "$cap"
printf 'adapter=codex\n'
printf 'native_skill=%s\n' "$native_skill"
if [ -n "$native_skill_path" ]; then
  printf 'native_skill_path=%s\n' "$native_skill_path"
fi
printf 'native_plugin=%s\n' "$native_plugin"
if [ -n "$native_plugin_skill_path" ]; then
  printf 'native_plugin_skill_path=%s\n' "$native_plugin_skill_path"
fi
printf 'realization=%s\n' "$realization"
printf 'portable_source=%s\n' "$portable_source"
printf 'compat_reference=not-projected\n'

printf 'bootstrap=adapters/codex/AGENTS.md\n'
printf 'guards=adapters/codex/bin/preflight.sh\n'
printf 'status=%s\n' "$status"
if [ -n "$pipeline_contract" ]; then
  printf 'pipeline_contract=%s\n' "$pipeline_contract"
fi
if [ -n "$optional_pipeline_step" ]; then
  printf 'optional_pipeline_step=%s\n' "$optional_pipeline_step"
fi
if [ -n "$artifact_contract" ]; then
  printf 'artifact_contract=%s\n' "$artifact_contract"
fi
if [ -n "$role_contract" ]; then
  printf 'role_contract=%s\n' "$role_contract"
fi
if [ -n "$dispatch_contract" ]; then
  printf 'dispatch_contract=%s\n' "$dispatch_contract"
fi
if [ -n "$stage_graph_contract" ]; then
  printf 'stage_graph_contract=%s\n' "$stage_graph_contract"
fi
if [ -n "$plan_policy" ]; then
  printf 'plan_policy=%s\n' "$plan_policy"
fi
if [ -n "$tool_contract" ]; then
  printf 'tool_contract=%s\n' "$tool_contract"
  if [ "$tool_contract" = "visual-harness" ]; then
    printf 'runtime_surface=adapter-owned-visual-harness\n'
    printf 'tool_contract_check=adapters/codex/bin/preflight.sh visual-harness <file.html>\n'
    printf 'fallback=preflight.sh visual-harness <file.html>\n'
  elif [ "$tool_contract" = "verification-runner" ]; then
    printf 'runtime_surface=adapter-owned-verification-runner\n'
    printf 'tool_contract_check=adapters/codex/bin/preflight.sh verification-runner --check -- <command>\n'
    printf 'fallback=satisfy-tool-contract-or-report-unavailable\n'
  fi
fi
printf 'note=%s\n' "$note"
topology_summary=$(mktemp)
trap 'rm -f "$topology_summary"' EXIT
if python3 "$ROOT/tools/capability_topology.py" summary --capability "$cap" >"$topology_summary" 2>/dev/null; then
  cat "$topology_summary"
else
  # Compose-on-demand capabilities intentionally have no topology registry
  # recipe. Their portable capability modes still come from the manifest and
  # must remain visible to the Codex dispatch wrapper, just as they are to the
  # sibling adapters. Without this fallback a valid composed route reaches the
  # owner selector but fails as capability-mode-contract-missing.
  python3 - "$ROOT/harness-manifest.json" "$cap" <<'PY'
import json
import sys

manifest_path, capability = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)
modes = manifest.get("capabilities", {}).get(capability, {}).get("modes", [])
if modes:
    print("capability_modes=" + ",".join(sorted(modes)))
PY
fi
