#!/usr/bin/env sh
# Regression: the Codex projection installer must not overwrite user-owned
# skill/agent entries, and must still refresh absent, owned and dangling ones.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' 0 HUP INT TERM
HOME_DIR=$T/home; CH=$T/codex
mkdir -p "$HOME_DIR/.config" "$CH/skills" "$CH/agents" "$T/user"
SRC_AGENTS=$ROOT/codex_setting/codex-agents; SRC_SKILLS=$ROOT/codex_setting/codex-skills
set -- $(cd "$SRC_AGENTS" && ls *.toml | sed -n '1,5p'); A_FOREIGN=$1 A_REAL=$2 A_DANGLING=$3 A_OWNED=$4 A_ABSENT=$5
set -- $(cd "$SRC_SKILLS" && ls | sed -n '1,4p'); S_FOREIGN=$1 S_DIR=$2 S_DANGLING=$3 S_OWNED=$4

printf 'USER AGENT\n' > "$T/user/agent.toml"; mkdir "$T/user/skill"; printf 'USER SKILL\n' > "$T/user/skill/SKILL.md"
ln -s "$T/user/agent.toml" "$CH/agents/$A_FOREIGN"
printf 'REAL AGENT\n' > "$CH/agents/$A_REAL"
ln -s "$T/missing/agent.toml" "$CH/agents/$A_DANGLING"
ln -s "$SRC_AGENTS/$A_OWNED" "$CH/agents/$A_OWNED"
ln -s "$T/user/skill" "$CH/skills/$S_FOREIGN"
mkdir "$CH/skills/$S_DIR"; printf 'MY SKILL\n' > "$CH/skills/$S_DIR/SKILL.md"
ln -s "$T/missing/skill" "$CH/skills/$S_DANGLING"
ln -s "$SRC_SKILLS/$S_OWNED" "$CH/skills/$S_OWNED"

run() { # run <outfile> [args]
  out=$1; shift
  env -i PATH="$PATH" HOME="$HOME_DIR" XDG_CONFIG_HOME="$HOME_DIR/.config" CODEX_HOME="$CH" \
    TMPDIR="$T" AGENT_HOME="$ROOT" sh "$ROOT/adapters/codex/bin/install-runtime-projection.sh" "$@" >"$out" 2>"$out.err"
}
fail() { echo "FAIL: $*" >&2; exit 1; }

for mode in native both; do
  run "$T/$mode.out" --skills-mode "$mode" || { cat "$T/$mode.out.err" >&2; fail "$mode install exited nonzero"; }
  grep -q '^status=ok$' "$T/$mode.out" || fail "$mode: status=ok missing"
  [ "$(readlink "$CH/agents/$A_FOREIGN")" = "$T/user/agent.toml" ] || fail "$mode: foreign agent link rewritten"
  [ "$(cat "$T/user/agent.toml")" = "USER AGENT" ] || fail "$mode: foreign agent target changed"
  [ ! -L "$CH/agents/$A_REAL" ] && [ "$(cat "$CH/agents/$A_REAL")" = "REAL AGENT" ] || fail "$mode: real agent file replaced"
  [ "$(readlink "$CH/skills/$S_FOREIGN")" = "$T/user/skill" ] || fail "$mode: foreign skill link rewritten"
  [ ! -e "$T/user/skill/$S_FOREIGN" ] && [ "$(ls -A "$T/user/skill")" = "SKILL.md" ] || fail "$mode: nested link written into user skill target"
  [ ! -L "$CH/skills/$S_DIR" ] && [ "$(ls -A "$CH/skills/$S_DIR")" = "SKILL.md" ] || fail "$mode: real skill directory replaced or nested"
  for pair in "agents/$A_DANGLING:$SRC_AGENTS/$A_DANGLING" "agents/$A_OWNED:$SRC_AGENTS/$A_OWNED" "agents/$A_ABSENT:$SRC_AGENTS/$A_ABSENT" \
              "skills/$S_DANGLING:$SRC_SKILLS/$S_DANGLING" "skills/$S_OWNED:$SRC_SKILLS/$S_OWNED"; do
    [ "$(readlink "$CH/${pair%%:*}")" = "${pair#*:}" ] || fail "$mode: ${pair%%:*} not linked to harness source"
  done
  grep -q "^skip=$CH/agents/$A_FOREIGN reason=foreign-symlink-exists$" "$T/$mode.out" || fail "$mode: foreign agent skip missing"
  grep -q "^skip=$CH/agents/$A_REAL reason=non-symlink-exists$" "$T/$mode.out" || fail "$mode: real agent skip missing"
  grep -q "^skip=$CH/skills/$S_FOREIGN reason=foreign-symlink-exists$" "$T/$mode.out" || fail "$mode: foreign skill skip missing"
  grep -q "^skip=$CH/skills/$S_DIR reason=non-symlink-exists$" "$T/$mode.out" || fail "$mode: real skill dir skip missing"
  grep -q '^leaves_preserved=4$' "$T/$mode.out" || fail "$mode: leaves_preserved != 4"
  echo "ok: $mode preserves user entries and refreshes owned/dangling/absent"
done

# Idempotent second run: same preservation, no extra effects.
snap() { (cd "$CH" && find agents skills -mindepth 1 -maxdepth 1 -printf '%p %y %l\n' | sort); }
snap > "$T/snap1"; run "$T/again.out" || fail "re-run exited nonzero"; snap > "$T/snap2"
cmp -s "$T/snap1" "$T/snap2" || fail "re-run changed leaves"

# Empty home: every generated leaf is linked and nothing is skipped.
CH2=$T/codex-empty; mkdir -p "$CH2"
env -i PATH="$PATH" HOME="$HOME_DIR" XDG_CONFIG_HOME="$HOME_DIR/.config" CODEX_HOME="$CH2" TMPDIR="$T" AGENT_HOME="$ROOT" \
  sh "$ROOT/adapters/codex/bin/install-runtime-projection.sh" >"$T/empty.out" 2>/dev/null || fail "empty-home install failed"
grep -q '^leaves_preserved=0$' "$T/empty.out" || fail "empty home preserved something"
! grep -q '^skip=.*/\(agents\|skills\)/' "$T/empty.out" || fail "empty home skipped a leaf"
[ "$(ls "$CH2/agents" | wc -l)" = "$(ls "$SRC_AGENTS"/*.toml | wc -l)" ] || fail "empty home agent count"
[ "$(ls "$CH2/skills" | wc -l)" = "$(ls "$SRC_SKILLS" | wc -l)" ] || fail "empty home skill count"
echo "install-runtime-projection.test: PASS"
