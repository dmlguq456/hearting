#!/usr/bin/env bash
# Shared, read-only PostToolUse spec synchronization nudge.
set -euo pipefail
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"

usage() {
  cat <<'EOF'
usage: spec-sync-nudge.sh --file <path> [--old <s>] [--new <s>] [--cwd <dir>] [--format text|claude-json]

Without --file, reads a PostToolUse JSON payload from stdin. --format text is
the adapter-neutral text surface; the default stdin format is Claude JSON.
EOF
}

[ "${SPEC_SYNC_NUDGE:-1}" = 0 ] && { cat >/dev/null 2>&1; exit 0; }

NUDGE_PY=$(cat <<'PY'
import glob, json, os, re, subprocess, sys, time

def quiet_exit():
    raise SystemExit(0)

def safe_cap(name, default):
    try:
        return max(0, min(int(os.environ.get(name, str(default))), 1000))
    except Exception:
        return default

def deadline_ms():
    # Parent ceilings: Claude 10s; Codex hook 10s > bridge 8s; OpenCode 8s.
    # Overrides may lower, never raise, the shared 6s ceiling.
    try:
        return max(0, min(int(os.environ.get("SPEC_SYNC_DEADLINE_MS", "6000")), 6000))
    except Exception:
        return 6000

def records_from_patch(patch):
    records = []
    path = None
    state = None
    old, new = [], []
    ignored = False
    def finish():
        if path and not ignored and (old or new):
            target = move_to or path
            if old and new:
                records.append((target, "\n".join(old), "\n".join(new)))
        return
    move_to = None
    for line in patch.splitlines():
        if line.startswith("*** Update File: ") or line.startswith("*** Add File: ") or line.startswith("*** Delete File: "):
            finish()
            path = line.split(":", 1)[1].strip()
            move_to = None
            old, new = [], []
            state = "update" if line.startswith("*** Update File: ") else "other"
            ignored = state != "update"
            continue
        if line.startswith("*** Move to: ") and path:
            move_to = line.split(":", 1)[1].strip()
            continue
        if line.startswith("*** Begin Patch") or line.startswith("*** End Patch") or line.startswith("*** End of File"):
            continue
        if line.startswith("@@"):
            continue
        if not ignored and state:
            if line.startswith("-") and not line.startswith("---"):
                old.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                new.append(line[1:])
            elif line.startswith(" "):
                old.append(line[1:]); new.append(line[1:])
    finish()
    return records

def walk(value, depth=0):
    if depth > 30:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, depth + 1)
    elif isinstance(value, str) and "*** Begin Patch" in value:
        yield from records_from_patch(value)

def direct_records(tool_input):
    result = []
    path = tool_input.get("file_path") or tool_input.get("filePath") or tool_input.get("path")
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            edit_path = edit.get("file_path") or edit.get("filePath") or edit.get("path") or path
            old = edit.get("old_string", edit.get("oldString", ""))
            new = edit.get("new_string", edit.get("newString", ""))
            if isinstance(edit_path, str) and isinstance(old, str) and isinstance(new, str) and old:
                result.append((edit_path, old, new))
    old = tool_input.get("old_string", tool_input.get("oldString", ""))
    new = tool_input.get("new_string", tool_input.get("newString", ""))
    if isinstance(path, str) and isinstance(old, str) and isinstance(new, str) and old:
        result.append((path, old, new))
    return result

