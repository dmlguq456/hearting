#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
HARNESS="$ROOT/tools/install/harness.sh"
FIXTURES="$ROOT/tools/install/fixtures/extensions"
TMP=$(mktemp -d)
# destructive-ok: reason=clean one mktemp extension fixture; boundary=TMP returned by the immediately preceding mktemp call
trap 'chmod -R u+w "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT HUP INT TERM

eval "$(python3 "$ROOT/tools/install/fixture_env.py" shell "$TMP" "$ROOT")"

mkdir -p "$TMP/sources"
cp -R "$FIXTURES/instruction-only" "$TMP/sources/instruction-only"
cp -R "$FIXTURES/runtime-specific" "$TMP/sources/runtime-specific"
SOURCE="$TMP/sources/instruction-only"
RUNTIME_SOURCE="$TMP/sources/runtime-specific"
CANONICAL="external/fixture-labs/portable-review"

expect_exit() {
    expected=$1
    shift
    set +e
    "$@"
    actual=$?
    set -e
    if [ "$actual" -ne "$expected" ]; then
        echo "expected exit $expected, got $actual: $*" >&2
        exit 1
    fi
}

json_value() {
    path=$1
    expression=$2
    python3 - "$path" "$expression" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2], {"__builtins__": {}}, {"data": data}))
PY
}

tree_digest() {
    directory=$1
    find "$directory" -type f -print0 |
        sort -z |
        xargs -0 sha256sum |
        sha256sum |
        awk '{print $1}'
}

assert_links() {
    expected=$1
    physical=$2
    for destination in "$HOME/.claude/skills/$physical" "$CODEX_HOME/skills/$physical" "$XDG_CONFIG_HOME/opencode/skills/$physical"
    do
        test -L "$destination"
        test "$(readlink "$destination")" = "$expected"
    done
}

SOURCE_BEFORE=$(tree_digest "$SOURCE")
mkdir "$TMP/sources/invalid"
expect_exit 2 "$HARNESS" extension inspect "$TMP/sources/invalid" --json >"$TMP/invalid.json"
test "$(json_value "$TMP/invalid.json" 'data["reason"]')" = manifest-missing
"$HARNESS" extension inspect "$FIXTURES/instruction-only" --json >"$TMP/git-inspect.json"
python3 - "$TMP/git-inspect.json" <<'PY'
import json
import sys

provenance = json.load(open(sys.argv[1], encoding="utf-8"))["extension"]["provenance"]
assert provenance["kind"] == "local-git"
assert len(provenance["revision"]) == 40
assert provenance["ref"]
assert isinstance(provenance["dirty"], bool)
PY
"$HARNESS" extension inspect "$SOURCE" --json >"$TMP/inspect.json"
test ! -e "$XDG_STATE_HOME"
python3 - "$TMP/inspect.json" <<'PY'
import json
import re
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
report = result["extension"]
assert result["exit"] == 0
assert report["status"] == "ready"
assert report["canonical_id"] == "external/fixture-labs/portable-review"
assert len(report["physical_id"]) <= 64
assert re.fullmatch(r"external-[a-z0-9-]+-[0-9a-f]{12}", report["physical_id"])
assert all(item["status"] == "full" for item in report["parity"].values())
assert report["inactive_surfaces"] == []
assert report["license"]["status"] == "declared-with-file"
PY
PYTHONPATH="$ROOT/tools/install:$ROOT/tools" python3 - <<'PY'
import extensions

first = extensions.physical_id("external/foo-bar/baz")
second = extensions.physical_id("external/foo/bar-baz")
assert first != second
assert len(first) <= 64 and len(second) <= 64
PY

"$HARNESS" extension inspect "$RUNTIME_SOURCE" --json >"$TMP/runtime-inspect.json"
python3 - "$TMP/runtime-inspect.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))["extension"]
assert report["status"] == "ready"
assert set(report["inactive_surfaces"]) == {"hooks", "plugins"}
assert all(item["status"] == "degraded" for item in report["parity"].values())
PY
expect_exit 3 env XDG_STATE_HOME=relative-state "$HARNESS" extension add "$RUNTIME_SOURCE" --runtime codex --json >"$TMP/relative-xdg.json"
test "$(json_value "$TMP/relative-xdg.json" 'data["reason"]')" = invalid-xdg-root

