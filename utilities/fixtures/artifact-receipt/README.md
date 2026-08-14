# `artifact-receipt` fixtures

Decision fixtures for `utilities/artifact_receipt.test.py` (D-12 receipt decoder).

- `golden.v1.json` — exact legacy executable receipt v1 (7 keys), golden accept input.
- `cases.json` — decode-stage (R0–R3) negative corpus plus the R2a `key-set-mismatch`
  fallback branch, tagged with `ladder_branch` per `plan.md` §3.2.
- `lineage-cases.json` — v3 local-resolution (`resolve()`, S1–S15) negative corpus,
  applied as mutations against a runtime-admitted golden v3 receipt.

## Why no v2 golden fixture lives here

The exact IDs-only v2 golden accept input is
`capabilities/report-bundle-receipt.v2.example.json` itself — that file is the
primary evidence for the v2 receipt shape and is byte-invariant (OD-10). This
directory does not clone it; the test suite reads that file directly so the
fixture and the byte-invariance target can never drift apart.

## Why no v3 golden fixture is a static file

A v3 receipt is only valid relative to a locally registered manifest: the
seven IDs must resolve against an admitted `index.json` and a published
`manifest.json` under a real artifact root (`resolve()`, S1–S15). A static
JSON file cannot carry that relationship, so the golden v3 accept case is
built at test time by admitting a fixture manifest into a temp artifact root
with step-1's `artifact_admission.admit()` and then constructing the receipt
from the IDs that admission returned.
