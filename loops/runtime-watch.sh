#!/usr/bin/env bash
# Runtime watch loop — deterministic currentness probe + report/proposal writer.
# It never edits policy files. It only fingerprints official sources and local runtime/projection state.
set -u

MODE="probe"
case "${1:---probe}" in
  --probe) MODE="probe" ;;
  --run) MODE="run" ;;
  -h|--help)
    sed -n '1,80p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *) echo "runtime-watch: unknown arg: $1" >&2; exit 64 ;;
esac

if [ -z "${AGENT_HOME:-}" ]; then
  if [ -n "${CLAUDE_HOME:-}" ]; then
    AGENT_HOME="$CLAUDE_HOME"
  elif [ -d "$HOME/hearting" ]; then
    AGENT_HOME="$HOME/hearting"
  elif [ -d "$HOME/agent_setting" ]; then
    AGENT_HOME="$HOME/agent_setting"
  else
    AGENT_HOME="$HOME/.claude"
  fi
fi
STATE_DIR="${RUNTIME_WATCH_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-runtime-watch}"
AGENT_LOOP_ENV="${AGENT_LOOP_ENV:-$HOME/.config/hearting/loops.env}"
[ -f "$AGENT_LOOP_ENV" ] && . "$AGENT_LOOP_ENV"
AGENT_NOTES_ROOT="${AGENT_NOTES_ROOT:-$HOME/agent-notes}"
OUT_DIR="${RUNTIME_WATCH_OUT_DIR:-$AGENT_NOTES_ROOT/runtime-watch}"
MAX_AGE_HOURS="${RUNTIME_WATCH_MAX_AGE_HOURS:-72}"
FETCH_CMD="${RUNTIME_WATCH_FETCH_CMD:-}"
LOCAL_PROBE_CMD="${RUNTIME_WATCH_LOCAL_PROBE_CMD:-}"

SOURCES=(
  "openai_codex_pricing https://developers.openai.com/codex/pricing"
  "openai_codex_changelog https://developers.openai.com/codex/changelog"
  "openai_codex_rate_card https://help.openai.com/en/articles/20001106-codex-rate-card-2"
  "openai_codex_plan https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan"
  "claude_code_plan https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan"
  "claude_code_changelog https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
)

hash_text() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'; else shasum -a 256 | awk '{print $1}'; fi
}

fetch_hash() {
  label=$1 url=$2
  if [ -n "$FETCH_CMD" ]; then
    body=$("$FETCH_CMD" "$url" 2>/dev/null) || {
      printf '%s status=unavailable reason=fetch-failed url=%s\n' "$label" "$url"
      return 0
    }
  elif ! command -v curl >/dev/null 2>&1; then
    printf '%s status=unavailable reason=curl-missing url=%s\n' "$label" "$url"
    return 0
  else
    body=$(curl -A 'Mozilla/5.0 (compatible; agent-runtime-watch/1.0)' \
      -fsSL --max-time 20 "$url" 2>/dev/null) || {
        printf '%s status=unavailable reason=fetch-failed url=%s\n' "$label" "$url"
        return 0
      }
  fi
  # Help centers frequently embed request IDs and hydration payloads. Fingerprint normalized
  # visible text so transport noise does not become a policy-currentness incident.
  if command -v pandoc >/dev/null 2>&1; then
    normalized=$(printf '%s' "$body" | pandoc -f html -t plain 2>/dev/null | sed '/^[[:space:]]*$/d')
  else
    normalized=$(printf '%s' "$body" | sed 's/<[^>]*>/ /g' | tr -s '[:space:]' ' ')
  fi
  fp=$(printf '%s' "$normalized" | hash_text)
  bytes=$(printf '%s' "$normalized" | wc -c | tr -d ' ')
  printf '%s status=ok sha256=%s bytes=%s url=%s\n' "$label" "$fp" "$bytes" "$url"
}