"$HARNESS" extension add "$SOURCE" --runtime all --json >"$TMP/add.json"
PHYSICAL=$(json_value "$TMP/add.json" 'data["extension"]["physical_id"]')
SNAPSHOT=$(json_value "$TMP/add.json" 'data["extension"]["snapshot"]')
assert_links "$SNAPSHOT" "$PHYSICAL"
test "$SOURCE_BEFORE" = "$(tree_digest "$SOURCE")"
test -f "$SNAPSHOT/SKILL.md"
test -f "$SNAPSHOT/reference.md"
test "$(find "$SNAPSHOT" -type f ! -name '*.md' | wc -l | tr -d ' ')" = 0
test "$(stat -c %a "$SNAPSHOT/SKILL.md")" = 444
grep -q "^name: $PHYSICAL$" "$SNAPSHOT/SKILL.md"
python3 - "$XDG_STATE_HOME/hearting/extensions/registry.json" "$SNAPSHOT" "$CODEX_HOME" <<'PY'
import json
import sys

registry = json.load(open(sys.argv[1], encoding="utf-8"))
entry = registry["extensions"]["external/fixture-labs/portable-review"]
assert registry["schema_version"] == 1
assert registry["generation"] == 1
assert entry["runtimes"] == ["claude", "codex", "opencode"]
assert entry["runtime_roots"]["codex"] == sys.argv[3]
assert sys.argv[2].endswith(entry["snapshot_key"])
assert all(len(entry[key]) == 64 for key in ("source_checksum", "projection_checksum", "snapshot_key"))
assert "dest" not in entry and "snapshot" not in entry
PY

expect_exit 3 env CODEX_HOME="$TMP/other-codex" "$HARNESS" extension update "$CANONICAL" --json >"$TMP/runtime-root.json"
test "$(json_value "$TMP/runtime-root.json" 'data["reason"]')" = runtime-root-changed
assert_links "$SNAPSHOT" "$PHYSICAL"

# External extension paths must never count as a built-in native projection.
PYTHONPATH="$ROOT/tools/install:$ROOT/tools" python3 - <<'PY'
import runtime_activation

runtime_activation._codex_plugin_active = lambda scope="global": True
assert runtime_activation._native_present("codex") is False
assert runtime_activation.duplicate_sources("codex") == []
PY

"$HARNESS" extension update "$CANONICAL" --json >"$TMP/noop.json"
test "$(json_value "$TMP/noop.json" 'data["extension"]["changed"]')" = False
test "$(json_value "$TMP/noop.json" 'data["extension"]["snapshot"]')" = "$SNAPSHOT"

printf '\nUpdated evidence.\n' >>"$SOURCE/skill/reference.md"
"$HARNESS" extension update "$CANONICAL" --json >"$TMP/update.json"
UPDATED_SNAPSHOT=$(json_value "$TMP/update.json" 'data["extension"]["snapshot"]')
test "$UPDATED_SNAPSHOT" != "$SNAPSHOT"
assert_links "$UPDATED_SNAPSHOT" "$PHYSICAL"
test ! -e "$SNAPSHOT"

# A normal failure rolls back every runtime link, registry generation, and journal.
printf '\nRollback candidate.\n' >>"$SOURCE/skill/reference.md"
REGISTRY="$XDG_STATE_HOME/hearting/extensions/registry.json"
REGISTRY_BEFORE=$(sha256sum "$REGISTRY" | awk '{print $1}')
expect_exit 3 env HARNESS_EXTENSION_FAIL_AFTER=1 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/fail.json"
assert_links "$UPDATED_SNAPSHOT" "$PHYSICAL"
test "$REGISTRY_BEFORE" = "$(sha256sum "$REGISTRY" | awk '{print $1}')"
test ! -e "$XDG_STATE_HOME/hearting/extensions/transaction.json"

# A hard process exit leaves a journal; the next mutation recovers before applying.
printf '\nCrash candidate.\n' >>"$SOURCE/skill/reference.md"
cp "$REGISTRY" "$TMP/registry.precrash"
expect_exit 97 env HARNESS_EXTENSION_HARD_EXIT_AFTER=1 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/crash.json"
test -f "$XDG_STATE_HOME/hearting/extensions/transaction.json"
python3 - "$REGISTRY" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["generation"] += 7
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle)
PY
expect_exit 3 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/recovery-conflict.json"
test "$(json_value "$TMP/recovery-conflict.json" 'data["reason"]')" = recovery-conflict
test -f "$XDG_STATE_HOME/hearting/extensions/transaction.json"
cp "$TMP/registry.precrash" "$REGISTRY"
"$HARNESS" extension update "$CANONICAL" --json >"$TMP/recovered.json"
RECOVERED_SNAPSHOT=$(json_value "$TMP/recovered.json" 'data["extension"]["snapshot"]')
test "$(json_value "$TMP/recovered.json" 'data["extension"]["recovered"]')" = True
assert_links "$RECOVERED_SNAPSHOT" "$PHYSICAL"
test ! -e "$XDG_STATE_HOME/hearting/extensions/transaction.json"

