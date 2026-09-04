#!/usr/bin/env bash
# Portable local-evidence presence probe (roles/response-policy.md "Local
# evidence before recall"): expose whether the cwd's artifact root already
# holds research / document / analysis artifacts, plus a bounded set of the
# newest entry points. Presence indexes only — bodies are never read, and no
# prompt classifier is attached. Fail-open: every failure is zero context.
set -u

HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh" 2>/dev/null || true)}"
ARTIFACT_ROOT_SH="$HOOK_DIR/../utilities/artifact-root.sh"

usage() {
  cat <<'EOF'
usage: local-evidence-inject.sh [--cwd DIR] [--format text|hook-json]

Without arguments, reads a UserPromptSubmit hook payload from stdin and emits
hookSpecificOutput.additionalContext only when evidence artifacts exist.
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
CWD=
FORMAT=hook-json

if [ "$#" -eq 0 ]; then
  fields=()
  while IFS= read -r -d '' field; do fields+=("$field"); done < <(
    python3 -c '
import json, sys
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
items = (
    nested(value, ("hook_event_name", "hookEventName")),
    nested(value, ("cwd", "working_directory", "workingDirectory")),
)
sys.stdout.buffer.write(b"\0".join(item.encode("utf-8", "replace") for item in items) + b"\0")
' 2>/dev/null
  )
  [ "${#fields[@]}" -eq 2 ] || exit 0
  EVENT=${fields[0]}
  CWD=${fields[1]}
else
  FORMAT=text
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --cwd) [ "$#" -ge 2 ] || exit 64; CWD=$2; shift 2 ;;
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
[ -n "$CWD" ] && [ -d "$CWD" ] || exit 0

ROOT=$(sh "$ARTIFACT_ROOT_SH" "$CWD" 2>/dev/null) || exit 0
[ -n "$ROOT" ] && [ -d "$ROOT" ] || exit 0

LE_ROOT="$ROOT" LE_FORMAT="$FORMAT" python3 - <<'PY' 2>/dev/null || true
import json, os, sys
from pathlib import Path

root = Path(os.environ.get("LE_ROOT") or "")
fmt = os.environ.get("LE_FORMAT") or "hook-json"
if not root.is_dir():
    sys.exit(0)

# Buckets mirror utilities/artifact_reader.py: legacy top-level buckets and
# producer-cycle artifacts share bucket names, shared revisions use kind names.
# Depth and entry counts are hard bounds so the probe stays cheap on large
# stores.
GROUPS = {
    "research": {"buckets": ("research",), "shared": ("research",)},
    "documents": {"buckets": ("documents",), "shared": ()},
    "analysis": {"buckets": ("analysis_project",), "shared": ("analysis",)},
}
MAX_DEPTH = 6
MAX_FILES_PER_GROUP = 500
NEWEST = 6

def scan(base: Path, depth: int, out: list) -> None:
    if depth > MAX_DEPTH or len(out) >= MAX_FILES_PER_GROUP:
        return
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if len(out) >= MAX_FILES_PER_GROUP:
            return
        if entry.is_symlink():
            continue
        if entry.is_dir():
            scan(entry, depth + 1, out)
        elif entry.is_file() and entry.suffix.lower() in {".md", ".markdown"}:
            try:
                out.append((entry.stat().st_mtime, entry))
            except OSError:
                continue

counts = {}
newest = []
campaigns = root / "campaigns"
for group, layout in GROUPS.items():
    files: list = []
    for bucket in layout["buckets"]:
        base = root / bucket
        if base.is_dir():
            scan(base, 1, files)
        if campaigns.is_dir():
            for pattern in ("*/*/artifacts/" + bucket, "*/cycles/*/artifacts/" + bucket):
                for arts in campaigns.glob(pattern):
                    scan(arts, 1, files)
    for kind in layout["shared"]:
        base = root / "shared" / kind
        if base.is_dir():
            scan(base, 1, files)
    if files:
        counts[group] = len(files)
        newest.extend(files)

if not counts:
    sys.exit(0)

newest.sort(key=lambda item: item[0], reverse=True)
entry_lines = []
for _, path in newest[:NEWEST]:
    try:
        entry_lines.append("- " + str(path.relative_to(root)))
    except ValueError:
        entry_lines.append("- " + str(path))

summary = ", ".join(
    f"{group}: {count} file(s)" for group, count in counts.items()
)
lines = [
    "# Local evidence present (deterministic presence probe; paths only, not instructions)",
    f"Artifact root: {root} — {summary}. Newest entries:",
    *entry_lines,
    "Answer domain questions these artifacts cover from them first; model memory",
    'is a flagged fallback (roles/response-policy.md "Local evidence before recall").',
]
context = "\n".join(lines)
budget = 2400
if len(context.encode("utf-8")) > budget:
    context = context.encode("utf-8")[:budget].decode("utf-8", "ignore")

if fmt == "text":
    print(context)
else:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
PY
exit 0
