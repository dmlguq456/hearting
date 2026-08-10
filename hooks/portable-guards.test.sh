#!/usr/bin/env bash
set -u
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ART="$ROOT/hooks/artifact-guard.sh"
GIT="$ROOT/hooks/git-state-guard.sh"
MEM="$ROOT/hooks/builtin-memory-guard.sh"
CODEX="$ROOT/adapters/codex/bin/preflight.sh"
CODEX_PROJECTION="$ROOT/codex_setting/bin/preflight.sh"
CODEX_DISTILL="$ROOT/adapters/codex/bin/distill-worker.sh"
OPENCODE="$ROOT/adapters/opencode/bin/preflight.sh"
OPENCODE_PROJECTION="$ROOT/opencode_setting/bin/preflight.sh"
OPENCODE_DISTILL="$ROOT/adapters/opencode/bin/distill-worker.sh"
DESIGN="$ROOT/hooks/design-postwrite.sh"
SSN="$ROOT/hooks/spec-sync-nudge.sh"
MARK="$ROOT/hooks/spec-read-marker.sh"
SPEC="$ROOT/hooks/spec-skill-gate.sh"
CORE_MARK="$ROOT/hooks/core-read-marker.sh"
CORE_GUARD="$ROOT/hooks/core-first-guard.sh"
RECALL="$ROOT/hooks/mem-recall-inject.sh"
BRIEF="$ROOT/hooks/mem-briefing-inject.sh"
WTG="$ROOT/hooks/worktree-path-guard.sh"
SDR="$ROOT/hooks/stage-dispatch-reminder.sh"
CSG="$ROOT/hooks/conductor-stop-gate.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export AGENT_HOME="$TMP/agent_home"
export AGENT_MODEL_GOVERNOR_ROOT="$TMP/repo/.agent_reports/.runtime/model-worker-governor"
export MEM_RECALL_RECEIPTS="$TMP/recall-opportunities"

recall_opportunity() {
  fixture_cwd=$1
  fixture_sid=$2
  (cd "$fixture_cwd" && MEM_STORE="$TMP/recall-fixture-store" \
    python3 "$ROOT/tools/memory/mem.py" recall-gate --decision skip \
      --reason fixture-recovery --session-id "$fixture_sid" >/dev/null)
}
unset AGENT_DISPATCH_PARENT_SESSION_ID AGENT_DISPATCH_OWNER_HARNESS CODEX_THREAD_ID
# D-42 hermeticity: a live main or worker session must not leak session identity
# or worker markers into fixtures — every case sets its own markers inline.
unset CLAUDE_CODE_SESSION_ID CODEX_SESSION_ID \
  AGENT_SESSION_ROLE AGENT_DISPATCH_CHILD AGENT_DISPATCH_DEPTH \
  AGENT_DISPATCH_JOBS \
  AGENT_ARTIFACT_ROOT AGENT_ROUTE_FILE AGENT_ROUTE_ID AGENT_ROUTE_NODE \
  CLAUDE_CODE_CHILD_SESSION OPENCODE_DISPATCH_SLUG FLEET_TITLE_REFRESH \
  MEM_DISTILL MEM_DISTILL_ENABLE
DIRECT_DISPATCH_HOME="$ROOT"
if [ -f "$HOME/.codex/hearting/core/CORE.md" ]; then
  DIRECT_DISPATCH_HOME="$HOME/.codex/hearting"
elif [ -f "$HOME/hearting/core/CORE.md" ]; then
  DIRECT_DISPATCH_HOME="$HOME/hearting"
elif [ -f "$HOME/agent_setting/core/CORE.md" ]; then
  DIRECT_DISPATCH_HOME="$HOME/agent_setting"
fi

echo "== artifact guard CLI =="
mkdir -p "$TMP/proj/.agent_reports/spec"
if "$ART" --file "$TMP/proj/.agent_reports/spec/prd.md" --session test >/tmp/art.out 2>/tmp/art.err; then
  bad "spec write with no route declared should fail"
else
  [ "$?" -eq 2 ] && grep -q 'capability-artifact-route-required' /tmp/art.err \
    && ok "spec write with no route declared fails closed" \
    || bad "spec write with no route declared returned the wrong failure"
fi
rm -f "$TMP/proj/.agent_reports/spec/prd.md"

fixture_route() {
  fixture_capability=$1
  fixture_mode=$2
  fixture_name=$3
  fixture_output="$TMP/proj/.agent_reports/.runtime/routes/$fixture_name.json"
  mkdir -p "$(dirname "$fixture_output")"
  python3 "$ROOT/utilities/capability-route.py" compile \
    --capability "$fixture_capability" --capability-mode "$fixture_mode" \
    --intensity direct --cwd "$TMP/proj" \
    --artifact-root "$TMP/proj/.agent_reports" \
    --predicate atomic-outcome --predicate known-scope \
    --predicate no-shared-contract --predicate no-resource-run \
    --predicate no-artifact-handoff --predicate no-independent-verifier \
    --predicate focused-verification --tracking untracked \
    --spec-read not-applicable --drift-verdict no-project-spec \
    --workflow-mode untracked --artifact-guard preflight-passed \
    --inline-reason atomic-direct --output "$fixture_output" >/dev/null
  printf '%s\n' "$fixture_output"
}
fixture_route_id() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["route_id"])' "$1"
}

route_no_spec=$(fixture_route autopilot-code dev route-no-spec)
route_no_spec_id=$(fixture_route_id "$route_no_spec")
if AGENT_ROUTE_FILE="$route_no_spec" AGENT_ROUTE_ID="$route_no_spec_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/spec/prd.md" >/tmp/art_route.out 2>/tmp/art_route.err; then
  bad "a non-spec capability route should not authorize spec output"
else
  [ "$?" -eq 2 ] && grep -q 'capability-artifact-route-required' /tmp/art_route.err \
    && grep -q "$route_no_spec_id" /tmp/art_route.err \
    && ok "a non-spec route is a route-addressed structured failure" \
    || bad "non-spec route mismatch missing structured failure"
fi
route_spec=$(fixture_route autopilot-spec update route-spec)
route_spec_id=$(fixture_route_id "$route_spec")
if AGENT_ROUTE_FILE="$route_spec" AGENT_ROUTE_ID="$route_spec_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/spec/prd.md" >/tmp/art_route_guard.out 2>/tmp/art_route_guard.err; then
  ok "verified spec route authorizes its declared output"
else
  bad "verified spec route should authorize its declared output"
fi

route_plan=$(fixture_route autopilot-code dev route-plan)
route_plan_id=$(fixture_route_id "$route_plan")
mkdir -p "$TMP/proj/.agent_reports/plans/2026-08-03_fixture/plan" "$TMP/proj/.agent_reports/test_logs" "$TMP/proj/.agent_reports/.runtime"
if AGENT_ROUTE_FILE="$route_plan" AGENT_ROUTE_ID="$route_plan_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/plans/2026-08-03_fixture/plan/plan.md" >/tmp/art_scope_in.out 2>/tmp/art_scope_in.err; then
  ok "artifact write inside the declared node scope passes"
else
  bad "artifact write inside the declared node scope should pass"
fi
if AGENT_ROUTE_FILE="$route_plan" AGENT_ROUTE_ID="$route_plan_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/test_logs/run.log" >/tmp/art_scope_out.out 2>/tmp/art_scope_out.err; then
  bad "artifact write outside the declared node scope should fail"
else
  [ "$?" -eq 2 ] && grep -q 'artifact-write-outside-node-scope' /tmp/art_scope_out.err \
    && grep -q "$route_plan_id" /tmp/art_scope_out.err && ok "out-of-scope artifact write is a route-addressed structured failure" \
    || bad "out-of-scope artifact write missing structured route failure"
fi
if "$ART" --file "$TMP/proj/.agent_reports/test_logs/run.log" >/tmp/art_scope_noroute.out 2>/tmp/art_scope_noroute.err; then
  ok "artifact write with no route declared stays unbound"
else
  bad "artifact write with no route declared should stay unbound"
fi
if AGENT_ROUTE_FILE="$route_plan" AGENT_ROUTE_ID="$route_plan_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/.runtime/state.json" >/tmp/art_scope_dot.out 2>/tmp/art_scope_dot.err; then
  ok "dot-prefixed runtime state is exempt from node scope binding"
else
  bad "dot-prefixed runtime state should be exempt from node scope binding"
fi
# NOTE: despite the file name, AGENT_ROUTE_NODE=inline below names a real
# route node ("inline" is in route["nodes"]), so this exercises the ordinary
# non-owner node-scope path, not the C-item owner defect (empty
# AGENT_ROUTE_NODE). See the "owner binding" cases further down for that.
route_owner="$route_plan"
route_owner_id="$route_plan_id"
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/plans/2026-08-03_fixture/plan/plan.md" >/tmp/art_cycle_in.out 2>/tmp/art_cycle_in.err; then
  ok "a <cycle> placeholder scope binds one path segment"
else
  bad "a <cycle> placeholder scope should bind one path segment"
fi
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/test_logs/run.log" >/tmp/art_cycle_out.out 2>/tmp/art_cycle_out.err; then
  bad "a worktree-only scope should not authorize an unrelated artifact write"
else
  [ "$?" -eq 2 ] && ok "a worktree-only scope does not authorize an unrelated artifact write" \
    || bad "worktree-only scope violation wrong exit"
fi

# Item C: owner writes (empty AGENT_ROUTE_NODE, per SD-97) must skip node
# write-scope matching, not fall through the node lookup's StopIteration.
mkdir -p "$TMP/proj/.agent_reports/plans/owner_cycle/dev_logs"
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE="" \
  "$ART" --file "$TMP/proj/.agent_reports/plans/owner_cycle/checklist.md" >/tmp/art_owner_ck.out 2>/tmp/art_owner_ck.err; then
  ok "owner binding (empty route node) can write cycle artifacts"
else
  bad "owner binding (empty route node) should be able to write cycle artifacts"
fi
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE="" \
  "$ART" --file "$TMP/proj/.agent_reports/plans/owner_cycle/dev_logs/x.md" >/tmp/art_owner_dl.out 2>/tmp/art_owner_dl.err; then
  ok "owner binding (empty route node) can write dev_logs artifacts"
else
  bad "owner binding (empty route node) should be able to write dev_logs artifacts"
fi
mkdir -p "$TMP/proj/.agent_reports/spec"
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE="" \
  "$ART" --file "$TMP/proj/.agent_reports/spec/x.md" >/tmp/art_owner_spec.out 2>/tmp/art_owner_spec.err; then
  bad "owner binding must still be rejected from spec/ writes"
else
  [ "$?" -eq 2 ] && grep -q 'capability-artifact-route-required' /tmp/art_owner_spec.err \
    && ok "owner binding is rejected from spec/ writes" \
    || bad "owner binding spec/ rejection missing structured reason"
fi
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/documents/fake/doc.md" >/tmp/art_node_scope.out 2>/tmp/art_node_scope.err; then
  bad "a real node's out-of-scope write must still be rejected"
else
  [ "$?" -eq 2 ] && grep -q 'capability-artifact-route-required' /tmp/art_node_scope.err \
    && ok "a real node's out-of-scope write is still rejected (owner exception did not widen)" \
    || bad "real node out-of-scope rejection missing structured reason"
fi
mkdir -p "$TMP/ownerrepo/.agent_reports/_internal" "$TMP/ownerrepo-wt"
(
  cd "$TMP/ownerrepo" || exit 1
  git init -q
  git config user.email test@example.com
  git config user.name Test
  printf 'canonical\n' > .agent_reports/_internal/marker
  git add .
  git commit -q -m init
  git worktree add -q -b owner-topic "$TMP/ownerrepo-wt/topic"
)
if AGENT_ROUTE_FILE="$route_owner" AGENT_ROUTE_ID="$route_owner_id" AGENT_ROUTE_NODE="" \
  "$ART" --file "$TMP/ownerrepo-wt/topic/.agent_reports/plans/owner_cycle/checklist.md" >/tmp/art_owner_root.out 2>/tmp/art_owner_root.err; then
  bad "owner binding must still respect the canonical artifact root boundary"
else
  [ "$?" -eq 2 ] && grep -q 'canonical-artifact-root-mismatch' /tmp/art_owner_root.err \
    && ok "owner binding does not bypass the canonical artifact root boundary" \
    || bad "owner binding canonical-root rejection missing structured reason"
fi

route_refine=$(fixture_route autopilot-refine default route-refine)
route_refine_id=$(fixture_route_id "$route_refine")
mkdir -p "$TMP/proj/.agent_reports/documents/cycle" "$TMP/proj/.agent_reports/rebuttal"
printf 'before\n' > "$TMP/proj/.agent_reports/documents/cycle/doc.md"
printf 'legacy\n' > "$TMP/proj/.agent_reports/rebuttal/rebuttal.md"
if AGENT_ROUTE_FILE="$route_refine" AGENT_ROUTE_ID="$route_refine_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/documents/cycle/doc.md" >/tmp/art_refine_owned.out 2>/tmp/art_refine_owned.err; then
  [ ! -e "$TMP/proj/.agent_reports/documents/cycle/_internal/versions" ] \
    && ok "direct refine authorizes an owned document without a snapshot" \
    || bad "direct refine unexpectedly created a snapshot"
else
  bad "target-artifact should authorize an owned document"
fi
if AGENT_ROUTE_FILE="$route_refine" AGENT_ROUTE_ID="$route_refine_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/rebuttal/rebuttal.md" >/tmp/art_refine_unowned.out 2>/tmp/art_refine_unowned.err; then
  bad "target-artifact should not authorize an unowned rebuttal container"
else
  [ "$?" -eq 2 ] && grep -q 'artifact-write-outside-node-scope' /tmp/art_refine_unowned.err \
    && ok "target-artifact rejects unowned top-level artifact containers" \
    || bad "unowned target-artifact failure was not route-addressed"
fi
route_refine_direct="$route_refine"
route_refine_direct_id="$route_refine_id"
mkdir -p "$TMP/proj/.agent_reports/documents/minor"
printf 'minor\n' > "$TMP/proj/.agent_reports/documents/minor/doc.md"
if AGENT_ROUTE_FILE="$route_refine_direct" AGENT_ROUTE_ID="$route_refine_direct_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/documents/minor/doc.md" >/tmp/art_refine_direct.out 2>/tmp/art_refine_direct.err; then
  [ ! -e "$TMP/proj/.agent_reports/documents/minor/_internal/versions" ] \
    && ok "direct minor refine remains snapshot-free" \
    || bad "direct minor refine unexpectedly created a snapshot"
else
  bad "direct minor refine should pass for an owned document"
fi
mkdir -p "$TMP/proj/.agent_reports/plans/cycle/documents/fake"
if AGENT_ROUTE_FILE="$route_refine_direct" AGENT_ROUTE_ID="$route_refine_direct_id" AGENT_ROUTE_NODE=inline \
  "$ART" --file "$TMP/proj/.agent_reports/plans/cycle/documents/fake/doc.md" >/tmp/art_refine_nested.out 2>/tmp/art_refine_nested.err; then
  bad "target-artifact must be anchored to a canonical top-level container"
else
  [ "$?" -eq 2 ] && ok "target-artifact cannot match an owned-looking nested suffix" \
    || bad "nested target-artifact violation wrong exit"
fi

echo "== source-only worktree artifact guard =="
mkdir -p "$TMP/artrepo/.agent_reports/_internal" "$TMP/artrepo-wt"
(
  cd "$TMP/artrepo" || exit 1
  git init -q
  git config user.email test@example.com
  git config user.name Test
  printf 'canonical\n' > .agent_reports/_internal/marker
  git add .
  git commit -q -m init
  git worktree add -q -b artifact-topic "$TMP/artrepo-wt/topic"
)
if "$ART" --file "$TMP/artrepo-wt/topic/.agent_reports/_internal/probe.md" >/tmp/art_local.out 2>/tmp/art_local.err; then
  bad "linked-worktree artifact write should fail"
else
  [ "$?" -eq 2 ] \
    && grep -q 'source-only' /tmp/art_local.err \
    && ok "linked-worktree artifact write exits 2" \
    || bad "linked-worktree artifact write wrong exit/message"
fi
if "$ART" --file "$TMP/artrepo/.agent_reports/_internal/probe.md" >/tmp/art_main.out 2>/tmp/art_main.err; then
  ok "canonical artifact write passes"
else
  bad "canonical artifact write should pass"
fi

echo "== git state guard CLI =="
mkdir -p "$TMP/repo"
(
  cd "$TMP/repo" || exit 1
  git init -q
  git config user.email test@example.com
  git config user.name Test
  printf 'a\n' > f
  git add f
  git commit -q -m init
)
if "$GIT" --file "$TMP/repo/f" >/tmp/git.out 2>/tmp/git.err; then
  ok "clean repo passes"
else
  bad "clean repo should pass"
fi
git -C "$TMP/repo" checkout --detach -q HEAD
if "$GIT" --file "$TMP/repo/f" >/tmp/git.out 2>/tmp/git.err; then
  bad "detached repo should fail"
else
  [ "$?" -eq 2 ] && ok "detached repo exits 2" || bad "detached repo wrong exit"
fi

echo "== worktree path guard CLI =="
# git repo for the guard (reuse "$TMP/repo"; a git toplevel is all the guard needs).
# (a) 내장 EnterWorktree 전면 deny (repo 안 .claude/worktrees/ 오염).
if "$WTG" --tool EnterWorktree --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err; then
  bad "EnterWorktree should be denied"
else
  [ "$?" -eq 2 ] && grep -q 'EnterWorktree' /tmp/wtg.err && ok "worktree guard denies builtin EnterWorktree (exit 2)" \
    || bad "worktree guard wrong exit/message on EnterWorktree"
fi
# (b) Bash `git worktree add` 대상이 <repo>-wt/ 밖 → deny.
if "$WTG" --tool Bash --command 'git worktree add .claude/worktrees/foo -b foo' --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err; then
  bad "git worktree add outside -wt/ should be denied"
else
  [ "$?" -eq 2 ] && grep -q -- '-wt/' /tmp/wtg.err && ok "worktree guard denies git worktree add outside -wt/ (exit 2)" \
    || bad "worktree guard wrong exit/message on non -wt/ worktree add"
fi
# 오차단 금지 ①: 정규 형제 경로 <repo>-wt/<slug> 의 git worktree add 는 절대 차단 금지 (분사 정상 흐름).
if "$WTG" --tool Bash --command 'git worktree add /home/x/repo-wt/slug -b slug main' --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err; then
  ok "worktree guard passes regular <repo>-wt/<slug> add"
else
  bad "worktree guard should never block a regular <repo>-wt/ add"
fi
# 오차단 금지 ②: 비-add 서브커맨드(remove/list/prune)는 무간섭.
if "$WTG" --tool Bash --command 'git worktree remove /home/x/repo-wt/slug' --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err \
  && "$WTG" --tool Bash --command 'git worktree prune' --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err \
  && "$WTG" --tool Bash --command 'git worktree list' --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err; then
  ok "worktree guard leaves non-add worktree subcommands alone"
else
  bad "worktree guard should leave remove/prune/list alone"
fi
# 오차단 금지 ③: WORKTREE_GUARD_BYPASS=1 → 전면 우회.
if WORKTREE_GUARD_BYPASS=1 "$WTG" --tool EnterWorktree --cwd "$TMP/repo" --session wtgbypass >/tmp/wtg.out 2>/tmp/wtg.err; then
  ok "worktree guard honors WORKTREE_GUARD_BYPASS=1"
else
  bad "worktree guard should bypass under WORKTREE_GUARD_BYPASS=1"
fi
# 오차단 금지 ④: 비-git cwd → 무간섭 (fail-open).
if "$WTG" --tool EnterWorktree --cwd "$TMP" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err; then
  ok "worktree guard fails open outside a git repo"
else
  bad "worktree guard should fail open outside a git repo"
fi
# 오차단 금지 ⑤: 무관한 Bash 명령 → 무간섭.
if "$WTG" --tool Bash --command 'ls -la && git status' --cwd "$TMP/repo" --session wtgsid >/tmp/wtg.out 2>/tmp/wtg.err; then
  ok "worktree guard leaves unrelated Bash commands alone"
else
  bad "worktree guard should leave unrelated Bash commands alone"
fi

echo "== codex preflight wrapper =="
git -C "$TMP/repo" switch -q -c work
if "$CODEX" write "$TMP/repo/f" testsid >/tmp/codex.out 2>/tmp/codex.err; then
  ok "codex preflight passes clean write"
else
  bad "codex preflight should pass clean write"
fi
# A3 fallback verb: `preflight worktree-path` reproduces the same CLI outcomes
# already asserted against $WTG directly.
if "$CODEX" worktree-path --tool Bash --command 'git worktree add .claude/worktrees/foo -b foo' --cwd "$TMP/repo" --session wtgsid >/tmp/codex_wtp.out 2>/tmp/codex_wtp.err; then
  bad "codex preflight worktree-path should deny non -wt/ worktree add"
else
  [ "$?" -eq 2 ] && ok "codex preflight worktree-path fallback denies non -wt/ worktree add" \
    || bad "codex preflight worktree-path wrong exit on non -wt/ worktree add"
fi
if "$CODEX" worktree-path --tool Bash --command 'git worktree add /home/x/repo-wt/slug -b slug main' --cwd "$TMP/repo" --session wtgsid >/tmp/codex_wtp.out 2>/tmp/codex_wtp.err; then
  ok "codex preflight worktree-path fallback passes canonical <repo>-wt/ add"
else
  bad "codex preflight worktree-path fallback should pass canonical <repo>-wt/ add"
fi
mkdir -p "$TMP/runtime/projects/abc/memory"
if "$MEM" --file "$TMP/runtime/projects/abc/memory/MEMORY.md" >/tmp/mem.out 2>/tmp/mem.err; then
  bad "builtin memory guard should fail memory file write"
else
  [ "$?" -eq 2 ] && ok "builtin memory guard exits 2" || bad "builtin memory guard wrong exit"
fi
if "$CODEX" write "$TMP/runtime/projects/abc/memory/MEMORY.md" testsid >/tmp/codex.out 2>/tmp/codex.err; then
  bad "codex preflight should block memory file write"
else
  [ "$?" -eq 2 ] && ok "codex preflight blocks memory file write" || bad "codex preflight memory wrong exit"
fi
if AGENT_HOME="$ROOT" bash "$DESIGN" --file "$TMP/not-design.txt" >/tmp/design.out 2>/tmp/design.err \
  && "$CODEX" design "$TMP/not-design.txt" >/tmp/design.out 2>/tmp/design.err; then
  ok "design postwrite wrappers no-op on non-html"
else
  bad "design postwrite wrappers should no-op on non-html"
fi

echo "== spec sync nudge CLI =="
SSNPROJ="$TMP/ssnproj"
mkdir -p "$SSNPROJ/.agent_reports/spec"
printf 'project_name: demo\nmode: [research]\n' > "$SSNPROJ/.agent_reports/spec/pipeline_state.yaml"
printf '# Demo Spec\n- requirement: SD-31\n- schema_version=1\n- sample rate: 48 kHz\n- optimizer: Adam\n' > "$SSNPROJ/.agent_reports/spec/prd.md"
printf 'schema_version=1\n' > "$SSNPROJ/train.py"
# ① removed value still described in spec → nudge surfaces the stale spec line
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/train.py" --old 'schema_version=1' --new 'schema_version=2' --cwd "$SSNPROJ" >/tmp/ssn.out 2>/tmp/ssn.err \
  && grep -q 'spec/prd.md' /tmp/ssn.out && grep -q 'schema_version=1' /tmp/ssn.out; then
  ok "spec sync nudge surfaces stale spec line for a removed value"
else
  bad "spec sync nudge should surface the stale spec line"
fi
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/train.py" --old 'shape = «3» and rank = «1»' --new 'shape = «4» and rank = «2»' --cwd "$SSNPROJ" >/tmp/ssn7.out 2>/tmp/ssn7.err \
  && [ ! -s /tmp/ssn7.out ]; then
  ok "spec sync nudge ignores standalone numeric tokens"
else
  bad "spec sync nudge should ignore standalone numeric tokens"
fi
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/train.py" --old 'SD-31 uses 48 kHz' --new 'SD-32 uses 44 kHz' --cwd "$SSNPROJ" >/tmp/ssn8.out 2>/tmp/ssn8.err \
  && grep -q 'SD-31' /tmp/ssn8.out && grep -q '48 kHz' /tmp/ssn8.out; then
  ok "spec sync nudge retains requirement and unit identifiers"
else
  bad "spec sync nudge should retain requirement and unit identifiers"
fi
# ② removed token absent from spec → silent no-op (empty stdout)
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/train.py" --old 'zqx_unique_token' --new 'other' --cwd "$SSNPROJ" >/tmp/ssn2.out 2>/tmp/ssn2.err \
  && [ ! -s /tmp/ssn2.out ]; then
  ok "spec sync nudge no-ops when the removed token is absent from spec"
else
  bad "spec sync nudge should no-op when the removed token is absent from spec"
fi
# ③ not a spec-backed project → silent no-op
SSNNOSPEC="$TMP/ssn_nospec"
mkdir -p "$SSNNOSPEC"
printf 'x = 30\n' > "$SSNNOSPEC/a.py"
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNNOSPEC/a.py" --old 'x = 30' --new 'x = 50' --cwd "$SSNNOSPEC" >/tmp/ssn3.out 2>/tmp/ssn3.err \
  && [ ! -s /tmp/ssn3.out ]; then
  ok "spec sync nudge no-ops outside a spec-backed project"
else
  bad "spec sync nudge should no-op outside a spec-backed project"
fi
# ④ editing the spec file itself → not a target, silent no-op
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/.agent_reports/spec/prd.md" --old '30' --new '50' --cwd "$SSNPROJ" >/tmp/ssn4.out 2>/tmp/ssn4.err \
  && [ ! -s /tmp/ssn4.out ]; then
  ok "spec sync nudge no-ops when the edited file is the spec itself"
else
  bad "spec sync nudge should no-op when the edited file is the spec itself"
fi
# ⑤ prose/문서 편집(.md 등)은 대상 아님 — 흔한 단어 reword 가 무관 spec 산문과 오탐 매칭되던 회귀
printf 'see runtime bootstrap adapter notes\n' > "$SSNPROJ/NOTES.md"
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/NOTES.md" --old 'runtime bootstrap adapter' --new 'see docs' --cwd "$SSNPROJ" >/tmp/ssn5.out 2>/tmp/ssn5.err \
  && [ ! -s /tmp/ssn5.out ]; then
  ok "spec sync nudge no-ops on prose/doc edits (markdown reword)"
else
  bad "spec sync nudge should no-op on prose/doc edits"
fi
# ⑥ code 편집이라도 흔한 소문자 산문 단어(code-like 아님)는 후보에서 제외 — spec 에 있어도 발동 X
printf 'optimizer = Adam\n' > "$SSNPROJ/model.py"
if AGENT_HOME="$ROOT" bash "$SSN" --file "$SSNPROJ/model.py" --old 'optimizer here' --new 'thing here' --cwd "$SSNPROJ" >/tmp/ssn6.out 2>/tmp/ssn6.err \
  && [ ! -s /tmp/ssn6.out ]; then
  ok "spec sync nudge ignores plain-lowercase prose words (non code-like) even inside code"
else
  bad "spec sync nudge should ignore plain-lowercase prose words"
fi

if "$CODEX_PROJECTION" capability-info audit >/tmp/codex_projection.out 2>/tmp/codex_projection.err \
  && grep -q '^capability=audit$' /tmp/codex_projection.out \
  && grep -q '^adapter=codex$' /tmp/codex_projection.out; then
  ok "codex projection preflight resolves harness root"
else
  bad "codex projection preflight should resolve harness root"
fi
mkdir -p "$TMP/codex_pointer_home/.codex"
ln -s "$ROOT" "$TMP/codex_pointer_home/.codex/hearting"
if env -u AGENT_HOME HOME="$TMP/codex_pointer_home" "$ROOT/adapters/codex/utilities/agent-home.sh" >/tmp/codex_agent_home.out 2>/tmp/codex_agent_home.err \
  && grep -q "^$TMP/codex_pointer_home/.codex/hearting$" /tmp/codex_agent_home.out; then
  ok "codex agent-home wrapper resolves runtime pointer"
else
  bad "codex agent-home wrapper should resolve runtime pointer"
fi
if AGENT_HOME="$TMP/not-agent-home" HOME="$TMP/codex_pointer_home" "$ROOT/adapters/codex/utilities/agent-home.sh" >/tmp/codex_agent_home_invalid.out 2>/tmp/codex_agent_home_invalid.err \
  && grep -q "^$TMP/codex_pointer_home/.codex/hearting$" /tmp/codex_agent_home_invalid.out; then
  ok "codex agent-home wrapper ignores invalid AGENT_HOME"
else
  bad "codex agent-home wrapper should ignore invalid AGENT_HOME"
fi
mkdir -p "$TMP/codex_linked_home/hearting/core" "$TMP/codex_linked_home/agent_setting/core"
printf 'core\n' > "$TMP/codex_linked_home/hearting/core/CORE.md"
printf 'legacy\n' > "$TMP/codex_linked_home/agent_setting/core/CORE.md"
if env -u AGENT_HOME HOME="$TMP/codex_linked_home" "$ROOT/adapters/codex/utilities/agent-home.sh" >/tmp/codex_agent_home_linked.out 2>/tmp/codex_agent_home_linked.err \
  && grep -q "^$TMP/codex_linked_home/hearting$" /tmp/codex_agent_home_linked.out; then
  ok "codex agent-home wrapper prefers canonical hearting checkout"
else
  bad "codex agent-home wrapper must prefer canonical hearting checkout"
fi
rm -rf "$TMP/codex_linked_home/hearting"
if env -u AGENT_HOME HOME="$TMP/codex_linked_home" "$ROOT/adapters/codex/utilities/agent-home.sh" >/tmp/codex_agent_home_legacy.out 2>/tmp/codex_agent_home_legacy.err \
  && grep -q "^$TMP/codex_linked_home/agent_setting$" /tmp/codex_agent_home_legacy.out; then
  ok "codex agent-home wrapper retains legacy agent_setting fallback"
else
  bad "codex agent-home wrapper must retain legacy agent_setting fallback"
fi

# A preflight executable located in a linked/feature checkout is an
# orchestration source, not the installed harness root. Its default registry
# must come from the canonical HOME resolution; a valid explicit AGENT_HOME
# remains stronger.
mkdir -p "$TMP/codex_preflight_home/agent_setting/core" "$TMP/codex_preflight_home/agent_setting/.dispatch"
printf 'core\n' > "$TMP/codex_preflight_home/agent_setting/core/CORE.md"
printf '2026-07-24T00:00:00Z\topen\t%s\t%s\tcanonical-root-sentinel\tcapability=audit\n' "$TMP/repo" "$TMP/repo" \
  > "$TMP/codex_preflight_home/agent_setting/.dispatch/jobs.log"
env -u AGENT_HOME HOME="$TMP/codex_preflight_home" "$CODEX" liveness >/tmp/codex_preflight_root.out 2>/tmp/codex_preflight_root.err || true
if grep -q 'canonical-root-sentinel' /tmp/codex_preflight_root.out; then
  ok "worktree-local codex preflight resolves the canonical installed harness root"
else
  bad "worktree-local codex preflight must not use its source worktree as AGENT_HOME"
fi
mkdir -p "$TMP/codex_explicit_home/core" "$TMP/codex_explicit_home/.dispatch"
printf 'core\n' > "$TMP/codex_explicit_home/core/CORE.md"
printf '2026-07-24T00:00:00Z\topen\t%s\t%s\texplicit-root-sentinel\tcapability=audit\n' "$TMP/repo" "$TMP/repo" \
  > "$TMP/codex_explicit_home/.dispatch/jobs.log"
AGENT_HOME="$TMP/codex_explicit_home" HOME="$TMP/codex_preflight_home" "$CODEX" liveness >/tmp/codex_preflight_explicit.out 2>/tmp/codex_preflight_explicit.err || true
if grep -q 'explicit-root-sentinel' /tmp/codex_preflight_explicit.out \
  && ! grep -q 'canonical-root-sentinel' /tmp/codex_preflight_explicit.out; then
  ok "codex preflight preserves a valid explicit AGENT_HOME override"
else
  bad "codex preflight should prefer a valid explicit AGENT_HOME override"
fi

echo "== spec read gate CLI =="
mkdir -p "$TMP/specproj/.agent_reports/spec"
printf 'prd\n' > "$TMP/specproj/.agent_reports/spec/prd.md"
if "$SPEC" --skill autopilot-code --cwd "$TMP/specproj" --session testsid >/tmp/spec.out 2>/tmp/spec.err; then
  bad "spec-backed capability without read marker should fail"
else
  [ "$?" -eq 2 ] && ok "spec-backed capability without read marker exits 2" || bad "spec-backed capability wrong exit"
fi
if "$SPEC" --skill audit --cwd "$TMP/specproj" --session testsid >/tmp/spec.out 2>/tmp/spec.err; then
  ok "non spec-changing capability passes"
else
  bad "non spec-changing capability should pass"
fi
if "$MARK" --file "$TMP/specproj/.agent_reports/spec/prd.md" --session testsid >/tmp/spec.out 2>/tmp/spec.err \
  && "$SPEC" --skill autopilot-code --cwd "$TMP/specproj" --session testsid >/tmp/spec.out 2>/tmp/spec.err; then
  ok "read marker allows spec-changing capability"
else
  bad "read marker should allow spec-changing capability"
fi
sleep 1
printf 'prd updated\n' > "$TMP/specproj/.agent_reports/spec/prd.md"
if "$SPEC" --skill autopilot-code --cwd "$TMP/specproj" --session testsid >/tmp/spec.out 2>/tmp/spec.err; then
  bad "updated prd after marker should fail"
else
  [ "$?" -eq 2 ] && ok "updated prd after marker exits 2" || bad "updated prd wrong exit"
fi
if "$CODEX" read "$TMP/specproj/.agent_reports/spec/prd.md" testsid >/tmp/codex.out 2>/tmp/codex.err \
  && "$CODEX" capability autopilot-code "$TMP/specproj" testsid >/tmp/codex.out 2>/tmp/codex.err; then
  ok "codex read+capability wrapper passes spec gate"
else
  bad "codex read+capability wrapper should pass spec gate"
fi
if (cd "$TMP/specproj" && "$CODEX" read .agent_reports/spec/prd.md relsid >/tmp/codex_relative_read.out 2>/tmp/codex_relative_read.err) \
  && "$CODEX" capability autopilot-code "$TMP/specproj" relsid >/tmp/codex_relative_capability.out 2>/tmp/codex_relative_capability.err; then
  ok "codex read wrapper resolves relative prd paths for spec gate"
else
  bad "codex read wrapper should resolve relative prd paths for spec gate"
fi

mkdir -p "$TMP/canonical-spec/.agent_reports/spec" "$TMP/canonical-spec-wt"
(
  cd "$TMP/canonical-spec" || exit 1
  git init -q
  git config user.email test@example.com
  git config user.name Test
  printf 'canonical prd\n' > .agent_reports/spec/prd.md
  git add .
  git commit -q -m init
  git worktree add -q -b spec-topic "$TMP/canonical-spec-wt/topic"
)
"$MARK" --file "$TMP/canonical-spec-wt/topic/.agent_reports/spec/prd.md" --session shadowread
if "$SPEC" --skill autopilot-code --cwd "$TMP/canonical-spec-wt/topic" --session shadowread >/tmp/spec_shadow.out 2>/tmp/spec_shadow.err; then
  bad "worker-local shadow spec read should not satisfy canonical gate"
else
  [ "$?" -eq 2 ] \
    && ok "worker-local shadow spec read does not satisfy gate" \
    || bad "worker-local shadow spec read wrong exit"
fi
if "$MARK" --file "$TMP/canonical-spec/.agent_reports/spec/prd.md" --session canonicalread \
  && (cd "$TMP/canonical-spec-wt/topic" && "$SPEC" --skill autopilot-code --cwd . --session canonicalread); then
  ok "canonical spec marker satisfies relative linked-worktree gate"
else
  bad "canonical spec marker should satisfy relative linked-worktree gate"
fi

echo "== spec read gate: multi-spec candidate set =="
mkdir -p "$TMP/multispec/.agent_reports/spec/alpha/_internal/versions/v1" \
  "$TMP/multispec/.agent_reports/spec/beta"
printf 'root prd\n' > "$TMP/multispec/.agent_reports/spec/prd.md"
printf 'alpha prd\n' > "$TMP/multispec/.agent_reports/spec/alpha/prd.md"
printf 'beta prd\n' > "$TMP/multispec/.agent_reports/spec/beta/prd.md"
printf 'internal snapshot prd\n' > "$TMP/multispec/.agent_reports/spec/alpha/_internal/versions/v1/prd.md"

# a. sub-spec read passes the gate
if "$MARK" --file "$TMP/multispec/.agent_reports/spec/alpha/prd.md" --session msubsid \
  && "$SPEC" --skill autopilot-code --cwd "$TMP/multispec" --session msubsid; then
  ok "sub-spec prd read satisfies multi-spec gate"
else
  bad "sub-spec prd read should satisfy multi-spec gate"
fi

# b. reading an _internal snapshot writes no marker and does not satisfy the gate
"$MARK" --file "$TMP/multispec/.agent_reports/spec/alpha/_internal/versions/v1/prd.md" --session msinternalsid
if ls "$AGENT_HOME/.spec-grounding/msinternalsid__"* >/dev/null 2>&1; then
  bad "_internal snapshot read should not write a marker"
else
  ok "_internal snapshot read writes no marker"
fi
if "$SPEC" --skill autopilot-code --cwd "$TMP/multispec" --session msinternalsid >/tmp/ms_internal.out 2>/tmp/ms_internal.err; then
  bad "_internal snapshot read should not satisfy the gate"
else
  [ "$?" -eq 2 ] && ok "_internal snapshot read leaves gate denied" || bad "_internal snapshot deny wrong exit"
fi

# c. no read at all: deny, enumerate every candidate path + the governing-scope phrase
if "$SPEC" --skill autopilot-code --cwd "$TMP/multispec" --session msnoreadsid >/tmp/ms_noread.out 2>/tmp/ms_noread.err; then
  bad "unread multi-spec project should deny"
else
  if [ "$?" -eq 2 ] \
    && grep -qF "$TMP/multispec/.agent_reports/spec/prd.md" /tmp/ms_noread.err \
    && grep -qF "$TMP/multispec/.agent_reports/spec/alpha/prd.md" /tmp/ms_noread.err \
    && grep -qF "$TMP/multispec/.agent_reports/spec/beta/prd.md" /tmp/ms_noread.err \
    && grep -qF "Read the one governing the declared work scope" /tmp/ms_noread.err; then
    ok "unread multi-spec project lists every candidate and the governing-scope phrase"
  else
    bad "unread multi-spec project deny message incomplete"
  fi
fi

# d. sub-spec drift: read alpha, pass, drift, deny, re-read, pass
if "$MARK" --file "$TMP/multispec/.agent_reports/spec/alpha/prd.md" --session msdriftsid \
  && "$SPEC" --skill autopilot-code --cwd "$TMP/multispec" --session msdriftsid; then
  ok "fresh sub-spec read passes before drift"
else
  bad "fresh sub-spec read should pass before drift"
fi
sleep 1
printf 'alpha prd updated\n' > "$TMP/multispec/.agent_reports/spec/alpha/prd.md"
if "$SPEC" --skill autopilot-code --cwd "$TMP/multispec" --session msdriftsid >/tmp/ms_drift.out 2>/tmp/ms_drift.err; then
  bad "drifted sub-spec candidate should deny"
else
  [ "$?" -eq 2 ] && ok "drifted sub-spec candidate denies" || bad "drifted sub-spec candidate wrong exit"
fi
if "$MARK" --file "$TMP/multispec/.agent_reports/spec/alpha/prd.md" --session msdriftsid \
  && "$SPEC" --skill autopilot-code --cwd "$TMP/multispec" --session msdriftsid; then
  ok "re-read after drift passes again"
else
  bad "re-read after drift should pass again"
fi

# e. root-only deny message parity, fresh session id (no marker at all under this sid)
if "$SPEC" --skill autopilot-code --cwd "$TMP/specproj" --session msparitysid >/tmp/ms_parity.out 2>/tmp/ms_parity.err; then
  bad "fresh-session root-only project should deny"
else
  if [ "$?" -eq 2 ] \
    && grep -qF "This cwd is spec-backed, but prd.md was not read in this session. Read $TMP/specproj/.agent_reports/spec/prd.md directly with the Read tool, then retry. A code comment or brief quotation does not satisfy the gate." /tmp/ms_parity.err; then
    ok "root-only single-candidate deny message matches byte-for-byte parity"
  else
    bad "root-only single-candidate deny message should match today's exact text"
  fi
fi

# f. legacy .claude_reports sub-spec variant, in its own fixture (resolver prefers .agent_reports when both exist)
mkdir -p "$TMP/legacyspec/.claude_reports/spec/gamma"
printf 'legacy root prd\n' > "$TMP/legacyspec/.claude_reports/spec/prd.md"
printf 'legacy gamma prd\n' > "$TMP/legacyspec/.claude_reports/spec/gamma/prd.md"
if "$MARK" --file "$TMP/legacyspec/.claude_reports/spec/gamma/prd.md" --session mslegacysid \
  && "$SPEC" --skill autopilot-code --cwd "$TMP/legacyspec" --session mslegacysid; then
  ok "legacy .claude_reports sub-spec read satisfies gate"
else
  bad "legacy .claude_reports sub-spec read should satisfy gate"
fi

# g. adapters/claude/hooks/spec-skill-gate.sh must stay a symlink to the root
# hook (mode 120000), not a hand-maintained duplicate (round_1 finding 3,
# dispatch-guard-identity cycle) — a future accidental de-symlink would let
# the two adapters silently diverge on the SD-45 route-record gate.
CLAUDE_SPEC="$ROOT/adapters/claude/hooks/spec-skill-gate.sh"
if [ -L "$CLAUDE_SPEC" ] && cmp -s "$SPEC" "$CLAUDE_SPEC"; then
  ok "adapters/claude/hooks/spec-skill-gate.sh stays a byte-identical symlink to the root hook"
else
  bad "adapters/claude/hooks/spec-skill-gate.sh must remain a symlink to hooks/spec-skill-gate.sh"
fi

echo "== core-first adapter edit gate CLI =="
mkdir -p "$TMP/coreproj/core" "$TMP/coreproj/adapters/codex"
(
  cd "$TMP/coreproj" || exit 1
  git init -q
  git config user.email test@example.com
  git config user.name Test
  printf 'core\n' > core/CORE.md
  printf 'adapter\n' > adapters/codex/AGENTS.md
  git add core/CORE.md adapters/codex/AGENTS.md
  git commit -q -m init
)
if "$CORE_GUARD" --file "$TMP/coreproj/adapters/codex/AGENTS.md" --session coregatesid >/tmp/core_gate.out 2>/tmp/core_gate.err; then
  bad "adapter edit without core read marker should fail"
else
  [ "$?" -eq 2 ] && ok "adapter edit without core read marker exits 2" || bad "adapter edit without core marker wrong exit"
fi
if "$CORE_GUARD" --file "$TMP/coreproj/adapters/codex/new/sub/AGENTS.md" --session coregatesid >/tmp/core_gate_newdir.out 2>/tmp/core_gate_newdir.err; then
  bad "new adapter subdir edit without core read marker should fail"
else
  [ "$?" -eq 2 ] && ok "new adapter subdir edit without core marker exits 2" || bad "new adapter subdir core marker wrong exit"
fi
if "$CORE_MARK" --file "$TMP/coreproj/core/CORE.md" --session coregatesid >/tmp/core_gate.out 2>/tmp/core_gate.err \
  && "$CORE_GUARD" --file "$TMP/coreproj/adapters/codex/AGENTS.md" --session coregatesid >/tmp/core_gate.out 2>/tmp/core_gate.err; then
  ok "core read marker allows adapter edit"
else
  bad "core read marker should allow adapter edit"
fi
sleep 1
printf 'core updated\n' > "$TMP/coreproj/core/CORE.md"
if "$CORE_GUARD" --file "$TMP/coreproj/adapters/codex/AGENTS.md" --session coregatesid >/tmp/core_gate.out 2>/tmp/core_gate.err; then
  bad "updated core after marker should fail adapter edit"
else
  [ "$?" -eq 2 ] && ok "updated core after marker exits 2" || bad "updated core after marker wrong exit"
fi
if "$CODEX" read "$TMP/coreproj/core/CORE.md" codexcoregatesid >/tmp/codex_core_gate.out 2>/tmp/codex_core_gate.err \
  && "$CODEX" write "$TMP/coreproj/adapters/codex/AGENTS.md" codexcoregatesid >/tmp/codex_core_gate.out 2>/tmp/codex_core_gate.err; then
  ok "codex read+write wrapper passes core-first gate"
else
  bad "codex read+write wrapper should pass core-first gate"
fi
# Spec reads satisfy grounding but never replace capability-route participation.
# Ordinary source remains outside the artifact route gate.
mkdir -p "$TMP/cxspec/.agent_reports/spec" "$TMP/cxspec/.agent_reports/research" "$TMP/cxspec/src"
printf 'prd\n' > "$TMP/cxspec/.agent_reports/spec/prd.md"
printf 'state: x\n' > "$TMP/cxspec/.agent_reports/spec/pipeline_state.yaml"
if "$CODEX" write "$TMP/cxspec/.agent_reports/plans/c1/dev.md" cxwsid >/tmp/codex_wg.out 2>/tmp/codex_wg.err; then
  bad "codex write guard should deny ungrounded spec-changing (plans) write"
else
  [ "$?" -eq 2 ] && ok "codex write guard denies ungrounded spec-changing write" \
    || bad "codex write guard wrong exit on ungrounded spec write"
fi
"$CODEX" read "$TMP/cxspec/.agent_reports/spec/prd.md" cxwsid >/dev/null 2>&1
if "$CODEX" write "$TMP/cxspec/.agent_reports/plans/c1/dev.md" cxwsid >/tmp/codex_wg.out 2>/tmp/codex_wg.err; then
  bad "codex write guard should still require a route after prd read"
else
  [ "$?" -eq 2 ] && grep -q 'capability-artifact-route-required' /tmp/codex_wg.err \
    && ok "codex write guard keeps route participation separate from prd grounding" \
    || bad "codex write guard returned the wrong post-grounding route failure"
fi
if "$CODEX" write "$TMP/cxspec/src/main.py" cxwsid2 >/tmp/codex_wg.out 2>/tmp/codex_wg.err; then
  ok "codex write guard does not gate ordinary source files"
else
  bad "codex write guard should not gate ordinary source files"
fi
mkdir -p "$TMP/codex-route-home/core"
printf 'fixture\n' > "$TMP/codex-route-home/core/CORE.md"
AGENT_HOME="$TMP/codex-route-home" "$CODEX" read "$TMP/specproj/.agent_reports/spec/prd.md" testsid >/dev/null 2>&1
if AGENT_HOME="$TMP/codex-route-home" "$CODEX" route autopilot-code "$TMP/specproj" testsid debug direct >/tmp/codex_route.out 2>/tmp/codex_route.err \
  && grep -q '^runtime_surface=adapter-owned-harness-status$' /tmp/codex_route.out \
  && grep -q '^git_dirty_tracked=' /tmp/codex_route.out \
  && grep -q '^headless_open_jobs=' /tmp/codex_route.out \
  && grep -q '^runtime_surface=codex-userprompt-hook-signal$' /tmp/codex_route.out \
  && grep -q '^capability=autopilot-code$' /tmp/codex_route.out \
  && grep -q '^compat_reference=not-projected$' /tmp/codex_route.out \
  && grep -q '^pipeline_contract=code-plan>code-execute>code-test>code-report$' /tmp/codex_route.out \
  && grep -q '^capability=autopilot-code$' "$TMP/codex-route-home/.capability-grounding/testsid" \
  && grep -q '^mode=debug$' "$TMP/codex-route-home/.capability-grounding/testsid" \
  && grep -q '^intensity=direct$' "$TMP/codex-route-home/.capability-grounding/testsid"; then
  ok "codex route wrapper combines status, prompt signal, capability-info, and spec gate"
else
  bad "codex route wrapper should combine status, prompt signal, capability-info, and spec gate"
fi
AGENT_HOME="$TMP/codex-route-home" "$CODEX" read "$TMP/specproj/.agent_reports/spec/prd.md" worker-testsid >/dev/null 2>&1
if AGENT_HOME="$TMP/codex-route-home" AGENT_SESSION_ROLE=worker \
  "$CODEX" route autopilot-code "$TMP/specproj" worker-testsid debug direct >/tmp/codex_worker_route.out 2>/tmp/codex_worker_route.err \
  && [ ! -e "$TMP/codex-route-home/.capability-grounding/worker-testsid" ]; then
  ok "codex worker route does not create inline main capability grounding"
else
  bad "codex worker route should not create inline main capability grounding"
fi
if "$CODEX" capability nope-capability "$TMP/specproj" testsid >/tmp/codex_bad_capability_gate.out 2>/tmp/codex_bad_capability_gate.err; then
  bad "codex capability wrapper should reject unknown capabilities"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^check=failed$' /tmp/codex_bad_capability_gate.out \
    && grep -q '^reason=unknown-capability$' /tmp/codex_bad_capability_gate.out; then
    ok "codex capability wrapper rejects unknown capabilities"
  else
    bad "codex capability wrapper unknown capability output wrong"
  fi
fi

echo "== workflow lifecycle CLI =="
mkdir -p "$TMP/flowproj/.agent_reports"
mkdir -p "$TMP/codex-artifact/.agent_reports/spec"
if "$CODEX" write "$TMP/codex-artifact/.agent_reports/spec/prd.md" testsid >/tmp/codex-artifact.out 2>/tmp/codex-artifact.err; then
  bad "codex write wrapper should require a verified capability route for spec output"
else
  grep -q 'capability-artifact-route-required' /tmp/codex-artifact.err \
    && ok "codex write wrapper requires a verified capability route for spec output" \
    || bad "codex write wrapper returned the wrong route failure for spec output"
fi
if "$CODEX" memory "$TMP/flowproj" >/tmp/mem_inject.out 2>/tmp/mem_inject.err; then
  ok "codex memory wrapper exits cleanly"
else
  bad "codex memory wrapper should exit cleanly"
fi
if MEM_STORE="$TMP/codex_launcher_store" "$ROOT/adapters/codex/tools/memory/mem.py" stats >/tmp/codex_mem_launcher.out 2>/tmp/codex_mem_launcher.err \
  && grep -q '^# store stats$' /tmp/codex_mem_launcher.out; then
  ok "codex memory launcher ignores invalid non-harness AGENT_HOME"
else
  bad "codex memory launcher should fall back from invalid AGENT_HOME"
fi
if MEM_PY="$ROOT/tools/memory/mem.py" MEM_STORE="$TMP/empty-recall-store" \
  "$RECALL" --prompt "일반 질문" --cwd "$TMP/flowproj" --session-id portable-zero-hit \
    --turn-id turn-zero --format text >/tmp/recall.out 2>/tmp/recall.err \
  && [ ! -s /tmp/recall.out ] \
  && find "$MEM_RECALL_RECEIPTS" -type f -name '*.json' -print -quit | grep -q .; then
  ok "candidate bridge keeps a valid zero-hit prompt silent and publishes opportunity"
else
  bad "candidate bridge should keep zero-hit output silent while publishing opportunity"
fi
if "$CODEX" recall "전에 결정한 내용 뭐였지" "$TMP/flowproj" >/tmp/recall.out 2>/tmp/recall.err; then
  ok "codex recall wrapper exits cleanly"
else
  bad "codex recall wrapper should exit cleanly"
fi
if bash "$BRIEF" --cwd "$TMP/flowproj" --format text >/tmp/brief.out 2>/tmp/brief.err \
  && [ ! -s /tmp/brief.out ]; then
  ok "briefing wrapper no-ops outside agent desk"
else
  bad "briefing wrapper should no-op outside agent desk"
fi
if "$CODEX" briefing "$TMP/flowproj" >/tmp/brief.out 2>/tmp/brief.err; then
  ok "codex briefing wrapper exits cleanly"
else
  bad "codex briefing wrapper should exit cleanly"
fi
mkdir -p "$TMP/notes/cards" "$TMP/notes/_layer2/notes" "$TMP/board/.cache" "$TMP/board-wt"
printf 'card\n' > "$TMP/notes/cards/demo.md"
printf 'note\n' > "$TMP/notes/_layer2/notes/demo.md"
if AGENT_NOTES_ROOT="$TMP/notes" CAIRN_APP="$TMP/board" CAIRN_WT="$TMP/board-wt" \
  "$CODEX" worklog "$TMP/flowproj" >/tmp/worklog.out 2>/tmp/worklog.err \
  && grep -q "^agent-notes-root=$TMP/notes$" /tmp/worklog.out \
  && grep -q "^worklog-board-app=$TMP/board$" /tmp/worklog.out \
  && grep -q '^notes.cards.files=1$' /tmp/worklog.out \
  && grep -q '^notes._layer2/notes.files=1$' /tmp/worklog.out \
  && grep -q '^note=read-only inventory;' /tmp/worklog.out; then
  ok "codex worklog wrapper reports read-only state"
else
  bad "codex worklog wrapper should report read-only state"
fi
if env -u AGENT_NOTES_ROOT -u WORKLOG_NOTES_ROOT -u CAIRN_APP -u CAIRN_WT -u WORKLOG_BOARD_APP -u WORKLOG_BOARD_WT \
  "$CODEX" worklog "$TMP/flowproj" >/tmp/worklog-default.out 2>/tmp/worklog-default.err \
  && grep -q '^agent-notes-root=unset$' /tmp/worklog-default.out \
  && grep -q '^worklog-board-app=unset$' /tmp/worklog-default.out \
  && grep -q '^worklog-board-wt=unset$' /tmp/worklog-default.out \
  && ! grep -q '/.claude/worklog-board' /tmp/worklog-default.out; then
  ok "codex worklog wrapper has no Claude runtime defaults"
else
  bad "codex worklog wrapper should not default to Claude runtime paths"
fi
printf 'T\topen\t/r\t/r-wt/a\tjob-a\tcap\nT\tdone\t/r\t/r-wt/b\tjob-b\tcap\n' > "$TMP/status_jobs.log"
if AGENT_NOTES_ROOT="$TMP/notes" CAIRN_APP="$TMP/board" CAIRN_WT="$TMP/board-wt" AGENT_DISPATCH_JOBS="$TMP/status_jobs.log" \
  "$CODEX" status "$TMP/flowproj" testsid >/tmp/codex_status.out 2>/tmp/codex_status.err \
  && grep -q '^adapter=codex$' /tmp/codex_status.out \
  && grep -q '^headless_open_jobs=1$' /tmp/codex_status.out \
  && grep -q '^headless_open_slugs=job-a$' /tmp/codex_status.out \
  && grep -q '^runtime_surface=adapter-owned-harness-status$' /tmp/codex_status.out \
  && grep -q '^artifact_root_exists=1$' /tmp/codex_status.out \
  && grep -q '^git_repo=0$' /tmp/codex_status.out \
  && grep -q "^agent_notes_root=$TMP/notes$" /tmp/codex_status.out \
  && grep -q '^note=read-only snapshot;' /tmp/codex_status.out; then
  ok "codex status wrapper reports harness snapshot"
else
  bad "codex status wrapper should report harness snapshot"
fi
mkdir -p "$TMP/donebranch"
git -C "$TMP/donebranch" init -q
git -C "$TMP/donebranch" config user.email t@t
git -C "$TMP/donebranch" config user.name t
git -C "$TMP/donebranch" checkout -q -b main
echo x > "$TMP/donebranch/f"; git -C "$TMP/donebranch" add f; git -C "$TMP/donebranch" commit -q -m x
git -C "$TMP/donebranch" checkout -q -b topic
git -C "$TMP/donebranch" branch -q --set-upstream-to=main topic 2>/dev/null
if "$CODEX" status "$TMP/donebranch" testsid >/tmp/codex_done.out 2>/tmp/codex_done.err \
  && grep -q '^git_upstream=main$' /tmp/codex_done.out \
  && grep -q '^git_ahead=0$' /tmp/codex_done.out \
  && grep -q '^git_branch_done=1$' /tmp/codex_done.out; then
  ok "codex status flags a merged dead branch (DONE-BRANCH risk)"
else
  bad "codex status should flag a merged dead branch"
fi
mkdir -p "$TMP/dirtyrepo"
git -C "$TMP/dirtyrepo" init -q
git -C "$TMP/dirtyrepo" config user.email t@t
git -C "$TMP/dirtyrepo" config user.name t
git -C "$TMP/dirtyrepo" checkout -q -b main
echo clean > "$TMP/dirtyrepo/f"
git -C "$TMP/dirtyrepo" add f
git -C "$TMP/dirtyrepo" commit -q -m clean
git -C "$TMP/dirtyrepo" worktree add -q -b wtbranch "$TMP/dirtyrepo-wt/extra"
echo changed > "$TMP/dirtyrepo/f"
echo new > "$TMP/dirtyrepo/newfile"
if "$CODEX" status "$TMP/dirtyrepo" testsid >/tmp/codex_dirty_status.out 2>/tmp/codex_dirty_status.err \
  && grep -q '^git_dirty=1$' /tmp/codex_dirty_status.out \
  && grep -q '^git_dirty_tracked=1$' /tmp/codex_dirty_status.out \
  && grep -q '^git_untracked=1$' /tmp/codex_dirty_status.out \
  && grep -q '^git_dirty_total=2$' /tmp/codex_dirty_status.out \
  && grep -q '^git_worktree_count=2$' /tmp/codex_dirty_status.out \
  && grep -q '^git_extra_worktrees=1$' /tmp/codex_dirty_status.out; then
  ok "codex status distinguishes tracked dirty, untracked files, and sibling worktrees"
else
  bad "codex status should distinguish tracked dirty, untracked files, and sibling worktrees"
fi
if "$CODEX" prompt-signal "$TMP/flowproj" testsid >/tmp/codex_prompt_signal_tracked.out 2>/tmp/codex_prompt_signal_tracked.err \
  && grep -q '^autopilot_route=autopilot-required-for-spec-and-nontrivial-work$' /tmp/codex_prompt_signal_tracked.out \
  && grep -q '^routing_contract=core/WORKFLOW.md$' /tmp/codex_prompt_signal_tracked.out \
  && grep -q '^routing_action=read-workflow-and-select-codex-skill$' /tmp/codex_prompt_signal_tracked.out \
  && grep -q '^capability_entrypoints=codex-native-skills$' /tmp/codex_prompt_signal_tracked.out \
  && grep -q '^hook_event=UserPromptSubmit$' /tmp/codex_prompt_signal_tracked.out \
  && grep -q '^hook_scope=runtime-hook$' /tmp/codex_prompt_signal_tracked.out \
  && grep -q '^hook_boundary=shell-read-write-targeted-detection-explicit-preflight-fallback$' /tmp/codex_prompt_signal_tracked.out; then
  ok "codex prompt signal carries the autopilot routing contract"
else
  bad "codex prompt signal should carry the autopilot routing contract"
fi
if "$CODEX" prompt-signal "$TMP/dirtyrepo" testsid >/tmp/codex_prompt_signal_dirty.out 2>/tmp/codex_prompt_signal_dirty.err \
  && grep -q '^git_dirty_tracked=1$' /tmp/codex_prompt_signal_dirty.out \
  && grep -q '^git_untracked=1$' /tmp/codex_prompt_signal_dirty.out \
  && grep -q '^git_extra_worktrees=1$' /tmp/codex_prompt_signal_dirty.out \
  && "$CODEX" prompt-signal "$TMP/donebranch" testsid >/tmp/codex_prompt_signal_done.out 2>/tmp/codex_prompt_signal_done.err \
  && grep -q '^git_branch_done=1$' /tmp/codex_prompt_signal_done.out; then
  ok "codex prompt signal carries git dirty, worktree, and dead-branch risks"
else
  bad "codex prompt signal should carry git dirty, worktree, and dead-branch risks"
fi
if "$CODEX" permissions >/tmp/codex_permissions.out 2>/tmp/codex_permissions.err \
  && grep -q '^adapter=codex$' /tmp/codex_permissions.out \
  && grep -q '^runtime_surface=codex-native-approval-sandbox$' /tmp/codex_permissions.out \
  && grep -q '^permission_model=approval-policy+sandbox$' /tmp/codex_permissions.out \
  && grep -q '^claude_allowed_tools=unsupported$' /tmp/codex_permissions.out \
  && grep -q '^guard_contract=preflight-write-hooks-and-explicit-tool-contracts$' /tmp/codex_permissions.out \
  && grep -q '^structured_write_hooks=Write,Edit,MultiEdit,apply_patch,functions.apply_patch$' /tmp/codex_permissions.out \
  && grep -q '^targeted_shell_hooks=Bash,Shell,functions.exec_command$' /tmp/codex_permissions.out \
  && grep -q '^shell_read_write_hooks=targeted-detection$' /tmp/codex_permissions.out; then
  ok "codex permissions wrapper reports native approval/sandbox contract"
else
  bad "codex permissions wrapper should report native approval/sandbox contract"
fi
if "$CODEX" headless >/tmp/codex_headless.out 2>/tmp/codex_headless.err \
  && grep -q '^adapter=codex$' /tmp/codex_headless.out \
  && grep -q '^runtime_surface=codex-exec-headless$' /tmp/codex_headless.out \
  && grep -q '^tool_contract=headless-dispatch$' /tmp/codex_headless.out \
  && grep -q '^strict_tool_contract_check=adapters/codex/bin/preflight.sh headless --check --require-hook-trust <worktree>$' /tmp/codex_headless.out \
  && grep -q '^runtime_projection_requires=hearting,AGENTS.md,hooks.json,native-skills,native-agents,native-modes$' /tmp/codex_headless.out \
  && grep -q '^runtime_projection_strict_requires=complete-codex-hook-trust$' /tmp/codex_headless.out \
  && grep -q '^model_selection_policy=main-orchestrator-must-select-per-job$' /tmp/codex_headless.out \
  && grep -q '^model_selection_surface=--model-profile <deep|balanced-deep|light|mini> \[--model-role <portable-role>\]|--model-role <portable-role>|--model <model> --reasoning <effort>|--inherit-model-settings$' /tmp/codex_headless.out \
  && grep -q '^claude_headless=unsupported$' /tmp/codex_headless.out \
  && grep -q '^liveness_surface=codex-session-jsonl-mtime$' /tmp/codex_headless.out \
  && grep -q '^liveness_check=adapters/codex/bin/preflight.sh liveness \[jobs.log\]$' /tmp/codex_headless.out \
  && grep -q '^dispatch_prompt_contract=portable-typed-worker-bootstrap$' /tmp/codex_headless.out \
  && grep -q '^dispatch_input_validation=capability-info,capability-mode-catalog,optional-worker-mode-info,owner-mode-axis-consistency,qa-level,intensity-dispatch_depth-parent$' /tmp/codex_headless.out \
  && grep -q '^worker_startup_signal=wrapper-validated-metadata-or-immutable-route$' /tmp/codex_headless.out \
  && grep -q '^worker_startup_signal_contract=dispatch-wrapper-validates-before-materializing-prompt; worker rechecks only for safety$' /tmp/codex_headless.out \
  && grep -q '^broker_lifecycle=retired-status-stop-only$' /tmp/codex_headless.out \
  && grep -q '^launch_authority=conductor$' /tmp/codex_headless.out \
  && grep -q '^constraints=main-or-owner-dispatched,max-dispatch-depth-2-for-standard-plus-owner,register-open-job,headless-owner-supervisor-or-managed-gateway-or-interactive-poll-fallback,explicit-capability-mode-qa-intensity-dispatch_depth-parent-parent_sid,transcript-liveness-required$' /tmp/codex_headless.out; then
  ok "codex headless wrapper reports dispatch contract"
else
  bad "codex headless wrapper should report dispatch contract"
fi
if grep -q 'adapters/codex/bin/preflight.sh liveness \[jobs.log\]' "$ROOT/core/OPERATIONS.md" \
  && grep -q 'adapters/opencode/bin/preflight.sh liveness \[jobs.log\]' "$ROOT/core/OPERATIONS.md" \
  && grep -q 'adapter liveness wrapper' "$ROOT/core/OPERATIONS.md" \
  && ! grep -q '능동 점검한다\\*\\*: `bash <agent-home>/utilities/dispatch-liveness.sh`' "$ROOT/core/OPERATIONS.md"; then
  ok "portable operations routes headless liveness through adapter wrappers"
else
  bad "portable operations should route headless liveness through adapter wrappers"
fi
if "$CODEX" headless --check "$TMP/missing-worktree" >/tmp/codex_headless_missing.out 2>/tmp/codex_headless_missing.err; then
  bad "codex headless wrapper should fail missing worktree"
else
  rc=$?
  if [ "$rc" -eq 66 ] \
    && grep -q '^reason=worktree-not-found$' /tmp/codex_headless_missing.out; then
    ok "codex headless wrapper reports missing worktree"
  else
    bad "codex headless wrapper should report missing worktree"
  fi
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$TMP/codex_headless_home" "$ROOT/adapters/codex/bin/install-runtime-projection.sh" >/tmp/codex_headless_install.out 2>/tmp/codex_headless_install.err \
  && AGENT_HOME="$ROOT" CODEX_HOME="$TMP/codex_headless_home" "$CODEX" headless --check "$TMP/repo" >/tmp/codex_headless_check.out 2>/tmp/codex_headless_check.err \
  && grep -q '^runtime_projection=ok$' /tmp/codex_headless_check.out \
  && grep -q '^check=hook-trust:skipped reason=authoritative-current-hash-check-not-requested' /tmp/codex_headless_check.out \
  && grep -q '^check=ok$' /tmp/codex_headless_check.out; then
  ok "codex headless check validates runtime projection"
else
  bad "codex headless check should validate runtime projection"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$TMP/codex_headless_home" "$CODEX" headless --check --require-hook-trust "$TMP/repo" >/tmp/codex_headless_strict.out 2>/tmp/codex_headless_strict.err; then
  bad "codex headless strict check should fail when hook trust is incomplete"
else
  grep -q '^check=hook-trust:review-needed' /tmp/codex_headless_strict.out && ok "codex headless strict check requires complete hook trust" || bad "codex headless strict check missing trust output wrong"
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-missing-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --jobs "$TMP/codex-missing-model.log" >/tmp/codex_missing_model.out 2>/tmp/codex_missing_model.err; then
  bad "codex dispatch wrapper should require main-selected model settings"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=missing-dispatch-model-selection$' /tmp/codex_missing_model.out \
    && [ ! -e "$TMP/codex-missing-model.log" ]; then
    ok "codex dispatch wrapper requires main-selected model settings"
  else
    bad "codex dispatch wrapper should fail cleanly without model selection"
  fi
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-dispatch --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --jobs "$TMP/codex-dispatch.log" >/tmp/codex_dispatch.out 2>/tmp/codex_dispatch.err \
  && grep -q '^adapter=codex$' /tmp/codex_dispatch.out \
  && grep -q '^status=dry-run$' /tmp/codex_dispatch.out \
  && grep -q '^registered=0$' /tmp/codex_dispatch.out \
  && grep -q '^started=0$' /tmp/codex_dispatch.out \
  && grep -q '^model_source=explicit$' /tmp/codex_dispatch.out \
  && grep -q '^model_role=-$' /tmp/codex_dispatch.out \
  && grep -q '^model=gpt-test$' /tmp/codex_dispatch.out \
  && grep -q '^reasoning=low$' /tmp/codex_dispatch.out \
  && grep -q '^approval=never$' /tmp/codex_dispatch.out \
  && grep -q '^completion_delivery=app-server-supervised$' /tmp/codex_dispatch.out \
  && grep -q '^command=.*/codex-app-server-supervisor.py ' /tmp/codex_dispatch.out \
  && grep -q -- '--model gpt-test' /tmp/codex_dispatch.out \
  && grep -q -- '--reasoning low' /tmp/codex_dispatch.out \
  && grep -q -- '--approval never' /tmp/codex_dispatch.out \
  && ! grep -q -- '--ask-for-approval' /tmp/codex_dispatch.out \
  && [ ! -e "$TMP/codex-dispatch.log" ]; then
  ok "codex dispatch wrapper dry-runs headless command with main-selected model settings"
else
  bad "codex dispatch wrapper should dry-run headless command with main-selected model settings"
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-custom-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --approval inherit --jobs "$TMP/codex-custom-model.log" >/tmp/codex_custom_model.out 2>/tmp/codex_custom_model.err \
  && grep -q '^model_source=explicit$' /tmp/codex_custom_model.out \
  && grep -q '^model_role=-$' /tmp/codex_custom_model.out \
  && grep -q '^model=gpt-test$' /tmp/codex_custom_model.out \
  && grep -q '^reasoning=low$' /tmp/codex_custom_model.out \
  && grep -q '^approval=inherit$' /tmp/codex_custom_model.out \
  && grep -q -- '--model gpt-test' /tmp/codex_custom_model.out \
  && ! grep -q -- 'approval_policy=' /tmp/codex_custom_model.out \
  && [ ! -e "$TMP/codex-custom-model.log" ]; then
  ok "codex dispatch wrapper supports explicit model/reasoning overrides"
else
  bad "codex dispatch wrapper should support explicit model/reasoning overrides"
fi
if CODEX_DISPATCH_SANDBOX=danger-full-access \
  "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-env-sandbox --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --jobs "$TMP/codex-env-sandbox.log" >/tmp/codex_env_sandbox.out 2>/tmp/codex_env_sandbox.err \
  && grep -q -- '--sandbox danger-full-access' /tmp/codex_env_sandbox.out \
  && [ ! -e "$TMP/codex-env-sandbox.log" ]; then
  ok "codex dispatch wrapper inherits an explicit sandbox environment override"
else
  bad "codex dispatch wrapper should inherit an explicit sandbox environment override"
fi
if CODEX_DISPATCH_SANDBOX=workspace-write CODEX_DISPATCH_SANDBOX_FORCE=danger-full-access \
  "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-forced-sandbox --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --sandbox workspace-write --jobs "$TMP/codex-forced-sandbox.log" >/tmp/codex_forced_sandbox.out 2>/tmp/codex_forced_sandbox.err \
  && grep -q -- '--sandbox danger-full-access' /tmp/codex_forced_sandbox.out \
  && [ ! -e "$TMP/codex-forced-sandbox.log" ]; then
  ok "codex dispatch wrapper preserves a forced nested sandbox invariant"
else
  bad "codex dispatch wrapper should preserve a forced nested sandbox invariant"
fi
if CODEX_THREAD_ID=codex-unmanaged-parent \
  "$CODEX" dispatch --register --worktree "$TMP/repo" --slug codex-unmanaged-parent --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --dispatch-depth 1 --jobs "$TMP/codex-unmanaged-parent.log" --log-dir "$TMP/unmanaged-parent-logs" >/tmp/codex_unmanaged_parent.out 2>/tmp/codex_unmanaged_parent.err; then
  bad "codex dispatch wrapper should reject an unmanaged interactive parent"
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^reason=managed-entry-required$' /tmp/codex_unmanaged_parent.out \
    && grep -q '^child_spawned=0$' /tmp/codex_unmanaged_parent.out \
    && [ ! -e "$TMP/codex-unmanaged-parent.log" ]; then
    ok "codex dispatch wrapper rejects unmanaged interactive parents before registry mutation"
  else
    bad "codex dispatch wrapper should fail unmanaged interactive parents before registry mutation"
  fi
fi
if CODEX_THREAD_ID=codex-current-thread CODEX_DISPATCH_PARENT_CURRENT_FORCE=1 \
  "$CODEX" dispatch --register --allow-unmanaged-parent-poll --worktree "$TMP/repo" --slug codex-current-parent --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --dispatch-depth 1 --parent invented-parent --parent-session-id invented-session --jobs "$TMP/codex-current-parent.log" --log-dir "$TMP/current-parent-logs" >/tmp/codex_current_parent.out 2>/tmp/codex_current_parent.err \
  && grep -q '^parent_session_id=codex-current-thread$' /tmp/codex_current_parent.out \
  && codex_current_parent_prompt=$(sed -n 's/^prompt_file=//p' /tmp/codex_current_parent.out) \
  && [ -f "$codex_current_parent_prompt" ] \
  && grep -q -- '- parent: -' "$codex_current_parent_prompt" \
  && grep -q -- '- parent_session_id: codex-current-thread' "$codex_current_parent_prompt" \
  && grep -q 'parent_sid=codex-current-thread' "$TMP/codex-current-parent.log" \
  && ! grep -q 'parent=invented-parent' "$TMP/codex-current-parent.log"; then
  ok "codex drill dispatch ancestry follows the current runtime thread"
else
  bad "codex drill dispatch ancestry should follow the current runtime thread"
fi
if AGENT_DISPATCH_JOBS="$TMP/codex-env-jobs.log" \
  "$CODEX" dispatch --register --worktree "$TMP/repo" --slug codex-env-jobs --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low >/tmp/codex_env_jobs.out 2>/tmp/codex_env_jobs.err \
  && grep -q "codex-env-jobs" "$TMP/codex-env-jobs.log" \
  && grep -q "^job_registry=$TMP/codex-env-jobs.log$" /tmp/codex_env_jobs.out; then
  ok "codex dispatch wrapper keeps nested jobs in the selected shared registry"
else
  bad "codex dispatch wrapper should keep nested jobs in the selected shared registry"
fi
if AGENT_DISPATCH_JOBS="$TMP/codex-env-jobs.log" \
  "$CODEX" harvest --slug codex-env-jobs --mark-done >/tmp/codex_env_harvest.out 2>/tmp/codex_env_harvest.err \
  && grep -q "^job_registry=$TMP/codex-env-jobs.log$" /tmp/codex_env_harvest.out \
  && grep -q '^marked_done=1$' /tmp/codex_env_harvest.out \
  && AGENT_DISPATCH_JOBS="$TMP/codex-env-jobs.log" "$CODEX" liveness >/tmp/codex_env_liveness.out 2>/tmp/codex_env_liveness.err \
  && grep -q '^open 0 ; alive 0 ; suspect/dead 0$' /tmp/codex_env_liveness.out; then
  ok "codex harvest and liveness keep using the selected shared registry"
else
  bad "codex harvest and liveness should keep using the selected shared registry"
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-inherit-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --inherit-model-settings --jobs "$TMP/codex-inherit-model.log" >/tmp/codex_inherit_model.out 2>/tmp/codex_inherit_model.err \
  && grep -q '^model_source=inherit$' /tmp/codex_inherit_model.out \
  && grep -q '^model_role=inherit$' /tmp/codex_inherit_model.out \
  && grep -q '^model=inherit$' /tmp/codex_inherit_model.out \
  && grep -q '^reasoning=inherit$' /tmp/codex_inherit_model.out \
  && ! grep -q -- '--model ' /tmp/codex_inherit_model.out \
  && [ ! -e "$TMP/codex-inherit-model.log" ]; then
  ok "codex dispatch wrapper can explicitly inherit model settings"
else
  bad "codex dispatch wrapper should explicitly inherit model settings only on request"
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-bad-cap --capability nope-capability --mode dev --qa standard --prompt-text "do work" --jobs "$TMP/codex-bad-cap.log" >/tmp/codex_bad_cap.out 2>/tmp/codex_bad_cap.err; then
  bad "codex dispatch wrapper should fail invalid capability"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=invalid-dispatch-capability$' /tmp/codex_bad_cap.out \
    && grep -q '^capability=nope-capability$' /tmp/codex_bad_cap.out \
    && [ ! -e "$TMP/codex-bad-cap.log" ]; then
    ok "codex dispatch wrapper validates capability before registry write"
  else
    bad "codex dispatch wrapper should validate capability before registry write"
  fi
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-bad-mode --capability autopilot-code --capability-mode dev --worker-mode dev/nope --worker-type stage --unit dev/nope --qa standard --prompt-text "do work" --jobs "$TMP/codex-bad-mode.log" >/tmp/codex_bad_mode.out 2>/tmp/codex_bad_mode.err; then
  bad "codex dispatch wrapper should fail invalid mode"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=invalid-dispatch-worker-mode$' /tmp/codex_bad_mode.out \
    && grep -q '^worker_mode=dev/nope$' /tmp/codex_bad_mode.out \
    && [ ! -e "$TMP/codex-bad-mode.log" ]; then
    ok "codex dispatch wrapper validates mode before registry write"
  else
    bad "codex dispatch wrapper should validate mode before registry write"
  fi
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-bad-qa --capability autopilot-code --mode dev --qa extreme --prompt-text "do work" --jobs "$TMP/codex-bad-qa.log" >/tmp/codex_bad_qa.out 2>/tmp/codex_bad_qa.err; then
  bad "codex dispatch wrapper should fail invalid QA level"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=invalid-dispatch-qa$' /tmp/codex_bad_qa.out \
    && grep -q '^qa=extreme$' /tmp/codex_bad_qa.out \
    && grep -q '^allowed_qa=quick,light,standard,thorough,adversarial$' /tmp/codex_bad_qa.out \
    && [ ! -e "$TMP/codex-bad-qa.log" ]; then
    ok "codex dispatch wrapper validates QA level before registry write"
  else
    bad "codex dispatch wrapper should validate QA level before registry write"
  fi
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-default-home --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low >/tmp/codex_dispatch_default.out 2>/tmp/codex_dispatch_default.err \
  && grep -Fxq "job_registry=$DIRECT_DISPATCH_HOME/.dispatch/jobs.log" /tmp/codex_dispatch_default.out \
  && grep -Fxq "prompt_file=$DIRECT_DISPATCH_HOME/.dispatch/logs/codex-default-home.codex.prompt.txt" /tmp/codex_dispatch_default.out \
  && [ ! -e "$AGENT_HOME/.dispatch/jobs.log" ]; then
  ok "codex dispatch wrapper defaults to validated harness root"
else
  bad "codex dispatch wrapper should not trust invalid AGENT_HOME for default registry"
fi
if AGENT_HOME="$TMP/not-agent-home" python3 "$ROOT/adapters/codex/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug codex-direct-home --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low >/tmp/codex_dispatch_direct.out 2>/tmp/codex_dispatch_direct.err \
  && grep -Fxq "job_registry=$DIRECT_DISPATCH_HOME/.dispatch/jobs.log" /tmp/codex_dispatch_direct.out \
  && grep -Fxq "prompt_file=$DIRECT_DISPATCH_HOME/.dispatch/logs/codex-direct-home.codex.prompt.txt" /tmp/codex_dispatch_direct.out; then
  ok "codex dispatch script ignores invalid AGENT_HOME"
else
  bad "codex dispatch script should validate AGENT_HOME"
fi
if "$CODEX" dispatch --register --worktree "$TMP/repo" --slug codex-quick-depth1 --capability autopilot-code --mode dev --intensity quick --dispatch-depth 1 --parent-session-id test-parent --owner-harness codex --prompt-text "quick work" --model gpt-test --reasoning low --jobs "$TMP/codex-quick-depth1.log" >/tmp/codex_quick_depth1.out 2>/tmp/codex_quick_depth1.err; then
  bad "codex dispatch wrapper should reject route-unbound quick jobs"
else
  rc=$?
  if [ "$rc" -eq 65 ] \
    && grep -q '^reason=quick-headless-unavailable$' /tmp/codex_quick_depth1.out \
    && grep -q '^child_spawned=0$' /tmp/codex_quick_depth1.out \
    && [ ! -e "$TMP/codex-quick-depth1.log" ]; then
    ok "codex dispatch wrapper rejects route-unbound quick jobs"
  else
    bad "codex dispatch wrapper should fail closed for route-unbound quick jobs"
  fi
fi
if "$CODEX" dispatch --dry-run --worktree "$TMP/repo" --slug codex-quick-depth2 --capability autopilot-code --capability-mode dev --worker-mode dev/backend --worker-type stage --unit dev/backend --intensity quick --dispatch-depth 2 --parent codex-parent --prompt-text "quick work" --model gpt-test --reasoning low >/tmp/codex_quick_depth2.out 2>/tmp/codex_quick_depth2.err; then
  bad "codex dispatch wrapper should reject quick dispatch-depth-2 jobs"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=invalid-depth-two-intensity$' /tmp/codex_quick_depth2.out \
    && grep -q '^dispatch_depth=2$' /tmp/codex_quick_depth2.out \
    && grep -q '^intensity=quick$' /tmp/codex_quick_depth2.out \
    && [ ! -e "$TMP/codex-quick-depth2.log" ]; then
    ok "codex dispatch wrapper rejects quick dispatch-depth-2 jobs"
  else
    bad "codex dispatch wrapper should reject quick dispatch-depth-2 jobs"
  fi
fi
mkdir -p "$TMP/codex-stubbin"
printf '{}\n' > "$TMP/codex_headless_home/auth.json"
cat > "$TMP/codex-stubbin/codex" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "$*" > "$CODEX_STUB_ARGV"
EOF
chmod +x "$TMP/codex-stubbin/codex"
if PATH="$TMP/codex-stubbin:$PATH" AGENT_HOME="$ROOT" CODEX_HOME="$TMP/codex_headless_home" CODEX_STUB_ARGV="$TMP/codex-start.argv" \
  "$CODEX" dispatch --start --completion-delivery poll --worktree "$TMP/repo" --slug nested/codex-start --capability autopilot-code --mode dev --qa standard --prompt-text "nested work" --model gpt-test --reasoning low --jobs "$TMP/codex-start.log" --log-dir "$TMP/codex-logs" >/tmp/codex_dispatch_start.out 2>/tmp/codex_dispatch_start.err \
  && grep -q '^status=start$' /tmp/codex_dispatch_start.out \
  && grep -q '^started=1$' /tmp/codex_dispatch_start.out \
  && [ "$(readlink "$TMP/repo/.dispatch/codex-home")" = "$TMP/codex_headless_home" ] \
  && grep -Fxq "runtime_home_projection=$TMP/repo/.dispatch/codex-home" /tmp/codex_dispatch_start.out \
  && codex_start_prompt=$(sed -n 's/^prompt_file=//p' /tmp/codex_dispatch_start.out) \
  && [ -f "$codex_start_prompt" ] \
  && grep -q '^# Portable Worker Kernel$' "$codex_start_prompt" \
  && grep -q '^# Worker Type: Owner$' "$codex_start_prompt" \
  && grep -q 'worker_type: owner' "$codex_start_prompt" \
  && grep -q 'assigned_contract: autopilot-code' "$codex_start_prompt" \
  && grep -q 'route_state: validated dispatch metadata' "$codex_start_prompt" \
  && grep -q 'adapters/codex/skills/autopilot-code/SKILL.md' "$codex_start_prompt" \
  && grep -q 'preflight.sh qa-policy standard code' "$codex_start_prompt" \
  && grep -q 'dispatch registered dispatch-depth-2 stages by invoking the' "$codex_start_prompt" \
  && grep -q 'nested work' "$codex_start_prompt"; then
  for _ in $(seq 1 20); do
    [ -f "$TMP/codex-start.argv" ] && break
    sleep 0.1
  done
  if grep -q -- '--cd' "$TMP/codex-start.argv" 2>/dev/null; then
    if grep -q -- '--model gpt-test' "$TMP/codex-start.argv" \
      && grep -q -- 'model_reasoning_effort=' "$TMP/codex-start.argv" \
      && grep -q -- 'approval_policy=' "$TMP/codex-start.argv"; then
      ok "codex dispatch wrapper starts nested slug with main-selected model settings"
    else
      bad "codex dispatch wrapper should launch codex exec with main-selected model settings"
    fi
  else
    bad "codex dispatch wrapper should launch codex exec after projection check"
  fi
else
  bad "codex dispatch wrapper should start nested slug after runtime projection check"
fi
if PATH="$TMP/codex-stubbin:$PATH" AGENT_HOME="$ROOT" CODEX_HOME="$TMP/codex_headless_home" CODEX_STUB_ARGV="$TMP/codex-strict-start.argv" \
  "$CODEX" dispatch --start --require-hook-trust --worktree "$TMP/repo" --slug codex-strict-start --capability autopilot-code --mode dev --qa standard --prompt-text "strict work" --model gpt-test --reasoning low --jobs "$TMP/codex-strict-start.log" --log-dir "$TMP/codex-strict-logs" >/tmp/codex_dispatch_strict_start.out 2>/tmp/codex_dispatch_strict_start.err; then
  bad "codex dispatch strict start should fail when hook trust is incomplete"
else
  if grep -q '^check=hook-trust:review-needed' /tmp/codex_dispatch_strict_start.out \
    && [ ! -e "$TMP/codex-strict-start.log" ] \
    && [ ! -e "$TMP/codex-strict-logs/codex-strict-start.codex.prompt.txt" ]; then
    ok "codex dispatch strict start fails before registry writes when hook trust is incomplete"
  else
    bad "codex dispatch strict start should fail before registry writes"
  fi
fi
if "$CODEX" dispatch --register --worktree "$TMP/repo" --slug codex-dispatch --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model gpt-test --reasoning low --jobs "$TMP/codex-dispatch.log" --log-dir "$TMP/codex-register-logs" >/tmp/codex_dispatch.out 2>/tmp/codex_dispatch.err \
  && grep -q '^status=register$' /tmp/codex_dispatch.out \
  && grep -q '^registered=1$' /tmp/codex_dispatch.out \
  && grep -q '^started=0$' /tmp/codex_dispatch.out \
  && grep -q '^registry_lock=.*/codex-dispatch.log.lock$' /tmp/codex_dispatch.out \
  && grep -Eq '^prompt_file=.*/codex-register-logs/codex-dispatch\.att-[A-Za-z0-9._-]+\.codex\.prompt\.txt$' /tmp/codex_dispatch.out \
  && codex_dispatch_prompt=$(sed -n 's/^prompt_file=//p' /tmp/codex_dispatch.out) \
  && [ -f "$TMP/codex-dispatch.log.lock" ] \
  && [ -f "$codex_dispatch_prompt" ] \
  && grep -q '^# Worker Type: Owner$' "$codex_dispatch_prompt" \
  && grep -q $'open\t.*/repo\t.*/repo\tcodex-dispatch\t' "$TMP/codex-dispatch.log" \
  && grep -q 'capability_mode=dev' "$TMP/codex-dispatch.log" \
  && ! grep -q ',mode=' "$TMP/codex-dispatch.log" \
  && grep -q 'worker_type=owner' "$TMP/codex-dispatch.log" \
  && grep -q 'assigned_contract=autopilot-code' "$TMP/codex-dispatch.log"; then
  ok "codex dispatch wrapper registers open headless job"
else
  bad "codex dispatch wrapper should register open headless job"
fi
if "$CODEX" harvest --jobs "$TMP/codex-dispatch.log" --slug codex-dispatch >/tmp/codex_harvest.out 2>/tmp/codex_harvest.err \
  && grep -q '^adapter=codex$' /tmp/codex_harvest.out \
  && grep -q '^runtime_surface=codex-dispatch-harvest$' /tmp/codex_harvest.out \
  && grep -q '^registry_lock=.*/codex-dispatch.log.lock$' /tmp/codex_harvest.out \
  && grep -q '^matched=1$' /tmp/codex_harvest.out \
  && grep -q '^marked_done=0$' /tmp/codex_harvest.out \
  && grep -q '^job_status=open$' /tmp/codex_harvest.out \
  && grep -q '^merge_action=unsupported$' /tmp/codex_harvest.out; then
  ok "codex harvest wrapper reports open registry jobs"
else
  bad "codex harvest wrapper should report open registry jobs"
fi
if "$CODEX" harvest --jobs "$TMP/codex-dispatch.log" --slug codex-dispatch --mark-done >/tmp/codex_harvest_done.out 2>/tmp/codex_harvest_done.err \
  && grep -q '^marked_done=1$' /tmp/codex_harvest_done.out \
  && grep -q '^registry_lock=.*/codex-dispatch.log.lock$' /tmp/codex_harvest_done.out \
  && grep -q $'done\t.*/repo\t.*/repo\tcodex-dispatch\t' "$TMP/codex-dispatch.log" \
  && grep -q 'worker_type=owner' "$TMP/codex-dispatch.log" \
  && grep -q 'assigned_contract=autopilot-code' "$TMP/codex-dispatch.log"; then
  ok "codex harvest wrapper marks selected jobs done"
else
  bad "codex harvest wrapper should mark selected jobs done"
fi
if python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug claude-missing-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --jobs "$TMP/claude-missing-model.log" >/tmp/claude_missing_model.out 2>/tmp/claude_missing_model.err; then
  bad "claude dispatch wrapper should require main-selected model settings"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=missing-dispatch-model-selection$' /tmp/claude_missing_model.out \
    && [ ! -e "$TMP/claude-missing-model.log" ]; then
    ok "claude dispatch wrapper requires main-selected model settings"
  else
    bad "claude dispatch wrapper should fail cleanly without model selection"
  fi
fi
# Expected light-tier values derive from the adapter config SoT (models.conf);
# restating them as literals here re-creates the drift the SoT removed.
claude_light_model=$(. "$ROOT/adapters/claude/config/models.conf" && printf '%s' "$CFG_TIER_LIGHT_MODEL")
claude_light_effort=$(. "$ROOT/adapters/claude/config/models.conf" && printf '%s' "$CFG_TIER_LIGHT_EFFORT")
if python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug claude-role-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model-role "fast implementer" --jobs "$TMP/claude-role-model.log" >/tmp/claude_role_model.out 2>/tmp/claude_role_model.err \
  && grep -q '^adapter=claude$' /tmp/claude_role_model.out \
  && grep -q '^model_source=role$' /tmp/claude_role_model.out \
  && grep -q '^model_role=fast implementer$' /tmp/claude_role_model.out \
  && grep -q "^model=${claude_light_model}\$" /tmp/claude_role_model.out \
  && grep -q "^effort=${claude_light_effort}\$" /tmp/claude_role_model.out \
  && grep -q -- "--model ${claude_light_model}" /tmp/claude_role_model.out \
  && grep -q -- "--effort ${claude_light_effort}" /tmp/claude_role_model.out \
  && [ ! -e "$TMP/claude-role-model.log" ]; then
  ok "claude dispatch wrapper supports main-selected model roles"
else
  bad "claude dispatch wrapper should support main-selected model roles"
fi
if python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug claude-explicit-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model claude-test --effort low --jobs "$TMP/claude-explicit-model.log" >/tmp/claude_explicit_model.out 2>/tmp/claude_explicit_model.err \
  && grep -q '^model_source=explicit$' /tmp/claude_explicit_model.out \
  && grep -q '^model=claude-test$' /tmp/claude_explicit_model.out \
  && grep -q '^effort=low$' /tmp/claude_explicit_model.out \
  && grep -q -- '--model claude-test' /tmp/claude_explicit_model.out \
  && grep -q -- '--effort low' /tmp/claude_explicit_model.out \
  && [ ! -e "$TMP/claude-explicit-model.log" ]; then
  ok "claude dispatch wrapper supports explicit model/effort selection"
else
  bad "claude dispatch wrapper should support explicit model/effort selection"
fi
if python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug claude-inherit-model --capability autopilot-code --capability-mode dev --qa standard --prompt-text "do work" --inherit-model-settings --jobs "$TMP/claude-inherit-model.log" >/tmp/claude_inherit_model.out 2>/tmp/claude_inherit_model.err; then
  bad "claude dispatch wrapper should reject unprovable model inheritance"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=headless-model-inheritance-ineligible$' /tmp/claude_inherit_model.out \
    && [ ! -e "$TMP/claude-inherit-model.log" ]; then
    ok "claude dispatch wrapper rejects unprovable model inheritance"
  else
    bad "claude dispatch wrapper should reject unprovable model inheritance cleanly"
  fi
fi
if AGENT_DISPATCH_JOBS="$TMP/claude-env-jobs.log" \
  python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --register --worktree "$TMP/repo" --slug claude-env-jobs --capability autopilot-code --capability-mode dev --qa standard --prompt-text "do work" --model-role "fast implementer" --log-dir "$TMP/claude-env-logs" >/tmp/claude_env_jobs.out 2>/tmp/claude_env_jobs.err \
  && grep -q "^job_registry=$TMP/claude-env-jobs.log$" /tmp/claude_env_jobs.out \
  && grep -q $'open\t.*/repo\t.*/repo\tclaude-env-jobs\t' "$TMP/claude-env-jobs.log"; then
  ok "claude dispatch wrapper uses the selected shared registry"
else
  bad "claude dispatch wrapper should use the selected shared registry"
fi
if CODEX_THREAD_ID=codex-parent python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --register --allow-unmanaged-parent-poll --worktree "$TMP/repo" --slug claude-owned --capability autopilot-code --capability-mode audit --qa standard --intensity thorough --dispatch-depth 1 --worker-type owner --unit _kernel/owner --assigned-contract autopilot-code --worker-role verifier --owner autopilot-code --model-role "fast reviewer" --prompt-text "verify" --jobs "$TMP/claude-owned.log" --log-dir "$TMP/claude-owned-logs" >/tmp/claude_owned.out 2>/tmp/claude_owned.err \
  && grep -q '^intensity=thorough$' /tmp/claude_owned.out \
  && grep -q '^dispatch_depth=1$' /tmp/claude_owned.out \
  && grep -q '^parent_session_id=codex-parent$' /tmp/claude_owned.out \
  && grep -q '^worker_role=verifier$' /tmp/claude_owned.out \
  && grep -q '^owner=autopilot-code$' /tmp/claude_owned.out \
  && grep -q '^owner_harness=codex$' /tmp/claude_owned.out \
  && grep -q $'open\t.*/repo\t.*/repo\tclaude-owned\t' "$TMP/claude-owned.log" \
  && grep -q 'worker_role=verifier' "$TMP/claude-owned.log" \
  && grep -q 'worker_type=owner' "$TMP/claude-owned.log" \
  && grep -q 'capability_mode=audit' "$TMP/claude-owned.log" \
  && ! grep -q ',mode=' "$TMP/claude-owned.log" \
  && grep -q 'assigned_contract=autopilot-code' "$TMP/claude-owned.log" \
  && claude_owned_prompt=$(sed -n 's/^prompt_file=//p' /tmp/claude_owned.out) \
  && [ -f "$claude_owned_prompt" ] \
  && grep -q 'parent_session_id: codex-parent' "$claude_owned_prompt"; then
  ok "claude dispatch wrapper records cross-harness ownership metadata"
else
  bad "claude dispatch wrapper should record cross-harness ownership metadata"
fi
if python3 "$ROOT/adapters/claude/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug claude-bad-depth --capability autopilot-code --capability-mode audit --worker-mode qa/test --worker-type review --unit qa/test --assigned-contract code-test --qa standard --intensity standard --dispatch-depth 2 --model-role "fast reviewer" --prompt-text "verify" --jobs "$TMP/claude-bad-depth.log" >/tmp/claude_bad_depth.out 2>/tmp/claude_bad_depth.err; then
  bad "claude dispatch wrapper should reject dispatch-depth-2 without parent"
else
  rc=$?
  if [ "$rc" -eq 64 ] && grep -q '^reason=missing-dispatch-parent$' /tmp/claude_bad_depth.out; then
    ok "claude dispatch wrapper validates dispatch-depth-2 parent metadata"
  else
    bad "claude dispatch wrapper should fail cleanly for invalid dispatch-depth-2 metadata"
  fi
fi
mkdir -p "$TMP/codex-live-sessions/2026/06/30"
cat > "$TMP/codex-live-sessions/2026/06/30/rollout-live-codex.jsonl" <<EOF
{"timestamp":"2026-06-30T00:00:00.000Z","type":"session_meta","payload":{"id":"live-codex","cwd":"$TMP/flowproj"}}
EOF
touch "$TMP/codex-live-sessions/2026/06/30/rollout-live-codex.jsonl"
printf '2026-06-30T00:00:00Z\topen\t%s\t%s\tlive-codex\t-\n' "$TMP/repo" "$TMP/flowproj" > "$TMP/codex-jobs.log"
if CODEX_SESSIONS="$TMP/codex-live-sessions" DISPATCH_STALE_MIN=60 "$CODEX" liveness "$TMP/codex-jobs.log" >/tmp/codex_liveness.out 2>/tmp/codex_liveness.err \
  && grep -q '^ALIVE    live-codex ' /tmp/codex_liveness.out \
  && grep -q '^open 1 ; alive 1 ; suspect/dead 0$' /tmp/codex_liveness.out; then
  ok "codex liveness wrapper matches worktree to session transcript"
else
  bad "codex liveness wrapper should match worktree to session transcript"
fi
printf '2026-06-30T00:00:00Z\topen\t%s\t%s\tdead-codex\t-\n' "$TMP/repo" "$TMP/missing-live-wt" > "$TMP/codex-dead-jobs.log"
if CODEX_SESSIONS="$TMP/codex-live-sessions" "$CODEX" liveness "$TMP/codex-dead-jobs.log" >/tmp/codex_liveness_dead.out 2>/tmp/codex_liveness_dead.err; then
  bad "codex liveness wrapper should fail dead jobs"
else
  rc=$?
  if [ "$rc" -eq 3 ] \
    && grep -q '^DEAD     dead-codex ' /tmp/codex_liveness_dead.out \
    && grep -q '^open 1 ; alive 0 ; suspect/dead 1$' /tmp/codex_liveness_dead.out; then
    ok "codex liveness wrapper reports dead jobs"
  else
    bad "codex liveness wrapper should report dead jobs"
  fi
fi
# codex-adapter-parity audit P-28 (2026-07-04): the fixtures above only exercise ALIVE
# (DISPATCH_STALE_MIN=60) and DEAD (never-matching worktree) — SUSPECT was never hit, and
# neither impl was exercised directly against the shared STALE_MIN=15 default. Call both
# impls (no edits) to close that gap.
echo "== dispatch-liveness.sh state transitions (P-28) =="
sh_wt="$TMP/sh-live-wt"
mkdir -p "$sh_wt"
sh_enc=$(printf '%s' "$sh_wt" | sed 's#[/._]#-#g')
mkdir -p "$AGENT_HOME/projects/$sh_enc"
printf '2026-07-01T00:00:00Z\topen\t%s\t%s\tsh-live\t-\n' "$TMP/repo" "$sh_wt" > "$TMP/sh-liveness-jobs.log"
touch "$AGENT_HOME/projects/$sh_enc/s.jsonl"
if DISPATCH_RUNTIME_ROOT="$AGENT_HOME" "$ROOT/utilities/dispatch-liveness.sh" "$TMP/sh-liveness-jobs.log" >/tmp/sh_liveness_alive.out 2>/tmp/sh_liveness_alive.err \
  && grep -q '^ALIVE      sh-live  ' /tmp/sh_liveness_alive.out \
  && grep -q '^— open 1 · alive 1 · suspect/dead/exited 0$' /tmp/sh_liveness_alive.out; then
  ok "dispatch-liveness.sh reports fresh transcript ALIVE"
else
  bad "dispatch-liveness.sh should report fresh transcript ALIVE"
fi
touch -d '20 minutes ago' "$AGENT_HOME/projects/$sh_enc/s.jsonl"
if DISPATCH_RUNTIME_ROOT="$AGENT_HOME" "$ROOT/utilities/dispatch-liveness.sh" "$TMP/sh-liveness-jobs.log" >/tmp/sh_liveness_suspect.out 2>/tmp/sh_liveness_suspect.err; then
  bad "dispatch-liveness.sh should exit non-zero when transcript goes SUSPECT"
else
  rc=$?
  if [ "$rc" -eq 3 ] \
    && grep -q 'SUSPECT  sh-live  ' /tmp/sh_liveness_suspect.out \
    && grep -q '^— open 1 · alive 0 · suspect/dead/exited 1$' /tmp/sh_liveness_suspect.out; then
    ok "dispatch-liveness.sh reports stale transcript SUSPECT"
  else
    bad "dispatch-liveness.sh should report stale transcript SUSPECT"
  fi
fi
rm -f "$AGENT_HOME/projects/$sh_enc/s.jsonl"
if DISPATCH_RUNTIME_ROOT="$AGENT_HOME" "$ROOT/utilities/dispatch-liveness.sh" "$TMP/sh-liveness-jobs.log" >/tmp/sh_liveness_dead.out 2>/tmp/sh_liveness_dead.err; then
  bad "dispatch-liveness.sh should exit non-zero when transcript is missing"
else
  rc=$?
  if [ "$rc" -eq 3 ] \
    && grep -q 'DEAD     sh-live  ' /tmp/sh_liveness_dead.out \
    && grep -q '^— open 1 · alive 0 · suspect/dead/exited 1$' /tmp/sh_liveness_dead.out; then
    ok "dispatch-liveness.sh reports missing transcript DEAD"
  else
    bad "dispatch-liveness.sh should report missing transcript DEAD"
  fi
fi

echo "== dispatch-liveness.py state transitions (P-28) =="
py_wt="$TMP/py-live-wt"
mkdir -p "$py_wt" "$TMP/py-liveness-sessions"
printf '{"payload":{"cwd":"%s"}}\n' "$py_wt" > "$TMP/py-liveness-sessions/s.jsonl"
printf '2026-07-01T00:00:00Z\topen\t%s\t%s\tpy-live\t-\n' "$TMP/repo" "$py_wt" > "$TMP/py-liveness-jobs.log"
if CODEX_SESSIONS="$TMP/py-liveness-sessions" "$ROOT/adapters/codex/bin/dispatch-liveness.py" "$TMP/py-liveness-jobs.log" >/tmp/py_liveness_alive.out 2>/tmp/py_liveness_alive.err \
  && grep -q '^ALIVE    py-live ' /tmp/py_liveness_alive.out \
  && grep -q '^open 1 ; alive 1 ; suspect/dead 0$' /tmp/py_liveness_alive.out; then
  ok "dispatch-liveness.py reports fresh transcript ALIVE"
else
  bad "dispatch-liveness.py should report fresh transcript ALIVE"
fi
touch -d '20 minutes ago' "$TMP/py-liveness-sessions/s.jsonl"
if CODEX_SESSIONS="$TMP/py-liveness-sessions" "$ROOT/adapters/codex/bin/dispatch-liveness.py" "$TMP/py-liveness-jobs.log" >/tmp/py_liveness_suspect.out 2>/tmp/py_liveness_suspect.err; then
  bad "dispatch-liveness.py should exit non-zero when transcript goes SUSPECT"
else
  rc=$?
  if [ "$rc" -eq 3 ] \
    && grep -q '^SUSPECT  py-live ' /tmp/py_liveness_suspect.out \
    && grep -q '^open 1 ; alive 0 ; suspect/dead 1$' /tmp/py_liveness_suspect.out; then
    ok "dispatch-liveness.py reports stale transcript SUSPECT"
  else
    bad "dispatch-liveness.py should report stale transcript SUSPECT"
  fi
fi
rm -f "$TMP/py-liveness-sessions/s.jsonl"
if CODEX_SESSIONS="$TMP/py-liveness-sessions" "$ROOT/adapters/codex/bin/dispatch-liveness.py" "$TMP/py-liveness-jobs.log" >/tmp/py_liveness_dead.out 2>/tmp/py_liveness_dead.err; then
  bad "dispatch-liveness.py should exit non-zero when transcript is missing"
else
  rc=$?
  if [ "$rc" -eq 3 ] \
    && grep -q '^DEAD     py-live ' /tmp/py_liveness_dead.out \
    && grep -q '^open 1 ; alive 0 ; suspect/dead 1$' /tmp/py_liveness_dead.out; then
    ok "dispatch-liveness.py reports missing transcript DEAD"
  else
    bad "dispatch-liveness.py should report missing transcript DEAD"
  fi
fi

echo "== dispatch-liveness .sh/.py STALE_MIN=15 default parity (P-28) =="
parity_sh_wt="$TMP/parity-sh-wt"
mkdir -p "$parity_sh_wt" "$AGENT_HOME/projects"
parity_sh_enc=$(printf '%s' "$parity_sh_wt" | sed 's#[/._]#-#g')
mkdir -p "$AGENT_HOME/projects/$parity_sh_enc"
printf '2026-07-01T00:00:00Z\topen\t%s\t%s\tparity-sh\t-\n' "$TMP/repo" "$parity_sh_wt" > "$TMP/parity-sh-jobs.log"
touch -d '10 minutes ago' "$AGENT_HOME/projects/$parity_sh_enc/s.jsonl"
if DISPATCH_RUNTIME_ROOT="$AGENT_HOME" "$ROOT/utilities/dispatch-liveness.sh" "$TMP/parity-sh-jobs.log" >/tmp/parity_sh_10m.out 2>/tmp/parity_sh_10m.err \
  && grep -q '^ALIVE      parity-sh ' /tmp/parity_sh_10m.out; then
  ok "dispatch-liveness.sh: ~10m transcript is ALIVE under default STALE_MIN=15"
else
  bad "dispatch-liveness.sh should report ~10m transcript ALIVE under default STALE_MIN=15"
fi

parity_py_wt="$TMP/parity-py-wt"
mkdir -p "$parity_py_wt" "$TMP/parity-py-sessions"
printf '{"payload":{"cwd":"%s"}}\n' "$parity_py_wt" > "$TMP/parity-py-sessions/s.jsonl"
touch -d '10 minutes ago' "$TMP/parity-py-sessions/s.jsonl"
printf '2026-07-01T00:00:00Z\topen\t%s\t%s\tparity-py\t-\n' "$TMP/repo" "$parity_py_wt" > "$TMP/parity-py-jobs.log"
if CODEX_SESSIONS="$TMP/parity-py-sessions" "$ROOT/adapters/codex/bin/dispatch-liveness.py" "$TMP/parity-py-jobs.log" >/tmp/parity_py_10m.out 2>/tmp/parity_py_10m.err \
  && grep -q '^ALIVE    parity-py ' /tmp/parity_py_10m.out; then
  ok "dispatch-liveness.py: ~10m transcript is ALIVE under default STALE_MIN=15"
else
  bad "dispatch-liveness.py should report ~10m transcript ALIVE under default STALE_MIN=15"
fi

touch -d '20 minutes ago' "$AGENT_HOME/projects/$parity_sh_enc/s.jsonl"
if DISPATCH_RUNTIME_ROOT="$AGENT_HOME" "$ROOT/utilities/dispatch-liveness.sh" "$TMP/parity-sh-jobs.log" >/tmp/parity_sh_20m.out 2>/tmp/parity_sh_20m.err; then
  bad "dispatch-liveness.sh should exit non-zero for a ~20m transcript under default STALE_MIN=15"
else
  rc=$?
  if [ "$rc" -eq 3 ] && grep -q 'SUSPECT  parity-sh  ' /tmp/parity_sh_20m.out; then
    ok "dispatch-liveness.sh: ~20m transcript is SUSPECT under default STALE_MIN=15"
  else
    bad "dispatch-liveness.sh should report ~20m transcript SUSPECT under default STALE_MIN=15"
  fi
fi

touch -d '20 minutes ago' "$TMP/parity-py-sessions/s.jsonl"
if CODEX_SESSIONS="$TMP/parity-py-sessions" "$ROOT/adapters/codex/bin/dispatch-liveness.py" "$TMP/parity-py-jobs.log" >/tmp/parity_py_20m.out 2>/tmp/parity_py_20m.err; then
  bad "dispatch-liveness.py should exit non-zero for a ~20m transcript under default STALE_MIN=15"
else
  rc=$?
  if [ "$rc" -eq 3 ] && grep -q '^SUSPECT  parity-py ' /tmp/parity_py_20m.out; then
    ok "dispatch-liveness.py: ~20m transcript is SUSPECT under default STALE_MIN=15"
  else
    bad "dispatch-liveness.py should report ~20m transcript SUSPECT under default STALE_MIN=15"
  fi
fi
if "$CODEX" mcp >/tmp/codex_mcp.out 2>/tmp/codex_mcp.err \
  && grep -q '^adapter=codex$' /tmp/codex_mcp.out \
  && grep -q '^runtime_surface=codex-native-mcp$' /tmp/codex_mcp.out \
  && grep -q '^mcp_surface=codex mcp$' /tmp/codex_mcp.out \
  && grep -q '^design_mcp_projection=policy-not-adopted-approval-gated$' /tmp/codex_mcp.out \
  && grep -q '^design_mcp_registration=stdio-mcp_servers.design-node-server.js$' /tmp/codex_mcp.out \
  && grep -q '^claude_settings_mcp=unsupported$' /tmp/codex_mcp.out; then
  ok "codex mcp wrapper reports native MCP contract"
else
  bad "codex mcp wrapper should report native MCP contract"
fi
if "$CODEX" mcp --check >/tmp/codex_mcp_check.out 2>/tmp/codex_mcp_check.err \
  && grep -q '^check=ok$' /tmp/codex_mcp_check.out; then
  ok "codex mcp wrapper checks native MCP CLI"
else
  bad "codex mcp wrapper should check native MCP CLI"
fi
if "$CODEX" ui-info >/tmp/codex_ui.out 2>/tmp/codex_ui.err \
  && grep -q '^adapter=codex$' /tmp/codex_ui.out \
  && grep -q '^runtime_surface=codex-native-ui-boundary$' /tmp/codex_ui.out \
  && grep -q '^statusline_surface=codex-native-footer-config$' /tmp/codex_ui.out \
  && grep -q '^statusline_custom_dynamic_fields=unsupported$' /tmp/codex_ui.out \
  && grep -q '^statusline_fragment=codex_setting/codex-config/tui-statusline.toml$' /tmp/codex_ui.out \
  && grep -q '^harness_status_surface=adapter-owned-preflight-status$' /tmp/codex_ui.out \
  && grep -q '^autopilot_entrypoints=codex-native-skills$' /tmp/codex_ui.out \
  && grep -q '^autopilot_auto_routing=instruction-guided-not-claude-slash-router$' /tmp/codex_ui.out \
  && grep -q '^subagent_auto_spawn=explicit-or-main-dispatched$' /tmp/codex_ui.out \
  && grep -q '^subagent_feature_check=adapters/codex/bin/preflight.sh subagent-info --check$' /tmp/codex_ui.out; then
  ok "codex ui-info reports native UI and parity boundaries"
else
  bad "codex ui-info should report native UI and parity boundaries"
fi
if "$CODEX" subagent-info >/tmp/codex_subagent_info.out 2>/tmp/codex_subagent_info.err \
  && grep -q '^runtime_surface=codex-native-subagents$' /tmp/codex_subagent_info.out \
  && grep -q '^feature=multi_agent$' /tmp/codex_subagent_info.out \
  && grep -q '^trigger=explicit-user-request-or-main-dispatch$' /tmp/codex_subagent_info.out \
  && grep -q '^claude_subagent_frontmatter=unsupported$' /tmp/codex_subagent_info.out; then
  ok "codex subagent-info reports native subagent contract"
else
  bad "codex subagent-info should report native subagent contract"
fi
if "$CODEX" subagent-info --check >/tmp/codex_subagent_check.out 2>/tmp/codex_subagent_check.err \
  && grep -q '^check=ok$' /tmp/codex_subagent_check.out \
  && grep -q '^feature=multi_agent$' /tmp/codex_subagent_check.out; then
  ok "codex subagent-info checks native multi-agent feature"
else
  bad "codex subagent-info should check native multi-agent feature"
fi
TUIHOME="$TMP/codex-tui-home"
rm -rf "$TUIHOME"; mkdir -p "$TUIHOME"
cat > "$TUIHOME/config.toml" <<'EOF'
model = "keep-me"

[tui]
status_line = ["old"]

[hooks.state]
"example" = "keep"
EOF
if AGENT_HOME="$ROOT" CODEX_HOME="$TUIHOME" "$CODEX" tui-config >/tmp/codex_tui.out 2>/tmp/codex_tui.err \
  && grep -q '^status=ok$' /tmp/codex_tui.out \
  && grep -q '^changed=yes$' /tmp/codex_tui.out \
  && grep -Fq 'status_line = ["project-name", "git-branch", "context-used", "current-dir", "model-with-reasoning", "five-hour-limit", "weekly-limit"]' "$TUIHOME/config.toml" \
  && grep -Fq 'status_line_use_colors = true' "$TUIHOME/config.toml" \
  && grep -Fq 'model = "keep-me"' "$TUIHOME/config.toml" \
  && grep -Fq '[hooks.state]' "$TUIHOME/config.toml" \
  && [ -f "$TUIHOME/config.toml.pre-harness-tui" ]; then
  ok "codex tui-config applies only harness statusline keys"
else
  bad "codex tui-config should apply only harness statusline keys"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$TUIHOME" "$CODEX" tui-config >/tmp/codex_tui2.out 2>/tmp/codex_tui2.err \
  && grep -q '^changed=no$' /tmp/codex_tui2.out; then
  ok "codex tui-config is idempotent"
else
  bad "codex tui-config should be idempotent"
fi
if "$CODEX" loop-info oncall >/tmp/codex_loop_oncall.out 2>/tmp/codex_loop_oncall.err \
  && grep -q '^adapter=codex$' /tmp/codex_loop_oncall.out \
  && grep -q '^loop=oncall$' /tmp/codex_loop_oncall.out \
  && grep -q '^source=loops/oncall.md$' /tmp/codex_loop_oncall.out \
  && grep -q '^status=manual-contract$' /tmp/codex_loop_oncall.out \
  && grep -q '^runtime_surface=codex-loop-guidance$' /tmp/codex_loop_oncall.out \
  && grep -q '^executable_projection=unsupported-runtime-script$' /tmp/codex_loop_oncall.out; then
  ok "codex loop wrapper reports oncall manual contract"
else
  bad "codex loop wrapper should report oncall manual contract"
fi
if "$CODEX" loop-info drill >/tmp/codex_loop_drill.out 2>/tmp/codex_loop_drill.err \
  && grep -q '^source=loops/drill/README.md$' /tmp/codex_loop_drill.out \
  && grep -q '^status=manual-contract$' /tmp/codex_loop_drill.out \
  && grep -q '^trigger=manual-only$' /tmp/codex_loop_drill.out \
  && grep -q '^auto_run=unsupported$' /tmp/codex_loop_drill.out \
  && grep -q '^fallback=report-drill-would-be-useful$' /tmp/codex_loop_drill.out; then
  ok "codex loop wrapper prevents automatic drill execution"
else
  bad "codex loop wrapper should prevent automatic drill execution"
fi
if "$CODEX" loop-info study >/tmp/codex_loop_study.out 2>/tmp/codex_loop_study.err \
  && grep -q '^source=loops/study.md$' /tmp/codex_loop_study.out \
  && grep -q '^status=manual-contract$' /tmp/codex_loop_study.out \
  && grep -q '^action=proposal-report-only$' /tmp/codex_loop_study.out \
  && grep -q '^fallback=read-source-and-draft-proposal-in-main-session$' /tmp/codex_loop_study.out; then
  ok "codex loop wrapper reports study proposal contract"
else
  bad "codex loop wrapper should report study proposal contract"
fi
if "$CODEX" loop-info note >/tmp/codex_loop_note.out 2>/tmp/codex_loop_note.err \
  && grep -q '^loop=note$' /tmp/codex_loop_note.out \
  && grep -q '^status=unsupported$' /tmp/codex_loop_note.out \
  && grep -q '^runtime_surface=missing-native-loop$' /tmp/codex_loop_note.out \
  && grep -q '^related_extension=artifact-sink$' /tmp/codex_loop_note.out \
  && grep -q '^native_extension_surface=application-owned-plugin$' /tmp/codex_loop_note.out \
  && grep -q '^scheduler_surface=external-application$' /tmp/codex_loop_note.out \
  && grep -q '^fallback=extension-unavailable-or-application-scheduler$' /tmp/codex_loop_note.out; then
  ok "codex loop wrapper marks missing note loop unsupported"
else
  bad "codex loop wrapper should mark missing note loop unsupported"
fi
if AGENT_MODEL_FAST=fast-model AGENT_REASONING_FAST=low "$CODEX" role fast reviewer >/tmp/role.out 2>/tmp/role.err \
  && grep -q '^family=fast$' /tmp/role.out \
  && grep -q '^adapter=codex$' /tmp/role.out \
  && grep -q '^source=roles/README.md$' /tmp/role.out \
  && grep -q '^model=fast-model$' /tmp/role.out \
  && grep -q '^reasoning=low$' /tmp/role.out; then
  ok "codex role wrapper maps fast portable role"
else
  bad "codex role wrapper should map fast portable role"
fi
# 재홈 2026-07-22 (CONVENTIONS §2.3): pipeline-stage aliases resolve deterministically to
# portable ROLES with unit-catalog guidance; retired team profiles/paths must not reappear.
if "$CODEX" role planning >/tmp/codex_role_planning.out 2>/tmp/codex_role_planning.err \
  && grep -q '^pipeline_stage=planning$' /tmp/codex_role_planning.out \
  && grep -q '^portable_model_role=deep maker$' /tmp/codex_role_planning.out \
  && grep -q '^unit_catalog=roles/units/$' /tmp/codex_role_planning.out \
  && ! grep -q '^role_profile=' /tmp/codex_role_planning.out \
  && ! grep -q '^native_agent_path=' /tmp/codex_role_planning.out \
  && ! grep -q 'team' /tmp/codex_role_planning.out \
  && "$CODEX" role implementation >/tmp/codex_role_impl.out 2>/tmp/codex_role_impl.err \
  && grep -q '^pipeline_stage=implementation$' /tmp/codex_role_impl.out \
  && grep -q '^portable_model_role=fast implementer$' /tmp/codex_role_impl.out \
  && "$CODEX" role verification >/tmp/codex_role_verify.out 2>/tmp/codex_role_verify.err \
  && grep -q '^pipeline_stage=verification$' /tmp/codex_role_verify.out \
  && grep -q '^portable_model_role=variable reviewer$' /tmp/codex_role_verify.out \
  && grep -q '^role_set=fast reviewer,deep reviewer,external adversary$' /tmp/codex_role_verify.out \
  && "$CODEX" role report >/tmp/codex_role_report.out 2>/tmp/codex_role_report.err \
  && grep -q '^pipeline_stage=report$' /tmp/codex_role_report.out \
  && grep -q '^portable_model_role=fast writer$' /tmp/codex_role_report.out; then
  ok "codex role wrapper maps pipeline stages to portable unit-catalog roles"
else
  bad "codex role wrapper should map pipeline stages to portable unit-catalog roles"
fi
if "$CODEX" role plan team >/tmp/codex_role_retired.out 2>/tmp/codex_role_retired.err; then
  bad "codex role wrapper should reject the retired 'plan team' profile alias"
else
  ok "codex role wrapper rejects the retired 'plan team' profile alias"
fi
if "$CODEX" role variable reviewer >/tmp/role_set.out 2>/tmp/role_set.err \
  && grep -q '^role=variable reviewer$' /tmp/role_set.out \
  && grep -q '^family=role-set$' /tmp/role_set.out \
  && grep -q '^role_set=fast reviewer,deep reviewer,external adversary$' /tmp/role_set.out \
  && grep -q '^reasoning=select-by-mode$' /tmp/role_set.out \
  && "$CODEX" role 'deep maker plus fast tool worker' >/tmp/role_set_material.out 2>/tmp/role_set_material.err \
  && grep -q '^role_set=deep maker,fast tool worker$' /tmp/role_set_material.out; then
  ok "codex role wrapper reports mixed role sets"
else
  bad "codex role wrapper should report mixed role sets"
fi
if AGENT_MODEL_ORCHESTRATOR=orchestrator-model AGENT_REASONING_ORCHESTRATOR=medium "$CODEX" role external adversary orchestrator >/tmp/role.out 2>/tmp/role.err \
  && grep -q '^family=balanced$' /tmp/role.out \
  && grep -q '^adapter=codex$' /tmp/role.out \
  && grep -q '^model=orchestrator-model$' /tmp/role.out \
  && grep -q '^reasoning=medium$' /tmp/role.out \
  && grep -q '^available=1$' /tmp/role.out \
  && grep -q '^status=configured$' /tmp/role.out; then
  ok "codex role wrapper maps external adversary orchestrator role"
else
  bad "codex role wrapper should map external adversary orchestrator role"
fi
if "$CODEX" role external adversary >/tmp/role.out 2>/tmp/role.err \
  && grep -q '^available=0$' /tmp/role.out \
  && grep -q '^status=unavailable$' /tmp/role.out \
  && grep -q '^reason=set AGENT_MODEL_EXTERNAL or AGENT_EXTERNAL_CMD for an independent external adversary$' /tmp/role.out; then
  ok "codex role wrapper marks external adversary unavailable by default"
else
  bad "codex role wrapper should mark external adversary unavailable by default"
fi
if AGENT_MODEL_EXTERNAL=external-model AGENT_REASONING_EXTERNAL=high "$CODEX" role external adversary >/tmp/role.out 2>/tmp/role.err \
  && grep -q '^family=external$' /tmp/role.out \
  && grep -q '^available=1$' /tmp/role.out \
  && grep -q '^status=configured$' /tmp/role.out \
  && grep -q '^model=external-model$' /tmp/role.out \
  && grep -q '^reasoning=high$' /tmp/role.out; then
  ok "codex role wrapper maps configured external adversary model"
else
  bad "codex role wrapper should map configured external adversary model"
fi
if AGENT_EXTERNAL_CMD="sh -c" "$CODEX" role external adversary >/tmp/role.out 2>/tmp/role.err \
  && grep -q '^available=1$' /tmp/role.out \
  && grep -q '^status=configured$' /tmp/role.out \
  && grep -q '^model=external-command$' /tmp/role.out \
  && grep -q '^external_command=sh -c$' /tmp/role.out; then
  ok "codex role wrapper accepts external adversary command with args"
else
  bad "codex role wrapper should accept external adversary command with args"
fi
if AGENT_EXTERNAL_CMD="missing-external-adversary-command --review" "$CODEX" role external adversary >/tmp/role.out 2>/tmp/role.err \
  && grep -q '^available=0$' /tmp/role.out \
  && grep -q '^status=unavailable$' /tmp/role.out \
  && grep -q '^reason=AGENT_EXTERNAL_CMD not found: missing-external-adversary-command$' /tmp/role.out; then
  ok "codex role wrapper reports missing external adversary command"
else
  bad "codex role wrapper should report missing external adversary command"
fi
if "$CODEX" qa-policy adversarial code >/tmp/codex_qa_policy.out 2>/tmp/codex_qa_policy.err \
  && grep -q '^runtime_surface=codex-qa-policy$' /tmp/codex_qa_policy.out \
  && grep -q '^source=core/CONVENTIONS.md$' /tmp/codex_qa_policy.out \
  && grep -q '^qa_level=adversarial$' /tmp/codex_qa_policy.out \
  && grep -q '^qa_track=code$' /tmp/codex_qa_policy.out \
  && grep -q '^fact_checker=skip-code-track$' /tmp/codex_qa_policy.out \
  && grep -q '^external_adversary=1x-external-adversary$' /tmp/codex_qa_policy.out \
  && grep -q '^codex_role_checks=.*preflight.sh role external adversary' /tmp/codex_qa_policy.out \
  && grep -q '^independent_delegation_policy=claim-only-if-separate-codex-agent-headless-or-external-pass-ran$' /tmp/codex_qa_policy.out; then
  ok "codex qa-policy maps QA level to reviewer and fallback contract"
else
  bad "codex qa-policy should map QA level to reviewer and fallback contract"
fi
if "$OPENCODE" qa-policy adversarial code >/tmp/opencode_qa_policy.out 2>/tmp/opencode_qa_policy.err \
  && grep -q '^runtime_surface=opencode-qa-policy$' /tmp/opencode_qa_policy.out \
  && grep -q '^source=core/CONVENTIONS.md$' /tmp/opencode_qa_policy.out \
  && grep -q '^qa_level=adversarial$' /tmp/opencode_qa_policy.out \
  && grep -q '^qa_track=code$' /tmp/opencode_qa_policy.out \
  && grep -q '^fact_checker=skip-code-track$' /tmp/opencode_qa_policy.out \
  && grep -q '^external_adversary=1x-external-adversary$' /tmp/opencode_qa_policy.out \
  && grep -q '^opencode_role_checks=.*preflight.sh role external adversary' /tmp/opencode_qa_policy.out \
  && grep -q '^stage_graph_selector=intensity-not-qa$' /tmp/opencode_qa_policy.out \
  && grep -q '^independent_delegation_policy=claim-only-if-separate-opencode-agent-headless-or-external-pass-ran$' /tmp/opencode_qa_policy.out; then
  ok "opencode qa-policy maps QA level to reviewer and fallback contract"
else
  bad "opencode qa-policy should map QA level to reviewer and fallback contract"
fi
if "$CODEX" capability-info autopilot-code >/tmp/cap.out 2>/tmp/cap.err \
  && grep -q '^capability=autopilot-code$' /tmp/cap.out \
  && grep -q '^adapter=codex$' /tmp/cap.out \
  && grep -q '^native_skill=1$' /tmp/cap.out \
  && grep -q '^native_skill_path=adapters/codex/skills/autopilot-code/SKILL.md$' /tmp/cap.out \
  && grep -q '^realization=codex-native-skill$' /tmp/cap.out \
  && grep -q '^compat_reference=not-projected$' /tmp/cap.out \
  && ! grep -q '^compat_reference=skills/' /tmp/cap.out \
  && grep -q '^status=instruction-only$' /tmp/cap.out \
  && grep -q '^pipeline_contract=code-plan>code-execute>code-test>code-report$' /tmp/cap.out \
  && grep -q '^optional_pipeline_step=code-refine$' /tmp/cap.out \
  && grep -q '^artifact_contract=plans/<date>_<slug>:plan.md,checklist.md,pipeline_summary.md,dev_logs/,test_logs/$' /tmp/cap.out \
  && grep -q '^role_contract=planning=plan/plan-author,plan-check=qa/plan-review,implementation=dev/\*,impl-review=qa/code-review,verification=qa/test,report=editorial/report$' /tmp/cap.out \
  && grep -Fq 'dispatch_contract=preflight.sh dispatch --capability autopilot-code --capability-mode <mode> [--worker-mode <family/mode>] --qa <level> --intensity <level> --dispatch-depth 1|2 [--parent <slug>]' /tmp/cap.out; then
  ok "codex capability wrapper reports native skill realization"
else
  bad "codex capability wrapper should report native skill realization"
fi
if "$CODEX" capability-info code-test >/tmp/cap_code_test.out 2>/tmp/cap_code_test.err \
  && grep -q '^capability=code-test$' /tmp/cap_code_test.out \
  && grep -q '^native_skill=1$' /tmp/cap_code_test.out \
  && grep -q '^status=tool-contract$' /tmp/cap_code_test.out \
  && grep -q '^tool_contract=verification-runner$' /tmp/cap_code_test.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh verification-runner --check -- <command>$' /tmp/cap_code_test.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/cap_code_test.out \
  && grep -q '^artifact_contract=plans/<date>_<slug>:test_logs/,_internal/test_reviews/;handoff=code-report$' /tmp/cap_code_test.out \
  && grep -Fq '`code-report` alone updates `pipeline_summary.md`' "$ROOT/capabilities/code-test.md" \
  && grep -q '^role_contract=verification=qa/test,review=qa/code-review$' /tmp/cap_code_test.out \
  && grep -q 'graduated verification' "$ROOT/adapters/codex/skills/code-test/SKILL.md"; then
  ok "codex code-test capability reports verification-runner contract"
else
  bad "codex code-test capability should report verification-runner contract"
fi
if "$CODEX" capability-info design-review >/tmp/cap.out 2>/tmp/cap.err \
  && grep -q '^capability=design-review$' /tmp/cap.out \
  && grep -q '^native_skill=1$' /tmp/cap.out \
  && grep -q '^realization=codex-native-skill$' /tmp/cap.out \
  && grep -q '^status=tool-contract$' /tmp/cap.out \
  && grep -q '^tool_contract=visual-harness$' /tmp/cap.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh visual-harness <file.html>$' /tmp/cap.out \
  && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/cap.out \
  && grep -q '^fallback=preflight.sh visual-harness <file.html>$' /tmp/cap.out; then
  ok "codex design capability reports visual harness contract"
else
  bad "codex design capability should report visual harness contract"
fi
if "$CODEX" visual-harness >/tmp/codex_visual.out 2>/tmp/codex_visual.err; then
  if grep -q '^adapter=codex$' /tmp/codex_visual.out \
    && grep -q '^status=tool-contract$' /tmp/codex_visual.out \
    && grep -q '^tool_contract=visual-harness$' /tmp/codex_visual.out \
    && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/codex_visual.out \
    && ! grep -q 'adapters/claude\|claude_setting\|settings.json\|statusline.sh' /tmp/codex_visual.out; then
    ok "codex visual harness reports adapter-native tool-contract"
  else
    bad "codex visual harness should report adapter-native tool-contract"
  fi
else
  bad "codex visual harness should report adapter-native tool-contract"
fi
cat >"$TMP/codex-preview.html" <<'EOF'
<!doctype html><html><body><h1>Codex visual harness</h1></body></html>
EOF
if "$CODEX" visual-harness "$TMP/codex-preview.html" --out "$TMP/codex-visual" >/tmp/codex_visual_file.out 2>/tmp/codex_visual_file.err; then
  if grep -q '^adapter=codex$' /tmp/codex_visual_file.out \
    && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/codex_visual_file.out \
    && grep -q '^status=ok$' /tmp/codex_visual_file.out \
    && grep -q '^console_errors=0$' /tmp/codex_visual_file.out \
    && [ -f "$TMP/codex-visual/codex-preview-html.png" ]; then
    ok "codex visual harness renders HTML when checker dependencies exist"
  else
    bad "codex visual harness should render HTML when checker dependencies exist"
  fi
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^adapter=codex$' /tmp/codex_visual_file.out \
    && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/codex_visual_file.out \
    && grep -q '^status=tool-contract$' /tmp/codex_visual_file.out \
    && grep -q '^reason=playwright-unavailable$' /tmp/codex_visual_file.out; then
    ok "codex visual harness reports unavailable checker dependency"
  else
    bad "codex visual harness should render or report unavailable checker dependency"
  fi
fi
cat >"$TMP/codex-data-script.py" <<'EOF'
import sys

print("rows=3")
print("args=" + ",".join(sys.argv[1:]))
EOF
if "$CODEX" data-script --check "$TMP/codex-data-script.py" >/tmp/codex_data_script.out 2>/tmp/codex_data_script.err \
  && grep -q '^adapter=codex$' /tmp/codex_data_script.out \
  && grep -q '^tool_contract=data-script$' /tmp/codex_data_script.out \
  && grep -q '^runtime_surface=adapter-owned-data-script$' /tmp/codex_data_script.out \
  && grep -q '^check=python-compile$' /tmp/codex_data_script.out \
  && grep -q '^status=ok$' /tmp/codex_data_script.out; then
  ok "codex data-script wrapper checks Python analysis scripts"
else
  bad "codex data-script wrapper should check Python analysis scripts"
fi
if "$CODEX" claim-verify >/tmp/codex_claim_verify.out 2>/tmp/codex_claim_verify.err \
  && grep -q '^adapter=codex$' /tmp/codex_claim_verify.out \
  && grep -q '^tool_contract=external-claim-verification$' /tmp/codex_claim_verify.out \
  && grep -q '^runtime_surface=adapter-owned-claim-verify$' /tmp/codex_claim_verify.out \
  && grep -q '^status=tool-contract$' /tmp/codex_claim_verify.out; then
  ok "codex claim-verify wrapper reports tool contract"
else
  bad "codex claim-verify wrapper should report tool contract"
fi
if "$CODEX" claim-verify --check "model X is state of the art" >/tmp/codex_claim_unavailable.out 2>/tmp/codex_claim_unavailable.err; then
  bad "codex claim-verify wrapper should report unavailable provider by default"
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^adapter=codex$' /tmp/codex_claim_unavailable.out \
    && grep -q '^reason=claim-verify-provider-unavailable$' /tmp/codex_claim_unavailable.out; then
    ok "codex claim-verify wrapper reports unavailable provider"
  else
    bad "codex claim-verify wrapper should report unavailable provider"
  fi
fi
if "$CODEX" figure-gen >/tmp/codex_figure_gen.out 2>/tmp/codex_figure_gen.err \
  && grep -q '^adapter=codex$' /tmp/codex_figure_gen.out \
  && grep -q '^tool_contract=figure-gen$' /tmp/codex_figure_gen.out \
  && grep -q '^runtime_surface=adapter-owned-figure-gen$' /tmp/codex_figure_gen.out \
  && grep -q '^status=tool-contract$' /tmp/codex_figure_gen.out; then
  ok "codex figure-gen wrapper reports tool contract"
else
  bad "codex figure-gen wrapper should report tool contract"
fi
if "$CODEX" figure-gen --check "$TMP/missing-figure.py" >/tmp/codex_figure_missing.out 2>/tmp/codex_figure_missing.err; then
  bad "codex figure-gen wrapper should fail missing script"
else
  rc=$?
  if [ "$rc" -eq 66 ] \
    && grep -q '^adapter=codex$' /tmp/codex_figure_missing.out \
    && grep -q '^reason=file-not-found$' /tmp/codex_figure_missing.out; then
    ok "codex figure-gen wrapper reports missing script"
  else
    bad "codex figure-gen wrapper should report missing script"
  fi
fi
if "$CODEX" browser-fetch >/tmp/codex_browser_fetch.out 2>/tmp/codex_browser_fetch.err \
  && grep -q '^adapter=codex$' /tmp/codex_browser_fetch.out \
  && grep -q '^tool_contract=browser-fetch$' /tmp/codex_browser_fetch.out \
  && grep -q '^runtime_surface=adapter-owned-browser-fetch$' /tmp/codex_browser_fetch.out \
  && grep -q '^status=tool-contract$' /tmp/codex_browser_fetch.out; then
  ok "codex browser-fetch wrapper reports tool contract"
else
  bad "codex browser-fetch wrapper should report tool contract"
fi
if "$CODEX" browser-fetch --check not-a-url >/tmp/codex_browser_bad_url.out 2>/tmp/codex_browser_bad_url.err; then
  bad "codex browser-fetch wrapper should fail bad URL"
else
  rc=$?
  if [ "$rc" -eq 65 ] \
    && grep -q '^adapter=codex$' /tmp/codex_browser_bad_url.out \
    && grep -q '^reason=bad-url$' /tmp/codex_browser_bad_url.out; then
    ok "codex browser-fetch wrapper reports bad URL"
  else
    bad "codex browser-fetch wrapper should report bad URL"
  fi
fi
if "$CODEX" pdf-extract >/tmp/codex_pdf_extract.out 2>/tmp/codex_pdf_extract.err \
  && grep -q '^adapter=codex$' /tmp/codex_pdf_extract.out \
  && grep -q '^tool_contract=pdf-extract$' /tmp/codex_pdf_extract.out \
  && grep -q '^runtime_surface=adapter-owned-pdf-extract$' /tmp/codex_pdf_extract.out \
  && grep -q '^status=tool-contract$' /tmp/codex_pdf_extract.out; then
  ok "codex pdf-extract wrapper reports tool contract"
else
  bad "codex pdf-extract wrapper should report tool contract"
fi
if "$CODEX" pdf-extract --check "$TMP/missing.pdf" >/tmp/codex_pdf_missing.out 2>/tmp/codex_pdf_missing.err; then
  bad "codex pdf-extract wrapper should fail missing PDF"
else
  rc=$?
  if [ "$rc" -eq 66 ] \
    && grep -q '^adapter=codex$' /tmp/codex_pdf_missing.out \
    && grep -q '^reason=file-not-found$' /tmp/codex_pdf_missing.out; then
    ok "codex pdf-extract wrapper reports missing PDF"
  else
    bad "codex pdf-extract wrapper should report missing PDF"
  fi
fi
if "$CODEX" web-image-search >/tmp/codex_web_image.out 2>/tmp/codex_web_image.err \
  && grep -q '^adapter=codex$' /tmp/codex_web_image.out \
  && grep -q '^tool_contract=web-image-search$' /tmp/codex_web_image.out \
  && grep -q '^runtime_surface=adapter-owned-web-image-search$' /tmp/codex_web_image.out \
  && grep -q '^status=tool-contract$' /tmp/codex_web_image.out; then
  ok "codex web-image-search wrapper reports tool contract"
else
  bad "codex web-image-search wrapper should report tool contract"
fi
if "$CODEX" web-image-search --check "speech enhancement timeline" >/tmp/codex_web_image_unavailable.out 2>/tmp/codex_web_image_unavailable.err; then
  bad "codex web-image-search wrapper should report unavailable provider by default"
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^adapter=codex$' /tmp/codex_web_image_unavailable.out \
    && grep -q '^reason=web-image-search-provider-unavailable$' /tmp/codex_web_image_unavailable.out; then
    ok "codex web-image-search wrapper reports unavailable provider"
  else
    bad "codex web-image-search wrapper should report unavailable provider"
  fi
fi
if "$CODEX" verification-runner --check -- python3 >/tmp/codex_verify_check.out 2>/tmp/codex_verify_check.err \
  && grep -q '^adapter=codex$' /tmp/codex_verify_check.out \
  && grep -q '^tool_contract=verification-runner$' /tmp/codex_verify_check.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/codex_verify_check.out \
  && grep -q '^check=command-available$' /tmp/codex_verify_check.out \
  && grep -q '^status=ok$' /tmp/codex_verify_check.out; then
  ok "codex verification runner checks explicit commands"
else
  bad "codex verification runner should check explicit commands"
fi
if "$CODEX" verification-runner --timeout 5 -- python3 -c 'print("verify-ok")' >/tmp/codex_verify_run.out 2>/tmp/codex_verify_run.err \
  && grep -q '^adapter=codex$' /tmp/codex_verify_run.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/codex_verify_run.out \
  && grep -q '^status=ok$' /tmp/codex_verify_run.out \
  && grep -q '^exit_code=0$' /tmp/codex_verify_run.out \
  && grep -q 'verify-ok' /tmp/codex_verify_run.out; then
  ok "codex verification runner executes explicit commands"
else
  bad "codex verification runner should execute explicit commands"
fi
# entry-router redesign (585c742b): the projection routes to the portable
# contract instead of inlining it -- the procedure must stay reachable through
# the referenced capability source.
if grep -q 'core/WORKFLOW.md §0.2' "$ROOT/adapters/codex/skills/autopilot-code/SKILL.md" \
  && grep -q 'capabilities/autopilot-code.md' "$ROOT/adapters/codex/skills/autopilot-code/SKILL.md" \
  && grep -q 'preflight.sh capability-info autopilot-code' "$ROOT/adapters/codex/skills/autopilot-code/SKILL.md" \
  && grep -q 'entry-router' "$ROOT/adapters/codex/skills/autopilot-code/SKILL.md" \
  && grep -q 'capabilities/autopilot-code.md' "$ROOT/adapters/codex/plugins/hearting-codex/skills/autopilot-code/SKILL.md" \
  && grep -q 'preflight.sh capability-info autopilot-code' "$ROOT/adapters/codex/plugins/hearting-codex/skills/autopilot-code/SKILL.md" \
  && grep -q 'spec-significance' "$ROOT/capabilities/autopilot-code.md" \
  && grep -q 'pipeline_summary.md' "$ROOT/capabilities/autopilot-code.md" \
  && grep -q 'code-plan' "$ROOT/capabilities/autopilot-code.md" \
  && grep -q 'code-execute' "$ROOT/capabilities/autopilot-code.md" \
  && grep -q 'code-test' "$ROOT/capabilities/autopilot-code.md" \
  && grep -q 'code-report' "$ROOT/capabilities/autopilot-code.md"; then
  ok "codex native skill projection carries portable autopilot-code procedure"
else
  bad "codex native skill projection should carry portable autopilot-code procedure"
fi
if command -v codex >/dev/null 2>&1; then
  mkdir -p "$TMP/codex_bootstrap_home"
  ln -s "$ROOT/codex_setting/AGENTS.md" "$TMP/codex_bootstrap_home/AGENTS.md"
  if CODEX_HOME="$TMP/codex_bootstrap_home" codex debug prompt-input 'bootstrap check' >/tmp/codex_bootstrap.out 2>/tmp/codex_bootstrap.err \
    && grep -q 'AGENTS.md — Codex Adapter Bootstrap' /tmp/codex_bootstrap.out \
    && grep -q 'adapters/codex/bin/preflight.sh capability-info' /tmp/codex_bootstrap.out \
    && grep -q 'codex_setting/codex-hooks' /tmp/codex_bootstrap.out \
    && ! grep -q 'adapters/claude/CLAUDE.md.*portable bootstrap' /tmp/codex_bootstrap.out; then
    ok "codex bootstrap projection is discoverable without Claude bootstrap"
  else
    bad "codex bootstrap projection should be discoverable without Claude bootstrap"
  fi
else
  ok "codex bootstrap runtime discovery skipped (codex not installed)"
fi
if command -v codex >/dev/null 2>&1; then
  mkdir -p "$TMP/codex_home/skills"
  for d in "$ROOT"/codex_setting/codex-skills/*; do
    [ -d "$d" ] || continue
    ln -s "$d" "$TMP/codex_home/skills/$(basename "$d")"
  done
  if CODEX_HOME="$TMP/codex_home" codex debug prompt-input 'autopilot-code' >/tmp/codex_skills.out 2>/tmp/codex_skills.err \
    && grep -q -- '- autopilot-code:' /tmp/codex_skills.out \
    && grep -q 'Use when source code must be implemented' /tmp/codex_skills.out \
    && ! grep -q '/.claude/skills' /tmp/codex_skills.out; then
    ok "codex native skill projection is discoverable without Claude skill paths"
  else
    bad "codex native skill projection should be discoverable without Claude skill paths"
  fi
else
  ok "codex native skill runtime discovery skipped (codex not installed)"
fi
if command -v codex >/dev/null 2>&1; then
  mkdir -p "$TMP/codex_plugin_home"
  if [ -f "$ROOT/codex_setting/codex-plugin-marketplace/.agents/plugins/marketplace.json" ] \
    && [ -L "$ROOT/codex_setting/codex-plugin-marketplace/plugins/hearting-codex" ] \
    && [ ! -e "$ROOT/codex_setting/codex-plugin-marketplace/bin" ] \
    && [ ! -e "$ROOT/codex_setting/codex-plugin-marketplace/hooks" ] \
    && CODEX_HOME="$TMP/codex_plugin_home" codex plugin marketplace add "$ROOT/codex_setting/codex-plugin-marketplace" --json >/tmp/codex_plugin_marketplace.out 2>/tmp/codex_plugin_marketplace.err \
    && CODEX_HOME="$TMP/codex_plugin_home" codex plugin list --available --json >/tmp/codex_plugin_list.out 2>/tmp/codex_plugin_list.err \
    && grep -q '"pluginId": "hearting-codex@hearting"' /tmp/codex_plugin_list.out \
    && CODEX_HOME="$TMP/codex_plugin_home" codex plugin add hearting-codex@hearting --json >/tmp/codex_plugin_add.out 2>/tmp/codex_plugin_add.err \
    && CODEX_HOME="$TMP/codex_plugin_home" codex debug prompt-input 'autopilot-code' >/tmp/codex_plugin_prompt.out 2>/tmp/codex_plugin_prompt.err \
    && grep -q -- '- hearting-codex:autopilot-code:' /tmp/codex_plugin_prompt.out \
    && ! grep -q 'adapters/claude/skills' /tmp/codex_plugin_prompt.out; then
    ok "codex native plugin projection is installable and discovers generated skills"
  else
    bad "codex native plugin projection should be installable and discover generated skills"
  fi
else
  ok "codex native plugin runtime discovery skipped (codex not installed)"
fi
mkdir -p "$TMP/codex_agent_home/agents"
for f in "$ROOT"/codex_setting/codex-agents/*.toml; do
  [ -f "$f" ] || continue
  ln -s "$f" "$TMP/codex_agent_home/agents/$(basename "$f")"
done
if python3 - "$TMP/codex_agent_home/agents" >/tmp/codex_agents.out 2>/tmp/codex_agents.err <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
agents = sorted(root.glob("*.toml"))
# The current native catalog carries the three general worker profiles plus the
# kernel memory scout. Keep this list exact so stale or accidental agents fail.
expected = ["deep.toml", "general-purpose.toml", "light.toml", "memory-scout.toml"]
if [a.name for a in agents] != expected:
    raise SystemExit(f"expected {expected}, got {[a.name for a in agents]}")
for agent in agents:
    body = agent.read_text(encoding="utf-8")
    for key in ("name", "description"):
        if not re.search(rf'^{key} = "[^"]+"$', body, re.MULTILINE):
            raise SystemExit(f"{agent.name}: missing {key}")
    for key in ("model", "model_reasoning_effort", "sandbox_mode"):
        if not re.search(rf'^{key} = "[^"]+"$', body, re.MULTILINE):
            raise SystemExit(f"{agent.name}: missing {key}")
    if not re.search(r'^developer_instructions = """\n.+\n"""$', body, re.MULTILINE | re.DOTALL):
        raise SystemExit(f"{agent.name}: missing developer_instructions")
    forbidden = ("adapters/claude/agents", "claude_setting", "adapters/opencode", "opencode_setting")
    if any(item in body for item in forbidden):
        raise SystemExit(f"{agent.name}: leaked non-Codex adapter path")
PY
then
  ok "codex native agent projection has valid custom agent TOML without Claude paths"
else
  bad "codex native agent projection should have valid custom agent TOML without Claude paths"
fi
# 재홈 2026-07-22: per-team TOML boundaries retired — role-specific runtime boundaries now
# live in unit-catalog frontmatter enforced by tools/check-unit-config.py; the kernel
# memory-scout TOML stays read-only and team TOMLs must not reappear in the projection.
if python3 "$ROOT/tools/check-unit-config.py" >/tmp/codex_unit_config.out 2>/tmp/codex_unit_config.err \
  && grep -q '^read_only: true$' "$ROOT/roles/units/qa/test.md" \
  && grep -q '^worker_type: review$' "$ROOT/roles/units/qa/test.md" \
  && grep -q '^read_only: true$' "$ROOT/roles/units/qa/security-review.md" \
  && grep -q '^read_only: false$' "$ROOT/roles/units/dev/backend.md" \
  && grep -q '^worker_type: stage$' "$ROOT/roles/units/dev/backend.md" \
  && grep -q 'sandbox_mode = "read-only"' "$TMP/codex_agent_home/agents/memory-scout.toml" \
  && [ -z "$(find "$TMP/codex_agent_home/agents" -name '*-team.toml' -print -quit)" ]; then
  ok "codex native agent projection enforces role-specific runtime boundaries"
else
  bad "codex native agent projection should encode role-specific runtime boundaries"
fi
# 재홈 2026-07-22: Codex role-map input lists retired — mixed role sets now live in unit
# frontmatter role fields (portable role names resolved via per-adapter models.conf).
if grep -q '^role: fast reviewer$' "$ROOT/roles/units/qa/test.md" \
  && grep -q '^role: deep reviewer$' "$ROOT/roles/units/qa/security-review.md" \
  && grep -q '^role: fast fact-checker$' "$ROOT/roles/units/research/fact-check.md" \
  && grep -q '^role: deep reviewer$' "$ROOT/roles/units/research/plan-review.md" \
  && grep -q '^role: deep maker$' "$ROOT/roles/units/material/data-script.md" \
  && grep -q '^role: fast tool worker$' "$ROOT/roles/units/material/pdf-extract.md" \
  && grep -q '^role: deep editor$' "$ROOT/roles/units/editorial/polish.md" \
  && grep -q '^role: fast reviewer$' "$ROOT/roles/units/editorial/review.md" \
  && grep -q 'PORTABLE role name only' "$ROOT/roles/units/_schema.md"; then
  ok "codex native agent projection preserves mixed role sets"
else
  bad "codex native agent projection should preserve mixed role sets"
fi
if grep -q 'model = "gpt-5.6-luna"' "$TMP/codex_agent_home/agents/memory-scout.toml" \
  && grep -q 'model_reasoning_effort = "low"' "$TMP/codex_agent_home/agents/memory-scout.toml" \
  && grep -q 'sandbox_mode = "read-only"' "$TMP/codex_agent_home/agents/memory-scout.toml" \
  && grep -q 'Never run memory mutation commands' "$TMP/codex_agent_home/agents/memory-scout.toml"; then
  ok "codex native memory-scout projection is low-cost read-only"
else
  bad "codex native memory-scout projection should be low-cost read-only"
fi
if [ -L "$ROOT/codex_setting/codex-modes" ] \
  && [ "$(readlink "$ROOT/codex_setting/codex-modes")" = "../adapters/codex/modes" ] \
  && "$ROOT/adapters/codex/bin/sync-native-modes.py" --check >/tmp/codex_modes_sync.out 2>/tmp/codex_modes_sync.err \
  && python3 - "$ROOT" >/tmp/codex_modes.out 2>/tmp/codex_modes.err <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
import harness_manifest

# 재홈 2026-07-22: portable personas live in roles/units; codex modes are generated from
# the unit-bearing manifest modes (internal fragments like design/_design_rules have no
# projection), and shared-tool paths (tools/design-mcp) are no longer Claude-only leaks.
manifest = harness_manifest.load()
sources = sorted(
    spec["unit"]
    for spec in manifest["modes"].values()
    if isinstance(spec, dict) and spec.get("unit")
)
if not sources:
    raise SystemExit("manifest lists no unit-bearing modes")
modes = sorted((root / "codex_setting" / "codex-modes").glob("*/*.md"))
if len(modes) != len(sources):
    raise SystemExit(f"expected {len(sources)} Codex modes, got {len(modes)}")
for mode in sources:
    unit_source = root / "roles" / "units" / f"{mode}.md"
    if not unit_source.is_file():
        raise SystemExit(f"{mode}: missing unit catalog source {unit_source}")
    native = root / "codex_setting" / "codex-modes" / f"{mode}.md"
    body = native.read_text(encoding="utf-8")
    required = [
        f"roles/units/{mode}.md",
        f"adapters/codex/bin/preflight.sh mode-info {mode}",
        f"adapters/codex/modes/{mode}.md",
        "not a legacy runtime mode copy",
        "Projected Portable Mode Contract",
    ]
    for item in required:
        if item not in body:
            raise SystemExit(f"{mode}: missing {item}")
    forbidden = ("adapters/claude", "claude_setting", "settings.json", "statusline.sh", "CLAUDE.md", "agent-modes", "allowedTools", "Design MCP", "mcp__design__", "getConsoleLogs", "eval_js")
    if any(item in body for item in forbidden):
        raise SystemExit(f"{mode}: leaked non-Codex runtime surface")
PY
then
  ok "codex native mode projection covers portable modes without Claude paths"
else
  bad "codex native mode projection should cover portable modes without Claude paths"
fi
if [ -L "$ROOT/codex_setting/scaffolds" ] \
  && [ "$(readlink "$ROOT/codex_setting/scaffolds")" = "../adapters/codex/scaffolds" ] \
  && [ -f "$ROOT/codex_setting/scaffolds/deck_stage/deck_stage.html" ] \
  && [ -f "$ROOT/codex_setting/scaffolds/tweaks_panel/tweaks_panel.html" ] \
  && grep -q 'adapter visual harness' "$ROOT/codex_setting/scaffolds/deck_stage/deck_stage.html" \
  && cmp -s "$ROOT/scaffolds/tweaks_panel/tweaks_panel.html" "$ROOT/codex_setting/scaffolds/tweaks_panel/tweaks_panel.html" \
  && ! rg -q 'adapters/claude|claude_setting|~/.claude|Design MCP|design-mcp' "$ROOT/codex_setting/scaffolds"; then
  ok "codex scaffold projection exposes shared design assets without Claude runtime paths"
else
  bad "codex scaffold projection should expose shared design assets without Claude runtime paths"
fi
# 재홈 2026-07-22: bodies regenerate from roles/units — 5b is now a "### Level 5b" heading
# and the meta-skill row says "units" instead of the retired "modes" persona surface.
if grep -q '## Levels' "$ROOT/codex_setting/codex-modes/qa/test.md" \
  && grep -q '### Level 5b: Behavioral runtime observation' "$ROOT/codex_setting/codex-modes/qa/test.md" \
  && grep -q 'verification-runner' "$ROOT/codex_setting/codex-modes/qa/test.md" \
  && grep -q 'Tool Contract: `visual-harness`' "$ROOT/codex_setting/codex-modes/design/maker.md" \
  && grep -q 'preflight.sh visual-harness <file.html>' "$ROOT/codex_setting/codex-modes/design/maker.md" \
  && grep -q 'preflight.sh visual-harness <file.html>' "$ROOT/codex_setting/codex-modes/design/verifier.md" \
  && grep -q 'Portable capabilities, roles, units, and adapter projections' "$ROOT/codex_setting/codex-modes/research/plan-review.md"; then
  ok "codex native mode projection embeds sanitized portable mode contracts"
else
  bad "codex native mode projection should embed sanitized portable mode contracts"
fi
mkdir -p "$TMP/codex_hook_home/.codex"
ln -s "$ROOT" "$TMP/codex_hook_home/.codex/hearting"
ln -s "$ROOT/codex_setting/codex-hooks/hooks.json" "$TMP/codex_hook_home/.codex/hooks.json"
if python3 -m json.tool "$TMP/codex_hook_home/.codex/hooks.json" >/tmp/codex_hook_json.out 2>/tmp/codex_hook_json.err \
  && grep -q 'sessionstart-lifecycle.py' /tmp/codex_hook_json.out \
  && grep -q 'sessionend-lifecycle.py' /tmp/codex_hook_json.out \
  && grep -q '"Stop"' /tmp/codex_hook_json.out \
  && grep -q 'userprompt-lifecycle.py' /tmp/codex_hook_json.out \
  && grep -q 'permissionrequest-lifecycle.py' /tmp/codex_hook_json.out \
  && grep -q 'pretooluse-write-guard.py' /tmp/codex_hook_json.out \
  && grep -q 'posttooluse-read-marker.py' /tmp/codex_hook_json.out \
  && grep -q 'posttooluse-design-check.py' /tmp/codex_hook_json.out \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); h=d["hooks"]["PreToolUse"]; assert len(h)==1; assert h[0]["matcher"]==r"Write|Edit|MultiEdit|apply_patch|functions\.apply_patch|Bash|Shell|functions\.exec_command"; assert "AGENT_PARENT_PARK_ONLY=1" not in h[0]["hooks"][0]["command"]; assert "stop-lifecycle.py" in d["hooks"]["Stop"][0]["hooks"][0]["command"]' "$TMP/codex_hook_home/.codex/hooks.json" \
  && printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/f" "$TMP/repo" \
    | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_hook.out 2>/tmp/codex_hook.err \
  && [ ! -s /tmp/codex_hook.out ]; then
  ok "codex native hook projection bridges clean writes to preflight"
else
  bad "codex native hook projection should bridge clean writes to preflight"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"printf x > %s"},"session_id":"shellwritesid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/SHELL.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_write_hook.out 2>/tmp/codex_shell_write_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_write_hook.out; then
  ok "codex native hook projection blocks obvious shell write targets"
else
  bad "codex native hook projection should block obvious shell write targets"
fi

# A3: the Codex shell PreToolUse bridge runs worktree-path before material-route.
if printf '{"tool_name":"Bash","tool_input":{"command":"git worktree add /tmp/somewhere-else/slug"},"session_id":"codex-worktree-deny","cwd":"%s"}\n' "$TMP/repo" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_worktree_deny.out 2>/tmp/codex_worktree_deny.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"' /tmp/codex_worktree_deny.out; then
  ok "codex native hook projection denies git worktree add outside <repo>-wt/"
else
  bad "codex native hook projection should deny git worktree add outside <repo>-wt/"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"git worktree add %s-wt/slug -b slug HEAD"},"session_id":"codex-worktree-pass","cwd":"%s"}\n' "$TMP/repo" "$TMP/repo" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_worktree_pass.out 2>/tmp/codex_worktree_pass.err \
  && [ ! -s /tmp/codex_worktree_pass.out ]; then
  ok "codex native hook projection passes canonical <repo>-wt/ worktree add"
else
  bad "codex native hook projection should pass canonical <repo>-wt/ worktree add [out=$(cat /tmp/codex_worktree_pass.out)]"
fi

# Material-route Codex projection fixtures: denial must be native JSON, while a
# successful local compile/bind remains silent and is scoped to session + cwd.
codex_source="$TMP/repo/source.py"
printf 'print(1)\n' > "$codex_source"
git -C "$TMP/repo" add "$codex_source"
no_route_write=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"codex-no-route","cwd":"%s"}\n' "$codex_source" "$TMP/repo" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py")
if python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["decision"]=="block"; assert "material-route" in d["reason"] or "route" in d["reason"]' "$no_route_write"; then
  ok "codex Write without material route returns native JSON denial"
else
  bad "codex Write without material route should return native JSON denial [$no_route_write]"
fi
patch_payload=$(python3 - "$codex_source" "$TMP/repo" <<'PY'
import json,sys
print(json.dumps({"tool_name":"functions.apply_patch","input":f"*** Begin Patch\n*** Update File: {sys.argv[1]}\n@@\n-print(1)\n+print(2)\n*** End Patch\n","session_id":"codex-patch-no-route","cwd":sys.argv[2]}))
PY
)
patch_decision=$(printf '%s\n' "$patch_payload" | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py")
if python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["decision"]=="block"' "$patch_decision"; then
  ok "qualified functions.apply_patch without route is denied"
else
  bad "qualified functions.apply_patch without route should be denied [$patch_decision]"
fi
commit_decision=$(printf '%s\n' "{\"tool_name\":\"functions.exec_command\",\"input\":{\"command\":\"git commit -am source\"},\"session_id\":\"codex-commit-no-route\",\"cwd\":\"$TMP/repo\"}" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py")
if python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["decision"]=="block"' "$commit_decision"; then
  ok "source-bearing functions.exec_command commit is denied"
else
  bad "source-bearing functions.exec_command commit should be denied [$commit_decision]"
fi
codex_route="$TMP/repo/.agent_reports/.runtime/routes/codex-route.json"
codex_compile="$ROOT/adapters/codex/bin/preflight.sh route --capability autopilot-code --capability-mode dev --intensity direct --cwd $TMP/repo --artifact-root $TMP/repo/.agent_reports --predicate atomic-outcome --predicate known-scope --predicate no-shared-contract --predicate no-resource-run --predicate no-artifact-handoff --predicate no-independent-verifier --predicate focused-verification --tracking untracked --spec-read not-applicable --drift-verdict no-project-spec --workflow-mode untracked --artifact-guard preflight-passed --inline-reason atomic-direct --output $codex_route"
mkdir -p "$(dirname "$codex_route")"
recall_opportunity "$TMP/repo" codex-bind
if eval "$codex_compile" >"$TMP/codex_compile.out" 2>"$TMP/codex_compile.err" \
  && printf '%s\n' "{\"tool_name\":\"functions.exec_command\",\"input\":{\"command\":\"$codex_compile\"},\"session_id\":\"codex-bind\",\"cwd\":\"$TMP/repo\"}" \
  | AGENT_HOME="$ROOT" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >"$TMP/codex_bind.out" 2>"$TMP/codex_bind.err" \
  && [ ! -s "$TMP/codex_bind.out" ] && [ ! -s "$TMP/codex_bind.err" ] \
  && printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"codex-bind","cwd":"%s"}\n' "$codex_source" "$TMP/repo" \
    | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >"$TMP/codex_bind_allow.out" 2>"$TMP/codex_bind_allow.err" \
  && [ ! -s "$TMP/codex_bind_allow.out" ] && [ ! -s "$TMP/codex_bind_allow.err" ]; then
  ok "trusted local Codex compile binds silently and same cwd/session allows Write"
else
  bad "trusted local Codex compile should bind silently and allow same cwd/session [route=$(test -f "$codex_route" && echo yes || echo no) bind_out=$(cat "$TMP/codex_bind.out") bind_err=$(cat "$TMP/codex_bind.err") allow_out=$(cat "$TMP/codex_bind_allow.out") allow_err=$(cat "$TMP/codex_bind_allow.err")]"
fi
foreign_bind=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"codex-foreign","cwd":"%s"}\n' "$codex_source" "$TMP/repo" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py")
if python3 -c 'import json,sys; assert json.loads(sys.argv[1])["decision"]=="block"' "$foreign_bind"; then
  ok "foreign session cannot reuse trusted Codex material route"
else
  bad "foreign session should be denied [$foreign_bind]"
fi
codex_bind_marker_hash=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(b"material-route-session-v1\0" + sys.argv[1].encode()).hexdigest())' codex-bind)
codex_bind_marker="$ROOT/.route-grounding/$codex_bind_marker_hash.json"
printf '{"hook_event_name":"Stop","session_id":"codex-bind","cwd":"%s"}\n' "$TMP/repo" \
  | MEM_STORE="$TMP/codex_hook_mem_stop" AGENT_HOME="$ROOT" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/stop-lifecycle.py" >/tmp/codex_stop_retention.out 2>/tmp/codex_stop_retention.err || true
if [ -f "$codex_bind_marker" ] && [ ! -s /tmp/codex_stop_retention.out ] && [ ! -s /tmp/codex_stop_retention.err ]; then
  ok "Codex Stop is a silent no-op and retains material route marker"
else
  bad "Codex Stop should be a silent no-op that retains material route marker"
fi
if printf '{"hook_event_name":"SessionEnd","session_id":"codex-bind","cwd":"%s"}\n' "$TMP/repo" \
  | MEM_STORE="$TMP/codex_hook_mem" AGENT_HOME="$ROOT" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/sessionend-lifecycle.py" >"$TMP/codex_sessionend.out" 2>"$TMP/codex_sessionend.err" \
  && [ ! -s "$TMP/codex_sessionend.out" ] \
  && [ ! -e "$codex_bind_marker" ] \
  && ! grep -q 'material-route\|route-guard' "$TMP/codex_sessionend.err"; then
  ok "Codex SessionEnd clears material route without clear-path output"
else
  bad "Codex SessionEnd should clear the exact material marker without clear-path output [marker=$(test -e "$codex_bind_marker" && echo present || echo absent) out=$(cat "$TMP/codex_sessionend.out") err=$(cat "$TMP/codex_sessionend.err")]"
fi
if printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"parked","cwd":"%s"}\n' "$codex_source" "$TMP/repo" \
  | AGENT_PARENT_PARK_ONLY=1 HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_parent_park_only.out 2>/tmp/codex_parent_park_only.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "route" in d["reason"]' /tmp/codex_parent_park_only.out \
  && [ ! -s /tmp/codex_parent_park_only.err ]; then
  ok "retired parent-park-only marker cannot bypass the material guard"
else
  bad "retired parent-park-only marker must not bypass the material guard"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"printf x | tee %s"},"session_id":"shellteesid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/TEE.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_tee_hook.out 2>/tmp/codex_shell_tee_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_tee_hook.out \
  && printf '{"tool_name":"Bash","tool_input":{"command":"rm %s"},"session_id":"shellrmsid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/RM.md" "$TMP/runtime" \
    | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_rm_hook.out 2>/tmp/codex_shell_rm_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_rm_hook.out; then
  ok "codex native hook projection blocks common shell mutation targets"
else
  bad "codex native hook projection should block common shell mutation targets"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"cp %s %s"},"session_id":"shellcpsourcesid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/SOURCE.md" "$TMP/repo/copied-source.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_cp_source_hook.out 2>/tmp/codex_shell_cp_source_hook.err \
  && [ ! -s /tmp/codex_shell_cp_source_hook.out ] \
  && printf '{"tool_name":"Bash","tool_input":{"command":"cp %s %s"},"session_id":"shellcpdestsid","cwd":"%s"}\n' "$TMP/repo/source.md" "$TMP/runtime/projects/abc/memory/COPIED.md" "$TMP/runtime" \
    | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_cp_dest_hook.out 2>/tmp/codex_shell_cp_dest_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_cp_dest_hook.out; then
  ok "codex native hook projection treats cp destination as the shell write target"
else
  bad "codex native hook projection should treat cp destination as the shell write target"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"install %s %s"},"session_id":"shellinstallsid","cwd":"%s"}\n' "$TMP/repo/source.md" "$TMP/runtime/projects/abc/memory/INSTALLED.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_install_hook.out 2>/tmp/codex_shell_install_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_install_hook.out \
  && printf '{"tool_name":"Bash","tool_input":{"command":"rsync %s %s"},"session_id":"shellrsyncsid","cwd":"%s"}\n' "$TMP/repo/source.md" "$TMP/runtime/projects/abc/memory/RSYNCED.md" "$TMP/runtime" \
    | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_rsync_hook.out 2>/tmp/codex_shell_rsync_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_rsync_hook.out; then
  ok "codex native hook projection blocks install and rsync destinations"
else
  bad "codex native hook projection should block install and rsync destinations"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"dd if=%s of=%s"},"session_id":"shellddsid","cwd":"%s"}\n' "$TMP/repo/source.md" "$TMP/runtime/projects/abc/memory/DD.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_dd_hook.out 2>/tmp/codex_shell_dd_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_dd_hook.out \
  && printf '{"tool_name":"Bash","tool_input":{"command":"sed -i s/a/b/ %s"},"session_id":"shellsedisid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/SED.md" "$TMP/runtime" \
    | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_shell_sedi_hook.out 2>/tmp/codex_shell_sedi_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["decision"]=="block"; assert "memory" in d["reason"].lower() or "기억" in d["reason"]' /tmp/codex_shell_sedi_hook.out; then
  ok "codex native hook projection blocks dd output and sed inline edits"
else
  bad "codex native hook projection should block dd output and sed inline edits"
fi
if printf '{"tool":"Write","input":{"path":"%s"},"session_id":"nestedpayloadsid","cwd":"%s"}\n' "$TMP/repo/nested-f" "$TMP/repo" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_hook_nested.out 2>/tmp/codex_hook_nested.err \
  && [ ! -s /tmp/codex_hook_nested.out ]; then
  ok "codex native hook projection accepts string-tool nested input payloads"
else
  bad "codex native hook projection should accept string-tool nested input payloads"
fi
codex_hook_command=$(python3 - "$TMP/codex_hook_home/.codex/hooks.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
PY
)
if printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/f" "$TMP/repo" \
  | AGENT_HOME="$ROOT" HOME="$TMP/no-codex-home" sh -c "$codex_hook_command" >/tmp/codex_hook_agent_home.out 2>/tmp/codex_hook_agent_home.err \
  && [ ! -s /tmp/codex_hook_agent_home.out ]; then
  ok "codex hook command resolves harness through AGENT_HOME"
else
  bad "codex hook command should resolve harness through AGENT_HOME"
fi
if printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/f" "$TMP/repo" \
  | AGENT_HOME="$TMP/not-agent-home" HOME="$TMP/codex_hook_home" sh -c "$codex_hook_command" >/tmp/codex_hook_invalid_agent_home.out 2>/tmp/codex_hook_invalid_agent_home.err \
  && [ ! -s /tmp/codex_hook_invalid_agent_home.out ]; then
  ok "codex hook command ignores invalid AGENT_HOME"
else
  bad "codex hook command should ignore invalid AGENT_HOME"
fi
if (cd "$TMP/repo" && MEM_STORE="$TMP/codex_hook_mem" python3 "$ROOT/tools/memory/mem.py" add durable thread "세션 시작 기억 주입 확인: Codex SessionStart bridge는 mem inject 결과를 hookSpecificOutput additionalContext로 전달해야 한다" >/tmp/codex_session_seed.out 2>/tmp/codex_session_seed.err) \
  && printf '{"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo" \
  | MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/sessionstart-lifecycle.py" >/tmp/codex_session_hook_default.out 2>/tmp/codex_session_hook_default.err \
  && [ ! -s /tmp/codex_session_hook_default.out ] \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_session_hook_default.out /tmp/codex_session_hook_default.err; then
  ok "codex native hook projection keeps session start context silent by default"
else
  bad "codex native hook projection should keep session start context silent by default"
fi
if printf '{"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo" \
  | CODEX_SESSION_MEMORY_INJECT=1 MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/sessionstart-lifecycle.py" >/tmp/codex_session_hook.out 2>/tmp/codex_session_hook.err \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); out=d["hookSpecificOutput"]; assert out["hookEventName"]=="SessionStart"; assert "세션 시작 기억 주입 확인" in out["additionalContext"]' /tmp/codex_session_hook.out \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_session_hook.out /tmp/codex_session_hook.err; then
  ok "codex native hook projection can opt into session start memory context"
else
  bad "codex native hook projection should opt into session start memory context"
fi
if printf '{"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo" \
  | MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/sessionend-lifecycle.py" >/tmp/codex_session_end_hook.out 2>/tmp/codex_session_end_hook.err \
  && [ ! -s /tmp/codex_session_end_hook.out ] \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_session_end_hook.out /tmp/codex_session_end_hook.err; then
  ok "codex native hook projection bridges session end lifecycle with silent success output"
else
  bad "codex native hook projection should bridge session end lifecycle with silent success output"
fi
codex_stop_command=$(python3 - "$TMP/codex_hook_home/.codex/hooks.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data["hooks"]["Stop"][0]["hooks"][0]["command"])
PY
)
if printf '{"session_id":"stopsid","cwd":"%s"}\n' "$TMP/repo" \
  | MEM_STORE="$TMP/codex_hook_mem_stop" HOME="$TMP/codex_hook_home" sh -c "$codex_stop_command" >/tmp/codex_stop_hook.out 2>/tmp/codex_stop_hook.err \
  && [ ! -s /tmp/codex_stop_hook.out ] \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_stop_hook.out /tmp/codex_stop_hook.err; then
  ok "codex native hook projection detaches Stop session end lifecycle with silent success output"
else
  bad "codex native hook projection should detach Stop session end lifecycle with silent success output"
fi
if git check-ignore -q "$ROOT/adapters/claude/loops/oncall.log"; then
  ok "adapter loop runtime logs are ignored"
else
  bad "adapter loop runtime logs should be ignored"
fi
if printf '{"prompt":"plain prompt","session_id":"promptlifecyclesid","cwd":"%s"}\n' "$TMP/flowproj" \
  | MEM_NUDGE_INTERVAL=100 MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/userprompt-lifecycle.py" >/tmp/codex_prompt_hook_tracked.out 2>/tmp/codex_prompt_hook_tracked.err \
  && [ ! -s /tmp/codex_prompt_hook_tracked.out ] \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_prompt_hook_tracked.out /tmp/codex_prompt_hook_tracked.err; then
  ok "codex native hook projection injects no workflow-mode banner (retired)"
else
  bad "codex native hook projection should not inject a workflow-mode banner"
fi
budget_sid=12345678-1234-1234-1234-123456789abc
budget_rollout="$TMP/codex_hook_home/.codex/sessions/2026/07/13/rollout-test-$budget_sid.jsonl"
mkdir -p "$(dirname "$budget_rollout")" "$TMP/codex_budget_state"
printf '%s\n' '{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"total_tokens":82000},"total_token_usage":{"input_tokens":120000,"cached_input_tokens":70000,"output_tokens":20000,"reasoning_output_tokens":10000,"total_tokens":150000},"model_context_window":112000}}}' > "$budget_rollout"
budget_preflight="$TMP/codex_hook_home/.codex/hearting/adapters/codex/bin/preflight.sh"
if CODEX_HOME="$TMP/codex_hook_home/.codex" "$budget_preflight" token-budget "$TMP/flowproj" "$budget_sid" kv >/tmp/codex_budget_kv.out 2>/tmp/codex_budget_kv.err \
  && grep -q '^active_context_tokens=82000$' /tmp/codex_budget_kv.out \
  && grep -q '^session_total_tokens=150000$' /tmp/codex_budget_kv.out \
  && grep -q '^policy_state=tight$' /tmp/codex_budget_kv.out; then
  ok "codex token-budget preflight separates active context and cumulative session counters"
else
  bad "codex token-budget preflight should expose exact-session telemetry"
fi
if printf '{"prompt":"plain prompt","session_id":"%s","cwd":"%s"}\n' "$budget_sid" "$TMP/flowproj" \
  | CODEX_HOME="$TMP/codex_hook_home/.codex" XDG_STATE_HOME="$TMP/codex_budget_state" MEM_NUDGE_INTERVAL=100 MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/userprompt-lifecycle.py" >/tmp/codex_budget_hook_first.out 2>/tmp/codex_budget_hook_first.err \
  && grep -q 'TOKEN_BUDGET=tight' /tmp/codex_budget_hook_first.out \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); out=d["hookSpecificOutput"]; assert out["hookEventName"]=="UserPromptSubmit"; ctx=out["additionalContext"]; line=[x for x in ctx.splitlines() if x.startswith("TOKEN_BUDGET=")]; assert len(line)==1; assert len((line[0]+"\n").encode()) <= 240; assert "required work" in line[0] and "tests" in line[0] and "input context unchanged" in line[0]' /tmp/codex_budget_hook_first.out \
  && printf '{"prompt":"plain prompt","session_id":"%s","cwd":"%s"}\n' "$budget_sid" "$TMP/flowproj" \
  | CODEX_HOME="$TMP/codex_hook_home/.codex" XDG_STATE_HOME="$TMP/codex_budget_state" MEM_NUDGE_INTERVAL=100 MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/userprompt-lifecycle.py" >/tmp/codex_budget_hook_repeat.out 2>/tmp/codex_budget_hook_repeat.err \
  && [ ! -s /tmp/codex_budget_hook_repeat.out ]; then
  ok "codex prompt hook injects token budget only on pressure-band transition"
else
  bad "codex prompt hook should keep same-band token budget reinjection at zero bytes"
fi
codex_accounting_dir="$TMP/codex_budget_state/agent-harness/token-budget/accounting"
if CODEX_HOME="$TMP/codex_hook_home/.codex" XDG_STATE_HOME="$TMP/codex_budget_state" "$budget_preflight" token-budget "$TMP/flowproj" "$budget_sid" kv >"$TMP/codex_budget_accounting_kv.out" 2>"$TMP/codex_budget_accounting_kv.err" \
  && grep -q '^accounting.hook_invocations=2$' "$TMP/codex_budget_accounting_kv.out" \
  && python3 - "$codex_accounting_dir" "$budget_sid" <<'PY'
import hashlib
import json
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(sys.argv[2].encode()).hexdigest()[:32]
path = directory / f"{digest}.json"
raw = path.read_text(encoding="utf-8")
data = json.loads(raw)
assert path.stat().st_size <= 8192
assert data["session_digest"] == digest and sys.argv[2] not in raw
assert data["hook_invocations"] == data["zero_injections"] + data["emissions"] == 2
assert data["emissions"] == 1 and data["zero_reason_counts"]["same_band"] == 1
assert data["directive_utf8_bytes_total"] == 154
assert data["observed_session_token_samples"] == 2
assert data["observed_session_token_delta_monotonic"] == 0
assert "TOKEN_BUDGET=" not in raw
assert "directive_exact_tokens" not in data
PY
then
  ok "codex prompt hook records exact content-free accounting and exposes it read-only in kv"
else
  bad "codex prompt hook should record bounded exact accounting without diagnostic reinjection"
fi
if printf '{"prompt":"remember this project context","session_id":"promptlifecyclesid","cwd":"%s"}\n' "$TMP/flowproj" \
  | MEM_NUDGE_INTERVAL=1 MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/userprompt-lifecycle.py" >/tmp/codex_prompt_hook.out 2>/tmp/codex_prompt_hook.err \
  && [ ! -s /tmp/codex_prompt_hook.out ] \
  && grep -q '^0$' "$TMP/codex_hook_mem/.codex-turn-state-promptlifecyclesid" \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_prompt_hook.out /tmp/codex_prompt_hook.err; then
  ok "codex native hook projection resets the turn-nudge counter on every prompt"
else
  bad "codex native hook projection should reset the turn-nudge counter on every prompt"
fi
if printf '{"context":{"cwd":"%s","session_id":"permissionsid"}}\n' "$TMP/flowproj" \
  | FLEET_INTERACTION_STATE_DIR="$TMP/codex-interactions" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/permissionrequest-lifecycle.py" >/tmp/codex_permission_hook.out 2>/tmp/codex_permission_hook.err \
  && [ ! -s /tmp/codex_permission_hook.out ] \
  && [ -f "$TMP/codex-interactions/codex/permissionsid.json" ] \
  && grep -q '"session_id":"permissionsid"' "$TMP/codex-interactions/codex/permissionsid.json" \
  && grep -q '"kind":"approval"' "$TMP/codex-interactions/codex/permissionsid.json" \
  && grep -q '"source":"codex-permissionrequest"' "$TMP/codex-interactions/codex/permissionsid.json" \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_permission_hook.out /tmp/codex_permission_hook.err; then
  ok "codex native hook projection publishes a silent privacy-minimal approval wait"
else
  bad "codex native hook projection should publish a silent exact-session approval wait"
fi
if (cd "$TMP/flowproj" && MEM_STORE="$TMP/codex_hook_mem" python3 "$ROOT/tools/memory/mem.py" add durable thread "지난번 결정론 우선 설계가 핵심이라고 배웠다" >/tmp/codex_nested_prompt_seed.out 2>/tmp/codex_nested_prompt_seed.err) \
  && printf '{"input":{"messages":[{"role":"user","content":[{"type":"text","text":"지난번 결정론 내용을 다시 확인"}]}]},"session_id":"nestedpromptsid","cwd":"%s"}\n' "$TMP/flowproj" \
  | MEM_NUDGE_INTERVAL=100 MEM_STORE="$TMP/codex_hook_mem" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/userprompt-lifecycle.py" >/tmp/codex_nested_prompt_hook.out 2>/tmp/codex_nested_prompt_hook.err \
  && ! grep -q '우선 설계가 핵심' /tmp/codex_nested_prompt_hook.out; then
  ok "codex native prompt hook does not classify nested message content for recall"
else
  bad "codex native prompt hook should leave semantic recall to the agent"
fi
if (cd "$TMP/flowproj" && MEM_STORE="$TMP/codex_hook_mem" python3 "$ROOT/tools/memory/mem.py" add durable decision \
  "This durable record keeps direct-body-marker outside prompt context" \
  --headline "Deterministic recall capsule" --alias "deterministic recall" >/tmp/codex_direct_prompt_seed.out 2>/tmp/codex_direct_prompt_seed.err) \
  && printf '{"prompt":"deterministic recall","session_id":"directpromptsid","turn_id":"directturnid","cwd":"%s"}\n' "$TMP/flowproj" \
  | MEM_NUDGE_INTERVAL=100 MEM_STORE="$TMP/codex_hook_mem" MEM_RECALL_RECEIPTS="$MEM_RECALL_RECEIPTS" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/userprompt-lifecycle.py" >/tmp/codex_direct_prompt_hook.out 2>/tmp/codex_direct_prompt_hook.err \
  && grep -q 'Deterministic recall capsule' /tmp/codex_direct_prompt_hook.out \
  && ! grep -q 'direct-body-marker' /tmp/codex_direct_prompt_hook.out; then
  ok "codex native prompt hook exposes bounded capsule candidates without record bodies"
else
  bad "codex native prompt hook should expose capsule candidates and keep bodies private"
fi
mkdir -p "$TMP/repo/.agent_reports/spec" "$TMP/codex_marker_home"
printf 'prd\n' > "$TMP/repo/.agent_reports/spec/prd.md"
if printf '{"tool_name":"Read","tool_input":{"file_path":"%s"},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/.agent_reports/spec/prd.md" "$TMP/repo" \
  | AGENT_HOME="$TMP/codex_marker_home" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >/tmp/codex_read_hook.out 2>/tmp/codex_read_hook.err \
  && find "$TMP/codex_marker_home/.spec-grounding" -type f -name 'testsid__*' -print -quit | grep -q . \
  && ! grep -q 'adapters/claude\|claude_setting\|statusline.sh' /tmp/codex_read_hook.out /tmp/codex_read_hook.err; then
  ok "codex native hook projection records spec read markers"
else
  bad "codex native hook projection should record spec read markers"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"cat .agent_reports/spec/prd.md"},"session_id":"shellreadsid","cwd":"%s"}\n' "$TMP/repo" \
  | AGENT_HOME="$TMP/codex_marker_home" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >/tmp/codex_shell_read_hook.out 2>/tmp/codex_shell_read_hook.err \
  && find "$TMP/codex_marker_home/.spec-grounding" -type f -name 'shellreadsid__*' -print -quit | grep -q .; then
  ok "codex native read hook marks obvious shell spec reads"
else
  bad "codex native read hook should mark obvious shell spec reads"
fi
mkdir -p "$TMP/repo/core"
printf 'core\n' > "$TMP/repo/core/MEMORY.md"
if printf '{"tool_name":"Read","tool_input":{"file_path":"%s"},"session_id":"corereadsid","cwd":"%s"}\n' "$TMP/repo/core/MEMORY.md" "$TMP/repo" \
  | AGENT_HOME="$TMP/codex_marker_home" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >/tmp/codex_core_read_hook.out 2>/tmp/codex_core_read_hook.err \
  && find "$TMP/codex_marker_home/.core-grounding" -type f -name 'corereadsid__*' -print -quit | grep -q .; then
  ok "codex native hook projection records core read markers"
else
  bad "codex native hook projection should record core read markers"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"cat core/MEMORY.md"},"session_id":"shellcorereadsid","cwd":"%s"}\n' "$TMP/repo" \
  | AGENT_HOME="$TMP/codex_marker_home" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >/tmp/codex_shell_core_read_hook.out 2>/tmp/codex_shell_core_read_hook.err \
  && find "$TMP/codex_marker_home/.core-grounding" -type f -name 'shellcorereadsid__*' -print -quit | grep -q .; then
  ok "codex native read hook marks obvious shell core reads"
else
  bad "codex native read hook should mark obvious shell core reads"
fi
if printf '{"tool":{"name":"Read","input":{"path":"%s"}},"session_id":"nestedreadsid","cwd":"%s"}\n' "$TMP/repo/.agent_reports/spec/prd.md" "$TMP/repo" \
  | AGENT_HOME="$TMP/codex_marker_home" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >/tmp/codex_read_hook_nested.out 2>/tmp/codex_read_hook_nested.err \
  && find "$TMP/codex_marker_home/.spec-grounding" -type f -name 'nestedreadsid__*' -print -quit | grep -q .; then
  ok "codex native read hook accepts nested tool input payloads"
else
  bad "codex native read hook should accept nested tool input payloads"
fi
if printf '{"tool_name":"Read","tool_input":{"file_path":".agent_reports/spec/prd.md"},"session":{"id":"nestedctxreadsid"},"workspace":{"cwd":"%s"}}\n' "$TMP/repo" \
  | AGENT_HOME="$TMP/codex_marker_home" HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-read-marker.py" >/tmp/codex_read_hook_nested_context.out 2>/tmp/codex_read_hook_nested_context.err \
  && find "$TMP/codex_marker_home/.spec-grounding" -type f -name 'nestedctxreadsid__*' -print -quit | grep -q .; then
  ok "codex native read hook resolves nested cwd/session payloads"
else
  bad "codex native read hook should resolve nested cwd/session payloads"
fi
if printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"testsid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/MEMORY.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_hook_block.out 2>/tmp/codex_hook_block.err \
  && grep -q '"decision": "block"' /tmp/codex_hook_block.out \
  && grep -q 'memory' /tmp/codex_hook_block.out; then
  ok "codex native hook projection blocks guarded writes"
else
  bad "codex native hook projection should block guarded writes"
fi
if printf '{"tool_name":"Write","tool_input":{"file_path":"projects/abc/memory/NESTED.md"},"session":{"id":"nestedcontextsid"},"context":{"cwd":"%s"}}\n' "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_nested_context_block.out 2>/tmp/codex_nested_context_block.err \
  && grep -q '"decision": "block"' /tmp/codex_nested_context_block.out \
  && grep -q 'memory' /tmp/codex_nested_context_block.out; then
  ok "codex native write hook resolves nested cwd/session payloads"
else
  bad "codex native write hook should resolve nested cwd/session payloads"
fi
if printf '{"tool_name":"MultiEdit","tool_input":{"file_path":"%s","edits":[]},"session_id":"testsid","cwd":"%s"}\n' "$TMP/runtime/projects/abc/memory/MEMORY.md" "$TMP/runtime" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_multiedit_block.out 2>/tmp/codex_multiedit_block.err \
  && grep -q '"decision": "block"' /tmp/codex_multiedit_block.out \
  && grep -q 'memory' /tmp/codex_multiedit_block.out; then
  ok "codex native hook projection blocks guarded MultiEdit writes"
else
  bad "codex native hook projection should block guarded MultiEdit writes"
fi
codex_qualified_patch_payload=$(python3 - "$TMP/runtime/projects/abc/memory/PATCHED.md" "$TMP/runtime" <<'PY'
import json
import sys

print(json.dumps({
  "tool_name": "functions.apply_patch",
  "input": f"*** Begin Patch\n*** Add File: {sys.argv[1]}\n+blocked\n*** End Patch\n",
  "session_id": "testsid",
  "cwd": sys.argv[2],
}))
PY
)
if printf '%s\n' "$codex_qualified_patch_payload" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_qualified_patch_block.out 2>/tmp/codex_qualified_patch_block.err \
  && grep -q '"decision": "block"' /tmp/codex_qualified_patch_block.out \
  && grep -q 'memory' /tmp/codex_qualified_patch_block.out; then
  ok "codex native hook projection blocks qualified apply_patch writes"
else
  bad "codex native hook projection should block qualified apply_patch writes"
fi
codex_freeform_patch_payload=$(python3 - "$TMP/runtime/projects/abc/memory/FREEFORM.md" "$TMP/runtime" <<'PY'
import json
import sys

print(json.dumps({
  "tool_name": "apply_patch",
  "tool_input": f"*** Begin Patch\n*** Add File: {sys.argv[1]}\n+blocked\n*** End Patch\n",
  "session_id": "testsid",
  "cwd": sys.argv[2],
}))
PY
)
if printf '%s\n' "$codex_freeform_patch_payload" \
  | HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/pretooluse-write-guard.py" >/tmp/codex_freeform_patch_block.out 2>/tmp/codex_freeform_patch_block.err \
  && grep -q '"decision": "block"' /tmp/codex_freeform_patch_block.out \
  && grep -q 'memory' /tmp/codex_freeform_patch_block.out; then
  ok "codex native hook projection parses freeform tool_input strings"
else
  bad "codex native hook projection should parse freeform apply_patch input"
fi
mkdir -p "$TMP/repo/spec/design"
printf '<!doctype html><title>ok</title>\n' > "$TMP/repo/spec/design/preview.html"
if printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/spec/design/preview.html" "$TMP/repo" \
  | DESIGN_POSTWRITE_HOOK=0 HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-design-check.py" >/tmp/codex_design_hook.out 2>/tmp/codex_design_hook.err \
  && [ ! -s /tmp/codex_design_hook.out ] \
  && [ ! -s /tmp/codex_design_hook.err ]; then
  ok "codex native hook projection bridges design post-write checks"
else
  bad "codex native hook projection should bridge design post-write checks"
fi
if printf '{"toolUse":{"name":"Write","input":{"path":"%s"}},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/spec/design/preview.html" "$TMP/repo" \
  | DESIGN_POSTWRITE_HOOK=0 HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-design-check.py" >/tmp/codex_design_hook_nested.out 2>/tmp/codex_design_hook_nested.err \
  && [ ! -s /tmp/codex_design_hook_nested.out ] \
  && [ ! -s /tmp/codex_design_hook_nested.err ]; then
  ok "codex native design hook accepts toolUse input payloads"
else
  bad "codex native design hook should accept toolUse input payloads"
fi
if printf '{"tool_name":"MultiEdit","tool_input":{"file_path":"%s","edits":[]},"session_id":"testsid","cwd":"%s"}\n' "$TMP/repo/spec/design/preview.html" "$TMP/repo" \
  | DESIGN_POSTWRITE_HOOK=0 HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-design-check.py" >/tmp/codex_design_hook_multiedit.out 2>/tmp/codex_design_hook_multiedit.err \
  && [ ! -s /tmp/codex_design_hook_multiedit.out ] \
  && [ ! -s /tmp/codex_design_hook_multiedit.err ]; then
  ok "codex native design hook accepts MultiEdit payloads"
else
  bad "codex native design hook should accept MultiEdit payloads"
fi
if printf '{"tool_name":"Bash","tool_input":{"command":"printf %s > spec/design/preview.html"},"session_id":"testsid","cwd":"%s"}\n' "'<!doctype html><title>ok</title>'" "$TMP/repo" \
  | DESIGN_POSTWRITE_HOOK=0 HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-design-check.py" >/tmp/codex_design_hook_shell.out 2>/tmp/codex_design_hook_shell.err \
  && [ ! -s /tmp/codex_design_hook_shell.out ] \
  && [ ! -s /tmp/codex_design_hook_shell.err ] \
  && python3 - "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-design-check.py" "$TMP/repo" <<'PY'
import sys

path, cwd = sys.argv[1], sys.argv[2]
ns = {"__name__": "design_hook_test", "__file__": path}
with open(path, encoding="utf-8") as fh:
    exec(compile(fh.read(), path, "exec"), ns)
payload = {
    "tool_name": "Bash",
    "tool_input": {"command": "printf '<!doctype html>' > spec/design/preview.html"},
    "session_id": "testsid",
    "cwd": cwd,
}
assert ns["target_files"](payload) == [f"{cwd}/spec/design/preview.html"]
payload["tool_input"]["command"] = "printf '<!doctype html>' | tee spec/design/tee.html"
assert ns["target_files"](payload) == [f"{cwd}/spec/design/tee.html"]
payload["tool_input"]["command"] = "cp source.html spec/design/copied.html"
assert ns["target_files"](payload) == [f"{cwd}/spec/design/copied.html"]
payload["tool_input"]["command"] = "install source.html spec/design/installed.html"
assert ns["target_files"](payload) == [f"{cwd}/spec/design/installed.html"]
payload["tool_input"]["command"] = "rsync source.html spec/design/rsynced.html"
assert ns["target_files"](payload) == [f"{cwd}/spec/design/rsynced.html"]
payload["tool_input"]["command"] = "dd if=source.html of=spec/design/dd.html"
assert ns["target_files"](payload) == [f"{cwd}/spec/design/dd.html"]
payload["tool_input"]["command"] = "sed -i s/a/b/ spec/design/preview.html"
assert ns["target_files"](payload) == [f"{cwd}/spec/design/preview.html"]
PY
then
  ok "codex native design hook marks targeted shell design writes"
else
  bad "codex native design hook should mark targeted shell design writes"
fi
codex_design_patch_payload=$(python3 - "$TMP/repo" <<'PY'
import json
import sys

print(json.dumps({
  "tool_name": "functions.apply_patch",
  "input": "*** Begin Patch\n*** Update File: spec/design/preview.html\n@@\n <!doctype html><title>ok</title>\n*** End Patch\n",
  "session_id": "testsid",
  "cwd": sys.argv[1],
}))
PY
)
if printf '%s\n' "$codex_design_patch_payload" \
  | DESIGN_POSTWRITE_HOOK=0 HOME="$TMP/codex_hook_home" python3 "$TMP/codex_hook_home/.codex/hearting/adapters/codex/hooks/posttooluse-design-check.py" >/tmp/codex_design_hook_qualified_patch.out 2>/tmp/codex_design_hook_qualified_patch.err \
  && [ ! -s /tmp/codex_design_hook_qualified_patch.out ] \
  && [ ! -s /tmp/codex_design_hook_qualified_patch.err ]; then
  ok "codex native design hook accepts qualified apply_patch payloads"
else
  bad "codex native design hook should accept qualified apply_patch payloads"
fi
if "$CODEX" mode-info dev/backend >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=portable$' /tmp/mode.out \
  && grep -q '^realization=portable-persona$' /tmp/mode.out; then
  ok "codex mode wrapper maps portable mode"
else
  bad "codex mode wrapper should map portable mode"
fi
if "$CODEX" mode-info qa/security-review >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=portable$' /tmp/mode.out \
  && grep -q '^realization=portable-persona$' /tmp/mode.out \
  && grep -q 'read-only security review with Codex file and git diff tools' /tmp/mode.out \
  && ! grep -q '^tool_contract=' /tmp/mode.out; then
  ok "codex mode wrapper treats security-review as portable read-only guidance"
else
  bad "codex mode wrapper should treat security-review as portable read-only guidance"
fi
codex_design_modes_ok=1
# 재홈 2026-07-22: design personas re-homed to roles/units/design; _-prefixed internal
# fragments (_design-rules, _NOTES) have no mode projection.
for mode_file in "$ROOT"/roles/units/design/*.md; do
  mode_name=$(basename "$mode_file" .md)
  case "$mode_name" in _*) continue ;; esac
  mode="design/$mode_name"
  if ! "$CODEX" mode-info "$mode" >/tmp/mode.out 2>/tmp/mode.err \
    || ! grep -q '^status=tool-contract$' /tmp/mode.out \
    || ! grep -q '^realization=codex-native-mode-with-tool-contract$' /tmp/mode.out \
    || ! grep -q '^tool_contract=visual-harness$' /tmp/mode.out \
    || ! grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh visual-harness <file.html>$' /tmp/mode.out \
    || ! grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/mode.out \
    || ! grep -q "^native_mode_path=adapters/codex/modes/design/$mode_name.md$" /tmp/mode.out \
    || ! grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
    codex_design_modes_ok=0
    break
  fi
done
if [ "$codex_design_modes_ok" -eq 1 ]; then
  ok "codex mode wrapper maps design modes to native visual-harness contract"
else
  bad "codex mode wrapper should map design modes to native visual-harness contract"
fi
if "$CODEX" mode-info material/data-script >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=data-script$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh data-script --check <script.py>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-data-script$' /tmp/mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
  ok "codex mode wrapper reports material data-script contract surface"
else
  bad "codex mode wrapper should report material data-script contract surface"
fi
if "$CODEX" mode-info material/figure-gen >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=figure-gen$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh figure-gen --check <script.py>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-figure-gen$' /tmp/mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
  ok "codex mode wrapper reports material figure-gen contract surface"
else
  bad "codex mode wrapper should report material figure-gen contract surface"
fi
if "$CODEX" mode-info material/pdf-extract >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=pdf-extract$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh pdf-extract --check <file.pdf>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-pdf-extract$' /tmp/mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
  ok "codex mode wrapper reports material pdf-extract contract surface"
else
  bad "codex mode wrapper should report material pdf-extract contract surface"
fi
if "$CODEX" mode-info qa/test >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=verification-runner$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh verification-runner --check -- <command>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
  ok "codex mode wrapper reports qa test verification runner surface"
else
  bad "codex mode wrapper should report qa test verification runner surface"
fi
if "$CODEX" mode-info material/browser-fetch >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=browser-fetch$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh browser-fetch --check <url>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-browser-fetch$' /tmp/mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
  ok "codex mode wrapper reports material browser-fetch contract surface"
else
  bad "codex mode wrapper should report material browser-fetch contract surface"
fi
if "$CODEX" mode-info material/web-image-search >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=web-image-search$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh web-image-search --check <query>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-web-image-search$' /tmp/mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/mode.out; then
  ok "codex mode wrapper reports material web-image-search contract surface"
else
  bad "codex mode wrapper should report material web-image-search contract surface"
fi
if "$CODEX" mode-info research/claim-verify >/tmp/mode.out 2>/tmp/mode.err \
  && grep -q '^status=tool-contract$' /tmp/mode.out \
  && grep -q '^tool_contract=external-claim-verification$' /tmp/mode.out \
  && grep -q '^tool_contract_check=adapters/codex/bin/preflight.sh claim-verify --check <claim>$' /tmp/mode.out \
  && grep -q '^runtime_surface=adapter-owned-claim-verify$' /tmp/mode.out; then
  ok "codex mode wrapper reports named claim verification contract"
else
  bad "codex mode wrapper should report named claim verification contract"
fi
mkdir -p "$TMP/codex_sessions/2026/06/29"
cat > "$TMP/codex_sessions/2026/06/29/rollout-2026-06-29T00-00-00-codexsid.jsonl" <<'EOF'
{"timestamp":"2026-06-29T00:00:00.000Z","type":"event_msg","payload":{"type":"user_message","message":"hello"}}
{"timestamp":"2026-06-29T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"world"}]}}
{"timestamp":"2026-06-29T00:00:02.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"call_1"}}
EOF
if CODEX_SESSIONS="$TMP/codex_sessions" python3 "$ROOT/tools/memory/mem.py" distill codexsid --source codex >/tmp/codex_delta.out 2>/tmp/codex_delta.err \
  && grep -q '^\[user\] hello' /tmp/codex_delta.out \
  && grep -q '^\[assistant\] world' /tmp/codex_delta.out \
  && grep -q '^\[assistant\] \[tool:exec_command\]' /tmp/codex_delta.out; then
  ok "codex session source distills transcript"
else
  bad "codex session source should distill transcript"
fi
if "$CODEX_DISTILL" codexsid "$TMP/flowproj" >/tmp/codex_distill.out 2>/tmp/codex_distill.err \
  && [ ! -s /tmp/codex_distill.out ]; then
  ok "codex distill worker is disabled by default"
else
  bad "codex distill worker should no-op unless enabled"
fi
if "$CODEX" distill-propose codexsid "$TMP/flowproj" >/tmp/codex_distill.out 2>/tmp/codex_distill.err; then
  bad "codex distill-propose should report tool-contract until explicitly enabled"
else
  if [ "$?" -eq 69 ] \
    && grep -q '^status=tool-contract$' /tmp/codex_distill.out \
    && grep -q '^reason=distill-proposal-disabled$' /tmp/codex_distill.out \
    && grep -q '^enable=CODEX_DISTILL_ENABLE=1$' /tmp/codex_distill.out; then
    ok "codex distill-propose reports disabled tool-contract by default"
  else
    bad "codex distill-propose should exit 69 with disabled tool-contract"
  fi
fi
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/codex" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "$@" > "$CODEX_STUB_ARGV"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then
    shift
    printf '{"action":"add","tier":"working","type":"decision","body":"stub codex distill memory record","headline":"Stub distill decision","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}\n' > "$1"
  fi
  shift || break
done
exit 0
EOF
chmod +x "$TMP/stubbin/codex"
if CODEX_DISTILL_ENABLE=1 CODEX_SESSIONS="$TMP/codex_sessions" MEM_STORE="$TMP/store" \
  PATH="$TMP/stubbin:$PATH" CODEX_STUB_ARGV="$TMP/codex_argv" \
  "$CODEX" distill-propose codexsid "$TMP/flowproj" >/tmp/codex_distill.out 2>/tmp/codex_distill.err \
  && grep -q -- '--sandbox' "$TMP/codex_argv" \
  && grep -q -- 'read-only' "$TMP/codex_argv" \
  && ! grep -q -- '--ask-for-approval' "$TMP/codex_argv" \
  && grep -q -- '--ephemeral' "$TMP/codex_argv" \
  && grep -q -- '--ignore-rules' "$TMP/codex_argv" \
  && grep -q -- '--skip-git-repo-check' "$TMP/codex_argv" \
  && grep -q '"action":"add"' /tmp/codex_distill.out; then
  ok "codex distill proposal uses constrained exec"
else
  bad "codex distill proposal should use constrained exec"
fi
if CODEX_DISTILL_ENABLE=1 CODEX_DISTILL_APPLY=1 CODEX_SESSIONS="$TMP/codex_sessions" MEM_STORE="$TMP/store_apply_blocked" \
  PATH="$TMP/stubbin:$PATH" CODEX_STUB_ARGV="$TMP/codex_argv_apply" \
  "$CODEX" distill-propose codexsid "$TMP/flowproj" >/tmp/codex_distill_apply.out 2>/tmp/codex_distill_apply.err; then
  bad "codex distill apply should require accepted no-tools/action contract"
else
  [ "$?" -eq 69 ] && ok "codex distill apply requires accepted no-tools/action contract" || bad "codex distill apply wrong exit"
fi
if CODEX_DISTILL_ENABLE=1 CODEX_DISTILL_APPLY=1 CODEX_DISTILL_CONTRACT_ACCEPTED=1 CODEX_SESSIONS="$TMP/codex_sessions" MEM_STORE="$TMP/store_apply" \
  PATH="$TMP/stubbin:$PATH" CODEX_STUB_ARGV="$TMP/codex_argv_apply_accepted" \
  "$CODEX" distill-propose codexsid "$TMP/flowproj" >/tmp/codex_distill_apply.out 2>/tmp/codex_distill_apply.err \
  && MEM_STORE="$TMP/store_apply" python3 "$ROOT/tools/memory/mem.py" stats >/tmp/codex_stats.out 2>/tmp/codex_stats.err \
  && grep -q 'total: 1' /tmp/codex_stats.out; then
  ok "codex distill explicit apply works after accepted contract"
else
  bad "codex distill explicit apply should require and obey accepted contract"
fi
# session-end auto-distillation is enabled by default after the tool-free proof
if CODEX_SESSIONS="$TMP/codex_sessions" MEM_STORE="$TMP/store_session_end" \
  PATH="$TMP/stubbin:$PATH" CODEX_STUB_ARGV="$TMP/codex_argv_se" \
  "$CODEX" session-end "$TMP/flowproj" codexsid >/tmp/codex_se.out 2>/tmp/codex_se.err \
  && MEM_STORE="$TMP/store_session_end" python3 "$ROOT/tools/memory/mem.py" \
    recall 'stub codex distill memory record' --all --full --no-touch 2>/dev/null \
    | grep -q 'stub codex distill memory record'; then
  ok "codex session-end auto-distills and applies by default"
else
  bad "codex session-end should auto-distill and apply by default"
fi
# recursion guard: MEM_DISTILL=1 makes the whole session-end pipeline a no-op
if MEM_DISTILL=1 CODEX_SESSIONS="$TMP/codex_sessions" MEM_STORE="$TMP/store_session_end_guard" \
  PATH="$TMP/stubbin:$PATH" CODEX_STUB_ARGV="$TMP/codex_argv_se_guard" \
  "$CODEX" session-end "$TMP/flowproj" codexsid >/tmp/codex_se_guard.out 2>/tmp/codex_se_guard.err \
  && ! { MEM_STORE="$TMP/store_session_end_guard" python3 "$ROOT/tools/memory/mem.py" stats 2>/dev/null | grep -q 'total: 1'; }; then
  ok "codex session-end no-ops under MEM_DISTILL=1 recursion guard"
else
  bad "codex session-end must no-op under MEM_DISTILL=1 recursion guard"
fi
# D-42: every worker path returns before sync/store/model work. Test both the
# preflight defense and the native SessionEnd/UserPrompt/SessionStart bridges.
if AGENT_SESSION_ROLE=worker CODEX_SESSIONS="$TMP/codex_sessions" MEM_STORE="$TMP/store_session_end_worker" \
  PATH="$TMP/stubbin:$PATH" CODEX_STUB_ARGV="$TMP/codex_argv_se_worker" \
  "$CODEX" session-end "$TMP/flowproj" codexsid >/tmp/codex_se_worker.out 2>/tmp/codex_se_worker.err \
  && [ ! -e "$TMP/store_session_end_worker" ] \
  && [ ! -e "$TMP/codex_argv_se_worker" ]; then
  ok "codex preflight session-end no-ops before state/model work for workers"
else
  bad "codex preflight session-end must be main-session-only"
fi
if printf '{"hook_event_name":"SessionEnd","session_id":"codex-worker-hook","cwd":"%s"}\n' "$TMP/flowproj" \
  | AGENT_SESSION_ROLE=worker MEM_STORE="$TMP/store_session_end_hook_worker" \
    python3 "$ROOT/adapters/codex/hooks/sessionend-lifecycle.py" \
  && [ ! -e "$TMP/store_session_end_hook_worker" ]; then
  ok "codex native SessionEnd bridge returns silently for workers"
else
  bad "codex native SessionEnd bridge must not detach worker lifecycle"
fi
codex_worker_prompt_out="$(printf '{"hook_event_name":"UserPromptSubmit","session_id":"codex-worker-prompt","cwd":"%s"}\n' "$TMP/flowproj" \
  | AGENT_SESSION_ROLE=worker MEM_STORE="$TMP/store_prompt_worker" \
    python3 "$ROOT/adapters/codex/hooks/userprompt-lifecycle.py" 2>/tmp/codex_worker_prompt.err)"
if [ -z "$codex_worker_prompt_out" ] && [ ! -e "$TMP/store_prompt_worker" ]; then
  ok "codex native UserPromptSubmit bridge injects no main context for workers"
else
  bad "codex worker prompt bridge must be context/state free"
fi
codex_worker_start_out="$(printf '{"hook_event_name":"SessionStart","session_id":"codex-worker-start","cwd":"%s"}\n' "$TMP/flowproj" \
  | AGENT_SESSION_ROLE=worker CODEX_SESSION_MEMORY_INJECT=1 MEM_STORE="$TMP/store_start_worker" \
    python3 "$ROOT/adapters/codex/hooks/sessionstart-lifecycle.py" 2>/tmp/codex_worker_start.err)"
if [ -z "$codex_worker_start_out" ]; then
  ok "codex native SessionStart skips opt-in memory context for workers"
else
  bad "codex worker SessionStart must not inject memory context"
fi
if grep -Fq '"AGENT_SESSION_ROLE": "worker"' "$ROOT/adapters/claude/bin/dispatch-headless.py" \
  && grep -Fq '"AGENT_SESSION_ROLE": "worker"' "$ROOT/adapters/codex/bin/dispatch-headless.py" \
  && grep -Fq '"AGENT_SESSION_ROLE": "worker"' "$ROOT/adapters/opencode/bin/dispatch-headless.py" \
  && grep -Fq 'AGENT_SESSION_ROLE=worker' "$ROOT/adapters/claude/bin/mem-distill-worker.sh" \
  && grep -Fq 'AGENT_SESSION_ROLE=worker' "$ROOT/adapters/codex/bin/distill-worker.sh" \
  && grep -Fq 'AGENT_SESSION_ROLE=worker' "$ROOT/adapters/opencode/bin/distill-worker.sh" \
  && grep -Fq 'AGENT_SESSION_ROLE=worker' "$ROOT/loops/lib.sh" \
  && grep -Fq 'AGENT_SESSION_ROLE=worker' "$ROOT/loops/lib-runner.sh" \
  && grep -Fq 'env["AGENT_SESSION_ROLE"] = "worker"' "$ROOT/tools/fleet/refresh_title.py"; then
  ok "all repo-owned dispatch/title/distill/loop model launchers mark workers"
else
  bad "every background model launcher must export AGENT_SESSION_ROLE=worker"
fi
RPHOME="$TMP/codex-runtime-home"
rm -rf "$RPHOME"; mkdir -p "$RPHOME"
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$TMP/codex_rp0.out" 2>"$TMP/codex_rp0.err"; then
  bad "codex check-runtime-projection should fail on an unwired home"
else
  grep -q '^status=failed' "$TMP/codex_rp0.out" && ok "codex check-runtime-projection reports an unwired home as failed" || bad "codex check-runtime-projection unwired output wrong"
fi
# Until the §6.1-owned baseline refresh lands, accept only the exact known
# bootstrap/router/unit-catalog warning census. The count and representative
# warnings keep an unrelated footprint regression from passing silently.
if python3 "$ROOT/tools/context-footprint.py" --root "$ROOT" --skip-runtime --skip-hooks >"$TMP/context_footprint.out" 2>"$TMP/context_footprint.err" \
  && grep -q '^context_footprint_report=1' "$TMP/context_footprint.out" \
  && grep -q '^surface=codex-plugin ' "$TMP/context_footprint.out" \
  && grep -q '^surface=claude ' "$TMP/context_footprint.out" \
  && grep -q '^surface=native-bootstrap-agents ' "$TMP/context_footprint.out" \
  && grep -q '^surface=unit-catalog ' "$TMP/context_footprint.out" \
  && grep -q '^unit-family=qa ' "$TMP/context_footprint.out" \
  && ! grep -q '^surface=native-bootstrap-agent-modes' "$TMP/context_footprint.out" \
  && { grep -q '^status=ok' "$TMP/context_footprint.out" \
    || { grep -q '^status=warn warnings=19$' "$TMP/context_footprint.out" \
      && grep -q 'owner worker bootstrap 5846 > 4096 bytes' "$TMP/context_footprint.out" \
      && grep -q 'stage worker bootstrap 4734 > 4096 bytes' "$TMP/context_footprint.out" \
      && grep -q 'review worker bootstrap 4227 > 4096 bytes' "$TMP/context_footprint.out" \
      && grep -q 'support worker bootstrap 4211 > 4096 bytes' "$TMP/context_footprint.out" \
      && grep -q 'bootstrap:claude footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'bootstrap:codex footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'bootstrap:opencode footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'entry-router:canonical:max footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'entry-router:claude:max footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'entry-router:claude-plugin:max footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'entry-router:codex:max footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'entry-router:opencode:max footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'missing from context footprint baseline: unit-catalog:total' "$TMP/context_footprint.out" \
      && grep -q 'worker-bootstrap:kernel footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'worker-bootstrap:owner footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'worker-bootstrap:review footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'worker-bootstrap:stage footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'worker-bootstrap:support footprint regression' "$TMP/context_footprint.out" \
      && grep -q 'was not measured: native-bootstrap:agent-modes-total' "$TMP/context_footprint.out"; }; }; then
  ok "context-footprint reports bootstrap and skill metadata without runtime hooks"
else
  bad "context-footprint should report deterministic metadata footprint"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" "$ROOT/adapters/codex/bin/install-runtime-projection.sh" >"$TMP/codex_rp1.out" 2>"$TMP/codex_rp1.err" \
  && grep -q '^status=ok' "$TMP/codex_rp1.out" \
  && AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$TMP/codex_rp2.out" 2>"$TMP/codex_rp2.err" \
  && grep -q '^check=hearting:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=hearting-readme:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-capabilities:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-roles:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-bin:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-tools:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-utilities:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-config:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-scaffolds:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=hook-trust:skipped reason=authoritative-current-hash-check-not-requested' "$TMP/codex_rp2.out" \
  && grep -q '^check=hooks-json:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=skill-link:autopilot-code:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=skill-discovery:native' "$TMP/codex_rp2.out" \
  && grep -q '^check=skills-linked:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agent-link:memory-scout.toml:ok' "$TMP/codex_rp2.out" \
  && grep -q '^check=agents-linked:ok' "$TMP/codex_rp2.out" \
  && grep -q '^status=ok' "$TMP/codex_rp2.out"; then
  ok "codex install-runtime-projection wires the home and the checker passes"
else
  bad "codex install-runtime-projection + checker should wire and validate the runtime home"
fi
RPPLUGIN="$TMP/codex-runtime-home-plugin"
rm -rf "$RPPLUGIN"; mkdir -p "$RPPLUGIN"
if AGENT_HOME="$ROOT" CODEX_HOME="$RPPLUGIN" "$ROOT/adapters/codex/bin/install-runtime-projection.sh" --install-plugin >"$TMP/codex_rp_plugin_install.out" 2>"$TMP/codex_rp_plugin_install.err" \
  && grep -q '^skills_mode=plugin' "$TMP/codex_rp_plugin_install.out" \
  && grep -q '^skills_linked=0' "$TMP/codex_rp_plugin_install.out" \
  && [ ! -e "$RPPLUGIN/skills/autopilot-code" ] \
  && AGENT_HOME="$ROOT" CODEX_HOME="$RPPLUGIN" "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$TMP/codex_rp_plugin.out" 2>"$TMP/codex_rp_plugin.err" \
  && grep -q '^check=skill-link:autopilot-code:absent' "$TMP/codex_rp_plugin.out" \
  && grep -q '^check=skill-discovery:plugin' "$TMP/codex_rp_plugin.out" \
  && grep -q '^check=skills-linked:skipped reason=plugin-skill-discovery' "$TMP/codex_rp_plugin.out" \
  && grep -q '^check=plugin:ok' "$TMP/codex_rp_plugin.out" \
  && grep -q '^status=ok' "$TMP/codex_rp_plugin.out"; then
  ok "codex install-runtime-projection supports plugin-only skill discovery"
else
  bad "codex install-runtime-projection should support plugin-only skill discovery"
fi
RPBAD="$TMP/codex-runtime-home-bad"
rm -rf "$RPBAD"; mkdir -p "$RPBAD"
if AGENT_HOME="$ROOT" CODEX_HOME="$RPBAD" "$ROOT/adapters/codex/bin/install-runtime-projection.sh" >"$TMP/codex_rp_bad_install.out" 2>"$TMP/codex_rp_bad_install.err" \
  && ln -sfn "$TMP" "$RPBAD/skills/autopilot-code" \
  && ln -sfn "$TMP" "$RPBAD/agents/memory-scout.toml" \
  && ! AGENT_HOME="$ROOT" CODEX_HOME="$RPBAD" "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >"$TMP/codex_rp_bad.out" 2>"$TMP/codex_rp_bad.err" \
  && grep -q '^check=skill-link:autopilot-code:failed' "$TMP/codex_rp_bad.out" \
  && grep -q '^check=agent-link:memory-scout.toml:failed' "$TMP/codex_rp_bad.out" \
  && grep -q '^status=failed' "$TMP/codex_rp_bad.out"; then
  ok "codex check-runtime-projection rejects miswired skill and agent links"
else
  bad "codex check-runtime-projection should reject miswired skill and agent links"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" runtime-projection >"$TMP/codex_rp3.out" 2>"$TMP/codex_rp3.err" \
  && grep -q '^check=agent-capabilities:ok' "$TMP/codex_rp3.out" \
  && grep -q '^check=agent-tools:ok' "$TMP/codex_rp3.out" \
  && grep -q '^check=agent-config:ok' "$TMP/codex_rp3.out" \
  && grep -q '^status=ok' "$TMP/codex_rp3.out"; then
  ok "codex preflight runtime-projection validates installed runtime wiring"
else
  bad "codex preflight runtime-projection should validate installed runtime wiring"
fi
cat > "$RPHOME/config.toml" <<EOF
[hooks.state]
[hooks.state."$RPHOME/hooks.json:session_start:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:session_end:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:user_prompt_submit:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:permission_request:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:pre_tool_use:1:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:post_tool_use:0:0"]
trusted_hash = "sha256:test"
EOF
if ! AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" runtime-projection --require-hook-trust >"$TMP/codex_rp_stop_trust.out" 2>"$TMP/codex_rp_stop_trust.err" \
  && grep -q '^check=hook-trust:review-needed reason=current-hash-not-trusted$' "$TMP/codex_rp_stop_trust.out" \
  && grep -q '^status=failed' "$TMP/codex_rp_stop_trust.out"; then
  ok "codex strict runtime-projection rejects stale static hook metadata"
else
  bad "codex strict runtime-projection must use authoritative current-hash trust"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" runtime-projection --require-hook-trust >"$TMP/codex_rp_strict_missing.out" 2>"$TMP/codex_rp_strict_missing.err"; then
  bad "codex strict runtime-projection should fail when hook trust is missing"
else
  grep -q '^check=hook-trust:review-needed reason=current-hash-not-trusted$' "$TMP/codex_rp_strict_missing.out" && ok "codex strict runtime-projection requires complete current-hash trust" || bad "codex strict runtime-projection missing trust output wrong"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_REQUIRE_HOOK_TRUST=1 CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" runtime-projection >"$TMP/codex_rp_trust.out" 2>"$TMP/codex_rp_trust.err"; then
  bad "codex runtime-projection should fail when hook trust is required but missing"
else
  grep -q '^check=hook-trust:review-needed' "$TMP/codex_rp_trust.out" && ok "codex runtime-projection can require hook trust" || bad "codex runtime-projection required hook trust output wrong"
fi
cat > "$RPHOME/config.toml" <<EOF
[hooks.state]
[hooks.state."$RPHOME/hooks.json:session_start:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:user_prompt_submit:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:permission_request:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:pre_tool_use:1:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:post_tool_use:0:0"]
trusted_hash = "sha256:test"
[hooks.state."$RPHOME/hooks.json:stop:0:0"]
trusted_hash = "sha256:test"
EOF
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" runtime-projection --require-hook-trust >"$TMP/codex_rp_stop_alias.out" 2>"$TMP/codex_rp_stop_alias.err"; then
  bad "codex strict runtime-projection should not accept a static Stop alias"
elif grep -q '^check=hook-trust:review-needed reason=current-hash-not-trusted$' "$TMP/codex_rp_stop_alias.out" \
  && ! grep -q 'session_end=stop-alias' "$TMP/codex_rp_stop_alias.out"; then
  ok "codex runtime-projection does not alias Stop trust to SessionEnd"
else
  bad "codex runtime-projection should reject Stop-as-SessionEnd alias metadata"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" doctor --runtime >"$TMP/codex_doctor_runtime.out" 2>"$TMP/codex_doctor_runtime.err" \
  && grep -q '^check=runtime-projection:ok' "$TMP/codex_doctor_runtime.out" \
  && grep -q '^check=native-subagents:ok' "$TMP/codex_doctor_runtime.out" \
  && grep -q '^status=ok' "$TMP/codex_doctor_runtime.out"; then
  ok "codex doctor --runtime includes runtime projection validation"
else
  bad "codex doctor --runtime should include runtime projection validation"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT=2 "$CODEX" doctor --runtime-strict >"$TMP/codex_doctor_runtime_strict.out" 2>"$TMP/codex_doctor_runtime_strict.err"; then
  bad "codex doctor --runtime-strict should fail closed on stale hook trust"
elif grep -q '^check=runtime-projection:failed' "$TMP/codex_doctor_runtime_strict.out" \
  && grep -q '^check=native-subagents:ok' "$TMP/codex_doctor_runtime_strict.out" \
  && grep -q '^status=failed' "$TMP/codex_doctor_runtime_strict.out"; then
  ok "codex doctor --runtime-strict fails closed without current-hash trust"
else
  bad "codex doctor --runtime-strict fail-closed result is incomplete"
fi
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" "$ROOT/adapters/codex/bin/install-runtime-projection.sh" >/dev/null 2>&1 \
  && AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME" "$ROOT/adapters/codex/bin/check-runtime-projection.sh" >/dev/null 2>&1; then
  ok "codex install-runtime-projection is idempotent"
else
  bad "codex install-runtime-projection should be idempotent"
fi
RPHOME2="$TMP/codex-runtime-home2"
rm -rf "$RPHOME2"; mkdir -p "$RPHOME2"; printf '{"old":1}\n' > "$RPHOME2/hooks.json"
if AGENT_HOME="$ROOT" CODEX_HOME="$RPHOME2" "$ROOT/adapters/codex/bin/install-runtime-projection.sh" >/dev/null 2>&1 \
  && [ -f "$RPHOME2/hooks.json.pre-harness" ] && [ -L "$RPHOME2/hooks.json" ]; then
  ok "codex install-runtime-projection backs up a pre-existing hooks.json"
else
  bad "codex install-runtime-projection should back up a pre-existing hooks.json"
fi

echo "== opencode preflight wrapper =="
git -C "$TMP/repo" switch -q -c opencode-work
if "$OPENCODE" write "$TMP/repo/f" opencodesid >/tmp/opencode.out 2>/tmp/opencode.err; then
  ok "opencode preflight passes clean write"
else
  bad "opencode preflight should pass clean write"
fi
if "$OPENCODE" write "$TMP/runtime/projects/abc/memory/MEMORY.md" opencodesid >/tmp/opencode.out 2>/tmp/opencode.err; then
  bad "opencode preflight should block memory file write"
else
  [ "$?" -eq 2 ] && ok "opencode preflight blocks memory file write" || bad "opencode preflight memory wrong exit"
fi
if AGENT_HOME="$ROOT" bash "$DESIGN" --file "$TMP/not-design.txt" >/tmp/design.out 2>/tmp/design.err \
  && "$OPENCODE" design "$TMP/not-design.txt" >/tmp/design.out 2>/tmp/design.err; then
  ok "opencode design postwrite wrappers no-op on non-html"
else
  bad "opencode design postwrite wrappers should no-op on non-html"
fi
if "$OPENCODE_PROJECTION" capability-info audit >/tmp/opencode_projection.out 2>/tmp/opencode_projection.err \
  && grep -q '^capability=audit$' /tmp/opencode_projection.out \
  && grep -q '^adapter=opencode$' /tmp/opencode_projection.out; then
  ok "opencode projection preflight resolves harness root"
else
  bad "opencode projection preflight should resolve harness root"
fi
mkdir -p "$TMP/opencode_pointer_home/.config/opencode"
ln -s "$ROOT" "$TMP/opencode_pointer_home/.config/opencode/hearting"
if env -u AGENT_HOME HOME="$TMP/opencode_pointer_home" "$ROOT/adapters/opencode/utilities/agent-home.sh" >/tmp/opencode_agent_home.out 2>/tmp/opencode_agent_home.err \
  && grep -q "^$TMP/opencode_pointer_home/.config/opencode/hearting$" /tmp/opencode_agent_home.out; then
  ok "opencode agent-home wrapper resolves runtime pointer"
else
  bad "opencode agent-home wrapper should resolve runtime pointer"
fi
if AGENT_HOME="$TMP/not-agent-home" HOME="$TMP/opencode_pointer_home" "$ROOT/adapters/opencode/utilities/agent-home.sh" >/tmp/opencode_agent_home_invalid.out 2>/tmp/opencode_agent_home_invalid.err \
  && grep -q "^$TMP/opencode_pointer_home/.config/opencode/hearting$" /tmp/opencode_agent_home_invalid.out; then
  ok "opencode agent-home wrapper ignores invalid AGENT_HOME"
else
  bad "opencode agent-home wrapper should ignore invalid AGENT_HOME"
fi
mkdir -p "$TMP/opencode_linked_home/hearting/core" "$TMP/opencode_linked_home/agent_setting/core"
printf 'core\n' > "$TMP/opencode_linked_home/hearting/core/CORE.md"
printf 'legacy\n' > "$TMP/opencode_linked_home/agent_setting/core/CORE.md"
if env -u AGENT_HOME HOME="$TMP/opencode_linked_home" "$ROOT/adapters/opencode/utilities/agent-home.sh" >/tmp/opencode_agent_home_linked.out 2>/tmp/opencode_agent_home_linked.err \
  && grep -q "^$TMP/opencode_linked_home/hearting$" /tmp/opencode_agent_home_linked.out; then
  ok "opencode agent-home wrapper prefers canonical hearting checkout"
else
  bad "opencode agent-home wrapper must prefer canonical hearting checkout"
fi
rm -rf "$TMP/opencode_linked_home/hearting"
if env -u AGENT_HOME HOME="$TMP/opencode_linked_home" "$ROOT/adapters/opencode/utilities/agent-home.sh" >/tmp/opencode_agent_home_legacy.out 2>/tmp/opencode_agent_home_legacy.err \
  && grep -q "^$TMP/opencode_linked_home/agent_setting$" /tmp/opencode_agent_home_legacy.out; then
  ok "opencode agent-home wrapper retains legacy agent_setting fallback"
else
  bad "opencode agent-home wrapper must retain legacy agent_setting fallback"
fi

echo "== opencode material-route + worktree-path wrapper =="
opencode_source="$TMP/repo/opencode_source.py"
printf 'print(1)\n' > "$opencode_source"
git -C "$TMP/repo" add "$opencode_source"
if "$OPENCODE" material-route check --tool Write --file "$opencode_source" --cwd "$TMP/repo" --session opencode-no-route >/tmp/opencode_mr.out 2>/tmp/opencode_mr.err; then
  bad "opencode material-route check should block a route-less material Write"
else
  [ "$?" -eq 2 ] && ok "opencode material-route check blocks a route-less material Write" \
    || bad "opencode material-route check wrong exit for route-less material Write"
fi
if "$OPENCODE" worktree-path --tool Bash --command 'git worktree add .claude/worktrees/foo -b foo' --cwd "$TMP/repo" --session opencode-wtg >/tmp/opencode_wtp.out 2>/tmp/opencode_wtp.err; then
  bad "opencode worktree-path should deny non -wt/ worktree add"
else
  [ "$?" -eq 2 ] && ok "opencode worktree-path denies non -wt/ worktree add" \
    || bad "opencode worktree-path wrong exit on non -wt/ worktree add"
fi
if "$OPENCODE" worktree-path --tool Bash --command 'git worktree add /home/x/repo-wt/slug -b slug main' --cwd "$TMP/repo" --session opencode-wtg >/tmp/opencode_wtp.out 2>/tmp/opencode_wtp.err; then
  ok "opencode worktree-path passes canonical <repo>-wt/ add"
else
  bad "opencode worktree-path should pass canonical <repo>-wt/ add"
fi

# route wrapper: compile-then-bind (D1). Successful same-sid/cwd bind allows a
# subsequent material Write; every negative creates no marker while
# preserving the compiler's own stdout/stderr/exit status.
mkdir -p "$TMP/repo/.agent_reports/.runtime/routes"
opencode_route="$TMP/repo/.agent_reports/.runtime/routes/opencode-route.json"
opencode_route_args="--capability autopilot-code --capability-mode dev --intensity direct --cwd $TMP/repo --artifact-root $TMP/repo/.agent_reports --predicate atomic-outcome --predicate known-scope --predicate no-shared-contract --predicate no-resource-run --predicate no-artifact-handoff --predicate no-independent-verifier --predicate focused-verification --tracking untracked --spec-read not-applicable --drift-verdict no-project-spec --workflow-mode untracked --artifact-guard preflight-passed --inline-reason atomic-direct"
recall_opportunity "$TMP/repo" opencode-bind
if OPENCODE_SESSION_ID=opencode-bind "$OPENCODE" route $opencode_route_args --output "$opencode_route" >/tmp/opencode_route.out 2>/tmp/opencode_route.err \
  && [ -f "$opencode_route" ] \
  && "$OPENCODE" material-route check --tool Write --file "$opencode_source" --cwd "$TMP/repo" --session opencode-bind >/tmp/opencode_bind_allow.out 2>/tmp/opencode_bind_allow.err; then
  ok "opencode route wrapper binds on successful compile and allows same-sid/cwd Write"
else
  bad "opencode route wrapper should bind on successful compile and allow same-sid/cwd Write [route=$(test -f "$opencode_route" && echo yes || echo no)]"
fi
if "$OPENCODE" material-route check --tool Write --file "$opencode_source" --cwd "$TMP/repo" --session opencode-foreign >/tmp/opencode_foreign.out 2>/tmp/opencode_foreign.err; then
  bad "foreign session should not reuse the opencode material route marker"
else
  [ "$?" -eq 2 ] && ok "foreign session cannot reuse opencode material route marker" \
    || bad "foreign session denial wrong exit"
fi
opencode_route_nooutput="$TMP/repo/.agent_reports/.runtime/routes/opencode-route-nooutput.json"
rm -f "$opencode_route_nooutput"
if OPENCODE_SESSION_ID=opencode-neg-nooutput "$OPENCODE" route $opencode_route_args >/tmp/opencode_neg_nooutput.out 2>/tmp/opencode_neg_nooutput.err \
  && [ ! -f "$opencode_route_nooutput" ] \
  && "$OPENCODE" material-route check --tool Write --file "$opencode_source" --cwd "$TMP/repo" --session opencode-neg-nooutput >/tmp/opencode_neg_nooutput_check.out 2>/tmp/opencode_neg_nooutput_check.err; then
  bad "opencode route wrapper without --output should stay unbound"
else
  [ ! -f "$opencode_route_nooutput" ] && ok "opencode route wrapper: no --output compiles unbound, creates no marker" \
    || bad "opencode route wrapper: no --output should create no marker"
fi
opencode_route_multi_a="$TMP/repo/.agent_reports/.runtime/routes/opencode-route-multi-a.json"
opencode_route_multi_b="$TMP/repo/.agent_reports/.runtime/routes/opencode-route-multi-b.json"
if OPENCODE_SESSION_ID=opencode-neg-multi "$OPENCODE" route $opencode_route_args --output "$opencode_route_multi_a" --output "$opencode_route_multi_b" >/tmp/opencode_neg_multi.out 2>/tmp/opencode_neg_multi.err; then
  :
fi
if "$OPENCODE" material-route check --tool Write --file "$opencode_source" --cwd "$TMP/repo" --session opencode-neg-multi >/tmp/opencode_neg_multi_check.out 2>/tmp/opencode_neg_multi_check.err; then
  bad "opencode route wrapper with more than one --output should stay unbound"
else
  [ "$?" -eq 2 ] && ok "opencode route wrapper: more than one --output creates no marker" \
    || bad "opencode route wrapper: more than one --output wrong denial exit"
fi
opencode_route_nosid="$TMP/repo/.agent_reports/.runtime/routes/opencode-route-nosid.json"
rm -f "$opencode_route_nosid"
if env -u OPENCODE_SESSION_ID "$OPENCODE" route $opencode_route_args --output "$opencode_route_nosid" >/tmp/opencode_neg_nosid.out 2>/tmp/opencode_neg_nosid.err \
  && [ -f "$opencode_route_nosid" ] \
  && "$OPENCODE" material-route check --tool Write --file "$opencode_source" --cwd "$TMP/repo" --session opencode-neg-nosid >/tmp/opencode_neg_nosid_check.out 2>/tmp/opencode_neg_nosid_check.err; then
  bad "opencode route wrapper without OPENCODE_SESSION_ID should stay unbound"
else
  [ -f "$opencode_route_nosid" ] && ok "opencode route wrapper: missing sid compiles unbound, creates no marker" \
    || bad "opencode route wrapper: missing sid should still compile the route file"
fi

echo "== opencode spec read gate =="
if "$OPENCODE" read "$TMP/specproj/.agent_reports/spec/prd.md" opencodesid >/tmp/opencode.out 2>/tmp/opencode.err \
  && "$OPENCODE" capability autopilot-code "$TMP/specproj" opencodesid >/tmp/opencode.out 2>/tmp/opencode.err; then
  ok "opencode read+capability wrapper passes spec gate"
else
  bad "opencode read+capability wrapper should pass spec gate"
fi
# ungrounded spec-governed capability is hard-denied (fresh session, no prd read)
if "$OPENCODE" capability autopilot-code "$TMP/specproj" opencode-ungrounded >/tmp/opencode.out 2>/tmp/opencode.err; then
  bad "opencode capability should deny autopilot-code without prd read"
else
  [ "$?" -eq 2 ] && ok "opencode capability denies spec capability without prd read" \
    || bad "opencode capability wrong exit without prd read"
fi
# non-spec-governed capability passes even ungrounded
if "$OPENCODE" capability autopilot-research "$TMP/specproj" opencode-ungrounded >/tmp/opencode.out 2>/tmp/opencode.err; then
  ok "opencode capability allows non-spec-governed capability ungrounded"
else
  bad "opencode capability should allow non-spec-governed capability ungrounded"
fi

echo "== opencode plugin spec-gate bridge =="
# Verify the JS plugin handlers (not just the preflight CLI) wire the gate:
# command.execute.before throws (= blocks command) when ungrounded, and
# tool.execute.after on a prd.md read drops the grounding marker so it then passes.
PLUGIN="$ROOT/adapters/opencode/plugins/hearting-guards.js"
BRIDGEPROJ="$TMP/bridgeproj"
mkdir -p "$BRIDGEPROJ/.agent_reports/spec"
printf 'prd\n' > "$BRIDGEPROJ/.agent_reports/spec/prd.md"
cat > "$TMP/bridge.mjs" <<MJS
import { AgentHarnessGuards } from 'file://$PLUGIN'
const SPEC = "$BRIDGEPROJ"
const PRD = SPEC + "/.agent_reports/spec/prd.md"
const SID = "bridge-grounded", SID2 = "bridge-nonspec"
const hooks = await AgentHarnessGuards({ directory: SPEC })
async function throws(fn){ try { await fn(); return false } catch { return true } }
const denied = await throws(() => hooks["command.execute.before"]({command:"autopilot-code",sessionID:SID,arguments:""},{parts:[]}))
await hooks["tool.execute.after"]({tool:"read",sessionID:SID,callID:"1",args:{filePath:PRD}},{title:"",output:"",metadata:{}})
const passed = !(await throws(() => hooks["command.execute.before"]({command:"autopilot-code",sessionID:SID,arguments:""},{parts:[]})))
const nonspec = !(await throws(() => hooks["command.execute.before"]({command:"autopilot-research",sessionID:SID2,arguments:""},{parts:[]})))
process.stdout.write(JSON.stringify({denied,passed,nonspec}))
MJS
if command -v node >/dev/null 2>&1; then
  if node "$TMP/bridge.mjs" >/tmp/opencode_bridge.out 2>/tmp/opencode_bridge.err; then
    grep -q '"denied":true' /tmp/opencode_bridge.out \
      && ok "opencode plugin command.execute.before blocks ungrounded spec capability" \
      || bad "opencode plugin should block ungrounded spec capability"
    grep -q '"passed":true' /tmp/opencode_bridge.out \
      && ok "opencode plugin tool.execute.after marks prd read so gate passes" \
      || bad "opencode plugin read marker should let gate pass"
    grep -q '"nonspec":true' /tmp/opencode_bridge.out \
      && ok "opencode plugin command.execute.before ignores non-spec capability" \
      || bad "opencode plugin should ignore non-spec capability"
  else
    bad "opencode plugin bridge harness failed to run"
  fi
  # marker lands under the resolved harness root (.spec-grounding is gitignored); clean test sids
  rm -f "$ROOT/.spec-grounding/"*bridge-grounded* 2>/dev/null || true
else
  printf '  --  skip opencode plugin bridge (node unavailable)\n'
fi

echo "== opencode workflow lifecycle CLI =="
mkdir -p "$TMP/opencode-artifact/.agent_reports/spec"
if "$OPENCODE" write "$TMP/opencode-artifact/.agent_reports/spec/prd.md" opencodesid >/tmp/opencode_artifact.out 2>/tmp/opencode_artifact.err; then
  bad "opencode write wrapper should require a verified capability route for spec output"
else
  grep -q 'capability-artifact-route-required' /tmp/opencode_artifact.err \
    && ok "opencode write wrapper requires a verified capability route for spec output" \
    || bad "opencode write wrapper returned the wrong route failure for spec output"
fi
if "$OPENCODE" memory "$TMP/flowproj" >/tmp/opencode_mem.out 2>/tmp/opencode_mem.err; then
  ok "opencode memory wrapper exits cleanly"
else
  bad "opencode memory wrapper should exit cleanly"
fi
if MEM_STORE="$TMP/opencode_launcher_store" "$ROOT/adapters/opencode/tools/memory/mem.py" stats >/tmp/opencode_mem_launcher.out 2>/tmp/opencode_mem_launcher.err \
  && grep -q '^# store stats$' /tmp/opencode_mem_launcher.out; then
  ok "opencode memory launcher ignores invalid non-harness AGENT_HOME"
else
  bad "opencode memory launcher should fall back from invalid AGENT_HOME"
fi
if "$OPENCODE" recall "전에 결정한 내용 뭐였지" "$TMP/flowproj" >/tmp/opencode_recall.out 2>/tmp/opencode_recall.err; then
  ok "opencode recall wrapper exits cleanly"
else
  bad "opencode recall wrapper should exit cleanly"
fi
if "$OPENCODE" briefing "$TMP/flowproj" >/tmp/opencode_brief.out 2>/tmp/opencode_brief.err; then
  ok "opencode briefing wrapper exits cleanly"
else
  bad "opencode briefing wrapper should exit cleanly"
fi
if AGENT_NOTES_ROOT="$TMP/notes" CAIRN_APP="$TMP/board" CAIRN_WT="$TMP/board-wt" \
  "$OPENCODE" worklog "$TMP/flowproj" >/tmp/opencode_worklog.out 2>/tmp/opencode_worklog.err \
  && grep -q "^agent-notes-root=$TMP/notes$" /tmp/opencode_worklog.out \
  && grep -q '^note=read-only inventory;' /tmp/opencode_worklog.out; then
  ok "opencode worklog wrapper reports read-only state"
else
  bad "opencode worklog wrapper should report read-only state"
fi
if env -u AGENT_NOTES_ROOT -u WORKLOG_NOTES_ROOT -u CAIRN_APP -u CAIRN_WT -u WORKLOG_BOARD_APP -u WORKLOG_BOARD_WT \
  "$OPENCODE" worklog "$TMP/flowproj" >/tmp/opencode_worklog_default.out 2>/tmp/opencode_worklog_default.err \
  && grep -q '^agent-notes-root=unset$' /tmp/opencode_worklog_default.out \
  && ! grep -q '/.claude/worklog-board' /tmp/opencode_worklog_default.out; then
  ok "opencode worklog wrapper has no Claude runtime defaults"
else
  bad "opencode worklog wrapper should not default to Claude runtime paths"
fi
if AGENT_NOTES_ROOT="$TMP/notes" CAIRN_APP="$TMP/board" CAIRN_WT="$TMP/board-wt" \
  "$OPENCODE" status "$TMP/flowproj" opencodesid >/tmp/opencode_status.out 2>/tmp/opencode_status.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_status.out \
  && grep -q '^runtime_surface=adapter-owned-harness-status$' /tmp/opencode_status.out \
  && grep -q '^artifact_root_exists=1$' /tmp/opencode_status.out \
  && grep -q '^git_repo=0$' /tmp/opencode_status.out \
  && grep -q "^agent_notes_root=$TMP/notes$" /tmp/opencode_status.out \
  && grep -q '^note=read-only snapshot;' /tmp/opencode_status.out; then
  ok "opencode status wrapper reports harness snapshot"
else
  bad "opencode status wrapper should report harness snapshot"
fi
if "$OPENCODE" permissions >/tmp/opencode_permissions.out 2>/tmp/opencode_permissions.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_permissions.out \
  && grep -q '^runtime_surface=opencode-native-permission-config$' /tmp/opencode_permissions.out \
  && grep -q '^permission_model=permission-allow-ask-deny$' /tmp/opencode_permissions.out \
  && grep -q '^claude_allowed_tools=unsupported$' /tmp/opencode_permissions.out \
  && grep -q '^guard_contract=preflight-write-plugin-and-explicit-tool-contracts$' /tmp/opencode_permissions.out; then
  ok "opencode permissions wrapper reports native permission contract"
else
  bad "opencode permissions wrapper should report native permission contract"
fi
if "$OPENCODE" headless >/tmp/opencode_headless.out 2>/tmp/opencode_headless.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_headless.out \
  && grep -q '^runtime_surface=opencode-run-headless$' /tmp/opencode_headless.out \
  && grep -q '^tool_contract=headless-dispatch$' /tmp/opencode_headless.out \
  && grep -q '^model_selection_policy=main-orchestrator-must-select-per-job$' /tmp/opencode_headless.out \
  && grep -q '^model_selection_surface=--model-profile <deep|balanced-deep|light|mini> \[--model-role <portable-role>\]|--model-role <portable-role>|--model <model> --variant <variant>|--inherit-model-settings$' /tmp/opencode_headless.out \
  && grep -q '^claude_headless=unsupported$' /tmp/opencode_headless.out \
  && grep -q '^liveness_surface=opencode-sqlite-session-mtime+plugin-heartbeat$' /tmp/opencode_headless.out \
  && grep -q '^liveness_heartbeat=<agent-home>/.dispatch/logs/<slug>.heartbeat$' /tmp/opencode_headless.out \
  && grep -q '^liveness_plugin_load_marker=<agent-home>/.dispatch/plugin-load.<slug>.mark$' /tmp/opencode_headless.out \
  && grep -q '^liveness_check=adapters/opencode/bin/preflight.sh liveness \[jobs.log\]$' /tmp/opencode_headless.out \
  && grep -q '^constraints=main-or-owner-dispatched,max-dispatch-depth-2-for-standard-plus-owner,register-open-job,explicit-capability-mode-qa-intensity-dispatch_depth-parent-parent_sid,transcript-liveness-required$' /tmp/opencode_headless.out; then
  ok "opencode headless wrapper reports dispatch contract"
else
  bad "opencode headless wrapper should report dispatch contract"
fi
if "$OPENCODE" headless --check "$TMP/missing-worktree" >/tmp/opencode_headless_missing.out 2>/tmp/opencode_headless_missing.err; then
  bad "opencode headless wrapper should fail missing worktree"
else
  rc=$?
  if [ "$rc" -eq 66 ] \
    && grep -q '^reason=worktree-not-found$' /tmp/opencode_headless_missing.out; then
    ok "opencode headless wrapper reports missing worktree"
  else
    bad "opencode headless wrapper should report missing worktree"
  fi
fi
mkdir -p "$TMP/opencode_headless_home/.config/opencode/agents" \
  "$TMP/opencode_headless_home/.config/opencode/commands" \
  "$TMP/opencode_headless_home/.config/opencode/skills" \
  "$TMP/opencode_headless_home/.config/opencode/plugins"
ln -s "$ROOT" "$TMP/opencode_headless_home/.config/opencode/hearting"
ln -s "$ROOT/opencode_setting/opencode-skills/autopilot-code" "$TMP/opencode_headless_home/.config/opencode/skills/autopilot-code"
# 재홈 2026-07-22: runtime team agents retired — the projected native agent is the
# kernel helper memory-scout (agents/memory-scout/memory-scout.md).
ln -s "$ROOT/opencode_setting/opencode-agents/memory-scout" "$TMP/opencode_headless_home/.config/opencode/agents/memory-scout"
ln -s "$ROOT/opencode_setting/opencode-commands/autopilot-code.md" "$TMP/opencode_headless_home/.config/opencode/commands/autopilot-code.md"
ln -s "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js" "$TMP/opencode_headless_home/.config/opencode/plugins/hearting-guards.js"
if HOME="$TMP/opencode_headless_home" XDG_CONFIG_HOME="$TMP/opencode_headless_home/.config" \
  "$OPENCODE" headless --check "$TMP/repo" >/tmp/opencode_headless_check.out 2>/tmp/opencode_headless_check.err \
  && grep -q '^runtime_projection=ok$' /tmp/opencode_headless_check.out \
  && grep -q '/agents/memory-scout/memory-scout.md$' /tmp/opencode_headless_check.out \
  && grep -q '/commands/autopilot-code.md$' /tmp/opencode_headless_check.out \
  && grep -q '^check=ok$' /tmp/opencode_headless_check.out; then
  ok "opencode headless check validates plural native runtime projection"
else
  bad "opencode headless check should validate plural native runtime projection"
fi
mkdir -p "$TMP/opencode_headless_config_home/.config/opencode/agent" \
  "$TMP/opencode_headless_config_home/.config/opencode/command" \
  "$TMP/opencode_headless_config_home/.config/opencode/plugins"
ln -s "$ROOT" "$TMP/opencode_headless_config_home/.config/opencode/hearting"
# 재홈 2026-07-22: singular-layout variant keeps a flat agent/memory-scout.md link.
ln -s "$ROOT/opencode_setting/opencode-agents/memory-scout/memory-scout.md" "$TMP/opencode_headless_config_home/.config/opencode/agent/memory-scout.md"
ln -s "$ROOT/opencode_setting/opencode-commands/autopilot-code.md" "$TMP/opencode_headless_config_home/.config/opencode/command/autopilot-code.md"
ln -s "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js" "$TMP/opencode_headless_config_home/.config/opencode/plugins/hearting-guards.js"
if OPENCODE_CONFIG_CONTENT='{"skills":{"paths":["/tmp/opencode-\u0073kills"]}}' \
  HOME="$TMP/opencode_headless_config_home" XDG_CONFIG_HOME="$TMP/opencode_headless_config_home/.config" \
  "$OPENCODE" headless --check "$TMP/repo" >/tmp/opencode_headless_config_check.out 2>/tmp/opencode_headless_config_check.err \
  && grep -q '^runtime_projection=ok$' /tmp/opencode_headless_config_check.out \
  && grep -q '^check=ok$' /tmp/opencode_headless_config_check.out; then
  ok "opencode headless check accepts JSON-configured native skills path"
else
  bad "opencode headless check should accept JSON-configured native skills path"
fi
if "$OPENCODE" dispatch --dry-run --worktree "$TMP/repo" --slug opencode-missing-model --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --jobs "$TMP/opencode-missing-model.log" >/tmp/opencode_missing_model.out 2>/tmp/opencode_missing_model.err; then
  bad "opencode dispatch wrapper should require main-selected model settings"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=missing-dispatch-model-selection$' /tmp/opencode_missing_model.out \
    && [ ! -e "$TMP/opencode-missing-model.log" ]; then
    ok "opencode dispatch wrapper requires main-selected model settings"
  else
    bad "opencode dispatch wrapper should fail cleanly without model selection"
  fi
fi
if "$OPENCODE" dispatch --dry-run --worktree "$TMP/repo" --slug opencode-dispatch --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model provider/test --variant low --jobs "$TMP/opencode-dispatch.log" >/tmp/opencode_dispatch.out 2>/tmp/opencode_dispatch.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_dispatch.out \
  && grep -q '^status=dry-run$' /tmp/opencode_dispatch.out \
  && grep -q '^registered=0$' /tmp/opencode_dispatch.out \
  && grep -q '^started=0$' /tmp/opencode_dispatch.out \
  && grep -q '^model_source=explicit$' /tmp/opencode_dispatch.out \
  && grep -q '^model=provider/test$' /tmp/opencode_dispatch.out \
  && grep -q '^variant=low$' /tmp/opencode_dispatch.out \
  && grep -q '^command=opencode run ' /tmp/opencode_dispatch.out \
  && grep -q -- '--model provider/test' /tmp/opencode_dispatch.out \
  && grep -q -- '--variant low' /tmp/opencode_dispatch.out \
  && grep -q 'opencode-dispatch.opencode.prompt.txt' /tmp/opencode_dispatch.out \
  && grep -q 'cat -- ' /tmp/opencode_dispatch.out \
  && ! grep -q 'do work' /tmp/opencode_dispatch.out \
  && [ ! -e "$TMP/opencode-dispatch.log" ]; then
  ok "opencode dispatch wrapper dry-runs headless command with main-selected model settings"
else
  bad "opencode dispatch wrapper should dry-run headless command with main-selected model settings"
fi
mkdir -p "$TMP/opencode-stubbin"
cat > "$TMP/opencode-stubbin/opencode" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "$*" > "$OPENCODE_STUB_ARGV"
EOF
chmod +x "$TMP/opencode-stubbin/opencode"
if PATH="$TMP/opencode-stubbin:$PATH" OPENCODE_STUB_ARGV="$TMP/opencode-start.argv" \
  HOME="$TMP/opencode_headless_home" XDG_CONFIG_HOME="$TMP/opencode_headless_home/.config" \
  "$OPENCODE" dispatch --start --worktree "$TMP/repo" --slug nested/opencode-start --capability autopilot-code --mode dev --qa standard --prompt-text "nested work" --model provider/test --variant low --jobs "$TMP/opencode-start.log" --log-dir "$TMP/opencode-logs" >/tmp/opencode_dispatch_start.out 2>/tmp/opencode_dispatch_start.err \
  && grep -q '^status=start$' /tmp/opencode_dispatch_start.out \
  && grep -q '^started=1$' /tmp/opencode_dispatch_start.out \
  && grep -q 'cat -- ' /tmp/opencode_dispatch_start.out \
  && opencode_start_prompt=$(sed -n 's/^prompt_file=//p' /tmp/opencode_dispatch_start.out) \
  && [ -f "$opencode_start_prompt" ]; then
  for _ in $(seq 1 20); do
    [ -f "$TMP/opencode-start.argv" ] && break
    sleep 0.1
  done
  if grep -q 'nested work' "$TMP/opencode-start.argv" 2>/dev/null \
    && grep -q -- '--model provider/test' "$TMP/opencode-start.argv" \
    && grep -q -- '--variant low' "$TMP/opencode-start.argv"; then
    ok "opencode dispatch wrapper starts nested slug with main-selected model settings"
  else
    bad "opencode dispatch wrapper should pass prompt and main-selected model settings to opencode"
  fi
else
  bad "opencode dispatch wrapper should start nested slug from prompt file"
fi
if "$OPENCODE" dispatch --dry-run --worktree "$TMP/repo" --slug opencode-default-home --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model provider/test --variant low >/tmp/opencode_dispatch_default.out 2>/tmp/opencode_dispatch_default.err \
  && grep -Fxq "job_registry=$ROOT/.dispatch/jobs.log" /tmp/opencode_dispatch_default.out \
  && grep -Fxq "prompt_file=$ROOT/.dispatch/logs/opencode-default-home.opencode.prompt.txt" /tmp/opencode_dispatch_default.out \
  && [ ! -e "$AGENT_HOME/.dispatch/jobs.log" ]; then
  ok "opencode dispatch wrapper defaults to validated harness root"
else
  bad "opencode dispatch wrapper should not trust invalid AGENT_HOME for default registry"
fi
if AGENT_HOME="$TMP/not-agent-home" python3 "$ROOT/adapters/opencode/bin/dispatch-headless.py" --dry-run --worktree "$TMP/repo" --slug opencode-direct-home --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model provider/test --variant low >/tmp/opencode_dispatch_direct.out 2>/tmp/opencode_dispatch_direct.err \
  && grep -Fxq "job_registry=$DIRECT_DISPATCH_HOME/.dispatch/jobs.log" /tmp/opencode_dispatch_direct.out \
  && grep -Fxq "prompt_file=$DIRECT_DISPATCH_HOME/.dispatch/logs/opencode-direct-home.opencode.prompt.txt" /tmp/opencode_dispatch_direct.out; then
  ok "opencode dispatch script ignores invalid AGENT_HOME"
else
  bad "opencode dispatch script should validate AGENT_HOME"
fi
if AGENT_DISPATCH_JOBS="$TMP/opencode-env-jobs.log" \
  "$OPENCODE" dispatch --register --worktree "$TMP/repo" --slug opencode-env-jobs --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model provider/test --variant low >/tmp/opencode_env_jobs.out 2>/tmp/opencode_env_jobs.err \
  && grep -q "^job_registry=$TMP/opencode-env-jobs.log$" /tmp/opencode_env_jobs.out \
  && AGENT_DISPATCH_JOBS="$TMP/opencode-env-jobs.log" "$OPENCODE" harvest --slug opencode-env-jobs --mark-done >/tmp/opencode_env_harvest.out 2>/tmp/opencode_env_harvest.err \
  && grep -q "^job_registry=$TMP/opencode-env-jobs.log$" /tmp/opencode_env_harvest.out \
  && grep -q '^marked_done=1$' /tmp/opencode_env_harvest.out; then
  ok "opencode dispatch and harvest use the selected shared registry"
else
  bad "opencode dispatch and harvest should use the selected shared registry"
fi
if "$OPENCODE" dispatch --register --worktree "$TMP/repo" --slug opencode-quick-depth1 --capability autopilot-code --mode dev --intensity quick --dispatch-depth 1 --parent-session-id test-parent --owner-harness opencode --prompt-text "quick work" --model provider/test --variant low --jobs "$TMP/opencode-quick-depth1.log" >/tmp/opencode_quick_depth1.out 2>/tmp/opencode_quick_depth1.err; then
  bad "opencode dispatch wrapper should reject route-unbound quick jobs"
else
  rc=$?
  if [ "$rc" -eq 65 ] \
    && grep -q '^reason=quick-headless-unavailable$' /tmp/opencode_quick_depth1.out \
    && grep -q '^child_spawned=0$' /tmp/opencode_quick_depth1.out \
    && [ ! -e "$TMP/opencode-quick-depth1.log" ]; then
    ok "opencode dispatch wrapper rejects route-unbound quick jobs"
  else
    bad "opencode dispatch wrapper should fail closed for route-unbound quick jobs"
  fi
fi
if "$OPENCODE" dispatch --dry-run --worktree "$TMP/repo" --slug opencode-quick-depth2 --capability autopilot-code --capability-mode dev --worker-mode dev/backend --worker-type stage --unit dev/backend --intensity quick --dispatch-depth 2 --parent opencode-parent --prompt-text "quick work" --model provider/test --variant low >/tmp/opencode_quick_depth2.out 2>/tmp/opencode_quick_depth2.err; then
  bad "opencode dispatch wrapper should reject quick dispatch-depth-2 jobs"
else
  rc=$?
  if [ "$rc" -eq 64 ] \
    && grep -q '^reason=invalid-depth-two-intensity$' /tmp/opencode_quick_depth2.out \
    && grep -q '^dispatch_depth=2$' /tmp/opencode_quick_depth2.out \
    && grep -q '^intensity=quick$' /tmp/opencode_quick_depth2.out \
    && [ ! -e "$TMP/opencode-quick-depth2.log" ]; then
    ok "opencode dispatch wrapper rejects quick dispatch-depth-2 jobs"
  else
    bad "opencode dispatch wrapper should reject quick dispatch-depth-2 jobs"
  fi
fi
if "$OPENCODE" dispatch --register --worktree "$TMP/repo" --slug opencode-dispatch --capability autopilot-code --mode dev --qa standard --prompt-text "do work" --model provider/test --variant low --jobs "$TMP/opencode-dispatch.log" >/tmp/opencode_dispatch.out 2>/tmp/opencode_dispatch.err \
  && grep -q '^status=register$' /tmp/opencode_dispatch.out \
  && grep -q '^registered=1$' /tmp/opencode_dispatch.out \
  && grep -q '^started=0$' /tmp/opencode_dispatch.out \
  && grep -q $'open\t.*/repo\t.*/repo\topencode-dispatch\t' "$TMP/opencode-dispatch.log" \
  && grep -q 'capability_mode=dev' "$TMP/opencode-dispatch.log" \
  && ! grep -q ',mode=' "$TMP/opencode-dispatch.log" \
  && grep -q 'worker_type=owner' "$TMP/opencode-dispatch.log" \
  && grep -q 'assigned_contract=autopilot-code' "$TMP/opencode-dispatch.log"; then
  ok "opencode dispatch wrapper registers open headless job"
else
  bad "opencode dispatch wrapper should register open headless job"
fi
if "$OPENCODE" dispatch --register --worktree "$TMP/repo" --slug opencode-generated --capability autopilot-code --mode dev --qa thorough --intensity thorough --model provider/test --variant low --jobs "$TMP/opencode-generated.log" --log-dir "$TMP/opencode-generated-logs" >/tmp/opencode_generated_dispatch.out 2>/tmp/opencode_generated_dispatch.err \
  && grep -q '^status=register$' /tmp/opencode_generated_dispatch.out \
  && grep -q '^registered=1$' /tmp/opencode_generated_dispatch.out \
  && grep -q '^prompt_source=generated$' /tmp/opencode_generated_dispatch.out \
  && opencode_generated_prompt=$(sed -n 's/^prompt_file=//p' /tmp/opencode_generated_dispatch.out) \
  && [ -f "$opencode_generated_prompt" ] \
  && grep -q 'qa-policy thorough code' "$opencode_generated_prompt" \
  && grep -q '^# Portable Worker Kernel$' "$opencode_generated_prompt" \
  && grep -q '^# Worker Type: Owner$' "$opencode_generated_prompt" \
  && grep -q 'assigned_contract: autopilot-code' "$opencode_generated_prompt" \
  && grep -q 'launch checked adapter wrappers directly' "$opencode_generated_prompt"; then
  ok "opencode dispatch wrapper materializes generated register prompt with QA policy"
else
  bad "opencode dispatch wrapper should materialize generated register prompt with QA policy"
fi
if "$OPENCODE" harvest --jobs "$TMP/opencode-dispatch.log" --slug opencode-dispatch >/tmp/opencode_harvest.out 2>/tmp/opencode_harvest.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_harvest.out \
  && grep -q '^runtime_surface=opencode-dispatch-harvest$' /tmp/opencode_harvest.out \
  && grep -q '^matched=1$' /tmp/opencode_harvest.out \
  && grep -q '^marked_done=0$' /tmp/opencode_harvest.out \
  && grep -q '^job_status=open$' /tmp/opencode_harvest.out \
  && grep -q '^merge_action=unsupported$' /tmp/opencode_harvest.out; then
  ok "opencode harvest wrapper reports open registry jobs"
else
  bad "opencode harvest wrapper should report open registry jobs"
fi
if "$OPENCODE" harvest --jobs "$TMP/opencode-dispatch.log" --slug opencode-dispatch --mark-done >/tmp/opencode_harvest_done.out 2>/tmp/opencode_harvest_done.err \
  && grep -q '^marked_done=1$' /tmp/opencode_harvest_done.out \
  && grep -q $'done\t.*/repo\t.*/repo\topencode-dispatch\t' "$TMP/opencode-dispatch.log" \
  && grep -q 'worker_type=owner' "$TMP/opencode-dispatch.log" \
  && grep -q 'assigned_contract=autopilot-code' "$TMP/opencode-dispatch.log"; then
  ok "opencode harvest wrapper marks selected jobs done"
else
  bad "opencode harvest wrapper should mark selected jobs done"
fi
OPENCODE_DB="$TMP/opencode.db" python3 - <<EOF
import sqlite3, time
con = sqlite3.connect("$TMP/opencode.db")
now = int(time.time() * 1000)
con.executescript("""
CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, workspace_id text, parent_id text, slug text NOT NULL, directory text NOT NULL, path text, title text NOT NULL, version text NOT NULL, share_url text, summary_additions integer, summary_deletions integer, summary_files integer, summary_diffs text, metadata text, cost real DEFAULT 0 NOT NULL, tokens_input integer DEFAULT 0 NOT NULL, tokens_output integer DEFAULT 0 NOT NULL, tokens_reasoning integer DEFAULT 0 NOT NULL, tokens_cache_read integer DEFAULT 0 NOT NULL, tokens_cache_write integer DEFAULT 0 NOT NULL, revert text, permission text, agent text, model text, time_created integer NOT NULL, time_updated integer NOT NULL, time_compacting integer, time_archived integer);
CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL, seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE session_input (id text PRIMARY KEY, session_id text NOT NULL, prompt text NOT NULL, delivery text NOT NULL, admitted_seq integer NOT NULL, promoted_seq integer, time_created integer NOT NULL);
""")
con.execute("INSERT INTO session (id, project_id, workspace_id, parent_id, slug, directory, path, title, version, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("ses_live", "proj", None, None, "live-open", "$TMP/flowproj", "", "title", "1", now - 1000, now - 1000))
con.execute("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)", ("msg_live", "ses_live", now - 900, now - 800, "{}"))
con.commit()
EOF
printf '2026-06-30T00:00:00Z\topen\t%s\t%s\tlive-opencode\t-\n' "$TMP/repo" "$TMP/flowproj" > "$TMP/opencode-jobs.log"
if OPENCODE_DB="$TMP/opencode.db" DISPATCH_STALE_MIN=60 "$OPENCODE" liveness "$TMP/opencode-jobs.log" >/tmp/opencode_liveness.out 2>/tmp/opencode_liveness.err \
  && grep -q '^ALIVE    live-opencode ' /tmp/opencode_liveness.out \
  && grep -q '^open 1 ; alive 1 ; suspect/dead 0$' /tmp/opencode_liveness.out; then
  ok "opencode liveness wrapper matches worktree to session DB"
else
  bad "opencode liveness wrapper should match worktree to session DB"
fi
printf '2026-06-30T00:00:00Z\topen\t%s\t%s\tdead-opencode\t-\n' "$TMP/repo" "$TMP/missing-opencode-wt" > "$TMP/opencode-dead-jobs.log"
if OPENCODE_DB="$TMP/opencode.db" "$OPENCODE" liveness "$TMP/opencode-dead-jobs.log" >/tmp/opencode_liveness_dead.out 2>/tmp/opencode_liveness_dead.err; then
  bad "opencode liveness wrapper should fail dead jobs"
else
  rc=$?
  if [ "$rc" -eq 3 ] \
    && grep -q '^DEAD     dead-opencode ' /tmp/opencode_liveness_dead.out \
    && grep -q '^open 1 ; alive 0 ; suspect/dead 1$' /tmp/opencode_liveness_dead.out; then
    ok "opencode liveness wrapper reports dead jobs"
  else
    bad "opencode liveness wrapper should report dead jobs"
  fi
fi
if "$OPENCODE" mcp >/tmp/opencode_mcp.out 2>/tmp/opencode_mcp.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_mcp.out \
  && grep -q '^runtime_surface=opencode-native-mcp$' /tmp/opencode_mcp.out \
  && grep -q '^mcp_surface=opencode mcp$' /tmp/opencode_mcp.out \
  && grep -q '^design_mcp_projection=unsupported$' /tmp/opencode_mcp.out \
  && grep -q '^claude_settings_mcp=unsupported$' /tmp/opencode_mcp.out; then
  ok "opencode mcp wrapper reports native MCP contract"
else
  bad "opencode mcp wrapper should report native MCP contract"
fi
if "$OPENCODE" mcp --check >/tmp/opencode_mcp_check.out 2>/tmp/opencode_mcp_check.err \
  && grep -q '^check=ok$' /tmp/opencode_mcp_check.out; then
  ok "opencode mcp wrapper checks native MCP CLI"
else
  bad "opencode mcp wrapper should check native MCP CLI"
fi
if "$OPENCODE" loop-info oncall >/tmp/opencode_loop_oncall.out 2>/tmp/opencode_loop_oncall.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_loop_oncall.out \
  && grep -q '^loop=oncall$' /tmp/opencode_loop_oncall.out \
  && grep -q '^source=loops/oncall.md$' /tmp/opencode_loop_oncall.out \
  && grep -q '^status=manual-contract$' /tmp/opencode_loop_oncall.out \
  && grep -q '^runtime_surface=opencode-loop-guidance$' /tmp/opencode_loop_oncall.out \
  && grep -q '^executable_projection=unsupported-runtime-script$' /tmp/opencode_loop_oncall.out; then
  ok "opencode loop wrapper reports oncall manual contract"
else
  bad "opencode loop wrapper should report oncall manual contract"
fi
if "$OPENCODE" loop-info drill >/tmp/opencode_loop_drill.out 2>/tmp/opencode_loop_drill.err \
  && grep -q '^source=loops/drill/README.md$' /tmp/opencode_loop_drill.out \
  && grep -q '^status=manual-contract$' /tmp/opencode_loop_drill.out \
  && grep -q '^trigger=manual-only$' /tmp/opencode_loop_drill.out \
  && grep -q '^auto_run=unsupported$' /tmp/opencode_loop_drill.out \
  && grep -q '^fallback=report-drill-would-be-useful$' /tmp/opencode_loop_drill.out; then
  ok "opencode loop wrapper prevents automatic drill execution"
else
  bad "opencode loop wrapper should prevent automatic drill execution"
fi
if "$OPENCODE" loop-info study >/tmp/opencode_loop_study.out 2>/tmp/opencode_loop_study.err \
  && grep -q '^source=loops/study.md$' /tmp/opencode_loop_study.out \
  && grep -q '^status=manual-contract$' /tmp/opencode_loop_study.out \
  && grep -q '^action=proposal-report-only$' /tmp/opencode_loop_study.out \
  && grep -q '^fallback=read-source-and-draft-proposal-in-main-session$' /tmp/opencode_loop_study.out; then
  ok "opencode loop wrapper reports study proposal contract"
else
  bad "opencode loop wrapper should report study proposal contract"
fi
if "$OPENCODE" loop-info note >/tmp/opencode_loop_note.out 2>/tmp/opencode_loop_note.err \
  && grep -q '^loop=note$' /tmp/opencode_loop_note.out \
  && grep -q '^status=unsupported$' /tmp/opencode_loop_note.out \
  && grep -q '^runtime_surface=missing-native-loop$' /tmp/opencode_loop_note.out \
  && grep -q '^related_extension=artifact-sink$' /tmp/opencode_loop_note.out \
  && grep -q '^native_extension_surface=application-owned-plugin$' /tmp/opencode_loop_note.out \
  && grep -q '^scheduler_surface=external-application$' /tmp/opencode_loop_note.out \
  && grep -q '^fallback=extension-unavailable-or-application-scheduler$' /tmp/opencode_loop_note.out; then
  ok "opencode loop wrapper marks missing note loop unsupported"
else
  bad "opencode loop wrapper should mark missing note loop unsupported"
fi

echo "== opencode role mapping =="
if AGENT_MODEL_FAST=fast-model AGENT_VARIANT_FAST=low "$OPENCODE" role fast reviewer >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q '^family=fast$' /tmp/opencode_role.out \
  && grep -q '^adapter=opencode$' /tmp/opencode_role.out \
  && grep -q '^source=roles/README.md$' /tmp/opencode_role.out \
  && grep -q '^model=fast-model$' /tmp/opencode_role.out \
  && grep -q '^variant=low$' /tmp/opencode_role.out; then
  ok "opencode role wrapper maps fast portable role"
else
  bad "opencode role wrapper should map fast portable role"
fi
if AGENT_MODEL_ORCHESTRATOR=provider/orchestrator-model AGENT_VARIANT_ORCHESTRATOR=medium "$OPENCODE" role external adversary orchestrator >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q '^family=balanced$' /tmp/opencode_role.out \
  && grep -q '^adapter=opencode$' /tmp/opencode_role.out \
  && grep -q '^model=provider/orchestrator-model$' /tmp/opencode_role.out \
  && grep -q '^variant=medium$' /tmp/opencode_role.out \
  && grep -q '^available=1$' /tmp/opencode_role.out \
  && grep -q '^status=configured$' /tmp/opencode_role.out; then
  ok "opencode role wrapper maps external adversary orchestrator role"
else
  bad "opencode role wrapper should map external adversary orchestrator role"
fi
if "$OPENCODE" role external adversary >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q '^available=0$' /tmp/opencode_role.out \
  && grep -q '^status=unavailable$' /tmp/opencode_role.out; then
  ok "opencode role wrapper marks external adversary unavailable by default"
else
  bad "opencode role wrapper should mark external adversary unavailable by default"
fi
if AGENT_MODEL_EXTERNAL=provider/external-model AGENT_VARIANT_EXTERNAL=high "$OPENCODE" role external adversary >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q '^family=external$' /tmp/opencode_role.out \
  && grep -q '^available=1$' /tmp/opencode_role.out \
  && grep -q '^status=configured$' /tmp/opencode_role.out \
  && grep -q '^model=provider/external-model$' /tmp/opencode_role.out \
  && grep -q '^variant=high$' /tmp/opencode_role.out; then
  ok "opencode role wrapper maps configured external adversary model"
else
  bad "opencode role wrapper should map configured external adversary model"
fi
if AGENT_EXTERNAL_CMD="sh -c" "$OPENCODE" role external adversary >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q '^available=1$' /tmp/opencode_role.out \
  && grep -q '^status=configured$' /tmp/opencode_role.out \
  && grep -q '^model=external-command$' /tmp/opencode_role.out \
  && grep -q '^external_command=sh -c$' /tmp/opencode_role.out; then
  ok "opencode role wrapper accepts external adversary command with args"
else
  bad "opencode role wrapper should accept external adversary command with args"
fi
if AGENT_EXTERNAL_CMD="missing-external-adversary-command --review" "$OPENCODE" role external adversary >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q '^available=0$' /tmp/opencode_role.out \
  && grep -q '^status=unavailable$' /tmp/opencode_role.out \
  && grep -q '^reason=AGENT_EXTERNAL_CMD not found: missing-external-adversary-command$' /tmp/opencode_role.out; then
  ok "opencode role wrapper reports missing external adversary command"
else
  bad "opencode role wrapper should report missing external adversary command"
fi
# Unconfigured fast roles fall back to the config-declared mini tier (models.conf
# SoT retired the old `opencode-default` placeholder); derive the expectation.
opencode_mini_model=$(. "$ROOT/adapters/opencode/config/models.conf" && printf '%s' "$CFG_TIER_MINI_MODEL")
if "$OPENCODE" role fast reviewer >/tmp/opencode_role.out 2>/tmp/opencode_role.err \
  && grep -q "^model=${opencode_mini_model}\$" /tmp/opencode_role.out \
  && grep -q '^variant=runtime-default$' /tmp/opencode_role.out; then
  ok "opencode role wrapper falls back to the config mini tier when unconfigured"
else
  bad "opencode role wrapper should fall back to the config mini tier when unconfigured"
fi
if command -v opencode >/dev/null 2>&1; then
  mkdir -p "$TMP/opencode_bootstrap_home/.config/opencode" "$TMP/opencode_bootstrap_home/.local/share"
  if OPENCODE_CONFIG_CONTENT="{\"instructions\":[\"$ROOT/opencode_setting/AGENTS.md\"],\"skills\":{\"paths\":[\"$ROOT/opencode_setting/opencode-skills\"]}}" \
    HOME="$TMP/opencode_bootstrap_home" XDG_CONFIG_HOME="$TMP/opencode_bootstrap_home/.config" XDG_DATA_HOME="$TMP/opencode_bootstrap_home/.local/share" \
    opencode debug config --pure >/tmp/opencode_bootstrap.out 2>/tmp/opencode_bootstrap.err \
    && grep -q "$ROOT/opencode_setting/AGENTS.md" /tmp/opencode_bootstrap.out \
    && grep -q "$ROOT/opencode_setting/opencode-skills" /tmp/opencode_bootstrap.out \
    && ! grep -q '/.claude/' /tmp/opencode_bootstrap.out; then
    ok "opencode bootstrap config projects instructions and skills without Claude paths"
  else
    bad "opencode bootstrap config should project instructions and skills without Claude paths"
  fi
else
  ok "opencode bootstrap config runtime discovery skipped (opencode not installed)"
fi
if command -v opencode >/dev/null 2>&1; then
  mkdir -p "$TMP/opencode_home/.config/opencode/agent" "$TMP/opencode_home/.local/share"
  for f in "$ROOT"/opencode_setting/opencode-agents/*/*.md; do
    [ -f "$f" ] || continue
    ln -s "$f" "$TMP/opencode_home/.config/opencode/agent/$(basename "$f")"
  done
  # 재홈 2026-07-22: team profiles retired — the discoverable native agent is the kernel
  # helper memory-scout, which declares its portable source (core/MEMORY.md §7.4).
  if HOME="$TMP/opencode_home" XDG_CONFIG_HOME="$TMP/opencode_home/.config" XDG_DATA_HOME="$TMP/opencode_home/.local/share" \
    opencode debug agent memory-scout --pure >/tmp/opencode_agent.out 2>/tmp/opencode_agent.err \
    && grep -q '"description": "Read-only memory scout for agent-initiated deep memory reconnaissance."' /tmp/opencode_agent.out \
    && grep -q 'core/MEMORY.md' /tmp/opencode_agent.out \
    && ! grep -q '/.claude/' /tmp/opencode_agent.out; then
    ok "opencode native agent projection is discoverable without Claude paths"
  else
    bad "opencode native agent projection should be discoverable without Claude paths"
  fi
  # 재홈 2026-07-22: qa-team projection retired — read-only/depth-one runtime enforcement
  # is preserved on the kernel memory-scout projection (edit/write/task off + deny rules).
  if HOME="$TMP/opencode_home" XDG_CONFIG_HOME="$TMP/opencode_home/.config" XDG_DATA_HOME="$TMP/opencode_home/.local/share" \
    opencode debug agent memory-scout --pure >/tmp/opencode_agent_qa.out 2>/tmp/opencode_agent_qa.err \
    && python3 - /tmp/opencode_agent_qa.out <<'PY'
import json
import sys

agent = json.load(open(sys.argv[1], encoding="utf-8"))
tools = agent.get("tools", {})
rules = {(r.get("permission"), r.get("action")) for r in agent.get("permission", [])}
assert tools.get("edit") is False, tools
assert tools.get("write") is False, tools
assert tools.get("task") is False, tools
assert ("edit", "deny") in rules, rules
assert ("task", "deny") in rules, rules
PY
  then
    ok "opencode qa agent projection enforces read-only and depth-one tools"
  else
    bad "opencode qa agent projection should disable edit/write/task"
  fi
else
  ok "opencode native agent runtime discovery skipped (opencode not installed)"
fi
if command -v opencode >/dev/null 2>&1; then
  mkdir -p "$TMP/opencode_command_home/.config/opencode/command" "$TMP/opencode_command_home/.local/share"
  for f in "$ROOT"/opencode_setting/opencode-commands/*.md; do
    [ -f "$f" ] || continue
    ln -s "$f" "$TMP/opencode_command_home/.config/opencode/command/$(basename "$f")"
  done
  if HOME="$TMP/opencode_command_home" XDG_CONFIG_HOME="$TMP/opencode_command_home/.config" XDG_DATA_HOME="$TMP/opencode_command_home/.local/share" \
    opencode debug config --pure >/tmp/opencode_command.out 2>/tmp/opencode_command.err \
    && grep -q '"autopilot-code": {' /tmp/opencode_command.out \
    && grep -q '"description": "Run the portable autopilot-code capability through the OpenCode adapter' /tmp/opencode_command.out \
    && ! grep -q '/.claude/' /tmp/opencode_command.out; then
    ok "opencode native command projection is discoverable without Claude paths"
  else
    bad "opencode native command projection should be discoverable without Claude paths"
  fi
else
  ok "opencode native command runtime discovery skipped (opencode not installed)"
fi
if command -v opencode >/dev/null 2>&1; then
  mkdir -p "$TMP/opencode_plugin_project/.opencode/plugins" "$TMP/opencode_plugin_home/.config" "$TMP/opencode_plugin_home/.local/share"
  ln -s "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js" "$TMP/opencode_plugin_project/.opencode/plugins/hearting-guards.js"
  if (
    cd "$TMP/opencode_plugin_project" || exit 1
    HOME="$TMP/opencode_plugin_home" XDG_CONFIG_HOME="$TMP/opencode_plugin_home/.config" XDG_DATA_HOME="$TMP/opencode_plugin_home/.local/share" \
      opencode debug config >/tmp/opencode_plugin.out 2>/tmp/opencode_plugin.err
  ) && grep -q 'hearting-guards.js' /tmp/opencode_plugin.out \
    && ! grep -q 'adapters/claude/hooks' /tmp/opencode_plugin.out; then
    ok "opencode native plugin projection is discoverable without Claude hooks"
  else
    bad "opencode native plugin projection should be discoverable without Claude hooks"
  fi
else
  ok "opencode native plugin runtime discovery skipped (opencode not installed)"
fi
if node --input-type=module >/tmp/opencode_plugin_hook.out 2>/tmp/opencode_plugin_hook.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
await plugin["tool.execute.before"]({ tool: "write", sessionID: "testsid" }, { args: { filePath: "$TMP/repo/f" } })
EOF
then
  ok "opencode native plugin write hook bridges to preflight"
else
  bad "opencode native plugin write hook should bridge to preflight"
fi
mkdir -p "$TMP/fake_agent_home/adapters/opencode/bin"
cat > "$TMP/fake_agent_home/adapters/opencode/bin/preflight.sh" <<'EOF'
#!/usr/bin/env sh
exit 77
EOF
chmod +x "$TMP/fake_agent_home/adapters/opencode/bin/preflight.sh"
if node --input-type=module >/tmp/opencode_plugin_invalid_home.out 2>/tmp/opencode_plugin_invalid_home.err <<EOF
process.env.AGENT_HOME = "$TMP/fake_agent_home"
const mod = await import("$ROOT/opencode_setting/opencode-plugins/hearting-guards.js")
const plugin = await mod.AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
await plugin["tool.execute.before"]({ tool: "write", sessionID: "testsid" }, { args: { filePath: "$TMP/repo/f" } })
EOF
then
  ok "opencode native plugin ignores invalid AGENT_HOME"
else
  bad "opencode native plugin should validate AGENT_HOME"
fi
mkdir -p "$TMP/opencode_copied_plugin"
cp "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js" "$TMP/opencode_copied_plugin/hearting-guards.js"
if node --input-type=module >/tmp/opencode_plugin_copy.out 2>/tmp/opencode_plugin_copy.err <<EOF
process.env.AGENT_HOME = "$ROOT"
const mod = await import("$TMP/opencode_copied_plugin/hearting-guards.js")
const plugin = await mod.AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
await plugin["tool.execute.before"]({ tool: "write", sessionID: "testsid" }, { args: { filePath: "$TMP/repo/f" } })
EOF
then
  ok "opencode native plugin copy resolves harness through AGENT_HOME"
else
  bad "opencode native plugin copy should resolve harness through AGENT_HOME"
fi
if node --input-type=module >/tmp/opencode_plugin_lifecycle.out 2>/tmp/opencode_plugin_lifecycle.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/flowproj", worktree: "$TMP/flowproj" })
if (!plugin["chat.message"]) process.exit(1)
await plugin["chat.message"]({ sessionID: "oplifecyclesid", messageID: "opturn" }, { parts: [{ type: "text", text: "ordinary lifecycle prompt" }] })
const output = { system: [] }
await plugin["experimental.chat.system.transform"]({ sessionID: "oplifecyclesid", model: {} }, output)
if (!output.system.join("\\n").includes("routing_contract=core/WORKFLOW.md")) process.exit(1)
if (output.system.join("\\n").includes("adapters/claude") || output.system.join("\\n").includes("statusline.sh")) process.exit(1)
EOF
then
  ok "opencode native plugin prompt lifecycle bridges to preflight"
else
  bad "opencode native plugin prompt lifecycle should bridge to preflight"
fi
OPENCODE_WORKER_ROOT="$TMP/opencode_worker_root"
mkdir -p "$OPENCODE_WORKER_ROOT/core" "$OPENCODE_WORKER_ROOT/adapters/opencode/bin"
printf 'worker fixture\n' > "$OPENCODE_WORKER_ROOT/core/CORE.md"
cat > "$OPENCODE_WORKER_ROOT/adapters/opencode/bin/preflight.sh" <<EOF
#!/usr/bin/env sh
printf '%s\n' "\$*" >> "$OPENCODE_WORKER_ROOT/calls"
case "\${1:-}" in
  write) exit 73 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$OPENCODE_WORKER_ROOT/adapters/opencode/bin/preflight.sh"
if AGENT_HOME="$OPENCODE_WORKER_ROOT" AGENT_SESSION_ROLE=worker \
  node --input-type=module >/tmp/opencode_plugin_worker.out 2>/tmp/opencode_plugin_worker.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/flowproj", worktree: "$TMP/flowproj" })
const output = { system: [] }
await plugin["experimental.chat.system.transform"]({ sessionID: "op-worker", model: {} }, output)
if (output.system.length !== 0) process.exit(1)
await plugin.event({ event: { type: "session.idle", properties: { sessionID: "op-worker" } } })
try {
  await plugin["tool.execute.before"]({ tool: "write", sessionID: "op-worker" }, { args: { filePath: "$TMP/flowproj/f" } })
  process.exit(1)
} catch {}
EOF
then
  if grep -q '^write ' "$OPENCODE_WORKER_ROOT/calls" \
    && ! grep -Eq '^(memory|briefing|prompt-signal|start|session-end) ' "$OPENCODE_WORKER_ROOT/calls"; then
    ok "opencode worker plugin skips main lifecycle while retaining write guards"
  else
    bad "opencode worker plugin must separate lifecycle from safety guards"
  fi
else
  bad "opencode worker plugin must separate lifecycle from safety guards"
fi
if node --input-type=module >/tmp/opencode_plugin_hook_block.out 2>/tmp/opencode_plugin_hook_block.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/runtime", worktree: "$TMP/runtime" })
try {
  await plugin["tool.execute.before"]({ tool: "write", sessionID: "testsid" }, { args: { filePath: "$TMP/runtime/projects/abc/memory/MEMORY.md" } })
  process.exit(1)
} catch (error) {
  if (!String(error.message || error).includes("memory")) process.exit(1)
}
EOF
then
  ok "opencode native plugin write hook blocks guarded writes"
else
  bad "opencode native plugin write hook should block guarded writes"
fi
if DESIGN_POSTWRITE_HOOK=0 node --input-type=module >/tmp/opencode_plugin_design_hook.out 2>/tmp/opencode_plugin_design_hook.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
await plugin["tool.execute.after"]({ tool: "write", sessionID: "testsid", args: { filePath: "$TMP/repo/spec/design/preview.html" } }, {})
EOF
then
  ok "opencode native plugin design after hook bridges to preflight"
else
  bad "opencode native plugin design after hook should bridge to preflight"
fi

# A1/A2/A3 plugin bridge: transitive material-write denial, bash git-commit
# denial, bash worktree-add denial/pass, neutral/non-add pass, raw command
# preserved verbatim, and shell.env's OPENCODE_SESSION_ID assignment.
opencode_plugin_source="$TMP/repo/opencode_plugin_source.py"
printf 'print(1)\n' > "$opencode_plugin_source"
git -C "$TMP/repo" add "$opencode_plugin_source"
if node --input-type=module >/tmp/opencode_plugin_write_route.out 2>/tmp/opencode_plugin_write_route.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
try {
  await plugin["tool.execute.before"]({ tool: "write", sessionID: "opencode-plugin-no-route" }, { args: { filePath: "$opencode_plugin_source" } })
  process.exit(1)
} catch (error) {
  if (!String(error.message || error).toLowerCase().includes("route")) process.exit(1)
}
EOF
then
  ok "opencode plugin tool.execute.before denies a route-less material write end-to-end"
else
  bad "opencode plugin tool.execute.before should deny a route-less material write end-to-end"
fi
if node --input-type=module >/tmp/opencode_plugin_bash_commit.out 2>/tmp/opencode_plugin_bash_commit.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
try {
  await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-bash-commit" }, { args: { command: "git commit -am 'opencode_plugin_source'" } })
  process.exit(1)
} catch {}
EOF
then
  ok "opencode plugin bash git commit of material content is denied"
else
  bad "opencode plugin bash git commit of material content should be denied"
fi
if node --input-type=module >/tmp/opencode_plugin_bash_wt_deny.out 2>/tmp/opencode_plugin_bash_wt_deny.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
try {
  await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-bash-wt" }, { args: { command: "git worktree add .claude/worktrees/foo -b foo" } })
  process.exit(1)
} catch {}
EOF
then
  ok "opencode plugin bash git worktree add outside -wt/ is denied"
else
  bad "opencode plugin bash git worktree add outside -wt/ should be denied"
fi
if node --input-type=module >/tmp/opencode_plugin_bash_wt_pass.out 2>/tmp/opencode_plugin_bash_wt_pass.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-bash-wt-pass" }, { args: { command: "git worktree add /home/x/repo-wt/slug -b slug main" } })
EOF
then
  ok "opencode plugin passes canonical <repo>-wt/ worktree add"
else
  bad "opencode plugin should pass canonical <repo>-wt/ worktree add"
fi
if node --input-type=module >/tmp/opencode_plugin_bash_neutral.out 2>/tmp/opencode_plugin_bash_neutral.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-bash-neutral" }, { args: { command: "git worktree remove /home/x/repo-wt/slug" } })
await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-bash-neutral" }, { args: { command: "ls -la && git status" } })
EOF
then
  ok "opencode plugin leaves non-add worktree subcommands and neutral commands alone"
else
  bad "opencode plugin should leave non-add worktree subcommands and neutral commands alone"
fi
if node --input-type=module >/tmp/opencode_plugin_bash_verbatim.out 2>/tmp/opencode_plugin_bash_verbatim.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
// Irregular internal spacing would break a token-rejoining classifier but not
// a verbatim single-argv passthrough; both the neutral command and the
// worktree add must still resolve exactly as their single-space equivalents.
await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-verbatim" }, { args: { command: "git status  &&  echo 'a   b'" } })
try {
  await plugin["tool.execute.before"]({ tool: "bash", sessionID: "opencode-plugin-verbatim" }, { args: { command: "git  worktree  add   .claude/worktrees/foo  -b  foo" } })
  process.exit(1)
} catch {}
EOF
then
  ok "opencode plugin passes the raw bash command through verbatim as one argv element"
else
  bad "opencode plugin should pass the raw bash command through verbatim as one argv element"
fi
if node --input-type=module >/tmp/opencode_plugin_shell_env.out 2>/tmp/opencode_plugin_shell_env.err <<EOF
import { AgentHarnessGuards } from "$ROOT/opencode_setting/opencode-plugins/hearting-guards.js"
const plugin = await AgentHarnessGuards({ directory: "$TMP/repo", worktree: "$TMP/repo" })
const withSid = { env: {} }
await plugin["shell.env"]({ cwd: "$TMP/repo", sessionID: "opencode-shell-env-sid" }, withSid)
const withoutSid = {}
await plugin["shell.env"]({ cwd: "$TMP/repo" }, withoutSid)
if (withSid.env.OPENCODE_SESSION_ID !== "opencode-shell-env-sid") process.exit(1)
if (withoutSid.env) process.exit(1)
EOF
then
  ok "opencode plugin shell.env sets OPENCODE_SESSION_ID only for a nonempty sessionID"
else
  bad "opencode plugin shell.env should set OPENCODE_SESSION_ID only for a nonempty sessionID"
fi

echo "== opencode capability mapping =="
if "$OPENCODE" capability-info autopilot-code >/tmp/opencode_cap.out 2>/tmp/opencode_cap.err \
  && grep -q '^capability=autopilot-code$' /tmp/opencode_cap.out \
  && grep -q '^adapter=opencode$' /tmp/opencode_cap.out \
  && grep -q '^native_skill=1$' /tmp/opencode_cap.out \
  && grep -q '^native_skill_path=adapters/opencode/skills/autopilot-code/SKILL.md$' /tmp/opencode_cap.out \
  && grep -q '^native_command=1$' /tmp/opencode_cap.out \
  && grep -q '^native_command_path=adapters/opencode/commands/autopilot-code.md$' /tmp/opencode_cap.out \
  && grep -q '^realization=opencode-native-skill-command$' /tmp/opencode_cap.out \
  && grep -q '^compat_reference=not-projected$' /tmp/opencode_cap.out \
  && ! grep -q '^compat_reference=skills/' /tmp/opencode_cap.out \
  && grep -q '^status=instruction-only$' /tmp/opencode_cap.out; then
  ok "opencode capability wrapper reports native skill and command realization"
else
  bad "opencode capability wrapper should report native skill and command realization"
fi
tmp_map_root="$TMP/opencode_map_root"
mkdir -p "$tmp_map_root/adapters/opencode/bin" "$tmp_map_root/capabilities"
cp "$ROOT/adapters/opencode/bin/capability-map.sh" "$tmp_map_root/adapters/opencode/bin/capability-map.sh"
cat >"$tmp_map_root/capabilities/README.md" <<'EOF'
| Capability | Meaning |
|---|---|
| `autopilot-code` | test |
EOF
if "$tmp_map_root/adapters/opencode/bin/capability-map.sh" autopilot-code >/tmp/opencode_cap_missing.out 2>/tmp/opencode_cap_missing.err \
  && grep -q '^native_skill=0$' /tmp/opencode_cap_missing.out \
  && grep -q '^native_command=0$' /tmp/opencode_cap_missing.out \
  && grep -q '^realization=portable-instructions$' /tmp/opencode_cap_missing.out \
  && grep -q '^note=OpenCode has no native Skill/command realization' /tmp/opencode_cap_missing.out; then
  ok "opencode capability wrapper downgrades note when native projections are missing"
else
  bad "opencode capability wrapper should not claim missing native projections"
fi
if "$OPENCODE" capability-info design-review >/tmp/opencode_cap.out 2>/tmp/opencode_cap.err \
  && grep -q '^capability=design-review$' /tmp/opencode_cap.out \
  && grep -q '^native_skill=1$' /tmp/opencode_cap.out \
  && grep -q '^native_command=1$' /tmp/opencode_cap.out \
  && grep -q '^realization=opencode-native-skill-command$' /tmp/opencode_cap.out \
  && grep -q '^status=tool-contract$' /tmp/opencode_cap.out \
  && grep -q '^tool_contract=visual-harness$' /tmp/opencode_cap.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh visual-harness <file.html>$' /tmp/opencode_cap.out \
  && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/opencode_cap.out \
  && grep -q '^fallback=preflight.sh visual-harness <file.html>$' /tmp/opencode_cap.out; then
  ok "opencode design capability reports visual harness contract"
else
  bad "opencode design capability should report visual harness contract"
fi
if "$OPENCODE" visual-harness >/tmp/opencode_visual.out 2>/tmp/opencode_visual.err; then
  if grep -q '^adapter=opencode$' /tmp/opencode_visual.out \
    && grep -q '^status=tool-contract$' /tmp/opencode_visual.out \
    && grep -q '^tool_contract=visual-harness$' /tmp/opencode_visual.out \
    && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/opencode_visual.out \
    && ! grep -q 'adapters/claude\|claude_setting\|settings.json\|statusline.sh' /tmp/opencode_visual.out; then
    ok "opencode visual harness reports adapter-native tool-contract"
  else
    bad "opencode visual harness should report adapter-native tool-contract"
  fi
else
  bad "opencode visual harness should report adapter-native tool-contract"
fi
cat >"$TMP/opencode-preview.html" <<'EOF'
<!doctype html><html><body><h1>OpenCode visual harness</h1></body></html>
EOF
if "$OPENCODE" visual-harness "$TMP/opencode-preview.html" --out "$TMP/opencode-visual" >/tmp/opencode_visual_file.out 2>/tmp/opencode_visual_file.err; then
  if grep -q '^adapter=opencode$' /tmp/opencode_visual_file.out \
    && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/opencode_visual_file.out \
    && grep -q '^status=ok$' /tmp/opencode_visual_file.out \
    && grep -q '^console_errors=0$' /tmp/opencode_visual_file.out \
    && [ -f "$TMP/opencode-visual/opencode-preview-html.png" ]; then
    ok "opencode visual harness renders HTML when checker dependencies exist"
  else
    bad "opencode visual harness should render HTML when checker dependencies exist"
  fi
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^adapter=opencode$' /tmp/opencode_visual_file.out \
    && grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/opencode_visual_file.out \
    && grep -q '^status=tool-contract$' /tmp/opencode_visual_file.out \
    && grep -q '^reason=playwright-unavailable$' /tmp/opencode_visual_file.out; then
    ok "opencode visual harness reports unavailable checker dependency"
  else
    bad "opencode visual harness should render or report unavailable checker dependency"
  fi
fi
cat >"$TMP/opencode-data-script.py" <<'EOF'
import sys

print("rows=3")
print("args=" + ",".join(sys.argv[1:]))
EOF
if "$OPENCODE" data-script --check "$TMP/opencode-data-script.py" >/tmp/opencode_data_script.out 2>/tmp/opencode_data_script.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_data_script.out \
  && grep -q '^tool_contract=data-script$' /tmp/opencode_data_script.out \
  && grep -q '^runtime_surface=adapter-owned-data-script$' /tmp/opencode_data_script.out \
  && grep -q '^check=python-compile$' /tmp/opencode_data_script.out \
  && grep -q '^status=ok$' /tmp/opencode_data_script.out; then
  ok "opencode data-script wrapper checks Python analysis scripts"
else
  bad "opencode data-script wrapper should check Python analysis scripts"
fi
if "$OPENCODE" claim-verify >/tmp/opencode_claim_verify.out 2>/tmp/opencode_claim_verify.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_claim_verify.out \
  && grep -q '^tool_contract=external-claim-verification$' /tmp/opencode_claim_verify.out \
  && grep -q '^runtime_surface=adapter-owned-claim-verify$' /tmp/opencode_claim_verify.out \
  && grep -q '^status=tool-contract$' /tmp/opencode_claim_verify.out; then
  ok "opencode claim-verify wrapper reports tool contract"
else
  bad "opencode claim-verify wrapper should report tool contract"
fi
if "$OPENCODE" claim-verify --check "model X is state of the art" >/tmp/opencode_claim_unavailable.out 2>/tmp/opencode_claim_unavailable.err; then
  bad "opencode claim-verify wrapper should report unavailable provider by default"
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^adapter=opencode$' /tmp/opencode_claim_unavailable.out \
    && grep -q '^reason=claim-verify-provider-unavailable$' /tmp/opencode_claim_unavailable.out; then
    ok "opencode claim-verify wrapper reports unavailable provider"
  else
    bad "opencode claim-verify wrapper should report unavailable provider"
  fi
fi
if "$OPENCODE" figure-gen >/tmp/opencode_figure_gen.out 2>/tmp/opencode_figure_gen.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_figure_gen.out \
  && grep -q '^tool_contract=figure-gen$' /tmp/opencode_figure_gen.out \
  && grep -q '^runtime_surface=adapter-owned-figure-gen$' /tmp/opencode_figure_gen.out \
  && grep -q '^status=tool-contract$' /tmp/opencode_figure_gen.out; then
  ok "opencode figure-gen wrapper reports tool contract"
else
  bad "opencode figure-gen wrapper should report tool contract"
fi
if "$OPENCODE" figure-gen --check "$TMP/missing-figure.py" >/tmp/opencode_figure_missing.out 2>/tmp/opencode_figure_missing.err; then
  bad "opencode figure-gen wrapper should fail missing script"
else
  rc=$?
  if [ "$rc" -eq 66 ] \
    && grep -q '^adapter=opencode$' /tmp/opencode_figure_missing.out \
    && grep -q '^reason=file-not-found$' /tmp/opencode_figure_missing.out; then
    ok "opencode figure-gen wrapper reports missing script"
  else
    bad "opencode figure-gen wrapper should report missing script"
  fi
fi
if "$OPENCODE" browser-fetch >/tmp/opencode_browser_fetch.out 2>/tmp/opencode_browser_fetch.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_browser_fetch.out \
  && grep -q '^tool_contract=browser-fetch$' /tmp/opencode_browser_fetch.out \
  && grep -q '^runtime_surface=adapter-owned-browser-fetch$' /tmp/opencode_browser_fetch.out \
  && grep -q '^status=tool-contract$' /tmp/opencode_browser_fetch.out; then
  ok "opencode browser-fetch wrapper reports tool contract"
else
  bad "opencode browser-fetch wrapper should report tool contract"
fi
if "$OPENCODE" browser-fetch --check not-a-url >/tmp/opencode_browser_bad_url.out 2>/tmp/opencode_browser_bad_url.err; then
  bad "opencode browser-fetch wrapper should fail bad URL"
else
  rc=$?
  if [ "$rc" -eq 65 ] \
    && grep -q '^adapter=opencode$' /tmp/opencode_browser_bad_url.out \
    && grep -q '^reason=bad-url$' /tmp/opencode_browser_bad_url.out; then
    ok "opencode browser-fetch wrapper reports bad URL"
  else
    bad "opencode browser-fetch wrapper should report bad URL"
  fi
fi
if "$OPENCODE" pdf-extract >/tmp/opencode_pdf_extract.out 2>/tmp/opencode_pdf_extract.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_pdf_extract.out \
  && grep -q '^tool_contract=pdf-extract$' /tmp/opencode_pdf_extract.out \
  && grep -q '^runtime_surface=adapter-owned-pdf-extract$' /tmp/opencode_pdf_extract.out \
  && grep -q '^status=tool-contract$' /tmp/opencode_pdf_extract.out; then
  ok "opencode pdf-extract wrapper reports tool contract"
else
  bad "opencode pdf-extract wrapper should report tool contract"
fi
if "$OPENCODE" pdf-extract --check "$TMP/missing.pdf" >/tmp/opencode_pdf_missing.out 2>/tmp/opencode_pdf_missing.err; then
  bad "opencode pdf-extract wrapper should fail missing PDF"
else
  rc=$?
  if [ "$rc" -eq 66 ] \
    && grep -q '^adapter=opencode$' /tmp/opencode_pdf_missing.out \
    && grep -q '^reason=file-not-found$' /tmp/opencode_pdf_missing.out; then
    ok "opencode pdf-extract wrapper reports missing PDF"
  else
    bad "opencode pdf-extract wrapper should report missing PDF"
  fi
fi
if "$OPENCODE" web-image-search >/tmp/opencode_web_image.out 2>/tmp/opencode_web_image.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_web_image.out \
  && grep -q '^tool_contract=web-image-search$' /tmp/opencode_web_image.out \
  && grep -q '^runtime_surface=adapter-owned-web-image-search$' /tmp/opencode_web_image.out \
  && grep -q '^status=tool-contract$' /tmp/opencode_web_image.out; then
  ok "opencode web-image-search wrapper reports tool contract"
else
  bad "opencode web-image-search wrapper should report tool contract"
fi
if "$OPENCODE" web-image-search --check "speech enhancement timeline" >/tmp/opencode_web_image_unavailable.out 2>/tmp/opencode_web_image_unavailable.err; then
  bad "opencode web-image-search wrapper should report unavailable provider by default"
else
  rc=$?
  if [ "$rc" -eq 69 ] \
    && grep -q '^adapter=opencode$' /tmp/opencode_web_image_unavailable.out \
    && grep -q '^reason=web-image-search-provider-unavailable$' /tmp/opencode_web_image_unavailable.out; then
    ok "opencode web-image-search wrapper reports unavailable provider"
  else
    bad "opencode web-image-search wrapper should report unavailable provider"
  fi
fi
if "$OPENCODE" verification-runner --check -- python3 >/tmp/opencode_verify_check.out 2>/tmp/opencode_verify_check.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_verify_check.out \
  && grep -q '^tool_contract=verification-runner$' /tmp/opencode_verify_check.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/opencode_verify_check.out \
  && grep -q '^check=command-available$' /tmp/opencode_verify_check.out \
  && grep -q '^status=ok$' /tmp/opencode_verify_check.out; then
  ok "opencode verification runner checks explicit commands"
else
  bad "opencode verification runner should check explicit commands"
fi
if "$OPENCODE" verification-runner --timeout 5 -- python3 -c 'print("verify-ok")' >/tmp/opencode_verify_run.out 2>/tmp/opencode_verify_run.err \
  && grep -q '^adapter=opencode$' /tmp/opencode_verify_run.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/opencode_verify_run.out \
  && grep -q '^status=ok$' /tmp/opencode_verify_run.out \
  && grep -q '^exit_code=0$' /tmp/opencode_verify_run.out \
  && grep -q 'verify-ok' /tmp/opencode_verify_run.out; then
  ok "opencode verification runner executes explicit commands"
else
  bad "opencode verification runner should execute explicit commands"
fi
if command -v opencode >/dev/null 2>&1; then
  if OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
    OPENCODE_CONFIG_CONTENT="{\"skills\":{\"paths\":[\"$ROOT/opencode_setting/opencode-skills\"]}}" \
    opencode debug skill --pure >/tmp/opencode_skills.out 2>/tmp/opencode_skills.err; then
    if grep -q '"name": "autopilot-code"' /tmp/opencode_skills.out \
      && grep -q "$ROOT/opencode_setting/opencode-skills/autopilot-code/SKILL.md" /tmp/opencode_skills.out \
      && ! grep -q '"location": ".*/\\.claude/skills' /tmp/opencode_skills.out; then
      ok "opencode native skill projection is discoverable without Claude compat autoload"
    else
      bad "opencode native skill projection should be discoverable without Claude compat autoload"
    fi
  else
    ok "opencode native skill runtime discovery skipped (opencode unavailable or sandboxed)"
  fi
else
  ok "opencode native skill runtime discovery skipped (opencode not installed)"
fi

echo "== opencode mode mapping =="
if "$OPENCODE" mode-info dev/backend >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=portable$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-persona$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper maps portable mode"
else
  bad "opencode mode wrapper should map portable mode"
fi
if "$OPENCODE" mode-info qa/security-review >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=portable$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-persona$' /tmp/opencode_mode.out \
  && grep -q 'read-only security review with OpenCode file and git diff tools' /tmp/opencode_mode.out \
  && ! grep -q '^tool_contract=' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper treats security-review as portable read-only guidance"
else
  bad "opencode mode wrapper should treat security-review as portable read-only guidance"
fi
opencode_design_modes_ok=1
# 재홈 2026-07-22: design personas re-homed to roles/units/design; _-prefixed internal
# fragments (_design-rules, _NOTES) have no mode projection.
for mode_file in "$ROOT"/roles/units/design/*.md; do
  mode_name=$(basename "$mode_file" .md)
  case "$mode_name" in _*) continue ;; esac
  mode="design/$mode_name"
  if ! "$OPENCODE" mode-info "$mode" >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
    || ! grep -q '^status=unsupported$' /tmp/opencode_mode.out \
    || ! grep -q '^realization=adapter-coupled$' /tmp/opencode_mode.out \
    || ! grep -q '^tool_contract=visual-harness$' /tmp/opencode_mode.out \
    || ! grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh visual-harness <file.html>$' /tmp/opencode_mode.out \
    || ! grep -q '^runtime_surface=adapter-owned-visual-harness$' /tmp/opencode_mode.out \
    || ! grep -q '^fallback=reference-only$' /tmp/opencode_mode.out; then
    opencode_design_modes_ok=0
    break
  fi
done
if [ "$opencode_design_modes_ok" -eq 1 ]; then
  ok "opencode mode wrapper marks every adapter-coupled design mode unsupported"
else
  bad "opencode mode wrapper should mark every adapter-coupled design mode unsupported"
fi
if "$OPENCODE" mode-info material/data-script >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=data-script$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh data-script --check <script.py>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-data-script$' /tmp/opencode_mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports material data-script contract surface"
else
  bad "opencode mode wrapper should report material data-script contract surface"
fi
if "$OPENCODE" mode-info material/figure-gen >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=figure-gen$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh figure-gen --check <script.py>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-figure-gen$' /tmp/opencode_mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports material figure-gen contract surface"
else
  bad "opencode mode wrapper should report material figure-gen contract surface"
fi
if "$OPENCODE" mode-info material/pdf-extract >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=pdf-extract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh pdf-extract --check <file.pdf>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-pdf-extract$' /tmp/opencode_mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports material pdf-extract contract surface"
else
  bad "opencode mode wrapper should report material pdf-extract contract surface"
fi
if "$OPENCODE" mode-info qa/test >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=verification-runner$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh verification-runner --check -- <command>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-verification-runner$' /tmp/opencode_mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports qa test verification runner surface"
else
  bad "opencode mode wrapper should report qa test verification runner surface"
fi
if "$OPENCODE" mode-info material/browser-fetch >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=browser-fetch$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh browser-fetch --check <url>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-browser-fetch$' /tmp/opencode_mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports material browser-fetch contract surface"
else
  bad "opencode mode wrapper should report material browser-fetch contract surface"
fi
if "$OPENCODE" mode-info material/web-image-search >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^realization=portable-with-tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=web-image-search$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh web-image-search --check <query>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-web-image-search$' /tmp/opencode_mode.out \
  && grep -q '^fallback=satisfy-tool-contract-or-report-unavailable$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports material web-image-search contract surface"
else
  bad "opencode mode wrapper should report material web-image-search contract surface"
fi
if "$OPENCODE" mode-info research/claim-verify >/tmp/opencode_mode.out 2>/tmp/opencode_mode.err \
  && grep -q '^status=tool-contract$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract=external-claim-verification$' /tmp/opencode_mode.out \
  && grep -q '^tool_contract_check=adapters/opencode/bin/preflight.sh claim-verify --check <claim>$' /tmp/opencode_mode.out \
  && grep -q '^runtime_surface=adapter-owned-claim-verify$' /tmp/opencode_mode.out; then
  ok "opencode mode wrapper reports named claim verification contract"
else
  bad "opencode mode wrapper should report named claim verification contract"
fi

echo "== opencode distill source =="
cat > "$TMP/opencode-export.json" <<'EOF'
{"messages":[
  {"id":"ou1","role":"user","time":"2026-06-29T00:00:00.000Z","content":[{"type":"text","text":"open hello"}]},
  {"id":"oa1","role":"assistant","time":"2026-06-29T00:00:01.000Z","content":[{"type":"text","text":"open world"}]},
  {"id":"ot1","type":"tool_call","name":"bash","time":"2026-06-29T00:00:02.000Z"}
]}
EOF
if OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" "$OPENCODE" distill-delta opencodesid >/tmp/opencode_delta.out 2>/tmp/opencode_delta.err \
  && grep -q '^\[user\] open hello' /tmp/opencode_delta.out \
  && grep -q '^\[assistant\] open world' /tmp/opencode_delta.out \
  && grep -q '^\[assistant\] \[tool:bash\]' /tmp/opencode_delta.out; then
  ok "opencode export source distills transcript"
else
  bad "opencode export source should distill transcript"
fi
# distill worker: no-tools opencode-run worker is implemented (gap closed). The
# deterministic guards below avoid a live model call.
# (1) disabled by default for direct calls → no-op exit 0
if "$OPENCODE_DISTILL" opencodesid "$TMP/flowproj" >/tmp/opencode_distill.out 2>/tmp/opencode_distill.err; then
  ok "opencode distill worker no-ops when OPENCODE_DISTILL_ENABLE unset"
else
  bad "opencode distill worker should no-op (exit 0) when disabled"
fi
# (2) recursion guard: MEM_DISTILL=1 → no-op even when enabled
if MEM_DISTILL=1 OPENCODE_DISTILL_ENABLE=1 "$OPENCODE_DISTILL" opencodesid "$TMP/flowproj" >/tmp/opencode_distill.out 2>/tmp/opencode_distill.err; then
  ok "opencode distill worker recursion guard no-ops under MEM_DISTILL=1"
else
  bad "opencode distill worker should no-op under MEM_DISTILL=1"
fi
# (3) enabled but opencode runtime unavailable → exit 69 (no hang, no model call)
if HOME="$TMP/no-oc-home" OPENCODE_DISTILL_ENABLE=1 OPENCODE_BIN="$TMP/no-such-opencode" \
   "$OPENCODE_DISTILL" opencodesid "$TMP/flowproj" >/tmp/opencode_distill.out 2>/tmp/opencode_distill.err; then
  bad "opencode distill worker should exit 69 when opencode runtime unavailable"
else
  [ "$?" -eq 69 ] && ok "opencode distill worker exits 69 when opencode runtime unavailable" \
    || bad "opencode distill worker wrong exit when runtime unavailable"
fi
# session-end: recursion guard writes no stamp under MEM_DISTILL=1
mkdir -p "$TMP/se-rec"
if MEM_STORE="$TMP/se-rec" MEM_DISTILL=1 "$OPENCODE" session-end "$TMP/flowproj" se-rec-sid >/dev/null 2>&1 \
  && [ ! -f "$TMP/se-rec/.opencode-distill-stamp-se-rec-sid" ]; then
  ok "opencode session-end recursion guard no-ops under MEM_DISTILL=1"
else
  bad "opencode session-end should no-op under MEM_DISTILL=1"
fi
# session-end: debounces repeated triggers within the min interval
mkdir -p "$TMP/se-deb"
OPENCODE_DISTILL_ENABLE=0 MEM_STORE="$TMP/se-deb" "$OPENCODE" session-end "$TMP/flowproj" se-deb-sid >/dev/null 2>&1
se_stamp=$(cat "$TMP/se-deb/.opencode-distill-stamp-se-deb-sid" 2>/dev/null || echo "")
OPENCODE_DISTILL_ENABLE=0 MEM_STORE="$TMP/se-deb" "$OPENCODE" session-end "$TMP/flowproj" se-deb-sid >/dev/null 2>&1
if [ -n "$se_stamp" ] \
  && [ "$(cat "$TMP/se-deb/.opencode-distill-stamp-se-deb-sid" 2>/dev/null)" = "$se_stamp" ]; then
  ok "opencode session-end debounces repeated triggers"
else
  bad "opencode session-end should debounce repeated triggers"
fi

# session-end selects curate: preflight.sh session-end passes "curate" as the
# distill-worker's third positional argument (:756); distill-propose stays
# increment. Both are asserted against the source, not a live model call.
if grep -q 'distill-worker.sh" "\$sid" "\$cwd" curate' adapters/opencode/bin/preflight.sh; then
  ok "opencode preflight session-end selects curate mode"
else
  bad "opencode preflight session-end should select curate mode"
fi
if awk '/distill-propose\)/{flag=1} flag{print} flag && /;;/{exit}' adapters/opencode/bin/preflight.sh \
  | grep -q 'distill-worker.sh" "\$sid" "\$cwd"$'; then
  ok "opencode preflight distill-propose stays default increment"
else
  bad "opencode preflight distill-propose should stay default increment"
fi

# A4: OpenCode distill-worker mode/lock/advance, using a stub OPENCODE_BIN so
# no live model call is made. The stub reads/captures the fed prompt from
# stdin so mode selection (increment vs curate) is verifiable without a model.
OC_STUB="$TMP/oc-stub"
mkdir -p "$OC_STUB"
cat > "$OC_STUB/opencode" <<'STUBEOF'
#!/bin/sh
cat > "$OPENCODE_STUB_CAPTURE" 2>/dev/null || true
STUBEOF
chmod +x "$OC_STUB/opencode"
OC_STUB_FAIL="$TMP/oc-stub-fail"
mkdir -p "$OC_STUB_FAIL"
cat > "$OC_STUB_FAIL/opencode" <<'STUBEOF'
#!/bin/sh
exit 1
STUBEOF
chmod +x "$OC_STUB_FAIL/opencode"

OC_CAPTURE_CURATE="$TMP/opencode-curate-capture.txt"
OC_STORE_CURATE="$TMP/oc-store-curate"
if OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" OPENCODE_DISTILL_ENABLE=1 OPENCODE_BIN="$OC_STUB/opencode" \
   OPENCODE_STUB_CAPTURE="$OC_CAPTURE_CURATE" MEM_STORE="$OC_STORE_CURATE" \
   "$OPENCODE_DISTILL" oc-curate-sid "$TMP/flowproj" curate >/tmp/opencode_curate.out 2>/tmp/opencode_curate.err \
   && grep -q 'no-tools session memory curator' "$OC_CAPTURE_CURATE"; then
  ok "opencode distill worker curate mode builds the curator prompt"
else
  bad "opencode distill worker curate mode should build the curator prompt"
fi
OC_CAPTURE_INCREMENT="$TMP/opencode-increment-capture.txt"
OC_STORE_INCREMENT="$TMP/oc-store-increment"
if OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" OPENCODE_DISTILL_ENABLE=1 OPENCODE_BIN="$OC_STUB/opencode" \
   OPENCODE_STUB_CAPTURE="$OC_CAPTURE_INCREMENT" MEM_STORE="$OC_STORE_INCREMENT" \
   "$OPENCODE_DISTILL" oc-increment-sid "$TMP/flowproj" >/tmp/opencode_increment_mode.out 2>/tmp/opencode_increment_mode.err \
   && grep -q 'You are a memory distillation worker' "$OC_CAPTURE_INCREMENT" \
   && ! grep -q 'no-tools session memory curator' "$OC_CAPTURE_INCREMENT"; then
  ok "opencode distill worker defaults to increment mode and builds the increment prompt"
else
  bad "opencode distill worker should default to increment mode"
fi
OC_STORE_LOCK="$TMP/oc-store-lock"
mkdir -p "$OC_STORE_LOCK/.opencode-distill-lock-oc-locksid"
if OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" OPENCODE_DISTILL_ENABLE=1 OPENCODE_BIN="$OC_STUB/opencode" MEM_STORE="$OC_STORE_LOCK" \
   "$OPENCODE_DISTILL" oc-locksid "$TMP/flowproj" increment >/tmp/opencode_lock.out 2>/tmp/opencode_lock.err \
   && grep -q 'another distill in progress' /tmp/opencode_lock.err; then
  ok "opencode distill worker skips when the same-sid lock is already held"
else
  bad "opencode distill worker should skip when the same-sid lock is already held"
fi
rmdir "$OC_STORE_LOCK/.opencode-distill-lock-oc-locksid" 2>/dev/null || true

OC_STORE_ADV="$TMP/oc-store-adv"
if OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" OPENCODE_DISTILL_ENABLE=1 OPENCODE_DISTILL_APPLY=1 OPENCODE_BIN="$OC_STUB/opencode" MEM_STORE="$OC_STORE_ADV" \
   "$OPENCODE_DISTILL" oc-adv-sid "$TMP/flowproj" increment >/tmp/opencode_adv.out 2>/tmp/opencode_adv.err \
   && AGENT_HOME="$ROOT" MEM_STORE="$OC_STORE_ADV" OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" \
      python3 "$ROOT/tools/memory/mem.py" distill oc-adv-sid --source opencode >/tmp/opencode_adv_check.out 2>/tmp/opencode_adv_check.err \
   && [ ! -s /tmp/opencode_adv_check.out ]; then
  ok "opencode distill worker advances the marker after a successful exec in apply mode"
else
  bad "opencode distill worker should advance the marker after a successful exec in apply mode"
fi
OC_STORE_NOADV="$TMP/oc-store-noadv"
OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" OPENCODE_DISTILL_ENABLE=1 OPENCODE_DISTILL_APPLY=1 OPENCODE_BIN="$OC_STUB_FAIL/opencode" OPENCODE_DISTILL_TIMEOUT=5 MEM_STORE="$OC_STORE_NOADV" \
  "$OPENCODE_DISTILL" oc-noadv-sid "$TMP/flowproj" increment >/tmp/opencode_noadv.out 2>/tmp/opencode_noadv.err
if AGENT_HOME="$ROOT" MEM_STORE="$OC_STORE_NOADV" OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" \
   python3 "$ROOT/tools/memory/mem.py" distill oc-noadv-sid --source opencode >/tmp/opencode_noadv_check.out 2>/tmp/opencode_noadv_check.err \
   && [ -s /tmp/opencode_noadv_check.out ]; then
  ok "opencode distill worker does not advance the marker on a failed exec"
else
  bad "opencode distill worker should not advance the marker on a failed exec"
fi
OC_STORE_PREVIEW="$TMP/oc-store-preview"
OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" OPENCODE_DISTILL_ENABLE=1 OPENCODE_BIN="$OC_STUB/opencode" MEM_STORE="$OC_STORE_PREVIEW" \
  "$OPENCODE_DISTILL" oc-preview-sid "$TMP/flowproj" increment >/tmp/opencode_preview.out 2>/tmp/opencode_preview.err
if AGENT_HOME="$ROOT" MEM_STORE="$OC_STORE_PREVIEW" OPENCODE_EXPORT_FILE="$TMP/opencode-export.json" \
   python3 "$ROOT/tools/memory/mem.py" distill oc-preview-sid --source opencode >/tmp/opencode_preview_check.out 2>/tmp/opencode_preview_check.err \
   && [ -s /tmp/opencode_preview_check.out ]; then
  ok "opencode distill worker does not advance the marker on a preview-only (non-apply) run"
else
  bad "opencode distill worker should not advance the marker on a preview-only run"
fi

echo "== SD-11b stage-dispatch gate (deny 상향 + opt-out + intensity 불명) =="
# (i) conductor + standard + code-plan, NO opt-out → HARD DENY (CLI: exit 2, stderr ⛔)
err=$(CLAUDE_CODE_CHILD_SESSION=1 AGENT_DISPATCH_SELF_SLUG=cyc "$SDR" --skill code-plan --dispatch-depth 1 --intensity standard 2>&1 >/dev/null); rc=$?
if [ "$rc" -eq 2 ] \
  && printf '%s' "$err" | grep -q 'stage-dispatch denied' \
  && printf '%s' "$err" | grep -q 'dispatch-node.py' \
  && printf '%s' "$err" | grep -q -- '--route <route-file>' \
  && printf '%s' "$err" | grep -q -- '--jobs <canonical-jobs.log>' \
  && printf '%s' "$err" | grep -q 'attempt_id'; then
  ok "SDR hard-denies conductor+standard+code-plan without opt-out (exit 2)"
else bad "SDR should deny conductor+standard+code-plan (rc=$rc) [$err]"; fi
# (i-opt) same but STAGE_DISPATCH_INLINE_OK=1 → soft reminder (additionalContext, exit 0)
out=$(CLAUDE_CODE_CHILD_SESSION=1 STAGE_DISPATCH_INLINE_OK=1 AGENT_DISPATCH_SELF_SLUG=cyc "$SDR" --skill code-plan --dispatch-depth 1 --intensity standard 2>/dev/null); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '"additionalContext"' && printf '%s' "$out" | grep -q 'stage-dispatch'; then
  ok "SDR downgrades to reminder under STAGE_DISPATCH_INLINE_OK=1 opt-out"
else bad "SDR should emit reminder (not deny) under opt-out [$out]"; fi
# (i-unknown) conductor + code-plan but intensity empty (old wrapper) → reminder, NEVER deny
out=$(CLAUDE_CODE_CHILD_SESSION=1 AGENT_DISPATCH_SELF_SLUG=cyc "$SDR" --skill code-plan --dispatch-depth 1 --intensity "" 2>/dev/null); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '"additionalContext"'; then
  ok "SDR keeps soft reminder (no deny) when intensity is unknown (backward compat)"
else bad "SDR should reminder (not deny) for unknown intensity [$out] rc=$rc"; fi
# (ii) direct/quick → no-op (direct inline; quick is a depth-1 one-shot worker)
out=$(CLAUDE_CODE_CHILD_SESSION=1 "$SDR" --skill code-plan --dispatch-depth 1 --intensity quick 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok "SDR no-ops for intensity=quick" || bad "SDR should no-op for quick one-shot worker [$out] rc=$rc"
# (iii) depth-2 stage session → no-op
out=$(CLAUDE_CODE_CHILD_SESSION=1 "$SDR" --skill code-plan --dispatch-depth 2 --intensity standard 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok "SDR no-ops for depth-2 stage session" || bad "SDR should no-op for depth-2 [$out] rc=$rc"
# (iv) non-code skill → no-op
out=$(CLAUDE_CODE_CHILD_SESSION=1 "$SDR" --skill autopilot-draft --dispatch-depth 1 --intensity standard 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok "SDR no-ops for non-code skill" || bad "SDR should no-op for non-code skill [$out] rc=$rc"
# (v) main (no dispatch depth) → no-op
out=$("$SDR" --skill code-plan --dispatch-depth "" --intensity standard 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok "SDR no-ops for main (no dispatch depth)" || bad "SDR should no-op for main [$out] rc=$rc"
# (vi) §5.10: a runtime child-session marker alone is a teammate session, not a conductor → no-op
out=$(CLAUDE_CODE_CHILD_SESSION=1 "$SDR" --skill code-plan --dispatch-depth "" --intensity standard 2>&1); rc=$?
[ "$rc" -eq 0 ] && [ -z "$out" ] && ok "SDR no-ops for teammate session (runtime marker, no depth)" || bad "SDR should no-op for teammate session [$out] rc=$rc"
# (vii) depth is sufficient evidence without the runtime marker → still denies
err=$(env -u CLAUDE_CODE_CHILD_SESSION AGENT_DISPATCH_SELF_SLUG=cyc "$SDR" --skill code-plan --dispatch-depth 1 --intensity standard 2>&1 >/dev/null); rc=$?
[ "$rc" -eq 2 ] && printf '%s' "$err" | grep -q 'stage-dispatch denied' \
  && ok "SDR denies on harness depth marker alone (no runtime marker)" \
  || bad "SDR should deny on depth alone (rc=$rc) [$err]"

echo "== SD-14b conductor Stop gate (UNREGISTERED / CLI unit) =="
sdtmp="$(mktemp -d)"
sdjobs="$sdtmp/jobs.log"
: > "$sdjobs"
# no open children → exit 0, no block
out=$(CLAUDE_CODE_CHILD_SESSION=1 AGENT_DISPATCH_DEPTH=1 "$CSG" --self-slug cyc --jobs "$sdjobs" 2>/dev/null)
[ -z "$out" ] && ok "CSG no block when no open children" || bad "CSG should not block with no children [$out]"
# open child, dead (no transcript) → block with diagnose
printf '%s\t%s\t%s\t%s\t%s\t%s\n' "2026-07-10T00:00:00" "open" "repo" "$sdtmp/wt/d" "dc" "capability=code-plan,parent=cyc" >> "$sdjobs"
out=$(CLAUDE_CODE_CHILD_SESSION=1 AGENT_DISPATCH_DEPTH=1 DISPATCH_RUNTIME_ROOT="$sdtmp/rt" "$CSG" --self-slug cyc --jobs "$sdjobs" 2>/dev/null)
if printf '%s' "$out" | grep -q '"decision":[[:space:]]*"block"' && printf '%s' "$out" | grep -q 'SUSPECT/DEAD'; then
  ok "CSG blocks with diagnose for dead open child"
else bad "CSG should block-diagnose for dead child [$out]"; fi
# stop_hook_active → no block (loop guard)
out=$(CLAUDE_CODE_CHILD_SESSION=1 AGENT_DISPATCH_DEPTH=1 "$CSG" --self-slug cyc --jobs "$sdjobs" --stop-active true 2>/dev/null)
[ -z "$out" ] && ok "CSG no block when stop_hook_active" || bad "CSG should no-op when stop_hook_active [$out]"
# non-conductor env (no dispatch depth) → no block
out=$("$CSG" --self-slug cyc --jobs "$sdjobs" 2>/dev/null)
[ -z "$out" ] && ok "CSG no block for non-conductor env" || bad "CSG should no-op for non-conductor [$out]"
# §5.10: a runtime child-session marker alone is a teammate session → no block
out=$(CLAUDE_CODE_CHILD_SESSION=1 "$CSG" --self-slug cyc --jobs "$sdjobs" 2>/dev/null)
[ -z "$out" ] && ok "CSG no block for teammate session (runtime marker, no depth)" || bad "CSG should no-op for teammate session [$out]"
# depth marker alone is sufficient conductor evidence → still blocks
out=$(env -u CLAUDE_CODE_CHILD_SESSION AGENT_DISPATCH_DEPTH=1 DISPATCH_RUNTIME_ROOT="$sdtmp/rt" "$CSG" --self-slug cyc --jobs "$sdjobs" 2>/dev/null)
printf '%s' "$out" | grep -q '"decision":[[:space:]]*"block"' \
  && ok "CSG blocks on harness depth marker alone (no runtime marker)" \
  || bad "CSG should block on depth alone [$out]"
rm -rf "$sdtmp"

echo "== §5.10 main-session lifecycle predicates: harness markers only =="
# A runtime injects CLAUDE_CODE_CHILD_SESSION into every child process an ordinary
# interactive session spawns (hooks included), so an agent-team teammate session carries
# it. Only harness-planted markers may gate main-session lifecycle hooks.
predtmp="$(mktemp -d)"
mkdir -p "$predtmp/desk" "$predtmp/proj"
printf '# oncall\n' > "$predtmp/oncall.md"
HERDR="$ROOT/hooks/herdr-agent-state.sh"

# (a) recall bridge: teammate marker → still publishes a recall opportunity receipt.
pred_recall() { # $1=case-tag ; env assignments come from the caller
  rcpt="$predtmp/receipts-$1"
  rm -rf "$rcpt"
  MEM_PY="$ROOT/tools/memory/mem.py" MEM_STORE="$predtmp/store-$1" MEM_RECALL_RECEIPTS="$rcpt" \
    "$RECALL" --prompt "일반 질문" --cwd "$predtmp/proj" --session-id "pred-$1" \
      --turn-id "turn-$1" --format text >/dev/null 2>&1 || true
  find "$rcpt" -type f -name '*.json' -print -quit 2>/dev/null | grep -q .
}
if CLAUDE_CODE_CHILD_SESSION=1 pred_recall teammate; then
  ok "recall bridge stays active for a teammate session (runtime marker only)"
else bad "recall bridge must not treat CLAUDE_CODE_CHILD_SESSION as worker evidence"; fi
for assignment in AGENT_SESSION_ROLE=worker AGENT_DISPATCH_CHILD=1 AGENT_DISPATCH_DEPTH=2; do
  if env "$assignment" bash -c 'MEM_PY="$1/tools/memory/mem.py" MEM_STORE="$2/store-w" MEM_RECALL_RECEIPTS="$2/receipts-w" \
      "$3" --prompt "일반 질문" --cwd "$2/proj" --session-id pred-w --turn-id turn-w --format text >/dev/null 2>&1; \
      ! find "$2/receipts-w" -type f -name "*.json" -print -quit 2>/dev/null | grep -q .' _ "$ROOT" "$predtmp" "$RECALL"; then
    ok "recall bridge stays silent for worker marker $assignment"
  else bad "recall bridge should no-op for worker marker $assignment"; fi
  rm -rf "$predtmp/receipts-w" "$predtmp/store-w"
done

# (b) briefing desk: teammate marker → still briefs; harness markers → silent.
pred_brief() { # $1=case-tag ; prints nothing, returns 0 when the briefing ran
  MEM_BRIEFING_DESK="$predtmp/desk" MEM_BRIEFING_ONCALL="$predtmp/oncall.md" \
    MEM_PY="$ROOT/tools/memory/mem.py" MEM_STORE="$predtmp/bstore-$1" \
    bash "$BRIEF" --cwd "$predtmp/desk" --format text 2>/dev/null | grep -q .
}
if CLAUDE_CODE_CHILD_SESSION=1 pred_brief teammate; then
  ok "briefing desk stays active for a teammate session (runtime marker only)"
else bad "briefing desk must not treat CLAUDE_CODE_CHILD_SESSION as worker evidence"; fi
for assignment in AGENT_SESSION_ROLE=worker AGENT_DISPATCH_CHILD=1 AGENT_DISPATCH_DEPTH=2; do
  bcase="b$(printf '%s' "$assignment" | tr -dc 'a-zA-Z')"
  if env "$assignment" bash -c 'MEM_BRIEFING_DESK="$1/desk" MEM_BRIEFING_ONCALL="$1/oncall.md" \
      MEM_PY="$2/tools/memory/mem.py" MEM_STORE="$1/bstore-$4" bash "$3" --cwd "$1/desk" --format text 2>/dev/null \
      | grep -q .' _ "$predtmp" "$ROOT" "$BRIEF" "$bcase"; then
    bad "briefing desk should no-op for worker marker $assignment"
  else ok "briefing desk stays silent for worker marker $assignment"; fi
done

# (c) herdr pane state: teammate marker → still reports to the pane socket.
predsock="$predtmp/herdr.sock"
predrecv="$predtmp/herdr.received"
python3 - "$predsock" "$predrecv" >/dev/null 2>&1 <<'PY' &
import socket, sys, os
path, out = sys.argv[1], sys.argv[2]
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path); srv.listen(8); srv.settimeout(20)
try:
    while True:
        conn, _ = srv.accept()
        data = conn.recv(4096)
        if data:
            with open(out, "ab") as handle:
                handle.write(data)
        conn.close()
except Exception:
    pass
PY
predsrv=$!
predwait=0
while [ ! -S "$predsock" ] && [ "$predwait" -lt 50 ]; do predwait=$((predwait + 1)); sleep 0.1; done
pred_herdr() { # $1=pane-id ; env assignments come from the caller
  printf '{"hook_event_name":"PreToolUse","session_id":"pred-herdr"}' \
    | HERDR_ENV=1 HERDR_SOCKET_PATH="$predsock" HERDR_PANE_ID="$1" sh "$HERDR" working >/dev/null 2>&1 || true
}
: > "$predrecv"
CLAUDE_CODE_CHILD_SESSION=1 pred_herdr pane-teammate
predwait=0
while ! grep -q 'pane-teammate' "$predrecv" 2>/dev/null && [ "$predwait" -lt 30 ]; do predwait=$((predwait + 1)); sleep 0.1; done
if grep -q 'pane-teammate' "$predrecv" 2>/dev/null; then
  ok "herdr pane state stays active for a teammate session (runtime marker only)"
else bad "herdr pane state must not treat CLAUDE_CODE_CHILD_SESSION as worker evidence"; fi
: > "$predrecv"
for assignment in AGENT_SESSION_ROLE=worker AGENT_DISPATCH_CHILD=1 AGENT_DISPATCH_DEPTH=2; do
  env "$assignment" sh -c 'printf "{\"hook_event_name\":\"PreToolUse\",\"session_id\":\"pred-herdr\"}" \
    | HERDR_ENV=1 HERDR_SOCKET_PATH="$1" HERDR_PANE_ID=pane-worker sh "$2" working >/dev/null 2>&1' _ "$predsock" "$HERDR" || true
done
sleep 0.3
if ! grep -q 'pane-worker' "$predrecv" 2>/dev/null; then
  ok "herdr pane state stays silent for every harness worker marker"
else bad "herdr pane state should no-op for harness worker markers"; fi
kill "$predsrv" 2>/dev/null || true
wait "$predsrv" 2>/dev/null || true
rm -rf "$predtmp"

echo "== drill runtime failure propagation + Fleet limit marker =="
drilltmp="$TMP/drill-runtime-failure"
drilljobs="$drilltmp/jobs.log"
fake_codex="$drilltmp/fake-codex"
mkdir -p "$drilltmp/case/repo"
: > "$drilljobs"
: > "$drilltmp/case/prompt.md"
printf '%s\n' \
  '#!/bin/sh' \
  "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"fake-thread\"}' '{\"type\":\"turn.failed\",\"error\":{\"message\":\"You have hit your usage limit. Try again at Jan 1st, 2099 9:06 AM.\"}}'" \
  'exit 0' > "$fake_codex"
chmod +x "$fake_codex"

runner_out=$(AGENT_HOME="$drilltmp/agent-home" AGENT_DISPATCH_JOBS="$drilljobs" \
  CODEX_BIN="$fake_codex" LOOP_CODEX_SANDBOX=danger-full-access \
  bash -c '. "$1"; run_case_on_adapter codex "$2" "$3" 10 "" "$4" "$5"' \
    _ "$ROOT/loops/lib-runner.sh" "$drilltmp/case/prompt.md" "$drilltmp/case/repo" \
    "$drilltmp/case.json" "$drilltmp/case.transcript.txt")
runner_rc=$?
if [ "$runner_rc" -eq 70 ] && [ "$runner_out" = '?|0|0|0' ]; then
  ok "drill adapter runner propagates Codex turn.failed as exit 70"
else
  bad "drill adapter runner should propagate turn.failed (rc=$runner_rc metrics=$runner_out)"
fi
last_runner_row=$(tail -n 1 "$drilljobs")
if printf '%s' "$last_runner_row" | grep -q $'\tdone\t' \
  && printf '%s' "$last_runner_row" | grep -q 'note=dead-usage-limit' \
  && printf '%s' "$last_runner_row" | grep -q 'reset=2099-01-01T09:06:00'; then
  ok "drill runner closes one Fleet row with usage-limit death and reset"
else
  bad "drill runner should close its existing Fleet row [$last_runner_row]"
fi
usage_out=$(bash "$ROOT/utilities/usage-check.sh" --harness codex --jobs "$drilljobs")
if printf '%s\n' "$usage_out" | grep -q '^codex limited(2099-01-01T09:06:00)$'; then
  ok "Fleet usage check exposes the drill Codex limit"
else
  bad "Fleet usage check should expose drill limit [$usage_out]"
fi

mini_drill="$drilltmp/mini-drill"
mini_case="$mini_drill/cases/runtime_fail"
mkdir -p "$mini_case"
printf '%s\n' '#!/bin/sh' 'mkdir -p "$1/repo"' > "$mini_case/fixture.sh"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$mini_case/assert.sh"
: > "$mini_case/prompt.md"
chmod +x "$mini_case/fixture.sh" "$mini_case/assert.sh"
AGENT_HOME="$drilltmp/agent-home" AGENT_DISPATCH_JOBS="$drilljobs" \
  CODEX_BIN="$fake_codex" DRILL_HOME="$mini_drill" DRILL_ADAPTER=codex \
  DRILL_SKIP_CONFORMANCE=1 DRILL_AUTO_DIAG=1 \
  bash "$ROOT/loops/drill/run.sh" runtime_fail > "$drilltmp/run.out" 2>&1
drill_rc=$?
if [ "$drill_rc" -eq 1 ] && grep -q '| runtime_fail | FAIL |' "$mini_drill"/results/*/summary.md; then
  ok "drill run exits non-zero when runtime fails despite a passing assertion"
else
  bad "drill run should fail on runtime failure (rc=$drill_rc) [$(cat "$drilltmp/run.out")]"
fi
if ! find "$mini_drill/results" -name '*.diagnosis.json' -print -quit | grep -q .; then
  ok "drill skips redundant auto-diagnosis after runtime failure"
else
  bad "drill should not retry unavailable runtime for auto-diagnosis"
fi

printf 'PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