# Replacing a snapshot parent with a symlink must not read or delete the target.
SNAPSHOT_PARENT="$XDG_DATA_HOME/hearting/extensions/fixture-labs/portable-review"
# destructive-ok: reason=simulate missing extension snapshot storage; boundary=fixture snapshot directory and sibling saved name below TMP
mv "$SNAPSHOT_PARENT" "$SNAPSHOT_PARENT.saved"
mkdir "$TMP/external-snapshot-parent"
printf 'keep\n' >"$TMP/external-snapshot-parent/marker"
ln -s "$TMP/external-snapshot-parent" "$SNAPSHOT_PARENT"
expect_exit 3 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/parent-symlink.json"
test "$(json_value "$TMP/parent-symlink.json" 'data["reason"]')" = unsafe-snapshot-path
test -f "$TMP/external-snapshot-parent/marker"
# destructive-ok: reason=remove one synthetic snapshot-parent symlink; boundary=exact fixture symlink below TMP
rm "$SNAPSHOT_PARENT"
# destructive-ok: reason=restore synthetic snapshot storage name; boundary=fixture saved directory and exact snapshot path below TMP
mv "$SNAPSHOT_PARENT.saved" "$SNAPSHOT_PARENT"

# Snapshot reuse is allowed only after a full digest recheck.
cp "$RECOVERED_SNAPSHOT/reference.md" "$TMP/reference.saved"
chmod u+w "$RECOVERED_SNAPSHOT/reference.md"
printf '\nTampered snapshot.\n' >>"$RECOVERED_SNAPSHOT/reference.md"
chmod 444 "$RECOVERED_SNAPSHOT/reference.md"
expect_exit 3 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/tamper.json"
test "$(json_value "$TMP/tamper.json" 'data["reason"]')" = snapshot-drift
chmod u+w "$RECOVERED_SNAPSHOT/reference.md"
cp "$TMP/reference.saved" "$RECOVERED_SNAPSHOT/reference.md"
chmod 444 "$RECOVERED_SNAPSHOT/reference.md"

# Registry fields are untrusted and cannot redirect mutation paths.
cp "$REGISTRY" "$TMP/registry.saved"
python3 - "$REGISTRY" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["extensions"]["external/fixture-labs/portable-review"]["physical_id"] = "external-attacker"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle)
PY
expect_exit 3 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/registry-tamper.json"
test "$(json_value "$TMP/registry-tamper.json" 'data["reason"]')" = registry-invalid
cp "$TMP/registry.saved" "$REGISTRY"

# A held writer lock fails closed when explicit non-blocking mode is requested.
LOCK="$XDG_STATE_HOME/hearting/extensions/.lock"
MARKER="$TMP/lock-held"
python3 - "$LOCK" "$MARKER" <<'PY' &
import fcntl
import pathlib
import sys
import time

