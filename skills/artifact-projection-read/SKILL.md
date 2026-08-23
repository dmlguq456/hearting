---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: artifact-projection-read
description: "Use when an agent or operator needs to browse or query an existing Cairn artifact projection without changing artifacts or runtime state. Not for primary task routing, ingest, mutation, activation, deactivation, apply, migration, namespace switching, or flat-browse fallback ownership."
argument-hint: "one JSON read request on stdin"
metadata:
  group: support
  fam: support
  invocation_class: model-support
  modes: []
  blurb: "Read-only lookup of Cairn artifact projections through the canonical W3a contract."
  use_when: "Use when an agent or operator needs to browse or query an existing Cairn artifact projection without changing artifacts or runtime state."
  not_for: "Not for primary task routing, ingest, mutation, activation, deactivation, apply, migration, namespace switching, or flat-browse fallback ownership."
---

# artifact-projection-read

Use `utilities/cairn-artifact-read.sh` for a read-only Cairn artifact
projection query. Provide exactly one JSON object on stdin, set `CAIRN_ROOT`
to the W3a checkout at commit `1fa0d99e4b714b5ce305f78c8f7c7773255e8f87`, and
provide read-only `CAIRN_READ_ENDPOINT` and optional `CAIRN_READ_TOKEN` through
the environment. The bridge passes request and cursor semantics to Cairn's
`ArtifactProjectionClient`; it emits one JSON value on stdout and diagnostics
only on stderr. Use flat browse, `rg`, or memory `artifact-pointer` as the
existing fallback when this projection is unavailable. Never place tokens,
database URLs, or credentials in requests or prose.
