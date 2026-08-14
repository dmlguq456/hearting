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
[ "$(AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" --completed-at 2026-07-30T07:00:00Z)" = '{"status":"created"}' ]
[ "$(AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 --bundle-version v2 --entrypoint report/index.html)" = '{"status":"created"}' ]
[ "$(AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --bundle-id demo/eval-1 --bundle-version v2 --entrypoint report/index.html)" = '{"status":"created"}' ]
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 --bundle-version v2 --entrypoint /absolute/index.html >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability 'bad value' --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" --completed-at not-a-date >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi
ln -s "$TMP/project/result.md" "$TMP/project/result-link.md"
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler" "$SINK" emit --source "$TMP/project/result-link.md" --capability autopilot-code --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi

# --- drift (1) contract lock (OD-3 sec.2.4): v1 args on a v2 emit are ignored,
# not leaked into the v2 receipt -- assert this on the receipt itself.
cat > "$TMP/handler-v1args" <<'SH'
#!/bin/sh
if [ "$1" = --check ]; then printf '{"status":"connected"}\n'; exit 0; fi
[ "$1" = --receipt ] || exit 64
python3 - "$2" <<'PY'
import json, sys
v = json.load(open(sys.argv[1], encoding='utf-8'))
bundle = {'schema_version','event','status','completed_at','bundle_id','version','entrypoint'}
assert v['schema_version'] == 2 and set(v) == bundle
assert not {'source_path','source_capability','project_root'} & set(v)
PY
printf '{"status":"created"}\n'
SH
chmod 700 "$TMP/handler-v1args"
[ "$(AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v1args" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-lab --project-root "$TMP/project" --bundle-id demo/eval-1 --bundle-version v2 --entrypoint report/index.html)" = '{"status":"created"}' ]

# --- OD-12 sink isolation: primary artifact result.md must never change ---
PRIMARY_SHA_BEFORE=$(sha256sum "$TMP/project/result.md" | awk '{print $1}')

# (a) sink absent -> exit 69, primary unchanged (publication word: not-offered)
if env -u AGENT_ARTIFACT_SINK_COMMAND "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; else [ "$?" -eq 69 ]; fi
[ -f "$TMP/project/result.md" ]
[ "$(sha256sum "$TMP/project/result.md" | awk '{print $1}')" = "$PRIMARY_SHA_BEFORE" ]

# (b) handler exit 1 -> primary unchanged (publication word: failed)
cat > "$TMP/handler-fail" <<'SH'
#!/bin/sh
exit 1
SH
chmod 700 "$TMP/handler-fail"
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-fail" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; fi
[ -f "$TMP/project/result.md" ]
[ "$(sha256sum "$TMP/project/result.md" | awk '{print $1}')" = "$PRIMARY_SHA_BEFORE" ]

# (c) --check ok, --receipt fail -> primary unchanged (publication word: failed)
cat > "$TMP/handler-check-ok-receipt-fail" <<'SH'
#!/bin/sh
if [ "$1" = --check ]; then printf '{"status":"connected"}\n'; exit 0; fi
exit 1
SH
chmod 700 "$TMP/handler-check-ok-receipt-fail"
AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-check-ok-receipt-fail" "$SINK" --check | grep -q connected
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-check-ok-receipt-fail" "$SINK" emit --source "$TMP/project/result.md" --capability autopilot-code --project-root "$TMP/project" >/dev/null 2>&1; then exit 1; fi
[ -f "$TMP/project/result.md" ]
[ "$(sha256sum "$TMP/project/result.md" | awk '{print $1}')" = "$PRIMARY_SHA_BEFORE" ]

# --- v3 (manifest-native) emit ---
MKROOT_DIR=$(mktemp -d)
cat > "$TMP/mkroot.py" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[2], "utilities"))
import artifact_admission as adm
import artifact_identity as idm
import artifact_manifest as m

root = sys.argv[1]
alloc = idm.IdAllocator()
identity = adm.ensure_root_identity(root, allocator=alloc)
camp_id = alloc.allocate("campaign")
cyc_id = alloc.allocate("cycle")
art_id = alloc.allocate("artifact")
arev_id = alloc.allocate("artifact_revision")
man_id = alloc.allocate("manifest")
mrev_id = alloc.allocate("manifest_revision")
prod_id = alloc.allocate("producer")
content = b"hello"
digest = m.digest_bytes(content)
provenance = {
    "source_manifest_id": man_id, "source_revision_id": mrev_id,
    "producer_route_id": "r", "algorithm_version": "v1",
    "schema_version": 1, "source_digest": "sha256:" + ("2" * 64),
}
doc = {
    "schema_version": 2, "manifest_kind": "artifact.cycle",
    "manifest_id": man_id, "manifest_revision_id": mrev_id,
    "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id,
    "campaign": {"campaign_id": camp_id, "goal": "g", "completion_criterion": {"statement": "s"}, "title": "t", "state": "active"},
    "cycle": {"cycle_id": cyc_id, "campaign_id": camp_id, "parent_cycle_id": None,
              "started_on": "2026-08-11T00:00:00Z", "input_digest": "sha256:" + ("0" * 64),
              "outcome_criterion": {"required_artifact_roles": [], "decision_required": False}, "state": "active"},
    "artifacts": [{"artifact_id": art_id, "cycle_id": cyc_id, "role": "primary", "type": "doc", "capability": "c", "title": "t"}],
    "artifact_revisions": [{"artifact_revision_id": arev_id, "artifact_id": art_id, "revision_sequence": 1,
                             "content_digest": digest, "byte_size": len(content), "media_type": "text/plain",
                             "locator": {"kind": "cycle-relative", "path": "plan.md"}, "provenance": provenance}],
    "shared_references": [], "shared_reference_revisions": [], "routes": [], "events": [],
    "producer": {"producer_id": prod_id, "contract_version": "artifact-cycle-manifest/v2", "source_revision": "abc"},
}
staging = sys.argv[3]
with open(os.path.join(staging, "plan.md"), "wb") as fh:
    fh.write(content)
