#!/usr/bin/env sh
set -eu
case_name=${2:-}; adapter=${TEST_ADAPTER:-$(basename "$(dirname "$(dirname "$0")")")}; repo_root=$(CDPATH= cd -P "$(dirname "$0")/../../.." && pwd -P); real_preflight=$repo_root/adapters/$adapter/bin/preflight.sh
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT HUP INT TERM
pass(){ printf 'PASS %s\n' "$1"; }
fail(){ printf 'FAIL %s: %s\n' "$1" "$2" >&2; exit 1; }
skip_typed(){ printf 'SKIP %s: %s\n' "$1" "$2"; exit 0; }
make_source(){ source=$1; mkdir -p "$source/core" "$source/roles" "$source/capabilities" "$source/utilities" "$source/hooks" "$source/adapters/$adapter/bin" "$source/adapters/$adapter/utilities"; : > "$source/core/CORE.md"; : > "$source/utilities/artifact-root.sh"; : > "$source/adapters/$adapter/utilities/agent-home.sh"; chmod +x "$source/utilities/artifact-root.sh" "$source/adapters/$adapter/utilities/agent-home.sh"; cp "$real_preflight" "$source/adapters/$adapter/bin/preflight.sh"; chmod +x "$source/adapters/$adapter/bin/preflight.sh"; for guard in git-state-guard.sh core-first-guard.sh artifact-guard.sh builtin-memory-guard.sh worktree-path-guard.sh; do printf '#!/usr/bin/env sh\nprintf "%%s\\n" "$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)/%s" >> "${FIXTURE_LOG:?}"\nexit 0\n' "$guard" > "$source/hooks/$guard"; chmod +x "$source/hooks/$guard"; done; printf '#!/usr/bin/env python3\nimport os\nwith open(os.environ["FIXTURE_LOG"], "a") as h: h.write(os.path.realpath(__file__) + "\\n")\n' > "$source/hooks/material-route-guard.py"; chmod +x "$source/hooks/material-route-guard.py"; }
assert_log(){ expected=$1; [ -f "$FIXTURE_LOG" ] || fail "$case_name" "fixture log missing"; printf '%s\n' "$expected" | tr ' ' '\n' > "$tmp/expected.log"; diff -u "$tmp/expected.log" "$FIXTURE_LOG" >/dev/null || fail "$case_name" "unexpected guard identities"; case "$(cat "$FIXTURE_LOG")" in *"/adapters/"*) fail "$case_name" "adapter hook observed";; esac; }
run_write(){ source=$1; target=$2; (cd "$source" && FIXTURE_LOG="$FIXTURE_LOG" AGENT_HOME= HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$source/adapters/$adapter/bin/preflight.sh" write "$target" test-session); }
layout(){ source=$tmp/source-$case_name; mkdir -p "$tmp/home" "$tmp/codex" "$tmp/config" "$tmp/runtime/.claude"; FIXTURE_LOG=$tmp/$case_name.log; export FIXTURE_LOG; make_source "$source"; expected_source=$source; : > "$source/target"; mkdir -p "$tmp/runtime/.claude/hooks"; printf '#!/usr/bin/env sh\nexit 99\n' > "$tmp/runtime/.claude/hooks/core-first-guard.sh"; chmod +x "$tmp/runtime/.claude/hooks/core-first-guard.sh"; case "$case_name" in runtime-home-logical) ln -s "$source/adapters" "$tmp/runtime/.claude/adapters"; run_write "$tmp/runtime/.claude" "$source/target";; bundle-hearting) ln -s "$source" "$tmp/runtime/.claude/hearting"; run_write "$tmp/runtime/.claude/hearting" "$source/target";; source-checkout) run_write "$source" "$source/target";; esac || fail "$case_name" "layout invocation failed"; assert_log "$expected_source/hooks/git-state-guard.sh $expected_source/hooks/core-first-guard.sh $expected_source/hooks/artifact-guard.sh $expected_source/hooks/builtin-memory-guard.sh $expected_source/hooks/material-route-guard.py"; pass "$case_name"; }
physical_copy(){ source=$tmp/source-physical; mkdir -p "$tmp/home" "$tmp/codex" "$tmp/config" "$tmp/agent-bin"; FIXTURE_LOG=$tmp/physical.log; export FIXTURE_LOG; make_source "$source"; : > "$source/target"; cp "$source/adapters/$adapter/bin/preflight.sh" "$tmp/agent-bin/preflight.sh"; chmod +x "$tmp/agent-bin/preflight.sh"; if [ "$adapter" = codex ]; then record="$tmp/codex/.harness/activation.json"; else record="$tmp/config/opencode/.harness/activation.json"; fi; mkdir -p "$(dirname "$record")"; printf '{"runtime":"%s","active_root":"%s"}\n' "$adapter" "$source" > "$record"; (cd "$tmp" && AGENT_HOME= HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$tmp/agent-bin/preflight.sh" write "$source/target" test-session) || fail physical-copy "activation failed"; assert_log "$source/hooks/git-state-guard.sh $source/hooks/core-first-guard.sh $source/hooks/artifact-guard.sh $source/hooks/builtin-memory-guard.sh $source/hooks/material-route-guard.py"; rm -rf "$record" "$tmp/codex/hearting" "$tmp/home/hearting" "$tmp/home/agent_setting" "$tmp/config/opencode/hearting"; err=$tmp/refusal.err; if (cd "$tmp" && AGENT_HOME= HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$tmp/agent-bin/preflight.sh" capability-info code-execute) > /dev/null 2>"$err"; then fail physical-copy "unresolved copy accepted"; else rc=$?; fi; [ "$rc" -eq 69 ] || fail physical-copy "refusal rc=$rc"; grep -Fqx reason=harness-source-root-unresolved "$err" || fail physical-copy "typed refusal missing"; printf 'PASS physical-copy.activation\nPASS physical-copy.unresolved-root\n'; }
precedence(){ case_name=$1; invocation=$tmp/source-$case_name-invocation; pinned=$tmp/source-$case_name-pinned; mkdir -p "$tmp/home" "$tmp/codex" "$tmp/config"; FIXTURE_LOG=$tmp/$case_name.log; export FIXTURE_LOG; make_source "$invocation"; make_source "$pinned"; : > "$invocation/target"; agent_home=; if [ "$case_name" = explicit-agent-home ]; then agent_home=$pinned; else if [ "$adapter" = codex ]; then record="$tmp/codex/.harness/activation.json"; else record="$tmp/config/opencode/.harness/activation.json"; fi; mkdir -p "$(dirname "$record")"; printf '{"runtime":"%s","active_root":"%s"}\n' "$adapter" "$pinned" > "$record"; fi; (cd "$invocation" && FIXTURE_LOG="$FIXTURE_LOG" AGENT_HOME="$agent_home" HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$invocation/adapters/$adapter/bin/preflight.sh" write "$invocation/target" test-session) || fail "$case_name" "precedence invocation failed"; assert_log "$pinned/hooks/git-state-guard.sh $pinned/hooks/core-first-guard.sh $pinned/hooks/artifact-guard.sh $pinned/hooks/builtin-memory-guard.sh $pinned/hooks/material-route-guard.py"; pass "$case_name"; }
converted(){ case_name=converted-guards; source=$tmp/source-converted; FIXTURE_LOG=$tmp/converted.log; export FIXTURE_LOG; make_source "$source"; : > "$source/target"; run_write "$source" "$source/target" || fail "$case_name" write; [ "$(wc -l < "$FIXTURE_LOG")" -eq 5 ] || fail "$case_name" "write count"; (cd "$source" && FIXTURE_LOG="$FIXTURE_LOG" AGENT_HOME= HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$source/adapters/$adapter/bin/preflight.sh" worktree-path --tool Write --cwd "$source") || fail "$case_name" worktree; tail -n 1 "$FIXTURE_LOG" | grep -Fqx "$source/hooks/worktree-path-guard.sh" || fail "$case_name" "worktree identity"; (cd "$source" && FIXTURE_LOG="$FIXTURE_LOG" AGENT_HOME= HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$source/adapters/$adapter/bin/preflight.sh" material-route check --tool Write --file "$source/target" --cwd "$source") || fail "$case_name" material; tail -n 1 "$FIXTURE_LOG" | grep -Fqx "$source/hooks/material-route-guard.py" || fail "$case_name" "material identity"; pass "$case_name"; }
proc_starttime(){
  pid=$1
  [ -r "/proc/$pid/stat" ] || return 1
  if stat_line=$(cat "/proc/$pid/stat" 2>/dev/null); then
    :
  else
    return 1
  fi
  printf '%s\n' "$stat_line" | awk '{print $22}'
}
mutation(){
  name=$1
  source=$tmp/$name
  FIXTURE_LOG=$tmp/$name.log
  export FIXTURE_LOG
  [ -r /proc/self/stat ] || skip_typed "$name" proc-self-stat-unreadable
  if self_stat=$(cat /proc/self/stat 2>/dev/null); then
    :
  else
    skip_typed "$name" proc-self-stat-capture-failed
  fi
  make_source "$source"
  : > "$source/target"
  dangerous=$tmp/dangerous-wrapper.sh
  printf '#!/usr/bin/env sh\nprintf dangerous-wrapper-ran > "%s"\n' "$tmp/dangerous" > "$dangerous"
  chmod +x "$dangerous"
  if [ "$name" = mutation-adapter-wrapper ]; then
    ln -sf "$dangerous" "$source/hooks/git-state-guard.sh"
  else
    printf '#!/usr/bin/env sh\n# altered forwarding wrapper\nexec "%s"\n' "$dangerous" > "$source/adapter-forwarder.sh"
    chmod +x "$source/adapter-forwarder.sh"
    ln -sf "$source/adapter-forwarder.sh" "$source/hooks/git-state-guard.sh"
  fi
  out=$tmp/$name.out
  err=$tmp/$name.err
  (cd "$source" && FIXTURE_LOG="$FIXTURE_LOG" AGENT_HOME= HOME="$tmp/home" CODEX_HOME="$tmp/codex" XDG_CONFIG_HOME="$tmp/config" "$source/adapters/$adapter/bin/preflight.sh" write "$source/target" test-session) >"$out" 2>"$err" &
  pid=$!
  observed=
  preflight_reaped=
  i=0
  while [ "$i" -lt 30 ]; do
    if observed=$(proc_starttime "$pid" 2>/dev/null); then
      break
    else
      observed=
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      preflight_reaped=1
      break
    fi
    i=$((i+1))
    sleep 0.1
  done
  if [ -n "$observed" ]; then
    case "$observed" in
      *[!0-9]*) fail "$name" "observed child starttime is not numeric";;
    esac
  elif [ -z "$preflight_reaped" ]; then
    fail "$name" "pre-wait observation neither captured nor confirmed reaped"
  fi
  if wait "$pid"; then
    rc=0
  else
    rc=$?
  fi
  [ "$rc" -eq 69 ] || fail "$name" "expected rc=69, got $rc"
  grep -Fqx reason=guard-target-self-reference "$err" || fail "$name" "self-reference refusal"
  [ ! -e "$tmp/dangerous" ] || fail "$name" "wrapper ran"
  if [ -n "$observed" ]; then
    i=0
    post_wait_gone=
    while [ "$i" -lt 10 ]; do
      if survivor=$(proc_starttime "$pid" 2>/dev/null); then
        [ "$survivor" != "$observed" ] && { post_wait_gone=1; break; }
      else
        post_wait_gone=1
        break
      fi
      i=$((i+1))
      sleep 0.1
    done
    [ -n "$post_wait_gone" ] || fail "$name" "child identity survived wait"
  fi
  pass "$name"
}
case "$case_name" in runtime-home-logical|bundle-hearting|source-checkout) layout;; physical-copy) physical_copy;; explicit-agent-home|activation-before-source) precedence "$case_name";; converted-guards) converted;; mutation-adapter-wrapper|mutation-byte-modified-wrapper) mutation "$case_name";; "") for c in runtime-home-logical bundle-hearting source-checkout physical-copy explicit-agent-home activation-before-source converted-guards mutation-adapter-wrapper mutation-byte-modified-wrapper; do "$0" --case "$c"; done;; *) printf 'SKIP unknown-case=%s\n' "$case_name"; exit 0;; esac
