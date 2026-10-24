#!/usr/bin/env bash
set -uo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WATCH="$ROOT/loops/runtime-watch.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Exercise the fallback even on hosts where pandoc happens to be installed.
mkdir -p "$tmp/bin"
for tool in bash dirname date sed tr sha256sum awk wc mkdir cmp stat rm mv grep sleep cat chmod; do
  ln -s "$(command -v "$tool")" "$tmp/bin/$tool"
done
export PATH="$tmp/bin"

fetch="$tmp/fetch"
local_probe="$tmp/local-probe"
cat > "$fetch" <<'SH'
#!/usr/bin/env bash
if [ -n "${RUNTIME_WATCH_FIXTURE_BODY:-}" ]; then
  cat "$RUNTIME_WATCH_FIXTURE_BODY"
  exit
fi
printf '<html><body><main>stable official content</main><script>request-id=%s</script></body></html>\n' "$RANDOM"
SH
cat > "$local_probe" <<'SH'
#!/usr/bin/env bash
printf 'codex_cli=codex-test\nclaude_cli=claude-test\nprojection=status-ok\n'
SH
chmod +x "$fetch" "$local_probe"

run_watch() {
  AGENT_HOME="$ROOT" \
  RUNTIME_WATCH_FETCH_CMD="$fetch" \
  RUNTIME_WATCH_LOCAL_PROBE_CMD="$local_probe" \
  RUNTIME_WATCH_STATE_DIR="$tmp/state" \
  RUNTIME_WATCH_OUT_DIR="$tmp/out" \
  bash "$WATCH" --run
}

first=$(run_watch)
case "$first" in *'status=report-written'*) ;; *) echo "FAIL first run: $first" >&2; exit 1 ;; esac
report=$(printf '%s' "$first" | sed 's/.* report=//')
first_mtime=$(stat -c %Y "$report")
sleep 1
second=$(run_watch)
second_mtime=$(stat -c %Y "$report")

case "$second" in *'status=unchanged'*) ;; *) echo "FAIL stable rerun: $second" >&2; exit 1 ;; esac
[ "$first_mtime" = "$second_mtime" ] || { echo 'FAIL unchanged report was rewritten' >&2; exit 1; }
grep -q '^## Runtime Support$' "$report" || { echo 'FAIL report contract' >&2; exit 1; }

# Prefix-sharing ordinary elements carry visible text, unlike script/style.
export RUNTIME_WATCH_FIXTURE_BODY="$tmp/body.html"
for tag in script-data style-box SCRIPT-DATA STYLE-BOX; do
  write_body() {
    cat > "$RUNTIME_WATCH_FIXTURE_BODY" <<HTML
<html><body><$tag>$1</$tag><main>stable tail</main>
<ScRiPt type="text/javascript">
request-id=$2
</sCrIpT >
<sTyLe media="screen">
.noise { color: $2; }
</sTyLe >
</body></html>
HTML
  }
  write_body first noise-one
  first=$(run_watch)
  case "$first" in *'status=report-written'*) ;; *) echo "FAIL $tag initial: $first" >&2; exit 1 ;; esac
  write_body first noise-two
  second=$(run_watch)
  case "$second" in *'status=unchanged'*) ;; *) echo "FAIL $tag script/style noise: $second" >&2; exit 1 ;; esac
  write_body second noise-two
  changed=$(run_watch)
  case "$changed" in *'status=report-written'*) ;; *) echo "FAIL $tag visible change lost: $changed" >&2; exit 1 ;; esac
done
echo 'runtime-watch conformance: PASS'
