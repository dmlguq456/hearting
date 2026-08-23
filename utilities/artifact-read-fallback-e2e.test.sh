#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

project=$tmp/project
artifact_root=$project/.agent_reports/plans/fallback-fixture
mkdir -p "$artifact_root" "$tmp/home" "$tmp/state"
marker=w3b-fallback-marker-7c70f77c
artifact=$artifact_root/canonical-artifact.json
printf '{"marker":"%s","value":42}\n' "$marker" >"$artifact"
canonical=$(CDPATH= cd -- "$(dirname "$artifact")" && pwd -P)/$(basename "$artifact")

# Existing flat browse: enumerate the unchanged filesystem hierarchy and resolve one canonical file.
browse_paths=$(find "$artifact_root" -maxdepth 1 -type f -name 'canonical-artifact.json' -print)
test "$(printf '%s\n' "$browse_paths" | sed '/^$/d' | wc -l | tr -d ' ')" = 1
browse_path=$(printf '%s\n' "$browse_paths")
test "$browse_path" = "$canonical"
test -z "$(find "$artifact_root" -maxdepth 1 -type f -name 'missing-artifact.json' -print)"

# Existing lexical fallback: resolve the same file from unique content, with a negative control.
rg_path=$(rg -l --fixed-strings "$marker" "$artifact_root")
test "$rg_path" = "$canonical"
! rg -q --fixed-strings 'w3b-fallback-marker-absent' "$artifact_root"

# Existing unified-memory fallback: write only to an isolated store, then recall a real artifact-pointer.
memory_env="MEM_STORE=$tmp/memory MEM_PROJECTS=$tmp/projects MEM_PROFILE=$tmp/profile MEM_RECALL_EVENTS=$tmp/state/recall.jsonl MEM_WRITE_EVENTS=$tmp/state/write.jsonl HOME=$tmp/home XDG_STATE_HOME=$tmp/state AGENT_HOME=$root"
(cd "$project" && env $memory_env python3 "$root/tools/memory/mem.py" add durable artifact-pointer \
  "Use $marker to retrieve the canonical artifact." --scope project --cwd-origin "$project" \
  --source w3b-fallback-e2e --headline 'W3b fallback pointer' --artifact-ref "$canonical" \
  >"$tmp/add.json")
(cd "$project" && env $memory_env python3 "$root/tools/memory/mem.py" recall "$marker" \
  --json --full --no-touch --all >"$tmp/recall.json")
record_id=$(python3 - "$tmp/recall.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
results = value.get("results")
assert isinstance(results, list) and len(results) == 1, value
assert results[0].get("type") == "artifact-pointer", value
print(results[0]["id"])
PY
)
test -n "$record_id"
(cd "$project" && env $memory_env python3 "$root/tools/memory/mem.py" show "$record_id" \
  --all >"$tmp/show.txt")
memory_path=$(python3 - "$tmp/show.txt" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("artifact_refs: "):
        refs = json.loads(line.split(": ", 1)[1])
        assert len(refs) == 1, refs
        print(refs[0])
        break
else:
    raise AssertionError("artifact_refs missing")
PY
)
test -n "$memory_path"
test "$memory_path" = "$canonical"
(cd "$project" && env $memory_env python3 "$root/tools/memory/mem.py" recall \
  'zzzz-unrelated-token-99173' --json --full --no-touch --all >"$tmp/recall-miss.json")
python3 - "$tmp/recall-miss.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value.get("results") == [], value
PY

browse_digest=$(sha256sum "$browse_path" | awk '{print $1}')
rg_digest=$(sha256sum "$rg_path" | awk '{print $1}')
memory_digest=$(sha256sum "$memory_path" | awk '{print $1}')
test "$browse_digest" = "$rg_digest"
test "$rg_digest" = "$memory_digest"

echo 'artifact read fallback E2E: ok'
