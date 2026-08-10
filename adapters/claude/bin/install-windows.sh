#!/usr/bin/env bash
# install-windows.sh — make the Claude Code harness run stably on Windows (Git Bash).
#
# The harness assumes a POSIX runtime: hook/statusLine commands are shell
# scripts invoked as `bash "$HOME/.claude/hooks/<x>.sh"`, and the repo wires
# harness-owned files into the runtime home. Two Windows-specific facts break
# this silently:
#
#   1. Unreliable $HOME. The shell Claude Code spawns for hooks/statusLine on
#      Windows sees $HOME as either empty or the MSYS "/home/<user>" — never the
#      real "%USERPROFILE%" where ".claude" actually lives. So every
#      `$HOME/.claude/...` command path fails to resolve and the hook/statusline
#      no-ops (or errors) with no obvious cause.
#
#   2. core.symlinks=false. On a Windows git checkout, repo symlinks are written
#      out as small pointer-TEXT files (their content is the link target path),
#      not real files. Any top-level projection file that is a symlink in the
#      repo therefore contains a path string instead of content.
#
# This script is idempotent and only mutates the runtime-owned settings.json and
# (optionally) the local memory DB. It never rewrites tracked repo content, and
# it is a no-op on non-Windows.
#
# Usage (from Git Bash):
#   bash ~/.claude/adapters/claude/bin/install-windows.sh
set -euo pipefail

# --- locate the runtime/repo home (.claude = three levels up from adapters/claude/bin) ---
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CLAUDE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
ADAPTER="$CLAUDE_DIR/adapters/claude"
HOME_DIR=$(CDPATH= cd -- "$CLAUDE_DIR/.." && pwd)   # parent of .claude = the real HOME
CANON_SETTINGS="$ADAPTER/settings.json"
RUN_SETTINGS="$CLAUDE_DIR/settings.json"

log() { printf '[install-windows] %s\n' "$*"; }

[ -f "$CANON_SETTINGS" ] || { echo "install-windows: canonical settings not found at $CANON_SETTINGS" >&2; exit 66; }

PYBIN=$(command -v python3 || command -v python || true)
[ -n "$PYBIN" ] || { echo "install-windows: python3/python not found on PATH" >&2; exit 69; }

log "CLAUDE_DIR=$CLAUDE_DIR"
log "HOME_DIR=$HOME_DIR"

