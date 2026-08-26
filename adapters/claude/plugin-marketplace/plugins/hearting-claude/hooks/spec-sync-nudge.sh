#!/usr/bin/env bash
# spec-sync-nudge — post-edit synchronization nudge for Edit|Write|MultiEdit.
# In a spec-backed project, direct source edits can stale spec prose. Search
# spec/*.md for values or identifiers removed by the edit and inject matching
# lines as additionalContext. This deterministically supports the contract that
# corresponding synchronization is part of the change.
#
#   Guards (all clean no-op status 0; never blocking):
#     - MEM_DISTILL=1 → prevent recursion in distiller sessions
#     - SPEC_SYNC_NUDGE=0 → per-shell opt-out
#     - hook_event_name ≠ PostToolUse → no-op
#     - not spec-backed → no-op
#     - edited file is inside the spec directory → no-op
#     - no removed token or no spec hit → no-op
#
#   Read-only invariant: no DB or file writes; emit additionalContext only.
#
#   Caps:
#     SPEC_SYNC_HITS   (default 8)  — maximum injected hit lines
#     SPEC_SYNC_TOKENS (default 12) — maximum removed tokens examined
#
#   Portable CLI:
#     spec-sync-nudge.sh --file <path> [--old <s>] [--new <s>] [--cwd <dir>] [--format text|claude-json]
#   With no arguments, read PostToolUse JSON from stdin and emit Claude JSON.
#   Adapter hook configuration owns registration.
set -euo pipefail
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh")}"
export AGENT_HOME

usage() {
  cat <<'EOF'
usage: spec-sync-nudge.sh --file <path> [--old <s>] [--new <s>] [--cwd <dir>] [--format text|claude-json]

Without arguments, reads Claude PostToolUse hook JSON from stdin and emits Claude hook JSON.
EOF
}

# Recursion guard: in a distiller session, drain stdin and exit 0.
[ "${MEM_DISTILL:-}" = "1" ] && { cat >/dev/null 2>&1; exit 0; }
# per-shell opt-out.
[ "${SPEC_SYNC_NUDGE:-1}" = "0" ] && { cat >/dev/null 2>&1; exit 0; }

# Core logic uses portable Python for safe regex, glob, and JSON escaping.
NUDGE_PY='
import os, sys, json, re, glob, subprocess

MODE = os.environ.get("SSN_MODE", "stdin")
FMT = os.environ.get("SSN_FORMAT", "claude-json")

if MODE == "cli":
    file_path = os.environ.get("SSN_FILE", "")
    old = os.environ.get("SSN_OLD", "")
    new = os.environ.get("SSN_NEW", "")
    cwd = os.environ.get("SSN_CWD") or os.getcwd()
else:
    try:
        d = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if (d.get("hook_event_name") or "") != "PostToolUse":
        sys.exit(0)
    cwd = d.get("cwd") or os.getcwd()
    ti = d.get("tool_input") or {}
    file_path = ti.get("file_path") or ti.get("filePath") or ti.get("path") or ""
    op, np = [], []
    if "old_string" in ti or "new_string" in ti:
        op.append(ti.get("old_string") or ""); np.append(ti.get("new_string") or "")
    for e in (ti.get("edits") or []):
        if isinstance(e, dict):
            op.append(e.get("old_string") or ""); np.append(e.get("new_string") or "")
    old = "\n".join(op); new = "\n".join(np)

if not file_path:
    sys.exit(0)
if not os.path.isabs(file_path):
    file_path = os.path.join(cwd, file_path)

def reader_spec_dir(root):
    # W7D: after the artifact write cutover the spec tree lives under
    # campaigns/*/cycles/*/artifacts/spec or shared/spec/*/revisions/*.
    # Shelled out rather than imported — the plugin projection rebases
    # AGENT_HOME onto its data dir, so a missing resolver must degrade to the
    # legacy walk above, never fail the hook.
    script = os.path.join(os.environ.get("AGENT_HOME", ""), "utilities", "artifact_reader.py")
    if not os.path.isfile(script):
        return None
    try:
        r = subprocess.run([sys.executable, script, "spec-dir", "--artifact-root", root],
                           capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout).get("path") if r.returncode == 0 else None
    except Exception:
        return None

def find_spec_dir(start):
    d = os.path.dirname(os.path.abspath(start)); prev = None
    roots = []
    while d and d != prev:
        for art in (".agent_reports", ".claude_reports"):
            spec = os.path.join(d, art, "spec")
            if os.path.isfile(os.path.join(spec, "pipeline_state.yaml")):
                return spec                                   # legacy spec/ wins (read-only fallback)
            if os.path.isdir(os.path.join(d, art)):
                roots.append(os.path.join(d, art))
        prev, d = d, os.path.dirname(d)
    for root in roots:   # no legacy spec/ → resolve the cycle/shared spec tree
        found = reader_spec_dir(root)
        if found:
            return found
    return None

spec_dir = find_spec_dir(file_path)
if not spec_dir:
    sys.exit(0)