local_probe() {
  if [ -n "$LOCAL_PROBE_CMD" ]; then
    "$LOCAL_PROBE_CMD"
    return
  fi
  printf 'agent_home=%s\n' "$AGENT_HOME"
  if command -v codex >/dev/null 2>&1; then
    printf 'codex_cli=%s\n' "$(codex --version 2>/dev/null | head -1 || true)"
  else
    printf 'codex_cli=unavailable\n'
  fi
  if command -v claude >/dev/null 2>&1; then
    printf 'claude_cli=%s\n' "$(claude --version 2>/dev/null | head -1 || true)"
  else
    printf 'claude_cli=unavailable\n'
  fi
  if [ -x "$AGENT_HOME/adapters/codex/bin/preflight.sh" ]; then
    "$AGENT_HOME/adapters/codex/bin/preflight.sh" runtime-projection 2>/dev/null | sed 's/^/codex_projection_/' || true
  else
    printf 'codex_projection_status=unavailable\n'
  fi
  if [ -x "$AGENT_HOME/utilities/usage-check.sh" ]; then
    AGENT_HOME="$AGENT_HOME" "$AGENT_HOME/utilities/usage-check.sh" --harness all 2>/dev/null | sed 's/^/usage_check_/' || true
  else
    printf 'usage_check_status=unavailable\n'
  fi
}

probe_all() {
  printf 'runtime_watch_probe_at=%s\n' "$(date -Iseconds)"
  printf 'section=official_sources\n'
  for row in "${SOURCES[@]}"; do
    set -- $row
    fetch_hash "$1" "$2"
  done
  printf 'section=local_projection\n'
  local_probe
}

if [ "$MODE" = "probe" ]; then
  probe_all
  exit 0
fi

mkdir -p "$STATE_DIR" "$OUT_DIR"
today=$(date +%F)
probe_file="$STATE_DIR/latest.probe"
new_probe="$STATE_DIR/latest.probe.new"
report="$OUT_DIR/$today.md"
probe_all > "$new_probe"

changed=0
if [ ! -f "$probe_file" ] || \
   ! cmp -s <(sed '/^runtime_watch_probe_at=/d' "$probe_file") \
            <(sed '/^runtime_watch_probe_at=/d' "$new_probe"); then
  changed=1
fi
if [ -f "$report" ]; then
  age_h=$(( ($(date +%s) - $(stat -c %Y "$report" 2>/dev/null || echo 0)) / 3600 ))
else
  age_h=$((MAX_AGE_HOURS + 1))
fi

if [ "$changed" -eq 0 ] && [ "$age_h" -lt "$MAX_AGE_HOURS" ]; then
  rm -f "$new_probe"
  printf 'runtime-watch status=unchanged report=%s\n' "$report"
  exit 0
fi

mv "$new_probe" "$probe_file"
{
  printf '# Runtime watch — %s\n\n' "$today"
  printf 'Trigger: %s. This loop writes a report/proposal only; it does not edit policy.\n\n' \
    "$([ "$changed" -eq 1 ] && echo source-or-projection-change || echo conservative-age)"
  printf '## Runtime Support\n\n'
  printf 'Official-source fingerprints are below. Interpret normative changes by opening the official pages; do not infer policy from this hash alone.\n\n'
  sed -n '/^section=official_sources$/,/^section=local_projection$/p' "$probe_file" | sed '/^section=/d; s/^/- /'
  printf '\n## Local Projection\n\n'
  sed -n '/^section=local_projection$/,$p' "$probe_file" | sed '/^section=/d; s/^/- /'
  printf '\n## Parity Gap\n\n'
  printf -- '- Compare official runtime support, local adapter projection, and fleet/dispatch assumptions before changing policy.\n'
  printf -- '- Codex windows may be duration-labeled; Claude Code may still expose 5h/7d usage buckets.\n'
  printf '\n## Fallback\n\n'
  printf -- '- If official fetch fails, keep existing policy and mark currentness unknown.\n'
  printf -- '- If local projection fails, report unsupported runtime contract rather than assuming parity.\n'
  printf '\n## Proposal\n\n'
  printf -- '- Review any changed official fingerprints. If semantics changed, open an autopilot-spec/code cycle with primary-source citations.\n'
} > "$report"

printf 'runtime-watch status=report-written report=%s\n' "$report"
