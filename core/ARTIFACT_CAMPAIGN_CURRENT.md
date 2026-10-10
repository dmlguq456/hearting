# Campaign current export, v1

`hearting run artifact_producer campaign-export --artifact-root ROOT` observes
current campaign lifecycle and presentation without mutation. It uses the
shared `artifact_campaign.campaign_state` fold; there is no second event fold.
The JSON contract is `artifact-campaign-current/v1`, `schema_version: 1`.
Producing a document exits 0, including invalid/missing/conflict observations;
an OS failure preventing a document exits 65. Content status is the verdict.

The command takes no admission lock, writes no index, record, history, recovery
intent or projection, and starts no background job. `campaign-status` and
`campaign-list` are also pure queries. Writers retain recovery and projection
materialization. Lifecycle and archive presentation have separate verdicts.

The `campaign-close` route sweep is limited to the selected campaign. Unrelated
campaigns' open routes and empty cycle controls remain untouched by that sweep.

## Fixed document fields

| Field | Type and meaning |
|---|---|
| `contract` | literal `artifact-campaign-current/v1` |
| `schema_version` | integer 1 |
| `artifact_root_id`, `repository_id` | RootIdentity stable ID or null when unavailable/invalid |
| `artifact_root_path` | current absolute resolved root path; historical event roots remain provenance |
| `status` | `valid`, `invalid`, `missing`, or `conflict` |
| `reason` | null for valid; reason code otherwise |
| `observed_at` | RFC3339 UTC observation time; excluded from every input digest |
| `inputs` | all consumed file/absence/directory observations, as below |
| `campaigns` | rows sorted by stable campaign ID, falling back to locator |

Each row has exactly `campaign_id` (stable ID or null), `locator` (directory
name), `record_state` (raw JSON value, null when absent/unavailable), `state`
(validated `active|satisfied|abandoned|superseded` or null), `status`, `reason`,
`stream_id` (ID or null), `last_sequence` (nonnegative integer), `last_event_id`
(ID or null), `projection_pending` (boolean), `inputs`, `presentation_kind`
(raw string or null), `presentation_status` (`absent|valid|invalid`), and
`presentation_reason` (null or reason code). Invalid enum/type/identity is not
normalized to an active or satisfied success. Legacy absence of a mutable
state field retains the shared fold's historical active compatibility; an
explicit null or `completed` campaign state is invalid. The cycle/manifest
`completed` enum retains its existing meaning.

Root status aggregates lifecycle observations with conflict before invalid
before missing before valid; a mandatory identity/enumeration failure has its
own root reason. Presentation errors do not replace lifecycle errors or make
valid lifecycle invalid. A duplicate ID is a conflict in every colliding row.
An official locator amendment's direct relative alias to a canonical sibling
is skipped without following it or counting another campaign. Other campaign
links remain enumeration errors.
An input mutation clears affected validated lifecycle states and yields
`input-changed`; the root document is never a valid mixed observation.

## Inputs and consistency

An input is one of these fixed shapes. Paths are root-relative, with no
observation time included in the digest:

```json
{"path":"campaigns/example/campaign.json","sha256":"sha256:<64 lowercase hex>","bytes":123}
```

```json
{"path":"campaigns/example/meta.json","missing":true}
```

```json
{"path":"campaigns/example/campaign.events","entries":["000001.json"],"listing_sha256":"sha256:<64 lowercase hex>"}
```

```json
{"path":"campaigns/example/campaign.events","error":"campaign-directory-unreadable"}
```

`sha256` is of the exact raw bytes consumed by validation, including invalid
JSON bytes. `listing_sha256` is SHA256 of canonical UTF-8 JSON for the sorted
entry-name array. First-read captures are immutable: subsequent reads of the
same path use those bytes, not a fresh parse with an overwritten digest.
Final byte/stat checks detect changed, replaced, newly present, removed and
changed-then-restored inputs and relevant directory observations. A row's
inputs include shared RootIdentity and campaign membership observations plus
every actually inspected event/meta/historical manifest proof for that row.
Historical preserved revisions outside the campaign directory remain inputs.

## Reason codes

Lifecycle/export reasons are:

`root-identity-missing`, `root-identity-invalid`, `campaigns-directory-missing`,
`campaign-directory-invalid`, `campaign-directory-unreadable`,
`campaign-input-missing`, `campaign-input-invalid`, `campaign-input-unreadable`,
`campaign-input-kind-or-size`, `campaign-path-outside-root`, `campaign-symlink`,
`campaign-id-invalid`, `campaign-schema-invalid`, `campaign-state-invalid`, `campaign-identity-mismatch`,
`campaign-projection-conflict`, `campaign-event-invalid`,
`campaign-event-sequence-invalid`, `campaign-event-transition-invalid`,
`campaign-duplicate-id`, `campaign-rename-evidence-missing`,
`campaign-rename-binding-mismatch`, `campaign-rename-evidence-tampered`, and
`input-changed`.

Root/campaign mandatory absence is missing; duplicate/foreign identity,
projection contradiction and changed input are conflicts; malformed input,
schema, kind, enum or event is invalid. `campaign-event-invalid` retains the
shared fold's detailed rejection internally, including
`rename-evidence-missing`, `rename-binding-mismatch`,
`rename-evidence-tampered` and `closure-provenance-mismatch`.

Presentation reuses artifact-meta/v1 validation. Its reason is the metadata
reason with `presentation-` prefixed unless already present. Examples are
`presentation-kind-invalid`, `presentation-cycle-not-allowed`,
`presentation-repository-mismatch`, `presentation-identity-missing`,
`presentation-identity-mismatch`, `presentation-contract-unknown`, and
`presentation-json-invalid`. An input read failure may carry the shared input
reason; input drift carries `input-changed`. The full known-field validation
codes remain those of [ARTIFACT_META.md](ARTIFACT_META.md).

## Historical rename and normal goal completion

A legacy v1 or current v2 close retains its old root/hash/event IDs
and snapshot bytes. Every close verifies the current stable root and repository
IDs. A new v2 close snapshot records repository_id automatically. At its original
path both authenticated snapshot IDs must equal RootIdentity; no manifest
fallback is needed to prove those IDs. Relocated or legacy histories bind through
the snapshot's exact campaign and manifest/revision IDs and raw-byte digests.
A legacy snapshot lacking repository_id uses its surviving historical
manifest's repository/root IDs. A matching root ID alone grants no foreign
repository compatibility. Preserved historical revisions have authority over
later current revisions. A present malformed historical revision cannot fall
back to a current manifest. Missing, foreign or changed proof remains invalid.
Official v2 rows for ended routes without manifests retain their authenticated
snapshot provenance. At least one surviving manifest still binds the exact
repository/root/campaign; the reader invents no manifest for those rows.

Campaign goal satisfaction is separate from cycle/route PASS. The exact normal
owner primary may carry one optional `campaign-goal` JSON fence containing
`campaign_id`, `verdict: "satisfied"`, and optional `reason`; a JSON primary
uses `campaign_goal`. A generic JSON example or neighboring sidecar is ignored.
Claude, Codex and OpenCode share the same completion consumer. The existing
terminal transaction retains the decision and expected campaign head before
claim; a transient head read fails preparation before claim and retries through
the existing controller. It saves the exact official close event before publication. Interruption
keeps PASS plus the recovery duty. Replay recognizes the saved event, including
after a later begin/reopen, and cannot close later work with the older judgment.
Scoped completion, child PASS and sealed cycles leave the goal judgment unset.
