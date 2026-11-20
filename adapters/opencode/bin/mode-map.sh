#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
usage: mode-map.sh <family>/<mode>

Prints how the OpenCode adapter may consume an agent mode fragment.
EOF
}

[ "${1:-}" != "-h" ] && [ "${1:-}" != "--help" ] || { usage; exit 0; }
[ "$#" -eq 1 ] || { usage >&2; exit 64; }

mode=$1
case "$mode" in
  */*) ;;
  *) echo "opencode mode-map: expected <family>/<mode>: $mode" >&2; exit 64 ;;
esac

family=${mode%%/*}
name=${mode#*/}
source="roles/units/$family/$name.md"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v git >/dev/null 2>&1 && ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null); then
  :
else
  ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
fi
if [ ! -f "$ROOT/$source" ]; then
  echo "opencode mode-map: unknown mode: $mode" >&2
  exit 64
fi

status=unsupported
realization=compat-reference
requirement=""
tool_contract=""
tool_contract_check=""
report_tool_contract_check=""
runtime_surface=""
fallback=""
note=""

case "$family" in
  dev|editorial|ops|plan)
    status=portable
    realization=portable-persona
    requirement="opencode edit/read tools plus normal preflight guards"
    ;;
  general)
    status=portable
    realization=portable-persona
    requirement="read-only lookup or investigation with OpenCode file, search and web tools; write nothing"
    ;;
  material)
    status=tool-contract
    realization=portable-with-tool-contract
    fallback="satisfy-tool-contract-or-report-unavailable"
    case "$name" in
      browser-fetch)
        tool_contract=browser-fetch
        tool_contract_check="adapters/opencode/bin/preflight.sh browser-fetch --check <url>"
        runtime_surface=adapter-owned-browser-fetch
        ;;
      data-script)
        tool_contract=data-script
        tool_contract_check="adapters/opencode/bin/preflight.sh data-script --check <script.py>"
        runtime_surface=adapter-owned-data-script
        ;;
      figure-gen)
        tool_contract=figure-gen
        tool_contract_check="adapters/opencode/bin/preflight.sh figure-gen --check <script.py>"
        report_tool_contract_check="adapters/opencode/bin/preflight.sh figure-gen --verify-report <manifest.json> <report.md>"
        runtime_surface=adapter-owned-figure-gen
        ;;
      pdf-extract)
        tool_contract=pdf-extract
        tool_contract_check="adapters/opencode/bin/preflight.sh pdf-extract --check <file.pdf>"
        runtime_surface=adapter-owned-pdf-extract
        ;;
      web-image-search)
        tool_contract=web-image-search
        tool_contract_check="adapters/opencode/bin/preflight.sh web-image-search --check <query>"
        runtime_surface=adapter-owned-web-image-search
        ;;
      *) tool_contract=material-tooling ;;
    esac
    if [ "$name" = "browser-fetch" ]; then
      requirement="run the adapter-owned Playwright browser-fetch launcher for rendered web inputs, or report unavailable"
    elif [ "$name" = "data-script" ]; then
      requirement="run the adapter-owned Python data-script launcher for generated analysis scripts, or report unavailable"
    elif [ "$name" = "figure-gen" ]; then
      requirement="run the adapter-owned matplotlib figure script launcher and, for report spectrograms, its --verify-report semantic gate; otherwise report unavailable"
    elif [ "$name" = "pdf-extract" ]; then
      requirement="run the adapter-owned PDF extraction launcher for PDF inputs, or report unavailable"
    elif [ "$name" = "web-image-search" ]; then
      requirement="run the adapter-owned web image search launcher with a configured provider, or report unavailable"
    else
      requirement="provide the named browser/pdf/script/web tool contract or report unavailable"
    fi
    ;;
  design)
    status=unsupported
    realization=adapter-coupled
    tool_contract=visual-harness
    tool_contract_check="adapters/opencode/bin/preflight.sh visual-harness <file.html>"
    runtime_surface=adapter-owned-visual-harness
    fallback=reference-only
    requirement="adapter-owned visual harness must be run for concrete design outputs; mode fragment remains reference-only"
    ;;
  qa)
    case "$name" in
      test)
        status=tool-contract
        realization=portable-with-tool-contract
        fallback="satisfy-tool-contract-or-report-unavailable"
        tool_contract=verification-runner
        tool_contract_check="adapters/opencode/bin/preflight.sh verification-runner --check -- <command>"
        runtime_surface=adapter-owned-verification-runner
        requirement="run explicit verification commands through the adapter-owned verification runner, or report unavailable"
        ;;
      security-review)
        status=portable
        realization=portable-persona
        requirement="perform read-only security review with OpenCode file and git diff tools; do not invoke Claude slash-command surfaces"
        ;;
      *)
        status=portable
        realization=portable-persona
        requirement="read-only review with OpenCode file/test tools"
        ;;
    esac
    ;;
  research)
    case "$name" in
      claim-verify)
        status=tool-contract
        realization=portable-with-tool-contract
        fallback="satisfy-tool-contract-or-report-unavailable"
        tool_contract=external-claim-verification
        tool_contract_check="adapters/opencode/bin/preflight.sh claim-verify --check <claim>"
        runtime_surface=adapter-owned-claim-verify
        requirement="run the adapter-owned external claim verification launcher with a configured provider, or report unavailable"
        ;;
      *)
        status=portable
        realization=portable-persona
        requirement="read/cite primary sources through available OpenCode tools"
        ;;
    esac
    ;;
  *)
    status=unsupported
    realization=unknown-family
    requirement="add adapter mapping before use"
    ;;
esac

case "$status" in
  portable)
    note="OpenCode may use the mode fragment after reading roles/MODES.md and resolving portable roles."
    ;;
  tool-contract)
    note="OpenCode may use the persona only after satisfying or explicitly downgrading the named tool contract."
    ;;
  unsupported)
    note="OpenCode must not claim native support; use as reference only."
    ;;
esac

printf 'mode=%s\n' "$mode"
printf 'adapter=opencode\n'
printf 'source=%s\n' "$source"
printf 'status=%s\n' "$status"
printf 'realization=%s\n' "$realization"
if [ -n "$tool_contract" ]; then
  printf 'tool_contract=%s\n' "$tool_contract"
fi
if [ -n "$tool_contract_check" ]; then
  printf 'tool_contract_check=%s\n' "$tool_contract_check"
fi
if [ -n "$report_tool_contract_check" ]; then
  printf 'report_tool_contract_check=%s\n' "$report_tool_contract_check"
fi
if [ -n "$runtime_surface" ]; then
  printf 'runtime_surface=%s\n' "$runtime_surface"
fi
if [ -n "$fallback" ]; then
  printf 'fallback=%s\n' "$fallback"
fi
printf 'requirement=%s\n' "$requirement"
printf 'note=%s\n' "$note"