def main():
    deadline = time.monotonic() + deadline_ms() / 1000
    def run_bounded(command, cap):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  timeout=min(cap, remaining))
        except Exception:
            return None

    mode = os.environ.get("SSN_MODE", "stdin")
    fmt = os.environ.get("SSN_FORMAT", "claude-json")
    cwd = os.environ.get("SSN_CWD") or os.getcwd()
    if mode == "cli":
        records = [(os.environ.get("SSN_FILE", ""), os.environ.get("SSN_OLD", ""), os.environ.get("SSN_NEW", ""))]
    else:
        try:
            data = json.load(sys.stdin)
            if not isinstance(data, dict) or data.get("hook_event_name") != "PostToolUse": quiet_exit()
            cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else cwd
            tool_input = data.get("tool_input")
            if not isinstance(tool_input, dict): quiet_exit()
            records = direct_records(tool_input)
            records.extend(walk(tool_input))
        except SystemExit:
            raise
        except Exception:
            quiet_exit()
    if not records:
        quiet_exit()

    # Resolve utilities only after cheap per-file filtering has produced candidates.
    prose_ext = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".org"}
    token_re = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9_]*-\d+|v\d+(?:\.\d+)*|\d+(?:\.\d+)?\s*(?:kHz|Hz|MHz|GHz|ms|s|GB|MB)|[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?:[A-Za-z_][A-Za-z0-9_.-]*|\d+(?:\.\d+)?)|[A-Za-z_][A-Za-z0-9_]{2,})(?![A-Za-z0-9_])")
    stop = {"def","for","pass","return","self","the","and","not","import","from","class","true","false","none","null","eof","this","that","with","print","range","function"}
    old_by_file, new_by_file = {}, {}
    for path, old, new in records:
        if not path or not isinstance(old, str) or not isinstance(new, str): continue
        absolute = os.path.abspath(path if os.path.isabs(path) else os.path.join(cwd, path))
        if os.path.splitext(absolute)[1].lower() in prose_ext: continue
        new_by_file.setdefault(absolute, set()).update(token_re.findall(new))
        values = old_by_file.setdefault(absolute, [])
        for token in token_re.findall(old):
            if token in values or token.lower() in stop: continue
            structured = "=" in token or "-" in token or re.fullmatch(r"v\d+(?:\.\d+)*", token) or re.fullmatch(r"\d+(?:\.\d+)?\s*(?:kHz|Hz|MHz|GHz|ms|s|GB|MB)", token)
            if token[:1].isalpha() and len(token) < 3 and not structured: continue
            if token[:1].isalpha() and not structured and token.islower() and "_" not in token and not any(c.isdigit() for c in token): continue
            values.append(token)
    removed_by_file = {
        path: [token for token in values if token not in new_by_file.get(path, set())]
        for path, values in old_by_file.items()
    }
    max_tokens, max_hits = safe_cap("SPEC_SYNC_TOKENS", 12), safe_cap("SPEC_SYNC_HITS", 8)
    if not max_tokens or not max_hits: quiet_exit()
    candidates = []
    for path, values in removed_by_file.items():
        try:
            with open(path, encoding="utf-8") as stream: content = stream.read()
            available = [v for v in values if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])", content)]
        except Exception:
            available = values
        candidates.extend((path, token) for token in available)
    candidates = candidates[:max_tokens]
    if not candidates: quiet_exit()

    hook_dir = os.environ.get("SSN_HOOK_DIR", "")
    local_utils = os.path.normpath(os.path.join(hook_dir, "..", "utilities"))
    fallback_home = ""
    if not os.path.isfile(os.path.join(local_utils, "artifact-root.sh")) or not os.path.isfile(os.path.join(local_utils, "artifact_reader.py")):
        agent_home_script = os.path.join(local_utils, "agent-home.sh")
        if os.path.isfile(agent_home_script):
            try:
                found = run_bounded([agent_home_script], 3)
                if found and found.returncode == 0: fallback_home = found.stdout.strip()
            except Exception:
                pass
        if not fallback_home:
            fallback_home = os.environ.get("AGENT_HOME", "")
    utility_dirs = [local_utils]
    if fallback_home:
        utility_dirs.append(os.path.join(fallback_home, "utilities"))
    artifact_resolver = next((os.path.join(u, "artifact-root.sh") for u in utility_dirs if os.path.isfile(os.path.join(u, "artifact-root.sh"))), "")
    reader = next((os.path.join(u, "artifact_reader.py") for u in utility_dirs if os.path.isfile(os.path.join(u, "artifact_reader.py"))), "")

    spec_cache, root_cache = {}, {}
    def spec_for(file_path):
        absolute = os.path.abspath(file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path))
        start = os.path.dirname(absolute)
        root = root_cache.get(start)
        if root is None:
            for probe in (start, cwd):
                if not artifact_resolver: break
                try:
                    resolved = run_bounded([artifact_resolver, probe], 5)
                    if resolved and resolved.returncode == 0 and resolved.stdout.strip():
                        root = os.path.abspath(resolved.stdout.strip()); break
                except Exception: pass
            root_cache[start] = root or ""
        if not root: return None, absolute, ""
        if root in spec_cache: return spec_cache[root], absolute, root
        found = None
        if reader:
            try:
                result = run_bounded([sys.executable, reader, "spec-dir", "--artifact-root", root], 5)
                if result and result.returncode == 0:
                    value = json.loads(result.stdout)
                    if isinstance(value, dict) and isinstance(value.get("path"), str): found = value["path"]
            except Exception:
                found = None
        else:
            legacy = os.path.join(root, "spec")
            if os.path.isfile(os.path.join(legacy, "prd.md")) or os.path.isfile(os.path.join(legacy, "pipeline_state.yaml")):
                found = legacy
        spec_cache[root] = found
        return found, absolute, root

    hit_rows, seen = [], set()
    for path, token in candidates:
        spec_dir, _, root = spec_for(path)
        if not spec_dir: continue
        try:
            if os.path.commonpath([path, os.path.abspath(spec_dir)]) == os.path.abspath(spec_dir): continue
        except ValueError:
            continue
        base = root or os.path.dirname(spec_dir)
        try: files = sorted(f for f in glob.glob(os.path.join(spec_dir, "**", "*.md"), recursive=True) if "_internal" not in os.path.relpath(f, spec_dir).split(os.sep))
        except Exception: continue
        for filename in files:
            try:
                with open(filename, encoding="utf-8") as stream:
                    for number, line in enumerate(stream, 1):
                        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", line):
                            key = (os.path.relpath(filename, base), number, token, line.rstrip().strip())
                            if key not in seen:
                                seen.add(key); hit_rows.append(key)
                            break
                if len(hit_rows) >= max_hits: break
            except Exception:
                continue
        if len(hit_rows) >= max_hits: break
    if not hit_rows or deadline - time.monotonic() <= 0: quiet_exit()
    rows = ["  {}:{}  «{}»  {}".format(rel, line, token, text if len(text) <= 120 else text[:117] + "...") for rel, line, token, text in hit_rows]
    body = ("# 🔗 Spec synchronization nudge — the edit may conflict with the spec lines below\n"
            "Values or identifiers removed by the edit still appear in the spec. Review the corresponding synchronization as part of this change:\n" + "\n".join(rows))
    if fmt == "text": print(body)
    else: print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": body}}, ensure_ascii=False))

