# route/ fixtures (F-28a, plan §3.5)

- `real_claude_staged.json` — verbatim copy of the real record compiled for THIS execute cycle
  (`/home/alice/agent_setting/.dispatch/logs/fleet-v10-process-view.route.json`), route_id
  `rt-9fa0fed86699b8f5`.
- `real_codex_staged.json` — verbatim copy of a real codex-runtime record
  (`/tmp/sample-note-d1-route.json`, since evicted — /tmp volatility is the point of the
  fixture), route_id `rt-f942824768304759`.
- `synth_broken_hash.json` — `real_claude_staged.json` with `capability` mutated by one
  character (`autopilot-code` → `autopilot-codf`), `route_hash`/`route_id` left untouched (now
  stale) — exercises `load()`'s hash-recheck rejection (T1-5).
- `synth_bad_schema.json` — `real_claude_staged.json` with `schema_version: 2`; `route_hash`/
  `route_id` were RECOMPUTED (via `route.route_hash()`) so the row is internally consistent —
  the only thing that must reject it is the schema-version guard (T1-6).
- `synth_parallel_lab.json` — synthetic (no real counterpart): `autopilot-lab/eval` topology,
  `setup → {eval-asr, eval-sep, eval-vad} → aggregate → report` (3-way fan-out + fan-in).
  `route_hash`/`route_id` generated with `route.route_hash()` (see the one-off script below) —
  never hand-write a hash, `load()` rejects it immediately.
- `jobs_route.log` — 4 real jobs.log rows (verbatim field values, tab-separated, per the
  writer's own vocabulary), with `route_file=` for two of them rewritten to the `{FIXDIR}`
  placeholder (tests `.format(FIXDIR=<tmpdir>)` before use, then drop this fixture dir's own
  copies of `real_claude_staged.json`/`real_codex_staged.json` alongside it) — the other two
  keep their real (now-gone) `/tmp` paths verbatim, proving the tolerant-fallback path.

Regeneration one-liner for the synth files (route_hash/route_id must always come from the
function, never hand-written):

```python
import json, sys; sys.path.insert(0, "tools")
from fleet import route as routemod
payload = {...}  # schema_version 1 dict, no route_hash/route_id yet
digest = routemod.route_hash(payload)
payload["route_hash"] = digest
payload["route_id"] = "rt-" + digest.split(":", 1)[1][:16]
```
# v16 composed DAG fixture

`synth_composed_survey.json` is a sealed schema-v2 fixture generated through the
repository compose path. It intentionally exercises `survey -> {claim-a, claim-b}
-> synth`, opaque node IDs, fan-out/fan-in, and per-node `write_scope`.

Fixture generation (uses the checked dispatch-evidence JSON supplied by the
compose test; the output path must not already exist):

```bash
python3 utilities/compose-route.py --capability analyze-project --capability-mode code \
  --units-json '[{"id":"survey","unit":"research/research-survey","write_scope":["analysis_project/code/**"],"gate":"research-retrieval"},{"id":"claim-a","unit":"research/claim-verify","depends_on":["survey"],"write_scope":["reviews/claim-a/**"],"gate":"research-claims"},{"id":"claim-b","unit":"research/claim-verify","depends_on":["survey"],"write_scope":["reviews/claim-b/**"],"gate":"research-claims"},{"id":"synth","unit":"research/research-survey","depends_on":["claim-a","claim-b"],"write_scope":["analysis_project/synthesis/**"],"gate":"research-synthesis"}]' \
  --cwd "$PWD" --artifact-root /tmp --tracking tracked --spec-read canonical-sha \
  --drift-verdict within-spec --workflow-mode tracked \
  --artifact-guard conductor-prechecked --dispatch-evidence /path/to/fixture-evidence.json \
  --output /tmp/synth_composed_survey.json
```

Capability-route verification of the generated sealed record:

```bash
python3 utilities/capability-route.py verify --route /tmp/synth_composed_survey.json --cwd "$PWD"
```

# continuation_round fixture

`continuation_round/` is a sanitized, resealed two-generation copy of the real
continuation records `rt-38a493e894b5202a` and `rt-bf652786c0a99795` captured on
2026-09-10. It replaces paths with `/fixture/...`, uses `att-fixture-*` attempt
ids, and keeps only the route/registry fields needed for semantic-round and
owner-lineage checks. `registry.tsv` uses `{FIXTURE_DIR}` so tests can materialize
the paths in a temporary directory.

Regenerate hashes after changing a payload with:

```sh
python3 -c 'import json,sys;sys.path.insert(0,"tools");from fleet import route; d=json.load(open(sys.argv[1])); h=route.route_hash(d); print(h, "rt-"+h.split(":",1)[1][:16])' gen0.json
```
