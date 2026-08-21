#!/usr/bin/env sh
set -eu
# The fixture implementation is shared byte-for-byte with the Codex harness;
# TEST_ADAPTER selects the OpenCode preflight and its activation/pointer paths.
TEST_ADAPTER=opencode exec "$(CDPATH= cd -P "$(dirname "$0")/../../.." && pwd -P)/adapters/codex/bin/preflight-root.test.sh" "$@"