try:
    main()
except BaseException:
    pass
PY
)

if [ "$#" -gt 0 ]; then
  FILE=""; OLD=""; NEW=""; CWD="$PWD"; FORMAT="text"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file) [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --file requires a path" >&2; exit 64; }; FILE=$2; shift 2 ;;
      --old) [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --old requires a value" >&2; exit 64; }; OLD=$2; shift 2 ;;
      --new) [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --new requires a value" >&2; exit 64; }; NEW=$2; shift 2 ;;
      --cwd) [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --cwd requires a dir" >&2; exit 64; }; CWD=$2; shift 2 ;;
      --format) [ "$#" -ge 2 ] || { echo "spec-sync-nudge: --format requires a value" >&2; exit 64; }; case "$2" in text|claude-json) FORMAT=$2 ;; *) echo "spec-sync-nudge: unknown format: $2" >&2; exit 64 ;; esac; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "spec-sync-nudge: unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
  done
  if [ -n "$FILE" ]; then
    SSN_MODE=cli SSN_FILE="$FILE" SSN_OLD="$OLD" SSN_NEW="$NEW" SSN_CWD="$CWD" SSN_FORMAT="$FORMAT" SSN_HOOK_DIR="$HOOK_DIR" python3 -c "$NUDGE_PY" 2>/dev/null || true
  else
    SSN_MODE=stdin SSN_FORMAT="$FORMAT" SSN_HOOK_DIR="$HOOK_DIR" python3 -c "$NUDGE_PY" 2>/dev/null || true
  fi
  exit 0
fi
SSN_MODE=stdin SSN_FORMAT=claude-json SSN_HOOK_DIR="$HOOK_DIR" python3 -c "$NUDGE_PY" 2>/dev/null || true
exit 0
