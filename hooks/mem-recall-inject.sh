#!/usr/bin/env bash
# Portable D-55 prompt bridge: expose capsule indexes, never record bodies.
set -u

HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh" 2>/dev/null || true)}"
MEM_PY="${MEM_PY:-$AGENT_HOME/tools/memory/mem.py}"

usage() {
  cat <<'EOF'
usage: mem-recall-inject.sh [--prompt TEXT --cwd DIR]
                            [--session-id ID] [--turn-id ID]
                            [--runtime NAME] [--format text|hook-json]

Without arguments, reads a UserPromptSubmit hook payload from stdin and emits
hookSpecificOutput.additionalContext only when capsule candidates exist.
EOF
}

is_worker() {
  [ "${AGENT_SESSION_ROLE:-}" = worker ] \
    || [ "${AGENT_DISPATCH_CHILD:-}" = 1 ] \
    || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
    || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
    || [ "${FLEET_TITLE_REFRESH:-}" = 1 ] \
    || [ "${MEM_DISTILL:-}" = 1 ]
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

if is_worker; then
  [ "$#" -gt 0 ] || cat >/dev/null 2>&1 || true
  exit 0
fi

EVENT=UserPromptSubmit
PROMPT=
CWD=
SID=
TURN=
RUNTIME=claude
FORMAT=hook-json

if [ "$#" -eq 0 ]; then
  fields=()
  while IFS= read -r -d '' field; do fields+=("$field"); done < <(
    python3 -c '
import hashlib, json, os, stat, sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
if not isinstance(value, dict):
    value = {}
def nested(obj, names):
    for name in names:
        item = obj.get(name)
        if isinstance(item, str) and item:
            return item
    for key in ("context", "workspace", "session", "payload", "event", "input", "data"):
        item = obj.get(key)
        if isinstance(item, dict):
            found = nested(item, names)
            if found:
                return found
    return ""
def transcript_turn(path):
    if not path:
        return ""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return ""
        with open(path, "rb") as handle:
            start = max(0, info.st_size - 1024 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if not isinstance(row, dict) or row.get("type") != "user" or row.get("isSidechain") is True:
            continue
        # A tool result is also a type:user row but not a prompt. The
        # material-route guard skips it (_is_tool_result_user_row), so the
        # probe must too; otherwise a prompt that writes no user row (a
        # background task notification) binds its receipt to the last tool
        # result, a turn the guard never computes.
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else [content]
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks):
            continue
        uid = row.get("uuid")
        if isinstance(uid, str) and uid:
            return "transcript-user:" + uid
        message = row.get("message")
        stamp = row.get("timestamp")
        material = json.dumps([stamp, message], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "transcript-user-hash:" + hashlib.sha256(material.encode()).hexdigest()
    return ""
turn = nested(value, ("turn_id", "turnID", "message_id", "messageID"))
if not turn:
    turn = transcript_turn(nested(value, ("transcript_path", "transcriptPath")))
items = (
    nested(value, ("hook_event_name", "hookEventName")),
    nested(value, ("prompt",)),
    nested(value, ("cwd", "working_directory", "workingDirectory")),
    nested(value, ("session_id", "sessionID", "thread_id", "threadID")),
    turn,
)
sys.stdout.buffer.write(b"\0".join(item.encode("utf-8", "replace") for item in items) + b"\0")
' 2>/dev/null
  )
  [ "${#fields[@]}" -eq 5 ] || exit 0
  EVENT=${fields[0]}
  PROMPT=${fields[1]}
  CWD=${fields[2]}
  SID=${fields[3]}
  TURN=${fields[4]}
else
  FORMAT=text
  RUNTIME=unknown
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --prompt) [ "$#" -ge 2 ] || exit 64; PROMPT=$2; shift 2 ;;
      --cwd) [ "$#" -ge 2 ] || exit 64; CWD=$2; shift 2 ;;
      --session-id) [ "$#" -ge 2 ] || exit 64; SID=$2; shift 2 ;;
      --turn-id) [ "$#" -ge 2 ] || exit 64; TURN=$2; shift 2 ;;
      --runtime) [ "$#" -ge 2 ] || exit 64; RUNTIME=$2; shift 2 ;;
      --format)
        [ "$#" -ge 2 ] || exit 64
        case "$2" in text|hook-json) FORMAT=$2 ;; *) exit 64 ;; esac
        shift 2
        ;;
      *) usage >&2; exit 64 ;;
    esac
  done
fi

[ "$EVENT" = UserPromptSubmit ] || exit 0
[ -n "$PROMPT" ] && [ -n "$CWD" ] && [ -f "$MEM_PY" ] || exit 0

args=(candidates "$PROMPT" --runtime "$RUNTIME" --session-id "${SID:-memory-prompt-hook}")
[ -z "$TURN" ] || args+=(--turn-id "$TURN")
[ "$FORMAT" = hook-json ] && args+=(--hook)

(cd "$CWD" 2>/dev/null && python3 "$MEM_PY" "${args[@]}") 2>/dev/null || true
exit 0