# --- 1. Project the harness-owned top-level entry files from adapters/claude/ ---
# On Linux the INSTALL_LAYOUT step symlinks these into the runtime home; Windows
# cannot follow git symlinks (see header), so we copy the real content from the
# single source of truth (adapters/claude/<name>) when the runtime file is
# missing or is an unresolved symlink-pointer-text file. Idempotent.
is_pointer_or_missing() {
  # A git symlink checked out with core.symlinks=false is a single-line file
  # (no trailing newline) whose content is the relative link target and resolves
  # to an existing path. Real scripts/docs are multi-line -> keep them.
  local f="$1" content
  [ -f "$f" ] || return 0                                             # missing -> project
  [ "$(wc -l <"$f" 2>/dev/null || echo 9)" -eq 0 ] || return 1        # multi-line -> real content
  content=$(tr -d '\r\n' <"$f")
  case "$content" in
    */*) [ -e "$(dirname "$f")/$content" ] && return 0 || return 1 ;; # resolvable target -> pointer
    *) return 1 ;;
  esac
}
project_file() {
  # $1 = file name that lives at both adapters/claude/<name> (real) and top-level
  local name="$1" src="$ADAPTER/$1" dst="$CLAUDE_DIR/$1"
  [ -f "$src" ] || return 0
  if is_pointer_or_missing "$dst"; then
    cp "$src" "$dst" && log "projected $name from adapters/claude/ -> $CLAUDE_DIR/$name"
  fi
}
for name in CLAUDE.md statusline.sh; do project_file "$name"; done

# --- 2. Merge harness hooks + statusLine + env into the runtime settings.json ---
# Preserve every existing runtime key (permissions, plugins, tui, ...). Inject a
# reliable HOME/CLAUDE_HOME/AGENT_HOME so `$HOME/.claude/...` hook/statusLine
# commands resolve regardless of the Windows shell's $HOME quirk.
[ -f "$RUN_SETTINGS" ] && cp -n "$RUN_SETTINGS" "$RUN_SETTINGS.pre-harness.bak" 2>/dev/null || true
"$PYBIN" - "$CANON_SETTINGS" "$RUN_SETTINGS" "$CLAUDE_DIR" "$HOME_DIR" <<'PY'
import json, os, sys
canon_p, run_p, cdir, home = sys.argv[1:5]
canon = json.load(open(canon_p, encoding='utf-8'))
run = json.load(open(run_p, encoding='utf-8')) if os.path.exists(run_p) else {}
run['hooks'] = canon['hooks']
run['statusLine'] = canon['statusLine']
env = dict(canon.get('env', {}))
env.update(run.get('env', {}))          # existing runtime env wins over canonical
env.setdefault('MEM_DISTILL_ENABLE', '1')
env['HOME'] = home                        # real %USERPROFILE% so $HOME/.claude resolves
env['CLAUDE_HOME'] = cdir
env['AGENT_HOME'] = cdir
run['env'] = env
json.dump(run, open(run_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('[install-windows] settings.json: wired %d hook events + statusLine + env(HOME/CLAUDE_HOME/AGENT_HOME)' % len(run['hooks']))
PY

# --- 3. Restore the local memory DB from the git-tracked dump if it is missing ---
# The DB (SQLite) is per-machine and gitignored; dump.jsonl is the tracked mirror.
MEM="$CLAUDE_DIR/tools/memory/mem.py"
if [ -f "$MEM" ]; then
  if "$PYBIN" "$MEM" stats 2>/dev/null | grep -qi 'DB 없음\|no db\|not found'; then
    DUMP=""
    for cand in "$HOME_DIR/hearting/memory/dump.jsonl" "$HOME_DIR/agent_setting/memory/dump.jsonl" "$CLAUDE_DIR/memory/dump.jsonl"; do
      [ -f "$cand" ] && { DUMP="$cand"; break; }
    done
    if [ -n "$DUMP" ]; then
      "$PYBIN" "$MEM" import "$DUMP" >/dev/null 2>&1 && log "memory DB restored from $DUMP"
    else
      log "memory DB missing and no dump.jsonl found — skipping (run 'mem import <dump>' manually)"
    fi
  else
    log "memory DB present — leaving as is"
  fi
fi

# --- 4. Install the `fleet` command launcher on PATH (~/.local/bin) ---
# A real wrapper (not a symlink — Windows git/ln do not create reliable symlinks)
# that execs the absolute fleet.sh, so `fleet` resolves regardless of the shell's
# $HOME. fleet.sh itself finds python and fleet.py from its own location.
LOCAL_BIN="$HOME_DIR/.local/bin"
mkdir -p "$LOCAL_BIN"
# Resolve a REAL python.exe. The `python`/`python3` on PATH are WindowsApps
# app-execution aliases (a pymanager stub) that can crash with
# "Internal error 0x00000001", and a machine may have several real interpreters
# (only some with windows-curses). Collect candidates (sys.executable + common
# install dirs), prefer one that can `import curses` (so the live TUI works),
# else fall back to any that runs — either way the stub is bypassed.
_to_unix() { cygpath -u "$1" 2>/dev/null || printf '%s' "$1" | sed -e 's#\\#/#g' -e 's#^\([A-Za-z]\):#/\l\1#'; }
REAL_PY=""; _any_py=""
{
  se=$("$PYBIN" -c 'import sys; print(sys.executable)' 2>/dev/null || true)
  [ -n "$se" ] && printf '%s\n' "$se"
  ls "$HOME_DIR"/AppData/Local/Python/*/python.exe \
     "$HOME_DIR"/AppData/Local/Programs/Python/*/python.exe \
     "$CLAUDE_DIR"/Python/*/python.exe 2>/dev/null
} | while IFS= read -r cand; do
  u=$(_to_unix "$cand"); [ -f "$u" ] || continue
  "$u" -c 'import sys' >/dev/null 2>&1 || continue
  if "$u" -c 'import curses' >/dev/null 2>&1; then printf 'CURSES\t%s\n' "$u"; else printf 'ANY\t%s\n' "$u"; fi
done > "$RUN_SETTINGS.pyscan.tmp" 2>/dev/null || true
REAL_PY=$(awk -F'\t' '$1=="CURSES"{print $2; exit}' "$RUN_SETTINGS.pyscan.tmp" 2>/dev/null)
[ -z "$REAL_PY" ] && REAL_PY=$(awk -F'\t' '$1=="ANY"{print $2; exit}' "$RUN_SETTINGS.pyscan.tmp" 2>/dev/null)
rm -f "$RUN_SETTINGS.pyscan.tmp"
{
  echo '#!/usr/bin/env bash'
  echo '# fleet launcher (Windows/Git Bash) — generated by install-windows.sh.'
  [ -n "$REAL_PY" ] && echo "export FLEET_PYTHON=\"$REAL_PY\""
  # Pin HOME + CLAUDE_CONFIG_DIR so the collectors find ~/.claude regardless of the
  # shell's unreliable Windows \$HOME (fleet reads sessions/ under the config home).
  echo "export HOME=\"$HOME_DIR\""
  echo "export CLAUDE_CONFIG_DIR=\"\${CLAUDE_CONFIG_DIR:-$CLAUDE_DIR}\""
  echo "exec bash \"$CLAUDE_DIR/tools/fleet/fleet.sh\" \"\$@\""
} > "$LOCAL_BIN/fleet"
chmod +x "$LOCAL_BIN/fleet"
log "installed fleet launcher: $LOCAL_BIN/fleet${REAL_PY:+ (FLEET_PYTHON=$REAL_PY)}"
case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *) log "note: add '$LOCAL_BIN' to PATH (in ~/.bashrc) to call 'fleet' as a command" ;;
esac

# --- 5. fleet note: --json/--once work here; only the live TUI needs curses ---
if ! "$PYBIN" -c 'import curses' >/dev/null 2>&1; then
  log "note: 'fleet --json' and 'fleet --once' work on Windows; the live TUI needs curses ('pip install windows-curses', or use WSL/Linux)."
fi

log "done. Restart Claude Code for settings.json changes to take effect."