outcome = adm.admit(root, adm.AdmissionRequest(idempotency_key=man_id, document=doc, staging_source=staging, allocator=alloc))
assert outcome.status == "admitted", (outcome.status, outcome.violations)
print("repository_id=" + identity.repository_id)
print("campaign_id=" + camp_id)
print("cycle_id=" + cyc_id)
print("artifact_id=" + art_id)
print("artifact_revision_id=" + arev_id)
print("manifest_id=" + man_id)
print("manifest_revision_id=" + mrev_id)
PY
STAGE_DIR=$(mktemp -d)
eval "$(python3 "$TMP/mkroot.py" "$MKROOT_DIR" "$ROOT" "$STAGE_DIR")"

cat > "$TMP/handler-v3" <<'SH'
#!/bin/sh
if [ "$1" = --check ]; then printf '{"status":"connected"}\n'; exit 0; fi
[ "$1" = --receipt ] || exit 64
[ "$(stat -c %a "$2")" = 600 ] || exit 71
python3 - "$2" <<'PY'
import json, sys
v = json.load(open(sys.argv[1], encoding='utf-8'))
v3keys = {'schema_version','event','status','completed_at','repository_id','campaign_id','cycle_id','artifact_id','artifact_revision_id','manifest_id','manifest_revision_id'}
assert v['schema_version'] == 3 and set(v) == v3keys
assert not {'manifest_path','cycle_path','bundle_id','source_path','body'} & set(v)
PY
printf '{"status":"created"}\n'
SH
chmod 700 "$TMP/handler-v3"

[ "$(AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:00Z)" = '{"status":"created"}' ]

# v3 partial delivery (6 of 8 args) -> exit 64 (all-or-none, OD-5)
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --completed-at 2026-08-11T00:00:00Z >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi

# v3 missing --completed-at -> exit 64 (OD-6)
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi

# v3 + v2 mixed -> exit 64
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:00Z --bundle-id demo/eval-1 --bundle-version v2 --entrypoint report/index.html >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi

# v3 duplicate delivery -> exit 0 idempotent, ledger records unchanged (sha256 sum of dir contents stable)
LEDGER_RECORDS="$MKROOT_DIR/.runtime/artifact-receipts/v1/records"
LEDGER_SHA_BEFORE=$(find "$LEDGER_RECORDS" -type f -exec sha256sum {} + | sort | sha256sum)
AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:00Z | grep -q created
LEDGER_SHA_AFTER=$(find "$LEDGER_RECORDS" -type f -exec sha256sum {} + | sort | sha256sum)
[ "$LEDGER_SHA_BEFORE" = "$LEDGER_SHA_AFTER" ]

# v3 identity conflict (same IDs, different --completed-at) -> exit 64
if [ -n "$(AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:01Z 2>/dev/null)" ]; then exit 1; fi
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:01Z >/dev/null 2>&1; then exit 1; else [ "$?" -eq 64 ]; fi

# ledger write failure remains inside the typed sink exit vocabulary (70), without traceback.
LEDGER_ROOT="$MKROOT_DIR/.runtime/artifact-receipts/v1"
mv "$LEDGER_ROOT" "$LEDGER_ROOT.saved"
printf 'read-only ledger placeholder\n' > "$LEDGER_ROOT"
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:00Z >"$TMP/v3-ledger-stdout" 2>"$TMP/v3-ledger-error"; then exit 1; else
  [ "$?" -eq 70 ]
fi
! grep -q 'Traceback' "$TMP/v3-ledger-error"
# exit 70 refusal is silent on stdout, exactly like the v1/v2 refusals above.
[ ! -s "$TMP/v3-ledger-stdout" ]

# v3 unavailable (69): a corrupt admission index refuses during resolve, before
# the ledger is reached. stdout stays empty here too.
printf '{not valid json' > "$MKROOT_DIR/.runtime/artifact-admission/v1/index.json"
if AGENT_ARTIFACT_SINK_COMMAND="$TMP/handler-v3" "$SINK" emit --artifact-root "$MKROOT_DIR" \
  --repository-id "$repository_id" --campaign-id "$campaign_id" --cycle-id "$cycle_id" \
  --artifact-id "$artifact_id" --artifact-revision-id "$artifact_revision_id" \
  --manifest-id "$manifest_id" --manifest-revision-id "$manifest_revision_id" \
  --completed-at 2026-08-11T00:00:00Z >"$TMP/v3-unavailable-stdout" 2>/dev/null; then exit 1; else
  [ "$?" -eq 69 ]
fi
[ ! -s "$TMP/v3-unavailable-stdout" ]

printf 'artifact-sink tests: ok\n'