# Spec-directory edits are not candidates.
try:
    if os.path.commonpath([os.path.abspath(file_path), os.path.abspath(spec_dir)]) == os.path.abspath(spec_dir):
        sys.exit(0)
except ValueError:
    pass

# Prose edits are not candidates; code/config value changes are the signal.
# Common prose words create unrelated spec matches.
PROSE_EXT = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".org"}
if os.path.splitext(file_path)[1].lower() in PROSE_EXT:
    sys.exit(0)

# Removed tokens: values or identifiers present in old but absent from new.
TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9_]*-\d+|v\d+(?:\.\d+)*|"
    r"\d+(?:\.\d+)?\s*(?:kHz|Hz|MHz|GHz|ms|s|GB|MB)|"
    r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?:[A-Za-z_][A-Za-z0-9_.-]*|\d+(?:\.\d+)?)|"
    r"[A-Za-z_][A-Za-z0-9_]{2,})(?![A-Za-z0-9_])"
)
STOP = {"def","for","pass","return","self","the","and","not","import","from","class",
        "true","false","none","null","eof","this","that","with","print","range","function"}
new_set = set(TOKEN_RE.findall(new or ""))
removed, seen = [], set()
maxtok = int(os.environ.get("SPEC_SYNC_TOKENS", "12"))
for t in TOKEN_RE.findall(old or ""):
    if t in new_set or t in seen or t.lower() in STOP:
        continue
    structured = ("=" in t or "-" in t or
                  re.fullmatch(r"v\d+(?:\.\d+)*", t) or
                  re.fullmatch(r"\d+(?:\.\d+)?\s*(?:kHz|Hz|MHz|GHz|ms|s|GB|MB)", t))
    if t[:1].isalpha():
        if len(t) < 3 and not structured:
            continue
        if not structured and t.islower() and "_" not in t and not any(c.isdigit() for c in t):
            continue
    seen.add(t); removed.append(t)
    if len(removed) >= maxtok:
        break
if not removed:
    sys.exit(0)

# Spec markdown (prd.md, design/*.md, etc.); exclude _internal snapshots.
md = [f for f in glob.glob(os.path.join(spec_dir, "**", "*.md"), recursive=True)
      if "_internal" not in os.path.relpath(f, spec_dir).split(os.sep)]
md.sort()

def bounded(line, tok):
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])", line) is not None

hits, cap = [], int(os.environ.get("SPEC_SYNC_HITS", "8"))
# Hits are shown relative to the artifact root so the layout stays legible
# whether the spec tree is a cycle dir, a shared revision, or legacy spec/ (W7D).
base, probe = os.path.dirname(spec_dir), spec_dir
while probe and probe != os.path.dirname(probe):
    if os.path.basename(probe) in (".agent_reports", ".claude_reports"):
        base = probe; break
    probe = os.path.dirname(probe)
for mf in md:
    try:
        with open(mf, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                for t in removed:
                    if bounded(line, t):
                        hits.append((os.path.relpath(mf, base), i, t, line.rstrip().strip()))
                        break
                if len(hits) >= cap:
                    break
    except Exception:
        continue
    if len(hits) >= cap:
        break
if not hits:
    sys.exit(0)

rows = []
for rel, ln, tok, text in hits:
    snip = text if len(text) <= 120 else text[:117] + "..."
    rows.append("  {}:{}  «{}»  {}".format(rel, ln, tok, snip))
body = ("# \U0001f517 Spec synchronization nudge — the edit may conflict with the spec lines below\n"
        "Values or identifiers removed by the edit still appear in the spec. Review the corresponding synchronization as part of this change:\n"
        + "\n".join(rows))

if FMT == "text":
    print(body)
else:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": body}}, ensure_ascii=False))
'

if [ "$#" -gt 0 ]; then
  FILE=""; OLD=""; NEW=""; CWD="$PWD"; FORMAT="text"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)   [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --file requires a path" >&2; exit 64; }; FILE=$2; shift 2 ;;
      --old)    [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --old requires a value" >&2; exit 64; }; OLD=$2; shift 2 ;;
      --new)    [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --new requires a value" >&2; exit 64; }; NEW=$2; shift 2 ;;
      --cwd)    [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --cwd requires a dir" >&2; exit 64; }; CWD=$2; shift 2 ;;
      --format) [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --format requires a value" >&2; exit 64; }
                case "$2" in text|claude-json) FORMAT=$2 ;; *) echo "spec-sync-nudge: unknown format: $2" >&2; exit 64 ;; esac; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "spec-sync-nudge: unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
  done
  [ -n "$FILE" ] || { echo "spec-sync-nudge: --file is required" >&2; exit 64; }
  SSN_MODE=cli SSN_FILE="$FILE" SSN_OLD="$OLD" SSN_NEW="$NEW" SSN_CWD="$CWD" SSN_FORMAT="$FORMAT" \
    python3 -c "$NUDGE_PY" || true
  exit 0
fi

input=$(cat 2>/dev/null || true)
[ -z "$input" ] && exit 0
printf '%s' "$input" | SSN_MODE=stdin SSN_FORMAT=claude-json python3 -c "$NUDGE_PY" || true
exit 0
