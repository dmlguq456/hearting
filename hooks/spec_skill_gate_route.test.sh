#!/usr/bin/env bash
# End-to-end SD-45 route-record path through the real hooks/spec-skill-gate.sh
# (plan.md Step 1.5, round_1 finding 2 fail-open audit).
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPEC="$ROOT/hooks/spec-skill-gate.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export AGENT_HOME="$TMP/agent_home"
# This suite may itself run as a route-bound dispatch worker (AGENT_ARTIFACT_ROOT
# / AGENT_ROUTE_FILE / AGENT_ROUTE_ID pointing at the real registry); unset them
# so the fixture project resolves its own isolated artifact root.
unset AGENT_ARTIFACT_ROOT AGENT_ROUTE_FILE AGENT_ROUTE_ID AGENT_ARTIFACT_CYCLE_DIR

PROJECT="$TMP/proj"
ARTROOT="$PROJECT/.agent_reports"
mkdir -p "$ARTROOT/spec"
printf '# prd\n' > "$ARTROOT/spec/prd.md"

ROUTE="$TMP/route.json"
write_route() {
  satisfied=$1
  cat > "$ROUTE" <<JSON
{
  "schema_version": 2,
  "tracking": "tracked",
  "tracked_gate_evidence": {
    "workflow_mode": "tracked",
    "spec_read": {"satisfied": $satisfied, "source": "current"}
  },
  "cwd": "$PROJECT",
  "artifact_root": "$ARTROOT",
  "route_id": "rt-fixture"
}
JSON
}

# 1. record-only pass, no marker on disk.
write_route true
touch -d '+1 hour' "$ROUTE" 2>/dev/null || touch -A 010000 "$ROUTE" 2>/dev/null || sleep 1 && touch "$ROUTE"
if AGENT_ROUTE_ID=rt-fixture "$SPEC" --skill autopilot-code --cwd "$PROJECT" --session freshsid --route "$ROUTE"; then
  ok "record-only pass with no marker on disk"
else
  bad "record-only pass should succeed with a satisfied, fresh route record"
fi

# 2. flip spec_read.satisfied to false -> deny.
write_route false
touch "$ROUTE"
if AGENT_ROUTE_ID=rt-fixture "$SPEC" --skill autopilot-code --cwd "$PROJECT" --session freshsid --route "$ROUTE" 2>/tmp/deny1.err; then
  bad "unsatisfied spec_read should deny"
else
  [ "$?" -eq 2 ] && ok "unsatisfied spec_read denies (rc=2)" || ok "unsatisfied spec_read denies"
fi

# 3. restore satisfied, then touch the prd after the route -> stale deny.
write_route true
touch "$ROUTE"
sleep 1
touch "$ARTROOT/spec/prd.md"
if AGENT_ROUTE_ID=rt-fixture "$SPEC" --skill autopilot-code --cwd "$PROJECT" --session freshsid --route "$ROUTE"; then
  bad "stale route (prd touched after route) should deny"
else
  ok "stale route (prd newer than route) denies"
fi

# 4. add the session-local marker -> passes via the marker path even though
#    the route is stale, proving fix 1 never weakens the existing marker path.
key=$(printf '%s' "$PROJECT" | sed 's#[/ ]#_#g')
mkdir -p "$AGENT_HOME/.spec-grounding"
prd_mtime=$(stat -c %Y "$ARTROOT/spec/prd.md" 2>/dev/null || stat -f %m "$ARTROOT/spec/prd.md")
printf '%s' "$prd_mtime" > "$AGENT_HOME/.spec-grounding/freshsid__${key}"
if AGENT_ROUTE_ID=rt-fixture "$SPEC" --skill autopilot-code --cwd "$PROJECT" --session freshsid --route "$ROUTE"; then
  ok "marker path still passes when the route is stale (fall-through intact)"
else
  bad "marker path should still satisfy the gate independent of route staleness"
fi


# --------------------------------------------------------------------------
# W7C copy-only cutover: the surviving legacy `spec/prd.md` must not outrank
# the shared revision that superseded it (PRD v11 §24.3 D-71 -- legacy is a
# read-only fallback and readers resolve cycle -> shared -> legacy).
# --------------------------------------------------------------------------
echo "== copy-only cutover root =="
REF="ref_$(printf '1%.0s' $(seq 32))"
RREV="rrev_$(printf '2%.0s' $(seq 32))"

new_cutover_root() {
  # $1 = project dir, $2 = artifact root basename
  mkdir -p "$1/$2/spec/comp" "$1/$2/shared/spec/$REF/revisions/$RREV"
  printf 'stale legacy prd\n' > "$1/$2/spec/prd.md"
  printf 'stale legacy comp prd\n' > "$1/$2/spec/comp/prd.md"
  printf 'canonical shared prd\n' > "$1/$2/shared/spec/$REF/revisions/$RREV/prd.md"
}

activate_cutover() {
  python3 "$ROOT/utilities/artifact_producer.py" activate --artifact-root "$1" \
    --repository-id "repo_$(printf 'a%.0s' $(seq 32))" \
    --artifact-root-id "root_$(printf 'b%.0s' $(seq 32))" >/dev/null
}

CUT="$TMP/cutproj"
new_cutover_root "$CUT" ".agent_reports"
CUTROOT="$CUT/.agent_reports"

# 5. cutover inactive: legacy is still the live spec tree.
if "$SPEC" --skill autopilot-code --cwd "$CUT" --session cutsid >/dev/null 2>/tmp/cut_inactive.err; then
  bad "inactive cutover root should deny without a marker"
