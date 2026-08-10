#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SINK="$ROOT/utilities/artifact-sink.sh"
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT HUP INT TERM
mkdir -p "$TMP/project"
printf '# Result\n' > "$TMP/project/result.md"

if env -u AGENT_ARTIFACT_SINK_COMMAND "$SINK" --check >/dev/null 2>&1; then exit 1; else [ "$?" -eq 69 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND='relative-handler' "$SINK" --check >/dev/null 2>&1; then exit 1; else [ "$?" -eq 69 ]; fi

cat > "$TMP/handler" <<'SH'
#!/bin/sh
if [ "$1" = --check ]; then printf '{"status":"connected"}\n'; exit 0; fi
[ "$1" = --receipt ] || exit 64
[ "$(stat -c %a "$2")" = 600 ] || exit 71
python3 - "$2" <<'PY'
import json, sys
v=json.load(open(sys.argv[1], encoding='utf-8'))
base={'schema_version','event','source_path','source_capability','project_root','status','completed_at'}
assert v['event']=='artifact.completed' and v['status']=='completed'
if v['schema_version']==1:
    assert set(v)==base
else:
    bundle={'schema_version','event','status','completed_at','bundle_id','version','entrypoint'}
    assert v['schema_version']==2 and set(v)==bundle
    assert v['bundle_id']=='demo/eval-1' and v['version']=='v2' and v['entrypoint']=='report/index.html'
    assert not {'bundle_path','source_path','source_capability','project_root','body'} & set(v)
    assert not v['entrypoint'].startswith('/')
PY
printf '{"status":"created"}\n'
SH
chmod 700 "$TMP/handler"
ln -s "$TMP/handler" "$TMP/handler-link"
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-link" "$SINK" --check >/dev/null 2>&1; then exit 1; else [ "$?" -eq 69 ]; fi
AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" --check | grep -q connected
AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" --completed-at 2026-07-30T07:00:00Z | grep -q created
AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 --bundle-version v2 --entrypoint report/index.html | grep -q created
AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --bundle-id demo/eval-1 --bundle-version v2 --entrypoint report/index.html | grep -q created
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 --bundle-version v2 --entrypoint /absolute/index.html >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability 'bad value' --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" --completed-at not-a-date >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
ln -s "$TMP/project/result.md" "$TMP/project/result-link.md"
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result-link.md" --capability autopilot-code --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
printf 'artifact-sink tests: ok\n'