with open(sys.argv[1], "r+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    pathlib.Path(sys.argv[2]).write_text("held", encoding="utf-8")
    time.sleep(2)
PY
LOCK_PID=$!
while [ ! -f "$MARKER" ]; do :; done
expect_exit 3 env HARNESS_EXTENSION_LOCK_NONBLOCK=1 "$HARNESS" extension update "$CANONICAL" --json >"$TMP/concurrent.json"
test "$(json_value "$TMP/concurrent.json" 'data["reason"]')" = concurrent-writer
wait "$LOCK_PID"

# Executable dependencies, high-confidence secrets, and source escapes block.
cp -R "$FIXTURES/instruction-only" "$TMP/sources/package"
printf '{}\n' >"$TMP/sources/package/package.json"
expect_exit 2 "$HARNESS" extension inspect "$TMP/sources/package" --json >"$TMP/package.json"
python3 - "$TMP/package.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))["extension"]
assert report["status"] == "blocked"
assert report["external_dependency_required"] is True
assert any(item["id"] == "external-dependency" for item in report["findings"])
PY
expect_exit 3 "$HARNESS" extension add "$TMP/sources/package" --runtime codex --json >"$TMP/package-add.json"

cp -R "$FIXTURES/instruction-only" "$TMP/sources/secret"
TOKEN="github_pat_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
printf '%s\n' "$TOKEN" >"$TMP/sources/secret/skill/secret.md"
printf '%s\n' '-----BEGIN ENCRYPTED PRIVATE KEY-----' >"$TMP/sources/secret/skill/key.md"
expect_exit 2 "$HARNESS" extension inspect "$TMP/sources/secret" --json >"$TMP/secret.json"
grep -q '"id": "secret-github-fine-grained-token"' "$TMP/secret.json"
grep -q '"id": "secret-private-key"' "$TMP/secret.json"
if grep -q "$TOKEN" "$TMP/secret.json"; then
    echo "secret value leaked into inspection output" >&2
    exit 1
fi

cp -R "$FIXTURES/instruction-only" "$TMP/sources/escape"
printf 'outside\n' >"$TMP/outside.md"
ln -s "$TMP/outside.md" "$TMP/sources/escape/skill/escape.md"
expect_exit 2 "$HARNESS" extension inspect "$TMP/sources/escape" --json >"$TMP/escape.json"
grep -q '"id": "symlink-escape"' "$TMP/escape.json"

cp -R "$FIXTURES/instruction-only" "$TMP/sources/internal-link"
# destructive-ok: reason=construct an internal-link extension fixture; boundary=two exact source leaves below TMP
mv "$TMP/sources/internal-link/skill/reference.md" "$TMP/sources/internal-link/skill/target.md"
ln -s target.md "$TMP/sources/internal-link/skill/reference.md"
"$HARNESS" extension inspect "$TMP/sources/internal-link" --json >"$TMP/internal-link.json"
test "$(json_value "$TMP/internal-link.json" 'data["extension"]["status"]')" = ready

cp -R "$FIXTURES/instruction-only" "$TMP/sources/manifest-link"
# destructive-ok: reason=construct a manifest-symlink extension fixture; boundary=two exact manifest leaves below TMP
mv "$TMP/sources/manifest-link/extension.json" "$TMP/sources/manifest-link/real.json"
ln -s real.json "$TMP/sources/manifest-link/extension.json"
expect_exit 2 "$HARNESS" extension inspect "$TMP/sources/manifest-link" --json >"$TMP/manifest-link.json"
test "$(json_value "$TMP/manifest-link.json" 'data["reason"]')" = manifest-type

# A foreign sibling matching the atomic-temp prefix remains untouched.
PYTHONPATH="$ROOT/tools/install:$ROOT/tools" python3 - "$TMP" <<'PY'
import pathlib
import sys
import extensions
import safe_fs

root = pathlib.Path(sys.argv[1]) / "atomic-link"
root.mkdir()
destination = root / "owned"
collision = root / ".owned.hearting-foreign"
collision.write_text("foreign", encoding="utf-8")
postimage = extensions._atomic_symlink(
    root / "target", destination, safe_fs.capture_state(destination)
)
assert postimage.kind == "symlink"
assert destination.readlink() == root / "target"
assert collision.read_text(encoding="utf-8") == "foreign"
PY

# A foreign destination collision is rejected before registry ownership changes.
RUNTIME_PHYSICAL=$(json_value "$TMP/runtime-inspect.json" 'data["extension"]["physical_id"]')
mkdir -p "$CODEX_HOME/skills"
printf 'foreign\n' >"$CODEX_HOME/skills/$RUNTIME_PHYSICAL"
GENERATION_BEFORE=$(json_value "$REGISTRY" 'data["generation"]')
expect_exit 3 "$HARNESS" extension add "$RUNTIME_SOURCE" --runtime codex --json >"$TMP/collision.json"
test "$(json_value "$TMP/collision.json" 'data["reason"]')" = destination-collision
test "$(json_value "$REGISTRY" 'data["generation"]')" = "$GENERATION_BEFORE"
# destructive-ok: reason=construct one missing extension projection; boundary=exact runtime skill link below fixture CODEX_HOME
rm "$CODEX_HOME/skills/$RUNTIME_PHYSICAL"

"$HARNESS" extension remove "$CANONICAL" --json >"$TMP/remove.json"
for destination in "$HOME/.claude/skills/$PHYSICAL" "$CODEX_HOME/skills/$PHYSICAL" "$XDG_CONFIG_HOME/opencode/skills/$PHYSICAL"
do
    test ! -e "$destination"
    test ! -L "$destination"
done
test ! -e "$RECOVERED_SNAPSHOT"
python3 - "$REGISTRY" <<'PY'
import json
import sys

registry = json.load(open(sys.argv[1], encoding="utf-8"))
assert registry["generation"] >= 4
assert registry["extensions"] == {}
PY

# Even a dangling external link cannot satisfy native built-in detection.
ln -s "$TMP/missing-extension-snapshot" "$CODEX_HOME/skills/$PHYSICAL"
PYTHONPATH="$ROOT/tools/install:$ROOT/tools" python3 - <<'PY'
import runtime_activation

runtime_activation._codex_plugin_active = lambda scope="global": True
assert runtime_activation._native_present("codex") is False
assert runtime_activation.duplicate_sources("codex") == []
PY

echo "extension-lifecycle: ok"
