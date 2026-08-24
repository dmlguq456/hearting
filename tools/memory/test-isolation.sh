#!/usr/bin/env sh
# Test-only memory environment bootstrap. Never resolves or inspects a
# production store; every caller gets fresh roots before importing mem.py.

hearting_test_isolate() {
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
  export MEM_STORE="$_hti_root/store"
  export MEM_PROJECTS="$_hti_root/projects"
  export GIT_CONFIG_GLOBAL="$_hti_root/gitconfig"
  mkdir -p "$HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$MEM_STORE" "$MEM_PROJECTS" || return 1
  git config --global --add safe.directory "$PWD" >/dev/null 2>&1 || return 1
  for _hti_var in HOME XDG_DATA_HOME XDG_STATE_HOME MEM_STORE; do
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
