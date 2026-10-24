#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
REPO="$TMP/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email fixture@example.com
git -C "$REPO" config user.name fixture

commit_file() {
  file=$1
  message=$2
  mkdir -p "$REPO/$(dirname -- "$file")"
  printf '%s\n' "$message" >> "$REPO/$file"
  git -C "$REPO" add "$file"
  git -C "$REPO" commit -qm "$message"
}

assert_plan() {
  expected_release=$1
  expected_bump=$2
  expected_version=$3
  expected_reason=${4:-}
  output=$(python3 "$ROOT/tools/release/plan.py" plan --repo "$REPO" --base-tag "$BASE_TAG")
  python3 - "$output" "$expected_release" "$expected_bump" "$expected_version" "$expected_reason" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["release"] is (sys.argv[2] == "true"), value
assert value["bump"] == sys.argv[3], value
assert value["version"] == sys.argv[4], value
assert not sys.argv[5] or value["reason"].startswith(sys.argv[5]), value
PY
}

commit_file core/CORE.md "chore: initial release"
git -C "$REPO" tag v1.2.3
BASE_TAG=v1.2.3

commit_file README.md "docs: explain usage"
assert_plan false none ""

commit_file AGENTS.md "docs: project maintainer instructions"
commit_file CLAUDE.md "docs: project bootstrap reference"
assert_plan false none ""

commit_file tools/example.test.sh "test: add coverage"
assert_plan false none ""

commit_file core/CORE.md "fix(core): correct projection"
assert_plan true patch v1.2.4
git -C "$REPO" tag v1.2.4
BASE_TAG=v1.2.4

commit_file capabilities/new.md "feat(capability): add new workflow"
assert_plan true minor v1.3.0
git -C "$REPO" tag v1.3.0
BASE_TAG=v1.3.0

commit_file roles/README.md "feat(roles)!: replace role contract"
assert_plan true minor v1.4.0 major-held:
git -C "$REPO" tag v1.4.0
BASE_TAG=v1.4.0

commit_file hooks/guard.sh "adjust guard behavior"
assert_plan true patch v1.4.1
git -C "$REPO" tag v1.4.1
BASE_TAG=v1.4.1

commit_file capabilities/next.md "feat: bundled feature"
mkdir -p "$REPO/hooks"
printf '%s\n' "breaking" >> "$REPO/hooks/compat.sh"
git -C "$REPO" add hooks/compat.sh
git -C "$REPO" commit -qm "fix!: change compatibility" \
  -m "feature and breaking in one bundle"
assert_plan true minor v1.5.0 major-held:
git -C "$REPO" tag v1.5.0
BASE_TAG=v1.5.0

mkdir -p "$REPO/core"
printf '%s\n' "new contract" >> "$REPO/core/WORKFLOW.md"
git -C "$REPO" add core/WORKFLOW.md
git -C "$REPO" commit -qm "refactor: replace workflow" -m "BREAKING CHANGE: callers must use the new workflow"
assert_plan true minor v1.6.0 major-held:

python3 "$ROOT/tools/release/plan.py" validate-version v2.1.0-rc.1 >/dev/null
if python3 "$ROOT/tools/release/plan.py" validate-version vbanana >/dev/null 2>&1; then
  echo "invalid release version accepted" >&2
  exit 1
fi
if python3 "$ROOT/tools/release/plan.py" validate-version v2.1.0-01 >/dev/null 2>&1; then
  echo "invalid numeric prerelease accepted" >&2
  exit 1
fi

echo "release-plan: PASS"
