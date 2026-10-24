# Config provenance

Config lifecycle is explicit: adopted defaults live in `configs/`, new or
unadopted experiment settings in `configs_exp/<slug>/`, and historical
model/checkpoint reproductions in `configs_legacy/`. A `.lab-config-layout.json`
root declaration (`{"schema_version": 1, "layout": "<name>", "roots":
{"default": "<dir>", "exp": "<dir>", "legacy": "<dir>"}}`) remaps `resolve`'s
physical roots, not just the recorded `config_layout` label — bare, `config:`,
`exp:`, and `legacy:` references then resolve against the declared
directories, with longest-match attribution when roots nest — except that an
explicitly prefixed `config:`/`exp:`/`legacy:` reference must canonicalize
into its own namespace; a nested-root crossover is rejected, and only an
explicit physical path can still reach a nested root. A plain-text
`.lab-config-layout` file or `experiment_conventions.md` label only sets the
label; `resolve` always exposes the actually-used `roots` and
`layout_declaration` so a label-only declaration is never silently assumed to
have remapped anything.

Before a full run, resolve a config without root fallback and seal it with
`tools/lab-config-provenance.py`. Routed commands take the repository and
artifact context from dispatch; standalone commands keep explicit `--repo`
and `--artifact-root` arguments. The seal copies the exact bytes into the
selected node's declared config-support subtree and writes a manifest
containing `schema_version` (2), `config_ref`, `run_id`, `source_path`,
`source_sha256`, `source_commit`, `source_dirty`, `source_git_state`,
`snapshot_path`, and `snapshot_sha256`. `verify` is fail-closed; retries with
the same inputs are idempotent. Case-insensitive filesystems are an explicit
non-goal (Linux-only harness).

Example:

```sh
python3 tools/lab-config-provenance.py resolve --ref exp:demo/train.yaml
python3 tools/lab-config-provenance.py run-id --slug demo --config-ref exp:demo/train.yaml --config-sha256 HASH
python3 tools/lab-config-provenance.py seal --config exp:demo/train.yaml --slug demo
```

For a standalone invocation, pass `--repo` and `--artifact-root` explicitly.

`--run-id` is optional on `seal` — omit it to have the tool emit the computed
run ID; if given, it must match the computed value exactly. `run-id` requires
`--config-sha256` (64 lowercase hex characters) and applies the same
safe-slug and canonical-ref rules `seal` applies; `--attempt`, if given, must
be a positive integer. A missing `--config-sha256` is the one rejection in
the whole provenance CLI that argparse raises directly (exit 2) — every other
rejection across `resolve`/`seal`/`verify`/`run-id` (unsafe slug, bad ref
grammar, malformed hash, non-positive attempt, and so on) is exit 65.

Pass the resulting manifest to smoke attestation and the registered resource
runner. Evaluation uses that snapshot or manifest, never a checkpoint
directory name. Existing runs remain unchanged; record an
`existing_run_exception` for an explicit compatibility handoff. Promotion is
a recommendation to the code/spec owner and never overwrites `configs/`
without user approval.

**Limits (by design):** attestation requires the config source file to exist
at attest time — a manifest whose source was later deleted stays
`verify`-valid and snapshot-reproducible, but cannot back a *new*
attestation, since the smoke gate is meant to guarantee the bytes a run will
actually read. A sealed manifest is not portable on its own: `verify`
re-proves the sealed identity, not just field shapes. It requires the full
`experiments/<slug>/_internal/configs` directory chain (not just the
hash-named snapshot beside the manifest), the exact `<run_id>.manifest.json`
filename, and that the slug recovered from that chain, together with
`config_ref` and `source_sha256`, recomputes the same `run_id`. Move the
manifest together with its snapshot *and* keep the `experiments/<slug>`
parents intact. The same suffix is accepted beneath the routed config-support
subtree. Verification accepts historical top-level manifests and preserves the
manifest's recorded path and bytes.

The smoke attestation now binds both the config snapshot and its source:
`payload()` verifies the config manifest against the full `verify` contract
before running the command, and adds top-level `config_sha256`,
`config_source_sha256`, and `config_source_path` alongside snapshot and source
rows in `inputs`. `verify()` requires an input row whose path matches
`config_source_path` and whose digest matches `config_source_sha256` — a
snapshot-only input can no longer satisfy that check, since source and
snapshot bytes are identical by construction and a hash-only check would be
vacuous. `verify()` requires `attestation_hash` to be present, and the three
config-provenance fields are all-or-none — partial config metadata is
rejected outright. The snapshot row itself must be proven by a distinct
input row carrying the config hash, unless the source's own real bytes
already are the claimed snapshot bytes
(`config_source_sha256 == config_sha256`); this closes hash/path binding but
does **not** create a genuine binding to the snapshot's own *path* — that
still only happens at `utilities/resource-runner.py start`, which
cross-checks the attestation against the sealed manifest. The actual
enforcement point for a `--config-manifest` launch whose run ID, config
hash, or source path disagrees with the sealed manifest is
`utilities/resource-runner.py start`, which rejects the launch (exit 65)
before the log, process, or registry row are created.
