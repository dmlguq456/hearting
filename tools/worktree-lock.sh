#!/usr/bin/env bash
# One owner of the test-suite worktree lock (2026-09-10).
#
# Several suites read or write the *live* checkout rather than a fixture:
# `adaptation-guard.test.sh` rewrites four tracked files (a hook, the
# adaptation exemptions TSV, `adapters/claude/CLAUDE.md`, a codex wrapper) to
# prove the guard reddens; `generated-projections.test.sh` edits
# `harness-manifest.json` and reruns the generator, rewriting every
# projection. Meanwhile `bytecode-cache-tolerance.test.sh` runs the repository
# checks against that same tree, `check_surface_budget.test.py` measures the
# budgeted surfaces (CLAUDE.md among them), and `portable-guards.test.sh`
# reads the codex wrapper. The runner executes suites in parallel over one
# working tree, so any pair of these can see the other mid-edit: measured in
# CI 2026-09-10 as `M adapters/codex/skills/post-it/SKILL.md` in a suite that
# never touches it, and as `field 4='-'` from an exemptions file that was
# briefly rewritten.
#
# The lock lives in the shared git directory, NOT under $TMPDIR: the runner
# hands every suite its own TMPDIR, so a $TMPDIR-derived path gives each suite
# a private lock and no exclusion at all (the first version of this guard
# changed nothing in CI). The git common dir is the one path two suites in the
# same checkout always agree on, and it is outside the tracked tree, so the
# lock file cannot dirty the `git status` these suites assert on.
#
# `worktree_lock_path <root>` prints the path; `worktree_lock_acquire <root>
# [seconds]` takes it on fd 9 and returns non-zero on timeout. Blocking, not
# non-blocking: every one of these suites has to run.

worktree_lock_path() {
  _wl_root=${1:-.}
  _wl_dir=$(git -C "$_wl_root" rev-parse --git-common-dir 2>/dev/null || true)
  case "$_wl_dir" in
    "") printf '/tmp/hearting-worktree-mutation.lock' ;;
    /*) printf '%s/hearting-worktree-mutation.lock' "$_wl_dir" ;;
    *)  printf '%s/%s/hearting-worktree-mutation.lock' "$_wl_root" "$_wl_dir" ;;
  esac
}

worktree_lock_acquire() {
  _wl_target=$(worktree_lock_path "${1:-.}")
  _wl_wait=${2:-900}
  command -v flock >/dev/null 2>&1 || return 0   # no flock: nothing to hold
  exec 9>"$_wl_target" || return 0
  if ! flock -w "$_wl_wait" 9; then
    echo "worktree lock not acquired within ${_wl_wait}s: $_wl_target" >&2
    return 1
  fi
  return 0
}
