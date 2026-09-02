#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
HOOK="$ROOT/hooks/local-evidence-inject.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Project with legacy, shared, and campaign-cycle evidence artifacts.
mkdir -p "$TMP/project/.agent_reports/research/topic-card" \
  "$TMP/project/.agent_reports/documents/briefing" \
  "$TMP/project/.agent_reports/shared/analysis/ref_x/revisions/rrev_y" \
  "$TMP/project/.agent_reports/campaigns/camp_a/cycles/cyc_b/artifacts/research"
printf 'card body stays private\n' > "$TMP/project/.agent_reports/research/topic-card/card.md"
printf 'briefing body\n' > "$TMP/project/.agent_reports/documents/briefing/overview.md"
printf 'analysis body\n' > "$TMP/project/.agent_reports/shared/analysis/ref_x/revisions/rrev_y/report.md"
printf 'cycle research body\n' > "$TMP/project/.agent_reports/campaigns/camp_a/cycles/cyc_b/artifacts/research/notes.md"

# Hook-JSON mode: structured context with counts and entry paths, no bodies.
printf '{"hook_event_name":"UserPromptSubmit","prompt":"x","cwd":"%s"}\n' "$TMP/project" \
  | "$HOOK" > "$TMP/hook.out"
python3 - "$TMP/hook.out" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
context = value["hookSpecificOutput"]["additionalContext"]
assert "Local evidence present" in context
assert "research: 2 file(s)" in context
assert "documents: 1 file(s)" in context
assert "analysis: 1 file(s)" in context
assert "research/topic-card/card.md" in context
assert "Local evidence before recall" in context
assert "card body stays private" not in context
assert len(context.encode("utf-8")) <= 2400
PY

# CLI text mode.
"$HOOK" --cwd "$TMP/project" --format text > "$TMP/cli.out"
grep -q 'Local evidence present' "$TMP/cli.out"
grep -q 'documents/briefing/overview.md' "$TMP/cli.out"

# No evidence artifacts: silent.
mkdir -p "$TMP/bare"
"$HOOK" --cwd "$TMP/bare" --format text > "$TMP/bare.out"
[ ! -s "$TMP/bare.out" ]

# Worker sessions are exempt.
printf '{"hook_event_name":"UserPromptSubmit","prompt":"x","cwd":"%s"}\n' "$TMP/project" \
  | AGENT_SESSION_ROLE=worker "$HOOK" > "$TMP/worker.out"
[ ! -s "$TMP/worker.out" ]

# Malformed payload: silent, fail-open.
printf 'not json' | "$HOOK" > "$TMP/malformed.out" 2> "$TMP/malformed.err"
[ ! -s "$TMP/malformed.out" ] && [ ! -s "$TMP/malformed.err" ]

"$HOOK" --help | grep -q 'evidence artifacts exist'
echo 'local evidence prompt probe: PASS'
