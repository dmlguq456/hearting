#!/usr/bin/env sh
# Test-only memory environment bootstrap. Never resolves or inspects a
# production store; every caller gets fresh roots before importing mem.py.

# A suite that cannot get its private roots stops here (exit 70): run on with
# the caller's HOME, store, TMPDIR and cwd, its `git -C "$EMPTY"`/`cd "$EMPTY"`
# lines would act on the current checkout and the real memory store.
hearting_test_isolate() {
  _hearting_test_make_roots "$@" && return 0
  echo "test isolation failed: no private memory test root; nothing was run" >&2
  exit 70
}

_hearting_test_make_roots() {
  _hti_root=${1:-}
  if [ -z "$_hti_root" ]; then
    _hti_root=$(mktemp -d "${TMPDIR:-/tmp}/hearting-memory-test.XXXXXX") || return 1
  fi
  case "$_hti_root" in
    /*) ;;
    *) echo "test isolation root must be absolute: $_hti_root" >&2; return 1 ;;
  esac
  mkdir -p "$_hti_root" || return 1
  _hti_root=$(CDPATH= cd -P -- "$_hti_root" && pwd) || return 1
  export HOME="$_hti_root/home"
  export XDG_DATA_HOME="$_hti_root/data"
  export XDG_STATE_HOME="$_hti_root/state"
  export XDG_CONFIG_HOME="$_hti_root/config"
  export TMPDIR="$_hti_root/tmp"
  export MEM_STORE="$_hti_root/store"
  export MEM_PROJECTS="$_hti_root/projects"
  export GIT_CONFIG_GLOBAL="$_hti_root/gitconfig"
  mkdir -p "$HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME/hearting" \
    "$TMPDIR" "$MEM_STORE" "$MEM_PROJECTS" || return 1
  # The shared exchange policy lives in $XDG_CONFIG_HOME/hearting/memory-sync.json
  # and an ambient MEM_SYNC_*/MEM_DUMP_PUSH variable overrides it: pin the remote off so no
  # test can reach a real remote unless it names a local bare repository itself.
  printf '%s\n' '{"enabled": false}' >"$XDG_CONFIG_HOME/hearting/memory-sync.json" || return 1
  unset MEM_SYNC_REMOTE MEM_SYNC_REMOTE_URL MEM_SYNC_DIR MEM_SYNC_REF MEM_DUMP_PUSH
  # No detached exchange worker unless a test turns the scheduler on itself: the
  # hundreds of `mem add` calls in the suites must not race temp-dir cleanup.
  export MEM_EXCHANGE_AUTO=0
  git config --global --add safe.directory "$PWD" >/dev/null 2>&1 || return 1
  for _hti_var in HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CONFIG_HOME TMPDIR MEM_STORE; do
    eval "_hti_path=\${$_hti_var}"
    case "$_hti_path" in
      "$_hti_root"/*) ;;
      *) echo "test isolation escape: $_hti_var=$_hti_path root=$_hti_root" >&2; return 1 ;;
    esac
  done
  export HEARTING_TEST_ROOT="$_hti_root"
  unset _hti_root _hti_path _hti_var
}

# Deliberate derived-store tests may use default resolution, but retain all
# fresh roots and never inherit an ambient store.
hearting_test_derived_env() {
  [ -n "${HEARTING_TEST_ROOT:-}" ] || { echo "isolation not initialized" >&2; return 1; }
  env -u MEM_STORE "$@"
}
