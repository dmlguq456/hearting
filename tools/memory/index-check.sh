#!/usr/bin/env bash
# Check legacy MEMORY.md text-index drift; --fix appends missing pointers.
# The store FTS5 index belongs to `mem index`. This script only checks the
# separate legacy `projects/<cwd>/memory/MEMORY.md` text index.
#
# Detect files missing from the index and index entries whose targets are gone.
# Report-only by default. --fix is append-only and derives new pointers from
# frontmatter name/description without modifying existing curated lines.
#
# Usage:
#   index-check.sh                 # inspect the current cwd memory index
#   index-check.sh <memory_dir>    # inspect a specific memory directory
#   index-check.sh [dir] --fix     # append missing pointers
set -u
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../../utilities/agent-home.sh")}"
ROOT="$AGENT_HOME/projects"
DIR=""; FIX=0
for a in "$@"; do
  case "$a" in --fix) FIX=1;; -h|--help) echo "usage: index-check.sh [memory_dir] [--fix]"; exit 2;; *) DIR="$a";; esac
done
if [ -z "$DIR" ]; then
  enc=$(python3 "$SCRIPT_DIR/../../utilities/claude_project_dir.py" "$PWD"); DIR="$ROOT/$enc/memory"
fi
[ -d "$DIR" ] || { echo "memory directory not found: $DIR"; exit 0; }
IDX="$DIR/MEMORY.md"

echo "# index-check: $DIR"
[ -f "$IDX" ] || echo "(MEMORY.md not found; --fix will create a header and pointers)"

missing=0
# Forward check: a memory file is missing from the index.
for f in "$DIR"/*.md; do
  [ -e "$f" ] || continue
  b=$(basename "$f")
  [ "$b" = "MEMORY.md" ] && continue
  if [ -f "$IDX" ] && grep -qF "($b)" "$IDX" 2>/dev/null; then continue; fi
  name=$(awk -F': *' '/^name:/{print $2; exit}' "$f" 2>/dev/null)
  desc=$(awk -F': *' '/^description:/{print $2; exit}' "$f" 2>/dev/null)
  [ -z "$name" ] && name="$b"
  line="- [${name}](${b}) — ${desc:-(no description)}"
  if [ "$FIX" -eq 1 ]; then
    [ -f "$IDX" ] || printf '# MEMORY.md — index\n\n' > "$IDX"
    printf '%s\n' "$line" >> "$IDX"; echo "[append] $b"
  else
    echo "[missing] $line"
  fi
  missing=$((missing+1))
done

# Reverse check: an index entry points to a missing file. Report only.
orphan=0
if [ -f "$IDX" ]; then
  while IFS= read -r ref; do
    [ -f "$DIR/$ref" ] || { echo "[orphan] index target is missing: $ref"; orphan=$((orphan+1)); }
  done < <(grep -oE '\(([A-Za-z0-9._-]+\.md)\)' "$IDX" 2>/dev/null | tr -d '()' | sort -u)
fi

if [ "$missing" -eq 0 ] && [ "$orphan" -eq 0 ]; then
  echo "index consistent — 0 missing, 0 orphaned"
else
  [ "$missing" -gt 0 ] && { [ "$FIX" -eq 1 ] && echo "→ appended $missing entries" || echo "→ $missing entries missing (append with --fix)"; }
  [ "$orphan" -gt 0 ] && echo "→ $orphan orphaned entries (manual cleanup recommended; never auto-removed)"
fi
