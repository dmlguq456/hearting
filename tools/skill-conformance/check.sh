#!/usr/bin/env bash
# Deterministic skill-design conformance gate.
# Combines scan.sh observations with the explicit invocation classification
# registry so deterministic checks and drill g7 cannot silently accept a forbidden frontmatter flip.
set -uo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)
if [ -z "$ROOT" ]; then
  ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
fi
SCAN="$SCRIPT_DIR/scan.sh"
DEFAULT_POLICY="$SCRIPT_DIR/invocation-policy.tsv"
POLICY="${SKILL_INVOCATION_POLICY:-$DEFAULT_POLICY}"

default_scope=0
if [ "$#" -eq 0 ]; then
  default_scope=1
  set -- \
    "$ROOT/skills" \
    "$ROOT/adapters/claude/skills" \
    "$ROOT/adapters/codex/skills" \
    "$ROOT/adapters/opencode/skills"
fi

fail=0
if [ ! -x "$SCAN" ]; then
  echo "FAIL: skill scan executable missing: $SCAN"
  exit 1
fi
if [ ! -f "$POLICY" ]; then
  echo "FAIL: invocation policy missing: $POLICY"
  exit 1
fi

declare -a policy_names=()
declare -A policy_class=()
while IFS=$'\t' read -r name class _rest; do
  name=${name%$'\r'}
  class=${class%$'\r'}
  [ -z "$name" ] && continue
  [[ "$name" == \#* ]] && continue
  case "$class" in
    user-only|parent-invoked|entry-router|model-support|compiler-internal) ;;
    *) echo "FAIL: unknown invocation class '$class' for $name"; fail=1; continue ;;
  esac
  if [ -n "${policy_class[$name]+x}" ]; then
    echo "FAIL: duplicate invocation policy row: $name"
    fail=1
    continue
  fi
  policy_names+=("$name")
  policy_class[$name]="$class"
done < "$POLICY"

if [ "${#policy_names[@]}" -eq 0 ]; then
  echo "FAIL: invocation policy has no classified skills: $POLICY"
  exit 1
fi
if [ "$POLICY" = "$DEFAULT_POLICY" ] && \
   ! python3 "$ROOT/tools/sync-skill-invocation-policy.py" --check >/dev/null; then
  echo "FAIL: canonical invocation policy is stale against harness-manifest.json"
  fail=1
fi

portable_names=$(find "$ROOT/capabilities" -maxdepth 1 -type f -name '*.md' ! -name README.md \
  -exec basename {} .md \; 2>/dev/null | sort)
if [ -z "$portable_names" ]; then
  echo "FAIL: portable capability domain is empty: $ROOT/capabilities"
  exit 1
fi
policy_names_sorted=$(printf '%s\n' "${policy_names[@]}" | sort)
# A compiler-internal capability is classified and documented but is never a Skill:
# every Skill-tree comparison below runs over the portable names without it.
internal_names=$(for name in "${policy_names[@]}"; do
  [ "${policy_class[$name]}" = "compiler-internal" ] && printf '%s\n' "$name"
done | sort)
skill_names=$(comm -23 <(printf '%s\n' "$portable_names") <(printf '%s\n' "$internal_names"))
missing_policy=$(comm -23 <(printf '%s\n' "$portable_names") <(printf '%s\n' "$policy_names_sorted"))
extra_policy=$(comm -13 <(printf '%s\n' "$portable_names") <(printf '%s\n' "$policy_names_sorted"))
[ -n "$missing_policy" ] && { echo "FAIL: portable capabilities missing from invocation policy: $missing_policy"; fail=1; }
[ -n "$extra_policy" ] && { echo "FAIL: non-portable capabilities in invocation policy: $extra_policy"; fail=1; }

