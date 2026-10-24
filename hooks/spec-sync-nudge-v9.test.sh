#!/usr/bin/env bash
set -euo pipefail
unset AGENT_ARTIFACT_ROOT AGENT_ARTIFACT_CYCLE_DIR
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.agent_reports/spec"
printf 'mode: [research]\n' > "$TMP/.agent_reports/spec/pipeline_state.yaml"
printf 'requirement: SD-31\nschema_version=1\nsample rate: 48 kHz\n' > "$TMP/.agent_reports/spec/prd.md"
printf 'x=1\n' > "$TMP/source.py"
run() { AGENT_HOME="$ROOT" bash "$ROOT/hooks/spec-sync-nudge.sh" --file "$TMP/source.py" --cwd "$TMP" "$@"; }
[ -z "$(run --old 'shape «3» rank «1»' --new 'shape «4» rank «2»')" ]
out=$(run --old 'SD-31 48 kHz schema_version=1' --new 'SD-32 44 kHz schema_version=2')
grep -Fq 'SD-31' <<<"$out"; grep -Fq '48 kHz' <<<"$out"; grep -Fq 'schema_version=1' <<<"$out"
echo 'spec-sync-nudge-v9: PASS'
