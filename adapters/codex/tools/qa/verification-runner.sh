#!/usr/bin/env sh
# Codex adapter-owned verification command runner.
set -u

usage() {
  cat <<'EOF'
usage: verification-runner.sh [--check] [--timeout seconds] -- <command> [args...]

Runs an explicit read-only verification command through a Codex-owned
tool-contract surface. Use --check to verify the command is available without
running it.
EOF
}

adapter=codex
timeout_s=60
check_only=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --check)
      check_only=1
      shift
      ;;
    --timeout)
      [ "$#" -ge 2 ] || { echo "codex verification-runner: --timeout requires seconds" >&2; exit 64; }
      timeout_s=$2
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

printf 'adapter=%s\n' "$adapter"
printf 'runtime_surface=adapter-owned-verification-runner\n'
printf 'tool_contract=verification-runner\n'

if [ "$#" -eq 0 ]; then
  if [ "$check_only" -eq 1 ]; then
    printf 'check=runner-available\n'
    printf 'status=ok\n'
  else
    printf 'status=tool-contract\n'
    printf 'tool_contract_check=adapters/codex/bin/preflight.sh verification-runner --check -- <command>\n'
    printf 'fallback=satisfy-tool-contract-or-report-unavailable\n'
  fi
  exit 0
fi

cmd=$1
if ! command -v "$cmd" >/dev/null 2>&1; then
  printf 'status=unavailable\n'
  printf 'reason=command-not-found\n'
  printf 'command=%s\n' "$cmd"
  exit 69
fi

printf 'command=%s\n' "$cmd"
printf 'timeout=%s\n' "$timeout_s"

if [ "$check_only" -eq 1 ]; then
  printf 'check=command-available\n'
  printf 'status=ok\n'
  exit 0
fi

runner_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd -P)
agent_root=$(CDPATH= cd -P -- "$runner_dir/../../../.." && pwd -P)
lease_runner="$agent_root/utilities/verification-background-lease.py"
if [ ! -r "$lease_runner" ]; then
  printf 'status=unavailable\n'
  printf 'reason=verification-lease-runner-not-found\n'
  exit 69
fi
python3 "$lease_runner" \
  --timeout "$timeout_s" -- "$@"
rc=$?

if [ "$rc" -eq 0 ]; then
  printf 'status=ok\n'
elif [ "$rc" -eq 124 ]; then
  printf 'status=failed\n'
  printf 'reason=timeout\n'
else
  printf 'status=failed\n'
fi
printf 'exit_code=%s\n' "$rc"
exit "$rc"