for skills_dir in "$@"; do
  if [ ! -d "$skills_dir" ]; then
    echo "FAIL: skills directory missing: $skills_dir"
    fail=1
    continue
  fi
  if ! tsv=$(bash "$SCAN" "$skills_dir" 2>/dev/null); then
    echo "FAIL: scan.sh failed: $skills_dir"
    fail=1
    continue
  fi

  header=$(printf '%s\n' "$tsv" | head -1)
  if [ "$(printf '%s\n' "$header" | cut -f1)" != "name" ] || \
     [ "$(printf '%s\n' "$header" | cut -f4)" != "disable_model" ] || \
     [ "$(printf '%s\n' "$header" | cut -f6)" != "use_when" ] || \
     [ "$(printf '%s\n' "$header" | cut -f11)" != "not_for" ] || \
     [ "$(printf '%s\n' "$header" | cut -f12)" != "generic_trigger" ]; then
    echo "FAIL: scan.sh TSV schema drift: $skills_dir"
    fail=1
    continue
  fi
  body=$(printf '%s\n' "$tsv" | tail -n +2)

  if [ "$default_scope" -eq 1 ]; then
    actual_names=$(printf '%s\n' "$body" | awk -F'\t' 'NF{print $1}' | sort)
    missing=$(comm -23 <(printf '%s\n' "$skill_names") <(printf '%s\n' "$actual_names"))
    extra=$(comm -13 <(printf '%s\n' "$skill_names") <(printf '%s\n' "$actual_names"))
    [ -n "$missing" ] && { echo "FAIL: portable capabilities missing from $skills_dir: $missing"; fail=1; }
    [ -n "$extra" ] && { echo "FAIL: non-portable skills present in $skills_dir: $extra"; fail=1; }
  fi

  case "$skills_dir" in
    "$ROOT/adapters/codex/skills"|"$ROOT/adapters/opencode/skills") native_generated=1 ;;
    *) native_generated=0 ;;
  esac

  bad_len=$(printf '%s\n' "$body" | awk -F'\t' '$3=="N"{print $1}')
  [ -n "$bad_len" ] && { echo "FAIL: SKILL.md >=500 lines in $skills_dir: $bad_len"; fail=1; }
  bad_depth=$(printf '%s\n' "$body" | awk -F'\t' '$9=="N"{print $1}')
  [ -n "$bad_depth" ] && { echo "FAIL: references/ depth >1 in $skills_dir: $bad_depth"; fail=1; }
  generic=$(printf '%s\n' "$body" | awk -F'\t' '$12=="Y"{print $1}')
  [ -n "$generic" ] && { echo "FAIL: generic or circular invocation trigger in $skills_dir: $generic"; fail=1; }

  for name in "${policy_names[@]}"; do
    row=$(printf '%s\n' "$body" | awk -F'\t' -v n="$name" '$1==n{print; exit}')
    if [ "${policy_class[$name]}" = "compiler-internal" ]; then
      [ -z "$row" ] || { echo "FAIL: compiler-internal capability has a Skill in $skills_dir: $name"; fail=1; }
      continue
    fi
    if [ -z "$row" ]; then
      echo "FAIL: classified skill missing from $skills_dir: $name"
      fail=1
      continue
    fi
    disable=$(printf '%s\n' "$row" | cut -f4)
    use_when=$(printf '%s\n' "$row" | cut -f6)
    not_for=$(printf '%s\n' "$row" | cut -f11)
    use_only_when=$(printf '%s\n' "$row" | cut -f13)
    top_level_exclusion=$(printf '%s\n' "$row" | cut -f14)
    primary_exclusion=$(printf '%s\n' "$row" | cut -f15)
    case "${policy_class[$name]}" in
      user-only)
        if [ "$native_generated" -eq 0 ]; then
          [ "$disable" = "true" ] || { echo "FAIL: $name is user-only but disable_model=$disable ($skills_dir)"; fail=1; }
        fi
        ;;
      parent-invoked)
        [ "$disable" = "false" ] || { echo "FAIL: $name is parent-invoked but disable_model=$disable ($skills_dir)"; fail=1; }
        [ "$use_only_when" = "Y" ] || { echo "FAIL: $name is parent-invoked but internal-stage trigger is missing ($skills_dir)"; fail=1; }
        [ "$top_level_exclusion" = "Y" ] || { echo "FAIL: $name is parent-invoked but top-level exclusion is missing ($skills_dir)"; fail=1; }
        ;;
      entry-router)
        # Explicit-only: Claude-form trees carry disable-model-invocation; Codex and OpenCode
        # trees keep frontmatter free of it (Codex uses agents/openai.yaml, OpenCode its commands).
        if [ "$native_generated" -eq 0 ]; then
          [ "$disable" = "true" ] || { echo "FAIL: $name is entry-router but disable_model=$disable ($skills_dir)"; fail=1; }
        else
          [ "$disable" = "false" ] || { echo "FAIL: $name is entry-router but disable_model=$disable ($skills_dir)"; fail=1; }
        fi
        [ "$use_when" = "Y" ] || { echo "FAIL: $name is entry-router but use_when=$use_when ($skills_dir)"; fail=1; }
        [ "$not_for" = "Y" ] || { echo "FAIL: $name is entry-router but exclusion boundary is missing ($skills_dir)"; fail=1; }
        ;;
      model-support)
        [ "$disable" = "false" ] || { echo "FAIL: $name is model-support but disable_model=$disable ($skills_dir)"; fail=1; }
        [ "$use_when" = "Y" ] || { echo "FAIL: $name is model-support but use_when=$use_when ($skills_dir)"; fail=1; }
        [ "$primary_exclusion" = "Y" ] || { echo "FAIL: $name is model-support but primary-route exclusion is missing ($skills_dir)"; fail=1; }
        ;;
    esac
  done

  if [ "$native_generated" -eq 1 ]; then
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      skill="$skills_dir/$name/SKILL.md"
      if ! grep -Fq "capabilities/$name.md" "$skill"; then
        echo "FAIL: native skill lacks portable source pointer: $skill"
        fail=1
      fi
    done <<EOF
$skill_names
EOF
  fi

  while IFS= read -r name; do
    [ -z "$name" ] && continue
    if [ "${policy_class[$name]:-}" != "user-only" ] && [ "${policy_class[$name]:-}" != "entry-router" ]; then
      echo "FAIL: unexpected disable-model-invocation=true without user-only classification: $name ($skills_dir)"
      fail=1
    fi
  done < <(printf '%s\n' "$body" | awk -F'\t' '$4=="true"{print $1}')

  # User-facing language follows the portable audience-language contract.
  # Reject fixed Korean-output directives while allowing conditional Korean
  # mirrors, examples, tokenization fixtures, and existing schema literals.
  fixed_language_re='Korean output|output in Korean|in Korean:[[:space:]]*$|Print to user \(Korean\)|print to chat \(Korean\)|Korean summary|Korean brief|보고는 한국어로|사용자 대화는 한국어|사용자 출력은 자연스러운 한국어'
  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    echo "FAIL: fixed user-facing language directive: $hit"
    fail=1
  done < <(grep -RInE --include='*.md' "$fixed_language_re" "$skills_dir" 2>/dev/null || true)
done

if [ "$fail" -eq 0 ]; then
  if ! PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tools/entry-skill-layer.test.py"; then
    fail=1
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "PASS: skill conformance (portable domain + four Skill trees + manifest-generated invocation policy ${#policy_names[@]} classifications + routing boundaries + audience-language neutrality)"
fi
exit "$fail"