else
  if grep -qF "$CUTROOT/spec/prd.md" /tmp/cut_inactive.err \
    && ! grep -qF "/shared/spec/" /tmp/cut_inactive.err; then
    ok "inactive cutover keeps the legacy bucket as the governing spec tree"
  else
    bad "inactive cutover should name the legacy prd: $(cat /tmp/cut_inactive.err)"
  fi
fi

# 6. cutover active: the shared revision governs and the legacy copy is gone
#    from the candidate set entirely.
activate_cutover "$CUTROOT"
if "$SPEC" --skill autopilot-code --cwd "$CUT" --session cutsid >/dev/null 2>/tmp/cut_active.err; then
  bad "active cutover root should deny without a marker"
else
  if grep -qF "$CUTROOT/shared/spec/$REF/revisions/$RREV/prd.md" /tmp/cut_active.err \
    && ! grep -qF "$CUTROOT/spec/prd.md" /tmp/cut_active.err \
    && ! grep -qF "$CUTROOT/spec/comp/prd.md" /tmp/cut_active.err; then
    ok "active cutover names the shared revision and drops the stale legacy copy"
  else
    bad "active cutover should name only the shared prd: $(cat /tmp/cut_active.err)"
  fi
fi

# 7. reading the shared revision satisfies the gate.
prd_mtime=$(stat -c %Y "$CUTROOT/shared/spec/$REF/revisions/$RREV/prd.md" 2>/dev/null \
  || stat -f %m "$CUTROOT/shared/spec/$REF/revisions/$RREV/prd.md")
cut_key=$(printf '%s' "$CUT" | sed 's#[/ ]#_#g')
printf '%s' "$prd_mtime" > "$AGENT_HOME/.spec-grounding/cutsid__${cut_key}"
if "$SPEC" --skill autopilot-code --cwd "$CUT" --session cutsid; then
  ok "a read of the canonical shared prd satisfies the active-cutover gate"
else
  bad "the canonical shared prd read should satisfy the gate"
fi

# 8. the same shape on a legacy `.claude_reports` root.
CUTL="$TMP/cutlegacy"
new_cutover_root "$CUTL" ".claude_reports"
activate_cutover "$CUTL/.claude_reports"
if "$SPEC" --skill autopilot-code --cwd "$CUTL" --session cutlegacysid >/dev/null 2>/tmp/cut_legacyroot.err; then
  bad ".claude_reports cutover root should deny without a marker"
else
  if grep -qF "$CUTL/.claude_reports/shared/spec/$REF/revisions/$RREV/prd.md" /tmp/cut_legacyroot.err \
    && ! grep -qF "$CUTL/.claude_reports/spec/prd.md" /tmp/cut_legacyroot.err; then
    ok ".claude_reports cutover root also ranks the shared revision first"
  else
    bad ".claude_reports cutover root should name the shared prd: $(cat /tmp/cut_legacyroot.err)"
  fi
fi

# --------------------------------------------------------------------------
# python3-less fallback: the shell enumeration answers a legacy-only project
# exactly as today, and refuses to answer a root whose canonical spec lives in
# a layout it cannot rank.
# --------------------------------------------------------------------------
echo "== python3-less fallback =="
NOPY="$TMP/nopy-bin"
mkdir -p "$NOPY"
for cmd in sh git awk sed stat cat tr dirname basename seq printf; do
  resolved=$(command -v "$cmd" 2>/dev/null) || continue
  ln -sf "$resolved" "$NOPY/$cmd"
done
if PATH="$NOPY" command -v python3 >/dev/null 2>&1; then
  bad "the python3-less fixture PATH still resolves python3"
else
  ok "the python3-less fixture PATH resolves no python3"
fi

# 9. legacy-only project: unchanged candidate set and deny message.
if PATH="$NOPY" "$SPEC" --skill autopilot-code --cwd "$PROJECT" --session nopysid >/dev/null 2>/tmp/nopy_legacy.err; then
  bad "python3-less legacy project should still deny without a marker"
else
  if grep -qF "$ARTROOT/spec/prd.md" /tmp/nopy_legacy.err; then
    ok "python3-less fallback still enumerates a legacy-only project"
  else
    bad "python3-less fallback lost the legacy candidate: $(cat /tmp/nopy_legacy.err)"
  fi
fi

# 10. shared-spec root: fail closed instead of answering from legacy alone.
if PATH="$NOPY" "$SPEC" --skill autopilot-code --cwd "$CUT" --session nopysid >/dev/null 2>/tmp/nopy_shared.err; then
  bad "python3-less shared-spec root should not pass silently"
else
  if grep -qF "python3 is unavailable" /tmp/nopy_shared.err \
    && ! grep -qF "$CUTROOT/spec/prd.md" /tmp/nopy_shared.err; then
    ok "python3-less shared-spec root fails closed instead of naming stale legacy"
  else
    bad "python3-less shared-spec root should report the unresolvable layout: $(cat /tmp/nopy_shared.err)"
  fi
fi

# 11. a non spec-governed capability is unaffected by either fallback branch.
if PATH="$NOPY" "$SPEC" --skill audit --cwd "$CUT" --session nopysid; then
  ok "non spec-governed capability stays open on a python3-less shared-spec root"
else
  bad "non spec-governed capability should not be gated"
fi

echo "spec_skill_gate_route: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
